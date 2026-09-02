import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from assistant_app.api.routes.director import (
    DirectorProjectCreatePayload,
    DirectorProjectRemasterPayload,
)
from assistant_app.core.config import Settings
from assistant_app.db.models import DirectorAgentRun, DirectorProject
from assistant_app.services.agent_model_router import AGENT_MODEL_PROFILES
from assistant_app.services.director import (
    DIRECTOR_PREFLIGHT_MIN_SCORE,
    DIRECTOR_SUBTITLE_FONT_SIZE,
    DirectorProjectNotResumableError,
    _dialogue_voice_filter,
    _dialogue_window,
    _director_video_size,
    _execute_agent_run,
    _extract_storyboard_plan,
    _fit_speech_text,
    _sanitize_character_voices,
    _shot_durations,
    _shot_prompt,
    _split_agent_output,
    _srt_timestamp,
    _validate_director_preflight,
    _validate_visual_data,
    create_director_project,
)


def test_resume_error_has_clear_user_facing_message() -> None:
    assert str(DirectorProjectNotResumableError("只有制作失败的项目可以继续制作")) == (
        "只有制作失败的项目可以继续制作"
    )


def test_remaster_defaults_to_explicit_chinese_female_voice() -> None:
    assert DirectorProjectRemasterPayload().voice_id == "edge:zh-CN-XiaoxiaoNeural"


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
    assert short.resolution == "768P"
    assert long.target_seconds == 300
    assert long.visual_style == "温暖动画"
    assert long.one_click is False
    assert short.story_confirmed is False

    high_resolution = DirectorProjectCreatePayload(
        premise="一只狐狸穿过失去星光的森林",
        resolution="2K",
    )
    assert high_resolution.resolution == "2K"

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
    with pytest.raises(ValidationError):
        DirectorProjectCreatePayload(premise="雨夜公交上的一次错过", resolution="4K")


def test_director_resolution_maps_to_orientation_aware_video_size() -> None:
    assert _director_video_size("9:16", "768P") == "720x1280"
    assert _director_video_size("16:9", "768P") == "1280x720"
    assert _director_video_size("9:16", "2K") == "1024x1792"
    assert _director_video_size("16:9", "2K") == "1792x1024"


def test_director_team_has_four_executing_agents() -> None:
    assert [profile.key for profile in AGENT_MODEL_PROFILES] == [
        "story",
        "visual",
        "media",
        "quality",
    ]
    assert AGENT_MODEL_PROFILES[2].executor == "tool"
    assert AGENT_MODEL_PROFILES[3].executor == "tool"


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
    assert project.resolution == "768P"
    assert project.status == "awaiting_confirmation"
    assert project.current_stage == "story_confirmation"
    assert session.events == ["add_project", "flush_project", "add_runs:4"]


@pytest.mark.asyncio
async def test_agent_run_keeps_detached_visual_result_in_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid4()
    user_id = uuid4()
    project = DirectorProject(
        id=project_id,
        user_id=user_id,
        title="晨露",
        premise="小刺猬帮助萤火精灵寻找晨露",
        target_seconds=30,
        aspect_ratio="9:16",
        visual_style="温暖动画",
        one_click=False,
    )
    run = DirectorAgentRun(
        id=uuid4(),
        project_id=project_id,
        user_id=user_id,
        agent_key="visual",
        agent_name="视觉 Agent",
        sequence=2,
        model_name="qwen3.7-max",
        status="pending",
        result_data={},
    )
    visual = {
        "continuity": {
            "characters": [
                {"name": "团团", "appearance": "浅棕短刺", "wardrobe": "红围巾"}
            ]
        },
        "shots": [
            {
                "sequence": 1,
                "title": "发现晨露",
                "action": "团团拨开叶片",
                "speech_text": "找到了。",
            }
        ],
    }
    content = (
        "【判断摘要】连续性和镜头均可执行。\n"
        "【交付物】视觉方案。\n"
        f"【视觉JSON】{json.dumps(visual, ensure_ascii=False)}"
    )
    persisted: dict[str, object] = {}

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def completed_context(*_args: object) -> str:
        return ""

    async def completion(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"content": content}

    async def update_run(
        _runtime: object, _run_id: object, **values: object
    ) -> None:
        persisted.update(values)

    monkeypatch.setattr("assistant_app.services.director._update_project", no_op)
    monkeypatch.setattr("assistant_app.services.director._update_run", update_run)
    monkeypatch.setattr(
        "assistant_app.services.director._completed_context", completed_context
    )
    monkeypatch.setattr("assistant_app.services.director.agent_text_completion", completion)

    await _execute_agent_run(
        SimpleNamespace(), Settings(_env_file=None), project, run, progress=25
    )

    assert persisted["result_data"] == run.result_data
    assert run.status == "completed"
    assert run.error_message is None
    assert run.result_data["continuity"]["characters"][0]["name"] == "团团"
    assert len(run.result_data["shots"]) == 1


