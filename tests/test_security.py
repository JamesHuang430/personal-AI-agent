from assistant_app.core.encryption import decrypt_secret, encrypt_secret
from assistant_app.core.security import (
    hash_password,
    normalize_email,
    verify_password,
)


def test_password_hash_round_trip() -> None:
    encoded = hash_password("correct-horse-battery")

    assert "correct-horse-battery" not in encoded
    assert verify_password("correct-horse-battery", encoded)
    assert not verify_password("wrong-password", encoded)


def test_email_normalization() -> None:
    assert normalize_email("  Person@Example.COM ") == "person@example.com"


def test_model_api_key_encryption_round_trip() -> None:
    encrypted = encrypt_secret("sk-private-value", "application-secret")

    assert "sk-private-value" not in encrypted
    assert decrypt_secret(encrypted, "application-secret") == "sk-private-value"
