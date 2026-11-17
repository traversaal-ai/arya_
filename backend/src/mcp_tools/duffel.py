"""Duffel flights and hotels: real, bookable inventory for a voice agent.

Why Duffel: one API and one key cover flights on 300 plus airlines and hotel
rooms, under Duffel's own accreditation, so the business needs none. A test
token (duffel_test_...) books for free on the sandbox airline Duffel Airways
and on Duffel's test properties; a live token issues real tickets against a
funded Duffel balance. Nothing here changes between the two except what the
spoken result admits to.

Runs in the arya-mcp sidecar because there is no official Duffel MCP server,
and the community ones are stdio processes taking one token from their
environment: single tenant, flights only, raw JSON. This module takes the
token per request from the `Authorization` header, so one process serves
every saved connection and stores no credential.

Voice shape, which is most of the design:
  - tools take CITY or airport names and resolve them here through Duffel's
    own place suggestions, so the model never converts London to LHR,
  - searches return numbered options and the module keeps the offers under
    a short trip id it mints (in memory, 20 minutes), so the model passes
    "option two" back rather than an offer id it could mangle,
  - a round trip is ONE Duffel offer with two slices, so one option carries
    both legs and one order books both,
  - every result is a spoken sentence (`say`) plus a machine block the model
    is told not to read, and an `itinerary` payload the worker turns into
    the written trip page (connectors.spoken_result),
  - money is spoken as "240 pounds" or "240 pounds 50", never "GBP 240.50",
    and times as "9:15 in the morning" in the airport's local time.

Gates live in code, not the prompt: a booking with a missing traveller field
is refused with the list of what is missing, a fare is re-priced before every
order and a changed price is reported instead of booked, a fare that needs
passport details is refused outright, and one conversation cannot book the
same fare twice (checked against Duffel's own order list through the
`metadata` we stamp, not against memory alone).

API shapes follow Duffel's public docs (v2, 2026-09). Requests and responses
are wrapped in {"data": ...}. Bookings are paid from the Duffel Balance,
never by card: a card number read out on a call is not something this
product will ever take.
"""

import asyncio
import contextlib
import datetime
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

import day_plan
import display_fmt as disp

logger = logging.getLogger("mcp.duffel")

# Told to the connector probe, which outranks the name heuristic: so
# plan_activity is a read even though it changes the written itinerary (it
# changes nothing at Duffel), and the four writes get the badge, start
# unticked, and carry the duplicate-call confirmation.
_READ = ToolAnnotations(readOnlyHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

API_BASE = "https://api.duffel.com"
VERSION = "v2"
CONVERSATION_HEADER = "X-Arya-Conversation"
# Duffel switches products on per account. Stays (hotels) is OFF on a new
# account and answers 403 with a plain sentence pointing at sales, which
# is not a bad token and must never be reported as one.
NOT_ENABLED = "that part of Duffel is not enabled on this account"
HOTEL_TOOLS = ("search_hotels", "hotel_rates", "book_hotel", "cancel_hotel")

# Timeout layering, same rule as calcom.py: every timeout here must be shorter
# than the worker's per-tool budget (`tool_timeout_s: 30` on the catalog
# entry) or a readable error never gets back before the worker gives up.
TIMEOUT_S = 12.0          # places, offer refresh, rates, quotes, lists
SEARCH_TIMEOUT_S = 25.0   # offer requests and stays searches fan out to suppliers
WRITE_TIMEOUT_S = 20.0    # orders, bookings, cancellations
# How long Duffel itself waits on airlines before answering with what it
# has. 15 s was the whole of a caller's patience and long enough for a
# stray "sure" to cancel the turn; 8 s still returned the sandbox fares.
SUPPLIER_TIMEOUT_MS = 8000

MAX_OPTIONS = 3           # spoken: three is what a caller can hold
MAX_RATES = 4
TRIP_TTL_S = 20 * 60
LIST_LIMIT = 50           # orders and bookings scanned for "my bookings"

# Duffel's test properties exist only here, only in test mode.
TEST_HOTEL_LAT, TEST_HOTEL_LNG, TEST_HOTEL_RADIUS_KM = -24.38, -128.32, 2
DEFAULT_RADIUS_KM = 5

CABINS = {"economy", "premium_economy", "business", "first"}
GENDERS = {"m", "f"}
TITLES = {"mr", "ms", "mrs", "miss", "dr"}

# Test hook: tests install an httpx.MockTransport here.
_TRANSPORT: httpx.AsyncBaseTransport | None = None


class DuffelError(Exception):
    """A readable reason, safe to speak."""


# ─── request plumbing ───────────────────────────────────────────────────────

def is_test_token(key: str) -> bool:
    return (key or "").startswith("duffel_test_")


def mode_of(key: str) -> str:
    return "test" if is_test_token(key) else "live"


def _key_from(ctx: Context) -> str:
    request = getattr(ctx.request_context, "request", None)
    raw = ""
    if request is not None:
        with contextlib.suppress(Exception):
            raw = (request.headers.get("authorization") or "").strip()
    key = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    if not key:
        raise DuffelError("no Duffel access token was supplied with this request")
    return key


def _conversation_from(ctx: Context) -> str:
    """The worker stamps the call's conversation id on every request so an
    order can be tied to the call that made it (duplicate guard, metadata).
    Absent in tests and console runs; "none" then."""
    request = getattr(ctx.request_context, "request", None)
    if request is not None:
        with contextlib.suppress(Exception):
            return (request.headers.get(CONVERSATION_HEADER) or "").strip() or "none"
    return "none"


async def _call(key: str, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None, timeout: float | None = None):
    headers = {
        "Authorization": f"Bearer {key}",
        "Duffel-Version": VERSION,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout or TIMEOUT_S,
                                     transport=_TRANSPORT) as client:
            r = await client.request(
                method, f"{API_BASE}{path}", headers=headers, params=params,
                json={"data": body} if body is not None else None,
            )
    except httpx.TimeoutException:
        raise DuffelError("Duffel did not answer in time") from None
    except httpx.HTTPError:
        raise DuffelError("Duffel could not be reached") from None
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if r.status_code in (401, 403):
        msg = _error_text(payload, r.status_code) if payload else r.text.strip()[:240]
        if "not enabled" in msg.lower():
            raise DuffelError(f"{NOT_ENABLED}: {msg}")
        raise DuffelError("Duffel rejected the access token")
    if r.status_code == 404:
        raise DuffelError("Duffel could not find that")
    if r.status_code == 429:
        raise DuffelError("Duffel is rate limiting requests, try again in a moment")
    if r.status_code >= 400:
        raise DuffelError(_error_text(payload, r.status_code))
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _error_text(payload, status: int) -> str:
    """Duffel: {"errors": [{"title", "message", "code", "source": {...}}]}.
    The message names the field, which is what a retry needs."""
    if isinstance(payload, dict):
        errs = payload.get("errors")
        if isinstance(errs, list) and errs:
            bits = []
            for e in errs[:2]:
                if isinstance(e, dict):
                    msg = e.get("message") or e.get("title") or ""
                    ptr = ((e.get("source") or {}).get("pointer") or "").strip("/")
                    bits.append(f"{msg} ({ptr})" if ptr and ptr not in msg else msg)
            text = "; ".join(b for b in bits if b)
            if text:
                return text[:240]
    return f"Duffel answered http {status}"


_UNREACHABLE = ("did not answer in time", "could not be reached")


def _was_unreachable(e: BaseException) -> bool:
    return isinstance(e, DuffelError) and any(m in str(e) for m in _UNREACHABLE)


# ─── spoken formatting ──────────────────────────────────────────────────────

_CURRENCY_WORDS = {
    "GBP": ("pound", "pounds"), "USD": ("dollar", "dollars"),
    "EUR": ("euro", "euros"), "CAD": ("Canadian dollar", "Canadian dollars"),
    "AUD": ("Australian dollar", "Australian dollars"),
    "AED": ("dirham", "dirhams"), "PKR": ("rupee", "rupees"),
    "INR": ("rupee", "rupees"), "SAR": ("riyal", "riyals"),
}


def money_number(text) -> float | None:
    try:
        return float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return None


def say_money(amount, currency: str) -> str:
    """"240 pounds", "240 pounds 50", "1 pound". Unknown currencies keep the
    code after the number, which is still better than a symbol."""
    value = money_number(amount)
    if value is None:
        return "an unknown amount"
    whole = int(value)
    cents = round((value - whole) * 100)
    if cents == 100:
        whole, cents = whole + 1, 0
    one, many = _CURRENCY_WORDS.get((currency or "").upper(), (currency, currency))
    unit = one if whole == 1 else many
    head = f"{whole:,} {unit}".strip()
    return f"{head} {cents:02d}".rstrip() if cents else head


def say_money_rounded(amount, currency: str) -> str:
    """For fares read out in a list: "about 138 dollars". Cents are noise
    when a caller is comparing three options by ear; they stay exact on the
    cards, the page, the email, and for anything actually charged."""
    value = money_number(amount)
    if value is None:
        return "an unknown amount"
    whole = round(value)
    exact = abs(value - whole) < 0.005
    one, many = _CURRENCY_WORDS.get((currency or "").upper(), (currency, currency))
    head = f"{whole:,} {one if whole == 1 else many}".strip()
    return head if exact else f"about {head}"


def _gflights_url(dep: str, arr: str, when: str) -> str:
    """A real Google Flights results page for the route and date, for the
    written itinerary. Duffel offers have no public page of their own."""
    if not (dep and arr):
        return ""
    return "https://www.google.com/travel/flights?q=" + quote_plus(
        f"flights from {dep} to {arr} on {when}")


def _maps_url(query: str) -> str:
    query = " ".join((query or "").split())
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}" if query else ""


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def say_day(value: str) -> str:
    """"Friday the 12th of September"."""
    try:
        d = datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{d.strftime('%A')} the {_ordinal(d.day)} of {d.strftime('%B')}"


def say_clock(iso_local: str) -> str:
    """Local wall time as a person says it: "9:15 in the morning", "noon",
    "2 in the afternoon", "7:30 in the evening", "11:40 at night"."""
    try:
        t = datetime.datetime.fromisoformat(str(iso_local).replace("Z", "+00:00"))
    except ValueError:
        return str(iso_local)
    h, m = t.hour, t.minute
    if h == 0 and m == 0:
        return "midnight"
    if h == 12 and m == 0:
        return "noon"
    part = ("in the morning" if h < 12 else "in the afternoon" if h < 17
            else "in the evening" if h < 21 else "at night")
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {part}" if m else f"{h12} {part}"


_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?")


def say_duration(iso: str) -> str:
    """"PT2H35M" -> "2 hours 35 minutes"."""
    m = _DUR.fullmatch(str(iso or ""))
    if not m:
        return ""
    days, hours, mins = (int(x) if x else 0 for x in m.groups())
    hours += days * 24
    bits = []
    if hours:
        bits.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins:
        bits.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return " ".join(bits) or "under a minute"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


# ─── validation ─────────────────────────────────────────────────────────────

_EMAILISH = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _check_date(value: str, what: str, *, not_before: str | None = None) -> str:
    value = (value or "").strip()[:10]
    try:
        d = datetime.date.fromisoformat(value)
    except ValueError:
        raise DuffelError(f"the {what} must be a full date like 2026-09-12") from None
    if d < datetime.date.today():
        raise DuffelError(f"the {what} {say_day(value)} has already passed")
    if not_before and d < datetime.date.fromisoformat(not_before):
        raise DuffelError(f"the {what} cannot be before {say_day(not_before)}")
    return value


def _check_email(email: str) -> str:
    email = (email or "").strip()
    if not _EMAILISH.match(email):
        raise DuffelError(
            "a valid email address is needed, the confirmation goes there;"
            " read it back to the caller to be sure"
        )
    return email


