"""Plan the days the way the sample desk always did: Python looks the
places up, Python lays them out, the model asks one question.

The sample desk (travel_tools.get_recommendations) fetched up to three
places for every day of the stay in ONE call, took Google's own
description for each, and handed the lot to a layout function. The model
chose nothing and wrote nothing, and the page that came out is the one
the user keeps pointing at.

Attaching Google Maps switched that desk off and gave the job back to the
model, which does not do it. Two live calls in a row went: "would you
like recommendations?", "shall I find restaurants too?", "shall I start
planning?", then one activity saved at a time with notes the model made
up. Eight tool calls for four days.

So the job comes back here. One tool, one call:

  - several `search_places` lookups at once, worded differently so a five
    day trip has more than one museum in it, five places each (the
    server's ceiling), deduplicated by place id,
  - restaurants in the same breath,
  - then the connector's own `plan_trip`, which already spreads places
    over the days, assigns the clock times, adds the meals and leaves the
    arrival and departure days light.

Nothing is invented: every name, sentence and link came from the lookup.
The model's part is to ask whether the caller wants a plan, and to read
one day back afterwards.

Built only when both halves are live on the call (a places connector and
a trip store), because a planner that can look places up and not save
them is worse than none.
"""

from __future__ import annotations

import asyncio
import logging

from livekit.agents import RunContext, function_tool

import connectors

logger = logging.getLogger("trip_planner")

PLACES_TOOL = "search_places"
PLAN_TOOL = "plan_trip"
TOOL_NAME = "plan_the_days"

# Worded differently on purpose. One query repeated returns one kind of
# place, and a week of museums is not a trip. The order is the order they
# are offered in, so a caller hears the famous things first.
SIGHT_QUERIES = [
    "top sights in {city}",
    "museums and galleries in {city}",
    "parks, gardens and viewpoints in {city}",
    "historic neighbourhoods to walk in {city}",
]
DINING_QUERY = "best restaurants in {city}"
# Grounding Lite caps a page at five however much is asked for, measured
# 2026-09-15. Three sights a day is what the layout wants.
PAGE_SIZE = 5
PER_DAY = 3
MAX_DINING = 6
# All the lookups run at once; this is the slowest of them, not a sum.
LOOKUP_BUDGET_S = 20.0


def _find(live: list[dict], tool: str) -> dict | None:
    for conn in live or []:
        if tool in (conn.get("tool_names") or []):
            return conn
    return None


def available(live: list[dict]) -> bool:
    """Both halves present: somewhere to look places up, and somewhere to
    put them."""
    return _find(live, PLACES_TOOL) is not None and _find(live, PLAN_TOOL) is not None


def prompt_block() -> str:
    """Appended to the instructions when the tool is built, because the
    catalog's own blocks cannot know whether it is. Says the flow, never
    the tool name."""
    return (
        "\n\nThe day-by-day plan is built for you in one step: when the caller"
        " agrees to one, do not ask which sights or which restaurants and do not"
        " look anything up first. Make the one call, then read ONE day back as an"
        " example and ask whether they would like anything changed or the"
        " itinerary emailed. Ask about the plan once, not three times."
    )


async def _lookup(conn: dict, query: str) -> list[dict]:
    """One search_places call, already shaped by connectors.SHAPERS into
    {name, about, rating, maps_url}. Failures are empty, never fatal: a
    plan from three queries instead of four is still a plan."""
    try:
        res = await connectors.call_tool(
            conn, PLACES_TOOL, {"textQuery": query, "pageSize": PAGE_SIZE})
    except Exception as e:
        logger.warning(f"place lookup failed ({query}): {e}")
        return []
    if not res["ok"]:
        logger.warning(f"place lookup refused ({query}): {res['error']}")
        return []
    import json
    try:
        data = json.loads(res["text"])
    except ValueError:
        return []
    shaped = connectors.shape_maps_places(data) if isinstance(data, dict) else None
    return (shaped or {}).get("places") or []


