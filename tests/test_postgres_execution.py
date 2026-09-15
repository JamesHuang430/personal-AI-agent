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
async def test_director_sound_audition_is_owned_idempotent_and_reused(db, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from assistant_app.api.routes import director_audio as routes
    from assistant_app.db.models import DirectorAgentRun, DirectorProject, DirectorShot, SpeechJob
    from assistant_app.services import director
    from assistant_app.services.director_audio import AudioSettings, director_speech

    runtime, _ = db
    runtime.redis = SimpleNamespace(incr=AsyncMock(return_value=1), expire=AsyncMock())
    user = await seed(runtime)
    other = await seed(runtime)
    project = DirectorProject(
        id=uuid4(),
        user_id=user.id,
        title="声音测试",
        premise="太阳出来了",
        visual_style="白板",
        production_mode="whiteboard",
        target_seconds=4,
        planned_shots=1,
        status="awaiting_storyboard",
        current_stage="storyboard_review",
        review_required=True,
    )
    visual = DirectorAgentRun(
        id=uuid4(),
        project_id=project.id,
        user_id=user.id,
        agent_key="visual",
        agent_name="visual",
        sequence=1,
        model_name="fixture",
        status="completed",
        result_data={"shots": [{"title": "太阳", "speech_text": "太阳出来了。"}], "continuity": {}},
    )
    async with runtime.sessions() as session, session.begin():
        session.add(project)
        await session.flush()
        session.add(visual)
    monkeypatch.setattr(director, "read_activity", AsyncMock(return_value=[]))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    original = director.storyboard_hash(project, visual.result_data)
    settings = AudioSettings(
        voice_mode="edge", voice_id="edge:zh-CN-XiaoxiaoNeural", subtitle_style="panel"
    )
    body = routes.SoundUpdate(settings=settings, storyboard_hash=original)
    with pytest.raises(HTTPException) as forbidden:
        await routes.save_sound(project.id, body, request, other)
    assert forbidden.value.status_code == 404
    saved = await routes.save_sound(project.id, body, request, user)
    assert saved["storyboard_hash"] != original
    with pytest.raises(HTTPException) as stale:
        await routes.save_sound(project.id, body, request, user)
    assert stale.value.status_code == 409
    payload = routes.AuditionRequest(storyboard_hash=saved["storyboard_hash"])
    first = await routes.audition(project.id, 1, payload, request, user)
    second = await routes.audition(project.id, 1, payload, request, user)
    assert first["id"] == second["id"]
    with pytest.raises(director.DirectorProjectNotApprovableError, match="试听"):
        await director.approve_storyboard(runtime, user.id, project.id, saved["storyboard_hash"])
    from uuid import UUID

    job_id = UUID(first["id"])
    async with runtime.sessions() as session, session.begin():
        job = await session.get(SpeechJob, job_id)
        assert job.channel_id is None
        job.status = "completed"
        job.storage_path = str(tmp_path / "audition.wav")
        jobs = list((await session.scalars(select(SpeechJob))).all())
        assert len(jobs) == 1
        shot = DirectorShot(
            id=uuid4(),
            project_id=project.id,
            user_id=user.id,
            sequence=1,
            title="太阳",
            prompt="sun",
            seconds="4",
            speech_text="太阳出来了。",
            status="pending",
        )
        session.add(shot)
    # Returning to a previous voice/settings variant reuses its original paid identity.
    different = settings.model_copy(update={"voice_id": "edge:zh-CN-YunxiNeural"})
    changed = await routes.save_sound(
        project.id,
        routes.SoundUpdate(settings=different, storyboard_hash=saved["storyboard_hash"]),
        request,
        user,
    )
    alternate = await routes.audition(
        project.id,
        1,
        routes.AuditionRequest(storyboard_hash=changed["storyboard_hash"]),
        request,
        user,
    )
    assert alternate["id"] != first["id"]
    restored = await routes.save_sound(
        project.id,
        routes.SoundUpdate(settings=settings, storyboard_hash=changed["storyboard_hash"]),
        request,
        user,
    )
    again = await routes.audition(
        project.id,
        1,
        routes.AuditionRequest(storyboard_hash=restored["storyboard_hash"]),
        request,
        user,
    )
    assert again["id"] == first["id"]
    reused = await director_speech(runtime, project, shot, visual.result_data["shots"][0])
    assert reused == job_id
    with pytest.raises(HTTPException, match="配音已用于"):
        await routes.save_sound(
            project.id,
            routes.SoundUpdate(settings=settings, storyboard_hash=saved["storyboard_hash"]),
            request,
            user,
        )


@pytest.mark.asyncio
async def test_director_audio_migration_preserves_keyless_jobs(db):
    runtime, engine = db
    migration = importlib.import_module("migrations.versions.20260915_0021_director_audio")
    async with engine.begin() as connection:

        def migrate(c):
            with Operations.context(MigrationContext.configure(c)):
                migration.downgrade()
                migration.upgrade()

        await connection.run_sync(migrate)
    user = await seed(runtime)
    from assistant_app.db.models import SpeechJob

    async with runtime.sessions() as session, session.begin():
        session.add(
            SpeechJob(
                user_id=user.id,
                channel_id=None,
                speech_text="太阳",
                voice_id="edge:test",
                audio_format="mp3",
                status="completed",
            )
        )
    async with engine.begin() as connection:

        def refuse(c):
            with Operations.context(MigrationContext.configure(c)):
                with pytest.raises(RuntimeError, match="不能无损回退"):
                    migration.downgrade()

        await connection.run_sync(refuse)
        assert (await connection.execute(text("SELECT count(*) FROM speech_jobs"))).scalar() == 1


@pytest.mark.asyncio
async def test_director_music_real_mix_ducking_and_ownership(db, tmp_path):
    import hashlib

    import numpy as np

    from assistant_app.db.models import MusicChannel, MusicJob
    from assistant_app.services.director_audio import (
        AudioSettings,
        mix_project_music,
        validate_audio_assets,
    )
    from assistant_app.services.director_media import _probe_media, _run_media_command

    runtime, _ = db
    user = await seed(runtime)
    channel = MusicChannel(
        id=uuid4(),
        name="test",
        base_url="https://invalid.test",
        model_name="test",
        encrypted_api_key="unused",
    )
    music_path = tmp_path / "music.wav"
    original = tmp_path / "source.mp4"
    await _run_media_command(
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", str(music_path)
    )
    await _run_media_command(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=white:s=320x180:d=4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=4",
        "-af",
        "volume='if(between(t,1,2),2,0)':eval=frame",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        str(original),
    )
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    music = MusicJob(
        id=uuid4(),
        user_id=user.id,
        channel_id=channel.id,
        prompt="fixture",
        audio_format="wav",
        status="completed",
        storage_path=str(music_path),
    )
    async with runtime.sessions() as session, session.begin():
        session.add(channel)
        await session.flush()
        session.add(music)
    config = AudioSettings(bgm_job_id=music.id, bgm_volume=0.4)
    async with runtime.sessions() as session:
        with pytest.raises(ValueError, match="所选音乐不可用"):
            await validate_audio_assets(session, uuid4(), config)
    project = SimpleNamespace(user_id=user.id, postproduction=config.model_dump(mode="json"))
    waveforms = []
    for ducking in (False, True):
        project.postproduction["ducking"] = ducking
        output = tmp_path / f"mixed-{ducking}.mp4"
        await mix_project_music(runtime, project, original, output, 4)
        info = await _probe_media(output)
        assert {s["codec_type"] for s in info["streams"]} == {"audio", "video"}
        assert abs(float(info["format"]["duration"]) - 4) < 0.2
        pcm = tmp_path / f"{ducking}.f32"
        await _run_media_command(
            "ffmpeg",
            "-y",
            "-i",
            str(output),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            str(pcm),
        )
        waveforms.append(np.fromfile(pcm, dtype="float32"))

    def music_amplitude(samples):
        segment = samples[round(1.3 * 48000) : round(1.8 * 48000)]
        t = np.arange(len(segment)) / 48000
        return abs(np.sum(segment * np.exp(-2j * np.pi * 440 * t))) / len(segment)

    assert music_amplitude(waveforms[1]) < music_amplitude(waveforms[0]) * 0.7
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest


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


@pytest.mark.asyncio
async def test_whiteboard_migration_preserves_existing_project_mode(db):
    runtime, engine = db
    user = await seed(runtime)
    from assistant_app.db.models import DirectorProject

    async with runtime.sessions() as session, session.begin():
        session.add(
            DirectorProject(user_id=user.id, title="old", premise="legacy", visual_style="natural")
        )
    migration = importlib.import_module("migrations.versions.20260909_0020_whiteboard")
    async with engine.begin() as connection:

        def migrate(c):
            with Operations.context(MigrationContext.configure(c)):
                migration.downgrade()
                migration.upgrade()
                assert (
                    c.execute(text("SELECT production_mode FROM director_projects")).scalar()
                    == "video"
                )

        await connection.run_sync(migrate)


@pytest.mark.asyncio
@pytest.mark.parametrize("edge", [False, True])
async def test_whiteboard_upload_ownership_and_real_media_pipeline(db, tmp_path, monkeypatch, edge):
    import io
    import wave
    from unittest.mock import AsyncMock

    from fastapi import HTTPException, UploadFile
    from PIL import Image, ImageDraw

    from assistant_app.api.routes import director as routes
    from assistant_app.db.models import (
        DirectorAgentRun,
        DirectorProject,
        DirectorShot,
        SpeechChannel,
        SpeechJob,
    )
    from assistant_app.services import director, director_media, image_gateway, whiteboard

    runtime, _ = db
    user = await seed(runtime)
    project = DirectorProject(
        id=uuid4(),
        user_id=user.id,
        title="白板测试",
        postproduction={"voice_mode": "edge", "voice_id": "edge:zh-CN-XiaoxiaoNeural"}
        if edge
        else {},
        premise="介绍太阳与树木",
        visual_style="白板",
        production_mode="whiteboard",
        target_seconds=4,
        one_click=True,
        planned_shots=1,
        status="awaiting_storyboard",
        review_required=True,
        storyboard_approved=False,
    )
    runs = [
        DirectorAgentRun(
            id=uuid4(),
            project_id=project.id,
            user_id=user.id,
            agent_key=key,
            agent_name=key,
            sequence=index,
            model_name="test",
            status="completed",
        )
        for index, key in enumerate(["story", "visual", "media", "quality"])
    ]
    runs[1].result_data = {
        "shots": [
            {"title": "阳光", "speech_text": "阳光照耀着树木。", "positive_prompt": "sun and tree"}
        ],
        "continuity": {},
    }
    async with runtime.sessions() as session, session.begin():
        session.add(project)
        await session.flush()
        session.add_all(runs)
        session.add(
            SpeechChannel(
                name="mock",
                base_url="https://invalid.test",
                model_name="mock",
                default_voice_id="mock",
                encrypted_api_key="mock",
                is_active=True,
            )
        )
    for module in (routes, director_media, whiteboard, image_gateway):
        monkeypatch.setattr(module, "GENERATED_ROOT", tmp_path)
    monkeypatch.setattr(director, "read_activity", AsyncMock(return_value=[]))
    # Only model-independent fixture artwork and synthetic audio are used by this test.
    picture = Image.new("RGB", (320, 180), "white")
    draw = ImageDraw.Draw(picture)
    draw.ellipse((220, 15, 275, 70), fill="#ffdc72", outline="black", width=2)
    draw.rectangle((90, 80, 100, 150), fill="#996633")
    draw.ellipse((55, 25, 140, 110), fill="#6cb886", outline="black", width=2)
    buffer = io.BytesIO()
    picture.save(buffer, "PNG")
    data = buffer.getvalue()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    with pytest.raises(HTTPException) as error:
        await routes.upload_storyboard_image(
            project.id, 1, UploadFile(io.BytesIO(data)), request, SimpleNamespace(id=uuid4())
        )
    assert error.value.status_code == 404
    previous = director.storyboard_hash(project, runs[1].result_data)
    uploaded = await routes.upload_storyboard_image(
        project.id, 1, UploadFile(io.BytesIO(data)), request, user
    )
    assert uploaded["storyboard_hash"] != previous

    async def fake_speech(runtime, settings, job_id):
        path = tmp_path / f"{job_id}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\x00\x00" * 24000)
        async with runtime.sessions() as session, session.begin():
            speech = await session.get(SpeechJob, job_id)
            speech.status, speech.storage_path = "completed", str(path)
            if edge:
                assert speech.channel_id is None
                speech.timing = {
                    "source": "edge",
                    "cues": [{"id": 1, "text": speech.speech_text, "startMs": 100, "endMs": 900}],
                }

    monkeypatch.setattr(whiteboard, "run_speech_job", fake_speech)
    paid = AsyncMock(side_effect=AssertionError("No paid video calls allowed"))
    monkeypatch.setattr(director, "run_video_job", paid)
    await whiteboard.run_whiteboard_media(runtime, None, project, runs[2], runs[3])
    from assistant_app.services.whiteboard_annotations import annotation_digest

    async with runtime.sessions() as session:
        result = await session.get(DirectorProject, project.id)
        shot = await session.scalar(
            select(DirectorShot).where(DirectorShot.project_id == project.id)
        )
        assert result.status == "awaiting_storyboard"
        assert result.current_stage == "whiteboard_annotation_review"
        state = shot.continuity_snapshot["_whiteboard"]
        assert not state["approved"]
        assert state["annotation"]["subtitleAlignment"] == ("provider" if edge else "estimated")
    payload = await director.project_payload(runtime, result)
    with pytest.raises(director.DirectorProjectNotApprovableError, match="逐镜保存"):
        await director.approve_storyboard(runtime, user.id, project.id, payload["storyboard_hash"])
    annotation = state["annotation"]
    annotation["elements"] = [
        {
            "id": "scene",
            "label": "阳光与树木",
            "sequence": 1,
            "narrativeRole": "旁白讲解",
            "cueIds": [1],
            "region": {"x": 0, "y": 0, "width": 320, "height": 180},
            "reveal": {"startMs": 100, "durationMs": 800, "protectedRegions": []},
        }
    ]
    update = routes.WhiteboardAnnotationUpdate(
        annotation=annotation, storyboard_hash=payload["storyboard_hash"]
    )
    with pytest.raises(HTTPException) as error:
        await routes.save_whiteboard_annotation(
            project.id, shot.id, update, request, SimpleNamespace(id=uuid4())
        )
    assert error.value.status_code == 404
    saved = await routes.save_whiteboard_annotation(project.id, shot.id, update, request, user)
    assert saved["storyboard_hash"] != payload["storyboard_hash"]
    with pytest.raises(HTTPException) as error:
        await routes.save_whiteboard_annotation(project.id, shot.id, update, request, user)
    assert error.value.status_code == 409
    assert saved["agents"][1]["result_data"]["whiteboard_annotations"]["1"] == annotation_digest(
        annotation
    )
    runtime.redis = SimpleNamespace(
        set=AsyncMock(return_value=True), eval=AsyncMock(return_value=1)
    )
    preview = await routes.create_whiteboard_preview(
        project.id,
        shot.id,
        routes.WhiteboardPreviewInput(digest=annotation_digest(annotation)),
        request,
        user,
    )
    assert annotation_digest(annotation) in preview["url"]
    response = await routes.get_whiteboard_preview(
        project.id, shot.id, annotation_digest(annotation), request, user
    )
    assert response.media_type == "video/mp4"
    with pytest.raises(HTTPException) as error:
        await routes.get_whiteboard_preview(
            project.id, shot.id, annotation_digest(annotation), request, SimpleNamespace(id=uuid4())
        )
    assert error.value.status_code == 404
    runtime.redis.set.return_value = False
    with pytest.raises(HTTPException) as error:
        await routes.create_whiteboard_preview(
            project.id,
            shot.id,
            routes.WhiteboardPreviewInput(digest=annotation_digest(annotation)),
            request,
            user,
        )
    assert error.value.status_code == 409
    project = await director.approve_storyboard(
        runtime, user.id, project.id, saved["storyboard_hash"]
    )
    await whiteboard.run_whiteboard_media(runtime, None, project, runs[2], runs[3])
    async with runtime.sessions() as session:
        result = await session.get(DirectorProject, project.id)
        shots = (await session.scalars(select(DirectorShot))).all()
        assert result.status == "completed" and result.quality_report["passed"]
        assert shots[0].image_source == "uploaded" and shots[0].video_job_id is None
        assert shots[0].image_submission_started_at is None
        assert result.quality_report["final"]["audio"]
        assert result.quality_report["final"]["video"]
    paid.assert_not_awaited()
