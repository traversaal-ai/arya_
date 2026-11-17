"""Cal.com booking tools: time parsing, service matching, response shapes,
and the whole tool surface against a fake Cal.com.

No network and no account needed: a stub Cal.com API is mounted next to the
sidecar's own app, so these run in CI and on a laptop. If ARYA_CALCOM_KEY is
set the last test also talks to the real thing.
"""

import asyncio
import datetime
import json
import os
import re
import socket

import httpx
import pytest

from mcp_tools import calcom

# ─── the fixture calendar, kept in the future ───────────────────────────────
#
# Every date below is anchored on Friday 2026-09-11, and the tools under test
# read the real clock (do_find_slots windows from datetime.now). Pinned
# literals therefore rot the moment that week passes: a "spread across a full
# open day" assertion silently starts checking a day the window no longer
# reaches. So every date string goes through _d(), which shifts it forward by
# whole WEEKS. A multiple of 7 preserves weekday identity, which is what the
# schedule (Monday to Friday, 9 to 5) and these assertions depend on: Friday
# stays a Friday, and the seeded booking stays on its Saturday.
_ANCHOR = datetime.date(2026, 9, 10)          # the day before the fixture's Friday
_SHIFT_WEEKS = max(0, -(-((datetime.date.today() - _ANCHOR).days + 1) // 7))
_SHIFT = datetime.timedelta(weeks=_SHIFT_WEEKS)


def _d(value: str) -> str:
    """Shift a 2026-09-NN date or datetime string into the current week."""
    def bump(m):
        return (datetime.date.fromisoformat(m.group(0)) + _SHIFT).isoformat()

    return re.sub(r"2026-09-\d\d", bump, value)


def _say_day(value: str) -> str:
    """The same date as the agent speaks it: "September 12"."""
    d = datetime.date.fromisoformat(_d(value)[:10])
    return f"{d.strftime('%B')} {d.day}"

# ─── times ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("given", "expect"), [
    (_d("2026-09-12T15:00:00Z"), _d("2026-09-12T15:00:00+00:00")),
    (_d("2026-09-12T15:00:00+05:00"), _d("2026-09-12T10:00:00+00:00")),
    (_d("2026-09-12T15:00:00"), _d("2026-09-12T15:00:00+00:00")),  # naive = UTC
    (_d("2026-09-12 15:00:00"), _d("2026-09-12T15:00:00+00:00")),  # space instead of T
])
def test_parse_when_normalises_to_utc(given, expect):
    assert calcom.parse_when(given).isoformat() == expect


@pytest.mark.parametrize("bad", ["", "   ", "next tuesday", "3pm", "12/09/2026"])
def test_parse_when_refuses_vague_times(bad):
    with pytest.raises(calcom.CalcomError) as ei:
        calcom.parse_when(bad)
    assert "no date" in str(ei.value) or "ISO 8601" in str(ei.value)


def test_speak_time_is_how_a_person_says_it():
    said = calcom._speak_time(_d("2026-09-12T10:00:00Z"), "Asia/Karachi")
    assert _say_day("2026-09-12") in said
    assert "3 PM" in said  # 10:00 UTC is 3 PM in Karachi
    assert ":" not in said  # a whole hour needs no minutes


def test_speak_time_keeps_minutes_when_there_are_any():
    assert "3:30 PM" in calcom._speak_time(_d("2026-09-12T10:30:00Z"), "Asia/Karachi")


def test_check_tz_demands_a_real_zone():
    assert calcom._check_tz("Asia/Karachi") == "Asia/Karachi"
    for bad in ("", "PKT", "GMT+5", "Mars/Olympus"):
        with pytest.raises(calcom.CalcomError):
            calcom._check_tz(bad)


# ─── service matching, the voice-shaped part ────────────────────────────────

SERVICES = [
    {"id": 1, "name": "30 Minute Consultation", "minutes": 30, "slug": "consult-30"},
    {"id": 2, "name": "Home Visit", "minutes": 60, "slug": "home-visit"},
]


@pytest.mark.parametrize(("said", "expect_id"), [
    ("30 Minute Consultation", 1),   # exact
    ("consult-30", 1),               # slug
    ("consultation", 1),             # substring
    ("Consultatoin", 1),             # fuzzy, caller mumbles
    ("home visit", 2),
])
def test_match_service_resolves_what_a_caller_says(said, expect_id):
    assert calcom.match_service(SERVICES, said)["id"] == expect_id


def test_match_service_with_one_service_needs_no_name():
    only = [SERVICES[0]]
    assert calcom.match_service(only, "")["id"] == 1
    assert calcom.match_service(only, "anything at all")["id"] == 1


def test_match_service_asks_rather_than_guessing():
    with pytest.raises(calcom.CalcomError) as ei:
        calcom.match_service(SERVICES, "")
    assert "which one they mean" in str(ei.value)
    with pytest.raises(calcom.CalcomError) as ei:
        calcom.match_service(SERVICES, "dog grooming")
    assert "30 Minute Consultation" in str(ei.value)  # names what IS offered
    with pytest.raises(calcom.CalcomError) as ei:
        calcom.match_service([], "anything")
    assert "no bookable services" in str(ei.value)


# Two services whose names overlap, which is how a real clinic names things
# and the case that used to book the wrong one.
CHECKUPS = [
    {"id": 1, "name": "Regular Checkup", "minutes": 15, "slug": "regular-checkup"},
    {"id": 2, "name": "Specialist Checkup", "minutes": 30, "slug": "specialist-checkup"},
    {"id": 3, "name": "15 min meeting", "minutes": 15, "slug": "15min"},
]