def _check_phone(phone: str) -> str:
    """E.164. Spaces, dots and dashes are dropped, a leading 00 becomes +,
    and a bare national number is refused rather than guessed: a wrong
    country code silently sends the airline's texts to a stranger."""
    raw = (phone or "").strip()
    digits = re.sub(r"[\s().-]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not re.fullmatch(r"\+[1-9]\d{7,14}", digits):
        raise DuffelError(
            "the phone number needs the country code, like +44 7700 900123;"
            " ask the caller which country it is"
        )
    return digits


def _check_traveller(t: dict, n: int, *, full: bool) -> dict[str, Any]:
    """Flights need every field; hotels only the names. Missing pieces are
    named so the agent asks for exactly those."""
    t = t if isinstance(t, dict) else {}
    out = {
        "given_name": (t.get("given_name") or t.get("first_name") or "").strip(),
        "family_name": (t.get("family_name") or t.get("last_name") or "").strip(),
    }
    missing = [k for k in ("given_name", "family_name") if not out[k]]
    if full:
        born = (t.get("born_on") or t.get("date_of_birth") or "").strip()[:10]
        try:
            d = datetime.date.fromisoformat(born)
            if d >= datetime.date.today():
                raise ValueError
            out["born_on"] = born
        except ValueError:
            missing.append("born_on (a date like 1980-07-24)")
        title = (t.get("title") or "").strip().lower().rstrip(".")
        if title in TITLES:
            out["title"] = title
        else:
            missing.append("title (mr, ms, mrs, miss or dr)")
        # The title settles the gender the airline wants, so the caller is
        # never asked it separately (live: "male" came through as "meal"
        # twice). Only Dr leaves it open.
        gender = (t.get("gender") or "").strip().lower()[:1]
        if gender not in GENDERS:
            gender = {"mr": "m", "ms": "f", "mrs": "f", "miss": "f"}.get(title, "")
        if gender in GENDERS:
            out["gender"] = gender
        else:
            missing.append("gender (m or f; the title Dr does not settle it)")
    if missing:
        who = "the traveller" if n == 1 else f"traveller {n}"
        raise DuffelError(f"I still need {who}'s " + ", ".join(missing).replace(
            "given_name", "first name").replace("family_name", "last name"))
    return out


# ─── trip cache ─────────────────────────────────────────────────────────────

@dataclass
class Trip:
    id: str
    mode: str
    created: float = field(default_factory=time.monotonic)
    conversation: str = "none"
    # flights
    origin: dict | None = None
    destination: dict | None = None
    depart: str = ""
    ret: str = ""
    adults: int = 1
    cabin: str = "economy"
    passenger_ids: list = field(default_factory=list)
    options: list = field(default_factory=list)   # spoken-order flight options
    flight_order: dict | None = None
    # hotels
    city: dict | None = None
    check_in: str = ""
    check_out: str = ""
    rooms: int = 1
    guests: int = 2
    hotel_options: list = field(default_factory=list)
    hotel_rates: list = field(default_factory=list)
    hotel_choice: int = 0
    hotel_booking: dict | None = None
    # things to do
    plan: list = field(default_factory=list)      # [{day, title, note, url}]

    def touch(self) -> None:
        self.created = time.monotonic()


_TRIPS: dict[str, Trip] = {}
_CANCELS: dict[str, dict] = {}   # order id -> pending cancellation quote


def _gc() -> None:
    now = time.monotonic()
    for tid in [t for t, trip in _TRIPS.items() if now - trip.created > TRIP_TTL_S]:
        _TRIPS.pop(tid, None)


def _new_trip(mode: str, conversation: str) -> Trip:
    _gc()
    trip = Trip(id=secrets.token_hex(3), mode=mode, conversation=conversation)
    _TRIPS[trip.id] = trip
    return trip


def _trip(trip_id: str, key: str, conversation: str, *, create: bool = False) -> Trip:
    _gc()
    trip = _TRIPS.get((trip_id or "").strip())
    if trip is None:
        if create:
            return _new_trip(mode_of(key), conversation)
        raise DuffelError(
            "that search has expired, so run the search again before choosing"
            " an option; do not tell the caller anything was booked"
        )
    # A trip belongs to the call that made it. A mistyped id must never land
    # on another caller's cached search, let alone their booking.
    if "none" not in (trip.conversation, conversation) and trip.conversation != conversation:
        if create:
            return _new_trip(mode_of(key), conversation)
        raise DuffelError("that trip id is not from this call; run the search again")
    trip.touch()
    return trip


def _option(items: list, option, what: str) -> dict[str, Any]:
    try:
        n = int(option)
    except (TypeError, ValueError):
        raise DuffelError(f"say which {what} by its number") from None
    if not items:
        raise DuffelError(f"no {what}s have been found yet on this search")
    if not 1 <= n <= len(items):
        raise DuffelError(f"there is no {what} {n}; the choices are 1 to {len(items)}")
    return items[n - 1]


# ─── places ─────────────────────────────────────────────────────────────────

_IATA = re.compile(r"^[A-Za-z]{3}$")


async def resolve_place(key: str, text: str, what: str) -> dict[str, Any]:
    """A city or airport name to Duffel's place: {kind, iata, name, city,
    lat, lng}. A city with several airports resolves to the CITY code (LON,
    NYC): Duffel searches all its airports at once, which beats asking the
    caller which airport before they have seen a single fare. Each option
    then names the airport it actually uses."""
    q = (text or "").strip()
    if not q:
        raise DuffelError(f"ask the caller for the {what}")
    try:
        items = await _call(key, "GET", "/places/suggestions", params={"query": q})
    except DuffelError as e:
        if "could not find" in str(e):
            items = []
        else:
            raise
    items = items if isinstance(items, list) else []
    if not items:
        raise DuffelError(f"I could not find a place called {q}; ask the caller"
                          f" to say the {what} city again")
    upper = q.upper()
    exact = [p for p in items if _IATA.match(q) and (p.get("iata_code") or "").upper() == upper]
    cities = [p for p in items if p.get("type") == "city"]
    airports = [p for p in items if p.get("type") == "airport"]
    pick = (exact or cities or airports or items)[0]
    return {
        "kind": pick.get("type") or "place",
        "iata": pick.get("iata_code") or pick.get("iata_city_code") or "",
        "name": pick.get("name") or q,
        "city": pick.get("city_name") or pick.get("name") or q,
        "lat": pick.get("latitude"),
        "lng": pick.get("longitude"),
    }


# ─── flights ────────────────────────────────────────────────────────────────

def _leg(slice_: dict) -> dict[str, Any]:
    segs = [s for s in (slice_.get("segments") or []) if isinstance(s, dict)]
    if not segs:
        return {}
    first, last = segs[0], segs[-1]
    via = [(s.get("destination") or {}).get("city_name") or (s.get("destination") or {}).get("name")
           for s in segs[:-1]]
    return {
        "from_name": (first.get("origin") or {}).get("name") or "",
        "from_code": (first.get("origin") or {}).get("iata_code") or "",
        "to_name": (last.get("destination") or {}).get("name") or "",
        "to_code": (last.get("destination") or {}).get("iata_code") or "",
        "depart": first.get("departing_at") or "",
        "arrive": last.get("arriving_at") or "",
        "stops": len(segs) - 1,
        "via": [v for v in via if v],
        "duration": slice_.get("duration") or "",
        # Who the caller actually boards. The offer's owner sells the fare;
        # a return can be flown by a partner, and callers ask.
        "carrier": (first.get("marketing_carrier") or {}).get("name") or "",
        "flights": [
            f"{(s.get('marketing_carrier') or {}).get('iata_code', '')}"
            f"{s.get('marketing_carrier_flight_number', '')}" for s in segs
        ],
    }


def _say_leg(leg: dict) -> str:
    stops = ("nonstop" if leg["stops"] == 0 else
             f"{_plural(leg['stops'], 'stop')}" + (f" in {', '.join(leg['via'])}" if leg["via"] else ""))
    dur = say_duration(leg["duration"])
    return (f"leaves {leg['from_name']} at {say_clock(leg['depart'])} on {say_day(leg['depart'])}"
            f" and lands in {leg['to_name']} at {say_clock(leg['arrive'])}, {stops}"
            + (f", {dur}" if dur else ""))


def _option_from_offer(o: dict, adults: int) -> dict[str, Any]:
    legs = [_leg(s) for s in (o.get("slices") or []) if isinstance(s, dict)]
    total = money_number(o.get("total_amount")) or 0.0
    conds = o.get("conditions") or {}
    return {
        "offer_id": o.get("id"),
        "airline": (o.get("owner") or {}).get("name") or "the airline",
        "airline_code": (o.get("owner") or {}).get("iata_code") or "",
        "legs": legs,
        "total": f"{total:.2f}",
        "per_person": f"{total / max(adults, 1):.2f}",
        "currency": o.get("total_currency") or "",
        "expires_at": o.get("expires_at") or "",
        "docs_required": bool(o.get("passenger_identity_documents_required")),
        "refundable": bool((conds.get("refund_before_departure") or {}).get("allowed")),
        "changeable": bool((conds.get("change_before_departure") or {}).get("allowed")),
        "bags": [
            {"type": b.get("type"), "quantity": b.get("quantity")}
            for p in (o.get("passengers") or [])[:1]
            for b in (p.get("baggages") or []) if isinstance(b, dict)
        ],
        "fare": ((o.get("slices") or [{}])[0].get("fare_brand_name") or ""),
    }


def _say_leg_short(leg: dict, *, back: bool) -> str:
    """"out at 9:15 in the morning landing 11:50 in the morning"; the
    return names its day, the outbound date was confirmed seconds ago."""
    when = ""
    if back and len(leg.get("depart") or "") >= 10:
        when = f" on the {_ordinal(int(leg['depart'][8:10]))}"
    return (f"{'back' if back else 'out'}{when} at {say_clock(leg['depart'])}"
            f" landing {say_clock(leg['arrive'])}")


def _say_stops(legs: list[dict]) -> str:
    if all(leg["stops"] == 0 for leg in legs):
        return "nonstop"
    bits = []
    for leg, label in zip(legs, ("on the way out", "on the way back"), strict=False):
        tail = f" {label}" if len(legs) > 1 else ""
        if leg["stops"]:
            via = f" in {', '.join(leg['via'])}" if leg["via"] else ""
            bits.append(f"{_plural(leg['stops'], 'stop')}{via}{tail}")
        else:
            bits.append(f"nonstop{tail}")
    return ", ".join(bits)


def _airline_phrase(opt: dict) -> str:
    """"Iberia both ways", or "out with Iberia, back with British Airways":
    the caller's first question after hearing an option was whether the
    return is on the same airline."""
    carriers = [leg.get("carrier") or opt["airline"] for leg in opt["legs"]]
    if len(carriers) <= 1:
        return carriers[0] if carriers else opt["airline"]
    if len(set(carriers)) == 1:
        return f"{carriers[0]} both ways"
    return f"out with {carriers[0]}, back with {carriers[1]}"


def _say_option(n: int, opt: dict, adults: int) -> str:
    """One breath per option. Durations and bags wait for flight_details;
    the first version read both legs with dates and lasted twenty seconds
    per option, which is when callers start talking over the agent."""
    if not opt["legs"]:
        body = "details unavailable"
    else:
        first = opt["legs"][0]
        legs = ", ".join(_say_leg_short(leg, back=i > 0) for i, leg in enumerate(opt["legs"]))
        body = (f"from {first['from_name']} to {first['to_name']}, {legs},"
                f" {_say_stops(opt['legs'])}")
    price = say_money_rounded(opt["total"], opt["currency"])
    if adults > 1:
        price = (f"{say_money_rounded(opt['per_person'], opt['currency'])} per person,"
                 f" {price} in total")
    return f"Option {n}: {_airline_phrase(opt)}, {body}, {price}."


def _summary_flights(picked: list[dict]) -> str:
    """One clause for the default bubble: a price range and the cheapest
    airline, not every option. The full per-option lines still exist in
    `say` for the model to read out if the caller actually asks to hear
    them (see do_search_flights); the caller looks at the cards for the
    rest."""
    priced = [(money_number(o["total"]), o) for o in picked]
    priced = [(p, o) for p, o in priced if p is not None]
    if not priced:
        return "at a price I could not read"
    priced.sort(key=lambda t: t[0])
    lo_amt, cheapest = priced[0]
    hi_amt = priced[-1][0]
    currency = cheapest["currency"]
    lo, hi = say_money_rounded(lo_amt, currency), say_money_rounded(hi_amt, currency)
    price_bit = lo if lo == hi else f"from {lo} to {hi}"
    return f"{price_bit}, the cheapest is {_airline_phrase(cheapest)}"


def _card_for_option(i: int, opt: dict) -> dict:
    """Both legs and the route on the card, since the caller reads it while
    the agent is still talking and it is the only place cents are exact.

    `label` and `detail` stay the spoken strings on purpose: they are the
    row's accessible name for a screen reader, read exactly as the agent
    would say them. `display` carries the same facts reshaped for a screen
    (24-hour times, a real currency symbol): the frontend renders from
    `display` when present and falls back to `label`/`detail` otherwise, so
    an older stored card with no `display` still renders."""
    legs = opt["legs"]
    price = disp.fmt_money(opt["total"], opt["currency"])
    if not legs:
        return {
            "n": i + 1, "value": price, "label": opt["airline"],
            "detail": say_money(opt["total"], opt["currency"]),
            "display": {"airline": opt["airline"], "airline_note": "",
                        "price": price, "price_note": "total", "legs": []},
        }
    out = f"out {say_clock(legs[0]['depart'])} to {say_clock(legs[0]['arrive'])}"
    back = (f" · back {say_clock(legs[1]['depart'])} to {say_clock(legs[1]['arrive'])}"
            if len(legs) > 1 else "")
    route = f"{legs[0]['from_code']} to {legs[0]['to_code']}"
    carriers = [leg.get("carrier") or opt["airline"] for leg in legs]
    display_legs = [
        {"dep": disp.fmt_clock(leg["depart"]), "arr": disp.fmt_clock(leg["arrive"]),
         "from": leg["from_code"], "to": leg["to_code"],
         "stops": "Nonstop" if leg["stops"] == 0 else _plural(leg["stops"], "stop"),
         "duration": disp.fmt_duration(leg["duration"]),
         "plus_days": disp.plus_days(leg["depart"], leg["arrive"])}
        for leg in legs
    ]
    return {
        "n": i + 1, "value": price,
        "label": f"{_airline_phrase(opt)} · {out}{back}",
        "detail": f"{_say_stops(legs)} · {say_money(opt['total'], opt['currency'])} · {route}",
        "display": {
            "airline": opt["airline"],
            "airline_note": (f"Out {carriers[0]}, back {carriers[1]}"
                              if len(carriers) > 1 and len(set(carriers)) > 1 else ""),
            "price": price, "price_note": "total",
            "legs": display_legs,
        },
    }


def _test_note(mode: str, what: str = "fares") -> str:
    return (f" These are sandbox {what} from Duffel's test mode, so times and prices are"
            " not real; say so if the caller asks." if mode == "test" else "")


def _cards(kind: str, title: str, items: list[dict], display: dict | None = None) -> dict[str, Any]:
    # `pick` is what a tap on the card should send. It used to fall back to
    # `value`, back when `value` held "Option 3" or "Rate 2". `value` now
    # always holds the price ($433.04), so `pick` is built from `n` instead,
    # kind-aware for a lone rate (a plain yes, nothing to number).
    for it in items:
        n = it.get("n")
        if kind == "rates":
            it.setdefault("pick", "Yes" if len(items) == 1 else f"Rate {n}")
        else:
            it.setdefault("pick", f"Option {n}")
    group = {"kind": kind, "title": title, "items": items}
    if display:
        group["display"] = display
    return group


async def do_search_flights(key: str, conversation: str, origin: str, destination: str,
                            departure_date: str, return_date: str = "", adults: int = 1,
                            cabin: str = "economy", trip_id: str = "") -> dict[str, Any]:
    try:
        depart = _check_date(departure_date, "departure date")
        ret = _check_date(return_date, "return date", not_before=depart) if (return_date or "").strip() else ""
        try:
            adults = int(adults)
        except (TypeError, ValueError):
            adults = 1
        if not 1 <= adults <= 9:
            raise DuffelError("ask how many travellers there are, between 1 and 9")
        cabin = (cabin or "economy").strip().lower().replace(" ", "_")
        if cabin not in CABINS:
            raise DuffelError("cabin must be economy, premium economy, business or first")
        frm, to = await asyncio.gather(resolve_place(key, origin, "departure"),
                                       resolve_place(key, destination, "destination"))
        if frm["iata"] and frm["iata"] == to["iata"]:
            raise DuffelError("the departure and destination came out the same place;"
                              " ask the caller for both again")
        slices = [{"origin": frm["iata"], "destination": to["iata"], "departure_date": depart}]
        if ret:
            slices.append({"origin": to["iata"], "destination": frm["iata"], "departure_date": ret})
        data = await _call(
            key, "POST", "/air/offer_requests",
            params={"return_offers": "true", "supplier_timeout": SUPPLIER_TIMEOUT_MS},
            body={"slices": slices, "passengers": [{"type": "adult"}] * adults,
                  "cabin_class": cabin},
            timeout=SEARCH_TIMEOUT_S,
        )
    except Exception as e:
        return _failed("Cannot search flights", e, "search_flights")

    trip = _trip(trip_id, key, conversation, create=True)
    trip.origin, trip.destination = frm, to
    trip.depart, trip.ret, trip.adults, trip.cabin = depart, ret, adults, cabin
    trip.passenger_ids = [p.get("id") for p in (data.get("passengers") or []) if p.get("id")]
    offers = [o for o in (data.get("offers") or []) if isinstance(o, dict) and o.get("slices")]
    offers.sort(key=lambda o: (money_number(o.get("total_amount")) or 1e12,
                               sum(len(s.get("segments") or []) for s in o["slices"])))
    seen, picked = set(), []
    for o in offers:
        first = (o["slices"][0].get("segments") or [{}])[0]
        sig = ((o.get("owner") or {}).get("iata_code"), first.get("departing_at"),
               (first.get("origin") or {}).get("iata_code"))
        if sig in seen:
            continue
        seen.add(sig)
        picked.append(_option_from_offer(o, adults))
        if len(picked) == MAX_OPTIONS:
            break
    trip.options = picked
    trip.flight_order = None

    route = f"{frm['city']} to {to['city']}"
    when = say_day(depart) + (f", returning {say_day(ret)}" if ret else "")
    if not picked:
        return {
            "say": (f"I found no flights from {route} on {when}. Offer another date or a"
                    " nearby airport; do not invent an option."),
            "machine": {"trip_id": trip.id, "options": [], "mode": trip.mode},
        }
    lines = [_say_option(i + 1, opt, adults) for i, opt in enumerate(picked)]
    say = (
        f"I found {_plural(len(picked), 'option')} from {route}, {when}"
        f"{', ' + _plural(adults, 'traveller') if adults > 1 else ''}:"
        f" {_summary_flights(picked)}. They are on the caller's screen; ask which one they"
        " want, never choose for them. Read the full list out ONLY if the caller asks to"
        f" hear all the options: {' '.join(lines)}"
    ) + _test_note(trip.mode)
    cards = _cards(
        "flights", f"Flights, {route}",
        [_card_for_option(i, opt) for i, opt in enumerate(picked)],
        display={
            "from_code": frm["iata"], "to_code": to["iata"],
            "from_city": frm["city"], "to_city": to["city"],
            "dates": (f"{disp.fmt_day(depart)} to {disp.fmt_day(ret)}" if ret else disp.fmt_day(depart)),
            "pax": _plural(adults, "adult"),
            "cabin": cabin.replace("_", " ").title(),
            "leg": "round_trip" if ret else "one_way",
        },
    )
    return {
        "say": say,
        "machine": {"trip_id": trip.id, "mode": trip.mode, "options": [
            {"n": i + 1, "airline": o["airline"], "total": o["total"], "currency": o["currency"],
             "expires_at": o["expires_at"], "legs": o["legs"], "docs_required": o["docs_required"]}
            for i, o in enumerate(picked)
        ]},
        "cards": [cards],
    }


async def do_flight_details(key: str, conversation: str, trip_id: str, option) -> dict[str, Any]:
    try:
        trip = _trip(trip_id, key, conversation)
        opt = _option(trip.options, option, "option")
    except Exception as e:
        return _failed("Cannot describe that option", e, "flight_details")
    bags = [b for b in opt["bags"] if b.get("quantity")]
    bag_bits = []
    for b in bags:
        kind = "checked bag" if b.get("type") == "checked" else "carry-on bag"
        bag_bits.append(_plural(int(b["quantity"]), kind))
    bag_text = ("Included: " + " and ".join(bag_bits) + "." if bag_bits
                else "No bags are included in the fare.")
    rules = (
        ("Refundable before departure" if opt["refundable"] else "Not refundable")
        + ", " + ("changes allowed" if opt["changeable"] else "no changes allowed") + "."
    )
    docs = (" This fare needs passport details, which cannot be taken on this line."
            if opt["docs_required"] else "")
    fare = f" Fare: {opt['fare']}." if opt["fare"] else ""
    return {
        "say": f"Option {int(option)} with {opt['airline']}.{fare} {bag_text} {rules}{docs}",
        "machine": {"trip_id": trip.id, "expires_at": opt["expires_at"]},
    }


def _order_summary(order: dict) -> dict[str, Any]:
    return {
        "order_id": order.get("id"),
        "reference": order.get("booking_reference") or "",
        "live_mode": bool(order.get("live_mode")),
        "total": order.get("total_amount"),
        "currency": order.get("total_currency"),
        "legs": [_leg(s) for s in (order.get("slices") or []) if isinstance(s, dict)],
        "airline": (order.get("owner") or {}).get("name") or "",
        "emails": sorted({(p.get("email") or "").lower()
                          for p in (order.get("passengers") or []) if p.get("email")}),
        # For the confirmation the worker sends and the agency's copy of it.
        "travellers": [
            " ".join(x for x in ((p.get("title") or "").title(), p.get("given_name"),
                                 p.get("family_name")) if x)
            for p in (order.get("passengers") or []) if p.get("family_name")
        ],
        "phones": sorted({p.get("phone_number") for p in (order.get("passengers") or [])
                          if p.get("phone_number")}),
    }


async def _recent_orders(key: str) -> list[dict]:
    data = await _call(key, "GET", "/air/orders",
                       params={"limit": LIST_LIMIT, "sort": "-created_at"})
    return [o for o in (data if isinstance(data, list) else []) if isinstance(o, dict)]


def _same_itinerary(order: dict, opt: dict) -> bool:
    legs = _order_summary(order)["legs"]
    return bool(legs) and bool(opt["legs"]) and legs[0].get("depart") == opt["legs"][0].get("depart") \
        and legs[0].get("from_code") == opt["legs"][0].get("from_code")


async def do_book_flight(key: str, conversation: str, trip_id: str, option,
                         travellers: list, email: str, phone: str) -> dict[str, Any]:
    try:
        trip = _trip(trip_id, key, conversation)
        opt = _option(trip.options, option, "option")
        if trip.flight_order and trip.flight_order.get("offer_id") == opt["offer_id"]:
            return _already_flight(trip, "this call already booked that flight")
        # Local checks first, cheapest and most specific.
        travellers = travellers if isinstance(travellers, list) else []
        if len(travellers) != trip.adults:
            raise DuffelError(
                f"the search was for {_plural(trip.adults, 'traveller')}, so I need the"
                f" details of {'each of them' if trip.adults > 1 else 'the traveller'}:"
                " first name, last name, date of birth, gender and title"
            )
        people = [_check_traveller(t, i + 1, full=True) for i, t in enumerate(travellers)]
        addr = _check_email(email)
        tel = _check_phone(phone)
        # Duplicate guard against Duffel itself, BEFORE re-pricing: a fare
        # this call already holds is "already booked" whatever it costs now.
        # Memory would forget after a sidecar restart, an order list does not.
        for order in await _recent_orders(key):
            meta = order.get("metadata") or {}
            if meta.get("arya_conversation") == conversation and _same_itinerary(order, opt) \
                    and addr.lower() in _order_summary(order)["emails"]:
                trip.flight_order = {**_order_summary(order), "offer_id": opt["offer_id"]}
                return _already_flight(trip, "this call already booked that flight")
        if opt["docs_required"]:
            raise DuffelError("this fare needs passport details for every traveller,"
                              " which cannot be taken on this line; offer another option")
        # Re-price: offers expire in minutes and the order must quote the
        # exact current total, so the cached price is never trusted.
        fresh = await _call(key, "GET", f"/air/offers/{opt['offer_id']}")
        if fresh.get("passenger_identity_documents_required"):
            raise DuffelError("this fare now needs passport details, which cannot be taken"
                              " on this line; offer another option")
        exp = fresh.get("expires_at") or opt["expires_at"]
        if exp and _is_past(exp):
            raise DuffelError("that fare has expired, so search again and offer the"
                              " current options")
        new_total = f"{money_number(fresh.get('total_amount')) or 0:.2f}"
        if new_total != opt["total"]:
            old = say_money(opt["total"], opt["currency"])
            opt["total"] = new_total
            opt["per_person"] = f"{(money_number(new_total) or 0) / max(trip.adults, 1):.2f}"
            return {
                "say": (f"Not booked yet: the price has changed from {old} to"
                        f" {say_money(new_total, opt['currency'])}. Tell the caller and ask"
                        " whether to go ahead at the new price; if yes, call book_flight"
                        " again with the same details."),
                "machine": {"trip_id": trip.id, "price_changed": True, "total": new_total},
            }
        ids = list(trip.passenger_ids) or [None] * len(people)
        passengers = [
            {**({"id": pid} if pid else {}), **person, "email": addr, "phone_number": tel}
            for pid, person in zip(ids, people, strict=False)
        ]
        body = {
            "type": "instant",
            "selected_offers": [opt["offer_id"]],
            "payments": [{"type": "balance", "currency": fresh.get("total_currency") or opt["currency"],
                          "amount": new_total}],
            "passengers": passengers,
            "metadata": {"arya_conversation": conversation},
        }
        try:
            order = await _call(key, "POST", "/air/orders", body=body, timeout=WRITE_TIMEOUT_S)
        except DuffelError as e:
            if not _was_unreachable(e):
                raise
            # Unknown outcome, not failure: the order can land after we stop
            # waiting. Go and look before telling the caller anything.
            logger.warning(f"book_flight timed out, reconciling: {e}")
            landed = [o for o in await _recent_orders(key)
                      if (o.get("metadata") or {}).get("arya_conversation") == conversation
                      and _same_itinerary(o, opt)]
            if not landed:
                raise
            order = landed[0]
    except Exception as e:
        return _failed("Not booked", e, "book_flight")

    trip.flight_order = {**_order_summary(order), "offer_id": opt["offer_id"]}
    return _booked_flight(trip)


def _is_past(iso: str) -> bool:
    try:
        t = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t <= datetime.datetime.now(datetime.timezone.utc)


def _booked_flight(trip: Trip, lead: str = "Booked.") -> dict[str, Any]:
    o = trip.flight_order or {}
    legs = o.get("legs") or []
    out = _say_leg(legs[0]) if legs else "flight details are in the itinerary"
    back = f" Coming back it {_say_leg(legs[1])}." if len(legs) > 1 else ""
    total = say_money(o.get("total"), o.get("currency") or "")
    return {
        "say": (f"{lead} {o.get('airline') or 'The airline'} {out}.{back} Total {total}."
                f" Say what was booked, the day and time and the total, and that their"
                f" booking is on their screen. The written itinerary is not finished"
                f" yet, so do not call it one. Do not read the"
                f" booking reference aloud unless asked; if asked, say it slowly once."),
        "machine": {"trip_id": trip.id, "booked": True, "reference": o.get("reference"),
                    "order_id": o.get("order_id"), "live_mode": o.get("live_mode")},
        "itinerary": _itinerary(trip, "flight"),
        # The facts, for the confirmation the WORKER sends. This module
        # renders no mail: it knows nothing about the business, the
        # deployment or which mailbox the agency connected.
        "notify": {
            "kind": "flight_booked",
            "email": (o.get("emails") or [""])[0],
            "phone": (o.get("phones") or [""])[0],
            "travellers": o.get("travellers") or [],
            "reference": o.get("reference") or "",
            "order_id": o.get("order_id") or "",
            "total": f"{money_number(o.get('total')):,.2f} {o.get('currency', '')}".strip()
                     if money_number(o.get("total")) is not None else "",
        },
    }


def _already_flight(trip: Trip, why: str) -> dict[str, Any]:
    out = _booked_flight(trip, lead=f"Already booked ({why}).")
    out["machine"]["already"] = True
    out["say"] += " Confirm it to the caller and do not book it again, whatever they say."
    return out


async def do_find_bookings(key: str, conversation: str, email: str) -> dict[str, Any]:
    try:
        addr = _check_email(email).lower()
        orders, stays = await asyncio.gather(_recent_orders(key), _recent_stays(key))
    except Exception as e:
        return _failed("Cannot look up bookings", e, "find_bookings")
    mine = [o for o in orders if addr in _order_summary(o)["emails"]
            and not o.get("cancelled_at")]
    mine_stays = [b for b in stays if (b.get("email") or "").lower() == addr]
    parts, machine = [], {"flights": [], "hotels": []}
    for o in mine:
        s = _order_summary(o)
        if not s["legs"]:
            continue
        leg = s["legs"][0]
        parts.append(f"A flight with {s['airline']} that {_say_leg(leg)}"
                     + (" with a return" if len(s["legs"]) > 1 else "") + ".")
        machine["flights"].append({"reference": s["reference"], "order_id": s["order_id"],
                                   "depart": leg["depart"], "live_mode": s["live_mode"]})
    for b in mine_stays:
        acc = b.get("accommodation") or {}
        status = b.get("status") or "confirmed"
        parts.append(f"A {'cancelled ' if status == 'cancelled' else ''}hotel booking at"
                     f" {acc.get('name') or 'a hotel'}, checking in {say_day(b.get('check_in_date') or '')}"
                     f" and out {say_day(b.get('check_out_date') or '')}.")
        machine["hotels"].append({"reference": b.get("reference"), "booking_id": b.get("id"),
                                  "status": status, "check_in": b.get("check_in_date")})
    if not parts:
        return {"say": f"I found no bookings under {addr}. Ask whether they booked with a"
                       " different email.", "machine": machine}
    return {
        "say": (f"I found {_plural(len(parts), 'booking')} under that email. " + " ".join(parts)
                + " Describe them without reading any reference aloud."),
        "machine": machine,
    }


async def _find_order(key: str, reference: str) -> dict[str, Any]:
    ref = (reference or "").strip()
    if not ref:
        raise DuffelError("ask which booking, and look it up with find_bookings first")
    if ref.startswith("ord_"):
        return await _call(key, "GET", f"/air/orders/{ref}")
    data = await _call(key, "GET", "/air/orders", params={"booking_reference": ref, "limit": 5})
    for o in (data if isinstance(data, list) else []):
        if (o.get("booking_reference") or "").upper() == ref.upper():
            return o
    raise DuffelError("no flight booking matches that reference")


async def do_cancel_flight(key: str, conversation: str, reference: str, confirm: bool = False) -> dict[str, Any]:
    try:
        order = await _find_order(key, reference)
        oid = order["id"]
        if order.get("cancelled_at"):
            return {"say": "That flight was already cancelled. Tell the caller so.",
                    "machine": {"order_id": oid, "cancelled": True}}
        pending = _CANCELS.get(oid)
        if pending is None or _is_past(pending.get("expires_at") or ""):
            pending = await _call(key, "POST", "/air/order_cancellations",
                                  body={"order_id": oid}, timeout=WRITE_TIMEOUT_S)
            _CANCELS[oid] = pending
        refund = say_money(pending.get("refund_amount") or "0",
                           pending.get("refund_currency") or order.get("total_currency") or "")
        if not confirm:
            return {
                "say": (f"Cancelling this flight would refund {refund}. Read that back and get a"
                        " clear yes, then call cancel_flight again with confirm set to true."
                        " Nothing has been cancelled yet."),
                "machine": {"order_id": oid, "refund": pending.get("refund_amount"),
                            "quote_expires_at": pending.get("expires_at")},
            }
        done = await _call(key, "POST", f"/air/order_cancellations/{pending['id']}/actions/confirm",
                           body={}, timeout=WRITE_TIMEOUT_S)
        _CANCELS.pop(oid, None)
    except Exception as e:
        return _failed("Not cancelled", e, "cancel_flight")
    test = "" if order.get("live_mode") else " This was a sandbox booking, so no real refund moves."
    return {
        "say": f"Cancelled. The refund is {refund}.{test} Say so plainly.",
        "machine": {"order_id": oid, "cancelled": True, "confirmed_at": done.get("confirmed_at")},
    }


# ─── hotels ─────────────────────────────────────────────────────────────────

def _bookable_rates(acc: dict) -> list[dict]:
    """Rates Duffel Balance can pay: paid now, and balance listed among the
    payment methods when Duffel says which ones apply."""
    out = []
    for room in acc.get("rooms") or []:
        for rate in room.get("rates") or []:
            if not isinstance(rate, dict):
                continue
            methods = rate.get("available_payment_methods")
            if (rate.get("payment_type") or "pay_now") != "pay_now":
                continue
            if isinstance(methods, list) and methods and "balance" not in methods:
                continue
            out.append({**rate, "room_name": room.get("name") or "room"})
    out.sort(key=lambda r: money_number(r.get("total_amount")) or 1e12)
    return out


def _free_cancel_until(rate: dict) -> str:
    """The last moment a full refund still applies, from the timeline."""
    total = money_number(rate.get("total_amount"))
    best = ""
    for step in rate.get("cancellation_timeline") or []:
        if not isinstance(step, dict):
            continue
        amt = money_number(step.get("refund_amount"))
        if total and amt is not None and abs(amt - total) < 0.005:
            before = step.get("before") or ""
            best = before if before > best else best
    return best


def _say_rate(n: int, rate: dict) -> str:
    board = (rate.get("board_type") or "").replace("_", " ")
    board = f", {board}" if board and board != "room only" else ""
    until = _free_cancel_until(rate)
    cancel = (f", free cancellation until {say_day(until)}" if until else
              ", non-refundable" if rate.get("cancellation_timeline") is not None else "")
    return (f"Rate {n}: {rate.get('room_name', 'room')}{board},"
            f" {say_money(rate.get('total_amount'), rate.get('total_currency') or '')} in total{cancel}.")


def _hotel_option(result: dict) -> dict[str, Any]:
    acc = result.get("accommodation") or {}
    addr = (acc.get("location") or {}).get("address") or {}
    rates = _bookable_rates(acc)
    cheapest = rates[0] if rates else None
    return {
        "search_result_id": result.get("id"),
        "accommodation_id": acc.get("id"),
        "name": acc.get("name") or "a hotel",
        "area": addr.get("city_name") or addr.get("line_one") or "",
        "rating": acc.get("rating"),
        "review_score": acc.get("review_score"),
        "total": (cheapest or {}).get("total_amount") or result.get("cheapest_rate_total_amount"),
        "currency": (cheapest or {}).get("total_currency") or result.get("cheapest_rate_currency") or "",
        "free_cancel_until": _free_cancel_until(cheapest) if cheapest else "",
        "rates_loaded": bool(acc.get("rooms")),
        "rates": rates,
    }


def _summary_hotels(options: list[dict]) -> str:
    """One clause for the default bubble, same idea as _summary_flights."""
    priced = [(money_number(o["total"]), o) for o in options]
    priced = [(p, o) for p, o in priced if p is not None]
    if not priced:
        return "at a price I could not read"
    priced.sort(key=lambda t: t[0])
    lo_amt, top = priced[0]
    hi_amt = priced[-1][0]
    currency = top["currency"]
    lo, hi = say_money_rounded(lo_amt, currency), say_money_rounded(hi_amt, currency)
    price_bit = f"{lo} total" if lo == hi else f"from {lo} to {hi} total"
    return f"{price_bit}, the top pick is {top['name']}"


async def do_search_hotels(key: str, conversation: str, city: str, check_in: str, check_out: str,
                           adults: int = 2, rooms: int = 1, trip_id: str = "") -> dict[str, Any]:
    try:
        cin = _check_date(check_in, "check-in date")
        cout = _check_date(check_out, "check-out date", not_before=cin)
        if cout == cin:
            raise DuffelError("check-out must be at least one night after check-in")
        adults, rooms = max(1, int(adults or 2)), max(1, int(rooms or 1))
        place = await resolve_place(key, city, "city")
        test = is_test_token(key)
        lat, lng, radius = ((TEST_HOTEL_LAT, TEST_HOTEL_LNG, TEST_HOTEL_RADIUS_KM) if test
                            else (place.get("lat"), place.get("lng"), DEFAULT_RADIUS_KM))
        if lat is None or lng is None:
            raise DuffelError(f"I could not place {place['city']} on the map; ask for the"
                              " nearest large city")
        body = {
            "location": {"radius": radius,
                         "geographic_coordinates": {"latitude": lat, "longitude": lng}},
            "check_in_date": cin, "check_out_date": cout, "rooms": rooms,
            "guests": [{"type": "adult"}] * adults,
        }
        try:
            data = await _call(key, "POST", "/stays/search", body={**body, "fetch_rates": True},
                               timeout=SEARCH_TIMEOUT_S)
        except DuffelError as e:
            if "fetch_rates" not in str(e):
                raise
            data = await _call(key, "POST", "/stays/search", body=body, timeout=SEARCH_TIMEOUT_S)
    except Exception as e:
        return _failed("Cannot search hotels", e, "search_hotels")

    trip = _trip(trip_id, key, conversation, create=True)
    trip.city, trip.check_in, trip.check_out = place, cin, cout
    trip.guests, trip.rooms = adults, rooms
    results = [r for r in (data.get("results") or []) if isinstance(r, dict)]
    options = [_hotel_option(r) for r in results]
    options = [o for o in options if o["total"] is not None]
    options.sort(key=lambda o: money_number(o["total"]) or 1e12)
    trip.hotel_options = options[:MAX_OPTIONS]
    trip.hotel_rates, trip.hotel_choice, trip.hotel_booking = [], 0, None

    nights = (datetime.date.fromisoformat(cout) - datetime.date.fromisoformat(cin)).days
    where = f"in {place['city']}" if not test else f"for {place['city']} (Duffel's test properties)"
    if not trip.hotel_options:
        return {
            "say": (f"I found no hotels {where} for those dates that can be booked on this line."
                    " Offer different dates or a nearby area; do not invent one."),
            "machine": {"trip_id": trip.id, "options": [], "mode": trip.mode},
        }
    lines = []
    for i, o in enumerate(trip.hotel_options):
        rating = f", rated {o['rating']} star{'s' if o['rating'] != 1 else ''}" if o.get("rating") else ""
        area = f" in {o['area']}" if o["area"] else ""
        cancel = f", free cancellation until {say_day(o['free_cancel_until'])}" if o["free_cancel_until"] else ""
        lines.append(f"Option {i + 1}: {o['name']}{area}{rating},"
                     f" {say_money(o['total'], o['currency'])} for {_plural(nights, 'night')}{cancel}.")
    say = (
        f"I found {_plural(len(trip.hotel_options), 'option')} {where}, checking in"
        f" {say_day(cin)} and out {say_day(cout)}: {_summary_hotels(trip.hotel_options)}."
        " They are on the caller's screen; ask which one they want, then hotel_rates for the"
        " room choices before booking. Read the full list out ONLY if the caller asks to hear"
        f" all the options: {' '.join(lines)}"
    ) + _test_note(trip.mode, "hotels")
    cards = _cards(
        "hotels", f"Hotels, {place['city']}",
        [
            {
                "n": i + 1, "value": disp.fmt_money(o["total"], o["currency"]), "label": o["name"],
                "detail": f"{say_money(o['total'], o['currency'])} · {_plural(nights, 'night')}",
                "display": {
                    "name": o["name"],
                    "stars": (f"{o['rating']}-star" if o.get("rating") else ""),
                    "rating": "", "reviews": "",
                    "area": o.get("area") or "",
                    "price": disp.fmt_money(o["total"], o["currency"]),
                    "price_note": f"for {_plural(nights, 'night')}",
                    "cancel": (f"Free cancellation until {say_day(o['free_cancel_until'])}"
                               if o.get("free_cancel_until") else ""),
                },
            }
            for i, o in enumerate(trip.hotel_options)
        ],
        display={
            "city": place["city"],
            "dates": f"{disp.fmt_day(cin)} to {disp.fmt_day(cout)}",
            "pax": _plural(adults, "adult"), "nights": _plural(nights, "night"),
        },
    )
    return {
        "say": say,
        "machine": {"trip_id": trip.id, "mode": trip.mode, "nights": nights, "options": [
            {"n": i + 1, "name": o["name"], "total": o["total"], "currency": o["currency"],
             "free_cancel_until": o["free_cancel_until"]}
            for i, o in enumerate(trip.hotel_options)
        ]},
        "cards": [cards],
    }


async def _load_rates(key: str, trip: Trip, option: int) -> list[dict]:
    opt = _option(trip.hotel_options, option, "hotel option")
    if not opt["rates_loaded"] or not opt["rates"]:
        data = await _call(key, "POST",
                           f"/stays/search_results/{opt['search_result_id']}/actions/fetch_all_rates",
                           body={}, timeout=SEARCH_TIMEOUT_S)
        acc = data.get("accommodation") or data
        opt["rates"] = _bookable_rates(acc)
        opt["rates_loaded"] = True
    trip.hotel_choice = int(option)
    trip.hotel_rates = opt["rates"][:MAX_RATES]
    return trip.hotel_rates


async def do_hotel_rates(key: str, conversation: str, trip_id: str, option) -> dict[str, Any]:
    try:
        trip = _trip(trip_id, key, conversation)
        rates = await _load_rates(key, trip, option)
        opt = trip.hotel_options[trip.hotel_choice - 1]
    except Exception as e:
        return _failed("Cannot list the rooms", e, "hotel_rates")
    if not rates:
        return {"say": (f"{opt['name']} has no rooms that can be paid on this line for those"
                        " dates. Offer another hotel."),
                "machine": {"trip_id": trip.id, "rates": []}}
    lines = [_say_rate(i + 1, r) for i, r in enumerate(rates)]
    return {
        "say": (f"{opt['name']} has {_plural(len(rates), 'rate')}. " + " ".join(lines)
                + " Ask which rate they want, then collect the guest names, email and phone"
                  " before book_hotel."),
        "machine": {"trip_id": trip.id, "rates": [
            {"n": i + 1, "room": r.get("room_name"), "total": r.get("total_amount"),
             "currency": r.get("total_currency"), "free_cancel_until": _free_cancel_until(r)}
            for i, r in enumerate(rates)
        ]},
        "cards": [_cards("rates", f"Rooms, {opt['name']}", [
            {
                "n": i + 1,
                "value": disp.fmt_money(r.get("total_amount"), r.get("total_currency") or ""),
                "label": r.get("room_name") or "room",
                "detail": say_money(r.get("total_amount"), r.get("total_currency") or ""),
                "display": {
                    "room": r.get("room_name") or "Room", "source": "",
                    "price": disp.fmt_money(r.get("total_amount"), r.get("total_currency") or ""),
                    "price_note": "total",
                    "cancel": (f"Free cancellation until {say_day(_free_cancel_until(r))}"
                               if _free_cancel_until(r) else ""),
                    "refundable": bool(_free_cancel_until(r)),
                },
            }
            for i, r in enumerate(rates)
        ])],
    }


async def _recent_stays(key: str) -> list[dict]:
    try:
        data = await _call(key, "GET", "/stays/bookings", params={"limit": LIST_LIMIT})
    except DuffelError as e:
        # No Stays on the account means no hotel bookings, not a failure.
        if "could not find" in str(e) or NOT_ENABLED in str(e):
            return []
        raise
    return [b for b in (data if isinstance(data, list) else []) if isinstance(b, dict)]


def _stay_summary(b: dict) -> dict[str, Any]:
    acc = b.get("accommodation") or {}
    rate = None
    for room in acc.get("rooms") or []:
        for r in room.get("rates") or []:
            rate = r
            break
        if rate:
            break
    return {
        "booking_id": b.get("id"), "reference": b.get("reference") or "",
        "name": acc.get("name") or "the hotel", "status": b.get("status") or "confirmed",
        "check_in": b.get("check_in_date") or "", "check_out": b.get("check_out_date") or "",
        "total": (rate or {}).get("total_amount"), "currency": (rate or {}).get("total_currency") or "",
        "free_cancel_until": _free_cancel_until(rate) if rate else "",
        "email": (b.get("email") or "").lower(),
        "maps_url": "", "rating": acc.get("rating"),
    }


async def do_book_hotel(key: str, conversation: str, trip_id: str, option, rate,
                        guests: list, email: str, phone: str, special_requests: str = "") -> dict[str, Any]:
    try:
        trip = _trip(trip_id, key, conversation)
        if trip.hotel_booking:
            return _already_hotel(trip)
        if not trip.hotel_rates or trip.hotel_choice != int(option):
            await _load_rates(key, trip, option)
        chosen = _option(trip.hotel_rates, rate, "rate")
        opt = trip.hotel_options[trip.hotel_choice - 1]
        guests = guests if isinstance(guests, list) else []
        if not guests:
            raise DuffelError("I need the lead guest's first and last name")
        people = [_check_traveller(g, i + 1, full=False) for i, g in enumerate(guests)]
        addr = _check_email(email)
        tel = _check_phone(phone)
        # Duplicate guard against Duffel: same call, same hotel, same night.
        for b in await _recent_stays(key):
            meta = b.get("metadata") or {}
            if meta.get("arya_conversation") == conversation \
                    and (b.get("accommodation") or {}).get("id") == opt["accommodation_id"] \
                    and b.get("check_in_date") == trip.check_in and b.get("status") != "cancelled":
                trip.hotel_booking = _stay_summary(b)
                return _already_hotel(trip)
        quote = await _call(key, "POST", "/stays/quotes", body={"rate_id": chosen["id"]},
                            timeout=SEARCH_TIMEOUT_S)
        q_total = f"{money_number(quote.get('total_amount')) or 0:.2f}"
        r_total = f"{money_number(chosen.get('total_amount')) or 0:.2f}"
        if q_total != r_total:
            old = say_money(r_total, chosen.get("total_currency") or "")
            chosen["total_amount"] = q_total
            return {
                "say": (f"Not booked yet: the rate has changed from {old} to"
                        f" {say_money(q_total, quote.get('total_currency') or '')}. Tell the caller"
                        " and ask whether to go ahead; if yes, call book_hotel again."),
                "machine": {"trip_id": trip.id, "price_changed": True, "total": q_total},
            }
        body = {
            "quote_id": quote["id"],
            "guests": people,
            "email": addr,
            "phone_number": tel,
            "metadata": {"arya_conversation": conversation},
        }
        if (special_requests or "").strip():
            body["accommodation_special_requests"] = special_requests.strip()[:500]
        try:
            booking = await _call(key, "POST", "/stays/bookings", body=body, timeout=WRITE_TIMEOUT_S)
        except DuffelError as e:
            if not _was_unreachable(e):
                raise
            logger.warning(f"book_hotel timed out, reconciling: {e}")
            landed = [b for b in await _recent_stays(key)
                      if (b.get("metadata") or {}).get("arya_conversation") == conversation
                      and b.get("check_in_date") == trip.check_in]
            if not landed:
                raise
            booking = landed[0]
    except Exception as e:
        return _failed("Not booked", e, "book_hotel")
    summary = _stay_summary(booking)
    if not summary["total"]:
        summary["total"], summary["currency"] = quote.get("total_amount"), quote.get("total_currency") or ""
    summary["free_cancel_until"] = summary["free_cancel_until"] or _free_cancel_until(chosen)
    summary["name"] = summary["name"] if summary["name"] != "the hotel" else opt["name"]
    summary["rating"] = summary["rating"] or opt.get("rating")
    trip.hotel_booking = summary
    return _booked_hotel(trip)


def _booked_hotel(trip: Trip, lead: str = "Booked.") -> dict[str, Any]:
    h = trip.hotel_booking or {}
    cancel = (f" Free cancellation until {say_day(h['free_cancel_until'])}."
              if h.get("free_cancel_until") else "")
    return {
        "say": (f"{lead} {h.get('name')}, checking in {say_day(h.get('check_in', ''))} and out"
                f" {say_day(h.get('check_out', ''))}, {say_money(h.get('total'), h.get('currency') or '')}"
                f" in total.{cancel} Say so, and that the confirmation is on their screen. Do not read"
                f" the hotel reference aloud unless asked."),
        "machine": {"trip_id": trip.id, "booked": True, "reference": h.get("reference"),
                    "booking_id": h.get("booking_id")},
        "itinerary": _itinerary(trip, "hotel"),
    }


def _already_hotel(trip: Trip) -> dict[str, Any]:
    out = _booked_hotel(trip, lead="Already booked on this call.")
    out["machine"]["already"] = True
    out["say"] += " Confirm it to the caller and do not book it again."
    return out


async def _find_stay(key: str, reference: str) -> dict[str, Any]:
    ref = (reference or "").strip()
    if not ref:
        raise DuffelError("ask which hotel booking, and look it up with find_bookings first")
    if ref.startswith("bok_"):
        return await _call(key, "GET", f"/stays/bookings/{ref}")
    for b in await _recent_stays(key):
        if (b.get("reference") or "").upper() == ref.upper():
            return b
    raise DuffelError("no hotel booking matches that reference")


async def do_cancel_hotel(key: str, conversation: str, reference: str, confirm: bool = False) -> dict[str, Any]:
    try:
        booking = await _find_stay(key, reference)
        s = _stay_summary(booking)
        if s["status"] == "cancelled":
            return {"say": "That hotel booking was already cancelled. Tell the caller so.",
                    "machine": {"booking_id": s["booking_id"], "cancelled": True}}
        refund_now = (s["free_cancel_until"] and not _is_past(s["free_cancel_until"]))
        refund = (say_money(s["total"], s["currency"]) if refund_now else "nothing")
        if not confirm:
            return {
                "say": (f"Cancelling {s['name']} would refund {refund}"
                        f"{' (free cancellation still applies)' if refund_now else ' (the free cancellation window has passed)'}."
                        " Read that back and get a clear yes, then call cancel_hotel again with"
                        " confirm set to true. Nothing has been cancelled yet."),
                "machine": {"booking_id": s["booking_id"], "refund": s["total"] if refund_now else "0"},
            }
        done = await _call(key, "POST", f"/stays/bookings/{s['booking_id']}/actions/cancel",
                           body={}, timeout=WRITE_TIMEOUT_S)
    except Exception as e:
        return _failed("Not cancelled", e, "cancel_hotel")
    test = " This was a sandbox booking, so no real refund moves." if is_test_token(key) else ""
    for trip in _TRIPS.values():
        if trip.hotel_booking and trip.hotel_booking.get("booking_id") == s["booking_id"]:
            trip.hotel_booking["status"] = "cancelled"
    return {
        "say": f"Cancelled {s['name']}. The refund is {refund}.{test} Say so plainly.",
        "machine": {"booking_id": s["booking_id"], "cancelled": True,
                    "cancelled_at": done.get("cancelled_at")},
    }


# ─── the plan and the written trip ───────────────────────────────────────────

def _trip_days(trip: Trip) -> list[str]:
    start = trip.check_in or trip.depart
    end = trip.check_out or trip.ret or trip.depart
    try:
        d0 = datetime.date.fromisoformat(start)
        d1 = datetime.date.fromisoformat(end) if end else d0
    except ValueError:
        return []
    if d1 < d0:
        d1 = d0
    out, cur = [], d0
    while cur <= d1 and len(out) < 31:
        out.append(cur.isoformat())
        cur += datetime.timedelta(days=1)
    return out


def _resolve_day(trip: Trip, day) -> str:
    """"2026-09-13", "day 2", "2", "second day", "the 13th" -> ISO date."""
    text = str(day or "").strip().lower()
    days = _trip_days(trip)
    with contextlib.suppress(ValueError):
        return datetime.date.fromisoformat(text[:10]).isoformat()
    m = re.search(r"\d+", text)
    words = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
             "seventh": 7, "last": len(days)}
    n = int(m.group()) if m else next((v for w, v in words.items() if w in text), 0)
    if days and 1 <= n <= len(days):
        return days[n - 1]
    if days and "the" in text and m and 1 <= int(m.group()) <= 31:
        for d in days:
            if int(d[-2:]) == int(m.group()):
                return d
    if days:
        raise DuffelError(f"say which day of the trip, from 1 to {len(days)}, or give the date")
    raise DuffelError("give the date for that activity, like 2026-09-13; there are no trip"
                      " dates yet to count days from")


