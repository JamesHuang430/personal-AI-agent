from copy import deepcopy

import pytest
from PIL import Image, ImageDraw

from assistant_app.services.whiteboard_annotations import (
    annotation_draft,
    parse_srt,
    validate_annotation,
)


def fixture(tmp_path):
    path = tmp_path / "scene.png"
    image = Image.new("RGB", (320, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 100, 140), fill="#70b070", outline="black", width=3)
    draw.ellipse((220, 40, 290, 110), fill="#f0c050", outline="black", width=3)
    # Deliberately unannotated detail must not suddenly appear at the end.
    draw.rectangle((150, 150, 180, 170), fill="black")
    image.save(path)
    data = annotation_draft(path, "先画太阳。再画树木。", 2400, 3000)
    data["cues"] = [
        {"id": 1, "startMs": 0, "endMs": 1000, "text": "先画太阳。"},
        {"id": 2, "startMs": 1000, "endMs": 2400, "text": "再画树木。"},
    ]
    data["elements"] = [
        {
            "id": "sun",
            "label": "太阳",
            "sequence": 1,
            "narrativeRole": "先讲太阳",
            "cueIds": [1],
            "region": {"x": 205, "y": 20, "width": 100, "height": 110},
            "reveal": {"startMs": 100, "durationMs": 800, "protectedRegions": []},
        },
        {
            "id": "tree",
            "label": "树木",
            "sequence": 2,
            "narrativeRole": "再讲树木",
            "cueIds": [2],
            "region": {"x": 20, "y": 20, "width": 90, "height": 130},
            "reveal": {"startMs": 1100, "durationMs": 1000, "protectedRegions": []},
        },
    ]
    return path, data


def test_image_binding_and_schema(tmp_path):
    path, data = fixture(tmp_path)
    assert validate_annotation(data, path)["version"] == 2
    for change in [
        lambda d: d["canvas"].update(width=0),
        lambda d: d["canvas"].update(width=321),
        lambda d: d.update(imageSha256="0" * 64),
        lambda d: d["elements"][0]["region"].update(x=-1),
        lambda d: d["elements"][0]["region"].update(width=400),
        lambda d: d["elements"][0]["region"].update(x=1.5),
        lambda d: d["elements"][1]["reveal"].update(startMs=500),
        lambda d: d["elements"][1]["reveal"].update(durationMs=2000),
        lambda d: d["elements"][0].update(cueIds=[99]),
        lambda d: d["elements"][1].update(sequence=1),
        lambda d: d["elements"][1].update(id="sun"),
        lambda d: d["elements"][1]["reveal"].update(
            protectedRegions=[{"x": 310, "y": 0, "width": 20, "height": 10}]
        ),
    ]:
        bad = deepcopy(data)
        change(bad)
        with pytest.raises(ValueError):
            validate_annotation(bad, path)


def test_srt_strict_timing():
    cues = parse_srt(
        "\ufeff1\n00:00:00,100 --> 00:00:01,000\n太阳。\n\n2\n00:00:01.000 --> 00:00:02.000\n树木。"
    )
    assert cues[1]["startMs"] == 1000
    for text in [
        "bad",
        "1\n00:00:02,000 --> 00:00:01,000\n倒置",
        "1\n00:99:00,000 --> 00:99:01,000\n错误",
    ]:
        with pytest.raises(ValueError):
            parse_srt(text)


@pytest.mark.parametrize("mode", ["grid", "skeleton"])
def test_real_stream_order_masks_and_exact_duration(tmp_path, mode):
    import cv2
    import numpy as np

    from assistant_app.services.whiteboard_stream import prepare, render

    path, data = fixture(tmp_path)
    data["inkPath"] = mode
    # Add an explicit permanent protection inside the first region.
    data["elements"][0]["reveal"]["protectedRegions"] = [
        {"x": 240, "y": 65, "width": 10, "height": 10}
    ]
    renderer, transformed = prepare(path, data, 320, 180)
    mask = renderer._allowed_mask(transformed["elements"][0], transformed["elements"][1:])
    assert not mask[65:75, 240:250].any()
    output = tmp_path / f"{mode}.mp4"
    render(path, data, output, 320, 180)
    capture = cv2.VideoCapture(str(output))
    try:
        assert capture.get(cv2.CAP_PROP_FRAME_COUNT) == 72
        frames = []
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
        assert np.mean(frames[0]) > 248
        # Right-hand sun is drawn before the left tree, following narration.
        assert np.mean(frames[23][50:100, 230:280]) < 235
        assert np.mean(frames[23][40:130, 40:90]) > 248
        assert np.mean(frames[-1][40:130, 40:90]) < 235
        assert np.mean(frames[-1][67:73, 242:248]) > 245
        assert np.mean(frames[-1][152:168, 152:178]) > 248
    finally:
        capture.release()


def test_blank_and_fully_occluded_regions_keep_frames(tmp_path):
    import cv2

    from assistant_app.services.whiteboard_stream import render

    path, data = fixture(tmp_path)
    data["elements"][0]["region"] = deepcopy(data["elements"][1]["region"])
    data["elements"][1]["region"] = {"x": 0, "y": 0, "width": 150, "height": 150}
    data["elements"][1]["reveal"]["protectedRegions"] = [data["elements"][1]["region"]]
    output = tmp_path / "occluded.mp4"
    render(path, data, output, 320, 180)
    capture = cv2.VideoCapture(str(output))
    assert capture.get(cv2.CAP_PROP_FRAME_COUNT) == 72
    capture.release()

    Image.new("RGB", (320, 180), "white").save(path)
    from assistant_app.services.whiteboard_annotations import image_identity

    data["imageSha256"] = image_identity(path)[1]
    data["elements"][1]["reveal"]["protectedRegions"] = []
    render(path, data, output, 320, 180)
    capture = cv2.VideoCapture(str(output))
    assert capture.get(cv2.CAP_PROP_FRAME_COUNT) == 72
    capture.release()


def test_inline_image_not_stored_in_request_logs():
    from assistant_app.services.request_logging import redact_api_keys

    payload = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,private"}}]
    assert "private" not in str(redact_api_keys(payload))


@pytest.mark.asyncio
async def test_annotation_model_gets_real_image_and_only_controls_regions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from assistant_app.services import model_gateway, whiteboard

    path, data = fixture(tmp_path)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def scalar(self, *_):
            return SimpleNamespace(model_name="fixture-vision")

    import json

    call = AsyncMock(return_value={"content": json.dumps({"elements": data["elements"]})})
    monkeypatch.setattr(model_gateway, "agent_text_completion", call)
    result = await whiteboard.propose_regions(
        SimpleNamespace(sessions=Session),
        object(),
        SimpleNamespace(id="fixture"),
        SimpleNamespace(image_path=str(path)),
        data | {"elements": []},
    )
    assert result["imageSha256"] == data["imageSha256"]
    messages = call.call_args.args[-1]
    assert messages[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert result["elements"][0]["label"] == "太阳"
