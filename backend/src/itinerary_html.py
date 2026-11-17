"""Render a saved trip into a self-contained HTML itinerary page.

Pure function of the payload dict travel_tools builds. No I/O, no external
assets (inline CSS, system fonts), so the stored HTML opens anywhere and
prints cleanly. The page offers one action, Download PDF, which is the
server-rendered file from GET /api/artifacts/{id}/pdf; the @media print
rules below still matter because WeasyPrint applies them (and because a
reader can always hit Ctrl+P), tuned so blocks never split across pages
and headings never strand at a page bottom. There is deliberately no
Print button: the browser already has one, and its output lacks the page
counters the server PDF gets from the @page margin boxes. Every
interpolated value passes through html.escape; tool data includes live
SerpAPI strings.

Themes (option A: chosen once, at generation, and baked into the stored
page): the layout is identical across themes; only the token values in
_PALETTES differ. "arya" matches the builder UI's indigo accent and is
used for builder test calls; "neutral" is a warm, brandless palette used
for calls that arrived through an embedded deployment.
"""

from __future__ import annotations

import datetime
import re
from html import escape as _e

# Colour tokens only; structure never varies by theme. Adding a theme =
# adding an entry here (a future per-deployment branding option would
# override individual tokens the same way).
_PALETTES = {
    # Builder-matched: indigo accent, cool neutrals.
    "arya": {
        "ink": "#23232a", "muted": "#6d6d74", "panel": "#f6f6f8",
        "line": "#e3e3ea", "accent": "#5a63d6", "deep": "#262a66",
        "band": "#eceefc", "link": "#4a52c4",
    },
    # Brandless warm palette for embedded deployments.
    "neutral": {
        "ink": "#1e2a28", "muted": "#6b7a77", "panel": "#f4f0ea",
        "line": "#e3ddd2", "accent": "#b4532f", "deep": "#123332",
        "band": "#dcebe3", "link": "#2c6e63",
    },
}

