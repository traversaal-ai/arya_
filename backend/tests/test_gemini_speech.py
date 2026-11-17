"""Voice notes on a Gemini agent: the bridge speaks and listens through
Google's own API, in the agent's voice, with its expressive settings as a
spoken direction. Before this a Gemini agent's WhatsApp voice notes went
through OpenAI, flat, and the 3.8 TTS the operator had chosen was never
heard outside a call. The SDK is faked here; the real call shape is in
media._gemini_synthesize and the wav it returns must still convert to the
OGG/Opus WhatsApp plays."""

import math
import types as pytypes

import pytest

import media
import providers

KEYS = {"gemini": "AIza-test", "openai": "sk-x"}


def test_a_gemini_agent_gets_a_gemini_speech_endpoint():
    ep = providers.speech_endpoint({"provider": "gemini"}, KEYS)
    assert ep["provider"] == "gemini"
    assert ep["api_key"] == "AIza-test"
    assert ep["tts_model"].startswith("gemini-") and ep["voice"]
    assert ep["stt_model"] == "gemini-3.5-flash"
    assert ep["style"] == ""  # expressive is off by default


def test_expressive_settings_become_a_short_bracketed_tag():
    # A sentence in front of the text gets read aloud by the 3.8 TTS
    # (measured: +2 to +15 seconds of narration); a bracketed tag does not.
    ep = providers.speech_endpoint(
        {"provider": "gemini", "gemini": {"expressive": True}}, KEYS
    )
    style = ep["style"]
    assert style.startswith("[") and style.endswith("]")
    assert "warm" in style and "no fillers" in style and "no laughter" in style
    assert "natural breaths" in style  # breathing is on in the defaults
    assert "normal" not in style  # the default pace is not mentioned
    assert "Keep the delivery" not in style  # free text would be spoken
    fast = providers.gemini_style({"speech_steering": {"pace": "fast"}})
    assert "fast pace" in fast and "no fillers" in fast


class _FakeModels:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def generate_content(self, **kw):
        self.calls.append(kw)
        return self.resp


def _fake_client(resp):
    return pytypes.SimpleNamespace(aio=pytypes.SimpleNamespace(models=_FakeModels(resp)))


def _pcm(rate: int = 24000, seconds: float = 0.5) -> bytes:
    return b"".join(
        int(8000 * math.sin(i * 0.05)).to_bytes(2, "little", signed=True)
        for i in range(int(rate * seconds))
    )


async def test_synthesis_uses_the_voice_and_style_and_returns_wav(monkeypatch):
    inline = pytypes.SimpleNamespace(data=_pcm(), mime_type="audio/L16;codec=pcm;rate=24000")
    resp = pytypes.SimpleNamespace(candidates=[pytypes.SimpleNamespace(
        content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(inline_data=inline)]))])
    fake = _fake_client(resp)
    monkeypatch.setattr(media, "_genai_client", lambda ep: fake)

    ep = {"provider": "gemini", "api_key": "k", "tts_model": "gemini-3.8-flash-tts",
          "voice": "Zephyr", "style": "[warm, no fillers]"}
    data, mime = await media.synthesize(ep, "Your order is on its way.")
    assert mime == "audio/wav" and data[:4] == b"RIFF"
    call = fake.aio.models.calls[0]
    assert call["model"] == "gemini-3.8-flash-tts"
    assert call["contents"] == "[warm, no fillers] Your order is on its way."
    voice = call["config"].speech_config.voice_config.prebuilt_voice_config.voice_name
    assert voice == "Zephyr"
    # And the wav converts to what WhatsApp plays.
    ogg = media.to_ogg_opus(data)
    assert ogg[:4] == b"OggS"


async def test_a_complete_wav_from_gemini_is_passed_through_untouched(monkeypatch):
    # The 3.8 TTS models answer with a whole WAV file. Wrapping it again
    # played its trailing bytes as noise at the end of every voice note
    # (heard live); the file must go through as it is.
    wav = media.pcm_to_wav(_pcm(), 24000) + b"LIST\x04\x00\x00\x00INFO"
    inline = pytypes.SimpleNamespace(data=wav, mime_type="audio/wav")
    resp = pytypes.SimpleNamespace(candidates=[pytypes.SimpleNamespace(
        content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(inline_data=inline)]))])
    monkeypatch.setattr(media, "_genai_client", lambda ep: _fake_client(resp))
    ep = {"provider": "gemini", "api_key": "k", "tts_model": "m", "voice": "Kore", "style": ""}
    data, mime = await media.synthesize(ep, "Hello")
    assert data == wav and mime == "audio/wav"
    assert media.to_ogg_opus(data)[:4] == b"OggS"


async def test_synthesis_without_style_sends_the_text_alone(monkeypatch):
    inline = pytypes.SimpleNamespace(data=b"\x00\x00" * 100, mime_type="audio/L16;rate=16000")
    resp = pytypes.SimpleNamespace(candidates=[pytypes.SimpleNamespace(
        content=pytypes.SimpleNamespace(parts=[pytypes.SimpleNamespace(inline_data=inline)]))])
    fake = _fake_client(resp)
    monkeypatch.setattr(media, "_genai_client", lambda ep: fake)
    ep = {"provider": "gemini", "api_key": "k", "tts_model": "m", "voice": "Kore", "style": ""}
    data, _ = await media.synthesize(ep, "Hello")
    assert fake.aio.models.calls[0]["contents"] == "Hello"
    assert media._pcm_rate("audio/L16;rate=16000") == 16000 and data[:4] == b"RIFF"


async def test_no_audio_back_is_a_media_error(monkeypatch):
    resp = pytypes.SimpleNamespace(candidates=[])
    monkeypatch.setattr(media, "_genai_client", lambda ep: _fake_client(resp))
    ep = {"provider": "gemini", "api_key": "k", "tts_model": "m", "voice": "Kore"}
    with pytest.raises(media.MediaError):
        await media.synthesize(ep, "Hello")


async def test_transcription_sends_the_recording_and_returns_the_words(monkeypatch):
    resp = pytypes.SimpleNamespace(text="  what is the status of my return  ")
    fake = _fake_client(resp)
    monkeypatch.setattr(media, "_genai_client", lambda ep: fake)
    ep = {"provider": "gemini", "api_key": "k", "stt_model": "gemini-3.5-flash"}
    out = await media.transcribe(ep, b"OggS\x00fake", mime="audio/ogg")
    assert out == "what is the status of my return"
    call = fake.aio.models.calls[0]
    assert call["model"] == "gemini-3.5-flash"
    part, prompt = call["contents"]
    assert prompt == media.TRANSCRIBE_PROMPT
    assert part.inline_data.mime_type == "audio/ogg"


async def test_a_failed_gemini_call_degrades_to_a_media_error(monkeypatch):
    class Broken:
        async def generate_content(self, **kw):
            raise RuntimeError("quota")

    client = pytypes.SimpleNamespace(aio=pytypes.SimpleNamespace(models=Broken()))
    monkeypatch.setattr(media, "_genai_client", lambda ep: client)
    ep = {"provider": "gemini", "api_key": "k", "stt_model": "m"}
    with pytest.raises(media.MediaError):
        await media.transcribe(ep, b"x", mime="audio/ogg")
