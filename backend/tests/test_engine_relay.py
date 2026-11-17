"""The builder sets a pushed agent's WhatsApp number and database on an
engine through the relay. Each route is a thin forward with the engine's
stored admin token; the engine does the real work and its answer passes
through unchanged, including its 201 on a create and its own error text.
"""

import json

import httpx
import pytest
from fastapi import HTTPException

import api

ENGINE = {"id": "e1", "name": "Production VM", "base_url": "https://vm.example",
          "admin_token": "tok"}


@pytest.fixture
def relay(monkeypatch):
    """Records every forwarded call and answers with a canned response."""
    seen = []
    canned = {"status": 200, "json": {"ok": True}}

    async def fake_relay(engine, method, path, json_body=None):
        seen.append((engine["id"], method, path, json_body))
        return httpx.Response(canned["status"], json=canned["json"],
                              request=httpx.Request(method, "https://vm.example"))

    monkeypatch.setattr(api, "_load_engines", lambda: [ENGINE])
    monkeypatch.setattr(api, "_engine_relay", fake_relay)
    return seen, canned


async def test_channels_are_forwarded_with_the_engines_paths(relay):
    seen, canned = relay
    canned["json"] = {"channels": []}
    assert await api.engine_list_channels("e1", "agent-x") == {"channels": []}
    assert seen[-1] == ("e1", "GET", "/api/agents/agent-x/channels", None)

    canned["status"], canned["json"] = 201, {"id": "chan-1"}
    req = api.ChannelReq(phone_number_id="1234567890", access_token="t" * 40,
                         app_secret="s" * 32)
    assert await api.engine_add_channel("e1", "agent-x", req) == {"id": "chan-1"}
    _eid, method, path, body = seen[-1]
    assert (method, path) == ("POST", "/api/agents/agent-x/channels")
    assert body["phone_number_id"] == "1234567890" and body["app_secret"] == "s" * 32
    assert "waba_id" not in body  # exclude_none: the engine applies its defaults

    canned["status"], canned["json"] = 200, {"id": "chan-1", "status": "paused"}
    await api.engine_patch_channel("e1", "chan-1", api.ChannelPatch(status="paused"))
    assert seen[-1] == ("e1", "PATCH", "/api/channels/chan-1", {"status": "paused"})
    await api.engine_delete_channel("e1", "chan-1")
    assert seen[-1][1:3] == ("DELETE", "/api/channels/chan-1")
    await api.engine_resubscribe_channel("e1", "chan-1")
    assert seen[-1][1:3] == ("POST", "/api/channels/chan-1/resubscribe")
    await api.engine_test_channel("e1", "chan-1", api.ChannelTestReq(to="923358370304"))
    assert seen[-1][2] == "/api/channels/chan-1/test"
    assert seen[-1][3]["to"] == "923358370304"


async def test_connections_and_attachments_are_forwarded(relay):
    seen, canned = relay
    canned["json"] = []
    await api.engine_list_connections("e1", type="postgres")
    assert seen[-1][1:3] == ("GET", "/api/connections?type=postgres")

    canned["status"], canned["json"] = 201, {"id": "conn-1", "shop_desk": True}
    out = await api.engine_add_connection(
        "e1", api.ConnectionReq(type="postgres", name="Shop", credential="postgresql://x")
    )
    assert out["shop_desk"] is True
    assert seen[-1][1:3] == ("POST", "/api/connections")
    assert seen[-1][3] == {"type": "postgres", "name": "Shop", "credential": "postgresql://x"}

    canned["status"], canned["json"] = 200, {"connectors": []}
    await api.engine_get_connectors("e1", "agent-x")
    assert seen[-1][1:3] == ("GET", "/api/agents/agent-x/connectors")
    att = [{"connection_id": "conn-1", "type": "postgres", "allowed_tools": [],
            "public_tools": []}]
    await api.engine_set_connectors("e1", "agent-x", api.ConnectorsReq(connectors=att))
    assert seen[-1] == ("e1", "PUT", "/api/agents/agent-x/connectors", {"connectors": att})


