"""Apply ``alembic upgrade head`` to every database the service uses (spec 13).

The service talks to up to three Postgres URLs, and each carries the full
schema:

* ``DATABASE_URL`` — the ``slowquery`` branch (required);
* ``DATABASE_URL_FAST`` — the ``slowquery-fast`` branch; ``POST
  /branches/switch`` moves the commerce engine (and the dashboard's reads of
  the bookkeeping tables) onto it;
* ``SLOWQUERY_STORE_URL`` — where the store writer persists (defaults to
  ``DATABASE_URL``).

``alembic/env.py`` only reads ``DATABASE_URL``, so each target is migrated in
its own subprocess with ``DATABASE_URL`` pointed at it. The container
entrypoint runs this before uvicorn; a non-zero exit stops the boot.

Output names the env var, never the URL: the URL carries the password.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

REQUIRED: Final = "DATABASE_URL"
OPTIONAL: Final = ("DATABASE_URL_FAST", "SLOWQUERY_STORE_URL")
_POSTGRES_SCHEMES: Final = ("postgres://", "postgresql://", "postgresql+asyncpg://")
_PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]


class MigrationConfigError(ValueError):
    """The environment does not describe a migratable set of databases."""


@dataclass(frozen=True)
class MigrationTarget:
    name: str
    url: str


def _clean(raw: str | None) -> str:
    return (raw or "").lstrip("\ufeff").strip()


def _identity(url: str) -> tuple[str, int | None, str]:
    """Which database a URL names: host, port and database, whatever the spelling.

    A libpq URL (``postgresql://…?sslmode=require``) and its asyncpg twin
    (``postgresql+asyncpg://…?ssl=require``) differ only in scheme and query,
    so they name the same database and are migrated once.
    """
    parts = urlsplit(url)
    return ((parts.hostname or "").lower(), parts.port, parts.path)


def migration_targets(env: Mapping[str, str]) -> list[MigrationTarget]:
    """The databases to migrate, in order, each once.

    Raises :class:`MigrationConfigError` when ``DATABASE_URL`` is missing or
    any configured URL is not Postgres. The message names the variable and
    never repeats its value.
    """
    if not _clean(env.get(REQUIRED)):
        raise MigrationConfigError(f"{REQUIRED} is required to run migrations")

    targets: list[MigrationTarget] = []
    seen: set[tuple[str, int | None, str]] = set()
    for name in (REQUIRED, *OPTIONAL):
        url = _clean(env.get(name))
        if not url:
            continue
        if not url.startswith(_POSTGRES_SCHEMES):
            raise MigrationConfigError(f"{name} is not a Postgres URL")
        identity = _identity(url)
        if identity in seen:
            continue
        seen.add(identity)
        targets.append(MigrationTarget(name=name, url=url))
    return targets


def _upgrade(target: MigrationTarget) -> bool:
    env = {**os.environ, "DATABASE_URL": target.url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        # alembic's own stderr names the failing revision and SQL error; it
        # does not print the connection URL.
        print(result.stderr.rstrip(), file=sys.stderr)
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    _ = argv  # no flags: the environment is the whole input
    try:
        targets = migration_targets(os.environ)
    except MigrationConfigError as exc:
        print(f"[migrate] configuration error: {exc}")
        return 1

    for target in targets:
        print(f"[migrate] {target.name}: alembic upgrade head")
        if not _upgrade(target):
            print(f"[migrate] {target.name}: migration failed; not starting")
            return 1
        print(f"[migrate] {target.name}: at head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
