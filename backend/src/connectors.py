"""Connectors: external systems an agent reaches through MCP tools.

Shared by the API (probe on save, OAuth token exchange) and the worker
(toolsets per call). No FastAPI imports: the worker imports this too.

Model:
  connectors catalog   what KINDS exist (catalog.SEEDS["connectors"]):
                       Custom MCP server, Google Calendar, ...
  connections table    the user's instances, GLOBAL: one Google account,
                       one custom server, each with its credential and the
                       tool list from the last probe (db.py)
  cfg["connectors"]    per-agent attachment: which connections this agent
                       may use, and which of their tools
                       [{"connection_id", "allowed_tools", "public_tools",
                         "purpose"}]

At call time build_toolsets() turns each attachment into a LiveKit
MCPToolset (an MCPServerHTTP with the connection's URL, auth header and the
agent's tool allowlist), connects them all in parallel under one wall-clock
budget, drops the ones that fail (the call still starts), and removes tool
names that collide with the agent's native tools (a duplicate name would
otherwise raise inside Agent.__init__ and kill the call).

HTTP transports only (streamable HTTP preferred, SSE accepted): one worker
subprocess per call makes stdio servers a cold start per call.
"""

import asyncio
import contextlib
import datetime
import functools
import json
import logging
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import urlparse

import httpx

import config
import itinerary_sync

logger = logging.getLogger("connectors")

try:
    from livekit.agents import mcp as lk_mcp
    from livekit.agents.llm import is_function_tool, is_raw_function_tool
    from livekit.agents.llm.tool_context import (
        get_function_info,
        get_raw_function_info,
    )
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client

    MCP_AVAILABLE = True
except ImportError:  # image built before the [mcp] extra was added
    MCP_AVAILABLE = False

MCP_UNAVAILABLE_MSG = (
    "the MCP client is not installed in this build (rebuild the image so"
    " livekit-agents[mcp] is present)"
)

# Wall-clock budget for connecting EVERY attached connector at session
# start, in parallel, so this is the slowest one and not a sum. Runs once per
# call, not per turn.
#
# 4 s was chosen when an agent had one connector, and was too tight the
# moment a travel agent had three: Google Maps alone took 5.6 s on a loaded
# laptop and took Duffel and Composio down with it. Losing a LIVE connector
# is far worse than a slow greeting, because the sample desk it replaced
# stays off (replaced_toolsets keys on attachment, never on health) and the
# agent is left with no tool for that job at all. Seen live: it answered a
# flight search from web_search and quoted prices off a web page. A dead
# server costs one caller this much silence once; a dropped live one costs
# every answer in the call.
SETUP_BUDGET_S = 10.0
CONNECT_TIMEOUT_S = 3.0
# Per tool call: the MCP request itself. This is the OUTER timeout, and it
# must always be longer than whatever the tool uses to reach the vendor, or
# the vendor's readable error never arrives before we give up. Seen live:
# this was 8 s while the Cal.com module waited 10 s, so a slow booking
# reported failure here, completed at the vendor, and the caller's retry
# made a second appointment. Raise it per type with `tool_timeout_s` on the
# catalog entry rather than raising it for everything.
TOOL_TIMEOUT_S = 8.0
# Probe (API side: save, Test): more patient, the user is watching a button.
PROBE_TIMEOUT_S = 10.0
# Spoken tool results: cap what reaches the LLM. Long JSON blows the prompt
# budget and the model starts reading identifiers aloud.
RESULT_MAX_CHARS = 1500
# A structured result from one of Arya's own modules carries a spoken
# sentence plus a machine block the model needs intact (option numbers,
# a trip id, a written summary for an email body), so it gets more room.
STRUCTURED_MAX_CHARS = 4000
# Sidecar modules get the call's conversation id on every request, so a
# booking can be tied to the call that made it (Duffel stamps it into the
# order's metadata and refuses to book the same fare twice for one call).
CONVERSATION_HEADER = "X-Arya-Conversation"
REFRESH_AHEAD_S = 15 * 60  # refresh an OAuth token this close to expiry
# How long a booking confirmation waits for the written page to land.
# Both are scheduled from the same result; the page is the mail's body.
NOTIFY_DELAY_S = 1.5
OAUTH_STATE_TTL_S = 10 * 60

# Case-insensitive because vendors do not agree on case. Composio names
# every tool in upper snake case (GOOGLECALENDAR_CREATE_EVENT,
# GMAIL_SEND_EMAIL), and while this matched only lowercase those all counted
# as reads: pre-ticked on attach, no writes badge, and no duplicate-call
# confirmation. A missed write is the expensive direction to be wrong in.
_WRITE_WORDS = re.compile(
    r"(^|_)(create|add|insert|update|edit|modify|patch|set|delete|remove|"
    r"cancel|send|post|write|book|reschedule|respond|reply|upload|move|"
    r"archive|publish|assign|complete|mark)(_|$)",
    re.IGNORECASE,
)


# ─── registry helpers ───────────────────────────────────────────────────────

def connector_type(type_id: str) -> dict | None:
    return config.connector_type(type_id)


def mint_id() -> str:
    return "conn-" + secrets.token_hex(6)


