"""Voice notes into words.

The request shape matters more than it looks: all three providers serve
the same OpenAI-shaped route but disagree about the model name and the
authorization, and getting either wrong fails at the customer rather than
at startup. WhatsApp sends OGG/Opus, which all three accept directly, so
these tests also pin down that nothing is decoded on the way.
"""

import httpx
import pytest

import catalog
import media
import providers
import whatsapp_bridge

OGG = b"OggS" + b"\x00" * 64


def _endpoint(provider: str = "openai") -> dict:
    return providers.speech_endpoint(
        {"provider": provider}, {"openai": "sk-x", "groq": "gsk-x"}
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_voice_note_is_posted_as_multipart_with_the_model():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "  where is my order  "})

    text = await media.transcribe(_endpoint(), OGG, client=_client(handler))
    assert text == "where is my order"
    assert seen["url"].endswith("/audio/transcriptions")
    assert seen["auth"] == "Bearer sk-x"
    body = seen["body"]
    assert OGG in body
    assert b"note.ogg" in body
    assert catalog.get("openai_defaults")["stt_model"].encode() in body
    assert b"response_format" in body and b"json" in body


async def test_groq_uses_its_own_root_and_model():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "hello"})

    await media.transcribe(_endpoint("groq"), OGG, client=_client(handler))
    assert seen["url"].startswith(providers.GROQ_BASE_URL)
    assert seen["auth"] == "Bearer gsk-x"
    assert catalog.get("groq_defaults")["stt_model"].encode() in seen["body"]


async def test_a_local_endpoint_is_not_sent_a_bearer_token():
    # Speaches takes any string, and "not-needed" is a placeholder, not a
    # credential. Sending it as a Bearer header is misleading at best.
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"text": "hi"})

    await media.transcribe(
        _endpoint("local"), OGG, client=_client(handler)
    )
    assert seen["auth"] is None


async def test_the_customers_mime_type_is_carried_through():
    # WhatsApp labels a held-mic note audio/ogg; codecs parameters are
    # already stripped upstream by download_media.
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "ok"})

    await media.transcribe(
        _endpoint(), OGG, mime="audio/mpeg", filename="clip.mp3",
        client=_client(handler),
    )
    assert b"audio/mpeg" in seen["body"]
    assert b"clip.mp3" in seen["body"]


async def test_silence_comes_back_as_an_empty_string_not_an_error():
    # A pocket recording is a normal event, and the bridge answers it with
    # its own wording rather than treating it as a failure.
    def handler(request):
        return httpx.Response(200, json={"text": "   "})

    assert await media.transcribe(_endpoint(), OGG, client=_client(handler)) == ""


async def test_a_refusal_is_raised_with_the_services_reason():
    def handler(request):
        return httpx.Response(400, text="unsupported file format")

    with pytest.raises(media.MediaError) as e:
        await media.transcribe(_endpoint(), OGG, client=_client(handler))
    assert "unsupported file format" in str(e.value)


async def test_a_network_failure_is_a_media_error_not_an_httpx_one():
    # The bridge catches MediaError to decide what the customer is told;
    # a raw httpx exception would escape that and kill the thread.
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(media.MediaError) as e:
        await media.transcribe(_endpoint(), OGG, client=_client(handler))
    assert "could not reach" in str(e.value)


async def test_a_plain_text_answer_is_accepted():
    # Some OpenAI-compatible servers ignore response_format and reply with
    # the bare transcript.
    def handler(request):
        return httpx.Response(200, text="just the words")

    got = await media.transcribe(_endpoint(), OGG, client=_client(handler))
    assert got == "just the words"


async def test_an_empty_recording_is_refused_before_any_request():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json={"text": ""})

    with pytest.raises(media.MediaError):
        await media.transcribe(_endpoint(), b"", client=_client(handler))
    assert called["n"] == 0


async def test_an_oversized_recording_is_refused_before_any_request():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json={"text": ""})

    with pytest.raises(media.MediaError) as e:
        await media.transcribe(
            _endpoint(), b"0" * (media.MAX_AUDIO_BYTES + 1),
            client=_client(handler),
        )
    assert "larger than" in str(e.value)
    assert called["n"] == 0


def test_the_transcript_tag_is_what_the_prompt_block_describes():
    # The bridge writes this tag and channel_media_rules explains it. If
    # they drift, the model starts reading the tag out to customers.
    assert whatsapp_bridge.VOICE_PREFIX.strip() == "[voice note]"
    assert "[voice note]" in catalog.SEEDS["prompt_blocks"]["channel_media_rules"]
    assert "[photo]" in catalog.SEEDS["prompt_blocks"]["channel_media_rules"]


def test_the_voice_refusals_are_distinct_and_house_style():
    assert whatsapp_bridge.VOICE_EMPTY_TEXT != whatsapp_bridge.VOICE_BROKEN_TEXT
    for note in (whatsapp_bridge.VOICE_EMPTY_TEXT,
                 whatsapp_bridge.VOICE_BROKEN_TEXT):
        assert note.strip()
        assert "—" not in note
