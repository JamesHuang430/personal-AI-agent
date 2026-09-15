from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from redis.asyncio import Redis

CAPTCHA_TTL_SECONDS = 300
REGISTRATION_CODE_TTL_SECONDS = 600
PASSWORD_RESET_TTL_SECONDS = 1800


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _keyed_digest(secret_key: str, value: str) -> str:
    return hmac.new(secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class CaptchaChallenge:
    id: str
    question: str
    expires_in: int = CAPTCHA_TTL_SECONDS


async def create_captcha(redis: Redis, secret_key: str) -> CaptchaChallenge:
    left = secrets.randbelow(8) + 2
    right = secrets.randbelow(8) + 1
    if secrets.randbelow(2):
        question = f"{left} + {right} = ?"
        answer = left + right
    else:
        high, low = max(left, right), min(left, right)
        question = f"{high} − {low} = ?"
        answer = high - low

    challenge_id = secrets.token_urlsafe(24)
    expected = _keyed_digest(secret_key, f"{challenge_id}:{answer}")
    await redis.set(
        f"auth:captcha:{_digest(challenge_id)}",
        expected,
        ex=CAPTCHA_TTL_SECONDS,
    )
    return CaptchaChallenge(id=challenge_id, question=question)


async def verify_captcha(redis: Redis, secret_key: str, challenge_id: str, answer: str) -> bool:
    key = f"auth:captcha:{_digest(challenge_id)}"
    expected = await redis.getdel(key)
    if not expected:
        return False
    actual = _keyed_digest(secret_key, f"{challenge_id}:{answer.strip()}")
    return hmac.compare_digest(expected, actual)


def new_registration_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def store_registration_code(
    redis: Redis, secret_key: str, email: str, code: str
) -> None:
    expected = _keyed_digest(secret_key, f"{email}:{code}")
    await redis.set(
        f"auth:register-code:{_digest(email)}",
        expected,
        ex=REGISTRATION_CODE_TTL_SECONDS,
    )


async def verify_registration_code(
    redis: Redis, secret_key: str, email: str, code: str
) -> bool:
    expected = await redis.getdel(f"auth:register-code:{_digest(email)}")
    if not expected:
        return False
    actual = _keyed_digest(secret_key, f"{email}:{code.strip()}")
    return hmac.compare_digest(expected, actual)


async def create_password_reset_token(redis: Redis, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    await redis.set(
        f"auth:password-reset:{_digest(token)}",
        user_id,
        ex=PASSWORD_RESET_TTL_SECONDS,
    )
    return token


async def consume_password_reset_token(redis: Redis, token: str) -> str | None:
    return await redis.getdel(f"auth:password-reset:{_digest(token)}")


async def enforce_rate_limit(redis: Redis, key: str, limit: int, window_seconds: int) -> bool:
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    return count <= limit


def privacy_key(value: str) -> str:
    """Return a stable non-PII value suitable for Redis rate-limit keys."""

    return _digest(value)
