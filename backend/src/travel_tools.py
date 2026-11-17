"""Travel-concierge toolset: a gated flight + hotel + day-plan booking flow.

Adapted from an internal LiveKit travel agent; the flow and prompts were
molded onto Arya's tool pattern rather than copied. Everything follows the
same contract as tools.py — deterministic Python owns every spoken string,
the docstring is the only interface the LLM sees, and hard gates in code
(not the prompt) enforce the conversation order: a small, fast model cannot
reliably remember which flight leg it is on or whether the hotel is
confirmed across turns, so the tools refuse to run ahead of the caller even
if the model tries to.

Backends, selected automatically per call:
  - live: with a SerpAPI key stored (keys.json "serpapi"), searches hit the
    structured Google Flights / Hotels / Maps engines.
  - mock: without a key, canned fixtures in the same trimmed shape answer
    instantly, so the template works end-to-end with zero extra keys. The
    model cannot tell the two apart — replace nothing to go live, just add
    the key.

State is per-call: build_travel_tools() creates one _TripState captured by
the tool closures (build_tools() runs once per session in agent.py), so
concurrent calls never share an itinerary.

Beyond the spoken flow, the toolset feeds two UI surfaces:
  - options_stash: each search appends its structured options; the worker
    publishes them on the arya.options text-stream topic so the transcript
    can render option cards next to the spoken sentence (symmetric with the
    web-search source chips).
  - the written itinerary: every save (flight, hotel, recommendations)
    re-renders the trip into an HTML page (itinerary_html.render), upserts
    it into the artifacts table keyed by conversation, and announces the
    stable link on the arya.artifact topic — the UI shows a "View
    itinerary" button that always opens the current version.

Deliberately NOT here (matches Arya's architecture): no end_call or
transfer tool (see tools.py header) — the recap_itinerary tool speaks the
itinerary; the artifact page is the visual counterpart.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import quote_plus

import httpx
from livekit.agents import RunContext, function_tool

import day_plan
import display_fmt as disp
import itinerary_sync

logger = logging.getLogger("travel_tools")

_SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
_CABIN_CODES = {"Economy": 1, "Business": 3, "First": 4}
_TOP_N = 3
_MAX_SIGHTS_PER_DAY = day_plan.MAX_SIGHTS_PER_DAY


# ─── canned fixtures (mock backend) ─────────────────────────────────────────
# Stored already in the trimmed "summary" shape the live parsers produce, so
# both backends funnel into the same spoken formatters. Keyed with a "*"
# catch-all so any offline query still returns plausible results.

_CANNED_FLIGHTS = {
    ("JFK", "CDG"): [
        {"airline": "Air France", "price": "$842", "duration": "7 hours 25 minutes",
         "stops": "nonstop", "depart": "19:30", "arrive": "08:55", "flight_number": "AF023"},
        {"airline": "Delta", "price": "$798", "duration": "7 hours 40 minutes",
         "stops": "nonstop", "depart": "22:15", "arrive": "11:55", "flight_number": "DL264"},
        {"airline": "Norse Atlantic", "price": "$624", "duration": "9 hours 50 minutes",
         "stops": "1 stop", "depart": "16:05", "arrive": "10:55", "flight_number": "N0710"},
    ],
    ("JFK", "LHR"): [
        {"airline": "British Airways", "price": "$712", "duration": "7 hours 5 minutes",
         "stops": "nonstop", "depart": "20:40", "arrive": "08:45", "flight_number": "BA178"},
        {"airline": "Virgin Atlantic", "price": "$689", "duration": "7 hours 10 minutes",
         "stops": "nonstop", "depart": "22:00", "arrive": "10:10", "flight_number": "VS004"},
        {"airline": "JetBlue", "price": "$641", "duration": "7 hours 20 minutes",
         "stops": "nonstop", "depart": "23:30", "arrive": "11:50", "flight_number": "B6014"},
    ],
    ("LAX", "NRT"): [
        {"airline": "ANA", "price": "$986", "duration": "11 hours 45 minutes",
         "stops": "nonstop", "depart": "11:20", "arrive": "15:05", "flight_number": "NH175"},
        {"airline": "Japan Airlines", "price": "$1,024", "duration": "11 hours 50 minutes",
         "stops": "nonstop", "depart": "12:40", "arrive": "16:30", "flight_number": "JL061"},
        {"airline": "Singapore Airlines", "price": "$938", "duration": "14 hours 10 minutes",
         "stops": "1 stop", "depart": "09:15", "arrive": "17:25", "flight_number": "SQ012"},
    ],
    "*": [
        {"airline": "United", "price": "$734", "duration": "8 hours 15 minutes",
         "stops": "nonstop", "depart": "18:10", "arrive": "07:25", "flight_number": "UA930"},
        {"airline": "Lufthansa", "price": "$705", "duration": "9 hours 30 minutes",
         "stops": "1 stop", "depart": "15:45", "arrive": "09:15", "flight_number": "LH441"},
        {"airline": "Iberia", "price": "$668", "duration": "10 hours 5 minutes",
         "stops": "1 stop", "depart": "21:20", "arrive": "12:25", "flight_number": "IB6252"},
    ],
}


def _rates(s1, r1, t1, s2, r2, t2):
    return [{"source": s1, "rate_per_night": r1, "total_rate": t1},
            {"source": s2, "rate_per_night": r2, "total_rate": t2}]


_CANNED_HOTELS = {
    "paris": [
        {"name": "Le Marais Boutique", "price_per_night": "$210", "rating": "4.6 (2,142 reviews)",
         "rating_value": 4.6, "reviews_count": 2142, "hotel_class": "4-star",
         "rate_options": _rates("Booking.com", "$210", "$1,470", "Expedia", "$225", "$1,575")},
        {"name": "Hotel Rivoli Rooms", "price_per_night": "$165", "rating": "4.3 (1,508 reviews)",
         "rating_value": 4.3, "reviews_count": 1508, "hotel_class": "3-star",
         "rate_options": _rates("Booking.com", "$165", "$1,155", "Hotels.com", "$172", "$1,204")},
        {"name": "Eiffel Grand Suites", "price_per_night": "$318", "rating": "4.8 (3,004 reviews)",
         "rating_value": 4.8, "reviews_count": 3004, "hotel_class": "5-star",
         "rate_options": _rates("Booking.com", "$318", "$2,226", "Expedia", "$334", "$2,338")},
    ],
    "london": [
        {"name": "Covent Garden Inn", "price_per_night": "$198", "rating": "4.5 (1,890 reviews)",
         "rating_value": 4.5, "reviews_count": 1890, "hotel_class": "4-star",
         "rate_options": _rates("Booking.com", "$198", "$1,386", "Expedia", "$209", "$1,463")},
        {"name": "Thames View Hotel", "price_per_night": "$142", "rating": "4.1 (2,377 reviews)",
         "rating_value": 4.1, "reviews_count": 2377, "hotel_class": "3-star",
         "rate_options": _rates("Booking.com", "$142", "$994", "Hotels.com", "$149", "$1,043")},
        {"name": "Mayfair Residence", "price_per_night": "$364", "rating": "4.9 (1,205 reviews)",
         "rating_value": 4.9, "reviews_count": 1205, "hotel_class": "5-star",
         "rate_options": _rates("Booking.com", "$364", "$2,548", "Expedia", "$379", "$2,653")},
    ],
    "tokyo": [
        {"name": "Shinjuku Sky Hotel", "price_per_night": "$176", "rating": "4.6 (2,910 reviews)",
         "rating_value": 4.6, "reviews_count": 2910, "hotel_class": "4-star",
         "rate_options": _rates("Booking.com", "$176", "$1,232", "Expedia", "$184", "$1,288")},
        {"name": "Asakusa Ryokan Stay", "price_per_night": "$121", "rating": "4.4 (1,633 reviews)",
         "rating_value": 4.4, "reviews_count": 1633, "hotel_class": "3-star",
         "rate_options": _rates("Booking.com", "$121", "$847", "Hotels.com", "$127", "$889")},
        {"name": "Ginza Prince Tower", "price_per_night": "$402", "rating": "4.8 (2,088 reviews)",
         "rating_value": 4.8, "reviews_count": 2088, "hotel_class": "5-star",
         "rate_options": _rates("Booking.com", "$402", "$2,814", "Expedia", "$419", "$2,933")},
    ],
    "*": [
        {"name": "City Center Grand", "price_per_night": "$189", "rating": "4.4 (1,720 reviews)",
         "rating_value": 4.4, "reviews_count": 1720, "hotel_class": "4-star",
         "rate_options": _rates("Booking.com", "$189", "$1,323", "Expedia", "$198", "$1,386")},
        {"name": "Old Town Boutique", "price_per_night": "$134", "rating": "4.2 (982 reviews)",
         "rating_value": 4.2, "reviews_count": 982, "hotel_class": "3-star",
         "rate_options": _rates("Booking.com", "$134", "$938", "Hotels.com", "$140", "$980")},
        {"name": "Riverside Luxury Hotel", "price_per_night": "$296", "rating": "4.7 (1,451 reviews)",
         "rating_value": 4.7, "reviews_count": 1451, "hotel_class": "5-star",
         "rate_options": _rates("Booking.com", "$296", "$2,072", "Expedia", "$309", "$2,163")},
    ],
}

# 12 sights per city — enough for a 4-day plan at 3/day; longer stays get
# them spread across every day (see _distribute), never a truncated plan.
_CANNED_SIGHTS = {
    "paris": [
        {"title": "Louvre Museum", "description": "World's largest art museum, home to the Mona Lisa.", "rating": 4.7},
        {"title": "Eiffel Tower", "description": "Iconic 330m iron tower with city-wide views.", "rating": 4.6},
        {"title": "Notre-Dame Cathedral", "description": "Medieval Gothic cathedral on the Ile de la Cite.", "rating": 4.7},
        {"title": "Montmartre", "description": "Hilltop artists' quarter with the Sacre-Coeur basilica.", "rating": 4.5},
        {"title": "Musee d'Orsay", "description": "Impressionist masterpieces in a former railway station.", "rating": 4.7},
        {"title": "Arc de Triomphe", "description": "Napoleon's triumphal arch atop the Champs-Elysees.", "rating": 4.7},
        {"title": "Sainte-Chapelle", "description": "Gothic chapel famous for its stained-glass walls.", "rating": 4.7},
        {"title": "Palace of Versailles", "description": "Opulent former royal palace with vast gardens.", "rating": 4.7},
        {"title": "Luxembourg Gardens", "description": "Formal gardens and a palace, popular with locals.", "rating": 4.7},
        {"title": "Centre Pompidou", "description": "Modern art museum in a bold inside-out building.", "rating": 4.5},
        {"title": "Champs-Elysees", "description": "Grand avenue for shopping, cafes, and people-watching.", "rating": 4.4},
        {"title": "Seine River Cruise", "description": "Boat tour past the city's landmarks, lovely at night.", "rating": 4.6},
    ],
    "london": [
        {"title": "British Museum", "description": "Two million years of human history, free to enter.", "rating": 4.7},
        {"title": "Tower of London", "description": "Historic castle housing the Crown Jewels.", "rating": 4.6},
        {"title": "The London Eye", "description": "135m observation wheel on the South Bank.", "rating": 4.5},
        {"title": "Westminster Abbey", "description": "Gothic abbey and coronation church.", "rating": 4.6},
        {"title": "Borough Market", "description": "Sprawling food market near London Bridge.", "rating": 4.5},
        {"title": "Buckingham Palace", "description": "Official residence of the monarch.", "rating": 4.5},
        {"title": "Camden Market", "description": "Eclectic market for food, fashion, and music.", "rating": 4.4},
        {"title": "Hyde Park", "description": "Central London's largest royal park.", "rating": 4.6},
        {"title": "St Paul's Cathedral", "description": "Wren's Baroque masterpiece with a famous dome.", "rating": 4.7},
        {"title": "Natural History Museum", "description": "Dinosaur skeletons in a grand building, free entry.", "rating": 4.7},
        {"title": "Notting Hill", "description": "Colorful houses and the Portobello Road market.", "rating": 4.5},
        {"title": "Greenwich", "description": "Royal Observatory, the Prime Meridian, and a maritime park.", "rating": 4.6},
    ],
    "tokyo": [
        {"title": "Senso-ji Temple", "description": "Tokyo's oldest temple, in historic Asakusa.", "rating": 4.5},
        {"title": "Shibuya Crossing", "description": "The world's busiest pedestrian scramble.", "rating": 4.5},
        {"title": "Meiji Shrine", "description": "Forested Shinto shrine beside Harajuku.", "rating": 4.6},
        {"title": "teamLab Planets", "description": "Immersive digital art museum you walk through.", "rating": 4.6},
        {"title": "Tsukiji Outer Market", "description": "Street-food stalls and fresh seafood.", "rating": 4.4},
        {"title": "Tokyo Tower", "description": "Red-and-white communications tower with city views.", "rating": 4.4},
        {"title": "Akihabara", "description": "Electronics, anime, and gaming district.", "rating": 4.4},
        {"title": "Shinjuku Gyoen", "description": "Park blending Japanese, French, and English gardens.", "rating": 4.6},
        {"title": "Odaiba", "description": "Futuristic waterfront district with a Rainbow Bridge view.", "rating": 4.4},
        {"title": "Ueno Park", "description": "Museums, a zoo, and cherry blossoms in season.", "rating": 4.5},
        {"title": "Ginza", "description": "Upscale shopping and dining district.", "rating": 4.4},
        {"title": "teamLab Borderless", "description": "A second, distinct immersive digital art museum.", "rating": 4.6},
    ],
    "*": [
        {"title": "Historic Old Town", "description": "Cobbled streets, landmark squares, and cafes.", "rating": 4.5},
        {"title": "National Museum", "description": "The city's flagship art and history collection.", "rating": 4.6},
        {"title": "Central Park and Gardens", "description": "Green space for a relaxed stroll.", "rating": 4.5},
        {"title": "Waterfront Promenade", "description": "Scenic walk with views and local restaurants.", "rating": 4.4},
        {"title": "Local Food Market", "description": "Regional specialties and street food.", "rating": 4.4},
        {"title": "Botanical Garden", "description": "Curated plant collections and greenhouses.", "rating": 4.5},
        {"title": "Art District", "description": "Galleries, murals, and independent studios.", "rating": 4.4},
        {"title": "Historic Cathedral", "description": "The city's principal house of worship.", "rating": 4.6},
        {"title": "Scenic Viewpoint", "description": "Elevated lookout over the skyline.", "rating": 4.6},
        {"title": "Craft Market", "description": "Local artisans selling handmade goods.", "rating": 4.3},
        {"title": "City Walking Tour", "description": "Guided tour through the main historic sights.", "rating": 4.5},
        {"title": "Riverside Park", "description": "Paths and picnic spots along the water.", "rating": 4.4},
    ],
}

_CANNED_DINING = {
    "paris": ["Le Comptoir du Relais", "Chez Janou", "Bouillon Pigalle", "L'As du Fallafel",
              "Cafe de Flore", "Angelina"],
    "london": ["Dishoom", "The Wolseley", "Flat Iron", "Padella", "Monmouth Coffee",
               "Borough Market Kitchen"],
    "tokyo": ["Ichiran Ramen", "Sushi Zanmai", "Afuri", "Gonpachi", "Tsuta", "Cafe Kitsune"],
    "*": ["The Old Town Brasserie", "Market Street Kitchen", "Riverside Trattoria",
          "The Courtyard Bistro", "Grand Cafe", "Corner Patisserie"],
}


# ─── small shared helpers ────────────────────────────────────────────────────


_MARKETING_SUFFIX = re.compile(r"\s*[\"\u201c\u2018\'][^\"\u201d\u2019\']{2,40}[\"\u201d\u2019\']\s*$")


def clean_hotel_name(name: str) -> str:
    """Google Hotels lists a property as its owner wrote it, marketing and
    all: 'Paris-Oasis "All Inclusive"'. The quoted tail is a sales label,
    not part of the name, and it reads badly spoken and printed. The raw
    name is kept for the Maps lookup, which needs it to find the place."""
    cleaned = _MARKETING_SUFFIX.sub("", (name or "").strip()).strip()
    return cleaned or (name or "").strip()


def _money_number(text) -> float | None:
    """'$842' / '1,024' -> 842.0 / 1024.0. None if nothing numeric — never
    raises, so a weird live price string can't crash a turn."""
    if text is None:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", str(text))
    try:
        return float(m.group(0).replace(",", "")) if m else None
    except ValueError:
        return None


