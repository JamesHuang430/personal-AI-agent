import base64
import io
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image
from pydantic import ValidationError

from assistant_app.api.routes.director import DirectorProjectCreatePayload
from assistant_app.api.routes.image_channels import ImageChannelPayload, channel_payload
from assistant_app.db.models import DirectorProject
from assistant_app.services import director, image_gateway, whiteboard
from assistant_app.services.chat_tools import DirectorArguments


def raster_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (100, 80), "white").save(buffer, "PNG")
    return buffer.getvalue()


def test_new_api_and_chat_default_whiteboard_but_db_preserves_legacy_video():
    assert DirectorProjectCreatePayload(premise="测试白板讲解").production_mode == "whiteboard"
    assert DirectorArguments(premise="测试白板讲解").production_mode == "whiteboard"
    assert DirectorProject.__table__.c.production_mode.server_default.arg.text == "'video'"
    with pytest.raises(ValidationError):
        DirectorArguments(premise="测试白板讲解", production_mode="free")


@pytest.mark.parametrize("seconds", [4, 30, 60, 180, 300])
def test_whiteboard_duration_budget(seconds):
    values = list(map(int, whiteboard.whiteboard_durations(seconds, True)))
    assert sum(values) == seconds
    assert max(values) <= 35
    assert whiteboard.whiteboard_durations(seconds, False) == ["4"]


def test_mode_changes_approval_digest():
    project = SimpleNamespace(
        id=uuid4(),
        premise="test",
        visual_style="ink",
        personalization={},
        target_seconds=60,
        one_click=True,
        resolution="768P",
        aspect_ratio="9:16",
        production_mode="video",
    )
    previous = director.storyboard_hash(project, {})
    project.production_mode = "whiteboard"
    assert director.storyboard_hash(project, {}) != previous
    assert director._project_durations(project) == ["30", "30"]


def test_channel_has_no_secret_in_payload():
    channel = SimpleNamespace(
        id=uuid4(),
        name="image",
        base_url="https://example.test",
        model_name="image-01",
        qps_limit=1,
        is_active=True,
        encrypted_api_key="secret",
    )
    assert "secret" not in str(channel_payload(channel))
    for url in [
        "http://example.test",
        "https://user:password@example.test",
        "https://a.test/?key=x",
    ]:
        with pytest.raises(ValidationError):
            ImageChannelPayload(name="test", base_url=url)


def test_image_request_and_raster_validation(tmp_path):
    request = image_gateway.image_request(
        SimpleNamespace(model_name="image-01"), "x" * 2000, "9:16"
    )
    assert request["response_format"] == "base64"
    assert request["n"] == 1 and len(request["prompt"]) == 1500
    data = raster_bytes()
    payload = {
        "base_resp": {"status_code": 0},
        "data": {"image_base64": [base64.b64encode(data).decode()]},
    }
    assert image_gateway.decode_response(payload) == data
    path = tmp_path / "test.png"
    image_gateway.save_image(data, path)
    assert Image.open(path).size == (100, 80)
    with pytest.raises((ValueError, OSError)):
        image_gateway.save_image(b"<svg><script>unsafe</script></svg>", path)
    with pytest.raises(ValueError):
        image_gateway.decode_response(
            {"base_resp": {"status_code": 0}, "data": {"image_urls": ["http://127.0.0.1"]}}
        )


