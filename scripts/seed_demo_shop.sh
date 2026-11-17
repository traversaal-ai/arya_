#!/usr/bin/env sh
# Create, or reset, the demo shop database for the WhatsApp support demo.
#
# Runs demo/shop/seed.sql inside the compose Postgres container as the
# superuser, creates the least-privilege shop_agent role with a fresh
# password, and prints the connection string to put in .env. Safe to run
# again at any time: the SQL drops and recreates the whole database, so
# this is also the reset between demos.
#
# It never edits .env itself. Copy the printed line in by hand, so nothing
# about your secrets changes without you seeing it.
#
#   SHOP_AGENT_PASSWORD=...  reuse a password instead of generating one,
#                            so a string already in .env keeps working
#                            after a reset.

set -eu

cd "$(dirname "$0")/.."

if ! docker compose ps --status running postgres >/dev/null 2>&1 \
   || [ -z "$(docker compose ps -q --status running postgres)" ]; then
    echo "postgres is not running. Start the stack first:" >&2
    echo "  docker compose up -d" >&2
    exit 1
fi

PW="${SHOP_AGENT_PASSWORD:-$(openssl rand -hex 16)}"

echo "seeding shopdemo ..."
docker compose exec -T postgres \
    psql -U arya -d arya -q -v ON_ERROR_STOP=1 -v pw="$PW" \
    < demo/shop/seed.sql

cat <<EOF

shopdemo is ready.

Connect it to an agent: Options, Connectors, Add database, paste this
string, then attach the connection to the agent. The check and the agent
both run in containers, and host.docker.internal reaches this Postgres
from both:

postgresql://shop_agent:${PW}@host.docker.internal:5432/shopdemo

Same database in Supabase later: paste its connection string instead,
with the same shop_agent role created there, and add ?sslmode=require.

Fallback for an install without the connector, in .env (the worker is
host-networked, so localhost is right there), then
docker compose up -d agent-worker:

ARYA_SHOP_DSN=postgresql://shop_agent:${PW}@localhost:5432/shopdemo

Look inside at any time:
  docker compose exec postgres psql -U arya -d shopdemo -c 'select order_ref, status from orders'
  docker compose exec postgres psql -U arya -d shopdemo -c 'select return_ref, reason, status from returns'

Before a class: replace the two PLACEHOLDER phones in demo/shop/seed.sql
with the other registered Meta test numbers, then run this again.
EOF
