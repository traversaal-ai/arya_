"""Function tools available to the agent, assembled from config toggles.

To add a tool: write a factory below that returns a @function_tool-decorated
async function, add a toggle key to the template schema (`tools.<name>`),
and register it in build_tools(). The docstring IS the contract — it is the
only interface the LLM sees.
"""

import contextlib
import datetime
import logging
import os

import asyncpg
import httpx
from livekit.agents import RunContext, function_tool

import config
import travel_tools

logger = logging.getLogger("tools")

# NOTE: there is deliberately no end_call tool — the model must never be
# able to end the session on a verbal cue. Calls end only via the UI's
# End-call button or the worker's inactivity watchdog (agent.py).


def choice_group(question: str, choices: list, multi: bool) -> dict:
    """The option group behind ask_choice: a "choose one of the following"
    card. Each choice is "label" or "label | one-line detail". Blank and
    duplicate labels are dropped; two to five must remain. `pick` is the
    label itself, so a tap sends the caller's answer in words, never
    "Option N". `always` pins the card under the very next reply, since the
    reply is told not to read the list aloud (see _names_an_option)."""
    items = []
    seen = set()
    for raw in choices or []:
        text = " ".join(str(raw or "").split()).strip()
        if not text:
            continue
        label, _, detail = (p.strip() for p in text.partition("|"))
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        items.append({
            "n": len(items) + 1,
            "label": label[:80],
            "detail": detail[:120] or None,
            "pick": label[:80],
        })
    if not 2 <= len(items) <= 5:
        raise ValueError("ask_choice needs two to five distinct choices")
    return {
        "kind": "choice",
        "title": " ".join(str(question or "").split()).strip()[:160],
        "multi": bool(multi),
        "always": True,
        "items": items,
    }


def choice_tool(runtime: dict | None):
    """The ask_choice tool. Every agent gets it (agent.py adds it after
    build_tools, so a config's own tool set stays exactly what build_tools
    says it is); the prompt rule in catalog.voice_rules decides when the
    model uses it."""
    @function_tool
    async def ask_choice(
        context: RunContext,
        question: str,
        choices: list[str],
        multi: bool = False,
    ):
        """Show the caller two to five answers to pick from, on screen, for a question you are asking in this same reply. Use it whenever the caller must choose between named alternatives you would otherwise list (appointment slots, which order or booking, which plan or service, a yes or no where each answer leads somewhere different). Never use it for open questions. Ask the question aloud in your own words in the same reply and do NOT read the choices out as a list; the caller taps one or says it, and either way the answer arrives as an ordinary message.

        Args:
            question: The question, as one short sentence.
            choices: Two to five short answers, a few words each. A choice may carry a one-line detail after " | ", for example "10:30 | 30 minutes with Dr Khan".
            multi: True only when the caller may pick several at once, for example "which days suit you".
        """
        try:
            group = choice_group(question, choices, multi)
        except ValueError as e:
            return f"Not shown: {e}. Ask the question in words instead."
        stash = (runtime or {}).get("options_stash")
        if isinstance(stash, list):
            stash.append(group)
        labels = ", ".join(i["label"] for i in group["items"])
        return (
            f"Shown on screen as buttons: {labels}. Ask the question in your"
            " own words now; do not read the list."
        )

    return ask_choice


def _weather_tool():
    @function_tool
    async def lookup_weather(context: RunContext, location: str):
        """Use this tool to look up current weather information in the given location.

        If the location is not supported by the weather service, the tool
        will indicate this. You must tell the user the location's weather is
        unavailable.

        Args:
            location: The location to look up weather information for (e.g. city name)
        """
        logger.info(f"looking up weather for {location}")
        # Deliberately fake and instant — this is the minimal tool example,
        # kept fast so tool latency doesn't muddy the voice pipeline.
        return "sunny with a temperature of 70 degrees."

    return lookup_weather


# ─── web search: provider adapters ──────────────────────────────────────────
# Each adapter normalizes its provider's response into result dicts of
# {title, snippet, source, link} — one shape feeds both the LLM's text
# answer and the UI's 🌐 source chips.


def _domain(url: str) -> str:
    try:
        return url.split("//", 1)[-1].split("/", 1)[0]
    except Exception:
        return url


async def _search_serpapi(client, endpoint: str, api_key: str, query: str) -> list[dict]:
    resp = await client.get(
        endpoint,
        params={"q": query, "engine": "google", "num": 5, "api_key": api_key},
    )
    resp.raise_for_status()
    data = resp.json()
    results = [
        {
            "title": r.get("title", ""),
            "snippet": (r.get("snippet") or "")[:240],
            "source": r.get("source") or r.get("displayed_link") or "",
            "link": r.get("link") or "",
        }
        for r in (data.get("organic_results") or [])[:5]
    ]
    if not results:
        answer = (data.get("answer_box") or {}).get("answer")
        if answer:
            results = [{"title": answer, "snippet": "", "source": "google", "link": ""}]
    return results


