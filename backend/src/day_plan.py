"""One layout for the day-by-day plan, shared by both trip writers.

The sample desk (travel_tools) has always laid its plan out in Python:
every day of the stay gets its share of the sights, with start times,
lunch, dinner, a theme and a spoken one-liner. The connector path lost
that. With Google Maps attached the sample places desk switches off, and
the model was left to lay the days out itself through plan_activity and
plan_days. It does not: the sixth live call looked up sights, looked up
restaurants, asked three times whether to start planning, and saved one
activity for one day.

So the layout lives here and both writers call it. The sample desk gets
back exactly the day dicts it built before (build_days); the connector
path gets flat plan items in the shape duffel.Trip.plan already holds
(lay_out). Neither invents a place: the caller's agent looks them up and
hands them over, and this module only decides which day each one lands
on and at what time.

Nothing here talks to a vendor, a database or a model, so it is all
pure functions over dates and dicts.
"""

from __future__ import annotations

import datetime

MAX_SIGHTS_PER_DAY = 3

THEME_KEYWORDS = [
    (("museum", "gallery", "art", "exhibit"), "Art and History"),
    (("cathedral", "temple", "shrine", "church", "basilica", "mosque"), "Heritage and Faith"),
    (("park", "garden", "nature", "botanical"), "Nature and Relaxation"),
    (("market", "shopping", "district", "boutique"), "Shopping and Local Life"),
    (("tower", "view", "observation", "skyline", "crossing"), "Views and Landmarks"),
    (("palace", "castle", "fort"), "Royalty and Architecture"),
]
SIGHT_VERBS = ["Visit", "Explore", "Discover"]
# The sample desk prints ranges on its page. The connector path stores one
# start time per item, because that is what the caller is told out loud and
# what orders the rows (duffel._time_rank).
SIGHT_TIMES = ["08:30 - 10:00", "10:30 - 12:30", "14:30 - 16:30"]
CLOCK_TIMES = ["09:30", "11:30", "15:00"]
LUNCH_TIME = "13:00"
DINNER_TIME = "19:30"

# A flight that lands this late, or leaves this early, takes the day.
LATE_ARRIVAL_HOUR = 15
EARLY_DEPARTURE_HOUR = 12


def theme_for(places: list[dict]) -> str:
    text = " ".join(f"{p.get('title', '')} {p.get('description', '')}".lower() for p in places)
    for keywords, theme in THEME_KEYWORDS:
        if any(k in text for k in keywords):
            return theme
    return "Sightseeing and Discovery"


def sentence(verb: str, title: str) -> str:
    """'Visit' + 'Louvre Museum' -> 'Visit the Louvre Museum'; skips the
    article when the title already starts with one ('The London Eye')."""
    lead = title.split(" ", 1)[0].lower()
    article = "" if lead in ("the", "a", "an") else "the "
    return f"{verb} {article}{title}"


def distribute(total: int, n_days: int, max_per_day: int,
               caps: list[int] | None = None) -> list[int]:
    """Spread `total` places across `n_days` round-robin so EVERY day gets
    some while places last — this is what makes a seven-day trip produce a
    seven-day plan instead of front-loading and truncating.

    `caps` overrides the per-day maximum for individual days, which is how
    an arrival or departure day gets a lighter load (or none at all)."""
    limits = list(caps) if caps else [max_per_day] * n_days
    counts = [0] * n_days
    i = 0
    while total > 0 and any(c < limits[j] for j, c in enumerate(counts)):
        if counts[i % n_days] < limits[i % n_days]:
            counts[i % n_days] += 1
            total -= 1
        i += 1
    return counts


def travel_day_caps(days: list[str], arrive_at: str = "", depart_at: str = "",
                    max_per_day: int = MAX_SIGHTS_PER_DAY) -> dict[str, int]:
    """How many sights the first and last day can really hold, read from
    the booked flights. Landing at 5:27 in the morning leaves a day worth
    using; landing at 11 at night does not, and a plan that says otherwise
    is the kind of detail a caller notices on the page."""
    caps: dict[str, int] = {}
    if not days:
        return caps
    hour = _hour_of(arrive_at)
    if hour is not None and arrive_at[:10] == days[0]:
        caps[days[0]] = 0 if hour >= LATE_ARRIVAL_HOUR else 1
    hour = _hour_of(depart_at)
    if hour is not None and depart_at[:10] == days[-1]:
        caps[days[-1]] = 0 if hour < EARLY_DEPARTURE_HOUR else 1
    # A one-day trip with both, or a same-day turnaround: keep the tighter.
    return {d: min(c, max_per_day) for d, c in caps.items()}


def _hour_of(iso: str) -> int | None:
    """The hour from "2026-10-01T05:27:00" or "2026-10-01 05:27"; None when
    there is no usable time, so an unknown flight never removes a day."""
    text = str(iso or "").strip()
    if len(text) < 13:
        return None
    try:
        return int(text[11:13])
    except ValueError:
        return None


