from business_signals.main import app
from fastapi.testclient import TestClient


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_local_web_app_is_allowed_by_cors() -> None:
    response = TestClient(app).get("/api/datasources", headers={"Origin": "http://localhost:3000"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_openapi_exposes_investigation_surface() -> None:
    schema = TestClient(app).get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/investigations" in paths
    assert "/api/investigations/{investigation_id}/events" in paths
    assert "/api/investigations/{investigation_id}/responses" in paths
    assert "/api/investigations/{investigation_id}/stop" in paths


def test_create_investigation_hides_datasource_connection_tracebacks(monkeypatch) -> None:
    async def unavailable(*_args, **_kwargs):
        raise ConnectionError("database connection refused")

    monkeypatch.setattr("business_signals.main.investigations.create", unavailable)

    response = TestClient(app).post(
        "/api/investigations",
        json={"question": "Why did physical-store sales decline?", "datasource_ids": ["ds_demo"]},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "A selected datasource is unavailable. Check its connection and try again."
