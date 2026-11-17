"""Meta WhatsApp Cloud API: signature checking, payload parsing, sending.

Transport and parsing only: no FastAPI, no database, no LiveKit. Every
orchestration decision lives in whatsapp_bridge.py, which keeps this module
unit-testable against recorded payloads with no network at all.

Two halves:

* `verify_signature` / `normalize` (inbound). Meta POSTs a signed JSON
  envelope and retries it for up to 7 days unless it gets a 200, so a
  caller MUST verify the signature and dedupe on `wamid` before acting.
  `normalize` is total: anything it cannot classify comes back as an
  `unsupported` message rather than an exception, because a webhook that
  raises is a webhook Meta retries forever.
* `MetaClient` (outbound). One per channel, holding that number's id and
  token. Graph version is per channel, not global: Meta keeps roughly two
  years of versions alive and a customer's app may sit on an older one.

Media note: the download URL Meta hands back is valid for five minutes and
needs the Bearer header, so it can never be given to an LLM provider or
embedded in a page. Bytes are always fetched here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("arya.meta_wa")

GRAPH = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v23.0"

# WhatsApp's own caps, applied here so a long model answer degrades into
# several readable messages instead of one rejected API call.
TEXT_LIMIT = 4096
LIST_ROW_LIMIT = 10
LIST_TITLE_LIMIT = 24
LIST_DESC_LIMIT = 72
LIST_BUTTON_LIMIT = 20
LIST_ID_LIMIT = 200

# Inbound kinds the bridge knows how to turn into a turn. Everything else
# (video, sticker, location, contacts, order, reaction, system) normalizes
# to "unsupported" and gets a polite decline.
TEXT_KINDS = ("text", "interactive", "button")
MEDIA_KINDS = ("image", "audio", "document")


class MetaError(RuntimeError):
    """A Graph API call failed. `retryable` drives the bridge's backoff."""

    def __init__(self, message: str, *, code: int | None = None,
                 status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass
class InboundMessage:
    """One customer message, flattened out of Meta's nested envelope."""

    wamid: str
    phone_number_id: str
    wa_user: str
    kind: str
    name: str | None = None
    text: str = ""
    media_id: str | None = None
    mime: str | None = None
    sha256: str | None = None
    voice: bool = False
    filename: str | None = None
    context_wamid: str | None = None
    timestamp: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def is_media(self) -> bool:
        return self.kind in MEDIA_KINDS


@dataclass
class StatusUpdate:
    """A delivery receipt (sent/delivered/read/failed) for one of our sends."""

    wamid: str
    phone_number_id: str
    status: str
    recipient: str = ""
    error: str | None = None
    timestamp: int = 0


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    """Constant-time check of Meta's X-Hub-Signature-256 over the RAW body.

    The body must be the exact bytes received: re-serializing the parsed
    JSON changes whitespace and key order and the digest no longer matches.
    A channel with no app secret configured cannot be verified, and this
    returns False rather than waving the request through.
    """
    if not app_secret or not header:
        return False
    header = header.strip()
    if not header.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header[7:], expected)


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_message(msg: dict, meta: dict, names: dict) -> InboundMessage:
    """One `messages[]` entry into an InboundMessage. Never raises."""
    kind = msg.get("type") or "unsupported"
    wa_user = str(msg.get("from") or "")
    out = InboundMessage(
        wamid=str(msg.get("id") or ""),
        phone_number_id=str(meta.get("phone_number_id") or ""),
        wa_user=wa_user,
        kind=kind,
        name=names.get(wa_user),
        context_wamid=(msg.get("context") or {}).get("id"),
        timestamp=_int(msg.get("timestamp")),
        raw=msg,
    )
    if kind == "text":
        out.text = (msg.get("text") or {}).get("body") or ""
    elif kind in MEDIA_KINDS:
        body = msg.get(kind) or {}
        out.media_id = body.get("id")
        out.mime = body.get("mime_type")
        out.sha256 = body.get("sha256")
        out.text = body.get("caption") or ""
        out.filename = body.get("filename")
        # Meta sets voice=true only for a recorded voice note; an attached
        # audio file arrives as the same type with voice absent or false.
        out.voice = bool(body.get("voice"))
    elif kind == "interactive":
        inter = msg.get("interactive") or {}
        reply = inter.get("list_reply") or inter.get("button_reply") or {}
        # The title is what the customer saw and tapped, so it is what the
        # agent should receive; the id carries our own routing payload.
        out.text = reply.get("title") or reply.get("id") or ""
        out.filename = reply.get("id")
    elif kind == "button":
        # Template quick reply: the button's visible text is the answer.
        out.text = (msg.get("button") or {}).get("text") or ""
    else:
        out.kind = "unsupported"
    return out


