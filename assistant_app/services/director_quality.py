from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from assistant_app.db.models import DirectorProject, DirectorShot
from assistant_app.services.director_media import _probe_media


async def _build_quality_report(
    project: DirectorProject,
    shots: list[DirectorShot],
    final_path: str | None,
) -> dict[str, object]:
    issues: list[str] = []
    shot_checks: list[dict[str, object]] = []
    for shot in shots:
        media = dict((shot.continuity_snapshot or {}).get("_media") or {})
        audio_source = str(media.get("audio_source") or "legacy")
        rendered = Path(shot.rendered_path or "")
        if not shot.rendered_path or not await asyncio.to_thread(rendered.is_file):
            issues.append(f"第 {shot.sequence} 镜缺少合成文件")
            continue
        info = await _probe_media(rendered)
        streams = info.get("streams", [])
        has_video = isinstance(streams, list) and any(
            isinstance(item, dict) and item.get("codec_type") == "video" for item in streams
        )
        has_audio = isinstance(streams, list) and any(
            isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams
        )
        try:
            duration = float(dict(info.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        has_subtitle = bool(shot.subtitle_text and shot.speech_text)
        if not has_video:
            issues.append(f"第 {shot.sequence} 镜没有视频轨")
        if not has_audio:
            issues.append(f"第 {shot.sequence} 镜没有语音轨")
        if audio_source == "native_h3" and not media.get("native_audio_detected"):
            issues.append(f"第 {shot.sequence} 镜标记为 H3 原生音频但未检测到原生音轨")
        if audio_source != "native_h3" and not shot.speech_job_id:
            issues.append(f"第 {shot.sequence} 镜既没有 H3 原生音频也没有兜底语音任务")
        if not has_subtitle:
            issues.append(f"第 {shot.sequence} 镜没有字幕文本")
        subtitle_start = media.get("subtitle_start_seconds")
        subtitle_end = media.get("subtitle_end_seconds")
        if audio_source != "legacy" and (
            not isinstance(subtitle_start, (int, float))
            or not isinstance(subtitle_end, (int, float))
            or not 0 <= float(subtitle_start) < float(subtitle_end) <= float(shot.seconds)
        ):
            issues.append(f"第 {shot.sequence} 镜字幕时间窗无效")
        if abs(duration - float(shot.seconds)) > 1.0:
            issues.append(f"第 {shot.sequence} 镜时长异常：{duration:.2f}s")
        shot_checks.append(
            {
                "sequence": shot.sequence,
                "video": has_video,
                "audio": has_audio,
                "audio_source": audio_source,
                "single_speaker": media.get("single_speaker"),
                "emotion": media.get("emotion"),
                "background_music_directed": bool(media.get("background_music")),
                "subtitle_text_present": has_subtitle,
                "duration_seconds": round(duration, 3),
            }
        )

    final_check: dict[str, object] | None = None
    if project.one_click:
        if not final_path or not await asyncio.to_thread(Path(final_path).is_file):
            issues.append("最终合片文件不存在")
        else:
            info = await _probe_media(final_path)
            streams = info.get("streams", [])
            has_video = isinstance(streams, list) and any(
                isinstance(item, dict) and item.get("codec_type") == "video" for item in streams
            )
            has_audio = isinstance(streams, list) and any(
                isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams
            )
            try:
                duration = float(dict(info.get("format") or {}).get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if not has_video or not has_audio:
                issues.append("最终合片缺少视频轨或语音轨")
            if abs(duration - project.target_seconds) > 1.5:
                issues.append(f"最终合片时长异常：{duration:.2f}s")
            final_check = {
                "video": has_video,
                "audio": has_audio,
                "duration_seconds": round(duration, 3),
            }
    return {
        "passed": not issues,
        "scope": "technical_integrity",
        "not_checked": ["dialogue_accuracy", "subtitle_alignment", "character_consistency"],
        "shots": shot_checks,
        "final": final_check,
        "issues": issues,
        "checked_at": datetime.now(UTC).isoformat(),
    }