def _fmt_money(value: float | None) -> str | None:
    return f"${value:,.0f}" if value is not None else None


def _say_money(text) -> str:
    """'$842' -> '842 dollars' — the appended voice rules ban currency
    symbols, so every SPOKEN price goes through here (the written
    itinerary and option cards keep the $ form)."""
    n = _money_number(text)
    if n is None:
        return "price unavailable"
    return f"{n:,.0f} dollars"


def _say_time(hhmm: str) -> str:
    """'19:30' -> '7:30 PM' so times read naturally in TTS."""
    try:
        h, m = str(hhmm).strip().split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return str(hhmm)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}" if m else f"{h12} {suffix}"


def _format_duration_hm(minutes) -> str:
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return "duration unavailable"
    h, m = minutes // 60, minutes % 60
    parts = []
    if h:
        parts.append(f"{h} hour{'' if h == 1 else 's'}")
    if m or not h:
        parts.append(f"{m} minute{'' if m == 1 else 's'}")
    return " ".join(parts)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _sort_flights(options: list[dict]) -> list[dict]:
    return sorted(options, key=lambda o: _money_number(o.get("price")) or float("inf"))


def _sort_hotels(options: list[dict]) -> list[dict]:
    return sorted(options, key=lambda o: (-(o.get("rating_value") or 0),
                                          _money_number(o.get("price_per_night")) or float("inf")))


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _date_range(start: str | None, end: str | None) -> list[date]:
    if not start:
        return []
    try:
        d0 = date.fromisoformat(start)
    except ValueError:
        return []
    try:
        d1 = date.fromisoformat(end) if end else d0
    except ValueError:
        d1 = d0
    if d1 < d0:
        d1 = d0
    out, cur = [], d0
    while cur <= d1:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _google_flights_url(dep: str, arr: str, when: str) -> str:
    """A real, clickable Google Flights results-page URL for this route and
    date — never a fabricated per-flight deep link."""
    q = quote_plus(f"flights from {dep} to {arr} on {when}")
    return f"https://www.google.com/travel/flights?q={q}"


