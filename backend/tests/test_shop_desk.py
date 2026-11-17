"""The order desk over the demo shop database.

Two things matter here beyond "does it query". The fake desk must be
untouched when the setting is absent, because every existing agent and
test depends on it. And the real desk must not let one customer reach
another's orders by any phrasing, because that is the whole point of
identifying people by their number rather than by what they say.

The database-backed tests use ARYA_SHOP_DSN (or backend/.env.local) and
skip when it is unset or unreachable, the same way the read-only connector
tests do. They insert a return, so the seed script is how you reset.
"""

import asyncio
import os
import pathlib
import socket
from urllib.parse import urlparse

import pytest
from dotenv import load_dotenv

import tools as tools_mod

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env.local")

# The main persona in demo/shop/seed.sql, and a phone nobody has.
USAMA_PHONE = "923358370304"
UNKNOWN_PHONE = "10000000000"


def _fns(built) -> dict:
    out = {}
    for tool in built:
        name = getattr(tool, "name", None) or getattr(
            getattr(tool, "__func__", tool), "__name__", "")
        out[name] = getattr(tool, "_fnc", None) or getattr(tool, "__wrapped__", tool)
    return out


# ── the switch ───────────────────────────────────────────────────────────────


def test_without_the_setting_the_fake_desk_is_built(monkeypatch):
    monkeypatch.delenv(tools_mod.SHOP_DSN_ENV, raising=False)
    fns = _fns(tools_mod.build_tools({"tools": {"orders": True}}, {}))
    assert set(fns) == {"find_orders", "lookup_order", "start_return"}


def test_with_the_setting_the_real_desk_is_built(monkeypatch):
    # Building the tools must not connect: the pool is opened lazily on the
    # first call, so a wrong string fails at the first question, not here.
    monkeypatch.setenv(tools_mod.SHOP_DSN_ENV, "postgresql://x:y@nowhere:1/z")
    tools_mod._shop["pools"].clear()
    fns = _fns(tools_mod.build_tools({"tools": {"orders": True}}, {}))
    assert set(fns) == {"find_orders", "lookup_order", "start_return",
                        "return_status", "show_picture"}


def test_the_attached_connector_supplies_the_database(monkeypatch):
    # The normal route: the worker found a shop-shaped connection attached
    # to the agent (connectors.served_toolsets) and put its string on the
    # runtime. No environment variable involved.
    monkeypatch.delenv(tools_mod.SHOP_DSN_ENV, raising=False)
    fns = _fns(tools_mod.build_tools(
        {"tools": {"orders": True}}, {}, None, None,
        {"shop_dsn": "postgresql://x:y@nowhere:1/z"},
    ))
    assert "return_status" in fns


def test_the_desk_is_replaced_by_a_connector_regardless(monkeypatch):
    monkeypatch.setenv(tools_mod.SHOP_DSN_ENV, "postgresql://x:y@nowhere:1/z")
    assert tools_mod.build_tools(
        {"tools": {"orders": True}}, {}, skip=frozenset({"orders"})
    ) == []


async def test_an_unreachable_shop_degrades_to_a_sentence(monkeypatch):
    # A database hiccup must never break a live conversation.
    monkeypatch.setenv(tools_mod.SHOP_DSN_ENV, "postgresql://x:y@127.0.0.1:1/z")
    tools_mod._shop["pools"].clear()
    fns = _fns(tools_mod._shop_order_tools(
        {"channel": "whatsapp", "external_user": USAMA_PHONE}))
    out = await fns["find_orders"](None)
    assert out == tools_mod.SHOP_DOWN_TEXT
    tools_mod._shop["pools"].clear()


