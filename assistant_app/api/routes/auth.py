from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text

from assistant_app.api.dependencies import USER_SESSION_COOKIE, current_user
from assistant_app.core.encryption import decrypt_secret
from assistant_app.core.security import (
    hash_password,
    new_session_token,
    normalize_email,
    session_digest,
    validate_password,
    verify_password,
)
from assistant_app.core.time import local_today, utc_day_bounds
from assistant_app.db.models import DailyCheckin, EmailChannel, User
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.auth_verification import (
    consume_password_reset_token,
    create_captcha,
    create_password_reset_token,
    enforce_rate_limit,
    new_registration_code,
    privacy_key,
    store_registration_code,
    verify_captcha,
    verify_registration_code,
)
from assistant_app.services.email_delivery import (
    EmailDeliveryError,
    SmtpConnection,
    send_password_reset_link,
    send_registration_code,
)

router = APIRouter()
DAILY_REGISTRATION_LIMIT = 3


class EmailPayload(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)


class CaptchaFields(BaseModel):
    captcha_id: str = Field(min_length=16, max_length=200)
    captcha_answer: str = Field(min_length=1, max_length=12)


class RegisterCodePayload(EmailPayload, CaptchaFields):
    pass


class RegisterPayload(EmailPayload):
    password: str
    email_code: str = Field(pattern=r"^\d{6}$")

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: str) -> str:
        return validate_password(value)


class LoginPayload(EmailPayload, CaptchaFields):
    password: str

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: str) -> str:
        return validate_password(value)


class PasswordResetRequestPayload(EmailPayload, CaptchaFields):
    pass


class PasswordResetConfirmPayload(BaseModel):
    token: str = Field(min_length=32, max_length=300)
    password: str

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: str) -> str:
        return validate_password(value)