_TIME_RANK = {"morning": 1, "breakfast": 1, "midday": 2, "noon": 2, "lunch": 2,
              "afternoon": 3, "evening": 4, "dinner": 4, "night": 4}


def _time_rank(text: str) -> float:
    """Order within a day: words or a clock time; unknown sits mid-day."""
    t = (text or "").strip().lower()
    if t in _TIME_RANK:
        return _TIME_RANK[t]
    m = re.match(r"^(\d{1,2})(?::\d{2})?\s*(am|pm)?$", t)
    if m:
        h = int(m.group(1))
        if m.group(2):  # 12-hour clock only when am or pm was said
            h = h % 12 + (12 if m.group(2) == "pm" else 0)
        return 1 if h < 12 else 2 if h < 14 else 3 if h < 17 else 4
    return 2.5


def _plan_item(trip: Trip, day, title: str, note: str = "", url: str = "",
               time: str = "") -> dict:
    title = (title or "").strip()
    if not title:
        raise DuffelError("name the place or activity to add")
    when = _resolve_day(trip, day)
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = ""
    item = {"day": when, "title": title[:120], "note": (note or "").strip()[:240],
            "url": url, "time": (time or "").strip()[:40]}
    trip.plan.append(item)
    trip.plan.sort(key=lambda p: (p["day"], _time_rank(p.get("time", ""))))
    return item


