"""Duffel module: the do_* functions against a fake Duffel over
httpx.MockTransport. No MCP session, no network.

What is pinned here is what a live call would punish: the spoken shape of
an option, every gate on booking (missing fields, changed price, passport
fares, duplicates), test-mode honesty, hotel rates Balance cannot pay being
dropped, and the two-step cancellations.
"""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import itinerary_html
from mcp_tools import duffel

KEY = "duffel_test_abc"
CONV = "conv-1"
OUT, BACK = "2027-03-10", "2027-03-15"
FAR = "2027-03-01T12:00:00Z"


def _seg(dep, arr, o, d, num, carrier=("Duffel Airways", "ZZ")):
    return {
        "departing_at": dep, "arriving_at": arr,
        "origin": o, "destination": d,
        "marketing_carrier": {"iata_code": carrier[1], "name": carrier[0]},
        "marketing_carrier_flight_number": num,
    }


LHR = {"name": "London Heathrow", "iata_code": "LHR", "city_name": "London"}
LIS = {"name": "Lisbon Airport", "iata_code": "LIS", "city_name": "Lisbon"}
MAD = {"name": "Madrid Barajas", "iata_code": "MAD", "city_name": "Madrid"}


def _offer(oid, total, *, stops=0, owner=("Duffel Airways", "ZZ"), docs=False):
    out_segs = [_seg(f"{OUT}T09:15:00", f"{OUT}T11:50:00", LHR, LIS, "1234", owner)]
    if stops:
        out_segs = [_seg(f"{OUT}T07:00:00", f"{OUT}T09:30:00", LHR, MAD, "2001", owner),
                    _seg(f"{OUT}T11:00:00", f"{OUT}T12:10:00", MAD, LIS, "2002", owner)]
    return {
        "id": oid, "live_mode": False, "total_amount": total, "total_currency": "GBP",
        "expires_at": FAR, "passenger_identity_documents_required": docs,
        "owner": {"name": owner[0], "iata_code": owner[1]},
        "conditions": {"refund_before_departure": {"allowed": True},
                       "change_before_departure": {"allowed": False}},
        "passengers": [{"id": "pas_1", "baggages": [{"type": "checked", "quantity": 1}]}],
        "slices": [
            {"duration": "PT2H35M" if not stops else "PT5H10M", "fare_brand_name": "Basic",
             "segments": out_segs},
            {"duration": "PT2H40M",
             "segments": [_seg(f"{BACK}T18:00:00", f"{BACK}T20:40:00", LIS, LHR, "4321", owner)]},
        ],
    }


RATE_OK = {
    "id": "rat_1", "total_amount": "600.00", "total_currency": "EUR",
    "payment_type": "pay_now", "available_payment_methods": ["balance", "card"],
    "board_type": "breakfast",
    "cancellation_timeline": [{"refund_amount": "600.00", "currency": "EUR",
                               "before": "2027-03-08T12:00:00Z"}],
}
RATE_PAY_AT_HOTEL = {
    "id": "rat_2", "total_amount": "500.00", "total_currency": "EUR",
    "payment_type": "pay_at_accommodation", "available_payment_methods": ["card"],
}
RATE_BETA = {
    "id": "rat_3", "total_amount": "700.00", "total_currency": "EUR",
    "payment_type": "pay_now", "cancellation_timeline": [],
}


