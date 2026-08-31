from __future__ import annotations

import logging
import time
from contextvars import ContextVar, Token
from typing import Any
from uuid import uuid4

logger = logging.getLogger("assistant.http")

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
        request_id = headers.get("x-request-id") or str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        request_token: Token[str | None] = _request_id.set(request_id)
        actor_token: Token[str | None] = _actor.set(None)
        started = time.perf_counter()
        request_body = bytearray()
        response_body = bytearray()
        response_headers: dict[str, str] = {}
        status_code = 500
        error_message: str | None = None

        async def logged_receive() -> dict[str, Any]:
            message = await receive()
            if message["type"] == "http.request":
                request_body.extend(message.get("body", b""))
            return message

        async def logged_send(message: dict[str, Any]) -> None:
            nonlocal status_code, response_headers
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                raw_headers = list(message.get("headers", []))
                raw_headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = raw_headers
                response_headers = {
                    key.decode("latin-1"): value.decode("latin-1")
                    for key, value in raw_headers
                }
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, logged_receive, logged_send)
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            actor = scope.get("state", {}).get("actor") or _actor.get()
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
                from assistant_app.services.request_logging import (
                    decode_http_body,
                    record_request_log,
                )

                request_content_type = headers.get("content-type", "")
                response_content_type = response_headers.get("content-type", "")
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
                        "headers": headers,
                        "query_string": scope.get("query_string", b"").decode(
                            "utf-8", errors="replace"
                        ),
                        "body": decode_http_body(bytes(request_body), request_content_type),
                    },
                    output_payload={
                        "headers": response_headers,
                        "body": decode_http_body(bytes(response_body), response_content_type),
                    },
                    error_message=error_message,
                )
            _actor.reset(actor_token)
            _request_id.reset(request_token)
