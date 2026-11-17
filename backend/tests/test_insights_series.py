"""The Insights trends: window and bucket rules, the zero-filled series built
from per-call rows, the composite error rate, the chart-to-table tags, the
engine relays, and the per-call timeline rebuilt from stored stage metrics.

The queries are SQL, so the series and timeline functions run against a fake
pool that answers each query by its shape. That checks what is Python here
(bucket mapping, overlap sweep, the error arithmetic, honesty about partial
scores); the SQL itself is exercised against the live database by hand.
"""

import datetime as dt
import json
import types

import httpx
import pytest
from fastapi import HTTPException

import api
import conv_timeline
import insights_breakdown
import insights_series as s

UTC = dt.timezone.utc


# ─── window and bucket rules ─────────────────────────────────────────────────


def test_a_date_range_is_read_on_the_callers_clock():
    # PKT sends tz_offset_min=-300 (UTC minus local). Local midnight on the
    # 14th is 19:00 UTC on the 13th.
    start, end, off, days = s.parse_window("2026-10-14", "2026-10-14", 7, -300)
    assert start == dt.datetime(2026, 10, 13, 19, tzinfo=UTC)
    assert end == dt.datetime(2026, 10, 14, 19, tzinfo=UTC)
    assert off == dt.timedelta(minutes=-300) and days == 1


def test_without_a_range_the_window_is_the_last_n_days_clamped():
    _, _, _, days = s.parse_window(None, None, 500, 0)
    assert days == 90
    _, _, _, days = s.parse_window(None, None, 0, 0)
    assert days == 1


@pytest.mark.parametrize(
    "start,end",
    [("not-a-date", "2026-10-14"), ("2026-10-14", "2026-10-13"), ("2026-01-01", "2026-12-31")],
)
def test_bad_ranges_are_refused_with_a_400(start, end):
    with pytest.raises(HTTPException) as e:
        s.parse_window(start, end, 7, 0)
    assert e.value.status_code == 400


def test_buckets_are_hourly_up_to_two_days_and_daily_beyond():
    assert s.pick_bucket(None, 1) == "hour" and s.pick_bucket(None, 2) == "hour"
    assert s.pick_bucket(None, 3) == "day"
    assert s.pick_bucket("hour", 30) == "hour" and s.pick_bucket("day", 1) == "day"
    assert s.pick_bucket("weird", 1) == "hour"  # an unknown override falls back


def test_the_breakdown_endpoint_reads_the_window_the_same_way():
    assert insights_breakdown.parse_window("2026-10-14", "2026-10-14", 7, -300) == (
        s.parse_window("2026-10-14", "2026-10-14", 7, -300)
    )


def test_provider_labels_name_the_backend_behind_a_host():
    hosts = {
        "speaches": {"localhost:8010"},
        "ollama": {"localhost:11434", "gpu-box:11434"},
        "vllm": {"gpu-box:8000", "localhost:8001"},
    }
    label = lambda raw: insights_breakdown.provider_label(raw, hosts)  # noqa: E731
    assert label("livekit") == "LiveKit"
    assert label("Gemini") == "Gemini"
    assert label("localhost:8010") == "Speaches (local)"
    assert label("http://127.0.0.1:8010/v1") == "Speaches (local)"  # loopback spellings match the configured URL
    assert label("ollama") == "Ollama (local)"
    assert label("localhost:11434") == "Ollama (local)"
    assert label("gpu-box:11434") == "Ollama (gpu-box:11434)"
    assert label("localhost:8001") == "vLLM (local)"
    assert label("gpu-box:8000") == "vLLM (gpu-box:8000)"
    assert label("speaches:8000") == "Speaches (local)"  # compose service name
    assert label("some-other-host:9000") == "some-other-host:9000"  # unknown: shown as stored
    assert label(None) == "" and label("") == ""


