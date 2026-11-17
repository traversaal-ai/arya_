"""Connectors (MCP): pure helpers, config validation, and one real MCP
round trip against an in-process FastMCP server over streamable HTTP.

No Postgres, no LiveKit session: build_toolsets() gets a stub pool.
"""

import asyncio
import json
import socket
import types

import pytest

import catalog
import config
import connectors

# ─── pure helpers ───────────────────────────────────────────────────────────


def test_is_write_tool_prefers_catalog_then_annotations_then_name():
    entry = {"write_tools": ["respond_to_event"]}
    assert connectors.is_write_tool("respond_to_event", None, entry)
    ro = types.SimpleNamespace(readOnlyHint=True, destructiveHint=None)
    assert not connectors.is_write_tool("delete_thing", ro, entry)
    rw = types.SimpleNamespace(readOnlyHint=False, destructiveHint=None)
    assert connectors.is_write_tool("lookup", rw, entry)
    assert connectors.is_write_tool("create_event", None, entry)
    assert connectors.is_write_tool("slack_send_message", None, entry)
    assert not connectors.is_write_tool("list_events", None, entry)
    assert not connectors.is_write_tool("search_products", None, entry)


def _ctx(*texts):
    content = [types.SimpleNamespace(text=t) for t in texts]
    return types.SimpleNamespace(result=types.SimpleNamespace(content=content))


def test_spoken_result_compacts_json_and_caps():
    out = connectors.spoken_result(_ctx('{"a": 1,   "b": [1, 2]}'))
    assert out == '{"a":1,"b":[1,2]}'
    assert connectors.spoken_result(_ctx()) == "The tool returned no content."
    long = connectors.spoken_result(_ctx("x" * 5000))
    assert len(long) < 5000 and long.endswith("(truncated; ask for a narrower query)")


# One real Grounding Lite answer, trimmed to three places. Probed with a
# live key on 2026-09-15: a place carries an id, a location and five Maps
# URLs, no name and no description, and ALL the prose is in one `summary`
# at the very end. Three places compact to 3616 chars; the cap is 1500.
MAPS_RESULT = {
    "places": [
        {"place": "places/ChIJAQAAMCxu5kcRx--_4QnbGcI", "id": "ChIJAQAAMCxu5kcRx--_4QnbGcI",
         "location": {"latitude": 48.8634916, "longitude": 2.3274943},
         "googleMapsLinks": {
             "directionsUrl": "https://www.google.com/maps/dir//''/data=!4m7!x!3e0",
             "placeUrl": "https://www.google.com/maps/place//data=!4m2!3m1!1s0x47e6:0xc219",
             "writeAReviewUrl": "https://www.google.com/maps/place//data=!12e1",
             "reviewsUrl": "https://www.google.com/maps/place//data=!9m1!1b1",
             "photosUrl": "https://www.google.com/maps/place//data=!10e5"},
         "attribution": {"title": "Tuileries Garden - Google Maps",
                         "url": "https://www.google.com/maps/place//data=!4m2"}},
        {"place": "places/ChIJ442GNENu5kcRGYUrvgqHw88", "id": "ChIJ442GNENu5kcRGYUrvgqHw88",
         "location": {"latitude": 48.8867046, "longitude": 2.3431043},
         "googleMapsLinks": {"placeUrl": "https://www.google.com/maps/place//data=!1s0x47e6:0xcfc3"},
         "attribution": {"title": "Arc de Triomphe - Google Maps", "url": "https://maps/2"}},
        {"place": "places/ChIJATr1n-Fx5kcRjQb6q6cdQDY", "id": "ChIJATr1n-Fx5kcRjQb6q6cdQDY",
         "location": {"latitude": 48.85, "longitude": 2.35},
         "googleMapsLinks": {"placeUrl": "https://www.google.com/maps/place//data=!1s0x47e6:0x8d06"},
         "attribution": {"title": "Sainte-Chapelle - Google Maps", "url": "https://maps/3"}},
    ],
    "summary": (
        "**Tuileries Garden** [0] (4.6★, 119,446) is an expansive, 17th-century formal"
        " garden featuring walking trails, fountains, statues, and 18 bronzes by Maillol.\n\n"
        "**Arc de Triomphe** [1] (4.7★, 298811) is an iconic triumphal arch built to"
        " commemorate Napoleon's victories that features an observation deck with city views.\n\n"
        "**Sainte-Chapelle** [2] (4.6★, 50250) is an ornate, 13th-century Gothic chapel"
        " housing sacred relics and notable stained-glass windows depicting biblical scenes."
    ),
}


def test_the_maps_result_keeps_the_prose_and_drops_the_url_noise():
    """Seen live: the model got two places of identifiers, a third cut
    mid-URL, and never the summary, so every note on the written page was
    its own filler. The prose is the whole point of the lookup."""
    shaped = connectors.shape_maps_places(MAPS_RESULT)
    assert [p["name"] for p in shaped["places"]] == [
        "Tuileries Garden", "Arc de Triomphe", "Sainte-Chapelle"]
    first = shaped["places"][0]
    assert first["about"].startswith("An expansive, 17th-century formal garden")
    assert first["about"].endswith(".")
    assert first["rating"] == 4.6 and first["reviews"] == 119446
    assert first["maps_url"] == "https://www.google.com/maps/place//data=!4m2!3m1!1s0x47e6:0xc219"
    # One link, not five, and no coordinates the model cannot use.
    assert "directionsUrl" not in json.dumps(shaped)
    assert "latitude" not in json.dumps(shaped)
    # All three survive the cap now, which is what the call needed.
    compact = json.dumps(shaped, separators=(",", ":"), ensure_ascii=False)
    assert len(compact) < connectors.RESULT_MAX_CHARS
    assert len(json.dumps(MAPS_RESULT, separators=(",", ":"))) > connectors.RESULT_MAX_CHARS


def test_an_unexpected_maps_answer_falls_through_untouched():
    """A shaper that cannot read a payload must hand it back, not empty it."""
    assert connectors.shape_maps_places({}) is None
    assert connectors.shape_maps_places({"places": []}) is None
    assert connectors.shape_maps_places({"error": "quota"}) is None
    # No summary at all: names still come from the attribution title.
    no_prose = connectors.shape_maps_places({"places": MAPS_RESULT["places"]})
    assert [p["name"] for p in no_prose["places"]] == [
        "Tuileries Garden", "Arc de Triomphe", "Sainte-Chapelle"]
    assert "about" not in no_prose["places"][0]


