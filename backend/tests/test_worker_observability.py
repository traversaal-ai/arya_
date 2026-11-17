"""Worker-side collection for the observability views: pipeline errors, how
fast the agent stops when the caller talks over it, which documents a lookup
injected, and the mapping of SDK metric events onto turn_metrics rows.

These are pure event handlers, so they run against the SDK's real event
classes and a fake session; no live call, database or LiveKit server.
"""

import asyncio

from livekit.agents.llm import LLMError
from livekit.agents.stt import STTError
from livekit.agents.tts import TTSError
from livekit.agents.voice.events import (
    AgentStateChangedEvent,
    ErrorEvent,
    UserStateChangedEvent,
)

import agent
import worker_observability as w


class _Source:
    model = "fake-model"
    provider = "fake-provider"


class _Session:
    """Just enough of AgentSession: a decorator that remembers handlers."""

    def __init__(self):
        self.handlers = {}

    def on(self, name):
        def deco(fn):
            self.handlers[name] = fn
            return fn

        return deco


class _Rows:
    """Stands in for _write_turn_metric and keeps what it was given."""

    def __init__(self):
        self.rows = []

    async def __call__(self, row, raw):
        self.rows.append((row, raw))

    def of(self, kind):
        return [r for r, _ in self.rows if r["metric_type"] == kind]


def _attach():
    session, sink = _Session(), _Rows()
    w.attach(session, sink)
    return session, sink


def _agent(old, new, t):
    return AgentStateChangedEvent(old_state=old, new_state=new, created_at=t)


def _user(old, new, t):
    return UserStateChangedEvent(old_state=old, new_state=new, created_at=t)


async def _settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ─── pipeline errors ─────────────────────────────────────────────────────────


async def test_each_stage_error_becomes_a_row_with_stage_and_fatality():
    session, sink = _attach()
    cases = (
        (STTError(timestamp=1.0, label="stt", error=TimeoutError("slow"), recoverable=True), "stt", False),
        (TTSError(timestamp=2.0, label="tts", error=RuntimeError("voice down"), recoverable=False), "tts", True),
        (LLMError(timestamp=3.0, label="llm", error=ValueError("429"), recoverable=True), "llm", False),
    )
    for err, _, _ in cases:
        session.handlers["error"](ErrorEvent(error=err, source=_Source()))
    await _settle()

    rows = sink.of("ErrorEvent")
    assert [r["model"] for r in rows] == [stage for _, stage, _ in cases]
    # `cancelled` carries "this error ended the turn" for ErrorEvent rows.
    assert [r["cancelled"] for r in rows] == [fatal for _, _, fatal in cases]
    assert all(r["provider"] == "fake-provider" for r in rows)
    raw = sink.rows[1][1]
    assert raw["message"] == "voice down" and raw["kind"] == "RuntimeError"
    assert raw["model"] == "fake-model" and raw["recoverable"] is False


# ─── interruption latency ────────────────────────────────────────────────────


async def test_the_agent_stopping_after_the_caller_starts_is_timed():
    session, sink = _attach()
    session.handlers["agent_state_changed"](_agent("thinking", "speaking", 100.0))
    session.handlers["user_state_changed"](_user("listening", "speaking", 101.0))
    session.handlers["agent_state_changed"](_agent("speaking", "listening", 101.35))
    await _settle()
    # 101.35 - 101.0 is 349.999... in floating point; the number must round,
    # not truncate to 349.
    assert [r["stage_ms"] for r in sink.of("Interruption")] == [350]


async def test_a_reply_that_ends_on_its_own_records_nothing():
    session, sink = _attach()
    session.handlers["agent_state_changed"](_agent("thinking", "speaking", 200.0))
    session.handlers["agent_state_changed"](_agent("speaking", "listening", 205.0))
    await _settle()
    assert sink.rows == []


async def test_a_long_overlap_is_a_backchannel_not_a_stop():
    session, sink = _attach()
    session.handlers["agent_state_changed"](_agent("thinking", "speaking", 300.0))
    session.handlers["user_state_changed"](_user("listening", "speaking", 301.0))
    # The agent talks on for ten more seconds: outside the 3s window.
    session.handlers["agent_state_changed"](_agent("speaking", "listening", 311.0))
    await _settle()
    assert sink.rows == []


