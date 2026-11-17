"""Arya API: agent setup, token server, knowledge base, conversations.

Also serves the built frontend (frontend/dist) so everything lives on one
URL: http://localhost:8000

Run from backend/:  uv run uvicorn api:app --app-dir src --port 8000
"""

import asyncio
import datetime
import hmac
import io
import json
import logging
import os
import pathlib
import re
import secrets
import tempfile
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from livekit import api as lk_api
from minio import Minio
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

import catalog
import media
import providers
import config
import connectors
import conv_timeline
import db
import insights_breakdown
import insights_series
import memory
import meta_wa
import rag
import visitors
import website

logger = logging.getLogger("api")
logging.basicConfig(level=logging.INFO)

load_dotenv(config.BACKEND_DIR / ".env.local")

S3 = config.s3_config()
minio = Minio(
    S3["endpoint"],
    access_key=S3["access_key"],
    secret_key=S3["secret_key"],
    secure=S3["secure"],  # S3_SECURE=true for any TLS endpoint (real S3/R2)
)
qd = rag.qdrant(config.qdrant_url())


def _dsn() -> str:
    return config.database_url()


def _rag_cfg() -> dict:
    return config.load_active().get("rag", {})


def _allowed_formats() -> list[str]:
    return _rag_cfg().get("allowed_formats") or config.ALLOWED_FORMATS


def _profile() -> dict:
    return config.embedding_profile(config.load_active(), config.load_keys())


async def _connections_snapshot() -> list[dict] | None:
    """{id, type, name, status, config} per connection, for config.validate()'s
    connector checks. None on DB trouble: validate() then skips those
    checks instead of failing every save over a hiccup."""
    try:
        pool = await db.get_pool(_dsn())
        rows = await pool.fetch(
            "SELECT id, type, name, status, config FROM connections"
        )
        # row_to_dict, not dict: the pool has no JSON codec, so `config`
        # arrives as a string and every reader would have to parse it.
        return [connectors.row_to_dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"connections snapshot skipped: {e}")
        return None


async def _validate_full(cfg: dict) -> list[str]:
    """config.validate() plus the connector checks (needs the DB)."""
    return config.validate(
        cfg, config.load_keys(), connections=await _connections_snapshot()
    )


async def session_reaper(pool):
    """conversations.ended_at can stay NULL after a worker crash. Every
    60s, close rows idle past the grace window, stamped with the last
    message's time so History shows a real duration. Cleanup, not
    correctness — the quota counts are already time-windowed."""
    while True:
        await asyncio.sleep(60)
        try:
            await pool.execute(
                "UPDATE conversations c SET ended_at = COALESCE("
                " (SELECT max(m.created_at) FROM messages m"
                "   WHERE m.conversation_id = c.id), c.started_at)"
                " WHERE c.ended_at IS NULL AND COALESCE("
                " (SELECT max(m.created_at) FROM messages m"
                "   WHERE m.conversation_id = c.id), c.started_at)"
                " < now() - interval '30 minutes'"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"session reaper skipped a cycle: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if RUNTIME_ONLY and not _admin_token():
        # An engine is public by definition; its admin surface (import,
        # publish) must never boot unprotected.
        raise RuntimeError(
            "ARYA_RUNTIME_ONLY=1 requires ARYA_ADMIN_TOKEN to be set"
        )
    pool = await db.get_pool(_dsn())
    # Catalogs (model lists, search registry, embedding profiles) are
    # DB-backed and editable at runtime; first boot writes the seeds.
    await catalog.ensure_seeded(pool)
    if not minio.bucket_exists(S3["bucket"]):
        minio.make_bucket(S3["bucket"])
        logger.info(f"created bucket {S3['bucket']}")
    worker = asyncio.create_task(ingestion_worker(pool))
    reaper = asyncio.create_task(session_reaper(pool))
    logger.info("ingestion worker + session reaper started")
    # The only URL the terminal shows otherwise is uvicorn's
    # http://0.0.0.0:8000, which no browser can open (it means "listening on
    # every address", not an address). A first-time user copies the one URL
    # on screen and lands on a dead page, so print the one to visit and say
    # what the other line is. Engines serve no builder UI, hence the guard.
    if not RUNTIME_ONLY:
        base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        bar = "=" * 60
        logger.info(bar)
        logger.info(f"  Arya is ready:  {base or 'http://localhost:8000'}")
        logger.info("  Open that in Chrome or Edge. The 0.0.0.0 address")
        logger.info("  uvicorn prints is the same server, not a link.")
        logger.info(bar)
    yield
    worker.cancel()
    reaper.cancel()


app = FastAPI(title="Arya API", lifespan=lifespan)

cors_origins = ["http://localhost:5173", "http://localhost:8000"]
cors_origins += [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── admin gate ──────────────────────────────────────────────────────────────
# Two routers: `open_rt` for the endpoints anything may call (health), and
# `api` for the builder/admin surface. When ARYA_ADMIN_TOKEN is set, every
# `api` route requires it; when unset (local single-user use) the gate is a
# no-op and behavior is byte-for-byte what it was before.


def _admin_token() -> str:
    # Read per-request, not at import — .env.local load order and tests
    # must not freeze an empty value.
    return os.environ.get("ARYA_ADMIN_TOKEN", "").strip()


# Runtime ("engine-only") mode: no builder SPA, embed surface + public API
# only — the deploy target agents get PUSHED to. See the mounts at the
# bottom of this file and the boot guard in lifespan.
RUNTIME_ONLY = os.environ.get("ARYA_RUNTIME_ONLY", "").lower() in (
    "1", "true", "yes",
)


async def require_admin(
    x_arya_admin_token: str | None = Header(None),
    authorization: str | None = Header(None),
) -> None:
    token = _admin_token()
    if not token:
        return
    supplied = x_arya_admin_token or (
        authorization[7:] if (authorization or "").startswith("Bearer ") else ""
    )
    if not supplied or not hmac.compare_digest(supplied, token):
        raise HTTPException(401, "admin token required")


open_rt = APIRouter(prefix="/api")
api = APIRouter(prefix="/api", dependencies=[Depends(require_admin)])


# ─── setup: provider keys ────────────────────────────────────────────────────


class KeysReq(BaseModel):
    openai: str | None = None
    groq: str | None = None
    gemini: str | None = None
    serpapi: str | None = None
    ares: str | None = None
    tavily: str | None = None
    mem0: str | None = None
    # OAuth app for the Google Calendar connector (not a provider key).
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None


@api.post("/setup/keys")
async def set_keys(req: KeysReq):
    """Store provider keys (local plaintext file, single-user dev tool).
    Empty string removes a key; null leaves it untouched."""
    config.save_keys({k: v for k, v in req.model_dump().items() if v is not None})
    return {"saved": True, "keys": config.key_status(), "masked": config.masked_keys()}


@api.get("/setup/status")
async def setup_status():
    """Which keys are present (booleans only — values never leave the
    server) + problems that would stop the active agent from running +
    the upload limits, so the frontend never mirrors server constants."""
    cfg = config.load_active()
    return {
        "keys": config.key_status(),
        "masked": config.masked_keys(),
        "problems": await _validate_full(cfg),
        "limits": {
            "max_upload_mb": MAX_UPLOAD_MB,
            "max_docs_per_agent": MAX_DOCS_PER_AGENT,
            "allowed_formats": config.ALLOWED_FORMATS,
        },
    }


@api.get("/models")
async def list_models():
    """Curated catalogs for the Options screen — served from the DB-backed
    catalog store (editable via PUT /api/catalogs/{name}, no rebuild).
    `defaults` stays the flat OpenAI dict the current UI reads;
    `defaults_by_provider`, `search`, and `embeddings` are the widened
    surface new frontend code should prefer. No key required."""
    pool = await db.get_pool(_dsn())
    await catalog.refresh(pool)
    return {
        "openai": catalog.get("openai_models"),
        "defaults": catalog.get("openai_defaults"),
        "local": catalog.get("local_backends"),
        "defaults_by_provider": {
            "openai": catalog.get("openai_defaults"),
            "groq": catalog.get("groq_defaults"),
            "local": catalog.get("local_defaults"),
            "gemini": catalog.get("gemini_defaults"),
        },
        "gemini": catalog.get("gemini_models"),
        "search": catalog.get("search_providers"),
        "tools": catalog.get("tool_catalog"),
        "connectors": catalog.get("connectors"),
        "embeddings": [
            {"id": p["id"], "label": p["label"],
             "needs_key": p.get("key_name")}
            for p in catalog.get("embedding_profiles")
        ],
        "industries": catalog.get("industries"),
    }


# ─── catalogs: runtime-editable model/provider registries ────────────────────
# NOTE: unauthenticated today, like every other route on this single-user
# tool. Gate it behind ARYA_ADMIN_TOKEN before exposing this API publicly —
# a writable catalog can repoint models and endpoints for every agent.


class CatalogPutReq(BaseModel):
    data: dict | list


@api.get("/catalogs")
async def list_catalogs():
    """Catalog names + when each was last edited (seed rows show first
    boot). The editable surface behind the /models response."""
    pool = await db.get_pool(_dsn())
    rows = await pool.fetch("SELECT name, updated_at FROM catalogs ORDER BY name")
    return {"catalogs": [dict(r) for r in rows]}


@api.get("/catalogs/{name}")
async def get_catalog(name: str):
    pool = await db.get_pool(_dsn())
    await catalog.refresh(pool)
    if name not in catalog.SEEDS:
        raise HTTPException(404, f"unknown catalog '{name}'")
    return {"name": name, "data": catalog.get(name)}


@api.put("/catalogs/{name}")
async def put_catalog(name: str, req: CatalogPutReq):
    """Validated upsert — e.g. add a new OpenAI model without a redeploy.
    Bad shapes 400 with the reason; the seed is restorable via reset."""
    pool = await db.get_pool(_dsn())
    try:
        await catalog.put(pool, name, req.data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"saved": True, "name": name, "data": catalog.get(name)}


@api.post("/catalogs/{name}/reset")
async def reset_catalog(name: str):
    """Restore a catalog to its shipped seed — the undo for a bad PUT."""
    pool = await db.get_pool(_dsn())
    try:
        await catalog.reset(pool, name)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {"reset": True, "name": name, "data": catalog.get(name)}


LOCAL_CHECK_TIMEOUT_S = 5.0
LOCAL_CHECK_MAX_MODELS = 200  # a runaway server listing thousands of LoRA
                              # adapters shouldn't balloon the response


class LocalCheckReq(BaseModel):
    url: str


@api.post("/local/check")
async def local_check(req: LocalCheckReq):
    """Reachability probe for a self-hosted Ollama/vLLM endpoint — the
    Options screen's Check button. GET {url}/models is ground truth for
    what that server actually serves: better than any list we could
    hardcode, and it catches an unreachable endpoint, an unpulled Ollama
    model, or a wrong vLLM model id all in one call. Never raises — always
    {ok, models} or {ok: False, error}, so a bad URL can't 500 the route."""
    url = (req.url or "").strip()
    if not url:
        return {"ok": False, "error": "no endpoint URL to check"}
    normalized = config._normalize_base_url(url)
    if not normalized.startswith(("http://", "https://")):
        return {"ok": False, "error": "endpoint must start with http:// or https://"}
    try:
        async with httpx.AsyncClient(timeout=LOCAL_CHECK_TIMEOUT_S) as client:
            resp = await client.get(f"{normalized}/models")
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        return {"ok": False, "error": "timed out"}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"http {e.response.status_code}"}
    except httpx.HTTPError:
        return {"ok": False, "error": "unreachable"}
    except Exception as e:
        return {"ok": False, "error": str(e) or "check failed"}
    models = [
        m.get("id")
        for m in (data.get("data") or [])
        if isinstance(m, dict) and m.get("id")
    ][:LOCAL_CHECK_MAX_MODELS]
    return {"ok": True, "models": models}


@open_rt.get("/health")
async def health():
    """Liveness + gate discovery: the SPA learns from `admin_required`
    whether it must ask the owner for the admin token."""
    return {"ok": True, "admin_required": bool(_admin_token())}


# ─── agents: templates + the active agent ────────────────────────────────────


@api.get("/agents")
async def list_agents():
    """Agents the user has created (saved on every PUT /agents/active)."""
    return config.list_saved_agents()


@api.post("/agents/{agent_id}/activate")
async def activate_agent(agent_id: str):
    """Make a saved agent the one the worker runs on the next call."""
    cfg = config.load_saved_agent(agent_id)
    if cfg is None:
        raise HTTPException(404, "unknown agent")
    config.save_active(cfg)
    return {"activated": True, "config": cfg}


@api.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, cascade: bool = True):
    """Remove an agent completely: its config, its knowledge-base documents
    (DB rows + S3 files + Qdrant vectors), its Mem0 memories, and its
    conversations/transcripts. Conversations created before agent stamping
    existed (no agent_id in metadata) are left untouched.

    cascade=0 (sent by the engine proxy) keeps this delete local to the box
    it runs on: the builder's "Remove from engine" is already the engine
    hop, so the engine must not fan out to engines of its own."""
    if not config.delete_saved_agent(agent_id):
        raise HTTPException(404, "unknown agent")
    # Revoke any deployment first: a deleted agent must never leave a live
    # embed key behind that could later resolve to the wrong agent.
    try:
        pool = await db.get_pool(_dsn())
        await pool.execute(
            "UPDATE deployments SET status='revoked', revoked_at=now(),"
            " updated_at=now() WHERE agent_id = $1",
            agent_id,
        )
    except Exception as e:
        logger.warning(f"deployment revoke failed for agent {agent_id}: {e}")
    # Same reach as Unpublish: a deleted agent that keeps answering on an
    # engine is the worst outcome here, and once the local config is gone
    # there is no Publish screen left to switch it off from. Only the KEY
    # is revoked there — the engine's own copy of the agent, its documents
    # and its transcripts are left alone, since deleting an agent here does
    # not imply wiping another machine's records.
    engines_revoked = await _revoke_on_engines(agent_id) if cascade else []
    docs_purged = 0
    try:
        pool = await db.get_pool(_dsn())
        rows = await pool.fetch(
            "SELECT id, object_key FROM knowledge_base WHERE agent_id = $1",
            agent_id,
        )
        for row in rows:
            _purge_doc_stores(row["id"], row["object_key"])
            await pool.execute(
                "DELETE FROM knowledge_base WHERE id = $1", row["id"]
            )
            docs_purged += 1
    except Exception as e:
        logger.warning(f"doc purge incomplete for agent {agent_id}: {e}")
    convs_purged = 0
    try:
        pool = await db.get_pool(_dsn())
        conv_rows = await pool.fetch(
            "SELECT id FROM conversations WHERE metadata->>'agent_id' = $1",
            agent_id,
        )
        for row in conv_rows:
            await pool.execute(
                "DELETE FROM messages WHERE conversation_id = $1", row["id"]
            )
            await pool.execute(
                "DELETE FROM artifacts WHERE conversation_id = $1", row["id"]
            )
        await pool.execute(
            "DELETE FROM conversations WHERE metadata->>'agent_id' = $1",
            agent_id,
        )
        convs_purged = len(conv_rows)
    except Exception as e:
        logger.warning(f"conversation purge incomplete for agent {agent_id}: {e}")
    memories_purged = False
    keys = config.load_keys()
    if keys.get("mem0"):
        try:
            await memory.purge_agent(keys["mem0"], agent_id)
            memories_purged = True
        except Exception as e:
            logger.warning(f"mem0 purge skipped for agent {agent_id}: {e}")
    return {
        "deleted": True,
        "documents_purged": docs_purged,
        "conversations_purged": convs_purged,
        "memories_purged": memories_purged,
        "engines": engines_revoked,
    }


@api.get("/agents/templates")
async def list_templates():
    return config.list_templates()


@api.get("/agents/templates/{template_id}")
async def get_template(template_id: str):
    cfg = config.load_template(template_id)
    if cfg is None:
        raise HTTPException(404, "unknown template")
    return cfg


@api.get("/agents/active")
async def get_active():
    return config.load_active()


class GeneratePromptReq(BaseModel):
    business: dict = {}
    rag_enabled: bool = False


@api.post("/agents/generate-prompt")
async def generate_prompt(req: GeneratePromptReq):
    """Deterministic system prompt + first message from the questionnaire
    answers — both shown editable on the Preview screen before saving."""
    return {
        "instructions": config.generate_instructions(
            req.business, rag_enabled=req.rag_enabled
        ),
        "greeting": config.generate_greeting(req.business),
    }


class FetchWebsiteReq(BaseModel):
    url: str


@api.post("/agents/fetch-website")
async def fetch_website(req: FetchWebsiteReq):
    """Read the business's public homepage so the generated prompt can be
    personalized. Failures are data, not HTTP errors — a down site must
    never block the wizard, so this always returns 200 with ok true/false."""
    return await website.fetch_website_text(req.url)


class WebsiteContextReq(BaseModel):
    url: str = ""
    instructions: str = ""


@api.post("/agents/website-context")
async def website_context(req: WebsiteContextReq):
    """Read the business's homepage and put its text into the GIVEN prompt
    as the business-context block (replacing an older block in place), so a
    template agent can add its website after the fact without losing the
    authored instructions. An empty url removes the block. Fetch failures
    are data (ok false) and hand the prompt back untouched."""
    url = (req.url or "").strip()
    if not url:
        return {
            "ok": True,
            "url": "",
            "text": "",
            "instructions": config.upsert_website_context(req.instructions, ""),
        }
    r = await website.fetch_website_text(url)
    if not r.get("ok"):
        return {**r, "instructions": req.instructions}
    return {
        "ok": True,
        "url": r["url"],
        "text": r["text"],
        "instructions": config.upsert_website_context(req.instructions, r["text"]),
    }


# ── voice previews ───────────────────────────────────────────────────────
# One fixed sentence per provider voice. Looked up in this order: the
# clips committed under backend/voices (made by scripts/gen_voice_samples.py,
# no key needed on any machine), then the per-machine cache under
# data/voices, then synthesis with the owner's saved key, which fills that
# cache. Kokoro runs in the bundled container and needs no key either way.
_VOICE_SAMPLE = "Hi, thanks for calling. How can I help you today?"
_voice_locks: dict[str, asyncio.Lock] = {}
_BUNDLED_VOICES = config.BACKEND_DIR / "voices"


def _voice_slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s or "")[:80] or "_"


@api.get("/voices/preview")
async def voice_preview(provider: str, model: str = "", voice: str = ""):
    """Audio of one sentence in the given voice. A bundled clip plays with
    no key at all; otherwise 409 {reason: no_key} when the provider's key is
    not saved, 502 with the provider's message when synthesis fails, 400 for
    a voice or model the catalog does not list."""
    pool = await db.get_pool(_dsn())
    await catalog.refresh(pool)
    keys = config.load_keys()
    provider = (provider or "").strip().lower()
    voice = (voice or "").strip()
    model = (model or "").strip()

    if provider == "openai":
        tts = {m["id"]: m for m in catalog.get("openai_models").get("tts", [])}
        entry = tts.get(model or catalog.get("openai_defaults").get("tts_model", ""))
        if not entry or voice not in (entry.get("voices") or []):
            raise HTTPException(400, "unknown model or voice")
        endpoint = {
            "provider": "openai",
            "tts_base_url": providers.OPENAI_BASE_URL,
            "api_key": keys["openai"],
            "tts_model": entry["id"],
            "voice": voice,
            "tts_format": "mp3",
        }
        model, ext, ctype = entry["id"], "mp3", "audio/mpeg"
    elif provider == "gemini":
        gm = catalog.get("gemini_models")
        tts = {m["id"]: m for m in gm.get("tts", [])}
        entry = tts.get(model or catalog.get("gemini_defaults").get("tts_model", ""))
        voices = (entry or {}).get("voices") or gm.get("voices") or []
        if not entry or voice not in voices:
            raise HTTPException(400, "unknown model or voice")
        endpoint = {
            "provider": "gemini",
            "api_key": keys["gemini"],
            "tts_model": entry["id"],
            "voice": voice,
            "style": "",
        }
        model, ext, ctype = entry["id"], "wav", "audio/wav"
    elif provider == "local":
        d = catalog.get("local_defaults")
        voice = voice or d.get("voice", "af_heart")
        model = d.get("speaches_tts_model", "")
        endpoint = {
            "provider": "local",
            "tts_base_url": config.speaches_url() + "/v1",
            "api_key": "not-needed",
            "tts_model": model,
            "voice": voice,
            "tts_format": "wav",
        }
        ext, ctype = "wav", "audio/wav"
    else:
        raise HTTPException(400, "unknown provider")

    headers = {"Cache-Control": "private, max-age=86400"}
    bundled = _BUNDLED_VOICES / provider / f"{_voice_slug(voice)}.{ext}"
    if bundled.is_file():
        return Response(bundled.read_bytes(), media_type=ctype, headers=headers)

    path = (
        config.DATA_DIR / "voices" / provider / _voice_slug(model)
        / f"{_voice_slug(voice)}.{ext}"
    )
    if not path.exists():
        if provider in ("openai", "gemini") and not keys.get(provider):
            raise HTTPException(409, {"reason": "no_key", "provider": provider})
        # One synthesis per clip, however many clicks arrive at once.
        lock = _voice_locks.setdefault(str(path), asyncio.Lock())
        async with lock:
            if not path.exists():
                try:
                    data, _mime = await media.synthesize(endpoint, _VOICE_SAMPLE)
                except media.MediaError as e:
                    raise HTTPException(502, str(e)) from None
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
    return Response(path.read_bytes(), media_type=ctype, headers=headers)


class ActiveReq(BaseModel):
    template_id: str | None = None
    overrides: dict = {}
    config: dict | None = None


