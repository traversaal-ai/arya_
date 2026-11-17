"""Cal.com booking tools: real appointments for a voice agent.

Why Cal.com rather than a raw calendar: a calendar knows what is busy, it
does not know that this business takes 30 minute consultations on weekdays
between 9 and 5 with a 15 minute buffer. Cal.com owns those rules, syncs to
the owner's Google or Outlook calendar, and emails the confirmation itself,
so the agent only has to ask, offer, and book.

Runs in the arya-mcp sidecar because Cal.com's own MCP server is a stdio
process that takes one API key from its environment: it could not serve
several saved connections, and it would put Node in the image. This module
takes the key per request from the `Authorization` header instead, so one
process serves every connection and stores nothing.

Voice shape, which is most of the design here:
  - tools take a SERVICE NAME, never an event type id, and resolve it here,
    because a model juggling numeric ids in speech gets them wrong,
  - times are ISO 8601 and every tool demands the caller's time zone,
    since "3pm" is meaningless without it,
  - a booking's reference comes back for the model to hold but never to say
    (the prompt block says so, and the voice rules already forbid reading
    identifiers aloud),
  - results are short spoken sentences, not JSON, wherever a sentence is
    enough.

The API shapes below come from Cal.com's public examples: their docs and
OpenAPI spec both sit behind Cloudflare, so the parsing is deliberately
defensive (envelopes, missing fields, both slot shapes) and the check route
surfaces a mismatch the moment a key is saved rather than mid-call.
"""

import asyncio
import contextlib
import datetime
import difflib
import json
import logging
import re
import time

import httpx
from mcp.server.fastmcp import Context, FastMCP

logger = logging.getLogger("mcp.calcom")

API_BASE = "https://api.cal.com/v2"
# Cal.com versions each ROUTE separately by date header, and a version the
# route does not serve makes it 404 "Cannot GET /v2/..." rather than
# complaining about the version. That reads exactly like a missing endpoint,
# so these three constants are load-bearing: verified against the live API
# on 2026-09-09 by probing with a deliberately invalid key, where 401 means
# the route exists and 404 means this version does not serve it.
#   /event-types  2024-06-14 works, 2024-08-13 and 2024-09-04 both 404
#   /bookings     2024-08-13
#   /slots        2024-09-04
#   /schedules    2024-06-11 works, 2024-08-13 404s
V_BOOKINGS = "2024-08-13"
V_SLOTS = "2024-09-04"
V_EVENT_TYPES = "2024-06-14"
V_SCHEDULES = "2024-06-11"

# Timeout layering, and the order matters. Creating a booking makes Cal.com
# send emails and mint a video room, so it is far slower than a read. Seen
# live: a booking took longer than the WORKER's 8 s per-tool budget, the
# worker reported failure, Cal.com finished anyway, the caller said "try
# again" and got a second appointment. So: this timeout (sidecar to Cal.com)
# must always be SHORTER than the worker's (connectors.TOOL_TIMEOUT_S, and
# `tool_timeout_s` on the catalog entry), or a readable error never gets the
# chance to come back before the worker gives up.
TIMEOUT_S = 10.0        # reads
WRITE_TIMEOUT_S = 15.0  # book, reschedule, cancel
MAX_SLOTS = 12          # spoken: nobody wants 40 times read out
MAX_BOOKINGS = 10
SLOT_WINDOW_DAYS = 14   # how far ahead "when are you free" looks by default
SLOT_DAYS_OFFERED = 5   # how many separate days to spread those slots over
SLOTS_PER_DAY = 3       # per day when several days are offered at once
SLOTS_ONE_DAY = 4       # when the caller has already named the day
SCHEDULE_TTL_S = 300.0  # the working hours change rarely; C asks often


class CalcomError(Exception):
    """A readable reason, safe to speak."""


# ─── HTTP ───────────────────────────────────────────────────────────────────

def _key_from(ctx: Context) -> str:
    request = getattr(ctx.request_context, "request", None)
    raw = ""
    if request is not None:
        with contextlib.suppress(Exception):
            raw = (request.headers.get("authorization") or "").strip()
    key = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    if not key:
        raise CalcomError("no Cal.com API key was supplied with this request")
    return key


async def _call(key: str, method: str, path: str, *, version: str,
                params: dict | None = None, body: dict | None = None,
                timeout: float | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {key}",
        "cal-api-version": version,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout or TIMEOUT_S) as client:
            r = await client.request(
                method, f"{API_BASE}{path}", headers=headers,
                params=params, json=body,
            )
    except httpx.TimeoutException:
        raise CalcomError("Cal.com did not answer in time") from None
    except httpx.HTTPError:
        raise CalcomError("Cal.com could not be reached") from None
    if r.status_code in (401, 403):
        raise CalcomError("Cal.com rejected the API key")
    if r.status_code == 404:
        raise CalcomError("Cal.com could not find that")
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if r.status_code >= 400:
        raise CalcomError(_error_text(payload, r.status_code))
    # v2 wraps everything as {"status": "success", "data": ...}
    if isinstance(payload, dict) and "data" in payload:
        data = payload["data"]
        return data if isinstance(data, dict) else {"items": data}
    return payload if isinstance(payload, dict) else {"items": payload}


_UNREACHABLE = ("did not answer in time", "could not be reached")


def _was_unreachable(e: BaseException) -> bool:
    """Did the request maybe-succeed? A timeout or a dropped connection on a
    WRITE leaves the outcome unknown, which is the only case where the caller
    must reconcile with Cal.com rather than believe the error."""
    return isinstance(e, CalcomError) and any(
        marker in str(e) for marker in _UNREACHABLE
    )


def _error_text(payload, status: int) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("details")
            if msg:
                return str(msg)[:200]
        for k in ("message", "detail"):
            if payload.get(k):
                return str(payload[k])[:200]
    return f"Cal.com answered http {status}"


# ─── times, spoken and parsed ───────────────────────────────────────────────