class FakeDuffel:
    def __init__(self):
        self.price_bump = False
        self.docs_required = False
        self.quote_bump = False
        self.stays_enabled = True
        self.last_quote_total = "600.00"
        self.orders: list[dict] = []
        self.stays: list[dict] = []
        self.requests: list[tuple[str, str, dict]] = []
        self.n = 0

    def _data(self, body):
        return json.loads(body or b"{}").get("data", {}) if body else {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        u = urlparse(str(request.url))
        path, q = u.path, parse_qs(u.query)
        body = self._data(request.content)
        self.requests.append((request.method, path, body))
        assert request.headers["Duffel-Version"] == "v2"
        assert request.headers["Authorization"] == f"Bearer {KEY}"

        def ok(data, status=200):
            return httpx.Response(status, json={"data": data})

        if path == "/places/suggestions":
            query = (q.get("query") or [""])[0].lower()
            if "london" in query:
                return ok([{"type": "city", "name": "London", "iata_code": "LON",
                            "city_name": "London", "latitude": 51.5, "longitude": -0.12},
                           {"type": "airport", "name": "London Heathrow", "iata_code": "LHR"}])
            if "lisbon" in query:
                return ok([{"type": "city", "name": "Lisbon", "iata_code": "LIS",
                            "city_name": "Lisbon", "latitude": 38.72, "longitude": -9.14}])
            return ok([])
        if path == "/air/offer_requests":
            assert q["return_offers"] == ["true"]
            return ok({"id": "orq_1", "passengers": [{"id": "pas_1", "type": "adult"}],
                       "offers": [_offer("off_2", "620.00", stops=1, owner=("Sandbox Air", "SB")),
                                  _offer("off_1", "480.00")]})
        if path == "/air/offers/off_1":
            o = _offer("off_1", "510.00" if self.price_bump else "480.00", docs=self.docs_required)
            return ok(o)
        if path == "/air/orders" and request.method == "POST":
            self.n += 1
            order = {
                "id": f"ord_{self.n}", "booking_reference": f"REF{self.n}", "live_mode": False,
                "total_amount": body["payments"][0]["amount"], "total_currency": "GBP",
                "owner": {"name": "Duffel Airways"}, "metadata": body.get("metadata") or {},
                "passengers": body["passengers"], "slices": _offer("off_1", "480.00")["slices"],
            }
            self.orders.append(order)
            return ok(order, 201)
        if path == "/air/orders" and request.method == "GET":
            ref = (q.get("booking_reference") or [None])[0]
            return ok([o for o in self.orders if not ref or o["booking_reference"] == ref])
        if path.startswith("/air/orders/"):
            oid = path.rsplit("/", 1)[1]
            hits = [o for o in self.orders if o["id"] == oid]
            return ok(hits[0]) if hits else httpx.Response(404, json={"errors": []})
        if path == "/air/order_cancellations":
            return ok({"id": "ore_1", "refund_amount": "480.00", "refund_currency": "GBP",
                       "expires_at": FAR}, 201)
        if path.startswith("/air/order_cancellations/") and path.endswith("/confirm"):
            for o in self.orders:
                o["cancelled_at"] = "2026-09-11T10:00:00Z"
            return ok({"id": "ore_1", "confirmed_at": "2026-09-11T10:00:00Z"})
        if path.startswith("/stays/") and not self.stays_enabled:
            # Duffel's real answer: 403 with a plain-text sentence, not JSON.
            return httpx.Response(403, text="This feature is not enabled for your account."
                                            " Please contact sales to get access:"
                                            " https://duffel.com/contact-us")
        if path == "/stays/search":
            return ok({"results": [
                {"id": "srr_1", "cheapest_rate_total_amount": "500.00", "cheapest_rate_currency": "EUR",
                 "accommodation": {"id": "acc_1", "name": "Test Hotel Alpha", "rating": 4,
                                   "location": {"address": {"city_name": "Pitcairn"}},
                                   "rooms": [{"name": "Deluxe Double",
                                              "rates": [RATE_PAY_AT_HOTEL, RATE_OK]}]}},
                {"id": "srr_2", "cheapest_rate_total_amount": "700.00", "cheapest_rate_currency": "EUR",
                 "accommodation": {"id": "acc_2", "name": "Test Hotel Beta", "rating": 3,
                                   "location": {"address": {}}, "rooms": []}},
            ]})
        if path == "/stays/search_results/srr_2/actions/fetch_all_rates":
            return ok({"accommodation": {"id": "acc_2", "name": "Test Hotel Beta",
                                         "rooms": [{"name": "Twin", "rates": [RATE_BETA]}]}})
        if path == "/stays/quotes":
            rate = {"rat_1": RATE_OK, "rat_3": RATE_BETA}[body["rate_id"]]
            total = "650.00" if self.quote_bump else rate["total_amount"]
            self.last_quote_total = total
            return ok({"id": "quo_1", "total_amount": total, "total_currency": "EUR"}, 201)
        if path == "/stays/bookings" and request.method == "POST":
            self.n += 1
            # The booked rate carries whatever the quote said, as Duffel's does.
            booked_rate = {**RATE_OK, "total_amount": self.last_quote_total,
                           "cancellation_timeline": [{"refund_amount": self.last_quote_total,
                                                      "currency": "EUR",
                                                      "before": "2027-03-08T12:00:00Z"}]}
            b = {"id": f"bok_{self.n}", "reference": "AFE33SE2", "status": "confirmed",
                 "check_in_date": OUT, "check_out_date": BACK, "email": body["email"],
                 "metadata": body.get("metadata") or {},
                 "accommodation": {"id": "acc_1", "name": "Test Hotel Alpha", "rating": 4,
                                   "rooms": [{"name": "Deluxe Double", "rates": [booked_rate]}]}}
            self.stays.append(b)
            return ok(b, 201)
        if path == "/stays/bookings" and request.method == "GET":
            return ok(list(self.stays))
        if path.startswith("/stays/bookings/") and path.endswith("/cancel"):
            bid = path.split("/")[3]
            for b in self.stays:
                if b["id"] == bid:
                    b["status"], b["cancelled_at"] = "cancelled", "2026-09-11T10:00:00Z"
                    return ok(b)
            return httpx.Response(404, json={"errors": []})
        if path.startswith("/stays/bookings/"):
            bid = path.rsplit("/", 1)[1]
            hits = [b for b in self.stays if b["id"] == bid]
            return ok(hits[0]) if hits else httpx.Response(404, json={"errors": []})
        if path == "/air/airlines":
            return ok([{"name": "Duffel Airways"}])
        return httpx.Response(404, json={"errors": [{"message": f"no route {path}"}]})


@pytest.fixture
def fake():
    f = FakeDuffel()
    duffel._TRANSPORT = httpx.MockTransport(f.handler)
    duffel._TRIPS.clear()
    duffel._CANCELS.clear()
    try:
        yield f
    finally:
        duffel._TRANSPORT = None
        duffel._TRIPS.clear()


TRAVELLER = {"given_name": "Tony", "family_name": "Stark", "born_on": "1980-07-24",
             "gender": "m", "title": "mr"}


def test_spoken_formatting():
    assert duffel.say_money("480.00", "GBP") == "480 pounds"
    assert duffel.say_money("240.50", "GBP") == "240 pounds 50"
    assert duffel.say_money("1.00", "EUR") == "1 euro"
    assert duffel.say_money("1899.00", "USD") == "1,899 dollars"
    assert duffel.say_money("12.00", "CHF") == "12 CHF"
    # Read out in a list of fares, cents are noise.
    assert duffel.say_money_rounded("137.87", "USD") == "about 138 dollars"
    assert duffel.say_money_rounded("480.00", "GBP") == "480 pounds"
    assert duffel.say_money_rounded("1.00", "EUR") == "1 euro"
    assert duffel.say_clock("2027-03-10T09:15:00") == "9:15 in the morning"
    assert duffel.say_clock("2027-03-10T14:00:00") == "2 in the afternoon"
    assert duffel.say_clock("2027-03-10T12:00:00") == "noon"
    assert duffel.say_clock("2027-03-10T23:40:00") == "11:40 at night"
    assert duffel.say_duration("PT2H35M") == "2 hours 35 minutes"
    assert duffel.say_duration("PT1H") == "1 hour"
    assert duffel.say_day("2027-03-10") == "Wednesday the 10th of March"


async def test_search_speaks_both_legs_and_flags_the_sandbox(fake):
    out = await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK)
    say = out["say"]
    # Cheapest first, both legs in one breath, airport names not codes,
    # spoken money, no durations (those wait for flight_details).
    assert say.index("Option 1: Duffel Airways") < say.index("Option 2: Sandbox Air")
    assert ("Option 1: Duffel Airways both ways, from London Heathrow to Lisbon Airport, out at"
            " 9:15 in the morning landing 11:50 in the morning, back on the 15th at 6 in the"
            " evening landing 8:40 in the evening, nonstop, 480 pounds.") in say
    assert "Option 2: Sandbox Air both ways" in say
    assert "1 stop in Madrid on the way out, nonstop on the way back, 620 pounds." in say
    assert "2 hours" not in say
    card = out["cards"][0]["items"][0]
    assert card["label"] == ("Duffel Airways both ways · out 9:15 in the morning to 11:50 in the"
                             " morning · back 6 in the evening to 8:40 in the evening")
    assert card["detail"] == "nonstop · 480 pounds · LHR to LIS"
    assert "LHR" not in say and "GBP" not in say
    # A search still carries the note, because a caller may ask whether the
    # prices are real; a BOOKING says nothing (see the booking test).
    assert "sandbox" in say and "not real" in say
    assert "never choose for them" in say
    m = out["machine"]
    assert m["mode"] == "test" and len(m["options"]) == 2 and m["options"][0]["total"] == "480.00"
    # `value` is the price now, never "Option 1" (the badge already says
    # that); `pick` is what a tap sends, built from `n`.
    assert out["cards"][0]["kind"] == "flights" and card["value"] == "£480"
    assert card["pick"] == "Option 1"
    disp = card["display"]
    assert disp["legs"][0] == {"dep": "09:15", "arr": "11:50", "from": "LHR", "to": "LIS",
                               "stops": "Nonstop", "duration": "2h 35m", "plus_days": 0}
    assert disp["legs"][1]["dep"] == "18:00" and disp["price"] == "£480"
    assert out["cards"][0]["display"]["from_code"] == "LON" and out["cards"][0]["display"]["leg"] == "round_trip"
    # The search went to Duffel with city codes, never model-made airport codes.
    _, _, body = next(r for r in fake.requests if r[1] == "/air/offer_requests")
    assert body["slices"][0] == {"origin": "LON", "destination": "LIS", "departure_date": OUT}
    assert body["slices"][1]["origin"] == "LIS" and body["cabin_class"] == "economy"