def test_director_preflight_requires_revised_visual_and_score_gate() -> None:
    project = DirectorProject(
        id=uuid4(),
        user_id=uuid4(),
        title="晨露",
        premise="小刺猬帮助萤火精灵寻找晨露",
        target_seconds=30,
        aspect_ratio="9:16",
        visual_style="温暖动画",
    )
    visual = {
        "continuity": {
            "characters": [{"name": "团团", "appearance": "浅棕短刺", "wardrobe": "红围巾"}]
        },
        "shots": [
            {
                "sequence": 1,
                "title": "发现晨露",
                "action": "团团拨开叶片，晨露反射第一缕阳光",
                "camera": "低机位缓慢推近",
                "speech_text": "闪闪，我们找到了。",
            }
        ],
    }

    rejected = _validate_director_preflight(
        {
            "approved": True,
            "score": DIRECTOR_PREFLIGHT_MIN_SCORE - 1,
            "verdict": "仍有动作因果需要收敛",
            "removed_irrelevant": ["无关的城市背景"],
            "risks": ["结束构图不明确"],
            "revised_visual": visual,
        },
        project,
        ["4"],
    )
    approved = _validate_director_preflight(
        {
            "approved": True,
            "score": DIRECTOR_PREFLIGHT_MIN_SCORE,
            "verdict": "单镜微节拍、动作因果和结束构图均可执行",
            "removed_irrelevant": ["无关的城市背景"],
            "risks": [],
            "revised_visual": visual,
        },
        project,
        ["4"],
    )

    assert rejected["approved"] is False
    assert approved["approved"] is True
    assert approved["revised_visual"]["shots"][0]["speech_text"] == "闪闪，我们找到了。"


def test_visual_plan_requires_spoken_text_and_keeps_subtitles_in_sync() -> None:
    project = DirectorProject(
        id=uuid4(),
        user_id=uuid4(),
        title="晨露",
        premise="小刺猬帮助萤火精灵寻找晨露",
        target_seconds=30,
        aspect_ratio="9:16",
        visual_style="温暖动画",
    )
    data = {
        "continuity": {
            "characters": [{"name": "团团", "appearance": "浅棕短刺", "wardrobe": "红围巾"}]
        },
        "shots": [
            {
                "sequence": 1,
                "title": "走入雾林",
                "action": "团团走入雾林",
                "positive_prompt": "远景慢降",
                "speech_text": "闪闪，别怕，我会找到晨露。",
                "subtitle_text": "不一致的旧字幕",
            }
        ],
    }

    result = _validate_visual_data(data, project, ["4"])

    shot = result["shots"][0]
    assert shot["speech_text"] == shot["subtitle_text"]
    assert len(str(shot["speech_text"])) <= 16