def _maps_url(name: str, city: str = "") -> str:
    query = ", ".join(p for p in (name, city) if p)
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"


# ─── per-call state + gates ──────────────────────────────────────────────────


@dataclass
class _TripState:
    # flight slots — all required before a search may run
    from_city: str | None = None
    from_code: str | None = None
    to_city: str | None = None
    to_code: str | None = None
    depart_date: str | None = None
    return_date: str | None = None
    cabin_class: str | None = None      # Economy | Business | First
    trip_type: str | None = None        # one_way | round_trip
    adults: int | None = None
    children: int = 0
    infants: int = 0
    # round-trip leg sequencing
    leg_cursor: str = "outbound"
    same_airline: bool | None = None
    outbound_airline: str | None = None
    # last-search caches so select_* can index by number
    flight_results: dict = field(default_factory=lambda: {"outbound": [], "return": []})
    hotel_results: list = field(default_factory=list)
    shortlisted_hotel: dict | None = None
    rate_options: list = field(default_factory=list)
    # hotel dates + saved itinerary
    check_in: str | None = None
    check_out: str | None = None
    saved_flights: dict = field(default_factory=lambda: {"outbound": None, "return": None})
    saved_hotel: dict | None = None
    day_plan: list = field(default_factory=list)   # structured entries (see _build_day_plan)


def _missing_flight_slots(st: _TripState) -> list[str]:
    """Everything still needed before a flight search may run. This — not the
    prompt — is what enforces slot-filling."""
    missing = []
    if not st.from_city:
        missing.append("departure city")
    if not st.to_city:
        missing.append("destination city")
    if not st.depart_date:
        missing.append("departure date")
    if not st.trip_type:
        missing.append("whether it's one-way or round-trip")
    elif st.trip_type == "round_trip" and not st.return_date:
        missing.append("return date")
    if not st.cabin_class:
        missing.append("cabin class (economy, business, or first)")
    if not st.adults:
        missing.append("number of travelers")
    return missing


def _flights_incomplete(st: _TripState) -> str | None:
    """Why the flight step isn't finished — a hard gate so the model can't
    jump to hotels before both legs of a round trip are saved."""
    if not st.saved_flights["outbound"]:
        return "let's finish choosing your flight first"
    if st.trip_type == "round_trip" and not st.saved_flights["return"]:
        return "let's choose your return flight first"
    return None


# ─── data layer: serpapi live or canned mock, one output shape ──────────────


