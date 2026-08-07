from __future__ import annotations

from collections.abc import Sequence

from agent.application.models import DependencyUnavailable
from agent.application.reindex import (
    DocumentReindexWorker,
    ReindexChunk,
    ReindexJob,
    ReindexSource,
)

TENANT_ID = "00000000-0000-0000-0000-000000000001"
DOCUMENT_ID = "00000000-0000-0000-0000-000000000201"
VERSION_ID = "00000000-0000-0000-0000-000000000202"
JOB_ID = "00000000-0000-0000-0000-000000000301"


class FakeRepository:
    def __init__(self, *, attempt: int = 1) -> None:
        self.job: ReindexJob | None = ReindexJob(JOB_ID, TENANT_ID, DOCUMENT_ID, attempt)
        self.source = ReindexSource(
            tenant_id=TENANT_ID,
            document_id=DOCUMENT_ID,
            document_version_id=VERSION_ID,
            index_version="rag-default-v1",
            chunks=(
                ReindexChunk("00000000-0000-0000-0000-000000000211", "第一块"),
                ReindexChunk("00000000-0000-0000-0000-000000000212", "第二块"),
                ReindexChunk("00000000-0000-0000-0000-000000000213", "第三块"),
            ),
        )
        self.claim_arguments: tuple[object, ...] | None = None
        self.stages: list[str] = []
        self.applied: tuple[ReindexSource, Sequence[Sequence[float]]] | None = None
        self.completed = False
        self.failure: tuple[object, ...] | None = None

    async def claim_reindex_job(
        self,
        worker_id: str,
        lease_seconds: int,
    ) -> ReindexJob | None:
        self.claim_arguments = (worker_id, lease_seconds)
        job, self.job = self.job, None
        return job

    async def get_reindex_source(
        self,
        tenant_id: str,
        document_id: str,
    ) -> ReindexSource:
        assert (tenant_id, document_id) == (TENANT_ID, DOCUMENT_ID)
        return self.source

    async def update_reindex_job_stage(self, job_id: str, stage: str) -> None:
        assert job_id == JOB_ID
        self.stages.append(stage)

    async def apply_document_embeddings(
        self,
        source: ReindexSource,
        embeddings: Sequence[Sequence[float]],
    ) -> None:
        self.applied = (source, embeddings)

    async def complete_reindex_job(self, job_id: str) -> None:
        assert job_id == JOB_ID
        self.completed = True

    async def fail_reindex_job(
        self,
        job_id: str,
        error_code: str,
        error_message: str,
        retry_after_seconds: int | None,
    ) -> None:
        self.failure = (job_id, error_code, error_message, retry_after_seconds)


class FakeEmbedding:
    def __init__(
        self,
        *,
        enabled: bool = True,
        dimension: int = 3,
        fails: bool = False,
    ) -> None:
        self.enabled = enabled
        self.dimension = dimension
        self.fails = fails
        self.batches: list[list[str]] = []

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        if self.fails:
            raise DependencyUnavailable("embedding service unavailable")
        return [[float(index)] * self.dimension for index, _text in enumerate(texts, start=1)]


def worker(
    repository: FakeRepository,
    embedding: FakeEmbedding,
) -> DocumentReindexWorker:
    return DocumentReindexWorker(
        repository,
        embedding,
        embedding_dimension=3,
        worker_id="test-worker",
        batch_size=2,
        retry_seconds=7,
        max_attempts=3,
    )


async def test_reindex_worker_builds_and_applies_embeddings() -> None:
    repository = FakeRepository()
    embedding = FakeEmbedding()

    processed = await worker(repository, embedding).run_once()

    assert processed is True
    assert repository.claim_arguments == ("test-worker", 120)
    assert embedding.batches == [["第一块", "第二块"], ["第三块"]]
    assert repository.applied is not None
    assert repository.applied[0] == repository.source
    assert len(repository.applied[1]) == 3
    assert repository.completed is True
    assert repository.failure is None
    assert repository.stages == [
        "EMBEDDING:0/3",
        "EMBEDDING:2/3",
        "EMBEDDING:3/3",
        "COMMITTING",
    ]


async def test_reindex_worker_retries_temporary_embedding_failure() -> None:
    repository = FakeRepository(attempt=1)
    embedding = FakeEmbedding(fails=True)

    processed = await worker(repository, embedding).run_once()

    assert processed is True
    assert repository.completed is False
    assert repository.failure == (
        JOB_ID,
        "EMBEDDING_UNAVAILABLE",
        "embedding service unavailable",
        7,
    )


async def test_reindex_worker_stops_retrying_after_max_attempts() -> None:
    repository = FakeRepository(attempt=3)
    embedding = FakeEmbedding(enabled=False)

    await worker(repository, embedding).run_once()

    assert repository.failure == (
        JOB_ID,
        "EMBEDDING_UNAVAILABLE",
        "embedding service is not configured",
        None,
    )


async def test_reindex_worker_rejects_wrong_embedding_dimension() -> None:
    repository = FakeRepository(attempt=3)
    embedding = FakeEmbedding(dimension=2)

    await worker(repository, embedding).run_once()

    assert repository.failure == (
        JOB_ID,
        "EMBEDDING_UNAVAILABLE",
        "embedding dimension does not match database schema",
        None,
    )


async def test_reindex_worker_returns_false_without_job() -> None:
    repository = FakeRepository()
    repository.job = None

    processed = await worker(repository, FakeEmbedding()).run_once()

    assert processed is False
    assert repository.applied is None