def test_visual_plan_uses_stable_role_voice_and_scene_emotion() -> None:
    project = DirectorProject(
        id=uuid4(),
        user_id=uuid4(),
        title="重逢",
        premise="祖孙二人在车站重逢",
        target_seconds=30,
        aspect_ratio="9:16",
        visual_style="写实",
    )
    data = {
        "continuity": {
            "characters": [
                {
                    "name": "陈爷爷",
                    "role": "七十岁的老人",
                    "voice_profile": "低沉温和",
                    "voice_id": "elder_warm_01",
                }
            ]
        },
        "shots": [
            {
                "sequence": 1,
                "title": "认出孙女",
                "action": "陈爷爷惊讶地抬头",
                "speaker": "陈爷爷",
                "speech_text": "真的是你吗？",
                "emotion": "surprised",
            }
        ],
    }

    result = _validate_visual_data(data, project, ["4"])

    character = result["continuity"]["characters"][0]
    shot = result["shots"][0]
    assert character["voice_role"] == "elder_male"
    assert "voice_id" not in character
    assert shot["emotion"] == "surprised"


def test_only_user_locked_voice_ids_survive_continuity_sanitizing() -> None:
    characters = [
        {"name": "林夏", "role": "成年女性", "voice_id": "verified_voice_01"},
        {"name": "程野", "role": "成年男性", "voice_id": "invented_voice_02"},
    ]

    _sanitize_character_voices(characters, "林夏固定 voice_id=verified_voice_01；不得更换")

    assert characters[0]["voice_id"] == "verified_voice_01"
    assert characters[0]["voice_role"] == "adult_female"
    assert "voice_id" not in characters[1]
    assert characters[1]["voice_role"] == "adult_male"


def test_speech_and_subtitle_helpers_fit_media_duration() -> None:
    assert _fit_speech_text("  我们 一起 去找 晨露。  ", "4") == "我们一起去找晨露。"
    assert len(_fit_speech_text("这是一句明显超过四秒容量需要被截短的对白", "4")) <= 16
    assert _srt_timestamp(12.345) == "00:00:12,345"
    assert DIRECTOR_SUBTITLE_FONT_SIZE == 9
    audio_filter = _dialogue_voice_filter(10.0)
    assert "[1:a]" in audio_filter
    assert "amix" not in audio_filter
    assert "[0:a]" not in audio_filter


def test_dialogue_window_is_planned_and_clamped_to_the_shot() -> None:
    assert _dialogue_window(
        {"speech_text": "开始。", "dialogue_start_seconds": 0, "dialogue_end_seconds": 1},
        4.0,
    ) == (0.0, 1.0)
    assert _dialogue_window(
        {
            "speech_text": "你终于来了。",
            "dialogue_start_seconds": 1.25,
            "dialogue_end_seconds": 3.5,
        },
        4.0,
    ) == (1.25, 3.5)
    start, end = _dialogue_window(
        {
            "speech_text": "这句对白很长但仍然不能跑到镜头外。",
            "dialogue_start_seconds": 99,
            "dialogue_end_seconds": 120,
        },
        4.0,
    )
    assert 0 <= start < end <= 4.0


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
    second = {
        "title": "发现灯笼",
        "instruction": "团团停步抬头，南瓜灯从树后漂出，近景慢推",
        "speaker": "团团",
        "speech_text": "闪闪，你在哪里？",
    }

    first_prompt = _shot_prompt(project, 1, 3, "12", first)
    second_prompt = _shot_prompt(project, 2, 3, "12", second)

    assert first_prompt != second_prompt
    assert "进入雾林" not in second_prompt
    assert "南瓜灯从树后漂出" in second_prompt
    assert "准确说出且只说台词" in second_prompt
    assert "闪闪，你在哪里" in second_prompt
    assert "只允许上述一句可辨识人声" in second_prompt
    assert "对白出现时音乐自动降低" in second_prompt
    assert "系统将按上述对白时间窗" in second_prompt
    assert "红围巾" in first_prompt and "红围巾" in second_prompt
