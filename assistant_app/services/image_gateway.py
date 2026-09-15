"""Synchronous MiniMax images with a durable, no-automatic-resubmission checkpoint."""

import asyncio
import base64
import io
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from PIL import Image
from sqlalchemy import select

from assistant_app.core.encryption import decrypt_secret
from assistant_app.db.models import DirectorShot, ImageChannel
from assistant_app.services.activity import emit_activity
from assistant_app.services.generated_files import GENERATED_ROOT

MAX_IMAGE_BYTES = 12 * 1024 * 1024


def save_image(data: bytes, destination: Path) -> None:
    """Decode/re-encode bounded raster input; never trust a filename or remote URL."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过 12 MB")
    with Image.open(io.BytesIO(data)) as source:
        width, height = source.size
        if width < 64 or height < 64 or width * height > 16_777_216:
            raise ValueError("图片尺寸须至少 64×64，且不超过 1600 万像素")
        if source.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("只支持 PNG、JPEG 或 WebP 图片")
        source.load()
        image = source.convert("RGBA")
        canvas = Image.new("RGB", image.size, "white")
        canvas.paste(image, mask=image.getchannel("A"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp.png")
        canvas.save(temporary, "PNG")
        temporary.replace(destination)


def image_request(channel, prompt, aspect_ratio):
    return {
        "model": channel.model_name,
        "prompt": prompt[:1500],
        "n": 1,
        "aspect_ratio": aspect_ratio,
        "response_format": "base64",
        "prompt_optimizer": False,
    }


def decode_response(payload):
    if not isinstance(payload, dict) or payload.get("base_resp", {}).get("status_code") != 0:
        raise ValueError("MiniMax 生图失败，请检查图片渠道权限、余额和服务状态")
    images = (payload.get("data") or {}).get("image_base64")
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], str):
        raise ValueError("MiniMax 未返回单张 base64 图片；不会自动下载不可信 URL")
    if len(images[0]) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise ValueError("图片响应超过大小限制")
    return base64.b64decode(images[0], validate=True)


async def generate_shot_image(runtime, settings, shot_id, aspect_ratio):
    async with runtime.sessions() as session:
        channel = await session.scalar(select(ImageChannel).where(ImageChannel.is_active.is_(True)))
    # Uploaded/completed images need neither a channel nor an additional billable request.
    async with runtime.sessions() as session:
        existing = await session.get(DirectorShot, shot_id)
        if existing.image_path and await asyncio.to_thread(Path(existing.image_path).is_file):
            return existing.image_path
    if channel is None:
        raise ValueError("尚未配置图片渠道：请管理员启用 MiniMax 生图，或在分镜确认前上传图片")
    key = f"image:qps:{channel.id}:{int(time.time())}"
    count = await runtime.redis.incr(key)
    if count == 1:
        await runtime.redis.expire(key, 2)
    if count > channel.qps_limit:
        raise ValueError("图片渠道暂时达到 QPS 上限，请稍后恢复任务")
    async with runtime.sessions() as session, session.begin():
        shot = await session.get(DirectorShot, shot_id, with_for_update=True)
        if shot.image_submission_started_at:
            raise ValueError(
                "上次生图已提交但结果未落盘；为避免重复扣费，不自动重发。"
                "请核对渠道账单后新建一版，或上传图片替换"
            )
        shot.image_submission_started_at = datetime.now(UTC)
        shot.image_channel_id = channel.id
        shot.image_source = "minimax"
        prompt = shot.prompt
    await emit_activity(runtime, f"第 {shot.sequence} 镜 · MiniMax 生图", "processing", kind="tool")
    try:
        async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
            async with client.stream(
                "POST",
                f"{channel.base_url.rstrip('/')}/v1/image_generation",
                headers={
                    "Authorization": "Bearer "
                    + decrypt_secret(channel.encrypted_api_key, settings.secret_key)
                },
                json=image_request(channel, prompt, aspect_ratio),
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_IMAGE_BYTES * 2:
                        raise ValueError("图片响应超过大小限制")
        import json

        data = decode_response(json.loads(body))
        path = GENERATED_ROOT / f"director-image-{shot_id}.png"
        await asyncio.to_thread(save_image, data, path)
        async with runtime.sessions() as session, session.begin():
            current = await session.get(DirectorShot, shot_id, with_for_update=True)
            current.image_path = str(path)
        await emit_activity(runtime, f"第 {shot.sequence} 镜 · 生图完成", "completed", kind="tool")
        return str(path)
    except Exception as exc:
        await emit_activity(runtime, f"第 {shot.sequence} 镜 · 生图失败", "failed", kind="tool")
        # Do not expose provider bodies, signed URLs or Authorization data in persisted errors.
        raise RuntimeError(
            "图片生成未完成；不会自动重复提交或切换视频模型。请检查渠道余额、权限或上传替代图片"
        ) from exc
