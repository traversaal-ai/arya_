"""WhatsApp channel: signature checking, payload parsing, outbound shapes.

Everything here is offline. meta_wa is deliberately free of FastAPI, the
database and LiveKit precisely so the two things most likely to break in
production (a webhook we wrongly accept, and a payload shape we misread)
can be tested against recorded envelopes with no moving parts.

The Graph API is exercised through an httpx MockTransport, so the exact
JSON body Meta would receive is asserted rather than mocked away.
"""

import hashlib
import hmac
import json

import httpx
import pytest

import meta_wa

APP_SECRET = "s3cr3t-app-secret"
PHONE_ID = "106540352242922"
WA_USER = "16505551234"


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _envelope(*changes: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "102290129340398", "changes": list(changes)}],
    }


def _messages_change(*messages: dict, contacts: list | None = None) -> dict:
    return {
        "field": "messages",
        "value": {
            "messaging_product": "whatsapp",
            "metadata": {
                "display_phone_number": "15550783881",
                "phone_number_id": PHONE_ID,
            },
            "contacts": contacts if contacts is not None else [
                {"profile": {"name": "Sheena Nelson"}, "wa_id": WA_USER}
            ],
            "messages": list(messages),
        },
    }


# ── signature ────────────────────────────────────────────────────────────────


def test_signature_accepts_a_correct_digest():
    body = b'{"object":"whatsapp_business_account"}'
    assert meta_wa.verify_signature(body, _sign(body), APP_SECRET)


def test_signature_rejects_a_tampered_body():
    body = b'{"object":"whatsapp_business_account"}'
    header = _sign(body)
    assert not meta_wa.verify_signature(body + b" ", header, APP_SECRET)


def test_signature_rejects_the_wrong_secret():
    body = b"{}"
    assert not meta_wa.verify_signature(body, _sign(body, "other"), APP_SECRET)


@pytest.mark.parametrize("header", [None, "", "sha1=abc", "deadbeef"])
def test_signature_rejects_missing_or_malformed_headers(header):
    assert not meta_wa.verify_signature(b"{}", header, APP_SECRET)


def test_signature_without_a_configured_secret_never_passes():
    """A channel with no app secret must fail closed, not wave requests in."""
    body = b"{}"
    assert not meta_wa.verify_signature(body, _sign(body, ""), "")


# ── normalize ────────────────────────────────────────────────────────────────


def test_normalize_reads_a_text_message():
    payload = _envelope(_messages_change({
        "from": WA_USER,
        "id": "wamid.TEXT1",
        "timestamp": "1749416383",
        "type": "text",
        "text": {"body": "Does it come in another color?"},
    }))
    messages, statuses = meta_wa.normalize(payload)
    assert statuses == []
    assert len(messages) == 1
    msg = messages[0]
    assert msg.wamid == "wamid.TEXT1"
    assert msg.kind == "text"
    assert msg.text == "Does it come in another color?"
    assert msg.wa_user == WA_USER
    assert msg.name == "Sheena Nelson"
    assert msg.phone_number_id == PHONE_ID
    assert msg.timestamp == 1749416383
    assert not msg.is_media


def test_normalize_reads_an_image_with_its_caption():
    payload = _envelope(_messages_change({
        "from": WA_USER,
        "id": "wamid.IMG1",
        "timestamp": "1749416500",
        "type": "image",
        "image": {
            "caption": "Is this the right cable?",
            "mime_type": "image/jpeg",
            "sha256": "b0a1",
            "id": "1479537139650973",
        },
    }))
    msg = meta_wa.normalize(payload)[0][0]
    assert msg.kind == "image"
    assert msg.is_media
    assert msg.media_id == "1479537139650973"
    assert msg.mime == "image/jpeg"
    assert msg.text == "Is this the right cable?"
    assert msg.voice is False


def test_normalize_marks_a_recorded_voice_note():
    payload = _envelope(_messages_change({
        "from": WA_USER,
        "id": "wamid.AUD1",
        "type": "audio",
        "audio": {
            "mime_type": "audio/ogg; codecs=opus",
            "sha256": "9f2b",
            "id": "1087562541234567",
            "voice": True,
        },
    }))
    msg = meta_wa.normalize(payload)[0][0]
    assert msg.kind == "audio"
    assert msg.voice is True
    assert msg.mime == "audio/ogg; codecs=opus"


