from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256 as sha256_digest
from uuid import UUID

import asyncpg

from agent.application.models import (
    AnswerResult,
    Candidate,
    Citation,
    ClaimedRun,
    GeneratedAnswer,
    InvalidRequest,
    PermissionDenied,
    RequestConflict,
    RequestInProgress,
    ResourceNotFound,
)

_ACL_MATCH = """
(
    ae.subject_type = 'ALL_EMPLOYEES'
    OR (ae.subject_type = 'USER' AND ae.subject_id = $2::uuid)
    OR (
        ae.subject_type = 'ROLE'
        AND EXISTS (
            SELECT 1 FROM user_roles ur
            JOIN roles r ON r.id = ur.role_id AND r.tenant_id = ur.tenant_id
            WHERE ur.tenant_id = $1::uuid
              AND ur.user_id = $2::uuid
              AND ur.role_id = ae.subject_id
              AND r.status = 'ACTIVE'
        )
    )
    OR (
        ae.subject_type = 'DEPARTMENT'
        AND EXISTS (
            SELECT 1
            FROM user_departments ud
            WHERE ud.tenant_id = $1::uuid
              AND ud.user_id = $2::uuid
              AND (
                  ud.department_id = ae.subject_id
                  OR (
                      ae.include_descendants
                      AND EXISTS (
                          SELECT 1 FROM department_closure dc
                          WHERE dc.tenant_id = $1::uuid
                            AND dc.ancestor_id = ae.subject_id
                            AND dc.descendant_id = ud.department_id
                      )
                  )
              )
        )
    )
)
"""

_RETRIEVAL_FROM = f"""
WITH authorized_kb AS (
    SELECT kb.id
    FROM knowledge_bases kb
    WHERE kb.tenant_id = $1::uuid
      AND kb.status = 'ACTIVE'
      AND ($3::uuid[] IS NULL OR kb.id = ANY($3::uuid[]))
      AND EXISTS (
          SELECT 1
          FROM acl_entries ae
          WHERE ae.tenant_id = kb.tenant_id
            AND ae.resource_type = 'KNOWLEDGE_BASE'
            AND ae.resource_id = kb.id
            AND {_ACL_MATCH}
      )
)
SELECT
    c.id::text AS chunk_id,
    c.document_id::text,
    c.document_version_id::text,
    dv.version_number AS document_version_number,
    d.title,
    c.content,
    c.content_hash,
    c.locator_type,
    c.locator_value,
    dv.effective_at,
    {{score_expression}} AS score
FROM chunks c
JOIN authorized_kb ak ON ak.id = c.knowledge_base_id
JOIN knowledge_bases kb
  ON kb.id = c.knowledge_base_id
 AND kb.tenant_id = c.tenant_id
JOIN documents d
  ON d.id = c.document_id
 AND d.tenant_id = c.tenant_id
JOIN document_versions dv
  ON dv.id = c.document_version_id
 AND dv.tenant_id = c.tenant_id
WHERE c.tenant_id = $1::uuid
  AND c.is_active
  AND c.index_version = kb.active_index_version
  AND d.status = 'READY'
  AND d.current_version_id = dv.id
  AND dv.is_current
  AND (dv.effective_at IS NULL OR dv.effective_at <= now())
  AND (dv.expires_at IS NULL OR dv.expires_at > now())
  AND (
      d.acl_mode = 'INHERIT'
      OR EXISTS (
          SELECT 1
          FROM acl_entries ae
          WHERE ae.tenant_id = d.tenant_id
            AND ae.resource_type = 'DOCUMENT'
            AND ae.resource_id = d.id
            AND {_ACL_MATCH}
      )
  )
  {{search_condition}}
ORDER BY {{order_expression}}
LIMIT $5
"""

_KEYWORD_SQL = _RETRIEVAL_FROM.format(
    score_expression="""
        (
            SELECT COALESCE(sum(char_length(term)), 0)::double precision
            FROM unnest($4::text[]) AS term
            WHERE strpos(lower(c.content), lower(term)) > 0
        )
    """,
    search_condition="""
        AND EXISTS (
            SELECT 1
            FROM unnest($4::text[]) AS term
            WHERE strpos(lower(c.content), lower(term)) > 0
        )
    """,
    order_expression="score DESC, c.id",
)