# The :root token block is generated from the palette dict (plain string
# join, not %-formatting: CSS is full of literal % signs).
_CSS = """
* { box-sizing: border-box; margin: 0; }
body { font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
  color: var(--ink); background: var(--panel); line-height: 1.5; }
.page { max-width: 760px; margin: 0 auto; padding: 40px 20px 120px; }
.brand { text-align: center; margin-bottom: 8px; }
.brand-badge { display: inline-flex; width: 52px; height: 52px; align-items: center;
  justify-content: center; background: var(--accent); color: #fff; font-size: 26px;
  border-radius: 16px 18px 14px 17px; }
.brand-kicker { margin-top: 10px; font-size: 12px; letter-spacing: 0.18em;
  text-transform: uppercase; color: var(--deep); font-weight: 600; }
h1 { font-family: 'Space Grotesk', 'Inter', system-ui, sans-serif; font-weight: 500;
  font-size: 34px; text-align: center; margin: 10px 0 6px; }
.tagline { text-align: center; color: var(--muted); font-size: 14px; margin-bottom: 26px; }
.sheet { background: #fff; border: 1px solid var(--line); border-radius: 14px;
  overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,.08); }
.journey { background: var(--band); padding: 18px 26px; display: flex;
  flex-wrap: wrap; align-items: baseline; gap: 6px 16px; }
.journey .kicker { flex-basis: 100%; }
.journey h2 { font-family: 'Space Grotesk', 'Inter', system-ui, sans-serif; font-weight: 500; font-size: 24px; }
.journey .meta { margin-left: auto; color: var(--muted); font-size: 13px; }
.kicker { font-size: 11px; letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--accent); font-weight: 700; }
.section { padding: 20px 26px; border-top: 1px solid var(--line); }
.section:first-of-type { border-top: none; }
.frow { display: flex; justify-content: space-between; gap: 16px; font-size: 14px; }
.frow + .frow { margin-top: 2px; }
.frow .right { color: var(--muted); text-align: right; white-space: nowrap; }
.fname { font-weight: 600; }
.fmeta { color: var(--muted); font-size: 13px; margin-top: 2px; }
/* Depart and arrive side by side, the way a printed itinerary reads.
   auto-fit collapses them to one column on a narrow screen with no media
   query, so the print stylesheet inherits the same rule. */
.legs { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 10px 24px; margin-top: 12px; }
.leg .kicker { font-size: 10px; }
.leg .t { font-family: 'Space Grotesk', 'Inter', system-ui, sans-serif; font-size: 20px;
  font-variant-numeric: tabular-nums; margin-top: 2px; }
.leg .t span { font-family: 'Inter', system-ui, sans-serif; font-size: 13px;
  color: var(--muted); margin-left: 8px; }
.leg .where { font-size: 13px; }
.ref { color: var(--muted); font-size: 12.5px; margin-top: 10px; }
a { color: var(--link); }
.day { border-top: 1px dashed var(--line); padding-top: 16px; margin-top: 16px; }
.day:first-of-type { border-top: none; padding-top: 4px; margin-top: 4px; }
.day h3 { font-family: 'Space Grotesk', 'Inter', system-ui, sans-serif; font-weight: 600; font-size: 17px; }
.day .loc { color: var(--muted); font-size: 12.5px; margin-bottom: 8px; }
.slot { display: flex; gap: 12px; font-size: 13.5px; padding: 4px 0; }
.slot .t { flex: none; width: 110px; color: var(--accent);
  font-variant-numeric: tabular-nums; }
.slot > span:last-child { min-width: 0; }
.slot .d { color: var(--muted); font-size: 12.5px; }
.totalbar { position: sticky; bottom: 0; margin-top: 18px; background: var(--deep);
  color: #fff; border-radius: 12px; padding: 16px 24px; display: flex;
  align-items: center; justify-content: space-between; gap: 16px; }
.totalbar .lbl { font-size: 13px; font-weight: 600; }
.totalbar .note { font-size: 11px; opacity: 0.65; }
.totalbar .amt { font-family: 'Space Grotesk', 'Inter', system-ui, sans-serif; font-size: 30px; }
.actions { display: flex; gap: 10px; justify-content: center; margin: 22px 0 0; }
.actions a { font: inherit; font-size: 14px; padding: 9px 18px;
  border-radius: 24px; border: 1px solid var(--accent); background: #fff;
  color: var(--accent); cursor: pointer; text-decoration: none; }
.actions .primary { background: var(--accent); color: #fff; }

/* Print: A4 with real margins; the sheet must not clip (overflow: hidden
   makes browsers cut content at page boundaries); flight/hotel blocks and
   individual day rows never split across pages, and headings never strand
   at a page bottom. The day-plan section (.plan) is deliberately allowed
   to break: it is usually longer than a page, and its children carry
   their own break rules. */
@page { size: A4; margin: 14mm;
  @bottom-right { content: counter(page) " of " counter(pages);
    font-size: 9px; color: #999; } }
@media print {
  body { background: #fff;
    -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .page { padding: 0; max-width: none; }
  .actions { display: none; }
  .sheet { border: none; box-shadow: none; overflow: visible; }
  .section { padding: 12px 6px; }
  .journey { padding: 14px 10px; }
  .section:not(.plan) { break-inside: avoid; }
  /* A day may break BETWEEN rows (avoiding it wholesale left page-sized
     gaps), but a single row never splits and a day heading never strands
     without its first rows. */
  .slot, .frow { break-inside: avoid; }
  .day h3, .kicker, .day .loc { break-after: avoid; }
  p, .tagline { orphans: 3; widows: 3; }
  .totalbar { position: static; border-radius: 8px; break-inside: avoid; }
}
"""


def _short_date(iso: str) -> str:
    """"2026-10-05" -> "Mon 5 Oct". Day number built by hand: the %-d that
    drops the leading zero is a GNU extension and not portable."""
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
    except (ValueError, TypeError):
        return str(iso or "")
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _short_duration(text: str) -> str:
    """"8 hours 27 minutes" -> "8h 27m". Spelled out is right for speech,
    which is where say_duration is used; on a line beside the airline and
    the flight number it is just long."""
    t = re.sub(r"\s*hours?\b", "h", str(text or ""))
    t = re.sub(r"\s*minutes?\b", "m", t)
    return re.sub(r"\s+", " ", t).strip()