def normalize(payload: dict) -> tuple[list[InboundMessage], list[StatusUpdate]]:
    """Meta's webhook envelope into flat lists of messages and statuses.

    Total by construction: a shape we do not recognise yields empty lists,
    never an exception, so the webhook can always answer 200 and stop the
    retry storm. One POST can legitimately carry several entries, changes
    and messages, so everything is flattened.
    """
    messages: list[InboundMessage] = []
    statuses: list[StatusUpdate] = []
    if not isinstance(payload, dict):
        return messages, statuses
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            meta = value.get("metadata") or {}
            phone_id = str(meta.get("phone_number_id") or "")
            names = {
                str(c.get("wa_id")): (c.get("profile") or {}).get("name")
                for c in value.get("contacts") or []
                if c.get("wa_id")
            }
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict) or not msg.get("id"):
                    continue
                messages.append(_parse_message(msg, meta, names))
            for st in value.get("statuses") or []:
                if not isinstance(st, dict) or not st.get("id"):
                    continue
                errs = st.get("errors") or []
                detail = None
                if errs:
                    first = errs[0] or {}
                    detail = first.get("title") or first.get("message")
                statuses.append(StatusUpdate(
                    wamid=str(st.get("id")),
                    phone_number_id=phone_id,
                    status=str(st.get("status") or ""),
                    recipient=str(st.get("recipient_id") or ""),
                    error=detail,
                    timestamp=_int(st.get("timestamp")),
                ))
    return messages, statuses


