#!/usr/bin/env python3
"""Generate the bundled voice previews under backend/voices/.

One fixed sentence per catalogue voice, synthesised through the same
helper the preview route uses (media.synthesize), so what ships in the
repo is exactly what the route would make on first play. Run it once
from a machine that has the provider keys saved (backend/data/keys.json
or the usual env vars) and, for the Kokoro clip, the local stack up
(docker compose up, Speaches on SPEACHES_URL, default localhost:8010).

    backend/.venv/bin/python backend/scripts/gen_voice_samples.py [--force]

A provider whose key is missing, or a Speaches that is not reachable, is
skipped with a message and exits non-zero at the end, so a half-generated
set never passes silently. Existing clips are kept unless --force.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))

import catalog  # noqa: E402
import config  # noqa: E402
import media  # noqa: E402
import providers  # noqa: E402

OUT = HERE.parent / "voices"
SAMPLE = "Hi, thanks for calling. How can I help you today?"


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s or "")[:80] or "_"


def jobs(keys: dict) -> list[tuple[str, str, dict, str]]:
    """(provider, voice, endpoint, extension) for every catalogue voice."""
    out: list[tuple[str, str, dict, str]] = []

    oa = catalog.get("openai_models")
    oa_model = catalog.get("openai_defaults").get("tts_model", "")
    oa_voices: list[str] = []
    for m in oa.get("tts", []):
        for v in m.get("voices") or []:
            if v not in oa_voices:
                oa_voices.append(v)
    for v in oa_voices:
        out.append(("openai", v, {
            "provider": "openai",
            "tts_base_url": providers.OPENAI_BASE_URL,
            "api_key": keys.get("openai", ""),
            "tts_model": oa_model,
            "voice": v,
            "tts_format": "mp3",
        }, "mp3"))

    gm = catalog.get("gemini_models")
    g_model = catalog.get("gemini_defaults").get("tts_model", "")
    for v in gm.get("voices") or []:
        out.append(("gemini", v, {
            "provider": "gemini",
            "api_key": keys.get("gemini", ""),
            "tts_model": g_model,
            "voice": v,
            "style": "",
        }, "wav"))

    d = catalog.get("local_defaults")
    out.append(("local", d.get("voice", "af_heart"), {
        "provider": "local",
        "tts_base_url": config.speaches_url() + "/v1",
        "api_key": "not-needed",
        "tts_model": d.get("speaches_tts_model", ""),
        "voice": d.get("voice", "af_heart"),
        "tts_format": "wav",
    }, "wav"))
    return out


async def main(force: bool) -> int:
    keys = config.load_keys()
    missing: list[str] = []
    made = kept = 0
    for provider, voice, endpoint, ext in jobs(keys):
        path = OUT / provider / f"{slug(voice)}.{ext}"
        if path.exists() and not force:
            kept += 1
            continue
        if provider in ("openai", "gemini") and not endpoint.get("api_key"):
            missing.append(f"{provider}/{voice}: no {provider} key saved")
            continue
        try:
            data, _mime = await media.synthesize(endpoint, SAMPLE)
        except media.MediaError as e:
            missing.append(f"{provider}/{voice}: {e}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        made += 1
        print(f"wrote {path.relative_to(OUT.parent)} ({len(data) // 1024} KB)")
    print(f"done: {made} written, {kept} kept")
    if missing:
        print("not generated:")
        for m in missing:
            print("  " + m)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--force" in sys.argv[1:])))
