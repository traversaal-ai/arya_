"""Visitor identity (signed widget tokens) and forgetting a person.

Token and secret tests are pure. The purge test needs a database:
ARYA_TEST_DSN, else DATABASE_URL (backend/.env.local, loaded when api is
imported), skipped when neither answers. The public session tests drive
the real route through FastAPI's TestClient with the deployment lookup,
validation and the LiveKit room stubbed out, so what is under test is
exactly the identity logic."""

import asyncio
import json
import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

import api
import db
import visitors

DEP_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEP_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _flip(sig: str) -> str:
    return ("0" if sig[0] != "0" else "1") + sig[1:]


# ─── tokens ──────────────────────────────────────────────────────────────────


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("ARYA_VISITOR_SECRET", "test-visitor-secret")


def test_mint_then_verify_round_trips(secret):
    tok = visitors.mint_token(DEP_A)
    rand = tok.split(".")[0]
    assert visitors.verify_token(DEP_A, tok) == rand


def test_a_flipped_signature_character_fails(secret):
    rand, sig = visitors.mint_token(DEP_A).split(".")
    assert visitors.verify_token(DEP_A, f"{rand}.{_flip(sig)}") is None


def test_a_token_minted_for_one_deployment_fails_on_another(secret):
    assert visitors.verify_token(DEP_B, visitors.mint_token(DEP_A)) is None


def test_garbage_is_not_a_token(secret):
    for bad in (None, "", "no-dot", "a.b", "x" * 200, 42):
        assert visitors.verify_token(DEP_A, bad) is None


def test_user_id_is_16_hex_and_deterministic():
    a = visitors.derive_user_id(DEP_A, "rand")
    assert a == visitors.derive_user_id(DEP_A, "rand")
    assert len(a) == 16
    int(a, 16)  # hex or raises
    assert a != visitors.derive_user_id(DEP_B, "rand")


def test_the_derived_secret_is_stable_without_the_env(monkeypatch):
    monkeypatch.delenv("ARYA_VISITOR_SECRET", raising=False)
    monkeypatch.setenv("LIVEKIT_API_SECRET", "lk-secret")
    assert visitors.visitor_secret() == visitors.visitor_secret()
    tok = visitors.mint_token(DEP_A)
    assert visitors.verify_token(DEP_A, tok)
    # A different LiveKit secret is a different install: tokens die with it.
    monkeypatch.setenv("LIVEKIT_API_SECRET", "other")
    assert visitors.verify_token(DEP_A, tok) is None


def test_the_bridge_token_needs_a_value_on_both_sides(monkeypatch):
    monkeypatch.setenv("ARYA_BRIDGE_TOKEN", "bridge-1")
    assert visitors.is_bridge_request("bridge-1")
    assert not visitors.is_bridge_request("bridge-2")
    assert not visitors.is_bridge_request(None)
    monkeypatch.delenv("ARYA_BRIDGE_TOKEN")
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    assert visitors.bridge_token() == ""
    assert not visitors.is_bridge_request("")


# ─── purge against a real database ───────────────────────────────────────────


def _dsn() -> str | None:
    return os.environ.get("ARYA_TEST_DSN") or os.environ.get("DATABASE_URL")


async def _reachable(dsn: str) -> bool:
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn, timeout=3), 5)
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture
async def pool():
    d = _dsn()
    if not d or not await _reachable(d):
        pytest.skip("no reachable database (set ARYA_TEST_DSN)")
    p = await db.get_pool(d)
    yield p
    # The module-level pool is bound to this test's event loop.
    await p.close()
    db._pool = None


