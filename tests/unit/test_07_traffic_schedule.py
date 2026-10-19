"""Unit tests for spec 07's schedule (``.github/workflows/traffic.yml``).

The driver's behaviour is covered in ``test_07_traffic_generator.py``. These
cases pin what the scheduled workflow does with it: when it runs, what it
targets, that it wakes the free-tier service before driving it, and that it
needs no secret.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "traffic.yml"
_TARGET = "https://slowquery-demo-backend.onrender.com"


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    loaded = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _job(workflow: dict[Any, Any]) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert len(jobs) == 1, "one job: wake, then drive"
    (job,) = jobs.values()
    return job  # type: ignore[no-any-return]


def _step_running(job: dict[str, Any], module: str) -> tuple[int, dict[str, Any]]:
    matches = [
        (index, step)
        for index, step in enumerate(job["steps"])
        if module in str(step.get("run", ""))
    ]
    assert len(matches) == 1, f"exactly one step runs {module}"
    return matches[0]


def _env(workflow: dict[Any, Any], job: dict[str, Any], step: dict[str, Any]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for scope in (workflow, job, step):
        merged.update({str(k): str(v) for k, v in (scope.get("env") or {}).items()})
    return merged


def _resolve(value: str, env: dict[str, str]) -> str:
    for name, replacement in env.items():
        value = value.replace(f"${{{{ env.{name} }}}}", replacement).replace(
            f"${name}", replacement
        )
    return value


def test_triggers_are_the_schedule_and_manual_dispatch(workflow: dict[Any, Any]) -> None:
    """Spec 07 test 15."""
    # PyYAML reads the bare key ``on`` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "0 */6 * * *"}]


def test_wakes_the_service_before_driving_it(workflow: dict[Any, Any]) -> None:
    """Spec 07 test 16."""
    job = _job(workflow)
    wake_index, wake = _step_running(job, "scripts.smoke_health")
    drive_index, _ = _step_running(job, "scripts.traffic_generator")
    assert wake_index < drive_index

    env = _env(workflow, job, wake)
    assert _resolve(env["SMOKE_BASE_URL"], env) == _TARGET
    budget_s = int(env["SMOKE_ATTEMPTS"]) * float(env["SMOKE_DELAY_S"])
    assert budget_s >= 90


def test_drives_the_target_with_json_output(workflow: dict[Any, Any]) -> None:
    """Spec 07 test 17."""
    job = _job(workflow)
    _, drive = _step_running(job, "scripts.traffic_generator")
    env = _env(workflow, job, drive)
    argv = shlex.split(_resolve(str(drive["run"]), env))
    assert argv[argv.index("--host") + 1] == _TARGET
    assert int(argv[argv.index("--duration") + 1]) > 0
    assert "--json" in argv


def test_target_permissions_and_hygiene(workflow: dict[Any, Any]) -> None:
    """Spec 07 test 18."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert _TARGET in text
    assert "secrets." not in text
    assert workflow["permissions"] == {"contents": "read"}
    assert "concurrency" in workflow
    assert int(_job(workflow)["timeout-minutes"]) > 0
