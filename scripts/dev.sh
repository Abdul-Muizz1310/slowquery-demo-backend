#!/usr/bin/env bash
# Local dev entry point for slowquery-demo-backend.
set -euo pipefail

cd "$(dirname "$0")/.."
uv sync --all-extras
uv run alembic upgrade head
exec uv run uvicorn slowquery_demo.main:app --reload --host 0.0.0.0 --port "${PORT:-8000}"
