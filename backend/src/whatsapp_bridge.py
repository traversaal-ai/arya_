"""arya-whatsapp: Meta's webhook on one side, a LiveKit room on the other.

WhatsApp customers talk to exactly the same agent the web widget talks to.
A message arrives, this service finds (or starts) that customer's session
through the ordinary public API, joins the room as a data-only
participant, writes the text on `lk.chat`, and sends whatever the agent
answers back to WhatsApp. The agent loop, RAG, memory, connectors, tool
auditing and transcript persistence are all untouched and unaware.

Why a separate service rather than a route in api.py: joining a LiveKit
room means negotiating ICE, and livekit-server runs with
`--node-ip 127.0.0.1`, so a container on the compose bridge network is
handed candidates pointing at its own loopback (agents.md gotcha 1). This
runs with network_mode: host for the same reason agent-worker does.

Lifecycle, and why it is not one room per thread forever: a WhatsApp
conversation can pause for hours, and an idle LiveKit room costs a worker
subprocess. After `idle_minutes` of silence the bridge leaves, the room
empties and the worker shuts down normally. The next message opens a new
session and the worker replays the tail of the previous transcript, so the
customer experiences one continuous conversation.

Ordering: one asyncio queue per thread. WhatsApp shows messages in arrival
order, so two questions sent a second apart must be answered in that
order, and a queue is the only thing that guarantees it under concurrent
webhook deliveries.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
import time
import uuid

import httpx
from fastapi import FastAPI, Request, Response
from livekit import rtc

import config
import db
import media
import meta_wa
import providers
import visitors

logger = logging.getLogger("arya.whatsapp")
logging.basicConfig(level=logging.INFO)

API_URL = os.environ.get("ARYA_API_URL", "http://localhost:8000").rstrip("/")
BRIDGE_PORT = int(os.environ.get("ARYA_WHATSAPP_PORT", "8030"))

# Quiet gap after the agent's last transcription segment before the buffer
# is sent as one WhatsApp message. Long enough to let a multi-segment reply
# (a filler phrase, then the real answer) arrive as one bubble; short
# enough that nobody notices the wait.
REPLY_QUIET_S = float(os.environ.get("ARYA_WA_REPLY_QUIET_S", "1.4"))
# How long the customer waits with nothing at all before we acknowledge.
SLOW_ACK_S = float(os.environ.get("ARYA_WA_SLOW_ACK_S", "25"))
# A message sent longer ago than this is acknowledged and recorded but not
# answered. Meta keeps retrying a webhook it could not deliver for up to
# seven days, so after a dead tunnel or a rebuild the bridge receives
# messages from the previous day and, seen live, greeted the customer in
# reply to a "Hi" they sent twenty hours earlier. Half an hour is longer
# than any honest delay and shorter than anyone's memory of what they asked.
STALE_INBOUND_S = float(os.environ.get("ARYA_WA_STALE_S", str(30 * 60)))
JOIN_TIMEOUT_S = 25.0
# Channel rows change rarely and are read on every inbound message.
CHANNEL_TTL_S = 30.0

BUSY_TEXT = ("We have a lot of conversations open right now. "
             "Please send your message again in a few minutes.")
BROKEN_TEXT = ("Sorry, something went wrong on our side. "
               "Please try again in a moment.")
SLOW_TEXT = "Still working on that, one moment."
# asyncio only holds a weak reference to a running task, so a fire-and-
# forget task can be collected mid-flight. Everything spawned here is kept
# in a set until it finishes.
_background: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


UNSUPPORTED_TEXT = {
    "image": "I cannot look at images yet. Could you describe it in words?",
    "audio": "I cannot listen to voice notes yet. Could you type it instead?",
    "document": "I cannot open files yet. Could you paste the key details?",
    "default": "I can only read text messages at the moment.",
}

# Two different refusals, because they have different answers. One is a
# setting the operator chose; the other is a model that cannot see, which
# no amount of resending will fix.
IMAGE_OFF_TEXT = (
    "I am not set up to look at photos here. Could you describe it in words?"
)
IMAGE_NO_VISION_TEXT = (
    "I cannot view images on this setup. Could you describe what you are"
    " seeing?"
)
IMAGE_BROKEN_TEXT = (
    "Something went wrong opening that photo. Could you send it again, or"
    " describe it in words?"
)
VOICE_EMPTY_TEXT = (
    "I could not make out anything in that voice note. Could you try again"
    " somewhere quieter, or type it?"
)
VOICE_BROKEN_TEXT = (
    "I had trouble listening to that voice note. Could you send it again,"
    " or type it instead?"
)

# A spoken reply is capped well below the text one. About a minute of
# speech is the limit of what anyone listens to from a support line, and
# past it the voice note becomes a worse version of the text that was
# sent alongside it anyway.
SPEAK_LIMIT = 900

# URLs read aloud are unusable, and the text message carries them already.
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+", re.I)


def remaining_idle(idle_s: float, last_activity: float, now: float) -> float:
    """How long a thread may wait for its next message before it is due an
    idle check: the time left until `idle_s` after the last activity, never
    less than a few seconds so a busy loop cannot form."""
    return max(5.0, idle_s - (now - last_activity))


def is_stale(sent_at: int, now: float, max_age_s: float = STALE_INBOUND_S) -> bool:
    """Was this message sent too long ago to answer now? `sent_at` is
    Meta's own timestamp for the message (seconds), 0 when absent, in
    which case nothing is known and the message is treated as fresh."""
    return sent_at > 0 and (now - sent_at) > max_age_s


def photo_bubbles(group: dict) -> list[tuple[str, str | None, str]]:
    """(link, fallback link, caption) for each item of an option group
    that carries a picture, in order. Items without one are skipped, so a
    shop with no images produces no bubbles and nothing else changes.
    Pure, so it is testable without a room."""
    out: list[tuple[str, str | None, str]] = []
    for item in group.get("items") or []:
        if not isinstance(item, dict):
            continue
        link = str(item.get("image") or "").strip()
        if not link:
            continue
        fallback = str(item.get("image_fallback") or "").strip() or None
        label = str(item.get("label") or "").strip()
        detail = str(item.get("detail") or item.get("value") or "").strip()
        caption = ", ".join(p for p in (label, detail) if p)
        out.append((link, fallback, caption))
    return out


def reply_mode(inbound_kind: str, setting: str | None) -> str:
    """How this reply goes out: "text" or "voice", never both.

    Both at once is noise. A customer who sent a voice note is usually
    walking or driving and wants to listen; one who typed has told you
    they can read. match answers the way they asked, which is why it is
    the default; the other two override that either way.
    """
    mode = config.normalize_voice_mode(setting)
    if mode == "voice":
        return "voice"
    if mode == "text":
        return "text"
    return "voice" if inbound_kind == "audio" else "text"


def speakable(text: str) -> str:
    """The reply as it should be heard: no URLs, and short enough to be
    worth listening to. Empty means say nothing and let the text stand."""
    body = _URL_RE.sub("", text or "").strip()
    # Collapse the gaps a stripped URL leaves behind.
    body = re.sub(r"\s{2,}", " ", body)
    if not body or len(body) > SPEAK_LIMIT:
        return ""
    return body


# What a transcribed turn looks like in the conversation. The tag is the
# only trace a voice note leaves, so History, memory and the link resolver
# all see that the customer spoke; catalog's channel_media_rules tells the
# model to read past it without commenting on it.
VOICE_PREFIX = "[voice note] "

# The topic the worker listens on for an attachment belonging to the turn
# being sent. Registered there before the ready signal, so a photo that
# arrives first cannot miss its handler.
ATTACH_TOPIC = "arya.attachments"


def decide_image(images_setting: str, vision: bool) -> str:
    """accept, declined_off or declined_model.

    Separated from the I/O so the rule an operator actually cares about
    (off always wins, auto defers to the model) is testable as a table.
    """
    if (images_setting or "auto").lower() == "off":
        return "declined_off"
    if not vision:
        return "declined_model"
    return "accept"


class Thread:
    """One customer's live link between a WhatsApp number and a room.

    Owns its LiveKit connection, its inbound queue and its reply buffer.
    Every await that touches WhatsApp or LiveKit happens on this object's
    single worker task, so there is no locking beyond the queue itself.
    """

    def __init__(self, bridge: Bridge, channel: dict, wa_user: str,
                 display_name: str | None):
        self.bridge = bridge
        self.channel = channel
        self.wa_user = wa_user
        self.display_name = display_name
        self.room: rtc.Room | None = None
        self.conversation_id: str | None = None
        self.room_name: str | None = None
        self.identity: str | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._reply: list[str] = []
        self._flush_handle: asyncio.TimerHandle | None = None
        self._slow_handle: asyncio.TimerHandle | None = None
        self._last_activity = time.monotonic()
        self._closing = False
        # Set the moment this thread commits to shutting down, before any
        # await. The registry treats a done thread as absent, so a message
        # arriving during the teardown opens a fresh one instead of
        # landing in a queue nobody is reading any more.
        self._done = False
        self._loop = asyncio.get_running_loop()
        self._pending_context: str | None = None
        self._ready = asyncio.Event()
        # Whether the model behind this channel can read a photo. Read
        # from the mounted data volume and cached for the life of the
        # thread: an operator changing the agent's model applies on the
        # next room, which is soon enough and keeps this off the hot path.
        self._agent_cfg: dict | None = None
        self._speech: dict | None = None
        # Whether the customer's last turn was spoken. Phase D mirrors it
        # when deciding whether to answer out loud.
        self._last_inbound_kind = "text"
        # Pictures sent by public link, keyed by wamid, with the hosted
        # copy to send if Meta's fetch of the link fails. Meta accepts the
        # send first and fetches later, so the failure is a delivery
        # receipt (photo_failed), not an error on the send.
        self._photo_retry: dict[str, tuple[str, str]] = {}

    # ── configuration helpers ────────────────────────────────────────────

    @property
    def cfg(self) -> dict:
        return self.channel["config"]

    @property
    def idle_s(self) -> float:
        return float(self.cfg.get("idle_minutes", 10)) * 60.0

    def _client(self) -> meta_wa.MetaClient:
        return self.bridge.client_for(self.channel)

    @property
    def agent_cfg(self) -> dict:
        if self._agent_cfg is None:
            self._agent_cfg = config.load_saved_agent(
                self.channel.get("agent_id") or ""
            ) or {}
        return self._agent_cfg

    @property
    def speech(self) -> dict:
        """Where this agent's audio goes, the same resolution the voice
        pipeline uses, so a channel never drifts onto a different
        provider than the widget."""
        if self._speech is None:
            self._speech = providers.speech_endpoint(
                self.agent_cfg, config.load_keys()
            )
        return self._speech

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def enqueue(self, msg: meta_wa.InboundMessage) -> None:
        self._queue.put_nowait(msg)
        self.start()

    async def _run(self) -> None:
        try:
            while True:
                # Wait only until the idle deadline, not a whole idle period
                # from now: a reply sent after the wait began pushed the
                # deadline out, and waiting another full period from here
                # left threads open for up to twice the setting (seen
                # live: a "10 minute" thread still open 14 minutes later,
                # so the next "Hi" joined the stale conversation).
                timeout = remaining_idle(self.idle_s, self._last_activity, time.monotonic())
                try:
                    msg = await asyncio.wait_for(self._queue.get(), timeout)
                except asyncio.TimeoutError:
                    if time.monotonic() - self._last_activity >= self.idle_s:
                        # Nothing may await between this check and _done,
                        # or a message could slip into a queue that is
                        # about to be abandoned. asyncio is cooperative,
                        # so with no await here the pair is atomic.
                        if not self._queue.empty():
                            continue
                        self._done = True
                        await self.close("idle")
                        return
                    continue
                try:
                    await self._handle(msg)
                except Exception as e:
                    logger.exception(f"thread {self.wa_user}: {e}")
                    await self._safe_send(BROKEN_TEXT)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        finally:
            self._done = True
            self.bridge.forget(self)

    async def close(self, reason: str) -> None:
        """Leave the room and let it empty out. Deliberately does NOT end
        the conversation through the API: the worker's own shutdown path
        already closes the row, and a race between the two produced
        conversations with two end reasons."""
        if self._closing:
            return
        self._closing = True
        self._cancel_timers()
        if self._reply:
            await self._flush_now()
        room, self.room = self.room, None
        if room is not None:
            with contextlib.suppress(Exception):
                await room.disconnect()
        if self.conversation_id:
            await self.bridge.mark_thread_closed(
                self.channel["id"], self.wa_user, self.conversation_id
            )
        logger.info(
            f"thread {self.wa_user} closed ({reason}), "
            f"conversation={self.conversation_id}"
        )
        self.conversation_id = None
        self.room_name = None

    def _cancel_timers(self) -> None:
        for handle in (self._flush_handle, self._slow_handle):
            if handle is not None:
                handle.cancel()
        self._flush_handle = None
        self._slow_handle = None

    # ── inbound ──────────────────────────────────────────────────────────

    async def _handle(self, msg: meta_wa.InboundMessage) -> None:
        self._last_activity = time.monotonic()
        client = self._client()
        # Blue ticks and the typing bubble immediately: the customer should
        # see that the message landed well before the model has an answer.
        await client.mark_read(msg.wamid, typing=True)

        text = (msg.text or "").strip()
        if msg.kind in meta_wa.MEDIA_KINDS or msg.kind == "unsupported":
            handled = await self._handle_non_text(msg)
            if not handled:
                return
            text = handled

        if not text:
            return
        if not await self._ensure_room():
            return
        self._pending_context = msg.wamid
        self._arm_slow_ack()
        assert self.room is not None
        await self.room.local_participant.send_text(text, topic="lk.chat")
        await self.bridge.record_thread_inbound(self.channel["id"], self.wa_user)

    async def _handle_non_text(self, msg: meta_wa.InboundMessage) -> str | None:
        """Media and unclassified types. Returns the text to feed the agent,
        or None when the turn is already fully handled: either the customer
        has been told we cannot use it, or (for a photo) the image has gone
        to the worker on its own stream and there is no text to send.

        Voice notes and documents are still declined. A wrong guess ("I got
        your voice note") is worse than an honest refusal.
        """
        if msg.kind == "image":
            return await self._handle_image(msg)
        if msg.kind == "audio":
            return await self._handle_audio(msg)
        note = UNSUPPORTED_TEXT.get(msg.kind, UNSUPPORTED_TEXT["default"])
        caption = (msg.text or "").strip()
        if caption:
            # A photo with a caption is usually a question that stands on
            # its own; answer the words and say the picture went unseen.
            await self._safe_send(note)
            return caption
        await self._safe_send(note)
        return None

    async def _handle_image(self, msg: meta_wa.InboundMessage) -> str | None:
        """A photo, either into the model or politely refused.

        The decision is made before anything is downloaded, so a refusal
        is instant and costs no bandwidth. An accepted photo is shrunk and
        published on its own byte stream with the caption as an attribute,
        so the worker can build a single user turn carrying both. That is
        why the accept path returns None: there is no text left to send.
        """
        caption = (msg.text or "").strip()
        decision = decide_image(
            self.cfg.get("images", "auto"), config.vision_capable(self.agent_cfg)
        )
        if decision != "accept":
            await self._safe_send(
                IMAGE_OFF_TEXT if decision == "declined_off"
                else IMAGE_NO_VISION_TEXT
            )
            await self._record_media(msg, status="declined", error=decision)
            # A caption usually asks something that stands on its own, so
            # answer the words even though the picture went unseen.
            return caption or None

        if not await self._ensure_room():
            return None
        try:
            data, _mime = await self._client().download_media(msg.media_id or "")
            jpeg, width, height = media.shrink_image(data)
        except (meta_wa.MetaError, media.MediaError) as e:
            logger.warning(f"image from {self.wa_user} unusable: {e}")
            await self._safe_send(IMAGE_BROKEN_TEXT)
            await self._record_media(msg, status="failed", error=str(e))
            return caption or None

        self._pending_context = msg.wamid
        self._arm_slow_ack()
        assert self.room is not None
        path = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
                fh.write(jpeg)
                path = fh.name
            await self.room.local_participant.send_file(
                path,
                topic=ATTACH_TOPIC,
                attributes={
                    "caption": caption,
                    "mime": "image/jpeg",
                    "wamid": msg.wamid,
                    "width": str(width),
                    "height": str(height),
                },
            )
        except Exception as e:
            logger.error(f"publishing image for {self.wa_user} failed: {e}")
            await self._safe_send(IMAGE_BROKEN_TEXT)
            await self._record_media(msg, status="failed", error=str(e))
            return None
        finally:
            if path:
                with contextlib.suppress(OSError):
                    os.unlink(path)

        await self._record_media(msg, status="received")
        await self.bridge.record_thread_inbound(self.channel["id"], self.wa_user)
        return None

    async def _handle_audio(self, msg: meta_wa.InboundMessage) -> str | None:
        """A voice note into words, then on as an ordinary text turn.

        Both a held-mic voice note and an attached audio file land here:
        to the customer they are the same act, and the transcript reads
        the same either way.

        The transcript goes out on lk.chat exactly like typed text, so
        everything downstream (the knowledge base, memory, the reply
        buffer) needs no knowledge that speech was involved. Only the tag
        distinguishes it, and that exists for the model and for History.
        """
        try:
            data, mime = await self._client().download_media(msg.media_id or "")
        except meta_wa.MetaError as e:
            logger.warning(f"voice note from {self.wa_user} not downloadable: {e}")
            await self._safe_send(VOICE_BROKEN_TEXT)
            await self._record_media(msg, status="failed", error=str(e))
            return None

        try:
            # Its own client, not the bridge's: that one is set to 30s
            # for Graph and API calls, and a busy speech service needs
            # longer to answer than to be given up on.
            transcript = await media.transcribe(self.speech, data, mime=mime)
        except media.MediaError as e:
            logger.warning(f"transcription for {self.wa_user} failed: {e}")
            await self._safe_send(VOICE_BROKEN_TEXT)
            await self._record_media(msg, status="failed", error=str(e))
            return None

        if not transcript:
            # Silence, a pocket recording, or speech the model could not
            # resolve. Saying so is better than sending the agent an empty
            # turn it will answer with a guess.
            await self._safe_send(VOICE_EMPTY_TEXT)
            await self._record_media(msg, status="empty")
            return None

        self._last_inbound_kind = "audio"
        await self._record_media(msg, status="received")
        return VOICE_PREFIX + transcript

    async def _record_media(self, msg: meta_wa.InboundMessage, *, status: str,
                            error: str | None = None) -> None:
        """What became of this photo, stamped onto the row claim_event
        already wrote for it.

        The real wamid, so record_event's upsert updates that row rather
        than adding a second one under a made-up id: the event log should
        answer "how often are photos refused, and why" with one filter,
        not by correlating two rows per photo.
        """
        await self.bridge.record_event(
            wamid=msg.wamid, channel_id=self.channel["id"],
            direction="in", kind=msg.kind, external_user=self.wa_user,
            conversation_id=self.conversation_id, status=status, error=error,
        )

    async def _ensure_room(self) -> bool:
        if self.room is not None and self.conversation_id:
            return True
        try:
            session = await self.bridge.start_session(self.channel, self.wa_user)
        except _CapacityError:
            await self._safe_send(BUSY_TEXT)
            return False
        except Exception as e:
            logger.error(f"session start failed for {self.wa_user}: {e}")
            await self._safe_send(BROKEN_TEXT)
            return False

        room = rtc.Room()
        self._ready.clear()
        # Known before connecting, so a transcription arriving in the first
        # milliseconds is still attributed correctly.
        self.identity = session.get("identity")
        room.on("disconnected", self._on_disconnected)
        room.register_text_stream_handler(
            "lk.transcription", self._on_transcription
        )
        room.register_text_stream_handler("arya.links", self._on_links)
        room.register_text_stream_handler("arya.options", self._on_options)
        room.register_text_stream_handler("arya.ready", self._on_ready)
        try:
            await asyncio.wait_for(
                room.connect(session["ws_url"], session["token"]),
                JOIN_TIMEOUT_S,
            )
        except Exception as e:
            logger.error(f"room join failed for {self.wa_user}: {e}")
            with contextlib.suppress(Exception):
                await room.disconnect()
            await self._safe_send(BROKEN_TEXT)
            return False
        self.room = room
        self.conversation_id = session["conversation_id"]
        self.room_name = session["room"]
        self.identity = room.local_participant.identity or self.identity
        self._closing = False
        # The agent registers its lk.chat handler inside session.start, so
        # a message written before that lands in a room nobody is
        # listening in and is lost with no error anywhere. The worker says
        # when it is ready; if that never arrives we go ahead regardless,
        # because a late message beats a dropped one.
        try:
            await asyncio.wait_for(self._ready.wait(), JOIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                f"agent never signalled ready in room {self.room_name}, "
                "sending anyway"
            )
        await self.bridge.mark_thread_open(
            self.channel["id"], self.wa_user, self.display_name,
            self.conversation_id, self.room_name,
        )
        logger.info(
            f"thread {self.wa_user} opened room {self.room_name} "
            f"(conversation {self.conversation_id})"
        )
        return True

    def _on_ready(self, reader, participant_identity: str) -> None:
        self._ready.set()

    def _on_disconnected(self, *_args) -> None:
        # The worker ended the session (inactivity watchdog, cap reached) or
        # the room was deleted. Drop our side so the next message opens a
        # fresh one rather than writing into a dead room.
        self.room = None
        self.conversation_id = None

    # ── outbound ─────────────────────────────────────────────────────────

    def _on_transcription(self, reader, participant_identity: str) -> None:
        if participant_identity == self.identity:
            return  # our own echo of the customer's message
        _spawn(self._collect(reader))

    async def _collect(self, reader) -> None:
        try:
            text = await reader.read_all()
        except Exception as e:
            logger.debug(f"transcription read failed: {e}")
            return
        text = (text or "").strip()
        if not text:
            return
        self._reply.append(text)
        self._last_activity = time.monotonic()
        self._arm_flush()

    def _arm_flush(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
        self._flush_handle = self._loop.call_later(
            REPLY_QUIET_S, lambda: _spawn(self._flush_now())
        )

    def _arm_slow_ack(self) -> None:
        if self._slow_handle is not None:
            self._slow_handle.cancel()
        self._slow_handle = self._loop.call_later(
            SLOW_ACK_S, lambda: _spawn(self._slow_ack())
        )

    async def _slow_ack(self) -> None:
        if self._reply:
            return  # something already arrived, no need to stall
        await self._safe_send(SLOW_TEXT)

    async def _flush_now(self) -> None:
        """Send the buffered reply as one WhatsApp message (or several, if
        it is past the 4096 character cap)."""
        self._cancel_timers()
        if not self._reply:
            return
        body = " ".join(part.strip() for part in self._reply if part.strip())
        self._reply = []
        context, self._pending_context = self._pending_context, None
        mode = reply_mode(
            self._last_inbound_kind, self.cfg.get("voice_reply")
        )
        # One voice note must not make the rest of the thread spoken under
        # match, so the state resets with the turn that set it.
        self._last_inbound_kind = "text"
        # Text is the fallback, never a duplicate: a spoken reply that
        # cannot be produced still has to reach the customer somehow, and
        # silence is the one outcome that is not acceptable.
        if mode == "voice" and await self._speak(body):
            return
        for chunk in meta_wa.split_text(body):
            await self._safe_send(chunk, context_wamid=context)
            context = None  # quote the question once, not on every chunk

    async def _speak(self, body: str) -> bool:
        """The reply as a voice note. False means it did not go, and the
        caller must send the text instead.

        Returns False rather than raising on every failure path, because
        every one of them has the same answer: fall back to writing.
        Links are not affected either way; they travel on their own
        stream and always arrive as tappable text.
        """
        say = speakable(body)
        if not say:
            return False
        try:
            audio, _mime = await media.synthesize(self.speech, say)
            # Which provider spoke, and whether an expressive tag went with
            # it: Google's SDK leaves no request line in this log (it does
            # not use httpx), so without this a Gemini reply and a silent
            # fallback look the same from outside.
            logger.info(
                f"spoken reply for {self.wa_user}: provider="
                f"{self.speech.get('provider')} voice={self.speech.get('voice')}"
                f" expressive={bool(self.speech.get('style'))}"
            )
            ogg = media.to_ogg_opus(audio)
            media_id = await self._client().upload_media(
                ogg, "audio/ogg", "reply.ogg"
            )
        except (media.MediaError, meta_wa.MetaError) as e:
            logger.warning(f"spoken reply for {self.wa_user} skipped: {e}")
            return False
        try:
            # voice=True is what makes WhatsApp draw a waveform and play
            # inline; without it the same file shows as a download card.
            wamid = await self._client().send_audio(
                self.wa_user, media_id, voice=True
            )
        except meta_wa.MetaError as e:
            logger.error(f"voice note to {self.wa_user} failed: {e}")
            await self.bridge.record_event(
                wamid=f"failed-{uuid.uuid4().hex}",
                channel_id=self.channel["id"], direction="out", kind="audio",
                external_user=self.wa_user,
                conversation_id=self.conversation_id, status="failed",
                error=str(e),
            )
            return False
        self._last_activity = time.monotonic()
        await self.bridge.record_event(
            wamid=wamid, channel_id=self.channel["id"], direction="out",
            kind="audio", external_user=self.wa_user,
            conversation_id=self.conversation_id, status="sent",
        )
        await self.bridge.record_thread_outbound(
            self.channel["id"], self.wa_user
        )
        return True

    def _on_links(self, reader, participant_identity: str) -> None:
        if participant_identity == self.identity:
            return
        _spawn(self._send_links(reader))

    async def _send_links(self, reader) -> None:
        payload = await _read_json(reader)
        hrefs = [
            link.get("href") for link in (payload.get("links") or [])
            if link.get("href")
        ]
        if not hrefs:
            return
        # The reply itself is still buffering; let it land first so the link
        # reads as a follow-up rather than arriving before the sentence
        # that introduces it.
        await asyncio.sleep(REPLY_QUIET_S + 0.6)
        await self._safe_send("\n".join(hrefs))

    def _on_options(self, reader, participant_identity: str) -> None:
        if participant_identity == self.identity:
            return
        _spawn(self._send_options(reader))

    async def _send_photo(self, link: str, fallback: str | None,
                          caption: str) -> None:
        """A product picture with its caption: the first link, then the
        hosted copy if Meta refuses the send outright, or later, through
        photo_failed, if Meta accepts it and then cannot fetch the link
        (seen live: Unsplash served AVIF to Meta's fetcher). A picture that
        will not go never stops the list or the reply that follows."""
        for candidate, backup in ((link, fallback), (fallback, None)):
            if not candidate:
                continue
            try:
                wamid = await self._client().send_image(
                    self.wa_user, link=candidate, caption=caption
                )
            except meta_wa.MetaError as e:
                logger.warning(f"photo not sent from {candidate}: {e}")
                continue
            self._last_activity = time.monotonic()
            await self.bridge.record_event(
                wamid=wamid, channel_id=self.channel["id"], direction="out",
                kind="image", external_user=self.wa_user,
                conversation_id=self.conversation_id, status="sent",
            )
            if backup:
                self._photo_retry[wamid] = (backup, caption)
            return

    async def photo_failed(self, wamid: str) -> bool:
        """A delivery receipt says this picture did not go: send its hosted
        copy, once. False when the wamid is not a picture with a copy."""
        entry = self._photo_retry.pop(wamid, None)
        if entry is None:
            return False
        fallback, caption = entry
        logger.info(f"photo {wamid} failed to deliver, sending the hosted copy")
        await self._send_photo(fallback, None, caption)
        return True

    async def _send_options(self, reader) -> None:
        payload = await _read_json(reader)
        groups = payload.get("options") or []
        await asyncio.sleep(REPLY_QUIET_S + 0.6)
        for group in groups:
            # Pictures first, so the list lands under them and the customer
            # sees what they are choosing between. A single looked-up order
            # sends its picture and no list: one row is not a choice.
            for link, fallback, caption in photo_bubbles(group):
                await self._send_photo(link, fallback, caption)
            if group.get("photo_only"):
                continue
            rows = []
            for item in group.get("items") or []:
                pick = str(item.get("pick") or "").strip()
                label = str(item.get("label") or pick).strip()
                if not pick or not label:
                    continue
                # The row's second line: the spoken detail where a desk
                # gives one (the order desk: status and amount), else the
                # travel cards' value column.
                value = str(item.get("detail") or item.get("value") or "").strip()
                rows.append({"id": pick, "title": label, "description": value})
            if not rows:
                continue
            title = str(group.get("title") or "Options").strip()
            try:
                await self._client().send_interactive_list(
                    self.wa_user, body=title, rows=rows,
                    button="Choose", section_title=title,
                )
            except meta_wa.MetaError as e:
                logger.warning(f"option list failed, sending text: {e}")
                lines = [f"{i}. {r['title']} {r['description']}".strip()
                         for i, r in enumerate(rows, 1)]
                await self._safe_send(f"{title}\n" + "\n".join(lines))

    async def _safe_send(self, body: str, *, context_wamid: str | None = None):
        body = (body or "").strip()
        if not body:
            return
        try:
            wamid = await self._client().send_text(
                self.wa_user, body, context_wamid=context_wamid
            )
        except meta_wa.MetaError as e:
            logger.error(f"send to {self.wa_user} failed: {e}")
            await self.bridge.record_event(
                wamid=f"failed-{uuid.uuid4().hex}", channel_id=self.channel["id"],
                direction="out", kind="text", external_user=self.wa_user,
                conversation_id=self.conversation_id, status="failed",
                error=str(e),
            )
            return
        self._last_activity = time.monotonic()
        await self.bridge.record_event(
            wamid=wamid, channel_id=self.channel["id"], direction="out",
            kind="text", external_user=self.wa_user,
            conversation_id=self.conversation_id, status="sent",
        )
        await self.bridge.record_thread_outbound(
            self.channel["id"], self.wa_user
        )


async def _read_json(reader) -> dict:
    try:
        raw = await reader.read_all()
        return json.loads(raw) if raw else {}
    except Exception as e:
        logger.debug(f"side-channel read failed: {e}")
        return {}


class _CapacityError(RuntimeError):
    """The deployment's caps or rate limits refused this session."""


class Bridge:
    """Channel lookup, thread registry and every database write."""

    def __init__(self):
        self._threads: dict[tuple[str, str], Thread] = {}
        self._clients: dict[str, meta_wa.MetaClient] = {}
        self._channels: dict[str, tuple[float, dict]] = {}
        self._http = httpx.AsyncClient(timeout=30.0)
        self._pool = None

    @property
    def open_threads(self) -> int:
        return len(self._threads)

    async def pool(self):
        if self._pool is None:
            self._pool = await db.get_pool(config.database_url())
        return self._pool

    async def aclose(self) -> None:
        for thread in list(self._threads.values()):
            await thread.close("shutdown")
        for client in self._clients.values():
            await client.aclose()
        await self._http.aclose()

    # ── channels ─────────────────────────────────────────────────────────

    async def channel_for(self, phone_number_id: str) -> dict | None:
        """The active channel serving this number, with its deployment's
        public key attached. Cached briefly: this runs on every inbound
        message and the row changes only when an operator edits it."""
        hit = self._channels.get(phone_number_id)
        if hit and time.monotonic() - hit[0] < CHANNEL_TTL_S:
            return hit[1]
        pool = await self.pool()
        row = await pool.fetchrow(
            "SELECT c.id, c.type, c.status, c.config, c.secret,"
            " d.id AS deployment_id, d.public_key, d.status AS deployment_status,"
            " d.agent_id"
            " FROM channels c JOIN deployments d ON d.id = c.deployment_id"
            " WHERE c.config->>'phone_number_id' = $1",
            phone_number_id,
        )
        if row is None:
            return None
        channel = {
            "id": row["id"],
            "type": row["type"],
            "status": row["status"],
            "config": json.loads(row["config"] or "{}"),
            "secret": json.loads(row["secret"] or "{}"),
            "deployment_id": str(row["deployment_id"]),
            "public_key": row["public_key"],
            "deployment_status": row["deployment_status"],
            "agent_id": row["agent_id"],
        }
        self._channels[phone_number_id] = (time.monotonic(), channel)
        return channel

    def client_for(self, channel: dict) -> meta_wa.MetaClient:
        key = channel["id"]
        client = self._clients.get(key)
        if client is None:
            client = meta_wa.MetaClient(
                channel["config"].get("phone_number_id", ""),
                channel["secret"].get("access_token", ""),
                api_version=channel["config"].get("api_version")
                or meta_wa.DEFAULT_API_VERSION,
            )
            self._clients[key] = client
        return client

    # ── sessions ─────────────────────────────────────────────────────────

    async def start_session(self, channel: dict, wa_user: str) -> dict:
        """Start a visitor session exactly the way the widget does, so the
        deployment's caps, rate limits and refusal accounting all apply to
        WhatsApp without a second code path. The bridge token is what lets
        this call NAME the customer (channel + phone number); without it the
        API treats the request as an anonymous widget visitor."""
        resp = await self._http.post(
            f"{API_URL}/api/public/v1/sessions",
            headers={
                "X-Arya-Key": channel["public_key"],
                "X-Arya-Bridge-Token": visitors.bridge_token(),
            },
            json={"channel": "whatsapp", "external_user_id": wa_user},
        )
        if resp.status_code == 429:
            raise _CapacityError(resp.text)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # ── threads ──────────────────────────────────────────────────────────

    def thread_for(self, channel: dict, wa_user: str,
                   display_name: str | None) -> Thread:
        key = (channel["id"], wa_user)
        thread = self._threads.get(key)
        if thread is None or thread._done:
            thread = Thread(self, channel, wa_user, display_name)
            self._threads[key] = thread
        else:
            thread.channel = channel  # pick up edited settings
        return thread

    def forget(self, thread: Thread) -> None:
        # Only if it is still the registered one: a thread that timed out
        # while a new message was arriving has already been replaced, and
        # removing by key alone would drop the live replacement.
        key = (thread.channel["id"], thread.wa_user)
        if self._threads.get(key) is thread:
            self._threads.pop(key, None)

    # ── persistence ──────────────────────────────────────────────────────

    async def claim_event(self, msg: meta_wa.InboundMessage,
                          channel_id: str) -> bool:
        """Insert the inbound id, returning False if it was already there.

        This is the dedupe gate. Meta retries a webhook for up to seven days
        unless it gets a 200, and a duplicate delivery must never produce a
        second answer, so the unique key on wamid does the work.
        """
        pool = await self.pool()
        row = await pool.fetchrow(
            "INSERT INTO channel_events (wamid, channel_id, direction, kind,"
            " external_user, payload) VALUES ($1, $2, 'in', $3, $4, $5::jsonb)"
            " ON CONFLICT (wamid) DO NOTHING RETURNING wamid",
            msg.wamid, channel_id, msg.kind, msg.wa_user, json.dumps(msg.raw),
        )
        return row is not None

    async def record_event(self, *, wamid: str, channel_id: str, direction: str,
                           kind: str, external_user: str | None = None,
                           conversation_id: str | None = None,
                           status: str | None = None,
                           error: str | None = None) -> None:
        try:
            pool = await self.pool()
            await pool.execute(
                "INSERT INTO channel_events (wamid, channel_id, direction,"
                " kind, external_user, conversation_id, status, error)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
                " ON CONFLICT (wamid) DO UPDATE SET status = EXCLUDED.status,"
                " error = EXCLUDED.error, conversation_id ="
                " COALESCE(EXCLUDED.conversation_id,"
                " channel_events.conversation_id)",
                wamid or f"out-{uuid.uuid4().hex}", channel_id, direction, kind,
                external_user,
                uuid.UUID(conversation_id) if conversation_id else None,
                status, error,
            )
        except Exception as e:
            logger.debug(f"event record skipped: {e}")

    async def apply_status(self, status: meta_wa.StatusUpdate,
                           channel_id: str) -> None:
        try:
            pool = await self.pool()
            await pool.execute(
                "UPDATE channel_events SET status = $2, error = COALESCE($3, error)"
                " WHERE wamid = $1", status.wamid, status.status, status.error,
            )
            if status.error:
                logger.warning(
                    f"delivery {status.status} for {status.wamid}: {status.error}"
                )
        except Exception as e:
            logger.debug(f"status update skipped: {e}")
        if status.status != "failed":
            return
        # A picture Meta could not fetch: the open thread for that customer
        # sends the hosted copy. A thread already closed has nothing to
        # retry with, and the receipt has been recorded above regardless.
        thread = self._threads.get((channel_id, status.recipient))
        if thread is None:
            return
        try:
            await thread.photo_failed(status.wamid)
        except Exception as e:
            logger.warning(f"photo retry skipped: {e}")

    async def mark_thread_open(self, channel_id: str, wa_user: str,
                               name: str | None, conversation_id: str,
                               room_name: str) -> None:
        pool = await self.pool()
        await pool.execute(
            "INSERT INTO channel_threads (channel_id, external_user,"
            " display_name, conversation_id, room_name, state, last_inbound_at,"
            " window_expires_at, updated_at) VALUES ($1, $2, $3, $4, $5,"
            " 'open', now(), now() + interval '24 hours', now())"
            " ON CONFLICT (channel_id, external_user) DO UPDATE SET"
            " display_name = COALESCE(EXCLUDED.display_name,"
            "   channel_threads.display_name),"
            " conversation_id = EXCLUDED.conversation_id,"
            " room_name = EXCLUDED.room_name, state = 'open',"
            " last_inbound_at = now(),"
            " window_expires_at = now() + interval '24 hours',"
            " updated_at = now()",
            channel_id, wa_user, name, uuid.UUID(conversation_id), room_name,
        )

    async def mark_thread_closed(self, channel_id: str, wa_user: str,
                                 conversation_id: str) -> None:
        # Scoped to the conversation that is actually closing. A thread
        # that timed out while the customer was already writing again has
        # a newer session in this row, and marking that one idle would
        # leave the database describing a room that is very much alive.
        pool = await self.pool()
        await pool.execute(
            "UPDATE channel_threads SET state = 'idle', room_name = NULL,"
            " updated_at = now() WHERE channel_id = $1 AND external_user = $2"
            " AND conversation_id = $3",
            channel_id, wa_user, uuid.UUID(conversation_id),
        )

    async def record_thread_inbound(self, channel_id: str, wa_user: str) -> None:
        pool = await self.pool()
        await pool.execute(
            "UPDATE channel_threads SET last_inbound_at = now(),"
            " window_expires_at = now() + interval '24 hours', updated_at = now()"
            " WHERE channel_id = $1 AND external_user = $2",
            channel_id, wa_user,
        )

    async def record_thread_outbound(self, channel_id: str,
                                     wa_user: str) -> None:
        pool = await self.pool()
        await pool.execute(
            "UPDATE channel_threads SET last_outbound_at = now(),"
            " updated_at = now() WHERE channel_id = $1 AND external_user = $2",
            channel_id, wa_user,
        )

    async def touch_channel(self, channel_id: str) -> None:
        try:
            pool = await self.pool()
            await pool.execute(
                "UPDATE channels SET last_event_at = now() WHERE id = $1",
                channel_id,
            )
        except Exception as e:
            logger.debug(f"channel touch skipped: {e}")

    # ── dispatch ─────────────────────────────────────────────────────────

    async def dispatch(self, payload: dict) -> None:
        """Runs after the webhook has already answered 200."""
        messages, statuses = meta_wa.normalize(payload)
        for status in statuses:
            channel = await self.channel_for(status.phone_number_id)
            if channel:
                await self.apply_status(status, channel["id"])
        for msg in messages:
            try:
                await self._dispatch_one(msg)
            except Exception as e:
                logger.exception(f"dispatch failed for {msg.wamid}: {e}")

    async def _dispatch_one(self, msg: meta_wa.InboundMessage) -> None:
        channel = await self.channel_for(msg.phone_number_id)
        if channel is None:
            logger.warning(f"no channel for number {msg.phone_number_id}")
            return
        if channel["status"] != "active" or \
                channel["deployment_status"] != "active":
            logger.info(f"channel {channel['id']} is not active, message dropped")
            return
        if not await self.claim_event(msg, channel["id"]):
            logger.info(f"duplicate delivery {msg.wamid} ignored")
            return
        # Claimed first, so a retry of the same stale message is a plain
        # duplicate; the webhook has already answered 200, so Meta stops.
        if is_stale(msg.timestamp, time.time()):
            age_h = (time.time() - msg.timestamp) / 3600
            logger.info(f"stale delivery {msg.wamid} ignored: sent {age_h:.1f}h ago")
            try:
                pool = await self.pool()
                await pool.execute(
                    "UPDATE channel_events SET status = 'ignored',"
                    " error = $2 WHERE wamid = $1",
                    msg.wamid, f"stale: sent {age_h:.1f} hours before delivery",
                )
            except Exception as e:
                logger.debug(f"stale mark skipped: {e}")
            return
        await self.touch_channel(channel["id"])
        thread = self.thread_for(channel, msg.wa_user, msg.name)
        thread.enqueue(msg)


bridge = Bridge()


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    await bridge.aclose()


app = FastAPI(title="Arya WhatsApp bridge", lifespan=_lifespan)


@app.get("/webhooks/whatsapp")
async def verify(request: Request):
    """Meta's one-time subscription handshake: echo hub.challenge back as
    plain text when the token matches any channel we hold. Matching across
    channels (rather than one configured token) is what lets several
    numbers share this endpoint."""
    params = request.query_params
    token = params.get("hub.verify_token") or ""
    challenge = params.get("hub.challenge") or ""
    if params.get("hub.mode") != "subscribe" or not token:
        return Response("bad request", status_code=400)
    pool = await bridge.pool()
    ok = await pool.fetchval(
        "SELECT 1 FROM channels WHERE secret->>'verify_token' = $1", token
    )
    if not ok:
        logger.warning("webhook verification refused: unknown verify token")
        return Response("forbidden", status_code=403)
    return Response(challenge, media_type="text/plain")


@app.post("/webhooks/whatsapp")
async def inbound(request: Request):
    """Verify, answer 200, then work.

    Meta retries anything that is not a 200 for up to seven days, so the
    response must not wait on the model. Everything after the signature
    check happens on a background task, and the dedupe on wamid makes a
    retry we did answer harmless.
    """
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError:
        return Response(status_code=400)

    phone_ids = {
        (change.get("value") or {}).get("metadata", {}).get("phone_number_id")
        for entry in payload.get("entry") or []
        for change in (entry or {}).get("changes") or []
    }
    channel = None
    for phone_id in phone_ids:
        if phone_id:
            channel = await bridge.channel_for(str(phone_id))
            if channel:
                break
    if channel is None:
        # Not a number we serve. 200 anyway: retrying will not help Meta,
        # and a 4xx here would have it hammering us for a week.
        logger.warning(f"webhook for unknown number(s) {phone_ids}")
        return Response(status_code=200)

    if not meta_wa.verify_signature(
        raw, request.headers.get("x-hub-signature-256"),
        channel["secret"].get("app_secret", ""),
    ):
        logger.warning(f"webhook signature rejected for channel {channel['id']}")
        return Response(status_code=403)

    _spawn(bridge.dispatch(payload))
    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"ok": True, "open_threads": bridge.open_threads}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=BRIDGE_PORT)
