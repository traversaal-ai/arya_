"""App database layer (Postgres `arya`): pool + schema bootstrap.

Shared by the API (ingestion status, conversations) and the agent worker
(conversation persistence).
"""

import os

import asyncpg

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_base (
    id UUID PRIMARY KEY,
    filename TEXT NOT NULL,
    content_type TEXT,
    size_bytes BIGINT,
    object_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_upload',
    error TEXT,
    chunk_count INT DEFAULT 0,
    timings JSONB,
    embedding_profile TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS progress INT DEFAULT 0;
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY,
    room_name TEXT NOT NULL,
    started_at TIMESTAMPTZ DEFAULT now(),
    ended_at TIMESTAMPTZ,
    metadata JSONB
);
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    conversation_id UUID REFERENCES conversations(id),
    role TEXT NOT NULL,
    content TEXT,
    citations JSONB,
    metrics JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS catalogs (
    name TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS deployments (
    id UUID PRIMARY KEY,
    public_key TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    allowed_origins JSONB NOT NULL DEFAULT '[]'::jsonb,
    branding JSONB NOT NULL DEFAULT '{}'::jsonb,
    max_concurrent_sessions INT NOT NULL DEFAULT 3,
    max_sessions_per_day INT NOT NULL DEFAULT 100,
    max_session_seconds INT NOT NULL DEFAULT 600,
    sessions_started BIGINT NOT NULL DEFAULT 0,
    last_session_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    revoked_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS deployments_agent_uniq ON deployments (agent_id);
-- Refused session starts (quota or rate limit), aggregated per minute.
-- A 429 creates no conversation row, so without this table an owner whose
-- visitors are being turned away has no way to discover it in the product.
-- Aggregation bounds growth under attack: one row per deployment, reason
-- and minute, however fast someone hammers the endpoint.
CREATE TABLE IF NOT EXISTS session_refusals (
    id BIGSERIAL PRIMARY KEY,
    deployment_id UUID NOT NULL,
    reason TEXT NOT NULL,
    count INT NOT NULL DEFAULT 1,
    window_start TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS session_refusals_uniq
    ON session_refusals (deployment_id, reason, window_start);
CREATE INDEX IF NOT EXISTS session_refusals_window_idx
    ON session_refusals (window_start DESC);
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS deployment_id UUID;
CREATE INDEX IF NOT EXISTS conversations_deploy_idx
    ON conversations (deployment_id, started_at DESC);
CREATE INDEX IF NOT EXISTS conversations_room_idx ON conversations (room_name);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS options JSONB;
-- The one verified link an answer gave (links.resolve_links), published on
-- arya.links and stored here for History to render the same chip from
-- stored data. Public pages, so unlike `citations` it needs no visitor
-- gate; see show_sources below for the client-side default instead.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS links JSONB;
-- Presentational, not a security gate (web citations are public pages).
-- Whether a visitor's widget/share-link session shows the collapsed
-- "Sources" control at all. Default on; an owner can turn it off on
-- Publish & deploy for a site that does not want outside links shown.
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS show_sources BOOLEAN NOT NULL DEFAULT true;
-- One row per retrieval attempt (including skips and errors), written by
-- the worker. In Postgres rather than the old per-container JSONL because
-- the Insights API runs in a different container and JSONL files there
-- were unreachable and wiped on every rebuild. room_name (not a FK) links
-- back to conversations — a retrieval must be recordable even when the
-- conversation row write failed.
CREATE TABLE IF NOT EXISTS rag_events (
    id BIGSERIAL PRIMARY KEY,
    room_name TEXT,
    agent_id TEXT,
    query TEXT,
    latency_ms INT,
    top_score REAL,
    kept INT,
    injected BOOLEAN NOT NULL DEFAULT FALSE,
    error TEXT,
    skipped TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rag_events_created_idx
    ON rag_events (created_at DESC);
-- Files a lookup injected, so Insights can say which documents are being
-- retrieved and which never are. NULL for rows written before the column.
ALTER TABLE rag_events ADD COLUMN IF NOT EXISTS files JSONB;
-- One row per pipeline stage per turn (EOU, STT, LLM, TTS, VAD, tool
-- calls), written by the worker from the SDK's metrics_collected events.
-- The same events also go to a JSONL file inside the worker container,
-- which the API cannot read and every rebuild wipes; this table is the
-- durable copy the dashboard aggregates per stage. stage_ms is the one
-- headline number per type (EOU delay, STT duration, LLM ttft, TTS ttfb,
-- VAD inference, tool duration); raw keeps the full event for anything
-- the columns do not name, so an SDK upgrade never loses data.
CREATE TABLE IF NOT EXISTS turn_metrics (
    id BIGSERIAL PRIMARY KEY,
    room_name TEXT NOT NULL,
    agent_id TEXT,
    metric_type TEXT NOT NULL,
    speech_id TEXT,
    stage_ms INT,
    duration_ms INT,
    audio_ms INT,
    chars INT,
    tokens_in INT,
    tokens_out INT,
    model TEXT,
    provider TEXT,
    cancelled BOOLEAN NOT NULL DEFAULT FALSE,
    raw JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS turn_metrics_room_idx ON turn_metrics (room_name);
CREATE INDEX IF NOT EXISTS turn_metrics_created_idx
    ON turn_metrics (created_at DESC);
CREATE TABLE IF NOT EXISTS artifacts (
    id UUID PRIMARY KEY,
    conversation_id UUID,
    agent_id TEXT,
    kind TEXT NOT NULL,
    title TEXT,
    html TEXT NOT NULL,
    data JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS artifacts_conv_kind_uniq
    ON artifacts (conversation_id, kind);
-- Connectors (MCP): one row per connected external system, GLOBAL (not per
-- agent); agents attach a connection by id in their config's `connectors`
-- list. In Postgres rather than a data/*.json file because the worker
-- rewrites rows (OAuth token refresh) while the API and other call
-- subprocesses read them: a per-row UPDATE is atomic, a whole-file replace
-- is last-writer-wins. `secret` is plaintext JSONB for now, the same posture
-- as keys.json and the deployments table (encryption is a deliberate
-- follow-up: add an encrypted column, backfill, drop this one).
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT,
    transport TEXT,
    status TEXT NOT NULL DEFAULT 'needs_auth',
    error TEXT,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    secret JSONB,
    oauth_state TEXT,
    oauth_state_expires_at TIMESTAMPTZ,
    tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    last_checked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS connections_oauth_state_idx
    ON connections (oauth_state);
-- One row per MCP tool call made during a call (rag_events twin): the audit
-- trail for actions an agent took in an external system, also what a site
-- owner reads to see what visitors triggered through a public embed.
CREATE TABLE IF NOT EXISTS tool_events (
    id BIGSERIAL PRIMARY KEY,
    room_name TEXT,
    agent_id TEXT,
    conversation_id UUID,
    connection_id TEXT,
    tool TEXT NOT NULL,
    arguments JSONB,
    ok BOOLEAN NOT NULL,
    error TEXT,
    latency_ms INT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tool_events_created_idx
    ON tool_events (created_at DESC);
-- Channels: a messaging surface (WhatsApp today) hanging off a deployment.
-- The deployment stays the unit of publication: it owns the pk_ key, the
-- caps and the agent, so a channel is "this published agent also answers
-- on this number" and inherits every quota and gate the widget has.
-- `secret` is plaintext JSONB, the same posture as connections.secret and
-- keys.json (encryption is a deliberate follow-up for all three at once).
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    deployment_id UUID NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    secret JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    last_event_at TIMESTAMPTZ
);
-- One number serves one channel: the webhook routes inbound messages by
-- phone_number_id, so two rows claiming the same number would be a coin
-- flip over which agent answers.
CREATE UNIQUE INDEX IF NOT EXISTS channels_phone_uniq
    ON channels ((config->>'phone_number_id'))
    WHERE config->>'phone_number_id' IS NOT NULL;
CREATE INDEX IF NOT EXISTS channels_deploy_idx ON channels (deployment_id);
-- One row per person talking to a channel. A WhatsApp thread outlives any
-- single session (people reply hours later), so this maps the customer's
-- number to whichever conversation is open RIGHT NOW, and remembers the
-- last one after it closes so the next message can replay its tail.
CREATE TABLE IF NOT EXISTS channel_threads (
    id BIGSERIAL PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    external_user TEXT NOT NULL,
    display_name TEXT,
    conversation_id UUID,
    room_name TEXT,
    state TEXT NOT NULL DEFAULT 'idle',
    last_inbound_at TIMESTAMPTZ,
    last_outbound_at TIMESTAMPTZ,
    window_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (channel_id, external_user)
);
-- Every message in or out, keyed by the provider's own id. This is the
-- dedupe gate before anything else happens: Meta retries a webhook for up
-- to 7 days when it does not get a 200, and a retried delivery must never
-- produce a second answer. Also the delivery audit trail an operator reads.
CREATE TABLE IF NOT EXISTS channel_events (
    wamid TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_user TEXT,
    conversation_id UUID,
    status TEXT,
    error TEXT,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS channel_events_created_idx
    ON channel_events (channel_id, created_at DESC);
"""


async def get_pool(dsn: str) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=int(os.environ.get("DB_POOL_MIN", "1")),
            max_size=int(os.environ.get("DB_POOL_MAX", "5")),
        )
        async with _pool.acquire() as conn:
            # CREATE TABLE IF NOT EXISTS is not race-free, and the API plus
            # every agent subprocess run this concurrently — serialize the
            # bootstrap behind an advisory lock.
            await conn.execute(
                "SELECT pg_advisory_lock(hashtext('arya_schema'))"
            )
            try:
                await conn.execute(SCHEMA)
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtext('arya_schema'))"
                )
    return _pool
