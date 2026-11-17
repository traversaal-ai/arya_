"""The written page: the flight block, and the two places money was wrong.

The reader of this page is looking for one time, not reading prose, and
they will add the numbers up. Both faults here were found by a caller
doing exactly that.
"""

import itinerary_html as ih

LEG_OUT = {
    "airline": "British Airways", "stops": "nonstop", "duration": "8 hours 27 minutes",
    "flight_number": "BA1516",
    "depart": {"time": "00:00", "date": "2026-10-01",
               "airport": "Islamabad International Airport", "code": "ISB"},
    "arrive": {"time": "05:27", "date": "2026-10-01",
               "airport": "Paris-Le Bourget Airport", "code": "LBG"},
    "price": "431.35 USD", "total": "431.35 USD", "reference": "JMXIGY",
    "url": "https://flights.example/1",
}
LEG_BACK = {
    **LEG_OUT, "duration": "8 hours 28 minutes",
    "depart": {"time": "03:05", "date": "2026-10-05",
               "airport": "Paris-Beauvais Airport", "code": "BVA"},
    "arrive": {"time": "14:33", "date": "2026-10-05",
               "airport": "Islamabad International Airport", "code": "ISB"},
    "url": "https://flights.example/2",
}


def page(flights, travelers=1, totals=None):
    return ih.render({
        "brand": {"name": "Avaera", "icon": "", "mock": False},
        "trip": {"origin_city": "Islamabad", "origin_code": "ISB", "dest_city": "Paris",
                 "dest_code": "PAR", "depart_date": "2026-10-01",
                 "return_date": "2026-10-05", "travelers": travelers},
        "flights": flights, "hotel": None, "day_plan": [],
        "totals": totals or {"flight": "431.35 USD", "hotel": None, "grand": "431.35 USD"},
    }, theme="arya")


def test_a_leg_leads_with_the_time_not_a_sentence():
    html = page({"outbound": LEG_OUT, "return": LEG_BACK,
                 "fare": {"scope": "journey", "total": "431.35 USD"}})
    assert "Departs Islamabad" not in html and " on 2026-10-01" not in html
    # Time, then a short date, then the airport with its code.
    assert ">00:00<span>Thu 1 Oct</span>" in html
    assert ">14:33<span>Mon 5 Oct</span>" in html
    assert "Islamabad International Airport (ISB)" in html
    assert "Paris-Beauvais Airport (BVA)" in html
    assert ">Depart<" in html and ">Arrive<" in html
    # Airline, flight number, stops and duration collapse to one line.
    assert "BA1516 · nonstop · 8h 27m" in html
    assert "8 hours 27 minutes" not in html
    # The reference is kept and said what it is for.
    assert "Airline reference (for check-in) JMXIGY" in html


def test_one_round_trip_fare_is_printed_once_not_under_each_leg():
    """Both legs of a Duffel round trip carry the same total, because it is
    one offer. Printed under each, the page read as 862.70 while the total
    bar said 431.35."""
    html = page({"outbound": LEG_OUT, "return": LEG_BACK,
                 "fare": {"scope": "journey", "total": "431.35 USD",
                          "per_traveler": "431.35 USD"}})
    assert "Flights 431.35 USD, round trip" in html
    # Once in the journey line, once in the total bar. Never under a leg.
    assert html.count("431.35 USD") == 2


def test_two_one_way_fares_are_printed_under_their_own_legs():
    """The sample desk picks each leg separately, so each really has its
    own price and the journey line must not claim one fare."""
    out = {**LEG_OUT, "price": "$437", "total": "$437"}
    back = {**LEG_BACK, "airline": "Qatar Airways", "price": "$465", "total": "$465"}
    html = page({"outbound": out, "return": back, "fare": {"scope": "leg"}},
                totals={"flight": "$902", "hotel": None, "grand": "$902"})
    assert "round trip" not in html
    assert "$437" in html and "$465" in html


def test_more_than_one_traveller_says_the_fare_is_each():
    html = page({"outbound": LEG_OUT, "return": LEG_BACK,
                 "fare": {"scope": "journey", "total": "862.70 USD",
                          "per_traveler": "431.35 USD"}}, travelers=2)
    assert "Flights 862.70 USD (431.35 USD each), round trip" in html
    assert "2 travelers" in html


def test_a_page_stored_before_this_still_renders():
    """merge_payload keeps whole legs, so an itinerary written by the old
    code can still be re-rendered when the next save merges onto it."""
    old = {**LEG_OUT,
           "depart": "Islamabad International Airport 00:00 on 2026-10-01",
           "arrive": "Paris-Le Bourget Airport 05:27 on 2026-10-01"}
    html = page({"outbound": old, "return": None, "fare": {"scope": "leg"}})
    assert "Islamabad International Airport 00:00 on 2026-10-01" in html
    assert ">Depart<" in html


def test_a_missing_time_or_date_is_left_out_not_guessed():
    thin = {"airline": "Iberia", "stops": "1 stop", "duration": "",
            "depart": {"time": "09:15", "date": "", "airport": "", "code": "MAD"},
            "arrive": {"time": "", "date": "2026-10-02", "airport": "Lisbon", "code": ""},
            "price": "", "url": ""}
    html = page({"outbound": thin, "return": None, "fare": {"scope": "leg"}})
    assert "09:15" in html and "MAD" in html and "Lisbon" in html
    assert "Fri 2 Oct" in html
    assert "None" not in html


def test_short_date_and_duration_never_raise_on_nonsense():
    assert ih._short_date("not a date") == "not a date"
    assert ih._short_date("") == ""
    assert ih._short_date(None) == ""
    assert ih._short_duration("1 hour 5 minutes") == "1h 5m"
    assert ih._short_duration("") == ""
    assert ih._short_duration(None) == ""
