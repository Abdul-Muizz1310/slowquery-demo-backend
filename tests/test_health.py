"""Smoke test: /health returns 200."""

from fastapi.testclient import TestClient

from slowquery_demo.main import app

client = TestClient(app)


def test_health_ok() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "slowquery_demo"
