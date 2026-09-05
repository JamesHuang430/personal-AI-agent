"""Opt-in DB checks: TEST_POSTGRES_URL must point to an isolated test server.

Each test creates/drops its own random schema. Also runnable against local PGlite;
that validates PostgreSQL SQL/ORM behavior but does not replace multi-worker load tests.
"""

from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from assistant_app.api.routes.videos import VideoConfirmation, confirm_video
from assistant_app.db.base import Base
from assistant_app.db.models import ChatRun, User, VideoChannel, VideoJob, WorkItem
from assistant_app.services import work_queue
from assistant_app.services.chat_runs import reserve_chat_run, run_chat_request
from assistant_app.services.conversations import (
    get_conversation_messages,
    prepare_conversation,
    record_assistant_message,
)
from assistant_app.services.video_gateway import create_video_job, video_draft_hash

pytestmark = pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="No test PostgreSQL URL")


@pytest_asyncio.fixture
async def db():
    schema = "reliability_" + uuid4().hex
    engine = create_async_engine(
        os.environ["TEST_POSTGRES_URL"],
        pool_size=1,
        max_overflow=0,
        pool_timeout=10,
        connect_args={"command_timeout": 15, "timeout": 15},
    )
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET search_path TO "{schema}"'))
        await connection.run_sync(
            lambda c: Base.metadata.create_all(
                c,
                tables=[t for t in Base.metadata.sorted_tables if t.name != "memory_embeddings"],
            )
        )
    runtime = SimpleNamespace(sessions=async_sessionmaker(engine, expire_on_commit=False))
    try:
        yield runtime, engine
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def seed(runtime):
    user = User(id=uuid4(), email=f"{uuid4()}@example.test", password_hash="dummy")
    channel = VideoChannel(
        id=uuid4(),
        name="test",
        base_url="https://example.test",
        model_name="test",
        encrypted_api_key="dummy",
        is_active=True,
    )
    async with runtime.sessions() as session, session.begin():
        session.add_all([user, channel])
    return user


@pytest.mark.asyncio
async def test_queue_claim_recovery_and_fencing_on_postgresql(db):
    runtime, _ = db
    user = await seed(runtime)
    job = await create_video_job(runtime, user.id, "test")
    first = await work_queue.claim(runtime)
    assert first.resource_id == job.id
    assert await work_queue.claim(runtime) is None
    async with runtime.sessions() as session, session.begin():
        row = await session.get(WorkItem, first.id)
        row.lease_until = datetime.now(UTC) - timedelta(minutes=1)
    second = await work_queue.claim(runtime)
    assert second.owner != first.owner
    assert second.attempts == 2
    assert await work_queue.renew(runtime, first) is False
    await work_queue.finish(runtime, first)
    async with runtime.sessions() as session:
        row = await session.get(WorkItem, first.id)
        assert row.status == "processing"
    await work_queue.finish(runtime, second)
    assert await work_queue.claim(runtime) is None


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_business_job(db, monkeypatch):
    from unittest.mock import AsyncMock

    from assistant_app.services import video_gateway

    runtime, _ = db
    user = await seed(runtime)
    monkeypatch.setattr(video_gateway, "enqueue", AsyncMock(side_effect=RuntimeError("queue")))
    with pytest.raises(RuntimeError, match="queue"):
        await create_video_job(runtime, user.id, "must roll back")
    async with runtime.sessions() as session:
        assert not (await session.scalars(select(VideoJob))).all()


@pytest.mark.asyncio
async def test_video_confirmation_is_owned_parameter_bound_and_idempotent(db):
    from fastapi import HTTPException

    runtime, _ = db
    user = await seed(runtime)
    job = await create_video_job(runtime, user.id, "review me", awaiting_confirmation=True)
    assert await work_queue.claim(runtime) is None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    with pytest.raises(HTTPException) as failure:
        await confirm_video(job.id, VideoConfirmation(draft_hash="wrong"), request, user)
    assert failure.value.status_code == 409
    with pytest.raises(HTTPException) as failure:
        await confirm_video(
            job.id,
            VideoConfirmation(draft_hash=video_draft_hash(job)),
            request,
            SimpleNamespace(id=uuid4()),
        )
    assert failure.value.status_code == 404
    payload = VideoConfirmation(draft_hash=video_draft_hash(job))
    await confirm_video(job.id, payload, request, user)
    await confirm_video(job.id, payload, request, user)
    first = await work_queue.claim(runtime)
    assert first.resource_id == job.id
    assert await work_queue.claim(runtime) is None