def test_normalize_distinguishes_an_attached_audio_file_from_a_voice_note():
    payload = _envelope(_messages_change({
        "from": WA_USER,
        "id": "wamid.AUD2",
        "type": "audio",
        "audio": {"mime_type": "audio/mpeg", "id": "222", "voice": False},
    }))
    assert meta_wa.normalize(payload)[0][0].voice is False


def test_normalize_reads_a_document_filename():
    payload = _envelope(_messages_change({
        "from": WA_USER,
        "id": "wamid.DOC1",
        "type": "document",
        "document": {
            "mime_type": "application/pdf",
            "id": "333",
            "filename": "invoice.pdf",
        },
    }))
    msg = meta_wa.normalize(payload)[0][0]
    assert msg.kind == "document"
    assert msg.filename == "invoice.pdf"


def test_normalize_turns_a_list_reply_into_the_tapped_title():
    """The title is what the customer saw, so it is what the agent should
    read; the id carries our routing payload alongside it."""
    payload = _envelope(_messages_change({
        "from": WA_USER,
        "id": "wamid.INT1",
        "type": "interactive",
        "interactive": {
            "type": "list_reply",
            "list_reply": {"id": "Option 2", "title": "Iberia, 433 dollars"},
        },
    }))
    msg = meta_wa.normalize(payload)[0][0]
    assert msg.kind == "interactive"
    assert msg.text == "Iberia, 433 dollars"
    assert msg.filename == "Option 2"


def test_normalize_reads_a_template_quick_reply():
    payload = _envelope(_messages_change({
        "from": WA_USER,
        "id": "wamid.BTN1",
        "type": "button",
        "button": {"text": "Yes, solved", "payload": "resolved_yes"},
    }))
    msg = meta_wa.normalize(payload)[0][0]
    assert msg.kind == "button"
    assert msg.text == "Yes, solved"


def test_normalize_keeps_the_quoted_message_id():
    payload = _envelope(_messages_change({
        "from": WA_USER,
        "id": "wamid.CTX1",
        "type": "text",
        "text": {"body": "this one"},
        "context": {"from": "15550783881", "id": "wamid.EARLIER"},
    }))
    assert meta_wa.normalize(payload)[0][0].context_wamid == "wamid.EARLIER"


def test_normalize_downgrades_unknown_types_rather_than_raising():
    """A type we cannot use must still be a message we can decline. Raising
    would mean a non-200, and Meta would retry it for a week."""
    payload = _envelope(_messages_change({
        "from": WA_USER, "id": "wamid.LOC1", "type": "location",
        "location": {"latitude": 1.0, "longitude": 2.0},
    }))
    msg = meta_wa.normalize(payload)[0][0]
    assert msg.kind == "unsupported"
    assert msg.wamid == "wamid.LOC1"


def test_normalize_reads_delivery_statuses_with_errors():
    payload = _envelope({
        "field": "messages",
        "value": {
            "metadata": {"phone_number_id": PHONE_ID},
            "statuses": [{
                "id": "wamid.OUT1",
                "status": "failed",
                "timestamp": "1749416700",
                "recipient_id": WA_USER,
                "errors": [{"code": 131047, "title": "Re-engagement message"}],
            }],
        },
    })
    messages, statuses = meta_wa.normalize(payload)
    assert messages == []
    assert len(statuses) == 1
    assert statuses[0].status == "failed"
    assert statuses[0].error == "Re-engagement message"
    assert statuses[0].recipient == WA_USER


def test_normalize_flattens_several_entries_and_messages():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {"changes": [_messages_change(
                {"from": WA_USER, "id": "a", "type": "text",
                 "text": {"body": "one"}},
                {"from": WA_USER, "id": "b", "type": "text",
                 "text": {"body": "two"}},
            )]},
            {"changes": [_messages_change(
                {"from": WA_USER, "id": "c", "type": "text",
                 "text": {"body": "three"}},
            )]},
        ],
    }
    messages, _ = meta_wa.normalize(payload)
    assert [m.wamid for m in messages] == ["a", "b", "c"]


def test_normalize_skips_messages_with_no_id():
    payload = _envelope(_messages_change({"from": WA_USER, "type": "text"}))
    assert meta_wa.normalize(payload)[0] == []


@pytest.mark.parametrize("payload", [
    {}, {"entry": None}, {"entry": [{}]}, {"entry": [{"changes": [{}]}]},
    [], "nonsense", None,
])
def test_normalize_survives_shapes_it_has_never_seen(payload):
    assert meta_wa.normalize(payload) == ([], [])


