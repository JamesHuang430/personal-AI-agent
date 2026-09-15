from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from assistant_app.core.request_context import MAX_BODY_BYTES, BodyCapture, RequestContextMiddleware
from assistant_app.services import chat_runs, chat_tools, director, video_gateway, work_queue
from assistant_app.services.request_logging import REDACTED, serialize_payload


class Session:
    def __init__(self, scalar=None, objects=None, rows=None):
        self.scalar = AsyncMock(return_value=scalar)
        self.get = AsyncMock(side_effect=lambda model, key, **kw: (objects or {}).get(key))
        self.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: rows or []))
        self.execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))
        self.flush = AsyncMock()
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def begin(self):
        return self

    def add(self, item):
        self.added.append(item)


def runtime(session):
    return SimpleNamespace(sessions=lambda: session)


def test_binary_capture_does_not_retain_streamed_video():
    capture = BodyCapture(False)
    chunk = b"v" * 65536
    for _ in range(2048):
        capture.append(chunk)
    assert capture.size == 128 * 1024 * 1024
    assert len(capture.buffer) == 0


def test_oversized_json_is_omitted_without_retaining_partial_secrets():
    capture = BodyCapture(True)
    capture.append(b'{"password":"sensitive", "large":"')
    capture.append(b"x" * MAX_BODY_BYTES)
    assert not capture.buffer
    assert capture.payload()["body_omitted"] is True


@pytest.mark.parametrize(
    "field",
    [
        "password",
        "Cookie",
        "Set-Cookie",
        "auth_code",
        "reset_token",
        "X-Pi-Runtime-Secret",
    ],
)
def test_nested_credentials_are_not_persisted(field):
    output = serialize_payload({"nested": [{field: "dummy-credential"}]})
    assert "dummy-credential" not in output
    assert REDACTED in output


@pytest.mark.asyncio
async def test_auth_middleware_omits_body_and_cookies(monkeypatch):
    logs = AsyncMock()
    monkeypatch.setattr("assistant_app.services.request_logging.record_request_log", logs)

    async def app(scope, receive, send):
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"set-cookie", b"secret-cookie"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b'{"password":"secret"}'})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "headers": [(b"content-type", b"application/json"), (b"cookie", b"secret-cookie")],
        "app": SimpleNamespace(state=SimpleNamespace(runtime=object(), settings=None)),
    }
    await RequestContextMiddleware(app)(
        scope,
        AsyncMock(
            return_value={
                "type": "http.request",
                "body": b'{"password":"secret"}',
            }
        ),
        AsyncMock(),
    )
    saved = logs.call_args.kwargs
    assert "secret" not in json.dumps(saved)
    assert saved["input_payload"]["body"]["body_omitted"]


@pytest.mark.asyncio
async def test_complete_shot_is_reused_without_paid_regeneration(tmp_path, monkeypatch):
    path = tmp_path / "shot.mp4"
    path.write_bytes(b"completed-media")
    job = SimpleNamespace(id=uuid4(), status="completed")
    shot = SimpleNamespace(status="completed", rendered_path=str(path), video_job_id=job.id)
    session = Session(scalar=shot, objects={job.id: job})
    create = AsyncMock()
    monkeypatch.setattr(director, "create_video_job", create)
    monkeypatch.setattr(director, "_shot_prompt", lambda *args: "unchanged prompt")
    result = await director._create_and_run_shot(
        runtime(session),
        None,
        SimpleNamespace(id=uuid4(), continuity_bible={"characters": []}),
        1,
        2,
        "4",
        {},
    )
    assert result == (shot, job)
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_keeps_completed_runs_and_shot_count(monkeypatch):
    project = SimpleNamespace(id=uuid4(), status="failed", completed_shots=7)
    done = SimpleNamespace(status="completed", result_data={"plan": "saved"})
    failed = SimpleNamespace(status="failed")
    session = Session(scalar=project, rows=[done, failed])
    enqueue = AsyncMock()
    monkeypatch.setattr(director, "enqueue", enqueue)
    await director.prepare_director_resume(runtime(session), uuid4(), project.id)
    assert project.completed_shots == 7
    assert done.status == "completed"
    assert done.result_data == {"plan": "saved"}
    assert failed.status == "pending"
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_job_id_resumes_polling_instead_of_creating(monkeypatch):
    videos = SimpleNamespace(
        create=AsyncMock(),
        poll=AsyncMock(return_value=SimpleNamespace(status="completed")),
        download_content=AsyncMock(
            return_value=SimpleNamespace(aread=AsyncMock(return_value=b"mp4"))
        ),
    )

    class Client:
        def __init__(self, **kwargs):
            self.videos = videos

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(video_gateway, "AsyncOpenAI", Client)
    content = await video_gateway._run_openai_video(
        SimpleNamespace(base_url="https://example.test"),
        SimpleNamespace(provider_job_id="provider-existing"),
        "dummy",
        None,
        uuid4(),
    )
    assert content == b"mp4"
    videos.create.assert_not_awaited()
    videos.poll.assert_awaited_once_with("provider-existing", poll_interval_ms=5000)


