from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from assistant_app import __version__
from assistant_app.core.config import Settings
from assistant_app.core.logging import configure_logging
from assistant_app.core.request_context import RequestContextMiddleware
from assistant_app.db.runtime import RuntimeDependencies


def create_runtime_app(settings: Settings, title: str | None = None) -> FastAPI:
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        runtime = RuntimeDependencies(settings)
        application.state.settings = settings
        application.state.runtime = runtime
        yield
        await runtime.close()

    application = FastAPI(
        title=title or settings.app_name,
        version=__version__,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.add_middleware(RequestContextMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
    )
    return application
