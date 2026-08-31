from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from assistant_app.db.models import RequestLog
from assistant_app.db.runtime import RuntimeDependencies

logger = logging.getLogger(__name__)

REDACTED = "***REDACTED***"
_API_KEY_FIELD = re.compile(r"^(?:authorization|x[-_]?api[-_]?key|api[-_]?key|apikey)$", re.I)
_API_KEY_VALUE = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b")
_API_KEY_ASSIGNMENT = re.compile(
    r"(?i)(api[-_ ]?key\s*[:=]\s*)([^\s,;}&]+)"
)


def redact_api_keys(value: Any) -> Any:
    """Preserve request/model content while masking only API-key-shaped values."""

    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _API_KEY_FIELD.fullmatch(str(key)) else redact_api_keys(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_api_keys(item) for item in value]
    if isinstance(value, str):
        masked = _API_KEY_VALUE.sub(REDACTED, value)
        return _API_KEY_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{REDACTED}", masked)
    return value


def serialize_payload(value: Any) -> str:
    sanitized = redact_api_keys(value)
    return json.dumps(sanitized, ensure_ascii=False, default=str, separators=(",", ":"))


def decode_http_body(body: bytes, content_type: str) -> Any:
    if not body:
        return None
    normalized = content_type.lower()
    if "application/json" in normalized:
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body.decode("utf-8", errors="replace")
    if normalized.startswith("text/") or "x-www-form-urlencoded" in normalized:
        return body.decode("utf-8", errors="replace")
    return {
        "content_type": content_type or "application/octet-stream",
        "size_bytes": len(body),
        "note": "二进制正文未写入日志",
    }


async def record_request_log(
    runtime: RuntimeDependencies,
    *,
    request_id: str,
    category: str,
    source: str,
    actor: str | None = None,
    method: str | None = None,
    path: str | None = None,
    status_code: int | None = None,
    duration_ms: float | None = None,
    model_name: str | None = None,
    input_payload: Any = None,
    output_payload: Any = None,
    error_message: str | None = None,
) -> None:
    """Persist an operational trace without ever failing the business request."""

    try:
        async with runtime.sessions() as session:
            session.add(
                RequestLog(
                    request_id=request_id[:64],
                    category=category[:16],
                    source=source[:100],
                    actor=actor[:320] if actor else None,
                    method=method[:16] if method else None,
                    path=path[:500] if path else None,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    model_name=model_name[:200] if model_name else None,
                    input_payload=(
                        serialize_payload(input_payload) if input_payload is not None else None
                    ),
                    output_payload=(
                        serialize_payload(output_payload) if output_payload is not None else None
                    ),
                    error_message=(
                        str(redact_api_keys(error_message)) if error_message else None
                    ),
                )
            )
            await session.commit()
    except Exception as exc:  # logging must never turn a valid request into a failure
        logger.warning("request_log_persist_failed", extra={"error_type": type(exc).__name__})