def test_normalize_handles_a_missing_contact_profile():
    payload = _envelope(_messages_change(
        {"from": WA_USER, "id": "x", "type": "text", "text": {"body": "hi"}},
        contacts=[],
    ))
    assert meta_wa.normalize(payload)[0][0].name is None


# ── split_text ───────────────────────────────────────────────────────────────


def test_split_text_leaves_a_normal_answer_alone():
    assert meta_wa.split_text("short answer") == ["short answer"]


def test_split_text_drops_an_empty_body():
    assert meta_wa.split_text("   ") == []


def test_split_text_breaks_on_a_paragraph_boundary():
    body = ("a" * 40) + "\n\n" + ("b" * 40)
    assert meta_wa.split_text(body, limit=50) == ["a" * 40, "b" * 40]


def test_split_text_never_cuts_mid_word():
    body = " ".join(["word"] * 60)
    chunks = meta_wa.split_text(body, limit=100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(c.replace(" ", "") for c in chunks) == body.replace(" ", "")


def test_split_text_handles_one_unbroken_run():
    chunks = meta_wa.split_text("x" * 250, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 50]


# ── MetaClient ───────────────────────────────────────────────────────────────


def _client(handler) -> meta_wa.MetaClient:
    transport = httpx.MockTransport(handler)
    return meta_wa.MetaClient(
        PHONE_ID, "TOKEN", api_version="v23.0",
        client=httpx.AsyncClient(transport=transport),
    )


def _sent_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"messages": [{"id": "wamid.SENT"}]})


async def test_send_text_posts_the_documented_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _sent_ok(request)

    client = _client(handler)
    wamid = await client.send_text(WA_USER, "hello", context_wamid="wamid.Q")
    await client.aclose()

    assert wamid == "wamid.SENT"
    assert seen["url"].endswith(f"/v23.0/{PHONE_ID}/messages")
    assert seen["auth"] == "Bearer TOKEN"
    assert seen["body"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": WA_USER,
        "type": "text",
        "text": {"preview_url": True, "body": "hello"},
        "context": {"message_id": "wamid.Q"},
    }


async def test_send_audio_marks_a_voice_note():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _sent_ok(request)

    client = _client(handler)
    await client.send_audio(WA_USER, "MEDIA1")
    await client.aclose()
    assert seen["body"]["audio"] == {"id": "MEDIA1", "voice": True}


async def test_send_image_by_public_link_with_caption():
    # Meta fetches the link itself: no upload, the picture is wherever it
    # is publicly hosted (Unsplash, a public bucket).
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _sent_ok(request)

    client = _client(handler)
    await client.send_image(
        WA_USER, link="https://img.example/washer.jpg",
        caption="Whirlpool top load, $599.00",
    )
    await client.aclose()
    assert seen["body"]["type"] == "image"
    assert seen["body"]["image"] == {
        "link": "https://img.example/washer.jpg",
        "caption": "Whirlpool top load, $599.00",
    }
    client = _client(_sent_ok)
    with pytest.raises(meta_wa.MetaError):
        await client.send_image(WA_USER)
    await client.aclose()


async def test_interactive_list_clips_to_whatsapp_limits():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _sent_ok(request)

    rows = [
        {"id": "pick-" + str(i), "title": "T" * 40, "description": "D" * 100}
        for i in range(14)
    ]
    client = _client(handler)
    await client.send_interactive_list(
        WA_USER, body="Pick one", rows=rows, button="Choose one option please",
    )
    await client.aclose()

    action = seen["body"]["interactive"]["action"]
    packed = action["sections"][0]["rows"]
    assert len(packed) == meta_wa.LIST_ROW_LIMIT
    assert len(action["button"]) <= meta_wa.LIST_BUTTON_LIMIT
    assert all(len(r["title"]) <= meta_wa.LIST_TITLE_LIMIT for r in packed)
    assert all(len(r["description"]) <= meta_wa.LIST_DESC_LIMIT for r in packed)


async def test_interactive_list_refuses_to_send_nothing():
    client = _client(_sent_ok)
    with pytest.raises(meta_wa.MetaError):
        await client.send_interactive_list(WA_USER, body="x", rows=[])
    await client.aclose()


async def test_a_client_error_is_raised_with_metas_own_wording():
    def handler(request):
        return httpx.Response(400, json={"error": {
            "message": "Recipient phone number not in allowed list",
            "code": 131030,
        }})

    client = _client(handler)
    with pytest.raises(meta_wa.MetaError) as excinfo:
        await client.send_text(WA_USER, "hi")
    await client.aclose()
    assert "not in allowed list" in str(excinfo.value)
    assert excinfo.value.code == 131030
    assert excinfo.value.retryable is False


