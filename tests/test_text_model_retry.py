import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from assistant_app.services import model_gateway as gateway
from assistant_app.services.text_model_retry import (
    TextModelRequestError,
    retry_delay,
    retryable_text_error,
)


def upstream_error(status, headers=None, body=None):
    request = httpx.Request("POST", "https://upstream.test/v1/chat/completions")
    response = httpx.Response(status, request=request, headers=headers)
    return APIStatusError("<html>504 secret provider response</html>", response=response, body=body)


@pytest.fixture
def traces(monkeypatch):
    logs, events, sleep = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(gateway, "record_request_log", logs)
    monkeypatch.setattr(gateway, "emit_activity", events)
    monkeypatch.setattr(gateway.asyncio, "sleep", sleep)
    return logs, events, sleep


def fake_client(*responses):
    create = AsyncMock(side_effect=list(responses))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), create


async def call(client):
    return await gateway.logged_model_completion(
        None,
        client,
        source="director-agent",
        model="test",
        messages=[{"role": "user", "content": "story"}],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
async def test_transient_errors_retry_only_current_text_request(status, traces):
    logs, events, sleep = traces
    client, create = fake_client(upstream_error(status), {"content": "ok"})
    assert await call(client) == {"content": "ok"}
    assert create.await_count == 2 and sleep.await_count == 1
    assert create.call_args_list[0] == create.call_args_list[1]
    assert "_retry" not in create.call_args.kwargs
    assert [row.kwargs["status_code"] for row in logs.call_args_list] == [status, 200]
    assert logs.call_args_list[1].kwargs["input_payload"]["_retry"]["attempt"] == 2
    assert any(row.kwargs.get("kind") == "retry" for row in events.call_args_list)


@pytest.mark.asyncio
async def test_persistent_504_is_bounded_and_public_message_has_no_html(traces):
    logs, _, sleep = traces
    client, create = fake_client(*[upstream_error(504) for _ in range(4)])
    with pytest.raises(TextModelRequestError) as error:
        await call(client)
    assert create.await_count == 4 and sleep.await_count == 3
    assert "504" in str(error.value) and "4 次" in str(error.value)
    assert "<html>" not in str(error.value) and "secret" not in str(error.value)
    assert [call.kwargs["status_code"] for call in logs.call_args_list] == [504] * 4
    assert all(
        2**index <= row.args[0] <= 2**index + 0.5
        for index, row in enumerate(sleep.call_args_list, 1)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_errors_are_not_retried(status, traces):
    _, _, sleep = traces
    client, create = fake_client(upstream_error(status))
    with pytest.raises(TextModelRequestError):
        await call(client)
    create.assert_awaited_once()
    sleep.assert_not_awaited()


def test_quota_errors_are_not_retried():
    assert not retryable_text_error(
        upstream_error(429, body={"error": {"code": "insufficient_quota"}})
    )


@pytest.mark.parametrize("kind", [APIConnectionError, APITimeoutError])
@pytest.mark.asyncio
async def test_network_errors_are_retried(kind, traces):
    client, create = fake_client(kind(request=httpx.Request("POST", "https://upstream.test")), "ok")
    assert await call(client) == "ok"
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_retry_after_longer_than_budget_does_not_retry_early(traces):
    _, _, sleep = traces
    client, create = fake_client(upstream_error(429, {"Retry-After": "3600"}))
    with pytest.raises(TextModelRequestError):
        await call(client)
    create.assert_awaited_once()
    sleep.assert_not_awaited()


def test_retry_after_headers():
    assert retry_delay(upstream_error(503, {"Retry-After": "7"}), 1) == 7
    assert retry_delay(upstream_error(503, {"Retry-After-Ms": "1250"}), 1) == 1.25
    assert 2 <= retry_delay(upstream_error(503, {"Retry-After": "invalid"}), 1) <= 2.5


@pytest.mark.asyncio
async def test_cancellation_during_backoff_stops_retry(traces):
    _, _, sleep = traces
    sleep.side_effect = asyncio.CancelledError
    client, create = fake_client(upstream_error(504), "must not run")
    with pytest.raises(asyncio.CancelledError):
        await call(client)
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_programming_errors_are_not_retried(traces):
    client, create = fake_client(ValueError("bad local data"))
    with pytest.raises(ValueError, match="bad local data"):
        await call(client)
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_sdk_has_no_hidden_retries_and_each_retry_passes_qps_gate(monkeypatch, traces):
    permit = AsyncMock()
    monkeypatch.setattr(gateway, "_enforce_qps", permit)
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(504, text="<html><h1>504 Gateway Time-out</h1></html>")
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

    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(gateway.httpx, "AsyncClient", MockClient)
    channel = SimpleNamespace(base_url="https://upstream.test/v1")
    async with gateway.model_client(None, channel, "dummy", 10) as client:
        assert isinstance(client, AsyncOpenAI) and client.max_retries == 0
        response = await call(client)
    assert response.choices[0].message.content == "ok"
    assert len(requests) == permit.await_count == 3