def test_only_the_named_connector_and_tool_are_reshaped():
    ctx = types.SimpleNamespace(
        tool_name="search_places",
        result=types.SimpleNamespace(
            structuredContent=None,
            content=[types.SimpleNamespace(text=json.dumps(MAPS_RESULT))]),
    )
    shaped = connectors.spoken_result(ctx, connector_type="google_maps")
    assert "Tuileries Garden" in shaped and "Sainte-Chapelle" in shaped
    assert "directionsUrl" not in shaped
    # The same payload from anything else, or from another tool, is left
    # alone: a shaper is a fact about one vendor's one endpoint.
    assert "directionsUrl" in connectors.spoken_result(ctx, connector_type="generic_mcp")
    other = types.SimpleNamespace(tool_name="compute_routes", result=ctx.result)
    assert "directionsUrl" in connectors.spoken_result(other, connector_type="google_maps")


def test_a_shipped_tool_reaches_an_agent_that_already_has_the_connector():
    """plan_trip sat in the running image through two live calls while the
    agent planned days one at a time: the connection predated the tool, an
    agent's ticks are intersected against the connection's list, and a tool
    nobody has heard of cannot pass. The probe that finds it switches it
    on. Writes stay off: shipping a booking tool must not arm it."""
    import api

    ours = config.connector_type("duffel")       # Arya hosts this one
    theirs = config.connector_type("composio")   # a vendor hosts this one
    before = [{"name": "search_flights", "write": False},
              {"name": "plan_activity", "write": False}]
    after = [*before, {"name": "plan_trip", "write": False},
             {"name": "cancel_flight", "write": True}]
    assert api._new_read_tools(ours, before, after) == {"plan_trip"}
    assert api._new_read_tools(ours, after, after) == set()
    assert api._new_read_tools(ours, None, after) == {"search_flights", "plan_activity",
                                                     "plan_trip"}
    assert api._new_read_tools(ours, before, None) == set()

    # A vendor's own new tools are never switched on, however harmless they
    # look. Composio grew SEARCH_SKILLS and USE_SKILL between two probes,
    # both reads, both nothing to do with the travel agent holding the
    # connection, and both were silently armed. Arya decides what ships in
    # its own sidecar; it does not decide what Composio ships.
    grew = [{"name": "COMPOSIO_SEARCH_TOOLS", "write": False},
            {"name": "COMPOSIO_SEARCH_SKILLS", "write": False},
            {"name": "COMPOSIO_USE_SKILL", "write": False}]
    assert api._new_read_tools(theirs, grew[:1], grew) == set()
    assert api._new_read_tools({}, grew[:1], grew) == set()

    cfg = {"connectors": [
        {"connection_id": "conn-a", "allowed_tools": ["search_flights"],
         "public_tools": ["search_flights"]},
        {"connection_id": "conn-b", "allowed_tools": ["lookup_weather"]},
    ]}
    saved = []
    original = (api.config.list_saved_agents, api.config.load_saved_agent,
                api.config.save_agent, api.config.load_active)
    api.config.list_saved_agents = lambda: [{"id": "a1"}]
    api.config.load_saved_agent = lambda _id: cfg
    api.config.save_agent = lambda c: saved.append(c)
    api.config.load_active = lambda: (_ for _ in ()).throw(RuntimeError("no active"))
    try:
        api._offer_new_tools_to_agents("conn-a", {"plan_trip"})
    finally:
        (api.config.list_saved_agents, api.config.load_saved_agent,
         api.config.save_agent, api.config.load_active) = original

    assert len(saved) == 1
    assert cfg["connectors"][0]["allowed_tools"] == ["search_flights", "plan_trip"]
    # What a website visitor may trigger stays a deliberate choice.
    assert cfg["connectors"][0]["public_tools"] == ["search_flights"]
    # A different connection is untouched.
    assert cfg["connectors"][1]["allowed_tools"] == ["lookup_weather"]


def test_a_connector_that_did_not_come_up_is_named_so_the_agent_can_refuse():
    """A dropped connector used to be pure silence: the sample desk it
    replaced stays off whatever its health, so the model was missing a tool
    nobody had mentioned. Seen live, a timed-out Duffel left it answering a
    flight search from web_search with prices off a web page.
    """
    cfg = {"connectors": [
        {"connection_id": "conn-live", "type": "google_maps"},
        {"connection_id": "conn-dead", "type": "duffel"},
        {"connection_id": "conn-odd", "type": "nonsense"},
    ]}
    live = [{"entry": config.connector_type("google_maps"),
             "row": {"id": "conn-live", "name": "Maps", "config": {}},
             "attachment": {"purpose": "places"}, "tool_names": ["search_places"]}]
    dead = connectors.dead_jobs(cfg, live)
    assert dead == ["searching or booking flights", "one of this agent's systems"]

    block = connectors.prompt_block(live, dead=dead)
    assert "NOT available on this call: searching or booking flights" in block
    assert "Never use a web search" in block
    # Nothing attached and nothing dead: no section at all.
    assert connectors.dead_jobs({}, []) == []
    assert connectors.prompt_block([], dead=[]) == ""
    # Dead but no live connector still has to reach the model.
    assert "NOT available" in connectors.prompt_block([], dead=["searching or booking flights"])


def test_prompt_block_only_for_live_connectors():
    entry = config.connector_type("google_calendar")
    live = [{
        "entry": entry,
        "row": {"name": "Work calendar", "config": {"account_email": "ops@acme.com"}},
        "attachment": {"purpose": "booking"},
    }]
    import datetime as dt

    fixed = dt.datetime(2026, 9, 8, 16, 7, tzinfo=dt.timezone.utc)
    block = connectors.prompt_block(live, now=fixed)
    assert "# Connected systems" in block
    assert "Work calendar" in block and "ops@acme.com" in block
    assert "Tuesday, September 08, 2026 at 16:07 UTC" in block
    assert connectors.prompt_block([]) == ""


