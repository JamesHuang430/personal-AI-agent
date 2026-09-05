from __future__ import annotations

import logging
import time
from contextvars import ContextVar, Token
from typing import Any
from uuid import uuid4

logger = logging.getLogger("assistant.http")
MAX_BODY_BYTES = 32_768
SAFE_HEADERS = frozenset({"content-type", "content-length", "x-request-id"})


class BodyCapture:
    """Count streamed bytes; retain only complete, small JSON bodies."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.size = 0
        self.buffer = bytearray()

    def append(self, data: bytes) -> None:
        self.size += len(data)
        if self.enabled and self.size <= MAX_BODY_BYTES:
            self.buffer.extend(data)
        else:
            self.enabled = False
            self.buffer.clear()

    def payload(self) -> Any:
        import json

        if self.enabled and self.buffer:
            try:
                return json.loads(self.buffer)
            except (ValueError, UnicodeDecodeError):
                pass
        return {"size_bytes": self.size, "body_omitted": True}

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_actor: ContextVar[str | None] = ContextVar("request_actor", default=None)


def current_request_id() -> str | None:
    return _request_id.get()


def current_request_actor() -> str | None:
    return _actor.get()


def set_request_actor(actor: str) -> None:
    _actor.set(actor)


class RequestContextMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        candidate = headers.get("x-request-id", "")
        request_id = candidate if candidate.isascii() and candidate.isprintable() and (
            0 < len(candidate) <= 64
        ) else str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        request_token: Token[str | None] = _request_id.set(request_id)
        actor_token: Token[str | None] = _actor.set(None)
        started = time.perf_counter()
        capture_body = not any(part in scope.get("path", "") for part in (
            "/auth/", "/internal/", "/request-logs", "/download", "/preview",
        ))
        request_body = BodyCapture(
            capture_body and "application/json" in headers.get("content-type", "").lower()
        )
        response_body = BodyCapture()
        response_headers: dict[str, str] = {}
        status_code = 500
        error_message: str | None = None
        response_finished: float | None = None

        async def logged_receive() -> dict[str, Any]:
            message = await receive()
            if message["type"] == "http.request":
                request_body.append(message.get("body", b""))
            return message

        async def logged_send(message: dict[str, Any]) -> None:
            nonlocal status_code, response_headers, response_finished
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                raw_headers = list(message.get("headers", []))
                raw_headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = raw_headers
                response_headers = {
                    key.decode("latin-1"): value.decode("latin-1")
                    for key, value in raw_headers
                }
                response_body.enabled = capture_body and "application/json" in (
                    response_headers.get("content-type", "").lower()
                )
            elif message["type"] == "http.response.body":
                response_body.append(message.get("body", b""))
                if not message.get("more_body", False):
                    response_finished = time.perf_counter()
            await send(message)

        try:
            await self.app(scope, logged_receive, logged_send)
        except Exception as exc:
            error_message = type(exc).__name__
            raise
        finally:
            duration_ms = round(((response_finished or time.perf_counter()) - started) * 1000, 2)
            actor = scope.get("state", {}).get("actor") or _actor.get()
            _actor.reset(actor_token)
            _request_id.reset(request_token)
            logger.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            application = scope.get("app")
            runtime = getattr(getattr(application, "state", None), "runtime", None)
            settings = getattr(getattr(application, "state", None), "settings", None)
            if runtime is not None and getattr(settings, "environment", "") != "test":
                from assistant_app.services.request_logging import record_request_log

                log_source = (
                    "admin-api"
                    if "Operations" in str(getattr(application, "title", ""))
                    else "assistant-api"
                )
                await record_request_log(
                    runtime,
                    request_id=request_id,
                    category="http",
                    source=log_source,
                    actor=actor,
                    method=scope.get("method"),
                    path=scope.get("path"),
                    status_code=status_code,
                    duration_ms=duration_ms,
                    input_payload={
                        "headers": {k: v for k, v in headers.items() if k in SAFE_HEADERS},
                        "body": request_body.payload(),
                    },
                    output_payload={
                        "headers": {k: v for k, v in response_headers.items() if k in SAFE_HEADERS},
                        "body": response_body.payload(),
                    },
                    error_message=error_message,
                )