@pytest.mark.parametrize("said", [
    "checkup",            # matches both by substring
    "check up",           # callers say it as two words
    "a checkup please",   # filler around the only real word
    "I want a checkup",
    "Checkup",            # case
])
def test_ambiguous_service_names_ask_instead_of_guessing(said):
    """It used to pick Regular Checkup silently, because it scored a hair
    higher on near-spelling. Booking a 15 minute slot for someone who wanted
    the 30 minute specialist is worse than one extra question."""
    with pytest.raises(calcom.CalcomError) as ei:
        calcom.match_service(CHECKUPS, said)
    msg = str(ei.value)
    assert "which one they mean" in msg
    assert "Regular Checkup" in msg and "Specialist Checkup" in msg
    assert "15 min meeting" not in msg  # only the candidates, not everything


@pytest.mark.parametrize(("said", "expect"), [
    ("Regular Checkup", 1),
    ("regular", 1),               # one distinguishing word is enough
    ("specialist", 2),
    ("regular check-up", 1),      # punctuation and spacing ignored
    ("SPECIALIST CHECKUP", 2),
    ("15 min", 3),                # matches the slug
    ("15min", 3),
])
def test_a_distinguishing_word_still_resolves(said, expect):
    assert calcom.match_service(CHECKUPS, said)["id"] == expect


def test_email_must_look_like_one():
    assert calcom._check_email(" a@b.co ") == "a@b.co"
    for bad in ("", "nope", "a@b", "a b@c.com"):
        with pytest.raises(calcom.CalcomError):
            calcom._check_email(bad)


# ─── Cal.com's two slot shapes ──────────────────────────────────────────────


def test_slot_starts_accepts_both_shapes_cal_has_shipped():
    keyed = {_d("2026-09-12"): [{"start": _d("2026-09-12T09:00:00Z")},
                            {"start": _d("2026-09-12T10:00:00Z")}]}
    flat = [{"start": _d("2026-09-12T09:00:00Z")}, _d("2026-09-12T10:00:00Z")]
    wrapped = {"slots": keyed}
    for shape in (keyed, flat, wrapped):
        assert calcom._slot_starts(shape) == [
            _d("2026-09-12T09:00:00Z"), _d("2026-09-12T10:00:00Z")
        ]
    assert calcom._slot_starts({}) == []
    assert calcom._slot_starts({"slots": {}}) == []


def test_each_route_keeps_its_own_api_version():
    """Cal.com routes by version header and a wrong one 404s the route, so
    these constants are load-bearing. Verified against the live API by
    probing with an invalid key: 401 means the route exists at that
    version, 404 means it does not."""
    assert calcom.V_EVENT_TYPES == "2024-06-14"
    assert calcom.V_BOOKINGS == "2024-08-13"
    assert calcom.V_SLOTS == "2024-09-04"
    assert calcom.V_EVENT_TYPES != calcom.V_BOOKINGS


def test_error_text_prefers_cals_own_message():
    assert calcom._error_text({"error": {"message": "no such event type"}}, 400) \
        == "no such event type"
    assert calcom._error_text({"message": "bad input"}, 400) == "bad input"
    assert calcom._error_text({}, 503) == "Cal.com answered http 503"


# ─── the tool surface, against a fake Cal.com ───────────────────────────────

BOOKED: list[dict] = []
UPCOMING: list[dict] = []     # what /bookings returns, i.e. what landed
WINDOWS: list[tuple] = []     # every (start, end) the slots route was asked for
SLOW = {"create": 0.0}        # seconds the create response is delayed by

# One booking that already exists when a test starts. 09:00 UTC is 2 PM in
# Karachi, and the real API reports the zone it was booked in per attendee.
# Tests that CREATE a booking must use a different slot, or the pre-book
# reconcile correctly reports it as already booked.
SEEDED_BOOKING = {
    "uid": "bk_abc123", "start": _d("2026-09-12T09:00:00Z"), "status": "accepted",
    "attendees": [{"name": "Ali Raza", "email": "ali@example.com",
                   "timeZone": "Asia/Karachi"}],
    # Deliberately the LIVE shape, copied from a real /bookings response:
    # eventType carries an id and a slug and NO title, there is a flat
    # eventTypeId beside it, and the title has Cal.com's host-and-guest
    # suffix. The fake used to send eventType.title, which is why the tests
    # passed while the agent could not name the type in a real call.
    "eventType": {"id": 1, "slug": "consult-30"},
    "eventTypeId": 1,
    "title": "30 Minute Consultation between Dr Who and Ali Raza",
}
FREE_SLOT = _d("2026-09-14T09:00:00Z")   # a Monday the schedule is open on

# 2026-09-11 is a Friday, open all day: 9 to 5 Karachi is 04:00 to 12:00
# UTC, in 30 minute steps. This is the shape that exposed the agent offering
# "9 AM or 9:30 AM" on a wide open day.
FULL_DAY = [_d(f"2026-09-11T{h:02d}:{m:02d}:00Z")
            for h in range(4, 12) for m in (0, 30)]

# 09:00 and 10:00 UTC are 2 PM and 3 PM in Karachi; the 14th is a Monday.
SLOTS = {
    _d("2026-09-11"): FULL_DAY,
    _d("2026-09-12"): [_d("2026-09-12T09:00:00Z"), _d("2026-09-12T10:00:00Z")],
    _d("2026-09-14"): [_d("2026-09-14T09:00:00Z"), _d("2026-09-14T11:00:00Z"),
                   _d("2026-09-14T12:00:00Z"), _d("2026-09-14T13:00:00Z")],
    _d("2026-09-15"): [_d("2026-09-15T09:00:00Z"), _d("2026-09-15T10:00:00Z")],
}