def test_provider_hosts_collect_the_deployment_urls():
    cfg = types.SimpleNamespace(
        speaches_url=lambda: "http://localhost:8010",
        ollama_url=lambda: "http://localhost:11434",
        list_saved_agents=lambda: [
            {"local": {"vllm": {"llm_url": "http://gpu-box:8000/v1", "tts_url": "http://localhost:8010/v1"}}},
            {"local": {"ollama": {"llm_url": "http://10.0.0.5:11434"}}},
        ],
        load_active=lambda: {},
    )
    hosts = insights_breakdown.provider_hosts(cfg)
    assert hosts["speaches"] == {"localhost:8010"}
    assert hosts["ollama"] == {"localhost:11434", "10.0.0.5:11434"}
    # The vLLM TTS URL pointed at the bundled Speaches container, so it is not a vLLM host.
    assert hosts["vllm"] == {"gpu-box:8000"}


class _BreakdownPool:
    """Answers only the per-bucket document references query; every other
    breakdown query comes back empty and the files column exists."""

    def __init__(self, refs):
        self.refs = refs

    async def fetch(self, sql, *a):
        if "jsonb_array_elements_text(r.files)" in sql and "date_trunc" in sql:
            return self.refs
        return []

    async def fetchval(self, sql, *a):
        return 1


async def test_document_references_keep_the_top_files_and_fold_the_rest():
    d12, d13 = dt.datetime(2026, 10, 12), dt.datetime(2026, 10, 13)
    rows = [{"b": d12, "filename": f, "n": n} for f, n in
            [("a.pdf", 5), ("b.pdf", 4), ("c.pdf", 3), ("d.pdf", 2), ("e.pdf", 2)]]
    rows += [{"b": d13, "filename": "f.pdf", "n": 1}, {"b": d13, "filename": "g.pdf", "n": 1},
             {"b": d13, "filename": "a.pdf", "n": 1}]
    out = await insights_breakdown.breakdown(
        _BreakdownPool(rows), agent_id=None, start_utc=_t(12, 0), end_utc=_t(15, 0),
        off=dt.timedelta(0), days=3, bucket="day",
    )
    refs = out["documents"]["references"]
    assert [f["filename"] for f in refs["files"]] == ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf"]
    assert refs["files"][0]["values"] == [5, 1, 0]
    # Two files beyond the five lines fold into one Other series.
    assert refs["other"] == [0, 2, 0]
    assert out["documents"]["retrieved_known"] is True


# ─── the series, against a fake pool ─────────────────────────────────────────


def _row(**kw):
    base = {
        "id": "x", "started_at": None, "ended_at": None, "ended_reason": None,
        "evals": None, "user_turns": 3, "assistant_turns": 3, "voice": True,
        "cut": 0, "last_msg": None,
    }
    return base | kw


class _Pool:
    def __init__(self, convs, tools=(), latency=()):
        self.convs, self.tools, self.latency = convs, tools, latency

    async def fetch(self, sql, *a):
        if "c.metadata->'evals' AS evals" in sql:
            return self.convs
        if "percentile_cont(0.9)" in sql:
            return self.latency
        if "metric_type = 'ToolCall' GROUP BY b" in sql:
            return self.tools
        return []

    async def fetchval(self, sql, *a):
        return 3 if "max_concurrent_sessions" in sql else 1


def _t(day, hour, minute=0):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=UTC)


async def _series(pool, bucket="day", days=3):
    return await s.series(
        pool, agent_id=None, start_utc=_t(12, 0), end_utc=_t(15, 0),
        off=dt.timedelta(0), days=days, bucket=bucket,
    )


async def test_series_are_zero_filled_on_one_shared_axis():
    out = await _series(_Pool([]))
    assert out["labels"] == ["2026-10-12T00:00", "2026-10-13T00:00", "2026-10-14T00:00"]
    ser = out["series"]
    assert ser["calls"] == [0, 0, 0] and ser["talk_min"] == [0.0, 0.0, 0.0]
    assert all(len(v) == 3 for v in ser["endings"].values())
    # No data is a gap, never a zero, for latency and quality.
    assert ser["latency"]["p50"] == [None, None, None]
    assert ser["quality"]["p50"] == [None, None, None]
    assert out["open_now"] == 1 and out["cap"] == 3


