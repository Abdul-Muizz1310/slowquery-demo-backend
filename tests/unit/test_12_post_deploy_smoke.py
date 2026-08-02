"""Unit tests for spec 12 (post-deploy ``/health`` smoke check).

The behaviour that matters most is the **skip** path: the Render backends in
this portfolio go up and down (billing, free-tier sleep), so an unconditional
live probe in CI would turn `main` red for reasons unrelated to the diff. Test 7
asserts the strong form of that — with the URL env var unset, zero HTTP calls
are made at all, not "calls made and failures swallowed".
"""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture(autouse=True)
def _clear_smoke_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inherit the developer's own smoke settings."""
    for key in ("SMOKE_BASE_URL", "SMOKE_ATTEMPTS", "SMOKE_DELAY_S"):
        monkeypatch.delenv(key, raising=False)


# --- pure predicates -----------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        # Spec 12 test 1
        (200, {"status": "ok", "service": "slowquery_demo", "db": "ok"}, "healthy"),
        # Spec 12 test 2
        (503, {"status": "degraded", "db": "down"}, "degraded"),
        # Spec 12 test 3 — a 200 whose body says the DB is down is not healthy.
        (200, {"status": "ok", "db": "down"}, "degraded"),
        # Spec 12 test 4 — fail closed on a non-object body.
        (200, ["not", "an", "object"], "malformed"),
        (200, None, "malformed"),
        (200, "ok", "malformed"),
        # Spec 12 test 5
        (500, {}, "degraded"),
    ],
)
def test_health_verdict(status_code: int, payload: object, expected: str) -> None:
    """Spec 12 tests 1-5."""
    from scripts.smoke_health import health_verdict

    assert health_verdict(status_code, payload) == expected


@pytest.mark.parametrize(
    "base",
    ["https://x.example", "https://x.example/", "https://x.example///"],
)
def test_health_url_joins_exactly_one_slash(base: str) -> None:
    """Spec 12 test 6."""
    from scripts.smoke_health import health_url

    assert health_url(base) == "https://x.example/health"


# --- skip path (invariant 1) --------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_unset_or_blank_url_skips_without_any_request(  # type: ignore[no-untyped-def]
    value: str | None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock,
    capsys,
) -> None:
    """Spec 12 tests 7-8: skipped means *zero* outbound calls, and exit 0."""
    from scripts.smoke_health import main

    if value is not None:
        monkeypatch.setenv("SMOKE_BASE_URL", value)

    catch_all = respx_mock.route()

    assert main([]) == 0
    assert not catch_all.called
    assert respx_mock.calls.call_count == 0
    assert "skip" in capsys.readouterr().out.lower()


# --- poll path -----------------------------------------------------------


def _set_url(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    monkeypatch.setenv("SMOKE_BASE_URL", "https://demo.test")
    monkeypatch.setenv("SMOKE_DELAY_S", "0")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_healthy_first_response_exits_zero_after_one_request(  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    respx_mock,
) -> None:
    """Spec 12 test 9."""
    from scripts.smoke_health import main

    _set_url(monkeypatch)
    route = respx_mock.get("https://demo.test/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "db": "ok"})
    )

    assert main([]) == 0
    assert route.call_count == 1


def test_retries_until_healthy(monkeypatch: pytest.MonkeyPatch, respx_mock) -> None:  # type: ignore[no-untyped-def]
    """Spec 12 test 10: two 503s then a 200 still succeeds."""
    from scripts.smoke_health import main

    _set_url(monkeypatch, SMOKE_ATTEMPTS="3")
    route = respx_mock.get("https://demo.test/health").mock(
        side_effect=[
            httpx.Response(503, json={"status": "degraded", "db": "down"}),
            httpx.Response(503, json={"status": "degraded", "db": "down"}),
            httpx.Response(200, json={"status": "ok", "db": "ok"}),
        ]
    )

    assert main([]) == 0
    assert route.call_count == 3


def test_exhausted_budget_exits_one(monkeypatch: pytest.MonkeyPatch, respx_mock) -> None:  # type: ignore[no-untyped-def]
    """Spec 12 test 11: never-healthy fails the step, and stops at the budget."""
    from scripts.smoke_health import main

    _set_url(monkeypatch, SMOKE_ATTEMPTS="2")
    route = respx_mock.get("https://demo.test/health").mock(
        return_value=httpx.Response(503, json={"status": "degraded", "db": "down"})
    )

    assert main([]) == 1
    assert route.call_count == 2


def test_transport_errors_are_retried_not_raised(  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    respx_mock,
) -> None:
    """Spec 12 test 12: a cold-booting container looks like a connect error."""
    from scripts.smoke_health import main

    _set_url(monkeypatch, SMOKE_ATTEMPTS="2")
    route = respx_mock.get("https://demo.test/health").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    assert main([]) == 1
    assert route.call_count == 2