async def _seed_call(conn, user_id: str):
    cid = uuid.uuid4()
    room = f"conv-test-{cid.hex[:12]}"
    meta = {"user_id": user_id, "agent_id": "agent-test", "agent_name": "Test agent"}
    await conn.execute(
        "INSERT INTO conversations (id, room_name, metadata) VALUES ($1, $2, $3)",
        cid, room, json.dumps(meta),
    )
    await conn.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES ($1, 'user', 'hello')",
        cid,
    )
    await conn.execute(
        "INSERT INTO artifacts (id, conversation_id, agent_id, kind, title, html)"
        " VALUES ($1, $2, 'agent-test', 'itinerary', 't', '<p>x</p>')",
        uuid.uuid4(), cid,
    )
    await conn.execute(
        "INSERT INTO tool_events (room_name, agent_id, conversation_id, tool, ok)"
        " VALUES ($1, 'agent-test', $2, 'lookup', true)",
        room, cid,
    )
    await conn.execute(
        "INSERT INTO rag_events (room_name, agent_id, query) VALUES ($1, 'agent-test', 'q')",
        room,
    )
    return cid, room


async def _counts(conn, cids, rooms) -> dict:
    by_conv = "SELECT count(*) FROM {} WHERE conversation_id = ANY($1::uuid[])"
    return {
        "conversations": await conn.fetchval(
            "SELECT count(*) FROM conversations WHERE id = ANY($1::uuid[])", cids
        ),
        "messages": await conn.fetchval(by_conv.format("messages"), cids),
        "artifacts": await conn.fetchval(by_conv.format("artifacts"), cids),
        "tool_events": await conn.fetchval(by_conv.format("tool_events"), cids),
        "rag_events": await conn.fetchval(
            "SELECT count(*) FROM rag_events WHERE room_name = ANY($1::text[])", rooms
        ),
    }


async def test_forgetting_a_visitor_clears_every_table_and_spares_others(pool):
    vis = f"test-{uuid.uuid4().hex[:12]}"
    other = f"test-{uuid.uuid4().hex[:12]}"
    async with pool.acquire() as conn:
        a = await _seed_call(conn, vis)
        b = await _seed_call(conn, vis)
        c = await _seed_call(conn, other)
    try:
        out = await visitors.forget_visitor_rows(pool, vis)
        assert out["conversations_deleted"] == 2
        async with pool.acquire() as conn:
            gone = await _counts(conn, [a[0], b[0]], [a[1], b[1]])
            kept = await _counts(conn, [c[0]], [c[1]])
        assert gone == {k: 0 for k in gone}, gone
        assert kept == {k: 1 for k in kept}, kept
        again = await visitors.forget_visitor_rows(pool, vis)
        assert again["conversations_deleted"] == 0
    finally:
        await visitors.forget_visitor_rows(pool, other)


# ─── the public session route ────────────────────────────────────────────────


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv("ARYA_VISITOR_SECRET", "test-visitor-secret")
    monkeypatch.setenv("ARYA_BRIDGE_TOKEN", "bridge-token")
    dep = {
        "id": DEP_A, "agent_id": "agent-test", "status": "active",
        "allowed_origins": "[]", "max_session_seconds": 600,
    }
    started = []

    async def resolve(request):
        return dep

    async def validate(cfg):
        return []

    async def start(*, agent_cfg, user_id, deployment, channel=None, external_user=None):
        started.append(
            {"user_id": user_id, "channel": channel, "external_user": external_user}
        )
        return {
            "conversation_id": str(uuid.uuid4()), "room": "conv-x",
            "token": "t", "ws_url": "ws://x", "identity": "user-x",
        }

    monkeypatch.setattr(api, "_resolve_pk", resolve)
    monkeypatch.setattr(api, "_validate_full", validate)
    monkeypatch.setattr(api, "_start_session", start)
    monkeypatch.setattr(
        api.config, "load_saved_agent", lambda aid: {"id": aid, "name": "Test"}
    )
    monkeypatch.setattr(api._ip_limiter, "limit", 0)
    monkeypatch.setattr(api._dep_limiter, "limit", 0)
    client = TestClient(api.app)

    def post(body, headers=None):
        r = client.post(
            "/api/public/v1/sessions", json=body,
            headers={"X-Arya-Key": "pk_test", **(headers or {})},
        )
        assert r.status_code == 200, r.text
        return r.json()

    return post, started


