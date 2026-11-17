"""day_plan: the layout both trip writers share.

The sample desk laid the days out in Python and the page looked planned.
The connector path handed that job to the model, and the sixth live call
ended with one activity on one day after three rounds of "shall I start
planning". These tests pin the arithmetic that took the job back.
"""

import datetime

import day_plan
import travel_tools as tt

PLACES = [
    {"title": "Belem Tower", "note": "Riverside fort", "url": "https://maps/1"},
    {"title": "Jeronimos Monastery", "note": "Gothic cloister", "url": "https://maps/2"},
    {"title": "Alfama", "note": "Old district", "url": "https://maps/3"},
    {"title": "Oceanario", "note": "Aquarium", "url": "https://maps/4"},
    {"title": "Sintra", "note": "Palaces in the hills", "url": "https://maps/5"},
    {"title": "Tram 28", "note": "The old line", "url": "https://maps/6"},
    {"title": "Se Cathedral", "note": "Romanesque", "url": "https://maps/7"},
]
DINING = [
    {"title": "Time Out Market", "url": "https://maps/d1"},
    {"title": "Cervejaria Ramiro", "url": "https://maps/d2"},
    {"title": "Taberna da Rua", "url": "https://maps/d3"},
    {"title": "A Cevicheria", "url": "https://maps/d4"},
]
DAYS = ["2027-03-10", "2027-03-11", "2027-03-12", "2027-03-13", "2027-03-14"]


def test_every_day_of_the_stay_gets_something():
    """The complaint that started this: five days planned, one filled."""
    items, empty = day_plan.lay_out(DAYS, PLACES, DINING)
    assert empty == []
    by_day = {d: [i for i in items if i["day"] == d] for d in DAYS}
    for day, rows in by_day.items():
        sights = [r for r in rows if not r["title"].startswith(("Lunch at", "Dinner at"))]
        assert sights, f"{day} was left empty"
        assert len(sights) <= day_plan.MAX_SIGHTS_PER_DAY
    # Seven places over five days: two on the first, one on each of the rest.
    assert [len([r for r in by_day[d] if not r["title"].startswith(("Lunch", "Dinner"))])
            for d in DAYS] == [2, 2, 1, 1, 1]


def test_times_run_forwards_and_meals_sit_between_the_sights():
    items, _ = day_plan.lay_out(DAYS, PLACES, DINING)
    first = [i for i in items if i["day"] == DAYS[0]]
    assert [i["time"] for i in first] == ["09:30", "11:30", "13:00", "19:30"]
    assert first[2]["title"] == "Lunch at Time Out Market"
    assert first[3]["title"] == "Dinner at Cervejaria Ramiro"
    # A restaurant is used once before the list wraps, so no day repeats it
    # and no two days open with the same lunch.
    second = [i for i in items if i["day"] == DAYS[1]]
    assert second[2]["title"] == "Lunch at Taberna da Rua"
    assert second[3]["title"] == "Dinner at A Cevicheria"
    # The Maps link the agent looked up rides along, for the written page.
    assert first[0]["url"] == "https://maps/1" and first[2]["url"] == "https://maps/d1"


def test_a_day_with_nothing_to_put_on_it_is_reported_not_hidden():
    """Two places over five days. The caller should be asked for more, not
    handed a page with three blank days."""
    items, empty = day_plan.lay_out(DAYS, PLACES[:2], DINING)
    assert empty == DAYS[2:]
    assert {i["day"] for i in items} == set(DAYS[:2])


def test_no_restaurants_means_no_invented_meals():
    """"Lunch at a local cafe" is the generic filler the caller objected to.
    The connector path never writes a place nobody looked up."""
    items, _ = day_plan.lay_out(DAYS, PLACES, [])
    assert not [i for i in items if i["title"].startswith(("Lunch at", "Dinner at"))]


def test_a_travel_day_is_not_a_whole_day():
    early, late = "2027-03-10T05:27:00", "2027-03-10T23:40:00"
    home, noon = "2027-03-14T03:05:00", "2027-03-14T18:00:00"
    # Lands at dawn, leaves in the evening: one sight at each end.
    assert day_plan.travel_day_caps(DAYS, early, noon) == {DAYS[0]: 1, DAYS[-1]: 1}
    # Lands near midnight, leaves at 3 in the morning: neither day is usable.
    assert day_plan.travel_day_caps(DAYS, late, home) == {DAYS[0]: 0, DAYS[-1]: 0}
    # Unknown flights never take a day away.
    assert day_plan.travel_day_caps(DAYS, "", "") == {}
    assert day_plan.travel_day_caps(DAYS, "2027-03-10", "nonsense") == {}
    # A flight on some other date says nothing about the first or last day.
    assert day_plan.travel_day_caps(DAYS, "2027-03-12T23:00:00", "") == {}


