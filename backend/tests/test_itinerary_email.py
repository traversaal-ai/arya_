"""The itinerary email: the page itself, sent by the worker.

The model used to write this email. It called for a plain-text recap,
retyped it as a tool argument, and the caller got a paraphrase of a page
nobody sent. These tests pin the shape of what actually goes out and,
just as importantly, what the agent is told when it cannot go out at all.
"""

import json

import pytest

import connectors
import db
import itinerary_email as mail
import itinerary_html

PAGE = {
    "brand": {"name": "Avaera Travels", "icon": "", "mock": False, "mode": "test"},
    "trip": {"origin_city": "Islamabad", "origin_code": "ISB", "dest_city": "Paris",
             "dest_code": "PAR", "depart_date": "2026-10-01",
             "return_date": "2026-10-05", "travelers": 1},
    "flights": {"outbound": {"airline": "British Airways", "stops": "nonstop",
                             "duration": "8 hours", "depart": "Islamabad 00:00",
                             "arrive": "Paris 05:27", "price": "429.85 USD",
                             "total": "429.85 USD"},
                "return": None},
    "hotel": {"name": "Paris-Oasis", "rating": "4.8", "source": "Voyabay",
              "price_per_night": "$561", "nights": 4, "total": "$2,244",
              "maps_url": "https://maps.example/oasis"},
    "day_plan": [],
    "totals": {"flight": "429.85 USD", "hotel": "$2,244", "grand": "2,673.85 USD"},
}
ART = "8f2c1d4e-0000-4000-8000-00000000abcd"
CONV = "bd8e94ce-57ff-41da-866c-d887c410db5e"
COMPOSIO = {
    "kind": "composio", "tool": "COMPOSIO_MULTI_EXECUTE_TOOL", "slug": "GMAIL_SEND_EMAIL",
}


def _live(email_via=COMPOSIO, tools=("COMPOSIO_MULTI_EXECUTE_TOOL",)):
    entry = {"id": "composio", "label": "Composio"}
    if email_via:
        entry["email_via"] = email_via
    return [{"entry": entry, "row": {"id": "conn-1", "name": "Avaera Test"},
             "tool_names": list(tools), "url": "https://mcp.composio.dev/x",
             "headers": {}, "transport": "streamable_http"}]


class _Pool:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, *_args):
        return self.row


@pytest.fixture
def sent(monkeypatch):
    """A recorded connectors.call_tool, plus an artifact in the database."""
    calls = []

    async def fake_call(conn, tool, arguments, timeout=None):
        calls.append({"conn": conn, "tool": tool, "arguments": arguments})
        return {"ok": True, "text": '{"successful": true}', "structured": None, "error": None}

    async def fake_pool(_dsn):
        return _Pool({"id": ART, "title": "Trip to Paris",
                      "html": itinerary_html.render(PAGE, theme="arya"),
                      "data": json.dumps(PAGE)})

    monkeypatch.setattr(connectors, "call_tool", fake_call)
    monkeypatch.setattr(db, "get_pool", fake_pool)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    return calls


RUNTIME = {"conversation_id": CONV, "dsn": "postgres://x", "agent_id": "a1"}


async def test_the_page_goes_out_as_html_and_the_model_writes_none_of_it(sent):
    said = await mail.send(_live(), RUNTIME, "usama@example.com")
    assert said.startswith("Sent the written itinerary to usama@example.com")
    assert len(sent) == 1

    args = sent[0]["arguments"]
    assert sent[0]["tool"] == "COMPOSIO_MULTI_EXECUTE_TOOL"
    action = args["tools"][0]
    assert action["tool_slug"] == "GMAIL_SEND_EMAIL"
    assert action["arguments"]["recipient_email"] == "usama@example.com"
    assert action["arguments"]["subject"] == "Trip to Paris"
    assert action["arguments"]["is_html"] is True

    body = action["arguments"]["body"]
    assert "Paris-Oasis" in body and "British Airways" in body
    # The on-page Download button is filled in by JavaScript from the
    # current location, so in an inbox it points at nothing.
    assert "pdfLink" not in body and "<script>" not in body
    assert len(body) > 2000, "the page went out, not a summary of it"