def _set_session_cookie(response: Response, token: str, max_age: int, secure: bool) -> None:
    response.set_cookie(
        USER_SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _client_identity(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _require_captcha(request: Request, challenge_id: str, answer: str) -> None:
    runtime: RuntimeDependencies = request.app.state.runtime
    valid = await verify_captcha(
        runtime.redis,
        request.app.state.settings.secret_key,
        challenge_id,
        answer,
    )
    if not valid:
        raise HTTPException(status_code=400, detail="验证码错误或已过期，请刷新后重试")


async def _active_smtp(request: Request) -> SmtpConnection:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        channel = await session.scalar(
            select(EmailChannel).where(EmailChannel.is_active.is_(True))
        )
    if channel is None:
        raise HTTPException(status_code=503, detail="运营后台尚未启用邮件渠道")
    try:
        auth_code = decrypt_secret(
            channel.encrypted_auth_code,
            request.app.state.settings.secret_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="邮件渠道授权码无法解密，请重新配置") from exc
    return SmtpConnection(
        host=channel.smtp_host,
        port=channel.smtp_port,
        username=channel.smtp_username,
        auth_code=auth_code,
        from_name=channel.from_name,
        use_ssl=channel.use_ssl,
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
    _set_session_cookie(
        response,
        token,
        ttl,
        secure=request.app.state.settings.environment == "production",
    )
    return await _session_payload(runtime, user)


async def _revoke_user_sessions(runtime: RuntimeDependencies, user_id: UUID) -> None:
    keys_to_delete: list[str] = []
    async for key in runtime.redis.scan_iter(match="session:user:*", count=200):
        if await runtime.redis.get(key) == str(user_id):
            keys_to_delete.append(key)
    if keys_to_delete:
        await runtime.redis.delete(*keys_to_delete)


@router.get("/captcha")
async def captcha(request: Request, response: Response) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    challenge = await create_captcha(runtime.redis, request.app.state.settings.secret_key)
    response.headers["Cache-Control"] = "no-store"
    return {
        "captcha_id": challenge.id,
        "question": challenge.question,
        "expires_in": challenge.expires_in,
    }


@router.post("/register/email-code", status_code=status.HTTP_202_ACCEPTED)
async def request_registration_code(
    payload: RegisterCodePayload, request: Request
) -> dict[str, str]:
    await _require_captcha(request, payload.captcha_id, payload.captcha_answer)
    connection = await _active_smtp(request)
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        existing = await session.scalar(select(User.id).where(User.email == payload.email))
    if existing is not None:
        raise HTTPException(status_code=409, detail="该邮箱已注册")

    email_key = privacy_key(payload.email)
    ip_key = privacy_key(_client_identity(request))
    email_allowed = await enforce_rate_limit(
        runtime.redis, f"auth:register-email-hour:{email_key}", 5, 3600
    )
    ip_allowed = await enforce_rate_limit(
        runtime.redis, f"auth:register-ip-hour:{ip_key}", 20, 3600
    )
    cooldown_key = f"auth:register-cooldown:{email_key}"
    cooldown = await runtime.redis.set(cooldown_key, "1", ex=60, nx=True)
    if not email_allowed or not ip_allowed or not cooldown:
        raise HTTPException(status_code=429, detail="验证码发送过于频繁，请稍后再试")

    code = new_registration_code()
    await store_registration_code(
        runtime.redis,
        request.app.state.settings.secret_key,
        payload.email,
        code,
    )
    try:
        await send_registration_code(
            connection,
            payload.email,
            code,
            request.app.state.settings.app_name,
        )
    except EmailDeliveryError as exc:
        await runtime.redis.delete(
            f"auth:register-code:{privacy_key(payload.email)}",
            cooldown_key,
        )
        raise HTTPException(status_code=502, detail="邮件发送失败，请检查运营后台邮件配置") from exc
    return {"message": "验证码已发送，请检查邮箱"}


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterPayload, request: Request, response: Response
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    code_valid = await verify_registration_code(
        runtime.redis,
        request.app.state.settings.secret_key,
        payload.email,
        payload.email_code,
    )
    if not code_valid:
        raise HTTPException(status_code=400, detail="邮箱验证码错误或已过期")

    day_start, day_end = utc_day_bounds()
    async with runtime.sessions() as session, session.begin():
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
    await _require_captcha(request, payload.captcha_id, payload.captcha_answer)
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        user = await session.scalar(select(User).where(User.email == payload.email))
        if user is None or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="邮箱或密码错误")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账号已被停用")
        user.last_login_at = datetime.now(UTC)

    return await _create_session(request, response, user)


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    payload: PasswordResetRequestPayload, request: Request
) -> dict[str, str]:
    await _require_captcha(request, payload.captcha_id, payload.captcha_answer)
    connection = await _active_smtp(request)
    runtime: RuntimeDependencies = request.app.state.runtime
    email_key = privacy_key(payload.email)
    ip_key = privacy_key(_client_identity(request))
    email_allowed = await enforce_rate_limit(
        runtime.redis, f"auth:reset-email-hour:{email_key}", 3, 3600
    )
    ip_allowed = await enforce_rate_limit(runtime.redis, f"auth:reset-ip-hour:{ip_key}", 12, 3600)
    if not email_allowed or not ip_allowed:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    async with runtime.sessions() as session:
        user = await session.scalar(select(User).where(User.email == payload.email))
    if user is not None and user.is_active:
        token = await create_password_reset_token(runtime.redis, str(user.id))
        try:
            settings = request.app.state.settings
            await send_password_reset_link(
                connection,
                user.email,
                token,
                settings.app_name,
                settings.public_url,
            )
        except EmailDeliveryError as exc:
            await consume_password_reset_token(runtime.redis, token)
            raise HTTPException(
                status_code=502,
                detail="邮件发送失败，请检查运营后台邮件配置",
            ) from exc

    return {"message": "如果该邮箱已注册，重置链接将发送到邮箱"}


@router.post("/password-reset/confirm")
async def confirm_password_reset(
    payload: PasswordResetConfirmPayload, request: Request
) -> dict[str, str]:
    runtime: RuntimeDependencies = request.app.state.runtime
    user_id_text = await consume_password_reset_token(runtime.redis, payload.token)
    if not user_id_text:
        raise HTTPException(status_code=400, detail="重置链接无效或已过期")

    try:
        user_id = UUID(user_id_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="重置链接无效或已过期") from exc

    async with runtime.sessions() as session, session.begin():
        user = await session.get(User, user_id, with_for_update=True)
        if user is None or not user.is_active:
            raise HTTPException(status_code=400, detail="重置链接无效或已过期")
        user.password_hash = hash_password(payload.password)

    await _revoke_user_sessions(runtime, user_id)
    return {"message": "密码已重置，请使用新密码登录"}


@router.get("/session")
async def session_status(
    request: Request, user: Annotated[User, Depends(current_user)]
) -> dict[str, object]:
    return await _session_payload(request.app.state.runtime, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(USER_SESSION_COOKIE)
    if token:
        runtime: RuntimeDependencies = request.app.state.runtime
        await runtime.redis.delete(f"session:user:{session_digest(token)}")
    response.delete_cookie(USER_SESSION_COOKIE, path="/")
