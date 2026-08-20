from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import select

from assistant_app.core.security import session_digest
from assistant_app.db.models import User
from assistant_app.db.runtime import RuntimeDependencies

USER_SESSION_COOKIE = "assistant_session"
ADMIN_SESSION_COOKIE = "assistant_admin_session"


async def current_user(request: Request) -> User:
    token = request.cookies.get(USER_SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")

    runtime: RuntimeDependencies = request.app.state.runtime
    user_id = await runtime.redis.get(f"session:user:{session_digest(token)}")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")

    async with runtime.sessions() as session:
        user = await session.scalar(select(User).where(User.id == UUID(user_id)))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被停用")
    return user


async def current_admin(request: Request) -> str:
    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录运营后台")

    runtime: RuntimeDependencies = request.app.state.runtime
    username = await runtime.redis.get(f"session:admin:{session_digest(token)}")
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")
    return username