def test_auth_headers_and_server_url():
    generic = config.connector_type("generic_mcp")
    row = {"url": "http://host:3001/mcp", "secret": {"token": "abc"}}
    assert connectors.auth_headers(generic, row["secret"]) == {"Authorization": "Bearer abc"}
    assert connectors.auth_headers(generic, None) == {}
    assert connectors.server_url(generic, row) == "http://host:3001/mcp"
    gcal = config.connector_type("google_calendar")
    assert connectors.server_url(gcal, {"url": "ignored"}).startswith("https://calendarmcp")
    assert connectors.auth_headers(gcal, {"access_token": "t"}) == {"Authorization": "Bearer t"}
    # A vendor with its own header name gets the raw key under that name.
    composio = config.connector_type("composio")
    # Composio names it x-consumer-api-key, verified against a live
    # server. Under the wrong name the key simply never arrives, and
    # the probe fails looking like a bad credential.
    assert connectors.auth_headers(composio, {"token": "ck_1"}) == {
        "x-consumer-api-key": "ck_1"
    }
    assert connectors.auth_headers({"auth": "bearer", "header": "Authorization"},
                                   {"token": "b"}) == {"Authorization": "Bearer b"}


def test_sidecar_types_resolve_to_the_arya_mcp_service(monkeypatch):
    monkeypatch.setenv("ARYA_MCP_URL", "http://arya-mcp:8020/")
    postgres = config.connector_type("postgres")
    assert connectors.is_sidecar(postgres)
    # Trailing slash on the env var must not double up on the path.
    assert connectors.server_url(postgres, {}) == "http://arya-mcp:8020/postgres/mcp"
    # The DSN travels in a header, so the sidecar stores no credentials.
    assert connectors.auth_headers(postgres, {"dsn": "postgresql://u:p@h/db"}) == {
        "X-Arya-Dsn": "postgresql://u:p@h/db"
    }
    assert connectors.auth_headers(postgres, None) == {}
    assert not connectors.is_sidecar(config.connector_type("generic_mcp"))


def test_secret_shape_and_masking_follow_the_auth_kind():
    postgres = config.connector_type("postgres")
    generic = config.connector_type("generic_mcp")
    assert connectors.secret_for(postgres, " postgresql://x ") == {
        "dsn": "postgresql://x"
    }
    assert connectors.secret_for(generic, "tok") == {"token": "tok"}
    assert connectors.secret_for(generic, "   ") is None
    # Masking reads whichever field this kind uses.
    assert connectors.secret_value(postgres, {"dsn": "postgresql://x"}) == "postgresql://x"
    assert connectors.secret_value(generic, {"token": "tok"}) == "tok"
    assert connectors.secret_value(config.connector_type("google_calendar"),
                                   {"access_token": "at"}) == "at"
    assert connectors.secret_value(generic, None) == ""


def test_write_tools_are_detected_whatever_case_the_vendor_uses():
    """Composio names tools in upper snake case, and while the heuristic was
    case-sensitive every one of its writes passed as a read: pre-ticked on
    attach, no writes badge, no duplicate-call guard. Found before the first
    Composio call, which is the only reason it is cheap.
    """
    composio = config.connector_type("composio")
    assert composio is not None and not composio.get("write_tools")

    for name in ("GOOGLECALENDAR_CREATE_EVENT", "GOOGLECALENDAR_DELETE_EVENT",
                 "GMAIL_SEND_EMAIL", "SHOPIFY_UPDATE_ORDER",
                 "HUBSPOT_CREATE_CONTACT", "TWILIO_SEND_SMS"):
        assert connectors.is_write_tool(name, None, composio), name

    for name in ("GOOGLECALENDAR_FIND_EVENT", "GOOGLECALENDAR_LIST_CALENDARS",
                 "GMAIL_FETCH_EMAILS", "SHOPIFY_GET_ORDER"):
        assert not connectors.is_write_tool(name, None, composio), name

    # Lower case still works, and so does the catalog list and the MCP
    # annotation, which both outrank the name.
    assert connectors.is_write_tool("create_event", None, composio)
    assert not connectors.is_write_tool("list_events", None, composio)
    calcom = config.connector_type("calcom")
    assert connectors.is_write_tool("book_appointment", None, calcom)
    assert not connectors.is_write_tool("find_slots", None, calcom)

    class Ann:
        # camelCase because these are the MCP protocol field names.
        readOnlyHint = True       # noqa: N815
        destructiveHint = None    # noqa: N815

    assert not connectors.is_write_tool("DELETE_EVERYTHING", Ann(), composio), (
        "an explicit readOnlyHint outranks the name"
    )


async def test_the_sample_order_desk_works_the_way_a_caller_does(monkeypatch):
    """Behaviours the live Peachtree calls taught us, pinned on the DEMO
    desk so a fresh agent with no connector behaves the same way a
    connected one does.
    """
    import tools as tools_mod

    # The demo desk, whatever the developer's shell has in ARYA_SHOP_DSN.
    monkeypatch.delenv(tools_mod.SHOP_DSN_ENV, raising=False)
    built = tools_mod.build_tools({"tools": {"orders": True}}, {})
    fns = {}
    for tool in built:
        name = getattr(tool, "name", None) or getattr(
            getattr(tool, "__func__", tool), "__name__", "")
        inner = getattr(tool, "_fnc", None) or getattr(tool, "__wrapped__", tool)
        fns[name] = inner
    assert set(fns) == {"find_orders", "lookup_order", "start_return"}

    # An email is what a caller actually has, and it is what the template's
    # prompt asks for. A desk that only took an order number left the agent
    # asking for something it could not use.
    listed = await fns["find_orders"](None, email="someone@example.com")
    assert "4 orders" in listed
    assert "never read the whole list aloud" in listed
    assert "not look like an email" in await fns["find_orders"](None, email="nope")
    # The template now says "call find_orders first", so an empty call must
    # come back as an instruction to ask, not as a schema error.
    assert "Ask for the email" in await fns["find_orders"](None)

    # A product word, which is how people refer to their own orders.
    assert "dishwasher" in await fns["lookup_order"](None, item="the dishwasher")
    # An order number still works when one is volunteered.
    assert "microwave" in await fns["lookup_order"](None, order_number="ORD-1023")

    # TWO WASHERS: the trap that produced a confidently wrong answer live,
    # where "the top load washer" was answered with the front load one.
    ambiguous = await fns["lookup_order"](None, item="the washer")
    assert "Two orders could match" in ambiguous
    assert "front load" in ambiguous and "top load" in ambiguous
    assert "Ask which one" in ambiguous

    # Writes: delivered only, once, and never speak a reference.
    started = await fns["start_return"](
        None, item="dishwasher", reason="arrived damaged")
    assert "Return started" in started
    assert "never read a reference aloud" in started
    again = await fns["start_return"](
        None, item="dishwasher", reason="changed my mind")
    assert "already on file" in again and "cannot be returned twice" in again
    undelivered = await fns["start_return"](
        None, item="microwave", reason="too big")
    assert "can't start a return yet" in undelivered
    # And an ambiguous item must not be written to at all.
    assert "Two orders could match" in await fns["start_return"](
        None, item="washer", reason="wrong size")


