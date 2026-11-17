"""Media conversions for the messaging channels.

Pure functions over bytes. No FastAPI, no LiveKit, no database, and no
network beyond an endpoint the caller hands in, for the same reason
meta_wa is kept that way: the things most likely to break here are a
photo some phone camera produced and a codec a provider changed, and
both are only testable against real bytes with nothing else running.

The bridge does all media work, never the worker. Downloading, resizing,
transcribing and encoding are network and CPU bound, and doing them on
the worker's turn path would add latency to every conversation that
carries no media at all.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import wave

import httpx
from PIL import Image, ImageOps

logger = logging.getLogger("media")

# WhatsApp caps an image at 5 MB on its own side, so anything larger is a
# malformed download rather than a photo somebody sent.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# WhatsApp's own audio ceiling. A voice note that long is a recording
# somebody sent by accident, and transcribing it would cost minutes of
# the customer's patience as well as the money.
MAX_AUDIO_BYTES = 16 * 1024 * 1024

# Speech calls are slow and worth waiting for. Learned from the local TTS
# path: a short read timeout turns a busy Speaches container into a lost
# answer rather than a late one.
SPEECH_TIMEOUT = httpx.Timeout(10.0, read=60.0)

# WhatsApp renders a voice note only for OGG/Opus; anything else shows as
# a file card with a download button, which is not what "reply by voice"
# means to a customer. 48 kHz mono is what Opus encodes natively, and
# 24 kbps voip is transparent for speech at a fraction of the bytes.
OPUS_SAMPLE_RATE = 48000
OPUS_BITRATE = 24000

# Roughly a 1500 px long edge. Every vision API downsamples beyond about
# this anyway, so a 12 megapixel phone photo only buys upload time.
DEFAULT_MAX_PX = 1500
DEFAULT_QUALITY = 85


class MediaError(RuntimeError):
    """Something arrived that cannot be used, with a reason worth showing
    to an operator in the event log. Never raised with a customer's words
    in it."""


def shrink_image(
    data: bytes,
    *,
    max_px: int = DEFAULT_MAX_PX,
    quality: int = DEFAULT_QUALITY,
) -> tuple[bytes, int, int]:
    """A phone photo down to something worth sending to a vision model.

    Returns JPEG bytes and the final width and height.

    Three corrections happen here, each for a reason seen in real photos:
    EXIF orientation is applied rather than carried, because the tag is
    lost on re-encode and a sideways photo of a serial number is
    unreadable; transparency is flattened onto white, because a screenshot
    with an alpha channel otherwise arrives as black text on black; and
    anything that is not RGB is converted, because JPEG cannot hold it.
    """
    if not data:
        raise MediaError("the image was empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise MediaError(
            f"the image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
        )
    try:
        img = Image.open(io.BytesIO(data))
        # Forces a decode, so a truncated or mislabelled file fails here
        # rather than halfway through the resize.
        img = ImageOps.exif_transpose(img) or img
    except Exception as e:
        raise MediaError(f"that file is not a readable image: {e}") from None

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[-1])
        img = flat
    elif img.mode != "RGB":
        img = img.convert("RGB")

    width, height = img.size
    if max(width, height) > max_px:
        scale = max_px / float(max(width, height))
        img = img.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.LANCZOS,
        )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), img.size[0], img.size[1]


# ── speech ───────────────────────────────────────────────────────────────────


def _speech_headers(endpoint: dict) -> dict:
    """Bearer unless the endpoint is a local one, which takes any string
    and is handed the literal "not-needed" by providers.speech_endpoint."""
    key = endpoint.get("api_key") or ""
    return {"Authorization": f"Bearer {key}"} if key and key != "not-needed" else {}


async def transcribe(
    endpoint: dict,
    data: bytes,
    *,
    mime: str = "audio/ogg",
    filename: str = "note.ogg",
    client: httpx.AsyncClient | None = None,
) -> str:
    """A voice note into words, through whichever provider the agent uses.

    Posts to the OpenAI-shaped /audio/transcriptions route, which OpenAI,
    Groq and Speaches all serve. WhatsApp sends OGG/Opus and all three
    accept it directly, so nothing is decoded on the way; that is the
    whole reason this needs no audio library.

    Returns the transcript, which may legitimately be empty when the
    recording held no speech. Raises MediaError on anything else.
    """
    if not data:
        raise MediaError("the voice note was empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise MediaError(
            f"the voice note is larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB"
        )
    if endpoint.get("provider") == "gemini":
        return await _gemini_transcribe(endpoint, data, mime or "audio/ogg")
    owned = client is None
    http = client or httpx.AsyncClient(timeout=SPEECH_TIMEOUT)
    try:
        resp = await http.post(
            endpoint["base_url"].rstrip("/") + "/audio/transcriptions",
            headers=_speech_headers(endpoint),
            files={"file": (filename, data, mime or "audio/ogg")},
            data={"model": endpoint["stt_model"], "response_format": "json"},
        )
    except httpx.HTTPError as e:
        raise MediaError(f"could not reach the speech service: {e}") from None
    finally:
        if owned:
            await http.aclose()
    if resp.status_code >= 400:
        raise MediaError(
            f"the speech service refused the recording:"
            f" HTTP {resp.status_code} {resp.text[:200]}"
        )
    try:
        return str((resp.json() or {}).get("text") or "").strip()
    except ValueError:
        # Some servers answer text/plain when asked for json.
        return resp.text.strip()


async def synthesize(
    endpoint: dict,
    text: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[bytes, str]:
    """Words into audio, through whichever provider the agent uses.

    Returns the raw bytes and their mime type, in whatever container the
    provider chose (mp3 on OpenAI, wav on Groq and Speaches). Converting
    to what WhatsApp wants is to_ogg_opus's job, kept separate so a
    provider changing its default format cannot break the send path.
    """
    body = (text or "").strip()
    if not body:
        raise MediaError("there was nothing to say")
    if endpoint.get("provider") == "gemini":
        return await _gemini_synthesize(endpoint, body)
    owned = client is None
    http = client or httpx.AsyncClient(timeout=SPEECH_TIMEOUT)
    try:
        resp = await http.post(
            endpoint["tts_base_url"].rstrip("/") + "/audio/speech",
            headers={**_speech_headers(endpoint),
                     "Content-Type": "application/json"},
            json={
                "model": endpoint["tts_model"],
                "voice": endpoint["voice"],
                "input": body,
                "response_format": endpoint.get("tts_format", "mp3"),
            },
        )
    except httpx.HTTPError as e:
        raise MediaError(f"could not reach the speech service: {e}") from None
    finally:
        if owned:
            await http.aclose()
    if resp.status_code >= 400:
        raise MediaError(
            f"the speech service refused the text:"
            f" HTTP {resp.status_code} {resp.text[:200]}"
        )
    data = resp.content
    if not data:
        raise MediaError("the speech service returned no audio")
    return data, resp.headers.get("content-type", "audio/mpeg").split(";")[0]


# ── Gemini: Google's speech routes are not OpenAI-shaped ─────────────────────
# Both calls go through the google-genai SDK the Gemini voice plugin already
# depends on, so the image grows by nothing. Imported inside the functions:
# only a Gemini agent's voice notes ever need the SDK.

TRANSCRIBE_PROMPT = (
    "Transcribe this recording exactly as spoken, in the language it is"
    " spoken in. Output only the transcript, with no introduction, labels"
    " or commentary. If it contains no speech, output nothing."
)


def _genai_client(endpoint: dict):
    from google import genai

    return genai.Client(api_key=endpoint.get("api_key") or "")


async def _gemini_transcribe(endpoint: dict, data: bytes, mime: str) -> str:
    """One finished recording into words, with a chat model that accepts
    audio (the Live transcription model only speaks the Live API)."""
    from google.genai import types

    client = _genai_client(endpoint)
    try:
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=endpoint["stt_model"],
                contents=[
                    types.Part.from_bytes(data=data, mime_type=mime),
                    TRANSCRIBE_PROMPT,
                ],
            ),
            SPEECH_TIMEOUT.read,
        )
    except Exception as e:
        raise MediaError(f"Gemini could not transcribe the recording: {e}") from None
    return str(getattr(resp, "text", "") or "").strip()


def pcm_to_wav(pcm: bytes, rate: int, channels: int = 1, width: int = 2) -> bytes:
    """Gemini TTS returns bare 16-bit PCM; PyAV wants a container to probe."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _pcm_rate(mime: str, default: int = 24000) -> int:
    m = re.search(r"rate=(\d+)", mime or "")
    return int(m.group(1)) if m else default