async def test_weekly_buckets_start_on_monday_and_match_postgres_date_trunc():
    # 2026-10-12 is a Monday. A window opening mid-week still labels the
    # whole week by its Monday, so rows and labels agree with date_trunc('week').
    pool = _Pool([
        _row(id="wed", started_at=_t(14, 9), ended_at=_t(14, 9, 5)),
        _row(id="tue2", started_at=_t(20, 9), ended_at=_t(20, 9, 5)),
    ])
    out = await s.series(
        pool, agent_id=None, start_utc=_t(14, 0), end_utc=_t(22, 0),
        off=dt.timedelta(0), days=8, bucket="week",
    )
    assert out["labels"] == ["2026-10-12T00:00", "2026-10-19T00:00"]
    assert out["series"]["calls"] == [1, 1]
    assert s.pick_bucket("week", 7) == "week"
    assert s.pick_bucket(None, 90) == "day"  # auto never picks weekly


async def test_voice_calls_from_before_the_interrupted_flag_stay_out_of_the_rate():
    pool = _Pool([
        _row(id="old", started_at=_t(12, 10), ended_at=_t(12, 11), tracked=False),
        _row(id="new1", started_at=_t(12, 11), ended_at=_t(12, 12), tracked=True, cut=1),
        _row(id="new2", started_at=_t(12, 12), ended_at=_t(12, 13), tracked=True),
        _row(id="oldonly", started_at=_t(13, 10), ended_at=_t(13, 11), tracked=False),
    ])
    ser = (await _series(pool))["series"]
    assert ser["calls"] == [3, 1, 0]
    # The untracked call would dilute 1 of 2 to 1 of 3; a day with only
    # untracked calls is a gap, not a 0% that was never measured.
    assert ser["interruptions"]["rate"] == [0.5, None, None]


async def test_calls_text_sessions_endings_and_tags_land_in_the_right_bucket():
    good = {"score": 0.9, "partial": False, "judges": {"accuracy": {"verdict": "pass"}}}
    pool = _Pool([
        # _t(day, hour, minute): A runs 10:00-10:05, B 10:03-10:08, C 12:00-12:01.
        _row(id="A", started_at=_t(12, 10, 0), ended_at=_t(12, 10, 5), ended_reason="user_ended",
             evals=json.dumps(good), cut=1),
        _row(id="B", started_at=_t(12, 10, 3), ended_at=_t(12, 10, 8), user_turns=1, voice=False,
             assistant_turns=1),
        _row(id="C", started_at=_t(13, 12, 0), ended_at=_t(13, 12, 1), ended_reason="connection_lost",
             evals=json.dumps({"error": "TimeoutError"}), assistant_turns=2, user_turns=4),
    ])
    out = await _series(pool)
    ser = out["series"]
    assert ser["calls"] == [2, 1, 0]
    assert ser["text_sessions"] == [1, 0, 0]
    assert ser["non_engaged"] == [1, 0, 0]  # B has a single caller turn
    assert ser["endings"]["user_ended"] == [1, 0, 0]
    assert ser["endings"]["unknown"] == [1, 0, 0]  # NULL is "unknown", never a guess
    assert ser["endings"]["connection_lost"] == [0, 1, 0]
    # A and B overlap for two minutes; C is alone the next day.
    assert ser["concurrency"] == [2, 1, 0]
    # Only the complete run is plotted, and the count says how many it is.
    assert ser["quality"]["p50"] == [0.9, None, None] and ser["quality"]["n"] == [1, 0, 0]
    # Rate is over VOICE calls only: day 1 has one (A, cut off; B is text),
    # day 2 has one (C, not cut off), day 3 has none, so a gap, not a zero.
    assert ser["interruptions"]["rate"] == [1.0, 0.0, None]
    assert out["flags"]["A"] == ["interrupted", "ending:user_ended", "scored"]
    assert out["flags"]["B"] == ["text", "non_engaged", "ending:unknown"]
    assert out["flags"]["C"] == ["ending:connection_lost", "eval_failed"]
    assert ser["talk_min"][0] == 10.0  # A five minutes plus B five minutes