def _fake_cal_app():
    """A Cal.com stand-in: enough of v2 to exercise every tool, and it
    asserts the version headers Cal.com silently requires."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def wrap(data):
        return JSONResponse({"status": "success", "data": data})

    async def me(request):
        assert request.headers["authorization"] == "Bearer cal_live_test"
        return wrap({"email": "owner@shop.test", "username": "shop"})

    async def event_types(request):
        # The live API serves this route ONLY at 2024-06-14 and answers
        # 404 "Cannot GET /v2/event-types" for a bookings-era version, which
        # reads like a missing endpoint. Mirror that, so getting the version
        # wrong fails here instead of on a real call.
        if request.headers.get("cal-api-version") != calcom.V_EVENT_TYPES:
            return JSONResponse(
                {"error": {"code": "NotFoundException",
                           "message": "Cannot GET /v2/event-types"}},
                status_code=404,
            )
        return wrap([
            {"id": 1, "title": "30 Minute Consultation",
             "lengthInMinutes": 30, "slug": "consult-30"},
            {"id": 2, "title": "Home Visit", "length": 60, "slug": "home-visit"},
            # An owner's API key returns hidden event types too; the agent
            # must not offer one, because a caller is the public.
            {"id": 3, "title": "Secret meeting", "lengthInMinutes": 15,
             "slug": "secret", "hidden": True},
        ])

    async def slots(request):
        assert request.headers["cal-api-version"] == calcom.V_SLOTS
        # Any service the account offers, not a hardcoded one: the tool
        # resolves the name to an id itself, and Home Visit is id 2.
        assert request.query_params["eventTypeId"] in {"1", "2"}
        assert request.query_params["timeZone"] == "Asia/Karachi"
        WINDOWS.append((request.query_params["start"], request.query_params["end"]))
        start = calcom.parse_when(request.query_params["start"])
        end = calcom.parse_when(request.query_params["end"])
        keep = {}
        for day, times in SLOTS.items():
            inside = [s for s in times if start <= calcom.parse_when(s) < end]
            if inside:
                keep[day] = [{"start": s} for s in inside]
        return wrap(keep)

    async def schedules(request):
        assert request.headers["cal-api-version"] == calcom.V_SCHEDULES
        return wrap({
            "id": 1, "name": "Working Hours", "isDefault": True,
            "timeZone": "Asia/Karachi",
            "availability": [{
                "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
                "startTime": "09:00", "endTime": "17:00",
            }],
        })

    async def one_booking(request):
        # UPCOMING first: it holds everything that exists, seeded or created.
        # BOOKED holds only the request bodies this test posted.
        uid = request.path_params["uid"]
        found = next((b for b in UPCOMING if b.get("uid") == uid), None)
        if found is None:
            found = next((b for b in BOOKED if b.get("uid") == uid), None)
        if found is None:
            return JSONResponse({"error": {"message": "not found"}}, status_code=404)
        return wrap(found)

    async def create(request):
        assert request.headers["cal-api-version"] == calcom.V_BOOKINGS
        body = await request.json()
        made = {"uid": f"bk_{len(BOOKED) + 1}", "start": body["start"],
                "status": "accepted",
                "attendees": [{"email": body["attendee"]["email"],
                               "timeZone": body["attendee"]["timeZone"]}],
                "eventType": {"title": "30 Minute Consultation"}}
        BOOKED.append({**body, "uid": made["uid"]})
        UPCOMING.append(made)
        if SLOW["create"]:
            # The write LANDS and then the response is late, which is the
            # case that used to produce a duplicate booking.
            await asyncio.sleep(SLOW["create"])
        return wrap(made)

    async def listing(request):
        # One source of truth, so a reconcile sees exactly what landed. The
        # fixture seeds SEEDED_BOOKING; creates append to it.
        if request.query_params.get("attendeeEmail") == "nobody@shop.test":
            return wrap([])
        return wrap(list(UPCOMING))

    async def reschedule(request):
        body = await request.json()
        return wrap({"uid": "bk_new456", "start": body["start"],
                     "status": "accepted"})

    async def cancel(request):
        return wrap({"uid": "bk_abc123", "status": "cancelled"})

    async def unauthorized(request):
        return JSONResponse({"error": {"message": "no"}}, status_code=401)

    return Starlette(routes=[
        Route("/v2/me", me),
        Route("/v2/event-types", event_types),
        Route("/v2/slots", slots),
        Route("/v2/bookings", create, methods=["POST"]),
        Route("/v2/bookings", listing, methods=["GET"]),
        Route("/v2/schedules/default", schedules),
        Route("/v2/bookings/bk_abc123/reschedule", reschedule, methods=["POST"]),
        Route("/v2/bookings/bk_abc123/cancel", cancel, methods=["POST"]),
        Route("/v2/bookings/{uid}", one_booking),
        Route("/v2/unauth/event-types", unauthorized),
        Route("/v2/unauth/me", unauthorized),
    ])


async def _serve(app, port):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    return server, task


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def fake_cal(monkeypatch):
    BOOKED.clear()
    UPCOMING.clear()
    WINDOWS.clear()
    UPCOMING.append(dict(SEEDED_BOOKING))
    SLOW["create"] = 0.0
    calcom._schedules.clear()
    port = _free_port()
    server, task = await _serve(_fake_cal_app(), port)
    monkeypatch.setattr(calcom, "API_BASE", f"http://127.0.0.1:{port}/v2")
    try:
        yield f"http://127.0.0.1:{port}/v2"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def sidecar(fake_cal):
    import mcp_server

    port = _free_port()
    server, task = await _serve(mcp_server.create_app(), port)
    try:
        yield port
    finally:
        server.should_exit = True
        await task


KEY = "cal_live_test"


async def test_the_whole_booking_flow(fake_cal):
    """The behaviour, called directly. The MCP adapter is proven once, in
    test_tools_are_reachable_over_mcp: standing up two servers and a session
    per assertion made this file slow and occasionally flaky."""
    tools = await calcom.do_list_services(KEY)
    assert "30 Minute Consultation" in tools
    assert json.loads(tools)["services"][0]["minutes"] == 30

    slots = await calcom.do_find_slots(
        KEY, "Asia/Karachi", "consultation", day=_d("2026-09-14"),
    )
    body = json.loads(slots)
    assert body["service"] == "30 Minute Consultation"
    assert body["timezone"] == "Asia/Karachi"
    # Spoken in the CALLER's zone, machine value kept for booking.
    assert body["slots"][0]["say"].endswith("2 PM")
    assert body["slots"][0]["starts_at"] == FREE_SLOT

    booked = await calcom.do_book(
        KEY, FREE_SLOT, "Ali Raza", "ali@example.com",
        "Asia/Karachi", "consultation", "first visit",
    )
    out = json.loads(booked)
    assert out["booked"] is True and not out.get("already")
    assert out["reference"].startswith("bk_")
    assert "2 PM" in out["when"]
    # What Cal.com actually received.
    assert BOOKED[0]["eventTypeId"] == 1
    assert BOOKED[0]["attendee"] == {
        "name": "Ali Raza", "email": "ali@example.com", "timeZone": "Asia/Karachi",
    }
    assert BOOKED[0]["bookingFieldsResponses"]["notes"] == "first visit"
    assert BOOKED[0]["start"] == FREE_SLOT

    mine = await calcom.do_find_bookings(KEY, "ali@example.com", "Asia/Karachi")
    assert json.loads(mine)["bookings"][0]["reference"] == "bk_abc123"

    moved = await calcom.do_reschedule(
        KEY, "bk_abc123", _d("2026-09-12T10:00:00Z"), "Asia/Karachi", "conflict")
    assert json.loads(moved)["rescheduled"] is True

    gone = await calcom.do_cancel(KEY, "bk_abc123", "changed plans")
    assert "cancelled" in gone


async def test_hidden_event_types_are_never_offered(fake_cal):
    """Hidden means "not on my public booking page". An owner's API key
    still returns them, and a phone caller is the public, so they must not
    be offered or bookable by name."""
    listed = json.loads(await calcom.do_list_services(KEY))["services"]
    assert [s["name"] for s in listed] == ["30 Minute Consultation", "Home Visit"]
    # Not reachable even if the caller names it exactly.
    out = await calcom.do_find_slots(KEY, "Asia/Karachi", "Secret meeting")
    assert "no service called" in out or "30 Minute Consultation" in out
    assert "Secret" not in json.dumps(listed)


# ─── A. booking reliability ─────────────────────────────────────────────────


async def test_booking_the_same_slot_twice_does_not_duplicate(fake_cal):
    """Cal.com has no idempotency key, so the tool looks before it writes."""
    args = (FREE_SLOT, "Ali", "ali@example.com", "Asia/Karachi",
            "consultation")
    first = json.loads(await calcom.do_book(KEY, *args))
    assert first["booked"] is True and not first.get("already")
    assert len(BOOKED) == 1

    second = json.loads(await calcom.do_book(KEY, *args))
    assert second["already"] is True
    assert "do NOT book again" in second["note"]
    assert len(BOOKED) == 1, "the second call must not reach Cal.com's create"


async def test_a_slow_booking_that_lands_is_reported_as_booked(fake_cal, monkeypatch):
    """The bug: the write took longer than the client waited, so the agent
    said "an issue", the appointment existed, and the caller's retry made a
    second one. A timeout on a write means UNKNOWN, so go and look."""
    monkeypatch.setattr(calcom, "WRITE_TIMEOUT_S", 0.2)
    SLOW["create"] = 0.6
    out = json.loads(await calcom.do_book(
        KEY, FREE_SLOT, "Ali", "ali@example.com",
        "Asia/Karachi", "consultation",
    ))
    assert out["booked"] is True and out["already"] is True
    assert len(BOOKED) == 1, "one create, reported honestly"


async def test_a_booking_that_truly_failed_still_says_so(fake_cal, monkeypatch):
    """Reconciling must not turn every failure into a success: with nothing
    landed, the caller has to hear that it did not work."""
    monkeypatch.setattr(calcom, "WRITE_TIMEOUT_S", 0.2)

    async def never_lands(*a, **kw):
        raise calcom.CalcomError("Cal.com did not answer in time")

    monkeypatch.setattr(calcom, "_call", _only_for_writes(never_lands))
    out = await calcom.do_book(
        KEY, FREE_SLOT, "Ali", "ali@example.com",
        "Asia/Karachi", "consultation",
    )
    assert "Not booked" in out and "did not answer in time" in out


def _only_for_writes(replacement):
    """Fail POSTs, let GETs through to the fake, so a reconcile can run."""
    real = calcom._call

    async def routed(key, method, path, **kw):
        if method == "POST":
            return await replacement(key, method, path, **kw)
        return await real(key, method, path, **kw)

    return routed


async def test_existing_booking_matches_to_the_minute(fake_cal):
    await calcom.do_book(KEY, FREE_SLOT, "Ali",
                         "ali@example.com", "Asia/Karachi", "consultation")
    hit = await calcom.existing_booking(
        KEY, "ali@example.com", calcom.parse_when(_d("2026-09-14T09:00:30Z")))
    assert hit is not None, "seconds must not defeat the match"
    miss = await calcom.existing_booking(
        KEY, "ali@example.com", calcom.parse_when(_d("2026-09-14T16:00:00Z")))
    assert miss is None


def test_the_worker_timeout_is_longer_than_the_vendor_timeout():
    """The invariant behind the duplicate booking: the inner wait must end
    before the outer one, or the readable error never gets home."""
    import catalog
    import connectors

    entry = next(c for c in catalog.SEEDS["connectors"] if c["id"] == "calcom")
    assert entry["tool_timeout_s"] > calcom.WRITE_TIMEOUT_S > calcom.TIMEOUT_S
    assert entry["tool_timeout_s"] > connectors.TOOL_TIMEOUT_S


# ─── B. working hours ───────────────────────────────────────────────────────


async def test_business_hours_reads_the_schedule(fake_cal):
    out = json.loads(await calcom.do_business_hours(KEY))
    assert out["hours"] == "Monday to Friday, 9 AM to 5 PM"
    assert out["timezone"] == "Asia/Karachi"
    assert "Karachi time" in out["say"]
    assert "in_caller_time" not in out


async def test_business_hours_converts_for_a_distant_caller(fake_cal):
    out = json.loads(await calcom.do_business_hours(KEY, "Europe/London"))
    # 9 to 5 Karachi is 5 AM to 1 PM in London (both +1h DST aside).
    assert "5 AM" in out["in_caller_time"] and "1 PM" in out["in_caller_time"]
    assert "where the caller is" in out["say"]


async def test_the_schedule_is_cached(fake_cal, monkeypatch):
    await calcom.schedule_for(KEY)
    calls = {"n": 0}
    real = calcom._call

    async def counted(key, method, path, **kw):
        if path == "/schedules/default":
            calls["n"] += 1
        return await real(key, method, path, **kw)

    monkeypatch.setattr(calcom, "_call", counted)
    await calcom.schedule_for(KEY)
    await calcom.schedule_for(KEY)
    assert calls["n"] == 0, "the working hours change rarely; C asks often"


@pytest.mark.parametrize(("days", "said"), [
    (["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], "Monday to Friday"),
    (["Saturday", "Sunday"], "Saturday and Sunday"),
    (["Monday"], "Monday"),
    (["Monday", "Wednesday", "Friday"], "Monday, Wednesday, Friday"),
    (["Monday", "Tuesday", "Thursday", "Friday"],
     "Monday and Tuesday, Thursday and Friday"),
])
def test_day_runs_are_said_the_way_people_say_them(days, said):
    assert calcom._day_runs(days) == said


def test_parse_schedule_tolerates_shape_changes():
    assert calcom.parse_schedule({}) == {"timezone": "", "blocks": []}
    one = calcom.parse_schedule({
        "timeZone": "Asia/Tokyo",
        "availability": {"days": ["Monday"], "startTime": "10:00:00",
                         "endTime": "18:00:00"},
    })
    assert one["blocks"] == [{"days": ["Monday"], "start": "10:00", "end": "18:00"}]
    junk = calcom.parse_schedule({"availability": ["nope", {"days": []}]})
    assert junk["blocks"] == []


# ─── C. offering times ──────────────────────────────────────────────────────


async def test_a_named_day_and_time_is_answered_yes_or_no(fake_cal):
    yes = json.loads(await calcom.do_find_slots(
        KEY, "Asia/Karachi", "consultation", day=_d("2026-09-12"),
        preferred_time="14:00",   # 09:00 UTC
    ))
    assert yes["available"] is True
    assert yes["starts_at"] == _d("2026-09-12T09:00:00Z")

    no = json.loads(await calcom.do_find_slots(
        KEY, "Asia/Karachi", "consultation", day=_d("2026-09-12"),
        preferred_time="16:00",   # nothing at 11:00 UTC
    ))
    assert no["available"] is False
    assert "2 PM" in no["requested"] or "4 PM" in no["requested"]
    assert 1 <= len(no["slots"]) <= 2, "offer the nearest, not a list"


async def test_a_day_is_converted_from_the_callers_zone(fake_cal):
    await calcom.do_find_slots(KEY, "Asia/Karachi", "consultation",
                              day=_d("2026-09-12"))
    start, end = WINDOWS[-1]
    # Midnight in Karachi is 19:00 UTC the day before, not 00:00 UTC.
    assert start.startswith(_d("2026-09-11T19:00"))
    assert end.startswith(_d("2026-09-12T19:00"))


async def test_a_vague_request_spreads_across_days(fake_cal, monkeypatch):
    """It used to return the first twelve free slots, which were all one
    day, so the caller heard Thursday over and over."""
    monkeypatch.setattr(calcom, "SLOTS_PER_DAY", 2)
    out = json.loads(await calcom.do_find_slots(KEY, "Asia/Karachi",
                                                "consultation"))
    days = {s["say"].split(",")[0] for s in out["slots"]}
    assert len(days) >= 2, "more than one day must be offered"
    assert len(out["slots"]) <= calcom.MAX_SLOTS
    per_day: dict = {}
    for s in out["slots"]:
        per_day.setdefault(s["say"].split(",")[0], []).append(s)
    assert all(len(v) <= 2 for v in per_day.values())
    assert "which day suits" in out["note"]


def test_spread_picks_across_the_list_not_off_the_front():
    day = [f"{h:02d}:00" for h in range(9, 17)]      # 8 items
    # Evenly spaced over eight items: first, middle-ish, last.
    assert _spread_len(day, 3) == ["09:00", "13:00", "16:00"]
    assert _spread_len(day, 2) == ["09:00", "16:00"]
    assert _spread_len(day, 1) == ["09:00"]
    assert _spread_len(day, 99) == day               # never invents
    assert _spread_len([], 3) == []
    assert _spread_len(["09:00"], 3) == ["09:00"]


def _spread_len(items, count):
    return calcom._spread(items, count)


async def test_a_wide_open_day_offers_morning_and_afternoon(fake_cal):
    """Regression, seen live: "On Friday you can book at 9 AM or 9:30 AM"
    on a day that was free until 5, because the slots arrive in order and
    the first two were handed over."""
    out = json.loads(await calcom.do_find_slots(
        KEY, "Asia/Karachi", "consultation", day=_d("2026-09-11")))
    said = [s["say"] for s in out["slots"]]
    assert len(said) == calcom.SLOTS_ONE_DAY
    assert said[0].endswith("9 AM"), "the earliest is still offered first"
    # 9:30 must NOT be the second option when the whole day is open.
    assert "9:30 AM" not in said[1]
    assert any("PM" in s for s in said), "an afternoon option is offered"
    assert "morning" in out["note"] and "afternoon" in out["note"]


async def test_several_days_are_each_spread_too(fake_cal):
    """The multi-day answer had the same fault one level down: three slots
    per day, but the three earliest of each day."""
    out = json.loads(await calcom.do_find_slots(KEY, "Asia/Karachi",
                                                "consultation"))
    # The default window starts today, so the day checked must be a full
    # future one: this used to name a fixed date and broke the morning that
    # date arrived.
    by_day: dict[str, list[str]] = {}
    for s in out["slots"]:
        by_day.setdefault(s["starts_at"][:10], []).append(s["say"])
    # A weekday: the fake's schedule is open Monday to Friday, and its
    # Saturday carries two hand-placed slots, not a spread.
    future = sorted(d for d in by_day if d > datetime.date.today().isoformat()
                    and datetime.date.fromisoformat(d).weekday() < 5)
    assert future, "the window offered no future weekday"
    first = by_day[future[0]]
    assert len(first) == calcom.SLOTS_PER_DAY
    assert any("PM" in s for s in first), "not just the start of the day"


async def test_the_next_opening_after_a_closed_day_is_not_two_early_slots(fake_cal):
    # Sunday the 13th is closed; Monday the 14th has slots in the fake.
    out = json.loads(await calcom.do_find_slots(
        KEY, "Asia/Karachi", "consultation", day=_d("2026-09-13")))
    assert out["closed"] is True
    said = [s["say"] for s in out["slots"]]
    assert said, "the next opening still gets offered"
    if len(said) > 1:
        assert said[0] != said[1]


async def test_a_closed_day_says_so_and_offers_the_next_opening(fake_cal):
    # 2026-09-13 is a Sunday; the schedule is Monday to Friday.
    out = json.loads(await calcom.do_find_slots(
        KEY, "Asia/Karachi", "consultation", day=_d("2026-09-13")))
    assert out["available"] is False and out["closed"] is True
    assert "closed on Sunday" in out["say"]
    assert "Monday" in out["say"], "name the next opening, do not go silent"


async def test_a_full_day_says_fully_booked_and_offers_nearby(fake_cal):
    # 2026-09-10 is a Thursday: open on the schedule, no slots in the fake.
    out = json.loads(await calcom.do_find_slots(
        KEY, "Asia/Karachi", "consultation", day=_d("2026-09-10")))
    assert out["available"] is False
    assert "fully booked" in out["say"]
    assert out["slots"], "the nearest free times still get offered"


async def test_a_time_without_a_day_is_refused(fake_cal):
    out = await calcom.do_find_slots(KEY, "Asia/Karachi", "consultation",
                                     preferred_time="15:00")
    assert "needs the day too" in out


async def test_a_found_booking_is_spoken_in_the_zone_it_was_booked_in(fake_cal):
    """Regression, seen live. The agent looked a booking up before it had
    asked which city the caller was in, so it passed UTC, and a 9:15 AM
    Karachi appointment was read back as "4:15 AM". A booking carries its
    own zone, so no caller zone is needed to describe it correctly."""
    out = json.loads(await calcom.do_find_bookings(KEY, "ali@example.com"))
    found = out["bookings"][0]
    assert found["timezone"] == "Asia/Karachi"
    assert "2 PM" in found["when"]          # 09:00 UTC in Karachi
    assert "9 AM" not in found["when"]      # never the raw UTC hour
    assert found["starts_at"] == _d("2026-09-12T09:00:00Z")


async def test_the_bookings_own_zone_wins_over_a_supplied_one(fake_cal):
    """Even when the caller's zone is known it is the wrong lens: the person
    booked at 2 PM their time and that is how they will refer to it."""
    out = json.loads(
        await calcom.do_find_bookings(KEY, "ali@example.com", "America/New_York")
    )
    assert out["bookings"][0]["timezone"] == "Asia/Karachi"
    assert "2 PM" in out["bookings"][0]["when"]


def test_booking_zone_falls_back_and_labels_utc():
    assert calcom.booking_zone(
        {"attendees": [{"timeZone": "Europe/Berlin"}]}) == "Europe/Berlin"
    assert calcom.booking_zone({"timeZone": "Asia/Tokyo"}) == "Asia/Tokyo"
    assert calcom.booking_zone({}, "Asia/Karachi") == "Asia/Karachi"
    assert calcom.booking_zone({}) == ""
    # With no zone anywhere, the hour is named as UTC so it cannot be
    # mistaken for the caller's local time.
    out = calcom._booking_out({"start": _d("2026-09-12T09:00:00Z")}, "")
    assert out["when"].endswith(" UTC") and out["timezone"] == "UTC"


async def test_find_slots_takes_the_type_from_a_booking_reference(fake_cal):
    """A4: a reschedule should not have to ask what the appointment is.
    The account offers two types, so with no name find_slots must ask; with
    the booking's reference it should work it out instead."""
    out = await calcom.do_find_slots(KEY, "Asia/Karachi", day=_d("2026-09-14"))
    assert "which one they mean" in out, "two types and no name: ask"

    body = json.loads(await calcom.do_find_slots(
        KEY, "Asia/Karachi", day=_d("2026-09-14"), reference="bk_abc123"))
    assert body["service"] == "30 Minute Consultation"
    assert body["slots"], "and it still returns times"

    # A name the model passes still wins over the reference.
    body = json.loads(await calcom.do_find_slots(
        KEY, "Asia/Karachi", "home visit", day=_d("2026-09-14"),
        reference="bk_abc123"))
    assert body["service"] == "Home Visit"

    # A reference that resolves to nothing asks, rather than picking one.
    out = await calcom.do_find_slots(KEY, "Asia/Karachi", day=_d("2026-09-14"),
                                     reference="bk_does_not_exist")
    assert "which one they mean" in out


