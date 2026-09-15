from __future__ import annotations

from types import SimpleNamespace

import pytest

from assistant_app.core.config import Settings
from assistant_app.services import document_skill
from assistant_app.services.document_skill import (
    extract_document_context,
    safe_document_filename,
)
from assistant_app.services.mcp_runtime import MCPRuntimeError, call_mcp_tool


def test_document_filename_is_sanitized_and_allowlisted() -> None:
    assert safe_document_filename("../../季度复盘.PDF") == "季度复盘.pdf"
    with pytest.raises(ValueError, match="文件类型"):
        safe_document_filename("payload.exe")


@pytest.mark.asyncio
async def test_document_skill_uses_only_markitdown_tool_and_marks_content_untrusted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source = tmp_path / "example.pdf"
    source.write_bytes(b"%PDF test")
    record = SimpleNamespace(filename="example.pdf", storage_path=str(source))
    calls: list[tuple[str, dict[str, object], object]] = []

    async def fake_call(
        _settings: Settings,
        _server_url: str,
        tool_name: str,
        arguments: dict[str, object],
        allowed_tools: object,
    ) -> str:
        calls.append((tool_name, arguments, allowed_tools))
        return "# 季度复盘\n收入增长 20%"

    monkeypatch.setattr(document_skill, "GENERATED_ROOT", tmp_path)
    monkeypatch.setattr(document_skill, "call_mcp_tool", fake_call)

    context, mcp_calls = await extract_document_context(
        Settings(_env_file=None),
        [record],
    )

    assert calls[0][0] == "convert_to_markdown"
    assert calls[0][1]["uri"].startswith("file:")
    assert calls[0][2] == frozenset({"convert_to_markdown"})
    assert "不可信数据" in context
    assert "收入增长 20%" in context
    assert mcp_calls == [
        {"server": "markitdown", "tool": "convert_to_markdown", "status": "ok"}
    ]


@pytest.mark.asyncio
async def test_mcp_runtime_rejects_non_allowlisted_tools_before_network() -> None:
    with pytest.raises(MCPRuntimeError, match="允许列表"):
        await call_mcp_tool(
            Settings(_env_file=None),
            "http://example.invalid/mcp",
            "read_arbitrary_file",
            {},
            {"convert_to_markdown"},
        )
