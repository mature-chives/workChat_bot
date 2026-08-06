from __future__ import annotations

import asyncio
import logging
import signal

import grpc

from agent.adapters.models import OpenAIEmbeddingClient, OpenAILLMClient
from agent.adapters.repository import PostgresRepository
from agent.api.grpc_service import AgentGRPCService
from agent.application.query import QueryService
from agent.settings import get_settings
from agent.v1 import agent_pb2_grpc


async def serve() -> None:
    settings = get_settings()
    repository = await PostgresRepository.connect(settings.database_url)
    embedding = OpenAIEmbeddingClient(
        settings.embedding_base_url,
        settings.embedding_api_key,
        settings.embedding_model,
        settings.embedding_timeout_seconds,
    )
    llm = OpenAILLMClient(
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_model,
        settings.llm_timeout_seconds,
    )
    query_service = QueryService(repository, embedding, llm, settings)
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServiceServicer_to_server(
        AgentGRPCService(query_service, repository), server
    )
    if server.add_insecure_port(settings.grpc_bind) == 0:
        await repository.close()
        await embedding.close()
        await llm.close()
        raise RuntimeError(f"cannot bind Agent gRPC server to {settings.grpc_bind}")

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    await server.start()
    logging.getLogger(__name__).info("Agent gRPC listening on %s", settings.grpc_bind)
    try:
        await stopping.wait()
    finally:
        await server.stop(grace=10)
        await embedding.close()
        await llm.close()
        await repository.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(serve())


if __name__ == "__main__":
    main()
