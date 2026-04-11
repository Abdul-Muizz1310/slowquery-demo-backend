#!/usr/bin/env bash
# One-shot secret propagation from workspace .env to GitHub Actions.
# Run after `gh repo create` and whenever secrets rotate.
set -euo pipefail

source "$(dirname "$0")/../../.env"
REPO=Abdul-Muizz1310/slowquery-demo-backend

# Neon
gh secret set DATABASE_URL       --repo "$REPO" --body "$NEON_DB_URL_SLOWQUERY"
gh secret set DATABASE_URL_FAST  --repo "$REPO" --body "$NEON_DB_URL_SLOWQUERY_FAST"
gh secret set NEON_API_KEY       --repo "$REPO" --body "$NEON_API_KEY"
gh secret set NEON_PROJECT_ID    --repo "$REPO" --body "$NEON_PROJECT_ID"

# OpenRouter (LLM fallback for the rules-engine explainer)
gh secret set OPENROUTER_API_KEY      --repo "$REPO" --body "$OPENROUTER_API_KEY"
gh secret set OPENROUTER_BASE_URL     --repo "$REPO" --body "$OPENROUTER_BASE_URL"
gh secret set OPENROUTER_MODEL_PRIMARY --repo "$REPO" --body "$OPENROUTER_MODEL_PRIMARY"

# Render deploy hook (fill RENDER_DEPLOY_HOOK_SLOWQUERY in workspace .env after Render service exists)
if [[ -n "${RENDER_DEPLOY_HOOK_SLOWQUERY:-}" ]]; then
  gh secret set RENDER_DEPLOY_HOOK --repo "$REPO" --body "$RENDER_DEPLOY_HOOK_SLOWQUERY"
else
  echo "note: RENDER_DEPLOY_HOOK_SLOWQUERY not set in workspace .env; skipping (deploy job will no-op)."
fi
