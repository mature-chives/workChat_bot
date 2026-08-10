from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.admin_api import create_admin_dependency, create_admin_router
from agent.api.http_app import create_app
from agent.application.models import InvalidRequest, ResourceNotFound
from agent.settings import Settings

TENANT_ID = "00000000-0000-0000-0000-000000000001"
USER_ID = "00000000-0000-0000-0000-000000000011"
KNOWLEDGE_BASE_ID = "00000000-0000-0000-0000-000000000101"
DOCUMENT_ID = "00000000-0000-0000-0000-000000000201"
MISSING_DOCUMENT_ID = "00000000-0000-0000-0000-000000000404"
CONFLICT_DOCUMENT_ID = "00000000-0000-0000-0000-000000000409"
ADMIN_TOKEN = "test-admin-token"


class FakeRepository:
    def __init__(self) -> None:
        self.list_arguments: tuple[object, ...] | None = None
        self.state_arguments: tuple[object, ...] | None = None
        self.reindex_arguments: tuple[object, ...] | None = None
        self.batch_reindex_arguments: tuple[object, ...] | None = None

    async def get_admin_overview(self, tenant_id: str) -> dict[str, object]:
        assert tenant_id == TENANT_ID
        return {
            "tenant_name": "测试企业",
            "knowledge_base_count": 1,
            "document_count": 2,
            "ready_document_count": 1,
            "disabled_document_count": 1,
            "active_chunk_count": 8,
            "vectorized_chunk_count": 6,
            "storage_bytes": 1024,
            "questions_24h": 3,
        }

    async def list_knowledge_bases(self, tenant_id: str) -> list[dict[str, object]]:
        assert tenant_id == TENANT_ID
        return [
            {
                "id": KNOWLEDGE_BASE_ID,
                "code": "company-public",
                "name": "企业公共知识库",
                "status": "ACTIVE",
                "document_count": 2,
                "ready_document_count": 1,
                "active_chunk_count": 8,
            }
        ]

    async def list_documents(self, *args: object) -> dict[str, object]:
        self.list_arguments = args
        return {
            "items": [
                {
                    "id": DOCUMENT_ID,
                    "title": "报销制度",
                    "status": "READY",
                    "knowledge_base_id": KNOWLEDGE_BASE_ID,
                    "knowledge_base_name": "企业公共知识库",
                    "version_number": 1,
                    "chunk_count": 8,
                }
            ],
            "total": 1,
            "limit": args[-2],
            "offset": args[-1],
        }

    async def get_document(self, tenant_id: str, document_id: str) -> dict[str, object]:
        assert tenant_id == TENANT_ID
        if document_id == MISSING_DOCUMENT_ID:
            raise ResourceNotFound("document not found")
        return {"id": document_id, "title": "报销制度", "versions": []}

    async def set_document_active(
        self,
        tenant_id: str,
        document_id: str,
        active: bool,
    ) -> str:
        self.state_arguments = (tenant_id, document_id, active)
        if document_id == MISSING_DOCUMENT_ID:
            raise ResourceNotFound("document not found")
        if document_id == CONFLICT_DOCUMENT_ID:
            raise InvalidRequest("document must be reindexed before activation")
        return "READY" if active else "DISABLED"

    async def enqueue_document_reindex(
        self,
        tenant_id: str,
        document_id: str,
    ) -> dict[str, object]:
        self.reindex_arguments = (tenant_id, document_id)
        if document_id == MISSING_DOCUMENT_ID:
            raise ResourceNotFound("document not found")
        if document_id == CONFLICT_DOCUMENT_ID:
            raise InvalidRequest("document has no chunks to reindex")
        return {
            "job_id": "00000000-0000-0000-0000-000000000301",
            "document_id": document_id,
            "status": "QUEUED",
            "created": True,
        }

    async def enqueue_reindex_jobs(
        self,
        tenant_id: str,
        knowledge_base_id: str | None,
        only_missing_vectors: bool,
    ) -> dict[str, object]:
        self.batch_reindex_arguments = (
            tenant_id,
            knowledge_base_id,
            only_missing_vectors,
        )
        return {
            "eligible_count": 2,
            "queued_count": 2,
            "already_queued_count": 0,
            "jobs": [],
        }

    async def list_reindex_jobs(self, tenant_id: str, limit: int) -> dict[str, object]:
        assert tenant_id == TENANT_ID
        return {
            "items": [],
            "queued_count": 0,
            "in_progress_count": 0,
            "retrying_count": 0,
            "succeeded_count": 1,
            "failed_count": 0,
            "active_count": 0,
            "limit": limit,
        }


class FakeModelClient:
    def __init__(
        self,
        model: str,
        *,
        enabled: bool = True,
        dimension: int | None = None,
    ) -> None:
        self.model = model
        self.enabled = enabled
        self.dimension = dimension

    async def probe(self) -> int | None:
        return self.dimension


