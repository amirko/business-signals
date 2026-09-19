from fastapi.testclient import TestClient

from business_signals.main import app


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_exposes_investigation_surface() -> None:
    schema = TestClient(app).get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/investigations" in paths
    assert "/api/investigations/{investigation_id}/events" in paths
    assert "/api/investigations/{investigation_id}/responses" in paths