def parse_when(value: str) -> datetime.datetime:
    """ISO 8601 in, aware UTC datetime out. The prompt block gives the model
    today's date and tells it to convert, so anything vaguer than ISO is a
    prompt problem, not something to guess at here."""
    text = (value or "").strip()
    if not text:
        raise CalcomError("no date or time was given")
    cleaned = text.replace("Z", "+00:00").replace(" ", "T", 1) if " " in text else text.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(cleaned)
    except ValueError:
        raise CalcomError(
            f"'{text}' is not a date I can use; give an ISO 8601 time"
            " like 2026-09-12T15:00:00"
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _speak_time(iso: str, tz: str) -> str:
    """A slot as a person would say it, in the CALLER's zone."""
    try:
        dt = parse_when(iso)
        zone = _zone(tz)
        if zone is not None:
            dt = dt.astimezone(zone)
        hour = dt.strftime("%I").lstrip("0") or "12"
        minute = f":{dt.strftime('%M')}" if dt.minute else ""
        return f"{dt.strftime('%A')} {dt.strftime('%B')} {dt.day}, {hour}{minute} {dt.strftime('%p')}"
    except Exception:
        return iso


def _zone(tz: str):
    if not tz:
        return None
    with contextlib.suppress(Exception):
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz)
    return None


def _check_tz(tz: str) -> str:
    tz = (tz or "").strip()
    if not tz:
        raise CalcomError(
            "ask the caller which time zone they are in first, then pass it"
            " (for example Asia/Karachi)"
        )
    if _zone(tz) is None:
        raise CalcomError(
            f"'{tz}' is not a time zone I recognise; use an IANA name like"
            " Asia/Karachi or America/New_York"
        )
    return tz


# ─── services (Cal.com event types) ─────────────────────────────────────────

async def _services(key: str) -> list[dict]:
    """The bookable services on this account, as {id, name, minutes, slug}.

    An API key alone does not always scope /event-types, so fall back to
    looking up the account's username and asking again by name.
    """
    data = await _call(key, "GET", "/event-types", version=V_EVENT_TYPES)
    items = _items(data)
    if not items:
        username = (await _me(key)).get("username")
        if username:
            data = await _call(key, "GET", "/event-types",
                               version=V_EVENT_TYPES,
                               params={"username": username})
            items = _items(data)
    out = []
    for e in items:
        if not isinstance(e, dict) or e.get("id") is None:
            continue
        # Hidden event types are off the owner's public booking
        # page, but an owner's API key still returns them. Hidden
        # means "do not offer this to the public", and a phone
        # caller IS the public, so the agent must not offer it.
        if e.get("hidden"):
            continue
        out.append({
            "id": e["id"],
            "name": e.get("title") or e.get("slug") or f"service {e['id']}",
            "minutes": e.get("lengthInMinutes") or e.get("length"),
            "slug": e.get("slug") or "",
        })
    return out


def _items(data: dict) -> list:
    if isinstance(data, dict):
        for k in ("items", "eventTypes", "data", "bookings"):
            v = data.get(k)
            if isinstance(v, list):
                return v
        if data.get("id") is not None:
            return [data]
    return data if isinstance(data, list) else []


async def _me(key: str) -> dict:
    with contextlib.suppress(CalcomError):
        return await _call(key, "GET", "/me", version=V_BOOKINGS)
    return {}


# Words that carry no meaning when a caller names a service, so they are
# ignored when looking for a shared word ("a checkup please" -> "checkup").
_SAID_FILLER = frozenset([
    "the", "and", "for", "please", "want", "need", "book", "booking", "make",
    "made", "get", "have", "would", "like", "could", "can", "appointment",
    "slot", "session", "time",
])


