from __future__ import annotations

import httpx
import pytest

from agent.adapters.models import OpenAIEmbeddingClient, OpenAILLMClient
from agent.application.models import DependencyUnavailable


async def test_embedding_probe_returns_model_dimension() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )

    client = OpenAIEmbeddingClient(
        "http://models.local/v1",
        "test-key",
        "bge-m3",
        1,
        transport=httpx.MockTransport(handler),
    )
    try:
        dimension = await client.probe()
    finally:
        await client.close()

    assert dimension == 3


async def test_llm_probe_accepts_default_model_tag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "Qwen3.5-4B:latest"}]})

    client = OpenAILLMClient(
        "http://models.local/v1",
        "test-key",
        "Qwen3.5-4B",
        1,
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.probe()
    finally:
        await client.close()


async def test_llm_probe_rejects_missing_model() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"data": [{"id": "another-model"}]})
    )
    client = OpenAILLMClient(
        "http://models.local/v1",
        "test-key",
        "Qwen3.5-4B",
        1,
        transport=transport,
    )
    try:
        with pytest.raises(DependencyUnavailable, match="model not found"):
            await client.probe()
    finally:
        await client.close()
