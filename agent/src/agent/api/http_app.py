from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse

from agent import __version__
from agent.adapters.models import OpenAIEmbeddingClient
from agent.adapters.object_store import MinioObjectStore
from agent.adapters.repository import PostgresRepository
from agent.application.ingestion import DocumentIngestionService
from agent.application.models import DependencyUnavailable, InvalidRequest, ResourceNotFound
from agent.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    actual_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repository = await PostgresRepository.connect(actual_settings.database_url)
        object_store = MinioObjectStore(
            actual_settings.minio_endpoint,
            actual_settings.minio_access_key,
            actual_settings.minio_secret_key,
            actual_settings.minio_secure,
            actual_settings.minio_bucket,
        )
        await _ensure_object_store(object_store)
        embedding = OpenAIEmbeddingClient(
            actual_settings.embedding_base_url,
            actual_settings.embedding_api_key,
            actual_settings.embedding_model,
            actual_settings.embedding_timeout_seconds,
        )
        app.state.repository = repository
        app.state.object_store = object_store
        app.state.ingestion = DocumentIngestionService(
            repository, object_store, embedding, actual_settings
        )
        try:
            yield
        finally:
            await embedding.close()
            await repository.close()

    app = FastAPI(
        title="WorkChat Agent Internal API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/internal/v1/health/live")
    async def live() -> dict[str, str]:
        return {"status": "UP", "version": __version__}

    @app.get("/internal/v1/health/ready")
    async def ready(request: Request) -> JSONResponse:
        components = {"database": "UP", "object_store": "UP"}
        try:
            await asyncio.wait_for(request.app.state.repository.ping(), timeout=0.75)
        except Exception:
            components["database"] = "DOWN"
        try:
            await asyncio.wait_for(request.app.state.object_store.ping(), timeout=0.75)
        except Exception:
            components["object_store"] = "DOWN"
        health = "UP" if all(value == "UP" for value in components.values()) else "DOWN"
        return JSONResponse(
            status_code=(
                status.HTTP_200_OK if health == "UP" else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            content={
                "status": health,
                "version": __version__,
                "components": components,
            },
        )

    @app.post("/internal/v1/documents", status_code=status.HTTP_201_CREATED)
    async def upload_document(
        request: Request,
        file: Annotated[UploadFile, File()],
        knowledge_base_id: Annotated[str, Form()],
        document_id: Annotated[str | None, Form()] = None,
        title: Annotated[str | None, Form()] = None,
        source_code: Annotated[str | None, Form()] = None,
        internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
    ) -> dict[str, object]:
        if actual_settings.admin_token is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "document upload is disabled")
        if internal_token is None or not secrets.compare_digest(
            internal_token, actual_settings.admin_token
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid internal token")
        data = await file.read(actual_settings.max_upload_bytes + 1)
        actual_title = title or Path(file.filename or "document").stem
        try:
            result = await request.app.state.ingestion.ingest(
                tenant_id=actual_settings.tenant_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                title=actual_title,
                source_code=source_code,
                file_name=file.filename or "document.bin",
                content_type=file.content_type or "application/octet-stream",
                data=data,
            )
        except InvalidRequest as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except ResourceNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except DependencyUnavailable as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        return {
            "document_id": result.document_id,
            "version_number": result.version_number,
            "chunk_count": result.chunk_count,
            "index_mode": result.index_mode,
            "status": "READY",
        }

    return app


async def _ensure_object_store(object_store: MinioObjectStore) -> None:
    last_error: Exception | None = None
    for _ in range(20):
        try:
            await object_store.ensure_ready()
            return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.5)
    raise RuntimeError("object store is unavailable") from last_error