@api.put("/agents/active")
async def put_active(req: ActiveReq):
    """Materialize the active agent the worker will run on the next call.

    Either pass a full `config`, or a `template_id` plus `overrides` to
    deep-merge on top of the template. If the result has questionnaire
    answers (`business`) but no explicit `instructions`, the prompt is
    generated from them.
    """
    if req.config is not None:
        cfg = req.config
    else:
        base = config.load_template(req.template_id) if req.template_id else None
        if base is None:
            base = config.load_active()
        cfg = json.loads(json.dumps(base))  # deep copy
        config.deep_merge(cfg, req.overrides)
    if cfg.get("business") and not cfg.get("instructions"):
        cfg["instructions"] = config.generate_instructions(
            cfg["business"], rag_enabled=cfg.get("rag", {}).get("enabled", False)
        )
    if cfg.get("business") and not cfg.get("greeting"):
        cfg["greeting"] = config.generate_greeting(cfg["business"])
    # {agent_name} / {business_name} tokens in instructions and greeting are
    # NOT substituted here on purpose: the saved prompt stays a template and
    # config.render_vars() fills it on every call (agent.py), so renaming
    # the agent or business later takes effect without a prompt edit.
    # Never persist redaction placeholders over real values.
    if cfg.get("instructions") == "***":
        cfg.pop("instructions")
    await _prune_attachments(cfg)
    config.save_active(cfg)
    config.save_agent(cfg)  # keep the per-agent copy so it shows on Home
    problems = await _validate_full(cfg)
    return {"saved": True, "problems": problems, "config": cfg}


# ─── deployments: publish an agent for embedding ─────────────────────────────
# One deployment per agent (unique index). public_key is PLAINTEXT BY
# DESIGN — it ships inside customers' page source; the security boundary
# is the origin allowlist + caps (enforced by the public session API),
# never secrecy. Keys minted here become callable when /api/public/v1
# lands (next phase); the snippet/share_url are generated against
# PUBLIC_BASE_URL so a local builder can render them for a remote engine.


# Session counting lives in three places (this list endpoint, the usage
# endpoint, and the quota gate in _start_session). They MUST agree, so the
# expressions live here once. open_now is time-windowed because ended_at
# can stay NULL after a crash — a stored counter would leak forever.
# NOTE: _start_session keeps its own inline copy of the first two counts:
# it runs inside the session-creating transaction and is the one truly
# load-bearing path, so it is deliberately not refactored to share this.
_SESSION_COUNTS = (
    "count(*) FILTER (WHERE started_at >= date_trunc('day', now())) AS today,"
    " count(*) FILTER (WHERE ended_at IS NULL"
    "   AND started_at > now() - interval '15 minutes') AS open_now,"
    " count(*) AS total"
)


def _public_base(request: Request) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    return base or str(request.base_url).rstrip("/")


def _deployment_out(row, request: Request) -> dict:
    base = _public_base(request)
    pk = row["public_key"]
    return {
        "id": str(row["id"]),
        "agent_id": row["agent_id"],
        "public_key": pk,
        # Display form for lists/screenshares. Presentation only: the key
        # is public by design and ships in full inside snippet/share_url.
        "public_key_masked": config.mask_secret(pk),
        "status": row["status"],
        "allowed_origins": json.loads(row["allowed_origins"]),
        "branding": json.loads(row["branding"]),
        "max_concurrent_sessions": row["max_concurrent_sessions"],
        "max_sessions_per_day": row["max_sessions_per_day"],
        "max_session_seconds": row["max_session_seconds"],
        # Presentational default for visitor sessions: whether the widget
        # and share-link page show the collapsed "Sources" control. The
        # one link an answer gives (arya.links) always shows regardless;
        # this only governs the "what the agent searched" list behind it.
        "show_sources": row["show_sources"],
        "sessions_started": row["sessions_started"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "share_url": f"{base}/embed/{pk}",
        "snippet": (
            f'<script src="{base}/arya-widget.js"\n'
            f'        data-arya-agent-key="{pk}" async></script>'
        ),
        # The Publish screen branches on this: a localhost base must show
        # the "take it live" flow, never a localhost snippet.
        "public_base_is_local": any(
            h in base for h in ("localhost", "127.0.0.1", "0.0.0.0")
        ),
    }


def _mint_pk() -> str:
    return "pk_" + secrets.token_urlsafe(24)


@api.get("/deployments")
async def list_deployments(request: Request):
    """Every deployment in one shot, plus the agents that have none (so the
    list can offer Publish without a second round trip).

    Admin-gated like the rest of `api`: this exposes nothing that
    GET /agents/{id}/deployment did not already. `snippet` is dropped
    because it is a paste target for one screen, not list data — N
    multi-line snippets on one page is N keys on one page.
    """
    pool = await db.get_pool(_dsn())
    rows = await pool.fetch(
        "SELECT d.*, COALESCE(c.today, 0) AS sessions_today,"
        " COALESCE(c.open_now, 0) AS sessions_open,"
        " COALESCE(c.total, 0) AS sessions_total"
        " FROM deployments d LEFT JOIN ("
        f"   SELECT deployment_id, {_SESSION_COUNTS}"
        "    FROM conversations WHERE deployment_id IS NOT NULL"
        "    GROUP BY deployment_id"
        " ) c ON c.deployment_id = d.id"
        # Active first, paused after (revoked rows are dropped below).
        " ORDER BY CASE d.status WHEN 'active' THEN 0"
        "   WHEN 'paused' THEN 1 ELSE 2 END, d.created_at DESC"
    )
    # Agent names live in JSON on disk, deployments in Postgres — there is
    # no SQL join available, so decorate in Python.
    agents = {a["id"]: a for a in config.list_saved_agents()}
    out = []
    for r in rows:
        # Revoked = unpublished. The row survives in the table (the dead key
        # must never be reissued) but it is not a deployment any more, so it
        # is skipped here and its agent stays in `agents` — i.e. it lands in
        # the "not published" section, ready to publish again.
        if r["status"] == "revoked":
            continue
        agent = agents.pop(r["agent_id"], None)
        dep = _deployment_out(r, request)
        dep.pop("snippet", None)
        dep.update(
            agent_name=(agent or {}).get("name"),
            agent_icon=(agent or {}).get("icon"),
            agent_industry=(agent or {}).get("industry"),
            # Agent JSON deleted by hand (delete_agent revokes first, so
            # this is rare). The public API 410s for such a key, so it has
            # to stay visible and revocable rather than silently hidden.
            orphaned=agent is None,
            sessions_today=r["sessions_today"],
            sessions_open=r["sessions_open"],
            sessions_total=r["sessions_total"],
            last_session_at=r["last_session_at"],
        )
        out.append(dep)
    # Whatever is left in `agents` is exactly the unpublished set, still in
    # list_saved_agents order (file mtime desc — same as Home shows).
    return {"deployments": out, "unpublished": list(agents.values())}


@api.get("/agents/{agent_id}/deployment")
async def get_deployment(agent_id: str, request: Request):
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT * FROM deployments WHERE agent_id = $1", agent_id
    )
    # A revoked row is a tombstone: it stays in the table so the dead key
    # can never be handed out again, but to every reader that agent is
    # simply not published. Returning it made the Publish screen keep
    # showing a live-looking key, snippet and limits after Unpublish.
    if row is None or row["status"] == "revoked":
        raise HTTPException(404, "agent is not published")
    return _deployment_out(row, request)


@api.post("/agents/{agent_id}/deployment")
async def publish_agent(agent_id: str, request: Request):
    """Publish: mint the agent's pk_ key. Idempotent — an already-active
    deployment is returned as-is; a revoked one is re-minted (fresh key,
    old snippets stay dead). Refuses while config.validate() reports
    problems: a published agent that can't run is a broken embed."""
    cfg = config.load_saved_agent(agent_id)
    if cfg is None:
        raise HTTPException(404, "unknown agent")
    problems = await _validate_full(cfg)
    if problems:
        raise HTTPException(400, "fix before publishing: " + " ".join(problems))
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT * FROM deployments WHERE agent_id = $1", agent_id
    )
    if row is None:
        await pool.execute(
            "INSERT INTO deployments (id, public_key, agent_id)"
            " VALUES ($1, $2, $3)",
            uuid.uuid4(),
            _mint_pk(),
            agent_id,
        )
    elif row["status"] == "revoked":
        await pool.execute(
            "UPDATE deployments SET public_key=$2, status='active',"
            " revoked_at=NULL, updated_at=now() WHERE agent_id=$1",
            agent_id,
            _mint_pk(),
        )
    row = await pool.fetchrow(
        "SELECT * FROM deployments WHERE agent_id = $1", agent_id
    )
    return _deployment_out(row, request)


class DeploymentPatch(BaseModel):
    allowed_origins: list[str] | None = None
    branding: dict | None = None
    # ge=1: a 0 cap silently bricks the deployment via the quota gate
    # (every session request fails the >= check) with nothing in the UI
    # to explain why.
    max_concurrent_sessions: int | None = Field(None, ge=1, le=1000)
    max_sessions_per_day: int | None = Field(None, ge=1, le=100000)
    max_session_seconds: int | None = Field(None, ge=1, le=7200)
    status: str | None = None  # active | paused
    show_sources: bool | None = None


def _norm_origins(values: list[str]) -> list[str]:
    """The origin allowlist IS the security boundary for public keys, so
    normalize it rather than storing whatever was typed. A scheme-less
    "acme.com" can never match a browser Origin header, so it used to fail
    CLOSED and silently — every site 403'd and the widget looked broken.
    Now it is a 400 with a readable reason.

    Stored lowercase and without a trailing slash to match how
    _resolve_pk() compares incoming Origins.
    """
    out: list[str] = []
    for raw in values:
        v = (raw or "").strip().rstrip("/").lower()
        if not v:
            continue
        if not v.startswith(("http://", "https://")):
            raise HTTPException(
                400,
                f"origin must start with https:// (or http:// for local dev) — got {raw!r}",
            )
        if v.count("/") > 2:
            raise HTTPException(
                400, f"origin must be scheme + host only, no path — got {raw!r}"
            )
        if v not in out:
            out.append(v)
    if len(out) > 50:
        raise HTTPException(400, "at most 50 origins")
    return out


@api.patch("/agents/{agent_id}/deployment")
async def patch_deployment(
    agent_id: str, req: DeploymentPatch, request: Request
):
    if req.status is not None and req.status not in ("active", "paused"):
        raise HTTPException(400, "status must be 'active' or 'paused'")
    origins = (
        _norm_origins(req.allowed_origins)
        if req.allowed_origins is not None
        else None
    )
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT id FROM deployments WHERE agent_id = $1", agent_id
    )
    if row is None:
        raise HTTPException(404, "agent is not published")
    sets, args = [], [agent_id]
    for col, val in (
        ("allowed_origins", origins),
        ("branding", req.branding),
        ("max_concurrent_sessions", req.max_concurrent_sessions),
        ("max_sessions_per_day", req.max_sessions_per_day),
        ("max_session_seconds", req.max_session_seconds),
        ("status", req.status),
        ("show_sources", req.show_sources),
    ):
        if val is not None:
            args.append(json.dumps(val) if col in ("allowed_origins", "branding") else val)
            cast = "::jsonb" if col in ("allowed_origins", "branding") else ""
            sets.append(f"{col} = ${len(args)}{cast}")
    if sets:
        await pool.execute(
            f"UPDATE deployments SET {', '.join(sets)}, updated_at=now()"
            " WHERE agent_id = $1",
            *args,
        )
    row = await pool.fetchrow(
        "SELECT * FROM deployments WHERE agent_id = $1", agent_id
    )
    return _deployment_out(row, request)


@api.post("/agents/{agent_id}/deployment/rotate")
async def rotate_deployment_key(agent_id: str, request: Request):
    """New key in place — every previously pasted snippet stops working
    immediately (that is the point of rotating).

    Refuses on a revoked deployment: rotating one used to mint a
    fresh-looking key while status stayed 'revoked', so the UI showed a
    key that _resolve_pk would reject. Re-publishing is the way back.
    """
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "UPDATE deployments SET public_key=$2, updated_at=now()"
        " WHERE agent_id=$1 AND status <> 'revoked' RETURNING id",
        agent_id,
        _mint_pk(),
    )
    if row is None:
        exists = await pool.fetchval(
            "SELECT status FROM deployments WHERE agent_id = $1", agent_id
        )
        if exists == "revoked":
            raise HTTPException(
                409, "this deployment is revoked — publish again to re-activate it"
            )
        raise HTTPException(404, "agent is not published")
    row = await pool.fetchrow(
        "SELECT * FROM deployments WHERE agent_id = $1", agent_id
    )
    return _deployment_out(row, request)


@api.delete("/agents/{agent_id}/deployment")
async def revoke_deployment(agent_id: str, cascade: bool = True):
    """Unpublish this agent everywhere this builder knows about: here
    first, then the same agent on every saved engine.

    "Unpublish" means the agent stops answering, and a key still live on an
    engine keeps it answering — so a local-only revoke was a half-measure
    that read as a full stop. Engines are best effort: one unreachable box
    must not leave the local key live, so failures are reported back by
    name instead of aborting. Engines reached by URL + token at push time
    (never saved here) are the one case this cannot cover.

    cascade=0 is what the engine proxy below sends: that call IS the engine
    hop, so it must stop there rather than fan out again from the engine.
    """
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "UPDATE deployments SET status='revoked', revoked_at=now(),"
        " updated_at=now() WHERE agent_id=$1 AND status <> 'revoked'"
        " RETURNING id",
        agent_id,
    )
    engines = await _revoke_on_engines(agent_id) if cascade else []
    if row is None and not engines:
        raise HTTPException(404, "agent is not published")
    return {"agent_id": agent_id, "revoked": True, "engines": engines}


@api.get("/agents/{agent_id}/deployment/usage")
async def deployment_usage(agent_id: str):
    """Derived counters, never stored ones — ended_at can stay NULL after
    a crash, so the open-now count is time-windowed (self-healing)."""
    pool = await db.get_pool(_dsn())
    dep = await pool.fetchrow(
        "SELECT id, sessions_started, last_session_at FROM deployments"
        " WHERE agent_id = $1",
        agent_id,
    )
    if dep is None:
        raise HTTPException(404, "agent is not published")
    counts = await pool.fetchrow(
        f"SELECT {_SESSION_COUNTS} FROM conversations WHERE deployment_id = $1",
        dep["id"],
    )
    # Same calendar-day window as the quota gate, so "refused today" and
    # "sessions today" always describe the same day.
    refused = await pool.fetchval(
        "SELECT COALESCE(SUM(count), 0) FROM session_refusals"
        " WHERE deployment_id = $1"
        " AND window_start >= date_trunc('day', now())",
        dep["id"],
    )
    return {
        "agent_id": agent_id,
        "sessions_today": counts["today"],
        "sessions_open": counts["open_now"],
        "sessions_total": counts["total"],
        "sessions_started": dep["sessions_started"],
        "last_session_at": dep["last_session_at"],
        "refused_today": refused,
    }


# ─── channels: a published agent also answers on a messaging number ──────────
# A channel hangs off a DEPLOYMENT, never off an agent directly: the
# deployment owns the pk_ key, the caps and the origin rules, so a WhatsApp
# thread is a visitor session with exactly the widget's gating (public
# tools only, no document citations) and counts against the same quotas.
#
# The webhook itself does NOT terminate here. It terminates in the
# arya-whatsapp bridge, which needs host networking to join LiveKit rooms
# (see docker-compose.yml). This module only manages the rows; the bridge
# reads them. `secret` is plaintext JSONB, the same posture as connections.

CHANNEL_TYPES = ("whatsapp",)

# Conservative defaults for a support number. idle_minutes is the silence
# after which the bridge lets the room close; it must stay BELOW the
# session reaper's 30 minutes and at or below the deployment's
# max_session_seconds, or a thread gets cut mid-conversation.
CHANNEL_DEFAULTS = {
    "api_version": meta_wa.DEFAULT_API_VERSION,
    "idle_minutes": 10,
    "images": "auto",
    "voice_reply": "match",
    "history_turns": 6,
}


class ChannelReq(BaseModel):
    type: Literal["whatsapp"] = "whatsapp"
    phone_number_id: str = Field(min_length=5, max_length=40)
    waba_id: str | None = Field(default=None, max_length=40)
    access_token: str = Field(min_length=20)
    app_secret: str = Field(min_length=8)
    api_version: str | None = None
    idle_minutes: int = Field(default=10, ge=1, le=60)
    images: Literal["auto", "off"] = "auto"
    # match answers the way the customer asked; the other two override it.
    # The legacy names are still accepted because stored rows carry them.
    voice_reply: Literal["match", "text", "voice",
                         "mirror", "always", "off"] = "match"
    history_turns: int = Field(default=6, ge=0, le=20)


class ChannelPatch(BaseModel):
    status: Literal["active", "paused"] | None = None
    access_token: str | None = None
    app_secret: str | None = None
    api_version: str | None = None
    idle_minutes: int | None = Field(default=None, ge=1, le=60)
    images: Literal["auto", "off"] | None = None
    voice_reply: Literal["match", "text", "voice",
                         "mirror", "always", "off"] | None = None
    history_turns: int | None = Field(default=None, ge=0, le=20)


class ChannelTestReq(BaseModel):
    to: str = Field(min_length=6, max_length=20)
    body: str = Field(default="Test message from Arya.", max_length=500)


def _mint_channel_id() -> str:
    return "chan-" + secrets.token_hex(6)


def _webhook_url(request: Request) -> str:
    """Where Meta should POST. The bridge listens on its own port, so a
    deployment behind a proxy sets ARYA_WEBHOOK_BASE_URL to the public
    origin that routes /webhooks/whatsapp to it; locally this is whatever
    tunnel is pointed at port 8030."""
    base = os.environ.get("ARYA_WEBHOOK_BASE_URL", "").rstrip("/")
    return f"{base or _public_base(request)}/webhooks/whatsapp"


def _agent_vision(row) -> bool:
    """Whether the agent behind this channel can read an image at all.

    Drives the hint on the card: with a text-only model, Photos set to
    auto still declines politely, and the operator should be told why
    rather than discovering it from a customer.
    """
    agent_id = dict(row).get("agent_id")
    if not agent_id:
        return False
    cfg = config.load_saved_agent(agent_id)
    return bool(cfg and config.vision_capable(cfg))


def _channel_out(row, request: Request) -> dict:
    cfg = row["config"] if isinstance(row["config"], dict) else json.loads(
        row["config"] or "{}"
    )
    secret = row["secret"] if isinstance(row["secret"], dict) else json.loads(
        row["secret"] or "{}"
    )
    merged = {**CHANNEL_DEFAULTS, **cfg}
    return {
        "id": row["id"],
        "deployment_id": str(row["deployment_id"]),
        "type": row["type"],
        "status": row["status"],
        "phone_number_id": merged.get("phone_number_id"),
        "waba_id": merged.get("waba_id"),
        "display_number": merged.get("display_number"),
        "verified_name": merged.get("verified_name"),
        "quality_rating": merged.get("quality_rating"),
        "api_version": merged.get("api_version"),
        "idle_minutes": merged.get("idle_minutes"),
        "images": merged.get("images"),
        # Normalized on the way out, so a row written under the old
        # names still matches an option in the card's select.
        "voice_reply": config.normalize_voice_mode(merged.get("voice_reply")),
        "history_turns": merged.get("history_turns"),
        # Whether Meta will actually deliver to us: the account has to
        # list this app among its webhook subscribers, which Arya arranges
        # on connect because nothing in Meta's own flow does. null, not
        # false, on a channel connected before Arya checked: a number that
        # is working must not be reported as broken on no evidence.
        "webhook_subscribed": (
            None if merged.get("webhook_subscribed") is None
            else bool(merged["webhook_subscribed"])
        ),
        "subscribe_error": merged.get("subscribe_error"),
        "app_id": merged.get("app_id"),
        "agent_vision": _agent_vision(row),
        # Credentials never leave in full. The verify token does: the
        # operator has to paste it into Meta's console, and it is only a
        # shared handshake string, not an authority over anything.
        "access_token_masked": config.mask_secret(secret.get("access_token", "")),
        "app_secret_masked": config.mask_secret(secret.get("app_secret", "")),
        "verify_token": secret.get("verify_token"),
        # Shown masked behind Show, as the public key is; Copy uses the
        # full value above.
        "verify_token_masked": config.mask_secret(secret.get("verify_token", "")),
        "webhook_url": _webhook_url(request),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_event_at": row["last_event_at"],
    }


async def _deployment_for_agent(pool, agent_id: str):
    row = await pool.fetchrow(
        "SELECT * FROM deployments WHERE agent_id = $1", agent_id
    )
    if row is None or row["status"] == "revoked":
        raise HTTPException(
            404, "publish this agent before adding a channel"
        )
    return row


async def _channel_or_404(pool, channel_id: str):
    row = await pool.fetchrow(
        "SELECT c.*, d.agent_id FROM channels c"
        " JOIN deployments d ON d.id = c.deployment_id WHERE c.id = $1",
        channel_id,
    )
    if row is None:
        raise HTTPException(404, "unknown channel")
    return row


def _channel_client(row) -> meta_wa.MetaClient:
    cfg = row["config"] if isinstance(row["config"], dict) else json.loads(
        row["config"] or "{}"
    )
    secret = row["secret"] if isinstance(row["secret"], dict) else json.loads(
        row["secret"] or "{}"
    )
    return meta_wa.MetaClient(
        cfg.get("phone_number_id", ""),
        secret.get("access_token", ""),
        api_version=cfg.get("api_version") or meta_wa.DEFAULT_API_VERSION,
    )


async def _probe_number(client: meta_wa.MetaClient, *,
                        close: bool = True) -> dict:
    """Validate the credentials against Meta before anything is stored, the
    same rule connections follow: a wrong token must fail on the Add dialog
    with a readable reason, not silently at the first customer message."""
    try:
        return await client.profile()
    except meta_wa.MetaError as e:
        await client.aclose()
        raise HTTPException(400, f"Meta refused these details: {e}") from None
    finally:
        if close:
            await client.aclose()


