#!/usr/bin/env bash
# Thin wrapper around scripts/set_secrets.py.
#
# The original bash implementation ``source``d the workspace .env, which
# broke on two real-world .env shapes:
#   1. URL values containing ``&`` (e.g. sslmode=require&channel_binding=...)
#      - bash interprets ``&`` as a background-command separator.
#   2. Lines with trailing decorative comments containing non-ASCII
#      characters - Windows stdout defaults to cp1252 and chokes.
# The Python script handles both cases and pipes secrets to ``gh secret set``
# as raw UTF-8 bytes via stdin.
set -euo pipefail

exec uv run python "$(dirname "$0")/set_secrets.py" "$@"