async def do_plan_activity(key: str, conversation: str, trip_id: str, day, title: str,
                           note: str = "", url: str = "", time: str = "") -> dict[str, Any]:
    try:
        trip = _trip(trip_id, key, conversation, create=True)
        item = _plan_item(trip, day, title, note, url, time)
    except Exception as e:
        return _failed("Not added", e, "plan_activity")
    when = item["day"]
    on_day = [p["title"] for p in trip.plan if p["day"] == when]
    return {
        "say": (f"Added {item['title']} to {say_day(when)}. That day now has"
                f" {', '.join(on_day)}. Say their written itinerary is on their screen."),
        "machine": {"trip_id": trip.id, "day": when, "count": len(trip.plan)},
        "itinerary": _itinerary(trip, "plan"),
    }


def _activities(n: int) -> str:
    return f"{n} {'activity' if n == 1 else 'activities'}"


async def do_plan_days(key: str, conversation: str, trip_id: str, items: list) -> dict[str, Any]:
    """The whole day-by-day plan in one call. Bad items are dropped and
    counted rather than sinking the good ones: the first live call saved
    one activity per day in five separate turns."""
    try:
        trip = _trip(trip_id, key, conversation, create=True)
        items = items if isinstance(items, list) else []
        if not items:
            raise DuffelError("give the plan as a list of items, each with a day and a title")
        added, rejected = [], 0
        for raw in items:
            raw = raw if isinstance(raw, dict) else {}
            try:
                added.append(_plan_item(trip, raw.get("day"), raw.get("title", ""),
                                        raw.get("note", ""), raw.get("url", ""),
                                        raw.get("time", "")))
            except DuffelError:
                rejected += 1
        if not added:
            raise DuffelError("none of the items had a usable day and title")
    except Exception as e:
        return _failed("Plan not saved", e, "plan_days")
    days = sorted({p["day"] for p in added})
    first = days[0]
    sample = ", ".join(
        (f"{p['time']} " if p.get("time") else "") + p["title"]
        for p in trip.plan if p["day"] == first
    )
    skipped = f" {rejected} item{'s' if rejected != 1 else ''} had no usable day or title and were skipped." if rejected else ""
    return {
        "say": (f"Planned {_activities(len(added))} across {_plural(len(days), 'day')}."
                f" For example {say_day(first)}: {sample}. Say their written itinerary"
                f" is on their screen, offer to read any day in full, and ask whether"
                f" to email it to them now that it is finished.{skipped}"),
        "machine": {"trip_id": trip.id, "count": len(added), "days": len(days),
                    "rejected": rejected, "total": len(trip.plan)},
        "itinerary": _itinerary(trip, "plan"),
    }


