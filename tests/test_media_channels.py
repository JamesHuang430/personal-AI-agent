from uuid import uuid4

from assistant_app.api.routes.admin import (
    MusicChannelCreatePayload,
    SpeechChannelCreatePayload,
    VideoChannelCreatePayload,
)
from assistant_app.db.models import VideoJob
from assistant_app.services.model_gateway import AGENT_TOOLS
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


def test_agent_exposes_video_speech_and_music_tools() -> None:
    tool_names = {tool["function"]["name"] for tool in AGENT_TOOLS}

    assert {"generate_video", "generate_speech", "generate_music"}.issubset(tool_names)


def test_completed_video_exposes_separate_preview_and_download_urls() -> None:
    job = VideoJob(
        id=uuid4(),
        user_id=uuid4(),
        channel_id=uuid4(),
        prompt="rainy bus stop",
        status="completed",
        seconds="4",
        size="720x1280",
    )

    payload = video_job_payload(job)

    assert payload["preview_url"] == f"/api/v1/videos/{job.id}/preview"
    assert payload["download_url"] == f"/api/v1/videos/{job.id}/download"