async def _gemini_synthesize(endpoint: dict, body: str) -> tuple[bytes, str]:
    """Words into audio with Gemini TTS, in the agent's voice, with its
    expressive settings as a short bracketed tag in front of the text
    (providers.gemini_style explains why a tag and not a sentence).
    Returns wav, so to_ogg_opus needs no special case."""
    from google.genai import types

    client = _genai_client(endpoint)
    style = str(endpoint.get("style") or "").strip()
    prompt = f"{style} {body}" if style else body
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=endpoint["voice"]
                )
            )
        ),
    )
    try:
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=endpoint["tts_model"], contents=prompt, config=config
            ),
            SPEECH_TIMEOUT.read,
        )
    except Exception as e:
        raise MediaError(f"Gemini could not speak the reply: {e}") from None
    try:
        part = resp.candidates[0].content.parts[0]
        audio = part.inline_data.data
        mime = part.inline_data.mime_type or ""
    except (AttributeError, IndexError, TypeError):
        raise MediaError("Gemini returned no audio") from None
    if not audio:
        raise MediaError("Gemini returned no audio")
    # The 3.8 TTS models answer with a complete WAV file (mime audio/wav),
    # not bare samples as the Live API does. Wrapping that in a second WAV
    # header played its trailing chunks as noise at the end of every voice
    # note (heard live). A file is passed through; only bare PCM is wrapped.
    if audio[:4] == b"RIFF" or mime.split(";")[0].strip() == "audio/wav":
        return audio, "audio/wav"
    return pcm_to_wav(audio, _pcm_rate(mime)), "audio/wav"


