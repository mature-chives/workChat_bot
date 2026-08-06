from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class QuestionRequest:
    request_id: str
    tenant_id: str
    user_id: str
    conversation_id: str | None
    question: str
    trace_id: str
    knowledge_base_ids: tuple[str, ...]
    channel: str


@dataclass(frozen=True, slots=True)
class Candidate:
    chunk_id: str
    document_id: str
    document_version_id: str
    document_version_number: int
    title: str
    content: str
    content_hash: str
    locator_type: str
    locator_value: str
    effective_at: datetime | None
    score: float


@dataclass(frozen=True, slots=True)
class Citation:
    index: int
    document_id: str
    document_version: int
    title: str
    locator_type: str
    locator_value: str
    effective_at: datetime | None


@dataclass(frozen=True, slots=True)
class AnswerResult:
    message_id: str
    answer: str
    citations: tuple[Citation, ...]
    refused: bool
    refusal_reason: str
    conversation_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    conversation_id: str
    existing_result: AnswerResult | None = None


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    answer: str
    citation_indexes: tuple[int, ...] = field(default_factory=tuple)
    refused: bool = False
    refusal_reason: str = ""


class AgentError(Exception):
    code = "AGENT_ERROR"


class InvalidRequest(AgentError):
    code = "REQUEST_INVALID"


class PermissionDenied(AgentError):
    code = "PERMISSION_DENIED"


class ResourceNotFound(AgentError):
    code = "RESOURCE_NOT_FOUND"


class RequestConflict(AgentError):
    code = "REQUEST_ID_CONFLICT"


class RequestInProgress(AgentError):
    code = "REQUEST_IN_PROGRESS"


class DependencyUnavailable(AgentError):
    code = "DEPENDENCY_UNAVAILABLE"