def test_booking_service_reads_every_shape_the_listing_sends():
    by_id = {1: "30 Minute Consultation", 2: "Home Visit"}
    # 1. The nested title, when it is there.
    assert calcom.booking_service(
        {"eventType": {"title": "Follow-up Visit"}}) == "Follow-up Visit"
    # 2. An id, nested or flat, looked up in the service map. This is the
    #    shape the real API sent and the one that used to come back empty.
    assert calcom.booking_service({"eventType": {"id": 2}}, by_id) == "Home Visit"
    assert calcom.booking_service({"eventTypeId": 1}, by_id) == \
        "30 Minute Consultation"
    assert calcom.booking_service({"eventType": 2}, by_id) == "Home Visit"
    # 3. The booking title, with Cal.com's host-and-guest suffix stripped.
    assert calcom.booking_service(
        {"title": "New Patient Consultation between Docbot and Usama"}
    ) == "New Patient Consultation"
    assert calcom.booking_service({"title": "Home Visit"}) == "Home Visit"
    # 4. The slug, spoken rather than hyphenated.
    assert calcom.booking_service(
        {"eventType": {"slug": "follow-up-visit"}}) == "follow up visit"
    # Nothing to go on says nothing, rather than inventing a type.
    assert calcom.booking_service({}) == ""
    assert calcom.booking_service({}, by_id) == ""
    # An id the map does not know falls through instead of raising.
    assert calcom.booking_service({"eventTypeId": 99}, by_id) == ""


