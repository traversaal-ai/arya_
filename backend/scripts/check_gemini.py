"""Probe the Gemini provider stage by stage with a real key.

Answers "is the key valid, and which of our catalog models does it reach"
without placing a call. Uses the SAME plugin classes providers.py builds
(beta.GeminiSTT, google.LLM's client, beta.GeminiTTS), so a pass here means
the provider path works and a failure names the stage and Google's reason.

Run from backend/:

    PYTHONPATH=src .venv/bin/python scripts/check_gemini.py            # key from keys.json
    PYTHONPATH=src .venv/bin/python scripts/check_gemini.py AIza...    # explicit key

Exit code 0 when every stage passes, 1 otherwise.
"""

import asyncio
import sys
import time

from google import genai
from livekit import rtc
from livekit.agents import APIConnectOptions
from livekit.agents import llm as lk_llm
from livekit.plugins import google

import catalog
import config

FAST = APIConnectOptions(max_retry=0, timeout=20)


def mask(key: str) -> str:
    return config.mask_secret(key) if key else "(none)"


def short(err: BaseException) -> str:
    text = str(err).strip().replace("\n", " ")
    return text[:220]


def check_key_and_models(key: str, wanted: list[str]) -> bool:
    """models.list() is the cheapest possible validity check: it needs the
    key and the Generative Language API, nothing else."""
    print("\n[0] key + model listing")
    # models.list() is a lazy pager: the client must outlive the iteration,
    # so it cannot be a temporary in the same expression.
    client = genai.Client(api_key=key)
    try:
        names = {m.name.removeprefix("models/") for m in client.models.list()}
    except Exception as e:
        print(f"    FAIL  models.list(): {short(e)}")
        return False
    print(f"    OK    key accepted, {len(names)} models visible")
    for w in wanted:
        # Live transcription models are not always listed; that alone is
        # not a failure, stage [3] is the real test for them.
        print(f"    {'listed ' if w in names else 'missing'}  {w}")
    return True


async def check_llm(key: str, entry: dict) -> bool:
    """Goes through the LiveKit plugin class (google.LLM), not the raw SDK,
    with the entry's thinking_config exactly as providers.py sends it. The
    plugin rewrites thinking settings for Gemini 3 (drops thinking_budget,
    defaults Flash to "minimal"), so only this path reproduces what a call
    does: an earlier raw-SDK version of this check passed while every live
    turn 400ed."""
    model = entry["id"]
    kwargs = {"model": model, "api_key": key}
    if entry.get("thinking_config"):
        kwargs["thinking_config"] = dict(entry["thinking_config"])
    try:
        llm = google.LLM(**kwargs)
    except Exception as e:
        print(f"    FAIL  {model} thinking={entry.get('thinking_config')}: rejected at"
              f" construction: {short(e)}")
        return False
    ctx = lk_llm.ChatContext()
    ctx.add_message(role="user", content="Reply with exactly: ok")
    try:
        t0 = time.monotonic()
        out = ""
        async with llm.chat(chat_ctx=ctx, conn_options=FAST) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    out += chunk.delta.content
        print(f"    OK    {model}: {out.strip()[:40]!r} in {time.monotonic() - t0:.1f}s,"
              f" thinking={entry.get('thinking_config')}")
        return True
    except Exception as e:
        print(f"    FAIL  {model} thinking={entry.get('thinking_config')}: {short(e)}")
        return False
    finally:
        await llm.aclose()


async def check_tts(key: str, model: str, voice: str) -> bool:
    tts = google.beta.GeminiTTS(model=model, voice_name=voice, api_key=key)
    try:
        t0 = time.monotonic()
        nbytes = 0
        async for ev in tts.synthesize("Hello from Arya.", conn_options=FAST):
            nbytes += len(ev.frame.data)
        print(f"    OK    {model} / {voice}: {nbytes} audio bytes in {time.monotonic() - t0:.1f}s")
        return nbytes > 0
    except Exception as e:
        print(f"    FAIL  {model} / {voice}: {short(e)}")
        return False
    finally:
        await tts.aclose()


async def check_stt(key: str, model: str) -> bool:
    """Open the Live transcription stream, feed 1.5 s of silence, close.
    Silence yields no transcript; what is being tested is that Google
    accepts the key for this model and holds the websocket open."""
    stt = google.beta.GeminiSTT(model=model, api_key=key)
    stream = stt.stream(conn_options=FAST)
    rate, chunk = 16000, 160
    silence = bytes(chunk * 2)

    async def feed() -> None:
        for _ in range(int(1.5 * rate / chunk)):
            stream.push_frame(rtc.AudioFrame(
                data=silence, sample_rate=rate, num_channels=1, samples_per_channel=chunk,
            ))
            await asyncio.sleep(chunk / rate)
        stream.end_input()

    try:
        t0 = time.monotonic()
        feeder = asyncio.create_task(feed())
        async with asyncio.timeout(25):
            async for _ in stream:
                pass
        await feeder
        print(f"    OK    {model}: stream accepted and closed cleanly in {time.monotonic() - t0:.1f}s")
        return True
    except Exception as e:
        print(f"    FAIL  {model}: {short(e)}")
        return False
    finally:
        await stream.aclose()
        await stt.aclose()


async def main() -> int:
    key = sys.argv[1].strip() if len(sys.argv) > 1 else config.load_keys().get("gemini", "")
    src = "argv" if len(sys.argv) > 1 else str(config.KEYS_PATH)
    print(f"key: {mask(key)}  (from {src})")
    if not key:
        print("no key: pass one as the first argument or save it in Options")
        return 1

    defaults = catalog.get("gemini_defaults")
    models = catalog.get("gemini_models")
    llm_ids = [m["id"] for m in models["llm"]]
    tts_ids = [m["id"] for m in models["tts"]]
    stt_id = defaults["stt_model"]
    voice = defaults["voice"]

    results = {"key": check_key_and_models(key, llm_ids + tts_ids + [stt_id])}

    # Every model is probed even after a failure (no short-circuit): the
    # point is a complete picture, not the first bad one.
    print("\n[1] LLM (livekit.plugins.google.LLM, the class the agent uses)")
    llm_ok = [await check_llm(key, m) for m in models["llm"]]
    results["llm"] = all(llm_ok)

    print("\n[2] TTS (livekit.plugins.google.beta.GeminiTTS)")
    tts_ok = [await check_tts(key, m, voice) for m in tts_ids]
    results["tts"] = all(tts_ok)

    print("\n[3] STT (livekit.plugins.google.beta.GeminiSTT, Live API)")
    results["stt"] = await check_stt(key, stt_id)

    print("\nsummary: " + "  ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
