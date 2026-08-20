import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def _fernet(secret_key: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str, secret_key: str) -> str:
    return _fernet(secret_key).encrypt(value.encode()).decode()


def decrypt_secret(value: str, secret_key: str) -> str:
    try:
        return _fernet(secret_key).decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("渠道密钥无法解密，请重新配置") from exc