async def test_the_engines_own_refusal_passes_through(relay):
    _seen, canned = relay
    canned["status"] = 409
    canned["json"] = {"detail": "that number already answers as 'agent-y'"}
    req = api.ChannelReq(phone_number_id="1234567890", access_token="t" * 40,
                         app_secret="s" * 32)
    with pytest.raises(HTTPException) as e:
        await api.engine_add_channel("e1", "agent-x", req)
    assert e.value.status_code == 409 and "agent-y" in e.value.detail


async def test_an_old_engine_is_named_not_a_bare_404(relay):
    _seen, canned = relay
    canned["status"], canned["json"] = 404, {"detail": "Not Found"}
    with pytest.raises(HTTPException) as e:
        await api.engine_list_channels("e1", "agent-x")
    assert e.value.status_code == 502 and "older build" in e.value.detail


async def test_setting_connectors_needs_connections_this_machine_holds(monkeypatch, tmp_path):
    # The engine-side half: the attachment names a connection id; one the
    # engine does not hold is refused, one it holds is saved on the agent
    # file with its type filled in, and the worker reads that file fresh.
    saved = {}
    monkeypatch.setattr(api.config, "load_saved_agent",
                        lambda aid: {"id": aid, "name": "Flux", "connectors": []})
    monkeypatch.setattr(api.config, "save_agent", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(api.config, "load_active", lambda: {"id": "other"})

    class Pool:
        async def fetch(self, sql, ids):
            return [{"id": i, "type": "postgres"} for i in ids if i == "conn-1"]

    async def get_pool(dsn):
        return Pool()

    monkeypatch.setattr(api.db, "get_pool", get_pool)

    with pytest.raises(HTTPException) as e:
        await api.set_connectors("agent-x", api.ConnectorsReq(
            connectors=[{"connection_id": "conn-9"}]))
    assert e.value.status_code == 404 and "conn-9" in e.value.detail

    out = await api.set_connectors("agent-x", api.ConnectorsReq(
        connectors=[{"connection_id": "conn-1"}]))
    assert out["connectors"] == [{"connection_id": "conn-1", "type": "postgres",
                                  "allowed_tools": [], "public_tools": []}]
    assert saved["connectors"] == out["connectors"]

    out = await api.set_connectors("agent-x", api.ConnectorsReq(connectors=[]))
    assert out == {"connectors": []} and saved["connectors"] == []


async def test_a_repush_keeps_what_the_engine_had_attached(monkeypatch):
    # Seen live: re-pushing Flux to switch its model replaced the agent file
    # on the engine, and the shop database attached there was gone; the
    # agent went back to asking for an email. An export never carries
    # connectors, so an import must keep the ones this machine holds.
    import io
    import zipfile

    from fastapi import UploadFile

    attached = [{"connection_id": "conn-1", "type": "postgres",
                 "allowed_tools": [], "public_tools": []}]
    saved = {}
    monkeypatch.setattr(api.config, "load_saved_agent",
                        lambda aid: {"id": aid, "name": "Flux", "connectors": attached})
    monkeypatch.setattr(api.config, "save_agent", lambda cfg: saved.update(cfg))

    class Pool:
        async def fetch(self, sql, *args):
            return []

    async def get_pool(dsn):
        return Pool()

    monkeypatch.setattr(api.db, "get_pool", get_pool)

    def export(cfg: dict) -> UploadFile:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("agent.json", json.dumps(cfg))
            zf.writestr("manifest.json", json.dumps({"docs": []}))
        return UploadFile(io.BytesIO(buf.getvalue()), filename="agent.zip")

    # The push strips connectors: what was attached here survives it.
    out = await api.import_agent(export({"id": "agent-x", "name": "Flux v2"}))
    assert out["imported"] is True
    assert saved["name"] == "Flux v2" and saved["connectors"] == attached

    # An export that does carry connectors (a future builder-side choice)
    # wins over the engine's, so the push stays the source of truth.
    await api.import_agent(export({"id": "agent-x", "name": "Flux v3",
                                   "connectors": [{"connection_id": "conn-2"}]}))
    assert saved["connectors"] == [{"connection_id": "conn-2"}]
