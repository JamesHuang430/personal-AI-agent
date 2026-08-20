from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from assistant_app.api.routes.admin import router as admin_router
from assistant_app.api.routes.health import router as health_router
from assistant_app.app_factory import create_runtime_app
from assistant_app.core.config import Settings, get_settings

WEB_DIR = Path(__file__).parent / "web" / "admin"


def create_admin_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    application = create_runtime_app(app_settings, title="Personal AI Assistant Operations")
    router = APIRouter(prefix=app_settings.api_v1_prefix)
    router.include_router(health_router, prefix="/health", tags=["health"])
    router.include_router(admin_router, prefix="/admin", tags=["admin"])
    application.include_router(router)
    application.mount("/static", StaticFiles(directory=WEB_DIR), name="admin-static")

    @application.get("/", include_in_schema=False, response_class=FileResponse)
    async def root() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return application


app = create_admin_app()