async def _serpapi_get(api_key: str, params: dict) -> dict:
    # google_flights in particular is a live scrape, not a cached lookup, and
    # SerpAPI documents it can take 15-30s+ under real conditions — 15s was
    # timing out live flight searches (seen in production logs as a bare,
    # message-less httpx.ReadTimeout).
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            _SERPAPI_ENDPOINT,
            params={**params, "api_key": api_key, "hl": "en", "currency": "USD"},
        )
        resp.raise_for_status()
        return resp.json()


async def _fetch_flights(st: _TripState, leg: str, api_key: str) -> tuple[list[dict], str | None]:
    """Returns (options, error). Round trips are two independent one-way
    searches — the return leg is the outbound route reversed."""
    if leg == "outbound":
        dep, arr, out_date = st.from_code, st.to_code, st.depart_date
    else:
        dep, arr, out_date = st.to_code, st.from_code, st.return_date
    if api_key:
        params = {
            "engine": "google_flights", "departure_id": dep, "arrival_id": arr,
            "outbound_date": out_date, "type": "2", "adults": st.adults or 1,
        }
        if st.children:
            params["children"] = st.children
        if st.infants:
            params["infants_in_seat"] = st.infants
        if st.cabin_class:
            params["travel_class"] = _CABIN_CODES.get(st.cabin_class, 1)
        try:
            data = await _serpapi_get(api_key, params)
        except Exception as exc:  # a search failure becomes a graceful turn
            logger.warning(f"flight search failed: {exc!r}")
            return [], "The flight search service is unavailable right now."
        raw = (data.get("best_flights") or []) + (data.get("other_flights") or [])
        options = []
        for opt in raw:
            legs = opt.get("flights") or []
            first, last = (legs[0] if legs else {}), (legs[-1] if legs else {})
            stops = len(legs) - 1
            options.append({
                "airline": first.get("airline", "Unknown airline"),
                "price": f"${opt.get('price')}" if opt.get("price") is not None else None,
                "duration": _format_duration_hm(opt.get("total_duration")),
                "stops": "nonstop" if stops <= 0 else f"{stops} stop" + ("" if stops == 1 else "s"),
                "depart": (first.get("departure_airport") or {}).get("time", "").split(" ")[-1] or "",
                "arrive": (last.get("arrival_airport") or {}).get("time", "").split(" ")[-1] or "",
                "depart_airport": (first.get("departure_airport") or {}).get("name", ""),
                "arrive_airport": (last.get("arrival_airport") or {}).get("name", ""),
                "flight_number": first.get("flight_number"),
            })
    else:
        options = [dict(o) for o in _CANNED_FLIGHTS.get((dep, arr), _CANNED_FLIGHTS["*"])]

    options = _sort_flights(options)
    if leg == "return" and st.same_airline and st.outbound_airline:
        # Filter client-side by airline name; fall back to the unfiltered
        # list rather than showing nothing.
        filtered = [o for o in options if o.get("airline") == st.outbound_airline]
        if filtered:
            options = filtered
    return options[:_TOP_N], None


async def _fetch_hotels(st: _TripState, location: str, api_key: str) -> tuple[list[dict], str | None]:
    if api_key:
        try:
            data = await _serpapi_get(api_key, {
                "engine": "google_hotels", "q": location,
                "check_in_date": st.check_in or "", "check_out_date": st.check_out or "",
                "adults": st.adults or 2,
            })
        except Exception as exc:
            logger.warning(f"hotel search failed: {exc!r}")
            return [], "The hotel search service is unavailable right now."
        options = []
        for prop in data.get("properties") or []:
            rate = prop.get("rate_per_night") or {}
            rating, reviews = prop.get("overall_rating"), prop.get("reviews")
            if isinstance(rating, (int, float)):
                rating = round(rating, 1)  # 4.809412 on a card reads as a bug
            rates = []
            for src in (prop.get("prices") or []):
                if not isinstance(src, dict):
                    continue
                per_night = src.get("rate_per_night")
                per_night = per_night.get("lowest") if isinstance(per_night, dict) else per_night
                total = src.get("total_rate")
                total = total.get("lowest") if isinstance(total, dict) else total
                if per_night or total:
                    rates.append({"source": src.get("source") or "Other source",
                                  "rate_per_night": per_night, "total_rate": total})
            if not rates:
                total = prop.get("total_rate") or {}
                rates = [{"source": "best available rate", "rate_per_night": rate.get("lowest"),
                          "total_rate": total.get("lowest") if isinstance(total, dict) else total}]
            raw_name = prop.get("name", "Unknown hotel")
            options.append({
                "name": clean_hotel_name(raw_name),
                "full_name": raw_name,
                "price_per_night": rate.get("lowest") or "price unavailable",
                "rating": f"{rating} ({reviews:,} reviews)" if rating and reviews else (str(rating) if rating else "unrated"),
                "rating_value": rating, "reviews_count": reviews,
                "hotel_class": prop.get("hotel_class", ""),
                "rate_options": rates,
            })
    else:
        options = list(_CANNED_HOTELS.get((location or "").strip().lower(), _CANNED_HOTELS["*"]))
    return _sort_hotels(options)[:_TOP_N], None


async def _fetch_sights(destination: str, api_key: str, count: int) -> tuple[list[dict], str | None]:
    """Fetch enough places for EVERY day of the stay (the caller computes
    `count` from the trip length) — fetching a fixed handful is what
    truncated long trips to a four-day plan."""
    if api_key:
        try:
            data = await _serpapi_get(api_key, {
                "engine": "google_maps", "q": f"things to do in {destination}", "type": "search"})
        except Exception as exc:
            logger.warning(f"recommendations search failed: {exc!r}")
            return [], "The recommendations service is unavailable right now."
        places = [{"title": p.get("title", ""), "description": p.get("description") or "",
                   "rating": p.get("rating")}
                  for p in (data.get("local_results") or []) if p.get("title")]
    else:
        places = list(_CANNED_SIGHTS.get((destination or "").strip().lower(), _CANNED_SIGHTS["*"]))
    places.sort(key=lambda p: -(p.get("rating") or 0))
    return places[:count], None


async def _fetch_dining(destination: str, api_key: str) -> list[str]:
    """Best-effort real restaurant names for meal lines; [] on any failure."""
    if api_key:
        try:
            data = await _serpapi_get(api_key, {
                "engine": "google_maps", "q": f"best restaurants in {destination}", "type": "search"})
            return [p.get("title", "") for p in (data.get("local_results") or []) if p.get("title")][:6]
        except Exception as exc:  # dining is a nicety, never fatal
            logger.warning(f"dining search failed: {exc!r}")
            return []
    return list(_CANNED_DINING.get((destination or "").strip().lower(), _CANNED_DINING["*"]))


# ─── day plan synthesis ──────────────────────────────────────────────────────

