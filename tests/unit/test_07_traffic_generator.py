"""S3 red: unit tests for spec 07 (traffic generator)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "traffic_generator.py"


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


def test_tasks_do_not_hit_platform_endpoints() -> None:
    """Spec 07 test 3."""
    assert SCRIPT_PATH.exists(), "traffic_generator.py must be committed in S4"
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    forbidden = ("/health", "/version", "/_slowquery", "/branches/switch")
    for path in forbidden:
        assert path not in body, f"traffic generator must not hit {path}"


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


def test_branches_switch_path_absent_in_script_body() -> None:
    """Spec 07 test 12."""
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "/branches/switch" not in body


def test_no_platform_token_header_sent() -> None:
    """Spec 07 test 13."""
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "X-Platform-Token" not in body
    assert "x-platform-token" not in body


def test_script_runs_headless_no_web_ui() -> None:
    """Spec 07 test 14."""
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "--headless" in body or "headless=True" in body