class FakeRagEvaluationService:
    def __init__(self) -> None:
        self.arguments: dict[str, object] | None = None

    async def evaluate(self, **kwargs: object) -> dict[str, object]:
        self.arguments = kwargs
        return {
            "evaluation_id": "evaluation-1",
            "user_id": USER_ID,
            "knowledge_base_ids": [KNOWLEDGE_BASE_ID],
            "summary": {
                "total_cases": 1,
                "passed_cases": 1,
                "pass_rate": 1.0,
                "p95_latency_ms": 125.0,
            },
            "cases": [{"index": 1, "passed": True}],
        }


def build_client(
    repository: FakeRepository | None = None,
    *,
    admin_token: str | None = ADMIN_TOKEN,
) -> tuple[TestClient, FakeRepository]:
    settings = Settings(
        AGENT_DATABASE_URL="postgresql://unused",
        AGENT_TENANT_ID=TENANT_ID,
        AGENT_ADMIN_TOKEN=admin_token,
    )
    actual_repository = repository or FakeRepository()
    app = FastAPI()
    app.state.repository = actual_repository
    app.state.llm = FakeModelClient("Qwen3.5-4B")
    app.state.embedding = FakeModelClient("bge-m3", dimension=1024)
    app.state.rag_evaluation = FakeRagEvaluationService()
    app.include_router(
        create_admin_router(settings, create_admin_dependency(settings)),
    )
    return TestClient(app), actual_repository


def admin_headers() -> dict[str, str]:
    return {"X-Internal-Token": ADMIN_TOKEN}


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong-token"}])
def test_admin_api_rejects_missing_or_invalid_token(headers: dict[str, str]) -> None:
    client, _repository = build_client()

    response = client.get("/internal/v1/admin/session", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid internal token"}


def test_admin_api_is_disabled_without_configured_token() -> None:
    client, _repository = build_client(admin_token=None)

    response = client.get("/internal/v1/admin/session")

    assert response.status_code == 503
    assert response.json() == {"detail": "admin console is disabled"}


def test_admin_session_and_overview() -> None:
    client, _repository = build_client()

    session = client.get("/internal/v1/admin/session", headers=admin_headers())
    overview = client.get("/internal/v1/admin/overview", headers=admin_headers())
    knowledge_bases = client.get(
        "/internal/v1/admin/knowledge-bases",
        headers=admin_headers(),
    )

    assert session.status_code == 200
    assert session.json()["authenticated"] is True
    assert session.json()["tenant_id"] == TENANT_ID
    assert session.json()["max_upload_bytes"] == 20 * 1024 * 1024
    assert "markdown" in session.json()["supported_extensions"]
    assert overview.status_code == 200
    assert overview.json()["document_count"] == 2
    assert knowledge_bases.status_code == 200
    assert knowledge_bases.json()[0]["id"] == KNOWLEDGE_BASE_ID


def test_admin_document_list_forwards_filters_and_pagination() -> None:
    client, repository = build_client()

    response = client.get(
        "/internal/v1/admin/documents",
        headers=admin_headers(),
        params={
            "knowledge_base_id": KNOWLEDGE_BASE_ID,
            "status": "READY",
            "search": "报销",
            "limit": 10,
            "offset": 20,
        },
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["title"] == "报销制度"
    assert repository.list_arguments == (
        TENANT_ID,
        KNOWLEDGE_BASE_ID,
        "READY",
        "报销",
        10,
        20,
    )


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"knowledge_base_id": "not-a-uuid"}, "invalid knowledge_base_id"),
        ({"status": "DELETED"}, "unsupported document status"),
    ],
)
def test_admin_document_list_rejects_invalid_filters(
    params: dict[str, str],
    detail: str,
) -> None:
    client, _repository = build_client()

    response = client.get(
        "/internal/v1/admin/documents",
        headers=admin_headers(),
        params=params,
    )

    assert response.status_code == 400
    assert response.json() == {"detail": detail}


def test_admin_document_detail_and_not_found() -> None:
    client, _repository = build_client()

    detail = client.get(
        f"/internal/v1/admin/documents/{DOCUMENT_ID}",
        headers=admin_headers(),
    )
    invalid = client.get(
        "/internal/v1/admin/documents/not-a-uuid",
        headers=admin_headers(),
    )
    missing = client.get(
        f"/internal/v1/admin/documents/{MISSING_DOCUMENT_ID}",
        headers=admin_headers(),
    )

    assert detail.status_code == 200
    assert detail.json()["id"] == DOCUMENT_ID
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "invalid document_id"}
    assert missing.status_code == 404


def test_admin_document_state_update() -> None:
    client, repository = build_client()

    response = client.patch(
        f"/internal/v1/admin/documents/{DOCUMENT_ID}/state",
        headers=admin_headers(),
        json={"active": False},
    )

    assert response.status_code == 200
    assert response.json() == {"document_id": DOCUMENT_ID, "status": "DISABLED"}
    assert repository.state_arguments == (TENANT_ID, DOCUMENT_ID, False)


@pytest.mark.parametrize(
    ("document_id", "expected_status"),
    [(MISSING_DOCUMENT_ID, 404), (CONFLICT_DOCUMENT_ID, 409)],
)
def test_admin_document_state_update_maps_domain_errors(
    document_id: str,
    expected_status: int,
) -> None:
    client, _repository = build_client()

    response = client.patch(
        f"/internal/v1/admin/documents/{document_id}/state",
        headers=admin_headers(),
        json={"active": True},
    )

    assert response.status_code == expected_status


