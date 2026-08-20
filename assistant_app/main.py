from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from assistant_app.api.router import api_router
from assistant_app.app_factory import create_runtime_app
from assistant_app.core.config import Settings, get_settings

WEB_DIR = Path(__file__).parent / "web" / "user"


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    application = create_runtime_app(app_settings)
    application.include_router(api_router, prefix=app_settings.api_v1_prefix)
    application.mount("/static", StaticFiles(directory=WEB_DIR), name="user-static")

    @application.get("/", include_in_schema=False, response_class=FileResponse)
    async def root() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return application


app = create_app()