async def test_a_short_mm_hm_that_ended_before_the_reply_did_is_not_an_interruption():
    session, sink = _attach()
    session.handlers["agent_state_changed"](_agent("thinking", "speaking", 320.0))
    session.handlers["user_state_changed"](_user("listening", "speaking", 328.0))
    session.handlers["user_state_changed"](_user("speaking", "listening", 328.4))
    # Inside the 3s window, but the caller stopped 1.1s earlier: the reply
    # simply finished. Counting it would invent an interruption.
    session.handlers["agent_state_changed"](_agent("speaking", "listening", 329.5))
    await _settle()
    assert sink.rows == []


async def test_a_one_word_stop_that_ends_as_the_agent_stops_counts():
    session, sink = _attach()
    session.handlers["agent_state_changed"](_agent("thinking", "speaking", 340.0))
    session.handlers["user_state_changed"](_user("listening", "speaking", 341.0))
    session.handlers["user_state_changed"](_user("speaking", "listening", 341.3))
    session.handlers["agent_state_changed"](_agent("speaking", "listening", 341.5))
    await _settle()
    assert [r["stage_ms"] for r in sink.of("Interruption")] == [500]


async def test_caller_speech_before_the_reply_starts_does_not_leak_into_it():
    session, sink = _attach()
    session.handlers["user_state_changed"](_user("listening", "speaking", 400.0))
    session.handlers["agent_state_changed"](_agent("thinking", "speaking", 402.0))
    session.handlers["agent_state_changed"](_agent("speaking", "listening", 403.0))
    await _settle()
    assert sink.rows == []


async def test_two_interruptions_in_one_call_are_both_recorded():
    session, sink = _attach()
    for base in (500.0, 600.0):
        session.handlers["agent_state_changed"](_agent("thinking", "speaking", base))
        session.handlers["user_state_changed"](_user("listening", "speaking", base + 1))
        session.handlers["agent_state_changed"](_agent("speaking", "listening", base + 1.2))
    await _settle()
    assert [r["stage_ms"] for r in sink.of("Interruption")] == [200, 200]


async def test_malformed_events_never_raise_out_of_a_handler():
    # A hook that throws would surface inside a live call.
    session, _ = _attach()

    class Weird:
        pass

    for name in ("error", "agent_state_changed", "user_state_changed"):
        session.handlers[name](Weird())
    await _settle()


# ─── documents a lookup injected ─────────────────────────────────────────────


def test_rag_files_are_the_kept_prefix_without_duplicates():
    assert w.rag_files({"filenames": ["a", "a", "b", "c"], "kept": 3}) == ["a", "b"]
    assert w.rag_files({"filenames": ["a"], "kept": 0}) == []
    assert w.rag_files({"skipped": "closing_turn"}) == []


# ─── SDK metric event -> turn_metrics row ────────────────────────────────────


def test_each_metric_type_maps_its_headline_number_to_stage_ms():
    meta = {"model_name": "m", "model_provider": "p"}
    assert agent._turn_metric_row(
        {"metric_type": "EOUMetrics", "end_of_utterance_delay": 0.42,
         "transcription_delay": 0.3, "speech_id": "s1", "metadata": meta}
    ) == {
        "metric_type": "EOUMetrics", "speech_id": "s1", "model": "m",
        "provider": "p", "cancelled": False, "stage_ms": 420, "duration_ms": 300,
    }
    llm = agent._turn_metric_row(
        {"metric_type": "LLMMetrics", "ttft": 0.9, "duration": 1.5,
         "prompt_tokens": 100, "completion_tokens": 20, "metadata": meta}
    )
    assert (llm["stage_ms"], llm["duration_ms"], llm["tokens_in"], llm["tokens_out"]) == (900, 1500, 100, 20)
    tts = agent._turn_metric_row(
        {"metric_type": "TTSMetrics", "ttfb": 1.1, "duration": 2.0,
         "audio_duration": 3.2, "characters_count": 40, "cancelled": True}
    )
    assert (tts["stage_ms"], tts["audio_ms"], tts["chars"], tts["cancelled"]) == (1100, 3200, 40, True)
    stt = agent._turn_metric_row(
        {"metric_type": "STTMetrics", "duration": 0.0, "audio_duration": 2.5}
    )
    # Streaming STT reports 0 duration: kept as 0, not dropped to None.
    assert (stt["stage_ms"], stt["audio_ms"]) == (0, 2500)


def test_an_unknown_metric_type_still_produces_a_row():
    # An SDK upgrade must not silently drop data.
    row = agent._turn_metric_row({"metric_type": "BrandNewMetrics", "x": 1})
    assert row["metric_type"] == "BrandNewMetrics" and row["stage_ms"] is None
