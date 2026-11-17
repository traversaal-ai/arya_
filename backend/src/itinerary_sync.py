"""The written itinerary: render, store, announce.

One trip page per conversation, re-rendered on every itinerary-changing
save and upserted into the artifacts table under a stable id, then
announced on the arya.artifact text-stream topic so the call UI's "View
itinerary" button always opens the current version. Used by the sample
travel desk (travel_tools) and, from Phase 2 of PLAN-Flight, by the Duffel
connector's results through connectors.spoken_result: both hand over the
same payload shape itinerary_html.render takes, so the page is the same
whichever built the trip.

Fire and forget by design: a UI nicety must never block or break a spoken
turn, so sync() schedules the work and swallows every failure into a log
line. write() is the awaitable core, for tests and for callers that need
the artifact id back.
"""

import asyncio
import json
import logging
import uuid

import db
import itinerary_html

logger = logging.getLogger("itinerary_sync")
_PENDING: set[asyncio.Task] = set()


_CURRENCY_SIGNS = {"$": "USD", "\u00a3": "GBP", "\u20ac": "EUR"}


def _present(value) -> bool:
    return value not in (None, "", [], {})


def money(text) -> tuple[float, str] | None:
    """"$850" or "1,285.09 USD" -> (amount, ISO code). Two writers format
    money differently and the merged total has to add them."""
    s = str(text or "").strip()
    if not s:
        return None
    code = ""
    for sign, iso in _CURRENCY_SIGNS.items():
        if sign in s:
            code, s = iso, s.replace(sign, "")
    parts = s.replace(",", "").split()
    number = next((p for p in parts if p.replace(".", "", 1).isdigit()), None)
    if number is None:
        return None
    code = code or next((p for p in parts if p.isalpha() and len(p) == 3), "")
    return float(number), code.upper()


def _grand(*totals) -> str | None:
    parsed = [money(t) for t in totals if _present(t)]
    if not parsed or any(p is None for p in parsed):
        return None
    codes = {c for _, c in parsed if c}
    if len(codes) > 1:
        return None  # two currencies do not add; show the parts, not a sum
    amount = sum(a for a, _ in parsed)
    code = next(iter(codes), "")
    return f"{amount:,.2f} {code}".strip()


def merge_payload(old: dict | None, new: dict) -> dict:
    """The itinerary is one page with several authors: the sample hotel desk
    writes from its own trip state, the Duffel module from its own, and each
    knows nothing of the other. Rendering whatever the last caller held
    replaced a page with flights by one without them, and vice versa (seen
    live: a chosen hotel vanished the moment the day plan was saved). So a
    writer replaces only the sections it actually has, and the total is
    recomputed from the merged parts."""
    if not old:
        return new
    out = dict(old)
    brand = {**(old.get("brand") or {}),
             **{k: v for k, v in (new.get("brand") or {}).items() if _present(v)}}
    if "test" in ((old.get("brand") or {}).get("mode"), (new.get("brand") or {}).get("mode")):
        brand["mode"] = "test"  # a sandbox anywhere on the page marks the page
    out["brand"] = brand
    out["trip"] = {**(old.get("trip") or {}),
                   **{k: v for k, v in (new.get("trip") or {}).items() if _present(v)}}
    of, nf = old.get("flights") or {}, new.get("flights") or {}
    out["flights"] = {leg: (nf.get(leg) if _present(nf.get(leg)) else of.get(leg))
                      for leg in set(of) | set(nf)}
    for key in ("hotel", "day_plan"):
        out[key] = new.get(key) if _present(new.get(key)) else old.get(key)
    out["day_plan"] = _with_hotel(out.get("day_plan"), (out.get("hotel") or {}).get("name"))
    ot, nt = old.get("totals") or {}, new.get("totals") or {}
    flight = nt.get("flight") if _present(nt.get("flight")) else ot.get("flight")
    hotel = nt.get("hotel") if _present(nt.get("hotel")) else ot.get("hotel")
    out["totals"] = {"flight": flight, "hotel": hotel, "grand": _grand(flight, hotel)}
    return out


def _with_hotel(days, hotel_name: str | None):
    """The grey line under a day heading reads "Paris · Hotel Name", which
    tells the reader where they are sleeping that night. Neither writer can
    produce it alone: the connector module never sees a hotel the sample
    desk noted, and the sample desk never sees connector flights. This is
    the one place that holds both, so it is where the two are joined."""
    if not days or not hotel_name:
        return days
    out = []
    for day in days:
        place = (day.get("location") or "").strip()
        # Only a bare city is filled in. A writer that already named the
        # hotel, or named something else entirely, is left alone.
        out.append({**day, "location": f"{place} · {hotel_name}"} if place and "·" not in place
                   else day)
    return out


def theme_for(runtime: dict) -> str:
    """Chosen once and baked into the stored page: embedded (widget) calls
    get the brandless palette, builder test calls match the builder UI."""
    return "neutral" if runtime.get("deployment_id") else "arya"


async def write(payload: dict, *, title: str, stage: str, runtime: dict) -> str | None:
    """Render, upsert, announce. Returns the artifact id, or None when the
    call has no conversation to attach the page to (console mode, tests)."""
    if not runtime or not runtime.get("conversation_id"):
        return None
    pool = await db.get_pool(runtime["dsn"])
    conversation = uuid.UUID(str(runtime["conversation_id"]))
    prior = await pool.fetchrow(
        "SELECT data FROM artifacts WHERE conversation_id = $1 AND kind = 'itinerary'",
        conversation,
    )
    old = None
    if prior is not None and prior["data"] is not None:
        old = prior["data"] if isinstance(prior["data"], dict) else json.loads(prior["data"])
    payload = merge_payload(old, payload)
    html = itinerary_html.render(payload, theme=theme_for(runtime))
    row = await pool.fetchrow(
        "INSERT INTO artifacts (id, conversation_id, agent_id, kind,"
        " title, html, data) VALUES ($1, $2, $3, 'itinerary', $4, $5, $6)"
        " ON CONFLICT (conversation_id, kind) DO UPDATE SET"
        " title = EXCLUDED.title, html = EXCLUDED.html,"
        " data = EXCLUDED.data, updated_at = now()"
        " RETURNING id",
        uuid.uuid4(), conversation,
        runtime.get("agent_id"), title, html, json.dumps(payload),
    )
    aid = str(row["id"])
    room = runtime.get("room")
    if room is not None:
        # `data` rides along so an embedding host page can render its own
        # live panel without a round trip; the url stays the printable page.
        await room.local_participant.send_text(
            json.dumps({"artifact": {
                "id": aid, "kind": "itinerary", "title": title,
                "url": f"/api/artifacts/{aid}",
                "stage": stage,
                "data": payload,
            }}),
            topic="arya.artifact",
        )
    return aid


def sync(payload: dict, *, title: str, stage: str, runtime: dict | None) -> None:
    """Schedule write() and never raise. `stage` tells the UI what triggered
    this sync ("flight", "hotel", "plan", "recap") so it can decide when the
    View itinerary action is worth surfacing."""
    if not runtime or not runtime.get("conversation_id"):
        return

    async def _run():
        try:
            await write(payload, title=title, stage=stage, runtime=runtime)
        except Exception as e:  # a UI nicety must never break a call
            logger.warning(f"itinerary sync skipped: {e}")

    # Held until done so the loop never garbage-collects a running write.
    task = asyncio.create_task(_run())
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