async def test_a_dropped_connection_is_retried_once_on_a_fresh_pool(monkeypatch):
    # Seen live: the worker had been up for hours, the pooled connection
    # through host.docker.internal was dead, and the first "Hi" got the
    # "system is not responding" line. One retry on a new pool fixes it;
    # a second failure still degrades to the sentence, never a traceback.
    calls = {"pools": 0, "queries": 0}

    class FlakyPool:
        def __init__(self, dead: bool):
            self.dead = dead

        async def fetchrow(self, sql, *args):
            calls["queries"] += 1
            if self.dead:
                raise ConnectionResetError("unexpected connection_lost() call")
            return {"id": 1, "first_name": "Usama"}

        async def fetch(self, sql, *args):
            return []

        def terminate(self):
            pass

    async def make_pool(dsn):
        calls["pools"] += 1
        pool = tools_mod._shop["pools"].get(dsn)
        if pool is None:
            pool = tools_mod._shop["pools"][dsn] = FlakyPool(dead=calls["pools"] == 1)
        return pool

    monkeypatch.setattr(tools_mod, "_shop_pool_raw", make_pool)
    tools_mod._shop["pools"].clear()
    fns = _fns(tools_mod._shop_order_tools(
        {"channel": "whatsapp", "external_user": USAMA_PHONE}, "postgresql://flaky"))
    out = await fns["find_orders"](None)
    assert "The customer is Usama" in out
    # The lookup ran twice, once on the dead pool and once on the fresh one
    # that replaced it; later queries reuse the fresh pool.
    assert calls["queries"] == 2 and calls["pools"] >= 2
    assert not tools_mod._shop["pools"][ "postgresql://flaky"].dead
    tools_mod._shop["pools"].clear()


def test_a_picture_is_offered_once_unless_asked_for():
    # Seen live: every reply came with photo bubbles, including the same
    # washer twice in a row. A product is pictured the first time it comes
    # up, then not again, unless the customer asks to see it.
    shown: set = set()
    washer = {"id": 1, "image": "https://img/w.jpg", "image_fallback": "https://cdn/w.jpg"}
    kettle = {"id": 2, "image": None, "image_fallback": None}
    first = tools_mod._fresh_pictures(shown, [washer, kettle])
    assert first == [("https://img/w.jpg", "https://cdn/w.jpg"), (None, None)]
    assert tools_mod._fresh_pictures(shown, [washer]) == [(None, None)]
    assert tools_mod._fresh_pictures(shown, [washer], force=True) == [
        ("https://img/w.jpg", "https://cdn/w.jpg")
    ]
    assert shown == {1}


async def test_the_greeting_list_goes_out_once_per_conversation(monkeypatch):
    # No database needed: a fake pool with two orders. Seen live: a model
    # re-ran find_orders on a later turn and the whole "Your orders" list
    # landed again under an answer about one order.
    import datetime

    class Pool:
        async def fetchrow(self, sql, *args):
            return {"id": 1, "first_name": "Usama"}

        async def fetch(self, sql, *args):
            now = datetime.datetime.now(datetime.timezone.utc)
            return [
                {"id": 10, "order_ref": "ORD-1", "status": "delivered",
                 "placed_at": now, "delivered_at": now, "total_cents": 1000,
                 "items": "Samsung front load washer", "window_days": 30,
                 "return_open": False, "image": None, "image_fallback": None},
                {"id": 11, "order_ref": "ORD-2", "status": "shipped",
                 "placed_at": now, "delivered_at": None, "total_cents": 2000,
                 "items": "Bosch 24 inch dishwasher", "window_days": 30,
                 "return_open": False, "image": None, "image_fallback": None},
            ]

        def terminate(self):
            pass

    async def make_pool(dsn):
        return Pool()

    monkeypatch.setattr(tools_mod, "_shop_pool_raw", make_pool)
    stash: list = []
    fns = _fns(tools_mod._shop_order_tools(
        {"channel": "whatsapp", "external_user": USAMA_PHONE, "options_stash": stash},
        "postgresql://fake"))
    first = await fns["find_orders"](None)
    assert "2 orders" in first and len(stash) == 1 and stash[0]["title"] == "Your orders"
    stash.clear()
    again = await fns["find_orders"](None)
    assert "already greeted" in again and stash == []
    # A real lookup still works, and only its own group (if any) goes out.
    out = await fns["lookup_order"](None, item="the dishwasher")
    assert "ORD-2" in out and stash == []