async def test_a_server_error_is_retried_then_surfaces():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"message": "oops"}})

    client = _client(handler)
    with pytest.raises(meta_wa.MetaError) as excinfo:
        await client._request("POST", "x", attempts=2, json={})
    await client.aclose()
    assert calls["n"] == 2
    assert excinfo.value.retryable is True


async def test_download_media_follows_the_two_hop_flow_and_checks_the_digest():
    blob = b"\x00\x01binary audio"
    digest = hashlib.sha256(blob).hexdigest()
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path.endswith("/MEDIA1"):
            return httpx.Response(200, json={
                "url": "https://lookaside.fbsbx.com/x?mid=1",
                "mime_type": "audio/ogg; codecs=opus",
                "sha256": digest,
                "file_size": len(blob),
            })
        assert request.headers.get("authorization") == "Bearer TOKEN"
        return httpx.Response(200, content=blob)

    client = _client(handler)
    data, mime = await client.download_media("MEDIA1")
    await client.aclose()

    assert data == blob
    assert mime == "audio/ogg"          # parameters stripped
    assert len(seen) == 2
    assert "lookaside" in seen[1]


async def test_download_media_rejects_a_truncated_file():
    def handler(request):
        if request.url.path.endswith("/MEDIA2"):
            return httpx.Response(200, json={
                "url": "https://lookaside.fbsbx.com/x",
                "mime_type": "image/jpeg",
                "sha256": hashlib.sha256(b"the whole thing").hexdigest(),
            })
        return httpx.Response(200, content=b"half")

    client = _client(handler)
    with pytest.raises(meta_wa.MetaError):
        await client.download_media("MEDIA2")
    await client.aclose()


async def test_mark_read_never_raises():
    """Read receipts are cosmetic; a failure must not cost the answer."""
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "nope"}})

    client = _client(handler)
    await client.mark_read("wamid.X", typing=True)   # must not raise
    await client.aclose()


def test_api_version_is_normalised():
    assert meta_wa.MetaClient(PHONE_ID, "t", api_version="23.0").api_version \
        == "v23.0"
    assert meta_wa.MetaClient(PHONE_ID, "t", api_version="v24.0").api_version \
        == "v24.0"


# ── thread registry ──────────────────────────────────────────────────────────
# The bridge keeps one Thread per (channel, customer). Getting the handover
# wrong loses a message with no error anywhere, which is the worst possible
# failure for a support channel, so the three cases are pinned here.

import whatsapp_bridge  # noqa: E402


def _channel() -> dict:
    return {"id": "chan-abc", "config": {}, "secret": {}}


async def test_registry_reuses_a_live_thread():
    bridge = whatsapp_bridge.Bridge()
    try:
        first = bridge.thread_for(_channel(), WA_USER, "Ada")
        assert bridge.thread_for(_channel(), WA_USER, "Ada") is first
    finally:
        await bridge._http.aclose()


async def test_registry_replaces_a_finished_thread():
    """A thread that has timed out must never be handed out again: its
    queue has no reader, so anything enqueued there is lost silently."""
    bridge = whatsapp_bridge.Bridge()
    try:
        first = bridge.thread_for(_channel(), WA_USER, "Ada")
        first._done = True
        assert bridge.thread_for(_channel(), WA_USER, "Ada") is not first
    finally:
        await bridge._http.aclose()


async def test_forget_leaves_a_replacement_alone():
    """The timed-out thread cleans up after the replacement is already
    registered, so it must remove itself by identity, not by key."""
    bridge = whatsapp_bridge.Bridge()
    try:
        first = bridge.thread_for(_channel(), WA_USER, "Ada")
        first._done = True
        second = bridge.thread_for(_channel(), WA_USER, "Ada")
        bridge.forget(first)
        assert bridge.thread_for(_channel(), WA_USER, "Ada") is second
    finally:
        await bridge._http.aclose()


async def test_editing_a_channel_reaches_an_open_thread():
    bridge = whatsapp_bridge.Bridge()
    try:
        thread = bridge.thread_for(_channel(), WA_USER, "Ada")
        updated = {"id": "chan-abc", "config": {"idle_minutes": 30},
                   "secret": {}}
        bridge.thread_for(updated, WA_USER, "Ada")
        assert thread.idle_s == 1800
    finally:
        await bridge._http.aclose()
