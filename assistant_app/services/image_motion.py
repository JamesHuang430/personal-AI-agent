"""Deterministic still-image camera moves. No video-model or network calls."""

import asyncio
import math
from pathlib import Path
from uuid import uuid4

from assistant_app.services.director_media import (
    _probe_media,
    _run_media_command,
    _subtitle_filter_path,
)
from assistant_app.services.generated_files import GENERATED_ROOT

IMAGE_MOTION_INSTRUCTIONS = (
    "\n本项目是图文动效讲解，不是动态电影，也不是逐笔白板。每镜是一幅完整静态图片，"
    "本地仅进行轻微推近、拉远或平移，并配合旁白和字幕。不要要求真实动作、口型、"
    "逐笔绘制或原生音效。positive_prompt 描述适合独立展示的完整静态构图，遵循用户"
    "指定的画风；不要图内文字，主体距画面边缘保留至少10%安全区。保持角色跨镜一致。"
    "speech_text 为解说词，speaker 和 voice_role 使用 narrator；每秒不超过4个汉字，"
    "subtitle_text 与解说一致。保留原有 JSON 结构和 continuity 对象。"
)


def motion_filter(width, height, frames, sequence):
    """24 fps, at most 8% zoom; deterministic so retries preserve the composition."""
    if width < 2 or height < 2 or width % 2 or height % 2 or frames < 2 or sequence < 1:
        raise ValueError("Invalid image-motion geometry")
    progress = f"min(on/{frames - 1},1)"
    effect = (sequence - 1) % 4
    zoom = f"1+0.08*{progress}" if effect == 0 else (
        f"1.08-0.08*{progress}" if effect == 1 else "1.08"
    )
    x = "(iw-iw/zoom)/2"
    if effect == 2:
        x = f"(iw-iw/zoom)*{progress}"
    elif effect == 3:
        x = f"(iw-iw/zoom)*(1-{progress})"
    return (
        f"scale={width*2}:{height*2}:force_original_aspect_ratio=decrease,"
        f"pad={width*2}:{height*2}:(ow-iw)/2:(oh-ih)/2:color=0x18212f,setsar=1,"
        f"zoompan=z='{zoom}':x='{x}':y='(ih-ih/zoom)/2':d={frames}:"
        f"s={width}x{height}:fps=24"
    )


async def render_image_motion(project, shot, speech):
    from assistant_app.services.director import _director_video_size
    from assistant_app.services.director_audio import subtitle_style
    from assistant_app.services.speech_timing import timed_cues
    from assistant_app.services.whiteboard import annotation_srt, subtitle_cues

    info = await _probe_media(speech.storage_path)
    audio_seconds = float((info.get("format") or {}).get("duration") or 0)
    if not 0.1 < audio_seconds <= 119:
        raise ValueError("图文动效旁白须在 0.1 至 119 秒之间")
    duration = math.ceil(max(float(shot.seconds), audio_seconds + 0.6) * 24) / 24
    if not 1 <= duration <= 120:
        raise ValueError("图文动效单镜不得超过 120 秒")
    if not shot.image_path or not await asyncio.to_thread(Path(shot.image_path).is_file):
        raise ValueError("图文动效图片不可用")
    frames = round(duration * 24)
    width, height = map(
        int, _director_video_size(project.aspect_ratio, project.resolution).split("x")
    )
    cues, source = timed_cues(speech, round(audio_seconds * 1000))
    subtitle = GENERATED_ROOT / f"director-shot-{shot.id}.srt"
    await asyncio.to_thread(
        subtitle.write_text,
        annotation_srt({"cues": cues}) if cues else subtitle_cues(shot.speech_text, audio_seconds),
        encoding="utf-8",
    )
    output = GENERATED_ROOT / f"director-shot-{shot.id}.mp4"
    temporary = GENERATED_ROOT / f"motion-{uuid4()}.mp4"
    filters = (
        motion_filter(width, height, frames, shot.sequence)
        + f",fade=t=in:d=0.25,fade=t=out:st={duration-0.25:.6f}:d=0.25,"
        + f"subtitles=filename='{_subtitle_filter_path(subtitle)}':"
        + f"force_style='{subtitle_style(project, whiteboard=True)}'"
    )
    try:
        await _run_media_command(
            "ffmpeg", "-y", "-filter_threads", "1", "-i", shot.image_path,
            "-i", speech.storage_path, "-vf", filters, "-af", "aresample=48000,apad",
            "-map", "0:v:0", "-map", "1:a:0", "-t", f"{duration:.6f}",
            "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(temporary),
        )
        await asyncio.to_thread(temporary.replace, output)
    finally:
        await asyncio.to_thread(temporary.unlink, missing_ok=True)
    return str(output), duration, audio_seconds, source
