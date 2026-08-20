from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select, text

from assistant_app.api.dependencies import USER_SESSION_COOKIE, current_user
from assistant_app.core.security import (
    hash_password,
    new_session_token,
    normalize_email,
    validate_password,
    verify_password,
)
from assistant_app.core.time import local_today, utc_day_bounds
from assistant_app.db.models import DailyCheckin, User
from assistant_app.db.runtime import RuntimeDependencies

router = APIRouter()
DAILY_REGISTRATION_LIMIT = 3


class RegisterPayload(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: str) -> str:
        return validate_password(value)


class LoginPayload(RegisterPayload):
    pass


def _set_session_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        USER_SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=False,
        samesite="lax",
        path="/",
    )


async def _session_payload(runtime: RuntimeDependencies, user: User) -> dict[str, object]:
    async with runtime.sessions() as session:
        checked_in = await session.scalar(
            select(DailyCheckin.id).where(
                DailyCheckin.user_id == user.id,
                DailyCheckin.checkin_date == local_today(),
            )
        )
    return {
        "id": str(user.id),
        "email": user.email,
        "points": user.points,
        "is_active": user.is_active,
        "checked_in_today": checked_in is not None,
        "created_at": user.created_at.isoformat(),
    }


async def _create_session(request: Request, response: Response, user: User) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    token, digest = new_session_token()
    ttl = request.app.state.settings.session_ttl_seconds
    await runtime.redis.set(f"session:user:{digest}", str(user.id), ex=ttl)
    _set_session_cookie(response, token, ttl)
    return await _session_payload(runtime, user)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterPayload, request: Request, response: Response
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    day_start, day_end = utc_day_bounds()

    async with runtime.sessions() as session, session.begin():
        # Serialize registration attempts for a strict global daily cap.
        await session.execute(text("SELECT pg_advisory_xact_lock(2026082003)"))
        existing = await session.scalar(select(User.id).where(User.email == payload.email))
        if existing is not None:
            raise HTTPException(status_code=409, detail="该邮箱已注册")

        registrations_today = await session.scalar(
            select(func.count(User.id)).where(
                User.created_at >= day_start,
                User.created_at < day_end,
            )
        )
        if (registrations_today or 0) >= DAILY_REGISTRATION_LIMIT:
            raise HTTPException(status_code=429, detail="今日注册名额已满，请明天再试")

        user = User(email=payload.email, password_hash=hash_password(payload.password))
        session.add(user)
        await session.flush()

    return await _create_session(request, response, user)


@router.post("/login")
async def login(payload: LoginPayload, request: Request, response: Response) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        user = await session.scalar(select(User).where(User.email == payload.email))
        if user is None or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="邮箱或密码错误")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账号已被停用")
        user.last_login_at = datetime.now(UTC)

    return await _create_session(request, response, user)


@router.get("/session")
async def session_status(
    request: Request, user: Annotated[User, Depends(current_user)]
) -> dict[str, object]:
    return await _session_payload(request.app.state.runtime, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(USER_SESSION_COOKIE)
    if token:
        from assistant_app.core.security import session_digest

        runtime: RuntimeDependencies = request.app.state.runtime
        await runtime.redis.delete(f"session:user:{session_digest(token)}")
    response.delete_cookie(USER_SESSION_COOKIE, path="/")