def _norm(text: str) -> str:
    """Letters and digits only, lowercased: "check up" and "Checkup" and
    "check-up" all become the same thing, because callers say all three."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _words(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) > 2 and w not in _SAID_FILLER
    }


def _ask_which(options: list[dict]) -> str:
    names = ", ".join(s["name"] for s in options[:5])
    return f"ask the caller which one they mean: {names}"


def match_service(services: list[dict], wanted: str) -> dict:
    """Resolve what the caller said to exactly one service.

    Four passes, narrowing: an exact name or URL slug, then one containing
    the other ("consultation" for "30 Minute Consultation"), then a shared
    distinctive word ("a checkup please"), then near-spelling for a mumbled
    name. All spacing- and case-insensitive, because a caller says "check
    up" for "Checkup".

    Ambiguity ASKS rather than guessing. With "Regular Checkup" and
    "Specialist Checkup" both on the account, a caller saying "checkup"
    matches both, and picking the shorter one silently would book the wrong
    appointment while sounding perfectly confident. With one service on the
    account there is nothing to get wrong, so take it.
    """
    if not services:
        raise CalcomError(
            "this Cal.com account has no bookable services set up yet"
        )
    wanted = (wanted or "").strip()
    if not wanted:
        if len(services) == 1:
            return services[0]
        raise CalcomError(_ask_which(services))
    low = wanted.lower()
    tight = _norm(wanted)
    if tight:
        for s in services:
            if _norm(s["name"]) == tight or _norm(s["slug"]) == tight:
                return s
        contained = [
            s for s in services
            if tight in _norm(s["name"]) or _norm(s["name"]) in tight
        ]
        if len(contained) == 1:
            return contained[0]
        if len(contained) > 1:
            raise CalcomError(_ask_which(contained))
    said = _words(wanted)
    if said:
        shared = [s for s in services if said & _words(s["name"])]
        if len(shared) == 1:
            return shared[0]
        if len(shared) > 1:
            raise CalcomError(_ask_which(shared))
    close = difflib.get_close_matches(
        low, [s["name"].lower() for s in services], n=1, cutoff=0.6
    )
    if close:
        return next(s for s in services if s["name"].lower() == close[0])
    if len(services) == 1:
        return services[0]
    names = ", ".join(s["name"] for s in services[:5])
    raise CalcomError(
        f"no service called '{wanted}'. What is offered: {names}"
    )


# ─── bookings ───────────────────────────────────────────────────────────────

_EMAILISH = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _check_email(email: str) -> str:
    email = (email or "").strip()
    if not _EMAILISH.match(email):
        raise CalcomError(
            "a valid email address is needed to book, so the confirmation"
            " can be sent; read it back to the caller to be sure"
        )
    return email


def booking_zone(b: dict, fallback: str = "") -> str:
    """The time zone an appointment was BOOKED in, which is how the person
    who booked it thinks of the time.

    Seen live: the agent looked a booking up before it had asked which city
    the caller was in, so it passed UTC, and a 9:15 AM Karachi appointment
    was read back as "4:15 AM". The booking carries its own zone, so it
    never has to be told.
    """
    attendees = b.get("attendees")
    if isinstance(attendees, list):
        for a in attendees:
            if isinstance(a, dict) and a.get("timeZone"):
                return a["timeZone"]
    for key in ("timeZone", "timezone"):
        if b.get(key):
            return b[key]
    return fallback


def booking_service(b: dict, services: dict | None = None) -> str:
    """Which appointment type a booking is for.

    Four routes, because the listing does not always carry the same one.
    Seen live: the agent could say a caller had appointments at 10 and 3 but
    not what they were FOR, because only `eventType.title` was read and the
    listing had sent `eventTypeId` instead. `services` maps event type id to
    name and is the reliable route whenever it is available.
    """
    kind = b.get("eventType")
    if isinstance(kind, dict):
        if kind.get("title"):
            return str(kind["title"])
        if services and kind.get("id") in services:
            return services[kind["id"]]
    for field in ("eventTypeId", "eventType"):
        ident = b.get(field)
        if services and isinstance(ident, int) and ident in services:
            return services[ident]
    # Cal.com titles a booking "<type> between <host> and <guest>"; the part
    # before "between" is the type, and the rest is noise to a caller.
    title = str(b.get("title") or "")
    if title:
        return re.split(r"\s+between\s+", title, maxsplit=1)[0].strip()
    if isinstance(kind, dict) and kind.get("slug"):
        return str(kind["slug"]).replace("-", " ")
    return ""


def _booking_out(b: dict, tz: str, services: dict | None = None) -> dict:
    when = b.get("start") or b.get("startTime") or ""
    zone = booking_zone(b, tz)
    return {
        "reference": b.get("uid") or b.get("id") or "",
        "service": booking_service(b, services),
        # Named so the model cannot mistake a UTC time for a local one when
        # neither the booking nor the caller told us a zone.
        "when": _speak_time(when, zone) if zone else _speak_time(when, "UTC") + " UTC",
        "timezone": zone or "UTC",
        "starts_at": when,
        "status": b.get("status") or "",
    }


async def _bookings_for(key: str, email: str) -> list[dict]:
    data = await _call(key, "GET", "/bookings", version=V_BOOKINGS,
                       params={"attendeeEmail": email, "status": "upcoming"})
    return [b for b in _items(data) if isinstance(b, dict)]


async def existing_booking(key: str, email: str,
                           start_utc: datetime.datetime) -> dict | None:
    """An upcoming booking for this email at this exact start, or None.

    This is what makes booking safe to retry. Cal.com has no idempotency
    key, so the only way to avoid a double booking after an unclear failure
    is to go and look.
    """
    with contextlib.suppress(Exception):
        for b in await _bookings_for(key, email):
            when = b.get("start") or b.get("startTime") or ""
            with contextlib.suppress(CalcomError):
                if parse_when(when).replace(second=0, microsecond=0) == \
                        start_utc.replace(second=0, microsecond=0):
                    return b
    return None


async def _booking_by_uid(key: str, uid: str) -> dict | None:
    with contextlib.suppress(Exception):
        data = await _call(key, "GET", f"/bookings/{uid}", version=V_BOOKINGS)
        if isinstance(data, dict) and (data.get("uid") or data.get("start")):
            return data
    return None


# ─── working hours (the owner's Cal.com schedule) ───────────────────────────

_DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
              "Saturday", "Sunday"]
_schedules: dict[str, tuple[float, dict]] = {}


def _say_clock(hhmm: str) -> str:
    """"09:00" -> "9 AM", "17:30" -> "5:30 PM"."""
    try:
        h, _, m = hhmm.partition(":")
        hour, minute = int(h), int((m or "0")[:2])
    except ValueError:
        return hhmm
    suffix = "AM" if hour < 12 else "PM"
    twelve = hour % 12 or 12
    return f"{twelve}:{minute:02d} {suffix}" if minute else f"{twelve} {suffix}"


def _day_runs(days: list[str]) -> str:
    """["Monday","Tuesday","Wednesday","Thursday","Friday"] ->
    "Monday to Friday". Nobody says five day names out loud."""
    idx = sorted({_DAY_ORDER.index(d) for d in days if d in _DAY_ORDER})
    if not idx:
        return ""
    runs, start, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i == prev + 1:
            prev = i
            continue
        runs.append((start, prev))
        start = prev = i
    runs.append((start, prev))
    parts = [
        _DAY_ORDER[a] if a == b
        else f"{_DAY_ORDER[a]} and {_DAY_ORDER[b]}" if b == a + 1
        else f"{_DAY_ORDER[a]} to {_DAY_ORDER[b]}"
        for a, b in runs
    ]
    return ", ".join(parts)


def parse_schedule(data: dict) -> dict:
    """Cal.com's default schedule -> {timezone, blocks: [{days, start, end}]}.

    Parsed defensively: the availability entries have shifted shape across
    Cal.com versions, and a working-hours answer is not worth a crash.
    """
    zone = data.get("timeZone") or data.get("timezone") or ""
    raw = data.get("availability")
    if isinstance(raw, dict):
        raw = [raw]
    blocks = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        days = item.get("days") or item.get("daysOfWeek") or []
        if isinstance(days, str):
            days = [days]
        named = [str(d).strip().title() for d in days
                 if str(d).strip().title() in _DAY_ORDER]
        start = str(item.get("startTime") or item.get("start") or "")[:5]
        end = str(item.get("endTime") or item.get("end") or "")[:5]
        if named and start and end:
            blocks.append({"days": named, "start": start, "end": end})
    return {"timezone": zone, "blocks": blocks}


async def schedule_for(key: str) -> dict:
    """The account's default schedule, cached briefly: the offering logic in
    do_find_slots consults it on every empty day."""
    now = time.monotonic()
    hit = _schedules.get(key)
    if hit and now - hit[0] < SCHEDULE_TTL_S:
        return hit[1]
    parsed = parse_schedule(
        await _call(key, "GET", "/schedules/default", version=V_SCHEDULES)
    )
    _schedules[key] = (now, parsed)
    return parsed


def open_weekdays(schedule: dict) -> set[int]:
    """Weekday numbers (Monday is 0) the business is open, for working out
    which day to offer next."""
    return {
        _DAY_ORDER.index(d)
        for block in schedule.get("blocks") or []
        for d in block["days"]
    }


# ─── what the tools actually do ─────────────────────────────────────────────
# Split from the MCP wrappers below so the behaviour is testable by calling
# a function with a key, instead of standing up a server and a session per
# assertion. The wrappers are the adapter: pull the key off the request,
# delegate, and carry the docstring the LLM reads.


def _failed(what: str, e: BaseException, tool: str) -> str:
    """Every tool answers with a sentence, never an exception: a raised
    error reaches the model as an opaque ToolError, and the caller hears
    dead air instead of "I cannot check that right now"."""
    if isinstance(e, CalcomError):
        return f"{what}: {e}."
    logger.warning(f"{tool} failed: {e!r}")
    return f"{what}: the booking system did not respond."


async def do_list_services(key: str) -> str:
    try:
        services = await _services(key)
    except Exception as e:
        return _failed("Cannot list the services", e, "list_services")
    if not services:
        return "No bookable services are set up on this account yet."
    return json.dumps(
        {"services": [{"name": s["name"], "minutes": s["minutes"]}
                       for s in services]},
        separators=(",", ":"),
    )


async def do_business_hours(key: str, timezone: str = "") -> str:
    try:
        schedule = await schedule_for(key)
    except Exception as e:
        return _failed("Cannot check the opening hours", e, "business_hours")
    blocks = schedule.get("blocks") or []
    if not blocks:
        return (
            "No working hours are set on this account, so I cannot say when"
            " it is open. Offer to check for a free time instead."
        )
    own = schedule.get("timezone") or "UTC"
    said = "; ".join(
        f"{_day_runs(b['days'])}, {_say_clock(b['start'])} to {_say_clock(b['end'])}"
        for b in blocks if _day_runs(b["days"])
    )
    offset = _utc_offset(own)
    here = f"{_zone_label(own)} time"
    out = {"hours": said, "timezone": own,
           "timezone_said": here,
           "say": f"{said} {here}" + (f", which is {offset}" if offset else "")}
    short = _zone_short(own)
    if short:
        # For "what's that in PKT?": the model has the abbreviation without
        # having to spell it out in every answer, which is a mouthful aloud.
        out["timezone_short"] = short
    if offset:
        out["utc_offset"] = offset
    caller = (timezone or "").strip()
    if caller and _zone(caller) is not None and caller != own:
        shifted = [
            f"{_day_runs(b['days'])}, "
            f"{_shift_clock(b['start'], own, caller)} to "
            f"{_shift_clock(b['end'], own, caller)}"
            for b in blocks if _day_runs(b["days"])
        ]
        out["in_caller_time"] = "; ".join(shifted)
        # Both zones NAMED, because the hours belong to the business and the
        # conversion belongs to the caller. Live, the agent said "9 AM to
        # 5 PM in Islamabad", which would have been false for a caller in
        # London hearing the same sentence.
        out["say"] = (
            f"{said} {here}"
            + (f", {offset}" if offset else "")
            + f", which is {'; '.join(shifted)} where the caller is"
        )
    return json.dumps(out, separators=(",", ":"))


def _zone_label(zone: str) -> str:
    """"Asia/Karachi" -> "Karachi": a city, which is how people say it."""
    return (zone or "").split("/")[-1].replace("_", " ") or "local"


def _zone_short(zone: str) -> str:
    """"Asia/Karachi" -> "PKT", when the zone has a real abbreviation.

    Some zones report a numeric name like "+05" from %Z, which says nothing
    a caller would recognise, so those come back empty.
    """
    tz = _zone(zone)
    if tz is None:
        return ""
    with contextlib.suppress(Exception):
        name = datetime.datetime.now(tz).strftime("%Z")
        if name and name.replace(" ", "").isalpha():
            return name
    return ""


def _utc_offset(zone: str) -> str:
    """"Asia/Karachi" -> "UTC plus 5", spoken rather than written.

    "+05:00" is not something a voice agent can say, and "9 to 5" with no
    zone at all is ambiguous to a caller in another country.
    """
    tz = _zone(zone)
    if tz is None:
        return ""
    with contextlib.suppress(Exception):
        offset = datetime.datetime.now(tz).utcoffset()
        if offset is None:
            return ""
        total = round(offset.total_seconds() / 60)
        if total == 0:
            return "UTC"
        hours, minutes = divmod(abs(total), 60)
        sign = "plus" if total > 0 else "minus"
        clock = f"{hours}:{minutes:02d}" if minutes else str(hours)
        return f"UTC {sign} {clock}"
    return ""


def _shift_clock(hhmm: str, from_zone: str, to_zone: str) -> str:
    """A wall-clock time moved between zones, for "9 to 5 Karachi time is
    5 AM to 1 PM where you are"."""
    src, dst = _zone(from_zone), _zone(to_zone)
    if src is None or dst is None:
        return _say_clock(hhmm)
    try:
        h, _, m = hhmm.partition(":")
        today = datetime.datetime.now(src).date()
        moment = datetime.datetime(
            today.year, today.month, today.day, int(h), int((m or "0")[:2]),
            tzinfo=src,
        ).astimezone(dst)
        return _say_clock(moment.strftime("%H:%M"))
    except (ValueError, OverflowError):
        return _say_clock(hhmm)


