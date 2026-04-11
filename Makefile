.PHONY: run test lint format typecheck check

run:
	uv run uvicorn slowquery_demo.main:app --reload

test:
	uv run pytest -m "not slow and not integration"

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy src/

check: lint typecheck test
	uv run ruff format --check .
