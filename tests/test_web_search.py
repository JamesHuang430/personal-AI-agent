from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from assistant_app.core.config import Settings
from assistant_app.services.model_gateway import AGENT_TOOLS, chat_completion
from assistant_app.services.web_search import (
    WebSearchError,
    _normalized_http_url,
    fetch_webpage,
    search_web,
)


def test_agent_exposes_search_and_page_fetch_tools() -> None:
    tools = {tool["function"]["name"]: tool for tool in AGENT_TOOLS}

    assert {"web_search", "fetch_webpage"}.issubset(tools)
    assert tools["web_search"]["function"]["parameters"]["properties"]["time_range"][
        "enum"
    ] == ["day", "week", "month", "year", "all"]


def test_web_url_normalization_rejects_credentials_and_unusual_ports() -> None:
    assert _normalized_http_url("https://example.com/news?q=ai#top") == (
        "https://example.com/news?q=ai"
    )
    assert _normalized_http_url("file:///etc/passwd") is None
    assert _normalized_http_url("http://user:secret@example.com/") is None
    assert _normalized_http_url("https://example.com:8443/") is None


@pytest.mark.asyncio
async def test_search_filters_unsafe_and_duplicate_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_search(*_args: object) -> list[dict[str, object]]:
        return [
            {
                "title": "最新资料",
                "url": "https://example.com/latest",
                "content": "公开摘要",
                "engines": ["baidu", "sogou"],
            },
            {"title": "重复", "url": "https://example.com/latest"},
            {"title": "脚本", "url": "javascript:alert(1)"},
        ]

    monkeypatch.setattr("assistant_app.services.web_search._search_searxng", fake_search)
    result = await search_web(
        Settings(_env_file=None),
        " 最新 AI 数据 ",
        time_range="day",
        max_results=5,
    )

    assert result["query"] == "最新 AI 数据"
    assert result["results"] == [
        {
            "title": "最新资料",
            "url": "https://example.com/latest",
            "snippet": "公开摘要",
            "date": "",
            "source": "baidu, sogou",
        }
    ]


@pytest.mark.asyncio
async def test_page_fetch_blocks_local_network_before_request() -> None:
    with pytest.raises(WebSearchError, match="禁止访问"):
        await fetch_webpage(Settings(_env_file=None), "http://127.0.0.1/admin")


@pytest.mark.asyncio
async def test_chat_runs_search_tool_then_returns_grounded_answer(
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

    first_call = SimpleNamespace(
        id="call-search",
        type="function",
        function=SimpleNamespace(
            name="web_search",
            arguments='{"query":"今日人工智能新闻","topic":"news","time_range":"day"}',
        ),
    )
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="", tool_calls=[first_call])
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="今天的要点来自来源 [1]。", tool_calls=[])
                )
            ],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=8, total_tokens=28),
        ),
    ]
    requests: list[dict[str, object]] = []

    class Completions:
        async def create(self, **kwargs: object) -> object:
            requests.append(kwargs)
            return responses.pop(0)

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.chat = SimpleNamespace(completions=Completions())

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def no_rate_limit(*_args: object) -> None:
        return None

    async def fake_search(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "query": "今日人工智能新闻",
            "topic": "news",
            "results": [
                {
                    "title": "AI 新闻",
                    "url": "https://example.com/ai-news",
                    "snippet": "今日发布",
                    "date": "2026-08-27",
                    "source": "Example",
                }
            ],
        }

    monkeypatch.setattr("assistant_app.services.model_gateway.AsyncOpenAI", FakeClient)
    monkeypatch.setattr("assistant_app.services.model_gateway._enforce_qps", no_rate_limit)
    monkeypatch.setattr("assistant_app.services.model_gateway.decrypt_secret", lambda *_: "key")
    monkeypatch.setattr("assistant_app.services.model_gateway.search_web", fake_search)

    result = await chat_completion(
        SimpleNamespace(sessions=Session),
        Settings(_env_file=None),
        "test-model",
        "搜索今天的人工智能新闻",
        [],
    )

    assert result["content"] == "今天的要点来自来源 [1]。"
    assert result["web_sources"][0]["url"] == "https://example.com/ai-news"
    assert result["usage"]["total_tokens"] == 40
    assert len(requests) == 2
    assert requests[1]["messages"][-1]["role"] == "tool"
