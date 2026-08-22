from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from assistant_app.core.config import Settings
from assistant_app.core.encryption import decrypt_secret
from assistant_app.db.models import SpeechChannel, SpeechJob
from assistant_app.db.runtime import RuntimeDependencies
from assistant_app.services.generated_files import GENERATED_ROOT


class SpeechChannelUnavailableError(RuntimeError):
    pass


class SpeechRateLimitError(RuntimeError):
    pass


class SpeechProviderError(RuntimeError):
    pass


async def _enforce_speech_qps(runtime: RuntimeDependencies, channel: SpeechChannel) -> None:
    key = f"speech:qps:{channel.id}:{int(time.time())}"
    async with runtime.redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 2)
        count, _ = await pipe.execute()
    if count > channel.qps_limit:
        raise SpeechRateLimitError("当前语音生成请求较多，请稍后重试")


async def create_speech_job(
    runtime: RuntimeDependencies,
    user_id: UUID,
    speech_text: str,
    voice_id: str | None = None,
    speed: float = 1.0,
) -> SpeechJob:
    async with runtime.sessions() as session:
        channel = await session.scalar(
            select(SpeechChannel).where(SpeechChannel.is_active.is_(True))
        )
    if channel is None:
        raise SpeechChannelUnavailableError("运营后台尚未启用语音配音渠道")

    job = SpeechJob(
        id=uuid4(),
        user_id=user_id,
        channel_id=channel.id,
        speech_text=speech_text[:10_000],
        voice_id=(voice_id or channel.default_voice_id)[:200],
        speed=max(0.5, min(float(speed), 2.0)),
        status="queued",
        audio_format=channel.default_format,
    )
    async with runtime.sessions() as session, session.begin():
        session.add(job)
    return job


async def run_speech_job(runtime: RuntimeDependencies, settings: Settings, job_id: UUID) -> None:
    async with runtime.sessions() as session:
        job = await session.get(SpeechJob, job_id)
        if job is None:
            return
        channel = await session.get(SpeechChannel, job.channel_id)
    if channel is None:
        await _fail_job(runtime, job_id, "语音渠道已不存在")
        return

    try:
        await _enforce_speech_qps(runtime, channel)
        await _update_job(runtime, job_id, status="processing")
        api_key = decrypt_secret(channel.encrypted_api_key, settings.secret_key)
        payload = {
            "model": channel.model_name,
            "text": job.speech_text,
            "stream": False,
            "voice_setting": {
                "voice_id": job.voice_id,
                "speed": job.speed,
                "vol": 1.0,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": channel.default_format,
                "channel": 1,
            },
            "output_format": "hex",
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=30.0), follow_redirects=True
        ) as client:
            response = await client.post(
                f"{channel.base_url.rstrip('/')}/v1/t2a_v2",
                headers=headers,
                json=payload,
            )
        result = response.json() if response.content else {}
        base_resp = result.get("base_resp", {})
        if response.status_code != 200 or base_resp.get("status_code") not in {None, 0}:
            message = str(base_resp.get("status_msg", "unknown"))[:240]
            raise SpeechProviderError(
                f"MiniMax 语音接口返回 HTTP {response.status_code}：{message}"
            )
        audio_value = str(result.get("data", {}).get("audio", "")).strip()
        if not audio_value:
            raise SpeechProviderError("MiniMax 语音接口未返回音频")
        try:
            content = bytes.fromhex(audio_value)
        except ValueError as exc:
            raise SpeechProviderError("MiniMax 返回的语音编码无效") from exc

        await asyncio.to_thread(GENERATED_ROOT.mkdir, parents=True, exist_ok=True)
        storage_path = GENERATED_ROOT / f"speech-{job_id}.{channel.default_format}"
        await asyncio.to_thread(storage_path.write_bytes, content)
        duration_ms = result.get("extra_info", {}).get("audio_length")
        await _update_job(
            runtime,
            job_id,
            status="completed",
            storage_path=str(storage_path),
            duration_ms=int(duration_ms) if duration_ms is not None else None,
            error_message=None,
        )
    except Exception as exc:  # background work must persist a safe failure state
        detail = str(exc)[:420] if isinstance(exc, SpeechProviderError) else type(exc).__name__
        await _fail_job(runtime, job_id, f"语音生成失败（{detail}）")


async def _update_job(runtime: RuntimeDependencies, job_id: UUID, **values: object) -> None:
    async with runtime.sessions() as session, session.begin():
        job = await session.get(SpeechJob, job_id, with_for_update=True)
        if job is None:
            return
        for name, value in values.items():
            setattr(job, name, value)
        job.updated_at = datetime.now(UTC)


async def _fail_job(runtime: RuntimeDependencies, job_id: UUID, message: str) -> None:
    await _update_job(runtime, job_id, status="failed", error_message=message)


def speech_job_payload(job: SpeechJob) -> dict[str, object]:
    return {
        "id": str(job.id),
        "text": job.speech_text,
        "voice_id": job.voice_id,
        "speed": job.speed,
        "status": job.status,
        "audio_format": job.audio_format,
        "duration_ms": job.duration_ms,
        "error_message": job.error_message if job.status == "failed" else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "download_url": (
            f"/api/v1/speech/{job.id}/download" if job.status == "completed" else None
        ),
    }
