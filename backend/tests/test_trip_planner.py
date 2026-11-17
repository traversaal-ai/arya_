"""The worker-side day planner: one question, one call, Google's words.

Two live calls in a row spent four rounds of questions and then saved one
activity at a time, with notes the model invented, because the layout job
had been handed to the model along with the lookups. These tests pin it
coming back: the worker looks the places up, the module lays them out, and
nothing on the page was written by a model.
"""

import json

import pytest

import connectors
import trip_planner as tp


def _maps_answer(names, start=0):
    """A Grounding Lite reply in its real shape: no display name and no
    description on a place, all the prose in one summary at the end."""
    places = [{
        "place": f"places/{n.replace(' ', '')}", "id": n.replace(" ", ""),
        "location": {"latitude": 48.8, "longitude": 2.3},
        "googleMapsLinks": {"placeUrl": f"https://maps.example/{n.replace(' ', '')}"},
        "attribution": {"title": f"{n} - Google Maps", "url": "https://maps.example/x"},
    } for n in names]
    summary = "\n\n".join(
        f"**{n}** [{i}] (4.{6 + i % 3}★, {10000 + i}) is a fine example of {n.lower()}."
        for i, n in enumerate(names)
    )
    return {"places": places, "summary": summary}


SIGHTS = {
    "top sights in Paris": ["Eiffel Tower", "Arc de Triomphe", "Louvre Museum",
                            "Notre-Dame", "Sacre-Coeur"],
    "museums and galleries in Paris": ["Louvre Museum", "Musee d'Orsay",
                                       "Centre Pompidou", "Musee Rodin", "Petit Palais"],
    "parks, gardens and viewpoints in Paris": ["Tuileries Garden", "Luxembourg Gardens",
                                               "Parc Monceau", "Buttes-Chaumont",
                                               "Champ de Mars"],
    "historic neighbourhoods to walk in Paris": ["Le Marais", "Montmartre",
                                                 "Latin Quarter", "Saint-Germain",
                                                 "Canal Saint-Martin"],
    "best restaurants in Paris": ["Pink Mamma", "Le Comptoir du Relais", "Septime",
                                  "Le Chateaubriand", "Bistrot Paul Bert"],
}


@pytest.fixture
def calls(monkeypatch):
    """Records every connector call and answers as the two servers would."""
    seen = []

    async def fake_call(conn, tool, arguments, timeout=None):
        seen.append({"tool": tool, "arguments": arguments})
        if tool == tp.PLACES_TOOL:
            names = SIGHTS.get(arguments["textQuery"])
            if names is None:
                return {"ok": False, "text": "", "structured": None, "error": "no such query"}
            return {"ok": True, "structured": None, "error": None,
                    "text": json.dumps(_maps_answer(names[:arguments["pageSize"]]))}
        if tool == tp.PLAN_TOOL:
            n = len(arguments["places"]) + len(arguments["dining"])
            return {"ok": True, "error": None, "text": "", "structured": {
                "say": f"Planned {n} activities across 4 days. For example Thursday.",
                "machine": {"count": n, "empty_days": []},
                "itinerary": {"stage": "plan", "title": "Trip to Paris",
                              "payload": {"day_plan": [{"date": "2026-10-02"}]}},
            }}
        return {"ok": False, "text": "", "structured": None, "error": "unknown tool"}

    monkeypatch.setattr(connectors, "call_tool", fake_call)
    return seen


def _live(places=True, planner=True):
    out = []
    if places:
        out.append({"entry": {"id": "google_maps"}, "row": {"id": "c1", "name": "Maps"},
                    "tool_names": [tp.PLACES_TOOL, "compute_routes"], "url": "https://maps",
                    "headers": {}, "transport": "streamable_http"})
    if planner:
        out.append({"entry": {"id": "duffel"}, "row": {"id": "c2", "name": "Duffel"},
                    "tool_names": ["search_flights", tp.PLAN_TOOL], "url": "https://duffel",
                    "headers": {}, "transport": "streamable_http"})
    return out


RUNTIME = {"conversation_id": "conv-1", "dsn": "postgres://x"}