def _day_bounds(day: str, tz: str) -> tuple[datetime.datetime, datetime.datetime]:
    """A calendar date in the CALLER's zone -> the UTC window Cal.com wants.
    The model gives a date rather than computing two timestamps, which is
    one less place for it to be wrong by five hours."""
    try:
        date = datetime.date.fromisoformat(day.strip())
    except ValueError:
        raise CalcomError(
            f"'{day}' is not a date I can use; give it as 2026-09-12"
        ) from None
    zone = _zone(tz) or datetime.timezone.utc
    start = datetime.datetime.combine(date, datetime.time(0, 0), tzinfo=zone)
    return (start.astimezone(datetime.timezone.utc),
            (start + datetime.timedelta(days=1)).astimezone(datetime.timezone.utc))


def _by_local_day(starts: list[str], tz: str) -> dict:
    zone = _zone(tz) or datetime.timezone.utc
    grouped: dict[datetime.date, list[str]] = {}
    for s in starts:
        with contextlib.suppress(CalcomError):
            grouped.setdefault(parse_when(s).astimezone(zone).date(), []).append(s)
    return grouped


def _spread(items: list[str], count: int) -> list[str]:
    """`count` items spaced ACROSS the list, not the first `count` of it.

    Free times come back in order, so handing over the head of the list made
    the agent say "9 AM or 9:30 AM" on a day that was open until 5. Evenly
    spaced gives it a morning and an afternoon to offer instead.
    """
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[0]]
    step = (len(items) - 1) / (count - 1)
    picked, seen = [], set()
    for i in range(count):
        idx = round(i * step)
        if idx not in seen:
            seen.add(idx)
            picked.append(items[idx])
    return picked


