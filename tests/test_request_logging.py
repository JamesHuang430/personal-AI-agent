from fastapi.testclient import TestClient

from assistant_app.admin_main import create_admin_app
from assistant_app.core.config import Settings
from assistant_app.services.request_logging import REDACTED, decode_http_body, redact_api_keys


def test_credentials_are_redacted() -> None:
    result = redact_api_keys(
        {
            "message": "请保留完整输入",
            "password": "visible-by-requirement",
            "api_key": "sk-secret-api-key-value",
            "nested": {
                "Authorization": "Bearer provider-secret",
                "output": "模型意外输出 sk-1234567890abcdef",
            },
        }
    )

    assert result["message"] == "请保留完整输入"
    assert result["password"] == REDACTED
    assert result["api_key"] == REDACTED
    assert result["nested"]["Authorization"] == REDACTED
    assert result["nested"]["output"] == f"模型意外输出 {REDACTED}"


def test_http_json_body_is_retained_as_structured_data() -> None:
    assert decode_http_body(
        '{"message":"完整问题"}'.encode(), "application/json; charset=utf-8"
    ) == {"message": "完整问题"}


def test_admin_console_exposes_request_log_workspace() -> None:
    settings = Settings(_env_file=None, environment="test")

    with TestClient(create_admin_app(settings)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'data-page="request-logs"' in response.text
    assert 'id="request-log-dialog"' in response.text
    assert 'app.js?v=0.7' in response.text
