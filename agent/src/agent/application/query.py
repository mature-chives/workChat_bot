from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from agent.application.models import (
    AnswerResult,
    Candidate,
    ClaimedRun,
    DependencyUnavailable,
    GeneratedAnswer,
    InvalidRequest,
    QuestionRequest,
)
from agent.settings import Settings

_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_CITATION_PATTERN = re.compile(r"\[(\d+)]")
_ALLOWED_CHANNELS = {"WECOM", "WEB", "EVAL"}
_ALLOWED_REFUSALS = {
    "NO_RELEVANT_EVIDENCE",
    "CONFLICTING_EVIDENCE",
    "HIGH_RISK_INSUFFICIENT_EVIDENCE",
    "INPUT_POLICY_BLOCKED",
}


class QueryRepository(Protocol):
    async def claim_query(self, **kwargs: object) -> ClaimedRun: ...

    async def search_keyword(
        self,
        tenant_id: str,
        user_id: str,
        knowledge_base_ids: Sequence[str],
        question: str,
        limit: int,
    ) -> list[Candidate]: ...

    async def search_vector(
        self,
        tenant_id: str,
        user_id: str,
        knowledge_base_ids: Sequence[str],
        embedding: Sequence[float],
        limit: int,
    ) -> list[Candidate]: ...

    async def persist_answer(self, **kwargs: object) -> AnswerResult: ...

    async def mark_retryable_failure(self, tenant_id: str, request_id: str, code: str) -> None: ...