def _nearest(starts: list[str], target: datetime.datetime, count: int = 2) -> list[str]:
    scored = []
    for s in starts:
        with contextlib.suppress(CalcomError):
            scored.append((abs((parse_when(s) - target).total_seconds()), s))
    return [s for _, s in sorted(scored)[:count]]


async def _slots_between(key: str, event_type_id, tz: str,
                         start: datetime.datetime,
                         end: datetime.datetime) -> list[str]:
    data = await _call(
        key, "GET", "/slots", version=V_SLOTS,
        params={"eventTypeId": event_type_id, "start": _z(start),
                "end": _z(end), "timeZone": tz},
    )
    return _slot_starts(data)


async def _why_empty(key: str, chosen: dict, tz: str, day: str,
                     bounds: tuple) -> str:
    """A day with nothing free must say WHICH kind of nothing: closed, or
    booked out. Silence sent the caller away for no reason."""
    zone = _zone(tz) or datetime.timezone.utc
    date = bounds[0].astimezone(zone).date()
    closed_msg = ""
    with contextlib.suppress(Exception):
        schedule = await schedule_for(key)
        openings = open_weekdays(schedule)
        if openings and date.weekday() not in openings:
            nxt = next(
                (date + datetime.timedelta(days=i) for i in range(1, 8)
                 if (date + datetime.timedelta(days=i)).weekday() in openings),
                None,
            )
            closed_msg = (
                f"We are closed on {date.strftime('%A')}s."
            )
            if nxt is not None:
                first = await _slots_between(
                    key, chosen["id"], tz,
                    *_day_bounds(nxt.isoformat(), tz),
                )
                if first:
                    return json.dumps({
                        "available": False,
                        "closed": True,
                        "say": f"{closed_msg} The next opening is"
                               f" {_speak_time(first[0], tz)}",
                        # The earliest, plus something later that day, so the
                        # caller is not offered 9 and 9:30 as the only choice.
                        "slots": [{"say": _speak_time(s, tz), "starts_at": s}
                                  for s in _spread(sorted(first), 2)],
                    }, separators=(",", ":"))
                return json.dumps({
                    "available": False, "closed": True,
                    "say": f"{closed_msg} The next open day is"
                           f" {nxt.strftime('%A')}, but nothing is free.",
                }, separators=(",", ":"))
    # Open but nothing left: look either side of the day for the nearest.
    around = []
    with contextlib.suppress(Exception):
        around = await _slots_between(
            key, chosen["id"], tz,
            bounds[0] - datetime.timedelta(days=3),
            bounds[1] + datetime.timedelta(days=3),
        )
    if around:
        return json.dumps({
            "available": False,
            "say": f"{closed_msg or f'{date.strftime(chr(37) + chr(65))} is fully booked.'}"
                   " The nearest free times are these",
            "slots": [{"say": _speak_time(s, tz), "starts_at": s}
                      for s in _nearest(around, bounds[0], 2)],
        }, separators=(",", ":"))
    return (
        f"Nothing is free on {date.strftime('%A %B %d')} for a"
        f" {chosen['name']}. Offer to look at a different week."
    )


async def _service_wanted(key: str, services: list[dict], service: str,
                          reference: str) -> str:
    """What appointment type to look for: what the model said, or failing
    that, whatever the referenced booking is already for.

    A reschedule already knows the booking, so "is this a follow-up or a new
    patient consultation?" is a question the system can answer itself. Seen
    live, and it made the agent sound like it had forgotten the appointment
    it had just read out. A name the model passes still wins, and a
    reference that resolves to nothing degrades to asking rather than to a
    confident wrong answer.
    """
    said = (service or "").strip()
    if said or not (reference or "").strip():
        return said
    booked = await _booking_by_uid(key, reference.strip())
    if booked is None:
        return said
    return booking_service(booked, {s["id"]: s["name"] for s in services})


