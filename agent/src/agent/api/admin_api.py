from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, StrictBool, StringConstraints

from agent import __version__
from agent.application.evaluation import RagEvaluationCase
from agent.application.models import DependencyUnavailable, InvalidRequest, ResourceNotFound
from agent.settings import Settings

_DOCUMENT_STATUSES = {
    "UPLOADED",
    "PARSING",
    "CHUNKING",
    "EMBEDDING",
    "INDEXING",
    "READY",
    "FAILED",
    "DISABLED",
}


class DocumentStateRequest(BaseModel):
    active: StrictBool


class BatchReindexRequest(BaseModel):
    knowledge_base_id: str | None = None
    only_missing_vectors: StrictBool = True


EvaluationQuestion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]
EvaluationExpectation = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]


class RagEvaluationCaseRequest(BaseModel):
    question: EvaluationQuestion
    expected_keywords: list[EvaluationExpectation] = Field(default_factory=list, max_length=20)
    expected_sources: list[EvaluationExpectation] = Field(default_factory=list, max_length=20)
    expect_refusal: StrictBool = False


class RagEvaluationRequest(BaseModel):
    user_id: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=20)
    cases: list[RagEvaluationCaseRequest] = Field(min_length=1, max_length=20)


def create_admin_dependency(settings: Settings) -> Callable[..., None]:
    def require_admin(
        internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
    ) -> None:
        if settings.admin_token is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "admin console is disabled",
            )
        if internal_token is None or not secrets.compare_digest(
            internal_token, settings.admin_token
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid internal token")

    return require_admin


def create_admin_router(
    settings: Settings,
    require_admin: Callable[..., None],
) -> APIRouter:
    router = APIRouter(
        prefix="/internal/v1/admin",
        tags=["admin"],
        dependencies=[Depends(require_admin)],
    )

    @router.get("/session")
    async def session() -> dict[str, object]:
        return {
            "authenticated": True,
            "tenant_id": settings.tenant_id,
            "version": __version__,
            "max_upload_bytes": settings.max_upload_bytes,
            "supported_extensions": [
                "pdf",
                "docx",
                "xlsx",
                "md",
                "markdown",
                "txt",
                "csv",
            ],
        }

    @router.get("/overview")
    async def overview(request: Request) -> dict[str, object]:
        return await request.app.state.repository.get_admin_overview(settings.tenant_id)

    @router.get("/knowledge-bases")
    async def knowledge_bases(request: Request) -> list[dict[str, object]]:
        return await request.app.state.repository.list_knowledge_bases(settings.tenant_id)

    @router.get("/models/status")
    async def model_status(request: Request) -> dict[str, object]:
        llm_status, embedding_status = await asyncio.gather(
            _probe_llm(request.app.state.llm),
            _probe_embedding(
                request.app.state.embedding,
                settings.embedding_dimension,
            ),
        )
        return {"llm": llm_status, "embedding": embedding_status}

    @router.post("/rag/evaluate")
    async def evaluate_rag(
        request: Request,
        payload: RagEvaluationRequest,
    ) -> dict[str, object]:
        if payload.user_id is not None:
            _validate_uuid(payload.user_id, "user_id")
        for knowledge_base_id in payload.knowledge_base_ids:
            _validate_uuid(knowledge_base_id, "knowledge_base_id")
        cases = tuple(
            RagEvaluationCase(
                question=item.question,
                expected_keywords=tuple(dict.fromkeys(item.expected_keywords)),
                expected_sources=tuple(dict.fromkeys(item.expected_sources)),
                expect_refusal=item.expect_refusal,
            )
            for item in payload.cases
        )
        return await request.app.state.rag_evaluation.evaluate(
            tenant_id=settings.tenant_id,
            user_id=payload.user_id,
            knowledge_base_ids=tuple(dict.fromkeys(payload.knowledge_base_ids)),
            cases=cases,
        )

    @router.get("/reindex/jobs")
    async def reindex_jobs(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, object]:
        return await request.app.state.repository.list_reindex_jobs(
            settings.tenant_id,
            limit,
        )

    @router.post("/reindex", status_code=status.HTTP_202_ACCEPTED)
    async def enqueue_batch_reindex(
        request: Request,
        payload: BatchReindexRequest,
    ) -> dict[str, object]:
        if payload.knowledge_base_id is not None:
            _validate_uuid(payload.knowledge_base_id, "knowledge_base_id")
        try:
            return await request.app.state.repository.enqueue_reindex_jobs(
                settings.tenant_id,
                payload.knowledge_base_id,
                payload.only_missing_vectors,
            )
        except ResourceNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    @router.get("/documents")
    async def documents(
        request: Request,
        knowledge_base_id: Annotated[str | None, Query()] = None,
        document_status: Annotated[str | None, Query(alias="status")] = None,
        search: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, object]:
        if knowledge_base_id is not None:
            _validate_uuid(knowledge_base_id, "knowledge_base_id")
        if document_status is not None and document_status not in _DOCUMENT_STATUSES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unsupported document status")
        return await request.app.state.repository.list_documents(
            settings.tenant_id,
            knowledge_base_id,
            document_status,
            search,
            limit,
            offset,
        )

    @router.get("/documents/{document_id}")
    async def document_detail(request: Request, document_id: str) -> dict[str, object]:
        _validate_uuid(document_id, "document_id")
        try:
            return await request.app.state.repository.get_document(settings.tenant_id, document_id)
        except ResourceNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    @router.patch("/documents/{document_id}/state")
    async def update_document_state(
        request: Request,
        document_id: str,
        payload: DocumentStateRequest,
    ) -> dict[str, str]:
        _validate_uuid(document_id, "document_id")
        try:
            next_status = await request.app.state.repository.set_document_active(
                settings.tenant_id,
                document_id,
                payload.active,
            )
        except ResourceNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except InvalidRequest as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return {"document_id": document_id, "status": next_status}

    @router.post(
        "/documents/{document_id}/reindex",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def enqueue_document_reindex(
        request: Request,
        document_id: str,
    ) -> dict[str, object]:
        _validate_uuid(document_id, "document_id")
        try:
            return await request.app.state.repository.enqueue_document_reindex(
                settings.tenant_id,
                document_id,
            )
        except ResourceNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except InvalidRequest as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return router


def _validate_uuid(value: str, field: str) -> None:
    try:
        UUID(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid {field}") from exc


async def _probe_llm(client: Any) -> dict[str, object]:
    result: dict[str, object] = {
        "configured": bool(client.enabled),
        "model": client.model,
    }
    if not client.enabled:
        return {**result, "status": "DISABLED", "detail": "LLM service is not configured"}
    try:
        await asyncio.wait_for(client.probe(), timeout=3)
    except TimeoutError:
        return {**result, "status": "DOWN", "detail": "LLM model probe timed out"}
    except DependencyUnavailable as exc:
        return {**result, "status": "DOWN", "detail": str(exc)}
    return {**result, "status": "UP", "detail": "model is available"}


async def _probe_embedding(client: Any, expected_dimension: int) -> dict[str, object]:
    result: dict[str, object] = {
        "configured": bool(client.enabled),
        "model": client.model,
        "expected_dimension": expected_dimension,
    }
    if not client.enabled:
        return {
            **result,
            "status": "DISABLED",
            "detail": "embedding service is not configured",
        }
    try:
        dimension = await asyncio.wait_for(client.probe(), timeout=3)
    except TimeoutError:
        return {**result, "status": "DOWN", "detail": "embedding model probe timed out"}
    except DependencyUnavailable as exc:
        return {**result, "status": "DOWN", "detail": str(exc)}
    if dimension != expected_dimension:
        return {
            **result,
            "status": "DOWN",
            "dimension": dimension,
            "detail": "embedding dimension does not match database schema",
        }
    return {
        **result,
        "status": "UP",
        "dimension": dimension,
        "detail": "model is available",
    }
