from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_app.services.director_audio import AudioSettings, audio_settings, subtitle_style
from assistant_app.services.speech_timing import match_cues, timed_cues


def test_timestamp_matching_retains_punctuation_and_pauses():
    records = [
        {"text": "太阳", "startMs": 100, "endMs": 600},
        {"text": "出来了", "startMs": 620, "endMs": 1100},
        {"text": "树木", "startMs": 1600, "endMs": 1900},
        {"text": "长大了", "startMs": 1950, "endMs": 2400},
    ]
    cues = match_cues("太阳出来了。树木长大了！", records, 2500)
    assert [c["text"] for c in cues] == ["太阳出来了。", "树木长大了！"]
    assert [(c["startMs"], c["endMs"]) for c in cues] == [(100, 1100), (1600, 2400)]
    speech = SimpleNamespace(
        speech_text="太阳出来了。树木长大了！", timing={"source": "edge", "cues": cues}
    )
    assert timed_cues(speech, 2500)[1] == "provider"


@pytest.mark.parametrize(
    "records",
    [
        [],
        [{"text": "错误", "startMs": 0, "endMs": 100}],
        [{"text": "太阳", "startMs": -1, "endMs": 100}],
        [{"text": "太阳", "startMs": 101, "endMs": 100}],
        [{"text": "太阳", "startMs": 0, "endMs": 2000}],
        [{"text": "太阳", "startMs": 0.1, "endMs": 100}],
        [{"text": "太", "startMs": 0, "endMs": 500}, {"text": "阳", "startMs": 400, "endMs": 600}],
    ],
)
def test_invalid_timestamps_fall_back_without_fabrication(records):
    assert match_cues("太阳", records, 1000) == []


@pytest.mark.parametrize(
    "data",
    [
        {"speed": float("nan")},
        {"speed": 0},
        {"bgm_volume": 1},
        {"bgm_volume": float("inf")},
        {"voice_mode": "edge", "voice_id": "bad"},
        {"voice_mode": "minimax", "voice_id": "edge:zh-CN-XiaoxiaoNeural"},
        {"subtitle_style": "FontName=evil"},
        {"_speech_previews": {}},
    ],
)
def test_sound_settings_reject_untrusted_or_invalid_values(data):
    with pytest.raises(ValueError):
        AudioSettings.model_validate(data)


def test_old_projects_preserve_auto_voice_and_private_cache_not_public():
    assert audio_settings(SimpleNamespace()).voice_mode == "auto"
    project = SimpleNamespace(postproduction={"subtitle_style": "panel", "_speech_previews": {}})
    assert "_speech_previews" not in audio_settings(project).model_dump()
    assert "BorderStyle=3" in subtitle_style(project)
    assert "FontSize=16" in subtitle_style(None, whiteboard=True)


@pytest.mark.asyncio
async def test_edge_stream_saves_audio_and_provider_boundaries(tmp_path, monkeypatch):
    import sys

    from assistant_app.services import speech_gateway

    class Communicate:
        def __init__(self, *args, **kwargs):
            assert kwargs["boundary"] == "WordBoundary"

        async def stream(self):
            yield {"type": "audio", "data": b"fixture-audio"}
            yield {"type": "WordBoundary", "text": "太阳", "offset": 1000000, "duration": 5000000}

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=Communicate))
    path = tmp_path / "voice.mp3"
    timing = await speech_gateway._save_edge_speech("太阳", "test", "+0%", "+0Hz", str(path))
    assert path.read_bytes() == b"fixture-audio"
    assert timing["cues"] == [{"text": "太阳", "startMs": 100, "endMs": 600}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/x",
        "https://127.0.0.1/x",
        "https://minimax.io.evil.test/x",
        "https://user:password@minimax.io/x",
        "https://minimax.io:88/x",
    ],
)
async def test_subtitle_download_rejects_unsafe_urls(url, monkeypatch):
    from assistant_app.services import speech_gateway

    client = AsyncMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(speech_gateway.httpx, "AsyncClient", client)
    result = await speech_gateway._minimax_timing({"data": {"subtitle_file": url}}, "太阳", 1000)
    assert result == {"source": "estimated", "cues": []}
    client.assert_not_called()


@pytest.mark.asyncio
async def test_minimax_subtitle_matching_and_no_auth_forwarded(monkeypatch):
    import httpx

    from assistant_app.services import speech_gateway

    real_client = httpx.AsyncClient

    def handle(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[{"text": "太阳", "time_begin": 100, "time_end": 800}])

    monkeypatch.setattr(
        speech_gateway.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    result = await speech_gateway._minimax_timing(
        {"data": {"subtitle_file": "https://cdn.minimax.io/subtitle.json"}}, "太阳。", 1000
    )
    assert result["source"] == "minimax"
    assert result["cues"][0]["text"] == "太阳。"


@pytest.mark.asyncio
async def test_zero_music_volume_never_opens_assets():
    from assistant_app.services.director_audio import mix_project_music

    project = SimpleNamespace(postproduction={"bgm_volume": 0})
    assert await mix_project_music(None, project, "original.mp4", "mixed.mp4", 4) == "original.mp4"
