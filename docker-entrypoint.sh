#!/bin/sh
# Container entrypoint: migrate every database, then start the server (spec 13).
#
# Render's free plan ignores a Blueprint `preDeployCommand`, so the container is
# the only place a migration can run on every deploy. `scripts.migrate_all`
# applies `alembic upgrade head` to DATABASE_URL (slow branch), DATABASE_URL_FAST
# (fast branch) and SLOWQUERY_STORE_URL when it names another database. On a
# database already at head that is a no-op, so every cold boot can run it.
#
# A failed migration aborts startup on purpose: Render keeps the previous deploy
# serving rather than starting a server against a half-migrated schema.
set -e

echo "[entrypoint] migrating every configured database"
python -m scripts.migrate_all
echo "[entrypoint] migrations applied; starting: $*"

exec "$@"
