from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from agent import __version__
from agent.adapters.models import OpenAIEmbeddingClient, OpenAILLMClient
from agent.adapters.object_store import MinioObjectStore
from agent.adapters.repository import PostgresRepository
from agent.api.admin_api import create_admin_dependency, create_admin_router
from agent.application.ingestion import DocumentIngestionService
from agent.application.models import DependencyUnavailable, InvalidRequest, ResourceNotFound
from agent.application.reindex import DocumentReindexWorker
from agent.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    actual_settings = settings or get_settings()
    require_admin = create_admin_dependency(actual_settings)
    web_root = Path(__file__).resolve().parents[1] / "web"

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
        llm = OpenAILLMClient(
            actual_settings.llm_base_url,
            actual_settings.llm_api_key,
            actual_settings.llm_model,
            actual_settings.llm_timeout_seconds,
        )
        app.state.repository = repository
        app.state.object_store = object_store
        app.state.embedding = embedding
        app.state.llm = llm
        app.state.ingestion = DocumentIngestionService(
            repository, object_store, embedding, actual_settings
        )
        reindex_worker = DocumentReindexWorker(
            repository,
            embedding,
            embedding_dimension=actual_settings.embedding_dimension,
            worker_id=f"agent-http-{uuid4()}",
        )
        reindex_task = asyncio.create_task(
            reindex_worker.run_forever(),
            name="document-reindex-worker",
        )
        try:
            yield
        finally:
            reindex_task.cancel()
            with suppress(asyncio.CancelledError):
                await reindex_task
            await llm.close()
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
    app.mount("/admin/assets", StaticFiles(directory=web_root), name="admin-assets")
    app.include_router(create_admin_router(actual_settings, require_admin))

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    async def admin_console() -> FileResponse:
        return FileResponse(
            web_root / "index.html",
            headers={"Cache-Control": "no-store"},
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

    @app.post(
        "/internal/v1/documents",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_admin)],
    )
    async def upload_document(
        request: Request,
        file: Annotated[UploadFile, File()],
        knowledge_base_id: Annotated[str, Form()],
        document_id: Annotated[str | None, Form()] = None,
        title: Annotated[str | None, Form()] = None,
        source_code: Annotated[str | None, Form()] = None,
    ) -> dict[str, object]:
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
