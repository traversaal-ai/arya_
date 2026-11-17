"""Email the written itinerary as the page itself, sent by the worker.

The caller sees a laid-out trip page on their screen and asks for it by
email. Until now the model wrote the email: it called trip_summary, read
the plain-text recap back out as a tool argument, and whatever arrived in
the inbox was a paraphrase of a page nobody sent. The page is about 8 KB
of HTML, which is not something a model should be retyping into a tool
argument at all, and the one time the cap was hit the turn died silently
mid-JSON.

So the model's part is one address. Everything else happens here: the
stored artifact is read straight out of Postgres, the on-page Download
button is stripped (a link to "/pdf" relative to nothing is dead in an
inbox), a real link to the PDF is added when this deployment has a public
address, and the whole thing goes to the email connector as the body.

Three things ride along with that:

  - **Add-to-calendar links**, so the trip reaches the traveller's own
    calendar. Nothing is written to anyone's calendar by Arya: a
    Composio-created event can only live on the account the AGENCY
    connected, and the trip belongs to the client.
  - **A booking confirmation**, sent the moment a flight is booked. Duffel
    sends a passenger nothing on a test token, and on a live one it is the
    airline that may or may not.
  - **An agency copy of that confirmation**, because money moved: the
    operational record, to the address the agency's own connected mailbox
    reports.

Which connector can send mail is a catalog fact, not a guess: an entry
declares `email_via` and this module renders the arguments for that
vendor's shape. Composio is the only one today.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import uuid
from html import escape as _esc
from urllib.parse import urlencode

from livekit.agents import RunContext, function_tool

import connectors
import db

logger = logging.getLogger("itinerary_email")

# The on-page action bar: a button whose href is filled in by JavaScript
# from the current location. In an inbox it points at nothing.
_ACTIONS = re.compile(r"<div class=\"actions\">.*?</div>\s*<script>.*?</script>",
                      re.DOTALL)
_BODY_END = "</div>\n</body>"
_GCAL = "https://calendar.google.com/calendar/render"


def public_base() -> str:
    """Where this Arya answers from, for a link someone can actually open.
    Empty on a laptop, and then no link is offered rather than one to
    localhost that dies the moment the mail leaves the machine."""
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    return base if base.startswith(("http://", "https://")) else ""


# ─── add to calendar ────────────────────────────────────────────────────────
#
# Every event is written as ALL DAY, with the exact local times in its
# description. Google's TEMPLATE URL has no way to say which timezone a
# time is in, so a timed event would be read in whatever timezone the
# reader's calendar happens to use: a flight leaving Islamabad at midnight
# would land in a European calendar at midnight European time, which is
# simply false. An all-day event on the right date is never wrong, and the
# times the traveller actually needs are one tap away inside it.


def _day_after(iso: str) -> str:
    try:
        d = datetime.date.fromisoformat(str(iso)[:10]) + datetime.timedelta(days=1)
    except (ValueError, TypeError):
        return ""
    return d.isoformat()


def calendar_url(title: str, start: str, end: str = "", *,
                 details: str = "", location: str = "") -> str:
    """A Google Calendar link that pre-fills one all-day event. `end` is
    exclusive, as Google reads it, so a stay from the 1st to the 5th is
    exactly "1/5" and covers the four nights."""
    a = str(start or "")[:10].replace("-", "")
    b = str(end or "")[:10].replace("-", "") or _day_after(start).replace("-", "")
    if not (a and b):
        return ""
    return _GCAL + "?" + urlencode({
        "action": "TEMPLATE", "text": title, "dates": f"{a}/{b}",
        "details": details.strip(), "location": location.strip(),
    })


def _point(p) -> tuple[str, str, str]:
    """(time, date, place) from a structured leg endpoint, or from the
    sentence a page stored before the structured form existed."""
    if isinstance(p, dict):
        place = p.get("airport") or ""
        code = p.get("code") or ""
        if code and code not in place:
            place = f"{place} ({code})" if place else code
        return p.get("time", ""), str(p.get("date", ""))[:10], place
    return "", "", str(p or "")


def calendar_links(payload: dict) -> list[dict]:
    """One link per thing a traveller would want in their calendar: each
    flight, the stay, and each planned day. [{label, url}]."""
    payload = payload or {}
    trip = payload.get("trip") or {}
    city = trip.get("dest_city") or ""
    out: list[dict] = []

    flights = payload.get("flights") or {}
    for key, word in (("outbound", "Outbound flight"), ("return", "Return flight")):
        f = flights.get(key)
        if not isinstance(f, dict):
            continue
        dep_t, dep_d, dep_p = _point(f.get("depart"))
        arr_t, arr_d, arr_p = _point(f.get("arrive"))
        if not dep_d:
            continue
        number = f.get("flight_number") or ""
        title = " ".join(x for x in (f.get("airline") or "Flight", number) if x)
        if dep_p and arr_p:
            title += f": {dep_p.split(' (')[0]} to {arr_p.split(' (')[0]}"
        details = []
        if dep_t:
            details.append(f"Departs {dep_p or 'the airport'} at {dep_t} local time.")
        if arr_t:
            details.append(f"Arrives {arr_p or 'the airport'} at {arr_t} local time"
                           + (f" on {arr_d}." if arr_d and arr_d != dep_d else "."))
        if f.get("reference"):
            details.append(f"Airline reference (for check-in) {f['reference']}.")
        # A bare URL in an event description is auto-linked by Calendar's
        # own UI, the same way Gmail auto-links one in a message body. This
        # is the same honest search link the itinerary page carries, not a
        # live tracker or a check-in page, since neither exists to link to.
        if f.get("url"):
            details.append(f"Search this route on Google Flights: {f['url']}")
        out.append({"label": word, "url": calendar_url(
            title, dep_d, details=" ".join(details), location=dep_p)})

    hotel = payload.get("hotel") or {}
    check_in = hotel.get("check_in") or trip.get("depart_date")
    check_out = hotel.get("check_out") or trip.get("return_date")
    if hotel.get("name") and check_in and check_out:
        details = [f"{hotel['name']}."]
        if hotel.get("price_per_night"):
            details.append(f"{hotel['price_per_night']} per night.")
        if hotel.get("total"):
            nights = hotel.get("nights")
            details.append(
                f"{hotel['total']} total"
                + (f" for {nights} night{'s' if nights != 1 else ''}." if nights else "."))
        if hotel.get("reference"):
            details.append(f"Hotel reference {hotel['reference']}.")
        if hotel.get("maps_url"):
            details.append(f"View on Google Maps: {hotel['maps_url']}")
        out.append({"label": "Hotel stay", "url": calendar_url(
            f"Stay at {hotel['name']}", check_in, check_out,
            details=" ".join(details), location=f"{hotel['name']}, {city}".strip(", "))})

    for day in payload.get("day_plan") or []:
        if not isinstance(day, dict) or not day.get("date"):
            continue
        slots = [s for s in (day.get("slots") or []) if isinstance(s, dict)]
        names = [s.get("activity", "") for s in slots if s.get("activity")]
        if not names:
            continue
        lines = []
        for s in slots:
            lines.append(" ".join(x for x in (s.get("time"), s.get("activity")) if x))
            if s.get("url"):
                lines.append(s["url"])
        out.append({"label": day.get("label") or day["date"], "url": calendar_url(
            f"{city}: {', '.join(names[:2])}" if city else ", ".join(names[:2]),
            day["date"], details="\n".join(lines), location=city)})

    return [x for x in out if x["url"]]


def _calendar_block(payload: dict) -> str:
    """The links as a block near the end of the mail rather than one under
    each section: injecting into the stored page's markup would depend on
    its exact shape, and this reads as one clear offer."""
    links = calendar_links(payload)
    if not links:
        return ""
    rows = "".join(
        f"<div class='frow'><a href='{_esc(x['url'])}'>Add {_esc(x['label'])}"
        " to your calendar →</a></div>"
        for x in links
    )
    return ("<div class='section'><div class='kicker'>Add to your calendar</div>"
            + rows + "</div>")


# ─── the itinerary mail ─────────────────────────────────────────────────────


def email_body(html: str, artifact_id: str, base: str = "",
               payload: dict | None = None) -> str:
    """The stored page, made sendable."""
    body = _ACTIONS.sub("", html or "")
    extra = _calendar_block(payload or {})
    if base:
        link = f"{base}/api/artifacts/{artifact_id}"
        extra += (
            f'<div class="actions"><a class="primary" href="{link}/pdf">'
            "Download PDF</a></div>"
            f'<p class="tagline">Or open it in your browser: '
            f'<a href="{link}">{link}</a></p>'
        )
    return body.replace(_BODY_END, extra + "\n" + _BODY_END, 1) if extra else body


def sender(live: list[dict]) -> tuple[dict, dict] | None:
    """The first live connector that says it can send mail, with the
    `email_via` recipe from its catalog entry. None when this call has no
    way to send, which the agent is told to say out loud."""
    for conn in live or []:
        via = (conn.get("entry") or {}).get("email_via")
        if not isinstance(via, dict):
            continue
        if via.get("tool") in (conn.get("tool_names") or []):
            return conn, via
    return None


def agency_address(live: list[dict], runtime: dict | None = None) -> str:
    """Where the agency's own copy goes. The mailbox the owner connected at
    the vendor is the default, learned when they pressed Test and shown on
    the card, because every mail the agent sends already leaves from it. An
    agent-level field overrides it for an operations inbox."""
    override = ((runtime or {}).get("copy_bookings_to") or "").strip()
    if "@" in override:
        return override
    found = sender(live)
    if found is None:
        return ""
    addr = ((found[0].get("row") or {}).get("config") or {}).get("account_email") or ""
    return addr.strip()


def _arguments(via: dict, to: str, subject: str, body: str) -> dict:
    """The vendor's own call shape. Composio exposes a router: one execute
    tool that runs a slug its search returned, so the real action is nested
    inside. A connector with a plain send tool takes the fields directly."""
    if via.get("kind") == "composio":
        return {"tools": [{
            "tool_slug": via.get("slug", "GMAIL_SEND_EMAIL"),
            "arguments": {"recipient_email": to, "to": to, "subject": subject,
                          "body": body, "is_html": True},
        }]}
    return {"to": to, "subject": subject, "body": body, "is_html": True}


async def _send_html(live: list[dict], to: str, subject: str, body: str) -> dict:
    found = sender(live)
    if found is None:
        return {"ok": False, "error": "no connector on this call can send email"}
    conn, via = found
    return await connectors.call_tool(conn, via["tool"], _arguments(via, to, subject, body))


async def _artifact(runtime: dict) -> dict | None:
    pool = await db.get_pool(runtime["dsn"])
    row = await pool.fetchrow(
        "SELECT id, title, html, data FROM artifacts"
        " WHERE conversation_id = $1 AND kind = 'itinerary'",
        uuid.UUID(str(runtime["conversation_id"])),
    )
    if not row:
        return None
    out = dict(row)
    data = out.get("data")
    if isinstance(data, str):
        import json
        try:
            out["data"] = json.loads(data)
        except ValueError:
            out["data"] = None
    return out


async def send(live: list[dict], runtime: dict, to: str) -> str:
    """Send the current itinerary page to one address. Returns the sentence
    the model reads back, which says what happened and nothing else."""
    to = (to or "").strip()
    if "@" not in to or " " in to:
        return f"{to or 'That'} is not an email address. Ask for it again."
    if sender(live) is None:
        return ("There is no way to send email on this call. Say so plainly"
                " and offer the page on their screen instead.")
    try:
        art = await _artifact(runtime)
    except Exception as e:
        logger.warning(f"itinerary lookup failed: {e}")
        return "The itinerary could not be read just now. Offer to try again."
    if not art or not art.get("html"):
        return ("There is no written itinerary yet, so there is nothing to"
                " send. Offer to build one first.")

    subject = art.get("title") or "Your trip itinerary"
    body = email_body(art["html"], str(art["id"]), public_base(),
                      art.get("data") if isinstance(art.get("data"), dict) else None)
    result = await _send_html(live, to, subject, body)
    if not result["ok"]:
        logger.warning(f"itinerary email failed: {result['error']}")
        return (f"The itinerary could not be sent: {result['error']}."
                " Say so plainly and offer to try again.")
    logger.info(f"itinerary {art['id']} emailed to {to}")
    return (f"Sent the written itinerary to {to}, with links to add the trip to"
            " their calendar. Say it was sent and nothing else about it.")


# ─── the booking confirmation, and the agency's copy ────────────────────────

_MAIL_CSS = (
    "font-family:'Segoe UI',system-ui,-apple-system,sans-serif;color:#23232a;"
    "line-height:1.5;max-width:620px;margin:0 auto;padding:24px"
)


def _leg_lines(f: dict) -> str:
    dep_t, dep_d, dep_p = _point(f.get("depart"))
    arr_t, arr_d, arr_p = _point(f.get("arrive"))
    meta = " · ".join(x for x in (f.get("flight_number"), f.get("stops"),
                                 f.get("duration")) if x)
    return (
        f"<p style='margin:14px 0 0'><b>{_esc(f.get('airline') or 'Flight')}</b>"
        + (f"<br><span style='color:#6d6d74;font-size:13px'>{_esc(meta)}</span>" if meta else "")
        + (f"<br>Departs {_esc(dep_p)} at <b>{_esc(dep_t)}</b> on {_esc(dep_d)}"
           if dep_t or dep_p else "")
        + (f"<br>Arrives {_esc(arr_p)} at <b>{_esc(arr_t)}</b> on {_esc(arr_d)}"
           if arr_t or arr_p else "")
        + "</p>"
    )


def booking_email(payload: dict, booking: dict) -> tuple[str, str]:
    """(subject, html) for the traveller, the moment a flight is booked."""
    trip = (payload or {}).get("trip") or {}
    flights = (payload or {}).get("flights") or {}
    where = trip.get("dest_city") or "your trip"
    legs = "".join(_leg_lines(f) for f in (flights.get("outbound"), flights.get("return"))
                   if isinstance(f, dict))
    fare = flights.get("fare") if isinstance(flights.get("fare"), dict) else {}
    total = booking.get("total") or fare.get("total") or ""
    ref = booking.get("reference") or ""
    cal = "".join(
        f"<p style='margin:6px 0'><a href='{_esc(x['url'])}'>Add {_esc(x['label'])}"
        " to your calendar →</a></p>"
        for x in calendar_links({**(payload or {}), "hotel": None, "day_plan": []})
    )
    html = (
        f"<div style=\"{_MAIL_CSS}\">"
        f"<h2 style='margin:0 0 4px'>Your flights are booked</h2>"
        f"<p style='color:#6d6d74;margin:0'>{_esc(where)}</p>"
        + legs
        + (f"<p style='margin:16px 0 0'>Total paid <b>{_esc(total)}</b></p>" if total else "")
        + (f"<p style='margin:4px 0 0'>Airline reference (for check-in)"
           f" <b>{_esc(ref)}</b></p>" if ref else "")
        + (f"<div style='margin-top:18px'>{cal}</div>" if cal else "")
        + "<p style='color:#6d6d74;font-size:13px;margin-top:22px'>Your full"
          " itinerary follows once the rest of the trip is planned.</p></div>"
    )
    return f"Your flights to {where}" if trip.get("dest_city") else "Your flights are booked", html


def agency_email(payload: dict, booking: dict, conversation: str) -> tuple[str, str]:
    """(subject, html) for the agency: the operational record, not the
    pretty page. No calendar links; nobody at the agency is putting a
    client's holiday in their own diary."""
    trip = (payload or {}).get("trip") or {}
    route = " to ".join(x for x in (trip.get("origin_city"), trip.get("dest_city")) if x)
    rows = [
        ("Route", route or "n/a"),
        ("Dates", " to ".join(x for x in (trip.get("depart_date"),
                                          trip.get("return_date")) if x) or "n/a"),
        ("Travellers", ", ".join(booking.get("travellers") or []) or "n/a"),
        ("Email", booking.get("email") or "n/a"),
        ("Phone", booking.get("phone") or "n/a"),
        ("Total", booking.get("total") or "n/a"),
        ("Airline reference", booking.get("reference") or "n/a"),
        ("Order id", booking.get("order_id") or "n/a"),
        ("Conversation", conversation or "n/a"),
    ]
    body = "".join(
        f"<tr><td style='padding:3px 14px 3px 0;color:#6d6d74'>{_esc(k)}</td>"
        f"<td style='padding:3px 0'><b>{_esc(str(v))}</b></td></tr>"
        for k, v in rows
    )
    html = (f"<div style=\"{_MAIL_CSS}\"><h2 style='margin:0 0 12px'>Booking recorded</h2>"
            f"<table style='border-collapse:collapse;font-size:14px'>{body}</table></div>")
    return f"Booking {booking.get('reference') or ''}: {route}".strip(": "), html