def _flight_times(trip: Trip) -> tuple[str, str]:
    """When the caller lands and when they leave again, so the first and
    last day are not planned as if they were whole ones."""
    legs = ((trip.flight_order or {}).get("legs") or [])
    arrive = legs[0].get("arrive", "") if legs else ""
    depart = legs[1].get("depart", "") if len(legs) > 1 else ""
    return arrive, depart


async def do_plan_trip(key: str, conversation: str, trip_id: str,
                       places: list, dining: list | None = None) -> dict[str, Any]:
    """The whole stay in one call: the agent looks the places up, this lays
    them out. The model proved it will not do the layout itself, and it has
    no business deciding it anyway: which day a sight lands on is arithmetic
    over the trip dates, and Python owns that here as it does on the sample
    desk."""
    try:
        trip = _trip(trip_id, key, conversation, create=True)
        places = [p for p in (places or []) if isinstance(p, dict)]
        dining = [d for d in (dining or []) if isinstance(d, dict)]
        if not any((p.get("title") or "").strip() for p in places):
            raise DuffelError("give the places you looked up, each with a title")
        days = _trip_days(trip)
        if not days:
            raise DuffelError("the trip has no dates yet, so ask for them or book"
                              " the flights before planning the days")
        arrive, leave = _flight_times(trip)
        taken: dict[str, int] = {}
        for p in trip.plan:
            taken[p["day"]] = taken.get(p["day"], 0) + 1
        items, empty = day_plan.lay_out(
            days, places, dining,
            caps=day_plan.travel_day_caps(days, arrive, leave), taken=taken,
        )
        if not items:
            raise DuffelError("every day of the trip is already planned;"
                              " use plan_activity to add one more")
        for it in items:
            _plan_item(trip, it["day"], it["title"], it["note"], it["url"], it["time"])
    except Exception as e:
        return _failed("Plan not saved", e, "plan_trip")
    planned = sorted({p["day"] for p in items})
    sample = ", ".join(
        (f"{p['time']} " if p.get("time") else "") + p["title"]
        for p in trip.plan if p["day"] == planned[0]
    )
    gap = ""
    if empty:
        gap = (f" {_plural(len(empty), 'day')} had nothing to fill it:"
               f" {', '.join(say_day(d) for d in empty)}."
               " Offer to look up more places for those.")
    return {
        "say": (f"Planned {_activities(len(items))} across {_plural(len(planned), 'day')}."
                f" For example {say_day(planned[0])}: {sample}. Say their written"
                f" itinerary is on their screen, offer to read any day in full, and ask"
                f" whether to email it to them now that it is finished.{gap}"),
        "machine": {"trip_id": trip.id, "count": len(items), "days": len(planned),
                    "empty_days": empty, "total": len(trip.plan)},
        "itinerary": _itinerary(trip, "plan"),
    }