async def do_find_slots(key: str, timezone: str, service: str = "",
                        day: str = "", preferred_time: str = "",
                        earliest: str = "", latest: str = "",
                        reference: str = "") -> str:
    try:
        tz = _check_tz(timezone)
        services = await _services(key)
        chosen = match_service(
            services, await _service_wanted(key, services, service, reference)
        )
        wanted_day = (day or "").strip()
        asked_for = (preferred_time or "").strip()
        if asked_for and not wanted_day:
            raise CalcomError(
                "a specific time needs the day too; pass day as 2026-09-12"
            )
        if wanted_day:
            bounds = _day_bounds(wanted_day, tz)
        elif earliest or latest:
            start = parse_when(earliest) if earliest else datetime.datetime.now(
                datetime.timezone.utc)
            end = parse_when(latest) if latest else start + datetime.timedelta(
                days=SLOT_WINDOW_DAYS)
            bounds = (start, end if end > start else start + datetime.timedelta(days=1))
        else:
            now = datetime.datetime.now(datetime.timezone.utc)
            bounds = (now, now + datetime.timedelta(days=SLOT_WINDOW_DAYS))
        starts = await _slots_between(key, chosen["id"], tz, *bounds)
    except Exception as e:
        return _failed("Cannot check availability", e, "find_slots")

    head = {"service": chosen["name"], "minutes": chosen["minutes"],
            "timezone": tz}

    # A named time is a yes or no question, not a list to search.
    if asked_for:
        try:
            target = parse_when(f"{wanted_day}T{asked_for}").replace(tzinfo=None)
            zone = _zone(tz) or datetime.timezone.utc
            target = target.replace(tzinfo=zone).astimezone(datetime.timezone.utc)
        except CalcomError:
            return _failed(
                "Cannot check availability",
                CalcomError(f"'{asked_for}' is not a time I can use; give it as 15:00"),
                "find_slots",
            )
        exact = next(
            (s for s in starts
             if abs((parse_when(s) - target).total_seconds()) < 60), None
        )
        if exact:
            return json.dumps({
                **head, "available": True, "starts_at": exact,
                "say": _speak_time(exact, tz),
                "note": "confirm this time and move on to their name and email",
            }, separators=(",", ":"))
        near = _nearest(starts, target, 2)
        return json.dumps({
            **head, "available": False,
            "requested": _speak_time(_z(target), tz),
            "slots": [{"say": _speak_time(s, tz), "starts_at": s} for s in near],
            "note": "say that time is taken and offer these two",
        }, separators=(",", ":"))

    if not starts:
        if wanted_day:
            return await _why_empty(key, chosen, tz, wanted_day, bounds)
        return (
            f"No free {chosen['name']} slots in the next two weeks. Offer to"
            " look further ahead."
        )

    # Spread, twice over: across days when no day was named, and across the
    # hours of each day either way. Chronological order alone made the agent
    # offer the two earliest times on every open day.
    if not wanted_day:
        grouped = _by_local_day(starts, tz)
        picked = []
        for date in sorted(grouped)[:SLOT_DAYS_OFFERED]:
            picked.extend(_spread(sorted(grouped[date]), SLOTS_PER_DAY))
        starts = picked
        head["note"] = ("offer two of these on different days and ask which"
                        " day suits; never read the whole list")
    else:
        head["day"] = wanted_day
        starts = _spread(sorted(starts), SLOTS_ONE_DAY)
        head["note"] = ("offer two of these, one in the morning and one in"
                        " the afternoon; never read the whole list")

    return json.dumps({
        **head,
        "slots": [{"say": _speak_time(s, tz), "starts_at": s}
                  for s in starts[:MAX_SLOTS]],
    }, separators=(",", ":"))


async def do_book(key: str, starts_at: str, name: str, email: str,
                  timezone: str, service: str = "", notes: str = "") -> str:
    try:
        # Local checks first, cheapest and most specific: a nonsense time or
        # a missing email should not cost a round trip to Cal.com, and the
        # complaint the model hears should be about what it actually got
        # wrong rather than about the next thing down the list.
        tz = _check_tz(timezone)
        who = (name or "").strip()
        if not who:
            raise CalcomError("ask the caller for their name first")
        addr = _check_email(email)
        moment = parse_when(starts_at)
        when = _z(moment)
        chosen = match_service(await _services(key), service)
        # Already there? Then the caller (or a retry after an unclear
        # failure) is asking for something that exists. Cal.com has no
        # idempotency key, so looking is the only way not to double-book.
        found = await existing_booking(key, addr, moment)
        if found is not None:
            return _already(found, chosen, tz)
        body = {
            "start": when,
            "eventTypeId": chosen["id"],
            "attendee": {"name": who, "email": addr, "timeZone": tz},
        }
        if (notes or "").strip():
            body["bookingFieldsResponses"] = {"notes": notes.strip()}
        try:
            data = await _call(key, "POST", "/bookings", version=V_BOOKINGS,
                               body=body, timeout=WRITE_TIMEOUT_S)
        except CalcomError as e:
            # The outcome is unknown, not failed: creating a booking sends
            # emails and mints a video room, so it can land after we stop
            # waiting. Go and see before telling the caller anything.
            if not _was_unreachable(e):
                raise
            logger.warning(f"book timed out, reconciling: {e}")
            landed = await existing_booking(key, addr, moment)
            if landed is None:
                raise
            return _already(landed, chosen, tz)
    except Exception as e:
        return _failed("Not booked", e, "book_appointment")
    out = _booking_out(data if isinstance(data, dict) else {}, tz)
    return json.dumps({
        "booked": True,
        "service": chosen["name"],
        "when": out["when"] or _speak_time(body["start"], tz),
        "reference": out["reference"],
        "note": (
            "confirm the day and time out loud and say the confirmation"
            " email is on its way; never read the reference aloud"
        ),
    }, separators=(",", ":"))