def server_url(entry: dict, row: dict) -> str:
    """Where this connection's MCP server lives, by `url_source`:
    "fixed"   the catalog's own URL (a vendor-hosted server),
    "sidecar" the arya-mcp service plus this type's path (a server Arya
              hosts itself, for systems nobody hosts one for),
    "user"    the URL the owner pasted."""
    source = entry.get("url_source", "user")
    if source == "fixed":
        return entry["url"]
    if source == "sidecar":
        return config.arya_mcp_url() + entry.get("path", "")
    return (row.get("url") or "").strip()


def is_sidecar(entry: dict) -> bool:
    return entry.get("url_source") == "sidecar"


def transport_for(entry: dict, row: dict) -> str:
    return row.get("transport") or entry.get("transport") or "streamable_http"


def auth_headers(entry: dict, secret: dict | None) -> dict:
    """The request header that carries the credential. Default is
    `Authorization: Bearer <token>`; a catalog entry may name a different
    header (`"header": "x-consumer-api-key"`, Composio) in which case the
    raw credential is sent under that name.

    `auth: "dsn"` is the sidecar case: the connection string travels to
    arya-mcp per request (default header `X-Arya-Dsn`), so that process
    stores no credentials and one instance serves every saved database."""
    secret = secret or {}
    auth = entry.get("auth", "bearer")
    if auth == "oauth":
        token = secret.get("access_token", "")
    elif auth == "dsn":
        token = secret.get("dsn", "")
    else:  # bearer, api_key
        token = secret.get("token", "")
    if not token:
        return {}
    header = (entry.get("header") or "").strip()
    if auth == "dsn" and not header:
        header = "X-Arya-Dsn"
    if header and header.lower() != "authorization":
        return {header: token}
    return {"Authorization": f"Bearer {token}"}


def secret_value(entry: dict, secret: dict | None) -> str:
    """The stored credential as one string, whatever shape its auth uses.
    Used for masking in API responses."""
    secret = secret or {}
    if entry.get("auth") == "dsn":
        return secret.get("dsn", "")
    return secret.get("access_token") or secret.get("token") or ""


def secret_for(entry: dict, credential: str) -> dict | None:
    """The `secret` JSONB for a pasted credential, keyed by auth kind."""
    credential = (credential or "").strip()
    if not credential:
        return None
    return {"dsn": credential} if entry.get("auth") == "dsn" else {"token": credential}


def is_write_tool(name: str, annotations, entry: dict) -> bool:
    """Does this tool change data on the remote side? Catalog list first,
    then the MCP tool annotations, then a name heuristic. Write tools are
    unchecked by default in the UI and confirm-guarded at runtime."""
    if name in (entry.get("write_tools") or []):
        return True
    if annotations is not None:
        ro = getattr(annotations, "readOnlyHint", None)
        if ro is True:
            return False
        if ro is False or getattr(annotations, "destructiveHint", None) is True:
            return True
    return bool(_WRITE_WORDS.search(name))


def row_to_dict(row) -> dict:
    """asyncpg Record -> plain dict with the JSONB columns decoded (the pool
    has no JSON codec registered, same as every other table here)."""
    d = dict(row)
    for k in ("config", "secret", "tools"):
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except json.JSONDecodeError:
                d[k] = None
    d["config"] = d.get("config") or {}
    d["tools"] = d.get("tools") or []
    return d


def _localhost_hint(url: str, err: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1"):
        return (
            f"{err}. From inside Docker, localhost is the api container"
            " itself: use http://host.docker.internal:<port>/... for a"
            " server running on this machine."
        )
    return err


# ─── probe: connect, list tools ─────────────────────────────────────────────

class ProbeError(Exception):
    """A readable reason the server could not be listed (for the UI)."""


async def probe(url: str, transport: str, headers: dict, entry: dict,
                timeout: float = PROBE_TIMEOUT_S) -> list[dict]:
    """Connect to an MCP server and list its tools, as
    [{name, description, write}]. Raises ProbeError with a plain reason.
    Uses the same mcp primitives livekit's MCPServerHTTP uses, so a server
    that lists here is one the worker can talk to."""
    if not MCP_AVAILABLE:
        raise ProbeError(MCP_UNAVAILABLE_MSG)
    if not url.startswith(("http://", "https://")):
        raise ProbeError("server URL must start with http:// or https://")

    async def _list(session: "ClientSession"):
        await session.initialize()
        return await session.list_tools()

    read_timeout = timedelta(seconds=timeout)
    try:
        if transport == "sse":
            async with (
                sse_client(url=url, headers=headers, timeout=timeout,
                           sse_read_timeout=60) as streams,
                ClientSession(streams[0], streams[1],
                              read_timeout_seconds=read_timeout) as session,
            ):
                result = await asyncio.wait_for(_list(session), timeout)
        else:
            async with (
                httpx.AsyncClient(headers=headers, follow_redirects=True,
                                  timeout=httpx.Timeout(timeout, read=60)) as http,
                streamable_http_client(url=url, http_client=http) as streams,
                ClientSession(streams[0], streams[1],
                              read_timeout_seconds=read_timeout) as session,
            ):
                result = await asyncio.wait_for(_list(session), timeout)
    except asyncio.TimeoutError:
        raise ProbeError(_localhost_hint(url, "timed out")) from None
    except Exception as eg:  # often an anyio ExceptionGroup; _describe unwraps
        raise ProbeError(_localhost_hint(url, _describe(eg))) from None

    return [
        {
            "name": t.name,
            "description": (t.description or "").strip()[:200],
            "write": is_write_tool(t.name, getattr(t, "annotations", None), entry),
        }
        for t in result.tools
    ]


def _describe(eg) -> str:
    """One readable line out of a (possibly nested) ExceptionGroup."""
    leaves = []

    def walk(e):
        subs = getattr(e, "exceptions", None)
        if subs:
            for s in subs:
                walk(s)
        else:
            leaves.append(e)

    walk(eg)
    for e in leaves:
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            if code in (401, 403):
                return "server rejected the token"
            return f"server answered http {code}"
        if isinstance(e, httpx.ConnectError):
            return "unreachable"
        if isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)):
            return "timed out"
    for e in leaves:
        text = str(e).strip()
        if text:
            if "401" in text or "Unauthorized" in text:
                return "server rejected the token"
            if "404" in text:
                return "server answered http 404 (wrong path? most servers end in /mcp)"
            return text[:200]
    return "connection failed"


