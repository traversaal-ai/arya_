"""display_fmt: the screen-facing formatters shared by duffel.py and
travel_tools.py. Pinned here once so both card generators can trust them
without re-testing the same regex twice."""

import display_fmt as fmt


def test_fmt_money_symbols_and_decimals():
    assert fmt.fmt_money("433.04", "USD") == "$433.04"
    assert fmt.fmt_money("210", "USD") == "$210"
    assert fmt.fmt_money(1024, "EUR") == "€1,024"
    assert fmt.fmt_money("240.50", "GBP") == "£240.50"
    assert fmt.fmt_money("1200", "PKR") == "PKR 1,200"
    assert fmt.fmt_money(None, "USD") == ""
    assert fmt.fmt_money("not a number", "USD") == ""


def test_fmt_money_negative():
    assert fmt.fmt_money("-50", "USD") == "-$50"


def test_fmt_clock_is_24_hour():
    assert fmt.fmt_clock("2026-10-01T00:00:00") == "00:00"
    assert fmt.fmt_clock("2026-10-01T05:27:00Z") == "05:27"
    assert fmt.fmt_clock("2026-10-01T14:33:00+00:00") == "14:33"
    assert fmt.fmt_clock("garbage") == ""


def test_fmt_duration_iso():
    assert fmt.fmt_duration("PT7H27M") == "7h 27m"
    assert fmt.fmt_duration("PT45M") == "45m"
    assert fmt.fmt_duration("PT2H") == "2h"
    assert fmt.fmt_duration("") == ""
    assert fmt.fmt_duration("not iso") == ""


def test_shorten_duration_words():
    assert fmt.shorten_duration_words("7 hours 25 minutes") == "7h 25m"
    assert fmt.shorten_duration_words("45 minutes") == "45m"
    assert fmt.shorten_duration_words("2 hours") == "2h"
    assert fmt.shorten_duration_words("") == ""
    assert fmt.shorten_duration_words("nonstop") == "nonstop"  # unrecognised shape, unchanged


def test_plus_days():
    assert fmt.plus_days("2026-10-01T23:50:00Z", "2026-10-02T01:10:00Z") == 1
    assert fmt.plus_days("2026-10-01T09:00:00Z", "2026-10-01T11:00:00Z") == 0
    assert fmt.plus_days("garbage", "2026-10-01T11:00:00Z") == 0


def test_fmt_day():
    assert fmt.fmt_day("2026-10-01") == "Oct 1"
    assert fmt.fmt_day("2026-12-25T00:00:00Z") == "Dec 25"
    assert fmt.fmt_day("garbage") == "garbage"