_VECTOR_SQL = _RETRIEVAL_FROM.format(
    score_expression="1 - (c.embedding <=> $4::vector)",
    search_condition="AND c.embedding IS NOT NULL",
    order_expression="c.embedding <=> $4::vector, c.id",
)


class PostgresRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> PostgresRepository:
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=10, command_timeout=5)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def ping(self) -> None:
        await self._pool.execute("SELECT 1")

    async def get_admin_overview(self, tenant_id: str) -> dict[str, object]:
        row = await self._pool.fetchrow(
            """
            SELECT
                t.name AS tenant_name,
                (SELECT count(*) FROM knowledge_bases kb
                 WHERE kb.tenant_id = t.id AND kb.status = 'ACTIVE') AS knowledge_base_count,
                (SELECT count(*) FROM documents d
                 WHERE d.tenant_id = t.id AND d.status <> 'DELETED') AS document_count,
                (SELECT count(*) FROM documents d
                 WHERE d.tenant_id = t.id AND d.status = 'READY') AS ready_document_count,
                (SELECT count(*) FROM documents d
                 WHERE d.tenant_id = t.id AND d.status = 'DISABLED') AS disabled_document_count,
                (SELECT count(*) FROM chunks c
                 WHERE c.tenant_id = t.id AND c.is_active) AS active_chunk_count,
                (SELECT count(*) FROM chunks c
                 WHERE c.tenant_id = t.id AND c.is_active
                   AND c.embedding IS NOT NULL) AS vectorized_chunk_count,
                (SELECT COALESCE(sum(dv.file_size), 0) FROM document_versions dv
                 WHERE dv.tenant_id = t.id) AS storage_bytes,
                (SELECT count(*) FROM query_runs qr
                 WHERE qr.tenant_id = t.id
                   AND qr.created_at >= now() - interval '24 hours') AS questions_24h
            FROM tenants t
            WHERE t.id = $1::uuid
            """,
            tenant_id,
        )
        if row is None:
            raise ResourceNotFound("tenant not found")
        return {key: row[key] for key in row.keys()}

    async def list_knowledge_bases(self, tenant_id: str) -> list[dict[str, object]]:
        rows = await self._pool.fetch(
            """
            SELECT
                kb.id::text,
                kb.code,
                kb.name,
                kb.description,
                kb.status,
                kb.active_index_version,
                kb.created_at,
                count(DISTINCT d.id) FILTER (WHERE d.status <> 'DELETED') AS document_count,
                count(DISTINCT d.id) FILTER (WHERE d.status = 'READY') AS ready_document_count,
                count(DISTINCT c.id) FILTER (WHERE c.is_active) AS active_chunk_count
            FROM knowledge_bases kb
            LEFT JOIN documents d
              ON d.tenant_id = kb.tenant_id
             AND d.knowledge_base_id = kb.id
            LEFT JOIN chunks c
              ON c.tenant_id = kb.tenant_id
             AND c.knowledge_base_id = kb.id
             AND c.document_id = d.id
            WHERE kb.tenant_id = $1::uuid
            GROUP BY kb.id
            ORDER BY kb.status, kb.name, kb.id
            """,
            tenant_id,
        )
        return [{key: row[key] for key in row.keys()} for row in rows]

    async def list_documents(
        self,
        tenant_id: str,
        knowledge_base_id: str | None,
        document_status: str | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, object]:
        normalized_search = search.strip() if search and search.strip() else None
        filters = """
            d.tenant_id = $1::uuid
            AND d.status <> 'DELETED'
            AND ($2::uuid IS NULL OR d.knowledge_base_id = $2::uuid)
            AND ($3::text IS NULL OR d.status = $3)
            AND (
                $4::text IS NULL
                OR d.title ILIKE '%' || $4 || '%'
                OR COALESCE(d.source_code, '') ILIKE '%' || $4 || '%'
                OR COALESCE(dv.file_name, '') ILIKE '%' || $4 || '%'
            )
        """
        total = await self._pool.fetchval(
            f"""
            SELECT count(*)
            FROM documents d
            LEFT JOIN document_versions dv
              ON dv.id = d.current_version_id AND dv.tenant_id = d.tenant_id
            WHERE {filters}
            """,
            tenant_id,
            knowledge_base_id,
            document_status,
            normalized_search,
        )
        rows = await self._pool.fetch(
            f"""
            SELECT
                d.id::text,
                d.title,
                d.source_code,
                d.classification_code,
                d.acl_mode,
                d.status,
                d.created_at,
                d.updated_at,
                kb.id::text AS knowledge_base_id,
                kb.name AS knowledge_base_name,
                dv.version_number,
                dv.file_name,
                dv.file_size,
                dv.index_status,
                dv.index_version,
                dv.indexed_at,
                (SELECT count(*) FROM chunks c
                 WHERE c.tenant_id = d.tenant_id
                   AND c.document_version_id = d.current_version_id) AS chunk_count,
                (SELECT count(*) FROM chunks c
                 WHERE c.tenant_id = d.tenant_id
                   AND c.document_version_id = d.current_version_id
                   AND c.embedding IS NOT NULL) AS vectorized_chunk_count
            FROM documents d
            JOIN knowledge_bases kb
              ON kb.id = d.knowledge_base_id AND kb.tenant_id = d.tenant_id
            LEFT JOIN document_versions dv
              ON dv.id = d.current_version_id AND dv.tenant_id = d.tenant_id
            WHERE {filters}
            ORDER BY d.updated_at DESC, d.id
            LIMIT $5 OFFSET $6
            """,
            tenant_id,
            knowledge_base_id,
            document_status,
            normalized_search,
            limit,
            offset,
        )
        return {
            "items": [{key: row[key] for key in row.keys()} for row in rows],
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
        }

    async def get_document(self, tenant_id: str, document_id: str) -> dict[str, object]:
        document = await self._pool.fetchrow(
            """
            SELECT
                d.id::text,
                d.title,
                d.source_code,
                d.classification_code,
                d.acl_mode,
                d.status,
                d.created_at,
                d.updated_at,
                d.current_version_id::text,
                kb.id::text AS knowledge_base_id,
                kb.name AS knowledge_base_name,
                kb.active_index_version,
                (SELECT count(*) FROM citations ci
                 WHERE ci.tenant_id = d.tenant_id
                   AND ci.document_id = d.id) AS citation_count
            FROM documents d
            JOIN knowledge_bases kb
              ON kb.id = d.knowledge_base_id AND kb.tenant_id = d.tenant_id
            WHERE d.tenant_id = $1::uuid AND d.id = $2::uuid
              AND d.status <> 'DELETED'
            """,
            tenant_id,
            document_id,
        )
        if document is None:
            raise ResourceNotFound("document not found")
        versions = await self._pool.fetch(
            """
            SELECT
                dv.id::text,
                dv.version_number,
                dv.file_name,
                dv.file_size,
                dv.sha256,
                dv.index_status,
                dv.index_version,
                dv.is_current,
                dv.created_at,
                dv.indexed_at,
                (SELECT count(*) FROM chunks c
                 WHERE c.tenant_id = dv.tenant_id
                   AND c.document_version_id = dv.id) AS chunk_count,
                (SELECT count(*) FROM chunks c
                 WHERE c.tenant_id = dv.tenant_id
                   AND c.document_version_id = dv.id
                   AND c.embedding IS NOT NULL) AS vectorized_chunk_count
            FROM document_versions dv
            WHERE dv.tenant_id = $1::uuid AND dv.document_id = $2::uuid
            ORDER BY dv.version_number DESC
            """,
            tenant_id,
            document_id,
        )
        return {
            **{key: document[key] for key in document.keys()},
            "versions": [{key: row[key] for key in row.keys()} for row in versions],
        }

    async def set_document_active(
        self,
        tenant_id: str,
        document_id: str,
        active: bool,
    ) -> str:
        async with self._pool.acquire() as connection, connection.transaction():
            document = await connection.fetchrow(
                """
                SELECT
                    d.status,
                    d.current_version_id::text,
                    dv.index_version,
                    kb.active_index_version
                FROM documents d
                JOIN knowledge_bases kb
                  ON kb.id = d.knowledge_base_id AND kb.tenant_id = d.tenant_id
                LEFT JOIN document_versions dv
                  ON dv.id = d.current_version_id AND dv.tenant_id = d.tenant_id
                WHERE d.tenant_id = $1::uuid AND d.id = $2::uuid
                FOR UPDATE OF d
                """,
                tenant_id,
                document_id,
            )
            if document is None or document["status"] == "DELETED":
                raise ResourceNotFound("document not found")
            if active:
                if document["current_version_id"] is None:
                    raise InvalidRequest("document has no indexed version")
                if document["index_version"] != document["active_index_version"]:
                    raise InvalidRequest("document must be reindexed before activation")
                await connection.execute(
                    """
                    UPDATE chunks
                    SET is_active = (document_version_id = $3::uuid)
                    WHERE tenant_id = $1::uuid AND document_id = $2::uuid
                    """,
                    tenant_id,
                    document_id,
                    document["current_version_id"],
                )
                next_status = "READY"
            else:
                await connection.execute(
                    """
                    UPDATE chunks
                    SET is_active = false
                    WHERE tenant_id = $1::uuid AND document_id = $2::uuid
                    """,
                    tenant_id,
                    document_id,
                )
                next_status = "DISABLED"
            await connection.execute(
                """
                UPDATE documents
                SET status = $3, updated_at = now()
                WHERE tenant_id = $1::uuid AND id = $2::uuid
                """,
                tenant_id,
                document_id,
                next_status,
            )
            return next_status

    async def claim_query(
        self,
        *,
        tenant_id: str,
        request_id: str,
        fingerprint: str,
        user_id: str,
        conversation_id: str | None,
        question: str,
        channel: str,
    ) -> ClaimedRun:
        async with self._pool.acquire() as connection, connection.transaction():
            user = await connection.fetchrow(
                "SELECT status FROM users WHERE tenant_id = $1::uuid AND id = $2::uuid",
                tenant_id,
                user_id,
            )
            if user is None:
                raise ResourceNotFound("user not found")
            if user["status"] != "ACTIVE":
                raise PermissionDenied("user is disabled")

            if conversation_id:
                conversation = await connection.fetchrow(
                    """
                    SELECT id::text
                    FROM conversations
                    WHERE tenant_id = $1::uuid AND id = $2::uuid
                      AND user_id = $3::uuid AND status = 'ACTIVE'
                    """,
                    tenant_id,
                    conversation_id,
                    user_id,
                )
                if conversation is None:
                    raise PermissionDenied("conversation is not available")
                actual_conversation_id = conversation["id"]
            else:
                actual_conversation_id = await connection.fetchval(
                    """
                    INSERT INTO conversations (tenant_id, user_id, channel)
                    VALUES ($1::uuid, $2::uuid, $3)
                    ON CONFLICT (tenant_id, user_id, channel)
                    DO UPDATE SET updated_at = now(), status = 'ACTIVE'
                    RETURNING id::text
                    """,
                    tenant_id,
                    user_id,
                    channel,
                )

            existing = await connection.fetchrow(
                """
                SELECT request_fingerprint, status, result_message_id::text, updated_at
                FROM query_runs
                WHERE tenant_id = $1::uuid AND request_id = $2
                FOR UPDATE
                """,
                tenant_id,
                request_id,
            )
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise RequestConflict("request ID was reused with different input")
                if existing["status"] in {"COMPLETED", "COMPLETED_WITH_REFUSAL"}:
                    result = await self._load_answer(
                        connection, tenant_id, existing["result_message_id"]
                    )
                    return ClaimedRun(actual_conversation_id, result)
                age_seconds = (datetime.now(UTC) - existing["updated_at"]).total_seconds()
                if existing["status"] == "IN_PROGRESS" and age_seconds < 30:
                    raise RequestInProgress("request is already in progress")
                if existing["status"] == "FINAL_FAILED":
                    raise RequestConflict("request has already failed permanently")
                await connection.execute(
                    """
                    UPDATE query_runs
                    SET status = 'IN_PROGRESS', attempt = attempt + 1,
                        error_code = NULL, updated_at = now(), conversation_id = $3::uuid
                    WHERE tenant_id = $1::uuid AND request_id = $2
                    """,
                    tenant_id,
                    request_id,
                    actual_conversation_id,
                )
            else:
                await connection.execute(
                    """
                    INSERT INTO query_runs (
                        tenant_id, request_id, request_fingerprint, user_id,
                        conversation_id, status
                    ) VALUES ($1::uuid, $2, $3, $4::uuid, $5::uuid, 'IN_PROGRESS')
                    """,
                    tenant_id,
                    request_id,
                    fingerprint,
                    user_id,
                    actual_conversation_id,
                )

            await connection.execute(
                """
                INSERT INTO messages (
                    tenant_id, conversation_id, user_id, request_id, role, content
                ) VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'USER', $5)
                ON CONFLICT (tenant_id, request_id, role) DO NOTHING
                """,
                tenant_id,
                actual_conversation_id,
                user_id,
                request_id,
                question,
            )
            return ClaimedRun(actual_conversation_id)

    async def search_keyword(
        self,
        tenant_id: str,
        user_id: str,
        knowledge_base_ids: Sequence[str],
        question: str,
        limit: int,
    ) -> list[Candidate]:
        requested = [UUID(value) for value in knowledge_base_ids] or None
        rows = await self._pool.fetch(
            _KEYWORD_SQL,
            tenant_id,
            user_id,
            requested,
            _keyword_terms(question),
            limit,
        )
        return [_candidate_from_row(row) for row in rows]

    async def search_vector(
        self,
        tenant_id: str,
        user_id: str,
        knowledge_base_ids: Sequence[str],
        embedding: Sequence[float],
        limit: int,
    ) -> list[Candidate]:
        requested = [UUID(value) for value in knowledge_base_ids] or None
        vector = "[" + ",".join(format(float(value), ".9g") for value in embedding) + "]"
        rows = await self._pool.fetch(
            _VECTOR_SQL,
            tenant_id,
            user_id,
            requested,
            vector,
            limit,
        )
        return [_candidate_from_row(row) for row in rows]

    async def persist_answer(
        self,
        *,
        tenant_id: str,
        request_id: str,
        user_id: str,
        conversation_id: str,
        generated: GeneratedAnswer,
        candidates: Sequence[Candidate],
        model_name: str | None,
        prompt_version: str,
        retrieval_config_version: str,
    ) -> AnswerResult:
        async with self._pool.acquire() as connection, connection.transaction():
            message = await connection.fetchrow(
                """
                INSERT INTO messages (
                    tenant_id, conversation_id, user_id, request_id, role, content,
                    refused, refusal_reason, model_name, prompt_version,
                    retrieval_config_version
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4, 'ASSISTANT', $5,
                    $6, NULLIF($7, ''), $8, $9, $10
                )
                RETURNING id::text, created_at
                """,
                tenant_id,
                conversation_id,
                user_id,
                request_id,
                generated.answer,
                generated.refused,
                generated.refusal_reason,
                model_name,
                prompt_version,
                retrieval_config_version,
            )
            message_id = message["id"]

            citations: list[Citation] = []
            for output_index, candidate_index in enumerate(generated.citation_indexes, start=1):
                candidate = candidates[candidate_index - 1]
                await connection.execute(
                    """
                    INSERT INTO citations (
                        tenant_id, message_id, citation_index, chunk_id, document_id,
                        document_version_id, document_version_number, title_snapshot,
                        locator_type, locator_value, effective_at, content_hash
                    ) VALUES (
                        $1::uuid, $2::uuid, $3, $4::uuid, $5::uuid,
                        $6::uuid, $7, $8, $9, $10, $11, $12
                    )
                    """,
                    tenant_id,
                    message_id,
                    output_index,
                    candidate.chunk_id,
                    candidate.document_id,
                    candidate.document_version_id,
                    candidate.document_version_number,
                    candidate.title,
                    candidate.locator_type,
                    candidate.locator_value,
                    candidate.effective_at,
                    candidate.content_hash,
                )
                citations.append(
                    Citation(
                        index=output_index,
                        document_id=candidate.document_id,
                        document_version=candidate.document_version_number,
                        title=candidate.title,
                        locator_type=candidate.locator_type,
                        locator_value=candidate.locator_value,
                        effective_at=candidate.effective_at,
                    )
                )

            final_status = "COMPLETED_WITH_REFUSAL" if generated.refused else "COMPLETED"
            await connection.execute(
                """
                UPDATE query_runs
                SET status = $3, result_message_id = $4::uuid, updated_at = now()
                WHERE tenant_id = $1::uuid AND request_id = $2
                """,
                tenant_id,
                request_id,
                final_status,
                message_id,
            )
            return AnswerResult(
                message_id=message_id,
                answer=generated.answer,
                citations=tuple(citations),
                refused=generated.refused,
                refusal_reason=generated.refusal_reason,
                conversation_id=conversation_id,
                created_at=message["created_at"],
            )

    async def mark_retryable_failure(self, tenant_id: str, request_id: str, code: str) -> None:
        await self._pool.execute(
            """
            UPDATE query_runs
            SET status = 'RETRYABLE_FAILED', error_code = $3, updated_at = now()
            WHERE tenant_id = $1::uuid AND request_id = $2 AND status = 'IN_PROGRESS'
            """,
            tenant_id,
            request_id,
            code,
        )

    async def save_document(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        title: str,
        source_code: str | None,
        object_key: str,
        file_name: str,
        file_size: int,
        sha256: str,
        chunks: Sequence[str],
        embeddings: Sequence[Sequence[float] | None],
    ) -> tuple[int, str]:
        if len(chunks) != len(embeddings):
            raise ValueError("chunk and embedding counts do not match")
        async with self._pool.acquire() as connection, connection.transaction():
            index_version = await connection.fetchval(
                """
                SELECT active_index_version
                FROM knowledge_bases
                WHERE tenant_id = $1::uuid AND id = $2::uuid AND status = 'ACTIVE'
                FOR UPDATE
                """,
                tenant_id,
                knowledge_base_id,
            )
            if index_version is None:
                raise ResourceNotFound("knowledge base not found")
            existing = await connection.fetchrow(
                """
                SELECT id::text, knowledge_base_id::text
                FROM documents
                WHERE tenant_id = $1::uuid AND id = $2::uuid
                FOR UPDATE
                """,
                tenant_id,
                document_id,
            )
            if existing is None:
                version_number = 1
                await connection.execute(
                    """
                    INSERT INTO documents (
                        id, tenant_id, knowledge_base_id, title, source_code, status
                    ) VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, 'INDEXING')
                    """,
                    document_id,
                    tenant_id,
                    knowledge_base_id,
                    title,
                    source_code,
                )
            else:
                if existing["knowledge_base_id"] != knowledge_base_id:
                    raise InvalidRequest("document belongs to a different knowledge base")
                version_number = await connection.fetchval(
                    """
                    SELECT COALESCE(max(version_number), 0) + 1
                    FROM document_versions
                    WHERE tenant_id = $1::uuid AND document_id = $2::uuid
                    """,
                    tenant_id,
                    document_id,
                )
                await connection.execute(
                    """
                    UPDATE documents
                    SET title = $3, source_code = $4, status = 'INDEXING', updated_at = now()
                    WHERE tenant_id = $1::uuid AND id = $2::uuid
                    """,
                    tenant_id,
                    document_id,
                    title,
                    source_code,
                )
                await connection.execute(
                    """
                    UPDATE document_versions
                    SET is_current = false
                    WHERE tenant_id = $1::uuid AND document_id = $2::uuid AND is_current
                    """,
                    tenant_id,
                    document_id,
                )
                await connection.execute(
                    """
                    UPDATE chunks
                    SET is_active = false
                    WHERE tenant_id = $1::uuid AND document_id = $2::uuid AND is_active
                    """,
                    tenant_id,
                    document_id,
                )

            await connection.execute(
                """
                INSERT INTO document_versions (
                    id, tenant_id, document_id, version_number, object_key,
                    file_name, file_size, sha256, index_status, index_version,
                    is_current, indexed_at
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4, $5,
                    $6, $7, $8, 'READY', $9, true, now()
                )
                """,
                document_version_id,
                tenant_id,
                document_id,
                version_number,
                object_key,
                file_name,
                file_size,
                sha256,
                index_version,
            )
            records = []
            for ordinal, (content, embedding) in enumerate(
                zip(chunks, embeddings, strict=True), start=1
            ):
                vector = None
                if embedding is not None:
                    vector = (
                        "[" + ",".join(format(float(value), ".9g") for value in embedding) + "]"
                    )
                records.append(
                    (
                        tenant_id,
                        knowledge_base_id,
                        document_id,
                        document_version_id,
                        index_version,
                        ordinal,
                        content,
                        sha256_digest(content.encode()).hexdigest(),
                        "CHUNK",
                        str(ordinal),
                        vector,
                    )
                )
            await connection.executemany(
                """
                INSERT INTO chunks (
                    tenant_id, knowledge_base_id, document_id, document_version_id,
                    index_version, ordinal, content, content_hash, locator_type,
                    locator_value, embedding
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                    $5, $6, $7, $8, $9, $10, $11::vector
                )
                """,
                records,
            )
            await connection.execute(
                """
                UPDATE documents
                SET current_version_id = $3::uuid, status = 'READY', updated_at = now()
                WHERE tenant_id = $1::uuid AND id = $2::uuid
                """,
                tenant_id,
                document_id,
                document_version_id,
            )
            return int(version_number), str(index_version)

    async def _load_answer(
        self,
        connection: asyncpg.Connection,
        tenant_id: str,
        message_id: str | None,
    ) -> AnswerResult:
        if message_id is None:
            raise RequestInProgress("completed run has no result message")
        message = await connection.fetchrow(
            """
            SELECT id::text, content, refused, COALESCE(refusal_reason, '') AS refusal_reason,
                   conversation_id::text, created_at
            FROM messages
            WHERE tenant_id = $1::uuid AND id = $2::uuid AND role = 'ASSISTANT'
            """,
            tenant_id,
            message_id,
        )
        if message is None:
            raise ResourceNotFound("answer message not found")
        citation_rows = await connection.fetch(
            """
            SELECT citation_index, document_id::text, document_version_number,
                   title_snapshot, locator_type, locator_value, effective_at
            FROM citations
            WHERE tenant_id = $1::uuid AND message_id = $2::uuid
            ORDER BY citation_index
            """,
            tenant_id,
            message_id,
        )
        citations = tuple(
            Citation(
                index=row["citation_index"],
                document_id=row["document_id"],
                document_version=row["document_version_number"],
                title=row["title_snapshot"],
                locator_type=row["locator_type"],
                locator_value=row["locator_value"],
                effective_at=row["effective_at"],
            )
            for row in citation_rows
        )
        return AnswerResult(
            message_id=message["id"],
            answer=message["content"],
            citations=citations,
            refused=message["refused"],
            refusal_reason=message["refusal_reason"],
            conversation_id=message["conversation_id"],
            created_at=message["created_at"],
        )


def _candidate_from_row(row: asyncpg.Record) -> Candidate:
    return Candidate(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        document_version_id=row["document_version_id"],
        document_version_number=row["document_version_number"],
        title=row["title"],
        content=row["content"],
        content_hash=row["content_hash"],
        locator_type=row["locator_type"],
        locator_value=row["locator_value"],
        effective_at=row["effective_at"],
        score=float(row["score"]),
    )


_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+")


def _keyword_terms(question: str) -> list[str]:
    terms: set[str] = set()
    normalized = question.casefold()
    for word in _WORD.findall(normalized):
        terms.add(word)
    for match in _CJK_RUN.finditer(normalized):
        run = match.group()
        terms.add(run)
        if len(run) > 2:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
            terms.update(run[index : index + 3] for index in range(len(run) - 2))
    if not terms and normalized.strip():
        terms.add(normalized.strip())
    return sorted(terms, key=lambda value: (-len(value), value))[:64]