async def _link_account(client: meta_wa.MetaClient, cfg: dict) -> None:
    """Derive the WhatsApp Business Account id and subscribe this app to it.

    Meta's setup flow leaves a new app off its own account's subscriber
    list, with no screen anywhere to fix it, so the webhook verifies and
    not one message is ever delivered. Arya does it on connect and on a
    token rotation so no operator meets that trap.

    Best effort by design: a number whose credentials are good still
    connects if this fails, and the card shows the failure with a repair
    button. Mutates cfg in place; never raises.
    """
    try:
        cfg["app_id"] = await client.app_id() or cfg.get("app_id")
    except meta_wa.MetaError as e:
        logger.debug(f"channel: could not read the app id: {e}")
    try:
        if not cfg.get("waba_id"):
            cfg["waba_id"] = (await client.waba_id()) or None
        waba = cfg.get("waba_id")
        if not waba:
            cfg["webhook_subscribed"] = False
            cfg["subscribe_error"] = (
                "Could not tell which WhatsApp Business Account this number"
                " belongs to. Add the account id and reconnect."
            )
            return
        await client.subscribe(waba)
        ours = cfg.get("app_id")
        ids = meta_wa.subscriber_ids(await client.subscribed_apps(waba))
        cfg["webhook_subscribed"] = (ours in ids) if ours else bool(ids)
        cfg["subscribe_error"] = None if cfg["webhook_subscribed"] else (
            "Meta accepted the subscription but does not list this app on"
            " the account. Check the token belongs to this app."
        )
    except meta_wa.MetaError as e:
        cfg["webhook_subscribed"] = False
        cfg["subscribe_error"] = str(e)


@api.get("/agents/{agent_id}/channels")
async def list_channels(agent_id: str, request: Request):
    pool = await db.get_pool(_dsn())
    dep = await pool.fetchrow(
        "SELECT id FROM deployments WHERE agent_id = $1 AND status <> 'revoked'",
        agent_id,
    )
    if dep is None:
        return {"channels": []}
    rows = await pool.fetch(
        "SELECT c.*, d.agent_id FROM channels c"
        " JOIN deployments d ON d.id = c.deployment_id"
        " WHERE c.deployment_id = $1 ORDER BY c.created_at",
        dep["id"],
    )
    return {"channels": [_channel_out(r, request) for r in rows]}


@api.post("/agents/{agent_id}/channels", status_code=201)
async def add_channel(agent_id: str, req: ChannelReq, request: Request):
    """Attach a WhatsApp number to a published agent. The credentials are
    probed against Meta first, so the row that lands is one that works."""
    pool = await db.get_pool(_dsn())
    dep = await _deployment_for_agent(pool, agent_id)
    phone_id = req.phone_number_id.strip()
    taken = await pool.fetchrow(
        "SELECT c.id, d.agent_id FROM channels c"
        " JOIN deployments d ON d.id = c.deployment_id"
        " WHERE c.config->>'phone_number_id' = $1",
        phone_id,
    )
    if taken:
        raise HTTPException(
            409, f"that number already answers as '{taken['agent_id']}'"
        )
    client = meta_wa.MetaClient(
        phone_id, req.access_token.strip(),
        api_version=req.api_version or meta_wa.DEFAULT_API_VERSION,
    )
    profile = await _probe_number(client, close=False)
    cfg = {
        **CHANNEL_DEFAULTS,
        "phone_number_id": phone_id,
        "waba_id": (req.waba_id or "").strip() or None,
        "api_version": req.api_version or meta_wa.DEFAULT_API_VERSION,
        "idle_minutes": req.idle_minutes,
        "images": req.images,
        "voice_reply": req.voice_reply,
        "history_turns": req.history_turns,
        "display_number": profile.get("display_phone_number"),
        "verified_name": profile.get("verified_name"),
        "quality_rating": profile.get("quality_rating"),
    }
    try:
        await _link_account(client, cfg)
    finally:
        await client.aclose()
    secret = {
        "access_token": req.access_token.strip(),
        "app_secret": req.app_secret.strip(),
        # Ours to choose: Meta echoes it back on the one-time verification
        # GET and never again, so it only has to be unguessable.
        "verify_token": secrets.token_urlsafe(18),
    }
    channel_id = _mint_channel_id()
    await pool.execute(
        "INSERT INTO channels (id, deployment_id, type, status, config, secret)"
        " VALUES ($1, $2, $3, 'active', $4::jsonb, $5::jsonb)",
        channel_id, dep["id"], req.type, json.dumps(cfg), json.dumps(secret),
    )
    return _channel_out(await _channel_or_404(pool, channel_id), request)


@api.patch("/channels/{channel_id}")
async def patch_channel(channel_id: str, req: ChannelPatch, request: Request):
    """Change behaviour or rotate credentials. A new token is re-probed
    before it replaces a working one."""
    pool = await db.get_pool(_dsn())
    row = await _channel_or_404(pool, channel_id)
    cfg = json.loads(row["config"] or "{}")
    secret = json.loads(row["secret"] or "{}")
    for key in ("api_version", "idle_minutes", "images", "voice_reply",
                "history_turns"):
        value = getattr(req, key)
        if value is not None:
            cfg[key] = value
    if req.app_secret:
        secret["app_secret"] = req.app_secret.strip()
    if req.access_token:
        secret["access_token"] = req.access_token.strip()
        client = meta_wa.MetaClient(
            cfg.get("phone_number_id", ""), secret["access_token"],
            api_version=cfg.get("api_version") or meta_wa.DEFAULT_API_VERSION,
        )
        profile = await _probe_number(client, close=False)
        cfg["display_number"] = profile.get("display_phone_number")
        cfg["verified_name"] = profile.get("verified_name")
        cfg["quality_rating"] = profile.get("quality_rating")
        # A new token can belong to a different app, which would leave the
        # old one subscribed and this one deaf.
        try:
            await _link_account(client, cfg)
        finally:
            await client.aclose()
    status = req.status or row["status"]
    await pool.execute(
        "UPDATE channels SET status=$2, config=$3::jsonb, secret=$4::jsonb,"
        " updated_at=now() WHERE id=$1",
        channel_id, status, json.dumps(cfg), json.dumps(secret),
    )
    return _channel_out(await _channel_or_404(pool, channel_id), request)


@api.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str):
    pool = await db.get_pool(_dsn())
    await _channel_or_404(pool, channel_id)
    # Threads and events are history, not state: the thread rows go (they
    # point at rooms that no longer exist), the event log stays so an
    # operator can still audit what the number did.
    await pool.execute("DELETE FROM channels WHERE id = $1", channel_id)
    return {"channel_id": channel_id, "deleted": True}


@api.post("/channels/{channel_id}/resubscribe")
async def resubscribe_channel(channel_id: str, request: Request):
    """Redo the account subscription for a channel that lost it.

    Needed when the number was moved to another account, the token was
    replaced outside Arya, or someone unsubscribed the app by hand. Same
    work connect does, on demand.
    """
    pool = await db.get_pool(_dsn())
    row = await _channel_or_404(pool, channel_id)
    cfg = json.loads(row["config"] or "{}")
    client = _channel_client(row)
    try:
        await _link_account(client, cfg)
    finally:
        await client.aclose()
    await pool.execute(
        "UPDATE channels SET config=$2::jsonb, updated_at=now() WHERE id=$1",
        channel_id, json.dumps(cfg),
    )
    return _channel_out(await _channel_or_404(pool, channel_id), request)


@api.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str, req: ChannelTestReq):
    """Send one message to a number you own. Meta only delivers this if
    that number is a registered test recipient or has messaged us in the
    last 24 hours; anything else comes back as Meta's own error."""
    pool = await db.get_pool(_dsn())
    row = await _channel_or_404(pool, channel_id)
    client = _channel_client(row)
    to = re.sub(r"[^0-9]", "", req.to)
    try:
        wamid = await client.send_text(to, req.body, preview_url=False)
    except meta_wa.MetaError as e:
        raise HTTPException(400, str(e)) from None
    finally:
        await client.aclose()
    return {"sent": True, "wamid": wamid, "to": to}


# ─── in-app operator docs ────────────────────────────────────────────────────
# A hardcoded allowlist, NOT a path parameter joined onto a directory: the
# dict itself is the guard, so there is no traversal surface to reason
# about. Served as JSON so the frontend's req() helper handles the admin
# header, the 401 signal and the error shape with no new plumbing.
_DOCS = {
    "deployment": ("DEPLOYMENT.md", "Deploying an engine on GCP"),
    "connectors": ("CONNECTORS.md", "Connecting external tools"),
    "channels": ("CHANNELS.md", "Answering on WhatsApp"),
}


@api.get("/docs/{name}")
async def get_doc(name: str):
    entry = _DOCS.get(name)
    if entry is None:
        raise HTTPException(404, "unknown document")
    path = config.BACKEND_DIR.parent / "docs" / entry[0]
    if not path.is_file():
        # Almost always an image built before docs/ was COPYied in.
        raise HTTPException(
            503, "docs are not shipped in this build — rebuild the image"
        )
    return {
        "name": name,
        "title": entry[1],
        "markdown": path.read_text(encoding="utf-8"),
    }


# ─── export / import / push: agents travel builder -> engine as data ─────────
# The zip carries agent.json + the ORIGINAL doc files + a manifest.
# Never keys (engines have their own), never vectors (embedder-coupled —
# re-ingesting the sources on the target is the only correct move).


@api.get("/agents/{agent_id}/export")
async def export_agent(agent_id: str, include_docs: bool = True):
    cfg = config.load_saved_agent(agent_id)
    if cfg is None:
        raise HTTPException(404, "unknown agent")
    buf = io.BytesIO()
    docs_meta = []
    # Connection ids are local to this builder (the engine has its own
    # connections table, and the credentials behind them never travel), so
    # an exported agent lands with no connectors attached.
    cfg_out = {k: v for k, v in cfg.items() if k != "connectors"}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("agent.json", json.dumps(cfg_out, indent=2, ensure_ascii=False))
        if include_docs:
            pool = await db.get_pool(_dsn())
            rows = await pool.fetch(
                "SELECT filename, object_key, content_type FROM knowledge_base"
                " WHERE agent_id = $1 AND status = 'ready'",
                agent_id,
            )
            for r in rows:
                try:
                    obj = minio.get_object(S3["bucket"], r["object_key"])
                    zf.writestr(f"docs/{r['filename']}", obj.read())
                    docs_meta.append(
                        {"filename": r["filename"],
                         "content_type": r["content_type"]}
                    )
                except Exception as e:
                    logger.warning(f"export skipped doc {r['filename']}: {e}")
        zf.writestr("manifest.json", json.dumps({
            "arya_export": 1, "agent_id": agent_id, "docs": docs_meta,
        }))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="{agent_id}.zip"'},
    )


@api.post("/agents/import")
async def import_agent(file: UploadFile = File(...)):
    """Idempotent by agent id (re-push = update). Docs are re-ingested
    through the normal pipeline; same-name docs already on this instance
    are skipped so re-pushes don't duplicate the knowledge base."""
    data = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        cfg = json.loads(zf.read("agent.json"))
    except Exception:
        raise HTTPException(400, "not a valid agent export zip")
    agent_id = cfg.get("id") or ""
    if not agent_id:
        raise HTTPException(400, "agent.json has no id")
    # An export carries no connectors (they hold credentials), but THIS
    # machine may have had some attached to the agent since the last push,
    # through PUT /agents/{id}/connectors. Seen live: a re-push to switch
    # the model silently detached the shop database on the engine, and the
    # agent went back to asking for an email. What was attached here stays
    # attached; the push updates the agent, not what it reaches.
    prev = config.load_saved_agent(agent_id)
    if prev and prev.get("connectors") and not cfg.get("connectors"):
        cfg["connectors"] = prev["connectors"]
    config.save_agent(cfg)
    pool = await db.get_pool(_dsn())
    existing = {
        r["filename"] for r in await pool.fetch(
            "SELECT filename FROM knowledge_base WHERE agent_id = $1", agent_id
        )
    }
    manifest = json.loads(zf.read("manifest.json")) if "manifest.json" in zf.namelist() else {"docs": []}
    ingested, skipped = 0, 0
    for d in manifest.get("docs", []):
        name = d.get("filename", "")
        if not name or f"docs/{name}" not in zf.namelist():
            continue
        if name in existing:
            skipped += 1
            continue
        blob = zf.read(f"docs/{name}")
        doc_id = uuid.uuid4()
        key = f"{doc_id}/{name}"
        minio.put_object(
            S3["bucket"], key, io.BytesIO(blob), len(blob),
            content_type=d.get("content_type") or "application/octet-stream",
        )
        await pool.execute(
            "INSERT INTO knowledge_base (id, filename, content_type,"
            " size_bytes, object_key, status, agent_id, embedding_profile,"
            " progress) VALUES ($1,$2,$3,$4,$5,'pending',$6,$7,0)",
            doc_id, name, d.get("content_type"), len(blob), key, agent_id,
            config.embedding_profile(cfg, config.load_keys())["name"],
        )
        ingested += 1
    return {"agent_id": agent_id, "imported": True,
            "docs_ingesting": ingested, "docs_skipped": skipped}


class PushReq(BaseModel):
    # Either a saved engine (engine_id) or manual runtime_url + admin_token.
    runtime_url: str | None = None
    admin_token: str | None = None
    engine_id: str | None = None
    include_docs: bool = True
    # None = leave the engine's allowlist untouched; a list (possibly empty)
    # replaces it. The UI always sends the field's parsed value, so what the
    # user sees in the field is what the engine ends up with.
    allowed_origins: list[str] | None = None


class ConnectorsReq(BaseModel):
    connectors: list[dict]


@api.get("/agents/{agent_id}/connectors")
async def get_connectors(agent_id: str):
    """The attachments a saved agent carries, as Options stores them."""
    cfg = config.load_saved_agent(agent_id)
    if cfg is None:
        raise HTTPException(404, "unknown agent")
    return {"connectors": [a for a in (cfg.get("connectors") or [])
                           if isinstance(a, dict)]}


@api.put("/agents/{agent_id}/connectors")
async def set_connectors(agent_id: str, req: ConnectorsReq):
    """Replace a saved agent's attachments. Exists for engines: a pushed
    agent lands with none (connections never travel), and an engine has
    no Options screen, so the builder sets them here over the relay. Each
    attachment must name a connection this machine holds; the worker
    reads the file fresh on the next call, so nothing restarts."""
    cfg = config.load_saved_agent(agent_id)
    if cfg is None:
        raise HTTPException(404, "unknown agent")
    wanted = [a for a in req.connectors if isinstance(a, dict) and a.get("connection_id")]
    ids = [a["connection_id"] for a in wanted]
    if ids:
        pool = await db.get_pool(_dsn())
        rows = await pool.fetch(
            "SELECT id, type FROM connections WHERE id = ANY($1::text[])", ids
        )
        known = {r["id"]: r["type"] for r in rows}
        missing = [i for i in ids if i not in known]
        if missing:
            raise HTTPException(
                404, f"no such connection here: {', '.join(missing)}"
            )
        for a in wanted:
            a["type"] = known[a["connection_id"]]
            a.setdefault("allowed_tools", [])
            a.setdefault("public_tools", [])
    cfg["connectors"] = wanted
    config.save_agent(cfg)
    if config.load_active().get("id") == agent_id:
        config.save_active(cfg)
    return {"connectors": wanted}


@api.post("/agents/{agent_id}/push")
async def push_agent(agent_id: str, req: PushReq):
    """One click, builder -> engine: export locally, import on the engine,
    publish there, apply the website allowlist there, return the engine's
    snippet + share link. Manual credentials go outward over the user's own
    HTTPS and are never stored; a saved engine_id resolves them server-side.

    The allowlist rides the push because it only matters ON the engine
    (localhost isn't internet-reachable); it is validated locally BEFORE
    anything ships, so a scheme-less origin fails fast instead of after
    the agent is already imported."""
    if req.engine_id:
        eng = _engine_or_404(req.engine_id)
        base, token = eng["base_url"], eng["admin_token"]
    elif req.runtime_url and req.admin_token:
        base, token = req.runtime_url, req.admin_token
    else:
        raise HTTPException(400, "provide engine_id, or runtime_url + admin_token")
    base = base.rstrip("/")
    origins = (
        _norm_origins(req.allowed_origins)
        if req.allowed_origins is not None
        else None
    )
    export = await export_agent(agent_id, include_docs=req.include_docs)
    hdrs = {"X-Arya-Admin-Token": token}
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{base}/api/agents/import", headers=hdrs,
            files={"file": (f"{agent_id}.zip", export.body, "application/zip")},
        )
        if r.status_code != 200:
            raise HTTPException(502, f"engine import failed: {r.text[:200]}")
        p = await client.post(
            f"{base}/api/agents/{agent_id}/deployment", headers=hdrs
        )
        if p.status_code != 200:
            raise HTTPException(502, f"engine publish failed: {p.text[:200]}")
        deployment = p.json()
        if origins is not None:
            w = await client.patch(
                f"{base}/api/agents/{agent_id}/deployment", headers=hdrs,
                json={"allowed_origins": origins},
            )
            if w.status_code != 200:
                # The agent IS live at this point — surface that honestly
                # rather than a bare failure.
                raise HTTPException(
                    502,
                    "agent pushed and published, but applying the website"
                    f" allowlist failed: {w.text[:200]}",
                )
            deployment = w.json()
    return {"pushed": True, "import": r.json(), "deployment": deployment}


# ─── connectors (MCP): external systems an agent can act on ──────────────────
# Registry = the `connectors` catalog; instances = the `connections` table
# (GLOBAL, see db.py); per-agent attachment = cfg["connectors"]. The browser
# never sees a credential: rows go out through _connection_out() masked, the
# same posture as engines. Non-OAuth connections are probed (tools listed)
# before they are stored, like engines. OAuth: `start` is a POST on the admin
# router (the SPA carries the admin header and receives the URL to send the
# browser to); the `callback` sits on open_rt because Google redirects the
# browser there with no header of ours, and finds its row through a random,
# single-use, expiring `state`.


class ConnectionReq(BaseModel):
    type: str
    name: str = Field(min_length=1, max_length=60)
    url: str | None = None
    credential: str | None = None


class ConnectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    url: str | None = None
    credential: str | None = None


class OauthStartReq(BaseModel):
    # Where the browser should land afterwards (the SPA's origin + path),
    # so the Vite dev server at :5173 gets its user back too. Falls back to
    # PUBLIC_BASE_URL / this server.
    return_url: str | None = None


def _connector_entry_or_400(type_id: str) -> dict:
    entry = config.connector_type(type_id)
    if entry is None:
        known = ", ".join(c["id"] for c in catalog.get("connectors"))
        raise HTTPException(400, f"unknown connector type '{type_id}' (one of: {known})")
    return entry


def _attached_agents(conn_id: str) -> list[dict]:
    """Saved agents whose config attaches this connection."""
    out = []
    for summary in config.list_saved_agents():
        cfg = config.load_saved_agent(summary["id"]) or {}
        if any(
            isinstance(a, dict) and a.get("connection_id") == conn_id
            for a in cfg.get("connectors") or []
        ):
            out.append({"id": summary["id"], "name": summary["name"]})
    return out


def _detach_everywhere(conn_id: str) -> None:
    """Strip a connection from every saved agent (and the active one)."""

    def strip(cfg: dict) -> bool:
        before = cfg.get("connectors") or []
        after = [
            a for a in before
            if not (isinstance(a, dict) and a.get("connection_id") == conn_id)
        ]
        if len(after) == len(before):
            return False
        cfg["connectors"] = after
        return True

    for summary in config.list_saved_agents():
        cfg = config.load_saved_agent(summary["id"])
        if cfg and strip(cfg):
            config.save_agent(cfg)
    try:
        active = config.load_active()
        if strip(active):
            config.save_active(active)
    except RuntimeError:
        pass


def _iso(ts) -> str | None:
    return ts.isoformat() if ts else None


def _absorb_check(cfg: dict, check: dict) -> None:
    """What a sidecar module's check route learned about the credential,
    kept on the connection so the card and the prompt can use it: a label
    ("Duffel (test mode)") and, where the module reports one, the mode
    itself, which the connector prompt block reads as {mode}."""
    label = check.get("label")
    if label:
        cfg["label"] = label
    mode = check.get("mode")
    if mode:
        cfg["mode"] = mode
    # Whether this database has the order desk's own tables, so the desk
    # runs over it instead of being replaced (connectors._kept_toolsets).
    # Always rewritten when the check reports it, so a database that loses
    # the tables loses the flag.
    if "shop_desk" in check:
        cfg["shop_desk"] = bool(check.get("shop_desk"))


async def _learn_sender(entry: dict, row: dict, cfg: dict, tools: list) -> None:
    """Which mailbox this connector sends from, asked once when the owner
    presses Test. Every email the agent sends leaves from it, so the owner
    should see it on the card, and a booking's agency copy goes there by
    default. Best effort: a server with no mail toolkit simply has no
    address, and the card says so rather than failing the save."""
    via = entry.get("email_via")
    if not isinstance(via, dict) or not via.get("profile_slug"):
        return
    names = {t.get("name") for t in (tools or []) if isinstance(t, dict)}
    if via.get("tool") not in names:
        cfg.pop("account_email", None)
        return
    conn = {
        "url": connectors.server_url(entry, row),
        "headers": connectors.auth_headers(entry, row.get("secret")),
        "transport": connectors.transport_for(entry, row),
        "tool_timeout_s": connectors.PROBE_TIMEOUT_S,
        "row": {"name": row.get("name", "")},
    }
    args = ({"tools": [{"tool_slug": via["profile_slug"], "arguments": {}}]}
            if via.get("kind") == "composio" else {})
    try:
        result = await connectors.call_tool(conn, via["tool"], args)
    except Exception as e:
        logger.warning(f"sender lookup failed for {row.get('name')}: {e}")
        return
    found = _first_email(result.get("text") or "")
    if found:
        cfg["account_email"] = found
    else:
        cfg.pop("account_email", None)


_EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _first_email(text: str) -> str:
    """The connected account's address out of whatever the vendor answered.
    Composio wraps its results differently per toolkit and per version, so
    this reads the text rather than trusting one field's path."""
    for hit in _EMAIL_IN_TEXT.findall(text or ""):
        # Skip the obvious non-accounts a response can carry.
        if not hit.lower().endswith((".png", ".jpg", ".svg")):
            return hit
    return ""


