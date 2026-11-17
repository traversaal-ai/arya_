"""Display-ready formatting shared by every option-card generator.

These format the SAME data the spoken helpers speak (duffel.py's
say_money/say_clock/say_duration, travel_tools.py's _say_money/_say_time),
but for a screen: symbols instead of words, a 24-hour clock instead of "in
the morning", short units instead of full words. Kept separate from the
spoken helpers so travel_tools.py (which has no lexicon of spoken currency
words) and duffel.py (which does) can both reach the same card formatting
without one importing the other's caller-facing strings.

Runs in both the agent-worker (travel_tools.py) and the arya-mcp sidecar
(mcp_tools/duffel.py): same image, same backend/src on the path (see
docker-compose.yml, arya-mcp: `build: .`), the same way both already import
day_plan.
"""

import datetime
import re

# Currencies common enough for a symbol; anything else keeps the ISO code
# after the number ("PKR 1,200"), which is still readable and never wrong.
_SYMBOLS = {
    "USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥", "INR": "₹",
    "CAD": "CA$", "AUD": "AU$", "AED": "AED ",
}


def money_number(text) -> float | None:
    """'$842' / '1,024' / 842.0 -> 842.0. None if nothing numeric, never
    raising, so a weird live price string can't crash a card build."""
    if text is None:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(text))
    try:
        return float(m.group(0).replace(",", "")) if m else None
    except ValueError:
        return None


def fmt_money(amount, currency: str = "") -> str:
    """"$433.04", "€1,024.00", "£240.50", "PKR 1,200": two decimals when the
    amount has cents, none when it is a whole number, so "$210" reads as a
    round nightly rate and "$433.04" reads as an exact fare. Returns "" for
    an unparseable amount rather than a placeholder string, so a card can
    drop the price line instead of showing garbage."""
    value = money_number(amount)
    if value is None:
        return ""
    code = (currency or "").upper()
    symbol = _SYMBOLS.get(code)
    whole = abs(value - round(value)) < 0.005
    num = f"{abs(value):,.0f}" if whole else f"{abs(value):,.2f}"
    sign = "-" if value < 0 else ""
    if symbol:
        return f"{sign}{symbol}{num}"
    return f"{sign}{code} {num}".strip() if code else f"{sign}{num}"


def fmt_day(value: str) -> str:
    """"2026-10-01" -> "Oct 1": the group header's date range, short enough
    to sit next to the route on one line."""
    try:
        d = datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{d.strftime('%b')} {d.day}"


def fmt_clock(iso_local: str) -> str:
    """"00:00", "05:27": 24-hour, locale-neutral, matches the itinerary
    page. "" for an unparseable timestamp."""
    try:
        t = datetime.datetime.fromisoformat(str(iso_local).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{t.hour:02d}:{t.minute:02d}"


_ISO_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?")


def fmt_duration(iso: str) -> str:
    """"PT7H27M" -> "7h 27m"; "PT45M" -> "45m". "" when nothing matches."""
    m = _ISO_DUR.fullmatch(str(iso or ""))
    if not m:
        return ""
    days, hours, mins = (int(x) if x else 0 for x in m.groups())
    hours += days * 24
    bits = []
    if hours:
        bits.append(f"{hours}h")
    if mins or not hours:
        bits.append(f"{mins}m")
    return " ".join(bits)


_WORDS_DUR = re.compile(r"(?:(\d+)\s*hours?)?\s*(?:(\d+)\s*minutes?)?", re.IGNORECASE)


def shorten_duration_words(text: str) -> str:
    """The SerpAPI desk already writes durations in words, "7 hours 25
    minutes"; shorten that to "7h 25m" for the card without a second
    ISO-duration path. Falls back to the original text untouched if it
    does not look like the expected shape."""
    if not text:
        return ""
    m = _WORDS_DUR.match(text.strip())
    if not m or not (m.group(1) or m.group(2)):
        return text
    bits = []
    if m.group(1):
        bits.append(f"{m.group(1)}h")
    if m.group(2):
        bits.append(f"{m.group(2)}m")
    return " ".join(bits)


def plus_days(dep_iso: str, arr_iso: str) -> int:
    """0 when a flight lands the same calendar day it departs (compared in
    each timestamp's own local time), 1 or more when it crosses midnight.
    0 for anything unparseable rather than raising."""
    try:
        dep = datetime.datetime.fromisoformat(str(dep_iso).replace("Z", "+00:00")).date()
        arr = datetime.datetime.fromisoformat(str(arr_iso).replace("Z", "+00:00")).date()
    except ValueError:
        return 0
    return max(0, (arr - dep).days)
