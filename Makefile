.PHONY: run test floors lint format typecheck check

run:
	uv run uvicorn slowquery_demo.main:app --reload

test:
	uv run pytest -m "not slow and not integration"

# Per-module coverage floors. coverage.py's --fail-under is global only, so each
# module the demo depends on gets its own gate. Floors sit below the measured
# value with headroom; raise one only after the number has actually moved.
# Run after `make test` — it reads the .coverage file pytest-cov wrote.
floors:
	uv run coverage report --include="src/slowquery_demo/api/routers/dashboard.py" --fail-under=85
	uv run coverage report --include="src/slowquery_demo/core/access.py" --fail-under=90
	uv run coverage report --include="src/slowquery_demo/core/platform.py" --fail-under=90
	uv run coverage report --include="src/slowquery_demo/core/observability.py" --fail-under=60
	uv run coverage report --include="src/slowquery_demo/services/store.py" --fail-under=85
	uv run coverage report --include="src/slowquery_demo/services/branch_switcher.py" --fail-under=95
	uv run coverage report --include="src/slowquery_demo/schemas/*.py" --fail-under=90
	uv run coverage report --include="src/slowquery_demo/models/*.py" --fail-under=95
	uv run coverage report --include="src/slowquery_demo/api/routers/*.py" --fail-under=85
	uv run coverage report --include="src/slowquery_demo/services/*.py" --fail-under=90

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy src/

check: lint typecheck test
	uv run ruff format --check .