def _strip_hidden_from_agents(conn_id: str, hidden: set[str]) -> None:
    """A tool the check hid must leave every agent that ticked it, or the
    stale attachment keeps two lies alive: replaced_toolsets switches the
    sample desk off for a tool that can only fail, and the worker offers
    the model a tool the account cannot use. Seen live: Duffel's hotel
    tools, ticked before Stays turned out to be off, silenced the SerpAPI
    hotel desk and then answered 403 themselves."""
    if not hidden:
        return

    def strip(cfg: dict) -> bool:
        changed = False
        for att in cfg.get("connectors") or []:
            if not isinstance(att, dict) or att.get("connection_id") != conn_id:
                continue
            for key in ("allowed_tools", "public_tools"):
                names = att.get(key) or []
                kept = [t for t in names if t not in hidden]
                if len(kept) != len(names):
                    att[key] = kept
                    changed = True
        return changed

    for summary in config.list_saved_agents():
        cfg = config.load_saved_agent(summary["id"])
        if cfg and strip(cfg):
            config.save_agent(cfg)
    try:
        active = config.load_active()
        if strip(active):
            config.save_active(active)
    except RuntimeError:
        pass


def _new_read_tools(entry: dict, before: list, after: list) -> set[str]:
    """Read tools this probe found that the connection had never listed,
    and only for a connector ARYA ITSELF HOSTS.

    Two exclusions, both learned the hard way:

    A write tool is never offered. Shipping one must not arm it on
    somebody's live agent.

    A vendor-hosted connector is never offered either, whatever the tool
    does. Arya decides what ships in its own sidecar, so a new tool there
    is one we added on purpose and an owner who ticked that connector
    wanted it. A router like Composio adds tools on its own schedule for
    its own reasons: it grew SEARCH_SKILLS and USE_SKILL between two
    probes, both reads, both nothing to do with the travel agent holding
    the connection, and both were silently switched on. More surface on a
    voice agent is a real risk, not a neutral one, so a vendor's new tools
    are listed on the card and left for the owner to choose.
    """
    if not connectors.is_sidecar(entry or {}):
        return set()
    known = {x.get("name") for x in (before or []) if isinstance(x, dict)}
    return {
        x.get("name") for x in (after or [])
        if isinstance(x, dict) and x.get("name") and x["name"] not in known
        and not x.get("write")
    }


def _offer_new_tools_to_agents(conn_id: str, added: set[str]) -> None:
    """Tick a newly discovered read tool on every agent already holding
    this connection.

    The mirror of _strip_hidden_from_agents. Without it a tool Arya ships
    is invisible until every owner notices it and ticks it by hand: seen
    live, plan_trip sat in the running image through two calls while the
    agent planned days one at a time because its connection predated the
    tool. Public (embed) tools are NOT touched: what a website visitor may
    trigger stays a deliberate choice."""
    if not added:
        return

    def offer(cfg: dict) -> bool:
        changed = False
        for att in cfg.get("connectors") or []:
            if not isinstance(att, dict) or att.get("connection_id") != conn_id:
                continue
            names = list(att.get("allowed_tools") or [])
            fresh = [t for t in sorted(added) if t not in names]
            if fresh:
                att["allowed_tools"] = names + fresh
                changed = True
        return changed

    for summary in config.list_saved_agents():
        cfg = config.load_saved_agent(summary["id"])
        if cfg and offer(cfg):
            config.save_agent(cfg)
            logger.info(f"agent '{summary['id']}': new tools ticked {sorted(added)}")
    try:
        active = config.load_active()
        if offer(active):
            config.save_active(active)
    except RuntimeError:
        pass


async def _prune_attachments(cfg: dict) -> None:
    """Drop ticked tool names a connection no longer lists, on every save.

    The Options page keeps the attachment in memory. Left open across a
    Test that hid tools, its next save writes the old list straight back,
    and a dead tick then switches off the sample desk that was covering
    for those tools (seen live, twice). The worker ignores such ticks too;
    this keeps Preview and the saved file honest as well.
    """
    atts = [a for a in (cfg.get("connectors") or []) if isinstance(a, dict)]
    if not atts:
        return
    try:
        pool = await db.get_pool(_dsn())
        rows = await pool.fetch(
            "SELECT id, tools FROM connections WHERE id = ANY($1::text[])",
            [str(a.get("connection_id") or "") for a in atts],
        )
    except Exception as e:
        logger.warning(f"attachment prune skipped: {e}")
        return
    listed: dict[str, set[str]] = {}
    for r in rows:
        d = connectors.row_to_dict(r)
        listed[d["id"]] = {t.get("name") for t in (d.get("tools") or [])
                           if isinstance(t, dict)}
    for a in atts:
        names = listed.get(a.get("connection_id"))
        if not names:  # unknown row or never probed: leave the ticks alone
            continue
        for key in ("allowed_tools", "public_tools"):
            a[key] = [t for t in (a.get(key) or []) if t in names]


def _visible_tools(tools: list, check: dict) -> list:
    """Drop tools the module's check says this account cannot use (Duffel
    hotel tools while Duffel Stays is off), so the owner is never offered a
    tick that can only fail. Test again once the vendor enables it."""
    hidden = set(check.get("hide_tools") or [])
    if not hidden:
        return tools
    return [t for t in tools if t.get("name") not in hidden]


def _connection_out(row: dict) -> dict:
    entry = config.connector_type(row["type"]) or {}
    token = connectors.secret_value(entry, row.get("secret"))
    cfg = row.get("config") or {}
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "url": connectors.server_url(entry, row) if entry else row.get("url"),
        "transport": row.get("transport"),
        "status": row["status"],
        "error": row.get("error"),
        "account_email": cfg.get("account_email"),
        # What this connection points at, safe to show: the signed-in
        # account, or "user@database" for a pasted DSN (never the password).
        "detail": cfg.get("account_email") or cfg.get("label") or "",
        "warnings": cfg.get("warnings") or [],
        # The order desk will run over this database (it has the desk's
        # tables). The connector card and Preview read it.
        "shop_desk": bool(cfg.get("shop_desk")),
        "secret_masked": config.mask_secret(token) if token else "",
        "tools": row.get("tools") or [],
        "attached_agents": _attached_agents(row["id"]),
        "created_at": _iso(row.get("created_at")),
        "last_checked_at": _iso(row.get("last_checked_at")),
    }


async def _connection_or_404(pool, conn_id: str) -> dict:
    row = await pool.fetchrow("SELECT * FROM connections WHERE id = $1", conn_id)
    if row is None:
        raise HTTPException(404, "unknown connection (it may have been removed)")
    return connectors.row_to_dict(row)


async def _probe_row(pool, row: dict, entry: dict, keys: dict) -> dict:
    """List the server's tools and persist the outcome (tools, status,
    error, last_checked_at). OAuth rows get their token refreshed first;
    a dead grant flips the row to needs_auth and is returned as-is.

    Sidecar types are checked against the real system first (see
    connectors.check_credential): their tool list is static, so listing it
    would report a wrong password as "connected"."""
    if entry.get("auth") == "oauth":
        fresh = await connectors.ensure_fresh_token(pool, row, keys, entry)
        if fresh is None:
            return await _connection_or_404(pool, row["id"])
        row = fresh
    cfg = dict(row.get("config") or {})
    check = await connectors.check_credential(entry, row.get("secret"))
    if not check.get("ok"):
        err = check.get("error") or "the check failed"
        await pool.execute(
            "UPDATE connections SET status='error', error=$2,"
            " last_checked_at=now(), updated_at=now() WHERE id=$1",
            row["id"], err,
        )
        return {**row, "status": "error", "error": err}
    _absorb_check(cfg, check)
    cfg["warnings"] = check.get("warnings") or []
    url = connectors.server_url(entry, row)
    try:
        tools = await connectors.probe(
            url, connectors.transport_for(entry, row),
            connectors.auth_headers(entry, row.get("secret")), entry,
        )
        tools = _visible_tools(tools, check)
        _strip_hidden_from_agents(row["id"], set(check.get("hide_tools") or []))
        _offer_new_tools_to_agents(row["id"], _new_read_tools(entry, row.get("tools"), tools))
        await _learn_sender(entry, row, cfg, tools)
        status, err = "connected", None
    except connectors.ProbeError as e:
        tools, status, err = row.get("tools") or [], "error", str(e)
    await pool.execute(
        "UPDATE connections SET tools=$2::jsonb, status=$3, error=$4,"
        " config=$5::jsonb, last_checked_at=now(), updated_at=now()"
        " WHERE id=$1",
        row["id"], json.dumps(tools), status, err, json.dumps(cfg),
    )
    return {**row, "tools": tools, "status": status, "error": err, "config": cfg}


def _oauth_redirect_uri(request: Request) -> str:
    return f"{_public_base(request)}/api/connections/oauth/callback"


def _probe_http_status(e: connectors.ProbeError) -> int:
    text = str(e)
    return 502 if ("unreachable" in text or "timed out" in text) else 400


def _seed_category(connector_id: str | None) -> str:
    for e in catalog.SEEDS.get("connectors", []):
        if e.get("id") == connector_id and e.get("category"):
            return e["category"]
    return "Anything else"


@api.get("/connectors")
async def list_connectors(request: Request):
    """The connector types (catalog) plus what the UI needs to offer them:
    whether each OAuth app is configured, and the redirect URI to paste
    into the provider's console."""
    pool = await db.get_pool(_dsn())
    await catalog.refresh(pool)
    keys = config.load_keys()
    entries = catalog.get("connectors")
    # Only pay for the health check when a sidecar type is actually offered.
    sidecar_ready = None
    if any(connectors.is_sidecar(c) for c in entries):
        sidecar_ready = await connectors.sidecar_reachable()
    out = []
    for c in entries:
        item = dict(c)
        if c.get("auth") == "oauth":
            item["oauth_ready"] = connectors.oauth_ready(c, keys)
        if connectors.is_sidecar(c):
            item["sidecar_ready"] = bool(sidecar_ready)
        # A connectors catalog saved to the DB by an older build has no
        # category; fill it from the seed so the grouping never goes blank.
        if not item.get("category"):
            item["category"] = _seed_category(c.get("id"))
        out.append(item)
    return {
        "connectors": out,
        "categories": catalog.get("connector_categories"),
        "redirect_uri": _oauth_redirect_uri(request),
        "mcp_available": connectors.MCP_AVAILABLE,
        "sidecar_ready": sidecar_ready,
    }


@api.get("/connections")
async def list_connections(type: str | None = None):
    pool = await db.get_pool(_dsn())
    if type:
        rows = await pool.fetch(
            "SELECT * FROM connections WHERE type = $1 ORDER BY created_at", type
        )
    else:
        rows = await pool.fetch("SELECT * FROM connections ORDER BY created_at")
    return [_connection_out(connectors.row_to_dict(r)) for r in rows]


@api.post("/connections", status_code=201)
async def add_connection(req: ConnectionReq):
    """Non-OAuth types are probed before anything is stored (a typo'd URL
    or token fails here with a readable reason). OAuth types are created
    `needs_auth` and become usable after the sign-in round trip."""
    entry = _connector_entry_or_400(req.type)
    if not connectors.MCP_AVAILABLE:
        raise HTTPException(503, connectors.MCP_UNAVAILABLE_MSG)
    pool = await db.get_pool(_dsn())
    name = req.name.strip()
    url = None
    if entry.get("url_source", "user") == "user":
        url = (req.url or "").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "server URL must start with http:// or https://")
    credential = (req.credential or "").strip()
    row = {
        "id": connectors.mint_id(),
        "type": entry["id"],
        "name": name,
        "url": url,
        "transport": entry.get("transport", "streamable_http"),
        "status": "needs_auth",
        "error": None,
        "config": {},
        "secret": None,
        "tools": [],
    }
    if entry["auth"] == "oauth":
        await pool.execute(
            "INSERT INTO connections (id, type, name, url, transport, status,"
            " config, tools) VALUES ($1, $2, $3, $4, $5, 'needs_auth',"
            " '{}'::jsonb, '[]'::jsonb)",
            row["id"], row["type"], name, url, row["transport"],
        )
        return _connection_out(await _connection_or_404(pool, row["id"]))
    if entry["auth"] in ("bearer", "api_key", "dsn") and not credential \
            and not entry.get("credential_optional"):
        raise HTTPException(400, f"{entry.get('credential_label', 'a token')} is required")
    row["secret"] = connectors.secret_for(entry, credential)
    if url and await pool.fetchval(
        "SELECT 1 FROM connections WHERE type = $1 AND url = $2", entry["id"], url
    ):
        raise HTTPException(409, "a connection to this server is already saved")
    if entry["auth"] == "dsn" and await pool.fetchval(
        "SELECT 1 FROM connections WHERE type = $1 AND secret->>'dsn' = $2",
        entry["id"], credential,
    ):
        raise HTTPException(409, "a connection to this database is already saved")
    # Sidecar types are validated against the real system before anything is
    # stored: a wrong password must fail here with the reason, not later
    # mid-call. (A static MCP tool list would happily "connect" regardless.)
    cfg = {}
    check = await connectors.check_credential(entry, row["secret"])
    if not check.get("ok"):
        raise HTTPException(400, check.get("error") or "the credential was refused")
    _absorb_check(cfg, check)
    if check.get("warnings"):
        cfg["warnings"] = check["warnings"]
    try:
        tools = await connectors.probe(
            connectors.server_url(entry, row), row["transport"],
            connectors.auth_headers(entry, row["secret"]), entry,
        )
    except connectors.ProbeError as e:
        raise HTTPException(_probe_http_status(e), f"could not connect: {e}") from None
    tools = _visible_tools(tools, check)
    await _learn_sender(entry, row, cfg, tools)
    await pool.execute(
        "INSERT INTO connections (id, type, name, url, transport, status,"
        " config, secret, tools, last_checked_at) VALUES ($1, $2, $3, $4, $5,"
        " 'connected', $6::jsonb, $7::jsonb, $8::jsonb, now())",
        row["id"], row["type"], name, url, row["transport"], json.dumps(cfg),
        json.dumps(row["secret"]) if row["secret"] else None, json.dumps(tools),
    )
    return _connection_out(await _connection_or_404(pool, row["id"]))


@api.patch("/connections/{conn_id}")
async def patch_connection(conn_id: str, req: ConnectionPatch):
    """Rename, or change URL/token on a non-OAuth connection (re-probed
    before saving, same rule as create)."""
    pool = await db.get_pool(_dsn())
    row = await _connection_or_404(pool, conn_id)
    entry = _connector_entry_or_400(row["type"])
    name = req.name.strip() if req.name else row["name"]
    url, secret, reprobe = row.get("url"), row.get("secret"), False
    if entry["auth"] != "oauth":
        if req.url is not None and entry.get("url_source", "user") == "user":
            url = req.url.strip().rstrip("/")
            if not url.startswith(("http://", "https://")):
                raise HTTPException(400, "server URL must start with http:// or https://")
            reprobe = reprobe or url != row.get("url")
        if req.credential is not None:
            secret = connectors.secret_for(entry, req.credential)
            reprobe = True
    candidate = {**row, "name": name, "url": url, "secret": secret}
    cfg = dict(row.get("config") or {})
    tools, status, err = row.get("tools") or [], row["status"], row.get("error")
    if reprobe:
        check = await connectors.check_credential(entry, secret)
        if not check.get("ok"):
            raise HTTPException(400, check.get("error") or "the credential was refused")
        _absorb_check(cfg, check)
        cfg["warnings"] = check.get("warnings") or []
        try:
            tools = await connectors.probe(
                connectors.server_url(entry, candidate), row.get("transport"),
                connectors.auth_headers(entry, secret), entry,
            )
            tools = _visible_tools(tools, check)
            _strip_hidden_from_agents(row["id"], set(check.get("hide_tools") or []))
            _offer_new_tools_to_agents(row["id"], _new_read_tools(entry, row.get("tools"), tools))
            await _learn_sender(entry, candidate, cfg, tools)
            status, err = "connected", None
        except connectors.ProbeError as e:
            raise HTTPException(_probe_http_status(e), f"could not connect: {e}") from None
    await pool.execute(
        "UPDATE connections SET name=$2, url=$3, secret=$4::jsonb, tools=$5::jsonb,"
        " status=$6, error=$7, config=$9::jsonb, updated_at=now(),"
        " last_checked_at = CASE WHEN $8 THEN now() ELSE last_checked_at END"
        " WHERE id=$1",
        conn_id, name, url, json.dumps(secret) if secret else None,
        json.dumps(tools), status, err, reprobe, json.dumps(cfg),
    )
    return _connection_out(await _connection_or_404(pool, conn_id))


@api.delete("/connections/{conn_id}")
async def remove_connection(conn_id: str, force: bool = False):
    """409 while any saved agent still attaches it, unless force=1, which
    also detaches it from those agents so no config points at a ghost."""
    pool = await db.get_pool(_dsn())
    await _connection_or_404(pool, conn_id)
    attached = _attached_agents(conn_id)
    if attached and not force:
        names = ", ".join(a["name"] for a in attached)
        raise HTTPException(
            409, f"still attached to: {names}. Detach it there first, or remove anyway."
        )
    if attached:
        _detach_everywhere(conn_id)
    await pool.execute("DELETE FROM connections WHERE id = $1", conn_id)
    return {"removed": True, "detached_from": attached}


@api.post("/connections/{conn_id}/test")
async def test_connection(conn_id: str):
    """Connect, list tools, persist the outcome. Returns the row (status
    connected | error | needs_auth) rather than failing, so the UI can show
    the state in place."""
    pool = await db.get_pool(_dsn())
    row = await _connection_or_404(pool, conn_id)
    entry = _connector_entry_or_400(row["type"])
    if not connectors.MCP_AVAILABLE:
        raise HTTPException(503, connectors.MCP_UNAVAILABLE_MSG)
    if entry["auth"] == "oauth" and not (row.get("secret") or {}).get("access_token"):
        raise HTTPException(400, "sign in first")
    return _connection_out(await _probe_row(pool, row, entry, config.load_keys()))


@api.post("/connections/{conn_id}/oauth/start")
async def connection_oauth_start(conn_id: str, req: OauthStartReq, request: Request):
    """Mint a single-use state, park it on the row, and hand the SPA the
    provider's consent URL to navigate to."""
    pool = await db.get_pool(_dsn())
    row = await _connection_or_404(pool, conn_id)
    entry = _connector_entry_or_400(row["type"])
    if entry["auth"] != "oauth":
        raise HTTPException(400, "this connector does not use sign-in")
    keys = config.load_keys()
    if not connectors.oauth_ready(entry, keys):
        raise HTTPException(
            400, f"add the {entry['label']} OAuth client id and secret first"
        )
    return_url = (req.return_url or "").strip()
    if return_url and not return_url.startswith(("http://", "https://")):
        return_url = ""
    cfg = {**(row.get("config") or {})}
    if return_url:
        cfg["return_url"] = return_url.rstrip("/")
    state = secrets.token_urlsafe(32)
    await pool.execute(
        "UPDATE connections SET oauth_state=$2, oauth_state_expires_at=$3,"
        " status='pending_auth', error=NULL, config=$4::jsonb, updated_at=now()"
        " WHERE id=$1",
        conn_id, state, connectors.utc_in(connectors.OAUTH_STATE_TTL_S),
        json.dumps(cfg),
    )
    o = entry["oauth"]
    client_id, _ = connectors.oauth_keys(entry, keys)
    params = {
        "client_id": client_id,
        "redirect_uri": _oauth_redirect_uri(request),
        "response_type": "code",
        "scope": " ".join(o.get("scopes") or []),
        "state": state,
        "include_granted_scopes": "true",
        **(o.get("extra_params") or {}),
    }
    from urllib.parse import urlencode
    return {
        "auth_url": f"{o['auth_url']}?{urlencode(params)}",
        "expires_in": connectors.OAUTH_STATE_TTL_S,
    }


def _oauth_return(base: str, conn_id: str | None, status: str,
                  error: str | None = None) -> RedirectResponse:
    from urllib.parse import urlencode
    q = {"connector": conn_id or "", "connector_status": status}
    if error:
        q["connector_error"] = error[:200]
    return RedirectResponse(f"{base.rstrip('/')}/?{urlencode(q)}#/options", status_code=302)


