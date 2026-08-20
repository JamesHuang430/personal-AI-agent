from __future__ import annotations

import asyncio
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from assistant_app.core.config import Settings


@dataclass(frozen=True)
class DependencyStatus:
    status: str
    detail: str | None = None


class RuntimeDependencies:
    """Owns process-level connection pools and readiness checks."""

    def __init__(self, settings: Settings) -> None:
        self._timeout = settings.dependency_timeout_seconds
        self.database: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
        )
        self.redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)

    async def close(self) -> None:
        await self.redis.aclose()
        await self.database.dispose()

    async def check_database(self) -> DependencyStatus:
        try:
            async with asyncio.timeout(self._timeout):
                async with self.database.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            return DependencyStatus(status="ok")
        except Exception as exc:  # readiness must report, not crash the process
            return DependencyStatus(status="error", detail=type(exc).__name__)

    async def check_redis(self) -> DependencyStatus:
        try:
            async with asyncio.timeout(self._timeout):
                await self.redis.ping()
            return DependencyStatus(status="ok")
        except Exception as exc:  # readiness must report, not crash the process
            return DependencyStatus(status="error", detail=type(exc).__name__)

    async def readiness(self) -> dict[str, DependencyStatus]:
        database, redis = await asyncio.gather(self.check_database(), self.check_redis())
        return {"database": database, "redis": redis}