def _endpoint(kicker: str, point) -> str:
    """One side of a flight: the time first, because that is what the
    reader came for, then the date, then where. Accepts the structured
    form and, for a page stored before this existed, the old sentence."""
    if not isinstance(point, dict):
        text = str(point or "")
        if not text:
            return ""
        return (f"<div class='leg'><div class='kicker'>{_e(kicker)}</div>"
                f"<div class='where'>{_e(text)}</div></div>")
    when = _short_date(point.get("date", ""))
    place = point.get("airport") or ""
    code = point.get("code") or ""
    if code and code not in place:
        place = f"{place} ({code})" if place else code
    return (f"<div class='leg'><div class='kicker'>{_e(kicker)}</div>"
            f"<div class='t'>{_e(point.get('time', ''))}"
            + (f"<span>{_e(when)}</span>" if when else "")
            + f"</div><div class='where'>{_e(place)}</div></div>")


def _flight_section(label: str, f: dict | None, travelers: int,
                    show_price: bool = True) -> str:
    """One leg. `show_price` is false when the fare covers the whole
    journey (one round-trip offer), because printing it under both legs
    read as double the real price."""
    if not f:
        return ""
    price = (f"<span class='right'>{_e(f['price'])}"
             + (" / traveler" if travelers > 1 else "")
             + "</span>") if show_price and f.get("price") else "<span class='right'></span>"
    meta = " · ".join(_e(str(x)) for x in (
        f.get("flight_number"), f.get("stops"), _short_duration(f.get("duration"))) if x)
    legs = _endpoint("Depart", f.get("depart")) + _endpoint("Arrive", f.get("arrive"))
    ref = (f"<div class='ref'>Airline reference (for check-in) {_e(f['reference'])}</div>"
           if f.get("reference") else "")
    link = (f"<div class='frow'><a href='{_e(f['url'])}' target='_blank'"
            f" rel='noreferrer'>Search this route on Google Flights →</a></div>"
            if f.get("url") else "")
    return f"""
<div class="section">
  <div class="kicker">{_e(label)}</div>
  <div class="frow"><span class="fname">{_e(f['airline'])}</span>{price}</div>
  {f"<div class='fmeta'>{meta}</div>" if meta else ""}
  <div class="legs">{legs}</div>
  {ref}{link}
</div>"""


def _hotel_section(h: dict | None) -> str:
    if not h:
        return ""
    total = (
        f"<div class='frow'><span></span><span class='right'>Total {_e(h['total'])}"
        f" ({h['nights']} night{'s' if h['nights'] != 1 else ''})</span></div>"
        if h.get("total") else ""
    )
    link = (
        f"<div class='frow'><a href='{_e(h['maps_url'])}' target='_blank'"
        f" rel='noreferrer'>View on Google Maps →</a></div>"
        if h.get("maps_url") else ""
    )
    dates = (
        f"<div class='frow'><span>Check in {_e(h['check_in'])}, check out {_e(h['check_out'])}"
        + (f" · free cancellation until {_e(str(h['free_cancel_until'])[:10])}"
           if h.get("free_cancel_until") else "")
        + "</span></div>"
        if h.get("check_in") and h.get("check_out") else ""
    )
    ref = (
        f"<div class='frow'><span>Hotel reference {_e(h['reference'])}</span></div>"
        if h.get("reference") else ""
    )
    return f"""
<div class="section">
  <div class="kicker">Hotel</div>
  <div class="frow"><span class="fname">{_e(h['name'])}</span>
    <span class="right">{_e(h['price_per_night'])}/night</span></div>
  <div class="frow"><span>via {_e(h['source'])} · rated {_e(h['rating'])}</span></div>
  {dates}{total}{ref}{link}
</div>"""


def _day_plan_section(days: list[dict]) -> str:
    if not days:
        return ""
    parts = ["<div class='section plan'><div class='kicker'>Day-by-day plan</div>"]
    for d in days:
        slots = "".join(
            f"<div class='slot'><span class='t'>{_e(s['time'])}</span>"
            # Google's terms want the Maps source shown for places it named,
            # and a linked name is that link. A line of its own under every
            # row turned a four-item day into four identical links with
            # places attached.
            + (f"<span><a href='{_e(s['url'])}' target='_blank' rel='noreferrer'>"
               f"{_e(s['activity'])}</a>" if s.get("url")
               else f"<span>{_e(s['activity'])}")
            + (f"<div class='d'>{_e(s['description'])}</div>" if s.get("description") else "")
            + "</span></div>"
            for s in d.get("slots", [])
        )
        theme = f" · {_e(d['theme'])}" if d.get("theme") else ""
        loc = f"<div class='loc'>{_e(d['location'])}</div>" if d.get("location") else ""
        parts.append(f"<div class='day'><h3>{_e(d['label'])}{theme}</h3>{loc}{slots}</div>")
    parts.append("</div>")
    return "".join(parts)