async def test_find_bookings_names_the_type_from_an_id_alone(fake_cal):
    """The regression, end to end: the seeded booking carries only
    eventTypeId, exactly as the live listing did. The agent must still be
    able to say WHAT the appointment is, not just when."""
    out = json.loads(await calcom.do_find_bookings(
        KEY, "ali@example.com", "Asia/Karachi"))
    got = out["bookings"][0]
    assert got["service"] == "30 Minute Consultation", \
        "resolved through the event type id, not a nested title"
    assert "2 PM" in got["when"]
    assert "appointment type" in out["note"]


async def test_find_bookings_still_names_the_type_without_the_service_list(
        fake_cal, monkeypatch):
    """The extra event-types call is a nicety, not a dependency. With it
    gone, the booking's own title still carries the type, which is what the
    live API showed: the same booking resolved correctly with and without
    the map. The caller must never lose the answer to one failed call."""
    async def boom(key):
        raise calcom.CalcomError("nope")
    monkeypatch.setattr(calcom, "_services", boom)
    out = json.loads(await calcom.do_find_bookings(
        KEY, "ali@example.com", "Asia/Karachi"))
    assert out["bookings"][0]["service"] == "30 Minute Consultation", \
        "resolved from the booking title, with no id lookup available"
    assert "2 PM" in out["bookings"][0]["when"]


