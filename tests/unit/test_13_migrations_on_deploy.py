"""Unit tests for spec 13 (migrations on deploy).

The pure half of ``scripts/migrate_all.py`` (which databases get migrated) and
its imperative shell (one alembic subprocess per database, fail fast, never
print a URL), plus the container entrypoint run for real with a stub
``python`` on ``PATH``. The migration itself is exercised against Postgres in
``tests/integration/test_13_migrations_on_deploy.py``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_VERSIONS = _ROOT / "alembic" / "versions"

_SLOW = "postgresql://u:secret-slow@slow.example/neondb?sslmode=require"
_FAST = "postgresql://u:secret-fast@fast.example/neondb?sslmode=require"
_STORE = "postgresql+asyncpg://u:secret-store@store.example/neondb"


@pytest.fixture(autouse=True)
def _clean_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DATABASE_URL", "DATABASE_URL_FAST", "SLOWQUERY_STORE_URL"):
        monkeypatch.delenv(key, raising=False)


def _names(env: dict[str, str]) -> list[str]:
    from scripts.migrate_all import migration_targets

    return [t.name for t in migration_targets(env)]


# --- which databases (pure) ------------------------------------------------


def test_only_database_url() -> None:
    """Spec 13 test 1."""
    assert _names({"DATABASE_URL": _SLOW}) == ["DATABASE_URL"]


def test_three_distinct_databases_in_order() -> None:
    """Spec 13 test 2."""
    from scripts.migrate_all import migration_targets

    targets = migration_targets(
        {"SLOWQUERY_STORE_URL": _STORE, "DATABASE_URL_FAST": _FAST, "DATABASE_URL": _SLOW}
    )
    assert [t.name for t in targets] == ["DATABASE_URL", "DATABASE_URL_FAST", "SLOWQUERY_STORE_URL"]
    assert [t.url for t in targets] == [_SLOW, _FAST, _STORE]


@pytest.mark.parametrize(
    "store",
    [
        _SLOW,
        "postgresql+asyncpg://u:secret-slow@slow.example/neondb?ssl=require",
    ],
)
def test_store_on_the_slow_database_is_migrated_once(store: str) -> None:
    """Spec 13 test 3: same database under two names (libpq or asyncpg spelling)."""
    assert _names({"DATABASE_URL": _SLOW, "SLOWQUERY_STORE_URL": store}) == ["DATABASE_URL"]


def test_blank_optional_is_skipped_and_values_are_cleaned() -> None:
    """Spec 13 test 4."""
    from scripts.migrate_all import migration_targets

    targets = migration_targets(
        {
            "DATABASE_URL": f"\ufeff{_SLOW}\r\n",
            "DATABASE_URL_FAST": "   ",
            "SLOWQUERY_STORE_URL": "",
        }
    )
    assert [(t.name, t.url) for t in targets] == [("DATABASE_URL", _SLOW)]


@pytest.mark.parametrize("value", [None, "", "  \n"])
def test_missing_database_url_is_a_config_error(value: str | None) -> None:
    """Spec 13 test 8."""
    from scripts.migrate_all import MigrationConfigError, migration_targets

    env = {"DATABASE_URL_FAST": _FAST}
    if value is not None:
        env["DATABASE_URL"] = value
    with pytest.raises(MigrationConfigError, match="DATABASE_URL"):
        migration_targets(env)


@pytest.mark.parametrize("name", ["DATABASE_URL", "DATABASE_URL_FAST", "SLOWQUERY_STORE_URL"])
@pytest.mark.parametrize("url", ["sqlite:///x.db", "mysql://u:pw-leak@h/db", "not a url"])
def test_non_postgres_url_is_a_config_error(name: str, url: str) -> None:
    """Spec 13 test 9: names the variable, never echoes the value."""
    from scripts.migrate_all import MigrationConfigError, migration_targets

    env = {"DATABASE_URL": _SLOW, name: url}
    with pytest.raises(MigrationConfigError) as info:
        migration_targets(env)
    assert name in str(info.value)
    assert url not in str(info.value)


# --- the runner (imperative shell) -----------------------------------------


class _AlembicRecorder:
    """Stands in for ``subprocess.run``; fails the call numbered ``fail_on``."""

    def __init__(self, fail_on: int | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self._fail_on = fail_on

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((cmd, kwargs["env"]["DATABASE_URL"]))
        code = 1 if self._fail_on == len(self.calls) else 0
        return subprocess.CompletedProcess(cmd, code, stdout="", stderr="boom" if code else "")


def test_main_runs_alembic_per_target_and_prints_names_not_urls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec 13 test 5."""
    import scripts.migrate_all as runner

    monkeypatch.setenv("DATABASE_URL", _SLOW)
    monkeypatch.setenv("DATABASE_URL_FAST", _FAST)
    recorder = _AlembicRecorder()
    monkeypatch.setattr(runner.subprocess, "run", recorder)

    assert runner.main([]) == 0
    assert [url for _, url in recorder.calls] == [_SLOW, _FAST]
    for cmd, _ in recorder.calls:
        assert cmd[-3:] == ["alembic", "upgrade", "head"]
    out = capsys.readouterr().out
    assert "DATABASE_URL_FAST" in out
    assert "secret" not in out