@open_rt.get("/connections/oauth/callback")
async def connection_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Provider -> here -> back into the SPA. Open route by necessity (the
    browser arrives without our admin header); the row is found by the
    state minted in `start`, which is cleared on first use and expires."""
    pool = await db.get_pool(_dsn())
    row = None
    if state:
        rec = await pool.fetchrow(
            "SELECT * FROM connections WHERE oauth_state = $1", state
        )
        row = connectors.row_to_dict(rec) if rec else None
    if row is None:
        return _oauth_return(
            _public_base(request), None, "error",
            "sign-in link is invalid or was already used; start again from Options",
        )
    entry = config.connector_type(row["type"]) or {}
    cfg = dict(row.get("config") or {})
    base = cfg.pop("return_url", None) or _public_base(request)
    expires = row.get("oauth_state_expires_at")
    expired = expires is not None and expires < datetime.datetime.now(datetime.timezone.utc)

    async def fail(msg: str):
        await pool.execute(
            "UPDATE connections SET oauth_state=NULL, oauth_state_expires_at=NULL,"
            " status='needs_auth', error=$2, config=$3::jsonb, updated_at=now()"
            " WHERE id=$1",
            row["id"], msg, json.dumps(cfg),
        )
        return _oauth_return(base, row["id"], "error", msg)

    if error:
        return await fail(f"sign-in was cancelled ({error})")
    if not code:
        return await fail("sign-in returned no code")
    if expired:
        return await fail("sign-in took too long; try again")
    keys = config.load_keys()
    try:
        secret = await connectors.oauth_exchange_code(
            entry, keys, code, _oauth_redirect_uri(request)
        )
    except connectors.ProbeError as e:
        return await fail(f"token exchange failed: {e}")
    except httpx.HTTPError as e:
        return await fail(f"token exchange failed: {e}")
    cfg["account_email"] = await connectors.oauth_account_email(entry, secret)
    # Same account signed in again (Connect clicked twice, or a re-auth
    # through Connect instead of Sign in): refresh the EXISTING row's tokens
    # and drop the new one, so one account is one connection. Different
    # accounts of the same type stay separate rows.
    target_id = row["id"]
    if cfg.get("account_email"):
        dup = await pool.fetchrow(
            "SELECT id, config FROM connections WHERE type = $1 AND id <> $2"
            " AND config->>'account_email' = $3 ORDER BY created_at LIMIT 1",
            row["type"], row["id"], cfg["account_email"],
        )
        if dup is not None:
            keep_cfg = dict(connectors.row_to_dict(dup).get("config") or {})
            keep_cfg.pop("return_url", None)
            keep_cfg["account_email"] = cfg["account_email"]
            cfg = keep_cfg
            await pool.execute("DELETE FROM connections WHERE id = $1", row["id"])
            target_id = dup["id"]
    await pool.execute(
        "UPDATE connections SET oauth_state=NULL, oauth_state_expires_at=NULL,"
        " status='connected', error=NULL, secret=$2::jsonb, config=$3::jsonb,"
        " updated_at=now() WHERE id=$1",
        target_id, json.dumps(secret), json.dumps(cfg),
    )
    fresh = await _connection_or_404(pool, target_id)
    # List the tools now so the Options card can show the checklist on
    # return. A failure here (MCP API not enabled in the Cloud project) is
    # recorded on the row as status=error, not a failed sign-in.
    await _probe_row(pool, fresh, entry, keys)
    return _oauth_return(base, target_id, "ok")


# ─── engines (saved connections to deployed engines) ─────────────────────────
# The builder remembers engines it administers (name + URL + admin token) so
# Deployments/Insights can show and manage the VM's real data. Same storage
# posture as provider keys: a gitignored JSON file on the user's machine,
# owner-only permissions, token never sent to the browser (responses carry
# it masked). The browser talks to the LOCAL backend only; the local backend
# relays to the engine with the stored token (so no CORS, no token exposure).

ENGINES_PATH = config.DATA_DIR / "engines.json"


def _load_engines() -> list[dict]:
    try:
        return json.loads(ENGINES_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_engines(engines: list[dict]) -> None:
    config.DATA_DIR.mkdir(exist_ok=True)
    tmp = ENGINES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(engines, indent=2) + "\n")
    os.chmod(tmp, 0o600)  # holds live admin tokens in plaintext
    tmp.replace(ENGINES_PATH)


def _engine_or_404(engine_id: str) -> dict:
    for e in _load_engines():
        if e["id"] == engine_id:
            return e
    raise HTTPException(404, "unknown engine — it may have been removed")


def _engine_out(e: dict) -> dict:
    return {
        "id": e["id"],
        "name": e["name"],
        "base_url": e["base_url"],
        "admin_token_masked": config.mask_secret(e["admin_token"]),
        "created_at": e.get("created_at"),
    }


async def _engine_relay(
    engine: dict, method: str, path: str, json_body: dict | None = None
):
    """Relay one request to an engine with its stored token.

    Error shape is deliberate: connection problems become a 502 that names
    the engine ("unreachable"), engine-side errors pass through with their
    original status + detail, and a 404 on an endpoint every current build
    has means the engine runs old code — say so instead of a bare 404."""
    base = engine["base_url"].rstrip("/")
    hdrs = {"X-Arya-Admin-Token": engine["admin_token"]}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.request(
                method, f"{base}{path}", headers=hdrs, json=json_body
            )
    except httpx.HTTPError as e:
        raise HTTPException(
            502, f"engine '{engine['name']}' is unreachable: {e}"
        )
    if r.status_code == 401:
        raise HTTPException(
            502,
            f"engine '{engine['name']}' rejected the saved admin token —"
            " remove and re-add this engine with the current token",
        )
    return r


def _relay_json(r: httpx.Response, engine: dict, missing_means_old=False):
    if r.status_code == 404 and missing_means_old:
        raise HTTPException(
            502,
            f"engine '{engine['name']}' runs an older build without this"
            " endpoint — update it (git pull, then rebuild) and retry",
        )
    if r.status_code not in (200, 201):
        detail = r.text[:300]
        try:
            detail = r.json().get("detail", detail)
        except Exception:
            pass
        raise HTTPException(r.status_code, detail)
    return r.json()


async def _revoke_on_engines(agent_id: str) -> list[dict]:
    """Unpublish one agent on every saved engine, best effort.

    Shared by Unpublish and Delete agent (both mean "this agent stops
    answering", and a key left live on an engine keeps it answering).
    Defined next to the relay helpers it uses; the callers live earlier in
    the file, which Python resolves at call time.

    Never raises: an unreachable engine must not abort the local action, so
    every engine reports its own outcome and the UI names the failures.
    `cascade=0` keeps the engine from fanning out again from there.
    """
    out = []
    for eng in _load_engines():
        try:
            r = await _engine_relay(
                eng, "DELETE", f"/api/agents/{agent_id}/deployment?cascade=0"
            )
            # 404 means it was never published on that engine: nothing to
            # unpublish, and not a failure worth reporting.
            if r.status_code == 404:
                continue
            _relay_json(r, eng)
            out.append({"name": eng["name"], "ok": True})
        except HTTPException as e:
            out.append({"name": eng["name"], "ok": False, "error": str(e.detail)})
    return out


class EngineReq(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    base_url: str
    admin_token: str = Field(min_length=1)


@api.get("/engines")
async def list_engines():
    return [_engine_out(e) for e in _load_engines()]


@api.post("/engines")
async def add_engine(req: EngineReq):
    """Validated on save: the engine must answer the deployments list with
    this token before anything is stored — a typo'd URL or token fails here
    with a readable reason, not later on some screen."""
    base = req.base_url.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(400, "engine URL must start with https:// (or http://)")
    candidate = {
        "id": uuid.uuid4().hex[:12],
        "name": req.name.strip(),
        "base_url": base,
        "admin_token": req.admin_token.strip(),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    r = await _engine_relay(candidate, "GET", "/api/deployments")
    _relay_json(r, candidate, missing_means_old=True)
    engines = _load_engines()
    if any(e["base_url"] == base for e in engines):
        raise HTTPException(409, "an engine with this URL is already saved")
    engines.append(candidate)
    _save_engines(engines)
    return _engine_out(candidate)


@api.delete("/engines/{engine_id}")
async def remove_engine(engine_id: str):
    engines = _load_engines()
    kept = [e for e in engines if e["id"] != engine_id]
    if len(kept) == len(engines):
        raise HTTPException(404, "unknown engine")
    _save_engines(kept)
    return {"removed": True}


@api.get("/engines/{engine_id}/deployments")
async def engine_deployments(engine_id: str):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(eng, "GET", "/api/deployments")
    return _relay_json(r, eng, missing_means_old=True)


@api.get("/engines/{engine_id}/insights/summary")
async def engine_insights(
    engine_id: str,
    agent_id: str | None = None,
    days: int = 7,
    start: str | None = None,
    end: str | None = None,
    tz_offset_min: int = 0,
):
    eng = _engine_or_404(engine_id)
    qs = f"?days={days}&tz_offset_min={tz_offset_min}"
    if agent_id:
        qs += f"&agent_id={agent_id}"
    if start:
        qs += f"&start={start}"
    if end:
        qs += f"&end={end}"
    # An engine on an older build ignores start/end and answers with its
    # 7-day default — degraded, not broken.
    r = await _engine_relay(eng, "GET", f"/api/insights/summary{qs}")
    return _relay_json(r, eng, missing_means_old=True)


@api.get("/engines/{engine_id}/insights/series")
async def engine_insights_series(engine_id: str, request: Request):
    # The query string is relayed untouched: the series, breakdown and
    # summary endpoints share one parameter set (agent_id, days or
    # start+end, tz_offset_min, bucket).
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(
        eng, "GET", f"/api/insights/series?{request.url.query}"
    )
    return _relay_json(r, eng, missing_means_old=True)


@api.get("/engines/{engine_id}/insights/breakdown")
async def engine_insights_breakdown(engine_id: str, request: Request):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(
        eng, "GET", f"/api/insights/breakdown?{request.url.query}"
    )
    return _relay_json(r, eng, missing_means_old=True)


@api.get("/engines/{engine_id}/conversations/{conversation_id}/timeline")
async def engine_conv_timeline(engine_id: str, conversation_id: uuid.UUID):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(
        eng, "GET", f"/api/conversations/{conversation_id}/timeline"
    )
    return _relay_json(r, eng, missing_means_old=True)


@api.get("/engines/{engine_id}/conversations")
async def engine_conversations(engine_id: str, limit: int = 50):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(eng, "GET", f"/api/conversations?limit={limit}")
    return _relay_json(r, eng)


@api.get("/engines/{engine_id}/conversations/{conversation_id}/messages")
async def engine_conv_messages(engine_id: str, conversation_id: uuid.UUID):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(
        eng, "GET", f"/api/conversations/{conversation_id}/messages"
    )
    return _relay_json(r, eng)


@api.delete("/engines/{engine_id}/conversations/{conversation_id}")
async def engine_delete_conversation(engine_id: str, conversation_id: uuid.UUID):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(
        eng, "DELETE", f"/api/conversations/{conversation_id}"
    )
    return _relay_json(r, eng)


@api.get("/engines/{engine_id}/agents/{agent_id}/deployment")
async def engine_get_deployment(engine_id: str, agent_id: str):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(eng, "GET", f"/api/agents/{agent_id}/deployment")
    return _relay_json(r, eng)  # engine's own 404 = "not published there"


@api.get("/engines/{engine_id}/agents/{agent_id}/deployment/usage")
async def engine_get_usage(engine_id: str, agent_id: str):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(
        eng, "GET", f"/api/agents/{agent_id}/deployment/usage"
    )
    return _relay_json(r, eng)


@api.patch("/engines/{engine_id}/agents/{agent_id}/deployment")
async def engine_patch_deployment(
    engine_id: str, agent_id: str, req: DeploymentPatch
):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(
        eng, "PATCH", f"/api/agents/{agent_id}/deployment",
        json_body=req.model_dump(exclude_none=True),
    )
    return _relay_json(r, eng)


@api.post("/engines/{engine_id}/agents/{agent_id}/deployment/rotate")
async def engine_rotate_key(engine_id: str, agent_id: str):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(
        eng, "POST", f"/api/agents/{agent_id}/deployment/rotate"
    )
    return _relay_json(r, eng)


@api.delete("/engines/{engine_id}/agents/{agent_id}")
async def engine_delete_agent(engine_id: str, agent_id: str):
    """Remove an agent from an engine entirely: its files, documents and the
    transcripts recorded there.

    Unpublishing only kills the key, deliberately — republishing should not
    require re-uploading documents. That leaves the files behind, and after
    the local copy is deleted nothing can reach them: this is the cleanup
    path (also the only one for a push whose import landed but whose publish
    failed). Deliberately a separate, explicit action on the engine's own
    screen: cross-machine deletion is the one thing here that cannot be
    undone, so it never rides along with a local delete."""
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(eng, "DELETE", f"/api/agents/{agent_id}?cascade=0")
    return _relay_json(r, eng)


@api.delete("/engines/{engine_id}/agents/{agent_id}/deployment")
async def engine_revoke_deployment(engine_id: str, agent_id: str):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(
        eng, "DELETE", f"/api/agents/{agent_id}/deployment?cascade=0"
    )
    return _relay_json(r, eng)


# ─── engines: channels and connections on a pushed agent ─────────────────────
# A push carries the agent and its documents, never its WhatsApp number or
# its database connection: those hold credentials and belong to the machine
# that answers. An engine has no screens, so the builder sets them there
# through the same relay Publish uses, and the engine does the real work
# (probing Meta, subscribing the account, checking the database's shape),
# so every answer reads exactly as it does locally. Each relay is a thin
# forward; `missing_means_old` because an engine built before this exists
# answers 404 on every one of them.


async def _engine_pass(engine_id: str, method: str, path: str,
                       body: dict | None = None):
    eng = _engine_or_404(engine_id)
    r = await _engine_relay(eng, method, path, json_body=body)
    return _relay_json(r, eng, missing_means_old=True)


@api.get("/engines/{engine_id}/agents/{agent_id}/channels")
async def engine_list_channels(engine_id: str, agent_id: str):
    return await _engine_pass(engine_id, "GET", f"/api/agents/{agent_id}/channels")


@api.post("/engines/{engine_id}/agents/{agent_id}/channels", status_code=201)
async def engine_add_channel(engine_id: str, agent_id: str, req: ChannelReq):
    return await _engine_pass(
        engine_id, "POST", f"/api/agents/{agent_id}/channels",
        req.model_dump(exclude_none=True),
    )


@api.patch("/engines/{engine_id}/channels/{channel_id}")
async def engine_patch_channel(engine_id: str, channel_id: str, req: ChannelPatch):
    return await _engine_pass(
        engine_id, "PATCH", f"/api/channels/{channel_id}",
        req.model_dump(exclude_none=True),
    )


@api.delete("/engines/{engine_id}/channels/{channel_id}")
async def engine_delete_channel(engine_id: str, channel_id: str):
    return await _engine_pass(engine_id, "DELETE", f"/api/channels/{channel_id}")


@api.post("/engines/{engine_id}/channels/{channel_id}/resubscribe")
async def engine_resubscribe_channel(engine_id: str, channel_id: str):
    return await _engine_pass(
        engine_id, "POST", f"/api/channels/{channel_id}/resubscribe"
    )


@api.post("/engines/{engine_id}/channels/{channel_id}/test")
async def engine_test_channel(engine_id: str, channel_id: str, req: ChannelTestReq):
    return await _engine_pass(
        engine_id, "POST", f"/api/channels/{channel_id}/test", req.model_dump()
    )


@api.get("/engines/{engine_id}/connections")
async def engine_list_connections(engine_id: str, type: str | None = None):
    qs = f"?type={type}" if type else ""
    return await _engine_pass(engine_id, "GET", f"/api/connections{qs}")


@api.post("/engines/{engine_id}/connections", status_code=201)
async def engine_add_connection(engine_id: str, req: ConnectionReq):
    return await _engine_pass(
        engine_id, "POST", "/api/connections", req.model_dump(exclude_none=True)
    )


@api.delete("/engines/{engine_id}/connections/{conn_id}")
async def engine_delete_connection(engine_id: str, conn_id: str, force: bool = False):
    qs = "?force=1" if force else ""
    return await _engine_pass(engine_id, "DELETE", f"/api/connections/{conn_id}{qs}")


@api.get("/engines/{engine_id}/agents/{agent_id}/connectors")
async def engine_get_connectors(engine_id: str, agent_id: str):
    return await _engine_pass(engine_id, "GET", f"/api/agents/{agent_id}/connectors")


@api.put("/engines/{engine_id}/agents/{agent_id}/connectors")
async def engine_set_connectors(engine_id: str, agent_id: str, req: ConnectorsReq):
    return await _engine_pass(
        engine_id, "PUT", f"/api/agents/{agent_id}/connectors", req.model_dump()
    )


# ─── conversations (token server) ────────────────────────────────────────────


def _lk_client() -> lk_api.LiveKitAPI:
    # Server API speaks http(s). Inside docker the API reaches livekit by
    # service name; the browser-facing ws URL is a different host.
    url = os.environ.get("LIVEKIT_INTERNAL_URL") or os.environ.get(
        "LIVEKIT_URL", "ws://localhost:7880"
    )
    url = url.replace("ws://", "http://").replace("wss://", "https://")
    # No fallback on either credential — a missing key must fail loudly
    # (KeyError), never silently mint tokens with a publicly known value.
    # Local values come from .env / backend/.env.local (scripts/init.sh).
    return lk_api.LiveKitAPI(
        url=url,
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )


# The dispatch name the worker registers under. Explicit dispatch (rather
# than auto-dispatch on room creation) is what lets a room say WHICH agent
# should answer — auto-dispatch carries no metadata at all. Set to "" to
# restore the old auto-dispatch behavior (must match the worker's env).
AGENT_NAME = os.environ.get("ARYA_AGENT_NAME", "arya")


def _job_metadata(*, agent_id, conversation_id, user_id,
                  max_session_seconds=None, deployment_id=None,
                  channel=None, external_user=None) -> str:
    """Job metadata: IDENTIFIERS ONLY, never the agent config itself —
    configs are 10+ KB (website_context grows unbounded) and the LiveKit
    metadata cap is 512 KiB. The worker loads the config by id.
    deployment_id tells the worker the call arrived through an embedded
    widget (None = builder test call), used to pick the itinerary theme.
    channel names the messaging surface it arrived on ("whatsapp"), which
    switches the worker to a text-only session; None is a live call."""
    return json.dumps({
        "agent_id": agent_id,
        "conversation_id": str(conversation_id),
        "user_id": user_id,
        "max_session_seconds": max_session_seconds,
        "deployment_id": str(deployment_id) if deployment_id else None,
        "channel": channel,
        # The customer's own handle on that channel (a phone number for
        # WhatsApp). The worker needs it to find this person's previous
        # thread and replay its tail.
        "external_user": external_user,
    })


async def _start_session(
    *, agent_cfg: dict, user_id: str | None, deployment=None,
    channel: str | None = None, external_user: str | None = None,
) -> dict:
    """Room + explicit dispatch + token + conversation row — the single
    path every session start goes through, so dispatch, grants, and the
    DB row can never drift apart. Fresh random room per call: dispatch
    (explicit or auto) fires on room creation, so the reused-room-agent-
    never-joins failure class stays impossible by construction."""
    conversation_id = uuid.uuid4()
    room_name = f"conv-{conversation_id.hex[:12]}"
    cap_s = deployment["max_session_seconds"] if deployment else None
    meta = _job_metadata(
        agent_id=agent_cfg.get("id"),
        conversation_id=conversation_id,
        user_id=user_id,
        max_session_seconds=cap_s,
        deployment_id=deployment["id"] if deployment else None,
        channel=channel,
        external_user=external_user,
    )
    row_meta = json.dumps({
        "user_id": user_id,
        "agent_id": agent_cfg.get("id"),
        "agent_name": agent_cfg.get("name"),
        # History and Insights read these to badge and filter a thread
        # without touching the messages table.
        **({"channel": channel} if channel else {}),
        **({"external_user": external_user} if external_user else {}),
    })
    pool = await db.get_pool(_dsn())
    if deployment is None:
        await pool.execute(
            "INSERT INTO conversations (id, room_name, metadata)"
            " VALUES ($1, $2, $3)",
            conversation_id, room_name, row_meta,
        )
    else:
        # Quota check + insert must be ATOMIC per deployment (two racing
        # visitors would both pass a cap of 1). The open-now count is
        # time-windowed: ended_at can stay NULL after a crash, so a bare
        # IS NULL check would leak a slot forever.
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                str(deployment["id"]),
            )
            c = await conn.fetchrow(
                "SELECT count(*) FILTER (WHERE ended_at IS NULL AND"
                " started_at > now() - interval '15 minutes') AS open_now,"
                " count(*) FILTER (WHERE started_at >="
                " date_trunc('day', now())) AS today"
                " FROM conversations WHERE deployment_id = $1",
                deployment["id"],
            )
            if c["open_now"] >= deployment["max_concurrent_sessions"]:
                raise HTTPException(429, "concurrent session limit reached")
            if c["today"] >= deployment["max_sessions_per_day"]:
                raise HTTPException(429, "daily session limit reached")
            await conn.execute(
                "INSERT INTO conversations (id, room_name, deployment_id,"
                " metadata) VALUES ($1, $2, $3, $4)",
                conversation_id, room_name, deployment["id"], row_meta,
            )
            await conn.execute(
                "UPDATE deployments SET sessions_started = sessions_started"
                " + 1, last_session_at = now() WHERE id = $1",
                deployment["id"],
            )
    room_kwargs = {
        "name": room_name,
        "empty_timeout": int(os.environ.get("LIVEKIT_EMPTY_TIMEOUT_S", "120")),
    }
    if AGENT_NAME:
        room_kwargs["agents"] = [
            lk_api.RoomAgentDispatch(agent_name=AGENT_NAME, metadata=meta)
        ]
    lkapi = _lk_client()
    try:
        await lkapi.room.create_room(lk_api.CreateRoomRequest(**room_kwargs))
    except Exception:
        # The row exists but the room never will — close it so the slot
        # frees immediately instead of waiting out the 15-minute window.
        await pool.execute(
            "UPDATE conversations SET ended_at = now() WHERE id = $1",
            conversation_id,
        )
        raise HTTPException(503, "voice server unavailable")
    finally:
        await lkapi.aclose()

    identity = f"user-{(user_id or conversation_id.hex)[:12]}"
    grants = lk_api.VideoGrants(room_join=True, room=room_name)
    at = (
        lk_api.AccessToken(
            os.environ["LIVEKIT_API_KEY"],
            os.environ["LIVEKIT_API_SECRET"],
        )
        .with_identity(identity)
    )
    if deployment is not None:
        # A token anyone can lift off a webpage: mic-only publishing, no
        # camera/screen, and a TTL sized to the session cap (+ slack so a
        # reconnect after an ICE restart still validates).
        grants.can_publish = True
        grants.can_subscribe = True
        grants.can_publish_data = True
        grants.can_publish_sources = ["microphone"]
        # A widget token is lifted off a webpage, so it lives exactly as
        # long as the session cap allows. A channel token is held by our
        # own bridge instead, and a WhatsApp thread can legitimately run
        # past the cap while someone keeps replying, so it gets an hour of
        # headroom rather than being cut mid-answer on a reconnect.
        ttl_s = int(cap_s or 600) + (3600 if channel else 300)
        at = at.with_ttl(datetime.timedelta(seconds=ttl_s))
    token = at.with_grants(grants).to_jwt()
    return {
        "conversation_id": str(conversation_id),
        "room": room_name,
        "token": token,
        "ws_url": os.environ.get("LIVEKIT_URL", "ws://localhost:7880"),
        # The joining participant's own identity. A browser can read this
        # off the room once connected, but a channel bridge needs it before
        # the first event arrives so it can tell the agent's transcript
        # from the echo of what it just sent.
        "identity": identity,
    }


class ConversationReq(BaseModel):
    user_id: str | None = None
    # Optional: start a call with a SPECIFIC saved agent instead of the
    # global active one — Test can target an agent without activating it.
    agent_id: str | None = None


@api.post("/conversations")
async def start_conversation(req: ConversationReq | None = None):
    """Room + token minted server-side. The user_id (a stable browser
    UUID) rides in the conversation row's metadata; the worker uses it
    for memory."""
    user_id = req.user_id if req else None
    agent_cfg = None
    if req and req.agent_id:
        agent_cfg = config.load_saved_agent(req.agent_id)
        if agent_cfg is None:
            raise HTTPException(404, f"unknown agent '{req.agent_id}'")
    if agent_cfg is None:
        agent_cfg = config.load_active()
    return await _start_session(agent_cfg=agent_cfg, user_id=user_id)


@api.post("/conversations/{conversation_id}/end")
async def end_conversation(conversation_id: uuid.UUID):
    """Delete the room -> participants disconnect -> the agent's shutdown
    callback stamps ended_at."""
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT room_name FROM conversations WHERE id = $1", conversation_id
    )
    if row is None:
        raise HTTPException(404, "unknown conversation_id")
    # Endings classification (Insights): this path is the deliberate
    # hang-up. Stamped BEFORE the room delete so the worker's shutdown
    # merge (which only writes ended_reason when a watchdog fired) can
    # never race it away.
    await pool.execute(
        "UPDATE conversations SET metadata ="
        " COALESCE(metadata, '{}'::jsonb) || '{\"ended_reason\":"
        " \"user_ended\"}'::jsonb WHERE id = $1",
        conversation_id,
    )
    lkapi = _lk_client()
    try:
        await lkapi.room.delete_room(
            lk_api.DeleteRoomRequest(room=row["room_name"])
        )
    except Exception as e:
        logger.warning(f"delete_room ({row['room_name']}): {e}")
    finally:
        await lkapi.aclose()
    return {"conversation_id": str(conversation_id), "ended": True}


@api.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: uuid.UUID):
    """Permanently delete one conversation and everything it left behind
    (History's per-conversation Delete chat button). Shares the purge
    helper with Forget visitor, so this can never again orphan the call's
    tool_events and rag_events the way the old three-table delete did."""
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT id, room_name FROM conversations WHERE id = $1", conversation_id
    )
    if row is None:
        raise HTTPException(404, "unknown conversation_id")
    await visitors.purge_conversation_rows(pool, conversation_id, row["room_name"])
    return {"conversation_id": str(conversation_id), "deleted": True}


async def _conversation_rows(pool, limit: int, user_id: str | None = None) -> list[dict]:
    # ended_at can be NULL after a crash or an abandoned room — the last
    # message (when any) stamps the duration; the UI shows 0s otherwise.
    # artifact_id joins in the call's written itinerary (when one was
    # generated) so History can link straight to it.
    rows = await pool.fetch(
        "SELECT c.id, c.room_name, c.started_at, c.ended_at,"
        " c.metadata->>'agent_name' AS agent_name,"
        # Lets the UI link a transcript back to the agent that ran it.
        " c.metadata->>'agent_id' AS agent_id,"
        # The visitor tag History shows, and the key the Visitors screen
        # groups by.
        " c.metadata->>'user_id' AS user_id,"
        " c.metadata->>'channel' AS channel,"
        " c.metadata->>'ended_reason' AS ended_reason,"
        " c.metadata->'evals' AS evals,"
        " COUNT(m.id) AS message_count, MAX(m.created_at) AS last_message_at,"
        " MAX(a.id::text) AS artifact_id, MAX(a.title) AS artifact_title"
        " FROM conversations c"
        " LEFT JOIN messages m ON m.conversation_id = c.id"
        " LEFT JOIN artifacts a ON a.conversation_id = c.id AND a.kind = 'itinerary'"
        " WHERE ($2::text IS NULL OR c.metadata->>'user_id' = $2)"
        " GROUP BY c.id"
        " ORDER BY c.started_at DESC LIMIT $1",
        max(1, min(limit, 200)),
        user_id or None,
    )
    return [
        dict(r)
        | {
            "id": str(r["id"]),
            "evals": json.loads(r["evals"]) if r["evals"] else None,
        }
        for r in rows
    ]


@api.get("/conversations")
async def list_conversations(limit: int = 50, user_id: str | None = None):
    pool = await db.get_pool(_dsn())
    return await _conversation_rows(pool, limit, user_id)


@api.get("/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: uuid.UUID):
    pool = await db.get_pool(_dsn())
    rows = await pool.fetch(
        "SELECT role, content, citations, options, links, metrics, created_at FROM messages"
        " WHERE conversation_id = $1 ORDER BY id",
        conversation_id,
    )
    return [
        dict(r)
        | {
            "citations": json.loads(r["citations"]) if r["citations"] else None,
            "options": json.loads(r["options"]) if r["options"] else None,
            "links": json.loads(r["links"]) if r["links"] else None,
            "metrics": json.loads(r["metrics"]) if r["metrics"] else None,
        }
        for r in rows
    ]


# ─── visitors (the people a published agent remembers) ───────────────────────
# A visitor is one browser (signed token, see visitors.py) or one channel
# customer (phone number via a bridge). The list is grouped from
# conversations.metadata; detail adds Mem0's memories; DELETE forgets the
# person everywhere in one call. On the admin router so an engine, which
# is the same image, answers the same routes through the relays below.


def _visitor_or_400(user_id: str) -> str:
    if not visitors.valid_user_id(user_id):
        raise HTTPException(400, "malformed visitor id")
    return user_id


@api.get("/visitors")
async def list_visitors(agent_id: str | None = None, limit: int = 100):
    pool = await db.get_pool(_dsn())
    rows = await pool.fetch(
        visitors.LIST_SQL, agent_id or None, max(1, min(limit, 500))
    )
    return [dict(r) for r in rows]


@api.get("/visitors/{user_id}")
async def get_visitor(user_id: str):
    """Conversations (History's row shape, filtered) plus Mem0 memories.
    memories is null, with a note, when no Mem0 key is saved here or Mem0
    cannot be reached: a visitor with no memories and one we could not ask
    about must read differently."""
    _visitor_or_400(user_id)
    pool = await db.get_pool(_dsn())
    convs = await _conversation_rows(pool, 200, user_id)
    memories = None
    note = None
    keys = config.load_keys()
    if not keys.get("mem0"):
        note = "No Mem0 key is saved on this machine, so memories cannot be listed."
    else:
        try:
            memories = await memory.list_for_user(keys["mem0"], user_id)
            # The screen groups by agent, so name each one. A memory with
            # no agent was stored under "all my agents" scope; an agent
            # that was deleted keeps its id as the label.
            names: dict = {}
            for m in memories:
                aid = m.get("agent_id")
                if aid and aid not in names:
                    try:
                        names[aid] = (config.load_saved_agent(aid) or {}).get("name")
                    except Exception:
                        names[aid] = None
                m["agent_name"] = names.get(aid) if aid else None
        except Exception as e:
            logger.warning(f"mem0 list failed for visitor {user_id}: {e}")
            note = f"Mem0 could not be reached ({type(e).__name__}), so memories are not shown."
    return {
        "user_id": user_id,
        "conversations": convs,
        "memories": memories,
        "note": note,
    }


class MemoryEditReq(BaseModel):
    memory: str = Field(min_length=1, max_length=2000)


def _memory_id_or_400(memory_id: str) -> str:
    # Mem0 ids are UUID-like; anything else is not one we could have listed.
    if not (1 <= len(memory_id) <= 64) or not all(
        c.isalnum() or c in "-_" for c in memory_id
    ):
        raise HTTPException(400, "malformed memory id")
    return memory_id


async def _owned_memory_key(user_id: str, memory_id: str) -> str:
    """The Mem0 key, after proving this memory belongs to this visitor.
    A memory id on its own is not scoped to anyone, so editing or deleting
    by id alone would let one visitor's page act on another's. Checked
    against the visitor's own listing, the same source the screen shows."""
    _visitor_or_400(user_id)
    _memory_id_or_400(memory_id)
    key = config.load_keys().get("mem0")
    if not key:
        raise HTTPException(409, "No Mem0 key is saved on this machine.")
    try:
        mine = await memory.list_for_user(key, user_id)
    except Exception as e:
        logger.warning(f"mem0 list failed for visitor {user_id}: {e}")
        raise HTTPException(502, f"Mem0 could not be reached ({type(e).__name__}).")
    if not any(m.get("id") == memory_id for m in mine):
        raise HTTPException(404, "that memory does not belong to this visitor")
    return key


@api.patch("/visitors/{user_id}/memories/{memory_id}")
async def edit_visitor_memory(user_id: str, memory_id: str, req: MemoryEditReq):
    key = await _owned_memory_key(user_id, memory_id)
    try:
        await memory.update_memory(key, memory_id, req.memory.strip())
    except Exception as e:
        logger.warning(f"mem0 update failed for {memory_id}: {e}")
        raise HTTPException(502, f"Mem0 could not save the change ({type(e).__name__}).")
    return {"id": memory_id, "updated": True}


@api.delete("/visitors/{user_id}/memories/{memory_id}")
async def delete_visitor_memory(user_id: str, memory_id: str):
    key = await _owned_memory_key(user_id, memory_id)
    try:
        await memory.delete_memory(key, memory_id)
    except Exception as e:
        logger.warning(f"mem0 delete failed for {memory_id}: {e}")
        raise HTTPException(502, f"Mem0 could not delete it ({type(e).__name__}).")
    return {"id": memory_id, "deleted": True}


@api.delete("/visitors/{user_id}")
async def forget_visitor(user_id: str):
    """Forget one person: Mem0 memories across every agent first (the
    point of forgetting a person, so no agent scope filter), then every
    Postgres row, atomically. A Mem0 outage is reported, not fatal: the
    transcripts still go, and memories_deleted=false tells the operator
    to retry. Idempotent, a second call returns zeros."""
    _visitor_or_400(user_id)
    memories_deleted = False
    keys = config.load_keys()
    if keys.get("mem0"):
        try:
            await memory.purge_user(keys["mem0"], user_id)
            memories_deleted = True
        except Exception as e:
            logger.warning(f"mem0 purge skipped for visitor {user_id}: {e}")
    pool = await db.get_pool(_dsn())
    counts = await visitors.forget_visitor_rows(pool, user_id)
    return {"user_id": user_id, "memories_deleted": memories_deleted, **counts}


@api.get("/engines/{engine_id}/visitors")
async def engine_visitors(engine_id: str, agent_id: str | None = None, limit: int = 100):
    qs = urlencode({"limit": limit, **({"agent_id": agent_id} if agent_id else {})})
    return await _engine_pass(engine_id, "GET", f"/api/visitors?{qs}")


@api.get("/engines/{engine_id}/visitors/{user_id}")
async def engine_visitor(engine_id: str, user_id: str):
    _visitor_or_400(user_id)
    return await _engine_pass(engine_id, "GET", f"/api/visitors/{user_id}")


@api.delete("/engines/{engine_id}/visitors/{user_id}")
async def engine_forget_visitor(engine_id: str, user_id: str):
    _visitor_or_400(user_id)
    return await _engine_pass(engine_id, "DELETE", f"/api/visitors/{user_id}")


@api.patch("/engines/{engine_id}/visitors/{user_id}/memories/{memory_id}")
async def engine_edit_visitor_memory(
    engine_id: str, user_id: str, memory_id: str, req: MemoryEditReq
):
    _visitor_or_400(user_id)
    _memory_id_or_400(memory_id)
    return await _engine_pass(
        engine_id, "PATCH", f"/api/visitors/{user_id}/memories/{memory_id}",
        req.model_dump(),
    )


@api.delete("/engines/{engine_id}/visitors/{user_id}/memories/{memory_id}")
async def engine_delete_visitor_memory(engine_id: str, user_id: str, memory_id: str):
    _visitor_or_400(user_id)
    _memory_id_or_400(memory_id)
    return await _engine_pass(
        engine_id, "DELETE", f"/api/visitors/{user_id}/memories/{memory_id}"
    )


# ─── insights (observability + evals over data the stack already stores) ─────
# Read-only aggregation, nothing new is collected here. Pre-aggregated
# server-side so the frontend renders numbers instead of crunching them:
# conversations + messages (turn metrics, citations) + rag_events (worker-
# written retrieval log). Costing is deliberately absent (product decision).


@api.get("/insights/summary")
async def insights_summary(
    agent_id: str | None = None,
    days: int = 7,
    start: str | None = None,
    end: str | None = None,
    tz_offset_min: int = 0,
):
    """Everything the Insights screen needs in one round-trip.

    Window: either a date range (start/end, YYYY-MM-DD, both inclusive —
    same day twice = that one day) or the days fallback. Timestamps are
    stored in UTC; the range is interpreted in the CALLER's timezone via
    tz_offset_min (JS getTimezoneOffset(): UTC minus local, so PKT sends
    -300), so "Aug 24" means Aug 24 on the user's clock, not the
    container's.

    Cards are computed in Python over the per-conversation rows (capped at
    500 — at this cap SQL-side aggregation buys nothing and the honest
    LIMIT keeps the endpoint O(bounded) on a runaway table); the two
    percentile scans stay in SQL where they belong.
    """
    off = datetime.timedelta(minutes=max(-840, min(tz_offset_min, 840)))
    if start and end:
        try:
            start_day = datetime.date.fromisoformat(start)
            end_day = datetime.date.fromisoformat(end)
        except ValueError:
            raise HTTPException(400, "start/end must be YYYY-MM-DD dates")
        if end_day < start_day:
            raise HTTPException(400, "end date is before start date")
        if (end_day - start_day).days > 90:
            raise HTTPException(400, "date range is capped at 90 days")
        # local midnight -> UTC instant: utc = local + offset
        start_utc = (
            datetime.datetime.combine(start_day, datetime.time.min)
            .replace(tzinfo=datetime.timezone.utc) + off
        )
        end_utc = (
            datetime.datetime.combine(
                end_day + datetime.timedelta(days=1), datetime.time.min
            ).replace(tzinfo=datetime.timezone.utc) + off
        )
        days = (end_day - start_day).days + 1
    else:
        days = max(1, min(days, 90))
        end_utc = datetime.datetime.now(datetime.timezone.utc)
        start_utc = end_utc - datetime.timedelta(days=days)
        # series still labels by local day below
        end_day = (end_utc - off).date()
        start_day = end_day - datetime.timedelta(days=days - 1)
    pool = await db.get_pool(_dsn())
    rows = await pool.fetch(
        "SELECT c.id, c.room_name, c.started_at, c.ended_at,"
        " c.metadata->>'agent_name' AS agent_name,"
        # Lets the UI link a row back to the agent that ran it.
        " c.metadata->>'agent_id' AS agent_id,"
        " c.metadata->>'ended_reason' AS ended_reason,"
        " c.metadata->'evals' AS evals,"
        " count(m.id) FILTER (WHERE m.role IN ('user','assistant')) AS turns,"
        " count(m.id) FILTER (WHERE m.role = 'assistant'"
        "   AND m.citations IS NOT NULL) AS cited_turns,"
        # doc-only citations (web sources carry type='web'; RAG cits carry
        # no type) — feeds the KB grounding card, not the row display
        " count(m.id) FILTER (WHERE m.role = 'assistant'"
        "   AND m.citations IS NOT NULL AND jsonb_path_exists(m.citations,"
        "   '$[*] ? (!(exists(@.type)) || @.type != \"web\")'))"
        "   AS doc_cited_turns,"
        # Response time per answer: end to end when the SDK recorded it,
        # otherwise the sum of the measured stages (streaming STT often
        # leaves e2e unset while ttft and ttfb are present).
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY COALESCE("
        "     (m.metrics->>'e2e_latency')::float,"
        "     (m.metrics->>'llm_node_ttft')::float"
        "       + (m.metrics->>'tts_node_ttfb')::float))"
        "   FILTER (WHERE m.role = 'assistant' AND COALESCE("
        "     (m.metrics->>'e2e_latency')::float,"
        "     (m.metrics->>'llm_node_ttft')::float"
        "       + (m.metrics->>'tts_node_ttfb')::float) IS NOT NULL)"
        "   AS med_latency_s,"
        # A spoken call has at least one answer with voice timings; a typed
        # chat has none, and the UI must say so instead of showing a dash.
        " bool_or(m.metrics ? 'tts_node_ttfb' OR m.metrics ? 'e2e_latency')"
        "   AS voice,"
        " bool_or(m.metrics ? 'interrupted') AS interrupt_tracked,"
        " count(m.id) FILTER (WHERE m.role = 'assistant'"
        "   AND m.metrics->>'interrupted' = 'true') AS interrupted_turns,"
        " max(m.created_at) AS last_message_at,"
        # Same itinerary join as list_conversations: the Dashboard opens the
        # very same transcript view, and without these two columns its
        # "View itinerary" chip silently never rendered from this path.
        # At most one itinerary per conversation (unique on kind), so the
        # join never multiplies the message counts above.
        " MAX(a.id::text) AS artifact_id, MAX(a.title) AS artifact_title"
        " FROM conversations c"
        " LEFT JOIN messages m ON m.conversation_id = c.id"
        " LEFT JOIN artifacts a ON a.conversation_id = c.id AND a.kind = 'itinerary'"
        " WHERE c.started_at >= $1 AND c.started_at < $2"
        "   AND ($3::text IS NULL OR c.metadata->>'agent_id' = $3)"
        " GROUP BY c.id ORDER BY c.started_at DESC LIMIT 500",
        start_utc,
        end_utc,
        agent_id or None,
    )
    # Response speed over every answer with timings. e2e when recorded,
    # else the stage sum, and the count of approximated answers so the card
    # can say so. The same window and agent filter as the rows above.
    _resp = (
        "COALESCE((m.metrics->>'e2e_latency')::float,"
        " (m.metrics->>'llm_node_ttft')::float"
        " + (m.metrics->>'tts_node_ttfb')::float)"
    )
    _scope = (
        " FROM messages m JOIN conversations c ON c.id = m.conversation_id"
        " WHERE c.started_at >= $1 AND c.started_at < $2"
        "   AND ($3::text IS NULL OR c.metadata->>'agent_id' = $3)"
    )
    lat = await pool.fetchrow(
        f"SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY {_resp}) AS p50,"
        f" percentile_cont(0.95) WITHIN GROUP (ORDER BY {_resp}) AS p95,"
        " count(*) AS n,"
        " count(*) FILTER (WHERE m.metrics->>'e2e_latency' IS NULL)"
        "   AS approximated"
        f"{_scope} AND m.role = 'assistant' AND {_resp} IS NOT NULL",
        start_utc,
        end_utc,
        agent_id or None,
    )
    # Where the time goes: p50 and p95 per pipeline stage from the per-turn
    # report. Waiting and hearing live on USER rows (they describe the end
    # of the caller's turn), thinking and speaking on assistant rows.
    def _pct(q, key):
        return (
            f" percentile_cont({q}) WITHIN GROUP"
            f" (ORDER BY (m.metrics->>'{key}')::float)"
            f" FILTER (WHERE m.metrics ? '{key}')"
        )
    stages = await pool.fetchrow(
        "SELECT"
        + _pct(0.5, "end_of_turn_delay") + " AS eou_p50,"
        + _pct(0.95, "end_of_turn_delay") + " AS eou_p95,"
        + _pct(0.5, "transcription_delay") + " AS stt_p50,"
        + _pct(0.95, "transcription_delay") + " AS stt_p95,"
        + _pct(0.5, "llm_node_ttft") + " AS llm_p50,"
        + _pct(0.95, "llm_node_ttft") + " AS llm_p95,"
        + _pct(0.5, "tts_node_ttfb") + " AS tts_p50,"
        + _pct(0.95, "tts_node_ttfb") + " AS tts_p95"
        + _scope,
        start_utc,
        end_utc,
        agent_id or None,
    )
    # Cold-start penalty: the first timed answer of each call against the
    # median of that call's later answers, averaged over calls with both.
    # Local stacks pay model-load costs on turn one and a plain average
    # hides it.
    cold = await pool.fetchrow(
        "WITH t AS ("
        f"  SELECT m.conversation_id, {_resp} AS resp,"
        "    row_number() OVER (PARTITION BY m.conversation_id"
        "      ORDER BY m.id) AS rn"
        f"{_scope} AND m.role = 'assistant' AND {_resp} IS NOT NULL"
        "), per_conv AS ("
        "  SELECT conversation_id,"
        "    max(resp) FILTER (WHERE rn = 1) AS first_resp,"
        "    percentile_cont(0.5) WITHIN GROUP (ORDER BY resp)"
        "      FILTER (WHERE rn > 1) AS rest_med"
        "  FROM t GROUP BY conversation_id"
        ")"
        " SELECT avg(first_resp - rest_med) AS penalty, count(*) AS n"
        " FROM per_conv WHERE first_resp IS NOT NULL AND rest_med IS NOT NULL",
        start_utc,
        end_utc,
        agent_id or None,
    )
    # From the durable stage stream: speaking rate (TTS audio per character,
    # a config knob), STT processing time (distinct from the transcript's
    # arrival delay above), and per-tool durations.
    _tm_scope = (
        " FROM turn_metrics t JOIN conversations c ON c.room_name = t.room_name"
        " WHERE c.started_at >= $1 AND c.started_at < $2"
        "   AND ($3::text IS NULL OR c.metadata->>'agent_id' = $3)"
    )
    tm = await pool.fetchrow(
        "SELECT"
        " sum(t.chars) FILTER (WHERE t.metric_type = 'TTSMetrics'"
        "   AND NOT t.cancelled) AS tts_chars,"
        " sum(t.audio_ms) FILTER (WHERE t.metric_type = 'TTSMetrics'"
        "   AND NOT t.cancelled) AS tts_audio_ms,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY t.stage_ms)"
        "   FILTER (WHERE t.metric_type = 'STTMetrics' AND t.stage_ms > 0)"
        "   AS stt_duration_p50_ms"
        + _tm_scope,
        start_utc,
        end_utc,
        agent_id or None,
    )
    tools = await pool.fetch(
        "SELECT t.model AS name, count(*) AS calls,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY t.stage_ms) AS p50_ms,"
        " count(*) FILTER (WHERE t.cancelled) AS failed"
        + _tm_scope
        + " AND t.metric_type = 'ToolCall' AND t.model IS NOT NULL"
        " GROUP BY t.model ORDER BY p50_ms DESC LIMIT 12",
        start_utc,
        end_utc,
        agent_id or None,
    )
    # Refused session starts (quota caps or the rate limiter). These have
    # no conversation row, so they come from session_refusals; the agent
    # filter goes through deployments since refusals are per deployment.
    refusals = await pool.fetch(
        "SELECT r.reason, SUM(r.count) AS n"
        " FROM session_refusals r JOIN deployments d ON d.id = r.deployment_id"
        " WHERE r.window_start >= $1 AND r.window_start < $2"
        "   AND ($3::text IS NULL OR d.agent_id = $3)"
        " GROUP BY r.reason",
        start_utc,
        end_utc,
        agent_id or None,
    )
    rag = await pool.fetchrow(
        "SELECT count(*) AS retrievals,"
        " count(*) FILTER (WHERE injected) AS injected,"
        " count(*) FILTER (WHERE error IS NOT NULL) AS errors,"
        " count(*) FILTER (WHERE error ILIKE '%timeout%'"
        "   OR error ILIKE '%cancelled%') AS timeouts,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)"
        "   FILTER (WHERE latency_ms IS NOT NULL) AS med_latency_ms,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY top_score)"
        "   FILTER (WHERE top_score IS NOT NULL) AS med_top_score"
        " FROM rag_events"
        " WHERE created_at >= $1 AND created_at < $2"
        "   AND ($3::text IS NULL OR agent_id = $3)"
        "   AND skipped IS NULL",
        start_utc,
        end_utc,
        agent_id or None,
    )

    convs = []
    durations, turn_counts = [], []
    endings: dict[str, int] = {}
    doc_cited_total = 0
    # The AI judge's per-answer document checks, over the judged answers
    # that received document context (metadata.evals.turns): its reading
    # of document use beside the overlap count, faithfulness to the
    # excerpts, and whether the excerpts fit the question.
    judged_ctx = judged_used = 0
    faith = {"answers": 0, "pass": 0}
    ctx_rel = {"answers": 0, "pass": 0}
    text_sessions = 0
    interrupt_counts: list[int] = []  # cut-off replies per VOICE call
    for r in rows:
        voice = bool(r["voice"])
        if r["turns"] and not voice:
            text_sessions += 1
        if voice and r.get("interrupt_tracked", True):
            interrupt_counts.append(r["interrupted_turns"] or 0)
        end = r["ended_at"] or r["last_message_at"]
        dur = max(0.0, (end - r["started_at"]).total_seconds()) if end else 0.0
        durations.append(dur)
        turn_counts.append(r["turns"])
        # NULL means nothing was recorded (crash, restart, or a call from
        # before endings tracking existed). "unknown" states exactly that,
        # where the old "disconnected" claimed a cause we never observed.
        reason = r["ended_reason"] or "unknown"
        endings[reason] = endings.get(reason, 0) + 1
        doc_cited_total += r["doc_cited_turns"]
        evals = json.loads(r["evals"]) if r["evals"] else None
        for t in (evals or {}).get("turns") or []:
            f = t.get("faithfulness")
            if isinstance(f, dict):
                faith["answers"] += 1
                faith["pass"] += int(f.get("verdict") == "pass")
                judged_ctx += 1
                judged_used += int(bool(t.get("used_context")))
            c = t.get("context_relevance")
            if isinstance(c, dict):
                ctx_rel["answers"] += 1
                ctx_rel["pass"] += int(c.get("verdict") == "pass")
        convs.append({
            "id": str(r["id"]),
            "room_name": r["room_name"],
            "agent_name": r["agent_name"],
            "agent_id": r["agent_id"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "last_message_at": r["last_message_at"],
            "ended_reason": r["ended_reason"],
            "duration_s": round(dur),
            "turns": r["turns"],
            "cited_turns": r["cited_turns"],
            "med_latency_s": round(r["med_latency_s"], 2)
            if r["med_latency_s"] is not None else None,
            "voice": voice,
            "interrupted_turns": r["interrupted_turns"] or 0,
            "evals": evals,
            "artifact_id": r["artifact_id"],
            "artifact_title": r["artifact_title"],
        })

    # Per-day series for the sparkline, zero-filled oldest → newest.
    # Bucketed by the CALLER's local day (utc - offset), matching the range
    # boundaries above, so a late-night call lands on the day the user saw.
    per_day: dict[str, int] = {}
    for r in rows:
        local_day = (r["started_at"] - off).date().isoformat()
        per_day[local_day] = per_day.get(local_day, 0) + 1
    series = [
        {
            "day": (d := (start_day + datetime.timedelta(days=i)).isoformat()),
            "calls": per_day.get(d, 0),
        }
        for i in range((end_day - start_day).days + 1)
    ]

    # Quality trend: oldest → newest so the line reads left-to-right.
    trend = [
        {"id": c["id"], "started_at": c["started_at"],
         "score": c["evals"]["score"]}
        for c in reversed(convs)
        if c["evals"] and isinstance(c["evals"].get("score"), (int, float))
    ][-20:]

    injected = rag["injected"] if rag else 0
    return {
        "days": days,
        "agent_id": agent_id,
        "calls": len(rows),
        "capped": len(rows) == 500,
        "series": series,
        "avg_duration_s": round(sum(durations) / len(durations)) if durations else 0,
        "avg_turns": round(sum(turn_counts) / len(turn_counts), 1) if turn_counts else 0,
        "latency": {
            "p50": round(lat["p50"], 2) if lat and lat["p50"] is not None else None,
            "p95": round(lat["p95"], 2) if lat and lat["p95"] is not None else None,
            "turns": lat["n"] if lat else 0,
            # answers whose response time is a stage sum, not a recorded e2e
            "approximated": lat["approximated"] if lat else 0,
        },
        # Typed chats in the window: they have no voice timings by nature
        # and are excluded from every speed number above.
        "text_sessions": text_sessions,
        # Per-stage p50/p95 in seconds; a stage is null when no turn in the
        # window recorded it (typed sessions, or streaming STT for hearing).
        "stages": {
            k: (
                {
                    "p50": round(stages[f"{k}_p50"], 2),
                    "p95": round(stages[f"{k}_p95"], 2)
                    if stages[f"{k}_p95"] is not None else None,
                }
                if stages and stages[f"{k}_p50"] is not None else None
            )
            for k in ("eou", "stt", "llm", "tts")
        },
        "cold_start": {
            "penalty_s": round(cold["penalty"], 2),
            "calls": cold["n"],
        } if cold and cold["penalty"] is not None else None,
        "speaking_rate": (
            round(tm["tts_chars"] / (tm["tts_audio_ms"] / 1000.0), 1)
            if tm and tm["tts_audio_ms"] and tm["tts_chars"] else None
        ),
        "stt_duration_p50_ms": (
            round(tm["stt_duration_p50_ms"])
            if tm and tm["stt_duration_p50_ms"] is not None else None
        ),
        "tools": [
            {
                "name": t["name"],
                "calls": t["calls"],
                "p50_ms": round(t["p50_ms"]) if t["p50_ms"] is not None else None,
                "failed": t["failed"],
            }
            for t in tools
        ],
        # Cut-off replies (the caller started talking over the agent), over
        # voice calls only. share = calls with at least one.
        "interruptions": {
            "total": sum(interrupt_counts),
            "voice_calls": len(interrupt_counts),
            "calls_with_any": sum(1 for c in interrupt_counts if c),
            "share": round(
                sum(1 for c in interrupt_counts if c) / len(interrupt_counts), 3
            ),
            "per_call_p50": sorted(interrupt_counts)[len(interrupt_counts) // 2],
        } if interrupt_counts else None,
        # Of the turns where KB context reached the model, how many answers
        # visibly used it. null when RAG never ran in the window.
        "grounding": {
            "used": doc_cited_total,
            "injected": injected,
            "rate": round(doc_cited_total / injected, 3) if injected else None,
            # The judge's reading over the answers it saw with their
            # excerpts, beside the word-overlap count above. None until a
            # judged call in the window had document context.
            "judged": {
                "answers": judged_ctx,
                "used": judged_used,
                "rate": round(judged_used / judged_ctx, 3),
            } if judged_ctx else None,
        } if injected else None,
        # The judge's per-answer document checks (METRICS.md section 6):
        # of the judged answers that received document context, the share
        # that passed each. None when no judged answer had any.
        "doc_checks": {
            "faithfulness": {
                **faith, "rate": round(faith["pass"] / faith["answers"], 3),
            },
            "context_relevance": {
                **ctx_rel,
                "rate": round(ctx_rel["pass"] / ctx_rel["answers"], 3)
                if ctx_rel["answers"] else None,
            },
        } if faith["answers"] else None,
        "rag": {
            "retrievals": rag["retrievals"],
            "injected": rag["injected"],
            "errors": rag["errors"],
            "timeouts": rag["timeouts"],
            "med_latency_ms": round(rag["med_latency_ms"])
            if rag["med_latency_ms"] is not None else None,
            "med_top_score": round(rag["med_top_score"], 3)
            if rag["med_top_score"] is not None else None,
        } if rag and rag["retrievals"] else None,
        "endings": endings,
        # {reason: count} of refused session starts in the window, plus the
        # total. Empty when nobody was turned away.
        "refusals": {
            "total": sum(r["n"] for r in refusals),
            "by_reason": {r["reason"]: r["n"] for r in refusals},
        },
        "eval_trend": trend,
        "conversations": convs,
    }


# Trends, breakdowns and the per-call timeline: the same data as the summary
# above, over time and per call. Separate modules so this file does not grow
# by another few hundred lines of SQL; each registers its route on the admin
# router (the engine proxy relays them below).
insights_series.register(api, db, _dsn)
insights_breakdown.register(api, db, _dsn)
conv_timeline.register(api, db, _dsn)


# ─── artifacts (written itineraries and other per-call documents) ────────────
# Served UN-gated (open_rt): the browser opens these as plain links from the
# call UI and the widget, where no admin header can ride along. Access
# control is the unguessable UUID — the same secret-URL model as public
# keys. Instructions/models never appear in an artifact.


@open_rt.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: uuid.UUID):
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT html FROM artifacts WHERE id = $1", artifact_id
    )
    if not row:
        raise HTTPException(404, "unknown artifact")
    # no-store: the row is upserted in place mid-call. PDF is the only
    # advertised download (the page's own Save-as-PDF button); the raw
    # HTML download option was removed on purpose.
    return Response(
        row["html"],
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@open_rt.get("/artifacts/{artifact_id}/pdf")
async def get_artifact_pdf(artifact_id: uuid.UUID):
    """Server-rendered PDF of the artifact: real pagination (page counters,
    break control, margins) instead of the browser's print dialog. WeasyPrint
    renders the SAME stored HTML and print CSS, so screen, browser-print,
    and this file can never drift apart."""
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT kind, title, html FROM artifacts WHERE id = $1", artifact_id
    )
    if not row:
        raise HTTPException(404, "unknown artifact")
    try:
        from weasyprint import HTML  # heavy import, deferred until first use
    except Exception as e:  # noqa: BLE001 — missing native libs read as ImportError variants
        logger.warning(f"weasyprint unavailable: {e}")
        raise HTTPException(
            503, "PDF generation is not available on this deployment"
        ) from e
    # The page references Google Fonts for browser viewing; the PDF renders
    # offline, so strip that block and let installed fonts take over.
    html = row["html"]
    if "<!--fonts-start-->" in html:
        pre, rest = html.split("<!--fonts-start-->", 1)
        html = pre + rest.split("<!--fonts-end-->", 1)[1]
    # write_pdf is CPU-bound (typically under a second) — off the event loop.
    pdf = await asyncio.to_thread(lambda: HTML(string=html).write_pdf())
    safe = "".join(
        ch for ch in (row["title"] or row["kind"] or "document")
        if ch.isalnum() or ch in " -_"
    ).strip() or "document"
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{safe}.pdf"',
        },
    )


# ─── knowledge base ──────────────────────────────────────────────────────────


class UploadReq(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    agent_id: str | None = None


async def _reject_duplicate(pool, filename: str, agent_id=None, exclude_id=None) -> None:
    """Same doc twice in one agent's collection wastes top_k slots on
    identical chunks, so same filename under the active profile AND the
    same agent is a 409. Different agents may hold the same filename. The
    doc being replaced may of course keep its own filename."""
    prof = _profile()["name"]
    row = await pool.fetchrow(
        "SELECT id FROM knowledge_base WHERE filename = $1"
        " AND status != 'failed'"
        " AND (embedding_profile = $2 OR embedding_profile IS NULL)"
        " AND agent_id IS NOT DISTINCT FROM $4"
        " AND ($3::uuid IS NULL OR id != $3)"
        " LIMIT 1",
        filename,
        prof,
        exclude_id,
        agent_id,
    )
    if row:
        raise HTTPException(
            409,
            f"'{filename}' already exists (doc {row['id']}) — delete it"
            f" first, or POST /api/knowledge-base/{row['id']}/ingest to"
            f" re-process it",
        )


# Upload guardrails. 20 MB mirrors hosted platforms (ElevenLabs: ~21 MB)
# and keeps CPU-bound docling extraction inside a tolerable wait; 5 docs
# per agent keeps retrieval competition for the top-k slots sane.
MAX_UPLOAD_MB = 20
MAX_DOCS_PER_AGENT = 5


def _check_size(size_bytes: int, filename: str) -> None:
    if size_bytes > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            413,
            f"'{filename}' is {size_bytes / 1024 / 1024:.1f} MB — the limit"
            f" is {MAX_UPLOAD_MB} MB per file. Split the document or upload"
            " a smaller version.",
        )


async def _check_doc_limit(pool, agent_id: str | None) -> None:
    n = await pool.fetchval(
        "SELECT count(*) FROM knowledge_base"
        " WHERE agent_id IS NOT DISTINCT FROM $1 AND status != 'failed'",
        agent_id,
    )
    if n >= MAX_DOCS_PER_AGENT:
        scope = "this agent" if agent_id else "the shared collection"
        raise HTTPException(
            400,
            f"{scope} already has {n} documents — the limit is"
            f" {MAX_DOCS_PER_AGENT}. Delete one first.",
        )


def _check_format(filename: str) -> None:
    suffix = pathlib.Path(filename or "").suffix.lower()
    allowed = _allowed_formats()
    if suffix not in allowed:
        raise HTTPException(
            400,
            f"unsupported format '{suffix or '(none)'}'"
            f" — allowed: {', '.join(allowed)}",
        )


@api.post("/knowledge-base/upload")
async def upload(file: UploadFile = File(...), agent_id: str | None = Form(None)):
    """Multipart file in -> stored in S3, ingestion queued immediately.
    The one-call path the UI uses. agent_id scopes the doc to one agent;
    without it the doc is shared (visible to every agent)."""
    _check_format(file.filename or "")
    pool = await db.get_pool(_dsn())
    await _reject_duplicate(pool, file.filename or "", agent_id)
    await _check_doc_limit(pool, agent_id)
    doc_id = uuid.uuid4()
    key = f"{doc_id}/{file.filename}"
    f = file.file
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    _check_size(size, file.filename or "")
    await asyncio.to_thread(
        minio.put_object,
        S3["bucket"],
        key,
        f,
        size,
        content_type=file.content_type or "application/octet-stream",
    )
    await pool.execute(
        "INSERT INTO knowledge_base (id, filename, content_type, object_key,"
        " size_bytes, status, embedding_profile, agent_id)"
        " VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7)",
        doc_id,
        file.filename,
        file.content_type,
        key,
        size,
        _profile()["name"],
        agent_id,
    )
    return {"doc_id": str(doc_id), "status": "pending"}


@api.post("/knowledge-base/upload-url")
async def upload_url(req: UploadReq):
    """Two-step presigned flow (bytes never transit the API): PUT the file
    to the returned URL, then POST /{id}/ingest."""
    _check_format(req.filename)
    pool = await db.get_pool(_dsn())
    await _reject_duplicate(pool, req.filename, req.agent_id)
    await _check_doc_limit(pool, req.agent_id)
    doc_id = uuid.uuid4()
    key = f"{doc_id}/{req.filename}"
    await pool.execute(
        "INSERT INTO knowledge_base (id, filename, content_type, object_key,"
        " embedding_profile, agent_id) VALUES ($1, $2, $3, $4, $5, $6)",
        doc_id,
        req.filename,
        req.content_type,
        key,
        _profile()["name"],
        req.agent_id,
    )
    url = minio.presigned_put_object(
        S3["bucket"],
        key,
        expires=datetime.timedelta(
            minutes=int(os.environ.get("S3_PRESIGN_TTL_MIN", "15"))
        ),
    )
    return {"doc_id": str(doc_id), "upload_url": url}


@api.post("/knowledge-base/{doc_id}/update")
async def update_doc(doc_id: uuid.UUID, file: UploadFile = File(...)):
    """Replace a document with a newer version — same doc_id (stored
    citations keep resolving), old vectors swapped out by the worker
    (delete-before-upsert), re-ingestion queued automatically."""
    _check_format(file.filename or "")
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT object_key, agent_id FROM knowledge_base WHERE id = $1", doc_id
    )
    if row is None:
        raise HTTPException(404, "unknown doc_id")
    # A replaced doc keeps its agent scope.
    await _reject_duplicate(
        pool, file.filename or "", row["agent_id"], exclude_id=doc_id
    )

    key = f"{doc_id}/{file.filename}"
    f = file.file
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    _check_size(size, file.filename or "")
    await asyncio.to_thread(
        minio.put_object,
        S3["bucket"],
        key,
        f,
        size,
        content_type=file.content_type or "application/octet-stream",
    )
    if row["object_key"] != key:
        try:
            minio.remove_object(S3["bucket"], row["object_key"])
        except Exception as e:
            logger.warning(f"old object cleanup failed: {e}")
    await pool.execute(
        "UPDATE knowledge_base SET filename=$2, content_type=$3,"
        " object_key=$4, size_bytes=$5, status='pending', error=NULL,"
        " chunk_count=0, embedding_profile=$6, updated_at=now()"
        " WHERE id=$1",
        doc_id,
        file.filename,
        file.content_type,
        key,
        size,
        _profile()["name"],
    )
    return {"doc_id": str(doc_id), "status": "pending", "updated": True}


async def _reconcile_upload_status(pool, doc: dict) -> dict:
    """Presigned uploads go straight to S3, so the API can't flip the row's
    status at upload time — reconcile lazily whenever a row is read."""
    if doc["status"] == "awaiting_upload":
        try:
            await asyncio.to_thread(
                minio.stat_object, S3["bucket"], doc["object_key"]
            )
        except Exception:
            return doc
        await pool.execute(
            "UPDATE knowledge_base SET status='uploaded', updated_at=now()"
            " WHERE id=$1",
            doc["id"],
        )
        doc["status"] = "uploaded"
    return doc


@api.post("/knowledge-base/{doc_id}/ingest")
async def ingest(doc_id: uuid.UUID):
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT object_key FROM knowledge_base WHERE id = $1", doc_id
    )
    if row is None:
        raise HTTPException(404, "unknown doc_id")
    try:
        minio.stat_object(S3["bucket"], row["object_key"])
    except Exception:
        raise HTTPException(409, "file not uploaded yet — PUT to upload_url first")
    await pool.execute(
        "UPDATE knowledge_base SET status='pending', error=NULL,"
        " updated_at=now() WHERE id=$1",
        doc_id,
    )
    return {"doc_id": str(doc_id), "status": "pending"}


