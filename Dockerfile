FROM python:3.12-slim

# Install uv so the image is built from the committed lock — the exact
# artifact CI tests (uv sync --frozen). pip install . would re-resolve every
# ">=" bound at build time and could silently pull an untested
# slowquery-detective that breaks the monkeypatch shims (REL-1 / P12).
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src ./src
COPY alembic ./alembic
COPY scripts ./scripts

# --frozen: never re-resolve; fail if the lock is stale. --no-dev: skip test/lint deps.
RUN uv sync --frozen --no-dev

# Run as a non-root user so an RCE/escape in a dependency isn't root (SEC-2).
RUN adduser --disabled-password --gecos "" app && chown -R app:app /app
USER app

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "slowquery_demo.main:app", "--host", "0.0.0.0", "--port", "8000"]
