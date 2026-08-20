from __future__ import annotations

import pytest

from assistant_app.services.auth_verification import (
    consume_password_reset_token,
    create_captcha,
    create_password_reset_token,
    enforce_rate_limit,
    store_registration_code,
    verify_captcha,
    verify_registration_code,
)

pytestmark = pytest.mark.asyncio


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)

    async def incr(self, key: str) -> int:
        count = int(self.values.get(key, "0")) + 1
        self.values[key] = str(count)
        return count

    async def expire(self, key: str, seconds: int) -> bool:
        del key, seconds
        return True


def _captcha_answer(question: str) -> str:
    left, operator, right, _, _ = question.split()
    if operator == "+":
        return str(int(left) + int(right))
    return str(int(left) - int(right))


async def test_captcha_is_valid_once() -> None:
    redis = FakeRedis()
    challenge = await create_captcha(redis, "test-secret")  # type: ignore[arg-type]
    answer = _captcha_answer(challenge.question)

    assert await verify_captcha(  # type: ignore[arg-type]
        redis, "test-secret", challenge.id, answer
    )
    assert not await verify_captcha(  # type: ignore[arg-type]
        redis, "test-secret", challenge.id, answer
    )


async def test_registration_code_is_bound_to_email_and_single_use() -> None:
    redis = FakeRedis()
    await store_registration_code(  # type: ignore[arg-type]
        redis, "test-secret", "person@example.com", "123456"
    )

    assert not await verify_registration_code(  # type: ignore[arg-type]
        redis, "test-secret", "other@example.com", "123456"
    )
    assert await verify_registration_code(  # type: ignore[arg-type]
        redis, "test-secret", "person@example.com", "123456"
    )
    assert not await verify_registration_code(  # type: ignore[arg-type]
        redis, "test-secret", "person@example.com", "123456"
    )


async def test_password_reset_token_is_single_use() -> None:
    redis = FakeRedis()
    token = await create_password_reset_token(redis, "user-id")  # type: ignore[arg-type]

    assert await consume_password_reset_token(redis, token) == "user-id"  # type: ignore[arg-type]
    assert await consume_password_reset_token(redis, token) is None  # type: ignore[arg-type]


async def test_rate_limit_rejects_after_limit() -> None:
    redis = FakeRedis()

    assert await enforce_rate_limit(redis, "limit", 2, 60)  # type: ignore[arg-type]
    assert await enforce_rate_limit(redis, "limit", 2, 60)  # type: ignore[arg-type]
    assert not await enforce_rate_limit(redis, "limit", 2, 60)  # type: ignore[arg-type]
