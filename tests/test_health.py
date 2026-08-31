import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from assistant_app.api.routes.chat import ChatPayload
from assistant_app.core.config import Settings
from assistant_app.main import create_app


def test_liveness_and_request_id() -> None:
    settings = Settings(_env_file=None, environment="test")

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/live", headers={"X-Request-ID": "test-request"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["X-Request-ID"] == "test-request"


def test_root_serves_user_interface() -> None:
    settings = Settings(_env_file=None, environment="test")

    with TestClient(create_app(settings)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "私人 AI 助理" in response.text
    assert '<select id="model-select"' in response.text
    assert 'id="model-custom"' in response.text
    assert 'id="video-gallery-list"' in response.text
    assert 'id="refresh-video-gallery"' in response.text
    assert 'id="start-director-project"' in response.text
    assert 'id="one-click-movie"' in response.text
    assert 'id="director-start-form"' in response.text
    assert 'id="director-continuity-notes"' in response.text
    assert 'id="director-confirm-story"' in response.text
    assert 'app.js?v=1.4' in response.text
    assert 'aria-label="停止生成"' not in response.text


def test_chat_requires_user_selected_model() -> None:
    with pytest.raises(ValidationError):
        ChatPayload(message="hello")

    payload = ChatPayload(
        model=" gpt-4.1-mini ",
        message="hello",
    )
    assert payload.model == "gpt-4.1-mini"

    with pytest.raises(ValidationError, match="模型 ID"):
        ChatPayload(model="   ", message="hello")