async def test_search_refuses_bad_input_as_an_error_not_an_empty_result(fake):
    out = await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", "2020-01-01")
    assert out["error"] and "already passed" in out["say"]
    assert "not an empty result" in out["say"]
    out = await duffel.do_search_flights(KEY, CONV, "London", "Atlantis", OUT)
    assert "could not find a place called Atlantis" in out["say"]
    out = await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, cabin="luxury")
    assert "cabin must be" in out["say"]


async def test_book_flight_gates_then_books_once(fake):
    trip = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]

    # Missing traveller fields are named, nothing is sent to Duffel.
    out = await duffel.do_book_flight(KEY, CONV, trip, 1, [{"given_name": "Tony"}],
                                      "tony@example.com", "+442080160508")
    assert "I still need the traveller's last name, born_on" in out["say"]
    assert not any(r[1] == "/air/orders" for r in fake.requests)
    out = await duffel.do_book_flight(KEY, CONV, trip, 1, [TRAVELLER], "tony@example.com", "07700 900123")
    assert "country code" in out["say"]
    out = await duffel.do_book_flight(KEY, CONV, trip, 1, [TRAVELLER], "nope", "+442080160508")
    assert "valid email" in out["say"]

    # A changed price is reported and NOT booked. The new price now stands.
    fake.price_bump = True
    out = await duffel.do_book_flight(KEY, CONV, trip, 1, [TRAVELLER], "tony@example.com", "+442080160508")
    assert out["machine"].get("price_changed") and "480 pounds to 510 pounds" in out["say"]
    assert not fake.orders

    # A passport fare is refused outright.
    fake.docs_required = True
    out = await duffel.do_book_flight(KEY, CONV, trip, 1, [TRAVELLER], "tony@example.com", "+442080160508")
    assert "passport" in out["say"] and not fake.orders
    fake.docs_required = False

    # Booked: balance payment at the refreshed total, metadata stamped,
    # phone normalised, test mode admitted, itinerary payload attached.
    out = await duffel.do_book_flight(KEY, CONV, trip, 1, [TRAVELLER], "tony@example.com",
                                      "+44 (0)20 8016 0508".replace("(0)", ""))
    assert out["machine"]["booked"] and out["machine"]["reference"] == "REF1"
    # Nothing spoken about the sandbox: the owner is warned on the card and
    # Publish is blocked while a test token is attached, so the caller is
    # never handed a disclaimer they cannot act on.
    assert "sandbox" not in out["say"] and "test" not in out["say"].lower()
    assert "Do not read the booking reference aloud" in out["say"]
    _, _, body = next(r for r in fake.requests if r[1] == "/air/orders" and r[0] == "POST")
    assert body["payments"] == [{"type": "balance", "currency": "GBP", "amount": "510.00"}]
    assert body["metadata"] == {"arya_conversation": CONV}
    assert body["passengers"][0]["phone_number"] == "+442080160508"
    assert body["passengers"][0]["id"] == "pas_1"
    itin = out["itinerary"]
    assert itin["stage"] == "flight" and itin["payload"]["brand"]["mode"] == "test"
    assert itin["payload"]["flights"]["outbound"]["url"].startswith("https://www.google.com/travel/flights")
    assert itin["payload"]["flights"]["outbound"]["airline"] == "Duffel Airways"
    assert itin["payload"]["flights"]["return"] is not None

    # Same call, same option again: refused from memory.
    again = await duffel.do_book_flight(KEY, CONV, trip, 1, [TRAVELLER], "tony@example.com", "+442080160508")
    assert again["machine"].get("already") and len(fake.orders) == 1

    # A fresh search on the same call (cache lost) still cannot double book:
    # Duffel's own order list carries our conversation stamp, and that check
    # runs before re-pricing, so a moved price cannot turn "already booked"
    # into "price changed".
    duffel._TRIPS.clear()
    trip2 = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]
    again = await duffel.do_book_flight(KEY, CONV, trip2, 1, [TRAVELLER], "tony@example.com", "+442080160508")
    assert again["machine"].get("already") and len(fake.orders) == 1

    # Another call cannot even see this call's trip id.
    other = await duffel.do_book_flight(KEY, "conv-2", trip2, 1, [TRAVELLER], "tony@example.com", "+442080160508")
    assert other["error"] and "not from this call" in other["say"] and len(fake.orders) == 1

    # A different call may book the same fare on its own search, at today's price.
    fake.price_bump = False
    trip3 = (await duffel.do_search_flights(KEY, "conv-2", "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]
    out = await duffel.do_book_flight(KEY, "conv-2", trip3, 1, [TRAVELLER], "tony@example.com", "+442080160508")
    assert out["machine"]["booked"] and len(fake.orders) == 2


async def test_traveller_count_must_match_the_search(fake):
    trip = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, adults=2))["machine"]["trip_id"]
    out = await duffel.do_book_flight(KEY, CONV, trip, 1, [TRAVELLER], "t@example.com", "+442080160508")
    assert "2 travellers" in out["say"] and "each of them" in out["say"]