def test_utc_offset_is_spoken_not_written():
    assert calcom._utc_offset("Asia/Karachi") == "UTC plus 5"
    assert calcom._utc_offset("UTC") == "UTC"
    # A half-hour zone reads as a clock, since "plus 5 30" is not a number.
    assert calcom._utc_offset("Asia/Kolkata") == "UTC plus 5:30"
    assert calcom._utc_offset("America/New_York").startswith("UTC minus ")
    # Nonsense in, nothing out: an unusable zone must not break an answer.
    assert calcom._utc_offset("Mars/Olympus") == ""
    assert calcom._utc_offset("") == ""


def test_zone_short_only_when_it_means_something():
    assert calcom._zone_short("Asia/Karachi") == "PKT"
    assert calcom._zone_short("Mars/Olympus") == ""
    # Zones whose %Z is numeric ("+04") say nothing a caller would know.
    assert calcom._zone_short("Asia/Dubai") in {"", "GST"}


async def test_business_hours_names_the_zone_it_means(fake_cal):
    """Seen live: "the clinic opens at 9 AM and closes at 5 PM in
    Islamabad". The hours belong to the business, not to the caller's city,
    so the zone has to be named."""
    out = json.loads(await calcom.do_business_hours(KEY))
    assert out["say"] == "Monday to Friday, 9 AM to 5 PM Karachi time, " \
        "which is UTC plus 5"
    assert out["utc_offset"] == "UTC plus 5"
    assert out["timezone_short"] == "PKT"
    assert out["timezone"] == "Asia/Karachi"


