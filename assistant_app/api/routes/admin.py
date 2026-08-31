from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select, update

from assistant_app.api.dependencies import ADMIN_SESSION_COOKIE, current_admin
from assistant_app.core.encryption import decrypt_secret, encrypt_secret
from assistant_app.core.security import new_session_token, normalize_email, session_digest
from assistant_app.core.time import utc_day_bounds
from assistant_app.db.models import (
    EmailChannel,
    ModelChannel,
    MusicChannel,
    Package,
    PointLedger,
    RequestLog,
    SpeechChannel,
    User,
    VideoChannel,
)
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.auth_verification import create_captcha, verify_captcha
from assistant_app.services.email_delivery import (
    EmailDeliveryError,
    SmtpConnection,
    send_test_email,
)

router = APIRouter()


class AdminLoginPayload(BaseModel):
    username: str
    password: str
    captcha_id: str = Field(min_length=16, max_length=200)
    captcha_answer: str = Field(min_length=1, max_length=12)


class UserStatusPayload(BaseModel):
    is_active: bool


class PointsPayload(BaseModel):
    delta: int = Field(ge=-1_000_000, le=1_000_000)
    note: str = Field(default="运营调整", max_length=500)


class PackageCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    price_yuan: int = Field(ge=1, le=1_000_000)
    points: int = Field(ge=1, le=100_000_000)
    is_active: bool = True
    sort_order: int = Field(default=0, ge=-10_000, le=10_000)


class PackageUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    price_yuan: int | None = Field(default=None, ge=1, le=1_000_000)
    points: int | None = Field(default=None, ge=1, le=100_000_000)
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=-10_000, le=10_000)


def _validate_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("请输入有效的 HTTP(S) Base URL")
    return normalized


class ChannelCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str
    api_key: str = Field(min_length=1, max_length=2000)
    qps_limit: int = Field(default=2, ge=1, le=1000)
    is_active: bool = False

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        return _validate_base_url(value)

class ChannelUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=2000)
    qps_limit: int | None = Field(default=None, ge=1, le=1000)

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str | None) -> str | None:
        return _validate_base_url(value) if value is not None else None

class VideoChannelCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str
    api_key: str = Field(min_length=1, max_length=2000)
    model_name: str = Field(default="sora-2", min_length=1, max_length=200)
    provider: Literal["openai", "minimax"] = "openai"
    qps_limit: int = Field(default=1, ge=1, le=1000)
    default_seconds: Literal["4", "8", "12"] = "4"
    default_size: Literal["720x1280", "1280x720", "1024x1792", "1792x1024"] = "1280x720"
    default_resolution: Literal["768P", "2K"] = "768P"
    is_active: bool = False

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        return _validate_base_url(value)


class VideoChannelUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=2000)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    provider: Literal["openai", "minimax"] | None = None
    qps_limit: int | None = Field(default=None, ge=1, le=1000)
    default_seconds: Literal["4", "8", "12"] | None = None
    default_size: Literal["720x1280", "1280x720", "1024x1792", "1792x1024"] | None = None
    default_resolution: Literal["768P", "2K"] | None = None

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str | None) -> str | None:
        return _validate_base_url(value) if value is not None else None


class MusicChannelCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str
    api_key: str = Field(min_length=1, max_length=2000)
    model_name: str = Field(default="music-2.6", min_length=1, max_length=200)
    qps_limit: int = Field(default=1, ge=1, le=1000)
    default_format: Literal["mp3", "wav", "flac"] = "mp3"
    is_active: bool = False

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        return _validate_base_url(value)


class MusicChannelUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=2000)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    qps_limit: int | None = Field(default=None, ge=1, le=1000)
    default_format: Literal["mp3", "wav", "flac"] | None = None

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str | None) -> str | None:
        return _validate_base_url(value) if value is not None else None


class SpeechChannelCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str
    api_key: str = Field(min_length=1, max_length=2000)
    model_name: str = Field(default="speech-2.8-hd", min_length=1, max_length=200)
    default_voice_id: str = Field(default="male-qn-qingse", min_length=1, max_length=200)
    qps_limit: int = Field(default=1, ge=1, le=1000)
    default_format: Literal["mp3", "wav", "flac"] = "mp3"
    is_active: bool = False

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        return _validate_base_url(value)


class SpeechChannelUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=2000)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    default_voice_id: str | None = Field(default=None, min_length=1, max_length=200)
    qps_limit: int | None = Field(default=None, ge=1, le=1000)
    default_format: Literal["mp3", "wav", "flac"] | None = None

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str | None) -> str | None:
        return _validate_base_url(value) if value is not None else None


class EmailChannelPayload(BaseModel):
    name: str = Field(default="163 SMTP", min_length=1, max_length=100)
    smtp_host: str = Field(default="smtp.163.com", min_length=1, max_length=255)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_username: str
    auth_code: str | None = Field(default=None, min_length=1, max_length=1000)
    from_name: str = Field(default="知伴 AI", min_length=1, max_length=100)
    use_ssl: bool = True
    is_active: bool = True

    @field_validator("smtp_username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("smtp_host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("请输入有效的 SMTP 主机")
        return normalized


class EmailTestPayload(BaseModel):
    recipient: str

    @field_validator("recipient")
    @classmethod
    def valid_recipient(cls, value: str) -> str:
        return normalize_email(value)


def _user_payload(user: User) -> dict[str, object]:
    return {
        "id": str(user.id),
        "email": user.email,
        "points": user.points,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def _package_payload(item: Package) -> dict[str, object]:
    return {
        "id": str(item.id),
        "name": item.name,
        "price_yuan": item.price_cents // 100,
        "points": item.points,
        "is_active": item.is_active,
        "sort_order": item.sort_order,
        "updated_at": item.updated_at.isoformat(),
    }


def _channel_payload(item: ModelChannel) -> dict[str, object]:
    return {
        "id": str(item.id),
        "name": item.name,
        "base_url": item.base_url,
        "qps_limit": item.qps_limit,
        "is_active": item.is_active,
        "api_key_configured": bool(item.encrypted_api_key),
        "updated_at": item.updated_at.isoformat(),
    }


def _video_channel_payload(item: VideoChannel) -> dict[str, object]:
    return {
        "id": str(item.id),
        "name": item.name,
        "base_url": item.base_url,
        "model_name": item.model_name,
        "provider": item.provider,
        "qps_limit": item.qps_limit,
        "default_seconds": item.default_seconds,
        "default_size": item.default_size,
        "default_resolution": item.default_resolution,
        "is_active": item.is_active,
        "api_key_configured": bool(item.encrypted_api_key),
        "updated_at": item.updated_at.isoformat(),
    }


def _music_channel_payload(item: MusicChannel) -> dict[str, object]:
    return {
        "id": str(item.id),
        "name": item.name,
        "base_url": item.base_url,
        "model_name": item.model_name,
        "qps_limit": item.qps_limit,
        "default_format": item.default_format,
        "is_active": item.is_active,
        "api_key_configured": bool(item.encrypted_api_key),
        "updated_at": item.updated_at.isoformat(),
    }


def _speech_channel_payload(item: SpeechChannel) -> dict[str, object]:
    return {
        "id": str(item.id),
        "name": item.name,
        "base_url": item.base_url,
        "model_name": item.model_name,
        "default_voice_id": item.default_voice_id,
        "qps_limit": item.qps_limit,
        "default_format": item.default_format,
        "is_active": item.is_active,
        "api_key_configured": bool(item.encrypted_api_key),
        "updated_at": item.updated_at.isoformat(),
    }


def _email_channel_payload(item: EmailChannel | None) -> dict[str, object]:
    if item is None:
        return {"configured": False}
    return {
        "configured": True,
        "id": str(item.id),
        "name": item.name,
        "smtp_host": item.smtp_host,
        "smtp_port": item.smtp_port,
        "smtp_username": item.smtp_username,
        "from_name": item.from_name,
        "use_ssl": item.use_ssl,
        "is_active": item.is_active,
        "auth_code_configured": bool(item.encrypted_auth_code),
        "updated_at": item.updated_at.isoformat(),
    }


def _request_log_payload(item: RequestLog, *, detail: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(item.id),
        "request_id": item.request_id,
        "category": item.category,
        "source": item.source,
        "actor": item.actor,
        "method": item.method,
        "path": item.path,
        "status_code": item.status_code,
        "duration_ms": item.duration_ms,
        "model_name": item.model_name,
        "has_error": bool(item.error_message),
        "created_at": item.created_at.isoformat(),
    }
    if detail:
        payload.update(
            {
                "input_payload": item.input_payload,
                "output_payload": item.output_payload,
                "error_message": item.error_message,
            }
        )
    return payload


def _smtp_connection(item: EmailChannel, secret_key: str) -> SmtpConnection:
    return SmtpConnection(
        host=item.smtp_host,
        port=item.smtp_port,
        username=item.smtp_username,
        auth_code=decrypt_secret(item.encrypted_auth_code, secret_key),
        from_name=item.from_name,
        use_ssl=item.use_ssl,
    )


@router.get("/auth/captcha")
async def admin_captcha(request: Request, response: Response) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    challenge = await create_captcha(runtime.redis, request.app.state.settings.secret_key)
    response.headers["Cache-Control"] = "no-store"
    return {
        "captcha_id": challenge.id,
        "question": challenge.question,
        "expires_in": challenge.expires_in,
    }


@router.post("/auth/login")
async def admin_login(
    payload: AdminLoginPayload, request: Request, response: Response
) -> dict[str, str]:
    settings = request.app.state.settings
    runtime: RuntimeDependencies = request.app.state.runtime
    captcha_valid = await verify_captcha(
        runtime.redis,
        settings.secret_key,
        payload.captcha_id,
        payload.captcha_answer,
    )
    if not captcha_valid:
        raise HTTPException(status_code=400, detail="验证码错误或已过期，请刷新后重试")
    valid = hmac.compare_digest(payload.username, settings.admin_username) and hmac.compare_digest(
        payload.password, settings.admin_password
    )
    if not valid:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token, digest = new_session_token()
    await runtime.redis.set(
        f"session:admin:{digest}", settings.admin_username, ex=settings.session_ttl_seconds
    )
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        path="/",
    )
    return {"username": settings.admin_username}


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def admin_logout(request: Request, response: Response) -> None:
    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if token:
        runtime: RuntimeDependencies = request.app.state.runtime
        await runtime.redis.delete(f"session:admin:{session_digest(token)}")
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")


@router.get("/auth/session")
async def admin_session(username: Annotated[str, Depends(current_admin)]) -> dict[str, str]:
    return {"username": username}


@router.get("/stats")
async def stats(request: Request, _admin: Annotated[str, Depends(current_admin)]) -> dict[str, int]:
    runtime: RuntimeDependencies = request.app.state.runtime
    day_start, day_end = utc_day_bounds()
    async with runtime.sessions() as session:
        total = await session.scalar(select(func.count(User.id))) or 0
        active = (
            await session.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0
        )
        today = (
            await session.scalar(
                select(func.count(User.id)).where(
                    User.created_at >= day_start, User.created_at < day_end
                )
            )
            or 0
        )
        package_count = (
            await session.scalar(select(func.count(Package.id)).where(Package.is_active.is_(True)))
            or 0
        )
    return {
        "total_users": total,
        "active_users": active,
        "registrations_today": today,
        "active_packages": package_count,
    }


@router.get("/request-logs")
async def request_logs(
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
    category: Literal["all", "http", "model"] = "all",
    status_code: int | None = Query(default=None, ge=100, le=599),
    query: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    filters = []
    if category != "all":
        filters.append(RequestLog.category == category)
    if status_code is not None:
        filters.append(RequestLog.status_code == status_code)
    if query.strip():
        pattern = f"%{query.strip()}%"
        filters.append(
            or_(
                RequestLog.request_id.ilike(pattern),
                RequestLog.source.ilike(pattern),
                RequestLog.path.ilike(pattern),
                RequestLog.actor.ilike(pattern),
                RequestLog.model_name.ilike(pattern),
            )
        )
    async with runtime.sessions() as session:
        total = await session.scalar(select(func.count(RequestLog.id)).where(*filters)) or 0
        rows = (
            await session.scalars(
                select(RequestLog)
                .where(*filters)
                .order_by(RequestLog.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    return {
        "items": [_request_log_payload(item) for item in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/request-logs/{log_id}")
async def request_log_detail(
    log_id: UUID,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        item = await session.get(RequestLog, log_id)
    if item is None:
        raise HTTPException(status_code=404, detail="请求日志不存在")
    return _request_log_payload(item, detail=True)


@router.get("/users")
async def users(
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
    query: str = Query(default="", max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    filters = []
    if query.strip():
        filters.append(User.email.ilike(f"%{query.strip()}%"))
    async with runtime.sessions() as session:
        count_stmt = select(func.count(User.id))
        list_stmt = select(User).order_by(User.created_at.desc())
        if filters:
            count_stmt = count_stmt.where(*filters)
            list_stmt = list_stmt.where(*filters)
        total = await session.scalar(count_stmt) or 0
        rows = (
            await session.scalars(list_stmt.offset((page - 1) * page_size).limit(page_size))
        ).all()
    return {"items": [_user_payload(user) for user in rows], "total": total}


@router.patch("/users/{user_id}/status")
async def update_user_status(
    user_id: UUID,
    payload: UserStatusPayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        user = await session.get(User, user_id, with_for_update=True)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        user.is_active = payload.is_active
        user.updated_at = datetime.now(UTC)
    return _user_payload(user)


@router.post("/users/{user_id}/points")
async def adjust_user_points(
    user_id: UUID,
    payload: PointsPayload,
    request: Request,
    admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    if payload.delta == 0:
        raise HTTPException(status_code=422, detail="积分调整值不能为 0")
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        user = await session.get(User, user_id, with_for_update=True)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if user.points + payload.delta < 0:
            raise HTTPException(status_code=409, detail="扣减后积分不能小于 0")
        user.points += payload.delta
        session.add(
            PointLedger(
                user_id=user.id,
                delta=payload.delta,
                balance_after=user.points,
                reason="admin_adjustment",
                note=f"{payload.note}（操作人：{admin}）",
            )
        )
    return _user_payload(user)


@router.get("/packages")
async def admin_packages(
    request: Request, _admin: Annotated[str, Depends(current_admin)]
) -> list[dict[str, object]]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        rows = (
            await session.scalars(select(Package).order_by(Package.sort_order, Package.price_cents))
        ).all()
    return [_package_payload(item) for item in rows]


@router.post("/packages", status_code=status.HTTP_201_CREATED)
async def create_package(
    payload: PackageCreatePayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        item = Package(
            name=payload.name.strip(),
            price_cents=payload.price_yuan * 100,
            points=payload.points,
            is_active=payload.is_active,
            sort_order=payload.sort_order,
        )
        session.add(item)
        await session.flush()
    return _package_payload(item)


@router.patch("/packages/{package_id}")
async def update_package(
    package_id: UUID,
    payload: PackageUpdatePayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    values = payload.model_dump(exclude_unset=True)
    async with runtime.sessions() as session, session.begin():
        item = await session.get(Package, package_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="套餐不存在")
        if "name" in values:
            item.name = values["name"].strip()
        if "price_yuan" in values:
            item.price_cents = values["price_yuan"] * 100
        if "points" in values:
            item.points = values["points"]
        if "is_active" in values:
            item.is_active = values["is_active"]
        if "sort_order" in values:
            item.sort_order = values["sort_order"]
        item.updated_at = datetime.now(UTC)
    return _package_payload(item)


@router.get("/model-channels")
async def model_channels(
    request: Request, _admin: Annotated[str, Depends(current_admin)]
) -> list[dict[str, object]]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        rows = (
            await session.scalars(select(ModelChannel).order_by(ModelChannel.created_at.desc()))
        ).all()
    return [_channel_payload(item) for item in rows]


@router.post("/model-channels", status_code=status.HTTP_201_CREATED)
async def create_model_channel(
    payload: ChannelCreatePayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    settings = request.app.state.settings
    async with runtime.sessions() as session, session.begin():
        if payload.is_active:
            await session.execute(update(ModelChannel).values(is_active=False))
        item = ModelChannel(
            name=payload.name.strip(),
            base_url=payload.base_url,
            encrypted_api_key=encrypt_secret(payload.api_key, settings.secret_key),
            qps_limit=payload.qps_limit,
            is_active=payload.is_active,
        )
        session.add(item)
        await session.flush()
    return _channel_payload(item)


@router.patch("/model-channels/{channel_id}")
async def update_model_channel(
    channel_id: UUID,
    payload: ChannelUpdatePayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    settings = request.app.state.settings
    values = payload.model_dump(exclude_unset=True)
    async with runtime.sessions() as session, session.begin():
        item = await session.get(ModelChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="文本模型渠道不存在")
        if "name" in values:
            item.name = values["name"].strip()
        if "base_url" in values:
            item.base_url = values["base_url"]
        if "api_key" in values:
            item.encrypted_api_key = encrypt_secret(values["api_key"], settings.secret_key)
        if "qps_limit" in values:
            item.qps_limit = values["qps_limit"]
        item.updated_at = datetime.now(UTC)
    return _channel_payload(item)


@router.post("/model-channels/{channel_id}/activate")
async def activate_model_channel(
    channel_id: UUID,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        item = await session.get(ModelChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="文本模型渠道不存在")
        await session.execute(
            update(ModelChannel).where(ModelChannel.id != channel_id).values(is_active=False)
        )
        item.is_active = True
        item.updated_at = datetime.now(UTC)
    return _channel_payload(item)


@router.post("/model-channels/{channel_id}/disable")
async def disable_model_channel(
    channel_id: UUID,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        item = await session.get(ModelChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="文本模型渠道不存在")
        item.is_active = False
        item.updated_at = datetime.now(UTC)
    return _channel_payload(item)


@router.get("/video-channels")
async def video_channels(
    request: Request, _admin: Annotated[str, Depends(current_admin)]
) -> list[dict[str, object]]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        rows = (
            await session.scalars(select(VideoChannel).order_by(VideoChannel.created_at.desc()))
        ).all()
    return [_video_channel_payload(item) for item in rows]


@router.post("/video-channels", status_code=status.HTTP_201_CREATED)
async def create_video_channel(
    payload: VideoChannelCreatePayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    settings = request.app.state.settings
    async with runtime.sessions() as session, session.begin():
        if payload.is_active:
            await session.execute(update(VideoChannel).values(is_active=False))
        item = VideoChannel(
            name=payload.name.strip(),
            base_url=payload.base_url,
            model_name=payload.model_name.strip(),
            provider=payload.provider,
            encrypted_api_key=encrypt_secret(payload.api_key, settings.secret_key),
            qps_limit=payload.qps_limit,
            default_seconds=payload.default_seconds,
            default_size=payload.default_size,
            default_resolution=payload.default_resolution,
            is_active=payload.is_active,
        )
        session.add(item)
        await session.flush()
    return _video_channel_payload(item)


@router.patch("/video-channels/{channel_id}")
async def update_video_channel(
    channel_id: UUID,
    payload: VideoChannelUpdatePayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    settings = request.app.state.settings
    values = payload.model_dump(exclude_unset=True)
    async with runtime.sessions() as session, session.begin():
        item = await session.get(VideoChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="视频生成渠道不存在")
        if "name" in values:
            item.name = values["name"].strip()
        if "base_url" in values:
            item.base_url = values["base_url"]
        if "api_key" in values:
            item.encrypted_api_key = encrypt_secret(values["api_key"], settings.secret_key)
        if "model_name" in values:
            item.model_name = values["model_name"].strip()
        if "provider" in values:
            item.provider = values["provider"]
        if "qps_limit" in values:
            item.qps_limit = values["qps_limit"]
        if "default_seconds" in values:
            item.default_seconds = values["default_seconds"]
        if "default_size" in values:
            item.default_size = values["default_size"]
        if "default_resolution" in values:
            item.default_resolution = values["default_resolution"]
        item.updated_at = datetime.now(UTC)
    return _video_channel_payload(item)


@router.post("/video-channels/{channel_id}/activate")
async def activate_video_channel(
    channel_id: UUID,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        item = await session.get(VideoChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="视频生成渠道不存在")
        await session.execute(
            update(VideoChannel).where(VideoChannel.id != channel_id).values(is_active=False)
        )
        item.is_active = True
        item.updated_at = datetime.now(UTC)
    return _video_channel_payload(item)


@router.post("/video-channels/{channel_id}/disable")
async def disable_video_channel(
    channel_id: UUID,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        item = await session.get(VideoChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="视频生成渠道不存在")
        item.is_active = False
        item.updated_at = datetime.now(UTC)
    return _video_channel_payload(item)


@router.get("/music-channels")
async def music_channels(
    request: Request, _admin: Annotated[str, Depends(current_admin)]
) -> list[dict[str, object]]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        rows = (
            await session.scalars(select(MusicChannel).order_by(MusicChannel.created_at.desc()))
        ).all()
    return [_music_channel_payload(item) for item in rows]


@router.post("/music-channels", status_code=status.HTTP_201_CREATED)
async def create_music_channel(
    payload: MusicChannelCreatePayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    settings = request.app.state.settings
    async with runtime.sessions() as session, session.begin():
        if payload.is_active:
            await session.execute(update(MusicChannel).values(is_active=False))
        item = MusicChannel(
            name=payload.name.strip(),
            base_url=payload.base_url,
            model_name=payload.model_name.strip(),
            encrypted_api_key=encrypt_secret(payload.api_key, settings.secret_key),
            qps_limit=payload.qps_limit,
            default_format=payload.default_format,
            is_active=payload.is_active,
        )
        session.add(item)
        await session.flush()
    return _music_channel_payload(item)


@router.patch("/music-channels/{channel_id}")
async def update_music_channel(
    channel_id: UUID,
    payload: MusicChannelUpdatePayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    settings = request.app.state.settings
    values = payload.model_dump(exclude_unset=True)
    async with runtime.sessions() as session, session.begin():
        item = await session.get(MusicChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="音乐生成渠道不存在")
        if "name" in values:
            item.name = values["name"].strip()
        if "base_url" in values:
            item.base_url = values["base_url"]
        if "api_key" in values:
            item.encrypted_api_key = encrypt_secret(values["api_key"], settings.secret_key)
        if "model_name" in values:
            item.model_name = values["model_name"].strip()
        if "qps_limit" in values:
            item.qps_limit = values["qps_limit"]
        if "default_format" in values:
            item.default_format = values["default_format"]
        item.updated_at = datetime.now(UTC)
    return _music_channel_payload(item)


@router.post("/music-channels/{channel_id}/activate")
async def activate_music_channel(
    channel_id: UUID,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        item = await session.get(MusicChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="音乐生成渠道不存在")
        await session.execute(
            update(MusicChannel).where(MusicChannel.id != channel_id).values(is_active=False)
        )
        item.is_active = True
        item.updated_at = datetime.now(UTC)
    return _music_channel_payload(item)


@router.post("/music-channels/{channel_id}/disable")
async def disable_music_channel(
    channel_id: UUID,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        item = await session.get(MusicChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="音乐生成渠道不存在")
        item.is_active = False
        item.updated_at = datetime.now(UTC)
    return _music_channel_payload(item)


@router.get("/speech-channels")
async def speech_channels(
    request: Request, _admin: Annotated[str, Depends(current_admin)]
) -> list[dict[str, object]]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        rows = (
            await session.scalars(
                select(SpeechChannel).order_by(SpeechChannel.created_at.desc())
            )
        ).all()
    return [_speech_channel_payload(item) for item in rows]


@router.post("/speech-channels", status_code=status.HTTP_201_CREATED)
async def create_speech_channel(
    payload: SpeechChannelCreatePayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    settings = request.app.state.settings
    async with runtime.sessions() as session, session.begin():
        if payload.is_active:
            await session.execute(update(SpeechChannel).values(is_active=False))
        item = SpeechChannel(
            name=payload.name.strip(),
            base_url=payload.base_url,
            model_name=payload.model_name.strip(),
            default_voice_id=payload.default_voice_id.strip(),
            encrypted_api_key=encrypt_secret(payload.api_key, settings.secret_key),
            qps_limit=payload.qps_limit,
            default_format=payload.default_format,
            is_active=payload.is_active,
        )
        session.add(item)
        await session.flush()
    return _speech_channel_payload(item)


@router.patch("/speech-channels/{channel_id}")
async def update_speech_channel(
    channel_id: UUID,
    payload: SpeechChannelUpdatePayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    settings = request.app.state.settings
    values = payload.model_dump(exclude_unset=True)
    async with runtime.sessions() as session, session.begin():
        item = await session.get(SpeechChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="语音配音渠道不存在")
        if "name" in values:
            item.name = values["name"].strip()
        if "base_url" in values:
            item.base_url = values["base_url"]
        if "api_key" in values:
            item.encrypted_api_key = encrypt_secret(values["api_key"], settings.secret_key)
        if "model_name" in values:
            item.model_name = values["model_name"].strip()
        if "default_voice_id" in values:
            item.default_voice_id = values["default_voice_id"].strip()
        if "qps_limit" in values:
            item.qps_limit = values["qps_limit"]
        if "default_format" in values:
            item.default_format = values["default_format"]
        item.updated_at = datetime.now(UTC)
    return _speech_channel_payload(item)


@router.post("/speech-channels/{channel_id}/activate")
async def activate_speech_channel(
    channel_id: UUID,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        item = await session.get(SpeechChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="语音配音渠道不存在")
        await session.execute(
            update(SpeechChannel).where(SpeechChannel.id != channel_id).values(is_active=False)
        )
        item.is_active = True
        item.updated_at = datetime.now(UTC)
    return _speech_channel_payload(item)


@router.post("/speech-channels/{channel_id}/disable")
async def disable_speech_channel(
    channel_id: UUID,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session, session.begin():
        item = await session.get(SpeechChannel, channel_id, with_for_update=True)
        if item is None:
            raise HTTPException(status_code=404, detail="语音配音渠道不存在")
        item.is_active = False
        item.updated_at = datetime.now(UTC)
    return _speech_channel_payload(item)


@router.get("/email-channel")
async def email_channel(
    request: Request, _admin: Annotated[str, Depends(current_admin)]
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        item = await session.scalar(
            select(EmailChannel).order_by(EmailChannel.created_at.desc()).limit(1)
        )
    return _email_channel_payload(item)


@router.put("/email-channel")
async def upsert_email_channel(
    payload: EmailChannelPayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, object]:
    runtime: RuntimeDependencies = request.app.state.runtime
    settings = request.app.state.settings
    async with runtime.sessions() as session, session.begin():
        item = await session.scalar(
            select(EmailChannel)
            .order_by(EmailChannel.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if item is None:
            if not payload.auth_code:
                raise HTTPException(status_code=422, detail="首次配置必须填写 SMTP 授权码")
            item = EmailChannel(
                name=payload.name.strip(),
                smtp_host=payload.smtp_host,
                smtp_port=payload.smtp_port,
                smtp_username=payload.smtp_username,
                encrypted_auth_code=encrypt_secret(payload.auth_code, settings.secret_key),
                from_name=payload.from_name.strip(),
                use_ssl=payload.use_ssl,
                is_active=payload.is_active,
            )
            session.add(item)
            await session.flush()
        else:
            item.name = payload.name.strip()
            item.smtp_host = payload.smtp_host
            item.smtp_port = payload.smtp_port
            item.smtp_username = payload.smtp_username
            item.from_name = payload.from_name.strip()
            item.use_ssl = payload.use_ssl
            item.is_active = payload.is_active
            if payload.auth_code:
                item.encrypted_auth_code = encrypt_secret(payload.auth_code, settings.secret_key)
            item.updated_at = datetime.now(UTC)
    return _email_channel_payload(item)


@router.post("/email-channel/test")
async def test_email_channel(
    payload: EmailTestPayload,
    request: Request,
    _admin: Annotated[str, Depends(current_admin)],
) -> dict[str, str]:
    runtime: RuntimeDependencies = request.app.state.runtime
    async with runtime.sessions() as session:
        item = await session.scalar(
            select(EmailChannel).order_by(EmailChannel.created_at.desc()).limit(1)
        )
    if item is None:
        raise HTTPException(status_code=404, detail="请先保存邮件渠道")
    try:
        connection = _smtp_connection(item, request.app.state.settings.secret_key)
        await send_test_email(connection, payload.recipient, request.app.state.settings.app_name)
    except (EmailDeliveryError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="测试邮件发送失败，请检查 SMTP 配置") from exc
    return {"message": "测试邮件已发送"}