def _build_day_plan(st: _TripState, places: list[dict], dining: list[str]) -> list[dict]:
    """The stay laid out day by day. The layout itself lives in day_plan,
    shared with the connector path so a trip planned through Duffel and
    Google Maps gets the same shaped page as this desk builds."""
    start = st.check_in or st.depart_date
    end = st.check_out or st.return_date or st.depart_date
    location = (st.to_city or "") + (f" · {st.saved_hotel['name']}" if st.saved_hotel else "")
    return day_plan.build_days(_date_range(start, end), places, dining, location)


# ─── written itinerary payload ───────────────────────────────────────────────


def _itinerary_payload(st: _TripState, cfg: dict, mock: bool) -> dict:
    """Everything itinerary_html.render() needs, computed from state —
    totals are computed HERE (Python owns money), never by the model."""
    biz = (cfg.get("business") or {}).get("business_name") or cfg.get("name") or "Arya"
    travelers = st.adults or 1

    def _leg(rec, dep_code, arr_code, dep_city, arr_city, when):
        if not rec:
            return None
        unit = _money_number(rec["price"])
        return {
            "airline": rec["airline"], "stops": rec["stops"], "duration": rec["duration"],
            # Same structured shape the connector writer emits, so the page
            # has one layout to render. Times are local and the date is the
            # leg's own, which for a redeye arrival is the day after.
            "depart": {"time": rec["depart"], "date": when or "",
                       "airport": rec.get("depart_airport") or dep_city or "",
                       "code": dep_code or ""},
            "arrive": {"time": rec["arrive"], "date": when or "",
                       "airport": rec.get("arrive_airport") or arr_city or "",
                       "code": arr_code or ""},
            "price": rec["price"] or "n/a",
            "flight_number": rec.get("flight_number"),
            "url": _google_flights_url(dep_code or "", arr_code or "", when or ""),
            "total": _fmt_money(unit * travelers) if unit is not None else None,
        }

    outbound = _leg(st.saved_flights["outbound"], st.from_code, st.to_code,
                    st.from_city, st.to_city, st.depart_date)
    ret = _leg(st.saved_flights["return"], st.to_code, st.from_code,
               st.to_city, st.from_city, st.return_date)

    flight_total = sum(
        (_money_number(r["price"]) or 0) * travelers
        for r in (st.saved_flights["outbound"], st.saved_flights["return"]) if r
    )

    hotel = None
    hotel_total = None
    if st.saved_hotel:
        nights = max(len(_date_range(st.check_in, st.check_out)) - 1, 1)
        unit = _money_number(st.saved_hotel["price_per_night"])
        # The vendor's total wins: it is the number the caller heard, and it
        # knows about taxes and fees that nightly times nights does not.
        hotel_total = _money_number(st.saved_hotel.get("total"))
        if hotel_total is None:
            hotel_total = unit * nights if unit is not None else None
        hotel = {
            "name": st.saved_hotel["name"], "rating": st.saved_hotel["rating"],
            "source": st.saved_hotel.get("source") or "best available rate",
            "price_per_night": st.saved_hotel["price_per_night"] or "n/a",
            "nights": nights, "total": _fmt_money(hotel_total),
            # The raw listing name finds the property; the cleaned one is
            # what the reader sees.
            "maps_url": _maps_url(st.saved_hotel.get("full_name") or st.saved_hotel["name"],
                                  st.to_city or ""),
        }

    grand = None
    if flight_total or hotel_total is not None:
        grand = _fmt_money(flight_total + (hotel_total or 0))

    return {
        "brand": {"name": biz, "icon": cfg.get("icon") or "✈️", "mock": mock},
        "trip": {
            "origin_city": st.from_city, "origin_code": st.from_code,
            "dest_city": st.to_city, "dest_code": st.to_code,
            "depart_date": st.depart_date, "return_date": st.return_date,
            "travelers": travelers,
        },
        # Two one-way selections, each with its own fare, so the price
        # belongs under its own leg. The connector path books one round-trip
        # offer and says "journey" instead.
        "flights": {"outbound": outbound, "return": ret, "fare": {"scope": "leg"}},
        "hotel": hotel,
        "day_plan": st.day_plan,
        "totals": {"flight": _fmt_money(flight_total) if flight_total else None,
                   "hotel": _fmt_money(hotel_total), "grand": grand},
    }


# ─── spoken formatters — Python owns every string ────────────────────────────


def _summary_flights(options: list[dict]) -> str:
    """One clause for the default bubble: a price range and the cheapest
    airline, not every option. The full per-option lines stay in the
    returned string, for the model to read out only if the caller actually
    asks to hear them; the caller looks at the cards for the rest."""
    priced = [(_money_number(o.get("price")), o) for o in options]
    priced = [(p, o) for p, o in priced if p is not None]
    if not priced:
        return "at a price I could not read"
    priced.sort(key=lambda t: t[0])
    cheapest, priciest = priced[0][1], priced[-1][1]
    lo, hi = _say_money(cheapest["price"]), _say_money(priciest["price"])
    price_bit = lo if lo == hi else f"from {lo} to {hi}"
    return f"{price_bit}, the cheapest is {cheapest['airline']}"


def _spoken_flights(options: list[dict], leg: str, route: str, when: str) -> str:
    if not options:
        return f"I could not find any {leg} flights for that route and date. Want to try different dates?"
    lines = [
        f"Option {i}: {o['airline']}, {o['stops']}, {o['duration']}, "
        f"departs {_say_time(o['depart'])}, arrives {_say_time(o['arrive'])}, {_say_money(o['price'])}."
        for i, o in enumerate(options, 1)
    ]
    return (
        f"Top {leg} flights {route} on {when}: {_summary_flights(options)}. They are on the"
        f" caller's screen; ask which {leg} option to save. Read the full list out ONLY if the"
        f" caller asks to hear all the options: {' '.join(lines)}"
    )


def _summary_hotels(options: list[dict]) -> str:
    """Same idea as _summary_flights, for the hotel search bubble."""
    priced = [(_money_number(o.get("price_per_night")), o) for o in options]
    priced = [(p, o) for p, o in priced if p is not None]
    if not priced:
        return "at a price I could not read"
    priced.sort(key=lambda t: t[0])
    cheapest, priciest = priced[0][1], priced[-1][1]
    lo, hi = _say_money(cheapest["price_per_night"]), _say_money(priciest["price_per_night"])
    price_bit = f"{lo} per night" if lo == hi else f"from {lo} to {hi} per night"
    return f"{price_bit}, the top pick is {cheapest['name']}"


def _spoken_hotels(options: list[dict], location: str) -> str:
    if not options:
        return f"I could not find hotels in {location} for those dates. Want to adjust the dates?"
    lines = []
    for i, o in enumerate(options, 1):
        cls = f" {o['hotel_class']}," if o.get("hotel_class") else ""
        rating = (f"rated {o['rating_value']} with {o['reviews_count']:,} reviews"
                  if o.get("rating_value") and o.get("reviews_count") else f"rated {o['rating']}")
        lines.append(f"Option {i}: {o['name']},{cls} {rating}, from {_say_money(o['price_per_night'])} per night.")
    return (
        f"Top hotels in {location}: {_summary_hotels(options)}. They are on the caller's"
        " screen; ask which one to look at rates for. Read the full list out ONLY if the"
        f" caller asks to hear all the options: {' '.join(lines)}"
    )


