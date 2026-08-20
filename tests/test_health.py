from fastapi.testclient import TestClient

from assistant_app.core.config import Settings
from assistant_app.main import create_app


def test_liveness_and_request_id() -> None:
    settings = Settings(_env_file=None, environment="test")

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/live", headers={"X-Request-ID": "test-request"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["X-Request-ID"] == "test-request"


def test_root_exposes_versioned_health_path() -> None:
    settings = Settings(_env_file=None, environment="test")

    with TestClient(create_app(settings)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.json()["health"] == "/api/v1/health/live"

