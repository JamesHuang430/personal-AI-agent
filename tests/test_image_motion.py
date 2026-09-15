import asyncio
import shutil
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from assistant_app.services import image_motion
from assistant_app.services.director import _project_durations
from assistant_app.services.director_audio import speech_options


def test_motion_mode_uses_local_duration_and_single_narrator():
    from assistant_app.api.routes.director import DirectorProjectCreatePayload
    from assistant_app.services.chat_tools import DirectorArguments

    for schema in (DirectorProjectCreatePayload, DirectorArguments):
        assert schema(premise="讲解测试", production_mode="image_motion").production_mode == (
            "image_motion"
        )
    project = SimpleNamespace(
        production_mode="image_motion", one_click=True, target_seconds=60, postproduction={}
    )
    assert _project_durations(project) == ["30", "30"]
    assert speech_options(project, {})["speaker"] == "narrator"
    project.one_click = False
    assert _project_durations(project) == ["4"]


@pytest.mark.parametrize("sequence", [1, 2, 3, 4])
def test_motion_filter_is_bounded_repeatable_and_preserves_aspect(sequence):
    result = image_motion.motion_filter(720, 1280, 96, sequence)
    assert result == image_motion.motion_filter(720, 1280, 96, sequence + 4)
    assert "force_original_aspect_ratio=decrease" in result
    assert "s=720x1280:fps=24" in result
    assert "on/95" in result and ":d=96:" in result
    assert "1.08" in result or "0.08" in result


@pytest.mark.parametrize("geometry", [(1, 2, 96, 1), (4, 3, 96, 1), (4, 4, 1, 1)])
def test_motion_filter_rejects_invalid_geometry(geometry):
    with pytest.raises(ValueError):
        image_motion.motion_filter(*geometry)


@pytest.mark.asyncio
async def test_render_failure_preserves_previous_output_and_cleans_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(image_motion, "GENERATED_ROOT", tmp_path)
    monkeypatch.setattr(image_motion, "_probe_media", AsyncMock(
        return_value={"format": {"duration": "6"}}
    ))
    picture = tmp_path / "source.png"
    picture.write_bytes(b"fixture")
    shot = SimpleNamespace(id=uuid4(), sequence=1, image_path=str(picture), seconds="4",
                           speech_text="测试")
    speech = SimpleNamespace(storage_path="fixture.wav", speech_text="测试", timing={})
    project = SimpleNamespace(aspect_ratio="16:9", resolution="768P", postproduction={})
    output = tmp_path / f"director-shot-{shot.id}.mp4"
    output.write_bytes(b"previous good output")

    async def fail(*args):
        assert float(args[args.index("-t") + 1]) > 6  # Never truncate longer narration.
        from pathlib import Path

        await asyncio.to_thread(Path(args[-1]).write_bytes, b"incomplete")
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(image_motion, "_run_media_command", fail)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        await image_motion.render_image_motion(project, shot, speech)
    assert output.read_bytes() == b"previous good output"
    assert not list(tmp_path.glob("motion-*.mp4"))


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Requires FFmpeg")
@pytest.mark.parametrize("sequence,shape", [(1, (320, 180)), (2, (180, 320)),
                                           (3, (320, 180)), (4, (180, 320))])
async def test_actual_motion_frames_audio_and_dimensions(tmp_path, monkeypatch, sequence, shape):
    from PIL import Image, ImageDraw

    from assistant_app.services import director

    cv2 = pytest.importorskip("cv2")
    monkeypatch.setattr(image_motion, "GENERATED_ROOT", tmp_path)
    monkeypatch.setattr(director, "_director_video_size", lambda *args: f"{shape[0]}x{shape[1]}")
    picture = tmp_path / "picture.png"
    raster = Image.new("RGB", shape, "#eeeeee")
    draw = ImageDraw.Draw(raster)
    for x in range(15, shape[0], 35):
        for y in range(15, shape[1], 35):
            draw.rectangle((x, y, x+15, y+15), fill="#2477bb")
    raster.save(picture)
    narration = tmp_path / "narration.wav"
    with wave.open(str(narration), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 48000)
    speech = SimpleNamespace(storage_path=str(narration), speech_text="测试旁白", timing={})
    shot = SimpleNamespace(id=uuid4(), sequence=sequence, image_path=str(picture), seconds="2",
                           speech_text=speech.speech_text)
    project = SimpleNamespace(aspect_ratio="16:9", resolution="768P", postproduction={})
    path, seconds, spoken, source = await image_motion.render_image_motion(project, shot, speech)
    assert seconds > spoken == 2 and source == "estimated"
    probe = await image_motion._probe_media(path)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == shape
    assert any(s["codec_type"] == "audio" for s in probe["streams"])
    assert abs(float(probe["format"]["duration"]) - seconds) < 0.15

    def sample_frames():
        capture = cv2.VideoCapture(path)
        try:
            capture.set(cv2.CAP_PROP_POS_MSEC, 600)
            ok1, first = capture.read()
            capture.set(cv2.CAP_PROP_POS_MSEC, 1600)
            ok2, later = capture.read()
            assert ok1 and ok2
            assert cv2.absdiff(first, later).mean() > 0.5
        finally:
            capture.release()

    await asyncio.to_thread(sample_frames)