def test_admin_document_state_requires_boolean() -> None:
    client, _repository = build_client()

    response = client.patch(
        f"/internal/v1/admin/documents/{DOCUMENT_ID}/state",
        headers=admin_headers(),
        json={"active": "yes"},
    )

    assert response.status_code == 422


def test_admin_model_status() -> None:
    client, _repository = build_client()

    response = client.get(
        "/internal/v1/admin/models/status",
        headers=admin_headers(),
    )

    assert response.status_code == 200
    assert response.json()["llm"] == {
        "configured": True,
        "model": "Qwen3.5-4B",
        "status": "UP",
        "detail": "model is available",
    }
    assert response.json()["embedding"]["status"] == "UP"
    assert response.json()["embedding"]["dimension"] == 1024


def test_admin_runs_rag_evaluation_with_real_acl_scope() -> None:
    client, _repository = build_client()

    response = client.post(
        "/internal/v1/admin/rag/evaluate",
        headers=admin_headers(),
        json={
            "user_id": USER_ID,
            "knowledge_base_ids": [KNOWLEDGE_BASE_ID, KNOWLEDGE_BASE_ID],
            "cases": [
                {
                    "question": "  客户开户需要哪些资料？  ",
                    "expected_keywords": ["营业执照", "营业执照"],
                    "expected_sources": ["客户开户指引"],
                    "expect_refusal": False,
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["summary"]["pass_rate"] == 1.0
    evaluation = client.app.state.rag_evaluation
    assert evaluation.arguments["tenant_id"] == TENANT_ID
    assert evaluation.arguments["user_id"] == USER_ID
    assert evaluation.arguments["knowledge_base_ids"] == (KNOWLEDGE_BASE_ID,)
    assert evaluation.arguments["cases"][0].question == "客户开户需要哪些资料？"
    assert evaluation.arguments["cases"][0].expected_keywords == ("营业执照",)


@pytest.mark.parametrize(
    "payload",
    [
        {"cases": []},
        {"user_id": "not-a-uuid", "cases": [{"question": "测试"}]},
        {"knowledge_base_ids": ["not-a-uuid"], "cases": [{"question": "测试"}]},
        {"cases": [{"question": "   "}]},
    ],
)
def test_admin_rag_evaluation_rejects_invalid_payload(payload: dict[str, object]) -> None:
    client, _repository = build_client()

    response = client.post(
        "/internal/v1/admin/rag/evaluate",
        headers=admin_headers(),
        json=payload,
    )

    assert response.status_code in {400, 422}


def test_admin_enqueues_document_reindex() -> None:
    client, repository = build_client()

    response = client.post(
        f"/internal/v1/admin/documents/{DOCUMENT_ID}/reindex",
        headers=admin_headers(),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"
    assert repository.reindex_arguments == (TENANT_ID, DOCUMENT_ID)


def test_admin_enqueues_batch_reindex_and_lists_jobs() -> None:
    client, repository = build_client()

    enqueue = client.post(
        "/internal/v1/admin/reindex",
        headers=admin_headers(),
        json={
            "knowledge_base_id": KNOWLEDGE_BASE_ID,
            "only_missing_vectors": False,
        },
    )
    jobs = client.get(
        "/internal/v1/admin/reindex/jobs",
        headers=admin_headers(),
        params={"limit": 5},
    )

    assert enqueue.status_code == 202
    assert enqueue.json()["queued_count"] == 2
    assert repository.batch_reindex_arguments == (
        TENANT_ID,
        KNOWLEDGE_BASE_ID,
        False,
    )
    assert jobs.status_code == 200
    assert jobs.json()["succeeded_count"] == 1
    assert jobs.json()["limit"] == 5


@pytest.mark.parametrize(
    ("document_id", "expected_status"),
    [("not-a-uuid", 400), (MISSING_DOCUMENT_ID, 404), (CONFLICT_DOCUMENT_ID, 409)],
)
def test_admin_document_reindex_maps_errors(
    document_id: str,
    expected_status: int,
) -> None:
    client, _repository = build_client()

    response = client.post(
        f"/internal/v1/admin/documents/{document_id}/reindex",
        headers=admin_headers(),
    )

    assert response.status_code == expected_status


def test_http_app_serves_console_and_protects_document_upload() -> None:
    settings = Settings(
        AGENT_DATABASE_URL="postgresql://unused",
        AGENT_TENANT_ID=TENANT_ID,
        AGENT_ADMIN_TOKEN=ADMIN_TOKEN,
    )
    client = TestClient(create_app(settings))

    page = client.get("/admin")
    upload = client.post(
        "/internal/v1/documents",
        data={"knowledge_base_id": KNOWLEDGE_BASE_ID},
        files={"file": ("policy.md", b"test", "text/markdown")},
    )

    assert page.status_code == 200
    assert "/admin/assets/app.js" in page.text
    assert "RAG 评测" in page.text
    assert upload.status_code == 401
    assert upload.json() == {"detail": "invalid internal token"}
