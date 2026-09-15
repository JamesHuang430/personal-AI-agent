from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr


class EmailDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class SmtpConnection:
    host: str
    port: int
    username: str
    auth_code: str
    from_name: str
    use_ssl: bool = True
    timeout_seconds: float = 10.0


def _deliver(connection: SmtpConnection, recipient: str, subject: str, content: str) -> None:
    message = EmailMessage()
    message["From"] = formataddr((connection.from_name, connection.username))
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(content)

    context = ssl.create_default_context()
    try:
        if connection.use_ssl:
            with smtplib.SMTP_SSL(
                connection.host,
                connection.port,
                timeout=connection.timeout_seconds,
                context=context,
            ) as client:
                client.login(connection.username, connection.auth_code)
                client.send_message(message)
        else:
            with smtplib.SMTP(
                connection.host,
                connection.port,
                timeout=connection.timeout_seconds,
            ) as client:
                client.starttls(context=context)
                client.login(connection.username, connection.auth_code)
                client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("SMTP delivery failed") from exc


async def send_registration_code(
    connection: SmtpConnection,
    recipient: str,
    code: str,
    app_name: str,
) -> None:
    content = (
        f"你正在注册 {app_name}。\n\n"
        f"邮箱验证码：{code}\n\n"
        "验证码 10 分钟内有效，请勿转发给他人。若非本人操作，请忽略本邮件。"
    )
    await asyncio.to_thread(
        _deliver,
        connection,
        recipient,
        f"【{connection.from_name}】注册邮箱验证码",
        content,
    )


async def send_password_reset_link(
    connection: SmtpConnection,
    recipient: str,
    token: str,
    app_name: str,
    public_url: str,
) -> None:
    # Use a URL fragment so the bearer token is not sent to Nginx access logs.
    reset_link = f"{public_url.rstrip('/')}/#reset_token={token}"
    content = (
        f"你正在重置 {app_name} 的登录密码。\n\n"
        f"请在 30 分钟内打开以下链接：\n{reset_link}\n\n"
        "该链接仅可使用一次。若非本人操作，请忽略本邮件，你的密码不会被修改。"
    )
    await asyncio.to_thread(
        _deliver,
        connection,
        recipient,
        f"【{connection.from_name}】重置登录密码",
        content,
    )


async def send_test_email(connection: SmtpConnection, recipient: str, app_name: str) -> None:
    await asyncio.to_thread(
        _deliver,
        connection,
        recipient,
        f"【{connection.from_name}】邮件渠道测试成功",
        f"这是 {app_name} 运营后台发出的 SMTP 测试邮件。\n\n收到本邮件表示渠道配置有效。",
    )
