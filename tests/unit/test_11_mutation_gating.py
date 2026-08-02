"""Spec 11: gating the state-mutating endpoints on a public URL.

Closes the two HIGH audit findings — anonymous ``force-explain`` could
overwrite a genuine captured plan, and ``branches/switch`` had no
throttle even though ``DEMO_MODE`` bypasses the platform-token
middleware. These tests drive the composed request path (routers +
dependencies) through a real ``create_app`` with a mocked DB session,
plus direct unit checks of the pure access-control helpers.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from slowquery_demo.core.access import (
    CooldownLimiter,
    enforce_cooldown,
    require_admin_token,
    require_admin_token_if_configured,
)


def _build_client(monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    from slowquery_demo.core.database import get_db
    from slowquery_demo.main import create_app

    for key, value in env.items():
        monkeypatch.setenv(key, value)

    app = create_app()
    switcher = app.state.branch_switcher
    switcher._engine_builder = None  # no Neon dial; state-only switch

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        session = AsyncMock()
        yield session

    app.dependency_overrides[get_db] = _override
    # raise_server_exceptions=False: force-explain proceeds past the gate to the
    # store, which fails at the dummy localhost DB — we assert on the gate, not
    # the downstream 5xx, so let it surface as a response instead of re-raising.
    client = TestClient(app, raise_server_exceptions=False)
    return client


# --- force-explain: destructive, fail-closed --------------------------------


def test_force_explain_fails_closed_without_token(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 11 test 1: with no DEMO_MUTATION_TOKEN, force-explain is 403 even in demo mode."""
    resp = test_client.post("/_slowquery/queries/abc123/force-explain")
    assert resp.status_code == 403


def test_force_explain_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 11 test 2: a wrong admin token is rejected before any store write."""
    client = _build_client(monkeypatch, DEMO_MUTATION_TOKEN="s3cret")
    resp = client.post(
        "/_slowquery/queries/abc123/force-explain",
        headers={"X-Admin-Token": "wrong"},
    )
    assert resp.status_code == 403


def test_force_explain_passes_gate_with_correct_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 11 test 3: a correct token clears the gate (handler then hits the store).

    The unit-lane store points at a dummy localhost URL, so the call
    proceeds past the 403/429 gate and fails later at the DB boundary —
    the assertion is only that the auth gate did not reject it.
    """
    client = _build_client(monkeypatch, DEMO_MUTATION_TOKEN="s3cret")
    resp = client.post(
        "/_slowquery/queries/abc123/force-explain",
        headers={"X-Admin-Token": "s3cret"},
    )
    assert resp.status_code not in (403, 429)


def test_force_explain_writes_plan_and_reports_suggestion_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 11 test 3 (past the gate): with a token *and* a working store, the
    handler upserts the fingerprint, writes the synthetic plan, and reports how
    many suggestions came out.

    ``test_force_explain_passes_gate_with_correct_token`` only asserts the gate
    did not refuse; the store then fails against the dummy localhost URL, so the
    handler body itself had no coverage. Substituting the store covers it.
    """
    client = _build_client(monkeypatch, DEMO_MUTATION_TOKEN="s3cret")
    store = AsyncMock()
    client.app.state.slowquery_store = store  # type: ignore[attr-defined]

    resp = client.post(
        "/_slowquery/queries/abc123/force-explain",
        headers={"X-Admin-Token": "s3cret"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # The synthetic plan is a bare Result node, which no rule matches, and the
    # unit-lane app has no LLM explainer wired — so zero suggestions.
    assert body["suggestions_count"] == "0"

    store.upsert_fingerprint.assert_awaited_once()
    assert store.upsert_fingerprint.await_args.args[0] == "abc123"
    store.upsert_plan.assert_awaited_once()
    plan = store.upsert_plan.await_args.kwargs["plan_json"]
    assert plan["Plan"]["Node Type"] == "Result"
    store.insert_suggestions.assert_not_awaited()


def test_force_explain_without_a_store_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative space: an app whose store was never wired fails closed with 503,
    not an AttributeError 500."""
    client = _build_client(monkeypatch, DEMO_MUTATION_TOKEN="s3cret")
    client.app.state.slowquery_store = None  # type: ignore[attr-defined]

    resp = client.post(
        "/_slowquery/queries/abc123/force-explain",
        headers={"X-Admin-Token": "s3cret"},
    )
    assert resp.status_code == 503


