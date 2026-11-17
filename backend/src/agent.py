"""Arya agent worker.

One LiveKit job = one conversation. The active agent's config is read
fresh at the start of every job (each job runs in its own subprocess), so
whatever the UI saved last is what the next call runs — no worker restart
needed.

Run modes:
  uv run python src/agent.py dev      # local dev, hot reload
  uv run python src/agent.py start    # production / docker
  uv run python src/agent.py console  # terminal-only, no server needed
"""

import asyncio
import base64
import contextlib
import dataclasses
import inspect
import json
import logging
import os
import pathlib
import re
import time
import uuid
from typing import Literal

from dotenv import load_dotenv
from livekit import api as lk_api, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    JobContext,
    RoomInputOptions,
    RoomOutputOptions,
    beta as lk_beta,
    cli,
    get_job_context,
    llm as lk_llm,
    stt as lk_stt,
)

import catalog
import config
import connectors
import db
import itinerary_email
import links as links_mod
import suggestions as suggestions_mod
import memory as memory_mod
import providers
import rag
import tools as tools_mod
import trip_planner
import worker_observability

logger = logging.getLogger("agent")

load_dotenv(pathlib.Path(__file__).parent.parent / ".env.local")

_rag_client = None


def _qdrant():
    global _rag_client
    if _rag_client is None:
        _rag_client = rag.qdrant(config.qdrant_url())
    return _rag_client


_rag_metrics_path = config.BACKEND_DIR / "metrics" / "rag.jsonl"

# room + session-start timestamp, set per job; names the per-session
# turn-metrics file AND stamps every rag.jsonl record so the two metrics
# datasets cross-reference by the same key.
_session_label = None


def _turn_metric_row(record: dict) -> dict:
    """Map one SDK metrics event (already model_dump'ed) onto the
    turn_metrics columns. stage_ms is the headline number per type; the
    rest are the secondary fields the dashboard aggregates. Unknown types
    still produce a row (stage_ms NULL, raw kept) so an SDK upgrade never
    silently drops data."""
    t = record.get("metric_type") or "Unknown"
    g = record.get

    def ms(v):
        return int(round(v * 1000)) if isinstance(v, (int, float)) else None

    meta = g("metadata") if isinstance(g("metadata"), dict) else {}
    row = {
        "metric_type": t,
        "speech_id": g("speech_id"),
        "model": meta.get("model_name"),
        "provider": meta.get("model_provider"),
        "cancelled": bool(g("cancelled") or False),
        # Present and NULL for a type this mapper does not know, as the
        # docstring promises, instead of the key being absent.
        "stage_ms": None,
    }
    if t == "EOUMetrics":
        row["stage_ms"] = ms(g("end_of_utterance_delay"))
        row["duration_ms"] = ms(g("transcription_delay"))
    elif t == "STTMetrics":
        row["stage_ms"] = ms(g("duration"))
        row["audio_ms"] = ms(g("audio_duration"))
    elif t == "LLMMetrics":
        row["stage_ms"] = ms(g("ttft"))
        row["duration_ms"] = ms(g("duration"))
        row["tokens_in"] = g("prompt_tokens")
        row["tokens_out"] = g("completion_tokens")
    elif t == "TTSMetrics":
        row["stage_ms"] = ms(g("ttfb"))
        row["duration_ms"] = ms(g("duration"))
        row["audio_ms"] = ms(g("audio_duration"))
        row["chars"] = g("characters_count")
    elif t == "VADMetrics":
        row["stage_ms"] = ms(g("inference_duration_total"))
    elif t == "EOTInferenceMetrics":
        row["stage_ms"] = ms(g("detection_delay"))
        row["duration_ms"] = ms(g("total_duration"))
    return row


def _log_rag(record: dict) -> None:
    # Local JSONL (debugging artifact, container-scoped) …
    try:
        _rag_metrics_path.parent.mkdir(exist_ok=True)
        with _rag_metrics_path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass
    # … AND a Postgres row: the Insights API runs in a different container,
    # so the JSONL alone is invisible to it (and wiped on rebuilds).
    # Fire-and-forget, exception-swallowed — same contract as transcript
    # writes: a DB hiccup must never break a live call.
    scores = record.get("scores") or []
    latency_s = record.get("latency_s")

    async def _write():
        try:
            pool = await db.get_pool(config.database_url())
            await pool.execute(
                "INSERT INTO rag_events (room_name, agent_id, query,"
                " latency_ms, top_score, kept, injected, error, skipped, files)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                record.get("room"),
                record.get("agent_id"),
                record.get("query"),
                int(latency_s * 1000) if latency_s is not None else None,
                float(scores[0]) if scores else None,
                record.get("kept"),
                bool(record.get("injected")),
                record.get("error"),
                record.get("skipped"),
                json.dumps(worker_observability.rag_files(record)),
            )
        except Exception as e:
            logger.debug(f"rag event write skipped: {e}")

    try:
        asyncio.create_task(_write())
    except RuntimeError:
        pass  # no running loop (unit tests) — the JSONL line still exists


# Closing/acknowledgment turns ("thanks, goodbye") don't need the knowledge
# base — retrieving on them injects chunks and puts source chips on goodbye
# bubbles (observed live: "No, thank you. Have a nice day." pulled 5 chunks
# at score 0.59, higher than a real membership question). Rule: no "?" and
# nothing substantive left after stripping closing phrases and filler.
_CLOSING_PHRASES = re.compile(
    r"\b(good\s?bye|bye+|see you( later| soon)?|talk to you later"
    r"|have a (nice|good|great|lovely) (day|one|evening|night|weekend)"
    r"|thank you( so much| very much)?|thanks( a lot)?"
    r"|that('s| is| will be| would be) (all|it)"
    r"|(end|finish|cut) (the |this )?call|hang up|disconnect"
    r"|no thanks|nothing else|i('m| am) (good|done|all set)|take care"
    r"|okay|ok|alright|all right|sounds (good|great)|perfect|great|cool"
    r"|got it|understood|you too|will do)\b"
)
_FILLER_WORDS = frozenset(
    "no yes yeah yep nope sure please so well um uh oh a an and the for now "
    "then that this is it all i am are was very really just right".split()
)


_OPTION_WORD = re.compile(r"\b(option|options|rate|rates)\b", re.IGNORECASE)


def _names_an_option(text: str, groups: list[dict]) -> bool:
    """Do these option cards belong under this bubble? A search's cards are
    stashed when the tool returns, and the next assistant message to be
    committed can be the filler said BEFORE the search ("Let me look for
    flights") whose playback outlasted the tool. Seen live: the cards sat
    above the message that listed the options. So a bubble takes the cards
    only if it talks about options or names one of them, unless a group
    is marked `always`: the order desk's lists go under whatever the
    model says next, because the reply is told not to name the items."""
    if any(isinstance(g, dict) and g.get("always") for g in groups):
        return True
    if _OPTION_WORD.search(text):
        return True
    low = text.lower()
    for group in groups:
        for item in group.get("items") or []:
            head = str(item.get("label") or "").split(" \u00b7 ")[0].strip().lower()
            if len(head) > 3 and head in low:
                return True
    return False


def _is_closing_turn(text: str) -> bool:
    # STT often emits curly quotes — normalize so "that’s all" matches.
    t = text.lower().replace("’", "'").replace("‘", "'")
    if "?" in t:
        return False
    residue = _CLOSING_PHRASES.sub(" ", t)
    words = [w for w in re.sub(r"[^a-z']+", " ", residue).split() if w not in _FILLER_WORDS]
    return not words


# ─── citation overlap check: a chip must mean "used", not "retrieved" ───────
# Generic conversational words don't count as evidence of usage.
_OVERLAP_STOP = frozenset(
    "the a an and or of to in for on at is are was were be been it its this "
    "that with as by from you your our we they he she not do does did have "
    "has had will would can could may might shall should about more some any "
    "all if then than so such just also very please thank thanks help know "
    "like want need there here when what which who how whom whose why must "
    "information available details specific seems might appear based meant "
    "offerings products services company business assist question questions".split()
)


# Spelled-out numbers vs digits, and comma thousand-separators, are the
# most common reason a genuinely-grounded answer loses its overlap: the
# voice-output rules make the model SAY "five thousand rupees" while the
# source document WRITES "5,000" — same fact, zero shared characters
# without this normalization.
_NUM_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}
_NUM_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUM_MULT = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_NUM_WORD = "|".join(
    sorted(set(_NUM_ONES) | set(_NUM_TENS) | set(_NUM_MULT), key=len, reverse=True)
)
_NUMBER_WORD_RUN = re.compile(
    rf"\b(?:{_NUM_WORD})(?:[\s-]+(?:and[\s-]+)?(?:{_NUM_WORD}))*\b",
    re.IGNORECASE,
)


def _words_to_int(phrase: str) -> int:
    total = current = 0
    for tok in re.split(r"[\s-]+", phrase.lower()):
        if tok == "and":
            continue
        if tok in _NUM_ONES:
            current += _NUM_ONES[tok]
        elif tok in _NUM_TENS:
            current += _NUM_TENS[tok]
        elif tok == "hundred":
            current = (current or 1) * 100
        elif tok in ("thousand", "million"):
            total += (current or 1) * _NUM_MULT[tok]
            current = 0
    return total + current


def _normalize_numbers(text: str) -> str:
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # "5,000" -> "5000"
    return _NUMBER_WORD_RUN.sub(lambda m: str(_words_to_int(m.group(0))), text)


def _stem(word: str) -> str:
    """Light, directional plural stripping ("officers"/"officer",
    "policies"/"policy") — can only make two already-similar words equal,
    never manufactures a match between unrelated words."""
    if word.endswith("ies") and len(word) > 5:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 4:
        return word[:-1]
    return word


def _overlap_tokens(text: str) -> list[str]:
    text = _normalize_numbers(text)
    out = []
    for w in re.sub(r"[^a-z0-9']+", " ", text.lower()).split():
        if w.isdigit():
            if len(w) >= 2:  # drop bare "1"/"2" (from lone "one"/"two") — noise
                out.append(w)
            continue
        if len(w) >= 4 and w not in _OVERLAP_STOP:
            out.append(_stem(w))
    return out


_ANY_DIGIT_SEQ = re.compile(r"\d(?:[\s-]?\d)+")


def _digit_runs(text: str) -> set[str]:
    """Digit sequences (6+ digits) with separators stripped, digit-words
    normalized first — so an answer saying "two-two-nine 2-7-9…" and a
    document saying "22927 9017…" produce comparable runs."""
    t = re.sub(
        rf"\b{_WORD}\b",
        lambda m: _DIGIT_WORDS[m.group(0).lower()],
        text,
        flags=re.IGNORECASE,
    )
    runs = set()
    for m in _ANY_DIGIT_SEQ.finditer(t):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) >= 6:
            runs.add(digits)
    return runs