def _already(booking: dict, chosen: dict, tz: str) -> str:
    out = _booking_out(booking, tz)
    return json.dumps({
        "booked": True,
        "already": True,
        "service": out["service"] or chosen["name"],
        "when": out["when"],
        "reference": out["reference"],
        "note": (
            "this appointment already exists: confirm it to the caller as"
            " booked and do NOT book again"
        ),
    }, separators=(",", ":"))


async def do_find_bookings(key: str, email: str, timezone: str = "") -> str:
    try:
        # Optional on purpose: this is the one tool the agent may reach for
        # before it has asked where the caller is, and each appointment is
        # then described in the zone it was booked in.
        tz = _check_tz(timezone) if (timezone or "").strip() else ""
        addr = _check_email(email)
        data = await _call(key, "GET", "/bookings", version=V_BOOKINGS,
                           params={"attendeeEmail": addr, "status": "upcoming"})
    except Exception as e:
        return _failed("Cannot look that up", e, "find_my_bookings")
    rows = [b for b in _items(data) if isinstance(b, dict)][:MAX_BOOKINGS]
    if not rows:
        return (
            "No upcoming appointments under that email. Offer to check a"
            " different email address, or to book a new appointment."
        )
    # The appointment TYPE is what a caller identifies a booking by ("move
    # my follow-up"), so it is worth one extra call to be able to name it.
    by_id: dict = {}
    with contextlib.suppress(Exception):
        by_id = {s["id"]: s["name"] for s in await _services(key)}
    return json.dumps({
        "bookings": [_booking_out(b, tz, by_id) for b in rows],
        "note": ("name the appointment type and the time when you describe"
                 " one; never read a reference aloud"),
    }, separators=(",", ":"))


async def do_reschedule(key: str, reference: str, starts_at: str,
                        timezone: str, reason: str = "") -> str:
    try:
        tz = _check_tz(timezone)
        uid = (reference or "").strip()
        if not uid:
            raise CalcomError("find the appointment first with find_my_bookings")
        moment = parse_when(starts_at)
        body = {"start": _z(moment)}  # parsed before any call
        if (reason or "").strip():
            body["reschedulingReason"] = reason.strip()
        try:
            data = await _call(key, "POST", f"/bookings/{uid}/reschedule",
                               version=V_BOOKINGS, body=body,
                               timeout=WRITE_TIMEOUT_S)
        except CalcomError as e:
            # Same unknown-outcome problem as booking. Cal.com issues a NEW
            # uid on reschedule, so check the appointment's real start time
            # rather than assuming either way.
            if not _was_unreachable(e):
                raise
            logger.warning(f"reschedule timed out, reconciling: {e}")
            now = await _booking_by_uid(key, uid)
            moved = now is not None and (
                parse_when(now.get("start") or "").replace(second=0, microsecond=0)
                == moment.replace(second=0, microsecond=0)
            )
            if not moved:
                raise
            data = now
    except Exception as e:
        return _failed("Not rescheduled", e, "reschedule_appointment")
    out = _booking_out(data if isinstance(data, dict) else {}, tz)
    return json.dumps({
        "rescheduled": True,
        "when": out["when"] or _speak_time(body["start"], tz),
        "reference": out["reference"] or uid,
        "note": "confirm the new day and time out loud",
    }, separators=(",", ":"))


async def do_cancel(key: str, reference: str, reason: str = "") -> str:
    try:
        uid = (reference or "").strip()
        if not uid:
            raise CalcomError("find the appointment first with find_my_bookings")
        body = {"cancellationReason": reason.strip()} if (reason or "").strip() else {}
        try:
            await _call(key, "POST", f"/bookings/{uid}/cancel",
                        version=V_BOOKINGS, body=body,
                        timeout=WRITE_TIMEOUT_S)
        except CalcomError as e:
            if not _was_unreachable(e):
                raise
            logger.warning(f"cancel timed out, reconciling: {e}")
            now = await _booking_by_uid(key, uid)
            if now is None or str(now.get("status", "")).lower() != "cancelled":
                raise
    except Exception as e:
        return _failed("Not cancelled", e, "cancel_appointment")
    return (
        "The appointment is cancelled. Say so plainly and offer to book"
        " another time."
    )


def _z(dt: datetime.datetime) -> str:
    """Cal.com wants Z-suffixed UTC, not +00:00."""
    return dt.isoformat().replace("+00:00", "Z")


# ─── the MCP surface ────────────────────────────────────────────────────────

