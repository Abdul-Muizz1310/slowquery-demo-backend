"""Tests for platform middleware: CORS resolution and the root redirect."""

from __future__ import annotations

from slowquery_demo.core.platform import resolve_cors_origins


def test_cors_origins_reads_configured_setting() -> None:
    """CORS_ORIGINS is honoured (no longer dead config)."""
    origins = resolve_cors_origins("https://a.example,https://b.example", app_env="production")
    assert origins == ["https://a.example", "https://b.example"]


def test_cors_origins_falls_back_to_prod_defaults_when_empty() -> None:
    origins = resolve_cors_origins("", app_env="production")
    assert "https://slowquery-dashboard-frontend.vercel.app" in origins
    # No localhost in production.
    assert "http://localhost:3000" not in origins


def test_cors_origins_adds_localhost_in_development() -> None:
    origins = resolve_cors_origins("https://a.example", app_env="development")
    assert "http://localhost:3000" in origins
    assert "https://a.example" in origins


def test_cors_origins_deduplicates() -> None:
    origins = resolve_cors_origins(
        "https://a.example,https://a.example,http://localhost:3000",
        app_env="development",
    )
    assert origins.count("https://a.example") == 1
    assert origins.count("http://localhost:3000") == 1


def test_root_redirects_to_docs(test_client) -> None:  # type: ignore[no-untyped-def]
    """GET / redirects to the interactive API docs."""
    resp = test_client.get("/", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == "/docs"