def _rate_phrase(o: dict) -> str:
    per_night, total = o.get("rate_per_night"), o.get("total_rate")
    if per_night and total:
        return f"{_say_money(per_night)} per night, {_say_money(total)} total"
    if per_night:
        return f"{_say_money(per_night)} per night"
    if total:
        return f"{_say_money(total)} total"
    return "price unavailable"


def _spoken_rates(hotel_name: str, options: list[dict]) -> str:
    """A single rate is phrased as a straight confirmation — 'Option 1 of 1,
    which rate?' reads oddly when there is nothing to choose between."""
    if not options:
        return f"I could not find rate options for {hotel_name} right now."
    if len(options) == 1:
        return f"The best available rate for {hotel_name} is {_rate_phrase(options[0])}. Shall I lock it in?"
    parts = [f"For {hotel_name}, here are the rate options."]
    for i, o in enumerate(options, 1):
        parts.append(f"Option {i}: {_rate_phrase(o)} via {o.get('source', 'unknown source')}.")
    parts.append("Which rate should I lock in?")
    return " ".join(parts)


# ─── tool factory ─────────────────────────────────────────────────────────────


def build_travel_tools(cfg: dict, keys: dict, options_stash: list | None = None,
                       runtime: dict | None = None,
                       parts: tuple[str, ...] = ("flights", "hotels", "places")) -> list:
    """The travel-concierge toolset. One _TripState per call; live SerpAPI
    when a key is stored, canned fixtures otherwise.

    parts: which sample desks to build. "flights" is behind `tools.travel`,
    the one a flights connector (Duffel) replaces; "hotels" (SerpAPI rates,
    a shortlist and never a booking) is behind `tools.hotels`, replaced only
    once a connector's hotel tools are ticked; "places" is the things-to-do
    planner behind `tools.places`, the one a places connector replaces. They
    share the
    trip state so a plan built after a saved hotel spans the right days,
    but each stands alone: the planner asks for a city when no trip is
    saved. The date helper lives in tools.py, because connector tools
    need it too.

    options_stash (optional): searches append their structured option
    groups here; the worker publishes them per assistant turn on the
    arya.options topic and persists them with the message row. Items are
    {n, label, detail, value, pick, display?}; in a live call the UI
    renders each as a button whose tap sends `pick` back as a typed turn,
    so the normal select_* tools handle it. `label`/`detail`/`value` stay
    the spoken-shaped strings (also the row's accessible name); `display`
    is the same facts reshaped for a screen (24-hour times, a real
    currency symbol) and is optional so an older stored card without it
    still renders. Rows carry no links by design.

    runtime (optional): {"conversation_id", "agent_id", "dsn", "room"} —
    when present, every itinerary-changing save re-renders the written
    itinerary, upserts it into the artifacts table (stable id per
    conversation), and announces {id, url, title} on the arya.artifact
    topic. Absent (console mode / tests), the spoken flow is unchanged.
    """
    st = _TripState()
    api_key = (keys or {}).get("serpapi", "")
    logger.info(f"travel tools: {'live serpapi' if api_key else 'mock fixtures'} backend")

    def _stash(kind: str, title: str, items: list[dict], display: dict | None = None) -> None:
        if options_stash is not None and items:
            # `pick` is what a tap on the card should send. "Option 1" after
            # a rates card was read as hotel 1 (seen live), and a lone rate
            # is a yes-or-no question, not a numbered choice.
            for it in items:
                if kind == "rates":
                    it.setdefault("pick", "Yes" if len(items) == 1 else f"Rate {it['n']}")
                else:
                    it.setdefault("pick", f"Option {it['n']}")
            group = {"kind": kind, "title": title, "items": items}
            if display:
                group["display"] = display
            options_stash.append(group)

    def _sync_itinerary(stage: str) -> None:
        """Re-render, store and announce the written itinerary (see
        itinerary_sync). Never blocks or breaks the spoken turn."""
        if not runtime or not runtime.get("conversation_id"):
            return
        payload = _itinerary_payload(st, cfg, mock=not api_key)
        title = f"Trip to {st.to_city}" if st.to_city else "Trip itinerary"
        itinerary_sync.sync(payload, title=title, stage=stage, runtime=runtime)

    @function_tool
    async def search_flights(
        context: RunContext,
        leg: str = "outbound",
        departure_city: str | None = None,
        arrival_city: str | None = None,
        departure_id: str | None = None,
        arrival_id: str | None = None,
        depart_date: str | None = None,
        return_date: str | None = None,
        cabin_class: str | None = None,
        trip_type: str | None = None,
        adults: int | None = None,
        children: int | None = None,
        infants: int | None = None,
        same_airline: bool | None = None,
    ):
        """Collect the caller's flight details and search once everything
        required is known. Call this with WHATEVER details the caller has
        given so far, even if incomplete; it saves what you provide and
        tells you exactly what's still missing instead of searching
        prematurely. Call it again with leg="return" (after the outbound leg
        is confirmed) to search the return flight.

        Args:
            leg: "outbound" or "return".
            departure_city: Origin city name, e.g. New York.
            arrival_city: Destination city name, e.g. Paris.
            departure_id: Origin airport IATA code, e.g. JFK.
            arrival_id: Destination airport IATA code, e.g. CDG.
            depart_date: Departure date, YYYY-MM-DD.
            return_date: Return date, YYYY-MM-DD. Only for round trips.
            cabin_class: "Economy", "Business", or "First".
            trip_type: "one_way" or "round_trip".
            adults: Number of adult travelers.
            children: Number of children.
            infants: Number of infants.
            same_airline: Round trip only: whether the return leg should use
                the same airline as the confirmed outbound. Ask once, before
                searching the return leg.
        """
        leg = "return" if str(leg).lower().startswith("ret") else "outbound"
        st.leg_cursor = leg
        # Canonical trip identity is set ONLY on the outbound leg — the model
        # naturally passes the REVERSED route on the return call, and applying
        # it here would clobber the trip's real origin/destination/dates.
        if leg == "outbound":
            if departure_city:
                st.from_city = departure_city
            if arrival_city:
                st.to_city = arrival_city
            if departure_id:
                st.from_code = departure_id.upper()
            if arrival_id:
                st.to_code = arrival_id.upper()
            if depart_date:
                st.depart_date = depart_date
            if return_date:
                st.return_date = return_date
            if cabin_class:
                st.cabin_class = cabin_class.strip().title()
            if trip_type:
                st.trip_type = "round_trip" if "round" in trip_type.lower() else "one_way"
            if adults is not None:
                st.adults = _safe_int(adults, st.adults or 1)
            if children is not None:
                st.children = _safe_int(children)
            if infants is not None:
                st.infants = _safe_int(infants)
            # A return date ALWAYS means a round trip, even if the model
            # guessed one-way — otherwise the return leg never gets prompted.
            if st.return_date:
                st.trip_type = "round_trip"
        else:
            if return_date and not st.return_date:
                st.return_date = return_date
            st.trip_type = "round_trip"  # asking for a return leg implies it
        if same_airline is not None:
            st.same_airline = bool(same_airline)

        missing = _missing_flight_slots(st)
        if missing:
            return "I still need: " + ", ".join(missing) + ". Could you tell me?"
        if leg == "outbound" and not (st.from_code and st.to_code):
            return "I still need the airport codes. Pass the IATA code for each city, like JFK for New York."
        if leg == "return" and st.same_airline is None:
            return "Would you like the return flight on the same airline, or are you open to a different one?"

        options, error = await _fetch_flights(st, leg, api_key)
        if error:
            return f"{error} Apologize and offer to try again."
        st.flight_results[leg] = options
        if leg == "outbound":
            route = f"from {st.from_city} to {st.to_city}"
            title_route = f"{st.from_city} to {st.to_city}"
            dep_code, arr_code = st.from_code, st.to_code
            when = st.depart_date
        else:
            route = f"from {st.to_city} back to {st.from_city}"
            title_route = f"{st.to_city} to {st.from_city}"
            dep_code, arr_code = st.to_code, st.from_code
            when = st.return_date
        title = f"{'Flights' if leg == 'outbound' else 'Return flights'}, {title_route}"
        _stash("flights", title, [
            {
                "n": i, "label": o["airline"],
                "detail": f"{o['stops']} · {o['duration']} · {o['depart']} → {o['arrive']}",
                "value": o["price"] or "",
                "display": {
                    "airline": o["airline"], "airline_note": "",
                    "price": o["price"] or "", "price_note": "total",
                    "legs": [{
                        "dep": o["depart"], "arr": o["arrive"],
                        "from": dep_code or "", "to": arr_code or "",
                        "stops": (o["stops"] or "").capitalize(),
                        "duration": disp.shorten_duration_words(o["duration"] or ""),
                        "plus_days": 0,
                    }],
                },
            }
            for i, o in enumerate(options, 1)
        ], display={
            "from_code": dep_code or "", "to_code": arr_code or "",
            "from_city": st.from_city if leg == "outbound" else st.to_city,
            "to_city": st.to_city if leg == "outbound" else st.from_city,
            "dates": disp.fmt_day(when) if when else "",
            "pax": _plural(st.adults or 1, "adult"),
            "cabin": st.cabin_class or "Economy",
            "leg": "outbound" if leg == "outbound" else "return",
        })
        return _spoken_flights(options, leg, route, when)

    @function_tool
    async def select_flight(context: RunContext, option_number: int, leg: str = "outbound"):
        """Save the caller's chosen flight option for the given leg to the
        itinerary. Only call this after the caller explicitly picks one.

        Args:
            option_number: 1-based index from the most recent search for this leg.
            leg: "outbound" or "return".
        """
        leg = "return" if str(leg).lower().startswith("ret") else "outbound"
        options = st.flight_results.get(leg) or []
        if not options:
            return f"Let's search {leg} flights first, then I can save your choice."
        n = _safe_int(option_number)
        if n < 1 or n > len(options):
            return f"I only have {len(options)} options for the {leg} flight. Which number would you like?"
        chosen = options[n - 1]
        st.saved_flights[leg] = chosen
        _sync_itinerary("flight")
        if leg == "outbound":
            st.outbound_airline = chosen["airline"]
            follow_up = ("Now let's find your return flight." if st.trip_type == "round_trip"
                         else "Would you like help finding a hotel?")
        else:
            follow_up = "Would you like help finding a hotel?"
        return f"Saved your {leg} flight: {chosen['airline']} at {_say_money(chosen['price'])}. {follow_up}"

    @function_tool
    async def search_hotels(
        context: RunContext,
        check_in_date: str | None = None,
        check_out_date: str | None = None,
        city: str | None = None,
    ):
        """Search hotel rates in the destination city for the given
        check-in and check-out dates. Provide whatever is known; missing
        pieces will be asked for. These are rates to compare and shortlist,
        not bookings.

        Args:
            check_in_date: YYYY-MM-DD.
            check_out_date: YYYY-MM-DD.
            city: The city, when no flight search on this call has set it.
        """
        # The flights-first gate only makes sense when this desk owns the
        # flights. With a connector booking them, the hotel desk stands alone.
        blocked = _flights_incomplete(st) if "flights" in parts else None
        if blocked:
            return f"Before we look at hotels, {blocked}."
        if city and not st.to_city:
            st.to_city = city.strip()
        if check_in_date:
            st.check_in = check_in_date
        if check_out_date:
            st.check_out = check_out_date
        missing = []
        if not st.check_in:
            missing.append("check-in date")
        if not st.check_out:
            missing.append("check-out date")
        if missing:
            return "I still need: " + ", ".join(missing) + "."
        location = st.to_city or ""
        if not location:
            return "Which city should I search hotels in?"
        options, error = await _fetch_hotels(st, location, api_key)
        if error:
            return f"{error} Apologize and offer to try again."
        st.hotel_results = options
        _stash("hotels", f"Hotels, {location}", [
            {
                "n": i, "label": o["name"],
                "detail": " · ".join(x for x in (o.get("hotel_class"), f"rated {o['rating']}") if x),
                "value": f"{o['price_per_night']}/night",
                "display": {
                    "name": o["name"],
                    "stars": o.get("hotel_class") or "",
                    "rating": str(o.get("rating_value") or ""),
                    "reviews": f"{o['reviews_count']:,}" if o.get("reviews_count") else "",
                    "area": "",
                    "price": disp.fmt_money(o.get("price_per_night"), "USD"),
                    "price_note": "per night",
                    "cancel": "",
                },
            }
            for i, o in enumerate(options, 1)
        ], display={
            "city": location,
            "dates": (f"{disp.fmt_day(st.check_in)} to {disp.fmt_day(st.check_out)}"
                      if st.check_in and st.check_out else ""),
            "pax": _plural(st.adults or 2, "adult"),
        })
        return _spoken_hotels(options, location)

    @function_tool
    async def select_hotel(context: RunContext, option_number: int):
        """Shortlist the caller's chosen hotel from the most recent search.
        This does NOT save it yet. If this reply already offers a single
        rate, get the caller's yes and call select_room; otherwise call
        search_rooms next to get the rate options, then select_room.

        Args:
            option_number: 1-based index from the most recent hotel search.
        """
        if not st.hotel_results:
            return "Let's search hotels first, then I can save your choice."
        n = _safe_int(option_number)
        if n < 1 or n > len(st.hotel_results):
            return f"I only have {len(st.hotel_results)} hotel options. Which number would you like?"
        st.shortlisted_hotel = st.hotel_results[n - 1]
        st.rate_options = list(st.shortlisted_hotel.get("rate_options") or [])
        name = st.shortlisted_hotel["name"]
        # Live google_hotels usually returns a single rate, and a separate
        # search_rooms turn would only repeat the nightly price the caller
        # just heard in the hotel list. Collapse that case: offer the rate
        # and the lock-in question right here (select_room still does the
        # actual save). Genuine multi-rate results keep the two-step flow.
        if len(st.rate_options) == 1:
            # One rate is a yes-or-no question, not a choice, so no card.
            # Seen live: a lone card tapped as "Option 1" was read as hotel
            # option 1, twice, and the hotel was never noted at all.
            r = st.rate_options[0]
            return (f"Great choice: {name}. The best available rate is "
                    f"{_rate_phrase(r)}. Shall I note it on your itinerary?"
                    " When the caller agrees, call select_room in that turn.")
        return f"Great choice: {name}. Let me pull up rate options for you."

    @function_tool
    async def search_rooms(context: RunContext):
        """Get the rate and price options for the hotel the caller just
        shortlisted with select_hotel."""
        if not st.shortlisted_hotel:
            return "Let's pick a hotel first, then I can show rate options."
        # Cards are for choosing between things. With one rate the spoken
        # line is already a yes-or-no question (see _spoken_rates).
        if len(st.rate_options) > 1:
            _stash("rates", f"Rooms, {st.shortlisted_hotel['name']}", [
                {
                    "n": i, "label": o.get("source") or "best available rate",
                    "detail": f"{o['total_rate']} total" if o.get("total_rate") else "",
                    "value": f"{o['rate_per_night']}/night" if o.get("rate_per_night") else "",
                    "display": {
                        "room": o.get("source") or "Best available rate", "source": "",
                        "price": disp.fmt_money(o.get("total_rate"), "USD"),
                        "price_note": "total",
                        "cancel": "", "refundable": False,
                    },
                }
                for i, o in enumerate(st.rate_options, 1)
            ])
        return _spoken_rates(st.shortlisted_hotel["name"], st.rate_options)

    @function_tool
    async def select_room(context: RunContext, option_number: int | None = None):
        """Note the caller's chosen rate for the shortlisted hotel on the
        itinerary. Call it the moment the caller agrees (yes, sure, go
        ahead, note it) to the single rate you offered, or names a rate
        number from the list. The hotel is NOT on the itinerary until this
        returns, so never say it is noted without calling this first. It is
        a shortlist at today's rate, NOT a booking, and the caller must be
        told so.

        Args:
            option_number: 1-based rate number from search_rooms. Omit it
                when only one rate was offered and the caller agreed.
        """
        if not st.shortlisted_hotel or not st.rate_options:
            return "Let's look at rate options first, then I can save your choice."
        if len(st.rate_options) == 1:
            n = 1  # a plain "yes" confirms the only rate
        else:
            n = _safe_int(option_number)
            if n < 1 or n > len(st.rate_options):
                return f"I only have {len(st.rate_options)} rate options. Which number would you like?"
        chosen = st.rate_options[n - 1]
        hotel = st.shortlisted_hotel
        price = chosen.get("rate_per_night") or hotel.get("price_per_night")
        st.saved_hotel = {"name": hotel["name"], "rating": hotel["rating"],
                          "full_name": hotel.get("full_name") or hotel["name"],
                          "price_per_night": price, "source": chosen.get("source", ""),
                          # The vendor's own total, which is what the caller
                          # was just told. Recomputing nightly times nights
                          # printed 1,116 against a spoken 1,731.
                          "total": chosen.get("total_rate") or ""}
        _sync_itinerary("hotel")
        return (f"Noted {hotel['name']} at {_say_money(price)} per night via"
                f" {chosen.get('source') or 'the best available rate'} on the itinerary."
                " Tell the caller this is a shortlist at today's rate, not a booking."
                " Would you like recommendations for things to do?")

    @function_tool
    async def get_recommendations(context: RunContext, destination: str | None = None):
        """Fetch things to do at the destination and build a day-by-day plan
        covering the whole stay, saved to the itinerary.

        Args:
            destination: Defaults to the trip's saved destination city.
        """
        dest = destination or st.to_city or ""
        if not dest:
            return "Which city should I find things to do in?"
        stay = _date_range(st.check_in or st.depart_date,
                           st.check_out or st.return_date or st.depart_date)
        want = min(max(len(stay) * _MAX_SIGHTS_PER_DAY, 6), 30)
        places, error = await _fetch_sights(dest, api_key, want)
        if error:
            return f"{error} Apologize and offer to try again."
        if not places:
            return f"I could not find recommendations for {dest} right now."
        dining = await _fetch_dining(dest, api_key)
        st.day_plan = _build_day_plan(st, places, dining)
        _sync_itinerary("plan")
        top = ", ".join(p["title"] for p in places[:3])
        themes = ", ".join(dict.fromkeys(d["theme"] for d in st.day_plan)) or "top sights"
        day_bit = (f" I've planned all {len(st.day_plan)} days around {themes}."
                   if st.day_plan else "")
        return (f"Recommendations saved. Highlights in {dest} include {top}.{day_bit} "
                "Would you like me to recap your full itinerary, or email it to them"
                " now that it is finished?")

    @function_tool
    async def recap_itinerary(context: RunContext):
        """Read the caller's saved itinerary back: flights, hotel, and the
        day-by-day plan. Call once at least a flight is saved and the caller
        is ready to review, or any time they ask for their itinerary."""
        outbound, ret = st.saved_flights["outbound"], st.saved_flights["return"]
        if not (outbound or st.saved_hotel):
            return "I don't have anything saved yet. Let's pick a flight or hotel first."
        parts = []
        if st.from_city and st.to_city:
            dates = st.depart_date + (f" to {st.return_date}" if st.return_date else "")
            parts.append(f"Your trip: {st.from_city} to {st.to_city}, {dates},"
                         f" {st.adults or 1} traveler{'s' if (st.adults or 1) != 1 else ''}.")
        total = 0.0
        for label, rec in (("Outbound", outbound), ("Return", ret)):
            if not rec:
                continue
            parts.append(f"{label} flight: {rec['airline']}, {rec['stops']},"
                         f" departs {_say_time(rec['depart'])}, {_say_money(rec['price'])}.")
            total += (_money_number(rec["price"]) or 0) * (st.adults or 1)
        if st.saved_hotel:
            nights = max(len(_date_range(st.check_in, st.check_out)) - 1, 1)
            per_night = _money_number(st.saved_hotel["price_per_night"]) or 0
            total += per_night * nights
            parts.append(f"Hotel: {st.saved_hotel['name']}, {_say_money(st.saved_hotel['price_per_night'])}"
                         f" per night for {nights} night{'s' if nights != 1 else ''}.")
        if st.day_plan:
            parts.append("Day plan: " + " ".join(d["spoken"] for d in st.day_plan[:3])
                         + (" And more after that." if len(st.day_plan) > 3 else ""))
        if total:
            parts.append(f"Estimated total: {total:,.0f} dollars.")
        if runtime and runtime.get("conversation_id"):
            _sync_itinerary("recap")
            parts.append("The full written itinerary is on your screen too.")
        return " ".join(parts)

    out = []
    if "flights" in parts:
        out.extend([search_flights, select_flight, recap_itinerary])
    if "hotels" in parts:
        out.extend([search_hotels, select_hotel, search_rooms, select_room])
        if "flights" not in parts:
            out.append(recap_itinerary)
    if "places" in parts:
        out.append(get_recommendations)
    return out