async def test_expired_search_is_an_honest_error(fake):
    out = await duffel.do_book_flight(KEY, CONV, "nope", 1, [TRAVELLER], "t@example.com", "+442080160508")
    assert "expired" in out["say"] and "do not tell the caller anything was booked" in out["say"]


async def test_flight_details_and_find_and_cancel(fake):
    trip = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]
    out = await duffel.do_flight_details(KEY, CONV, trip, 1)
    assert "1 checked bag" in out["say"] and "Refundable" in out["say"] and "no changes" in out["say"]

    await duffel.do_book_flight(KEY, CONV, trip, 1, [TRAVELLER], "tony@example.com", "+442080160508")
    found = await duffel.do_find_bookings(KEY, CONV, "tony@example.com")
    assert "1 booking" in found["say"] and found["machine"]["flights"][0]["reference"] == "REF1"
    assert (await duffel.do_find_bookings(KEY, CONV, "other@example.com"))["say"].startswith("I found no bookings")

    quote = await duffel.do_cancel_flight(KEY, CONV, "REF1")
    assert "would refund 480 pounds" in quote["say"] and "Nothing has been cancelled" in quote["say"]
    assert not fake.orders[0].get("cancelled_at")
    done = await duffel.do_cancel_flight(KEY, CONV, "REF1", confirm=True)
    assert done["machine"]["cancelled"] and "sandbox" in done["say"]
    assert fake.orders[0]["cancelled_at"]
    again = await duffel.do_cancel_flight(KEY, CONV, "REF1", confirm=True)
    assert "already cancelled" in again["say"]


