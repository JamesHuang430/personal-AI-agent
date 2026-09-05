from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from assistant_app.core.config import Settings
from assistant_app.core.encryption import decrypt_secret
from assistant_app.db.models import MusicChannel, MusicJob
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.generated_files import GENERATED_ROOT
from assistant_app.services.work_queue import enqueue


class MusicChannelUnavailableError(RuntimeError):
    pass


class MusicRateLimitError(RuntimeError):
    pass


class MusicProviderError(RuntimeError):
    pass


async def _enforce_music_qps(runtime: RuntimeDependencies, channel: MusicChannel) -> None:
    key = f"music:qps:{channel.id}:{int(time.time())}"
    async with runtime.redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 2)
        count, _ = await pipe.execute()
    if count > channel.qps_limit:
        raise MusicRateLimitError("当前音乐生成请求较多，请稍后重试")


async def create_music_job(
    runtime: RuntimeDependencies,
    user_id: UUID,
    prompt: str,
    lyrics: str | None = None,
    is_instrumental: bool = True,
    *,
    schedule: bool = True,
) -> MusicJob:
    async with runtime.sessions() as session:
        channel = await session.scalar(select(MusicChannel).where(MusicChannel.is_active.is_(True)))
    if channel is None:
        raise MusicChannelUnavailableError("运营后台尚未启用音乐生成渠道")

    job = MusicJob(
        id=uuid4(),
        user_id=user_id,
        channel_id=channel.id,
        prompt=prompt[:2000],
        lyrics=lyrics[:6000] if lyrics else None,
        is_instrumental=is_instrumental,
        status="queued",
        audio_format=channel.default_format,
    )
    async with runtime.sessions() as session, session.begin():
        session.add(job)
        if schedule:
            await session.flush()
            await enqueue(session, "music", job.id)
    return job


async def run_music_job(runtime: RuntimeDependencies, settings: Settings, job_id: UUID) -> None:
    async with runtime.sessions() as session:
        job = await session.get(MusicJob, job_id)
        if job is None or job.status in {"completed", "awaiting_confirmation"}:
            return
        channel = await session.get(MusicChannel, job.channel_id)
    if channel is None:
        await _fail_job(runtime, job_id, "音乐渠道已不存在")
        return

    if job.submission_started_at:
        await _fail_job(
            runtime, job_id, "上次提交结果不确定，为避免重复计费已停止自动重试，请核对供应商记录"
        )
        return

    try:
        await _enforce_music_qps(runtime, channel)
        await _update_job(
            runtime, job_id, status="processing", submission_started_at=datetime.now(UTC)
        )
        api_key = decrypt_secret(channel.encrypted_api_key, settings.secret_key)
        payload = {
            "model": channel.model_name,
            "prompt": job.prompt,
            "lyrics": job.lyrics or "",
            "lyrics_optimizer": not job.is_instrumental and not job.lyrics,
            "is_instrumental": job.is_instrumental,
            "audio_setting": {
                "sample_rate": 44100,
                "bitrate": 256000,
                "format": channel.default_format,
            },
            "output_format": "url",
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(900.0, connect=30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.post(
                f"{channel.base_url.rstrip('/')}/v1/music_generation",
                headers=headers,
                json=payload,
            )
            if response.status_code != 200:
                raise MusicProviderError(f"MiniMax 音乐接口返回 HTTP {response.status_code}")
            result = response.json()
            base_resp = result.get("base_resp", {})
            if base_resp.get("status_code") not in {None, 0}:
                raise MusicProviderError(
                    f"MiniMax 音乐任务失败：{base_resp.get('status_msg', 'unknown')}"
                )
            audio_value = str(result.get("data", {}).get("audio", "")).strip()
            if not audio_value:
                raise MusicProviderError("MiniMax 音乐接口未返回音频")
            if audio_value.startswith(("http://", "https://")):
                audio_response = await client.get(audio_value)
                if audio_response.status_code != 200:
                    raise MusicProviderError(
                        f"MiniMax 音频下载返回 HTTP {audio_response.status_code}"
                    )
                content = audio_response.content
            else:
                try:
                    content = bytes.fromhex(audio_value)
                except ValueError as exc:
                    raise MusicProviderError("MiniMax 返回的音频编码无效") from exc

        await asyncio.to_thread(GENERATED_ROOT.mkdir, parents=True, exist_ok=True)
        storage_path = GENERATED_ROOT / f"music-{job_id}.{channel.default_format}"
        await asyncio.to_thread(storage_path.write_bytes, content)
        duration_ms = result.get("extra_info", {}).get("music_duration")
        await _update_job(
            runtime,
            job_id,
            status="completed",
            storage_path=str(storage_path),
            duration_ms=int(duration_ms) if duration_ms is not None else None,
            error_message=None,
        )
    except Exception as exc:  # background work must persist a safe failure state
        detail = str(exc)[:420] if isinstance(exc, MusicProviderError) else type(exc).__name__
        await _fail_job(runtime, job_id, f"音乐生成失败（{detail}）")


async def _update_job(runtime: RuntimeDependencies, job_id: UUID, **values: object) -> None:
    async with runtime.sessions() as session, session.begin():
        job = await session.get(MusicJob, job_id, with_for_update=True)
        if job is None:
            return
        for name, value in values.items():
            setattr(job, name, value)
        job.updated_at = datetime.now(UTC)


async def _fail_job(runtime: RuntimeDependencies, job_id: UUID, message: str) -> None:
    await _update_job(runtime, job_id, status="failed", error_message=message)


def music_job_payload(job: MusicJob) -> dict[str, object]:
    return {
        "id": str(job.id),
        "prompt": job.prompt,
        "status": job.status,
        "is_instrumental": job.is_instrumental,
        "audio_format": job.audio_format,
        "duration_ms": job.duration_ms,
        "error_message": job.error_message if job.status == "failed" else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "download_url": (f"/api/v1/music/{job.id}/download" if job.status == "completed" else None),
    }