async def test_one_call_looks_up_a_mix_and_lays_the_days_out(calls):
    said = await tp.plan(_live(), RUNTIME, "Paris", "t1", days=4)
    assert "Planned" in said and "For example Thursday" in said

    lookups = [c for c in calls if c["tool"] == tp.PLACES_TOOL]
    plans = [c for c in calls if c["tool"] == tp.PLAN_TOOL]
    assert len(plans) == 1
    # Several differently worded lookups, not one repeated: a week of
    # museums is not a trip.
    assert len(lookups) >= 3
    assert len({c["arguments"]["textQuery"] for c in lookups}) == len(lookups)
    assert any("restaurants" in c["arguments"]["textQuery"] for c in lookups)
    # The server caps a page at five however much is asked for.
    assert all(c["arguments"]["pageSize"] == tp.PAGE_SIZE for c in lookups)

    saved = plans[0]["arguments"]
    assert saved["trip_id"] == "t1"
    assert len(saved["places"]) == 12  # four days at three a day
    assert saved["dining"], "restaurants were looked up and handed over"
    # Google's own sentence, carried word for word. Nothing was written by
    # a model: that is the whole reason the lookup happens here.
    assert all(p["note"].startswith("A fine example of") for p in saved["places"])
    assert all(p["url"].startswith("https://maps.example/") for p in saved["places"])


async def test_the_same_place_in_two_lookups_is_planned_once(calls):
    await tp.plan(_live(), RUNTIME, "Paris", "t1", days=4)
    saved = next(c for c in calls if c["tool"] == tp.PLAN_TOOL)["arguments"]
    titles = [p["title"] for p in saved["places"]]
    assert len(titles) == len(set(titles))
    assert titles.count("Louvre Museum") == 1  # in two of the lookups


async def test_the_places_are_mixed_not_one_query_then_the_next(calls):
    """Round-robin across the lookups, so day one is not four museums."""
    await tp.plan(_live(), RUNTIME, "Paris", "t1", days=4)
    saved = next(c for c in calls if c["tool"] == tp.PLAN_TOOL)["arguments"]
    first_four = [p["title"] for p in saved["places"][:4]]
    assert "Eiffel Tower" in first_four
    assert any(t in first_four for t in ("Tuileries Garden", "Le Marais"))


async def test_a_lookup_that_fails_does_not_lose_the_plan(calls, monkeypatch):
    """Three queries instead of four is still a plan."""
    original = connectors.call_tool

    async def flaky(conn, tool, arguments, timeout=None):
        if tool == tp.PLACES_TOOL and "museums" in arguments.get("textQuery", ""):
            raise RuntimeError("Maps hiccup")
        return await original(conn, tool, arguments, timeout)

    monkeypatch.setattr(connectors, "call_tool", flaky)
    said = await tp.plan(_live(), RUNTIME, "Paris", "t1", days=4)
    assert "Planned" in said
    saved = next(c for c in calls if c["tool"] == tp.PLAN_TOOL)["arguments"]
    assert saved["places"] and not any(
        p["title"] in ("Musee d'Orsay", "Centre Pompidou") for p in saved["places"])


async def test_nothing_found_is_said_not_invented(calls):
    said = await tp.plan(_live(), RUNTIME, "Atlantis", "t1", days=3)
    assert "Nothing came back for Atlantis" in said
    assert not [c for c in calls if c["tool"] == tp.PLAN_TOOL]


async def test_a_refused_save_is_reported_word_for_word(calls, monkeypatch):
    async def refuse(conn, tool, arguments, timeout=None):
        if tool == tp.PLAN_TOOL:
            return {"ok": False, "text": "", "structured": None,
                    "error": "the trip has no dates yet"}
        return {"ok": True, "structured": None, "error": None,
                "text": json.dumps(_maps_answer(SIGHTS["top sights in Paris"]))}

    monkeypatch.setattr(connectors, "call_tool", refuse)
    said = await tp.plan(_live(), RUNTIME, "Paris", "t1", days=3)
    assert "could not be saved: the trip has no dates yet" in said


async def test_a_missing_city_asks_rather_than_guessing(calls):
    assert "Ask which city" in await tp.plan(_live(), RUNTIME, "  ", "t1")
    assert not calls


def test_the_tool_exists_only_when_both_halves_are_live():
    assert tp.available(_live())
    assert not tp.available(_live(planner=False))
    assert not tp.available(_live(places=False))
    assert not tp.available([])
    assert tp.tool(_live(), RUNTIME) is not None
    assert tp.tool(_live(), RUNTIME).__name__ == "plan_the_days"
    # A planner that can look places up and not save them is worse than none.
    assert tp.tool(_live(planner=False), RUNTIME) is None
    assert tp.tool(_live(), None) is None


def test_the_instruction_says_the_flow_and_never_a_tool_name():
    block = tp.prompt_block()
    assert "one step" in block and "read ONE day back" in block
    assert "once, not three times" in block
    for name in (tp.TOOL_NAME, tp.PLACES_TOOL, tp.PLAN_TOOL):
        assert name not in block
