"""Insights time series (Phase A of PLAN-Arya-V1-Observability_v2).

Drop-in module. Nothing here touches existing tables or the worker; it
reads what the stack already stores (conversations, messages, rag_events,
session_refusals, turn_metrics, deployments) and returns every Phase A
series bucketed by hour, day or week, zero-filled, on one shared x axis.

Wiring (see INTEGRATION.md): import `register` and call
`register(api, db, _dsn)` once after the admin router exists. The engine
proxy relays `/api/insights/series` unchanged, so remote engines get
trends for free once they run this build.

Window and timezone rules are identical to insights_summary: timestamps are
stored in UTC; the range and the buckets are interpreted in the CALLER's
timezone via tz_offset_min (JS getTimezoneOffset(): UTC minus local, so
PKT sends -300). "Oct 8" means Oct 8 on the user's clock.
"""

import datetime
import json
import statistics

from fastapi import HTTPException

BUCKETS = {
    "hour": datetime.timedelta(hours=1),
    "day": datetime.timedelta(days=1),
    "week": datetime.timedelta(days=7),  # Monday to Sunday, like Postgres date_trunc('week')
}
MAX_DAYS = 90
# Same vocabulary as the existing endings card; anything else folds into unknown.
ENDINGS = ("user_ended", "inactivity", "time_limit", "connection_lost", "unknown")
REFUSAL_REASONS = ("concurrent_limit", "daily_limit", "rate_limited")


def parse_window(start, end, days, tz_offset_min):
    """(start_utc, end_utc, off, days). Mirrors insights_summary so both
    endpoints agree on what a day is; lift into a shared helper when the
    series endpoint moves into api.py."""
    off = datetime.timedelta(minutes=max(-840, min(int(tz_offset_min or 0), 840)))
    if start and end:
        try:
            start_day = datetime.date.fromisoformat(start)
            end_day = datetime.date.fromisoformat(end)
        except ValueError:
            raise HTTPException(400, "start/end must be YYYY-MM-DD dates") from None
        if end_day < start_day:
            raise HTTPException(400, "end date is before start date")
        if (end_day - start_day).days > MAX_DAYS:
            raise HTTPException(400, f"date range is capped at {MAX_DAYS} days")
        start_utc = (
            datetime.datetime.combine(start_day, datetime.time.min)
            .replace(tzinfo=datetime.timezone.utc) + off
        )
        end_utc = (
            datetime.datetime.combine(
                end_day + datetime.timedelta(days=1), datetime.time.min
            ).replace(tzinfo=datetime.timezone.utc) + off
        )
        days = (end_day - start_day).days + 1
    else:
        # Only None means "use the default". `days or 7` turned an explicit 0
        # into 7, where the summary endpoint clamps it to 1: two endpoints
        # answering the same parameter differently.
        days = max(1, min(int(7 if days is None else days), MAX_DAYS))
        end_utc = datetime.datetime.now(datetime.timezone.utc)
        start_utc = end_utc - datetime.timedelta(days=days)
    return start_utc, end_utc, off, days


def pick_bucket(bucket, days):
    """Hourly up to two days, daily beyond, unless the caller overrides
    (weekly is only ever chosen by the caller)."""
    if bucket in BUCKETS:
        return bucket
    return "hour" if days <= 2 else "day"


def _truncate(local: datetime.datetime, bucket: str) -> datetime.datetime:
    if bucket == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - datetime.timedelta(days=day.weekday()) if bucket == "week" else day


def _median(xs):
    return statistics.median(xs) if xs else None


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