@api.get("/knowledge-base")
async def list_docs(agent_id: str | None = None):
    """All docs, or — with ?agent_id= — one agent's docs plus shared
    (untagged) ones, mirroring what that agent can retrieve."""
    pool = await db.get_pool(_dsn())
    if agent_id:
        rows = await pool.fetch(
            "SELECT id, filename, status, progress, chunk_count, error, object_key,"
            " embedding_profile, agent_id, created_at, updated_at"
            " FROM knowledge_base"
            " WHERE agent_id = $1 OR agent_id IS NULL"
            " ORDER BY created_at DESC",
            agent_id,
        )
    else:
        rows = await pool.fetch(
            "SELECT id, filename, status, progress, chunk_count, error, object_key,"
            " embedding_profile, agent_id, created_at, updated_at"
            " FROM knowledge_base ORDER BY created_at DESC"
        )
    docs = [await _reconcile_upload_status(pool, dict(r)) for r in rows]
    for d in docs:
        d["id"] = str(d["id"])
        d.pop("object_key", None)
        # The client polls while anything is still ingesting — served as a
        # boolean so the frontend never enumerates the status state machine.
        d["busy"] = d["status"] not in ("ready", "failed")
    return docs


@api.get("/knowledge-base/{doc_id}")
async def get_doc(doc_id: uuid.UUID):
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT id, filename, status, chunk_count, error, timings,"
        " object_key, embedding_profile, created_at, updated_at"
        " FROM knowledge_base WHERE id = $1",
        doc_id,
    )
    if row is None:
        raise HTTPException(404, "unknown doc_id")
    doc = await _reconcile_upload_status(pool, dict(row))
    doc.pop("object_key", None)
    return doc | {
        "id": str(doc["id"]),
        "timings": json.loads(doc["timings"]) if doc["timings"] else None,
    }