def split_text(body: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split an over-long answer on paragraph, then line, then word
    boundaries. WhatsApp rejects anything past 4096 characters, and a hard
    slice mid-word reads like a bug to the customer."""
    body = (body or "").strip()
    if len(body) <= limit:
        return [body] if body else []
    out: list[str] = []
    rest = body
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return [chunk for chunk in out if chunk]


def _clip(value: str, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


class MetaClient:
    """Graph API calls for one WhatsApp number.

    Holds no state beyond the credentials; the caller owns retries across
    process restarts. 429 and 5xx are retried here a couple of times with
    backoff because a transient Graph blip must not lose a customer's
    answer, while 4xx (bad token, number not in the window) is raised
    straight away so the bridge can record it.
    """

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.phone_number_id = str(phone_number_id or "")
        self.token = access_token or ""
        version = (api_version or DEFAULT_API_VERSION).strip()
        self.api_version = version if version.startswith("v") else f"v{version}"
        self._timeout = timeout
        self._client = client
        self._owned = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owned:
            await self._client.aclose()
            self._client = None

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _url(self, path: str) -> str:
        return f"{GRAPH}/{self.api_version}/{path.lstrip('/')}"

    async def _request(self, method: str, path: str, *, attempts: int = 3,
                       **kwargs) -> httpx.Response:
        http = await self._http()
        url = path if path.startswith("http") else self._url(path)
        headers = {**self._auth, **(kwargs.pop("headers", None) or {})}
        last: MetaError | None = None
        for attempt in range(attempts):
            try:
                resp = await http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as e:
                last = MetaError(f"network error talking to Meta: {e}",
                                 retryable=True)
            else:
                if resp.status_code < 400:
                    return resp
                detail, code = _error_detail(resp)
                retryable = resp.status_code == 429 or resp.status_code >= 500
                last = MetaError(detail, code=code, status=resp.status_code,
                                 retryable=retryable)
                if not retryable:
                    raise last
            if attempt < attempts - 1:
                await asyncio.sleep(1.5 * (2 ** attempt))
        raise last or MetaError("request failed")

    # ── sending ──────────────────────────────────────────────────────────

    async def _send(self, payload: dict) -> str:
        payload = {"messaging_product": "whatsapp",
                   "recipient_type": "individual", **payload}
        resp = await self._request(
            "POST", f"{self.phone_number_id}/messages", json=payload
        )
        body = resp.json()
        msgs = body.get("messages") or [{}]
        return str(msgs[0].get("id") or "")

    async def send_text(self, to: str, body: str, *,
                        context_wamid: str | None = None,
                        preview_url: bool = True) -> str:
        payload: dict = {
            "to": to,
            "type": "text",
            "text": {"preview_url": preview_url, "body": body},
        }
        if context_wamid:
            payload["context"] = {"message_id": context_wamid}
        return await self._send(payload)

    async def send_audio(self, to: str, media_id: str, *,
                         voice: bool = True) -> str:
        """`voice: true` plus OGG/Opus is what makes WhatsApp render a real
        voice note (waveform, playback speed) instead of a file card."""
        audio: dict = {"id": media_id}
        if voice:
            audio["voice"] = True
        return await self._send({"to": to, "type": "audio", "audio": audio})

    async def send_image(self, to: str, *, link: str | None = None,
                         media_id: str | None = None,
                         caption: str | None = None) -> str:
        """A picture bubble. `link` is a public HTTPS URL Meta fetches
        itself, nothing to upload; `media_id` is a file already uploaded.
        The caption shows under the picture."""
        image: dict = {}
        if media_id:
            image["id"] = media_id
        elif link:
            image["link"] = link
        else:
            raise MetaError("an image needs a link or a media id")
        if caption:
            image["caption"] = _clip(caption, 1024)
        return await self._send({"to": to, "type": "image", "image": image})

    async def send_document(self, to: str, media_id: str, *, filename: str,
                            caption: str | None = None) -> str:
        doc: dict = {"id": media_id, "filename": filename}
        if caption:
            doc["caption"] = _clip(caption, 1024)
        return await self._send({"to": to, "type": "document", "document": doc})

    async def send_interactive_list(self, to: str, *, body: str,
                                    rows: list[dict],
                                    button: str = "Choose",
                                    section_title: str = "Options") -> str:
        """Options as a tappable list. `rows` is [{id, title, description}];
        it is clipped to WhatsApp's limits here so a caller never has to."""
        packed = []
        for row in rows[:LIST_ROW_LIMIT]:
            item = {
                "id": _clip(str(row.get("id") or ""), LIST_ID_LIMIT),
                "title": _clip(str(row.get("title") or ""), LIST_TITLE_LIMIT),
            }
            desc = row.get("description")
            if desc:
                item["description"] = _clip(str(desc), LIST_DESC_LIMIT)
            if item["id"] and item["title"]:
                packed.append(item)
        if not packed:
            raise MetaError("an interactive list needs at least one row")
        return await self._send({
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": _clip(body, 1024)},
                "action": {
                    "button": _clip(button, LIST_BUTTON_LIMIT),
                    "sections": [
                        {"title": _clip(section_title, LIST_TITLE_LIMIT),
                         "rows": packed}
                    ],
                },
            },
        })

    async def mark_read(self, wamid: str, *, typing: bool = False) -> None:
        """Blue ticks, and optionally the typing bubble. Best effort: a
        failure here must never stop the actual answer, so it is logged and
        swallowed rather than raised."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": wamid,
        }
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        try:
            await self._request(
                "POST", f"{self.phone_number_id}/messages",
                json=payload, attempts=1,
            )
        except MetaError as e:
            logger.debug(f"mark_read skipped: {e}")

    # ── media ────────────────────────────────────────────────────────────

    async def upload_media(self, data: bytes, mime: str,
                           filename: str = "file") -> str:
        resp = await self._request(
            "POST", f"{self.phone_number_id}/media",
            files={"file": (filename, data, mime)},
            data={"messaging_product": "whatsapp", "type": mime},
        )
        return str(resp.json().get("id") or "")

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Two hops: the id resolves to a short-lived URL, which then needs
        the same Bearer header. Returns (bytes, mime). The sha256 Meta
        reports is verified; a mismatch means a truncated download."""
        meta = (await self._request("GET", str(media_id))).json()
        url = meta.get("url")
        if not url:
            raise MetaError(f"media {media_id} has no download url")
        resp = await self._request("GET", url)
        data = resp.content
        want = meta.get("sha256")
        if want and hashlib.sha256(data).hexdigest() != want:
            raise MetaError(f"media {media_id} failed its checksum",
                            retryable=True)
        mime = meta.get("mime_type") or resp.headers.get(
            "content-type", "application/octet-stream"
        )
        return data, str(mime).split(";")[0].strip()

    # ── account ──────────────────────────────────────────────────────────

    async def profile(self) -> dict:
        """The number's own record, used to validate a channel before it is
        stored: a bad token or a wrong phone number id fails here with a
        readable reason instead of silently at the first customer message."""
        resp = await self._request(
            "GET", self.phone_number_id, attempts=1,
            params={"fields": "display_phone_number,verified_name,"
                              "quality_rating,platform_type"},
        )
        return resp.json()

    async def waba_id(self) -> str:
        """The WhatsApp Business Account this number lives under.

        Derived rather than asked for on the form: operators rarely know
        the id, and it is needed for subscribe().
        """
        resp = await self._request(
            "GET", self.phone_number_id, attempts=1,
            params={"fields": "whatsapp_business_account"},
        )
        account = (resp.json() or {}).get("whatsapp_business_account") or {}
        return str(account.get("id") or "")

    async def subscribed_apps(self, waba: str) -> list[dict]:
        """Which apps receive this account's webhooks. Empty means nobody
        does, which is the same failure as never subscribing."""
        resp = await self._request(
            "GET", f"{waba}/subscribed_apps", attempts=1
        )
        return list((resp.json() or {}).get("data") or [])

    async def subscribe(self, waba: str) -> bool:
        """Put this app on the account's webhook subscriber list.

        The step with no user interface in either Meta portal and no
        mention in their docs. A newly created app is NOT subscribed to
        its own account, so the callback URL verifies, the messages field
        is ticked, the app is live, and not one message is ever delivered.
        No app id is passed: Meta subscribes whichever app the token
        belongs to, which is why the token has to be the right one.
        """
        resp = await self._request("POST", f"{waba}/subscribed_apps")
        return bool((resp.json() or {}).get("success"))

    async def app_id(self) -> str:
        """The app this token belongs to, for display on the channel card
        and to tell our own subscription apart from Meta's internal one."""
        resp = await self._request(
            "GET", "debug_token", attempts=1,
            params={"input_token": self.token},
        )
        data = (resp.json() or {}).get("data") or {}
        return str(data.get("app_id") or "")


def subscriber_ids(apps: list[dict]) -> list[str]:
    """App ids out of a subscribed_apps response. Each entry wraps them in
    `whatsapp_business_api_data`; tolerate a flat shape too."""
    out = []
    for entry in apps or []:
        if not isinstance(entry, dict):
            continue
        data = entry.get("whatsapp_business_api_data") or entry
        if isinstance(data, dict) and data.get("id"):
            out.append(str(data["id"]))
    return out


def _error_detail(resp: httpx.Response) -> tuple[str, int | None]:
    """Meta's error envelope into one readable line for the operator."""
    try:
        err = (resp.json() or {}).get("error") or {}
    except ValueError:
        return f"HTTP {resp.status_code} from Meta", None
    msg = err.get("error_user_msg") or err.get("message") or "unknown error"
    detail = err.get("error_data", {}).get("details") if isinstance(
        err.get("error_data"), dict
    ) else None
    if detail and detail not in msg:
        msg = f"{msg} ({detail})"
    return f"{msg} [HTTP {resp.status_code}]", err.get("code")
