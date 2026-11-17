"""Visitor identity for the public widget path, and forgetting a person.

Pure pieces, no FastAPI: api.py imports them and the tests exercise them
without the app.

Identity model. A widget visitor is one browser. On the first session the
server mints a token, the widget stores it in localStorage and sends it
back on every later session. The token is `<rand>.<sig>` where sig is an
HMAC over `<deployment_id>:<rand>`, so a client can only ever present an
identity this server minted FOR THIS deployment: it cannot pick its own
user_id (the old client-chosen UUID let any page read any memory bucket),
and a token minted for one published agent is rejected on another. An
invalid token is never an error: the visitor simply becomes a new one,
because a widget must keep working across a secret rotation.

What signing does not do: it cannot stop someone who copies the token
out of the host page's localStorage (XSS on the customer's site, a shared
device) from being that visitor. Only the site's own login could, and
that is explicitly out of scope.

Channel bridges (arya-whatsapp) are the one caller allowed to NAME the
customer (`channel` + `external_user_id`, a phone number). They prove it
with a shared bridge token header, so a browser holding a public key
cannot claim a WhatsApp customer's identity.
"""

import base64
import hashlib
import hmac
import os
import secrets

# 24 hex chars = 96 bits of the HMAC: plenty against forgery, short enough
# that the whole token stays under 50 chars in localStorage.
_SIG_HEX = 24
_MAX_TOKEN_LEN = 80


def visitor_secret() -> bytes:
    """ARYA_VISITOR_SECRET, or a secret derived from the LiveKit secret so
    installs that never re-ran scripts/init.sh keep stable tokens across
    restarts instead of forgetting every visitor on each boot."""
    explicit = os.environ.get("ARYA_VISITOR_SECRET", "").strip()
    if explicit:
        return explicit.encode()
    base = os.environ.get("LIVEKIT_API_SECRET", "").encode()
    return hmac.new(base, b"arya-visitor-v1", hashlib.sha256).digest()


def bridge_token() -> str:
    """The shared secret a channel bridge presents in X-Arya-Bridge-Token.
    ARYA_BRIDGE_TOKEN, else derived from the LiveKit secret (both the api
    and arya-whatsapp get that from compose, so an install that only
    rebuilt keeps WhatsApp working). Empty when neither exists, which
    means no caller can act as a bridge."""
    explicit = os.environ.get("ARYA_BRIDGE_TOKEN", "").strip()
    if explicit:
        return explicit
    base = os.environ.get("LIVEKIT_API_SECRET", "")
    if not base:
        return ""
    return hmac.new(base.encode(), b"arya-bridge-v1", hashlib.sha256).hexdigest()


def is_bridge_request(header_value: str | None) -> bool:
    expected = bridge_token()
    if not expected or not header_value:
        return False
    return hmac.compare_digest(header_value.strip(), expected)


def _sign(deployment_id, rand: str) -> str:
    msg = f"{deployment_id}:{rand}".encode()
    return hmac.new(visitor_secret(), msg, hashlib.sha256).hexdigest()[:_SIG_HEX]


def mint_token(deployment_id) -> str:
    rand = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
    return f"{rand}.{_sign(deployment_id, rand)}"


def verify_token(deployment_id, token: str | None) -> str | None:
    """The token's `rand` when it was minted here for this deployment,
    else None. Never raises: a malformed token is just not ours."""
    if not token or not isinstance(token, str) or len(token) > _MAX_TOKEN_LEN:
        return None
    rand, sep, sig = token.partition(".")
    if not sep or not rand or len(sig) != _SIG_HEX:
        return None
    if not hmac.compare_digest(sig, _sign(deployment_id, rand)):
        return None
    return rand


def derive_user_id(deployment_id, raw: str) -> str:
    """The conversations.metadata user_id: namespaced per deployment so one
    site's key can never probe another site's memories. Same 16-hex shape
    History has always stored."""
    return hashlib.sha256(f"{deployment_id}:{raw}".encode()).hexdigest()[:16]


def valid_user_id(user_id: str) -> bool:
    """Widget ids are 16 hex chars; builder test calls store the browser's
    raw UUID. Anything else is not an id this product ever wrote."""
    return 1 <= len(user_id) <= 64 and all(
        c.isalnum() or c in "-_" for c in user_id
    )


# ─── SQL ─────────────────────────────────────────────────────────────────────

# One row per person who has talked to a published agent (or a builder test
# call with a browser id). Grouped from conversations.metadata, so a visitor
# exists exactly as long as at least one of their calls does.
LIST_SQL = """
SELECT c.metadata->>'user_id' AS user_id,
       COUNT(*)::int AS calls,
       MIN(c.started_at) AS first_seen,
       MAX(c.started_at) AS last_seen,
       array_remove(array_agg(DISTINCT c.metadata->>'agent_name'), NULL)
         AS agent_names,
       MAX(c.metadata->>'channel') AS channel,
       MAX(c.metadata->>'external_user') AS external_user
FROM conversations c
WHERE c.metadata->>'user_id' IS NOT NULL
  AND ($1::text IS NULL OR c.metadata->>'agent_id' = $1)
GROUP BY 1
ORDER BY last_seen DESC
LIMIT $2
"""


async def purge_conversation_rows(conn, conversation_id, room_name: str | None) -> None:
    """Every row one call left behind, then the call itself. No FK cascade
    exists, so the order is children first. rag_events is keyed by room
    (it holds the caller's own queries); turn_metrics stays, it is numbers
    only and the dashboard aggregates over it. `conn` is a pool or a
    connection, whichever the caller holds."""
    await conn.execute(
        "DELETE FROM messages WHERE conversation_id = $1", conversation_id
    )
    await conn.execute(
        "DELETE FROM artifacts WHERE conversation_id = $1", conversation_id
    )
    await conn.execute(
        "DELETE FROM tool_events WHERE conversation_id = $1", conversation_id
    )
    if room_name:
        await conn.execute(
            "DELETE FROM rag_events WHERE room_name = $1", room_name
        )
    await conn.execute(
        "DELETE FROM conversations WHERE id = $1", conversation_id
    )


async def forget_visitor_rows(pool, user_id: str) -> dict:
    """Delete every Postgres row about one visitor, atomically. Returns
    counts; a second call returns zeros. Channel thread and event rows are
    removed only for the deployments this visitor actually talked to: the
    same phone number talking to another published agent is, by the
    per-deployment hashing, a different visitor."""
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            "SELECT id, room_name, deployment_id,"
            " metadata->>'external_user' AS external_user"
            " FROM conversations WHERE metadata->>'user_id' = $1",
            user_id,
        )
        for r in rows:
            await purge_conversation_rows(conn, r["id"], r["room_name"])
        threads = 0
        pairs = {
            (r["external_user"], r["deployment_id"])
            for r in rows
            if r["external_user"] and r["deployment_id"]
        }
        for external, dep_id in pairs:
            await conn.execute(
                "DELETE FROM channel_events e USING channels ch"
                " WHERE e.channel_id = ch.id AND ch.deployment_id = $2"
                " AND e.external_user = $1",
                external, dep_id,
            )
            res = await conn.execute(
                "DELETE FROM channel_threads t USING channels ch"
                " WHERE t.channel_id = ch.id AND ch.deployment_id = $2"
                " AND t.external_user = $1",
                external, dep_id,
            )
            threads += int(res.rsplit(" ", 1)[-1] or 0)
    return {"conversations_deleted": len(rows), "channel_threads_deleted": threads}