def _itinerary(trip: Trip, stage: str) -> dict[str, Any]:
    """The payload itinerary_html.render takes (see travel_tools
    _itinerary_payload for the sample desk's twin), plus mode for the page's
    footer note. Brand is filled by the worker, which knows the business."""
    o = trip.flight_order or {}
    legs = o.get("legs") or []

    def _flight(leg: dict | None) -> dict | None:
        if not leg:
            return None
        stops = "nonstop" if leg["stops"] == 0 else _plural(leg["stops"], "stop")
        per = money_number(o.get("total"))
        per_text = (f"{(per or 0) / max(trip.adults, 1):,.2f} {o.get('currency', '')}".strip()
                    if per is not None else "n/a")
        return {
            "airline": o.get("airline") or "", "stops": stops,
            "duration": say_duration(leg["duration"]),
            # Structured, not a sentence: the page decides how a time and a
            # place are laid out, and the sample desk emits the same shape.
            "depart": {"time": leg["depart"][11:16], "date": leg["depart"][:10],
                       "airport": leg["from_name"], "code": leg["from_code"]},
            "arrive": {"time": leg["arrive"][11:16], "date": leg["arrive"][:10],
                       "airport": leg["to_name"], "code": leg["to_code"]},
            "price": per_text, "flight_number": ", ".join(leg["flights"]) or None,
            "url": _gflights_url(leg["from_code"], leg["to_code"], leg["depart"][:10]),
            "reference": o.get("reference") or "",
            "total": f"{per:,.2f} {o.get('currency', '')}".strip() if per is not None else None,
        }

    h = trip.hotel_booking
    hotel = None
    nights = 0
    if h and h.get("status") != "cancelled":
        try:
            nights = (datetime.date.fromisoformat(h["check_out"]) - datetime.date.fromisoformat(h["check_in"])).days
        except (ValueError, KeyError):
            nights = 0
        total = money_number(h.get("total"))
        hotel = {
            "name": h.get("name"), "rating": h.get("rating") or "n/a", "source": "Duffel Stays",
            "price_per_night": (f"{total / nights:,.2f} {h.get('currency', '')}".strip()
                                if total and nights else "n/a"),
            "nights": max(nights, 1), "total": f"{total:,.2f} {h.get('currency', '')}".strip() if total else None,
            "maps_url": h.get("maps_url") or _maps_url(
                f"{h.get('name') or ''} {(trip.city or trip.destination or {}).get('city', '')}"),
            "reference": h.get("reference") or "",
            "check_in": h.get("check_in"), "check_out": h.get("check_out"),
            "free_cancel_until": h.get("free_cancel_until") or "",
        }
    # NOT `day_plan`: that is the module this function calls for themes.
    days_out = []
    city = (trip.city or trip.destination or {}).get("city", "")
    for day in sorted({p["day"] for p in trip.plan}):
        items = [p for p in trip.plan if p["day"] == day]
        d = datetime.date.fromisoformat(day)
        days_out.append({
            "date": day,
            "label": f"{d.strftime('%d %b %Y')}, {d.strftime('%A')}",
            # The same themer the sample desk uses, so a trip planned
            # through connectors gets the same headings as one planned by
            # the sample desk. `location` gains the hotel name in
            # itinerary_sync.merge_payload, which is the only place that
            # sees both writers.
            "theme": day_plan.theme_for(
                [{"title": p["title"], "description": p.get("note", "")} for p in items]),
            "location": (trip.city or trip.destination or {}).get("city", ""),
            "slots": [{"time": p.get("time", ""), "activity": p["title"],
                       "description": p["note"],
                       "url": p["url"] or _maps_url(f"{p['title']} {city}")} for p in items],
            "spoken": f"{d.strftime('%A, %B')} {d.day}: " + ", ".join(p["title"] for p in items) + ".",
        })
    flight_total = money_number(o.get("total")) or 0.0
    hotel_total = money_number((hotel or {}).get("total")) if hotel else None
    cur = o.get("currency") or (h or {}).get("currency") or ""
    grand = flight_total + (hotel_total or 0)
    return {
        "stage": stage,
        "title": f"Trip to {(trip.destination or trip.city or {}).get('city')}" if (trip.destination or trip.city) else "Trip itinerary",
        "payload": {
            "brand": {"name": "", "icon": "✈️", "mock": False, "mode": trip.mode},
            "trip": {
                "origin_city": (trip.origin or {}).get("city"), "origin_code": (trip.origin or {}).get("iata"),
                "dest_city": (trip.destination or trip.city or {}).get("city"),
                "dest_code": (trip.destination or {}).get("iata"),
                "depart_date": trip.depart or trip.check_in or None,
                "return_date": trip.ret or trip.check_out or None,
                "travelers": trip.adults if trip.flight_order else trip.guests,
            },
            "flights": {
                "outbound": _flight(legs[0] if legs else None),
                "return": _flight(legs[1] if len(legs) > 1 else None),
                # A Duffel round trip is ONE offer with two slices, so the
                # fare covers the journey. Printed under each leg it read
                # as double what was charged.
                "fare": {
                    "scope": "journey",
                    "total": (f"{money_number(o.get('total')):,.2f} {o.get('currency', '')}".strip()
                              if money_number(o.get("total")) is not None else None),
                    "per_traveler": (
                        f"{(money_number(o.get('total')) or 0) / max(trip.adults, 1):,.2f}"
                        f" {o.get('currency', '')}".strip()
                        if money_number(o.get("total")) is not None else None),
                } if o else {},
            },
            "hotel": hotel,
            "day_plan": days_out,
            "totals": {
                "flight": f"{flight_total:,.2f} {cur}".strip() if flight_total else None,
                "hotel": f"{hotel_total:,.2f} {cur}".strip() if hotel_total else None,
                "grand": f"{grand:,.2f} {cur}".strip() if grand else None,
            },
        },
    }