async def test_tools_before_identification_say_so(monkeypatch):
    monkeypatch.setenv(tools_mod.SHOP_DSN_ENV, "postgresql://x:y@nowhere:1/z")
    fns = _fns(tools_mod._shop_order_tools({}))
    out = await fns["lookup_order"](None, item="the washer")
    assert "Call find_orders first" in out


# ── against the seeded shop ──────────────────────────────────────────────────


def _dsn() -> str:
    return (os.environ.get(tools_mod.SHOP_DSN_ENV) or "").strip()


async def _reachable(dsn: str) -> bool:
    u = urlparse(dsn)
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: socket.create_connection(
                    (u.hostname or "localhost", u.port or 5432), 2).close()),
            3,
        )
        return True
    except Exception:
        return False


@pytest.fixture
async def shop():
    d = _dsn()
    if not d or not await _reachable(d):
        pytest.skip("no reachable shop database (set ARYA_SHOP_DSN)")
    tools_mod._shop["pools"].clear()
    yield d
    await tools_mod.close_shop_pools()


def _whatsapp(phone: str) -> dict:
    return {"channel": "whatsapp", "external_user": phone}


async def test_a_known_number_is_greeted_by_name_without_asking(shop):
    fns = _fns(tools_mod._shop_order_tools(_whatsapp(USAMA_PHONE)))
    out = await fns["find_orders"](None)
    assert "The customer is Usama" in out
    assert "4 orders" in out
    assert "front load washer" in out and "top load washer" in out
    assert "Greet them by first name" in out


async def test_the_desk_reaches_the_shop_the_connector_named(shop, monkeypatch):
    # Same database, but handed over as the attached connection's string
    # rather than read from the environment: the way a published agent
    # gets it.
    monkeypatch.delenv(tools_mod.SHOP_DSN_ENV, raising=False)
    fns = _fns(tools_mod._order_tools({**_whatsapp(USAMA_PHONE), "shop_dsn": shop}))
    out = await fns["find_orders"](None)
    assert "The customer is Usama" in out
    assert "4 orders" in out
    assert "front load washer" in out and "top load washer" in out
    assert "Greet them by first name" in out


async def test_an_unknown_number_falls_back_to_email(shop):
    fns = _fns(tools_mod._shop_order_tools(_whatsapp(UNKNOWN_PHONE)))
    out = await fns["find_orders"](None)
    assert "not on an account" in out and "email" in out
    # And the email path then works for the same session.
    out = await fns["find_orders"](None, email="amina@example.com")
    assert "The customer is Amina" in out


async def test_the_widget_path_asks_for_and_uses_an_email(shop):
    fns = _fns(tools_mod._shop_order_tools({}))
    # The agent calls with no arguments first, everywhere; off WhatsApp
    # the answer is "ask for the email", never "no account".
    first = await fns["find_orders"](None)
    assert "Nobody is identified yet" in first and "email" in first
    assert "not look like an email" in await fns["find_orders"](None, email="nope")
    assert "different email" in await fns["find_orders"](None, email="ghost@example.com")
    assert "The customer is Bilal" in await fns["find_orders"](
        None, email="BILAL@example.com")


async def test_two_washers_are_still_a_question(shop):
    fns = _fns(tools_mod._shop_order_tools(_whatsapp(USAMA_PHONE)))
    await fns["find_orders"](None)
    out = await fns["lookup_order"](None, item="the washer")
    assert "Two orders could match" in out
    assert "front load" in out and "top load" in out