def test_templates_never_name_their_own_tools_in_the_prompt():
    """A template must not name a tool. The sample desks are switched off the
    moment a connector is attached (connectors.replaced_toolsets), and the
    connector's own prompt_block names ITS tools, so a template that names
    either set sends the agent hunting for something that is not there.
    Cost a live call to find: the retail prompt said "call lookup_order"
    while Postgres was attached and lookup_order no longer existed.
    """
    import json
    import pathlib as _pathlib

    # Sample desk tool names, plus the connector tool names they get replaced
    # by. Neither belongs in a template.
    forbidden = (
        "lookup_order", "start_return", "find_orders", "check_openings",
        "list_services", "book_appointment", "cancel_appointment",
        "reschedule_appointment", "find_slots", "business_hours",
        "find_my_bookings", "order_lookup", "run_query", "list_tables",
        "describe_table",
        # The travel sample desk and the Duffel and Google Maps tools.
        "search_flights", "select_flight", "search_hotels", "select_hotel",
        "search_rooms", "select_room", "get_recommendations", "recap_itinerary",
        "book_flight", "flight_details", "hotel_rates", "book_hotel",
        "cancel_flight", "cancel_hotel", "find_bookings", "plan_activity",
        "trip_summary", "search_places", "compute_routes",
    )
    # Templates not yet rewritten. They are done one at a time with a live
    # call each, because eight prompts edited blind is eight ways to be
    # wrong at once. Remove a name from this set when its template is fixed:
    # the test tightens on its own and never lets a fixed one regress.
    todo = {"admissions.json", "finance.json", "fitness.json",
            "home-visits.json", "intake.json"}

    for path in sorted(_pathlib.Path("agents/templates").glob("*.json")):
        ins = json.loads(path.read_text()).get("instructions") or ""
        named = [n for n in forbidden if n in ins]
        if path.name in todo:
            assert named, (
                f"{path.name} no longer names a tool, so take it out of the"
                " todo set in this test"
            )
            continue
        assert not named, f"{path.name} names the tool(s) {named}"


def test_templates_keep_their_substitution_tokens_and_stay_preamble_only():
    """Two ways a template silently breaks: a hardcoded business name, which
    survives into every agent built from it, and an inlined copy of the
    shared voice rules, which stops load_template appending the real ones.
    """
    import json
    import pathlib as _pathlib

    for path in sorted(_pathlib.Path("agents/templates").glob("*.json")):
        cfg = json.loads(path.read_text())
        ins = cfg.get("instructions") or ""
        if not ins:
            # _base.json is a shared config fragment, not a persona.
            continue
        assert "# Output rules" not in ins, (
            f"{path.name} inlines the shared voice rules, so load_template"
            " will not append them"
        )
        # The token config.render_vars() fills on EVERY call, so a rename
        # follows through. It was "<Business Name>" until the prompt-variable
        # change; this test kept asserting the old spelling and went red
        # across all twelve templates at once.
        assert "{business_name}" in ins, f"{path.name} lost its business token"
        assert "\u2014" not in ins, f"{path.name} contains an em dash"


def test_the_retail_prompt_and_the_sample_order_desk_agree():
    """The template asks for an email, so the desk it ships with must accept
    one. This is the mismatch that made a fresh retail agent ask for an
    email and then have no way to use it."""
    import json
    import pathlib as _pathlib

    import tools as tools_mod

    cfg = json.loads(_pathlib.Path("agents/templates/retail.json").read_text())
    ins = cfg["instructions"]
    assert "email address the order was placed with" in ins

    names = connectors.native_tool_names(
        tools_mod.build_tools({"tools": {"orders": True}}, {})
    )
    assert "find_orders" in names, "the desk must be reachable by email"
    # And attaching a connector must still take the whole desk away.
    replaced = connectors.native_tool_names(tools_mod.build_tools(
        {"tools": {"orders": True}}, {}, skip=frozenset({"orders"})
    ))
    assert not (set(names) & set(replaced))


def test_templates_only_want_connector_types_that_exist():
    """A template's connectors_wanted list drives the "Recommended" nudge on
    the connector card. A typo there would switch the nudge off silently,
    with nothing failing, so the ids are checked against the catalog.
    """
    import json
    import pathlib as _pathlib

    known = {c["id"] for c in catalog.get("connectors")}
    templates = sorted(_pathlib.Path("agents/templates").glob("*.json"))
    assert templates, "no templates found, wrong working directory?"
    wanted_somewhere = False
    for path in templates:
        cfg = json.loads(path.read_text())
        for type_id in cfg.get("connectors_wanted") or []:
            wanted_somewhere = True
            assert type_id in known, f"{path.name} wants unknown '{type_id}'"
    assert wanted_somewhere, "the appointments template should want calcom"


def test_travel_split_keeps_old_agents_whole_and_lets_each_desk_go_alone():
    """The travel toggle used to build one desk: flights, hotels, things to
    do and the date helper together. A flights connector must be able to
    replace the flight and hotel desk while the things-to-do planner stays,
    and a places connector the reverse, so they are two toggles now. Agents
    saved before the split have no `places` key and must lose nothing.
    """
    import catalog
    import tools as tools_mod

    def names(cfg, skip=frozenset()):
        return connectors.native_tool_names(tools_mod.build_tools(cfg, {}, skip=skip))

    old = {"tools": {"travel": True}}
    assert {"search_flights", "select_room", "recap_itinerary",
            "get_recommendations", "get_current_date"} <= names(old)

    # Duffel attached: the flight desk is gone; hotel rates, the planner
    # and the date helper are not (Duffel Stays is off on new accounts).
    kept = names(old, frozenset({"travel"}))
    assert "search_flights" not in kept and "select_flight" not in kept
    assert {"search_hotels", "select_room", "get_recommendations", "get_current_date"} <= kept

    # Everything replaced: only the date helper is left, because every
    # connector tool still takes an ISO date.
    assert names(old, frozenset({"travel", "hotels", "places"})) == {"get_current_date"}

    # Explicit offs are respected, and each desk stands alone.
    assert "get_recommendations" not in names({"tools": {"travel": True, "places": False}})
    assert "search_hotels" not in names({"tools": {"travel": True, "hotels": False}})
    alone = names({"tools": {"places": True}})
    assert "get_recommendations" in alone and "search_flights" not in alone
    hotels_alone = names({"tools": {"hotels": True}})
    assert {"search_hotels", "recap_itinerary"} <= hotels_alone
    assert "search_flights" not in hotels_alone

    # Loading fills the missing keys so Preview and the worker agree.
    cfg = config.normalize_tools({"tools": {"travel": True, "weather": False}})
    assert cfg["tools"]["places"] is True and cfg["tools"]["hotels"] is True
    assert config.normalize_tools({"tools": {"weather": True}})["tools"] == {"weather": True}
    for key in ("places", "hotels"):
        assert key in catalog.SEEDS["tool_catalog"], key
    tpl = config.load_template("travel-concierge")
    assert tpl["tools"]["places"] is True and tpl["tools"]["hotels"] is True


