from __future__ import annotations

import asyncio
import hashlib
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


SPEECH_VOICE_ROLES = {
    "narrator",
    "adult_male",
    "adult_female",
    "elder_male",
    "elder_female",
    "boy",
    "girl",
}
SPEECH_EMOTIONS = {
    "calm",
    "happy",
    "surprised",
    "disappointed",
    "sad",
    "devastated",
    "angry",
    "fearful",
}

# These IDs come from MiniMax's public system voice list. The account-level
# /v1/get_voice response is still the authority: unavailable entries are ignored.
ROLE_VOICE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "narrator": (
        "Chinese (Mandarin)_Radio_Host",
        "Chinese (Mandarin)_Male_Announcer",
        "Chinese (Mandarin)_News_Anchor",
    ),
    "adult_male": (
        "Chinese (Mandarin)_Reliable_Executive",
        "Chinese (Mandarin)_Gentleman",
        "Chinese (Mandarin)_Sincere_Adult",
        "Chinese (Mandarin)_Southern_Young_Man",
    ),
    "adult_female": (
        "Chinese (Mandarin)_Mature_Woman",
        "Chinese (Mandarin)_Sweet_Lady",
        "Chinese (Mandarin)_IntellectualGirl",
        "Chinese (Mandarin)_Warm_Bestie",
    ),
    "elder_male": (
        "Chinese (Mandarin)_Humorous_Elder",
        "Chinese (Mandarin)_Kind-hearted_Elder",
        "Chinese (Mandarin)_Gentle_Senior",
    ),
    "elder_female": (
        "Chinese (Mandarin)_Wise_Women",
        "Chinese (Mandarin)_Kind-hearted_Antie",
        "Chinese (Mandarin)_Warm-HeartedAunt",
    ),
    "boy": (
        "Chinese (Mandarin)_Pure-hearted_Boy",
        "Chinese (Mandarin)_Straightforward_Boy",
        "Chinese (Mandarin)_Gentle_Youth",
    ),
    "girl": (
        "Chinese (Mandarin)_Crisp_Girl",
        "Chinese (Mandarin)_Warm_Girl",
        "Chinese (Mandarin)_Soft_Girl",
    ),
}

_VOICE_CACHE_SECONDS = 600.0
_voice_cache: dict[UUID, tuple[float, set[str]]] = {}


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
    *,
    speaker: str | None = None,
    voice_role: str | None = None,
    emotion: str = "calm",
) -> SpeechJob:
    async with runtime.sessions() as session:
        channel = await session.scalar(
            select(SpeechChannel).where(SpeechChannel.is_active.is_(True))
        )
    if channel is None:
        raise SpeechChannelUnavailableError("运营后台尚未启用语音配音渠道")

    normalized_role = voice_role if voice_role in SPEECH_VOICE_ROLES else None
    normalized_emotion = emotion if emotion in SPEECH_EMOTIONS else "calm"
    job = SpeechJob(
        id=uuid4(),
        user_id=user_id,
        channel_id=channel.id,
        speech_text=speech_text[:10_000],
        voice_id=(voice_id or channel.default_voice_id)[:200],
        speaker=(speaker or "")[:100] or None,
        voice_role=normalized_role,
        emotion=normalized_emotion,
        speed=max(0.5, min(float(speed), 2.0)),
        status="queued",
        audio_format=channel.default_format,
    )
    async with runtime.sessions() as session, session.begin():
        session.add(job)
    return job


def _select_role_voice(
    voice_role: str | None,
    speaker: str | None,
    available_voice_ids: set[str],
    default_voice_id: str,
) -> str:
    candidates = [
        voice_id
        for voice_id in ROLE_VOICE_CANDIDATES.get(voice_role or "", ())
        if voice_id in available_voice_ids
    ]
    if not candidates:
        return default_voice_id
    identity = (speaker or voice_role or "narrator").strip().casefold().encode("utf-8")
    index = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big") % len(candidates)
    return candidates[index]


