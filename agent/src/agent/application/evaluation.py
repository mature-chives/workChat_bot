from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from agent.application.models import AgentError, AnswerResult, QuestionRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RagEvaluationCase:
    question: str
    expected_keywords: tuple[str, ...] = ()
    expected_sources: tuple[str, ...] = ()
    expect_refusal: bool = False


class EvaluationQueryService(Protocol):
    async def answer(self, request: QuestionRequest) -> AnswerResult: ...


class EvaluationUserRepository(Protocol):
    async def ensure_evaluation_user(self, tenant_id: str) -> str: ...


class RagEvaluationService:
    def __init__(
        self,
        repository: EvaluationUserRepository,
        query_service: EvaluationQueryService,
    ) -> None:
        self._repository = repository
        self._query_service = query_service

    async def evaluate(
        self,
        *,
        tenant_id: str,
        cases: tuple[RagEvaluationCase, ...],
        knowledge_base_ids: tuple[str, ...] = (),
        user_id: str | None = None,
    ) -> dict[str, object]:
        evaluation_id = str(uuid4())
        actual_user_id = user_id or await self._repository.ensure_evaluation_user(tenant_id)
        started = perf_counter()
        results: list[dict[str, object]] = []

        for index, case in enumerate(cases, start=1):
            results.append(
                await self._evaluate_case(
                    evaluation_id=evaluation_id,
                    index=index,
                    tenant_id=tenant_id,
                    user_id=actual_user_id,
                    knowledge_base_ids=knowledge_base_ids,
                    case=case,
                )
            )

        duration_ms = (perf_counter() - started) * 1000
        return {
            "evaluation_id": evaluation_id,
            "user_id": actual_user_id,
            "knowledge_base_ids": list(knowledge_base_ids),
            "summary": _summarize(results, duration_ms),
            "cases": results,
        }

    async def _evaluate_case(
        self,
        *,
        evaluation_id: str,
        index: int,
        tenant_id: str,
        user_id: str,
        knowledge_base_ids: tuple[str, ...],
        case: RagEvaluationCase,
    ) -> dict[str, object]:
        started = perf_counter()
        try:
            answer = await self._query_service.answer(
                QuestionRequest(
                    request_id=f"eval-{evaluation_id}-{index}",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=None,
                    question=case.question,
                    trace_id=uuid4().hex,
                    knowledge_base_ids=knowledge_base_ids,
                    channel="EVAL",
                )
            )
        except AgentError as exc:
            return _error_result(index, case, started, exc.code, str(exc))
        except Exception:
            logger.exception("unexpected RAG evaluation failure", extra={"case_index": index})
            return _error_result(
                index,
                case,
                started,
                "INTERNAL_ERROR",
                "evaluation case failed unexpectedly",
            )

        latency_ms = (perf_counter() - started) * 1000
        keyword_matches = _match_values(case.expected_keywords, (answer.answer,))
        citation_titles = tuple(citation.title for citation in answer.citations)
        source_matches = _match_values(case.expected_sources, citation_titles)
        refusal_correct = answer.refused == case.expect_refusal
        passed = refusal_correct
        if not case.expect_refusal:
            passed = (
                passed
                and not answer.refused
                and bool(answer.citations)
                and all(item["matched"] for item in keyword_matches)
                and all(item["matched"] for item in source_matches)
            )

        return {
            "index": index,
            "question": case.question,
            "expect_refusal": case.expect_refusal,
            "passed": passed,
            "latency_ms": _round_metric(latency_ms),
            "answer": answer.answer,
            "refused": answer.refused,
            "refusal_reason": answer.refusal_reason,
            "refusal_correct": refusal_correct,
            "citations": [
                {
                    "index": citation.index,
                    "document_id": citation.document_id,
                    "document_version": citation.document_version,
                    "title": citation.title,
                    "locator_type": citation.locator_type,
                    "locator_value": citation.locator_value,
                }
                for citation in answer.citations
            ],
            "keyword_matches": keyword_matches,
            "source_matches": source_matches,
            "error_code": None,
            "error_message": None,
        }


def _match_values(expected: tuple[str, ...], actual: tuple[str, ...]) -> list[dict[str, object]]:
    normalized_actual = tuple(_normalize(value) for value in actual)
    return [
        {
            "value": value,
            "matched": any(_normalize(value) in candidate for candidate in normalized_actual),
        }
        for value in expected
    ]


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _error_result(
    index: int,
    case: RagEvaluationCase,
    started: float,
    code: str,
    message: str,
) -> dict[str, object]:
    return {
        "index": index,
        "question": case.question,
        "expect_refusal": case.expect_refusal,
        "passed": False,
        "latency_ms": _round_metric((perf_counter() - started) * 1000),
        "answer": "",
        "refused": False,
        "refusal_reason": "",
        "refusal_correct": False,
        "citations": [],
        "keyword_matches": [{"value": value, "matched": False} for value in case.expected_keywords],
        "source_matches": [{"value": value, "matched": False} for value in case.expected_sources],
        "error_code": code,
        "error_message": " ".join(message.split())[:500],
    }


def _summarize(results: list[dict[str, object]], duration_ms: float) -> dict[str, object]:
    total = len(results)
    passed = sum(bool(item["passed"]) for item in results)
    errors = sum(bool(item["error_code"]) for item in results)
    latencies = [float(item["latency_ms"]) for item in results]
    expected_non_refusals = [item for item in results if not bool(item["expect_refusal"])]
    citation_hits = sum(bool(item["citations"]) for item in expected_non_refusals)
    refusal_matches = sum(bool(item["refusal_correct"]) for item in results)
    keyword_checks = [
        match
        for item in results
        for match in item["keyword_matches"]  # type: ignore[union-attr]
    ]
    source_checks = [
        match
        for item in results
        for match in item["source_matches"]  # type: ignore[union-attr]
    ]

    return {
        "total_cases": total,
        "passed_cases": passed,
        "failed_cases": total - passed,
        "error_cases": errors,
        "pass_rate": _ratio(passed, total),
        "citation_rate": _ratio(citation_hits, len(expected_non_refusals)),
        "refusal_accuracy": _ratio(refusal_matches, total),
        "keyword_recall": _ratio(
            sum(bool(item["matched"]) for item in keyword_checks),
            len(keyword_checks),
        ),
        "source_hit_rate": _ratio(
            sum(bool(item["matched"]) for item in source_checks),
            len(source_checks),
        ),
        "average_latency_ms": _round_metric(sum(latencies) / total if total else 0),
        "p50_latency_ms": _round_metric(_percentile(latencies, 0.50)),
        "p95_latency_ms": _round_metric(_percentile(latencies, 0.95)),
        "duration_ms": _round_metric(duration_ms),
        "queries_per_second": _round_metric(total * 1000 / duration_ms if duration_ms else 0),
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _round_metric(value: float) -> float:
    return round(value, 2)