def test_caps_hold_when_the_places_are_laid_out():
    caps = day_plan.travel_day_caps(DAYS, "2027-03-10T05:27:00", "2027-03-14T18:00:00")
    items, empty = day_plan.lay_out(DAYS, PLACES, DINING, caps=caps)
    counts = [len([i for i in items if i["day"] == d
                   and not i["title"].startswith(("Lunch", "Dinner"))]) for d in DAYS]
    assert counts == [1, 2, 2, 1, 1] and sum(counts) == len(PLACES)
    assert empty == []


def test_a_day_already_planned_is_not_planned_twice():
    """A second call adds to the gaps rather than doubling up what is there."""
    items, empty = day_plan.lay_out(DAYS, PLACES[:2], DINING,
                                    taken={DAYS[0]: 3, DAYS[1]: 3, DAYS[2]: 3})
    assert {i["day"] for i in items} == {DAYS[3], DAYS[4]}
    assert empty == []


def test_a_zero_capacity_day_is_not_called_empty():
    """A day the flights took is not a day short of places to visit."""
    caps = {DAYS[0]: 0, DAYS[-1]: 0}
    _, empty = day_plan.lay_out(DAYS, PLACES[:3], DINING, caps=caps)
    assert empty == []


def test_no_days_and_no_places_are_answered_not_crashed():
    assert day_plan.lay_out([], PLACES, DINING) == ([], [])
    items, empty = day_plan.lay_out(DAYS, [], DINING)
    assert items == [] and empty == DAYS
    items, _ = day_plan.lay_out(DAYS, [{"title": "  "}, {"note": "no title"}], DINING)
    assert items == []


def test_the_sample_desk_plan_is_what_it_always_was():
    """The layout moved into day_plan; the desk's own page must not have
    moved with it. Verbs, time ranges, themes, the spoken line and the
    hotel in the location are all unchanged."""
    st = tt._TripState()
    st.to_city = "Lisbon"
    st.check_in, st.check_out = "2027-03-10", "2027-03-11"
    st.saved_hotel = {"name": "Hotel Alfama"}
    places = [{"title": "Belem Tower", "description": "Riverside fort"},
              {"title": "Alfama", "description": "Old district"}]
    assert tt._build_day_plan(st, places, ["Time Out Market", "Cervejaria Ramiro"]) == [
        {
            "date": "2027-03-10",
            "label": "10 Mar 2027, Wednesday",
            "theme": "Views and Landmarks",
            "location": "Lisbon · Hotel Alfama",
            "slots": [
                {"time": "08:30 - 10:00", "activity": "Visit the Belem Tower",
                 "description": "Riverside fort"},
                {"time": "13:00 - 14:00", "activity": "Lunch at Time Out Market",
                 "description": ""},
                {"time": "19:30 - 21:00", "activity": "Dinner at Cervejaria Ramiro",
                 "description": ""},
            ],
            "spoken": "Wednesday, March 10: Belem Tower.",
        },
        {
            "date": "2027-03-11",
            "label": "11 Mar 2027, Thursday",
            "theme": "Shopping and Local Life",
            "location": "Lisbon · Hotel Alfama",
            "slots": [
                {"time": "08:30 - 10:00", "activity": "Visit the Alfama",
                 "description": "Old district"},
                {"time": "13:00 - 14:00", "activity": "Lunch at Time Out Market",
                 "description": ""},
                {"time": "19:30 - 21:00", "activity": "Dinner at Cervejaria Ramiro",
                 "description": ""},
            ],
            "spoken": "Thursday, March 11: Alfama.",
        },
    ]


def test_the_sample_desk_still_spans_a_long_stay():
    """The reason distribute exists: a seven-day trip used to come back as
    a four-day plan with the rest of the places dropped."""
    days = [datetime.date(2027, 3, 10) + datetime.timedelta(days=i) for i in range(7)]
    places = [{"title": f"Place {i}", "description": ""} for i in range(9)]
    plan = day_plan.build_days(days, places, ["Somewhere"])
    assert len(plan) == 7
    assert sum(len([s for s in d["slots"] if s["activity"].startswith(("Visit", "Explore",
                                                                      "Discover"))])
               for d in plan) == 9