def _used_in_answer(answer: str, cits: list[dict]) -> list[dict]:
    """Keep only the citations whose source text visibly shows up in the
    answer. Shared distinctive word-pairs (bigrams) are the strong signal;
    a high distinctive-word overlap catches paraphrase; a shared long
    number (account/phone/reference, any spelling) is decisive on its own —
    numeric answers reorder words and would otherwise lose their chip.
    Deterministic, no model calls. Checked per source file, so an answer
    drawing on one of two documents keeps only that document's chip."""
    tune = catalog.get("rag_tuning")
    a_tokens = _overlap_tokens(answer)
    if len(a_tokens) < tune["min_answer_tokens"]:
        return []
    a_set = set(a_tokens)
    a_bigrams = {f"{x} {y}" for x, y in zip(a_tokens, a_tokens[1:])}
    a_digits = _digit_runs(answer)
    # Short exact amounts (fees, extensions, room numbers — 2-5 digits,
    # below the identifier threshold in _digit_runs): a compact numeric
    # answer ("five thousand... three thousand...") has too few shared
    # words to clear the ratio check below, but two DIFFERENT numbers
    # matching exactly is specific enough on its own.
    a_nums = {t for t in a_tokens if t.isdigit() and len(t) < 6}
    by_file: dict[str, list[dict]] = {}
    for c in cits:
        by_file.setdefault(c.get("filename", ""), []).append(c)
    kept = []
    for cs in by_file.values():
        src = " ".join(c.get("_text") or c.get("excerpt") or "" for c in cs)
        s_tokens = _overlap_tokens(src)
        s_set = set(s_tokens)
        s_bigrams = {f"{x} {y}" for x, y in zip(s_tokens, s_tokens[1:])}
        shared_bi = len(a_bigrams & s_bigrams)
        shared_uni = len(a_set & s_set)
        s_digits = _digit_runs(src)
        digit_hit = any(
            a in s or s in a for a in a_digits for s in s_digits
        )
        shared_nums = len(a_nums & {t for t in s_tokens if t.isdigit() and len(t) < 6})
        if (
            shared_bi >= tune["min_shared_bigrams"]
            or digit_hit
            or shared_nums >= tune["min_shared_numbers"]
            or (shared_nums >= 1 and shared_bi >= 1)
            or (
                shared_uni >= tune["min_shared_unigrams"]
                and shared_uni >= tune["unigram_ratio"] * len(a_set)
            )
        ):
            kept.extend(cs)
    # _text is check-only — never published to the UI or stored in the DB.
    return [{k: v for k, v in c.items() if k != "_text"} for c in kept]


def _head(text: str) -> str:
    """The opening words of an answer: the key everything per answer shares.
    The citation chips, the judge's per-answer verdicts and the document
    context kept for the judge all find their bubble by it (Transcript.jsx
    and ConvDetail.jsx cut the same 80 characters)."""
    return " ".join(text.split())[:80]


def _label_verdict(v: float) -> str:
    return "pass" if v >= 0.75 else ("maybe" if v >= 0.5 else "fail")


def _context_block(context: dict | None, limit: int = 6000) -> str:
    """The document excerpts an answer was given, as the judge reads them:
    one block per chunk with its source, cut at `limit` characters in all
    so a wide retrieval never turns one judge call into a long one."""
    if not context or not context.get("chunks"):
        return ""
    parts = []
    left = limit
    for ch in context["chunks"]:
        text = (ch.get("text") or "").strip()
        if not text or left <= 0:
            continue
        text = text[:left]
        left -= len(text)
        parts.append(f"[source: {ch.get('filename') or 'document'}]\n{text}")
    return "\n---\n".join(parts)


def _context_checks(turn_results: list[dict], overlap: tuple[int, int] = (0, 0)) -> dict:
    """Roll the per-answer document checks up to the call.

    Every answer that received document context was judged on three more
    things in the same call as accuracy and relevancy: faithfulness (the
    worst answer counts, like accuracy: one invented fee is the failure
    that matters), context relevance (averaged: it grades the lookup, not
    the agent, so it is shown and never scored) and whether the answer
    drew on the excerpts at all (the judge's reading of what the
    word-overlap chips estimate; `overlap` is that tally, quoted beside it).
    Returns {check: {"value": float | None, "verdict", "reasoning"}}; a
    None value means shown, never scored. Empty when no judged answer had
    document context, so a call without lookups keeps its old checks."""
    V = {"pass": 1.0, "maybe": 0.5, "fail": 0.0}
    ctx = [t for t in turn_results if isinstance(t.get("faithfulness"), dict)]
    if not ctx:
        return {}
    n = len(ctx)
    answers = f"{n} answer{'s' if n != 1 else ''}"
    out = {}
    vals = [V.get(t["faithfulness"].get("verdict"), 0.0) for t in ctx]
    worst = min(vals)
    w = ctx[vals.index(worst)]
    out["faithfulness"] = {
        "value": worst,
        "verdict": _label_verdict(worst),
        "reasoning": f"Worst of {answers} with document context. "
        + (w["faithfulness"].get("reasoning") or ""),
    }
    rel = [V.get((t.get("context_relevance") or {}).get("verdict"), 0.0) for t in ctx]
    mean = sum(rel) / n
    off = sum(
        1 for t in ctx if (t.get("context_relevance") or {}).get("verdict") != "pass"
    )
    out["context_relevance"] = {
        "value": None,
        "verdict": _label_verdict(mean),
        "reasoning": f"Average over {n} lookup{'s' if n != 1 else ''}: {round(mean * 100)}%."
        + (
            f" {off} of them brought back excerpts that did not cover the question."
            if off
            else " Every lookup brought back excerpts that covered the question."
        ),
    }
    used = sum(1 for t in ctx if t.get("used_context"))
    ov_used, ov_inj = overlap
    out["grounding"] = {
        "value": None,
        "verdict": _label_verdict(used / n),
        "reasoning": f"{used} of {answers} that received document context drew on it,"
        " as read by the judge"
        + (f" (word overlap said {ov_used} of {ov_inj})." if ov_inj else "."),
    }
    return out


async def _retrieve(query: str, cfg: dict, keys: dict) -> list[dict]:
    prof = config.embedding_profile(cfg, keys)
    vector = (
        await rag.embed_texts(
            [query],
            base_url=prof["base_url"],
            api_key=prof["api_key"],
            model=prof["model"],
        )
    )[0]
    return await asyncio.to_thread(
        rag.search,
        _qdrant(),
        prof["collection"],
        vector,
        cfg.get("rag", {}).get("top_k", 5),
        cfg.get("id"),  # own docs + untagged shared docs
    )


# ─── transcript digit-collapsing ─────────────────────────────────────────────
# The prompt makes the LLM write identifiers as hyphen-separated digits so
# TTS speaks them digit by digit — sometimes as numerals ("2-2-9 2-7-9"),
# sometimes as words ("two-two-nine"). The transcript would otherwise show
# that spelling — collapse both forms back to a continuous number for
# display. The audio path is untouched.
_ID_RUN = re.compile(r"\b\d(?:[- ]\d){2,}\b")
_DIGIT_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_WORD = r"(?:zero|one|two|three|four|five|six|seven|eight|nine)"
_WORD_RUN = re.compile(rf"\b{_WORD}(?:[- ]{_WORD}){{2,}}\b", re.IGNORECASE)
# Trailing digit / digit-word run (plus an optional partial word the next
# chunk may complete) — held back by _split_hold so a number split across
# streaming chunks still collapses as one.
_TOKEN = rf"(?:\d|{_WORD})"
_TAIL_RUN = re.compile(rf"{_TOKEN}(?:[- ]{_TOKEN})*[- ]?[A-Za-z]{{0,6}}$", re.IGNORECASE)


def _collapse_ids(text: str) -> str:
    # Only hyphenated runs are identifiers; space-only runs ("gates 1 2 3",
    # "one two three") are enumerations and stay as written.
    def _num(m):
        s = m.group(0)
        return re.sub(r"[- ]", "", s) if "-" in s else s

    def _words(m):
        s = m.group(0)
        if "-" not in s:
            return s
        return "".join(_DIGIT_WORDS[w.lower()] for w in re.split(r"[- ]", s))

    return _WORD_RUN.sub(_words, _ID_RUN.sub(_num, text))


def _split_hold(buf: str) -> tuple[str, str]:
    """Split buf into (emit, hold): the trailing run that might still grow
    (digits, digit-words, separators, one partial word) is held for the next
    chunk. Anything held is emitted at stream end regardless; pathological
    tails over 120 chars are emitted as-is."""
    m = _TAIL_RUN.search(buf)
    if not m or len(buf) - m.start() > 120:
        return buf, ""
    return buf[: m.start()], buf[m.start():]


# Expressive-mode delivery markers (`<expr type="expression" label="..."/>`)
# are lowered for the TTS and stripped from the transcript by the SDK, but
# only when well formed. Seen live on Gemini: the LLM ended a sentence with
# a half-written tag (`<expr type="expression" label`), the stripper let it
# through, and the caller saw it in the chat. This guard is display-only
# and belt-and-braces: complete tags are removed, a tag split across chunks
# is held until it closes, and a fragment still open at the end of the
# reply is dropped rather than shown.
_EXPR_TAG = re.compile(r"<expr\b[^<>]*/?>")
_EXPR_OPEN = re.compile(r"<(?:e(?:x(?:p(?:r\b[^<>]*)?)?)?)?$")


def _strip_expr(text: str) -> str:
    return _EXPR_TAG.sub("", text)


def _hold_open_tag(buf: str) -> tuple[str, str]:
    """Hold a trailing `<expr ...` (or a prefix of it) that has no `>` yet."""
    m = _EXPR_OPEN.search(buf)
    if not m or len(buf) - m.start() > 200:
        return buf, ""
    return buf[: m.start()], buf[m.start():]


async def _transcript_display(text):
    """The display-only transcript transform behind Assistant.transcription_node,
    module-level so it can be driven with synthetic chunks in tests: strip
    expressive markers (holding one that is split across chunks), then
    collapse hyphen-grouped identifiers (holding a run that may still grow)."""
    buf = ""
    async for delta in text:
        buf = _strip_expr(buf + delta)
        emit, buf = _hold_open_tag(buf)
        emit, held = _split_hold(emit)
        buf = held + buf
        if emit:
            yield _collapse_ids(emit)
    # Stream end: a still-open `<expr` fragment is a broken marker, not
    # words, so it is dropped; a held digit run is emitted as usual.
    buf, _dangling = _hold_open_tag(_strip_expr(buf))
    if buf:
        yield _collapse_ids(buf)