async def notify_booking(runtime: dict | None, booking: dict) -> None:
    """Fire and forget, called when a module reports a booking. Never
    raises: a confirmation that does not go out must not cost the caller
    their turn, and the page on their screen already has the facts."""
    runtime = runtime or {}
    live = runtime.get("connectors") or []
    to = (booking.get("email") or "").strip()
    if not live or sender(live) is None:
        return
    try:
        art = await _artifact(runtime) if runtime.get("conversation_id") else None
    except Exception as e:
        logger.warning(f"booking confirmation skipped, no page yet: {e}")
        art = None
    payload = art.get("data") if art and isinstance(art.get("data"), dict) else {}
    if to:
        subject, html = booking_email(payload, booking)
        res = await _send_html(live, to, subject, html)
        logger.info(f"booking confirmation to {to}: ok={res['ok']} {res.get('error') or ''}")
    agency = agency_address(live, runtime)
    if agency and agency.lower() != to.lower():
        subject, html = agency_email(payload, booking,
                                     str(runtime.get("conversation_id") or ""))
        res = await _send_html(live, agency, subject, html)
        logger.info(f"agency copy to {agency}: ok={res['ok']} {res.get('error') or ''}")


TOOL_NAME = "email_itinerary"


def tool(live: list[dict], runtime: dict | None):
    """The native tool, built only when this call can actually send. A tool
    that exists and always fails is worse than none: the agent would keep
    offering it."""
    if not runtime or not runtime.get("conversation_id") or sender(live) is None:
        return None
    # This is added AFTER the connectors were deduped against the native
    # tool names, so it has to check for itself. Two tools with one name
    # raise "duplicate function name" inside Agent.__init__, which does not
    # degrade the call, it ends it.
    if any(TOOL_NAME in (c.get("tool_names") or []) for c in live):
        logger.warning(f"{TOOL_NAME} is already a connector tool on this call, not built")
        return None

    @function_tool
    async def email_itinerary(context: RunContext, to_address: str):
        """Email the caller their written itinerary, exactly as it appears
        on their screen, with links they can tap to put the trip in their
        own calendar. Call this the moment they give or confirm an address;
        you do not write the email and you must never paste the itinerary
        into a message yourself. Say it was sent only after this returns.

        Args:
            to_address: The email address the caller gave.
        """
        return await send(live, runtime, to_address)

    return email_itinerary