@pytest.mark.asyncio
async def test_unknown_submission_cannot_be_automatically_charged_again(monkeypatch):
    job = SimpleNamespace(
        id=uuid4(),
        channel_id=uuid4(),
        status="processing",
        submission_started_at=datetime.now(UTC),
        provider_job_id=None,
    )
    session = Session(objects={job.id: job, job.channel_id: object()})
    fail = AsyncMock()
    submit = AsyncMock()
    monkeypatch.setattr(video_gateway, "_fail_job", fail)
    monkeypatch.setattr(video_gateway, "_run_openai_video", submit)
    await video_gateway.run_video_job(runtime(session), None, job.id)
    fail.assert_awaited_once()
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_tool_does_not_lose_previous_resource(monkeypatch):
    handler = AsyncMock(side_effect=[("files", {"id": "saved"}, "saved"), ValueError("invalid")])
    monkeypatch.setitem(
        chat_tools.TOOL_REGISTRY, "create_file", (chat_tools.FileArguments, handler)
    )
    snapshots = []

    async def checkpoint(value):
        snapshots.append(json.loads(json.dumps(value)))

    result, notices = await chat_tools.execute_tools(
        None,
        None,
        None,
        [
            {"name": "create_file", "arguments": {"filename": "a.txt", "content": "ok"}},
            {"name": "create_file", "arguments": {"filename": "b.txt", "content": "bad"}},
        ],
        checkpoint,
    )
    assert result["files"] == [{"id": "saved"}]
    assert result["tool_results"][1]["status"] == "failed"
    assert snapshots[-1]["files"] == [{"id": "saved"}]
    assert len(notices) == 2


@pytest.mark.asyncio
async def test_idempotent_replay_never_executes_model_or_tools(monkeypatch):
    run = SimpleNamespace(status="completed", response={"content": "original"})
    monkeypatch.setattr(chat_runs, "reserve_chat_run", AsyncMock(return_value=(run, False)))
    execute = AsyncMock()
    assert await chat_runs.run_chat_request(None, None, "same-key", {}, execute) == run.response
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_run_replay_returns_partial_resources_without_reexecution(monkeypatch):
    run = SimpleNamespace(
        id=uuid4(),
        status="failed",
        response={"files": [{"id": "saved"}]},
        error="interrupted",
        error_status=409,
        conversation_id=uuid4(),
    )
    monkeypatch.setattr(chat_runs, "reserve_chat_run", AsyncMock(return_value=(run, False)))
    execute = AsyncMock()
    with pytest.raises(HTTPException) as error:
        await chat_runs.run_chat_request(None, None, "same-key", {}, execute)
    assert error.value.detail["artifacts"]["files"] == [{"id": "saved"}]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_request_conflicts_with_active_user_run():
    active = SimpleNamespace(status="processing", heartbeat_at=datetime.now(UTC))
    session = Session()
    session.scalar.side_effect = [uuid4(), active, None]
    with pytest.raises(HTTPException) as error:
        await chat_runs.reserve_chat_run(runtime(session), uuid4(), "new-key", {})
    assert error.value.status_code == 409
    assert not session.added


@pytest.mark.asyncio
async def test_expired_chat_is_marked_failed_before_new_reservation():
    active = SimpleNamespace(
        status="processing", heartbeat_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    session = Session()
    session.scalar.side_effect = [uuid4(), active, None]
    _, created = await chat_runs.reserve_chat_run(runtime(session), uuid4(), "new-key", {})
    assert created
    assert active.status == "failed"


@pytest.mark.asyncio
async def test_queue_claim_uses_skip_locked_and_replaces_expired_owner():
    from sqlalchemy.dialects import postgresql

    item = SimpleNamespace(owner=uuid4(), attempts=1)
    old_owner = item.owner
    session = Session(scalar=item)
    claimed = await work_queue.claim(runtime(session))
    statement = session.scalar.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert claimed.owner != old_owner
    assert claimed.attempts == 2
    assert claimed.lease_until > datetime.now(UTC)


@pytest.mark.asyncio
async def test_same_transaction_enqueues_work_without_extra_commits():
    session = Session()
    resource_id = uuid4()
    await work_queue.enqueue(session, "video", resource_id)
    assert session.added[0].resource_id == resource_id
    assert session.added[0].kind == "video"


@pytest.mark.asyncio
async def test_every_outbound_model_and_embedding_request_acquires_qps(monkeypatch):
    import httpx

    from assistant_app.services import model_gateway

    permit = AsyncMock()
    monkeypatch.setattr(model_gateway, "_enforce_qps", permit)

    def respond(request):
        if request.url.path.endswith("embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": [0.1]}],
                    "model": "test",
                    "usage": {"total_tokens": 1},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    client_type = httpx.AsyncClient
    class TestClient(client_type):
        def __init__(self, **kw):
            super().__init__(**kw, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(model_gateway.httpx, "AsyncClient", TestClient)
    channel = SimpleNamespace(base_url="https://example.test/v1")
    async with model_gateway.model_client(None, channel, "dummy", 10) as client:
        for _ in range(2):
            await client.chat.completions.create(
                model="test", messages=[{"role": "user", "content": "hi"}]
            )
        await client.embeddings.create(model="test", input="hi")
    assert permit.await_count == 3


def test_usage_token_counts_are_preserved_in_logs():
    assert json.loads(serialize_payload({"total_tokens": 12})) == {"total_tokens": 12}


@pytest.mark.asyncio
async def test_explicit_resume_fences_worker_still_finishing_failed_task():
    item = SimpleNamespace(status="processing", owner=uuid4(), attempts=1)
    await work_queue.enqueue(Session(scalar=item), "director", uuid4(), restart=True)
    assert item.status == "queued"
    assert item.owner is None
