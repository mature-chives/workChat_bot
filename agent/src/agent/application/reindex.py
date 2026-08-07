from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agent.application.models import DependencyUnavailable, InvalidRequest, ResourceNotFound

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReindexJob:
    id: str
    tenant_id: str
    document_id: str
    attempt: int


@dataclass(frozen=True, slots=True)
class ReindexChunk:
    id: str
    content: str


@dataclass(frozen=True, slots=True)
class ReindexSource:
    tenant_id: str
    document_id: str
    document_version_id: str
    index_version: str
    chunks: tuple[ReindexChunk, ...]


class ReindexRepository(Protocol):
    async def claim_reindex_job(
        self,
        worker_id: str,
        lease_seconds: int,
    ) -> ReindexJob | None: ...

    async def get_reindex_source(
        self,
        tenant_id: str,
        document_id: str,
    ) -> ReindexSource: ...

    async def update_reindex_job_stage(self, job_id: str, stage: str) -> None: ...

    async def apply_document_embeddings(
        self,
        source: ReindexSource,
        embeddings: Sequence[Sequence[float]],
    ) -> None: ...

    async def complete_reindex_job(self, job_id: str) -> None: ...

    async def fail_reindex_job(
        self,
        job_id: str,
        error_code: str,
        error_message: str,
        retry_after_seconds: int | None,
    ) -> None: ...


class ReindexEmbeddingClient(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class DocumentReindexWorker:
    def __init__(
        self,
        repository: ReindexRepository,
        embedding: ReindexEmbeddingClient,
        *,
        embedding_dimension: int,
        worker_id: str,
        poll_seconds: float = 1.0,
        lease_seconds: int = 120,
        retry_seconds: int = 5,
        max_attempts: int = 3,
        batch_size: int = 16,
    ) -> None:
        self._repository = repository
        self._embedding = embedding
        self._embedding_dimension = embedding_dimension
        self._worker_id = worker_id
        self._poll_seconds = poll_seconds
        self._lease_seconds = lease_seconds
        self._retry_seconds = retry_seconds
        self._max_attempts = max_attempts
        self._batch_size = batch_size

    async def run_forever(self) -> None:
        while True:
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reindex worker poll failed")
                processed = False
            if not processed:
                await asyncio.sleep(self._poll_seconds)

    async def run_once(self) -> bool:
        job = await self._repository.claim_reindex_job(
            self._worker_id,
            self._lease_seconds,
        )
        if job is None:
            return False

        try:
            source = await self._repository.get_reindex_source(
                job.tenant_id,
                job.document_id,
            )
            embeddings = await self._build_embeddings(job.id, source.chunks)
            await self._repository.update_reindex_job_stage(job.id, "COMMITTING")
            await self._repository.apply_document_embeddings(source, embeddings)
            await self._repository.complete_reindex_job(job.id)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(
                    self._repository.fail_reindex_job(
                        job.id,
                        "WORKER_STOPPED",
                        "reindex worker stopped before completion",
                        0,
                    )
                )
            except Exception:
                logger.exception("failed to release reindex job during shutdown")
            raise
        except ResourceNotFound as exc:
            await self._repository.fail_reindex_job(
                job.id,
                "RESOURCE_NOT_FOUND",
                _safe_message(exc),
                None,
            )
        except InvalidRequest as exc:
            await self._repository.fail_reindex_job(
                job.id,
                "INVALID_DOCUMENT_STATE",
                _safe_message(exc),
                None,
            )
        except DependencyUnavailable as exc:
            await self._repository.fail_reindex_job(
                job.id,
                "EMBEDDING_UNAVAILABLE",
                _safe_message(exc),
                self._retry_seconds if job.attempt < self._max_attempts else None,
            )
        except Exception as exc:
            await self._repository.fail_reindex_job(
                job.id,
                "INTERNAL_ERROR",
                _safe_message(exc),
                self._retry_seconds if job.attempt < self._max_attempts else None,
            )
        return True

    async def _build_embeddings(
        self,
        job_id: str,
        chunks: Sequence[ReindexChunk],
    ) -> list[list[float]]:
        if not self._embedding.enabled:
            raise DependencyUnavailable("embedding service is not configured")
        if not chunks:
            raise InvalidRequest("document has no chunks to reindex")

        result: list[list[float]] = []
        total = len(chunks)
        for start in range(0, total, self._batch_size):
            end = min(start + self._batch_size, total)
            await self._repository.update_reindex_job_stage(
                job_id,
                f"EMBEDDING:{start}/{total}",
            )
            batch = await self._embedding.embed_documents(
                [chunk.content for chunk in chunks[start:end]]
            )
            if len(batch) != end - start:
                raise DependencyUnavailable("embedding response count does not match chunks")
            if any(len(vector) != self._embedding_dimension for vector in batch):
                raise DependencyUnavailable("embedding dimension does not match database schema")
            result.extend(batch)
        await self._repository.update_reindex_job_stage(
            job_id,
            f"EMBEDDING:{total}/{total}",
        )
        return result


def _safe_message(error: Exception) -> str:
    message = " ".join(str(error).split()) or error.__class__.__name__
    return message[:500]