async def series(pool, *, agent_id, start_utc, end_utc, off, days, bucket):
    step = BUCKETS[bucket]
    # Shared x axis in caller-local time, zero-filled.
    start_local = _truncate((start_utc - off).replace(tzinfo=None), bucket)
    end_local = (end_utc - off).replace(tzinfo=None)
    labels = []
    t = start_local
    while t < end_local and len(labels) < 24 * MAX_DAYS + 1:
        labels.append(t)
        t += step
    n = len(labels)
    index = {lab: i for i, lab in enumerate(labels)}

    def idx(ts_utc):
        if ts_utc is None:
            return None
        return index.get(_truncate((ts_utc - off).replace(tzinfo=None), bucket))

    def zeros(kind=0):
        return [kind] * n if kind is not None else [None] * n

    # Same scope predicate as insights_summary, reused by every query.
    scope = (
        " WHERE c.started_at >= $1 AND c.started_at < $2"
        "   AND ($3::text IS NULL OR c.metadata->>'agent_id' = $3)"
    )
    args = (start_utc, end_utc, agent_id or None)

    # 1. One row per conversation. Calls, text sessions, non-engaged,
    # talk time, endings, interruptions, quality and checks, eval
    # failures and concurrency all derive from this in Python; at the
    # 90-day cap it is a few thousand rows, which is cheaper and safer
    # than seven GROUP BY queries that must agree on bucket edges.
    rows = await pool.fetch(
        "SELECT c.id, c.started_at, c.ended_at,"
        " c.metadata->>'ended_reason' AS ended_reason,"
        " c.metadata->'evals' AS evals,"
        " count(m.id) FILTER (WHERE m.role = 'user') AS user_turns,"
        " count(m.id) FILTER (WHERE m.role = 'assistant') AS assistant_turns,"
        " bool_or(m.metrics ? 'tts_node_ttfb' OR m.metrics ? 'e2e_latency')"
        "   AS voice,"
        # Calls before the worker recorded the interrupted flag have no key;
        # counting them as zero would understate the rate.
        " bool_or(m.metrics ? 'interrupted') AS tracked,"
        " count(m.id) FILTER (WHERE m.role = 'assistant'"
        "   AND m.metrics->>'interrupted' = 'true') AS cut,"
        " max(m.created_at) AS last_msg"
        " FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id"
        + scope
        + " GROUP BY c.id",
        *args,
    )

    calls, text_sessions, non_engaged = zeros(), zeros(), zeros()
    talk_s, voice_calls, interrupted_calls = zeros(0.0), zeros(), zeros()
    assistant_turns = zeros()
    endings = {k: zeros() for k in ENDINGS}
    quality_vals = [[] for _ in range(n)]
    checks_pass = {}   # name -> [pass count per bucket]
    checks_n = {}      # name -> [count per bucket]
    eval_failures = zeros()
    flags = {}         # conversation id -> tags, for the chart-to-table filter
    intervals = []     # (start_utc, end_utc) for the concurrency sweep

    for r in rows:
        i = idx(r["started_at"])
        if i is None:
            continue
        cid = str(r["id"])
        tags = []
        calls[i] += 1
        voice = bool(r["voice"])
        if r["user_turns"] and not voice:
            text_sessions[i] += 1
            tags.append("text")
        if (r["user_turns"] or 0) < 2:
            # First approximation of "non-engaged": fewer than two caller
            # turns. The worker's closing-turn detector can refine this
            # once it writes a per-call flag (Tier 2).
            non_engaged[i] += 1
            tags.append("non_engaged")
        end = r["ended_at"] or r["last_msg"]
        dur = max(0.0, (end - r["started_at"]).total_seconds()) if end else 0.0
        talk_s[i] += dur
        intervals.append((r["started_at"], end or r["started_at"]))
        assistant_turns[i] += r["assistant_turns"] or 0
        if voice and r.get("tracked", True):
            voice_calls[i] += 1
            if (r["cut"] or 0) > 0:
                interrupted_calls[i] += 1
                tags.append("interrupted")
        reason = r["ended_reason"] if r["ended_reason"] in ENDINGS else "unknown"
        endings[reason][i] += 1
        tags.append(f"ending:{reason}")
        ev = json.loads(r["evals"]) if r["evals"] else None
        if ev:
            if ev.get("error"):
                eval_failures[i] += 1
                tags.append("eval_failed")
            score = ev.get("score")
            if isinstance(score, (int, float)) and not ev.get("partial"):
                quality_vals[i].append(float(score))
                tags.append("scored")
            for name, j in (ev.get("judges") or {}).items():
                v = (j or {}).get("verdict")
                if v not in ("pass", "fail", "maybe"):
                    continue
                checks_pass.setdefault(name, zeros())
                checks_n.setdefault(name, zeros())
                checks_n[name][i] += 1
                if v == "pass":
                    checks_pass[name][i] += 1
        flags[cid] = tags

    # Concurrency: max simultaneous calls per bucket. Sweep sorted events,
    # carrying the running count across bucket edges so a long call that
    # spans buckets counts in every one it touches.
    concurrency = zeros()
    events = []
    for s, e in intervals:
        events.append((s, 1))
        events.append((max(e, s), -1))
    events.sort(key=lambda x: (x[0], x[1]))  # ends (-1) before starts (+1) at equal t
    cur = 0
    last_i = 0
    for t_, d in events:
        i = idx(t_)
        if i is None:
            i = 0 if t_ < start_utc else n - 1
        for k in range(last_i + 1, i + 1):
            concurrency[k] = max(concurrency[k], cur)
        last_i = max(last_i, i)
        cur += d
        if cur > concurrency[i]:
            concurrency[i] = cur

    # 2. Latency per bucket from the per-turn report: response time (e2e,
    # else stage sum) p50/p90/p95 and the four stage p50s. Stage keys are
    # distinct per role so no role filter is needed for them.
    lat_rows = await pool.fetch(
        "SELECT date_trunc($5::text, c.started_at - $4::interval) AS b,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY t.resp) AS p50,"
        " percentile_cont(0.9) WITHIN GROUP (ORDER BY t.resp) AS p90,"
        " percentile_cont(0.95) WITHIN GROUP (ORDER BY t.resp) AS p95,"
        " count(t.resp) AS n,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY (t.metrics->>'end_of_turn_delay')::float)"
        "   FILTER (WHERE t.metrics ? 'end_of_turn_delay') AS eou,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY (t.metrics->>'transcription_delay')::float)"
        "   FILTER (WHERE t.metrics ? 'transcription_delay') AS stt,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY (t.metrics->>'llm_node_ttft')::float)"
        "   FILTER (WHERE t.metrics ? 'llm_node_ttft') AS llm,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY (t.metrics->>'tts_node_ttfb')::float)"
        "   FILTER (WHERE t.metrics ? 'tts_node_ttfb') AS tts"
        " FROM (SELECT m.conversation_id, m.role, m.metrics,"
        "   CASE WHEN m.role = 'assistant' THEN COALESCE("
        "     (m.metrics->>'e2e_latency')::float,"
        "     (m.metrics->>'llm_node_ttft')::float"
        "       + (m.metrics->>'tts_node_ttfb')::float) END AS resp"
        "   FROM messages m WHERE m.metrics IS NOT NULL) t"
        " JOIN conversations c ON c.id = t.conversation_id"
        + scope
        + " GROUP BY b",
        *args, off, bucket,
    )
    latency = {"p50": zeros(None), "p90": zeros(None), "p95": zeros(None), "n": zeros()}
    stages = {k: zeros(None) for k in ("eou", "stt", "llm", "tts")}
    for r in lat_rows:
        i = index.get(r["b"].replace(tzinfo=None))
        if i is None:
            continue
        for k in ("p50", "p90", "p95"):
            latency[k][i] = round(r[k], 3) if r[k] is not None else None
        latency["n"][i] = r["n"]
        for k in stages:
            stages[k][i] = round(r[k], 3) if r[k] is not None else None

    # 3. Knowledge base per bucket.
    kb_rows = await pool.fetch(
        "SELECT date_trunc($5::text, e.created_at - $4::interval) AS b,"
        " count(*) AS lookups,"
        " count(*) FILTER (WHERE e.error IS NOT NULL) AS errors,"
        " count(*) FILTER (WHERE e.error ILIKE '%timeout%'"
        "   OR e.error ILIKE '%cancelled%') AS timeouts,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY e.top_score)"
        "   FILTER (WHERE e.top_score IS NOT NULL) AS med_score,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY e.latency_ms)"
        "   FILTER (WHERE e.latency_ms IS NOT NULL) AS med_latency_ms"
        " FROM rag_events e"
        " WHERE e.created_at >= $1 AND e.created_at < $2"
        "   AND ($3::text IS NULL OR e.agent_id = $3) AND e.skipped IS NULL"
        " GROUP BY b",
        *args, off, bucket,
    )
    kb = {"lookups": zeros(), "errors": zeros(), "timeouts": zeros(),
          "med_score": zeros(None), "med_latency_ms": zeros(None)}
    for r in kb_rows:
        i = index.get(r["b"].replace(tzinfo=None))
        if i is None:
            continue
        kb["lookups"][i] = r["lookups"]
        kb["errors"][i] = r["errors"]
        kb["timeouts"][i] = r["timeouts"]
        kb["med_score"][i] = round(r["med_score"], 3) if r["med_score"] is not None else None
        kb["med_latency_ms"][i] = round(r["med_latency_ms"]) if r["med_latency_ms"] is not None else None

    # 4. Refusals per bucket, by reason (rows are already per-minute aggregates).
    ref_rows = await pool.fetch(
        "SELECT date_trunc($5::text, r.window_start - $4::interval) AS b,"
        " r.reason, sum(r.count) AS n"
        " FROM session_refusals r JOIN deployments d ON d.id = r.deployment_id"
        " WHERE r.window_start >= $1 AND r.window_start < $2"
        "   AND ($3::text IS NULL OR d.agent_id = $3)"
        " GROUP BY b, r.reason",
        *args, off, bucket,
    )
    refusals = {k: zeros() for k in REFUSAL_REASONS}
    for r in ref_rows:
        i = index.get(r["b"].replace(tzinfo=None))
        if i is None or r["reason"] not in refusals:
            continue
        refusals[r["reason"]][i] = int(r["n"])

    # 5. Tools and usage per bucket from the durable stage stream, plus a
    # per-tool ranking over the whole window.
    tm_scope = (
        " FROM turn_metrics t JOIN conversations c ON c.room_name = t.room_name"
        + scope
    )
    tool_rows = await pool.fetch(
        "SELECT date_trunc($5::text, t.created_at - $4::interval) AS b,"
        " count(*) AS calls, count(*) FILTER (WHERE t.cancelled) AS failed,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY t.stage_ms) AS p50_ms"
        + tm_scope
        + " AND t.metric_type = 'ToolCall' GROUP BY b",
        *args, off, bucket,
    )
    tools = {"calls": zeros(), "failed": zeros(), "p50_ms": zeros(None)}
    for r in tool_rows:
        i = index.get(r["b"].replace(tzinfo=None))
        if i is None:
            continue
        tools["calls"][i] = r["calls"]
        tools["failed"][i] = r["failed"]
        tools["p50_ms"][i] = round(r["p50_ms"]) if r["p50_ms"] is not None else None
    tool_rank = await pool.fetch(
        "SELECT t.model AS name, count(*) AS calls,"
        " count(*) FILTER (WHERE t.cancelled) AS failed,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY t.stage_ms) AS p50_ms"
        + tm_scope
        + " AND t.metric_type = 'ToolCall' AND t.model IS NOT NULL"
        " GROUP BY t.model ORDER BY p50_ms DESC NULLS LAST LIMIT 12",
        *args,
    )
    usage_rows = await pool.fetch(
        "SELECT date_trunc($5::text, t.created_at - $4::interval) AS b,"
        " sum(t.tokens_in) FILTER (WHERE t.metric_type = 'LLMMetrics') AS tokens_in,"
        " sum(t.tokens_out) FILTER (WHERE t.metric_type = 'LLMMetrics') AS tokens_out,"
        " count(*) FILTER (WHERE t.metric_type = 'LLMMetrics') AS llm_requests,"
        " sum(t.audio_ms) FILTER (WHERE t.metric_type = 'TTSMetrics'"
        "   AND NOT t.cancelled) AS tts_audio_ms"
        + tm_scope
        + " GROUP BY b",
        *args, off, bucket,
    )
    usage = {"tokens_in": zeros(), "tokens_out": zeros(), "llm_requests": zeros(),
             "tts_minutes": zeros(0.0)}
    for r in usage_rows:
        i = index.get(r["b"].replace(tzinfo=None))
        if i is None:
            continue
        usage["tokens_in"][i] = int(r["tokens_in"] or 0)
        usage["tokens_out"][i] = int(r["tokens_out"] or 0)
        usage["llm_requests"][i] = int(r["llm_requests"] or 0)
        usage["tts_minutes"][i] = round((r["tts_audio_ms"] or 0) / 60000.0, 2)

    # 6. Composite error rate (plan section 4, graph 12): tool failures,
    # knowledge-base errors or timeouts, failed eval runs, connection-lost
    # endings; over answers + lookups + tool calls. Components kept so the
    # tooltip can explain a spike.
    err_by = {
        "tools": tools["failed"],
        "kb": [kb["errors"][i] for i in range(n)],
        "evals": eval_failures,
        "connection": endings["connection_lost"],
    }
    err_total = [sum(err_by[k][i] for k in err_by) for i in range(n)]
    err_denom = [assistant_turns[i] + kb["lookups"][i] + tools["calls"][i] for i in range(n)]
    err_rate = [
        round(err_total[i] / err_denom[i], 4) if err_denom[i] else None
        for i in range(n)
    ]

    # 7. Live reading and cap for the "Open now" signal. Same 15-minute
    # self-healing window as the quota gate.
    open_now = await pool.fetchval(
        "SELECT count(*) FROM conversations c"
        " WHERE c.ended_at IS NULL AND c.started_at > now() - interval '15 minutes'"
        "   AND ($1::text IS NULL OR c.metadata->>'agent_id' = $1)",
        agent_id or None,
    )
    cap = await pool.fetchval(
        "SELECT CASE WHEN $1::text IS NULL THEN sum(max_concurrent_sessions)"
        " ELSE max(max_concurrent_sessions) END"
        " FROM deployments WHERE status = 'active'"
        "   AND ($1::text IS NULL OR agent_id = $1)",
        agent_id or None,
    )

    quality = {
        "p50": [round(_median(v), 3) if v else None for v in quality_vals],
        "n": [len(v) for v in quality_vals],
    }
    checks = {
        name: {
            "pass_rate": [
                round(checks_pass[name][i] / checks_n[name][i], 3) if checks_n[name][i] else None
                for i in range(n)
            ],
            "n": checks_n[name],
        }
        for name in checks_n
    }
    check_rank = sorted(
        (
            {"name": name, "n": sum(checks_n[name]),
             "pass_rate": round(sum(checks_pass[name]) / sum(checks_n[name]), 3)}
            for name in checks_n if sum(checks_n[name])
        ),
        key=lambda x: x["pass_rate"],
    )

    return {
        "bucket": bucket,
        "days": days,
        "agent_id": agent_id,
        "labels": [lab.isoformat(timespec="minutes") for lab in labels],
        "series": {
            "calls": calls,
            "text_sessions": text_sessions,
            "non_engaged": non_engaged,
            "talk_min": [round(s / 60.0, 1) for s in talk_s],
            "concurrency": concurrency,
            "latency": latency,
            "stages": stages,
            "endings": endings,
            "quality": quality,
            "checks": checks,
            "interruptions": {
                "rate": [
                    round(interrupted_calls[i] / voice_calls[i], 3) if voice_calls[i] else None
                    for i in range(n)
                ],
                "voice_calls": voice_calls,
            },
            "refusals": refusals,
            "tools": tools,
            "kb": kb,
            "usage": usage,
            "errors": {"rate": err_rate, "errors": err_total, "denominator": err_denom, "by": err_by},
        },
        "ranking": {
            "checks": check_rank,
            "tools": [
                {"name": r["name"], "calls": r["calls"], "failed": r["failed"],
                 "p50_ms": round(r["p50_ms"]) if r["p50_ms"] is not None else None}
                for r in tool_rank
            ],
        },
        "flags": flags,
        "open_now": int(open_now or 0),
        "cap": int(cap) if cap is not None else None,
    }


def register(api, db, dsn):
    """Mount GET /insights/series on the admin router. `db` is the app's db
    module (get_pool), `dsn` the zero-arg function api.py uses (_dsn)."""

    @api.get("/insights/series")
    async def insights_series(
        agent_id: str | None = None,
        days: int = 7,
        start: str | None = None,
        end: str | None = None,
        tz_offset_min: int = 0,
        bucket: str | None = None,
    ):
        start_utc, end_utc, off, days_ = parse_window(start, end, days, tz_offset_min)
        pool = await db.get_pool(dsn())
        return await series(
            pool,
            agent_id=agent_id,
            start_utc=start_utc,
            end_utc=end_utc,
            off=off,
            days=days_,
            bucket=pick_bucket(bucket, days_),
        )

    return insights_series
