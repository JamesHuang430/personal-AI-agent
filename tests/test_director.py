from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from assistant_app.api.routes.director import DirectorProjectCreatePayload
from assistant_app.core.config import Settings
from assistant_app.db.models import DirectorProject
from assistant_app.services.agent_model_router import AGENT_MODEL_PROFILES
from assistant_app.services.director import (
    _extract_storyboard_plan,
    _shot_durations,
    _shot_prompt,
    _split_agent_output,
    create_director_project,
)


class RecordingDirectorSession:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def __aenter__(self) -> "RecordingDirectorSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> "RecordingDirectorSession":
        return self

    def add(self, _item: object) -> None:
        self.events.append("add_project")

    async def flush(self) -> None:
        self.events.append("flush_project")

    def add_all(self, items: list[object]) -> None:
        self.events.append(f"add_runs:{len(items)}")


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


@pytest.mark.asyncio
async def test_director_project_is_flushed_before_agent_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def available_models(*_args: object) -> tuple[str, list[str]]:
        return "test", ["qwen3.7-max"]

    monkeypatch.setattr("assistant_app.services.director.list_available_models", available_models)
    session = RecordingDirectorSession()
    runtime = SimpleNamespace(sessions=lambda: session)

    project = await create_director_project(
        runtime,
        Settings(_env_file=None),
        uuid4(),
        "雨夜里寻找失踪信件的女孩",
    )

    assert project.title == "雨夜里寻找失踪信件的女孩"
    assert session.events == ["add_project", "flush_project", "add_runs:9"]


def test_storyboard_markdown_becomes_distinct_per_shot_plan() -> None:
    content = """
### 镜头 01：00-12s 雾林远景
正向提示词：小刺猬从画面左侧走入雾林，镜头缓慢下降。

### 镜头 02：12-24s 灯笼出现
正向提示词：南瓜灯从树后漂出，小刺猬停步抬头，镜头向前推进。

### 镜头 03：24-36s 精灵求助
正向提示词：萤火精灵飞到鼻尖，小刺猬伸出爪子，微距固定镜头。
"""

    plan = _extract_storyboard_plan(content, 3, "小刺猬帮助萤火精灵寻找晨露")

    assert [item["title"] for item in plan] == ["雾林远景", "灯笼出现", "精灵求助"]
    assert len({str(item["instruction"]) for item in plan}) == 3
    assert "漂出" in str(plan[1]["instruction"])


def test_storyboard_markdown_table_becomes_distinct_per_shot_plan() -> None:
    content = """
| 镜号 | 时间轴 | 景别 | 机位/视角 | 摄像机运动 | 画面内容 | 声音设计 | 转场方式 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 01 | 00-03s | ECU | 微距平视 | 慢推 | **钩子**：南瓜灯在雾气中晃动。 | 风声 | 淡入 |
| 02 | 03-08s | MCU | 微距平视 | 右摇 | **发现**：团团拨开蕨叶并停步抬头。 | 叶片声 | 擦除 |
| 03 | 08-15s | CU | 低角度 | 固定 | **求助**：闪闪在灯笼内指向山巅。 | 风铃声 | 硬切 |
"""

    plan = _extract_storyboard_plan(content, 3, "小刺猬帮助萤火精灵寻找晨露")

    assert [item["title"] for item in plan] == ["钩子", "发现", "求助"]
    assert len({str(item["instruction"]) for item in plan}) == 3
    assert "团团拨开蕨叶" in str(plan[1]["instruction"])


def test_duplicate_storyboard_entries_receive_unique_fallback_beats() -> None:
    content = """【分镜JSON】
[
  {"sequence": 1, "title": "重复", "action": "主角向前走"},
  {"sequence": 2, "title": "重复", "action": "主角向前走"},
  {"sequence": 3, "title": "重复", "action": "主角向前走"}
]
"""

    plan = _extract_storyboard_plan(content, 3, "一次夜间冒险")

    assert len({str(item["instruction"]) for item in plan}) == 3
    assert "一次细节互动揭示人物关系" in str(plan[1]["instruction"])
    assert "情绪回收" in str(plan[2]["instruction"])


def test_video_prompt_combines_unique_shot_with_shared_continuity() -> None:
    project = DirectorProject(
        id=uuid4(),
        user_id=uuid4(),
        title="晨露",
        premise="小刺猬帮助萤火精灵寻找晨露",
        target_seconds=30,
        aspect_ratio="9:16",
        visual_style="温暖动画",
        continuity_bible={
            "characters": [
                {"name": "团团", "appearance": "浅棕短刺", "wardrobe": "红围巾"}
            ]
        },
    )
    first = {"title": "进入雾林", "instruction": "团团从左侧走入雾林，远景慢降镜头"}
    second = {"title": "发现灯笼", "instruction": "团团停步抬头，南瓜灯从树后漂出，近景慢推"}

    first_prompt = _shot_prompt(project, 1, 3, "12", first)
    second_prompt = _shot_prompt(project, 2, 3, "12", second)

    assert first_prompt != second_prompt
    assert "进入雾林" not in second_prompt
    assert "南瓜灯从树后漂出" in second_prompt
    assert "红围巾" in first_prompt and "红围巾" in second_prompt