# ─── sidecar (arya-mcp) health and credential checks ────────────────────────

async def sidecar_reachable(timeout: float = 2.0) -> bool:
    """Is the arya-mcp service up? Only for telling the UI why a sidecar
    connector cannot be added yet, never for gating a call."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{config.arya_mcp_url()}/health")
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def check_credential(entry: dict, secret: dict | None) -> dict:
    """Ask the sidecar whether a stored credential actually works.

    Listing MCP tools proves nothing for a sidecar module: the tool list is
    static, so a wrong password would still "connect". The module's own
    check route is what turns a saved connection into a working one, and it
    returns a readable reason plus warnings (a superuser DSN, for example).

    Returns {ok, label?, warnings?, error?}. A type with no check route is
    reported ok, so this stays safe to call for every kind.
    """
    path = entry.get("check_path")
    if not path or not is_sidecar(entry):
        return {"ok": True}
    url = config.arya_mcp_url() + path
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            r = await client.post(url, headers=auth_headers(entry, secret))
    except httpx.HTTPError:
        return {
            "ok": False,
            "error": (
                "the arya-mcp service is not reachable, so this connector"
                " cannot be checked (start it with: docker compose up -d arya-mcp)"
            ),
        }
    if r.status_code >= 500:
        return {"ok": False, "error": f"the arya-mcp service answered http {r.status_code}"}
    try:
        body = r.json()
    except ValueError:
        return {"ok": False, "error": "the arya-mcp service returned an unreadable answer"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "the arya-mcp service returned an unreadable answer"}
    return body


# ─── OAuth (authorization code, refresh) ────────────────────────────────────

def oauth_keys(entry: dict, keys: dict) -> tuple[str, str]:
    names = (entry.get("oauth") or {}).get("keys") or {}
    return (
        keys.get(names.get("client_id", ""), "") or "",
        keys.get(names.get("client_secret", ""), "") or "",
    )


def oauth_ready(entry: dict, keys: dict) -> bool:
    cid, csec = oauth_keys(entry, keys)
    return bool(cid and csec)


def _now() -> float:
    return time.time()


async def oauth_exchange_code(entry: dict, keys: dict, code: str,
                              redirect_uri: str) -> dict:
    """Authorization code -> token secret {access_token, refresh_token,
    expires_at, token_type, scope}."""
    o = entry["oauth"]
    cid, csec = oauth_keys(entry, keys)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(o["token_url"], data={
            "code": code,
            "client_id": cid,
            "client_secret": csec,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
    if r.status_code != 200:
        raise ProbeError(_token_error(r))
    return _token_secret(r.json(), previous=None)


async def oauth_refresh(entry: dict, keys: dict, secret: dict) -> dict:
    o = entry["oauth"]
    cid, csec = oauth_keys(entry, keys)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(o["token_url"], data={
            "refresh_token": secret.get("refresh_token", ""),
            "client_id": cid,
            "client_secret": csec,
            "grant_type": "refresh_token",
        })
    if r.status_code != 200:
        raise ProbeError(_token_error(r))
    return _token_secret(r.json(), previous=secret)


def _token_error(r: httpx.Response) -> str:
    try:
        body = r.json()
        return body.get("error_description") or body.get("error") or f"http {r.status_code}"
    except Exception:
        return f"http {r.status_code}"


def _token_secret(body: dict, previous: dict | None) -> dict:
    expires_in = body.get("expires_in")
    return {
        "access_token": body.get("access_token", ""),
        # Google returns the refresh token only on the first consent; keep
        # the one we have across refreshes.
        "refresh_token": body.get("refresh_token")
        or (previous or {}).get("refresh_token", ""),
        "expires_at": _now() + float(expires_in) if expires_in else None,
        "token_type": body.get("token_type", "Bearer"),
        "scope": body.get("scope", ""),
    }


async def oauth_account_email(entry: dict, secret: dict) -> str:
    url = (entry.get("oauth") or {}).get("userinfo_url")
    if not url or not secret.get("access_token"):
        return ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                url, headers={"Authorization": f"Bearer {secret['access_token']}"}
            )
        if r.status_code == 200:
            return r.json().get("email", "") or ""
    except httpx.HTTPError as e:
        logger.debug(f"userinfo skipped: {e}")
    return ""


def token_stale(secret: dict | None) -> bool:
    exp = (secret or {}).get("expires_at")
    return bool(exp) and exp - _now() < REFRESH_AHEAD_S


async def ensure_fresh_token(pool, row: dict, keys: dict, entry: dict) -> dict | None:
    """For oauth connections: refresh the access token when it is close to
    expiry and persist it. Returns the (possibly updated) row, or None when
    the grant is dead (row flipped to needs_auth) so the caller skips it."""
    if entry.get("auth") != "oauth":
        return row
    secret = row.get("secret") or {}
    if not secret.get("refresh_token"):
        return row if secret.get("access_token") else None
    if not token_stale(secret):
        return row
    try:
        fresh = await oauth_refresh(entry, keys, secret)
    except ProbeError as e:
        logger.warning(f"connector '{row['name']}': token refresh failed: {e}")
        await pool.execute(
            "UPDATE connections SET status='needs_auth', error=$2,"
            " updated_at=now() WHERE id=$1",
            row["id"], f"sign-in expired: {e}",
        )
        return None
    await pool.execute(
        "UPDATE connections SET secret=$2::jsonb, updated_at=now()"
        " WHERE id=$1",
        row["id"], json.dumps(fresh),
    )
    logger.info(f"connector '{row['name']}': token refreshed")
    return {**row, "secret": fresh}


# ─── runtime: toolsets for one call ─────────────────────────────────────────

# One paragraph of a Grounding Lite `summary`, which is the only prose
# the response carries: "**Tuileries Garden** [2] (4.6★, 119446) is an
# expansive, 17th-century formal garden featuring walking trails ...".
# The bracketed number indexes the `places` array, so the prose can be
# joined back to the place it describes.
_MAPS_SUMMARY = re.compile(
    r"\*\*(?P<name>[^*]+?)\*\*\s*\[(?P<idx>\d+)\]"
    r"(?:\s*\((?P<rating>[\d.]+)\s*\u2605\s*,\s*(?P<reviews>[\d,]+)\))?"
    r"\s*(?P<about>[^\n]*)"
)


def _sentence_case(text: str) -> str:
    """"is an expansive, 17th-century garden" -> "An expansive, 17th-century
    garden." Google writes each summary as a predicate; a note on a page is
    a sentence."""
    text = text.strip()
    if text.lower().startswith("is "):
        text = text[3:].lstrip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def shape_maps_places(data: dict) -> dict | None:
    """Grounding Lite's search_places result, rebuilt as the few fields a
    voice agent uses: the name to say, Google's own sentence to write down,
    the rating to compare, and ONE link for the written page.

    Returns None when the payload is not that shape, so an unexpected
    response falls through to the untouched generic path rather than
    arriving empty.
    """
    places = data.get("places")
    if not isinstance(places, list) or not places:
        return None
    prose: dict[int, dict] = {}
    for m in _MAPS_SUMMARY.finditer(str(data.get("summary") or "")):
        prose[int(m.group("idx"))] = {
            "name": m.group("name").strip(),
            "rating": float(m.group("rating")) if m.group("rating") else None,
            "reviews": int(m.group("reviews").replace(",", "")) if m.group("reviews") else None,
            "about": _sentence_case(m.group("about")),
        }
    out = []
    for i, p in enumerate(places):
        if not isinstance(p, dict):
            continue
        said = prose.get(i, {})
        # The place carries no display name of its own. The attribution
        # title is "Tuileries Garden - Google Maps", and the summary
        # paragraph repeats it; either will do, the summary first because
        # it is the name Google chose to write.
        name = said.get("name") or re.sub(
            r"\s*-\s*Google Maps\s*$", "", (p.get("attribution") or {}).get("title", "")
        ).strip()
        if not name:
            continue
        row = {"name": name, "id": p.get("id") or ""}
        if said.get("rating") is not None:
            row["rating"] = said["rating"]
        if said.get("reviews") is not None:
            row["reviews"] = said["reviews"]
        if said.get("about"):
            row["about"] = said["about"]
        # One link, not five. placeUrl is the page for the place itself,
        # which is what Google's attribution terms ask a listing to show.
        url = (p.get("googleMapsLinks") or {}).get("placeUrl") or \
            (p.get("attribution") or {}).get("url") or ""
        if url:
            row["maps_url"] = url
        out.append(row)
    return {"places": out} if out else None


# Per (connector type, tool) reshaping, applied BEFORE the cap. A vendor
# free to answer in whatever shape suits its own API is not free to spend
# a voice agent's whole result budget on identifiers.
SHAPERS = {("google_maps", "search_places"): shape_maps_places}


def spoken_result(ctx, runtime: dict | None = None, connector_type: str = "") -> str:
    """tool_result_resolver: what the model reads from an MCP result.

    Three shapes. A plain result (Cal.com, Composio, any vendor): text
    parts joined, JSON compacted, capped, so a 40 KB listing never reaches
    the model. A result with a SHAPER for its (type, tool): rebuilt first,
    so the cap trims nothing that matters (see shape_maps_places). A
    STRUCTURED result from one of Arya's own modules (Duffel), a dict with
    `say`: the model reads `say` plus the compact `machine` block, and the
    rest is the worker's business. `cards` become option cards on the
    transcript and `itinerary` becomes the written trip page. Neither
    reaches the model, so a page-sized payload costs no tokens and cannot
    be read aloud.
    """
    structured = getattr(ctx.result, "structuredContent", None)
    if isinstance(structured, dict) and isinstance(structured.get("say"), str):
        return absorb_structured(structured, runtime or {})
    parts = []
    for item in ctx.result.content:
        text = getattr(item, "text", None)
        if text is None:
            try:
                text = item.model_dump_json()
            except Exception:
                text = str(item)
        parts.append(text)
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        return "The tool returned no content."
    data = None
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        data = json.loads(text)
    if data is not None:
        shaper = SHAPERS.get((connector_type, getattr(ctx, "tool_name", "") or ""))
        if shaper is not None and isinstance(data, dict):
            try:
                shaped = shaper(data)
            except Exception as e:  # a shaper must never cost a turn
                logger.warning(f"result shaper for {connector_type} failed: {e}")
                shaped = None
            if shaped is not None:
                data = shaped
        text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if len(text) > RESULT_MAX_CHARS:
        text = text[:RESULT_MAX_CHARS] + " ... (truncated; ask for a narrower query)"
    return text


def absorb_structured(sc: dict, runtime: dict) -> str:
    """A structured result from one of Arya's own modules: the cards reach
    the transcript, the itinerary reaches the written page, a booking gets
    its confirmation, and the model is handed the spoken sentence and
    nothing else.

    Public because worker code can call a module tool itself (see
    trip_planner) and its result must be handled identically, or a plan
    built by the worker would never appear on the page.
    """
    cards = sc.get("cards")
    stash = runtime.get("options_stash")
    if isinstance(cards, list) and isinstance(stash, list):
        stash.extend(c for c in cards if isinstance(c, dict) and c.get("items"))
    itin = sc.get("itinerary")
    if isinstance(itin, dict) and isinstance(itin.get("payload"), dict) \
            and runtime.get("conversation_id"):
        payload = itin["payload"]
        # The module does not know whose agent this is; the worker does.
        brand = runtime.get("brand") or {}
        if isinstance(payload.get("brand"), dict) and isinstance(brand, dict):
            payload["brand"] = {**payload["brand"], **{k: v for k, v in brand.items() if v}}
        itinerary_sync.sync(payload, title=itin.get("title") or "Trip itinerary",
                            stage=itin.get("stage") or "update", runtime=runtime)
    # A module that booked something says so here, and the worker sends the
    # confirmation. Scheduled after the itinerary sync above so the stored
    # page the mail is built from already has this booking on it.
    notify = sc.get("notify")
    if isinstance(notify, dict) and runtime.get("connectors"):
        _notify_later(notify, runtime)
    text = sc["say"].strip()
    machine = sc.get("machine")
    if machine:
        text += "\n" + json.dumps(machine, separators=(",", ":"), ensure_ascii=False)
    if len(text) > STRUCTURED_MAX_CHARS:
        text = text[:STRUCTURED_MAX_CHARS] + " ... (truncated)"
    return text


def _notify_later(notify: dict, runtime: dict) -> None:
    """Fire and forget, like the itinerary sync: a confirmation email must
    never hold or break the spoken turn. Imported here rather than at the
    top because itinerary_email imports this module."""
    async def _run():
        try:
            import itinerary_email
            # The page is written by a task scheduled a moment ago; give it
            # a beat so the mail is built from the booking, not from what
            # the page held before it.
            await asyncio.sleep(NOTIFY_DELAY_S)
            await itinerary_email.notify_booking(runtime, notify)
        except Exception as e:
            logger.warning(f"booking notification skipped: {e}")

    task = asyncio.create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def tool_name(tool) -> str | None:
    if not MCP_AVAILABLE:
        return None
    if is_raw_function_tool(tool):
        return get_raw_function_info(tool).name
    if is_function_tool(tool):
        return get_function_info(tool).name
    return None


def native_tool_names(tools: list) -> set[str]:
    return {n for n in (tool_name(t) for t in tools) if n}


def dead_jobs(cfg: dict, live: list[dict]) -> list[str]:
    """What this agent is attached to that did NOT come up, named in the
    words the caller would use.

    Derived from the config rather than the connections table, so it needs no
    DB round trip and works even when loading the rows is what failed.
    """
    alive = {c["row"]["id"] for c in live}
    out: list[str] = []
    for att in cfg.get("connectors") or []:
        if not isinstance(att, dict) or att.get("connection_id") in alive:
            continue
        entry = connector_type(att.get("type") or "") or {}
        job = entry.get("job") or entry.get("label") or "one of this agent's systems"
        if job not in out:
            out.append(job)
    return out


def prompt_block(live: list[dict], now: datetime.datetime | None = None,
                 dead: list[str] | None = None) -> str:
    """One catalog prompt_block per LIVE connector, appended to the system
    prompt. Only for connectors that actually connected: telling the model
    about tools it does not have is worse than silence.

    `dead` names the jobs whose connector did not come up. That silence was
    not neutral: the sample desk a connector replaces stays off whatever its
    health, so the model was simply missing a tool nobody mentioned, and it
    filled the hole from a web search. Naming the gap is what makes an honest
    refusal possible.

    Also states the current date and time: without it the model fills tool
    arguments from its training-era sense of "today" (seen live: a
    calendar query for 2024 during a call in 2026)."""
    parts = []
    for item in live:
        entry, row, att = item["entry"], item["row"], item["attachment"]
        tpl = entry.get("prompt_block") or ""
        if not tpl:
            continue
        parts.append(tpl.format(
            name=row["name"],
            purpose=(att.get("purpose") or "").strip() or "what these tools cover",
            account=(row.get("config") or {}).get("account_email") or "the connected account",
            # From the module's check route (Duffel: test or live), stored on
            # the connection by the API. Only some blocks use it.
            mode=(row.get("config") or {}).get("mode") or "live",
        ))
    if dead:
        jobs = ", ".join(dead)
        parts.append(
            "NOT available on this call: " + jobs + ". Say plainly that you"
            " cannot do that right now and offer what you can instead. Never"
            " use a web search or your own knowledge to stand in for it, and"
            " never quote a price, a time or an availability you did not get"
            " from a tool on this call."
        )
    if not parts:
        return ""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    clock = (
        f"Right now it is {now.strftime('%A, %B %d, %Y')} at {now.strftime('%H:%M')} UTC."
        " Use this as today's date in every tool call; never assume the year."
        " Times the caller gives are in their own time zone: ask for it if"
        " unknown, then convert to UTC (ISO 8601) for tool arguments."
    )
    return (
        "\n\n# Connected systems\n\n" + clock + "\n\n"
        + "\n".join(f"- {p}" for p in parts)
    )


def replaced_toolsets(cfg: dict, rows: dict | None = None) -> frozenset[str]:
    """Which `tools.*` sample toolsets an attached connector has taken over.

    `rows` (connection id -> row, as load_attached returns) lets a ticked
    tool count only while the connection still lists it. Seen live: Test
    hid Duffel's hotel tools and stripped them from the agent, then an
    Options page that had been open the whole time saved its stale list
    back, and the four dead ticks switched the SerpAPI hotel desk off for
    a whole call. Without rows (the API's sync validate path) the ticks
    are taken at face value.

    Derived from the attached connector TYPES' `overlaps`, not from a choice
    the owner makes, because there was never a sound way to choose. The
    sample appointment desk and Cal.com both define `list_services`,
    `book_appointment`, `reschedule_appointment` and `cancel_appointment`,
    and build_toolsets seeds its dedupe set from the native tools, so with
    both built the SAMPLE desk wins those four names. The agent would quote
    a real free slot from the connector and book it into the fake desk,
    telling the caller it was confirmed when it existed nowhere.

    Keyed on being ATTACHED, never on whether the connector actually
    connected. If the owner's system is unreachable mid-call the agent must
    say it cannot look that up, never fall back to the canned desk and
    invent a booking. It also has to be knowable before any network call,
    since build_tools runs first.
    """
    out: set[str] = set()
    for att in cfg.get("connectors") or []:
        if not isinstance(att, dict):
            continue
        type_id = att.get("type") or ""
        if type_id:
            entry = connector_type(type_id) or {}
            row = (rows.get(att.get("connection_id")) or {}) if rows is not None else {}
            # A connection can SERVE a sample desk instead of replacing it,
            # when the check found it has the desk's own tables (see
            # _kept_toolsets). Without rows nothing is known, so the desk
            # counts as replaced, which is the safe reading.
            kept = _kept_toolsets(entry, row)
            out.update(
                t for t in (entry.get("overlaps") or [])
                if isinstance(t, str) and t not in kept
            )
            # Some connectors cover a sample desk only once particular tools
            # are ticked. Duffel replaces the hotel desk only when its hotel
            # tools are, because Duffel Stays is switched off on new accounts
            # and the check hides those tools until Duffel enables it. The
            # ticked tools stay the whole truth, and nothing merges.
            allowed = {t for t in (att.get("allowed_tools") or []) if isinstance(t, str)}
            if rows is not None:
                listed = {t.get("name") for t in (row.get("tools") or [])
                          if isinstance(t, dict)}
                if listed:
                    allowed &= listed
            for toggle, names in (entry.get("overlaps_by_tool") or {}).items():
                if isinstance(names, list) and allowed.intersection(names):
                    out.add(toggle)
        else:
            # Saved before the type was recorded on the attachment: use the
            # list that was ticked at the time, which the UI pre-filled from
            # these same `overlaps`.
            out.update(
                t for t in (att.get("replaces") or []) if isinstance(t, str)
            )
    return frozenset(out)


def _kept_toolsets(entry: dict, row: dict) -> set[str]:
    """Sample toolsets this connection serves rather than replaces.

    The type's `keeps_when` maps a toolset to a flag the check route sets on
    the connection's config: postgres marks `shop_desk` when the database
    has the order desk's four tables. While the row carries the flag the
    desk stays on and runs over that database (served_toolsets hands the
    worker its connection string). The flag is re-derived on every check,
    so a database that loses the tables switches the desk off again.
    """
    cfg = row.get("config") or {}
    return {
        toolset for toolset, flag in (entry.get("keeps_when") or {}).items()
        if isinstance(toolset, str) and isinstance(flag, str) and cfg.get(flag)
    }


def served_toolsets(cfg: dict, rows: dict | None) -> dict[str, dict]:
    """toolset -> {entry, row} for each sample desk an attached connection
    serves (see _kept_toolsets). The first attachment that serves a desk
    wins; needs the rows, so it is a worker-side call."""
    out: dict[str, dict] = {}
    if not rows:
        return out
    for att in cfg.get("connectors") or []:
        if not isinstance(att, dict):
            continue
        entry = connector_type(att.get("type") or "") or {}
        row = rows.get(att.get("connection_id")) or {}
        for toolset in _kept_toolsets(entry, row):
            out.setdefault(toolset, {"entry": entry, "row": row})
    return out


async def load_attached(pool, cfg: dict) -> list[dict]:
    ids = [
        a.get("connection_id") for a in (cfg.get("connectors") or [])
        if isinstance(a, dict) and a.get("connection_id")
    ]
    if not ids:
        return []
    rows = await pool.fetch(
        "SELECT * FROM connections WHERE id = ANY($1::text[])", ids
    )
    return [row_to_dict(r) for r in rows]


async def build_toolsets(cfg: dict, pool, keys: dict, *, is_public: bool,
                         taken_names: set[str],
                         budget_s: float = SETUP_BUDGET_S,
                         runtime: dict | None = None,
                         rows: dict | None = None) -> tuple[list, list[dict]]:
    """MCPToolsets for this agent's attached connections.

    Returns (toolsets, live) where live = [{entry, row, attachment,
    toolset, tool_names}] for the connectors that connected. Failures are
    logged and dropped: the call starts without that connector. Public
    (embed) sessions only get the attachment's public_tools.

    runtime is the call's {conversation_id, room, options_stash, brand,
    ...} (agent.py tool_runtime): the result resolver needs it to render
    option cards and the written itinerary, and sidecar requests carry
    its conversation id. Absent in tests and the API.
    """
    attachments = [a for a in (cfg.get("connectors") or []) if isinstance(a, dict)]
    if not attachments:
        return [], []
    if not MCP_AVAILABLE:
        logger.warning(f"connectors skipped: {MCP_UNAVAILABLE_MSG}")
        return [], []
    if rows is None:  # the worker preloads them for replaced_toolsets
        try:
            rows = {r["id"]: r for r in await load_attached(pool, cfg)}
        except Exception as e:
            logger.warning(f"connectors skipped (could not load connections): {e}")
            return [], []

    candidates = []
    for att in attachments:
        row = rows.get(att.get("connection_id"))
        if row is None:
            logger.warning(f"connector '{att.get('connection_id')}' no longer exists, skipped")
            continue
        entry = connector_type(row["type"])
        if entry is None:
            logger.warning(f"connector '{row['name']}': unknown type '{row['type']}', skipped")
            continue
        allowed = [t for t in (att.get("allowed_tools") or []) if isinstance(t, str)]
        # The probe's list is the truth about what this account can use. A
        # tool the check hid (Duffel Stays off) must not reach the model
        # because an attachment saved earlier still names it.
        listed = {t.get("name") for t in (row.get("tools") or []) if isinstance(t, dict)}
        if listed:
            allowed = [t for t in allowed if t in listed]
        if is_public:
            public = set(att.get("public_tools") or [])
            allowed = [t for t in allowed if t in public]
        if not allowed:
            logger.info(f"connector '{row['name']}': no tools allowed for this session, skipped")
            continue
        if row.get("status") in ("needs_auth", "pending_auth"):
            logger.warning(f"connector '{row['name']}' needs sign-in, skipped")
            continue
        try:
            row = await ensure_fresh_token(pool, row, keys, entry)
        except Exception as e:
            logger.warning(f"connector '{row['name']}': token check failed: {e}")
            row = None
        if row is None:
            continue
        url = server_url(entry, row)
        if not url:
            logger.warning(f"connector '{row['name']}' has no URL, skipped")
            continue
        write_tools = set(entry.get("write_tools") or [])
        tool_timeout = entry.get("tool_timeout_s") or TOOL_TIMEOUT_S
        headers = auth_headers(entry, row.get("secret"))
        if runtime and runtime.get("conversation_id") and is_sidecar(entry):
            headers[CONVERSATION_HEADER] = str(runtime["conversation_id"])
        server = lk_mcp.MCPServerHTTP(
            url,
            transport_type=transport_for(entry, row),
            allowed_tools=allowed,
            headers=headers,
            timeout=CONNECT_TIMEOUT_S,
            client_session_timeout_seconds=float(tool_timeout),
            tool_result_resolver=functools.partial(
                spoken_result, runtime=runtime, connector_type=entry.get("id", "")),
        )
        toolset = lk_mcp.MCPToolset(
            id=f"conn_{row['id'].replace('conn-', '')}",
            mcp_server=server,
            # A write tool called twice in one turn (seen with eager models)
            # must ask instead of double-booking.
            tool_options={
                t: lk_mcp.MCPToolOptions(on_duplicate="confirm")
                for t in allowed if t in write_tools or is_write_tool(t, None, entry)
            },
        )
        candidates.append({"entry": entry, "row": row, "attachment": att,
                           "toolset": toolset, "tool_names": [],
                           # So worker code can call one of these tools
                           # itself (call_tool): the URL as resolved, and
                           # the headers AFTER any token refresh.
                           "url": url, "headers": headers,
                           "transport": transport_for(entry, row),
                           "tool_timeout_s": tool_timeout})

    if not candidates:
        return [], []

    t0 = time.monotonic()
    results = await asyncio.gather(
        *(asyncio.wait_for(
            c["toolset"].setup(),
            # A vendor-hosted server can need longer than the local sidecar:
            # Google Maps takes 2.5 to 3.7 s on a quiet machine and timed
            # out at 4 on two live calls. The catalog names the budget.
            float(c["entry"].get("setup_timeout_s") or budget_s),
        ) for c in candidates),
        return_exceptions=True,
    )
    live: list[dict] = []
    seen = set(taken_names)
    for c, res in zip(candidates, results, strict=True):
        name = c["row"]["name"]
        if isinstance(res, BaseException):
            why = "timed out" if isinstance(res, asyncio.TimeoutError) else (
                _describe(res) if hasattr(res, "exceptions") else (str(res) or type(res).__name__)
            )
            logger.warning(f"connector '{name}' unavailable: {why}")
            _close_in_background(c["toolset"])
            continue
        # Dedupe: a name already taken by a native tool or an earlier
        # connector would raise "duplicate function name" in Agent.__init__.
        dropped = []

        def keep(tool, _seen=seen, _dropped=dropped):
            n = tool_name(tool)
            if n is None or n in _seen:
                _dropped.append(n)
                return False
            _seen.add(n)
            return True

        c["toolset"].filter_tools(keep)
        if dropped:
            logger.warning(
                f"connector '{name}': dropped duplicate tool name(s) {dropped}"
            )
        names = [n for n in (tool_name(t) for t in c["toolset"].tools) if n]
        if not names:
            logger.warning(f"connector '{name}': none of the selected tools exist on the server")
            _close_in_background(c["toolset"])
            continue
        c["tool_names"] = names
        live.append(c)
        logger.info(
            f"connector '{name}': {len(names)} tool(s) in"
            f" {int((time.monotonic() - t0) * 1000)}ms"
        )
    return [c["toolset"] for c in live], live


_bg_tasks: set[asyncio.Task] = set()  # keeps fire-and-forget closes alive


def _close_in_background(toolset) -> None:
    async def _close():
        with contextlib.suppress(Exception):
            await toolset.aclose()

    task = asyncio.create_task(_close())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def call_tool(conn: dict, tool: str, arguments: dict,
                    timeout: float | None = None) -> dict:
    """Call ONE tool on a connector this call already opened, from worker
    code rather than from the model.

    Returns {ok, text, structured, error}. Never raises: a failed send is a
    sentence the agent reads out, not a dead turn.

    Why a fresh session rather than the toolset's own: an MCPTool is a raw
    function tool whose call signature depends on options we set per tool,
    and reaching into it would tie worker code to LiveKit internals. This
    uses the same mcp primitives probe_tools does, costs one handshake, and
    is worth it for something that happens once a call.

    The point of it is the itinerary email (itinerary_email.send): the page
    is 8 KB of HTML, which must never travel as a tool argument written by
    the model.
    """
    if not MCP_AVAILABLE:
        return {"ok": False, "text": "", "structured": None,
                "error": MCP_UNAVAILABLE_MSG}
    url = conn.get("url") or ""
    if not url:
        return {"ok": False, "text": "", "structured": None,
                "error": "that connector has no address on this call"}
    seconds = float(timeout or conn.get("tool_timeout_s") or TOOL_TIMEOUT_S)
    headers = conn.get("headers") or {}
    read_timeout = timedelta(seconds=seconds)

    async def _run(session):
        await session.initialize()
        return await session.call_tool(tool, arguments)

    try:
        if conn.get("transport") == "sse":
            async with (
                sse_client(url=url, headers=headers, timeout=seconds,
                           sse_read_timeout=60) as streams,
                ClientSession(streams[0], streams[1],
                              read_timeout_seconds=read_timeout) as session,
            ):
                result = await asyncio.wait_for(_run(session), seconds)
        else:
            async with (
                httpx.AsyncClient(headers=headers, follow_redirects=True,
                                  timeout=httpx.Timeout(seconds, read=seconds)) as http,
                streamable_http_client(url=url, http_client=http) as streams,
                ClientSession(streams[0], streams[1],
                              read_timeout_seconds=read_timeout) as session,
            ):
                result = await asyncio.wait_for(_run(session), seconds)
    except asyncio.TimeoutError:
        return {"ok": False, "text": "", "structured": None,
                "error": f"{conn.get('row', {}).get('name', 'that system')} did not answer in time"}
    except Exception as eg:
        return {"ok": False, "text": "", "structured": None, "error": _describe(eg)}

    text = "\n".join(
        t for t in (getattr(c, "text", None) for c in (result.content or [])) if t
    ).strip()
    if result.isError:
        return {"ok": False, "text": text, "structured": None,
                "error": (text or "the tool reported an error")[:300]}
    return {"ok": True, "text": text,
            "structured": getattr(result, "structuredContent", None), "error": None}


def owner_of(tool: str, live: list[dict]) -> dict | None:
    """Which live connector a tool name belongs to (for tool_events)."""
    for c in live:
        if tool in c["tool_names"]:
            return c
    return None


def utc_in(seconds: float) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
