import pytest
from pydantic import ValidationError

from assistant_app.api.routes.director import DirectorProjectCreatePayload
from assistant_app.services.agent_model_router import AGENT_MODEL_PROFILES
from assistant_app.services.director import _shot_durations, _split_agent_output


def test_director_project_payload_supports_short_and_five_minute_projects() -> None:
    short = DirectorProjectCreatePayload(premise="雨夜公交上的一次错过")
    long = DirectorProjectCreatePayload(
        premise="一只狐狸穿过失去星光的森林",
        target_seconds=300,
        aspect_ratio="16:9",
        visual_style="温暖动画",
    )

    assert short.target_seconds == 60
    assert short.aspect_ratio == "9:16"
    assert long.target_seconds == 300
    assert long.visual_style == "温暖动画"
    assert long.one_click is False

    one_click = DirectorProjectCreatePayload(
        premise="一只狐狸穿过失去星光的森林",
        target_seconds=30,
        one_click=True,
        continuity_notes="狐狸左耳有金色耳钉，始终穿红围巾",
    )
    assert one_click.one_click is True
    assert "红围巾" in one_click.continuity_notes

    with pytest.raises(ValidationError):
        DirectorProjectCreatePayload(premise="太短", target_seconds=120)


def test_director_team_has_one_director_and_eight_gate_agents() -> None:
    assert len(AGENT_MODEL_PROFILES) == 9
    assert AGENT_MODEL_PROFILES[0].key == "director"
    assert {profile.key for profile in AGENT_MODEL_PROFILES[1:]} == {
        "concept",
        "script",
        "assets",
        "storyboard",
        "video",
        "audio",
        "edit",
        "quality",
    }


def test_director_output_separates_visible_summary_and_deliverable() -> None:
    summary, deliverable = _split_agent_output(
        "【判断摘要】核心钩子成立。\n\n【交付物】第一幕从雨夜公交站开始。"
    )

    assert summary == "核心钩子成立。"
    assert deliverable == "第一幕从雨夜公交站开始。"


def test_one_click_movie_plans_supported_clip_lengths() -> None:
    assert _shot_durations(30) == ["12", "12", "8"]
    assert _shot_durations(60) == ["12", "12", "12", "12", "12"]
    assert sum(map(int, _shot_durations(300))) == 300