async def test_hotels_use_test_coordinates_and_only_balance_rates(fake):
    out = await duffel.do_search_hotels(KEY, CONV, "Lisbon", OUT, BACK)
    _, _, body = next(r for r in fake.requests if r[1] == "/stays/search")
    assert body["location"]["geographic_coordinates"] == {"latitude": -24.38, "longitude": -128.32}
    assert body["fetch_rates"] is True and body["guests"] == [{"type": "adult"}] * 2
    say = out["say"]
    # Alpha's cheapest rate is the pay-at-hotel one (500) which Balance
    # cannot pay, so it is quoted at 600 with its free cancellation.
    assert "Option 1: Test Hotel Alpha in Pitcairn, rated 4 stars, 600 euros for 5 nights, free cancellation until Monday the 8th of March" in say
    assert "Option 2: Test Hotel Beta" in say and "test properties" in say
    trip = out["machine"]["trip_id"]

    rates = await duffel.do_hotel_rates(KEY, CONV, trip, 1)
    assert "1 rate" in rates["say"] and "Rate 1: Deluxe Double, breakfast, 600 euros in total" in rates["say"]
    assert all(r["room"] != "rat_2" for r in rates["machine"]["rates"])
    # Beta had no rates in the search result, so they are fetched on demand.
    rates2 = await duffel.do_hotel_rates(KEY, CONV, trip, 2)
    assert "Rate 1: Twin, 700 euros in total, non-refundable" in rates2["say"]
    assert any(r[1].endswith("srr_2/actions/fetch_all_rates") for r in fake.requests)

    # Quote drift is reported, not booked. The new rate now stands.
    fake.quote_bump = True
    out = await duffel.do_book_hotel(KEY, CONV, trip, 1, 1, [{"given_name": "Tony", "family_name": "Stark"}],
                                     "tony@example.com", "+442080160508")
    assert out["machine"].get("price_changed") and "600 euros to 650 euros" in out["say"] and not fake.stays

    out = await duffel.do_book_hotel(KEY, CONV, trip, 1, 1, [{"given_name": "Tony", "family_name": "Stark"}],
                                     "tony@example.com", "+442080160508", special_requests="late arrival")
    assert out["machine"]["booked"] and out["machine"]["reference"] == "AFE33SE2"
    assert "checking in Wednesday the 10th of March and out Monday the 15th of March, 650 euros" in out["say"]
    _, _, body = next(r for r in fake.requests if r[1] == "/stays/bookings" and r[0] == "POST")
    assert body["quote_id"] == "quo_1" and body["accommodation_special_requests"] == "late arrival"
    assert "payment" not in body  # omitted means Balance
    assert out["itinerary"]["payload"]["hotel"]["name"] == "Test Hotel Alpha"
    assert out["itinerary"]["payload"]["hotel"]["nights"] == 5

    again = await duffel.do_book_hotel(KEY, CONV, trip, 1, 1, [{"given_name": "Tony", "family_name": "Stark"}],
                                       "tony@example.com", "+442080160508")
    assert again["machine"].get("already") and len(fake.stays) == 1

    quote = await duffel.do_cancel_hotel(KEY, CONV, "AFE33SE2")
    assert "would refund 650 euros" in quote["say"] and fake.stays[0]["status"] == "confirmed"
    done = await duffel.do_cancel_hotel(KEY, CONV, "AFE33SE2", confirm=True)
    assert done["machine"]["cancelled"] and fake.stays[0]["status"] == "cancelled"


