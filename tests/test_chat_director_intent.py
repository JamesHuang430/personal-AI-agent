import pytest

from assistant_app.api.routes.chat import (
    director_full_production_requested,
    video_generation_confirmed,
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


@pytest.mark.parametrize(
    "message",
    [
        "提示词已确认，立即生成",
        "我确认现在生成这个视频",
        "分镜确认通过，请立即提交视频生成",
    ],
)
def test_expensive_direct_video_requires_explicit_confirmation(message: str) -> None:
    assert video_generation_confirmed(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "帮我生成一个视频",
        "先看看提示词",
        "做一个四秒镜头",
    ],
)
def test_initial_video_idea_is_not_treated_as_confirmation(message: str) -> None:
    assert video_generation_confirmed(message) is False