class Assistant(Agent):
    def __init__(self, cfg: dict, keys: dict, instructions: str, tools: list):
        super().__init__(instructions=instructions, tools=tools)
        self._cfg = cfg
        self._keys = keys
        self._last_citations = []
        # Per-call grounding tally for the deterministic eval judge:
        # injected = turns where KB context reached the LLM, used = answers
        # that visibly drew on it (the used-in-answer gate). Counted here
        # (not re-derived from the DB at shutdown) so the judge works even
        # when transcript writes were skipped.
        self._ground_injected = 0
        self._ground_used = 0
        # The excerpts each answer was given, kept for the post-call judge
        # (faithfulness, context relevance, whether the answer drew on
        # them): _pending_context is this turn's, moved to _kb_turns under
        # the answer's opening words once the answer is committed. Chunk
        # text leaves the worker only inside the judge prompt.
        self._pending_context: dict | None = None
        self._kb_turns: list[dict] = []
        self._inject_rag = cfg.get("rag", {}).get("enabled", False)
        self._web_on = bool(
            cfg.get("tools", {}).get("web_search")
            and config.search_ready(cfg, keys)[0]
        )
        # Echo guard for stt_node: the exact vocabulary hint providers.py
        # hands the transcriber, as a whitespace-tolerant, case-insensitive
        # pattern (trailing period optional). None when the agent has no
        # vocabulary, so the guard costs nothing.
        hint = providers._stt_prompt(cfg.get("stt", {}) or {})
        self._stt_echo = (
            re.compile(
                r"\s*" + r"\s+".join(re.escape(w) for w in hint.rstrip(".").split()) + r"\.?",
                re.IGNORECASE,
            )
            if hint
            else None
        )

    async def stt_node(self, audio, model_settings):
        """Scrub echoes of the STT vocabulary hint out of transcripts.

        Whisper-family models sometimes return their own prompt as the
        transcript of near-silent audio (seen live on the realtime path:
        "Domain vocabulary: policy, premium, ..." committed as speech and
        answered by the agent). The hint is removed wherever it appears;
        an event left with no text is dropped so it never becomes a turn.
        """
        stream = Agent.default.stt_node(self, audio, model_settings)
        if asyncio.iscoroutine(stream):
            stream = await stream
        async for ev in stream:
            if self._stt_echo is not None and isinstance(ev, lk_stt.SpeechEvent) and ev.alternatives:
                alt = ev.alternatives[0]
                cleaned = self._stt_echo.sub(" ", alt.text or "")
                cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
                if cleaned != (alt.text or "").strip():
                    if not cleaned and ev.type in (
                        lk_stt.SpeechEventType.FINAL_TRANSCRIPT,
                        lk_stt.SpeechEventType.INTERIM_TRANSCRIPT,
                        lk_stt.SpeechEventType.PREFLIGHT_TRANSCRIPT,
                    ):
                        logger.info("STT echoed its vocabulary hint; dropped")
                        continue
                    alt.text = cleaned
            yield ev

    async def transcription_node(self, text, model_settings):
        """Display-only transform: the transcript gets continuous numbers
        while TTS keeps the digit-by-digit spelling. Chunk boundaries are
        buffered (_split_hold) so a run split across deltas still matches."""
        async for out in _transcript_display(text):
            yield out

    async def on_user_turn_completed(self, turn_ctx, new_message):
        """Per-turn hook: RAG retrieval with a hard timeout so it can never
        stall a turn; any failure means proceeding without KB context.
        Closing turns skip retrieval (no junk grounding on farewells) —
        ending the call is NOT tied to speech in any way.
        """
        if not self._inject_rag:
            return
        query = (new_message.text_content or "").strip()
        if not query:
            return
        try:
            room_name = get_job_context().room.name
        except Exception:
            room_name = None
        if _is_closing_turn(query):
            logger.info("closing/ack turn: retrieval skipped")
            _log_rag({
                "ts": time.time(),
                "room": room_name,
                "session": _session_label,
                "agent_id": self._cfg.get("id"),
                "query": query,
                "skipped": "closing_turn",
                "injected": False,
            })
            return
        cfg = self._cfg.get("rag", {})
        # Embed the query with recent turns prepended so pronoun follow-ups
        # and STT-mangled names retrieve on conversation context, not the
        # bare utterance. Previously-injected KB excerpts are filtered out.
        embed_query = query
        prior = []
        n_ctx = cfg.get("query_context_turns", 1)
        if n_ctx:
            prior = [
                f"{item.role}: {_without_excerpts(item.text_content)}"
                for item in turn_ctx.items
                if item is not new_message
                and getattr(item, "role", None) in ("user", "assistant")
                and getattr(item, "text_content", None)
                # Skip a previously injected KB block (older shape, a
                # message of its own); excerpts appended to a user turn are
                # cut by _without_excerpts above. Both match on the SAME
                # prompt_blocks preamble the injection uses.
                and not item.text_content.startswith(
                    catalog.get("prompt_blocks")["kb_injection_preamble"]
                )
            ][-n_ctx:]
            if prior:
                embed_query = "\n".join(prior) + f"\nuser: {query}"
        t0 = time.monotonic()
        try:
            hits = await asyncio.wait_for(
                _retrieve(embed_query, self._cfg, self._keys),
                timeout=cfg.get("timeout_s", 2.0),
            )
        except Exception as e:
            logger.warning(f"RAG skipped: {type(e).__name__}: {e}")
            _log_rag({
                "ts": time.time(),
                "room": room_name,
                "session": _session_label,
                "agent_id": self._cfg.get("id"),
                "query": query,
                "latency_s": round(time.monotonic() - t0, 4),
                "error": f"{type(e).__name__}: {e}",
                "injected": False,
            })
            return
        latency_s = round(time.monotonic() - t0, 4)
        raw_hits = list(hits)
        if cfg.get("min_score") is not None:
            hits = [h for h in hits if h["score"] >= cfg["min_score"]]
        # Relative gate: absolute thresholds don't separate well on small
        # KBs (nomic scores nearly everything 0.45+), but chunks far below
        # the turn's best hit are echoes, not evidence.
        tune = catalog.get("rag_tuning")
        if hits:
            top = hits[0]["score"]
            hits = [h for h in hits if h["score"] >= top - tune["relative_gate"]]
        _log_rag({
            "ts": time.time(),
            "room": room_name,
            "session": _session_label,
            "agent_id": self._cfg.get("id"),
            "profile": cfg.get("embedding_profile", "nomic"),
            "query": query,
            "top_k": cfg.get("top_k", 5),
            "min_score": cfg.get("min_score"),
            "latency_s": latency_s,
            "context_turns": len(prior),
            "scores": [round(h["score"], 4) for h in raw_hits],
            "filenames": [h["filename"] for h in raw_hits],
            "chunks": [h.get("chunk_index") for h in raw_hits],
            "kept": len(hits),
            "injected": bool(hits),
        })
        if not hits:
            logger.info("RAG: no hits above min_score, nothing injected")
            return
        context = "\n---\n".join(
            f"[source: {h['filename']}]\n{h['text']}" for h in hits
        )
        # The fallback clause decides tool behavior: without it the model
        # apologizes instead of reaching for web search (seen live).
        blocks = catalog.get("prompt_blocks")
        fallback = (
            blocks["rag_fallback_web"]
            if self._web_on
            else blocks["rag_fallback_no_web"]
        )
        # The excerpts go INTO the user's own message, as a second text part
        # after what they said, not as a message of their own. Both separate
        # shapes failed live: as an assistant-role note, Gemini imitated it
        # and ended a reply with the whole policy document; as a user-role
        # note it answered the note instead of the person ("it looks like
        # you shared some policy excerpts!"). OpenAI tolerated either, which
        # is why neither showed for weeks. One turn, question first then
        # reference material, is the shape every model reads the same way;
        # it also keeps the context alternating user/assistant, which some
        # chat templates require. Everything that records the person's words
        # cuts the excerpts off again with _without_excerpts.
        new_message.content.append(
            blocks["kb_injection_preamble"]
            + fallback
            + blocks["kb_injection_suffix"]
            + "\n"
            + context
        )
        self._last_citations = [
            {
                "doc_id": h["doc_id"],
                "filename": h["filename"],
                "chunk_index": h["chunk_index"],
                "score": h["score"],
                # short quote so the UI chips can show the evidence — flows
                # to both the live arya.citations event and the messages row
                "excerpt": " ".join((h.get("text") or "").split())[
                    : int(tune["excerpt_chars"])
                ],
                # full chunk text for the answer-overlap check only;
                # stripped before anything leaves the worker
                "_text": " ".join((h.get("text") or "").split())[
                    : int(tune["overlap_check_chars"])
                ],
            }
            for h in hits
        ]
        self._pending_context = {
            "query": query,
            "chunks": [
                {
                    "filename": h["filename"],
                    "text": " ".join((h.get("text") or "").split())[
                        : int(tune.get("judge_context_chars", 1200))
                    ],
                }
                for h in hits
            ],
        }
        self._ground_injected += 1
        logger.info(
            f"RAG: injected {len(hits)} chunk(s) from "
            f"{sorted({h['filename'] for h in hits})}"
        )


# The SDK default shutdown grace is 10s, which killed the post-call eval
# judges mid-flight on longer transcripts (observed live: process reaped,
# no score, no error anywhere). Sized to the evals timeout (75s) plus
# slack for the usage write; when evals finish sooner the process exits
# immediately, so a long cap costs nothing on ordinary calls.
def _prewarm(proc) -> None:
    """Pay each worker process's first-use costs before it takes a call.

    Every conversation runs in its own process, so anything done lazily on
    first use happens inside the first turn of every call. Seen in the log
    as "event loop blocked for 124 to 296ms": building the first OpenAI
    client (httpx's header checks warm Python's ABC caches), importing
    anyio's asyncio backend on the first HTTP request, and importing the
    session report module at hang-up. None needs a key or a loop, so all
    three are done here, once per process, while it is still idle.
    Best effort: a failure only means the old, slower first turn.
    """
    import importlib

    for mod in ("anyio._backends._asyncio", "livekit.agents.voice.report"):
        try:
            importlib.import_module(mod)
        except Exception as e:
            logger.debug(f"prewarm import {mod} skipped: {e}")
    try:
        from openai import AsyncOpenAI

        AsyncOpenAI(api_key="prewarm", base_url="http://127.0.0.1:9")
    except Exception as e:
        logger.debug(f"prewarm client skipped: {e}")


server = AgentServer(
    shutdown_process_timeout=float(
        os.environ.get("SHUTDOWN_PROCESS_TIMEOUT_S", "90")
    ),
    setup_fnc=_prewarm,
)


def _job_meta(ctx: JobContext) -> dict:
    """Explicit-dispatch metadata (identifiers only, set by the API's
    _start_session). Empty for auto-dispatched/console/CLI rooms.
    NOTE: ctx.room.metadata is NOT readable here — the rtc.Room is only
    name-seeded before ctx.connect(); ctx.job.metadata is the documented
    accessor for dispatch metadata."""
    try:
        return json.loads(ctx.job.metadata or "{}")
    except (json.JSONDecodeError, AttributeError):
        return {}


CHANNEL_HISTORY_DEFAULT = 6
# A previous thread older than this is not resumed: the customer gets the
# welcome, the identification and the order list as on a first contact.
# Half an hour, the owner's call: long enough for "and the other one?"
# after a short break, short enough that coming back later feels like a
# new visit rather than being held to something said before lunch.
CHANNEL_HISTORY_MAX_AGE_S = 30 * 60
# The bridge shrinks a photo to well under this; the cap is only here so a
# malformed or hostile stream cannot grow unbounded in memory.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


def _without_excerpts(text: str) -> str:
    """A user turn as the person wrote it.

    The RAG step appends the knowledge-base excerpts to the SAME user
    message, after the catalog preamble, so the model reads them as
    reference material that came with the question. Everything that
    records or reasons about what the person said (History, memory, the
    link resolver, evals) wants the words before that preamble.
    """
    preamble = catalog.get("prompt_blocks")["kb_injection_preamble"]
    head, sep, _rest = (text or "").partition(preamble)
    return head.rstrip() if sep else (text or "")


def _without_spoken_output_rules(text: str, blocks: dict) -> str:
    """The saved instructions minus the "# Output rules" section of the
    catalog's voice_rules block, for the written variant of the prompt.

    Every agent's instructions embed voice_rules verbatim (config.
    generate_instructions and load_template). Its first section is pure
    text-to-speech wording (digit groups, never read a URL) and appending a
    written-chat contradiction under it lost on live threads: URLs came out
    as "arxiv dot org slash abs". Removing the section beats arguing with
    it. The later sections (conversational flow, tools, guardrails) are
    general conduct and stay.

    Matched by its headings, not by the catalog's current text: an owner
    who trims a line from their agent's copy (seen live: the email and URL
    lines removed to stop "name at domain dot com" on WhatsApp) must not
    thereby switch the removal off and get the rest of the speech rules
    back. The catalog's verbatim section is tried first so an owner who
    moved the heading still gets the exact block removed.
    """
    voice = blocks.get("voice_rules") or ""
    section, sep, _rest = voice.partition("\n# Conversational flow")
    if sep and section and section in text:
        return text.replace(section, "", 1)
    start = text.find("# Output rules")
    end = text.find("\n# Conversational flow", start) if start != -1 else -1
    if start != -1 and end != -1:
        return text[:start] + text[end + 1:]
    return text