async def test_a_tie_also_offers_a_tappable_choice(shop):
    # The same tie, with the runtime's option stash present (a live call):
    # one group the bridge turns into a WhatsApp list. Each row's title is
    # short enough for a list row, and tapping it, which sends the title
    # back as text, resolves to exactly that order.
    stash: list = []
    runtime = {**_whatsapp(USAMA_PHONE), "options_stash": stash}
    fns = _fns(tools_mod._shop_order_tools(runtime))
    # The greeting comes with the whole order list, and tells the model
    # not to read it out.
    out = await fns["find_orders"](None)
    assert "do not list them yourself" in out
    assert len(stash) == 1 and stash[0]["title"] == "Your orders"
    assert stash[0]["always"] is True and len(stash[0]["items"]) == 4
    # The overview never carries pictures: four photo bubbles for a list
    # of names is noise, whatever the shop has on file.
    assert all(it["image"] is None for it in stash[0]["items"])
    stash.clear()
    # The greeting list goes out once. Seen live: a model re-ran this tool
    # on a later turn and the whole list landed under a one-order answer.
    again = await fns["find_orders"](None)
    assert "already greeted" in again and stash == []
    await fns["lookup_order"](None, item="the washer")
    assert len(stash) == 1
    group = stash[0]
    assert group["kind"] == "orders" and len(group["items"]) == 2
    for it in group["items"]:
        assert it["pick"] == it["label"] and len(it["label"]) <= 24
        # Price only on the second line: WhatsApp echoes it on a tap.
        assert it["detail"].startswith("$") and "delivered" not in it["detail"]
        # The picture slot is always there; a shop seeded before the image
        # columns existed leaves it empty rather than failing the query.
        assert "image" in it and "image_fallback" in it
    picks = {it["pick"] for it in group["items"]}
    assert picks == {"Samsung front load", "Whirlpool top load"}
    out = await fns["lookup_order"](None, item="Whirlpool top load")
    assert "ORD-1031" in out and "$599.00" in out
    # A return on a tie never writes, and offers the same choice.
    stash.clear()
    out = await fns["start_return"](None, reason="dented", item="the washer")
    assert "Two orders could match" in out and len(stash) == 1
    out = await fns["lookup_order"](None, item="the dishwasher")
    assert "ORD-1047" in out and "shipped" in out and "$849.00" in out


async def test_one_customer_cannot_see_anothers_order(shop):
    # Amina's Dyson is ORD-1018. Usama names it; the desk must not find it.
    fns = _fns(tools_mod._shop_order_tools(_whatsapp(USAMA_PHONE)))
    await fns["find_orders"](None)
    assert "cannot see that order" in await fns["lookup_order"](
        None, order_number="ORD-1018")
    assert "cannot see that order" in await fns["lookup_order"](
        None, item="the Dyson vacuum")
    assert "cannot tell which order" in await fns["start_return"](
        None, reason="wrong item", order_number="ORD-1018")


async def test_returns_are_refused_for_the_right_reasons(shop):
    fns = _fns(tools_mod._shop_order_tools(_whatsapp(USAMA_PHONE)))
    await fns["find_orders"](None)
    # Still being packed.
    out = await fns["start_return"](None, reason="too big", item="the microwave")
    assert "cannot start yet" in out
    # Ambiguous: never write.
    out = await fns["start_return"](None, reason="wrong size", item="the washer")
    assert "Two orders could match" in out

    # Bilal's fridge: delivered 45 days ago on a 30 day window.
    bilal = _fns(tools_mod._shop_order_tools({}))
    await bilal["find_orders"](None, email="bilal@example.com")
    out = await bilal["start_return"](None, reason="noisy", item="the refrigerator")
    assert "too late" in out and "30 days" in out


async def test_a_return_is_written_once_and_then_refused(shop):
    # Writes a row. The seed script is the reset, so the first call may
    # find the row from a previous run; the invariant is the second call.
    fns = _fns(tools_mod._shop_order_tools(_whatsapp(USAMA_PHONE)))
    await fns["find_orders"](None)
    first = await fns["start_return"](
        None, reason="test: door hinge cracked", item="the top load washer",
        note="Photo shows a crack along the door hinge.")
    assert ("Return RET-" in first) or ("already open" in first)
    again = await fns["start_return"](
        None, reason="test: changed my mind", item="the top load washer")
    assert "already open" in again
    status = await fns["return_status"](None)
    assert "top load washer" in status and "requested" in status


async def test_an_existing_return_has_a_status_on_day_one(shop):
    fns = _fns(tools_mod._shop_order_tools({}))
    await fns["find_orders"](None, email="amina@example.com")
    out = await fns["return_status"](None)
    assert "RET-5001" in out and "approved" in out and "Dyson" in out