@pytest.mark.asyncio
async def test_chat_replay_and_message_artifacts_survive_new_sessions(db):
    runtime, _ = db
    user = await seed(runtime)
    calls = []

    async def execute(run):
        calls.append(run.id)
        prepared = await prepare_conversation(runtime, user.id, None, "hello", "test")
        await record_assistant_message(
            runtime,
            user.id,
            prepared.conversation.id,
            "answer",
            "test",
            "test",
            {},
            artifacts={"web_sources": [{"url": "https://example.test"}]},
        )
        return {"content": "answer", "conversation_id": str(prepared.conversation.id)}

    result = await run_chat_request(runtime, user.id, "request-1", {"message": "hello"}, execute)
    replay = await run_chat_request(runtime, user.id, "request-1", {"message": "hello"}, execute)
    assert replay == result
    assert len(calls) == 1
    from uuid import UUID

    history = await get_conversation_messages(runtime, user.id, UUID(result["conversation_id"]))
    assert history["messages"][1]["artifacts"]["web_sources"][0]["url"] == "https://example.test"


@pytest.mark.asyncio
async def test_changed_idempotency_payload_and_active_request_are_rejected(db):
    from fastapi import HTTPException

    runtime, _ = db
    user = await seed(runtime)
    await reserve_chat_run(runtime, user.id, "request-1", {"message": "a"})
    for key, message in [("request-1", "b"), ("request-2", "a")]:
        with pytest.raises(HTTPException) as error:
            await reserve_chat_run(runtime, user.id, key, {"message": message})
        assert error.value.status_code == 409
    async with runtime.sessions() as session:
        assert len((await session.scalars(select(ChatRun))).all()) == 1


@pytest.mark.asyncio
async def test_new_migration_upgrade_and_downgrade(db):
    _, engine = db
    migration = importlib.import_module("migrations.versions.20260905_0017_reliable_execution")
    async with engine.begin() as connection:

        def migrate(c):
            # Reconstruct the previous application tables without the new columns/tables.
            Base.metadata.drop_all(
                c, tables=[t for t in Base.metadata.sorted_tables if t.name != "memory_embeddings"]
            )
            previous = MetaData()
            for table in Base.metadata.sorted_tables:
                if table.name in {"work_items", "chat_runs", "memory_embeddings"}:
                    continue
                clone = table.to_metadata(previous)
                for column in ("artifacts", "submission_started_at"):
                    if column in clone.c:
                        clone._columns.remove(clone.c[column])
            previous.create_all(c)
            with Operations.context(MigrationContext.configure(c)):
                migration.upgrade()
                assert c.execute(text("SELECT count(*) FROM work_items")).scalar() == 0
                migration.downgrade()
                migration.upgrade()

        await connection.run_sync(migrate)


