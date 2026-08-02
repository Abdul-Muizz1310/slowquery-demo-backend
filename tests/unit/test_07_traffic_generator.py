"""S3 red: unit tests for spec 07 (traffic generator).

The endpoint-scope invariants (spec 07 invariants 1-2, tests 3 / 12 / 13 / 14)
are asserted against the **composed driver flow** — ``_run_driver`` is executed
end to end with ``respx`` intercepting httpx — not by grepping the script's
source text. The earlier source-grep versions were satisfiable by a literal
anywhere in the file (and test 14 was satisfied by a dead
``_GREP_MARKER_HEADLESS = "--headless"`` constant planted in the script for
exactly that purpose), so they could pass while the driver did the wrong thing.
"""

from __future__ import annotations

import random
from pathlib import Path

import httpx
import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "traffic_generator.py"

# Spec 07 invariant 1: platform probes, observability routes and the branch
# toggle are all out of scope for generated traffic.
FORBIDDEN_PREFIXES = ("/health", "/version", "/metrics", "/_slowquery", "/branches")

_DRIVER_HOST = "http://demo.test"

# ``_run_driver`` prefetches ``/users?limit=20`` and ``/products?limit=20`` once
# before the task loop; neither is counted in ``TrafficStats.total``.
_PREFETCH_REQUESTS = 2


async def _drive_and_record(respx_mock, **overrides):  # type: ignore[no-untyped-def]
    """Run the real driver loop against a mocked host; return the calls made.

    Executes ``_run_driver`` — the same coroutine ``main()`` runs — so the
    assertions cover the composed task-dispatch flow rather than a helper in
    isolation.
    """
    from scripts.traffic_generator import TrafficArgs, _run_driver

    respx_mock.route(host="demo.test").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "3f1d8e2a-0000-4000-8000-000000000001"}]}
        )
    )
    kwargs = {"host": _DRIVER_HOST, "duration": 1, "users": 50, "json": False}
    kwargs.update(overrides)
    stats = await _run_driver(TrafficArgs(**kwargs))  # type: ignore[arg-type]
    return stats, [call.request for call in respx_mock.calls]


def test_weighted_choice_distribution() -> None:
    """Spec 07 test 1."""
    from scripts.traffic_generator import choose_weighted

    tasks = [("a", 10), ("b", 30), ("c", 60)]
    rng = random.Random(42)
    counts: dict[str, int] = {"a": 0, "b": 0, "c": 0}
    for _ in range(10_000):
        name = choose_weighted(rng, tasks)
        counts[name] += 1
    assert 800 < counts["a"] < 1200
    assert 2700 < counts["b"] < 3300
    assert 5700 < counts["c"] < 6300


def test_parse_args_defaults_and_override() -> None:
    """Spec 07 test 2."""
    from scripts.traffic_generator import parse_args

    defaults = parse_args([])
    assert defaults.users > 0
    assert defaults.duration > 0

    override = parse_args(["--users", "50", "--json"])
    assert override.users == 50
    assert override.json is True


async def test_tasks_do_not_hit_platform_endpoints(respx_mock) -> None:  # type: ignore[no-untyped-def]
    """Spec 07 test 3 / invariant 1: generated traffic is commerce-only.

    Asserted on the requests the driver actually issues, so a task that
    started probing ``/health`` would fail here even if the literal never
    appeared in the script's source text.
    """
    stats, requests = await _drive_and_record(respx_mock)

    assert stats.total > 0, "the driver must actually issue traffic"
    assert requests
    offenders = [str(r.url) for r in requests if r.url.path.startswith(FORBIDDEN_PREFIXES)]
    assert offenders == [], f"traffic generator must not hit platform routes: {offenders}"


@pytest.mark.slow
def test_unreachable_host_exits_nonzero() -> None:
    """Spec 07 test 8. Marked @slow — spawns a subprocess for ~2s."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--host",
            "http://127.0.0.1:1",
            "--duration",
            "2",
            "--users",
            "1",
        ],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0


def test_high_p95_exits_nonzero(fake_locust_stats_high_p95) -> None:  # type: ignore[no-untyped-def]
    """Spec 07 test 9."""
    from scripts.traffic_generator import exit_code_for_stats

    assert exit_code_for_stats(fake_locust_stats_high_p95) != 0


def test_high_failure_rate_exits_nonzero(fake_locust_stats_high_failures) -> None:  # type: ignore[no-untyped-def]
    """Spec 07 test 10."""
    from scripts.traffic_generator import exit_code_for_stats

    assert exit_code_for_stats(fake_locust_stats_high_failures) != 0


def test_empty_seed_data_still_runs_with_fallback_ids() -> None:
    """Spec 07 test 11 — generator runs even when seed data is empty (uses fallback UUIDs)."""
    from scripts.traffic_generator import main

    # The generator falls back to random UUIDs when /users and /products
    # return empty lists, so it shouldn't crash — it just has no "real" ids.
    # We verify it starts, runs briefly, and exits cleanly (code 0).
    # Use a host that 404s everything so seed fetch returns empty.
    exit_code = main(["--host", "http://127.0.0.1:1", "--duration", "0", "--json"])
    # Duration 0 means it exits immediately after seed fetch attempt.
    # Exit code 0 = success (no errors), 1 = >5% failure rate (expected with dead host).
    assert exit_code in (0, 1)


async def test_driver_never_switches_branches(respx_mock) -> None:  # type: ignore[no-untyped-def]
    """Spec 07 test 12: the generator issues no state-mutating request.

    Every request must be a ``GET``, and none may target ``/branches/switch``.
    A read-only driver cannot flip the demo out from under a viewer.
    """
    _stats, requests = await _drive_and_record(respx_mock)

    assert requests
    assert {r.method for r in requests} == {"GET"}
    assert [str(r.url) for r in requests if r.url.path == "/branches/switch"] == []


async def test_no_platform_token_header_sent(respx_mock) -> None:  # type: ignore[no-untyped-def]
    """Spec 07 test 13: the generator relies on DEMO_MODE, never on a token.

    Asserted on the wire, so a header added by a future client tweak (default
    headers, an auth hook) is caught even though the literal string would not
    appear in this module.
    """
    _stats, requests = await _drive_and_record(respx_mock)

    assert requests
    for request in requests:
        header_names = {name.lower() for name in request.headers}
        assert "x-platform-token" not in header_names


async def test_driver_is_headless_and_terminates_at_its_deadline(  # type: ignore[no-untyped-def]
    respx_mock,
) -> None:
    """Spec 07 test 14 (httpx equivalent of Locust's ``--headless`` / ``--no-web``).

    This driver is httpx-based, not Locust (see DEVIATIONS §10), so there is no
    web UI or stats-upload endpoint to switch off: headless is the *only* mode.
    The behaviour that matters is therefore twofold — no web-UI/stats flag is
    accepted, and the run terminates on its own at the ``--duration`` deadline
    instead of looping forever (invariant 2).
    """
    from scripts.traffic_generator import parse_args

    for flag in ("--headless", "--no-web", "--web-host", "--web-port"):
        with pytest.raises(SystemExit):
            parse_args([flag])

    stats, requests = await _drive_and_record(respx_mock, duration=1)

    # Returning at all proves the deadline terminated the loop rather than the
    # test timing out. Each completed task counts once in ``stats.total`` while
    # issuing one or more requests, on top of the two prefetch calls.
    assert stats.total > 0
    assert len(requests) >= stats.total + _PREFETCH_REQUESTS
    assert stats.p95_ms >= 0.0
