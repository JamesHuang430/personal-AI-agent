from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from assistant_app.api.routes import internal_pi
from assistant_app.api.routes.internal_pi import PiToolExecutionPayload, execute_pi_tool
from assistant_app.core.config import Settings
from assistant_app.services import pi_runtime


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, set[str]] = {}

    async def sadd(self, key: str, *values: str) -> None:
        self.values.setdefault(key, set()).update(values)

    async def expire(self, _key: str, _seconds: int) -> None:
        return None

    async def sismember(self, key: str, value: str) -> bool:
        return value in self.values.get(key, set())


@pytest.mark.asyncio
async def test_pi_readiness_reports_sidecar_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"status": "ok"}

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str) -> Response:
            return Response()

    monkeypatch.setattr(pi_runtime.httpx, "AsyncClient", Client)

    status = await pi_runtime.pi_runtime_readiness(Settings(_env_file=None))

    assert status.status == "ok"


@pytest.mark.asyncio
async def test_tool_bridge_requires_shared_secret() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=Settings(_env_file=None),
                runtime=SimpleNamespace(redis=FakeRedis()),
            )
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        await execute_pi_tool(
            PiToolExecutionPayload(run_id=uuid4(), name="web_search", arguments={}),
            request,
            None,
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_tool_bridge_fetches_only_urls_returned_in_same_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "s" * 32
    redis = FakeRedis()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=Settings(_env_file=None, pi_runtime_shared_secret=secret),
                runtime=SimpleNamespace(redis=redis),
            )
        )
    )
    run_id = uuid4()

    async def fake_search(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "results": [
                {
                    "title": "Source",
                    "url": "https://example.com/source",
                    "snippet": "text",
                }
            ]
        }

    async def fake_fetch(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {
            "title": "Source",
            "url": "https://example.com/source",
            "content": "body",
        }

    monkeypatch.setattr(internal_pi, "search_web", fake_search)
    monkeypatch.setattr(internal_pi, "fetch_webpage", fake_fetch)

    searched = await execute_pi_tool(
        PiToolExecutionPayload(
            run_id=run_id,
            name="web_search",
            arguments={"query": "test"},
        ),
        request,
        secret,
    )
    fetched = await execute_pi_tool(
        PiToolExecutionPayload(
            run_id=run_id,
            name="fetch_webpage",
            arguments={"url": "https://example.com/source"},
        ),
        request,
        secret,
    )
    blocked = await execute_pi_tool(
        PiToolExecutionPayload(
            run_id=uuid4(),
            name="fetch_webpage",
            arguments={"url": "https://example.com/source"},
        ),
        request,
        secret,
    )

    assert searched["is_error"] is False
    assert fetched["data"]["content"] == "body"
    assert blocked == {
        "is_error": True,
        "message": "只能读取本轮搜索结果中已经返回的链接",
    }


@pytest.mark.asyncio
async def test_pi_runtime_adapter_preserves_existing_response_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(
        id=uuid4(),
        name="test-channel",
        base_url="https://models.example/v1",
        encrypted_api_key="encrypted",
        qps_limit=1,
    )

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _statement: object) -> object:
            return channel

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "content": "Pi answer",
                "tool_calls": [],
                "web_sources": [],
                "usage": {"total_tokens": 12},
            }

    requests: list[dict[str, object]] = []

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, **kwargs: object) -> Response:
            requests.append({"url": url, **kwargs})
            return Response()

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(pi_runtime.httpx, "AsyncClient", Client)
    monkeypatch.setattr(pi_runtime, "decrypt_secret", lambda *_args: "api-key")
    monkeypatch.setattr(pi_runtime, "_enforce_qps", noop)
    monkeypatch.setattr(pi_runtime, "record_request_log", noop)

    result = await pi_runtime.pi_chat_completion(
        SimpleNamespace(sessions=Session),
        Settings(
            _env_file=None,
            agent_runtime="pi",
            pi_runtime_shared_secret="s" * 32,
        ),
        "test-model",
        "hello",
        [],
    )

    assert result == {
        "content": "Pi answer",
        "channel": "test-channel",
        "model": "test-model",
        "tool_calls": [],
        "web_sources": [],
        "usage": {"total_tokens": 12},
    }
    assert requests[0]["headers"] == {"X-Pi-Runtime-Secret": "s" * 32}
    assert requests[0]["json"]["api_key"] == "api-key"