async def _search_ares(client, endpoint: str, api_key: str, query: str) -> list[dict]:
    # Ares (Traversaal): returns an LLM-ready answer plus the source URLs.
    resp = await client.post(
        endpoint,
        headers={"x-api-key": api_key, "content-type": "application/json"},
        json={"query": [query]},
    )
    if resp.status_code == 422:
        # Some deployments take a plain string instead of a list.
        resp = await client.post(
            endpoint,
            headers={"x-api-key": api_key, "content-type": "application/json"},
            json={"query": query},
        )
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    text = (data.get("response_text") or "").strip()
    urls = data.get("web_url") or []
    results = []
    for i, url in enumerate(urls[:5]):
        results.append(
            {
                "title": _domain(url),
                "snippet": text[:240] if i == 0 else "",
                "source": _domain(url),
                "link": url,
            }
        )
    if text and not results:
        results = [{"title": text[:80], "snippet": text[:240], "source": "ares", "link": ""}]
    # The full answer text rides in the first result's title line for the
    # LLM via the caller's formatting; keep it available.
    if results and text:
        results[0]["answer_text"] = text[:1500]
    return results


async def _search_tavily(client, endpoint: str, api_key: str, query: str) -> list[dict]:
    # Tavily: search built for LLMs — returns ranked results plus a
    # composed answer when include_answer is set.
    resp = await client.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        json={"query": query, "max_results": 5, "include_answer": True},
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for r in (data.get("results") or [])[:5]:
        if not isinstance(r, dict):
            continue
        link = r.get("url") or ""
        results.append(
            {
                "title": r.get("title") or _domain(link),
                "snippet": (r.get("content") or "")[:240],
                "source": _domain(link),
                "link": link,
            }
        )
    answer = (data.get("answer") or "").strip()
    if answer:
        if not results:
            results = [{"title": answer[:80], "snippet": answer[:240], "source": "tavily", "link": ""}]
        results[0]["answer_text"] = answer[:1500]
    return results


def _web_search_tool(search_cfg: dict, keys: dict, stash: list | None = None):
    provider = (search_cfg or {}).get("provider", "serpapi")
    # Endpoint + key name come from the catalog registry (DB-backed);
    # only the response-shape adapters below are code. build_tools() has
    # already gated on search_ready(), so the entry exists here.
    entry = config.search_provider(provider) or {}
    endpoint = entry.get("endpoint", "")
    api_key = keys.get(entry.get("key", provider), "")

    @function_tool
    async def web_search(context: RunContext, query: str):
        """Search the web for current information: news, prices, opening
        hours, events, travel conditions, or anything you don't know or that
        may have changed recently.

        Args:
            query: The search query, phrased like a web search
        """
        logger.info(f"web search ({provider}): {query}")
        async with httpx.AsyncClient(timeout=10.0) as client:
            if provider == "ares":
                results = await _search_ares(client, endpoint, api_key, query)
            elif provider == "tavily":
                results = await _search_tavily(client, endpoint, api_key, query)
            else:
                results = await _search_serpapi(client, endpoint, api_key, query)
        if not results:
            return "No results found for that search."
        if stash is not None:
            stash.extend(
                {k: r.get(k, "") for k in ("title", "snippet", "source", "link")}
                for r in results
            )
        # Ares-style providers return a composed answer — prefer it.
        answer = results[0].get("answer_text")
        if answer:
            sources = ", ".join(_domain(r["link"]) for r in results if r.get("link"))
            return answer + (f"\n(sources: {sources})" if sources else "")
        return "\n".join(
            f"{r['title']}: {r['snippet']} (source: {r['source']})" for r in results
        )

    return web_search


def _booking_tools():
    """Deterministic fake booking flow — demonstrates multi-tool round
    trips without a real backend. Replace the bodies with real API calls
    to make it live."""

    @function_tool
    async def check_availability(context: RunContext, date: str, party_size: int):
        """Check available time slots for a reservation on a given date.

        Args:
            date: The requested date, e.g. "2026-08-15" or "next Friday"
            party_size: Number of people in the party
        """
        logger.info(f"checking availability: {date}, party of {party_size}")
        if party_size > 8:
            return "Parties larger than eight require calling the venue directly."
        return (
            f"Available on {date}: five thirty PM, seven PM, and nine PM "
            f"for a party of {party_size}."
        )

    @function_tool
    async def create_booking(
        context: RunContext, name: str, date: str, time: str, party_size: int
    ):
        """Create a reservation. Only call this after the user has confirmed
        the name, date, time, and party size out loud.

        Args:
            name: Name for the reservation
            date: Reservation date
            time: Reservation time, e.g. "7 PM"
            party_size: Number of people
        """
        ref = f"AL-{abs(hash((name, date, time))) % 10000:04d}"
        logger.info(f"booking created: {ref} {name} {date} {time} x{party_size}")
        return (
            f"Booked: {date} at {time} for {party_size}, under {name}. "
            f"Confirmation reference {ref}."
        )

    return [check_availability, create_booking]


