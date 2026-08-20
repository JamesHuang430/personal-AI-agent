from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

from openai import AsyncOpenAI
from sqlalchemy import select

from assistant_app.core.config import Settings
from assistant_app.core.encryption import decrypt_secret
from assistant_app.db.models import VideoChannel, VideoJob
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.generated_files import GENERATED_ROOT

VIDEO_SECONDS = {"4", "8", "12"}
VIDEO_SIZES = {"720x1280", "1280x720", "1024x1792", "1792x1024"}


class VideoChannelUnavailableError(RuntimeError):
    pass


class VideoRateLimitError(RuntimeError):
    pass


async def _enforce_video_qps(runtime: RuntimeDependencies, channel: VideoChannel) -> None:
    key = f"video:qps:{channel.id}:{int(time.time())}"
    async with runtime.redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 2)
        count, _ = await pipe.execute()
    if count > channel.qps_limit:
        raise VideoRateLimitError("当前视频生成请求较多，请稍后重试")


async def create_video_job(
    runtime: RuntimeDependencies,
    user_id: UUID,
    prompt: str,
    seconds: str | None = None,
    size: str | None = None,
) -> VideoJob:
    async with runtime.sessions() as session:
        channel = await session.scalar(select(VideoChannel).where(VideoChannel.is_active.is_(True)))
    if channel is None:
        raise VideoChannelUnavailableError("运营后台尚未启用视频生成渠道")

    selected_seconds = seconds if seconds in VIDEO_SECONDS else channel.default_seconds
    selected_size = size if size in VIDEO_SIZES else channel.default_size
    job = VideoJob(
        id=uuid4(),
        user_id=user_id,
        channel_id=channel.id,
        prompt=prompt[:8000],
        status="queued",
        seconds=selected_seconds,
        size=selected_size,
    )
    async with runtime.sessions() as session, session.begin():
        session.add(job)
    return job


async def run_video_job(runtime: RuntimeDependencies, settings: Settings, job_id: UUID) -> None:
    async with runtime.sessions() as session:
        job = await session.get(VideoJob, job_id)
        if job is None:
            return
        channel = await session.get(VideoChannel, job.channel_id)
    if channel is None:
        await _fail_job(runtime, job_id, "视频渠道已不存在")
        return

    try:
        await _enforce_video_qps(runtime, channel)
        await _update_job(runtime, job_id, status="processing")
        api_key = decrypt_secret(channel.encrypted_api_key, settings.secret_key)
        async with AsyncOpenAI(
            api_key=api_key,
            base_url=channel.base_url,
            timeout=900,
        ) as client:
            provider_job = await client.videos.create(
                prompt=job.prompt,
                model=channel.model_name,
                seconds=job.seconds,
                size=job.size,
            )
            await _update_job(runtime, job_id, provider_job_id=provider_job.id)
            completed = await client.videos.poll(provider_job.id, poll_interval_ms=5000)
            if getattr(completed, "status", None) not in {"completed", "succeeded"}:
                detail = getattr(completed, "error", None)
                raise RuntimeError(f"视频渠道任务失败：{detail or completed.status}")
            response = await client.videos.download_content(provider_job.id)
            content = await response.aread()

        await asyncio.to_thread(GENERATED_ROOT.mkdir, parents=True, exist_ok=True)
        storage_path = GENERATED_ROOT / f"{job_id}.mp4"
        await asyncio.to_thread(storage_path.write_bytes, content)
        await _update_job(
            runtime,
            job_id,
            status="completed",
            storage_path=str(storage_path),
            error_message=None,
        )
    except Exception as exc:  # background work must persist a safe failure state
        await _fail_job(runtime, job_id, f"视频生成失败（{type(exc).__name__}）")


async def _update_job(runtime: RuntimeDependencies, job_id: UUID, **values: object) -> None:
    async with runtime.sessions() as session, session.begin():
        job = await session.get(VideoJob, job_id, with_for_update=True)
        if job is None:
            return
        for name, value in values.items():
            setattr(job, name, value)
        job.updated_at = datetime.now(UTC)


async def _fail_job(runtime: RuntimeDependencies, job_id: UUID, message: str) -> None:
    await _update_job(runtime, job_id, status="failed", error_message=message)


def video_job_payload(job: VideoJob) -> dict[str, object]:
    return {
        "id": str(job.id),
        "prompt": job.prompt,
        "status": job.status,
        "seconds": job.seconds,
        "size": job.size,
        "error_message": job.error_message if job.status == "failed" else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "download_url": (
            f"/api/v1/videos/{job.id}/download" if job.status == "completed" else None
        ),
    }
