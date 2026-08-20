from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from assistant_app import __version__
from assistant_app.api.router import api_router
from assistant_app.core.config import Settings, get_settings
from assistant_app.core.logging import configure_logging
from assistant_app.core.request_context import RequestContextMiddleware
from assistant_app.db.runtime import RuntimeDependencies


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level, app_settings.log_json)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        runtime = RuntimeDependencies(app_settings)
        application.state.settings = app_settings
        application.state.runtime = runtime
        yield
        await runtime.close()

    application = FastAPI(
        title=app_settings.app_name,
        version=__version__,
        docs_url="/docs" if app_settings.environment != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.add_middleware(RequestContextMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
    )
    application.include_router(api_router, prefix=app_settings.api_v1_prefix)

    @application.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": app_settings.app_name,
            "version": __version__,
            "health": f"{app_settings.api_v1_prefix}/health/live",
        }

    return application


app = create_app()