async def _available_voice_ids(
    client: httpx.AsyncClient,
    channel: SpeechChannel,
    headers: dict[str, str],
) -> set[str]:
    cached = _voice_cache.get(channel.id)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _VOICE_CACHE_SECONDS:
        return cached[1]
    try:
        response = await client.post(
            f"{channel.base_url.rstrip('/')}/v1/get_voice",
            headers=headers,
            json={"voice_type": "all"},
        )
        result = response.json() if response.content else {}
        base_resp = result.get("base_resp", {})
        if response.status_code != 200 or base_resp.get("status_code") not in {None, 0}:
            return set()
        available = {
            str(item.get("voice_id") or "").strip()
            for group in ("system_voice", "voice_cloning", "voice_generation")
            for item in result.get(group, [])
            if isinstance(item, dict) and str(item.get("voice_id") or "").strip()
        }
    except (httpx.HTTPError, ValueError, TypeError):
        return set()
    _voice_cache[channel.id] = (now, available)
    return available


def _speech_performance(
    model_name: str,
    text: str,
    speed: float,
    emotion: str,
) -> tuple[str, float, int, str | None]:
    normalized = emotion if emotion in SPEECH_EMOTIONS else "calm"
    provider_emotion = {
        "disappointed": "sad",
        "devastated": "sad",
    }.get(normalized, normalized)
    if model_name.startswith(("speech-2.6", "speech-02", "speech-01")):
        return text, speed, 0, provider_emotion

    # Speech 2.8 uses expressive interjection tags instead of the older
    # voice_setting.emotion field. Keep the stored subtitle text untouched.
    tag, speed_factor, pitch = {
        "calm": ("", 1.0, 0),
        "happy": ("(laughs) ", 1.05, 1),
        "surprised": ("(gasps) ", 1.08, 2),
        "disappointed": ("(sighs) ", 0.92, -1),
        "sad": ("(sniffs) ", 0.88, -2),
        "devastated": ("(sniffs) ", 0.82, -3),
        "angry": ("(groans) ", 1.06, 1),
        "fearful": ("(gasps) ", 0.96, 1),
    }[normalized]
    adjusted_speed = max(0.5, min(speed * speed_factor, 2.0))
    return f"{tag}{text}", adjusted_speed, pitch, None


async def _request_speech(
    client: httpx.AsyncClient,
    channel: SpeechChannel,
    headers: dict[str, str],
    payload: dict[str, object],
) -> dict[str, object]:
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
    return result


async def _request_speech_with_fallback(
    client: httpx.AsyncClient,
    channel: SpeechChannel,
    headers: dict[str, str],
    payload: dict[str, object],
    selected_voice_id: str,
) -> tuple[dict[str, object], str]:
    try:
        return await _request_speech(client, channel, headers, payload), selected_voice_id
    except SpeechProviderError as exc:
        invalid_voice = "voice id not exist" in str(exc).casefold()
        if not invalid_voice or selected_voice_id == channel.default_voice_id:
            raise
        voice_setting = payload.get("voice_setting")
        if not isinstance(voice_setting, dict):
            raise
        voice_setting["voice_id"] = channel.default_voice_id
        result = await _request_speech(client, channel, headers, payload)
        return result, channel.default_voice_id


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
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=30.0), follow_redirects=True
        ) as client:
            available = (
                await _available_voice_ids(client, channel, headers) if job.voice_role else set()
            )
            selected_voice_id = (
                _select_role_voice(
                    job.voice_role, job.speaker, available, channel.default_voice_id
                )
                if job.voice_role
                else job.voice_id
            )
            spoken_text, spoken_speed, pitch, provider_emotion = _speech_performance(
                channel.model_name, job.speech_text, job.speed, job.emotion
            )
            voice_setting: dict[str, object] = {
                "voice_id": selected_voice_id,
                "speed": spoken_speed,
                "vol": 1.0,
                "pitch": pitch,
            }
            if provider_emotion is not None:
                voice_setting["emotion"] = provider_emotion
            payload: dict[str, object] = {
                "model": channel.model_name,
                "text": spoken_text,
                "stream": False,
                "language_boost": "auto",
                "voice_setting": voice_setting,
                "audio_setting": {
                    "sample_rate": 32000,
                    "bitrate": 128000,
                    "format": channel.default_format,
                    "channel": 1,
                },
                "output_format": "hex",
            }
            await _update_job(runtime, job_id, voice_id=selected_voice_id)
            result, used_voice_id = await _request_speech_with_fallback(
                client, channel, headers, payload, selected_voice_id
            )
            if used_voice_id != selected_voice_id:
                await _update_job(runtime, job_id, voice_id=used_voice_id)
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
        "speaker": job.speaker,
        "voice_role": job.voice_role,
        "emotion": job.emotion,
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
