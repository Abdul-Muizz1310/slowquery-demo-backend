"""Persist the active branch across process restarts.

The branch switcher writes ``slow`` or ``fast`` to a single-line file
(``BRANCH_STATE_FILE`` env var, default ``.branch_state`` in cwd) and
reloads it on boot. Malformed contents fall back to ``slow`` so a
fresh or corrupted state file never breaks startup.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, cast

BranchName = Literal["slow", "fast"]

_VALID: frozenset[str] = frozenset({"slow", "fast"})


def _state_file() -> Path:
    return Path(os.environ.get("BRANCH_STATE_FILE", ".branch_state"))


def load_branch() -> BranchName:
    """Return the persisted branch, defaulting to ``slow`` on any error."""
    path = _state_file()
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return "slow"
    if raw not in _VALID:
        return "slow"
    return cast(BranchName, raw)


def save_branch(name: BranchName) -> None:
    """Persist the active branch name."""
    if name not in _VALID:
        raise ValueError(f"invalid branch: {name}")
    path = _state_file()
    path.write_text(f"{name}\n", encoding="utf-8")