async def test_business_hours_names_both_zones_for_a_distant_caller(fake_cal):
    out = json.loads(await calcom.do_business_hours(KEY, "Europe/London"))
    # The business's own hours, named and offset, come first.
    assert out["say"].startswith(
        "Monday to Friday, 9 AM to 5 PM Karachi time, UTC plus 5, which is")
    # Then the same hours where the caller is: 9 to 5 Karachi is 5 AM to 1 PM.
    assert out["in_caller_time"] == "Monday to Friday, 5 AM to 1 PM"
    assert "where the caller is" in out["say"]


async def test_tools_refuse_politely_instead_of_raising(fake_cal):
    # No time zone: ask the caller, do not guess.
    out = await calcom.do_find_slots(KEY, "")
    assert "time zone" in out and "Cannot check availability" in out
    # A service that does not exist names what does.
    out = await calcom.do_find_slots(KEY, "Asia/Karachi", "dog grooming")
    assert "30 Minute Consultation" in out
    # Booking without a usable email refuses before it writes anything.
    out = await calcom.do_book(KEY, _d("2026-09-12T09:00:00Z"), "Ali",
                               "not-an-email", "Asia/Karachi")
    assert "Not booked" in out and "email" in out
    assert BOOKED == []
    # ... and without a name.
    out = await calcom.do_book(KEY, _d("2026-09-12T09:00:00Z"), " ",
                               "a@b.co", "Asia/Karachi")
    assert "name" in out and BOOKED == []
    # Rescheduling nothing.
    out = await calcom.do_reschedule(KEY, "", _d("2026-09-12T09:00:00Z"),
                                     "Asia/Karachi")
    assert "find_my_bookings" in out
    # An email with no bookings gets a next step, not an empty list.
    out = await calcom.do_find_bookings(KEY, "nobody@shop.test", "Asia/Karachi")
    assert "No upcoming appointments" in out
    # A vague time is refused rather than guessed at.
    out = await calcom.do_book(KEY, "next tuesday", "Ali", "a@b.co",
                               "Asia/Karachi")
    assert "ISO 8601" in out and BOOKED == []