def test_force_explain_rejects_malformed_fingerprint_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The id is validated before any store write, so a path payload can never
    reach the database layer."""
    client = _build_client(monkeypatch, DEMO_MUTATION_TOKEN="s3cret")
    store = AsyncMock()
    client.app.state.slowquery_store = store  # type: ignore[attr-defined]

    resp = client.post(
        "/_slowquery/queries/NOT-HEX!/force-explain",
        headers={"X-Admin-Token": "s3cret"},
    )

    assert resp.status_code == 404
    store.upsert_fingerprint.assert_not_awaited()


# --- branch switch: cooldown + optional lockdown ----------------------------


def test_branch_switch_cooldown_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 11 test 4: a second switch inside the cooldown window is throttled."""
    client = _build_client(monkeypatch, DEMO_MUTATION_COOLDOWN_S="60")
    first = client.post("/branches/switch", json={"target": "fast"})
    assert first.status_code == 200
    second = client.post("/branches/switch", json={"target": "slow"})
    assert second.status_code == 429


def test_branch_switch_public_when_no_token(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 11 test 5: with no token and no cooldown, switching stays public (demo punchline)."""
    resp = test_client.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 200


def test_branch_switch_requires_token_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec 11 test 6: configuring the token locks down branch switch too."""
    client = _build_client(monkeypatch, DEMO_MUTATION_TOKEN="s3cret")
    denied = client.post("/branches/switch", json={"target": "fast"})
    assert denied.status_code == 403
    allowed = client.post(
        "/branches/switch",
        json={"target": "fast"},
        headers={"X-Admin-Token": "s3cret"},
    )
    assert allowed.status_code == 200


# --- pure helper units ------------------------------------------------------


def test_cooldown_limiter_zero_disables() -> None:
    limiter = CooldownLimiter(0.0)
    assert limiter.allow("ip") is True
    assert limiter.allow("ip") is True  # never throttles at 0


def test_cooldown_limiter_enforces_window() -> None:
    limiter = CooldownLimiter(60.0)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False  # inside window
    assert limiter.allow("b") is True  # a different key is independent


def test_cooldown_limiter_rejects_negative() -> None:
    with pytest.raises(ValueError):
        CooldownLimiter(-1.0)


class _FakeState:
    pass


class _FakeApp:
    def __init__(self) -> None:
        self.state = _FakeState()


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, *, settings, limiter=None, token=None, host="1.2.3.4") -> None:  # type: ignore[no-untyped-def]
        self.app = _FakeApp()
        self.app.state.settings = settings
        if limiter is not None:
            self.app.state.mutation_limiter = limiter
        self.headers = {"x-admin-token": token} if token is not None else {}
        self.client = _FakeClient(host)


class _Settings:
    def __init__(self, token: str | None) -> None:
        self.demo_mutation_token = token


@pytest.mark.asyncio
async def test_require_admin_token_fails_closed_when_unconfigured() -> None:
    from fastapi import HTTPException

    req = _FakeRequest(settings=_Settings(None))
    with pytest.raises(HTTPException) as exc:
        await require_admin_token(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_token_accepts_matching_secret() -> None:
    req = _FakeRequest(settings=_Settings("secret"), token="secret")
    await require_admin_token(req)  # type: ignore[arg-type]  # no raise == pass


@pytest.mark.asyncio
async def test_require_admin_token_if_configured_is_public_when_unset() -> None:
    req = _FakeRequest(settings=_Settings(None))
    await require_admin_token_if_configured(req)  # type: ignore[arg-type]  # no raise


@pytest.mark.asyncio
async def test_enforce_cooldown_raises_429_when_denied() -> None:
    from fastapi import HTTPException

    limiter = CooldownLimiter(60.0)
    req = _FakeRequest(settings=_Settings(None), limiter=limiter)
    await enforce_cooldown(req)  # type: ignore[arg-type]  # first passes
    with pytest.raises(HTTPException) as exc:
        await enforce_cooldown(req)  # type: ignore[arg-type]
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_enforce_cooldown_noop_without_limiter() -> None:
    req = _FakeRequest(settings=_Settings(None))  # no limiter on state
    await enforce_cooldown(req)  # type: ignore[arg-type]  # no raise
