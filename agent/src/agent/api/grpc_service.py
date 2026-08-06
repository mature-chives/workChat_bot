from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from agent import __version__
from agent.application.models import (
    AgentError,
    DependencyUnavailable,
    InvalidRequest,
    PermissionDenied,
    QuestionRequest,
    RequestConflict,
    RequestInProgress,
    ResourceNotFound,
)
from agent.application.query import QueryService
from agent.v1 import agent_pb2, agent_pb2_grpc

logger = logging.getLogger(__name__)


class HealthProbe(Protocol):
    async def ping(self) -> None: ...


_STATUS_BY_ERROR: dict[type[AgentError], grpc.StatusCode] = {
    InvalidRequest: grpc.StatusCode.INVALID_ARGUMENT,
    PermissionDenied: grpc.StatusCode.PERMISSION_DENIED,
    ResourceNotFound: grpc.StatusCode.NOT_FOUND,
    RequestConflict: grpc.StatusCode.FAILED_PRECONDITION,
    RequestInProgress: grpc.StatusCode.ABORTED,
    DependencyUnavailable: grpc.StatusCode.UNAVAILABLE,
}


class AgentGRPCService(agent_pb2_grpc.AgentServiceServicer):
    def __init__(self, query_service: QueryService, health_probe: HealthProbe) -> None:
        self._query_service = query_service
        self._health_probe = health_probe

    async def AnswerQuestion(
        self,
        request: agent_pb2.AnswerQuestionRequest,
        context: grpc.aio.ServicerContext,
    ) -> agent_pb2.AnswerQuestionResponse:
        question = QuestionRequest(
            request_id=request.request_id,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            conversation_id=request.conversation_id or None,
            question=request.question,
            trace_id=request.trace_id,
            knowledge_base_ids=tuple(request.knowledge_base_ids),
            channel=request.channel,
        )
        try:
            result = await self._query_service.answer(question)
        except AgentError as exc:
            status = _STATUS_BY_ERROR.get(type(exc), grpc.StatusCode.INTERNAL)
            await context.abort(
                status,
                str(exc),
                trailing_metadata=(("error-code", exc.code),),
            )
            raise AssertionError("gRPC abort unexpectedly returned") from None
        except Exception:
            logger.exception("unhandled error while answering question")
            await context.abort(grpc.StatusCode.INTERNAL, "internal agent error")
            raise AssertionError("gRPC abort unexpectedly returned") from None

        response = agent_pb2.AnswerQuestionResponse(
            message_id=result.message_id,
            answer=result.answer,
            refused=result.refused,
            refusal_reason=result.refusal_reason,
            conversation_id=result.conversation_id,
        )
        response.created_at.CopyFrom(_timestamp(result.created_at))
        for citation in result.citations:
            item = response.citations.add(
                index=citation.index,
                document_id=citation.document_id,
                document_version=citation.document_version,
                title=citation.title,
                locator_type=citation.locator_type,
                locator_value=citation.locator_value,
            )
            if citation.effective_at is not None:
                item.effective_at.CopyFrom(_timestamp(citation.effective_at))
        return response

    async def GetHealth(
        self,
        request: agent_pb2.HealthRequest,
        context: grpc.aio.ServicerContext,
    ) -> agent_pb2.HealthResponse:
        del request, context
        try:
            await asyncio.wait_for(self._health_probe.ping(), timeout=0.75)
        except Exception:
            logger.warning("agent database health check failed", exc_info=True)
            return agent_pb2.HealthResponse(status="DOWN", version=__version__)
        return agent_pb2.HealthResponse(status="UP", version=__version__)


def _timestamp(value: datetime) -> Timestamp:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    result = Timestamp()
    result.FromDatetime(value)
    return result