async def do_trip_summary(key: str, conversation: str, trip_id: str) -> dict[str, Any]:
    try:
        trip = _trip(trip_id, key, conversation)
    except Exception as e:
        return _failed("Cannot summarise the trip", e, "trip_summary")
    o, h = trip.flight_order, trip.hotel_booking
    if not (o or h or trip.plan):
        return {"say": "Nothing is booked or planned on this trip yet. Say so.",
                "machine": {"trip_id": trip.id, "empty": True}}
    dest = (trip.destination or trip.city or {}).get("city") or "your destination"
    paras = [f"Your trip to {dest}" + (f" from {trip.origin['city']}" if trip.origin else "") + "."]
    if o:
        legs = o.get("legs") or []
        for label, leg in zip(("Outbound", "Return"), legs, strict=False):
            paras.append(
                f"{label}: {o.get('airline')} {' '.join(leg['flights'])}, {leg['from_name']} to"
                f" {leg['to_name']}, departs {leg['depart'][:10]} at {leg['depart'][11:16]}, arrives"
                f" {leg['arrive'][11:16]}, {'nonstop' if leg['stops'] == 0 else _plural(leg['stops'], 'stop')}."
            )
        paras.append(f"Flight total {o.get('total')} {o.get('currency')}. Airline reference"
                     f" {o.get('reference') or 'pending'}."
                     + (" (Test booking on Duffel's sandbox: no real ticket.)" if not o.get("live_mode") else ""))
    if h and h.get("status") != "cancelled":
        paras.append(f"Hotel: {h.get('name')}, check in {h.get('check_in')}, check out {h.get('check_out')},"
                     f" total {h.get('total')} {h.get('currency')}. Hotel reference {h.get('reference')}."
                     + (f" Free cancellation until {h['free_cancel_until'][:10]}." if h.get("free_cancel_until") else "")
                     + (" (Test property, nothing real reserved.)" if trip.mode == "test" else ""))
    for day in sorted({p["day"] for p in trip.plan}):
        items = "; ".join(
            (f"{p['time']}: " if p.get("time") else "") + p["title"]
            + (f" ({p['note']})" if p["note"] else "")
            for p in trip.plan if p["day"] == day
        )
        paras.append(f"{say_day(day)}: {items}.")
    written = "\n\n".join(paras)
    spoken_bits = []
    if o and o.get("legs"):
        spoken_bits.append(f"flights with {o['airline']} on {say_day(o['legs'][0]['depart'])}")
    if h and h.get("status") != "cancelled":
        spoken_bits.append(f"{_plural(max(1, len(_trip_days(trip)) - 1), 'night')} at {h.get('name')}")
    if trip.plan:
        spoken_bits.append(_plural(len(trip.plan), "planned activity").replace("activitys", "activities"))
    return {
        "say": (f"The trip has {', '.join(spoken_bits)}. Recap it in two sentences. The written"
                " version is in the summary field: when emailing, use it as the email body word"
                " for word, and use the subject field as the subject."),
        "machine": {"trip_id": trip.id, "summary": written,
                    "subject": f"Your trip to {dest}" + (f", {trip.depart or trip.check_in}" if (trip.depart or trip.check_in) else "")},
        "itinerary": _itinerary(trip, "recap"),
    }


# ─── errors ─────────────────────────────────────────────────────────────────

def _failed(what: str, e: BaseException, tool: str) -> dict[str, Any]:
    """Every tool answers with a sentence, never an exception: a raised error
    reaches the model as an opaque ToolError and the caller hears dead air.
    The sentence says what to do next, and says it was an error so the model
    never reports it as "nothing found"."""
    if isinstance(e, DuffelError):
        reason = str(e)
    else:
        logger.warning(f"{tool} failed: {e!r}")
        reason = "the travel system did not respond"
    return {
        "say": (f"{what}: {reason}. This was an error, not an empty result: do not tell the"
                " caller nothing was found or that anything was booked."),
        "machine": {"error": True, "tool": tool},
        "error": True,
    }


# ─── the MCP app ────────────────────────────────────────────────────────────