def test_a_new_visitor_gets_a_token_and_keeps_it(session):
    post, started = session
    first = post({})
    tok = first["visitor_token"]
    assert visitors.verify_token(DEP_A, tok)
    second = post({"visitor_token": tok})
    assert second["visitor_token"] == tok
    assert started[0]["user_id"] == started[1]["user_id"]
    assert len(started[0]["user_id"]) == 16


def test_a_tampered_token_is_a_new_visitor_not_an_error(session):
    post, started = session
    rand, sig = post({})["visitor_token"].split(".")
    bad = f"{rand}.{_flip(sig)}"
    out = post({"visitor_token": bad})
    assert out["visitor_token"] != bad
    assert started[0]["user_id"] != started[1]["user_id"]


def test_the_old_client_chosen_user_id_is_ignored(session):
    post, started = session
    out = post({"user_id": "attacker-picked"})
    assert out["visitor_token"]
    assert started[0]["user_id"] != visitors.derive_user_id(DEP_A, "attacker-picked")


def test_channel_fields_need_the_bridge_token(session):
    post, started = session
    body = {"channel": "whatsapp", "external_user_id": "15551234567"}
    out = post(body)
    assert started[-1]["channel"] is None and started[-1]["external_user"] is None
    assert out["visitor_token"]  # a plain widget visitor
    out = post(body, {"X-Arya-Bridge-Token": "bridge-token"})
    assert started[-1]["channel"] == "whatsapp"
    assert started[-1]["external_user"] == "15551234567"
    assert started[-1]["user_id"] == visitors.derive_user_id(DEP_A, "15551234567")
    assert "visitor_token" not in out


# ─── editing and deleting one memory ─────────────────────────────────────────


@pytest.fixture
def mem0(monkeypatch):
    """The admin API with Mem0 faked: visitor v1 owns memories m1 and m2,
    anything else belongs to someone. Records what was changed."""
    monkeypatch.delenv("ARYA_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(api.config, "load_keys", lambda: {"mem0": "key"})
    calls = []

    async def listing(key, user_id):
        return (
            [{"id": "m1", "memory": "likes tea"}, {"id": "m2", "memory": "allergic"}]
            if user_id == "v1" else []
        )

    async def delete(key, mid):
        calls.append(("delete", mid))

    async def update(key, mid, text):
        calls.append(("update", mid, text))

    monkeypatch.setattr(api.memory, "list_for_user", listing)
    monkeypatch.setattr(api.memory, "delete_memory", delete)
    monkeypatch.setattr(api.memory, "update_memory", update)
    return TestClient(api.app), calls


def test_a_visitor_can_edit_and_delete_their_own_memory(mem0):
    client, calls = mem0
    r = client.patch("/api/visitors/v1/memories/m1", json={"memory": "  likes coffee "})
    assert r.status_code == 200
    assert client.delete("/api/visitors/v1/memories/m2").status_code == 200
    assert calls == [("update", "m1", "likes coffee"), ("delete", "m2")]


def test_a_memory_that_is_not_theirs_is_never_touched(mem0):
    client, calls = mem0
    # m1 belongs to v1; acting on it as v2 must 404 and change nothing.
    assert client.delete("/api/visitors/v2/memories/m1").status_code == 404
    r = client.patch("/api/visitors/v2/memories/m1", json={"memory": "x"})
    assert r.status_code == 404
    assert client.delete("/api/visitors/v1/memories/nope").status_code == 404
    assert calls == []


def test_a_malformed_id_or_empty_text_is_refused(mem0):
    client, calls = mem0
    assert client.delete("/api/visitors/v1/memories/bad%20id").status_code == 400
    assert client.patch("/api/visitors/v1/memories/m1", json={"memory": ""}).status_code == 422
    assert calls == []