def test_main_exits_1_without_running_alembic_on_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec 13 test 8 (runner)."""
    import scripts.migrate_all as runner

    recorder = _AlembicRecorder()
    monkeypatch.setattr(runner.subprocess, "run", recorder)
    assert runner.main([]) == 1
    assert recorder.calls == []
    assert "DATABASE_URL" in capsys.readouterr().out


def test_main_stops_at_the_first_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec 13 test 10."""
    import scripts.migrate_all as runner

    monkeypatch.setenv("DATABASE_URL", _SLOW)
    monkeypatch.setenv("DATABASE_URL_FAST", _FAST)
    recorder = _AlembicRecorder(fail_on=1)
    monkeypatch.setattr(runner.subprocess, "run", recorder)

    assert runner.main([]) == 1
    assert len(recorder.calls) == 1
    out = capsys.readouterr().out
    assert "DATABASE_URL" in out
    assert "secret" not in out


# --- the migrations on disk ------------------------------------------------


def _load_revision(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0002_follows_0001() -> None:
    """Spec 13 test 6."""
    module = _load_revision(_VERSIONS / "0002_dedupe_suggestions.py")
    assert module.revision == "0002"
    assert module.down_revision == "0001"


def test_0001_is_never_edited_again() -> None:
    """Spec 13 test 7 / invariant 1: an applied migration is immutable."""
    body = (_VERSIONS / "0001_initial.py").read_bytes()
    assert (
        hashlib.sha256(body).hexdigest()
        == "7d14c5ed3da7ec43326a7052d1a5f2552242aa95d104e18a7c673b5e61443437"
    )


# --- the container entrypoint ----------------------------------------------


def _run_entrypoint(tmp_path: Path, *, migrate_exit: int) -> subprocess.CompletedProcess[str]:
    """Run the real entrypoint with a stub ``python`` that logs its arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "python.log"
    stub = bin_dir / "python"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit {migrate_exit}\n')
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["sh", str(_ROOT / "docker-entrypoint.sh"), "echo", "server-started"],
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )


def test_entrypoint_migrates_then_execs_the_command(tmp_path: Path) -> None:
    """Spec 13 test 11."""
    result = _run_entrypoint(tmp_path, migrate_exit=0)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "python.log").read_text().split() == ["-m", "scripts.migrate_all"]
    assert result.stdout.strip().endswith("server-started")


def test_entrypoint_never_starts_the_server_after_a_failed_migration(tmp_path: Path) -> None:
    """Spec 13 test 12 / invariant 6."""
    result = _run_entrypoint(tmp_path, migrate_exit=1)
    assert result.returncode != 0
    assert "server-started" not in result.stdout


def test_dockerfile_uses_the_entrypoint_and_render_has_no_pre_deploy() -> None:
    """Spec 13 test 13."""
    import yaml

    dockerfile = (_ROOT / "Dockerfile").read_text()
    assert 'ENTRYPOINT ["./docker-entrypoint.sh"]' in dockerfile
    copies = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]
    assert any("docker-entrypoint.sh" in line for line in copies)
    blueprint = yaml.safe_load((_ROOT / "render.yaml").read_text())
    for service in blueprint["services"]:
        assert "preDeployCommand" not in service
