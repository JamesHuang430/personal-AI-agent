"""Bounded retry policy for text requests only, never media submissions or tool execution."""

import math
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from openai import APIConnectionError, APIStatusError, APITimeoutError

MAX_TEXT_ATTEMPTS = 4
MAX_RETRY_DELAY = 30.0


class TextModelRequestError(RuntimeError):
    """Safe public failure, without upstream HTML, prompts, URLs, or credentials."""


def retryable_text_error(error):
    if isinstance(error, APIConnectionError):
        return True
    if not isinstance(error, APIStatusError):
        return False
    body = error.body if isinstance(error.body, dict) else {}
    nested = body.get("error")
    code = (nested if isinstance(nested, dict) else body).get("code")
    if isinstance(code, str) and code in {
        "insufficient_quota", "billing_hard_limit_reached", "billing_not_active"
    }:
        return False
    return error.status_code in {408, 409, 429} or 500 <= error.status_code < 600


def retry_delay(error, attempt):
    """Respect Retry-After; decline automatic retry if its delay exceeds our budget."""
    response = getattr(error, "response", None)
    headers = response.headers if response is not None else {}
    delay = None
    try:
        if "retry-after-ms" in headers:
            delay = float(headers["retry-after-ms"]) / 1000
        elif "retry-after" in headers:
            value = headers["retry-after"]
            try:
                delay = float(value)
            except ValueError:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                delay = (when - datetime.now(UTC)).total_seconds()
    except (ValueError, TypeError, OverflowError):
        delay = None
    if delay is not None and math.isfinite(delay) and delay >= 0:
        return max(0.1, delay) if delay <= MAX_RETRY_DELAY else None
    return min(MAX_RETRY_DELAY, 2**attempt + random.uniform(0, 0.5))


def public_text_error(error):
    if isinstance(error, APITimeoutError):
        return "上游文本模型响应超时"
    if isinstance(error, APIConnectionError):
        return "上游文本模型连接失败"
    status = getattr(error, "status_code", None)
    if status in {401, 403}:
        return f"文本渠道认证或权限异常（HTTP {status}），请检查渠道配置"
    if status == 429:
        return "上游文本模型限流或额度不足（HTTP 429）"
    return f"上游文本模型请求失败（HTTP {status}）"
