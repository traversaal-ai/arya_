"""The sample travel desk: the hotel shortlist, the cards it offers, and
the strings a caller hears or reads.

The sixth live call said "Paris-Oasis is noted on your itinerary" and the
stored page had no hotel at all: the model answered a rate card's
"Option 1" tap in prose, twice, and never called select_room. These tests
pin the two halves of that fix. A lone rate gets no card, so the only
answer to it is a spoken yes; and a yes with no number still saves.
"""

import asyncio

import pytest

import itinerary_html
import travel_tools as tt


def _tools(stash, parts=("hotels",), keys=None):
    """The hotel desk alone, with a stash to inspect. runtime=None keeps
    the itinerary writes out of these tests (no DB, no room)."""
    built = tt.build_travel_tools({}, keys or {}, stash, None, parts=parts)
    return {t.__name__: t for t in built}


def _hotel(name, rates, rating="4.8 (170 reviews)", per_night="$561"):
    return {"name": name, "price_per_night": per_night, "rating": rating,
            "rating_value": 4.8, "reviews_count": 170, "hotel_class": "4-star hotel",
            "rate_options": rates}


def test_a_single_rate_is_a_question_not_a_card():
    """Seen live twice in one call: a one-item rates card offered an
    "Option 1" tap, the model read it as hotel option 1, and the hotel was
    never noted. One rate is a yes or no, so there is nothing to tap."""
    stash = []
    api = _tools(stash)
    st = tt._TripState()
    st.to_city = "Paris"
    st.check_in, st.check_out = "2026-10-01", "2026-10-05"
    st.hotel_results = [_hotel("Paris-Oasis", [
        {"source": "Voyabay", "rate_per_night": "$561", "total_rate": "$2,244"}])]
    # select_hotel closes over the state built inside build_travel_tools, so
    # drive it through the public path instead: search first.
    said = asyncio.run(_shortlist(api, stash, st.hotel_results))

    assert "Shall I note it on your itinerary?" in said
    assert not [g for g in stash if g["kind"] == "rates"], "a lone rate must not be a card"


def test_several_rates_are_a_card_whose_taps_say_rate():
    """With a real choice the card comes back, and a tap sends "Rate 2",
    never "Option 2" (which the model reads as a hotel)."""
    stash = []
    api = _tools(stash)
    rates = [
        {"source": "Voyabay", "rate_per_night": "$561", "total_rate": "$2,244"},
        {"source": "Hotels.com", "rate_per_night": "$540", "total_rate": "$2,160"},
        {"source": "Booking.com", "rate_per_night": "$533", "total_rate": "$2,132"},
    ]
    asyncio.run(_shortlist(api, stash, [_hotel("Paris-Oasis", rates)]))
    asyncio.run(api["search_rooms"](None))

    cards = [g for g in stash if g["kind"] == "rates"]
    assert len(cards) == 1
    assert [i["pick"] for i in cards[0]["items"]] == ["Rate 1", "Rate 2", "Rate 3"]
    assert [i["label"] for i in cards[0]["items"]] == ["Voyabay", "Hotels.com", "Booking.com"]


def test_a_plain_yes_saves_the_only_rate():
    """The caller says yes with no number. That has to reach the itinerary,
    because the spoken line already promised it would."""
    stash = []
    api = _tools(stash)
    asyncio.run(_shortlist(api, stash, [_hotel("Paris-Oasis", [
        {"source": "Voyabay", "rate_per_night": "$561", "total_rate": "$2,244"}])]))

    said = asyncio.run(api["select_room"](None))
    assert "Noted Paris-Oasis" in said
    assert "via Voyabay" in said
    assert "not a booking" in said

    page = asyncio.run(api["recap_itinerary"](None))
    assert "Paris-Oasis" in page


def test_a_rate_with_no_named_source_reads_as_the_best_available():
    """"lowest available" is SerpAPI's internal label for a property with no
    per-source prices. It was being spoken and printed on the page."""
    stash = []
    api = _tools(stash)
    asyncio.run(_shortlist(api, stash, [_hotel("Bonsoir Madame", [
        {"source": "", "rate_per_night": "$394", "total_rate": "$1,576"}])]))
    said = asyncio.run(api["select_room"](None))
    assert "the best available rate" in said
    assert "lowest available" not in said


def test_the_finished_plan_offers_email_in_the_same_breath():
    """Seen live on the connector path: the plan was read back, the caller
    declined more, and the call ended without email ever coming up. The
    sample desk gets the same fix: ask right when the plan is saved."""
    stash = []
    api = _tools(stash, parts=("places",))
    st = tt._TripState()
    st.to_city = "Lisbon"
    st.check_in, st.check_out = "2027-03-10", "2027-03-12"

    async def fake_sights(_dest, _key, _count):
        return [{"title": "Belem Tower", "description": "Riverside fort", "rating": 4.6}], None

    async def fake_dining(_dest, _key):
        return ["Time Out Market"]

    orig_sights, orig_dining = tt._fetch_sights, tt._fetch_dining
    tt._fetch_sights, tt._fetch_dining = fake_sights, fake_dining
    try:
        said = asyncio.run(api["get_recommendations"](None, "Lisbon"))
    finally:
        tt._fetch_sights, tt._fetch_dining = orig_sights, orig_dining

    assert "email it to them now that it is finished" in said


