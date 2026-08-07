from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, StrictBool

from agent import __version__
from agent.application.models import InvalidRequest, ResourceNotFound
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

    return router


def _validate_uuid(value: str, field: str) -> None:
    try:
        UUID(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid {field}") from exc