def _appointment_tools():
    """Deterministic fake appointment desk — book, reschedule, cancel — with
    per-call closure state so references stay consistent within one call.
    Same contract as the booking fakes: replace the bodies with real API
    calls (a calendar, Cal.com, your own backend) to make it live."""

    services = {
        "consultation": "30 minutes",
        "cleaning": "45 minutes",
        "follow-up": "15 minutes",
    }
    booked: dict[str, dict] = {}  # normalized ref -> appointment details

    def _norm_ref(ref: str) -> str:
        return "".join(c for c in str(ref).upper() if c.isalnum())

    def _say_ref(ref: str) -> str:
        # "AP4821" -> "AP-4-8-2-1" so TTS reads the digits one by one.
        digits = "-".join(ref[2:])
        return f"AP-{digits}"

    @function_tool
    async def list_services(context: RunContext):
        """List the services that can be booked, with their durations."""
        parts = [f"a {name}, {dur}" for name, dur in services.items()]
        return "We offer " + ", ".join(parts[:-1]) + f", and {parts[-1]}."

    @function_tool
    async def check_openings(context: RunContext, service: str, date: str):
        """Check open appointment slots for a service on a given date.

        Args:
            service: The service to book, e.g. "consultation"
            date: The requested date, e.g. "2026-08-25" or "next Tuesday"
        """
        logger.info(f"checking openings: {service} on {date}")
        return (
            f"On {date} for a {service}, I have 10 AM, 2 PM, or 4 PM open. "
            "Which works best?"
        )

    @function_tool
    async def book_appointment(
        context: RunContext, name: str, service: str, date: str, time: str
    ):
        """Book an appointment. Only call this after the caller has verbally
        confirmed the name, service, date, and time in one summary.

        Args:
            name: Name for the appointment
            service: The service being booked
            date: Appointment date
            time: Appointment time, e.g. "2 PM"
        """
        ref = f"AP{abs(hash((name, service, date, time))) % 10000:04d}"
        booked[ref] = {"name": name, "service": service, "date": date, "time": time}
        logger.info(f"appointment booked: {ref} {name} {service} {date} {time}")
        return (
            f"Booked: {service} on {date} at {time} for {name}. "
            f"Your reference is {_say_ref(ref)}."
        )

    @function_tool
    async def reschedule_appointment(
        context: RunContext, reference: str, new_date: str, new_time: str
    ):
        """Move an existing appointment to a new date and time. Ask for the
        reference code and read it back before calling this.

        Args:
            reference: The appointment reference code, e.g. "AP-4821"
            new_date: The new date
            new_time: The new time, e.g. "4 PM"
        """
        ref = _norm_ref(reference)
        appt = booked.get(ref)
        if not appt:
            return (
                "I can't find an appointment under that reference. "
                "Could you read the code again, digit by digit?"
            )
        old = f"{appt['date']} at {appt['time']}"
        appt["date"], appt["time"] = new_date, new_time
        logger.info(f"appointment rescheduled: {ref} -> {new_date} {new_time}")
        return (
            f"Done. Your {appt['service']} moved from {old} to {new_date} at "
            f"{new_time}. The reference stays {_say_ref(ref)}."
        )

    @function_tool
    async def cancel_appointment(context: RunContext, reference: str):
        """Cancel an existing appointment. Ask for the reference code, read it
        back, and get a clear yes before calling this.

        Args:
            reference: The appointment reference code, e.g. "AP-4821"
        """
        ref = _norm_ref(reference)
        appt = booked.pop(ref, None)
        if not appt:
            return (
                "I can't find an appointment under that reference. "
                "Could you read the code again, digit by digit?"
            )
        logger.info(f"appointment cancelled: {ref}")
        return (
            f"Cancelled: the {appt['service']} on {appt['date']} at "
            f"{appt['time']} for {appt['name']}. Anything else?"
        )

    return [
        list_services, check_openings, book_appointment,
        reschedule_appointment, cancel_appointment,
    ]


