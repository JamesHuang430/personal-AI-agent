from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from assistant_app.core.config import Settings


class WebSearchError(RuntimeError):
    pass


class _ReadableTextParser(HTMLParser):
    _SKIPPED = {"script", "style", "svg", "canvas", "template", "noscript"}
    _BLOCKS = {
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "header",
        "li",
        "main",
        "nav",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIPPED:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCKS and not self._skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in self._SKIPPED and self._skip_depth:
            self._skip_depth -= 1
        if tag in self._BLOCKS and not self._skip_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        self.parts.append(cleaned)
        if self._in_title:
            self.title_parts.append(cleaned)

    def readable_text(self) -> str:
        text = " ".join(self.parts)
        text = re.sub(r"[ \t]+", " ", text)
        return re.sub(r"\s*\n\s*", "\n", text).strip()

    def title(self) -> str:
        return " ".join(self.title_parts).strip()


def _normalized_http_url(value: object) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port not in {None, 80, 443}:
        return None
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, "")
    )


async def _assert_public_url(url: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        raise WebSearchError("网页地址缺少域名")
    try:
        literal = ipaddress.ip_address(host)
        addresses = {literal}
    except ValueError:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise WebSearchError("网页域名无法解析") from exc
        addresses = {ipaddress.ip_address(item[4][0]) for item in records}
    if not addresses or any(not address.is_global for address in addresses):
        raise WebSearchError("禁止访问本机、内网或保留网络地址")


async def _search_searxng(
    settings: Settings,
    query: str,
    topic: str,
    time_range: str | None,
) -> list[dict[str, object]]:
    base_url = settings.web_search_base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebSearchError("SearXNG 搜索服务地址配置无效")

    params: dict[str, str | int] = {
        "q": query,
        "format": "json",
        "categories": "news" if topic == "news" else "general",
        "language": "zh-CN",
        "safesearch": 1,
    }
    if time_range:
        params["time_range"] = time_range

    timeout = httpx.Timeout(settings.web_search_timeout_seconds, connect=5.0)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            headers={"X-Real-IP": "127.0.0.1"},
        ) as client:
            response = await client.get(f"{base_url}/search", params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WebSearchError(f"SearXNG 搜索服务暂时不可用：{type(exc).__name__}") from exc

    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise WebSearchError("SearXNG 返回了无法识别的数据")
    return [row for row in rows if isinstance(row, dict)]


async def search_web(
    settings: Settings,
    query: str,
    *,
    topic: str = "general",
    time_range: str = "all",
    max_results: int | None = None,
) -> dict[str, object]:
    if not settings.web_search_enabled:
        raise WebSearchError("管理员已关闭联网检索")
    cleaned_query = " ".join(query.split())[:500]
    if not cleaned_query:
        raise WebSearchError("搜索词不能为空")
    selected_topic = topic if topic in {"general", "news"} else "general"
    ranges = {
        "day": "day",
        "week": "week",
        "month": "month",
        "year": "year",
        "all": None,
    }
    selected_range = ranges.get(time_range)
    limit = max(1, min(max_results or settings.web_search_max_results, 10))
    try:
        rows = await asyncio.wait_for(
            _search_searxng(
                settings,
                cleaned_query,
                selected_topic,
                selected_range,
            ),
            timeout=settings.web_search_timeout_seconds + 2,
        )
    except TimeoutError as exc:
        raise WebSearchError("SearXNG 搜索超时，请稍后重试") from exc
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        url = _normalized_http_url(row.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": str(row.get("title") or url)[:300],
                "url": url,
                "snippet": str(row.get("content") or "")[:1_200],
                "date": str(row.get("publishedDate") or "")[:80],
                "source": str(
                    ", ".join(str(item) for item in row.get("engines", []))
                    if isinstance(row.get("engines"), list)
                    else row.get("engine") or urlsplit(url).hostname or ""
                )[:120],
            }
        )
        if len(results) >= limit:
            break
    if not results:
        raise WebSearchError("没有检索到可用的公开网页结果")
    return {"query": cleaned_query, "topic": selected_topic, "results": results}


async def fetch_webpage(settings: Settings, url: str) -> dict[str, str]:
    current = _normalized_http_url(url)
    if not current:
        raise WebSearchError("只允许读取标准 HTTP/HTTPS 网页")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ZhibanAI/1.0; +web-research)",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }
    timeout = httpx.Timeout(settings.web_search_timeout_seconds, connect=8.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers=headers,
        follow_redirects=False,
    ) as client:
        for _attempt in range(4):
            await _assert_public_url(current)
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    redirected = _normalized_http_url(urljoin(current, location or ""))
                    if not redirected:
                        raise WebSearchError("网页重定向地址不安全")
                    current = redirected
                    continue
                if response.status_code >= 400:
                    raise WebSearchError(f"网页返回 HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").lower()
                if not any(
                    allowed in content_type
                    for allowed in ("text/html", "application/xhtml+xml", "text/plain")
                ):
                    raise WebSearchError("该链接不是可读取的文本网页")
                declared = response.headers.get("content-length")
                if declared and int(declared) > settings.web_fetch_max_bytes:
                    raise WebSearchError("网页正文超过允许的读取大小")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > settings.web_fetch_max_bytes:
                        raise WebSearchError("网页正文超过允许的读取大小")
                encoding = response.encoding or "utf-8"
                raw_text = body.decode(encoding, errors="replace")
                if "text/plain" in content_type:
                    title = urlsplit(current).hostname or current
                    text = raw_text
                else:
                    parser = _ReadableTextParser()
                    parser.feed(raw_text)
                    title = parser.title() or (urlsplit(current).hostname or current)
                    text = parser.readable_text()
                compact = text.strip()[: settings.web_fetch_max_chars]
                if not compact:
                    raise WebSearchError("网页没有可提取的正文")
                return {"title": title[:300], "url": current, "content": compact}
        raise WebSearchError("网页重定向次数过多")
