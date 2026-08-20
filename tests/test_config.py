import pytest
from pydantic import ValidationError

from assistant_app.core.config import Settings


def test_cors_origins_are_normalized() -> None:
    settings = Settings(_env_file=None, cors_origins="http://a.test, http://b.test, ")

    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]


def test_production_rejects_placeholder_secrets() -> None:
    with pytest.raises(ValidationError, match="Production secrets"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+asyncpg://assistant:change-this@db/assistant",
            redis_url="redis://redis/0",
        )
