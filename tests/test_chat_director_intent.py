import pytest

from assistant_app.api.routes.chat import (
    director_full_production_requested,
)


@pytest.mark.parametrize(
    "message",
    [
        "请生成一个30秒的视频，要多镜头且连贯",
        "请启动导演工作室，制作一部有对白的60秒短剧",
        "最终生成一步时长约5分钟的小电影",
        "请一键成片",
        "视频共计 30 秒，一定要有趣",
    ],
)
def test_explicit_full_video_request_runs_full_director_production(message: str) -> None:
    assert director_full_production_requested(message, {"one_click": False}) is True


@pytest.mark.parametrize(
    "message",
    [
        "请先生成一个预览镜头，我确认后再继续",
        "只生成首个测试镜头",
        "先逐镜确认，不要直接合片",
    ],
)
def test_preview_request_does_not_run_full_production(message: str) -> None:
    assert director_full_production_requested(message, {"one_click": False}) is False


def test_explicit_tool_argument_still_enables_full_production() -> None:
    assert director_full_production_requested("先做规划", {"one_click": True}) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "提示词已确认，立即生成",
        "我还没确认生成，请不要提交视频任务",
        "不要现在提交视频生成",
    ],
)
async def test_chat_video_tool_always_creates_a_draft(monkeypatch, message):
    from types import SimpleNamespace

    from assistant_app.services import chat_tools

    calls = []

    async def create(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id="draft")

    monkeypatch.setattr(chat_tools, "create_video_job", create)
    monkeypatch.setattr(chat_tools, "video_job_payload", lambda job: {"id": job.id})
    await chat_tools.video_tool(None, None, None, chat_tools.VideoArguments(prompt=message))
    assert calls[0]["awaiting_confirmation"] is True
