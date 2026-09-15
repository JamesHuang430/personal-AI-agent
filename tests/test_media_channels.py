from types import SimpleNamespace
from uuid import uuid4

import pytest

from assistant_app.api.routes.admin import (
    MusicChannelCreatePayload,
    SpeechChannelCreatePayload,
    VideoChannelCreatePayload,
)
from assistant_app.db.models import VideoJob
from assistant_app.services.model_gateway import AGENT_TOOLS
from assistant_app.services.speech_gateway import (
    EDGE_FEMALE_VOICE_ID,
    SPEECH_BALANCE_MESSAGE,
    SpeechProviderError,
    _edge_performance,
    _edge_voice_name,
    _request_speech,
    _request_speech_with_fallback,
    _select_role_voice,
    _speech_performance,
)
from assistant_app.services.video_gateway import (
    _minimax_ratio,
    _minimax_video_urls,
    video_job_payload,
)


def test_minimax_video_channel_defaults() -> None:
    payload = VideoChannelCreatePayload(
        name="MiniMax H3",
        base_url="https://api.minimaxi.com/",
        api_key="secret",
        model_name="MiniMax-H3",
        provider="minimax",
    )

    assert payload.base_url == "https://api.minimaxi.com"
    assert payload.default_resolution == "768P"
    assert _minimax_ratio("1280x720") == "16:9"
    assert _minimax_ratio("720x1280") == "9:16"


def test_minimax_video_urls_support_official_and_aiping() -> None:
    official_create, official_query = _minimax_video_urls(
        "https://api.minimaxi.com/", "task-1"
    )
    aiping_create, aiping_query = _minimax_video_urls(
        "https://aiping.cn/api/v1", "task-2"
    )

    assert official_create == "https://api.minimaxi.com/v2/video_generation"
    assert official_query == "https://api.minimaxi.com/v2/query/video_generation/task-1"
    assert aiping_create == (
        "https://aiping.cn/api/v1/multimodal/minimax/videos/video_generation"
    )
    assert aiping_query == (
        "https://aiping.cn/api/v1/multimodal/minimax/videos/query/video_generation/task-2"
    )


def test_music_channel_defaults() -> None:
    payload = MusicChannelCreatePayload(
        name="MiniMax Music",
        base_url="https://api.minimaxi.com/",
        api_key="secret",
    )

    assert payload.base_url == "https://api.minimaxi.com"
    assert payload.model_name == "music-2.6"
    assert payload.default_format == "mp3"


def test_speech_channel_defaults() -> None:
    payload = SpeechChannelCreatePayload(
        name="MiniMax Speech",
        base_url="https://api.minimaxi.com/",
        api_key="secret",
    )

    assert payload.base_url == "https://api.minimaxi.com"
    assert payload.model_name == "speech-2.8-hd"
    assert payload.default_voice_id == "male-qn-qingse"
    assert payload.default_format == "mp3"


def test_role_voice_selection_is_real_and_stable_per_character() -> None:
    available = {
        "Chinese (Mandarin)_Pure-hearted_Boy",
        "Chinese (Mandarin)_Straightforward_Boy",
        "Chinese (Mandarin)_Mature_Woman",
    }

    first = _select_role_voice("boy", "小明", available, "fallback")
    second = _select_role_voice("boy", "小明", available, "fallback")
    woman = _select_role_voice("adult_female", "林夏", available, "fallback")

    assert first == second
    assert first in available
    assert woman == "Chinese (Mandarin)_Mature_Woman"


def test_speech_28_uses_performance_tags_without_changing_subtitle_text() -> None:
    text, speed, pitch, provider_emotion = _speech_performance(
        "speech-2.8-hd", "你终于回来了。", 1.0, "devastated"
    )

    assert text == "(sniffs) 你终于回来了。"
    assert speed == 0.82
    assert pitch == -3
    assert provider_emotion is None

    legacy = _speech_performance("speech-2.6-hd", "真的吗？", 1.0, "surprised")
    assert legacy == ("真的吗？", 1.0, 0, "surprised")


@pytest.mark.asyncio
async def test_invalid_voice_retries_once_with_channel_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def request(*_args: object) -> dict[str, object]:
        payload = _args[-1]
        assert isinstance(payload, dict)
        voice_setting = payload["voice_setting"]
        assert isinstance(voice_setting, dict)
        voice_id = str(voice_setting["voice_id"])
        calls.append(voice_id)
        if len(calls) == 1:
            raise SpeechProviderError("MiniMax：voice id not exist")
        return {"data": {"audio": "00"}}

    monkeypatch.setattr("assistant_app.services.speech_gateway._request_speech", request)
    channel = SimpleNamespace(default_voice_id="male-qn-qingse")
    payload: dict[str, object] = {"voice_setting": {"voice_id": "invented_voice"}}

    result, used_voice = await _request_speech_with_fallback(
        SimpleNamespace(), channel, {}, payload, "invented_voice"
    )

    assert result["data"] == {"audio": "00"}
    assert used_voice == "male-qn-qingse"
    assert calls == ["invented_voice", "male-qn-qingse"]


def test_balance_error_is_actionable() -> None:
    assert "余额不足" in SPEECH_BALANCE_MESSAGE
    assert "运营后台" in SPEECH_BALANCE_MESSAGE


def test_explicit_edge_female_voice_and_emotional_performance() -> None:
    assert _edge_voice_name(EDGE_FEMALE_VOICE_ID) == "zh-CN-XiaoxiaoNeural"
    assert _edge_voice_name("Chinese (Mandarin)_Sweet_Lady") is None
    assert _edge_performance(1.0, "happy") == ("+6%", "+3Hz")
    assert _edge_performance(1.0, "sad") == ("-8%", "-4Hz")


@pytest.mark.asyncio
async def test_provider_balance_error_is_translated() -> None:
    class BalanceClient:
        async def post(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                status_code=200,
                content=b"{}",
                json=lambda: {
                    "base_resp": {"status_code": 1008, "status_msg": "insufficient balance"}
                },
            )

    channel = SimpleNamespace(base_url="https://api.minimaxi.com")
    with pytest.raises(SpeechProviderError, match="余额不足"):
        await _request_speech(BalanceClient(), channel, {}, {})


def test_agent_exposes_director_video_speech_and_music_tools() -> None:
    tool_names = {tool["function"]["name"] for tool in AGENT_TOOLS}

    assert {
        "start_director_production",
        "generate_video",
        "generate_speech",
        "generate_music",
    }.issubset(tool_names)

    director_tool = next(
        tool for tool in AGENT_TOOLS if tool["function"]["name"] == "start_director_production"
    )
    assert director_tool["function"]["parameters"]["properties"]["resolution"]["enum"] == [
        "768P",
        "2K",
    ]


def test_completed_video_exposes_separate_preview_and_download_urls() -> None:
    job = VideoJob(
        id=uuid4(),
        user_id=uuid4(),
        channel_id=uuid4(),
        prompt="rainy bus stop",
        status="completed",
        seconds="4",
        size="720x1280",
        resolution="2K",
    )

    payload = video_job_payload(job)

    assert payload["preview_url"] == f"/api/v1/videos/{job.id}/preview"
    assert payload["download_url"] == f"/api/v1/videos/{job.id}/download"
    assert payload["resolution"] == "2K"
