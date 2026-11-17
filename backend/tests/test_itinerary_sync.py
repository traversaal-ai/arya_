"""itinerary_sync.merge_payload: one page, several authors."""

import itinerary_sync as isy

FLIGHTS_PAGE = {
    "brand": {"name": "Avaera", "icon": "", "mock": False, "mode": "test"},
    "trip": {"origin_city": "Islamabad", "dest_city": "Paris", "travelers": 1,
             "depart_date": "2026-10-01", "return_date": "2026-10-05"},
    "flights": {"outbound": {"airline": "Iberia", "total": "435.09 USD"},
                "return": {"airline": "Iberia", "total": "435.09 USD"}},
    "hotel": None,
    "day_plan": [],
    "totals": {"flight": "435.09 USD", "hotel": None, "grand": "435.09 USD"},
}

HOTEL_PAGE = {
    "brand": {"name": "Avaera", "icon": "", "mock": False},
    "trip": {"dest_city": "Paris", "travelers": 1},
    "flights": {"outbound": None, "return": None},
    "hotel": {"name": "Novotel Paris Porte de Versailles", "nights": 3, "total": "$850"},
    "day_plan": [],
    "totals": {"flight": None, "hotel": "$850", "grand": "$850"},
}


def test_the_day_headings_learn_the_hotel_from_the_other_writer():
    """"Paris · JO&JOE Paris Gentilly" under a day heading tells the reader
    where they are sleeping. Neither writer can produce it alone: the
    connector module never sees a hotel the sample desk noted. The merge is
    the one place that holds both."""
    plan_page = {
        "trip": {"dest_city": "Paris"},
        "flights": {},
        "hotel": None,
        "day_plan": [{"date": "2026-10-01", "label": "01 Oct", "location": "Paris",
                      "slots": []}],
        "totals": {},
    }
    merged = isy.merge_payload(HOTEL_PAGE, plan_page)
    assert merged["day_plan"][0]["location"] == "Paris · Novotel Paris Porte de Versailles"
    # Run it again and the hotel is not appended twice.
    assert isy.merge_payload(merged, plan_page)["day_plan"][0]["location"] == (
        "Paris · Novotel Paris Porte de Versailles")
    # A page with no hotel is left exactly as it was.
    assert isy.merge_payload(FLIGHTS_PAGE, plan_page)["day_plan"][0]["location"] == "Paris"
    # A writer that already named something keeps its own words.
    own = {**plan_page, "day_plan": [{"location": "Paris · Hotel Alfama", "slots": []}]}
    assert isy.merge_payload(HOTEL_PAGE, own)["day_plan"][0]["location"] == "Paris · Hotel Alfama"


def test_money_reads_both_writers_formats():
    assert isy.money("$850") == (850.0, "USD")
    assert isy.money("1,285.09 USD") == (1285.09, "USD")
    assert isy.money("480 GBP") == (480.0, "GBP")
    assert isy.money("n/a") is None
    assert isy.money("") is None


def test_a_hotel_noted_after_the_flights_keeps_the_flights():
    """Seen live: the SerpAPI desk wrote the hotel, the Duffel plan rewrote
    the page without it, and the caller asked why the itinerary had no hotel."""
    merged = isy.merge_payload(FLIGHTS_PAGE, HOTEL_PAGE)
    assert merged["flights"]["outbound"]["airline"] == "Iberia"
    assert merged["hotel"]["name"] == "Novotel Paris Porte de Versailles"
    assert merged["trip"]["origin_city"] == "Islamabad"  # kept
    assert merged["trip"]["dest_city"] == "Paris"
    assert merged["totals"] == {"flight": "435.09 USD", "hotel": "$850", "grand": "1,285.09 USD"}
    # The sandbox flag from either page marks the merged page.
    assert merged["brand"]["mode"] == "test"


def test_the_flight_writer_coming_back_keeps_the_hotel():
    merged = isy.merge_payload(HOTEL_PAGE, FLIGHTS_PAGE)
    assert merged["hotel"]["name"] == "Novotel Paris Porte de Versailles"
    assert merged["flights"]["return"]["airline"] == "Iberia"
    plan = {**FLIGHTS_PAGE, "day_plan": [{"date": "2026-10-02", "slots": [{"activity": "Louvre"}]}]}
    merged = isy.merge_payload(merged, plan)
    assert merged["hotel"] is not None and merged["day_plan"][0]["slots"][0]["activity"] == "Louvre"


def test_two_currencies_show_their_parts_and_no_sum():
    gbp = {**FLIGHTS_PAGE, "totals": {"flight": "480.00 GBP", "hotel": None, "grand": "480.00 GBP"}}
    merged = isy.merge_payload(gbp, HOTEL_PAGE)
    assert merged["totals"]["flight"] == "480.00 GBP" and merged["totals"]["hotel"] == "$850"
    assert merged["totals"]["grand"] is None


def test_first_write_is_taken_whole():
    assert isy.merge_payload(None, HOTEL_PAGE) is HOTEL_PAGE
    assert isy.merge_payload({}, HOTEL_PAGE) is HOTEL_PAGE
