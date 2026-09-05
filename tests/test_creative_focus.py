from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from assistant_app.db.models import DirectorAgentRun, DirectorProject, MemoryItem, User
from assistant_app.services import creative_preferences as creative
from assistant_app.services import director


class Session:
    def __init__(self, project=None, rows=()):
        self.get = AsyncMock(return_value=project)
        self.scalar = AsyncMock(return_value=project)
        self.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: list(rows)))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return self


@pytest.mark.parametrize(
    "content,kind,confidence,expected",
    [
        ("视频喜欢复古胶片，配乐轻一点", "preference", 0.9, True),
        ("我住在上海", "fact", 1, False),
        ("明天天气晴", "preference", 1, False),
        ("我可能喜欢恐怖电影", "preference", 0.3, False),
        ("剧中人物是医生", "fact", 0.9, False),
        ("视频渠道 API Key 是 secret", "preference", 1, False),
    ],
)
def test_only_supported_creative_memories_are_used(content, kind, confidence, expected):
    assert (
        creative.is_creative_memory(
            {"content": content, "memory_type": kind, "confidence": confidence}
        )
        is expected
    )


@pytest.mark.asyncio
async def test_project_opt_out_does_not_retrieve_memories(monkeypatch):
    monkeypatch.setattr(
        creative,
        "get_preferences",
        AsyncMock(return_value=creative.CreativePreferences(visual_style="复古胶片")),
    )
    vectors = AsyncMock()
    monkeypatch.setattr(creative, "_retrieve_vector_memories", vectors)
    result = await creative.build_personalization(
        None, SimpleNamespace(memory_enabled=True), uuid4(), "test", use_memory=False
    )
    assert result["preferences"]["visual_style"] == "复古胶片"
    assert not result["memories"]
    vectors.assert_not_called()


@pytest.mark.asyncio
async def test_vector_outage_uses_creative_feedback_and_excludes_other_memories(monkeypatch):
    user_id = uuid4()
    user = User(id=user_id, creative_preferences={})
    rows = [
        MemoryItem(
            id=uuid4(),
            content="视频创作偏好：配乐轻一点",
            memory_type="preference",
            confidence=1,
            extra_data={"source": "director_feedback"},
        ),
        MemoryItem(id=uuid4(), content="周末去杭州", memory_type="goal", confidence=1),
    ]
    session = Session(user, rows)
    monkeypatch.setattr(creative, "_retrieve_vector_memories", AsyncMock(side_effect=TimeoutError))
    result = await creative.build_personalization(
        SimpleNamespace(sessions=lambda: session),
        SimpleNamespace(memory_enabled=True),
        user_id,
        "治愈短片",
    )
    assert len(result["memories"]) == 1
    assert result["memories"][0]["source"] == "director_feedback"
    assert result["retrieval"] == "recent_creative_preferences"


def test_current_brief_and_preferences_are_part_of_approval_digest():
    project = DirectorProject(id=uuid4(), premise="故事", visual_style="胶片", personalization={})
    first = director.storyboard_hash(project, {"shots": [{"speech_text": "你好"}]})
    changed = director.storyboard_hash(project, {"shots": [{"speech_text": "再见"}]})
    assert first != changed
    project.personalization = {"preferences": {"avoid": "恐怖"}}
    assert first != director.storyboard_hash(project, {"shots": [{"speech_text": "你好"}]})


@pytest.mark.asyncio
async def test_storyboard_gate_stops_before_any_media_submission(monkeypatch):
    project = DirectorProject(
        id=uuid4(), status="queued", review_required=True, storyboard_approved=False
    )
    runs = [
        DirectorAgentRun(id=uuid4(), agent_key=key)
        for key in ("story", "visual", "media", "quality")
    ]
    session = Session(project, runs)
    updates = AsyncMock()
    media = AsyncMock()
    monkeypatch.setattr(director, "_update_project", updates)
    monkeypatch.setattr(director, "_execute_agent_run", AsyncMock())
    monkeypatch.setattr(director, "_run_director_preflight", AsyncMock())
    monkeypatch.setattr(director, "_create_and_run_shot", media)
    await director.run_director_project(SimpleNamespace(sessions=lambda: session), None, project.id)
    assert updates.call_args.kwargs["status"] == "awaiting_storyboard"
    media.assert_not_called()


@pytest.mark.asyncio
async def test_running_project_cannot_be_overwritten():
    project = DirectorProject(id=uuid4(), status="processing", premise="original")
    session = Session(project)
    with pytest.raises(director.DirectorProjectNotApprovableError):
        await director.update_director_draft(
            SimpleNamespace(sessions=lambda: session), uuid4(), project.id, {"premise": "overwrite"}
        )
    assert project.premise == "original"


@pytest.mark.asyncio
async def test_feedback_needs_explicit_preference_before_remembering():
    with pytest.raises(ValueError, match="具体创作偏好"):
        await creative.save_feedback(
            None,
            uuid4(),
            uuid4(),
            creative.CreativeFeedback(verdict="accepted", rating=5, remember=True),
        )


@pytest.mark.asyncio
async def test_rendered_shot_download_uses_subtitled_file_and_rejects_outside_root(
    monkeypatch, tmp_path
):
    from fastapi import HTTPException

    from assistant_app.api.routes import director as routes
    from assistant_app.db.models import DirectorShot

    rendered = tmp_path / "subtitled.mp4"
    rendered.write_bytes(b"rendered")
    shot = DirectorShot(
        id=uuid4(),
        project_id=uuid4(),
        user_id=uuid4(),
        status="completed",
        rendered_path=str(rendered),
    )
    session = Session(shot)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(runtime=SimpleNamespace(sessions=lambda: session))
        )
    )
    monkeypatch.setattr(routes, "_owned_project", AsyncMock())
    monkeypatch.setattr(routes, "GENERATED_ROOT", tmp_path)
    response = await routes._rendered_shot_response(
        shot.project_id, shot.id, request, SimpleNamespace(id=shot.user_id), download=True
    )
    assert response.path == rendered
    assert response.filename.endswith(".mp4")
    monkeypatch.setattr(routes, "GENERATED_ROOT", tmp_path / "different-root")
    with pytest.raises(HTTPException) as failure:
        await routes._rendered_shot_response(
            shot.project_id, shot.id, request, SimpleNamespace(id=shot.user_id)
        )
    assert failure.value.status_code == 404
    monkeypatch.setattr(routes, "_owned_project", AsyncMock(side_effect=HTTPException(404)))
    with pytest.raises(HTTPException):
        await routes._rendered_shot_response(
            shot.project_id, shot.id, request, SimpleNamespace(id=uuid4())
        )
