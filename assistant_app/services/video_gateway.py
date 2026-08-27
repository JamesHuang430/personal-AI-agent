from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from openai import AsyncOpenAI
from sqlalchemy import select

from assistant_app.core.config import Settings
from assistant_app.core.encryption import decrypt_secret
from assistant_app.db.models import VideoChannel, VideoJob
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.generated_files import GENERATED_ROOT

VIDEO_SECONDS = {"4", "8", "12"}
VIDEO_SIZES = {"720x1280", "1280x720", "1024x1792", "1792x1024"}
VIDEO_RESOLUTIONS = {"768P", "2K"}


class VideoChannelUnavailableError(RuntimeError):
    pass


class VideoRateLimitError(RuntimeError):
    pass


class VideoProviderError(RuntimeError):
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
    resolution: str | None = None,
) -> VideoJob:
    async with runtime.sessions() as session:
        channel = await session.scalar(select(VideoChannel).where(VideoChannel.is_active.is_(True)))
    if channel is None:
        raise VideoChannelUnavailableError("运营后台尚未启用视频生成渠道")

    selected_seconds = seconds if seconds in VIDEO_SECONDS else channel.default_seconds
    selected_size = size if size in VIDEO_SIZES else channel.default_size
    selected_resolution = (
        resolution if resolution in VIDEO_RESOLUTIONS else channel.default_resolution
    )
    job = VideoJob(
        id=uuid4(),
        user_id=user_id,
        channel_id=channel.id,
        prompt=prompt[:8000],
        status="queued",
        seconds=selected_seconds,
        size=selected_size,
        resolution=selected_resolution,
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
        if channel.provider == "minimax":
            content = await _run_minimax_video(channel, job, api_key, runtime, job_id)
        else:
            content = await _run_openai_video(channel, job, api_key, runtime, job_id)

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
        detail = str(exc)[:420] if isinstance(exc, VideoProviderError) else type(exc).__name__
        await _fail_job(runtime, job_id, f"视频生成失败（{detail}）")


async def _run_openai_video(
    channel: VideoChannel,
    job: VideoJob,
    api_key: str,
    runtime: RuntimeDependencies,
    job_id: UUID,
) -> bytes:
    async with AsyncOpenAI(api_key=api_key, base_url=channel.base_url, timeout=900) as client:
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
            raise VideoProviderError(f"渠道任务失败：{detail or completed.status}")
        response = await client.videos.download_content(provider_job.id)
        return await response.aread()


def _minimax_ratio(size: str) -> str:
    width, height = (int(value) for value in size.split("x", 1))
    return "9:16" if height > width else "16:9"


def _minimax_video_urls(base_url: str, task_id: str | None = None) -> tuple[str, str]:
    """Return create/query URLs for official MiniMax or AI Ping's H3 gateway."""
    normalized = base_url.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.hostname in {"aiping.cn", "www.aiping.cn"}:
        origin = f"{parsed.scheme}://{parsed.netloc}"
        prefix = f"{origin}/api/v1/multimodal/minimax/videos"
        return (
            f"{prefix}/video_generation",
            f"{prefix}/query/video_generation/{task_id or '{task_id}'}",
        )
    return (
        f"{normalized}/v2/video_generation",
        f"{normalized}/v2/query/video_generation/{task_id or '{task_id}'}",
    )


async def _run_minimax_video(
    channel: VideoChannel,
    job: VideoJob,
    api_key: str,
    runtime: RuntimeDependencies,
    job_id: UUID,
) -> bytes:
    create_url, _ = _minimax_video_urls(channel.base_url)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": channel.model_name,
        "content": [{"type": "text", "text": job.prompt}],
        "resolution": job.resolution,
        "duration": int(job.seconds),
        "ratio": _minimax_ratio(job.size),
    }
    timeout = httpx.Timeout(900.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.post(
            create_url,
            headers=headers,
            json=payload,
        )
        if response.status_code != 200:
            try:
                provider_message = str(response.json().get("base_resp", {}).get("status_msg", ""))
            except (ValueError, AttributeError):
                provider_message = ""
            suffix = f"（{provider_message[:180]}）" if provider_message else ""
            raise VideoProviderError(
                f"MiniMax 创建任务返回 HTTP {response.status_code}{suffix}"
            )
        task_id = str(response.json().get("task_id", "")).strip()
        if not task_id:
            raise VideoProviderError("MiniMax 未返回 task_id")
        await _update_job(runtime, job_id, provider_job_id=task_id)
        _, query_url = _minimax_video_urls(channel.base_url, task_id)

        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            result = await client.get(
                query_url,
                headers=headers,
            )
            if result.status_code != 200:
                raise VideoProviderError(f"MiniMax 查询任务返回 HTTP {result.status_code}")
            task = result.json().get("task", {})
            task_status = str(task.get("status", "")).lower()
            if task_status == "succeeded":
                video_url = str(task.get("content", {}).get("url", "")).strip()
                if not video_url:
                    raise VideoProviderError("MiniMax 任务完成但未返回视频地址")
                download = await client.get(video_url)
                if download.status_code != 200:
                    raise VideoProviderError(
                        f"MiniMax 视频下载返回 HTTP {download.status_code}"
                    )
                return download.content
            if task_status in {"failed", "cancelled"}:
                raise VideoProviderError(f"MiniMax 任务状态为 {task_status}")
        raise VideoProviderError("MiniMax 视频任务等待超时")


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
        "resolution": job.resolution or "768P",
        "error_message": job.error_message if job.status == "failed" else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "download_url": (
            f"/api/v1/videos/{job.id}/download" if job.status == "completed" else None
        ),
        "preview_url": (
            f"/api/v1/videos/{job.id}/preview" if job.status == "completed" else None
        ),
    }