def test_a_marketing_suffix_is_not_part_of_a_hotel_name():
    """Google Hotels lists a property as its owner wrote it. 'Paris-Oasis
    "All Inclusive"' was read aloud and printed with the quotes in."""
    assert tt.clean_hotel_name('Paris-Oasis "All Inclusive"') == "Paris-Oasis"
    assert tt.clean_hotel_name("Le Parisis Hotel") == "Le Parisis Hotel"
    # A real name that happens to end in a quoted word is left alone only
    # when stripping would leave nothing.
    assert tt.clean_hotel_name('"The Hoxton"') == '"The Hoxton"'
    assert tt.clean_hotel_name("") == ""
    # Curly quotes too, which is what a listing usually carries.
    assert tt.clean_hotel_name("Hotel Lumiere “Spa & Suites”") == "Hotel Lumiere"


def test_the_page_links_the_place_name_and_finds_the_property(monkeypatch):
    """The raw listing name is what Google Maps needs to find the property;
    the cleaned one is what the reader sees."""
    async def fake_get(_key, params):
        return {"properties": [{
            "name": 'Paris-Oasis "All Inclusive"', "overall_rating": 4.8, "reviews": 170,
            "rate_per_night": {"lowest": "$561"}, "total_rate": {"lowest": "$2,244"},
        }]}

    monkeypatch.setattr(tt, "_serpapi_get", fake_get)
    st = tt._TripState()
    st.check_in, st.check_out, st.adults = "2026-10-01", "2026-10-05", 1
    options, _ = asyncio.run(tt._fetch_hotels(st, "Paris", "a-key"))
    assert options[0]["name"] == "Paris-Oasis"
    assert options[0]["full_name"] == 'Paris-Oasis "All Inclusive"'

    st.to_city = "Paris"
    st.saved_hotel = {"name": "Paris-Oasis", "full_name": 'Paris-Oasis "All Inclusive"',
                      "rating": "4.8", "price_per_night": "$561", "source": "Voyabay"}
    hotel = tt._itinerary_payload(st, {}, mock=False)["hotel"]
    assert hotel["name"] == "Paris-Oasis"
    assert "All+Inclusive" in hotel["maps_url"]


def test_a_plan_row_links_its_own_name_and_adds_no_link_line():
    """Four identical "View on Google Maps" lines under one day read as a
    list of links with places attached."""
    payload = {
        "brand": {"name": "Avaera", "icon": "", "mock": False},
        "trip": {"dest_city": "Paris", "travelers": 1},
        "flights": {"outbound": None, "return": None},
        "hotel": None,
        "day_plan": [{"date": "2026-10-02", "label": "02 Oct 2026, Friday",
                      "theme": "Art and History", "location": "Paris · Hotel Alfama",
                      "slots": [
                          {"time": "09:30", "activity": "Visit the Louvre Museum",
                           "description": "Landmark art museum.",
                           "url": "https://maps.example/louvre"},
                          {"time": "13:00", "activity": "Lunch at Pink Mamma",
                           "description": "", "url": ""},
                      ]}],
        "totals": {},
    }
    html = itinerary_html.render(payload, theme="arya")
    assert "View on Google Maps" not in html
    assert '<a href=\'https://maps.example/louvre\'' in html
    assert "Visit the Louvre Museum</a>" in html
    # A row with no link is still rendered, just not as one.
    assert "Lunch at Pink Mamma" in html
    assert "Art and History" in html and "Hotel Alfama" in html


def test_the_page_says_nothing_about_a_sandbox():
    """The owner is warned on the connection card when they paste a test
    token. The page can be emailed and downloaded, and its reader cannot
    act on the warning anyway."""
    payload = {
        "brand": {"name": "Avaera", "icon": "", "mock": False, "mode": "test"},
        "trip": {"dest_city": "Paris", "travelers": 1},
        "flights": {"outbound": None, "return": None},
        "hotel": None, "day_plan": [],
        "totals": {"flight": None, "hotel": None, "grand": "429.85 USD"},
    }
    html = itinerary_html.render(payload, theme="arya")
    assert "Test mode" not in html and "sandbox" not in html
    assert "nothing real was booked" not in html
    assert "429.85 USD" in html
    # A mock-data page still says its offers are samples: that IS actionable,
    # because the prices were never real to begin with.
    mock = itinerary_html.render({**payload, "brand": {**payload["brand"], "mode": "",
                                                       "mock": True}}, theme="arya")
    assert "Sample offers" in mock