async def _replay_channel_history(dsn, meta, conversation_id, agent):
    """Seed a new channel session with the tail of the same customer's
    previous one.

    A WhatsApp thread has no end: someone asks a question, goes quiet for
    an hour and replies "and the other one?". Each quiet spell closes the
    room, so without this the agent would meet them as a stranger every
    time. Bounded to the last few turns on purpose, because the point is
    continuity of reference, not a full transcript in every prompt.

    Never raises: arriving with no history is a worse first answer, not a
    broken session.

    Only a RECENT thread is resumed (CHANNEL_HISTORY_MAX_AGE_S). Seen live:
    a "Hi" two hours after the last exchange was answered as "Hi Usama,
    how can I assist", with no welcome and no order list, because the
    replayed note said not to greet again. After a gap that long the
    customer is starting over and expects to be greeted and identified.
    """
    external = meta.get("external_user")
    deployment_id = meta.get("deployment_id")
    if not external or not deployment_id:
        return
    try:
        pool = await db.get_pool(dsn)
        limit = await pool.fetchval(
            "SELECT (c.config->>'history_turns')::int FROM channels c"
            " WHERE c.deployment_id = $1::uuid LIMIT 1",
            deployment_id,
        )
        limit = CHANNEL_HISTORY_DEFAULT if limit is None else int(limit)
        if limit <= 0:
            return
        rows = await pool.fetch(
            "SELECT m.role, m.content FROM messages m WHERE m.conversation_id = ("
            "  SELECT c.id FROM conversations c"
            "   WHERE c.deployment_id = $1::uuid"
            "     AND c.metadata->>'external_user' = $2"
            "     AND c.id <> $3"
            "     AND coalesce(c.ended_at, c.started_at)"
            "         > now() - ($5::int * interval '1 second')"
            "   ORDER BY c.started_at DESC LIMIT 1"
            ") AND m.role IN ('user', 'assistant') AND m.content <> ''"
            " ORDER BY m.id DESC LIMIT $4",
            deployment_id, external, conversation_id, limit * 2,
            CHANNEL_HISTORY_MAX_AGE_S,
        )
    except Exception as e:
        logger.warning(f"channel history replay skipped: {e}")
        return
    if not rows:
        return
    turns = list(reversed(rows))
    chat_ctx = agent.chat_ctx.copy()
    for row in turns:
        chat_ctx.add_message(role=row["role"], content=[row["content"]])
    # One line of framing so the model treats these as a resumed thread
    # rather than as things said a moment ago.
    chat_ctx.add_message(
        role="system",
        content=["The messages above are from this customer's earlier"
                 " conversation on this channel. Continue naturally; do not"
                 " greet them again or repeat what was already answered."],
    )
    try:
        await agent.update_chat_ctx(chat_ctx)
        logger.info(f"replayed {len(turns)} message(s) of channel history")
    except Exception as e:
        logger.warning(f"channel history not applied: {e}")


