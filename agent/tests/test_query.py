from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from agent.adapters.repository import _keyword_terms
from agent.application.models import (
    AnswerResult,
    Candidate,
    Citation,
    ClaimedRun,
    DependencyUnavailable,
    GeneratedAnswer,
    InvalidRequest,
    QuestionRequest,
)
from agent.application.query import QueryService
from agent.settings import Settings

TENANT_ID = "00000000-0000-0000-0000-000000000001"
USER_ID = "00000000-0000-0000-0000-000000000002"
CONVERSATION_ID = "00000000-0000-0000-0000-000000000003"


def candidate(number: int, title: str) -> Candidate:
    return Candidate(
        chunk_id=f"00000000-0000-0000-0001-{number:012d}",
        document_id=f"00000000-0000-0000-0002-{number:012d}",
        document_version_id=f"00000000-0000-0000-0003-{number:012d}",
        document_version_number=1,
        title=title,
        content=f"{title} 的知识内容",
        content_hash=f"hash-{number}",
        locator_type="PAGE",
        locator_value=str(number),
        effective_at=None,
        score=1.0,
    )


class FakeRepository:
    def __init__(
        self,
        keyword: list[Candidate] | None = None,
        vector: list[Candidate] | None = None,
    ) -> None:
        self.keyword = keyword or []
        self.vector = vector or []
        self.claimed = False
        self.persisted: dict[str, Any] | None = None
        self.failure_code: str | None = None

    async def claim_query(self, **kwargs: object) -> ClaimedRun:
        self.claimed = True
        return ClaimedRun(CONVERSATION_ID)

    async def search_keyword(self, *args: object) -> list[Candidate]:
        return self.keyword

    async def search_vector(self, *args: object) -> list[Candidate]:
        return self.vector

    async def persist_answer(self, **kwargs: Any) -> AnswerResult:
        self.persisted = kwargs
        generated: GeneratedAnswer = kwargs["generated"]
        candidates: list[Candidate] = kwargs["candidates"]
        citations = tuple(
            Citation(
                index=output_index,
                document_id=candidates[candidate_index - 1].document_id,
                document_version=1,
                title=candidates[candidate_index - 1].title,
                locator_type="PAGE",
                locator_value="1",
                effective_at=None,
            )
            for output_index, candidate_index in enumerate(generated.citation_indexes, start=1)
        )
        return AnswerResult(
            message_id="00000000-0000-0000-0004-000000000001",
            answer=generated.answer,
            citations=citations,
            refused=generated.refused,
            refusal_reason=generated.refusal_reason,
            conversation_id=CONVERSATION_ID,
            created_at=datetime.now(UTC),
        )

    async def mark_retryable_failure(self, tenant_id: str, request_id: str, code: str) -> None:
        self.failure_code = code


class FakeEmbedding:
    def __init__(self, vector: list[float] | None = None, fails: bool = False) -> None:
        self.vector = vector
        self.fails = fails

    @property
    def enabled(self) -> bool:
        return self.vector is not None or self.fails

    async def embed_query(self, text: str) -> list[float] | None:
        if self.fails:
            raise DependencyUnavailable("embedding failed")
        return self.vector


class FakeLLM:
    def __init__(
        self,
        generated: GeneratedAnswer | None = None,
        enabled: bool = True,
        fails: bool = False,
    ) -> None:
        self.generated = generated
        self._enabled = enabled
        self.fails = fails
        self.seen_candidates: list[Candidate] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def model(self) -> str:
        return "fake-model"

    async def generate(self, question: str, candidates: list[Candidate]) -> GeneratedAnswer:
        self.seen_candidates = list(candidates)
        if self.fails:
            raise DependencyUnavailable("LLM failed")
        assert self.generated is not None
        return self.generated


def settings(*, fallback: bool = True) -> Settings:
    return Settings(
        AGENT_DATABASE_URL="postgresql://unused",
        AGENT_ALLOW_EXTRACTIVE_FALLBACK=fallback,
        top_k_final=8,
    )


def request(**overrides: object) -> QuestionRequest:
    values: dict[str, object] = {
        "request_id": "request-1",
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "question": "  报销   流程是什么？ ",
        "trace_id": "0123456789abcdef0123456789abcdef",
        "knowledge_base_ids": (),
        "channel": "WECOM",
    }
    values.update(overrides)
    return QuestionRequest(**values)  # type: ignore[arg-type]


def test_keyword_terms_include_chinese_ngrams() -> None:
    terms = _keyword_terms("客户开户需要哪些资料 ABC-123")

    assert {"客户", "开户", "资料", "abc-123"}.issubset(terms)


@pytest.mark.asyncio
async def test_no_candidates_returns_grounded_refusal() -> None:
    repository = FakeRepository()
    service = QueryService(
        repository,
        FakeEmbedding(),
        FakeLLM(enabled=False),
        settings(),
    )

    result = await service.answer(request())

    assert result.refused is True
    assert result.refusal_reason == "NO_RELEVANT_EVIDENCE"
    assert repository.persisted is not None
    assert repository.persisted["model_name"] is None


@pytest.mark.asyncio
async def test_rrf_order_and_citations_are_renumbered() -> None:
    first = candidate(1, "第一份")
    second = candidate(2, "第二份")
    repository = FakeRepository(keyword=[first, second], vector=[second])
    llm = FakeLLM(GeneratedAnswer("先看第二条 [2]，再看第一条 [1]", (2, 1)))
    service = QueryService(repository, FakeEmbedding([0.1]), llm, settings())

    result = await service.answer(request())

    assert [item.title for item in llm.seen_candidates] == ["第二份", "第一份"]
    assert result.answer == "先看第二条 [1]，再看第一条 [2]"
    assert [item.title for item in result.citations] == ["第一份", "第二份"]


@pytest.mark.asyncio
async def test_llm_failure_uses_extractive_fallback() -> None:
    repository = FakeRepository(keyword=[candidate(1, "制度")])
    service = QueryService(
        repository,
        FakeEmbedding(),
        FakeLLM(fails=True),
        settings(fallback=True),
    )

    result = await service.answer(request())

    assert result.answer == "根据知识库：制度 的知识内容 [1]"
    assert repository.persisted is not None
    assert repository.persisted["model_name"] == "extractive"


@pytest.mark.asyncio
async def test_invalid_request_is_rejected_before_claim() -> None:
    repository = FakeRepository()
    service = QueryService(
        repository,
        FakeEmbedding(),
        FakeLLM(enabled=False),
        settings(),
    )

    with pytest.raises(InvalidRequest):
        await service.answer(request(trace_id="not-a-trace"))

    assert repository.claimed is False
