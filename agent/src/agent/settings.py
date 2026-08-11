from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    grpc_bind: str = Field("0.0.0.0:50051", validation_alias="AGENT_GRPC_BIND")
    http_bind: str = Field("0.0.0.0", validation_alias="AGENT_HTTP_BIND")
    http_port: int = Field(8081, validation_alias="AGENT_HTTP_PORT")
    database_url: str = Field(..., validation_alias="AGENT_DATABASE_URL")
    tenant_id: str = Field(
        "00000000-0000-0000-0000-000000000001",
        validation_alias="AGENT_TENANT_ID",
    )
    admin_token: str | None = Field(None, validation_alias="AGENT_ADMIN_TOKEN")

    minio_endpoint: str = Field("127.0.0.1:19000", validation_alias="MINIO_ENDPOINT")
    minio_access_key: str = Field("minioadmin", validation_alias="MINIO_ACCESS_KEY")
    minio_secret_key: str = Field("minioadmin", validation_alias="MINIO_SECRET_KEY")
    minio_secure: bool = Field(False, validation_alias="MINIO_SECURE")
    minio_bucket: str = Field("workchat-documents", validation_alias="MINIO_BUCKET")

    llm_base_url: str | None = Field("https://api.deepseek.com", validation_alias="LLM_BASE_URL")
    llm_api_key: str = Field("", validation_alias="LLM_API_KEY")
    llm_model: str = Field("deepseek-v4-flash", validation_alias="LLM_MODEL")
    llm_api_mode: Literal["responses", "chat_completions"] = Field(
        "responses", validation_alias="LLM_API_MODE"
    )
    embedding_base_url: str | None = Field(None, validation_alias="EMBEDDING_BASE_URL")
    embedding_api_key: str = Field("local", validation_alias="EMBEDDING_API_KEY")
    embedding_model: str = Field("bge-m3", validation_alias="EMBEDDING_MODEL")

    allow_extractive_fallback: bool = Field(
        False, validation_alias="AGENT_ALLOW_EXTRACTIVE_FALLBACK"
    )
    evaluation_enabled: bool = Field(False, validation_alias="AGENT_EVALUATION_ENABLED")
    prompt_version: str = Field("answer-grounded-v1", validation_alias="AGENT_PROMPT_VERSION")
    retrieval_config_version: str = Field(
        "rag-default-v1", validation_alias="AGENT_RETRIEVAL_CONFIG_VERSION"
    )
    top_k_keyword: int = Field(10, ge=1, le=100)
    top_k_vector: int = Field(20, ge=1, le=100)
    top_k_final: int = Field(5, ge=1, le=20)
    llm_timeout_seconds: float = Field(60.0, gt=0, le=60)
    embedding_timeout_seconds: float = Field(30.0, gt=0, le=60)
    embedding_dimension: int = Field(1024, ge=1, le=8192)
    max_upload_bytes: int = Field(20 * 1024 * 1024, ge=1024)
    max_extracted_characters: int = Field(2_000_000, ge=1000)
    chunk_size: int = Field(800, ge=200, le=4000)
    chunk_overlap: int = Field(120, ge=0, le=1000)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