# agent_name registers this worker for EXPLICIT dispatch — the API names
# it on room creation and says which agent to run via job metadata. With
# a name set, rooms created outside the API (lk CLI, meet.livekit.io) get
# NO agent; set ARYA_AGENT_NAME="" on BOTH services to restore
# auto-dispatch. Must match the API's ARYA_AGENT_NAME.
@server.rtc_session(agent_name=os.environ.get("ARYA_AGENT_NAME", "arya"))
async def my_agent(ctx: JobContext):
    # Fresh read per job. Explicit dispatch says WHICH agent this room is
    # for; without metadata (console/CLI/auto-dispatch) fall back to the
    # global active agent, exactly the old behavior.
    keys = config.load_keys()
    dsn = config.database_url()
    meta = _job_meta(ctx)
    requested = meta.get("agent_id")
    if requested:
        cfg = config.load_saved_agent(requested)
        if cfg is None:
            # FAIL CLOSED. Falling back to load_active() here would answer
            # this room with whatever agent was activated last — the exact
            # bug per-agent dispatch exists to fix.
            logger.error(f"agent '{requested}' not found — refusing the job")
            ctx.shutdown(reason="unknown agent")
            return
    else:
        cfg = config.load_active()

    ctx.log_context_fields = {"room": ctx.room.name}
    logger.info(
        f"agent '{cfg.get('name', '?')}' provider={cfg.get('provider')} "
        f"rag={cfg.get('rag', {}).get('enabled')} "
        f"memory={cfg.get('memory', {}).get('enabled')}"
    )

    # Catalogs (model defaults/flags) are DB-backed and can change between
    # calls — pull the latest snapshot before building the session. DB
    # trouble degrades to the built-in seeds (console runs, tests).
    try:
        await catalog.refresh(await db.get_pool(dsn), force=True)
    except Exception as e:
        logger.warning(f"catalog refresh skipped (built-in defaults): {e}")

    # Conversation persistence: POST /api/conversations creates the row
    # before the session starts — attach by the id the dispatch metadata
    # carries (exact), falling back to room_name lookup (auto-dispatch),
    # and create only for rooms started outside the API (lk CLI, tests).
    conversation_id = uuid.uuid4()
    user_id = meta.get("user_id")
    try:
        pool = await db.get_pool(dsn)
        row = None
        if meta.get("conversation_id"):
            row = await pool.fetchrow(
                "SELECT id, metadata FROM conversations WHERE id = $1",
                uuid.UUID(meta["conversation_id"]),
            )
        if row is None:
            row = await pool.fetchrow(
                "SELECT id, metadata FROM conversations WHERE room_name = $1"
                " AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
                ctx.room.name,
            )
        if row:
            conversation_id = row["id"]
            if row["metadata"]:
                user_id = user_id or json.loads(row["metadata"]).get("user_id")
        else:
            await pool.execute(
                "INSERT INTO conversations (id, room_name) VALUES ($1, $2)",
                conversation_id,
                ctx.room.name,
            )
    except Exception as e:
        logger.warning(f"conversation row skipped: {e}")

    # Long-term memory: recall once (hard timeout inside), append to the
    # system prompt; each user turn is queued for fact extraction below.
    mem = None
    mem_cfg = cfg.get("memory", {})
    # {agent_name} / {business_name} are filled here, per call, so a rename
    # on the Options screen reaches the model without a prompt edit.
    instructions = config.render_vars(
        cfg.get("instructions", "You are a helpful voice assistant."), cfg
    )
    if mem_cfg.get("enabled") and keys.get("mem0") and not user_id:
        # No caller identity (lk CLI, tests, rooms started outside the API):
        # skip memory rather than pooling every anonymous caller into one
        # shared bucket — caller A's remembered details must never surface
        # in caller B's call.
        logger.info("memory skipped: no user identity for this session")
    elif mem_cfg.get("enabled") and keys.get("mem0"):
        try:
            mem = memory_mod.Memory(
                keys["mem0"],
                user_id,
                agent_id=cfg.get("id"),
                scope=mem_cfg.get("scope", "agent"),
            )
            notes = await mem.recall(
                catalog.get("prompt_blocks")["memory_recall_query"].format(
                    agent_name=cfg.get("name", "assistant")
                ),
                limit=mem_cfg.get("recall_limit", 5),
                timeout=mem_cfg.get("recall_timeout_s", 2.0),
            )
            instructions += memory_mod.memory_block(notes)
            if notes:
                logger.info(f"memory: recalled {len(notes)} note(s)")
        except Exception as e:
            logger.warning(f"memory disabled for this call: {e}")
            mem = None

    # Embedding warmup: the first embed call of a session pays cold-start
    # costs (ollama loading the model into memory) and can blow the RAG
    # timeout on the session's first turn. Fire-and-forget during setup.
    if cfg.get("rag", {}).get("enabled"):
        async def _warm_embed():
            try:
                await _retrieve("warmup", cfg, keys)
            except Exception as e:
                logger.debug(f"embed warmup failed (non-fatal): {e}")
        asyncio.create_task(_warm_embed())

    # NOTE: the model has NO way to end the call — no end_call tool, no
    # goodbye inference. Calls end only via the UI's End-call button or the
    # inactivity watchdog below.
    if cfg.get("provider") == "local":
        # Small local models sometimes leak their chat template's role
        # header into the reply (seen live: replies starting "assistant").
        instructions += catalog.get("prompt_blocks")["local_role_label_rule"]

    # Web-search results are stashed here by the tool so the answer's
    # sources can be published as chips, symmetric with RAG citations.
    web_stash: list[dict] = []
    # Structured option groups (flights, hotels, rates) land here and are
    # published per assistant turn on arya.options — the transcript renders
    # them as cards next to the spoken sentence.
    options_stash: list[dict] = []
    # Handles a tool needs to save and announce per-call artifacts (the
    # written itinerary): identifiers + the room for text-stream publishes.
    tool_runtime = {
        "conversation_id": str(conversation_id),
        "agent_id": cfg.get("id"),
        "dsn": dsn,
        "room": ctx.room,
        # Present only for calls that arrived through an embedded widget;
        # builder test calls have none. Drives the itinerary theme.
        "deployment_id": meta.get("deployment_id"),
        # Which channel this session arrived on and who is on the other end
        # (the WhatsApp number, digits). The order desk identifies the
        # customer from these instead of asking, so a user who messaged from
        # a known phone is greeted by name.
        "channel": meta.get("channel"),
        "external_user": meta.get("external_user"),
        # Connector results (Duffel) hand the worker option cards and the
        # written itinerary through connectors.spoken_result, which needs
        # the same stash the sample desk appends to, and the business name
        # the module cannot know.
        "options_stash": options_stash,
        "brand": {
            "name": (cfg.get("business") or {}).get("business_name") or cfg.get("name") or "",
            "icon": cfg.get("icon") or "",
        },
        # Where the business wants its own copy of a booking. Empty means
        # the mailbox the email connector sends from, which the connector
        # card reports after a Test.
        "copy_bookings_to": (cfg.get("business") or {}).get("copy_bookings_to") or "",
    }
    # Public/embed call (came through a pk_ key)? Two consequences, both
    # publish-only (everything is still computed, stored on the messages
    # row, and counted for grounding metrics; skipping the computation
    # would silently break observability):
    #   - DOC citations (filenames + verbatim excerpts) belong to the
    #     agent's owner, not to site visitors, and are never published to
    #     these rooms.
    #   - WEB citations (public pages the answer drew on) ARE published to
    #     everyone: the voice rules stop the agent reading URLs aloud, so
    #     the source cards are a visitor's only way to reach a link.
    is_public_session = bool(meta.get("deployment_id"))
    # A messaging channel (WhatsApp) rather than a live call. The whole
    # agent loop is identical; what changes is that there is no audio in
    # either direction, no spoken greeting, and no inactivity hang-up
    # (silence is normal in a chat thread, and the bridge owns that clock).
    channel = meta.get("channel")
    # A connector marked as replacing a sample toolset switches that toolset
    # off, so the model cannot answer from the canned desk while a real
    # system is attached (see connectors.replaced_toolsets for why this is
    # decided by the config rather than by which connectors came up).
    # The connection rows are loaded once, here, so a ticked tool the
    # connection no longer lists (a stale Options page saving over a Test)
    # cannot switch a sample desk off, and build_toolsets reuses them.
    conn_rows: dict | None = None
    if cfg.get("connectors"):
        try:
            conn_rows = {
                r["id"]: r
                for r in await connectors.load_attached(await db.get_pool(dsn), cfg)
            }
        except Exception as e:
            logger.warning(f"connections not loaded for the replacement check: {e}")
    replaced = connectors.replaced_toolsets(cfg, rows=conn_rows)
    if replaced:
        logger.info(f"sample tools replaced by connectors: {sorted(replaced)}")
    # A database with the order desk's own tables serves the desk instead of
    # replacing it: the desk runs over that connection's string, set on
    # Options like any connector, with no worker-wide setting involved.
    served = connectors.served_toolsets(cfg, conn_rows)
    if "orders" in served:
        s = served["orders"]
        tool_runtime["shop_dsn"] = connectors.secret_value(
            s["entry"], s["row"].get("secret"))
        logger.info(f"order desk uses connection {s['row'].get('name')!r}")
    tool_list = tools_mod.build_tools(
        cfg, keys, web_stash, options_stash, tool_runtime, skip=replaced
    )
    # Choice cards ("choose one of the following") for every agent, no
    # toggle. Delete this line to take the ask_choice tool away again.
    tool_list.append(tools_mod.choice_tool(tool_runtime))
    # Connectors (MCP): one toolset per external system attached to this
    # agent, connected HERE rather than left to the SDK so that (a)
    # has_tools is true only when a connector actually delivered tools
    # (gotcha 4: parallel_tool_calls with no tools is a 400) and (b) a tool
    # name colliding with a native tool is filtered before Agent.__init__
    # would raise on it. A connector that fails to connect is skipped and
    # logged; the call still starts. Public (embed) sessions only get the
    # tools the owner marked public.
    mcp_toolsets: list = []
    live_connectors: list[dict] = []
    if cfg.get("connectors"):
        try:
            mcp_toolsets, live_connectors = await connectors.build_toolsets(
                cfg, await db.get_pool(dsn), keys,
                is_public=is_public_session,
                taken_names=connectors.native_tool_names(tool_list),
                runtime=tool_runtime,
                rows=conn_rows,
            )
        except Exception as e:
            logger.warning(f"connectors skipped: {e}")
        instructions += connectors.prompt_block(
            live_connectors, dead=connectors.dead_jobs(cfg, live_connectors)
        )
        # Built here and not in build_tools because it exists only when a
        # connector that can send mail actually came up: a tool that is
        # always going to fail is worse than none, since the agent keeps
        # offering it. The body never passes through the model.
        # Kept on the runtime so a result resolver, which runs after the
        # model's turn, can send a booking confirmation without the model.
        tool_runtime["connectors"] = live_connectors
        mail = itinerary_email.tool(live_connectors, tool_runtime)
        if mail is not None:
            tool_list.append(mail)
        # The day plan, built the way the sample desk always built it: the
        # worker looks the places up and the module lays them out. Only
        # when both halves came up, and its instruction line goes with it
        # because a catalog prompt block cannot know that.
        planner = trip_planner.tool(live_connectors, tool_runtime)
        if planner is not None:
            tool_list.append(planner)
            instructions += trip_planner.prompt_block()
    has_tools = bool(tool_list) or any(c["tool_names"] for c in live_connectors)
    # A channel thread is text in, text out: no speech stages at all (see
    # providers._session for the Gemini reason).
    session = providers.build_session(
        cfg, keys, has_tools=has_tools, text_only=bool(channel))
    if channel:
        # Same prompt, different output contract. The voice rules exist to
        # make a text-to-speech answer sound natural (no lists, digits in
        # spoken groups, never read a URL aloud) and every one of them
        # makes a WhatsApp reply worse. Instructions carries both variants
        # and the framework picks per turn, so a future voice call on the
        # same agent still gets the spoken wording.
        blocks = catalog.get("prompt_blocks")
        instructions = lk_beta.Instructions(
            audio=instructions,
            text=(_without_spoken_output_rules(instructions, blocks)
                  + blocks["channel_text_rules"]
                  + blocks["channel_media_rules"]),
        )
    agent = Assistant(cfg, keys, instructions, tool_list + mcp_toolsets)

    _mem_writes = 0
    _mem_cap = mem_cfg.get("max_writes_per_call", 20)
    # Last time the USER did anything (spoken or typed) — the inactivity
    # watchdog reads this. Monotonic clock; starts at session setup.
    _activity = {"t": time.monotonic()}
    _t_session_start = time.monotonic()  # wall-clock cap baseline
    _cit_seq = {"n": 0}
    _opts_held = {"n": 0}  # bubbles skipped while option cards wait for the right one
    _last_user = {"text": ""}  # the caller's most recent turn, for the link resolver
    _last_web = {"cits": []}  # last turn's web citations, so "send me that link" one turn later still resolves
    _greet = {"text": ""}  # the spoken greeting, so its bubble gets the template's openers
    # Why the call ended, for the Insights endings breakdown. Writers, in
    # priority order: the two watchdog paths below, then the participant
    # disconnect listener (which only fills a still-empty value, so a
    # watchdog decision is never overwritten by the ROOM_DELETED event its
    # own room deletion causes). The End-call button also stamps
    # "user_ended" API-side. Anything left NULL reads as "unknown", which
    # is deliberately not a claim about what happened.
    _end_reason = {"v": None}

    @session.on("conversation_item_added")
    def _on_item_added(ev):
        _record_item(ev.item)

    def _record_item(item):
        """Everything a finished turn is recorded into: History, memory, the
        link resolver's last-user text, the activity clock, source chips.
        Called by the session event, and directly for a user turn that was
        inserted into the context by hand (typed text with excerpts, a
        photo): those never pass through conversation_item_added, and seen
        live, a whole WhatsApp conversation was stored with Flux's replies
        and none of the customer's messages."""
        nonlocal _mem_writes
        m = getattr(item, "metrics", None)
        if m is not None:
            logger.info(f"turn metrics ({item.role}): {m}")
        role = getattr(item, "role", None)
        text = _without_excerpts(getattr(item, "text_content", None) or "")
        if role == "user":
            _activity["t"] = time.monotonic()
            _last_user["text"] = text
        # Memory: user turns only (each add is a Mem0-side LLM call),
        # capped per call via memory.max_writes_per_call.
        if mem is not None and role == "user" and _mem_writes < _mem_cap:
            _mem_writes += 1
            mem.remember("user", text)
        # Persistence: every turn -> messages row, fire-and-forget and
        # exception-swallowed — a DB hiccup must never break a live call.
        if role not in ("user", "assistant"):
            return

        # Capture this answer's sources once, for both consumers: the
        # Postgres row AND a live text-stream event the frontend renders
        # as chips under the agent's bubble. RAG citations (📄) and
        # web-search results (🌐) ride the same event.
        cits = None
        opts = None
        links_out = None
        if role == "assistant":
            rag_cits = getattr(agent, "_last_citations", None) or []
            if rag_cits:
                agent._last_citations = []
                # Only chip sources the answer actually drew from —
                # retrieval alone isn't evidence (seen live: an unrelated
                # answer chipped just because chunks scored above 0.55).
                used = _used_in_answer(text, rag_cits)
                if len(used) != len(rag_cits):
                    logger.info(
                        f"overlap check: kept {len(used)}/{len(rag_cits)}"
                        " citation(s) for this answer"
                    )
                rag_cits = used
                if used:
                    agent._ground_used += 1
                # What the judge will read for this answer: the excerpts
                # it was given, and whether the overlap check saw them used.
                pending = getattr(agent, "_pending_context", None)
                agent._pending_context = None
                if pending:
                    agent._kb_turns.append(
                        {"head": _head(text), "used": bool(used), **pending}
                    )
            web_cits = [{"type": "web", **r} for r in web_stash]
            web_stash.clear()
            combined = rag_cits + web_cits
            if combined:
                # Persisted whole, always: History and metrics see everything.
                cits = combined
                # Published selectively: visitors on embed/share-link calls
                # get the web sources only (doc names + excerpts stay
                # server-side), the builder gets both.
                visible = web_cits if is_public_session else combined
                if visible:
                    _cit_seq["n"] += 1
                    # head = the answer's opening words, so the UI can attach
                    # the cards to the right bubble by CONTENT match; time-
                    # based pairing raced against TTS/transcription ordering.
                    head = _head(text)

                    async def _publish(payload=visible, seq=_cit_seq["n"], h=head):
                        try:
                            await ctx.room.local_participant.send_text(
                                json.dumps(
                                    {"citations": payload, "seq": seq, "head": h}
                                ),
                                topic="arya.citations",
                            )
                        except Exception as e:
                            logger.debug(f"citation publish skipped: {e}")

                    asyncio.create_task(_publish())

            # The one link this answer gives, verified against the search
            # results it drew on: never the full result list (see
            # links.resolve_links). Published to every session, public
            # included: these are public pages, and the voice rules keep
            # the agent from reading a full address aloud, so the chip is
            # how a caller (or a visitor) actually reaches it. A bare
            # follow-up ("send me that link") one turn later still
            # resolves against the PREVIOUS turn's web citations.
            web_for_links = web_cits or _last_web["cits"]
            resolved_links = links_mod.resolve_links(text, _last_user["text"], web_for_links) \
                if web_for_links else []
            if resolved_links:
                _cit_seq["n"] += 1
                link_head = " ".join(text.split())[:80]

                async def _publish_links(payload=resolved_links, seq=_cit_seq["n"], h=link_head):
                    try:
                        await ctx.room.local_participant.send_text(
                            json.dumps({"links": payload, "seq": seq, "head": h}),
                            topic="arya.links",
                        )
                    except Exception as e:
                        logger.debug(f"links publish skipped: {e}")

                asyncio.create_task(_publish_links())
                links_out = resolved_links
            _last_web["cits"] = web_cits

            # Quick replies under this bubble: the template's openers under
            # the greeting, otherwise chips derived from the question the
            # reply ends with (suggestions.derive, no model call). The
            # transcript shows them under the latest bubble only and drops
            # them as soon as the caller speaks or types.
            try:
                norm_text = " ".join(text.split())
                if _greet["text"] and norm_text[:60] == " ".join(_greet["text"].split())[:60]:
                    chips = suggestions_mod.clean_openers(cfg.get("openers"))
                else:
                    chips = suggestions_mod.derive(text)
            except Exception:
                chips = []
            if chips:
                _cit_seq["n"] += 1
                sugg_head = norm_text[:80]

                async def _publish_sugg(payload=chips, seq=_cit_seq["n"], h=sugg_head):
                    try:
                        await ctx.room.local_participant.send_text(
                            json.dumps({"chips": payload, "seq": seq, "head": h}),
                            topic="arya.suggestions",
                        )
                    except Exception as e:
                        logger.debug(f"suggestions publish skipped: {e}")

                asyncio.create_task(_publish_sugg())

            # Structured option groups (travel searches) ride their own
            # topic, same head-matching contract as citations so the UI
            # attaches the cards to the right bubble.
            # Skip at most one bubble that does not mention the options (the
            # filler before a slow search); never hold cards back twice.
            if options_stash and (
                _names_an_option(text, options_stash) or _opts_held["n"] >= 1
            ):
                _opts_held["n"] = 0
                opts = list(options_stash)
                options_stash.clear()
                _cit_seq["n"] += 1
                opt_head = " ".join(text.split())[:80]

                async def _publish_opts(payload=opts, seq=_cit_seq["n"], h=opt_head):
                    try:
                        await ctx.room.local_participant.send_text(
                            json.dumps({"options": payload, "seq": seq, "head": h}),
                            topic="arya.options",
                        )
                    except Exception as e:
                        logger.debug(f"options publish skipped: {e}")

                asyncio.create_task(_publish_opts())

        async def _write(item=item, cits=cits, opts=opts, links_out=links_out):
            try:
                pool = await db.get_pool(dsn)
                im = getattr(item, "metrics", None)
                if isinstance(im, dict) and item.role == "assistant":
                    # Two facts the SDK report lacks, stored beside it so
                    # the transcript can show them per bubble: whether the
                    # caller cut this reply off (barge-in), and the tools
                    # that ran to produce it with their durations. The
                    # function_tools_executed event fires before the reply
                    # item lands, so the timings wait in _pending_tools
                    # and ride with the next assistant message.
                    im = {
                        **im,
                        "interrupted": bool(getattr(item, "interrupted", False)),
                    }
                    if _pending_tools:
                        im["tools"] = list(_pending_tools)
                        _pending_tools.clear()
                citations = json.dumps(cits) if cits else None
                await pool.execute(
                    "INSERT INTO messages (conversation_id, role, content,"
                    " citations, options, links, metrics) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    conversation_id,
                    item.role,
                    _without_excerpts(item.text_content or ""),
                    citations,
                    json.dumps(opts) if opts else None,
                    json.dumps(links_out) if links_out else None,
                    json.dumps(im, default=str)
                    if isinstance(im, dict)
                    else (
                        json.dumps(im.model_dump(mode="json"), default=str)
                        if im is not None and hasattr(im, "model_dump")
                        else None
                    ),
                )
            except Exception as e:
                logger.warning(f"transcript write skipped: {e}")

        asyncio.create_task(_write())

    # Connector tool calls -> tool_events rows (the audit trail for actions
    # taken in an external system) + one log line each. Fire-and-forget,
    # exception-swallowed, same contract as transcript writes.
    if live_connectors:
        async def _write_tool_event(conn_id, tool, arguments, ok, error, latency_ms):
            try:
                args = None
                if arguments:
                    raw = arguments[:2000]
                    try:
                        args = json.loads(raw)
                        if not isinstance(args, dict):
                            args = {"value": args}
                    except json.JSONDecodeError:
                        args = {"raw": raw}
                pool = await db.get_pool(dsn)
                await pool.execute(
                    "INSERT INTO tool_events (room_name, agent_id, conversation_id,"
                    " connection_id, tool, arguments, ok, error, latency_ms)"
                    " VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)",
                    ctx.room.name, cfg.get("id"), conversation_id, conn_id, tool,
                    json.dumps(args) if args is not None else None,
                    ok, error, latency_ms,
                )
            except Exception as e:
                logger.debug(f"tool event write skipped: {e}")

        @session.on("function_tools_executed")
        def _on_tools_executed(ev):
            for call, out in ev.zipped():
                owner = connectors.owner_of(call.name, live_connectors)
                if owner is None:
                    continue  # a native tool; those log themselves
                ok = out is not None and not out.is_error
                latency_ms = (
                    int((out.created_at - call.created_at) * 1000)
                    if out is not None else None
                )
                error = (out.output or "")[:300] if (out is not None and out.is_error) else None
                logger.info(
                    f"tool call: {call.name} ({owner['row']['name']}) ok={ok}"
                    + (f" {latency_ms}ms" if latency_ms is not None else "")
                )
                asyncio.create_task(_write_tool_event(
                    owner["row"]["id"], call.name, call.arguments, ok, error, latency_ms,
                ))

    # Per-stage metrics (STT/LLM/TTS/VAD/EOU) as one JSON line per event —
    # the consumption dataset costing and dashboards are built on.
    metrics_dir = config.BACKEND_DIR / "metrics"
    metrics_dir.mkdir(exist_ok=True)
    global _session_label
    _session_label = f"{ctx.room.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    metrics_path = metrics_dir / f"{_session_label}.jsonl"

    # Tool timings waiting for the assistant reply they belong to (see the
    # transcript write in _on_item_added).
    _pending_tools: list[dict] = []

    async def _write_turn_metric(row: dict, raw: dict | None) -> None:
        """Durable copy of one stage metric (turn_metrics). Fire-and-forget,
        exception-swallowed, same contract as the transcript and RAG writes:
        a DB hiccup must never touch a live call."""
        try:
            pool = await db.get_pool(dsn)
            await pool.execute(
                "INSERT INTO turn_metrics (room_name, agent_id, metric_type,"
                " speech_id, stage_ms, duration_ms, audio_ms, chars,"
                " tokens_in, tokens_out, model, provider, cancelled, raw)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,"
                " $13, $14)",
                ctx.room.name,
                cfg.get("id"),
                row["metric_type"],
                row.get("speech_id"),
                row.get("stage_ms"),
                row.get("duration_ms"),
                row.get("audio_ms"),
                row.get("chars"),
                row.get("tokens_in"),
                row.get("tokens_out"),
                row.get("model"),
                row.get("provider"),
                bool(row.get("cancelled", False)),
                json.dumps(raw, default=str) if raw is not None else None,
            )
        except Exception as e:
            logger.debug(f"turn metric write skipped: {e}")

    @session.on("metrics_collected")
    def _on_metrics_collected(ev):
        m = ev.metrics
        record = {"metric_type": type(m).__name__}
        if hasattr(m, "model_dump"):
            record.update(m.model_dump(mode="json"))
        elif dataclasses.is_dataclass(m):
            record.update(dataclasses.asdict(m))
        else:
            record["data"] = str(m)
        # Container-local JSONL stays as a debug artifact …
        with metrics_path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        # … and the durable row goes to Postgres, where the dashboard can
        # aggregate per stage and the numbers survive rebuilds.
        try:
            asyncio.create_task(
                _write_turn_metric(_turn_metric_row(record), record)
            )
        except RuntimeError:
            pass  # no running loop (unit tests); the JSONL line still exists

    @session.on("function_tools_executed")
    def _on_tools_timed(ev):
        # Tool time sits inside the end-to-end latency and otherwise gets
        # blamed on the model. One turn_metrics row per call with the tool
        # name in `model` (Insights shows p50 per tool), and the same
        # timing stashed for the transcript badge of the reply it fed.
        for call, out in ev.zipped():
            if out is None:
                continue
            ms = max(0, int((out.created_at - call.created_at) * 1000))
            failed = bool(getattr(out, "is_error", False))
            _pending_tools.append({"name": call.name, "ms": ms, "ok": not failed})
            try:
                asyncio.create_task(_write_turn_metric(
                    {"metric_type": "ToolCall", "model": call.name,
                     "stage_ms": ms, "cancelled": failed},
                    {"name": call.name, "ok": not failed},
                ))
            except RuntimeError:
                pass

    # Pipeline errors (STT, LLM, TTS failures) and how fast the agent stops
    # when the caller talks over it: same durable stream, same
    # fire-and-forget contract, never blocks a call.
    worker_observability.attach(session, _write_turn_metric, logger)

    # Typed chat parity: the library's default text handler calls
    # generate_reply() directly, which SKIPS on_user_turn_completed — so a
    # typed question got no RAG injection or memory recall while the same
    # question spoken was fully grounded (seen live: "I don't have that
    # information" typed vs a correct cited answer spoken). This callback
    # mirrors the default one (claim the turn, interrupt) and then runs the
    # same per-turn hook the audio path runs, persisting its chat-context
    # edits (the injected KB excerpts) before generating the reply.
    async def _typed_input_cb(sess, ev):
        async with sess._claim_user_turn():
            await sess.interrupt()
            user_msg = lk_llm.ChatMessage(role="user", content=[ev.text])
            turn_ctx = agent.chat_ctx.copy()
            try:
                await agent.on_user_turn_completed(turn_ctx, user_msg)
            except Exception as e:
                logger.warning(f"typed-turn hook skipped: {e}")
            # The hook appends the knowledge excerpts to user_msg itself, so
            # the message is inserted as built (same as the photo path) and
            # generate_reply(user_input=...) is not used: it would rebuild the
            # turn from the bare text and drop the excerpts.
            turn_ctx.insert(user_msg)
            try:
                await agent.update_chat_ctx(turn_ctx)
            except Exception as e:
                logger.warning(f"typed-turn context not applied: {e}")
            # Inserted by hand, so the session never announces it: record it
            # here, before the reply, so History keeps the order.
            _record_item(user_msg)
            sess.generate_reply()

    await session.start(
        agent=agent,
        room=ctx.room,
        # A channel session publishes no audio track and subscribes to
        # none: the only participant is a bridge relaying text. Leaving
        # audio on would start a TTS synthesis nobody can hear and bill
        # for it.
        room_input_options=RoomInputOptions(
            text_input_cb=_typed_input_cb,
            **({"audio_enabled": False} if channel else {}),
        ),
        **(
            {"room_output_options": RoomOutputOptions(audio_enabled=False)}
            if channel else {}
        ),
    )
    # First message: the configured greeting (generated from the
    # questionnaire, editable on the Preview screen) is spoken
    # word-for-word through the agent's own TTS — works on every provider.
    # No greeting configured (older agents) -> the LLM improvises one.
    #
    # A channel session gets none of this. The customer opened the thread
    # with an actual question, and answering "Hi, you've reached support,
    # how can I help?" first would be an extra WhatsApp message that
    # ignores what they just asked (and, from October 2026, a billable
    # one). Their message is already queued and becomes the first turn.
    if not channel:
        greeting = config.render_vars((cfg.get("greeting") or "").strip(), cfg)
        if greeting:
            _greet["text"] = greeting
            await session.say(greeting)
        else:
            await session.generate_reply(
                instructions=catalog.get("prompt_blocks")["greet_instruction"]
            )
    else:
        # A chat thread is continuous to the customer even though each
        # quiet spell closes the room behind it. Replay the tail of their
        # previous session so "what about the other one?" still resolves
        # an hour later, then tell the bridge the agent is listening.
        await _replay_channel_history(dsn, meta, conversation_id, agent)

        # Photos arrive on their own byte stream rather than as text,
        # because an image cannot travel on lk.chat and because the bridge
        # owns every network hop. This turns one into an ordinary user
        # turn carrying both the caption and the picture, so RAG, memory
        # and the reply path are the same code the typed path uses.
        _attach_tasks: set[asyncio.Task] = set()

        async def _image_turn(data: bytes, attrs: dict) -> None:
            caption = (attrs.get("caption") or "").strip()
            # The tag is what History, memory and the link resolver see, so
            # a photo leaves a trace even though the image itself is not
            # persisted. catalog's channel_media_rules explains it to the
            # model.
            text = (f"[photo] {caption}" if caption
                    else "[photo] The customer sent this image.")
            content: list = [text]
            if config.vision_capable(cfg):
                content.append(lk_llm.ImageContent(
                    image="data:image/jpeg;base64,"
                          + base64.b64encode(data).decode(),
                    # Low detail keeps a support photo at a few hundred
                    # tokens. Fine for "is this the right cable"; raise it
                    # if the photos turn out to be screenshots of text.
                    inference_detail="low",
                ))
            else:
                # The bridge already refuses in this case, so reaching here
                # means the agent's model changed mid-thread. Say so rather
                # than answering as if the picture had been seen.
                content = [text + " (You cannot see images on this setup;"
                                  " say so and ask them to describe it.)"]
            async with session._claim_user_turn():
                await session.interrupt()
                user_msg = lk_llm.ChatMessage(role="user", content=content)
                turn_ctx = agent.chat_ctx.copy()
                try:
                    await agent.on_user_turn_completed(turn_ctx, user_msg)
                except Exception as e:
                    logger.warning(f"photo-turn hook skipped: {e}")
                # Unlike the typed path, the user message is inserted here
                # rather than left to generate_reply(user_input=...), which
                # takes a string and would drop the image part.
                turn_ctx.insert(user_msg)
                await agent.update_chat_ctx(turn_ctx)
                # Recorded by hand for the same reason as the typed path;
                # this also sets the last-user text and the activity clock.
                _record_item(user_msg)
                session.generate_reply()

        async def _read_attachment(reader) -> None:
            attrs = dict(getattr(reader.info, "attributes", None) or {})
            chunks: list[bytes] = []
            size = 0
            async for chunk in reader:
                size += len(chunk)
                if size > MAX_ATTACHMENT_BYTES:
                    logger.warning("attachment over the cap, dropped")
                    return
                chunks.append(chunk)
            if chunks:
                await _image_turn(b"".join(chunks), attrs)

        def _on_attachment(reader, _participant_identity: str) -> None:
            task = asyncio.create_task(_read_attachment(reader))
            _attach_tasks.add(task)
            task.add_done_callback(_attach_tasks.discard)

        with contextlib.suppress(Exception):
            ctx.room.register_byte_stream_handler("arya.attachments",
                                                  _on_attachment)
        with contextlib.suppress(Exception):
            await ctx.room.local_participant.send_text("1", topic="arya.ready")
    _activity["t"] = time.monotonic()  # the clock starts after the greeting

    # Inactivity watchdog — the ONLY automatic way a call ends (the other
    # path is the UI's End-call button). No verbal cue can hang up. A
    # gentle warning fires 30s before the cutoff, then a sign-off, then
    # the room is deleted.
    inact_s = cfg.get("session", {}).get("inactivity_timeout_s", 180)

    async def _inactivity_watchdog():
        warned = False
        while True:
            await asyncio.sleep(5)
            idle = time.monotonic() - _activity["t"]
            if idle < max(10.0, inact_s - 30):
                warned = False
                continue
            # Wall-clock cap (public embeds): warn, sign off, delete —
            # never a hard cut. meta carries it from the deployment row.
            cap = meta.get("max_session_seconds")
            if cap and time.monotonic() - _t_session_start > float(cap):
                logger.info(f"session cap ({cap}s) — ending the call")
                _end_reason["v"] = "time_limit"
                with contextlib.suppress(Exception):
                    handle = session.say(
                        (cfg.get("session", {}) or {}).get("signoff_text")
                        or catalog.get("prompt_blocks")["inactivity_signoff"]
                    )
                    await handle
                with contextlib.suppress(Exception):
                    await ctx.api.room.delete_room(
                        lk_api.DeleteRoomRequest(room=ctx.room.name)
                    )
                return
            if idle < inact_s:
                if not warned:
                    warned = True
                    with contextlib.suppress(Exception):
                        # Spoken to end users — per-agent override first
                        # (session.inactivity_warning_text), store default
                        # otherwise, so a customer's agent warns in the
                        # customer's own voice.
                        session.say(
                            (cfg.get("session", {}) or {}).get(
                                "inactivity_warning_text"
                            )
                            or catalog.get("prompt_blocks")["inactivity_warning"]
                        )
                continue
            logger.info(f"inactivity timeout ({inact_s}s) — ending the call")
            _end_reason["v"] = "inactivity"
            with contextlib.suppress(Exception):
                handle = session.say(
                    (cfg.get("session", {}) or {}).get("signoff_text")
                    or catalog.get("prompt_blocks")["inactivity_signoff"]
                )
                await handle
            with contextlib.suppress(Exception):
                await ctx.api.room.delete_room(
                    lk_api.DeleteRoomRequest(room=ctx.room.name)
                )
            return

    # How the caller's connection actually went away, for the endings card.
    # LiveKit reports a per-participant disconnect_reason; only two buckets
    # are honest enough to show:
    #   CLIENT_INITIATED  the browser SDK sent a clean leave. That is the
    #                     End button AND a closed tab or navigation (the
    #                     SDK leaves on page unload), so both read as a
    #                     deliberate hang-up.
    #   timeout/failure   no leave arrived and the server gave up on the
    #                     participant: network drop, sleeping device,
    #                     killed process.
    # Everything else (ROOM_DELETED from our own watchdog deletion,
    # migrations, shutdowns) is intentionally NOT mapped: those either
    # already have a better reason recorded or say nothing about the
    # caller. A backgrounded mobile browser can skip its unload handler
    # and surface as a timeout, so "connection lost" is a best effort
    # signal and is labeled softly in the UI rather than as a fact.
    # ParticipantKind is a protobuf enum wrapper, so members are read by
    # name through Value() rather than as attributes.
    _AGENT_KIND = rtc.ParticipantKind.Value("PARTICIPANT_KIND_AGENT")
    _DR = rtc.DisconnectReason
    _LOST = {
        _DR.CONNECTION_TIMEOUT,
        _DR.MEDIA_FAILURE,
        _DR.SIGNAL_CLOSE,
        _DR.STATE_MISMATCH,
        _DR.JOIN_FAILURE,
    }

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(participant):
        # Agents are participants too; only the human's exit is a call
        # ending. Filter on kind when the SDK reports it, and never
        # overwrite a reason the watchdog or the API already decided.
        kind = getattr(participant, "kind", None)
        if kind is not None and kind == _AGENT_KIND:
            return
        if _end_reason["v"]:
            return
        reason = getattr(participant, "disconnect_reason", None)
        if reason == _DR.CLIENT_INITIATED:
            _end_reason["v"] = "user_ended"
        elif reason in _LOST:
            _end_reason["v"] = "connection_lost"
        logger.info(
            f"participant left: reason={reason} -> {_end_reason['v'] or 'unclassified'}"
        )

    # Not on a messaging channel. Silence in a WhatsApp thread means the
    # customer is busy, not that the conversation is over, and a spoken
    # "are you still there?" written into a chat reads as a bot talking to
    # itself. The bridge decides when a thread has gone quiet and leaves
    # the room; the empty-room timeout then ends this job normally.
    watchdog = None if channel else asyncio.create_task(_inactivity_watchdog())

    async def _cancel_watchdog():
        if watchdog is not None:
            watchdog.cancel()

    ctx.add_shutdown_callback(_cancel_watchdog)

    # End-of-call cumulative usage summary (per-model tokens/requests).
    async def _log_session_usage():
        usage = getattr(session, "usage", None)
        logger.info(f"session usage summary: {usage}")
        # Structured, not str(usage): AgentSessionUsage's repr silently
        # drops zero-valued fields, which made the old string unparseable.
        try:
            usage_json = [
                u.model_dump(mode="json") for u in usage.model_usage
            ] if usage else []
        except Exception:
            usage_json = []
        patch = {"usage": usage_json}
        if _end_reason["v"]:
            # Only when a watchdog set it — never overwrite the API's
            # "user_ended" stamp with a null.
            patch["ended_reason"] = _end_reason["v"]
        if user_id:
            patch["user_id"] = user_id
        try:
            pool = await db.get_pool(dsn)
            await pool.execute(
                # MERGE into metadata (never replace): the row already
                # carries agent_id/agent_name/user_id stamped at creation —
                # a plain assignment wiped them and History fell back to
                # showing the room id.
                "UPDATE conversations SET ended_at = now(),"
                " metadata = COALESCE(metadata, '{}'::jsonb) || $2::jsonb"
                " WHERE id = $1",
                conversation_id,
                json.dumps(patch, default=str),
            )
        except Exception as e:
            logger.warning(f"conversation close skipped: {e}")

    ctx.add_shutdown_callback(_log_session_usage)

    # AI-judged evals (opt-in, cfg.evals.enabled): after the call, a
    # JudgeGroup reads the finished transcript and grades it — one LLM call
    # per judge, on the agent's own key (providers.build_judge_llm). The
    # session-end callback is the framework-blessed place for this;
    # fire-and-forget semantics, a failure only logs.
    async def _run_evals():
        evals_cfg = cfg.get("evals", {}) or {}
        if not evals_cfg.get("enabled"):
            return
        try:
            from livekit.agents.evals import (
                Judge,
                JudgeGroup,
                JudgmentResult,
                coherence_judge,
                conciseness_judge,
                task_completion_judge,
                tool_use_judge,
            )
        except ImportError as e:
            logger.warning(f"evals skipped: SDK has no evals module ({e})")
            return
        history = session.history
        # The conversation as the transcript shows it: user and assistant
        # messages with text, minus the knowledge-base excerpts the RAG step
        # injects as assistant-role context (those are not replies).
        preamble = catalog.get("prompt_blocks")["kb_injection_preamble"]
        turns: list[tuple[str, str]] = []
        for it in history.items:
            role = getattr(it, "role", None)
            text = _without_excerpts(getattr(it, "text_content", None) or "").strip()
            if role not in ("user", "assistant") or not text:
                continue
            if text.startswith(preamble):
                continue
            turns.append((role, text))
        if not any(r == "user" for r, _ in turns):
            logger.info("evals skipped: no user turns to judge")
            return
        judge_llm = providers.build_judge_llm(cfg, keys)
        if judge_llm is None:
            logger.info(
                f"evals skipped: provider {cfg.get('provider')} has no judge"
            )
            return
        # Second-chance model for checks the primary fails on (rate limit,
        # model access, a flaky verdict). Only the failed checks re-run.
        fallback_llm = providers.build_judge_llm(cfg, keys, fallback=True)
        deadline = time.monotonic() + float(os.environ.get("EVALS_TIMEOUT_S", "75"))

        def _left(floor: float = 8.0) -> float:
            # One shared budget across every pass, so a slow primary plus a
            # fallback retry can never outlive the worker's shutdown grace.
            return max(floor, deadline - time.monotonic())

        class GroundingJudge(Judge):
            """Deterministic (no LLM call): did the answers that received
            knowledge-base context actually use it? Reads the worker's own
            per-call tally, the same used-in-answer gate the citation
            chips are built on, formalized as a verdict."""

            def __init__(self):
                super().__init__(name="grounding")

            async def evaluate(self, *, chat_ctx, reference=None, llm=None):
                inj, used = agent._ground_injected, agent._ground_used
                if inj == 0:
                    return JudgmentResult(
                        verdict="maybe",
                        reasoning="The knowledge base was never consulted"
                        " this call, so there was nothing to ground.",
                    )
                rate = used / inj
                verdict = "pass" if rate >= 0.75 else (
                    "maybe" if rate >= 0.5 else "fail"
                )
                return JudgmentResult(
                    verdict=verdict,
                    reasoning=f"{used} of {inj} answers that received"
                    " document context visibly used it.",
                )

        # The split. Per-call checks judge the whole conversation once:
        # task completion (no single turn completes a task), conciseness
        # (pacing is a whole-call property), tool usage (choices only make
        # sense in flow context; skipped when the agent has no tools) and
        # coherence, labeled Consistency in the UI (contradictions are
        # cross-turn by nature). Accuracy and relevancy are NOT here: they
        # run per answer below and aggregate worst-turn and mean, which is
        # what catches a single wrong fee quote a whole-call verdict
        # averages away. Grounding rides along when RAG is on and is never
        # scored; voice format is deterministic and never scored either.
        factories = {
            "task_completion": task_completion_judge,
            "conciseness": conciseness_judge,
            "tool_use": tool_use_judge,
            "coherence": coherence_judge,
        }
        wanted = evals_cfg.get("judges") or (
            list(factories) + ["accuracy", "relevancy"]
        )
        call_judges = [
            factories[k]()
            for k in wanted
            if k in factories and (k != "tool_use" or tool_list)
        ]
        per_turn_wanted = [k for k in ("accuracy", "relevancy") if k in wanted]
        if cfg.get("rag", {}).get("enabled"):
            call_judges.append(GroundingJudge())
        if not call_judges and not per_turn_wanted:
            logger.info("evals skipped: no judges selected")
            return

        async def _write_evals(payload: dict):
            try:
                pool = await db.get_pool(dsn)
                await pool.execute(
                    "UPDATE conversations SET"
                    " metadata = COALESCE(metadata, '{}'::jsonb) || $2::jsonb"
                    " WHERE id = $1",
                    conversation_id,
                    json.dumps({"evals": payload}, default=str),
                )
            except Exception as e:
                logger.warning(f"evals write skipped: {e}")

        judgments: dict = {}
        used_fallback: list[str] = []

        # Per-call pass, then the fallback model for whatever dropped out.
        # JudgeGroup swallows per-judge errors and simply omits them, which
        # is exactly the signal for "retry this one elsewhere".
        async def _run_group(llm_, judges_):
            return await asyncio.wait_for(
                JudgeGroup(llm=llm_, judges=judges_).evaluate(history),
                timeout=_left(),
            )

        try:
            if call_judges:
                res = await _run_group(judge_llm, call_judges)
                judgments.update(res.judgments)
                missing = [
                    j.name for j in call_judges
                    if j.name not in judgments and j.name in factories
                ]
                if missing and fallback_llm is not None:
                    res2 = await _run_group(
                        fallback_llm, [factories[n]() for n in missing]
                    )
                    judgments.update(res2.judgments)
                    used_fallback.extend(res2.judgments)
        except Exception as e:
            # The user turned evals ON, so a failure must be diggable from
            # the product, not only from a log line that scrolls away: store
            # the error where the score would have gone and the transcript
            # view says "scoring failed" instead of showing nothing.
            kind = "timeout" if isinstance(e, asyncio.TimeoutError) else type(e).__name__
            logger.warning(f"evals failed: {kind}: {e}")
            await _write_evals({
                "error": f"{kind}: {e}" if str(e) else kind,
                "model": str(getattr(judge_llm, "model", "") or ""),
            })
            return

        # Per-answer pass: accuracy and relevancy in ONE combined LLM call
        # per substantive answer, with the last few exchanges as context
        # instead of the whole transcript. Greeting (no caller turn before
        # it) and closing turns ("thanks, bye") are skipped, which is what
        # keeps a 10-turn call at roughly 7 to 9 judge calls. An answer
        # that received document context gets the excerpts in the same
        # call and three more fields (faithfulness, context relevance,
        # whether it drew on them): no extra call, no extra wait.
        Verdict = Literal["pass", "fail", "maybe"]

        _ARG_DOC = {
            "accuracy": "pass if every fact in the answer is supported by the recent conversation, the provided context, or tool results; fail if it states unsupported facts, contradicts earlier information, or invents details; maybe if unsure.",
            "accuracy_reasoning": "One sentence explaining the accuracy verdict.",
            "relevancy": "pass if the answer addresses what the caller just said; fail if it ignores the caller or goes off topic; maybe if unsure.",
            "relevancy_reasoning": "One sentence explaining the relevancy verdict.",
            "faithfulness": "pass if every claim the answer makes about the business (prices, hours, policies, products, procedures, names) is supported by the document excerpts or by tool results; fail if it states such a claim that the excerpts contradict or do not contain; maybe if unsure, or if the answer makes no such claim.",
            "faithfulness_reasoning": "One sentence explaining the faithfulness verdict.",
            "context_relevance": "pass if the document excerpts contain what is needed to answer what the caller asked; fail if they are about something else; maybe if they cover it only partly.",
            "context_relevance_reasoning": "One sentence explaining the context relevance verdict.",
            "used_context": "true if the answer draws on the document excerpts, in their words or paraphrased; false if it answers from somewhere else or does not answer.",
        }

        def _verdict_tool(with_context: bool):
            # Two signatures, one set of field notes: the judge sees the
            # document fields only for an answer that had document context,
            # so it is never asked to grade excerpts it was not shown.
            if with_context:
                async def submit_turn_verdicts(
                    accuracy: Verdict,
                    accuracy_reasoning: str,
                    relevancy: Verdict,
                    relevancy_reasoning: str,
                    faithfulness: Verdict,
                    faithfulness_reasoning: str,
                    context_relevance: Verdict,
                    context_relevance_reasoning: str,
                    used_context: bool,
                ):
                    return None
            else:
                async def submit_turn_verdicts(
                    accuracy: Verdict,
                    accuracy_reasoning: str,
                    relevancy: Verdict,
                    relevancy_reasoning: str,
                ):
                    return None
            params = inspect.signature(submit_turn_verdicts).parameters
            submit_turn_verdicts.__doc__ = (
                "Submit every verdict for the agent answer under evaluation.\n\n"
                "Args:\n" + "\n".join(f"    {p}: {_ARG_DOC[p]}" for p in params)
            )
            return submit_turn_verdicts, lk_llm.function_tool(submit_turn_verdicts)

        async def _judge_turn_llm(llm_, window, answer, context=None):
            excerpts = _context_block(context)
            raw, tool = _verdict_tool(bool(excerpts))
            convo = "\n".join(
                f"{'Caller' if r == 'user' else 'Agent'}: {t}" for r, t in window[:-1]
            )
            ectx = lk_llm.ChatContext()
            ectx.add_message(
                role="system",
                content=(
                    "You are an evaluator for a voice agent. Judge ONLY the"
                    " final agent answer, using the recent conversation"
                    + (" and the document excerpts the agent was given" if excerpts else "")
                    + " as context. Call submit_turn_verdicts exactly once,"
                    " filling every field."
                ),
            )
            body = f"Recent conversation:\n{convo}\n\n"
            if excerpts:
                body += (
                    "Document excerpts the agent was given for this answer:\n"
                    f"{excerpts}\n\n"
                )
            body += f"Agent answer under evaluation:\n{answer}\n\n"
            body += (
                "Evaluate this answer's accuracy and relevancy, whether it is"
                " faithful to the document excerpts, whether the excerpts were"
                " relevant to what the caller asked, and whether the answer"
                " drew on them."
                if excerpts
                else "Evaluate this answer's accuracy and relevancy."
            )
            ectx.add_message(role="user", content=body)
            model_name = str(getattr(llm_, "model", "") or "")
            extra = {} if "gpt-5" in model_name else {"temperature": 0.0}
            resp = await llm_.chat(
                chat_ctx=ectx,
                tools=[tool],
                tool_choice="required",
                extra_kwargs=extra,
            ).collect()
            if not resp.tool_calls:
                raise ValueError("judge returned no verdict")
            f_args, f_kwargs = lk_llm.utils.prepare_function_arguments(
                fnc=tool,
                json_arguments=resp.tool_calls[0].arguments,
            )
            got = inspect.signature(raw).bind(*f_args, **f_kwargs).arguments
            out = {
                "accuracy": {"verdict": got["accuracy"], "reasoning": got["accuracy_reasoning"]},
                "relevancy": {"verdict": got["relevancy"], "reasoning": got["relevancy_reasoning"]},
            }
            if excerpts:
                out["faithfulness"] = {
                    "verdict": got["faithfulness"],
                    "reasoning": got["faithfulness_reasoning"],
                }
                out["context_relevance"] = {
                    "verdict": got["context_relevance"],
                    "reasoning": got["context_relevance_reasoning"],
                }
                out["used_context"] = bool(got["used_context"])
            return out

        turn_results: list[dict] = []
        turns_total = 0
        turns_failed = 0
        ctx_total = 0  # judged answers that had document context
        if per_turn_wanted:
            kb_by_head = {t["head"]: t for t in getattr(agent, "_kb_turns", [])}
            targets = []
            for i, (role, text) in enumerate(turns):
                if role != "assistant":
                    continue
                j = i - 1
                while j >= 0 and turns[j][0] != "user":
                    j -= 1
                if j < 0 or _is_closing_turn(turns[j][1]):
                    continue
                targets.append((i, turns[max(0, i - 7): i + 1], text, kb_by_head.get(_head(text))))
            turns_total = len(targets)
            ctx_total = sum(1 for t in targets if t[3])
            # Bounded concurrency: a burst of per-turn calls is exactly how
            # a free-tier TPM budget gets exhausted mid-run.
            sem = asyncio.Semaphore(int(os.environ.get("EVALS_TURN_CONCURRENCY", "3")))

            async def _judge_turn(idx, window, answer, context=None):
                async with sem:
                    for llm_, tag in ((judge_llm, "primary"), (fallback_llm, "fallback")):
                        if llm_ is None:
                            continue
                        try:
                            r = await asyncio.wait_for(
                                _judge_turn_llm(llm_, window, answer, context),
                                timeout=_left(),
                            )
                            if tag == "fallback":
                                used_fallback.append(f"turn:{_head(answer)[:24]}")
                            return idx, r
                        except Exception as e:
                            logger.warning(
                                f"turn judge ({tag}) failed on turn {idx}:"
                                f" {type(e).__name__}: {e}"
                            )
                    return idx, None

            done = await asyncio.gather(*(_judge_turn(*t) for t in targets))
            for idx, r in done:
                if r is None:
                    turns_failed += 1
                    continue
                turn_results.append({"head": _head(turns[idx][1]), **r})

        V = {"pass": 1.0, "maybe": 0.5, "fail": 0.0}

        # Numeric contribution per scored check. Per-call checks map the
        # verdict; accuracy is the WORST answer (one confidently wrong fee
        # quote is the failure that burns you); relevancy is the MEAN (one
        # slightly off-topic reply should not fail a call, a pattern should).
        values: dict[str, float] = {}
        for n, jr in judgments.items():
            if n != "grounding":
                values[n] = V.get(jr.verdict, 0.0)
        n_judged = len(turn_results)
        if n_judged and "accuracy" in per_turn_wanted:
            vals = [V[t["accuracy"]["verdict"]] for t in turn_results]
            worst = min(vals)
            w = turn_results[vals.index(worst)]
            values["accuracy"] = worst
            judgments["accuracy"] = JudgmentResult(
                verdict=_label_verdict(worst),
                reasoning=f"Worst of {n_judged} answers. {w['accuracy']['reasoning']}",
            )
        if n_judged and "relevancy" in per_turn_wanted:
            mean = sum(V[t["relevancy"]["verdict"]] for t in turn_results) / n_judged
            values["relevancy"] = mean
            off = sum(1 for t in turn_results if t["relevancy"]["verdict"] != "pass")
            judgments["relevancy"] = JudgmentResult(
                verdict=_label_verdict(mean),
                reasoning=(
                    f"Average over {n_judged} answers: {round(mean * 100)}%."
                    + (f" {off} answer(s) drifted off what the caller said." if off else
                       " Every answer addressed what the caller said.")
                ),
            )
        # The document checks from the same per-answer calls. Faithfulness
        # is scored (worst answer); context relevance is shown only; and
        # where the judge read the excerpts, "used your documents" becomes
        # its reading instead of the word-overlap tally, which it quotes.
        for name, r in _context_checks(
            turn_results, (agent._ground_used, agent._ground_injected)
        ).items():
            judgments[name] = JudgmentResult(verdict=r["verdict"], reasoning=r["reasoning"])
            if r["value"] is not None:
                values[name] = r["value"]

        # Voice-format check: deterministic, free, never scored. Flags text
        # that breaks spoken delivery (a raw 14-digit account number read
        # as one huge number, URLs, markdown). A symptom flag, not a quality
        # judgment, which is why it stays outside the score like grounding.
        _digits = re.compile(r"\d{7,}")
        _url = re.compile(r"https?://|www\.", re.I)
        _md = re.compile(r"(\*\*|__|`|^#{1,6}\s|^\s*[-*]\s|^\s*\d+\.\s)", re.M)
        voice_flags: list[dict] = []
        for role, text in turns:
            if role != "assistant":
                continue
            issues = []
            if _digits.search(text):
                issues.append("long digit string")
            if _url.search(text):
                issues.append("URL")
            if _md.search(text):
                issues.append("markdown")
            if issues:
                voice_flags.append({"head": _head(text), "issues": issues})
        judgments["voice_format"] = JudgmentResult(
            verdict="pass" if not voice_flags else ("maybe" if len(voice_flags) == 1 else "fail"),
            reasoning=(
                "Every reply was written the way it should be spoken."
                if not voice_flags else
                f"{len(voice_flags)} repl{'y' if len(voice_flags) == 1 else 'ies'}"
                " contain text that reads badly aloud: "
                + ", ".join(sorted({i for f in voice_flags for i in f['issues']}))
                + "."
            ),
        )

        # Honest accounting. A check counts as expected only when it could
        # have run: per-turn checks need at least one substantive answer.
        # The UI hides the score for partial runs instead of presenting a
        # confident number computed off whatever happened to survive.
        expected = [j.name for j in call_judges if j.name != "grounding"]
        if turns_total:
            expected += per_turn_wanted
            if ctx_total:
                expected.append("faithfulness")
        ran = [n for n in expected if n in values]
        partial = len(ran) < len(expected) or turns_failed > 0
        score = (
            round(sum(values[n] for n in ran) / len(ran), 3) if ran else None
        )
        fb_model = (
            str(getattr(fallback_llm, "model", "") or "") if used_fallback else None
        )
        payload = {
            "score": score,
            "checks_run": len(ran),
            "checks_expected": len(expected),
            "partial": partial,
            "judges": {
                name: {"verdict": jr.verdict, "reasoning": jr.reasoning}
                for name, jr in judgments.items()
            },
            "per_turn": per_turn_wanted + (["faithfulness"] if ctx_total else []),
            "turns": turn_results,
            "turns_judged": n_judged,
            "turns_total": turns_total,
            # Answers that had document context: how many were judged on
            # it and how many the judge saw drawing on it. None when no
            # judged answer had any, so the dashboard can tell "no
            # lookups" from "not judged".
            "context": {
                "answers": ctx_total,
                "judged": sum(1 for t in turn_results if "faithfulness" in t),
                "used": sum(1 for t in turn_results if t.get("used_context")),
            } if ctx_total else None,
            "voice_flags": voice_flags,
            "model": str(getattr(judge_llm, "model", "") or ""),
            "fallback_model": fb_model,
            "fallback_checks": used_fallback,
        }
        verdict_summary = ", ".join(
            f"{n}={v['verdict']}" for n, v in payload["judges"].items()
        )
        logger.info(
            f"evals: score {payload['score']} ({verdict_summary};"
            f" turns {n_judged}/{turns_total}"
            + (f"; fallback for {len(used_fallback)}" if used_fallback else "")
            + ")"
        )
        await _write_evals(payload)

    ctx.add_shutdown_callback(_run_evals)

    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