async def test_a_partial_eval_run_is_never_plotted_as_a_score():
    partial = {"score": 0.1, "partial": True, "judges": {"accuracy": {"verdict": "fail"}}}
    out = await _series(_Pool([_row(id="P", started_at=_t(12, 9), evals=json.dumps(partial))]))
    assert out["series"]["quality"]["n"] == [0, 0, 0]
    assert out["series"]["quality"]["p50"] == [None, None, None]
    # Its verdicts still count toward the per-check pass rate: they happened.
    assert out["series"]["checks"]["accuracy"]["n"] == [1, 0, 0]


async def test_the_error_rate_is_the_composite_over_answers_lookups_and_tool_calls():
    pool = _Pool(
        [
            _row(id="A", started_at=_t(12, 10), ended_at=_t(12, 15), assistant_turns=3),
            _row(id="C", started_at=_t(13, 12), ended_at=_t(13, 13), assistant_turns=2,
                 ended_reason="connection_lost", evals=json.dumps({"error": "x"})),
        ],
        tools=[{"b": dt.datetime(2026, 10, 12), "calls": 2, "failed": 1, "p50_ms": 500}],
    )
    err = (await _series(pool))["series"]["errors"]
    # Day 1: one failed tool call over 3 answers + 0 lookups + 2 tool calls.
    assert err["errors"][0] == 1 and err["denominator"][0] == 5
    assert err["rate"][0] == pytest.approx(0.2)
    # Day 2: a failed eval run and a lost connection over 2 answers.
    assert err["errors"][1] == 2 and err["by"]["evals"][1] == 1 and err["by"]["connection"][1] == 1
    assert err["rate"][1] == pytest.approx(1.0)
    assert err["rate"][2] is None  # nothing happened: no rate, not 0%


async def test_latency_rows_fill_their_bucket_and_leave_the_rest_as_gaps():
    lat = [{"b": dt.datetime(2026, 10, 13), "p50": 1.5, "p90": 2.0, "p95": 2.5, "n": 4,
            "eou": 0.4, "stt": 0.3, "llm": 0.6, "tts": 0.2}]
    ser = (await _series(_Pool([], latency=lat)))["series"]
    assert ser["latency"]["p50"] == [None, 1.5, None] and ser["latency"]["n"] == [0, 4, 0]
    assert ser["stages"]["tts"] == [None, 0.2, None]


async def test_hourly_buckets_cover_the_window_hour_by_hour():
    out = await s.series(
        _Pool([_row(id="H", started_at=_t(12, 10, 30), ended_at=_t(12, 10, 45))]),
        agent_id=None, start_utc=_t(12, 9), end_utc=_t(12, 12), off=dt.timedelta(0),
        days=1, bucket="hour",
    )
    assert out["labels"] == ["2026-10-12T09:00", "2026-10-12T10:00", "2026-10-12T11:00"]
    assert out["series"]["calls"] == [0, 1, 0]


# ─── engine relays ───────────────────────────────────────────────────────────

ENGINE = {"id": "e1", "name": "Production VM", "base_url": "https://vm.example", "admin_token": "tok"}


@pytest.fixture
def relay(monkeypatch):
    seen, canned = [], {"status": 200, "json": {"ok": True}}

    async def fake_relay(engine, method, path, json_body=None):
        seen.append((method, path))
        return httpx.Response(canned["status"], json=canned["json"],
                              request=httpx.Request(method, "https://vm.example"))

    monkeypatch.setattr(api, "_load_engines", lambda: [ENGINE])
    monkeypatch.setattr(api, "_engine_relay", fake_relay)
    return seen, canned


def _req(query):
    return types.SimpleNamespace(url=types.SimpleNamespace(query=query))


async def test_series_and_breakdown_are_relayed_with_the_query_untouched(relay):
    seen, _ = relay
    q = "tz_offset_min=-300&start=2026-10-08&end=2026-10-14&bucket=day&agent_id=agent-x"
    await api.engine_insights_series("e1", _req(q))
    await api.engine_insights_breakdown("e1", _req(q))
    assert seen == [
        ("GET", f"/api/insights/series?{q}"),
        ("GET", f"/api/insights/breakdown?{q}"),
    ]


async def test_a_timeline_is_relayed_by_conversation_id(relay):
    import uuid

    seen, _ = relay
    cid = uuid.uuid4()
    await api.engine_conv_timeline("e1", cid)
    assert seen == [("GET", f"/api/conversations/{cid}/timeline")]


