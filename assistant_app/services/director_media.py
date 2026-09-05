from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from assistant_app.db.models import DirectorProject, DirectorShot, SpeechJob, VideoJob
from assistant_app.services.generated_files import GENERATED_ROOT

DIRECTOR_SUBTITLE_FONT_SIZE = 9


async def _run_media_command(*command: str) -> tuple[str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(600):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    decoded_out = stdout.decode(errors="ignore")
    decoded_err = stderr.decode(errors="ignore")
    if process.returncode != 0:
        raise RuntimeError(f"媒体处理失败：{decoded_err[-500:]}")
    return decoded_out, decoded_err


async def _probe_media(path: str | Path) -> dict[str, object]:
    stdout, _ = await _run_media_command(
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        os.fspath(path),
    )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe 没有返回有效 JSON") from exc
    return payload if isinstance(payload, dict) else {}


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def _subtitle_filter_path(path: Path) -> str:
    return path.as_posix().replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _dialogue_voice_filter(duration: float) -> str:
    # Video providers may synthesize their own spoken audio. Mixing that track
    # with the selected character TTS creates two simultaneous speakers, so the
    # final dialogue track intentionally contains only the verified TTS voice.
    return f"[1:a]aresample=48000,apad,atrim=0:{duration:.3f},volume=1.35[voice]"


async def _video_source_path(video_job: VideoJob) -> str:
    original_video_path = GENERATED_ROOT / f"{video_job.id}.mp4"
    if await asyncio.to_thread(original_video_path.is_file):
        return os.fspath(original_video_path)
    return str(video_job.storage_path or "")


async def _subtitle_filter(
    shot: DirectorShot,
    duration: float,
    start_seconds: float | None,
    end_seconds: float | None,
) -> str:
    await asyncio.to_thread(GENERATED_ROOT.mkdir, parents=True, exist_ok=True)
    subtitle_path = GENERATED_ROOT / f"director-shot-{shot.id}.srt"
    subtitle = re.sub(r"[\r\n]+", " ", shot.subtitle_text or shot.speech_text or "").strip()
    start = max(0.0, min(float(start_seconds or 0.0), max(0.0, duration - 0.5)))
    end = max(start + 0.5, min(float(end_seconds or duration - 0.05), duration - 0.05))
    srt = f"1\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{subtitle}\n"
    await asyncio.to_thread(subtitle_path.write_text, srt, encoding="utf-8")
    return (
        f"subtitles=filename='{_subtitle_filter_path(subtitle_path)}':"
        f"force_style='FontName=Noto Sans CJK SC,FontSize={DIRECTOR_SUBTITLE_FONT_SIZE},"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=72'"
    )


async def _render_dialogue_shot(
    shot: DirectorShot,
    video_job: VideoJob,
    speech_job: SpeechJob,
    *,
    subtitle_start_seconds: float | None = None,
    subtitle_end_seconds: float | None = None,
) -> str:
    video_source = await _video_source_path(video_job)
    video_available = video_source and await asyncio.to_thread(Path(video_source).is_file)
    if not video_available:
        raise RuntimeError("视频渠道没有留下可合成的文件")
    speech_available = speech_job.storage_path and await asyncio.to_thread(
        Path(speech_job.storage_path).is_file
    )
    if not speech_available:
        raise RuntimeError("语音渠道没有留下可合成的文件")

    rendered_path = GENERATED_ROOT / f"director-shot-{shot.id}.mp4"
    duration = float(shot.seconds)
    subtitle_filter = await _subtitle_filter(
        shot,
        duration,
        subtitle_start_seconds,
        subtitle_end_seconds,
    )
    voice_chain = _dialogue_voice_filter(duration)

    await _run_media_command(
        "ffmpeg",
        "-y",
        "-i",
        video_source,
        "-i",
        speech_job.storage_path,
        "-vf",
        subtitle_filter,
        "-filter_complex",
        voice_chain,
        "-map",
        "0:v:0",
        "-map",
        "[voice]",
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        os.fspath(rendered_path),
    )
    return os.fspath(rendered_path)


async def _render_native_audio_shot(
    shot: DirectorShot,
    video_job: VideoJob,
    *,
    subtitle_start_seconds: float | None = None,
    subtitle_end_seconds: float | None = None,
) -> str:
    video_source = await _video_source_path(video_job)
    if not video_source or not await asyncio.to_thread(Path(video_source).is_file):
        raise RuntimeError("视频渠道没有留下可合成的文件")
    duration = float(shot.seconds)
    subtitle_filter = await _subtitle_filter(
        shot,
        duration,
        subtitle_start_seconds,
        subtitle_end_seconds,
    )
    rendered_path = GENERATED_ROOT / f"director-shot-{shot.id}.mp4"
    await _run_media_command(
        "ffmpeg",
        "-y",
        "-i",
        video_source,
        "-vf",
        subtitle_filter,
        "-af",
        f"aresample=48000,apad,atrim=0:{duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        os.fspath(rendered_path),
    )
    return os.fspath(rendered_path)


async def _concat_shots(project: DirectorProject, shots: list[DirectorShot]) -> str:
    await asyncio.to_thread(GENERATED_ROOT.mkdir, parents=True, exist_ok=True)
    list_path = GENERATED_ROOT / f"director-{project.id}.concat.txt"
    output_path = GENERATED_ROOT / f"director-{project.id}.mp4"
    lines = [f"file '{Path(str(shot.rendered_path)).as_posix()}'" for shot in shots]
    await asyncio.to_thread(list_path.write_text, "\n".join(lines), encoding="utf-8")
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-t",
            str(project.target_seconds),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(600):
                _, stderr = await process.communicate()
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            raise RuntimeError(f"合片失败：{stderr.decode(errors='ignore')[-300:]}")
    finally:
        await asyncio.to_thread(list_path.unlink, missing_ok=True)
    return os.fspath(output_path)