async def test_a_public_deployment_adds_a_link_someone_can_open(sent, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://arya.example.com/")
    await mail.send(_live(), RUNTIME, "usama@example.com")
    body = sent[0]["arguments"]["tools"][0]["arguments"]["body"]
    assert f"https://arya.example.com/api/artifacts/{ART}/pdf" in body
    assert f"https://arya.example.com/api/artifacts/{ART}" in body
    assert "localhost" not in body


async def test_no_public_address_means_no_dead_link(sent):
    await mail.send(_live(), RUNTIME, "usama@example.com")
    body = sent[0]["arguments"]["tools"][0]["arguments"]["body"]
    assert "/api/artifacts/" not in body
    assert "Download PDF" not in body


async def test_nothing_written_yet_is_said_not_sent(monkeypatch):
    calls = []

    async def fake_call(*a, **k):
        calls.append(a)
        return {"ok": True, "text": "", "structured": None, "error": None}

    async def fake_pool(_dsn):
        return _Pool(None)

    monkeypatch.setattr(connectors, "call_tool", fake_call)
    monkeypatch.setattr(db, "get_pool", fake_pool)
    said = await mail.send(_live(), RUNTIME, "usama@example.com")
    assert "no written itinerary yet" in said
    assert not calls


async def test_a_refused_send_is_reported_word_for_word(sent, monkeypatch):
    async def failing(*_a, **_k):
        return {"ok": False, "text": "", "structured": None,
                "error": "Tool GMAIL_SEND_EMAIL not found"}

    monkeypatch.setattr(connectors, "call_tool", failing)
    said = await mail.send(_live(), RUNTIME, "usama@example.com")
    assert "could not be sent: Tool GMAIL_SEND_EMAIL not found" in said
    assert "offer to try again" in said.lower()


async def test_a_call_with_no_mail_connector_says_so(sent):
    said = await mail.send(_live(email_via=None), RUNTIME, "usama@example.com")
    assert "no way to send email on this call" in said
    assert not sent


async def test_a_connector_that_cannot_run_its_own_send_tool_does_not_count(sent):
    """Composio attached with only its search tool ticked can look things
    up and send nothing. The agent must not be handed a tool for it."""
    live = _live(tools=("COMPOSIO_SEARCH_TOOLS",))
    assert mail.sender(live) is None
    assert mail.tool(live, RUNTIME) is None
    said = await mail.send(live, RUNTIME, "usama@example.com")
    assert "no way to send email" in said
    assert not sent


def test_the_tool_exists_only_when_the_call_can_actually_send():
    assert mail.tool(_live(), RUNTIME) is not None
    assert mail.tool(_live(), RUNTIME).__name__ == "email_itinerary"
    assert mail.tool([], RUNTIME) is None
    assert mail.tool(_live(), None) is None          # console mode, no conversation
    assert mail.tool(_live(), {"dsn": "x"}) is None  # nothing to attach a page to


def test_a_connector_owning_this_name_is_left_alone():
    """This tool is added after the connectors were deduped, so it checks
    for itself. Two tools with one name do not degrade a call, they end
    it: Agent.__init__ raises on the duplicate."""
    live = _live(tools=("COMPOSIO_MULTI_EXECUTE_TOOL", "email_itinerary"))
    assert mail.tool(live, RUNTIME) is None


async def test_a_mistyped_address_never_reaches_the_vendor(sent):
    for bad in ("", "usama at example dot com", "nobody"):
        said = await mail.send(_live(), RUNTIME, bad)
        assert "not an email address" in said
    assert not sent


FULL_PAGE = {
    **PAGE,
    "flights": {
        "outbound": {"airline": "British Airways", "flight_number": "BA1516",
                     "stops": "nonstop", "duration": "8 hours 27 minutes",
                     "depart": {"time": "00:00", "date": "2026-10-01",
                                "airport": "Islamabad International Airport", "code": "ISB"},
                     "arrive": {"time": "05:27", "date": "2026-10-01",
                                "airport": "Paris-Le Bourget Airport", "code": "LBG"},
                     "reference": "JMXIGY", "price": "429.85 USD",
                     "url": "https://www.google.com/travel/flights?q=isb-lbg"},
        "return": {"airline": "British Airways", "flight_number": "BA1516",
                   "stops": "nonstop", "duration": "8 hours 28 minutes",
                   "depart": {"time": "03:05", "date": "2026-10-05",
                              "airport": "Paris-Beauvais Airport", "code": "BVA"},
                   "arrive": {"time": "14:33", "date": "2026-10-05",
                              "airport": "Islamabad International Airport", "code": "ISB"},
                   "reference": "JMXIGY", "price": "429.85 USD",
                   "url": "https://www.google.com/travel/flights?q=bva-isb"},
        "fare": {"scope": "journey", "total": "429.85 USD"},
    },
    "hotel": {**PAGE["hotel"], "check_in": "2026-10-01", "check_out": "2026-10-05"},
    "day_plan": [{"date": "2026-10-02", "label": "02 Oct 2026, Friday",
                  "theme": "Art and History", "location": "Paris · Paris-Oasis",
                  "slots": [{"time": "09:30", "activity": "Visit the Louvre Museum",
                             "description": "A landmark art museum.",
                             "url": "https://maps.example/louvre"},
                            {"time": "13:00", "activity": "Lunch at Pink Mamma",
                             "description": ""}]}],
}

BOOKING = {
    "kind": "flight_booked", "email": "usama@example.com", "phone": "+923330000000",
    "travellers": ["Mr Usama Shami"], "reference": "JMXIGY",
    "order_id": "ord_123", "total": "429.85 USD",
}


def test_the_trip_reaches_the_travellers_own_calendar_not_the_agencys():
    """No calendar is written to by Arya. A Composio event can only live on
    the mailbox the AGENCY connected, and the trip is the client's."""
    links = mail.calendar_links(FULL_PAGE)
    assert [x["label"] for x in links] == [
        "Outbound flight", "Return flight", "Hotel stay", "02 Oct 2026, Friday"]
    out = links[0]["url"]
    assert out.startswith("https://calendar.google.com/calendar/render?")
    assert "action=TEMPLATE" in out
    # All day on the departure date: Google's template URL cannot carry a
    # timezone, so a timed event would be read in the reader's own.
    assert "dates=20261001%2F20261002" in out
    assert "British+Airways+BA1516" in out
    assert "Islamabad+International+Airport" in out
    # The exact local times are inside the event instead of being lost.
    assert "00%3A00+local+time" in out and "05%3A27+local+time" in out
    assert "JMXIGY" in out
    # A stay from the 1st to the 5th is four nights, and Google reads the
    # end as exclusive, so this is exactly right.
    assert "dates=20261001%2F20261005" in links[2]["url"]
    assert "Stay+at+Paris-Oasis" in links[2]["url"]
    # One event per planned day, with that day's schedule inside it.
    assert "Paris%3A+Visit+the+Louvre+Museum" in links[3]["url"]
    assert "09%3A30" in links[3]["url"]


def test_calendar_events_carry_the_maps_link_the_total_and_the_search_link():
    """Google Calendar auto-links a bare URL sitting in an event's plain
    text description, the same way Gmail auto-links one in a message body,
    so a Maps link or a fare only had to be appended as its own line."""
    links = mail.calendar_links(FULL_PAGE)
    outbound, hotel_link, day = links[0]["url"], links[2]["url"], links[3]["url"]

    # The same honest search link the itinerary page carries, worded the
    # same way: a search, never a live tracker or a check-in page.
    assert "Search+this+route+on+Google+Flights%3A+https%3A%2F%2Fwww.google" in outbound

    # The vendor's own total, and the property's own Maps page.
    assert "%242%2C244+total+for+4+nights" in hotel_link
    assert "https%3A%2F%2Fmaps.example%2Foasis" in hotel_link

    # Each place in a planned day carries its own Maps link, not only the
    # city-level one the event's location field already has.
    assert "https%3A%2F%2Fmaps.example%2Flouvre" in day
    # A slot with no link (the lunch stop) adds nothing, not a blank line.
    assert day.count("maps.example") == 1


def test_a_thin_payload_offers_only_the_links_it_can_stand_behind():
    assert mail.calendar_links({}) == []
    assert mail.calendar_links({"flights": {"outbound": {"airline": "X"}}}) == []
    # A day with no activities is not a calendar entry.
    assert mail.calendar_links({"day_plan": [{"date": "2026-10-02", "slots": []}]}) == []
    # A page stored before legs were structured still yields nothing wrong.
    old = {"flights": {"outbound": {"airline": "BA",
                                    "depart": "Islamabad 00:00 on 2026-10-01"}}}
    assert mail.calendar_links(old) == []


async def test_the_itinerary_email_carries_the_calendar_links(sent):
    async def fake_pool(_dsn):
        return _Pool({"id": ART, "title": "Trip to Paris",
                      "html": itinerary_html.render(FULL_PAGE, theme="arya"),
                      "data": json.dumps(FULL_PAGE)})

    import db as db_mod
    db_mod.get_pool = fake_pool
    said = await mail.send(_live(), RUNTIME, "usama@example.com")
    body = sent[0]["arguments"]["tools"][0]["arguments"]["body"]
    assert "Add to your calendar" in body
    assert "Add Outbound flight to your calendar" in body
    assert "Add Hotel stay to your calendar" in body
    assert "calendar.google.com/calendar/render" in body
    assert "calendar" in said


def test_the_traveller_gets_a_confirmation_the_airline_may_not_send():
    """Duffel sends a passenger nothing on a test token, and on a live one
    it is the airline that may or may not."""
    subject, html = mail.booking_email(FULL_PAGE, BOOKING)
    assert subject == "Your flights to Paris"
    assert "Your flights are booked" in html
    assert "British Airways" in html
    assert "<b>00:00</b> on 2026-10-01" in html
    assert "<b>14:33</b> on 2026-10-05" in html
    assert "Total paid <b>429.85 USD</b>" in html
    assert "Airline reference (for check-in) <b>JMXIGY</b>" in html
    assert "Add Outbound flight to your calendar" in html
    # The stay and the days are not in it: they are not booked yet, and the
    # itinerary mail at the end of the call carries them.
    assert "Hotel stay" not in html and "Louvre" not in html


def test_the_agency_gets_the_record_not_the_pretty_page():
    subject, html = mail.agency_email(FULL_PAGE, BOOKING, "conv-abc")
    assert subject == "Booking JMXIGY: Islamabad to Paris"
    assert "Mr Usama Shami" in html and "+923330000000" in html
    assert "ord_123" in html and "conv-abc" in html
    assert "429.85 USD" in html
    # Nobody at the agency is putting a client's holiday in their own diary.
    assert "calendar.google.com" not in html


def test_the_agency_address_is_the_mailbox_it_sends_from():
    live = _live()
    assert mail.agency_address(live) == ""
    live[0]["row"]["config"] = {"account_email": "ops@avaera.com"}
    assert mail.agency_address(live) == "ops@avaera.com"
    # An agent-level field wins, for an operations inbox.
    assert mail.agency_address(live, {"copy_bookings_to": "desk@avaera.com"}) == "desk@avaera.com"
    assert mail.agency_address(live, {"copy_bookings_to": "not an address"}) == "ops@avaera.com"
    assert mail.agency_address([], {}) == ""


async def test_a_booking_emails_the_traveller_and_the_agency(sent, monkeypatch):
    async def fake_pool(_dsn):
        return _Pool({"id": ART, "title": "Trip to Paris",
                      "html": itinerary_html.render(FULL_PAGE, theme="arya"),
                      "data": json.dumps(FULL_PAGE)})

    monkeypatch.setattr(db, "get_pool", fake_pool)
    live = _live()
    live[0]["row"]["config"] = {"account_email": "ops@avaera.com"}
    await mail.notify_booking({**RUNTIME, "connectors": live}, BOOKING)

    assert len(sent) == 2
    to = [c["arguments"]["tools"][0]["arguments"]["recipient_email"] for c in sent]
    assert to == ["usama@example.com", "ops@avaera.com"]
    assert "Your flights are booked" in sent[0]["arguments"]["tools"][0]["arguments"]["body"]
    assert "Booking recorded" in sent[1]["arguments"]["tools"][0]["arguments"]["body"]


async def test_one_copy_only_when_the_agency_booked_for_itself(sent, monkeypatch):
    async def fake_pool(_dsn):
        return _Pool(None)

    monkeypatch.setattr(db, "get_pool", fake_pool)
    live = _live()
    live[0]["row"]["config"] = {"account_email": "usama@example.com"}
    await mail.notify_booking({**RUNTIME, "connectors": live}, BOOKING)
    assert len(sent) == 1


async def test_a_booking_with_nowhere_to_send_is_simply_not_sent(sent):
    await mail.notify_booking({**RUNTIME, "connectors": []}, BOOKING)
    await mail.notify_booking(None, BOOKING)
    await mail.notify_booking({**RUNTIME, "connectors": _live(email_via=None)}, BOOKING)
    assert not sent


def test_public_base_ignores_anything_that_is_not_a_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "arya.example.com")
    assert mail.public_base() == ""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://arya.example.com//")
    assert mail.public_base() == "https://arya.example.com"
    monkeypatch.delenv("PUBLIC_BASE_URL")
    assert mail.public_base() == ""