async def test_plan_and_summary(fake):
    trip = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]
    empty = await duffel.do_trip_summary(KEY, CONV, trip)
    assert empty["machine"].get("empty")

    out = await duffel.do_plan_activity(KEY, CONV, trip, "day 2", "Belem Tower", "go early",
                                      "https://maps.google.com/?q=belem")
    assert out["machine"]["day"] == "2027-03-11" and "Added Belem Tower to Thursday the 11th of March" in out["say"]
    out = await duffel.do_plan_activity(KEY, CONV, trip, "2027-03-11", "Pasteis de Belem")
    assert "Belem Tower, Pasteis de Belem" in out["say"]
    bad = await duffel.do_plan_activity(KEY, CONV, trip, "day 9", "Nowhere")
    assert "from 1 to 6" in bad["say"]
    plan = out["itinerary"]["payload"]["day_plan"]
    assert plan[0]["slots"][0]["url"] == "https://maps.google.com/?q=belem"
    # An item without a link gets a Maps search for the written page.
    assert plan[0]["slots"][1]["url"].startswith("https://www.google.com/maps/search/")

    await duffel.do_book_flight(KEY, CONV, trip, 1, [TRAVELLER], "tony@example.com", "+442080160508")
    summary = await duffel.do_trip_summary(KEY, CONV, trip)
    text = summary["machine"]["summary"]
    assert "Outbound: Duffel Airways ZZ1234, London Heathrow to Lisbon Airport" in text
    assert "Airline reference REF1" in text and "Test booking" in text
    assert "Thursday the 11th of March: Belem Tower (go early); Pasteis de Belem." in text
    assert summary["machine"]["subject"] == f"Your trip to Lisbon, {OUT}"
    assert "word for word" in summary["say"]


def test_title_settles_gender_so_nobody_is_asked_twice():
    t = duffel._check_traveller({"given_name": "Ana", "family_name": "Silva",
                                 "born_on": "1990-01-02", "title": "Ms"}, 1, full=True)
    assert t["gender"] == "f" and t["title"] == "ms"
    t = duffel._check_traveller({"given_name": "Tony", "family_name": "Stark",
                                 "born_on": "1980-07-24", "title": "mr."}, 1, full=True)
    assert t["gender"] == "m"
    # An explicit gender still wins, and Dr settles nothing.
    t = duffel._check_traveller({**TRAVELLER, "title": "dr", "gender": "f"}, 1, full=True)
    assert t["gender"] == "f"
    with pytest.raises(duffel.DuffelError) as ei:
        duffel._check_traveller({"given_name": "Sam", "family_name": "Lee",
                                 "born_on": "1980-07-24", "title": "dr"}, 1, full=True)
    assert "gender" in str(ei.value) and "Dr" in str(ei.value)


async def test_stays_switched_off_is_named_not_blamed_on_the_token(fake):
    """Duffel Stays is off on new accounts and answers 403 in plain text. The
    first live call called that a bad token, apologised, then invented three
    hotels from memory."""
    fake.stays_enabled = False
    out = await duffel.do_search_hotels(KEY, CONV, "Lisbon", OUT, BACK)
    assert out["error"] and "not enabled on this account" in out["say"]
    assert "rejected" not in out["say"]
    # Flight bookings are still findable when Stays is off.
    found = await duffel.do_find_bookings(KEY, CONV, "nobody@example.com")
    assert found["say"].startswith("I found no bookings")
    ok = await duffel.check_key(KEY)
    assert ok["ok"] and ok["stays"] is False
    assert set(ok["hide_tools"]) == {"search_hotels", "hotel_rates", "book_hotel", "cancel_hotel"}
    assert any("Stays" in w for w in ok["warnings"])
    fake.stays_enabled = True
    ok = await duffel.check_key(KEY)
    assert ok["stays"] is True and "hide_tools" not in ok