def lay_out(days: list[str], places: list[dict], dining: list[dict] | None = None, *,
            caps: dict[str, int] | None = None,
            taken: dict[str, int] | None = None,
            max_per_day: int = MAX_SIGHTS_PER_DAY) -> tuple[list[dict], list[str]]:
    """The connector path: flat plan items, one per row of the written page.

    days   ISO dates of the stay, in order.
    places [{title, note, url}] the agent looked up, in its own order.
    dining the same shape for restaurants. Empty means no meal rows at all:
           a made-up "lunch at a local cafe" is exactly the generic filler
           the caller complained about, so it is never written.
    caps   per-day sight maximum (travel_day_caps).
    taken  sights already on a day from an earlier call, which count
           against its cap so a second pass fills gaps instead of doubling.

    Returns the items and the days that ended up with nothing despite
    having room, so the caller can be asked for more places rather than
    shown a hole."""
    places = [p for p in (places or []) if (p.get("title") or "").strip()]
    dining = [d for d in (dining or []) if (d.get("title") or "").strip()]
    if not days:
        return [], []
    caps = caps or {}
    taken = taken or {}
    limits = [max(caps.get(d, max_per_day) - taken.get(d, 0), 0) for d in days]
    counts = distribute(len(places), len(days), max_per_day, caps=limits)

    items: list[dict] = []
    empty: list[str] = []
    idx = 0
    meals = 0
    for i, day in enumerate(days):
        mine = places[idx:idx + counts[i]]
        idx += len(mine)
        if not mine:
            if limits[i] > 0 and not taken.get(day):
                empty.append(day)
            continue
        for k, place in enumerate(mine):
            # "Visit the Louvre Museum" reads as a plan; a bare "Louvre
            # Museum" reads as a list. The sample desk has always done
            # this, and the two pages should not differ.
            item = _item(day, place, CLOCK_TIMES[min(k, len(CLOCK_TIMES) - 1)])
            item["title"] = sentence(SIGHT_VERBS[k % len(SIGHT_VERBS)], item["title"])
            items.append(item)
        if dining:
            items.append(_meal(day, dining[meals % len(dining)], LUNCH_TIME, "Lunch"))
            meals += 1
            items.append(_meal(day, dining[meals % len(dining)], DINNER_TIME, "Dinner"))
            meals += 1
    return items, empty


def _item(day: str, place: dict, time: str) -> dict:
    return {
        "day": day,
        "title": (place.get("title") or "").strip(),
        "note": (place.get("note") or place.get("description") or "").strip(),
        "url": (place.get("url") or "").strip(),
        "time": time,
    }


def _meal(day: str, place: dict, time: str, meal: str) -> dict:
    item = _item(day, place, time)
    item["title"] = f"{meal} at {item['title']}"
    return item


def build_days(days: list[datetime.date], places: list[dict], dining: list[str],
               location: str = "") -> list[dict]:
    """The sample desk's rendered day entries: timestamped, themed, one
    spoken line each, spanning the WHOLE stay. Every named sight and
    restaurant was actually fetched; times are generic pacing estimates,
    never claimed venue hours."""
    if not days:
        return []
    counts = distribute(len(places), len(days), MAX_SIGHTS_PER_DAY)

    plan, idx = [], 0
    for i, day in enumerate(days):
        day_places = places[idx:idx + counts[i]]
        if not day_places:
            continue
        idx += len(day_places)
        lunch = f"Lunch at {dining[(i * 2) % len(dining)]}" if dining else "Lunch at a local cafe"
        dinner = (f"Dinner at {dining[(i * 2 + 1) % len(dining)]}" if dining
                  else "Dinner at a nearby restaurant")
        slots = []
        for k, place in enumerate(day_places):
            if k == 2:  # lunch sits between the morning pair and the afternoon slot
                slots.append({"time": "13:00 - 14:00", "activity": lunch, "description": ""})
            slots.append({
                "time": SIGHT_TIMES[k],
                "activity": sentence(SIGHT_VERBS[k % len(SIGHT_VERBS)], place["title"]),
                "description": place.get("description") or "",
            })
        if len(day_places) < 3:
            slots.append({"time": "13:00 - 14:00", "activity": lunch, "description": ""})
        slots.append({"time": "19:30 - 21:00", "activity": dinner, "description": ""})
        slots.sort(key=lambda s: s["time"])
        names = [p["title"] for p in day_places]
        spoken_names = (", ".join(names[:-1]) + f" and {names[-1]}") if len(names) > 1 else names[0]
        plan.append({
            "date": day.isoformat(),
            "label": f"{day.strftime('%d %b %Y')}, {day.strftime('%A')}",
            "theme": theme_for(day_places),
            "location": location,
            "slots": slots,
            "spoken": f"{day.strftime('%A, %B')} {day.day}: {spoken_names}.",
        })
    return plan