@pytest.mark.asyncio
async def test_creative_preferences_feedback_review_and_user_isolation(db, monkeypatch):
    from unittest.mock import AsyncMock

    from assistant_app.core.config import Settings
    from assistant_app.db.models import DirectorAgentRun, DirectorProject, MemoryItem
    from assistant_app.services import creative_preferences as creative
    from assistant_app.services import director

    runtime, _ = db
    user = await seed(runtime)
    other = await seed(runtime)
    settings = Settings(_env_file=None, memory_enabled=True)
    monkeypatch.setattr(
        director, "list_available_models", AsyncMock(return_value=("test", ["qwen3.7-max"]))
    )
    monkeypatch.setattr(creative, "_retrieve_vector_memories", AsyncMock(return_value=[]))
    await creative.save_preferences(
        runtime, user.id, creative.CreativePreferences(visual_style="复古胶片")
    )
    project = await director.create_director_project(runtime, settings, user.id, "雨天的治愈故事")
    assert project.visual_style == "复古胶片"
    assert project.review_required
    assert (await creative.get_preferences(runtime, other.id)).visual_style == ""
    with pytest.raises(director.DirectorProjectNotFoundError):
        await director.update_director_draft(runtime, other.id, project.id, {"premise": "changed"})
    await director.update_director_draft(
        runtime, user.id, project.id, {"premise": "另一个温暖故事"}
    )
    await creative.save_preferences(
        runtime, user.id, creative.CreativePreferences(visual_style="水彩动画")
    )
    # Profile edits do not rewrite an existing project's creative basis.
    async with runtime.sessions() as session, session.begin():
        record = await session.get(DirectorProject, project.id)
        assert record.personalization["preferences"]["visual_style"] == "复古胶片"
        record.status = "awaiting_storyboard"
        visual = await session.scalar(
            select(DirectorAgentRun).where(
                DirectorAgentRun.project_id == project.id, DirectorAgentRun.agent_key == "visual"
            )
        )
        visual.result_data = {
            "shots": [{"speech_text": "你好"}],
            "director_preflight": {"passed": True},
        }
        digest = director.storyboard_hash(record, visual.result_data)
    with pytest.raises(director.DirectorProjectNotApprovableError):
        await director.approve_storyboard(runtime, user.id, project.id, "0" * 64)
    with pytest.raises(director.DirectorProjectNotFoundError):
        await director.approve_storyboard(runtime, other.id, project.id, digest)
    await director.approve_storyboard(runtime, user.id, project.id, digest)
    await director.approve_storyboard(runtime, user.id, project.id, digest)
    assert (await work_queue.claim(runtime)).resource_id == project.id
    assert await work_queue.claim(runtime) is None
    async with runtime.sessions() as session, session.begin():
        record = await session.get(DirectorProject, project.id)
        record.status = "completed"
    feedback = creative.CreativeFeedback(verdict="accepted", rating=5, notes="剧情很棒")
    await creative.save_feedback(runtime, user.id, project.id, feedback)
    async with runtime.sessions() as session:
        assert not (await session.scalars(select(MemoryItem))).all()
    remembered = feedback.model_copy(
        update={"remember": True, "reusable_preference": "对白停顿长一些"}
    )
    await creative.save_feedback(runtime, user.id, project.id, remembered)
    await creative.save_feedback(runtime, user.id, project.id, remembered)
    next_project = await director.create_director_project(
        runtime, settings, user.id, "秋天的重逢故事"
    )
    assert next_project.visual_style == "水彩动画"
    assert len(next_project.personalization["memories"]) == 1
    isolated = await creative.build_personalization(runtime, settings, other.id, "重逢")
    assert not isolated["memories"]
    await creative.save_feedback(runtime, user.id, project.id, feedback)
    withdrawn = await creative.build_personalization(runtime, settings, user.id, "重逢")
    assert not withdrawn["memories"]
    with pytest.raises(LookupError):
        await creative.save_feedback(runtime, other.id, project.id, feedback)


@pytest.mark.asyncio
async def test_creative_migration_preserves_legacy_execution_policy(db):
    _, engine = db
    migration = importlib.import_module("migrations.versions.20260905_0018_creative_focus")
    async with engine.begin() as connection:

        def migrate(c):
            with Operations.context(MigrationContext.configure(c)):
                migration.downgrade()
                migration.upgrade()
                columns = (
                    c.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = current_schema() "
                            "AND table_name = 'director_projects'"
                        )
                    )
                    .scalars()
                    .all()
                )
                assert "personalization" in columns
                assert "review_required" in columns

        await connection.run_sync(migrate)


@pytest.mark.asyncio
async def test_legacy_status_constraint_upgrade_and_safe_downgrade(db):
    runtime, engine = db
    user = await seed(runtime)
    from assistant_app.db.models import DirectorProject

    project = DirectorProject(
        user_id=user.id, title="test", premise="test", visual_style="natural", status="queued"
    )
    async with runtime.sessions() as session, session.begin():
        session.add(project)
    migration = importlib.import_module("migrations.versions.20260905_0019_storyboard_status")
    async with engine.begin() as connection:

        def migrate(c):
            with Operations.context(MigrationContext.configure(c)):
                migration.downgrade()  # Restore the actual legacy CHECK, not only ORM columns.
                migration.upgrade()
                c.execute(text("UPDATE director_projects SET status='awaiting_storyboard'"))
                migration.downgrade()
                assert c.execute(text("SELECT status FROM director_projects")).scalar() == (
                    "awaiting_confirmation"
                )
                migration.upgrade()
                c.execute(text("UPDATE director_projects SET status='awaiting_storyboard'"))

        await connection.run_sync(migrate)
