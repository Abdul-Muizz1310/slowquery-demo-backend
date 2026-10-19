# Spec 13 — Migrations on deploy (0002 dedupe + every branch at boot)

## Goal

Two defects, one fix.

1. **An applied migration was edited.** `0001_initial.py` was applied to both live Neon branches on
   2026-04-12. On 2026-04-13 (`133bb7d`) the unique constraint `uq_suggestions_fp_kind_sql` was added to
   *that same file*, and the store writer's insert was changed to
   `ON CONFLICT (fingerprint_id, kind, sql) DO NOTHING` in the same commit. Alembic never re-runs a
   revision it has recorded, so the live branches never got the constraint: duplicate suggestions
   accumulated (the live `/_slowquery/queries` showed `ix_orders_created_at` three times on one
   fingerprint), and every later suggestion insert failed outright, because `ON CONFLICT (cols)` needs a
   matching unique constraint. Fresh databases (the Testcontainers lane) got the constraint from the edited
   `0001`, so tests never saw either symptom.
2. **Migrations never run on deploy.** `render.yaml` declares `preDeployCommand: alembic upgrade head`,
   which Render's free plan ignores. The live schema only moves when someone runs alembic by hand.

The fix is a new revision `0002` that removes the duplicates and adds the constraint only where it is
missing, plus a container entrypoint that applies migrations to **every database the service uses**
before uvicorn starts. `0001` is not touched again.

## Migration `0002_dedupe_suggestions`

- `revision = "0002"`, `down_revision = "0001"`.
- `upgrade()`, inside alembic's transaction:
  1. `LOCK TABLE suggestions IN SHARE ROW EXCLUSIVE MODE`: an instance still serving the previous release
     cannot insert a fresh duplicate between the delete and the constraint.
  2. Delete every row that has a twin with a lower `id` and the same `(fingerprint_id, kind, sql)`. `sql`
     is nullable, so it is compared with `IS NOT DISTINCT FROM`: two `NULL`s are duplicates too. The
     lowest `id` of each group survives; rows without a twin are untouched.
  3. Add `uq_suggestions_fp_kind_sql UNIQUE (fingerprint_id, kind, sql)` **only if** no constraint of that
     name exists on `suggestions` (a database created from the edited `0001` already has it).
- `downgrade()` is a no-op. The deleted duplicates cannot be restored, and the constraint belongs to
  `0001` on every fresh database, so `0001`'s downgrade (which drops it) keeps working on both kinds of
  database once `0002` has run.

## Migrating every branch at boot

The service talks to up to three Postgres URLs: `DATABASE_URL` (the `slowquery` branch),
`DATABASE_URL_FAST` (the `slowquery-fast` branch; `POST /branches/switch` swaps the commerce engine onto
it, and the dashboard reads the bookkeeping tables through that engine), and `SLOWQUERY_STORE_URL` (where
the store writer persists; defaults to `DATABASE_URL`). Each one carries the full schema, so each one is
migrated.

```python
# scripts/migrate_all.py
@dataclass(frozen=True)
class MigrationTarget:
    name: str   # the env var it came from, e.g. "DATABASE_URL_FAST" — never the URL itself
    url: str

def migration_targets(env: Mapping[str, str]) -> list[MigrationTarget]: ...  # pure
def main(argv: list[str] | None = None) -> int: ...                          # runs alembic per target
```

- `migration_targets` reads `DATABASE_URL` (required), `DATABASE_URL_FAST` and `SLOWQUERY_STORE_URL`
  (optional), in that order. Blank values count as unset. Values are stripped of edge whitespace and a
  leading byte-order mark. A URL that is not Postgres (`postgres://`, `postgresql://`,
  `postgresql+asyncpg://`) is a configuration error. Two names that point at the same database (same URL
  after `normalise_asyncpg_url`) are migrated once, under the first name.
