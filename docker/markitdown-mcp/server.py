from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from markitdown import MarkItDown
from mcp.server.fastmcp import FastMCP

DOCUMENT_ROOT = Path("/data/generated").resolve()
ALLOWED_SUFFIXES = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".csv",
    ".txt",
    ".md",
    ".json",
    ".html",
}

mcp = FastMCP(
    "markitdown",
    instructions="Convert allowlisted user documents into Markdown.",
    host="0.0.0.0",
    port=3001,
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
)


def safe_document_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("Only local document URIs are allowed")
    path = Path(unquote(parsed.path)).resolve()
    if path.parent != DOCUMENT_ROOT or path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError("Document is outside the allowlisted attachment directory")
    if not path.is_file():
        raise ValueError("Document does not exist")
    return path


@mcp.tool()
def convert_to_markdown(uri: str) -> str:
    """Convert one allowlisted user attachment into Markdown."""

    path = safe_document_path(uri)
    return MarkItDown().convert(str(path)).markdown


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