def test_the_stripe_connector_is_a_catalog_entry_and_nothing_else():
    """Stripe hosts its own MCP server, so unlike Duffel and Cal.com there
    is no module to maintain. What matters is that the entry points at the
    right place, sends the credential the way Stripe wants it, and carries
    the two rules this project has already been burned by."""
    entry = config.connector_type("stripe")
    assert entry["url"] == "https://mcp.stripe.com"
    assert entry["url_source"] == "fixed" and entry["transport"] == "streamable_http"
    assert entry["auth"] == "api_key"
    # No custom header: Stripe wants Authorization: Bearer, which is the
    # default. Composio's x-consumer-api-key is the exception, not the rule.
    assert "header" not in entry
    assert connectors.auth_headers(entry, {"token": "sk_agent_x"}) == {
        "Authorization": "Bearer sk_agent_x"}
    # One tick stands between reading payments and moving money.
    assert entry["write_tools"] == ["stripe_api_write"]
    # Nothing to stand down: Arya ships no sample payments desk.
    assert entry["overlaps"] == []
    assert "Agent" in entry["credential_label"]

    block = connectors.prompt_block([{
        "entry": entry, "row": {"name": "Peachtree payments", "config": {}},
        "attachment": {"purpose": "refunds"}, "tool_names": ["stripe_api_read"],
    }])
    assert "Peachtree payments" in block and "refunds" in block
    assert "{" not in block.split("# Connected systems")[1].split("\n\n")[1]
    # Its tools are a generic router, the shape that had a model inventing a
    # calendar slug on two live calls. Search first, never guess.
    assert "never guess an API method" in block
    # Stripe answers a refund with an approval link for a human to click. An
    # agent reading that as success would tell a caller their money is on
    # the way when nothing has happened.
    assert "has not happened yet" in block
    assert "never say a refund has gone through" in block
    # A voice agent has no business hearing a card number.
    assert "Never ask for a card number" in block

    assert "connector:stripe" in catalog.SEEDS["tool_catalog"]


def test_spoken_result_lifts_structured_results_for_the_worker(monkeypatch):
    """Arya's own modules (Duffel) return a dict: the model reads `say` and
    the compact machine block, the worker takes the option cards and the
    written itinerary. Neither reaches the model, so a page-sized payload
    costs no tokens and cannot be read aloud.
    """
    calls = []
    monkeypatch.setattr(connectors.itinerary_sync, "sync",
                        lambda payload, **kw: calls.append((payload, kw)))
    sc = {"say": "Option 1: Duffel Airways.", "machine": {"trip_id": "abc"},
          "cards": [{"kind": "flights", "title": "Flights", "items": [{"n": 1}]}],
          "itinerary": {"stage": "flight", "title": "Trip to Lisbon",
                        "payload": {"brand": {"name": "", "icon": "\u2708\ufe0f", "mode": "test"}}}}
    ctx = types.SimpleNamespace(result=types.SimpleNamespace(
        content=[types.SimpleNamespace(text=json.dumps(sc))], structuredContent=sc))
    stash = []
    runtime = {"conversation_id": "c1", "options_stash": stash,
               "brand": {"name": "Avaera", "icon": ""}}
    text = connectors.spoken_result(ctx, runtime=runtime)
    assert text.startswith("Option 1: Duffel Airways.")
    assert '{"trip_id":"abc"}' in text and "cards" not in text and "payload" not in text
    assert stash == sc["cards"]
    assert calls and calls[0][1]["stage"] == "flight" and calls[0][1]["title"] == "Trip to Lisbon"
    # The worker fills the brand the module cannot know; an empty icon does
    # not overwrite the module's.
    assert calls[0][0]["brand"] == {"name": "Avaera", "icon": "\u2708\ufe0f", "mode": "test"}
    # No runtime (console mode, tests): still speaks, writes nothing.
    calls.clear()
    assert connectors.spoken_result(ctx).startswith("Option 1")
    assert not calls
    # A plain result is untouched by all this.
    plain = types.SimpleNamespace(result=types.SimpleNamespace(
        content=[types.SimpleNamespace(text=" hello ")], structuredContent=None))
    assert connectors.spoken_result(plain, runtime=runtime) == "hello"