SIGHTS = [
    {"title": "Belem Tower", "note": "Riverside fort", "url": "https://maps/1"},
    {"title": "Jeronimos Monastery", "note": "Gothic cloister", "url": "https://maps/2"},
    {"title": "Alfama", "note": "Old district", "url": "https://maps/3"},
    {"title": "Oceanario", "note": "Aquarium", "url": "https://maps/4"},
    {"title": "Sintra", "note": "Palaces in the hills", "url": "https://maps/5"},
    {"title": "Tram 28", "note": "The old line", "url": "https://maps/6"},
    {"title": "Se Cathedral", "note": "Romanesque", "url": "https://maps/7"},
]
EATS = [
    {"title": "Time Out Market", "url": "https://maps/d1"},
    {"title": "Cervejaria Ramiro", "url": "https://maps/d2"},
    {"title": "Taberna da Rua", "url": "https://maps/d3"},
    {"title": "A Cevicheria", "url": "https://maps/d4"},
]


async def test_the_booked_flight_reaches_the_page_as_times_and_one_fare(fake):
    """A round trip is ONE Duffel offer with two slices. Printed under each
    leg its fare read as double what was charged, and the departure was a
    sentence the page could not lay out."""
    trip = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]
    out = await duffel.do_book_flight(KEY, CONV, trip, 1, [TRAVELLER],
                                      "tony@example.com", "+442080160508")
    flights = out["itinerary"]["payload"]["flights"]
    assert flights["outbound"]["depart"] == {
        "time": "09:15", "date": OUT, "airport": "London Heathrow", "code": "LHR"}
    assert flights["outbound"]["arrive"] == {
        "time": "11:50", "date": OUT, "airport": "Lisbon Airport", "code": "LIS"}
    assert flights["return"]["depart"]["time"] == "18:00"
    assert flights["return"]["arrive"]["airport"] == "London Heathrow"
    # One fare for the journey, and it is the amount actually charged.
    assert flights["fare"]["scope"] == "journey"
    assert flights["fare"]["total"] == "480.00 GBP"
    assert flights["fare"]["per_traveler"] == "480.00 GBP"
    # The page renders it without a per-leg price appearing twice.
    html = itinerary_html.render(out["itinerary"]["payload"], theme="arya")
    assert "Flights 480.00 GBP, round trip" in html
    assert ">09:15<span>" in html and "London Heathrow (LHR)" in html


async def test_plan_trip_fills_every_day_from_one_call(fake):
    """The sixth live call asked three times whether to start planning and
    saved one activity for one day. The layout is not the model's job."""
    trip = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]
    out = await duffel.do_plan_trip(KEY, CONV, trip, SIGHTS, EATS)

    assert out["machine"]["empty_days"] == []
    assert out["machine"]["days"] == 6 and out["machine"]["count"] == 19
    assert "Planned 19 activities across 6 days" in out["say"]
    assert "For example Wednesday the 10th of March: 09:30 Visit the Belem Tower" in out["say"]
    assert "offer to read any day in full" in out["say"]
    # After the flight, "your booking is on your screen"; the itinerary is
    # only claimed once the days are on it.
    assert "written itinerary is on their screen" in out["say"]
    # Seen live: the agent read one day back, the caller declined more, and
    # the call ended without email ever being mentioned. The offer is made
    # in the same breath the plan is finished, not left to a later moment.
    assert "ask whether to email it to them now that it is finished" in out["say"]

    days = out["itinerary"]["payload"]["day_plan"]
    assert len(days) == 6
    # The verb the sample desk has always used, so the two pages match.
    first = [s["activity"] for s in days[0]["slots"]]
    assert first == ["Visit the Belem Tower", "Explore the Jeronimos Monastery",
                     "Lunch at Time Out Market", "Dinner at Cervejaria Ramiro"]
    # And the heading carries a theme, as the sample desk's always did.
    assert days[0]["theme"] and days[0]["theme"] != ""
    assert [s["time"] for s in days[0]["slots"]] == ["09:30", "11:30", "13:00", "19:30"]
    # The note and the Maps link the agent looked up reach the page.
    assert days[0]["slots"][0]["description"] == "Riverside fort"
    assert days[0]["slots"][0]["url"] == "https://maps/1"


