from __future__ import annotations

from typing import Any

import pytest

from agent.application.ingestion import DocumentIngestionService, parse_document, split_text
from agent.settings import Settings

TENANT_ID = "00000000-0000-0000-0000-000000000001"
KNOWLEDGE_BASE_ID = "00000000-0000-0000-0000-000000000101"


class FakeRepository:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None

    async def save_document(self, **kwargs: Any) -> tuple[int, str]:
        self.saved = kwargs
        return 1, "rag-default-v1"


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class DisabledEmbedding:
    @property
    def enabled(self) -> bool:
        return False

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("disabled embedding client was called")


def settings() -> Settings:
    return Settings(
        AGENT_DATABASE_URL="postgresql://unused",
        AGENT_ALLOW_EXTRACTIVE_FALLBACK=True,
        chunk_size=200,
        chunk_overlap=20,
    )


@pytest.mark.asyncio
async def test_ingest_text_document_without_embedding() -> None:
    repository = FakeRepository()
    objects = FakeObjectStore()
    service = DocumentIngestionService(repository, objects, DisabledEmbedding(), settings())

    result = await service.ingest(
        tenant_id=TENANT_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
        document_id=None,
        title="报销制度",
        source_code="policy-expense",
        file_name="expense.md",
        content_type="text/markdown",
        data=("报销流程包括提交申请、部门审批和财务付款。" * 30).encode(),
    )

    assert result.index_mode == "KEYWORD"
    assert result.chunk_count > 1
    assert repository.saved is not None
    assert len(repository.saved["chunks"]) == result.chunk_count
    assert set(objects.objects) == {result.object_key}


def test_parse_gb18030_text() -> None:
    assert parse_document("制度.txt", "中文制度".encode("gb18030")) == "中文制度"


def test_split_text_has_bounded_chunks_and_overlap() -> None:
    chunks = split_text("第一段。" * 100, chunk_size=200, overlap=20)

    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)