def build(security=None) -> FastMCP:
    """The FastMCP app for Cal.com. These wrappers are deliberately thin:
    the docstrings ARE the contract the model reads, so they say what to
    ask the caller for and in what order to act."""
    app = FastMCP("arya-calcom", stateless_http=True, transport_security=security)

    @app.tool()
    async def list_services(ctx: Context) -> str:
        """List the appointment types this business offers, with how long
        each one takes. Use it when the caller is vague about what they want.
        """
        return await do_list_services(_key_from(ctx))

    @app.tool()
    async def business_hours(ctx: Context, timezone: str = "") -> str:
        """Say when the business is open. Use this when the caller asks
        about opening hours or which days you work.

        Do NOT use it to find a free appointment: it only reports the
        working hours, and find_slots is what knows what is actually free.

        Args:
            timezone: Optional. The caller's IANA time zone, so the hours
                can also be given in their local time.
        """
        return await do_business_hours(_key_from(ctx), timezone)

    @app.tool()
    async def find_slots(ctx: Context, timezone: str, service: str = "",
                         day: str = "", preferred_time: str = "",
                         earliest: str = "", latest: str = "",
                         reference: str = "") -> str:
        """Find free appointment times, honouring the business's working
        hours, buffers and existing bookings. Call this BEFORE offering any
        time, and never invent one.

        Three ways to ask, in order of preference:
        - the caller named a day AND a time: pass `day` and
          `preferred_time`, and you get a straight yes or no plus the
          nearest alternatives,
        - the caller named a day: pass `day`, and you get that day's free
          times, or an explanation if it is closed or fully booked,
        - the caller is vague: pass neither, and you get a few times spread
          across the next open days. Offer two and ask which day suits.

        Args:
            timezone: The CALLER's IANA time zone, e.g. Asia/Karachi. Ask
                for it if you do not know it.
            service: Which appointment type, by name. Omit if the business
                only offers one.
            day: The date the caller wants, as 2026-09-12, in their own
                time zone. Prefer this over earliest and latest.
            preferred_time: A specific time on that day, as 15:00, in the
                caller's time zone. Requires `day`.
            earliest: Rarely needed. ISO 8601 start of a custom window.
            latest: Rarely needed. ISO 8601 end of a custom window.
            reference: When moving an existing appointment, the `reference`
                from find_my_bookings. The appointment type is then taken
                from that booking, so do NOT ask the caller what type it
                is. Leave `service` empty when you pass this.
        """
        return await do_find_slots(_key_from(ctx), timezone, service, day,
                                   preferred_time, earliest, latest,
                                   reference)

    @app.tool()
    async def book_appointment(ctx: Context, starts_at: str, name: str,
                               email: str, timezone: str, service: str = "",
                               notes: str = "") -> str:
        """Book one appointment. Confirm the date, time, name and email out
        loud with the caller BEFORE calling this. Cal.com emails the
        confirmation itself.

        Safe to call once. If the answer comes back with `already` set, the
        appointment exists: confirm it to the caller and do not call this
        again, whatever the caller says.

        Args:
            starts_at: The exact `starts_at` value from find_slots.
            name: The caller's full name.
            email: The caller's email address, read back to them first.
            timezone: The caller's IANA time zone.
            service: Appointment type by name. Omit if there is only one.
            notes: Anything the caller wants the business to know.
        """
        return await do_book(_key_from(ctx), starts_at, name, email,
                             timezone, service, notes)

    @app.tool()
    async def find_my_bookings(ctx: Context, email: str,
                               timezone: str = "") -> str:
        """Look up the caller's existing appointments by their email, so one
        can be moved or cancelled. Ask for the email they booked with.

        Each appointment comes back with the time already worded for
        speaking, so read the `when` field out as it is given and do not
        convert it yourself.

        Args:
            email: The email the appointment was booked with.
            timezone: Optional. The caller's IANA time zone if you already
                know it. Leave it out otherwise: each appointment is
                described in the time zone it was booked in.
        """
        return await do_find_bookings(_key_from(ctx), email, timezone)

    @app.tool()
    async def reschedule_appointment(ctx: Context, reference: str,
                                     starts_at: str, timezone: str,
                                     reason: str = "") -> str:
        """Move an existing appointment to a new time. Get the reference
        from find_my_bookings and a free time from find_slots first, and
        confirm the new time out loud before calling this.

        Args:
            reference: The `reference` from find_my_bookings. Never say it aloud.
            starts_at: The new `starts_at` from find_slots.
            timezone: The caller's IANA time zone.
            reason: Why it is moving, if the caller says.
        """
        return await do_reschedule(_key_from(ctx), reference, starts_at,
                                   timezone, reason)

    @app.tool()
    async def cancel_appointment(ctx: Context, reference: str,
                                 reason: str = "") -> str:
        """Cancel an existing appointment. Get the reference from
        find_my_bookings and confirm WHICH appointment out loud before
        calling this: a cancellation cannot be undone.

        Args:
            reference: The `reference` from find_my_bookings. Never say it aloud.
            reason: Why it is being cancelled, if the caller says.
        """
        return await do_cancel(_key_from(ctx), reference, reason)

    return app


def _slot_starts(data) -> list[str]:
    """Cal.com has shipped slots as a date-keyed object and as a flat list;
    accept both, and both {"start": ...} items and bare strings."""
    found: list[str] = []

    def take(item):
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            v = item.get("start") or item.get("time") or item.get("startTime")
            if isinstance(v, str):
                found.append(v)

    payload = data.get("slots", data) if isinstance(data, dict) else data
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                for item in value:
                    take(item)
            else:
                take(value)
    elif isinstance(payload, list):
        for item in payload:
            take(item)
    return sorted(set(found))


# ─── operational check (used by the API when a connection is saved) ─────────

async def check_key(key: str) -> dict:
    """Is this API key real, and does the account have anything bookable?
    Listing MCP tools cannot tell us either, so the API calls this before
    it stores the connection."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "no API key was supplied"}
    if not key.startswith("cal_"):
        return {
            "ok": False,
            "error": "a Cal.com API key starts with cal_ (create one under"
                     " Settings, Developer, API keys)",
        }
    # /me first: it is the one route that answers 401 for a bad key, so a
    # wrong key reads as "rejected" instead of as some downstream 404.
    try:
        me = await asyncio.wait_for(
            _call(key, "GET", "/me", version=V_BOOKINGS), TIMEOUT_S + 5
        )
    except CalcomError as e:
        if "rejected" in str(e):
            return {"ok": False, "error": str(e)}
        me = {}  # this key type may not have /me; the services call decides
    except Exception as e:
        logger.warning(f"calcom check failed: {e!r}")
        return {"ok": False, "error": "Cal.com could not be reached"}
    try:
        services = await asyncio.wait_for(_services(key), TIMEOUT_S + 5)
    except CalcomError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"calcom check failed: {e!r}")
        return {"ok": False, "error": "Cal.com could not be reached"}
    label = me.get("email") or me.get("username") or "Cal.com account"
    warnings = []
    if not services:
        warnings.append(
            "this account has no event types yet, so there is nothing to"
            " book: create one in Cal.com first"
        )
    return {
        "ok": True,
        "label": label,
        "services": [s["name"] for s in services],
        "warnings": warnings,
    }
