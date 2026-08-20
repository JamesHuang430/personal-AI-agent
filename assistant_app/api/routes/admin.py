from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update

from assistant_app.api.dependencies import ADMIN_SESSION_COOKIE, current_admin
from assistant_app.core.encryption import encrypt_secret
from assistant_app.core.security import new_session_token, session_digest
from assistant_app.core.time import utc_day_bounds
from assistant_app.db.models import ModelChannel, Package, PointLedger, User, VideoChannel
from assistant_app.db.runtime import RuntimeDependencies

router = APIRouter()


class AdminLoginPayload(BaseModel):
    username: str
    password: str


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
    model_name: str = Field(default="gpt-4o-mini", min_length=1, max_length=200)
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
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
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
    qps_limit: int = Field(default=1, ge=1, le=1000)
    default_seconds: Literal["4", "8", "12"] = "4"
    default_size: Literal["720x1280", "1280x720", "1024x1792", "1792x1024"] = "1280x720"
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
    qps_limit: int | None = Field(default=None, ge=1, le=1000)
    default_seconds: Literal["4", "8", "12"] | None = None
    default_size: Literal["720x1280", "1280x720", "1024x1792", "1792x1024"] | None = None

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str | None) -> str | None:
        return _validate_base_url(value) if value is not None else None


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
        "model_name": item.model_name,
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
        "qps_limit": item.qps_limit,
        "default_seconds": item.default_seconds,
        "default_size": item.default_size,
        "is_active": item.is_active,
        "api_key_configured": bool(item.encrypted_api_key),
        "updated_at": item.updated_at.isoformat(),
    }


@router.post("/auth/login")
async def admin_login(
    payload: AdminLoginPayload, request: Request, response: Response
) -> dict[str, str]:
    settings = request.app.state.settings
    valid = hmac.compare_digest(payload.username, settings.admin_username) and hmac.compare_digest(
        payload.password, settings.admin_password
    )
    if not valid:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token, digest = new_session_token()
    runtime: RuntimeDependencies = request.app.state.runtime
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
            model_name=payload.model_name.strip(),
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
            raise HTTPException(status_code=404, detail="大模型渠道不存在")
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
            raise HTTPException(status_code=404, detail="大模型渠道不存在")
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
            raise HTTPException(status_code=404, detail="大模型渠道不存在")
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
            encrypted_api_key=encrypt_secret(payload.api_key, settings.secret_key),
            qps_limit=payload.qps_limit,
            default_seconds=payload.default_seconds,
            default_size=payload.default_size,
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
        if "qps_limit" in values:
            item.qps_limit = values["qps_limit"]
        if "default_seconds" in values:
            item.default_seconds = values["default_seconds"]
        if "default_size" in values:
            item.default_size = values["default_size"]
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
