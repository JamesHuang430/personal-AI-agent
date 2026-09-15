"""Shared sound settings, durable audition reuse and deterministic local mixing."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from assistant_app.db.models import MusicJob, SpeechChannel, SpeechJob
from assistant_app.services.work_queue import enqueue

EDGE_VOICES = {
    "edge:zh-CN-XiaoxiaoNeural": "晓晓 · 女声（Edge）",
    "edge:zh-CN-YunxiNeural": "云希 · 男声（Edge）",
    "edge:zh-CN-YunjianNeural": "云健 · 男声（Edge）",
    "edge:en-US-JennyNeural": "Jenny · 英语女声（Edge）",
}


class AudioSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    voice_mode: Literal["auto", "edge", "minimax"] = "auto"
    voice_id: str = Field(default="", max_length=200)
    speed: float = Field(default=1.0, ge=0.5, le=2)
    subtitle_style: Literal["classic", "large", "panel"] = "classic"
    bgm_job_id: UUID | None = None
    bgm_volume: float = Field(default=0.15, ge=0, le=0.5)
    ducking: bool = True

    @model_validator(mode="after")
    def voice_matches_mode(self):
        if self.voice_mode == "edge" and self.voice_id not in EDGE_VOICES:
            raise ValueError("请选择列表中的 Edge 音色")
        if self.voice_mode == "minimax" and (
            not self.voice_id or self.voice_id.startswith("edge:")
        ):
            raise ValueError("请选择已配置的 MiniMax 音色")
        if self.voice_mode == "auto":
            self.voice_id = ""
        return self


def audio_settings(project):
    data = getattr(project, "postproduction", None) or {}
    return AudioSettings.model_validate({k: v for k, v in data.items() if not k.startswith("_")})


async def validate_audio_assets(session, user_id, config):
    if config.voice_mode == "minimax":
        channel = await session.scalar(
            select(SpeechChannel).where(SpeechChannel.is_active.is_(True))
        )
        if not channel:
            raise ValueError("未启用 MiniMax 语音渠道")
    if config.bgm_job_id:
        music = await session.scalar(
            select(MusicJob).where(
                MusicJob.id == config.bgm_job_id,
                MusicJob.user_id == user_id,
                MusicJob.status == "completed",
            )
        )
        if (
            not music
            or not music.storage_path
            or not await asyncio.to_thread(Path(music.storage_path).is_file)
        ):
            raise ValueError("所选音乐不可用，请选择自己的已完成音乐")


def speech_options(project, spec):
    config = audio_settings(project)
    if config.voice_mode != "auto":
        return {
            "voice_id": config.voice_id,
            "speed": config.speed,
            "speaker": str(spec.get("speaker") or "narrator")[:100],
            "voice_role": None,
            "emotion": "calm",
        }
    from assistant_app.services.director import _voice_id_for_spec, _voice_role_for_spec

    if project.production_mode in {"whiteboard", "image_motion"}:
        return {
            "voice_id": None,
            "speed": config.speed,
            "speaker": "narrator",
            "voice_role": None,
            "emotion": "calm",
        }
    voice = _voice_id_for_spec(spec, project.continuity_bible or {})
    return {
        "voice_id": voice,
        "speed": float(spec.get("speech_speed") or 1),
        "speaker": str(spec.get("speaker") or "旁白")[:100],
        "voice_role": None if voice else _voice_role_for_spec(spec, project.continuity_bible or {}),
        "emotion": str(spec.get("emotion") or "calm"),
    }


async def reserve_speech(session, project, sequence, text, options, *, schedule=False):
    """Caller holds project lock. Job + preview identity + queue commit atomically."""
    edge = str(options.get("voice_id") or "").startswith("edge:")
    channel = (
        None
        if edge
        else await session.scalar(select(SpeechChannel).where(SpeechChannel.is_active.is_(True)))
    )
    if not edge and not channel:
        raise ValueError("未启用语音渠道；可选择 Edge 音色")
    voice = options.get("voice_id") or (channel.default_voice_id if channel else "")
    identity = {
        "text": text,
        "options": options,
        "voice": voice,
        "channel": str(channel.id) if channel else None,
        "model": channel.model_name if channel else "edge",
        "endpoint": channel.base_url if channel else "edge",
        "format": channel.default_format if channel else "mp3",
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    state = dict(project.postproduction or {})
    previews = dict(state.get("_speech_previews") or {})
    cache = dict(state.get("_speech_cache") or {})
    old = previews.get(str(sequence), {})
    existing_id = cache.get(digest) or (old.get("job_id") if old.get("digest") == digest else None)
    if existing_id:
        job = await session.get(SpeechJob, UUID(existing_id))
        if job and job.user_id == project.user_id:
            # Failed/ambiguous jobs remain visible; never silently create another paid job.
            previews[str(sequence)] = {"job_id": str(job.id), "digest": digest}
            project.postproduction = state | {"_speech_previews": previews}
            return job
    if len(cache) >= 256:
        raise ValueError("此项目试听版本已达上限，请新建一版")
    job = SpeechJob(
        id=uuid4(),
        user_id=project.user_id,
        channel_id=channel.id if channel else None,
        speech_text=text,
        voice_id=voice,
        speed=options["speed"],
        speaker=options["speaker"],
        voice_role=options["voice_role"],
        emotion=options["emotion"],
        audio_format=channel.default_format if channel else "mp3",
        status="queued",
    )
    session.add(job)
    await session.flush()
    previews[str(sequence)] = {"job_id": str(job.id), "digest": digest}
    cache[digest] = str(job.id)
    project.postproduction = state | {"_speech_previews": previews, "_speech_cache": cache}
    if schedule:
        await enqueue(session, "speech", job.id)
    return job


async def director_speech(runtime, project, shot, spec):
    from assistant_app.db.models import DirectorProject, DirectorShot

    async with runtime.sessions() as session, session.begin():
        stored = await session.get(DirectorProject, project.id, with_for_update=True)
        current = await session.get(DirectorShot, shot.id)
        if current.speech_job_id:
            return current.speech_job_id
        options = speech_options(stored, spec)
        job = await reserve_speech(session, stored, shot.sequence, shot.speech_text, options)
        if job.status in {"queued", "processing"}:
            # A queued audition belongs to the speech worker; do not race it inline.
            from assistant_app.db.models import WorkItem

            queued = await session.scalar(
                select(WorkItem).where(
                    WorkItem.kind == "speech",
                    WorkItem.resource_id == job.id,
                    WorkItem.status.in_(["queued", "processing"]),
                )
            )
            if queued:
                raise ValueError("试听仍在生成，请完成后再继续制作")
        current.speech_job_id = job.id
        return job.id


def subtitle_style(project=None, *, whiteboard=False):
    style = audio_settings(project).subtitle_style
    size = 16 if whiteboard else 9
    if style == "large":
        size = 21 if whiteboard else 14
    border = "BorderStyle=3,BackColour=&H80000000," if style == "panel" else "BorderStyle=1,"
    return (
        f"FontName=Noto Sans CJK SC,FontSize={size},PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,{border}Outline={1 if whiteboard else 2},Shadow=0,Alignment=2,"
        f"MarginV={24 if whiteboard else 72}"
    )


async def mix_project_music(runtime, project, source, output, duration):
    """Never overwrite source or silently ignore a requested music track."""
    config = audio_settings(project)
    if not config.bgm_job_id or config.bgm_volume == 0:
        return str(source)
    from assistant_app.services.director_media import _run_media_command

    async with runtime.sessions() as session:
        await validate_audio_assets(session, project.user_id, config)
        music = await session.get(MusicJob, config.bgm_job_id)
    if await asyncio.to_thread(Path(source).resolve) == await asyncio.to_thread(
        Path(output).resolve
    ):
        raise ValueError("混音输出不能覆盖原始音轨")
    fade = max(0, duration - min(1.5, duration / 3))
    chain = (
        f"[1:a]aresample=48000,volume={config.bgm_volume},"
        f"afade=t=in:d=0.4,afade=t=out:st={fade:.3f}:d={duration - fade:.3f}[music];"
    )
    if config.ducking:
        chain += (
            "[0:a]asplit=2[voice][key];[music][key]"
            "sidechaincompress=threshold=0.025:ratio=8:attack=20:release=350[bed];"
        )
    else:
        chain += "[0:a]anull[voice];[music]anull[bed];"
    chain += "[voice][bed]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]"
    temporary = Path(output).with_suffix(".pending.mp4")
    try:
        await _run_media_command(
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-stream_loop",
            "-1",
            "-i",
            music.storage_path,
            "-filter_complex",
            chain,
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(temporary),
        )
        await asyncio.to_thread(temporary.replace, output)
    finally:
        await asyncio.to_thread(temporary.unlink, missing_ok=True)
    return str(output)
