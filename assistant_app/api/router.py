from fastapi import APIRouter

from assistant_app.api.routes.auth import router as auth_router
from assistant_app.api.routes.chat import router as chat_router
from assistant_app.api.routes.director import router as director_router
from assistant_app.api.routes.files import router as files_router
from assistant_app.api.routes.health import router as health_router
from assistant_app.api.routes.internal_pi import router as internal_pi_router
from assistant_app.api.routes.memory import router as memory_router
from assistant_app.api.routes.music import router as music_router
from assistant_app.api.routes.packages import router as packages_router
from assistant_app.api.routes.speech import router as speech_router
from assistant_app.api.routes.users import router as users_router
from assistant_app.api.routes.videos import router as videos_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(internal_pi_router, prefix="/internal/pi", tags=["internal"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(packages_router, prefix="/packages", tags=["packages"])
api_router.include_router(chat_router, prefix="/chat", tags=["chat"])
api_router.include_router(director_router, prefix="/director", tags=["director"])
api_router.include_router(memory_router, prefix="/memory", tags=["memory"])
api_router.include_router(files_router, prefix="/files", tags=["files"])
api_router.include_router(videos_router, prefix="/videos", tags=["videos"])
api_router.include_router(music_router, prefix="/music", tags=["music"])
api_router.include_router(speech_router, prefix="/speech", tags=["speech"])
