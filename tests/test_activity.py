import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from assistant_app.api.routes.chat import chat_activity
from assistant_app.core.config import Settings
from assistant_app.main import create_app
from assistant_app.services import activity, chat_runs, director


class EventRedis:
    def __init__(self):
        self.rows = {}

    def pipeline(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def rpush(self, key, value):
        self.rows.setdefault(key, []).append(value)

    def ltrim(self, key, start, end):
        self.rows[key] = self.rows[key][start:]

    def expire(self, *args):
        pass

    async def execute(self):
        pass

    async def lrange(self, key, *args):
        return self.rows.get(key, [])


@pytest.mark.asyncio
async def test_concurrent_activity_is_isolated_and_bounded():
    runtime = SimpleNamespace(redis=EventRedis())

    async def work(run_id):
        token = activity.activity_run.set(run_id)
        try:
            for i in range(205):
                await activity.emit_activity(runtime, f"{run_id}:{i}")
                await asyncio.sleep(0)
        finally:
            activity.activity_run.reset(token)

    await asyncio.gather(work("first"), work("second"))
    for run_id in ("first", "second"):
        events = await activity.read_activity(runtime, run_id)
        assert len(events) == 200
        assert all(e["name"].startswith(run_id) for e in events)
        assert all(set(e) == {"id", "time", "name", "status", "kind", "detail", "duration_ms"}
                   for e in events)
    assert activity.activity_run.get() is None


@pytest.mark.asyncio
async def test_logging_outage_does_not_break_work():
    runtime = SimpleNamespace(redis=SimpleNamespace())
    await activity.emit_activity(runtime, "step", run_id="run")
    assert await activity.read_activity(runtime, "run") == []


class Session:
    def __init__(self, record):
        self.record = record
        self.statement = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return self

    async def get(self, *args, **kwargs):
        return self.record

    async def scalar(self, stmt):
        self.statement = stmt
        return self.record


@pytest.mark.asyncio
async def test_activity_endpoint_scopes_owner_and_uses_archived_events():
    user_id = uuid4()
    saved = [{"name": "archived"}]
    session = Session(SimpleNamespace(id=uuid4(), status="completed", response={"activity": saved}))
    runtime = SimpleNamespace(sessions=lambda: session, redis=EventRedis())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime=runtime)))
    result = await chat_activity("request-key", request, SimpleNamespace(id=user_id))
    compiled = session.statement.compile()
    assert "chat_runs.user_id =" in str(compiled)
    assert "chat_runs.idempotency_key =" in str(compiled)
    assert user_id in compiled.params.values()
    assert "request-key" in compiled.params.values()
    assert result["activity"] == saved
    session.record = None
    assert await chat_activity("unknown", request, SimpleNamespace(id=user_id)) == {
        "status": "pending", "activity": [],
    }


def test_activity_endpoint_requires_login():
    with TestClient(create_app(Settings(_env_file=None, environment="test"))) as client:
        assert client.get("/api/v1/chat/activity?key=other").status_code == 401


@pytest.mark.asyncio
async def test_chat_completion_archives_actual_events(monkeypatch):
    runtime = SimpleNamespace(redis=EventRedis())
    run = SimpleNamespace(id=uuid4())
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_runs, "reserve_chat_run", AsyncMock(return_value=(run, True)))
    monkeypatch.setattr(chat_runs, "update_chat_run", save)

    async def execute(_run):
        await activity.emit_activity(runtime, "actual tool", kind="tool")
        return {"content": "done"}

    result = await chat_runs.run_chat_request(runtime, uuid4(), "key", {}, execute)
    assert [e["name"] for e in result["activity"]] == ["请求已接收", "actual tool"]
    assert save.call_args.kwargs["response"]["activity"] == result["activity"]
    assert activity.activity_run.get() is None


@pytest.mark.asyncio
async def test_director_archives_worker_events_and_restores_context(monkeypatch):
    project = SimpleNamespace(status="awaiting_storyboard", current_stage="review",
                              quality_report={"passed": True})
    runtime = SimpleNamespace(redis=EventRedis(), sessions=lambda: Session(project))

    async def execute(runtime, settings, project_id):
        await activity.emit_activity(runtime, "视觉 Agent", kind="agent")

    monkeypatch.setattr(director, "_run_director_project", execute)
    await director.run_director_project(runtime, None, uuid4())
    assert project.quality_report["passed"] is True
    assert any(e["name"] == "视觉 Agent" for e in project.quality_report["execution_activity"])
    assert activity.activity_run.get() is None