def _as_items(places: list[dict]) -> list[dict]:
    """The shape plan_trip takes. `about` is Google's own sentence, carried
    word for word, which is the whole reason for the lookup."""
    return [{"title": p["name"], "note": p.get("about", ""),
             "url": p.get("maps_url", "")} for p in places if p.get("name")]


def _dedupe(batches: list[list[dict]], want: int) -> list[dict]:
    """Round-robin across the batches so the plan is a mix rather than four
    museums then four parks. Deduplicated by place id, then by name for a
    server that returned none."""
    seen: set[str] = set()
    out: list[dict] = []
    for i in range(max((len(b) for b in batches), default=0)):
        for batch in batches:
            if i >= len(batch):
                continue
            p = batch[i]
            key = (p.get("id") or p.get("name", "")).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(p)
            if len(out) >= want:
                return out
    return out


async def plan(live: list[dict], runtime: dict, city: str, trip_id: str,
               days: int = 0) -> str:
    """Look the places up and lay the days out. Returns the sentence the
    model reads back."""
    city = (city or "").strip()
    if not city:
        return "Ask which city the plan is for, then try again."
    places_conn, plan_conn = _find(live, PLACES_TOOL), _find(live, PLAN_TOOL)
    if places_conn is None or plan_conn is None:
        return ("The day plan cannot be built on this call. Say so in one line"
                " and offer what you can do instead.")

    want = max(min(int(days or 0), 10), 2) * PER_DAY
    queries = SIGHT_QUERIES[:max(1, -(-want // PAGE_SIZE))]
    try:
        batches = await asyncio.wait_for(
            asyncio.gather(*[_lookup(places_conn, q.format(city=city)) for q in queries],
                           _lookup(places_conn, DINING_QUERY.format(city=city)),
                           return_exceptions=False),
            LOOKUP_BUDGET_S,
        )
    except asyncio.TimeoutError:
        return ("The place lookups did not answer in time. Say so and offer to"
                " try again in a moment.")
    *sights, dining = batches
    picked = _dedupe(sights, want)
    if not picked:
        return (f"Nothing came back for {city}. Say so plainly and offer to plan"
                " a different city or to skip the day plan.")

    result = await connectors.call_tool(plan_conn, PLAN_TOOL, {
        "trip_id": trip_id,
        "places": _as_items(picked),
        "dining": _as_items(dining[:MAX_DINING]),
    })
    if not result["ok"]:
        logger.warning(f"plan_trip refused: {result['error']}")
        return (f"The plan could not be saved: {result['error']}. Say so plainly"
                " and offer to try again.")
    sc = result.get("structured")
    if isinstance(sc, dict) and isinstance(sc.get("say"), str):
        # Same handling a model-made call gets: the written page is updated
        # and any cards reach the transcript.
        return connectors.absorb_structured(sc, runtime or {})
    return result["text"] or "The plan is saved. Read one day back as an example."


def tool(live: list[dict], runtime: dict | None):
    if not runtime or not available(live):
        return None
    if any(TOOL_NAME in (c.get("tool_names") or []) for c in live):
        logger.warning(f"{TOOL_NAME} is already a connector tool on this call, not built")
        return None

    @function_tool
    async def plan_the_days(context: RunContext, city: str, trip_id: str,
                            days: int = 0):
        """Build the caller's whole day-by-day plan in ONE step, once they
        have said they want one. The sights and the restaurants are looked
        up for you and spread across the days with times and meals, so do
        NOT look anything up first, do not ask which places they want, and
        do not add activities one at a time. Afterwards read one day back
        as an example and ask if they want anything changed.

        Args:
            city: The destination city.
            trip_id: The trip_id for this call.
            days: How many days the stay covers, if known. 0 is fine.
        """
        return await plan(live, runtime, city, trip_id, days)

    return plan_the_days