def _purge_doc_stores(doc_id, object_key: str) -> None:
    """Remove a doc's chunks and file (Qdrant, both profile collections —
    a re-ingested doc can have vectors in more than one — plus the S3
    object). The DB row is the caller's job. Best-effort; each store
    failure is logged, not fatal."""
    try:
        # Every profile's collection comes from the catalog registry — a
        # third embedding profile is purged here automatically instead of
        # silently leaking vectors.
        for coll in catalog.embedding_collections():
            if qd.collection_exists(coll):
                rag.delete_doc(qd, coll, str(doc_id))
    except Exception as e:
        logger.warning(f"qdrant delete failed for {doc_id}: {e}")
    try:
        minio.remove_object(S3["bucket"], object_key)
    except Exception as e:
        logger.warning(f"s3 delete failed for {doc_id}: {e}")


@api.delete("/knowledge-base/{doc_id}")
async def delete(doc_id: uuid.UUID):
    """Remove doc + chunks: DB row, S3 object, Qdrant points."""
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT object_key FROM knowledge_base WHERE id = $1", doc_id
    )
    if row is None:
        raise HTTPException(404, "unknown doc_id")
    _purge_doc_stores(doc_id, row["object_key"])
    await pool.execute("DELETE FROM knowledge_base WHERE id = $1", doc_id)
    return {"doc_id": str(doc_id), "deleted": True}