async def test_plan_trip_leaves_room_on_the_days_the_flights_take(fake):
    """Landing at 11:50 and leaving at 18:00 leaves one sight at each end,
    not three. The times come from the booked order, not from the model."""
    trip = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]
    await duffel.do_book_flight(KEY, CONV, trip, 1, [TRAVELLER],
                                "tony@example.com", "+442080160508")
    out = await duffel.do_plan_trip(KEY, CONV, trip, SIGHTS, EATS)
    days = out["itinerary"]["payload"]["day_plan"]
    sights = [[s for s in d["slots"] if not s["activity"].startswith(("Lunch at", "Dinner at"))]
              for d in days]
    assert [len(s) for s in sights] == [1, 2, 1, 1, 1, 1]


async def test_plan_trip_asks_for_more_places_instead_of_leaving_holes(fake):
    trip = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]
    out = await duffel.do_plan_trip(KEY, CONV, trip, SIGHTS[:2], EATS)
    assert out["machine"]["empty_days"] == ["2027-03-12", "2027-03-13",
                                            "2027-03-14", "2027-03-15"]
    assert "4 days had nothing to fill it" in out["say"]
    assert "Friday the 12th of March" in out["say"]
    assert "Offer to look up more places" in out["say"]


async def test_plan_trip_without_restaurants_invents_no_meals(fake):
    trip = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]
    out = await duffel.do_plan_trip(KEY, CONV, trip, SIGHTS)
    rows = [s["activity"] for d in out["itinerary"]["payload"]["day_plan"] for s in d["slots"]]
    assert not [r for r in rows if r.startswith(("Lunch at", "Dinner at"))]
    assert "local cafe" not in " ".join(rows)


async def test_plan_trip_says_what_stopped_it(fake):
    trip = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]
    bad = await duffel.do_plan_trip(KEY, CONV, trip, [])
    assert bad["error"] and "each with a title" in bad["say"]
    # A second pass fills the gaps rather than doubling what is already there.
    await duffel.do_plan_trip(KEY, CONV, trip, SIGHTS, EATS)
    again = await duffel.do_plan_trip(KEY, CONV, trip, SIGHTS, EATS)
    assert again["error"] and "already planned" in again["say"]
    # No dates yet: say so rather than inventing a week.
    empty = duffel._new_trip("test", CONV)
    nowhere = await duffel.do_plan_trip(KEY, CONV, empty.id, SIGHTS)
    assert nowhere["error"] and "no dates yet" in nowhere["say"]


async def test_plan_days_saves_a_whole_plan_in_one_call(fake):
    trip = (await duffel.do_search_flights(KEY, CONV, "London", "Lisbon", OUT, BACK))["machine"]["trip_id"]
    out = await duffel.do_plan_days(KEY, CONV, trip, [
        {"day": "day 2", "time": "afternoon", "title": "Alfama walk", "note": "old town"},
        {"day": "day 2", "time": "morning", "title": "Belem Tower", "url": "https://maps.google.com/?q=belem"},
        {"day": "day 2", "time": "19:30", "title": "Dinner at Time Out Market"},
        {"day": "day 3", "title": "Sintra day trip"},
        {"day": "day 9", "title": "Nowhere"},    # no such day: skipped, the rest survive
        {"day": "day 3", "title": ""},           # no title: skipped
    ])
    assert out["machine"]["count"] == 4 and out["machine"]["rejected"] == 2
    assert "Planned 4 activities across 2 days" in out["say"]
    assert "For example Thursday the 11th of March: morning Belem Tower, afternoon Alfama walk, 19:30 Dinner at Time Out Market" in out["say"]
    assert "2 items had no usable day or title" in out["say"]
    assert "ask whether to email it to them now that it is finished" in out["say"]
    days = out["itinerary"]["payload"]["day_plan"]
    assert [s["activity"] for s in days[0]["slots"]] == ["Belem Tower", "Alfama walk", "Dinner at Time Out Market"]
    assert days[0]["slots"][0]["time"] == "morning" and days[0]["slots"][2]["time"] == "19:30"
    summary = await duffel.do_trip_summary(KEY, CONV, trip)
    assert "morning: Belem Tower" in summary["machine"]["summary"]
    bad = await duffel.do_plan_days(KEY, CONV, trip, [])
    assert bad["error"] and "list of items" in bad["say"]


async def test_check_key(fake):
    assert "starts with duffel_test_" in (await duffel.check_key("sk-nope"))["error"]
    ok = await duffel.check_key(KEY)
    assert ok["ok"] and ok["mode"] == "test" and ok["label"] == "Duffel (test mode)"
    assert "Test token" in ok["warnings"][0]