def build(security=None) -> FastMCP:
    """The FastMCP app for Duffel. Every tool returns a dict, which the MCP
    SDK ships as structuredContent next to its JSON text; the worker's
    result resolver (connectors.spoken_result) speaks `say`, keeps `machine`
    for the model, renders `cards` and writes `itinerary`. The docstrings
    ARE the contract the model reads."""
    app = FastMCP("arya-duffel", stateless_http=True, transport_security=security)

    def _ids(ctx: Context) -> tuple[str, str]:
        return _key_from(ctx), _conversation_from(ctx)

    @app.tool(structured_output=True, annotations=_READ)
    async def search_flights(ctx: Context, origin: str, destination: str, departure_date: str,
                             return_date: str = "", adults: int = 1, cabin: str = "economy",
                             trip_id: str = "") -> dict[str, Any]:
        """Search real flights. Call this only after the caller has confirmed
        the route and dates out loud. Say one short sentence first ("Let me
        look for flights"): real searches take a few seconds.

        Cities are fine: pass "London" or "New York", never convert to airport
        codes yourself. A round trip is ONE search with a return_date, and
        each option then covers both legs; read both legs of every option.

        Args:
            origin: Departure city or airport, as the caller said it.
            destination: Destination city or airport, as the caller said it.
            departure_date: YYYY-MM-DD. Use get_current_date for relative dates.
            return_date: YYYY-MM-DD for a round trip, empty for one way. Never
                assume one way: ask if the caller has not said.
            adults: Number of travellers, 1 to 9.
            cabin: economy, premium_economy, business or first.
            trip_id: The trip_id from an earlier result on this call, so the
                flights join the same trip. Empty on the first search.
        """
        key, conv = _ids(ctx)
        return await do_search_flights(key, conv, origin, destination, departure_date,
                                       return_date, adults, cabin, trip_id)

    @app.tool(structured_output=True, annotations=_READ)
    async def flight_details(ctx: Context, trip_id: str, option: int) -> dict[str, Any]:
        """Bags, refund and change rules for one option from the last flight
        search, when the caller asks. Not needed before booking.

        Args:
            trip_id: The trip_id from search_flights.
            option: The option number the caller means.
        """
        key, conv = _ids(ctx)
        return await do_flight_details(key, conv, trip_id, option)

    @app.tool(structured_output=True, annotations=_WRITE)
    async def book_flight(ctx: Context, trip_id: str, option: int, travellers: list[dict],
                          email: str, phone: str) -> dict[str, Any]:
        """Book one option from the last flight search. Before calling this,
        collect every traveller's details ONE AT A TIME, then read all of them
        back in one sentence and wait for a clear yes. The fare is re-priced
        first; if the price changed you get told and nothing is booked.

        Safe to call once. If the answer says `already`, the flight exists:
        confirm it and never call this again for it.

        Args:
            trip_id: The trip_id from search_flights.
            option: The option number the caller chose.
            travellers: One entry per traveller (as many as the search had),
                each {"given_name", "family_name", "born_on" (YYYY-MM-DD),
                "gender" ("m" or "f"), "title" (mr, ms, mrs, miss or dr)}.
            email: The lead traveller's email, read back to them first.
            phone: The lead traveller's phone with country code, like
                +44 7700 900123.
        """
        key, conv = _ids(ctx)
        return await do_book_flight(key, conv, trip_id, option, travellers, email, phone)

    @app.tool(structured_output=True, annotations=_READ)
    async def find_bookings(ctx: Context, email: str) -> dict[str, Any]:
        """The caller's existing flight and hotel bookings, by the email they
        booked with. Ask for that email first. Use before cancelling.

        Args:
            email: The email the bookings were made with.
        """
        key, conv = _ids(ctx)
        return await do_find_bookings(key, conv, email)

    @app.tool(structured_output=True, annotations=_DESTRUCTIVE)
    async def cancel_flight(ctx: Context, reference: str, confirm: bool = False) -> dict[str, Any]:
        """Cancel a flight booking in two steps: first call with confirm false
        to learn the refund, read it back, get a clear yes, then call again
        with confirm true. A cancellation cannot be undone.

        Args:
            reference: The flight's `reference` (or order_id) from
                find_bookings or the booking result. Never say it aloud.
            confirm: False to quote the refund, true to cancel.
        """
        key, conv = _ids(ctx)
        return await do_cancel_flight(key, conv, reference, confirm)

    @app.tool(structured_output=True, annotations=_READ)
    async def search_hotels(ctx: Context, city: str, check_in: str, check_out: str,
                            adults: int = 2, rooms: int = 1, trip_id: str = "") -> dict[str, Any]:
        """Search real hotels in a city for given nights. Offer this only
        after flights are settled or when the caller asks for a place to
        stay. Say a short sentence first; the search takes a few seconds.

        Args:
            city: The city, as the caller said it.
            check_in: YYYY-MM-DD.
            check_out: YYYY-MM-DD, after check_in.
            adults: Guests, default 2.
            rooms: Rooms, default 1.
            trip_id: The trip_id from this call's earlier searches, so the
                hotel joins the same trip.
        """
        key, conv = _ids(ctx)
        return await do_search_hotels(key, conv, city, check_in, check_out, adults, rooms, trip_id)

    @app.tool(structured_output=True, annotations=_READ)
    async def hotel_rates(ctx: Context, trip_id: str, option: int) -> dict[str, Any]:
        """The rooms and rates for one hotel option, with each rate's free
        cancellation deadline. Call after the caller picks a hotel and before
        book_hotel; then ask which rate they want.

        Args:
            trip_id: The trip_id from search_hotels.
            option: The hotel option number the caller chose.
        """
        key, conv = _ids(ctx)
        return await do_hotel_rates(key, conv, trip_id, option)

    @app.tool(structured_output=True, annotations=_WRITE)
    async def book_hotel(ctx: Context, trip_id: str, option: int, rate: int, guests: list[dict],
                         email: str, phone: str, special_requests: str = "") -> dict[str, Any]:
        """Book one rate at one hotel option. Collect the guest names, email
        and phone, read them back, wait for a clear yes. The rate is
        re-quoted first; a changed price is reported, not booked.

        Safe to call once; `already` means it exists, confirm it and stop.

        Args:
            trip_id: The trip_id from search_hotels.
            option: The hotel option number.
            rate: The rate number from hotel_rates (1 if there was one).
            guests: One {"given_name", "family_name"} per guest, lead first.
            email: The lead guest's email, read back first.
            phone: The lead guest's phone with country code.
            special_requests: Anything for the hotel, if the caller says.
        """
        key, conv = _ids(ctx)
        return await do_book_hotel(key, conv, trip_id, option, rate, guests, email, phone,
                                   special_requests)

    @app.tool(structured_output=True, annotations=_DESTRUCTIVE)
    async def cancel_hotel(ctx: Context, reference: str, confirm: bool = False) -> dict[str, Any]:
        """Cancel a hotel booking in two steps: confirm false to learn the
        refund, read it back, get a clear yes, then confirm true.

        Args:
            reference: The hotel's `reference` (or booking_id) from
                find_bookings or the booking result. Never say it aloud.
            confirm: False to quote, true to cancel.
        """
        key, conv = _ids(ctx)
        return await do_cancel_hotel(key, conv, reference, confirm)

    @app.tool(structured_output=True, annotations=_READ)
    async def plan_trip(ctx: Context, trip_id: str, places: list[dict],
                        dining: list[dict] | None = None) -> dict[str, Any]:
        """Lay out the caller's whole day-by-day plan in ONE call, once they
        have agreed to one. Look the places up first, then hand over
        everything you found and the days are filled in for you: which day
        each place lands on, the start times, and the meals. Never plan the
        days yourself and never add them one at a time. The result names one
        day as an example; read that, not the whole plan.

        Args:
            trip_id: The trip_id for this call.
            places: The sights and activities you looked up, in the order
                you would suggest them: {"title", "note": one line from the
                lookup saying what it is, never written from memory, "url":
                the Google Maps link}.
            dining: The restaurants you looked up, same shape. Leave it out
                if you have none: no meals are better than invented ones.
        """
        key, conv = _ids(ctx)
        return await do_plan_trip(key, conv, trip_id, places, dining)

    @app.tool(structured_output=True, annotations=_READ)
    async def plan_activity(ctx: Context, trip_id: str, day: str, title: str, note: str = "",
                            url: str = "", time: str = "") -> dict[str, Any]:
        """Add ONE place or activity to a day of an existing plan, or move
        something the caller asked to change. For a first plan use plan_trip.

        Args:
            trip_id: The trip_id for this call.
            day: A date (YYYY-MM-DD) or "day 2".
            title: The place or activity, as spoken to the caller.
            note: One line of detail, optional.
            url: The place's Google Maps link if you have one, optional.
            time: When in the day: morning, afternoon, evening, or 13:00.
        """
        key, conv = _ids(ctx)
        return await do_plan_activity(key, conv, trip_id, day, title, note, url, time)

    @app.tool(structured_output=True, annotations=_READ)
    async def plan_days(ctx: Context, trip_id: str, items: list[dict]) -> dict[str, Any]:
        """Save a plan you have already laid out day by day, when the
        caller dictated the days themselves. For a first plan built from
        places you looked up, use plan_trip and let the days be filled in.
        Look places up first; never invent one. The result names one day as
        an example; read that, not the whole plan.

        Args:
            trip_id: The trip_id for this call.
            items: One entry per activity: {"day": "day 2" or a date,
                "time": a start time such as 10:00 (morning, afternoon or
                evening only if the caller prefers), "title", "note": one
                line from the place lookup saying what it is and why go,
                never written from memory, "url": the Google Maps link}.
        """
        key, conv = _ids(ctx)
        return await do_plan_days(key, conv, trip_id, items)

    @app.tool(structured_output=True, annotations=_READ)
    async def trip_summary(ctx: Context, trip_id: str) -> dict[str, Any]:
        """The whole trip in writing: flights, hotel, plan, totals and
        references. Call when the caller wants a recap, or before emailing
        the itinerary: the `summary` field is the email body, word for word.

        Args:
            trip_id: The trip_id for this call.
        """
        key, conv = _ids(ctx)
        return await do_trip_summary(key, conv, trip_id)

    return app


# ─── operational check (used by the API when a connection is saved) ─────────

async def check_key(key: str) -> dict[str, Any]:
    """Is this token real, and is it test or live? The mode is stored on the
    connection and shown on the card, and every booking result already says
    which it was, so nobody mistakes a sandbox ticket for a real one."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "no access token was supplied"}
    if not key.startswith(("duffel_test_", "duffel_live_")):
        return {
            "ok": False,
            "error": "a Duffel access token starts with duffel_test_ or duffel_live_"
                     " (create one under Developers, Access tokens)",
        }
    try:
        await asyncio.wait_for(_call(key, "GET", "/air/airlines", params={"limit": 1}),
                               TIMEOUT_S + 5)
    except DuffelError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"duffel check failed: {e!r}")
        return {"ok": False, "error": "Duffel could not be reached"}
    mode = mode_of(key)
    warnings = []
    if mode == "live":
        warnings.append("Live token: real tickets are issued and your Duffel balance is"
                        " charged for every booking this agent makes.")
    else:
        warnings.append("Test token: bookings land on Duffel Airways and the agent says"
                        " so on every booking.")
    # Stays is a separate product Duffel switches on per account. Probe it,
    # and when it is off hide the hotel tools rather than offer four ticks
    # that can only fail mid-call. The first live call found this out the
    # hard way: the agent apologised, then invented three hotels.
    stays: bool | None = None
    try:
        await asyncio.wait_for(_call(key, "GET", "/stays/bookings", params={"limit": 1}),
                               TIMEOUT_S)
        stays = True
    except DuffelError as e:
        if NOT_ENABLED in str(e):
            stays = False
    except Exception as e:
        logger.warning(f"duffel stays probe failed: {e!r}")
    out: dict[str, Any] = {"ok": True, "mode": mode, "label": f"Duffel ({mode} mode)",
                           "warnings": warnings, "stays": stays}
    if stays is False:
        out["hide_tools"] = list(HOTEL_TOOLS)
        warnings.append("Hotels (Duffel Stays) are not enabled on this Duffel account, so"
                        " the hotel tools are hidden and the sample hotel rates stay on."
                        " Ask Duffel to enable Stays, then press Test again.")
    return out