def test_duffel_and_google_maps_connector_types():
    duffel = config.connector_type("duffel")
    assert duffel["url_source"] == "sidecar" and duffel["path"] == "/duffel/mcp"
    assert duffel["check_path"] == "/duffel/check" and duffel["overlaps"] == ["travel"]
    assert connectors.is_sidecar(duffel)
    for name in ("book_flight", "book_hotel", "cancel_flight", "cancel_hotel"):
        assert connectors.is_write_tool(name, None, duffel), name
    for name in ("search_flights", "search_hotels", "hotel_rates", "find_bookings",
                 "trip_summary", "flight_details"):
        assert not connectors.is_write_tool(name, None, duffel), name
    assert connectors.auth_headers(duffel, {"token": "duffel_test_x"}) == {
        "Authorization": "Bearer duffel_test_x"
    }
    maps = config.connector_type("google_maps")
    assert connectors.server_url(maps, {}) == "https://mapstools.googleapis.com/mcp"
    assert connectors.auth_headers(maps, {"token": "AIza"}) == {"X-Goog-Api-Key": "AIza"}
    assert set(maps["overlaps"]) == {"places", "weather"} and not maps["write_tools"]
    assert connectors.replaced_toolsets({"connectors": [
        {"connection_id": "a", "type": "duffel"},
        {"connection_id": "b", "type": "google_maps"},
    ]}) == frozenset({"travel", "places", "weather"})
    # The hotel desk goes only once Duffel's hotel tools are ticked: Stays
    # is off on new accounts and the check hides those tools until then.
    assert connectors.replaced_toolsets({"connectors": [
        {"connection_id": "a", "type": "duffel", "allowed_tools": ["search_flights"]},
    ]}) == frozenset({"travel"})
    assert connectors.replaced_toolsets({"connectors": [
        {"connection_id": "a", "type": "duffel",
         "allowed_tools": ["search_flights", "book_hotel"]},
    ]}) == frozenset({"travel", "hotels"})
    # A stale tick does not count once the connection stops listing the
    # tool: an Options page left open across a Test saved its old list
    # back, and four dead hotel ticks silenced the SerpAPI desk for a call.
    rows = {"a": {"id": "a", "tools": [{"name": "search_flights"}, {"name": "plan_days"}]}}
    assert connectors.replaced_toolsets({"connectors": [
        {"connection_id": "a", "type": "duffel",
         "allowed_tools": ["search_flights", "book_hotel"]},
    ]}, rows=rows) == frozenset({"travel"})
    # A row that never listed anything leaves the ticks at face value.
    assert connectors.replaced_toolsets({"connectors": [
        {"connection_id": "a", "type": "duffel", "allowed_tools": ["book_hotel"]},
    ]}, rows={"a": {"id": "a", "tools": []}}) == frozenset({"travel", "hotels"})
    assert maps["setup_timeout_s"] == 10
    # A sandbox token is the OWNER's business, not the caller's. The owner
    # is warned on the connection card when they paste it; a caller told
    # "this is a test booking" mid-demo has been handed something they
    # cannot act on. Seen live twice after the say-strings were cleaned,
    # because the prompt block was still asking for it.
    live = [{"entry": duffel, "row": {"name": "Duffel", "config": {"mode": "test"}},
             "attachment": {"purpose": "trips"}, "toolset": None, "tool_names": ["search_flights"]}]
    block = connectors.prompt_block(live)
    assert "{mode}" not in block
    assert "test mode" not in block and "test booking" not in block
    assert "search and book real flights" in block
    for key in ("connector:duffel", "connector:google_maps", "places"):
        assert key in catalog.SEEDS["tool_catalog"], key
    tpl = config.load_template("travel-concierge")
    assert tpl["connectors_wanted"] == ["duffel", "google_maps", "composio"]
    assert "# Output rules" in tpl["instructions"]  # voice_rules appended, preamble only


async def test_option_cards_say_what_a_tap_should_send():
    """A tap used to send "Option n" whatever the card was. After a rates
    card that is ambiguous (hotel 1 or rate 1?) and the model took the
    hotel; a lone rate is a yes-or-no question. Every item now carries the
    text a tap should send."""
    import travel_tools

    stash: list[dict] = []
    fns = {}
    for tool in travel_tools.build_travel_tools({"tools": {}}, {}, stash, None,
                                                parts=("hotels",)):
        name = getattr(tool, "name", None) or getattr(
            getattr(tool, "__func__", tool), "__name__", "")
        fns[name] = getattr(tool, "_fnc", None) or getattr(tool, "__wrapped__", tool)
    await fns["search_hotels"](None, check_in_date="2027-03-10",
                               check_out_date="2027-03-12", city="Lisbon")
    hotels = [g for g in stash if g["kind"] == "hotels"]
    assert hotels and all(it["pick"] == f"Option {it['n']}" for it in hotels[0]["items"])
    await fns["select_hotel"](None, option_number=1)
    rates = [g for g in stash if g["kind"] == "rates"]
    if rates:
        items = rates[-1]["items"]
        expected = "Yes" if len(items) == 1 else None
        for it in items:
            assert it["pick"] == (expected or f"Rate {it['n']}")


def test_replaced_toolsets_comes_from_the_connector_type():
    """Attaching a connector switches off the sample desk it covers. Not a
    setting: the two collide on tool names and the sample desk wins, so
    "both on" meant booking a real slot into the fake desk."""
    cfg = {"connectors": [
        {"connection_id": "a", "type": "calcom"},     # overlaps appointments, booking
        {"connection_id": "b", "type": "postgres"},   # overlaps orders
        {"connection_id": "c", "type": "generic_mcp"},  # overlaps nothing
        {"connection_id": "d", "type": "nonsense"},   # unknown type, tolerated
        "not-a-dict",                                 # tolerated
    ]}
    assert connectors.replaced_toolsets(cfg) == frozenset(
        {"appointments", "booking", "orders"}
    )
    assert connectors.replaced_toolsets({}) == frozenset()


def test_replaced_toolsets_still_honours_older_saved_agents():
    """Agents saved before the type was recorded carry the list the UI
    pre-ticked from these same `overlaps`, so they must behave identically
    rather than silently losing their replacement."""
    cfg = {"connectors": [
        {"connection_id": "a", "replaces": ["appointments", "booking"]},
        {"connection_id": "b", "replaces": ["orders"]},
        {"connection_id": "c"},                      # nothing marked
        {"connection_id": "d", "replaces": None},    # tolerated
    ]}
    assert connectors.replaced_toolsets(cfg) == frozenset(
        {"appointments", "booking", "orders"}
    )
    # A type present wins over a stale list, so re-saving an agent corrects
    # a hand-edited or imported config rather than trusting it.
    assert connectors.replaced_toolsets({"connectors": [
        {"connection_id": "a", "type": "generic_mcp",
         "replaces": ["appointments"]},
    ]}) == frozenset()


def test_a_shop_shaped_database_serves_the_order_desk_instead_of_replacing_it():
    # The postgres check marks a connection `shop_desk` when the database
    # has the desk's four tables. Then the desk stays on and runs over it.
    # Any other database, or no rows at all (the API's sync path), replaces
    # the desk exactly as before.
    assert config.connector_type("postgres")["keeps_when"] == {"orders": "shop_desk"}
    cfg = {"connectors": [{"connection_id": "db", "type": "postgres"}]}
    shop = {"db": {"id": "db", "name": "Shop", "type": "postgres",
                   "config": {"shop_desk": True},
                   "secret": {"dsn": "postgresql://a:b@h/s"}}}
    crm = {"db": {"id": "db", "name": "CRM", "type": "postgres", "config": {}}}
    assert connectors.replaced_toolsets(cfg, rows=shop) == frozenset()
    assert connectors.replaced_toolsets(cfg, rows=crm) == frozenset({"orders"})
    assert connectors.replaced_toolsets(cfg) == frozenset({"orders"})
    served = connectors.served_toolsets(cfg, shop)
    assert set(served) == {"orders"}
    assert connectors.secret_value(
        served["orders"]["entry"], served["orders"]["row"]["secret"]
    ) == "postgresql://a:b@h/s"
    assert connectors.served_toolsets(cfg, crm) == {}
    assert connectors.served_toolsets(cfg, None) == {}


