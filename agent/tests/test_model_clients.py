from __future__ import annotations

import json

import httpx
import pytest

from agent.adapters.models import OpenAIEmbeddingClient, OpenAILLMClient
from agent.application.models import Candidate, DependencyUnavailable
from agent.settings import Settings


def candidate() -> Candidate:
    return Candidate(
        chunk_id="chunk-1",
        document_id="document-1",
        document_version_id="version-1",
        document_version_number=1,
        title="Normandy",
        content="Normandy is a region in France.",
        content_hash="hash-1",
        locator_type="CHUNK",
        locator_value="1",
        effective_at=None,
        score=1.0,
    )


def test_settings_default_to_deepseek_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_API_MODE"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(AGENT_DATABASE_URL="postgresql://unused")

    assert settings.llm_base_url == "https://api.deepseek.com"
    assert settings.llm_api_key == ""
    assert settings.llm_model == "deepseek-v4-flash"
    assert settings.llm_api_mode == "responses"


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
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "Qwen3.5-4B:latest",
                        "object": "model",
                        "created": 0,
                        "owned_by": "test",
                    }
                ],
            },
        )

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


async def test_llm_responses_mode_generates_grounded_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/responses"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-v4-flash"
        assert "最短结论" in payload["instructions"]
        assert "Normandy is a region in France." in payload["input"]
        return httpx.Response(
            200,
            json={
                "id": "resp-1",
                "object": "response",
                "created_at": 0,
                "model": "deepseek-v4-flash",
                "status": "completed",
                "output": [
                    {
                        "id": "message-1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    '{"answer":"France [1]","citation_indexes":[1],'
                                    '"refused":false,"refusal_reason":""}'
                                ),
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    client = OpenAILLMClient(
        "http://models.local",
        "test-key",
        "deepseek-v4-flash",
        1,
        api_mode="responses",
        transport=httpx.MockTransport(handler),
    )
    try:
        generated = await client.generate(
            "In what country is Normandy located?",
            [candidate()],
        )
    finally:
        await client.close()

    assert generated.answer == "France [1]"
    assert generated.citation_indexes == (1,)
    assert generated.refused is False


async def test_llm_chat_completions_mode_remains_supported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3.5:4b"
        return httpx.Response(
            200,
            json={
                "id": "chat-1",
                "object": "chat.completion",
                "created": 0,
                "model": "qwen3.5:4b",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": (
                                '{"answer":"France [1]","citation_indexes":[1],'
                                '"refused":false,"refusal_reason":""}'
                            ),
                        },
                    }
                ],
            },
        )

    client = OpenAILLMClient(
        "http://models.local/v1",
        "local",
        "qwen3.5:4b",
        1,
        api_mode="chat_completions",
        transport=httpx.MockTransport(handler),
    )
    try:
        generated = await client.generate(
            "In what country is Normandy located?",
            [candidate()],
        )
    finally:
        await client.close()

    assert generated.answer == "France [1]"
    assert generated.citation_indexes == (1,)


async def test_llm_is_disabled_without_api_key() -> None:
    client = OpenAILLMClient(
        "https://api.deepseek.com",
        "",
        "deepseek-v4-flash",
        1,
    )
    try:
        assert client.enabled is False
        with pytest.raises(DependencyUnavailable, match="not configured"):
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