def test_the_hotel_total_is_the_one_the_caller_heard():
    """The agent spoke the vendor's total (1,731) while the page recomputed
    433 times 4 and printed 1,732. Two numbers for one stay."""
    st = tt._TripState()
    st.to_city = "Paris"
    st.check_in, st.check_out, st.adults = "2026-10-01", "2026-10-05", 1
    st.saved_hotel = {"name": "Le Parisis", "rating": "4.6", "price_per_night": "$433",
                      "source": "Voyabay", "total": "$1,731"}
    page = tt._itinerary_payload(st, {}, mock=False)
    assert page["hotel"]["total"] == "$1,731"
    assert page["totals"]["hotel"] == "$1,731"
    # No vendor total: the computed one still stands rather than nothing.
    st.saved_hotel.pop("total")
    assert tt._itinerary_payload(st, {}, mock=False)["hotel"]["total"] == "$1,732"


def test_the_sample_desk_emits_the_same_leg_shape_as_the_connector():
    """One page layout needs one payload shape, whichever desk built it."""
    st = tt._TripState()
    st.from_city, st.from_code = "New York", "JFK"
    st.to_city, st.to_code = "Paris", "CDG"
    st.depart_date, st.return_date = "2026-10-01", "2026-10-05"
    st.adults = 1
    st.saved_flights["outbound"] = {
        "airline": "Air France", "price": "$842", "duration": "7 hours 25 minutes",
        "stops": "nonstop", "depart": "19:30", "arrive": "08:55",
        "flight_number": "AF023", "depart_airport": "John F Kennedy Intl",
    }
    out = tt._itinerary_payload(st, {}, mock=False)["flights"]["outbound"]
    assert out["depart"] == {"time": "19:30", "date": "2026-10-01",
                             "airport": "John F Kennedy Intl", "code": "JFK"}
    # No airport name from the search: the city stands in rather than a blank.
    assert out["arrive"] == {"time": "08:55", "date": "2026-10-01",
                             "airport": "Paris", "code": "CDG"}
    # Two separate one-way choices, so the fare is genuinely per leg.
    assert tt._itinerary_payload(st, {}, mock=False)["flights"]["fare"]["scope"] == "leg"


def test_the_written_page_never_says_lowest_available():
    payload = {
        "brand": {"name": "Avaera", "icon": "", "mock": False},
        "trip": {"dest_city": "Paris", "travelers": 1,
                 "depart_date": "2026-10-01", "return_date": "2026-10-05"},
        "flights": {"outbound": None, "return": None},
        "hotel": {"name": "Bonsoir Madame", "rating": "4.8", "source": "best available rate",
                  "price_per_night": "$394", "nights": 4, "total": "$1,576",
                  "maps_url": "https://maps.example/x"},
        "day_plan": [],
        "totals": {"flight": None, "hotel": "$1,576", "grand": "$1,576"},
    }
    html = itinerary_html.render(payload, theme="arya")
    assert "lowest available" not in html
    assert "best available rate" in html


@pytest.mark.parametrize("raw,shown", [(4.809412, "4.8"), (4.5, "4.5"), (None, None)])
def test_a_live_rating_is_rounded_before_anyone_sees_it(monkeypatch, raw, shown):
    """Google Hotels returns 4.809412. Spoken it is unreadable, and on a
    card it looks like a bug."""
    async def fake_get(_key, params):
        return {"properties": [{
            "name": "Paris-Oasis", "overall_rating": raw, "reviews": 170,
            "rate_per_night": {"lowest": "$561"}, "total_rate": {"lowest": "$2,244"},
            "hotel_class": "4-star hotel",
        }]}

    monkeypatch.setattr(tt, "_serpapi_get", fake_get)
    st = tt._TripState()
    st.check_in, st.check_out, st.adults = "2026-10-01", "2026-10-05", 1
    options, err = asyncio.run(tt._fetch_hotels(st, "Paris", "a-key"))
    assert err is None
    if shown is None:
        assert options[0]["rating"] == "unrated"
    else:
        assert options[0]["rating"] == f"{shown} (170 reviews)"
        assert "4.809412" not in options[0]["rating"]
        assert options[0]["rating_value"] == float(shown)


async def _shortlist(api, stash, hotels):
    """Search (mock backend), then pick hotel 1, returning what was said.
    Goes through the tools so the closure state is the real one."""
    async def fake_fetch(_st, _location, _key):
        return hotels, None

    original = tt._fetch_hotels
    tt._fetch_hotels = fake_fetch
    try:
        await api["search_hotels"](None, check_in_date="2026-10-01",
                                   check_out_date="2026-10-05", city="Paris")
        stash.clear()  # the hotels card is not what these tests are about
        return await api["select_hotel"](None, 1)
    finally:
        tt._fetch_hotels = original
