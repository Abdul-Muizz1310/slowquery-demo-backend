"""Push GitHub Actions secrets from the workspace ``.env`` file.

Replaces the original ``scripts/set-secrets.sh`` which used ``bash
source`` and broke on:

1. URL values containing ``&`` (e.g. ``sslmode=require&channel_binding=...``)
   — bash treats ``&`` as a background-command separator.
2. Lines with trailing decorative comments containing non-ASCII characters
   (``→`` / ``—``) — Windows stdout defaults to cp1252 and chokes.

This script parses ``.env`` as UTF-8, strips inline comments (``# …`` after
whitespace), and pipes each value to ``gh secret set`` via stdin as raw
bytes so neither the shell nor the Windows console codec touches the value.

Run after ``gh repo create`` and whenever secrets rotate. Re-runs are
idempotent — GitHub overwrites the previous secret value.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = "Abdul-Muizz1310/slowquery-demo-backend"

# (gh_secret_name, workspace_env_key, optional_transform)
# The Neon URLs are stored in .env as plain ``postgresql://`` so asyncpg
# drivers get the right dialect at runtime via a ``+asyncpg`` rewrite.
_TO_ASYNCPG = "to_asyncpg"
SECRETS: list[tuple[str, str, str | None]] = [
    ("DATABASE_URL", "NEON_DB_URL_SLOWQUERY", _TO_ASYNCPG),
    ("DATABASE_URL_FAST", "NEON_DB_URL_SLOWQUERY_FAST", _TO_ASYNCPG),
    ("NEON_API_KEY", "NEON_API_KEY", None),
    ("NEON_PROJECT_ID", "NEON_PROJECT_ID", None),
    ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY", None),
    ("OPENROUTER_BASE_URL", "OPENROUTER_BASE_URL", None),
    ("OPENROUTER_MODEL_PRIMARY", "OPENROUTER_MODEL_PRIMARY", None),
]
# Optional; skipped silently if the env var is empty.
OPTIONAL: list[tuple[str, str, str | None]] = [
    ("RENDER_DEPLOY_HOOK", "RENDER_DEPLOY_HOOK_SLOWQUERY", None),
]


def _strip_inline_comment(value: str) -> str:
    """Return the part of a ``.env`` value before any whitespace-then-``#``.

    ``FOO=bar # comment`` -> ``bar``
    ``FOO=bar#still-value`` -> ``bar#still-value`` (no leading whitespace)
    ``FOO=bar`` -> ``bar``
    """
    out: list[str] = []
    in_value = True
    for i, ch in enumerate(value):
        if in_value and ch.isspace():
            # Look ahead: if the next non-space char is ``#`` it's a comment.
            rest = value[i:].lstrip()
            if rest.startswith("#"):
                break
            out.append(ch)
            continue
        out.append(ch)
    return "".join(out).rstrip()


def parse_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if not line or line.lstrip().startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = _strip_inline_comment(value).strip().strip('"').strip("'")
            env[key] = value
    return env


def to_asyncpg(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def push_secret(name: str, value: str) -> None:
    """Pipe ``value`` to ``gh secret set`` as raw UTF-8 bytes."""
    result = subprocess.run(
        ["gh", "secret", "set", name, "--repo", REPO],
        input=value.encode("utf-8"),
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"gh secret set {name} failed: {stderr}")


def main() -> int:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        print(f"error: workspace .env not found at {env_path}", file=sys.stderr)
        return 1

    env = parse_env(env_path)

    missing: list[str] = []
    for gh_name, key, _ in SECRETS:
        if not env.get(key):
            missing.append(f"{gh_name} <- {key}")
    if missing:
        print("error: missing values in .env for:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        return 1

    # Push required secrets.
    for gh_name, key, transform in SECRETS:
        value = env[key]
        if transform == _TO_ASYNCPG:
            value = to_asyncpg(value)
        try:
            push_secret(gh_name, value)
        except RuntimeError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        sys.stdout.buffer.write(f"ok: {gh_name}\n".encode())

    # Push optional secrets.
    for gh_name, key, _ in OPTIONAL:
        value = env.get(key, "")
        if not value:
            sys.stdout.buffer.write(f"skip: {gh_name} (workspace .env has {key}= empty)\n".encode())
            continue
        try:
            push_secret(gh_name, value)
        except RuntimeError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        sys.stdout.buffer.write(f"ok: {gh_name}\n".encode())

    sys.stdout.buffer.write(b"done.\n")
    return 0


if __name__ == "__main__":
    # Guarantee UTF-8 on stdout even when PYTHONIOENCODING isn't set.
    if sys.platform == "win32":
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
