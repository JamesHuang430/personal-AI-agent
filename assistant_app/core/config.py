from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
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

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def reject_placeholder_secrets_in_production(self) -> Settings:
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