def test_build_tools_skips_replaced_sample_toolsets():
    import tools as tools_mod

    cfg = {"tools": {"appointments": True, "orders": True, "weather": True}}
    all_names = connectors.native_tool_names(tools_mod.build_tools(cfg, {}))
    assert "book_appointment" in all_names or len(all_names) > 3

    # With the sample desks replaced, only weather is left.
    kept = connectors.native_tool_names(tools_mod.build_tools(
        cfg, {}, skip=frozenset({"appointments", "orders"})
    ))
    assert "lookup_weather" in kept
    assert len(kept) < len(all_names)
    # The toggle itself is untouched, so unticking the connector brings the
    # demo desk back.
    assert cfg["tools"]["appointments"] is True


def test_a_sample_desk_next_to_a_connector_is_no_longer_a_problem():
    """There is nothing left to warn about: the sample desk is switched off
    by the attachment itself, so the state the old clash message described
    cannot occur. The toggle stays true in the config, which is what makes
    detaching the connector bring the demo desk back."""
    rows = [{"id": "conn-1", "type": "calcom", "name": "Bookings",
             "status": "connected"}]
    attached = {"connection_id": "conn-1", "type": "calcom",
                "allowed_tools": ["find_slots"]}
    cfg = _cfg(tools={"appointments": True}, connectors=[attached])
    assert config.validate(cfg, KEYS, connections=rows) == []
    assert connectors.replaced_toolsets(cfg) >= frozenset({"appointments"})
    assert cfg["tools"]["appointments"] is True, "the toggle is untouched"


def test_an_attached_connector_with_no_tools_still_gates_publish():
    """The one state that must stay loud: attached means the connector owns
    that job, so attached with nothing ticked is an agent that cannot book
    at all rather than one that quietly uses the demo desk."""
    rows = [{"id": "conn-1", "type": "calcom", "name": "Bookings",
             "status": "connected"}]
    problems = config.validate(
        _cfg(tools={"appointments": True},
             connectors=[{"connection_id": "conn-1", "type": "calcom",
                          "allowed_tools": []}]),
        KEYS, connections=rows,
    )
    assert any("no tools selected" in p for p in problems)


def test_a_vendor_sandbox_is_a_warning_on_the_card_not_a_publish_gate():
    """Test mode must NOT be a validate() problem. Problems mean the agent
    cannot run, so they also stop Preview's Test button (it saves, sees a
    problem and returns): gating on test mode blocked the owner from testing
    the very thing test mode exists for, and blocked publishing a demo embed
    on purpose. The warning lives on the connection card instead, where the
    module's check route already puts it.
    """
    rows = [connectors.row_to_dict(
        {"id": "conn-1", "type": "duffel", "name": "Avaera",
         "status": "connected", "config": '{"mode": "test"}'})]
    cfg = _cfg(tools={"travel": True},
               connectors=[{"connection_id": "conn-1", "type": "duffel",
                            "allowed_tools": ["search_flights", "book_flight"]}])
    assert config.validate(cfg, KEYS, connections=rows) == []
    # A row whose config could not be decoded must not break validation.
    rows[0]["config"] = None
    assert config.validate(cfg, KEYS, connections=rows) == []


async def test_check_credential_is_a_noop_without_a_check_route():
    # Types with no `check_path` (every vendor-hosted one) must not be
    # blocked by a sidecar that is not running.
    assert await connectors.check_credential(
        config.connector_type("generic_mcp"), {"token": "t"}
    ) == {"ok": True}


async def test_check_credential_reports_a_missing_sidecar(monkeypatch):
    import socket

    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("ARYA_MCP_URL", f"http://127.0.0.1:{port}")
    out = await connectors.check_credential(
        config.connector_type("postgres"), {"dsn": "postgresql://u:p@h/db"}
    )
    assert out["ok"] is False
    assert "arya-mcp" in out["error"] and "docker compose up" in out["error"]
    assert await connectors.sidecar_reachable(timeout=1.0) is False


# ─── catalog + config validation ────────────────────────────────────────────


def test_connectors_catalog_seed_validates_and_rejects_bad_auth():
    catalog._validate("connectors", catalog.SEEDS["connectors"])
    bad = [{"id": "generic_mcp", "label": "x", "auth": "magic"}]
    with pytest.raises(ValueError):
        catalog._validate("connectors", bad)
    with pytest.raises(ValueError):  # bad placeholder in prompt_block
        catalog._validate("connectors", [
            {"id": "generic_mcp", "label": "x", "auth": "bearer",
             "prompt_block": "hello {nope}"},
        ])


def _cfg(**over):
    base = {"provider": "openai", "tools": {}, "connectors": []}
    base.update(over)
    return base


KEYS = {"openai": "sk-x"}


def test_validate_skips_connector_checks_without_snapshot():
    cfg = _cfg(connectors=[{"connection_id": "conn-missing", "allowed_tools": []}])
    assert config.validate(cfg, KEYS) == []


def test_validate_reports_connector_problems():
    rows = [
        {"id": "conn-1", "type": "google_calendar", "name": "Work", "status": "needs_auth"},
        {"id": "conn-2", "type": "generic_mcp", "name": "Demo", "status": "connected"},
        {"id": "conn-3", "type": "generic_mcp", "name": "Broken", "status": "error"},
    ]
    cfg = _cfg(
        tools={"appointments": True},
        connectors=[
            {"connection_id": "conn-1", "allowed_tools": ["list_events"]},
            {"connection_id": "conn-2", "allowed_tools": []},
            {"connection_id": "conn-3", "allowed_tools": ["x"]},
            {"connection_id": "conn-gone", "allowed_tools": ["x"]},
        ],
    )
    problems = config.validate(cfg, KEYS, connections=rows)
    text = " ".join(problems)
    assert "needs sign-in" in text
    assert "OAuth client id and secret are missing" in text
    assert "no tools selected" in text
    assert "failed its last check" in text
    assert "was removed" in text
    # The sample desk sitting next to a connector is no longer a problem:
    # the attachment switches it off, so there is nothing to warn about.
    assert "sample appointment tools" not in text
    ok_keys = {**KEYS, "google_oauth_client_id": "a", "google_oauth_client_secret": "b"}
    fixed = config.validate(
        _cfg(connectors=[{"connection_id": "conn-2", "allowed_tools": ["x"]}]),
        ok_keys, connections=rows,
    )
    assert fixed == []