class EmbeddingClient(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def embed_query(self, text: str) -> list[float] | None: ...


class LLMClient(Protocol):
    @property
    def enabled(self) -> bool: ...

    @property
    def model(self) -> str: ...

    async def generate(self, question: str, candidates: Sequence[Candidate]) -> GeneratedAnswer: ...


class QueryService:
    def __init__(
        self,
        repository: QueryRepository,
        embedding: EmbeddingClient,
        llm: LLMClient,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._embedding = embedding
        self._llm = llm
        self._settings = settings

    async def answer(self, request: QuestionRequest) -> AnswerResult:
        normalized = _validate_and_normalize(request)
        fingerprint = _fingerprint(request, normalized)
        claimed = await self._repository.claim_query(
            tenant_id=request.tenant_id,
            request_id=request.request_id,
            fingerprint=fingerprint,
            user_id=request.user_id,
            conversation_id=request.conversation_id,
            question=normalized,
            channel=request.channel,
        )
        if claimed.existing_result is not None:
            return claimed.existing_result

        try:
            keyword = await self._repository.search_keyword(
                request.tenant_id,
                request.user_id,
                request.knowledge_base_ids,
                normalized,
                self._settings.top_k_keyword,
            )
            vector: list[Candidate] = []
            if self._embedding.enabled:
                try:
                    embedding = await self._embedding.embed_query(normalized)
                    if embedding is not None:
                        vector = await self._repository.search_vector(
                            request.tenant_id,
                            request.user_id,
                            request.knowledge_base_ids,
                            embedding,
                            self._settings.top_k_vector,
                        )
                except DependencyUnavailable:
                    if not self._settings.allow_extractive_fallback:
                        raise

            candidates = _rrf(keyword, vector, self._settings.top_k_final)
            if not candidates:
                generated = GeneratedAnswer(
                    answer="当前授权知识库中没有找到足够依据，请补充问题细节或联系知识负责人。",
                    refused=True,
                    refusal_reason="NO_RELEVANT_EVIDENCE",
                )
                model_name = None
            else:
                generated, model_name = await self._generate(normalized, candidates)
                generated = _validate_and_renumber(generated, len(candidates))

            return await self._repository.persist_answer(
                tenant_id=request.tenant_id,
                request_id=request.request_id,
                user_id=request.user_id,
                conversation_id=claimed.conversation_id,
                generated=generated,
                candidates=candidates,
                model_name=model_name,
                prompt_version=self._settings.prompt_version,
                retrieval_config_version=self._settings.retrieval_config_version,
            )
        except DependencyUnavailable as exc:
            await self._repository.mark_retryable_failure(
                request.tenant_id, request.request_id, exc.code
            )
            raise

    async def _generate(
        self, question: str, candidates: Sequence[Candidate]
    ) -> tuple[GeneratedAnswer, str]:
        if self._llm.enabled:
            try:
                return await self._llm.generate(question, candidates), self._llm.model
            except DependencyUnavailable:
                if not self._settings.allow_extractive_fallback:
                    raise
        if not self._settings.allow_extractive_fallback:
            raise DependencyUnavailable("LLM service is unavailable")

        top = candidates[0]
        content = " ".join(top.content.split())
        if len(content) > 800:
            content = content[:797].rstrip() + "..."
        generated = GeneratedAnswer(
            answer=f"根据知识库：{content} [1]",
            citation_indexes=(1,),
        )
        return generated, "extractive"


def _validate_and_normalize(request: QuestionRequest) -> str:
    try:
        UUID(request.tenant_id)
        UUID(request.user_id)
        if request.conversation_id:
            UUID(request.conversation_id)
        for knowledge_base_id in request.knowledge_base_ids:
            UUID(knowledge_base_id)
    except ValueError as exc:
        raise InvalidRequest("invalid UUID in request") from exc

    if not request.request_id or len(request.request_id) > 128:
        raise InvalidRequest("invalid request ID")
    if request.channel not in _ALLOWED_CHANNELS:
        raise InvalidRequest("unsupported channel")
    if not _TRACE_ID_PATTERN.fullmatch(request.trace_id):
        raise InvalidRequest("invalid trace ID")

    normalized = unicodedata.normalize("NFC", request.question)
    normalized = " ".join(normalized.strip().split())
    if not normalized or len(normalized) > 4000:
        raise InvalidRequest("question length is out of range")
    return normalized


def _fingerprint(request: QuestionRequest, normalized_question: str) -> str:
    values = [
        request.tenant_id,
        request.user_id,
        request.conversation_id or "",
        normalized_question,
        ",".join(sorted(set(request.knowledge_base_ids))),
        request.channel,
    ]
    return hashlib.sha256("\x1f".join(values).encode()).hexdigest()


def _rrf(
    keyword: Sequence[Candidate],
    vector: Sequence[Candidate],
    limit: int,
    rrf_k: int = 60,
) -> list[Candidate]:
    candidates: dict[str, Candidate] = {}
    scores: dict[str, float] = {}
    for channel in (keyword, vector):
        for rank, candidate in enumerate(channel, start=1):
            candidates.setdefault(candidate.chunk_id, candidate)
            scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + 1.0 / (rrf_k + rank)
    ordered_ids = sorted(scores, key=lambda item: (-scores[item], item))[:limit]
    return [candidates[item] for item in ordered_ids]


def _validate_and_renumber(generated: GeneratedAnswer, candidate_count: int) -> GeneratedAnswer:
    reason = generated.refusal_reason
    if generated.refused:
        if reason not in _ALLOWED_REFUSALS:
            reason = "NO_RELEVANT_EVIDENCE"
        return GeneratedAnswer(
            answer=generated.answer,
            citation_indexes=(),
            refused=True,
            refusal_reason=reason,
        )

    indexes = tuple(dict.fromkeys(generated.citation_indexes))
    if not indexes or any(index < 1 or index > candidate_count for index in indexes):
        raise DependencyUnavailable("model returned invalid citations")
    markers = {int(value) for value in _CITATION_PATTERN.findall(generated.answer)}
    if markers != set(indexes):
        raise DependencyUnavailable("answer citation markers do not match structured citations")

    mapping = {old: new for new, old in enumerate(indexes, start=1)}

    def replace_marker(match: re.Match[str]) -> str:
        return f"[{mapping[int(match.group(1))]}]"

    answer = _CITATION_PATTERN.sub(replace_marker, generated.answer)
    return GeneratedAnswer(
        answer=answer,
        citation_indexes=indexes,
        refused=False,
        refusal_reason="",
    )