def _fake_order_tools():
    """Deterministic fake order desk: find orders by email, look one up,
    start a return. Per-call closure state, so a return started earlier in
    the call is remembered later in it.

    Identified by EMAIL, not by an order number, because every real system
    we connect to works that way: the Postgres connector's order view is
    keyed by email, Cal.com's find_my_bookings takes one, and Shopify Admin
    looks orders up by email or phone. The retail template's prompt
    therefore asks for an email, and a sample desk that only accepted an
    order number left a fresh agent asking for something its own tools could
    not use. An order number still works when a caller volunteers one.

    Same contract as the appointment fakes: replace the bodies with real API
    calls to make it live, or attach a connector and this desk switches
    itself off (see connectors.replaced_toolsets).

    The canned orders cover the three states a caller actually rings about,
    and an unknown order number falls back to a believable in-transit one so
    a demo never dead-ends on "no such order"."""

    # Generic appliance-store items: the template that uses these ships no
    # business identity of its own, so the caller's own business name (set
    # on Options) is the only brand spoken on the call.
    orders = {
        "ORD1023": {"status": "on its way", "items": "a Panasonic countertop microwave",
                    "when": "arriving Thursday", "returnable": False},
        "ORD2087": {"status": "delivered", "items": "a Bosch 24 inch dishwasher",
                    "when": "it arrived last Friday", "returnable": True},
        "ORD3145": {"status": "still being packed", "items": "a Samsung front load washer",
                    "when": "shipping within two days", "returnable": False},
        # A SECOND washer, on purpose. Two items sharing a word is the case
        # that caught us on a live call: asked about "the top load washer",
        # the agent answered about the front load one. The demo data has to
        # contain the trap or the asking-which-one path is never exercised.
        "ORD4210": {"status": "delivered", "items": "a Whirlpool top load washer",
                    "when": "it arrived on Tuesday", "returnable": True},
    }
    fallback = {"status": "on its way", "items": "one appliance",
                "when": "arriving within three days", "returnable": False}
    returns: dict[str, dict] = {}  # normalized order -> return details

    def _norm(value: str) -> str:
        return "".join(c for c in str(value).upper() if c.isalnum())

    def _looks_like_email(value: str) -> bool:
        v = str(value).strip()
        return "@" in v and "." in v.rsplit("@", 1)[-1]

    def _match(number: str, item: str):
        """The order a caller meant, by number or by product word.

        Returns (key, order) or (None, None). A product word is what people
        actually say ("the dishwasher"), and it is matched on distinctive
        words rather than the whole phrase, since a caller says "the Bosch
        one" and not "a Bosch 24 inch dishwasher".
        """
        num = _norm(number)
        if num and num in orders:
            return num, orders[num]
        said = {w for w in str(item).lower().split() if len(w) > 3}
        if said:
            hits = [
                (k, o) for k, o in orders.items()
                if said & {w.strip(",.") for w in o["items"].lower().split()}
            ]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                return "AMBIGUOUS", [o["items"] for _, o in hits]
        if num:
            return num, fallback
        return None, None

    @function_tool
    async def find_orders(context: RunContext, email: str = ""):
        """Identify the caller and list their orders so one can be discussed
        or returned. Call this first, before asking who they are: if it
        needs an email it will say so, and you ask and call again with it.

        Args:
            email: The email the orders were placed with, once you have it.
        """
        if not email:
            return ("Nobody is identified yet. Ask for the email address the"
                    " order was placed with, read it back, and call again.")
        if not _looks_like_email(email):
            return "That does not look like an email address. Could I take it again?"
        logger.info(f"order list for {email}: {len(orders)} order(s)")
        listed = "; ".join(
            f"{o['items']}, {o['status']}" for o in orders.values()
        )
        return (
            f"There are {len(orders)} orders under that email: {listed}."
            " Name two or three by product and ask which one they mean;"
            " never read the whole list aloud."
        )

    @function_tool
    async def lookup_order(context: RunContext, order_number: str = "",
                           item: str = ""):
        """Look up one order's status, items, and expected delivery.

        Pass whichever the caller gave you. Prefer `item`, the product they
        named, since callers rarely have an order number to hand.

        Args:
            order_number: Optional. An order number, e.g. "ORD-1023".
            item: Optional. The product the caller named, e.g. "the dishwasher".
        """
        key, order = _match(order_number, item)
        if key == "AMBIGUOUS":
            return (
                "Two orders could match that: "
                + ", and ".join(order)
                + ". Ask which one they mean."
            )
        if order is None:
            return (
                "Ask which product they mean, or for the order number if"
                " they have it to hand."
            )
        logger.info(f"order lookup: {key} -> {order['status']}")
        return (
            f"That order contains {order['items']}. "
            f"It's {order['status']}, {order['when']}."
        )

    @function_tool
    async def start_return(context: RunContext, reason: str,
                           order_number: str = "", item: str = ""):
        """Start a return for a delivered order. Only call this after
        confirming the item and the reason out loud and getting a clear yes.

        Args:
            reason: Why they're returning it, e.g. "arrived damaged".
            order_number: Optional. The order number, if they gave one.
            item: Optional. The product they named, e.g. "the dishwasher".
        """
        key, order = _match(order_number, item)
        if key == "AMBIGUOUS":
            return (
                "Two orders could match that: "
                + ", and ".join(order)
                + ". Ask which one they mean before starting a return."
            )
        if order is None or order is fallback:
            return (
                "I can't tell which order that is. Ask which product they"
                " mean, or for the order number."
            )
        if key in returns:
            return (
                f"A return is already on file for {order['items']}, so it"
                " cannot be returned twice. Confirm that to the caller."
            )
        if not order["returnable"]:
            return (
                f"That order is still {order['status']}, so I can't start a "
                "return yet. Once it arrives, call back and I'll set it up."
            )
        returns[key] = {"reason": reason}
        logger.info(f"return started: {key} ({reason})")
        return (
            f"Return started for {order['items']}, reason: {reason}."
            " Tell the caller it is done and that the confirmation email is"
            " on its way; never read a reference aloud."
        )

    return [find_orders, lookup_order, start_return]


# ─── the shop database behind the order desk ─────────────────────────────────
# One setting, ARYA_SHOP_DSN, turns the fake desk above into a real one over
# a small Postgres shop (see demo/shop). Unset, nothing here runs.
#
# Why a second pool rather than db.get_pool: that one is a singleton for
# Arya's own database and bootstraps Arya's schema on first use. The shop
# is somebody else's data and must not be touched by that.
#
# The role the DSN logs in as can SELECT everything and INSERT into returns
# only (demo/shop/seed.sql). So the one write below is the only write the
# database will accept from this process, whatever the model asks for.

SHOP_DSN_ENV = "ARYA_SHOP_DSN"
# One pool per database: the desk's database comes from the agent's attached
# connector (tool_runtime["shop_dsn"]), so two agents may point at two shops.
_shop: dict = {"pools": {}}


def shop_dsn() -> str:
    """The worker-wide fallback. The attached connector is the normal route
    (connectors.served_toolsets); this covers tests and single-agent installs."""
    return (os.environ.get(SHOP_DSN_ENV) or "").strip()


