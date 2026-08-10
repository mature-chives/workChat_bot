from __future__ import annotations

from datetime import UTC, datetime

from agent.application.evaluation import RagEvaluationCase, RagEvaluationService
from agent.application.models import AnswerResult, Citation, DependencyUnavailable, QuestionRequest

TENANT_ID = "00000000-0000-0000-0000-000000000001"
USER_ID = "00000000-0000-0000-0000-000000000011"
KNOWLEDGE_BASE_ID = "00000000-0000-0000-0000-000000000101"


class FakeRepository:
    def __init__(self) -> None:
        self.ensure_calls: list[str] = []

    async def ensure_evaluation_user(self, tenant_id: str) -> str:
        self.ensure_calls.append(tenant_id)
        return USER_ID


class FakeQueryService:
    def __init__(self) -> None:
        self.requests: list[QuestionRequest] = []

    async def answer(self, request: QuestionRequest) -> AnswerResult:
        self.requests.append(request)
        if request.question == "依赖异常":
            raise DependencyUnavailable("embedding service unavailable")
        if request.question == "公司附近有哪些咖啡店？":
            return AnswerResult(
                message_id="message-refused",
                answer="当前授权知识库中没有找到足够依据。",
                citations=(),
                refused=True,
                refusal_reason="NO_RELEVANT_EVIDENCE",
                conversation_id="conversation-1",
                created_at=datetime.now(UTC),
            )
        return AnswerResult(
            message_id="message-ok",
            answer="需要营业执照和法人身份证。[1]",
            citations=(
                Citation(
                    index=1,
                    document_id="00000000-0000-0000-0000-000000000201",
                    document_version=1,
                    title="客户开户指引",
                    locator_type="SECTION",
                    locator_value="开户资料",
                    effective_at=None,
                ),
            ),
            refused=False,
            refusal_reason="",
            conversation_id="conversation-1",
            created_at=datetime.now(UTC),
        )


async def test_rag_evaluation_calculates_quality_and_latency_metrics() -> None:
    repository = FakeRepository()
    query_service = FakeQueryService()
    service = RagEvaluationService(repository, query_service)

    result = await service.evaluate(
        tenant_id=TENANT_ID,
        knowledge_base_ids=(KNOWLEDGE_BASE_ID,),
        cases=(
            RagEvaluationCase(
                question="客户开户需要哪些资料？",
                expected_keywords=("营业执照", "法人身份证"),
                expected_sources=("开户指引",),
            ),
            RagEvaluationCase(
                question="公司附近有哪些咖啡店？",
                expect_refusal=True,
            ),
            RagEvaluationCase(
                question="依赖异常",
                expected_keywords=("不可能命中",),
            ),
        ),
    )

    assert repository.ensure_calls == [TENANT_ID]
    assert len(query_service.requests) == 3
    assert all(request.channel == "EVAL" for request in query_service.requests)
    assert all(request.user_id == USER_ID for request in query_service.requests)
    assert all(
        request.knowledge_base_ids == (KNOWLEDGE_BASE_ID,) for request in query_service.requests
    )
    assert result["user_id"] == USER_ID
    assert result["cases"][0]["passed"] is True
    assert result["cases"][1]["passed"] is True
    assert result["cases"][2]["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert result["summary"]["total_cases"] == 3
    assert result["summary"]["passed_cases"] == 2
    assert result["summary"]["error_cases"] == 1
    assert result["summary"]["pass_rate"] == 0.6667
    assert result["summary"]["citation_rate"] == 0.5
    assert result["summary"]["refusal_accuracy"] == 0.6667
    assert result["summary"]["keyword_recall"] == 0.6667
    assert result["summary"]["source_hit_rate"] == 1.0
    assert result["summary"]["p95_latency_ms"] >= 0


async def test_rag_evaluation_uses_explicit_acl_user_without_creating_one() -> None:
    repository = FakeRepository()
    query_service = FakeQueryService()
    service = RagEvaluationService(repository, query_service)

    result = await service.evaluate(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        cases=(RagEvaluationCase(question="客户开户需要哪些资料？"),),
    )

    assert repository.ensure_calls == []
    assert result["cases"][0]["passed"] is True
    assert result["cases"][0]["citations"][0]["title"] == "客户开户指引"
