#!/usr/bin/env sh
# Resets every editable catalog (model lists, connector types, search
# providers, ...) back to whatever backend/src/catalog.py currently ships.
#
# Why this exists: catalogs seed into Postgres ONCE, on an engine's first
# boot, and are never auto-updated after that. A plain `git pull &&
# docker compose up -d --build` changes the code's fallback seed but NOT
# the row already sitting in the database, so an old engine keeps showing
# stale connector text, old model lists, old defaults, forever, until this
# runs. Run it after every such rebuild. See docs/DEPLOYMENT.md, "Updating
# the engine".
#
# Idempotent: resetting a catalog that did not change is a harmless no-op.
# The one thing it discards is a catalog ever hand-edited through
# PUT /api/catalogs/{name} (the admin "edit catalog" API) instead of code;
# nobody does that on a normal engine.
#
# Reads PUBLIC_BASE_URL and ARYA_ADMIN_TOKEN from .env, so nothing needs
# to be typed by hand or pasted wrong.

set -eu
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  # shellcheck disable=SC1091
  . ./.env
fi
BASE="${PUBLIC_BASE_URL:-http://localhost:8000}"

# Every top-level key in backend/src/catalog.py's SEEDS dict. Keep this
# list in sync if a new catalog is ever added there.
CATALOGS="openai_models openai_defaults groq_defaults gemini_defaults gemini_models local_defaults local_backends search_providers prompt_blocks rag_tuning industries embedding_profiles tool_catalog connectors"

for name in $CATALOGS; do
  if [ -n "${ARYA_ADMIN_TOKEN:-}" ]; then
    out=$(curl -s -X POST "$BASE/api/catalogs/$name/reset" -H "X-Arya-Admin-Token: $ARYA_ADMIN_TOKEN")
  else
    out=$(curl -s -X POST "$BASE/api/catalogs/$name/reset")
  fi
  printf '%s <- %s\n' "$(printf '%s' "$out" | cut -c1-80)" "$name"
done

echo "done"