def render(p: dict, theme: str = "arya") -> str:
    """payload dict (see travel_tools._itinerary_payload) -> full HTML page.
    theme picks a palette from _PALETTES (unknown names fall back to arya);
    it changes colours only, never structure."""
    palette = _PALETTES.get(theme) or _PALETTES["arya"]
    _root = ":root { " + " ".join(f"--{k}: {v};" for k, v in palette.items()) + " }"
    brand = p.get("brand", {})
    trip = p.get("trip", {})
    flights = p.get("flights", {})
    totals = p.get("totals", {})
    travelers = trip.get("travelers") or 1

    route = ""
    if trip.get("dest_city"):
        origin = (
            f"{_e(trip.get('origin_city') or '')}"
            + (f" ({_e(trip['origin_code'])})" if trip.get("origin_code") else "")
        )
        dest = (
            f"{_e(trip['dest_city'])}"
            + (f" ({_e(trip['dest_code'])})" if trip.get("dest_code") else "")
        )
        route = f"{origin} → {dest}" if origin.strip() else dest
    dates = _e(trip.get("depart_date") or "")
    if trip.get("return_date"):
        dates += f" – {_e(trip['return_date'])}"
    # One round-trip offer carries ONE fare, which belongs here and not
    # under each leg; two one-way selections really are priced per leg.
    fare = flights.get("fare") if isinstance(flights.get("fare"), dict) else {}
    per_leg = fare.get("scope") != "journey"
    fare_text = ""
    if not per_leg and fare.get("total"):
        each = (f" ({_e(fare['per_traveler'])} each)"
                if fare.get("per_traveler") and travelers > 1 else "")
        fare_text = f"Flights {_e(fare['total'])}{each}, round trip"
    meta = " · ".join(
        x for x in (dates, f"{travelers} traveler{'s' if travelers != 1 else ''}",
                    fare_text) if x
    )

    # `brand.mode` is deliberately NOT printed. The owner sees the sandbox
    # warning on the connection card when they paste the token, so the one
    # person who could act on it already knows, and the caller cannot.
    if brand.get("mock"):
        total_note = "Sample offers · for planning purposes only"
    else:
        total_note = "Prices as found at search time"
    totalbar = (
        f"""<div class="totalbar"><div><div class="lbl">Estimated total</div>
        <div class="note">{_e(total_note)}</div></div>
        <div class="amt">{_e(totals['grand'])}</div></div>"""
        if totals.get("grand") else ""
    )

    name = brand.get("name") or "Arya"
    icon = brand.get("icon") or "✈️"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(name)} · Itinerary</title>
<!--fonts-start-->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
<!--fonts-end-->
<style>{_root}{_CSS}</style>
</head>
<body>
<div class="page">
  <div class="brand">
    <span class="brand-badge">{_e(icon)}</span>
    <div class="brand-kicker">{_e(name)} · Itinerary</div>
  </div>
  <h1>Your trip, planned out loud.</h1>
  <p class="tagline">Flights, stay, and a day-by-day plan, grounded in what
  the agent actually found for you. Nothing invented.</p>
  <div class="sheet">
    <div class="journey">
      <div class="kicker">Journey</div>
      <h2>{route or "Trip in progress"}</h2>
      <span class="meta">{meta}</span>
    </div>
    {_flight_section("Outbound flight", flights.get("outbound"), travelers, per_leg)}
    {_flight_section("Return flight", flights.get("return"), travelers, per_leg)}
    {_hotel_section(p.get("hotel"))}
    {_day_plan_section(p.get("day_plan") or [])}
  </div>
  {totalbar}
  <div class="actions">
    <a id="pdfLink" class="primary" href="#">Download PDF</a>
  </div>
  <script>
    document.getElementById("pdfLink").href =
      /* [/] not \\/ : this JS lives inside a Python f-string, and \\/ is not a
         Python escape (SyntaxWarning today, SyntaxError on newer Pythons).
         A character class strips trailing slashes with no escape at all. */
      location.pathname.replace(/[/]+$/, "") + "/pdf";
  </script>
</div>
</body>
</html>"""