async def test_an_engine_on_an_older_build_gets_a_readable_message(relay):
    _, canned = relay
    canned["status"], canned["json"] = 404, {"detail": "Not Found"}
    with pytest.raises(HTTPException) as e:
        await api.engine_insights_series("e1", _req("days=7"))
    assert e.value.status_code == 502 and "older build" in e.value.detail


# ─── the per-call timeline ───────────────────────────────────────────────────


class _TimelinePool:
    def __init__(self, conv, metrics, msgs):
        self.conv, self.metrics, self.msgs = conv, metrics, msgs

    async def fetchrow(self, sql, *a):
        return self.conv

    async def fetch(self, sql, *a):
        return self.metrics if "FROM turn_metrics" in sql else self.msgs


def _m(kind, at, **raw):
    t = at.timestamp()
    return {"metric_type": kind, "speech_id": "sp1", "stage_ms": None, "duration_ms": None,
            "audio_ms": None, "cancelled": False, "model": None,
            "raw": json.dumps({"timestamp": t, **raw}), "created_at": at}


async def test_a_turn_is_rebuilt_from_end_times_and_durations():
    t0 = _t(14, 10)
    conv = {"id": "cid", "room_name": "room", "started_at": t0, "ended_at": t0 + dt.timedelta(seconds=40)}
    at = lambda sec: t0 + dt.timedelta(seconds=sec)  # noqa: E731
    metrics = [
        # The turn was declared over at +10s after 1.5s of waiting; transcript took 1.0s.
        _m("EOUMetrics", at(10), end_of_utterance_delay=1.5, transcription_delay=1.0),
        # Model reply finished at +14s: 3s long, first token after 1s.
        _m("LLMMetrics", at(14), duration=3.0, ttft=1.0),
        # Voice: 2s to synthesize, first audio after 0.5s, 4s of audio.
        _m("TTSMetrics", at(16), duration=2.0, ttfb=0.5, audio_duration=4.0),
    ]
    msgs = [
        {"role": "user", "content": "What are your hours?", "created_at": at(10), "metrics": None},
        {"role": "assistant", "content": "We open at nine.", "created_at": at(16),
         "metrics": json.dumps({"interrupted": True})},
    ]
    out = await conv_timeline.timeline(_TimelinePool(conv, metrics, msgs), "cid")
    assert out["has_data"] and len(out["turns"]) == 1
    turn = out["turns"][0]
    spans = {sp["kind"]: sp for sp in turn["spans"]}
    assert (spans["waiting"]["start_s"], spans["waiting"]["end_s"]) == (8.5, 10.0)
    assert (spans["hearing"]["start_s"], spans["hearing"]["end_s"]) == (8.5, 9.5)
    assert spans["hearing"]["lane"] == 1  # runs alongside waiting
    assert (spans["thinking"]["start_s"], spans["thinking"]["end_s"]) == (11.0, 12.0)
    assert (spans["streaming"]["start_s"], spans["streaming"]["end_s"]) == (12.0, 14.0)
    assert (spans["voice"]["start_s"], spans["voice"]["end_s"]) == (14.0, 14.5)
    assert (spans["audio"]["start_s"], spans["audio"]["end_s"]) == (14.5, 18.5)
    # Words attach by time, once each, and the cut-off flag comes along.
    assert turn["user"] == "What are your hours?" and turn["agent"] == "We open at nine."
    assert turn["interrupted"] is True


async def test_a_call_with_no_stage_rows_says_so_instead_of_drawing_nothing():
    t0 = _t(14, 10)
    conv = {"id": "cid", "room_name": "room", "started_at": t0, "ended_at": None}
    out = await conv_timeline.timeline(_TimelinePool(conv, [], []), "cid")
    assert out["has_data"] is False and out["turns"] == []


async def test_an_unknown_conversation_is_a_404():
    class NoConv(_TimelinePool):
        async def fetchrow(self, sql, *a):
            return None

    with pytest.raises(HTTPException) as e:
        await conv_timeline.timeline(NoConv(None, [], []), "nope")
    assert e.value.status_code == 404
