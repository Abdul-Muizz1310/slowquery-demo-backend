# Spec 10 — Branch switch engine rebuild (closing DEVIATIONS §3)

## Goal

Wire the `engine_builder` callable into `BranchSwitcher` so `POST /branches/switch` actually rebuilds the `AsyncEngine` and `async_sessionmaker` against the target branch's URL. Subsequent queries route to the new branch.

## Close path (from DEVIATIONS.md §3)

1. Build a new `AsyncEngine` against the target URL via `build_engine(url)`
2. Run `SELECT 1` health check with 5s timeout
3. If fail: dispose new engine, return 503
4. If success: atomically swap `app.state.engine` + `app.state.db_sessionmaker`
5. Dispose old engine with 5s grace window for in-flight queries

## Test cases
1. After switch to "fast", a query through `get_db` uses the fast branch URL
2. After switch to "slow", queries route back to the slow branch URL
3. `SELECT 1` failure on the new engine returns 503 and keeps the old engine
4. Concurrent switch requests are serialized (only one completes)
5. The old engine is disposed after the grace period

## Acceptance criteria
- [ ] `services/branch_switcher.py` accepts and calls `engine_builder`
- [ ] `main.py` passes the engine_builder closure
- [ ] 5 unit tests in `tests/unit/test_10_engine_rebuild.py`
- [ ] Lint + mypy + tests green
