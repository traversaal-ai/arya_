"""Spoken replies: when to send one, and what WhatsApp will actually play.

The format is not negotiable. WhatsApp renders a voice note, with a
waveform and inline playback, only for OGG/Opus sent with voice set; any
other container arrives as a file card with a download button, which is
not what a customer understands by a spoken reply. So these tests decode
the output rather than trusting the encoder call.
"""

import io
import math
import wave

import httpx
import pytest

import config
import media
import providers
import whatsapp_bridge as wb


def _wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            int(8000 * math.sin(i * 0.07)).to_bytes(2, "little", signed=True)
            for i in range(int(rate * seconds))
        ))
    return buf.getvalue()


def _endpoint(provider: str = "openai") -> dict:
    return providers.speech_endpoint(
        {"provider": provider}, {"openai": "sk-x", "groq": "gsk-x"}
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── when to speak ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "setting", "expected"),
    [
        # match: answer the way they asked.
        ("audio", "match", "voice"),
        ("text", "match", "text"),
        # The overrides ignore how they asked.
        ("audio", "voice", "voice"),
        ("text", "voice", "voice"),
        ("audio", "text", "text"),
        ("text", "text", "text"),
        # A photo is not speech, so match answers it in writing.
        ("image", "match", "text"),
        # The names the first build shipped with are still in stored rows.
        ("audio", "mirror", "voice"),
        ("text", "always", "voice"),
        ("audio", "off", "text"),
        # Unset or unrecognised falls back to the default, not to a guess.
        ("audio", "", "voice"),
        ("text", None, "text"),
        ("audio", "MATCH", "voice"),
        ("text", "nonsense", "text"),
    ],
)
def test_the_reply_mode_table(kind, setting, expected):
    assert wb.reply_mode(kind, setting) == expected


def test_a_reply_is_never_sent_both_ways():
    # The whole point of the revision: one or the other. If this ever
    # returns anything but one of two values, _flush_now has to change.
    assert {wb.reply_mode(k, m)
            for k in ("audio", "text", "image")
            for m in ("match", "text", "voice", "mirror", "always", "off")
            } == {"text", "voice"}


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("match", "match"), ("mirror", "match"), ("text", "text"),
     ("off", "text"), ("voice", "voice"), ("always", "voice"),
     ("", "match"), (None, "match"), ("  VOICE  ", "voice")],
)
def test_stored_settings_normalize_for_the_card(stored, expected):
    # The select has three options; a row written under the old names must
    # still match one of them or the control renders blank.
    assert config.normalize_voice_mode(stored) == expected


# ── what gets spoken ─────────────────────────────────────────────────────────


def test_urls_are_stripped_because_they_are_unusable_aloud():
    said = wb.speakable(
        "The paper is at https://arxiv.org/abs/2507.06122 and it is open access."
    )
    assert "arxiv" not in said
    assert "open access" in said
    assert "  " not in said  # the gap the URL left is closed up


def test_a_bare_www_address_is_stripped_too():
    assert "traversaal" not in wb.speakable("See www.traversaal.ai for more.")


def test_a_long_reply_falls_back_to_text():
    # Past about a minute of speech a voice note stops being worth
    # listening to. speakable returns nothing, and _flush_now reads that
    # as "send it in writing instead", never as "send nothing".
    assert wb.speakable("x" * (wb.SPEAK_LIMIT + 1)) == ""
    assert wb.speakable("x" * wb.SPEAK_LIMIT) != ""


def test_a_reply_that_was_only_a_link_falls_back_to_text():
    # An address read aloud is useless, so a reply that is nothing else
    # has to be written. The link itself also arrives separately, on the
    # links stream, whatever the reply mode is.
    assert wb.speakable("https://example.com/thing") == ""


def test_an_empty_reply_is_not_spoken():
    assert wb.speakable("") == ""
    assert wb.speakable("   ") == ""


# ── synthesis ────────────────────────────────────────────────────────────────


async def test_synthesis_posts_the_model_voice_and_format():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read()
        return httpx.Response(200, content=b"ID3fake",
                              headers={"content-type": "audio/mpeg"})

    ep = _endpoint()
    data, mime = await media.synthesize(ep, "hello there",
                                        client=_client(handler))
    assert data == b"ID3fake"
    assert mime == "audio/mpeg"
    assert seen["url"].endswith("/audio/speech")
    assert seen["auth"] == "Bearer sk-x"
    body = seen["body"]
    assert ep["tts_model"].encode() in body
    assert ep["voice"].encode() in body
    assert b"hello there" in body
    assert b"mp3" in body


async def test_groq_synthesis_asks_for_wav():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, content=b"RIFFfake")

    await media.synthesize(_endpoint("groq"), "hi", client=_client(handler))
    assert b"wav" in seen["body"]


async def test_synthesising_nothing_is_refused_before_any_request():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, content=b"x")

    with pytest.raises(media.MediaError):
        await media.synthesize(_endpoint(), "   ", client=_client(handler))
    assert called["n"] == 0


async def test_a_refusal_becomes_a_media_error():
    def handler(request):
        return httpx.Response(429, text="rate limited")

    with pytest.raises(media.MediaError) as e:
        await media.synthesize(_endpoint(), "hello", client=_client(handler))
    assert "rate limited" in str(e.value)


async def test_an_empty_body_is_not_passed_off_as_audio():
    def handler(request):
        return httpx.Response(200, content=b"")

    with pytest.raises(media.MediaError):
        await media.synthesize(_endpoint(), "hello", client=_client(handler))


# ── the format WhatsApp will play ────────────────────────────────────────────


def test_wav_becomes_an_ogg_opus_stream_whatsapp_can_render():
    import av

    ogg = media.to_ogg_opus(_wav())
    assert ogg.startswith(b"OggS")
    assert len(ogg) < len(_wav())  # Opus at 24 kbps beats raw PCM comfortably
    with av.open(io.BytesIO(ogg)) as container:
        stream = container.streams.audio[0]
        assert stream.codec_context.name == "opus"
        assert stream.codec_context.sample_rate == media.OPUS_SAMPLE_RATE
        assert stream.codec_context.layout.name == "mono"


def test_a_stereo_source_at_another_rate_is_still_converted():
    # Providers disagree on both, and WhatsApp accepts only the one shape.
    import av

    raw = io.BytesIO()
    with wave.open(raw, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x10" * 44100)
    ogg = media.to_ogg_opus(raw.getvalue())
    with av.open(io.BytesIO(ogg)) as container:
        ctx = container.streams.audio[0].codec_context
        assert (ctx.sample_rate, ctx.layout.name) == (media.OPUS_SAMPLE_RATE,
                                                      "mono")


def test_the_whole_recording_survives_the_encode():
    # The resampler and the encoder each need flushing; skipping either
    # truncates the end of the sentence, which is where the question is.
    import av

    ogg = media.to_ogg_opus(_wav(seconds=2.0))
    with av.open(io.BytesIO(ogg)) as container:
        frames = sum(f.samples for f in container.decode(audio=0))
    # 2 seconds at 48 kHz, allowing for Opus's own frame padding.
    assert frames >= int(1.9 * media.OPUS_SAMPLE_RATE)


def test_garbage_is_refused_with_a_reason():
    with pytest.raises(media.MediaError) as e:
        media.to_ogg_opus(b"this is not audio at all")
    assert "could not convert" in str(e.value)


def test_converting_nothing_is_refused():
    with pytest.raises(media.MediaError):
        media.to_ogg_opus(b"")
