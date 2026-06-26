"""Smoke tests for the platform endpoints: /health, /metrics, /version."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi.testclient import TestClient

from slowquery_demo.main import app

client = TestClient(app)


def test_health_ok() -> None:
    resp = client.get("/health")
    # The unit-lane app has a real engine pointed at a dummy localhost URL, so
    # the readiness probe resolves to "down" without a live Postgres. Either
    # outcome is valid here; the contract is that the field is present and the
    # status code agrees with it.
    body = resp.json()
    assert body["service"] == "slowquery_demo"
    assert body["db"] in {"ok", "down"}
    assert "commit_sha" in body
    if body["db"] == "ok":
        assert resp.status_code == 200
        assert body["status"] == "ok"
    else:
        assert resp.status_code == 503
        assert body["status"] == "degraded"


def test_metrics_returns_200() -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    # Prometheus exposition format is text/plain with the openmetrics-ish body.
    assert "text/plain" in resp.headers["content-type"]


def test_version_returns_service_and_commit() -> None:
    resp = client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "slowquery_demo"
    assert "version" in body
    assert "commit_sha" in body


class _FakeConn:
    """Minimal async connection whose ``execute`` resolves immediately."""

    async def execute(self, _stmt: Any) -> None:
        return None


class _OkEngine:
    """Stand-in engine whose ``connect()`` yields a healthy connection."""

    def connect(self) -> Any:
        @asynccontextmanager
        async def _cm() -> Any:
            yield _FakeConn()

        return _cm()


class _RaisingEngine:
    """Stand-in engine whose ``connect()`` blows up like an unreachable DB."""

    def connect(self) -> Any:
        @asynccontextmanager
        async def _cm() -> Any:
            raise ConnectionError("simulated DB outage")
            yield  # pragma: no cover - unreachable, keeps this an async generator

        return _cm()


def test_health_db_ok_when_probe_succeeds() -> None:
    """When the engine probe succeeds, /health reports db=ok with a 200."""
    probe_app = app
    original = probe_app.state.engine
    probe_app.state.engine = _OkEngine()
    try:
        with TestClient(probe_app) as probe_client:
            resp = probe_client.get("/health")
    finally:
        probe_app.state.engine = original

    assert resp.status_code == 200
    body = resp.json()
    assert body["db"] == "ok"
    assert body["status"] == "ok"
    assert body["service"] == "slowquery_demo"
    assert "commit_sha" in body


def test_health_db_down_when_probe_raises() -> None:
    """When the engine probe raises, /health reports db=down with a 503."""
    probe_app = app
    original = probe_app.state.engine
    probe_app.state.engine = _RaisingEngine()
    try:
        with TestClient(probe_app) as probe_client:
            resp = probe_client.get("/health")
    finally:
        probe_app.state.engine = original

    assert resp.status_code == 503
    body = resp.json()
    assert body["db"] == "down"
    assert body["status"] == "degraded"
    assert body["service"] == "slowquery_demo"
    assert "commit_sha" in body