# ─── ingestion worker (job queue on Postgres row locking) ───────────────────


async def ingestion_worker(pool):
    while True:
        row = None
        try:
            async with pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, filename, object_key, agent_id FROM knowledge_base"
                    " WHERE status='pending' ORDER BY updated_at"
                    " LIMIT 1 FOR UPDATE SKIP LOCKED"
                )
                if row:
                    await conn.execute(
                        "UPDATE knowledge_base SET status='extracting',"
                        " progress=15, embedding_profile=$2, updated_at=now()"
                        " WHERE id=$1",
                        row["id"],
                        _profile()["name"],
                    )
            if not row:
                await asyncio.sleep(float(os.environ.get("INGEST_POLL_S", "2")))
                continue
            logger.info(f"ingesting {row['filename']} ({row['id']})")
            await process_doc(pool, row)
            logger.info(f"ready: {row['filename']} ({row['id']})")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("ingestion failed")
            if row:
                await pool.execute(
                    "UPDATE knowledge_base SET status='failed', error=$2,"
                    " updated_at=now() WHERE id=$1",
                    row["id"],
                    str(e),
                )


async def _doc_gone(pool, doc_id) -> bool:
    """The doc's ✕ in the UI is also a cancel: DELETE removes the row, and
    the worker checks for that at every stage boundary so it stops instead
    of upserting vectors for a document that no longer exists."""
    return (
        await pool.fetchval(
            "SELECT 1 FROM knowledge_base WHERE id = $1", doc_id
        )
        is None
    )


async def process_doc(pool, row):
    rag_cfg = _rag_cfg()
    prof = _profile()
    timings = {"ocr": rag_cfg.get("ocr", "auto"), "profile": prof["name"]}
    t0 = time.monotonic()
    suffix = pathlib.Path(row["filename"]).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
    minio.fget_object(S3["bucket"], row["object_key"], tmp_path)
    timings["fetch_s"] = round(time.monotonic() - t0, 3)
    # docling can take a while on first run (model downloads) — keep the
    # event loop free
    t0 = time.monotonic()
    text = await asyncio.to_thread(
        rag.extract_text, tmp_path, rag_cfg.get("ocr", "auto")
    )
    timings["extract_s"] = round(time.monotonic() - t0, 3)
    pathlib.Path(tmp_path).unlink(missing_ok=True)
    if await _doc_gone(pool, row["id"]):
        logger.info(f"doc {row['id']} deleted mid-ingestion — aborting")
        return

    await pool.execute(
        "UPDATE knowledge_base SET status='chunking', progress=75,"
        " updated_at=now() WHERE id=$1",
        row["id"],
    )
    t0 = time.monotonic()
    # Chunking geometry is a per-agent RAG knob (rag.chunk_size /
    # rag.chunk_overlap in the agent config), defaulting to rag.py's
    # constants — it used to be uncallable-with-arguments hardcoding.
    chunks = rag.chunk(
        text,
        size=int(rag_cfg.get("chunk_size") or rag.CHUNK_SIZE),
        overlap=int(rag_cfg.get("chunk_overlap") or rag.CHUNK_OVERLAP),
    )
    timings["chunk_s"] = round(time.monotonic() - t0, 3)
    if not chunks:
        raise ValueError("no text extracted")

    await pool.execute(
        "UPDATE knowledge_base SET status='embedding', progress=80,"
        " updated_at=now() WHERE id=$1",
        row["id"],
    )
    # Batched so the UI can show real progress across 80→99%, and so a
    # mid-flight delete aborts between batches.
    t0 = time.monotonic()
    vectors = []
    batch_size = 8
    for i in range(0, len(chunks), batch_size):
        if await _doc_gone(pool, row["id"]):
            logger.info(f"doc {row['id']} deleted mid-ingestion — aborting")
            return
        vectors.extend(
            await rag.embed_texts(
                chunks[i : i + batch_size],
                base_url=prof["base_url"],
                api_key=prof["api_key"],
                model=prof["model"],
            )
        )
        pct = 80 + int(19 * min(i + batch_size, len(chunks)) / len(chunks))
        await pool.execute(
            "UPDATE knowledge_base SET progress=$2, updated_at=now()"
            " WHERE id=$1",
            row["id"],
            pct,
        )
    timings["embed_s"] = round(time.monotonic() - t0, 3)
    if await _doc_gone(pool, row["id"]):
        logger.info(f"doc {row['id']} deleted mid-ingestion — aborting")
        return
    t0 = time.monotonic()
    await asyncio.to_thread(rag.ensure_collection, qd, prof["collection"], prof["dim"])
    # replace semantics: clear this doc's existing vectors before upserting
    # the new set — re-ingest and /update must swap, never append
    await asyncio.to_thread(rag.delete_doc, qd, prof["collection"], str(row["id"]))
    await asyncio.to_thread(
        rag.upsert_chunks,
        qd,
        prof["collection"],
        str(row["id"]),
        row["filename"],
        chunks,
        vectors,
        row["agent_id"],
    )
    timings["upsert_s"] = round(time.monotonic() - t0, 3)
    timings["total_s"] = round(
        sum(v for k, v in timings.items() if str(k).endswith("_s")), 3
    )
    logger.info(f"timings for {row['filename']}: {timings}")
    await pool.execute(
        "UPDATE knowledge_base SET status='ready', progress=100,"
        " chunk_count=$2, timings=$3::jsonb, updated_at=now() WHERE id=$1",
        row["id"],
        len(chunks),
        json.dumps(timings),
    )


# ─── public API v1 (pk_-key surface for embeds / customer frontends) ─────────
# Deliberately tiny: safe agent metadata, start a session, end a session.
# No transcripts, no agent list, no KB — a publishable key must never read
# the owner's business data.

public = APIRouter(prefix="/api/public/v1")

_PUBLIC_AGENT_FIELDS = ("id", "name", "icon", "description", "greeting")


async def _resolve_pk(request: Request):
    """pk_ header/query → active deployment row, enforcing the origin
    allowlist on the real request (preflights can't carry the key; the
    browser discards a disallowed response anyway). No Origin header
    (curl, server-to-server) passes — the key itself is the credential."""
    pk = request.headers.get("x-arya-key") or request.query_params.get("key") or ""
    if not (pk.startswith("pk_") and 20 <= len(pk) <= 70):
        raise HTTPException(401, "missing or malformed key")
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT * FROM deployments WHERE public_key = $1", pk
    )
    if row is None or row["status"] == "revoked":
        raise HTTPException(401, "unknown or revoked key")
    if row["status"] == "paused":
        raise HTTPException(403, "this agent is paused")
    origins = json.loads(row["allowed_origins"])
    origin = (request.headers.get("origin") or "").rstrip("/").lower()
    if origins and origin:
        # The engine's own pages (the share link) always pass: a same-origin
        # call's Origin host equals the Host header it arrives with, and
        # PUBLIC_BASE_URL names this engine explicitly. Without this, adding
        # the first customer domain silently killed the share link, because
        # nobody thinks to whitelist the engine itself. Not spoofable from
        # another site: browsers set Origin, pages can't.
        own_host = (request.headers.get("host") or "").lower()
        is_self = (
            origin.split("://", 1)[-1] == own_host
            or origin == os.environ.get("PUBLIC_BASE_URL", "").rstrip("/").lower()
        )
        if not is_self and origin not in [o.rstrip("/").lower() for o in origins]:
            raise HTTPException(403, "origin not allowed for this key")
    return row


@public.get("/agent")
async def public_agent_info(request: Request):
    dep = await _resolve_pk(request)
    cfg = config.load_saved_agent(dep["agent_id"])
    if cfg is None:
        raise HTTPException(410, "agent no longer exists")
    # Allowlist projection — instructions/keys/models never leave here.
    public = {k: cfg.get(k) for k in _PUBLIC_AGENT_FIELDS}
    # The widget shows the greeting as it will be spoken.
    public["greeting"] = config.render_vars(public.get("greeting"), cfg)
    return {
        "agent": public,
        "theme": json.loads(dep["branding"]),
        "limits": {"max_session_seconds": dep["max_session_seconds"]},
        # Presentational default the widget reads on boot: whether visitor
        # sessions show the collapsed "Sources" control at all. Never a
        # security gate (web citations are public pages); doc citations
        # are withheld server-side regardless of this flag.
        "display": {"show_sources": dep["show_sources"]},
    }


class PublicSessionReq(BaseModel):
    # The signed token this server handed the widget on an earlier session
    # (visitors.py). Absent or invalid: the visitor is new and gets a fresh
    # one in the response. Never an error, a widget must keep working.
    visitor_token: str | None = Field(default=None, max_length=80)
    # Legacy field from widgets built before signed tokens. Accepted so an
    # old loader still parses, IGNORED: a client-chosen id let any page
    # read any memory bucket.
    user_id: str | None = None
    # Set by a channel bridge (arya-whatsapp), never by a browser: it puts
    # the worker into a text-only session and tags the conversation so
    # History can tell a WhatsApp thread from a widget call. Honoured only
    # when the request also carries the bridge token header; from anyone
    # else they are dropped and the session is a plain widget visitor.
    channel: Literal["whatsapp", "phone"] | None = None
    external_user_id: str | None = Field(default=None, max_length=64)


# Rate limiting for public session starts. The quota caps bound how many
# calls exist; nothing bounded how FAST someone could attempt them, so a
# script with a leaked public key could burn a whole daily quota in
# seconds (each attempt also takes the per-deployment advisory lock and
# runs two counting queries). Two sliding one-minute windows: per
# deployment (the key that costs money) and per client IP (one visitor
# must not start many calls, while one deployment legitimately serves
# many visitors). In-memory on purpose: single api process, and a lost
# window on restart merely admits one extra burst.
class _SlidingLimiter:
    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True  # 0 disables the limiter
        now = time.monotonic()
        cutoff = now - self.window_s
        q = [t for t in self._hits.get(key, []) if t > cutoff]
        if len(q) >= self.limit:
            self._hits[key] = q
            return False
        q.append(now)
        self._hits[key] = q
        # Housekeeping: an attacker rotating IPs must not grow the dict
        # without bound. Sweep dead keys once it gets large.
        if len(self._hits) > 10000:
            self._hits = {
                k: v for k, v in self._hits.items() if v and v[-1] > cutoff
            }
        return True


_dep_limiter = _SlidingLimiter(
    int(os.environ.get("PUBLIC_SESSION_RATE_PER_MIN", "10"))
)
_ip_limiter = _SlidingLimiter(
    int(os.environ.get("PUBLIC_SESSION_RATE_PER_IP_PER_MIN", "5"))
)


def _client_ip(request: Request) -> str:
    # First X-Forwarded-For hop when a reverse proxy fronts the engine.
    # Spoofable by a direct caller, which is acceptable: the per-IP limit
    # is a second dimension, the per-deployment limit still holds.
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "unknown")


async def _record_refusal(deployment_id, reason: str) -> None:
    """One aggregated row per deployment, reason and minute. A 429 creates
    no conversation row, so this table is the only trace the product has
    that visitors were turned away; Insights and the Publish usage chips
    read it. Never lets a bookkeeping error break the refusal response."""
    try:
        pool = await db.get_pool(_dsn())
        await pool.execute(
            "INSERT INTO session_refusals (deployment_id, reason, count,"
            " window_start) VALUES ($1, $2, 1, date_trunc('minute', now()))"
            " ON CONFLICT (deployment_id, reason, window_start)"
            " DO UPDATE SET count = session_refusals.count + 1",
            deployment_id,
            reason,
        )
    except Exception as e:
        logger.warning(f"refusal record skipped: {e}")


@public.post("/sessions")
async def public_start_session(
    request: Request, req: PublicSessionReq | None = None
):
    # Per-IP throttle first: it needs no key lookup, so hammering with an
    # invalid key never reaches the database at all.
    if not _ip_limiter.allow(_client_ip(request)):
        raise HTTPException(
            429, "too many call attempts from this address, slow down"
        )
    dep = await _resolve_pk(request)
    if not _dep_limiter.allow(str(dep["id"])):
        await _record_refusal(dep["id"], "rate_limited")
        raise HTTPException(
            429, "too many call attempts for this agent, slow down"
        )
    cfg = config.load_saved_agent(dep["agent_id"])
    if cfg is None:
        raise HTTPException(410, "agent no longer exists")
    if await _validate_full(cfg):
        raise HTTPException(503, "agent is not currently runnable")
    # Only a bridge may name the customer. A browser that posts channel
    # fields without the bridge token gets a plain widget session, not a
    # refusal: refusing would tell a probe exactly which header it lacks.
    bridge = visitors.is_bridge_request(request.headers.get("x-arya-bridge-token"))
    if req and (req.channel or req.external_user_id) and not bridge:
        logger.warning(
            "public session carried channel fields without the bridge token;"
            " treated as a widget visitor"
        )
    channel = req.channel if (req and bridge) else None
    external = (req.external_user_id if (req and bridge) else None) or ""
    visitor_token = None
    if external:
        # A channel identifies its customer by phone number, which is
        # stable across threads and is what long-term memory should key on.
        raw = external
    else:
        # A widget visitor is whoever holds a token WE minted for THIS
        # deployment. Anything else (nothing, tampered, another agent's
        # token, a rotated secret) starts a new visitor with a new token.
        rand = visitors.verify_token(dep["id"], req.visitor_token if req else None)
        if rand is None:
            visitor_token = visitors.mint_token(dep["id"])
            rand = visitor_token.split(".", 1)[0]
        else:
            visitor_token = req.visitor_token
        raw = rand
    # Namespaced per deployment so one site's key can never probe another
    # site's memories.
    user_id = visitors.derive_user_id(dep["id"], raw)
    try:
        out = await _start_session(
            agent_cfg=cfg, user_id=user_id, deployment=dep,
            channel=channel, external_user=external or None,
        )
    except HTTPException as e:
        # The quota gate's refusals must leave a trace too, or an owner
        # whose caps are full loses visitors invisibly.
        if e.status_code == 429:
            reason = (
                "concurrent_limit"
                if "concurrent" in str(e.detail)
                else "daily_limit"
            )
            await _record_refusal(dep["id"], reason)
        raise
    out["agent"] = {k: cfg.get(k) for k in _PUBLIC_AGENT_FIELDS}
    if visitor_token:
        # The widget stores this and sends it back next time. A channel
        # session has no browser to hold one.
        out["visitor_token"] = visitor_token
    return out


@public.get("/itinerary/{artifact_id}")
async def public_itinerary(artifact_id: uuid.UUID, request: Request):
    """The itinerary's structured payload, for host pages that render their
    own panel (the widget announces the artifact id over postMessage; this
    endpoint lets the page re-fetch after a refresh). Scoped to the key's
    own deployment — any key must not read any itinerary. Returns data
    only, never HTML, instructions, or model settings; the printable page
    stays at /api/artifacts/{id}."""
    dep = await _resolve_pk(request)
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT a.title, a.data, a.updated_at FROM artifacts a"
        " JOIN conversations c ON c.id = a.conversation_id"
        " WHERE a.id = $1 AND c.deployment_id = $2",
        artifact_id, dep["id"],
    )
    if row is None:
        raise HTTPException(404, "unknown itinerary for this key")
    return {
        "artifact_id": str(artifact_id),
        "title": row["title"],
        "url": f"/api/artifacts/{artifact_id}",
        "updated_at": row["updated_at"],
        "data": json.loads(row["data"]) if row["data"] else None,
    }


@public.post("/sessions/{conversation_id}/end")
async def public_end_session(conversation_id: uuid.UUID, request: Request):
    dep = await _resolve_pk(request)
    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT room_name FROM conversations WHERE id = $1"
        " AND deployment_id = $2",
        conversation_id, dep["id"],
    )
    if row is None:
        # Scoped to THIS key's sessions — any key must not end any call.
        raise HTTPException(404, "unknown session for this key")
    # Same endings classification as the admin end route: this path IS the
    # widget's End button, so it is a deliberate hang-up. Without this stamp
    # every embedded caller who pressed End landed in the unclassified
    # bucket, which made the Insights endings card read as mostly unknown.
    await pool.execute(
        "UPDATE conversations SET metadata ="
        " COALESCE(metadata, '{}'::jsonb) || '{\"ended_reason\":"
        " \"user_ended\"}'::jsonb WHERE id = $1",
        conversation_id,
    )
    lkapi = _lk_client()
    try:
        await lkapi.room.delete_room(
            lk_api.DeleteRoomRequest(room=row["room_name"])
        )
    except Exception as e:
        logger.warning(f"public end delete_room: {e}")
    finally:
        await lkapi.aclose()
    return {"session_id": str(conversation_id), "ended": True}


class _PublicCORS(BaseHTTPMiddleware):
    """Per-deployment origins can't live in the static CORSMiddleware
    allowlist. Scoped to the public/embed paths; MUST terminate preflights
    itself (the inner CORSMiddleware answers 400 for unknown origins).
    Authorization happens on the real request in _resolve_pk — a
    disallowed origin gets its 403 body, credentials are never allowed."""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if not (path.startswith("/api/public/") or path.startswith("/embed/")):
            return await call_next(request)
        origin = request.headers.get("origin")
        if (
            request.method == "OPTIONS"
            and "access-control-request-method" in request.headers
        ):
            return Response(status_code=204, headers={
                "access-control-allow-origin": origin or "*",
                "access-control-allow-methods": "GET, POST, OPTIONS",
                "access-control-allow-headers": "content-type, x-arya-key",
                "access-control-max-age": "600",
                "vary": "Origin",
            })
        resp = await call_next(request)
        if origin:
            resp.headers["access-control-allow-origin"] = origin
            resp.headers.append("vary", "Origin")
        return resp


@app.get("/embed/{public_key}")
async def embed_page(public_key: str):
    """The widget iframe document (also the share link). Key resolved
    HERE so a revoked key 404s before any JS loads, and frame-ancestors —
    not CORS — is what stops unauthorized sites from framing it."""
    from fastapi.responses import FileResponse, HTMLResponse

    pool = await db.get_pool(_dsn())
    row = await pool.fetchrow(
        "SELECT status, allowed_origins FROM deployments WHERE public_key=$1",
        public_key,
    )
    if row is None or row["status"] != "active":
        # HTML, not JSON — this renders inside an iframe on someone's site.
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8>"
            "<body style='font-family:system-ui;display:grid;"
            "place-items:center;height:100vh;margin:0;color:#555'>"
            "This assistant is unavailable.</body>",
            status_code=404,
        )
    origins = " ".join(json.loads(row["allowed_origins"]))
    page = config.BACKEND_DIR.parent / "frontend" / "dist" / "widget" / "widget.html"
    if not page.is_file():
        return HTMLResponse("widget not built", status_code=503)
    return FileResponse(
        page,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",  # revocation must bite instantly
            "Referrer-Policy": "no-referrer",
            # Empty allowlist = dev mode: any site may frame it.
            "Content-Security-Policy":
                f"frame-ancestors 'self' {origins}" if origins
                else "frame-ancestors *",
        },
    )


# Added AFTER CORSMiddleware -> runs OUTSIDE it (Starlette: last added is
# outermost), so public preflights never reach the static allowlist.
app.add_middleware(_PublicCORS)

app.include_router(open_rt)
app.include_router(api)
app.include_router(public)

_dist = config.BACKEND_DIR.parent / "frontend" / "dist"

if RUNTIME_ONLY:
    # Runtime ("engine-only") mode: serve ONLY the embed surface — the
    # widget assets, the loader, and the public API. The builder SPA is
    # not mounted at all, so a public engine exposes no builder to find.
    # Admin /api/* stays available to the token holder (that is how
    # agents are pushed/imported onto an engine); the boot guard in
    # lifespan refuses to start without ARYA_ADMIN_TOKEN.
    @app.get("/arya-widget.js")
    async def loader_js():
        from fastapi.responses import FileResponse
        f = _dist / "arya-widget.js"
        if not f.is_file():
            raise HTTPException(503, "widget not built")
        return FileResponse(
            f,
            media_type="application/javascript",
            # Stable filename customers paste — short TTL so loader fixes
            # propagate same-day without cache-busting their HTML.
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/")
    async def engine_root():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8>"
            "<body style='font-family:system-ui;display:grid;"
            "place-items:center;height:100vh;margin:0;color:#555'>"
            "Arya engine is running.</body>"
        )

    if (_dist / "widget").is_dir():
        app.mount(
            "/widget",
            StaticFiles(directory=_dist / "widget"),
            name="widget-assets",
        )
    else:
        logger.warning(f"widget build not found at {_dist}/widget")
elif _dist.is_dir():
    # Full mode (see Dockerfile / `npm run build`): one URL for everything —
    # builder SPA at /, widget assets + loader come from the same dist tree.
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
else:
    logger.warning(
        f"frontend build not found at {_dist} — API only. "
        "Run `npm run build` in frontend/ (or use the Vite dev server)."
    )
