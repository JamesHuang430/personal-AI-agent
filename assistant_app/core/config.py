from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from ``ASSISTANT_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ASSISTANT_",
        extra="ignore",
    )

    app_name: str = "Personal AI Assistant"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = False
    api_v1_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    database_url: str = "postgresql+asyncpg://assistant:assistant@localhost:5432/assistant"
    redis_url: str = "redis://localhost:6379/0"
    dependency_timeout_seconds: float = 3.0
    admin_username: str = "admin"
    admin_password: str = "change-this-admin-password"
    secret_key: str = "change-this-application-secret-key"
    session_ttl_seconds: int = 604800
    public_url: str = "http://127.0.0.1:18000"
    memory_enabled: bool = True
    memory_embedding_provider: Literal["local", "channel"] = "local"
    memory_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    memory_embedding_cache: str = ".cache/fastembed"
    memory_embedding_local_files_only: bool = False
    memory_embedding_threads: int = 2
    memory_retrieval_limit: int = 12
    memory_graph_limit: int = 200
    web_search_enabled: bool = True
    web_search_base_url: str = "http://searxng:8080"
    web_search_max_results: int = Field(default=6, ge=1, le=10)
    web_search_timeout_seconds: float = Field(default=15.0, ge=3.0, le=60.0)
    web_fetch_max_bytes: int = Field(default=1_500_000, ge=100_000, le=5_000_000)
    web_fetch_max_chars: int = Field(default=12_000, ge=1_000, le=30_000)
    mcp_enabled: bool = True
    mcp_markitdown_url: str = "http://markitdown-mcp:3001/mcp"
    mcp_timeout_seconds: float = Field(default=30.0, ge=3.0, le=120.0)
    mcp_max_result_chars: int = Field(default=30_000, ge=2_000, le=100_000)
    document_max_bytes: int = Field(default=20_000_000, ge=100_000, le=50_000_000)
    document_max_files_per_message: int = Field(default=4, ge=1, le=8)
    agent_runtime: Literal["python", "pi"] = "python"
    pi_runtime_url: str = "http://pi-runtime:8787"
    pi_runtime_tool_bridge_url: str = (
        "http://assistant-api:8000/api/v1/internal/pi/tools/execute"
    )
    pi_runtime_shared_secret: str = ""
    pi_runtime_timeout_seconds: float = Field(default=120.0, ge=10.0, le=300.0)

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def reject_placeholder_secrets_in_production(self) -> Settings:
        if self.agent_runtime == "pi" and (
            len(self.pi_runtime_shared_secret) < 32
            or "change-this" in self.pi_runtime_shared_secret.lower()
        ):
            raise ValueError(
                "Pi runtime requires a dedicated shared secret of at least 32 characters"
            )
        if self.environment != "production":
            return self

        sensitive_values = {
            "database_url": self.database_url,
            "redis_url": self.redis_url,
            "admin_password": self.admin_password,
            "secret_key": self.secret_key,
        }

        invalid = [
            name
            for name, value in sensitive_values.items()
            if not value or "change-this" in value.lower()
        ]
        if invalid:
            fields = ", ".join(sorted(invalid))
            raise ValueError(f"Production secrets are missing or still placeholders: {fields}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