async def test_cal_being_down_is_a_sentence_not_a_crash(monkeypatch):
    # Nothing listening: every tool still answers in words.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    monkeypatch.setattr(calcom, "API_BASE", f"http://127.0.0.1:{dead}/v2")
    assert "could not be reached" in await calcom.do_list_services(KEY)
    assert "Cannot check availability" in await calcom.do_find_slots(
        KEY, "Asia/Karachi")


async def test_tools_are_reachable_over_mcp(sidecar):
    """The adapter: the six tools are exposed at /calcom/mcp, they receive
    the key from the Authorization header, and a call round-trips."""
    from datetime import timedelta

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {KEY}"},
                          timeout=20) as http,
        streamable_http_client(
            url=f"http://127.0.0.1:{sidecar}/calcom/mcp", http_client=http
        ) as streams,
        ClientSession(streams[0], streams[1],
                      read_timeout_seconds=timedelta(seconds=20)) as session,
    ):
        await session.initialize()
        listed = (await session.list_tools()).tools
        assert [t.name for t in listed] == [
            "list_services", "business_hours", "find_slots",
            "book_appointment", "find_my_bookings", "reschedule_appointment",
            "cancel_appointment",
        ]
        # The docstring is the LLM's contract, so it must survive the wrapper.
        # Wrapped in the source, so compare on collapsed whitespace.
        doc = next(t for t in listed if t.name == "book_appointment").description
        flat = " ".join(doc.split())
        assert "Confirm the date, time, name and email out loud" in flat
        assert "Cal.com emails the confirmation itself" in flat
        res = await session.call_tool("list_services", {})
        assert "30 Minute Consultation" in res.content[0].text


async def test_a_missing_key_is_a_config_error_not_a_cal_error(sidecar):
    """No Authorization header at all: that is a misconfigured worker, and
    the tool says so rather than blaming Cal.com."""
    from datetime import timedelta

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx.AsyncClient(timeout=20) as http,
        streamable_http_client(
            url=f"http://127.0.0.1:{sidecar}/calcom/mcp", http_client=http
        ) as streams,
        ClientSession(streams[0], streams[1],
                      read_timeout_seconds=timedelta(seconds=20)) as session,
    ):
        await session.initialize()
        res = await session.call_tool("list_services", {})
        assert "no Cal.com API key" in res.content[0].text


async def test_check_key_validates_the_account(fake_cal):
    out = await calcom.check_key("cal_live_test")
    assert out["ok"] is True
    assert out["label"] == "owner@shop.test"
    assert "30 Minute Consultation" in out["services"]
    assert out["warnings"] == []


@pytest.mark.parametrize(("key", "because"), [
    ("", "no API key"),
    ("sk-not-a-cal-key", "starts with cal_"),
])
async def test_check_key_rejects_obvious_nonsense(key, because):
    out = await calcom.check_key(key)
    assert out["ok"] is False and because in out["error"]


async def test_check_key_reports_a_rejected_key(fake_cal, monkeypatch):
    monkeypatch.setattr(calcom, "API_BASE", fake_cal + "/unauth")
    out = await calcom.check_key("cal_live_test")
    assert out["ok"] is False and "rejected the API key" in out["error"]


@pytest.mark.skipif(not os.environ.get("ARYA_CALCOM_KEY"),
                    reason="set ARYA_CALCOM_KEY to test the real Cal.com")
async def test_against_the_real_calcom():
    out = await calcom.check_key(os.environ["ARYA_CALCOM_KEY"])
    assert out["ok"] is True, out.get("error")
    print("real Cal.com account:", out["label"], "services:", out["services"])


def test_slot_window_default_is_two_weeks():
    # A caller asking "when are you free" should not get a year of slots.
    assert calcom.SLOT_WINDOW_DAYS == 14
    assert calcom.MAX_SLOTS <= 12
    assert isinstance(datetime.timedelta(days=calcom.SLOT_WINDOW_DAYS),
                      datetime.timedelta)