async def _shop_pool_raw(dsn: str):
    pool = _shop["pools"].get(dsn)
    if pool is None:
        pool = _shop["pools"][dsn] = await asyncpg.create_pool(
            dsn, min_size=0, max_size=3, command_timeout=5,
            # The worker process lives for days and the desk is used in
            # bursts; a connection left idle across Docker Desktop's
            # host.docker.internal proxy, or a pooler, is dropped without
            # a word. Recycle idle ones early rather than hand one out dead.
            max_inactive_connection_lifetime=60.0,
            server_settings={
                "application_name": "arya-order-desk",
                # A runaway query must never hold a turn hostage.
                "statement_timeout": "3000",
            },
        )
    return pool


async def _drop_pool(dsn: str) -> None:
    pool = _shop["pools"].pop(dsn, None)
    if pool is not None:
        # Already broken; there is nothing worth keeping from it.
        with contextlib.suppress(Exception):
            pool.terminate()


class _ShopDB:
    """The pool behind a retry: a query that dies with a dropped connection
    ("unexpected connection_lost() call", seen live after hours idle) is
    run once more on a fresh pool before the desk gives up. SQL errors
    (a unique violation on a second return) are answers, never retried."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    async def _run(self, how: str, sql: str, *args):
        for attempt in (1, 2):
            pool = await _shop_pool_raw(self.dsn)
            try:
                return await getattr(pool, how)(sql, *args)
            except asyncpg.PostgresError:
                raise
            except Exception as e:
                await _drop_pool(self.dsn)
                if attempt == 2:
                    raise
                logger.info(f"order desk: reconnecting after {e!r}")

    async def fetch(self, sql: str, *args):
        return await self._run("fetch", sql, *args)

    async def fetchrow(self, sql: str, *args):
        return await self._run("fetchrow", sql, *args)

    async def fetchval(self, sql: str, *args):
        return await self._run("fetchval", sql, *args)


async def _shop_pool(dsn: str) -> _ShopDB:
    return _ShopDB(dsn)


async def close_shop_pools() -> None:
    pools = list(_shop["pools"].values())
    _shop["pools"].clear()
    for pool in pools:
        await pool.close()


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _money(cents) -> str:
    return f"${(cents or 0) / 100:,.2f}"


def _when(ts) -> str:
    return ts.date().isoformat() if ts else "no date yet"


SHOP_DOWN_TEXT = (
    "The order system is not responding right now. Apologise, say you"
    " cannot check orders at this moment, and offer to try again shortly."
)


def _fresh_pictures(pictured: set, orders: list[dict], *,
                    force: bool = False) -> list[tuple[str | None, str | None]]:
    """Which orders get their picture this time: (image, fallback) per
    order, or (None, None) when it was already shown in this conversation.
    Marks what it hands out. `force` is an explicit "show me a picture",
    which always wins. Pictures answer a question; they are not decoration,
    so the same product is never pictured twice unasked."""
    out: list[tuple[str | None, str | None]] = []
    for o in orders:
        image = o.get("image") or None
        key = o.get("id")
        if not image or (key in pictured and not force):
            out.append((None, None))
            continue
        pictured.add(key)
        out.append((image, o.get("image_fallback") or None))
    return out


def _shop_order_tools(runtime: dict | None, dsn: str = ""):
    """The real desk: the same three tools the fake has, plus return_status,
    over the shop database at `dsn` (the attached connector's string, or the
    ARYA_SHOP_DSN fallback).

    Identity comes from the channel, never from the model. On WhatsApp the
    runtime carries the sender's number, so find_orders needs no argument
    and the agent never asks who is messaging. Elsewhere it asks for an
    email as today. Every read after that is filtered by the resolved
    customer id in SQL, so a session cannot reach another customer's rows
    by naming them, in any phrasing.

    Answers keep the fake desk's habit of telling the model what to say
    next: that is what made it behave on live calls.
    """
    runtime = runtime or {}
    dsn = dsn or shop_dsn()
    channel = str(runtime.get("channel") or "")
    phone = _digits(runtime.get("external_user")) if channel == "whatsapp" else ""
    # `listed`: the greeting's tappable order list has gone out once. Seen
    # live: a model that re-ran find_orders on a later turn sent the whole
    # list again under an answer about one order. The list is a greeting
    # thing, so it goes out once per conversation whatever the model does.
    state: dict = {"customer": None, "pictured": set(), "listed": False}

    def _looks_like_email(value: str) -> bool:
        v = str(value or "").strip()
        return "@" in v and "." in v.rsplit("@", 1)[-1]

    async def _customer_orders(cid: int) -> list[dict]:
        pool = await _shop_pool(dsn)
        rows = await pool.fetch(
            "SELECT o.id, o.order_ref, o.status, o.placed_at, o.delivered_at,"
            " o.total_cents,"
            " string_agg(p.name, ', ' ORDER BY p.name) AS items,"
            " min(p.return_window_days) AS window_days,"
            # Product pictures, for the WhatsApp photo bubbles and the widget
            # cards. Read through to_jsonb so a shop seeded before these
            # columns existed yields NULL here instead of an error.
            " (array_agg(to_jsonb(p)->>'image_url' ORDER BY p.name))[1] AS image,"
            " (array_agg(to_jsonb(p)->>'fallback_image_url' ORDER BY p.name))[1]"
            "   AS image_fallback,"
            " EXISTS (SELECT 1 FROM returns r WHERE r.order_id = o.id"
            "         AND r.status IN ('requested','approved')) AS return_open"
            " FROM orders o"
            " JOIN order_items oi ON oi.order_id = o.id"
            " JOIN products p ON p.id = oi.product_id"
            " WHERE o.customer_id = $1"
            " GROUP BY o.id ORDER BY o.placed_at DESC",
            cid,
        )
        return [dict(r) for r in rows]

    def _match(orders: list[dict], number: str, item: str):
        """The order the user meant, by reference or by product word.
        (key, order), (None, None), or ("AMBIGUOUS", [names])."""
        num = _digits(number)
        if num:
            for o in orders:
                if _digits(o["order_ref"]) == num:
                    return o["order_ref"], o
        # Every word of three letters or more except filler, so "top" in
        # "the top load washer" counts. Score each order by how many of
        # those words its items contain; a unique best wins, a tie is a
        # genuine question, no overlap at all is not this customer's order.
        filler = {"the", "one", "that", "this", "and", "for", "our", "with"}
        said = {
            w.strip(",.").lower() for w in str(item or "").split()
            if len(w.strip(",.")) >= 3
        } - filler
        if said:
            scored = []
            for o in orders:
                words = {w.strip(",.").lower() for w in o["items"].split()}
                overlap = len(said & words)
                if overlap:
                    scored.append((overlap, o))
            if scored:
                best = max(n for n, _ in scored)
                hits = [o for n, o in scored if n == best]
                if len(hits) == 1:
                    return hits[0]["order_ref"], hits[0]
                return "AMBIGUOUS", hits
        return None, None

    def _short(name: str, limit: int = 24) -> str:
        """A product name cut at a word so it fits a WhatsApp list row
        title, and still matches uniquely when tapped and sent back."""
        if len(name) <= limit:
            return name
        return name[:limit + 1].rsplit(" ", 1)[0] or name[:limit]

    def _offer(title: str, hits: list[dict], *, photos: bool,
               photo_only: bool = False, force: bool = False) -> bool:
        """Put these orders in front of the user as a choice, on top of
        whatever the model says.

        On WhatsApp the bridge turns the stashed group into a tappable list
        under the reply; in the widget it renders as option cards. A tap
        sends the row's title back as an ordinary message, which _match
        resolves on its own since each title is one product's words. The
        second line is the amount paid only: WhatsApp echoes both lines in
        the user's bubble when they tap, and a status there read oddly.
        `always` pins the group to the very next reply even when that
        reply names none of the products (a greeting that says "four
        orders" and stops).

        Pictures (`photos`) go only where they answer something: the
        two-way choice, and a specific order the first time it comes up
        (_fresh_pictures). The overview list never carries them, four
        photo bubbles for a list of names is noise. `photo_only` is a
        single order: its picture and no list, since one row is not a
        choice. `force` is an explicit "show me a picture".
        """
        stash = runtime.get("options_stash")
        if not isinstance(stash, list):
            return False
        pics = (_fresh_pictures(state["pictured"], hits, force=force)
                if photos else [(None, None)] * len(hits))
        if photo_only and not any(img for img, _ in pics):
            return False  # nothing new to show, and no list to send
        group = {
            "kind": "orders",
            "title": title,
            "always": True,
            "items": [
                {
                    "n": i,
                    "label": _short(o["items"]),
                    "detail": _money(o["total_cents"]),
                    "pick": _short(o["items"]),
                    "image": img,
                    "image_fallback": fallback if img else None,
                }
                for i, (o, (img, fallback)) in enumerate(zip(hits, pics, strict=True), 1)
            ],
        }
        if photo_only:
            group["photo_only"] = True
        stash.append(group)
        return True

    def _ambiguous(hits: list[dict], then: str) -> str:
        # A choice shows every candidate, seen before or not: the point of
        # the pictures here is comparing them side by side.
        _offer("Which one do you mean?", hits, photos=True, force=True)
        names = ", and ".join(o["items"] for o in hits)
        return f"Two orders could match that: {names}. {then}"

    def _need_customer() -> str | None:
        if state["customer"] is None:
            return ("You have not identified the customer yet. Call"
                    " find_orders first.")
        return None

    @function_tool
    async def find_orders(context: RunContext, email: str = ""):
        """Identify the customer and list their orders so one can be
        discussed or returned. Call this first, with no arguments, before
        asking who they are: on a messaging channel the customer is already
        known by their number and you can greet them by name. If it says
        it needs an email, ask for it and call again with it.

        Args:
            email: The email the orders were placed with, once you have it.
        """
        try:
            pool = await _shop_pool(dsn)
            row = None
            if phone:
                row = await pool.fetchrow(
                    "SELECT id, first_name FROM customers WHERE phone = $1", phone
                )
            if row is None and email:
                if not _looks_like_email(email):
                    return ("That does not look like an email address."
                            " Could I take it again?")
                row = await pool.fetchrow(
                    "SELECT id, first_name FROM customers"
                    " WHERE lower(email) = lower($1)", email.strip(),
                )
            if row is None:
                if not email:
                    who = ("This number is not on an account" if phone
                           else "Nobody is identified yet")
                    return (f"{who}. Ask for the email address the order"
                            " was placed with, read it back, and call again.")
                return ("No account matches that. Ask whether they might"
                        " have ordered under a different email.")
            state["customer"] = {"id": row["id"], "first_name": row["first_name"]}
            orders = await _customer_orders(row["id"])
        except Exception as e:
            logger.warning(f"order desk: find_orders failed: {e}")
            return SHOP_DOWN_TEXT
        name = row["first_name"]
        if not orders:
            return f"The customer is {name}. They have no orders on file."
        listed = "; ".join(f"{o['items']}, {o['status']}" for o in orders)
        logger.info(f"order desk: {name} identified, {len(orders)} order(s)")
        if state["listed"]:
            return (
                f"The customer is {name}, already greeted. Their orders:"
                f" {listed}. Do not greet again and do not list these;"
                " answer the question, using lookup_order for any one order."
            )
        if len(orders) > 1 and _offer("Your orders", orders, photos=False):
            state["listed"] = True
            return (
                f"The customer is {name}. There are {len(orders)} orders:"
                f" {listed}. Greet them by first name, say how many orders"
                " they have, and ask which one they mean. A tappable list"
                " of the orders is shown under your reply, so do not list"
                " them yourself. For any question about one of these"
                " orders, call lookup_order; never answer from this list,"
                " which is only for the greeting."
            )
        return (
            f"The customer is {name}. There are {len(orders)} orders:"
            f" {listed}. Greet them by first name. If there are several,"
            " name two or three by product and ask which one they mean;"
            " never read the whole list out."
        )

    @function_tool
    async def lookup_order(context: RunContext, order_number: str = "",
                           item: str = ""):
        """Look up one of this customer's orders: items, status, dates and
        what they paid. Call this for EVERY question about a particular
        order, even one you already listed: it is also what puts the
        pictures and the tappable choice on the customer's screen, which
        never happens if you answer from memory. Prefer `item`, the
        product they named.

        Args:
            order_number: Optional. An order reference, e.g. "ORD-1023".
            item: Optional. The product they named, e.g. "the dishwasher".
        """
        if (missing := _need_customer()):
            return missing
        try:
            orders = await _customer_orders(state["customer"]["id"])
        except Exception as e:
            logger.warning(f"order desk: lookup failed: {e}")
            return SHOP_DOWN_TEXT
        key, order = _match(orders, order_number, item)
        if key == "AMBIGUOUS":
            return _ambiguous(order, "Ask which one they mean.")
        if order is None:
            return ("I cannot see that order on this account. Name the"
                    " orders you can see and ask which they mean.")
        text = (
            f"Order {order['order_ref']} contains {order['items']}."
            f" It is {order['status']}, placed {_when(order['placed_at'])}"
        )
        if order["delivered_at"]:
            text += f", delivered {_when(order['delivered_at'])}"
        text += f". They paid {_money(order['total_cents'])}."
        if order["return_open"]:
            text += " A return is already open on it."
        # The product's picture rides under the answer the first time this
        # order comes up in the conversation, when the shop has one.
        _offer(order["items"], [order], photos=True, photo_only=True)
        return text

    @function_tool
    async def show_picture(context: RunContext, order_number: str = "",
                           item: str = ""):
        """Send the customer a picture of a product on one of their orders,
        when they ask to see it. Prefer `item`, the product they named.

        Args:
            order_number: Optional. An order reference, e.g. "ORD-1023".
            item: Optional. The product they named, e.g. "the dishwasher".
        """
        if (missing := _need_customer()):
            return missing
        try:
            orders = await _customer_orders(state["customer"]["id"])
        except Exception as e:
            logger.warning(f"order desk: show_picture failed: {e}")
            return SHOP_DOWN_TEXT
        key, order = _match(orders, order_number, item)
        if key == "AMBIGUOUS":
            return _ambiguous(order, "Ask which one they mean.")
        if order is None:
            return ("I cannot see that order on this account. Name the"
                    " orders you can see and ask which they mean.")
        if not order.get("image"):
            return (f"There is no picture on file for {order['items']}."
                    " Say so in one sentence.")
        _offer(order["items"], [order], photos=True, photo_only=True, force=True)
        return (f"The picture of {order['items']} is being sent with your"
                " reply. Say it is on its way, in one short sentence.")

    @function_tool
    async def start_return(context: RunContext, reason: str,
                           order_number: str = "", item: str = "",
                           note: str = ""):
        """Start a return for one of this customer's delivered orders. Only
        call this after confirming the item and the reason and getting a
        clear yes.

        Args:
            reason: Why they are returning it, e.g. "arrived damaged".
            order_number: Optional. The order reference, if they gave one.
            item: Optional. The product they named.
            note: Optional. If they sent a photo of the problem, one plain
                sentence describing what you saw in it.
        """
        if (missing := _need_customer()):
            return missing
        cid = state["customer"]["id"]
        try:
            orders = await _customer_orders(cid)
            key, order = _match(orders, order_number, item)
            if key == "AMBIGUOUS":
                return _ambiguous(
                    order, "Ask which one they mean before starting a return.")
            if order is None:
                return ("I cannot tell which order that is. Ask which product"
                        " they mean, or for the order reference.")
            if order["return_open"]:
                return (f"A return is already open for {order['items']}, so"
                        " it cannot be returned twice. Say so, and offer to"
                        " check its status.")
            if order["status"] != "delivered" or not order["delivered_at"]:
                return (f"That order is still {order['status']}, so a return"
                        " cannot start yet. Once it has arrived they can ask"
                        " again.")
            pool = await _shop_pool(dsn)
            days_since = await pool.fetchval(
                "SELECT extract(day FROM now() - $1::timestamptz)::int",
                order["delivered_at"],
            )
            if days_since > (order["window_days"] or 30):
                return (f"That order was delivered {days_since} days ago and"
                        f" the return window is {order['window_days']} days,"
                        " so it is too late for a standard return. Say so"
                        " plainly and suggest the store team.")
            ref = await pool.fetchval(
                "INSERT INTO returns (order_id, customer_id, reason, note)"
                " VALUES ($1, $2, $3, NULLIF($4, '')) RETURNING return_ref",
                order["id"], cid, reason.strip(), (note or "").strip(),
            )
        except asyncpg.UniqueViolationError:
            return ("A return is already open for that order, so it cannot"
                    " be returned twice.")
        except Exception as e:
            logger.warning(f"order desk: start_return failed: {e}")
            return SHOP_DOWN_TEXT
        logger.info(f"order desk: return {ref} started for order {order['order_ref']}")
        return (
            f"Return {ref} started for {order['items']}, reason: {reason}."
            " Tell them it is done and that the return reference is"
            f" {ref}; a confirmation will follow."
        )

    @function_tool
    async def return_status(context: RunContext):
        """The status of this customer's returns. Call when they ask what
        happened to a return."""
        if (missing := _need_customer()):
            return missing
        try:
            pool = await _shop_pool(dsn)
            rows = await pool.fetch(
                "SELECT r.return_ref, r.status, r.reason, r.created_at,"
                " string_agg(p.name, ', ') AS items"
                " FROM returns r JOIN orders o ON o.id = r.order_id"
                " JOIN order_items oi ON oi.order_id = o.id"
                " JOIN products p ON p.id = oi.product_id"
                " WHERE r.customer_id = $1"
                " GROUP BY r.id ORDER BY r.created_at DESC",
                state["customer"]["id"],
            )
        except Exception as e:
            logger.warning(f"order desk: return_status failed: {e}")
            return SHOP_DOWN_TEXT
        if not rows:
            return "There are no returns on this account."
        listed = "; ".join(
            f"{r['return_ref']} for {r['items']}: {r['status']}"
            f" (opened {_when(r['created_at'])}, reason: {r['reason']})"
            for r in rows
        )
        return f"Returns on this account: {listed}."

    return [find_orders, lookup_order, start_return, return_status, show_picture]


def _order_tools(runtime: dict | None = None):
    """The fake desk, or the real one over the database the agent's attached
    connector supplied (runtime["shop_dsn"], see connectors.served_toolsets),
    falling back to ARYA_SHOP_DSN."""
    dsn = str((runtime or {}).get("shop_dsn") or "").strip() or shop_dsn()
    if dsn:
        return _shop_order_tools(runtime, dsn)
    return _fake_order_tools()


def _date_tool():
    @function_tool
    async def get_current_date(context: RunContext):
        """Get today's date. Call this before converting any relative date the
        caller says, like "next Friday" or "in two weeks", into YYYY-MM-DD
        for any search or booking tool."""
        today = datetime.date.today()
        return f"Today is {today.strftime('%A')}, {today.isoformat()}."

    return get_current_date


def build_tools(cfg: dict, keys: dict, web_stash: list | None = None,
                options_stash: list | None = None,
                runtime: dict | None = None,
                skip: frozenset[str] = frozenset()) -> list:
    """Assemble the tool list from the agent config's `tools` toggles.
    web_stash (optional) collects each search's sources for the UI chips;
    options_stash (optional) collects structured option groups for the UI's
    option cards; runtime (optional) carries the call's conversation/room
    handles so tools can save + announce artifacts (see travel_tools).

    `skip` names toggles a real connector has taken over
    (connectors.replaced_toolsets): the sample desks below invent plausible
    data, so leaving one enabled next to a live system lets the model answer
    from the fake one. The toggle stays true in the config (turn the
    connector off and the demo desk is back), it simply does not build."""
    toggles = cfg.get("tools", {})
    out = []

    def on(name: str) -> bool:
        return bool(toggles.get(name)) and name not in skip

    if on("weather"):
        out.append(_weather_tool())
    if on("web_search") and config.search_ready(cfg, keys)[0]:
        out.append(_web_search_tool(cfg.get("search", {}), keys, web_stash))
    if on("booking"):
        out.extend(_booking_tools())
    # Travel is three sample desks behind three toggles: flights (`travel`,
    # what Duffel replaces), hotels (`hotels`, SerpAPI rates, replaced only
    # once a connector's hotel tools are ticked because Duffel Stays is off
    # on new accounts) and the things-to-do planner (`places`). Agents saved
    # before the split have only `travel` and keep behaving as before: the
    # other two follow it here as they do in config.normalize_tools.
    hotels_on = bool(toggles.get("hotels", toggles.get("travel")))
    places_on = bool(toggles.get("places", toggles.get("travel")))
    if toggles.get("travel") or hotels_on or places_on:
        # Every date a caller says is relative ("next Friday"). The helper
        # stays even when connectors have replaced every desk, because the
        # connector tools take ISO dates too.
        out.append(_date_tool())
    parts = tuple(p for p, flag in (
        ("flights", on("travel")),
        ("hotels", hotels_on and "hotels" not in skip),
        ("places", places_on and "places" not in skip),
    ) if flag)
    if parts:
        out.extend(travel_tools.build_travel_tools(
            cfg, keys, options_stash, runtime, parts=parts))
    if on("appointments"):
        out.extend(_appointment_tools())
    if on("orders"):
        out.extend(_order_tools(runtime))
    return out