def to_ogg_opus(data: bytes) -> bytes:
    """Any container the providers produce into the one WhatsApp plays.

    Deliberately container agnostic on the way in: PyAV probes the bytes,
    so mp3 from OpenAI and wav from Groq or Speaches both work and a
    provider silently changing its default cannot break this.

    In process with PyAV rather than shelling out to ffmpeg, which is not
    in the image and would roughly double it for this one conversion.
    """
    if not data:
        raise MediaError("there was no audio to convert")
    # Imported here, not at module scope: PyAV costs about a tenth of a
    # second to import and only the spoken-reply path ever needs it.
    import av

    out = io.BytesIO()
    try:
        with av.open(io.BytesIO(data)) as src:
            in_stream = next(
                (s for s in src.streams if s.type == "audio"), None
            )
            if in_stream is None:
                raise MediaError("that audio has no sound in it")
            with av.open(out, mode="w", format="ogg") as dst:
                dst_stream = dst.add_stream(
                    "libopus", rate=OPUS_SAMPLE_RATE,
                    layout="mono", bit_rate=OPUS_BITRATE,
                )
                resampler = av.AudioResampler(
                    format="s16", layout="mono", rate=OPUS_SAMPLE_RATE
                )
                for frame in src.decode(in_stream):
                    for resampled in resampler.resample(frame):
                        for packet in dst_stream.encode(resampled):
                            dst.mux(packet)
                # Flush the resampler, then the encoder. Skipping either
                # truncates the last fraction of a second, which is where
                # the end of the sentence lives.
                for resampled in resampler.resample(None):
                    for packet in dst_stream.encode(resampled):
                        dst.mux(packet)
                for packet in dst_stream.encode(None):
                    dst.mux(packet)
    except MediaError:
        raise
    except Exception as e:
        raise MediaError(f"could not convert that audio: {e}") from None

    encoded = out.getvalue()
    if not encoded.startswith(b"OggS"):
        raise MediaError("the converted audio is not a valid Ogg stream")
    return encoded
