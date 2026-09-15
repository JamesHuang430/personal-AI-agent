"""Image configuration is deliberately independent of video and speech credentials."""

from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update

from assistant_app.api.dependencies import current_admin
from assistant_app.core.encryption import encrypt_secret
from assistant_app.db.models import ImageChannel

router = APIRouter(dependencies=[Depends(current_admin)])


class ImageChannelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    base_url: str = "https://api.minimaxi.com"
    model_name: Literal["image-01", "image-01-live"] = "image-01"
    api_key: str | None = Field(default=None, min_length=1, max_length=4000)
    qps_limit: int = Field(default=1, ge=1, le=20)
    is_active: bool = False

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("图片渠道必须使用不含用户名、查询参数的 HTTPS 地址")
        return value.rstrip("/")


def channel_payload(row):
    return {
        key: getattr(row, key)
        for key in ("name", "base_url", "model_name", "qps_limit", "is_active")
    } | {"id": str(row.id), "key_configured": bool(row.encrypted_api_key)}


@router.get("/image-channels")
async def list_channels(request: Request):
    async with request.app.state.runtime.sessions() as session:
        rows = (
            await session.scalars(select(ImageChannel).order_by(ImageChannel.created_at.desc()))
        ).all()
    return [channel_payload(row) for row in rows]


async def save_channel(request, payload, channel_id=None):
    async with request.app.state.runtime.sessions() as session, session.begin():
        # Serialize activation even when creating the very first channel.
        from sqlalchemy import text

        await session.execute(text("SELECT pg_advisory_xact_lock(901920)"))
        row = await session.get(ImageChannel, channel_id) if channel_id else ImageChannel()
        if row is None:
            raise HTTPException(404, "图片渠道不存在")
        if not channel_id and not payload.api_key:
            raise HTTPException(422, "新增图片渠道需要 API Key")
        if payload.is_active:
            await session.execute(update(ImageChannel).values(is_active=False))
        for key, value in payload.model_dump(exclude={"api_key"}).items():
            setattr(row, key, value)
        if payload.api_key:
            row.encrypted_api_key = encrypt_secret(
                payload.api_key, request.app.state.settings.secret_key
            )
        session.add(row)
        await session.flush()
    return channel_payload(row)


@router.post("/image-channels", status_code=201)
async def create_channel(payload: ImageChannelPayload, request: Request):
    return await save_channel(request, payload)


@router.put("/image-channels/{channel_id}")
async def edit_channel(channel_id: UUID, payload: ImageChannelPayload, request: Request):
    return await save_channel(request, payload, channel_id)