# ─── real MCP round trip (in-process FastMCP over streamable HTTP) ──────────

pytestmark_mcp = pytest.mark.skipif(
    not connectors.MCP_AVAILABLE, reason="mcp extra not installed"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def mcp_server():
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    app = FastMCP("demo", stateless_http=True)

    @app.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search_products(query: str) -> str:
        """Find products by name."""
        return json.dumps([{"sku": "A1", "name": f"{query} deluxe"}])

    @app.tool()
    def cancel_order(order_id: str) -> str:
        """Cancel an order."""
        return f"cancelled {order_id}"

    @app.tool()
    def lookup_weather(location: str) -> str:
        """Collides with the native weather tool name on purpose."""
        return "sunny"

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        app.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning",
    ))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn did not start"
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


class _StubPool:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    async def fetch(self, _sql, ids):
        return [dict(r) for r in self.rows if r["id"] in ids]

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


@pytestmark_mcp
async def test_probe_lists_tools_with_write_flags(mcp_server):
    entry = config.connector_type("generic_mcp")
    tools = await connectors.probe(mcp_server, "streamable_http", {}, entry)
    by_name = {t["name"]: t for t in tools}
    assert set(by_name) == {"search_products", "cancel_order", "lookup_weather"}
    assert by_name["search_products"]["write"] is False  # readOnlyHint
    assert by_name["cancel_order"]["write"] is True  # name heuristic
    assert "Find products" in by_name["search_products"]["description"]


@pytestmark_mcp
async def test_probe_unreachable_is_a_readable_error():
    entry = config.connector_type("generic_mcp")
    with pytest.raises(connectors.ProbeError) as ei:
        await connectors.probe(
            f"http://127.0.0.1:{_free_port()}/mcp", "streamable_http", {}, entry,
            timeout=3.0,
        )
    assert "unreachable" in str(ei.value) or "timed out" in str(ei.value)
    assert "host.docker.internal" in str(ei.value)  # localhost hint


@pytestmark_mcp
async def test_build_toolsets_filters_public_dedupes_and_drops_dead(mcp_server):
    rows = [
        {"id": "conn-live", "type": "generic_mcp", "name": "demo", "url": mcp_server,
         "transport": "streamable_http", "status": "connected", "config": {},
         "secret": None, "tools": []},
        {"id": "conn-dead", "type": "generic_mcp", "name": "dead",
         "url": f"http://127.0.0.1:{_free_port()}/mcp", "transport": "streamable_http",
         "status": "connected", "config": {}, "secret": None, "tools": []},
        {"id": "conn-auth", "type": "google_calendar", "name": "cal", "url": None,
         "transport": "streamable_http", "status": "needs_auth", "config": {},
         "secret": None, "tools": []},
    ]
    cfg = {"connectors": [
        {"connection_id": "conn-live",
         "allowed_tools": ["search_products", "cancel_order", "lookup_weather"],
         "public_tools": ["search_products"]},
        {"connection_id": "conn-dead", "allowed_tools": ["x"]},
        {"connection_id": "conn-auth", "allowed_tools": ["list_events"]},
        {"connection_id": "conn-gone", "allowed_tools": ["x"]},
    ]}
    pool = _StubPool(rows)

    toolsets, live = await connectors.build_toolsets(
        cfg, pool, {}, is_public=False, taken_names={"lookup_weather"},
    )
    assert len(toolsets) == 1 and len(live) == 1
    assert live[0]["row"]["id"] == "conn-live"
    # lookup_weather collides with the native tool and is dropped
    assert sorted(live[0]["tool_names"]) == ["cancel_order", "search_products"]
    assert connectors.owner_of("cancel_order", live) is live[0]
    assert connectors.owner_of("nope", live) is None
    for ts in toolsets:
        await ts.aclose()

    # Public session: only the public subset survives.
    toolsets, live = await connectors.build_toolsets(
        cfg, pool, {}, is_public=True, taken_names=set(),
    )
    assert len(live) == 1 and live[0]["tool_names"] == ["search_products"]
    for ts in toolsets:
        await ts.aclose()

    # A tool the connection no longer lists (the check hid it) is never
    # exposed, however stale the attachment: cancel_order is ticked but
    # the probe result only lists search_products.
    rows[0]["tools"] = [{"name": "search_products", "write": False}]
    toolsets, live = await connectors.build_toolsets(
        cfg, _StubPool(rows), {}, is_public=False, taken_names=set(),
    )
    assert len(live) == 1 and live[0]["tool_names"] == ["search_products"]
    for ts in toolsets:
        await ts.aclose()


@pytestmark_mcp
async def test_the_worker_can_call_a_connector_tool_without_the_model(mcp_server):
    """The itinerary email needs this: an 8 KB page must never travel as a
    tool argument the model types out, so the worker calls the connector
    itself. A live connector carries its own address for exactly that."""
    rows = [{"id": "conn-live", "type": "generic_mcp", "name": "demo", "url": mcp_server,
             "transport": "streamable_http", "status": "connected", "config": {},
             "secret": None, "tools": []}]
    cfg = {"connectors": [{"connection_id": "conn-live",
                           "allowed_tools": ["search_products", "cancel_order"]}]}
    toolsets, live = await connectors.build_toolsets(
        cfg, _StubPool(rows), {}, is_public=False, taken_names=set(),
    )
    conn = live[0]
    assert conn["url"] == mcp_server and conn["transport"] == "streamable_http"

    out = await connectors.call_tool(conn, "search_products", {"query": "kettle"})
    assert out["ok"] and out["error"] is None
    assert json.loads(out["text"]) == [{"sku": "A1", "name": "kettle deluxe"}]

    # A tool that is not there comes back as a sentence, never an exception:
    # a failed send is something the agent reads out, not a dead turn.
    missing = await connectors.call_tool(conn, "no_such_tool", {})
    assert not missing["ok"] and missing["error"]

    for ts in toolsets:
        await ts.aclose()

    # A connector with nowhere to call says so rather than raising.
    nowhere = await connectors.call_tool({"row": {"name": "demo"}}, "x", {})
    assert not nowhere["ok"] and "no address" in nowhere["error"]


@pytestmark_mcp
async def test_build_toolsets_without_attachments_is_a_noop():
    toolsets, live = await connectors.build_toolsets(
        {}, _StubPool([]), {}, is_public=False, taken_names=set(),
    )
    assert toolsets == [] and live == []
