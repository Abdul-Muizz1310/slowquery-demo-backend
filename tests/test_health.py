"""Platform-endpoint tests: ``/health``, ``/metrics``, ``/version``.

Every test here builds its app with ``create_app()`` *inside* the test, so the
autouse ``_isolated_runtime`` fixture (``tests/conftest.py``) has already moved
``cwd`` to a tmp dir and ``Settings`` reads no values from the developer's
gitignored repo-root ``.env``. Importing the module-level ``slowquery_demo.main.app``
singleton would evaluate ``create_app()`` at import time and defeat that, which
also made the readiness assertion environment-dependent — the old
``assert body["db"] in {"ok", "down"}`` covered the only two values
``core/platform.py`` can emit and therefore could not fail either way.

Readiness is instead pinned deterministically from both sides with stand-in
engines: a probe that resolves ⇒ 200 / ``db: "ok"``, a probe that raises ⇒
503 / ``db: "down"``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


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


def _app_with_engine(engine: object) -> FastAPI:
    """A freshly-built app whose readiness probe outcome is pinned."""
    from slowquery_demo.main import create_app

    app = create_app()
    app.state.engine = engine
    return app


@pytest.fixture
def healthy_client() -> Iterator[TestClient]:
    with TestClient(_app_with_engine(_OkEngine())) as client:
        yield client


def test_health_reports_db_ok_when_probe_succeeds(healthy_client: TestClient) -> None:
    """A resolving ``SELECT 1`` means 200 + ``db: "ok"`` + full service identity."""
    resp = healthy_client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["service"] == "slowquery_demo"
    assert body["version"]
    assert "commit_sha" in body


def test_health_reports_db_down_when_probe_raises() -> None:
    """An unreachable engine means 503 + ``db: "down"`` — never a 200 with a lie."""
    with TestClient(_app_with_engine(_RaisingEngine())) as client:
        resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "down"
    assert body["service"] == "slowquery_demo"
    assert "commit_sha" in body


def test_health_reports_db_down_when_no_engine_is_wired() -> None:
    """Negative space: a half-built app fails closed rather than 200-ing blind."""
    app = _app_with_engine(None)
    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 503
    assert resp.json()["db"] == "down"


def test_metrics_returns_200(healthy_client: TestClient) -> None:
    resp = healthy_client.get("/metrics")
    assert resp.status_code == 200
    # Prometheus exposition format is text/plain with the openmetrics-ish body.
    assert "text/plain" in resp.headers["content-type"]


def test_version_returns_service_and_commit(healthy_client: TestClient) -> None:
    resp = healthy_client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "slowquery_demo"
    assert body["version"]
    assert "commit_sha" in body
