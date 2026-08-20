from fastapi import APIRouter

from assistant_app.api.routes.auth import router as auth_router
from assistant_app.api.routes.chat import router as chat_router
from assistant_app.api.routes.health import router as health_router
from assistant_app.api.routes.packages import router as packages_router
from assistant_app.api.routes.users import router as users_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(packages_router, prefix="/packages", tags=["packages"])
api_router.include_router(chat_router, prefix="/chat", tags=["chat"])