class Session:
    def __init__(self, project=None, shot=None, runs=None, channel=None):
        self.project, self.shot, self.runs, self.channel = project, shot, runs, channel

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def begin(self):
        return self

    async def get(self, model, *_args, **_kwargs):
        return self.project if model is DirectorProject else self.shot

    async def scalar(self, *_args):
        return self.channel

    async def scalars(self, *_args):
        return SimpleNamespace(all=lambda: self.runs)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_whiteboard_branch_keeps_approval_gate_and_never_calls_video(monkeypatch, approved):
    project = SimpleNamespace(
        id=uuid4(),
        status="queued",
        production_mode="whiteboard",
        review_required=True,
        storyboard_approved=approved,
    )
    runs = [
        SimpleNamespace(id=uuid4(), agent_key=key)
        for key in ("story", "visual", "media", "quality")
    ]
    session = Session(project=project, runs=runs)
    runtime = SimpleNamespace(sessions=lambda: session)
    for name in ("_execute_agent_run", "_run_director_preflight", "_update_project", "_update_run"):
        monkeypatch.setattr(director, name, AsyncMock())
    whiteboard_run = AsyncMock()
    video_run = AsyncMock(side_effect=AssertionError("video must never be called"))
    monkeypatch.setattr(director, "run_whiteboard_media", whiteboard_run)
    monkeypatch.setattr(director, "_create_and_run_shot", video_run)
    await director._run_director_project(runtime, None, project.id)
    assert whiteboard_run.await_count == int(approved)
    video_run.assert_not_awaited()
    if not approved:
        assert director._update_project.call_args.kwargs["status"] == "awaiting_storyboard"


@pytest.mark.asyncio
async def test_uploaded_image_reused_without_channel_or_http(tmp_path, monkeypatch):
    path = tmp_path / "already.png"
    path.write_bytes(raster_bytes())
    shot = SimpleNamespace(image_path=str(path))
    session = Session(shot=shot)
    runtime = SimpleNamespace(sessions=lambda: session)
    monkeypatch.setattr(image_gateway.httpx, "AsyncClient", lambda **_: pytest.fail("No network"))
    assert await image_gateway.generate_shot_image(runtime, None, uuid4(), "9:16") == str(path)


@pytest.mark.asyncio
async def test_uncertain_image_submission_cannot_charge_again(monkeypatch):
    shot = SimpleNamespace(image_path=None, image_submission_started_at=datetime.now(UTC))
    channel = SimpleNamespace(id=uuid4(), qps_limit=1)
    session = Session(shot=shot, channel=channel)
    runtime = SimpleNamespace(
        sessions=lambda: session,
        redis=SimpleNamespace(incr=AsyncMock(return_value=1), expire=AsyncMock()),
    )
    monkeypatch.setattr(
        image_gateway.httpx, "AsyncClient", lambda **_: pytest.fail("No repeat bill")
    )
    with pytest.raises(ValueError, match="不自动重发"):
        await image_gateway.generate_shot_image(runtime, None, uuid4(), "9:16")


def test_subtitle_chunks_are_bounded_and_escape_markup():
    srt = whiteboard.subtitle_cues("<b>字幕</b>{\\pos(0,0)}" + "你好" * 30, 8)
    assert "<b>" not in srt and "\\pos" not in srt
    assert "00:00:08,000" in srt
    assert max(len(line) for line in srt.splitlines() if line and "-->" not in line) <= 22


def test_real_whiteboard_encoding_blank_and_sparse(tmp_path):
    import cv2
    import numpy as np

    from assistant_app.services.whiteboard_renderer import frames, render

    blank = np.full((180, 320, 3), 255, dtype=np.uint8)
    assert len(list(frames(blank, 0.5, fps=4))) == 2
    image = blank.copy()
    cv2.rectangle(image, (30, 35), (140, 130), (90, 180, 220), -1)
    cv2.circle(image, (230, 80), 35, (40, 120, 40), 2)
    sequence = list(frames(image, 2, fps=4))
    assert np.array_equal(sequence[-1], image)
    assert np.all(sequence[0] == 255)
    source, output = tmp_path / "fixture.png", tmp_path / "whiteboard.mp4"
    cv2.imwrite(str(source), image)
    render(source, output, 2, 320, 180)
    capture = cv2.VideoCapture(str(output))
    try:
        assert capture.isOpened()
        assert capture.get(cv2.CAP_PROP_FRAME_COUNT) == 48
        assert capture.get(cv2.CAP_PROP_FPS) == 24
        ok, frame = capture.read()
        assert ok and frame.shape == (180, 320, 3)
    finally:
        capture.release()