- `main` runs `python -m alembic upgrade head` once per target, in a subprocess with `DATABASE_URL` set to
  that target (alembic's `env.py` reads only `DATABASE_URL`). It prints one line per target naming the env
  var, never the URL (it holds the password), and stops at the first failure with exit code `1`. A
  configuration error also exits `1`, before any migration runs.
- `docker-entrypoint.sh` runs `python -m scripts.migrate_all` under `set -e`, then `exec "$@"` (the
  Dockerfile `CMD`, uvicorn). A failed migration aborts startup, so Render keeps the previous deploy
  serving instead of starting a server against a half-migrated schema.
- `render.yaml` no longer declares `preDeployCommand`.
- Every boot re-runs `upgrade head`; on a database already at head that is a no-op.

## Invariants

1. `0001_initial.py` is byte-identical to its committed state before this spec (never edit an applied
   migration).
2. After `upgrade head`, every database has exactly one `uq_suggestions_fp_kind_sql`, whichever path
   created it.
3. Running `upgrade head` again, or re-running `0002` itself (`stamp 0001` then `upgrade head`), changes
   nothing and does not fail.
4. `0002` never deletes the lowest-`id` row of a group, and never deletes a row that has no duplicate.
5. A database URL never appears in the migration runner's output.
6. The server never starts after a failed migration.

## Test cases

**Success (unit — no database):**
1. `migration_targets` with only `DATABASE_URL` → one target, `DATABASE_URL`.
2. All three set to different databases → three targets in order `DATABASE_URL`, `DATABASE_URL_FAST`,
   `SLOWQUERY_STORE_URL`.
3. `SLOWQUERY_STORE_URL` equal to `DATABASE_URL`, including once spelled as libpq
   (`postgresql://…?sslmode=require`) and once as asyncpg (`postgresql+asyncpg://…?ssl=require`) → migrated
   once, as `DATABASE_URL`.
4. A blank `DATABASE_URL_FAST` is skipped; a value wrapped in a BOM and a trailing newline is cleaned.
5. `main` runs alembic once per target with that target's URL as `DATABASE_URL` and prints the env-var
   names, not the URLs.
6. The `0002` module declares `revision "0002"` and `down_revision "0001"`, and the no-index guard (spec 00
   test 16) covers every file under `alembic/versions/`.
7. `0001_initial.py` still matches its committed hash (invariant 1).

**Failure (unit):**
8. `DATABASE_URL` unset or blank → `MigrationConfigError` naming `DATABASE_URL`; `main` exits `1` without
   running alembic.
9. A non-Postgres URL (`sqlite:///x.db`, `mysql://…`) under any of the three names → `MigrationConfigError`
   naming that variable and not echoing the URL.
10. The first target's alembic exits non-zero → `main` exits `1` and the second target is never migrated.

**Entrypoint (unit — the real script, a stub `python` on `PATH`):**
11. The entrypoint runs the migration runner, then execs its arguments.
12. A failing migration runner → the entrypoint exits non-zero and never execs its arguments.
13. The Dockerfile uses the entrypoint; `render.yaml` has no `preDeployCommand`.

**Integration (Testcontainers Postgres):**
14. Fresh database: `upgrade head` ends at `0002` with exactly one `uq_suggestions_fp_kind_sql`.
15. Live-shaped database (at `0001`, constraint dropped, duplicate rows inserted, including a `NULL`-`sql`
    pair and rows that are unique): `upgrade head` keeps the lowest `id` of each group, keeps every unique
    row, and adds the constraint.
16. After 15, `PostgresStoreWriter.insert_suggestions` with an already-stored suggestion succeeds and adds
    no row; a raw duplicate insert raises a unique violation.
17. `upgrade head` twice in a row, and `stamp 0001` + `upgrade head`, both succeed and change nothing.
18. `downgrade base` after `upgrade head` works on both the fresh and the live-shaped database.
19. `scripts.migrate_all` against two separate Postgres databases (slow and fast) brings both to `0002`.

**Composed (CI `build` job):** the built image, started with `DATABASE_URL` and `DATABASE_URL_FAST`
pointing at two empty Postgres services, migrates both to `0002` and answers `/health` with `db: ok`.

## Acceptance

- [ ] `alembic/versions/0002_dedupe_suggestions.py`; `0001_initial.py` unchanged.
- [ ] `scripts/migrate_all.py`, `docker-entrypoint.sh`, Dockerfile `ENTRYPOINT`.
- [ ] `render.yaml` without `preDeployCommand`; DEVIATIONS §6 and `RENDER_FREE_TIER_MIGRATIONS.md` describe
      what ships.
- [ ] Every case above has a test.
