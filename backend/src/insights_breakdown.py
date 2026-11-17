"""Insights breakdown (Phase C of PLAN-Arya-V1-Observability_v2).

One endpoint, four views that the Trends section cannot draw from the
series alone:

  models        latency per pipeline stage per model and provider (graph 15):
                the benchmark table from docs/METRICS.md as data
  documents     which knowledge-base documents answers actually used
                (from stored citations), which were retrieved (from
                rag_events.files once the worker writes it), which were
                never used (graph 26), and references per bucket per file
                for the Knowledge base tab's chart
  pipeline      STT / LLM / TTS errors per bucket and by stage, recoverable
                versus fatal (graph 14), from ErrorEvent rows the worker
                writes via worker_observability.attach
  interruption  how fast the agent stops once the caller talks over it
                (graph 17), from Interruption rows written by the same hook

Reads only. `documents.retrieved` and the two worker-fed views are empty
until the Phase C worker additions ship; the endpoint still answers.

Drop-in: `insights_breakdown.register(api, db, _dsn)`.
"""

import datetime
import statistics

from fastapi import HTTPException

BUCKETS = {
    "hour": datetime.timedelta(hours=1),
    "day": datetime.timedelta(days=1),
    "week": datetime.timedelta(days=7),
}
MAX_DAYS = 90
# Document references over time: this many files keep their own line, the
# rest fold into "Other", so a thirty-document knowledge base stays legible.
MAX_REF_LINES = 5
STAGE_LABEL = {
    "EOUMetrics": "waiting",
    "STTMetrics": "stt",
    "LLMMetrics": "thinking",
    "TTSMetrics": "speaking",
    "ToolCall": "tool",
}

# Provider names for the By model table. The worker stores whatever the
# LiveKit SDK puts in `model_provider`: a name for the cloud plugins
# ("Gemini"), "livekit" for its own turn detector, and for the
# OpenAI-compatible plugins only the host it talked to ("localhost:8010").
# The table should say who served the request, so hosts are resolved to the
# backend behind them: the bundled Speaches container (Whisper STT and
# Kokoro TTS), Ollama, or a vLLM server, with "(local)" when it runs on this
# machine and the host otherwise. Anything unrecognised is shown as stored.
LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}
# Compose service names count as local too: a worker inside the network
# reaches the bundled containers by these names.
SERVICE_HOSTNAMES = {"speaches": "speaches", "ollama": "ollama", "vllm": "vllm"}
PROVIDER_WORDS = {"livekit": "LiveKit", "ollama": "Ollama", "speaches": "Speaches", "vllm": "vLLM"}


def _host(url_or_host):
    """'http://Localhost:8010/v1' -> 'localhost:8010'; 'gpu-box' -> 'gpu-box'.
    Every loopback spelling (127.0.0.1, 0.0.0.0, host.docker.internal)
    becomes 'localhost', so one configured URL matches them all."""
    s = (url_or_host or "").strip().lower()
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0].rstrip(".")
    hostname, _, port = s.partition(":")
    if hostname in LOCAL_HOSTNAMES:
        hostname = "localhost"
    return f"{hostname}:{port}" if port else hostname


def provider_label(raw, hosts=None):
    """`hosts` maps a backend ("speaches", "ollama", "vllm") to the set of
    host[:port] strings it is reached at, from the deployment's config."""
    r = (raw or "").strip()
    if not r:
        return ""
    low = r.lower()
    if low in PROVIDER_WORDS:
        name = PROVIDER_WORDS[low]
        return name if name == "LiveKit" else f"{name} (local)"
    h = _host(r)
    hostname = h.split(":")[0]
    backend = SERVICE_HOSTNAMES.get(hostname)
    for key, hs in (hosts or {}).items():
        if h in hs or hostname in hs:
            backend = key
            break
    if backend is None:
        return r
    local = hostname in LOCAL_HOSTNAMES or hostname in SERVICE_HOSTNAMES
    return f"{PROVIDER_WORDS[backend]} ({'local' if local else h})"


def provider_hosts(config):
    """The hosts each local backend is reached at in this deployment: the
    bundled Speaches and Ollama URLs plus any Ollama or vLLM URL a saved
    agent points at. Read per request; agent configs are small files."""
    hosts = {
        "speaches": {_host(config.speaches_url())},
        "ollama": {_host(config.ollama_url())},
        "vllm": set(),
    }
    agents = []
    try:
        agents = list(config.list_saved_agents())
        agents.append(config.load_active())
    except Exception:
        pass
    for cfg in agents:
        lcl = (cfg or {}).get("local") or {}
        for url in ((lcl.get("ollama") or {}).get("llm_url"),):
            if url:
                hosts["ollama"].add(_host(url))
        v = lcl.get("vllm") or {}
        for key in ("stt_url", "llm_url", "tts_url"):
            if v.get(key):
                hosts["vllm"].add(_host(v[key]))
    # A vLLM URL that points at the bundled Speaches container is Speaches.
    hosts["vllm"] -= hosts["speaches"]
    return hosts


def parse_window(start, end, days, tz_offset_min):
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
        days = max(1, min(int(7 if days is None else days), MAX_DAYS))
        end_utc = datetime.datetime.now(datetime.timezone.utc)
        start_utc = end_utc - datetime.timedelta(days=days)
    return start_utc, end_utc, off, days


def pick_bucket(bucket, days):
    if bucket in BUCKETS:
        return bucket
    return "hour" if days <= 2 else "day"


def _truncate(local, bucket):
    if bucket == "hour":
        return local.replace(minute=0, second=0, microsecond=0)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - datetime.timedelta(days=day.weekday()) if bucket == "week" else day


def _labels(start_utc, end_utc, off, bucket):
    step = BUCKETS[bucket]
    t = _truncate((start_utc - off).replace(tzinfo=None), bucket)
    end_local = (end_utc - off).replace(tzinfo=None)
    out = []
    while t < end_local and len(out) < 24 * MAX_DAYS + 1:
        out.append(t)
        t += step
    return out


async def _has_column(pool, table, column):
    return bool(await pool.fetchval(
        "SELECT 1 FROM information_schema.columns"
        " WHERE table_name = $1 AND column_name = $2",
        table, column,
    ))


async def breakdown(pool, *, agent_id, start_utc, end_utc, off, days, bucket, hosts=None):
    labels = _labels(start_utc, end_utc, off, bucket)
    n = len(labels)
    index = {lab: i for i, lab in enumerate(labels)}
    scope = (
        " WHERE c.started_at >= $1 AND c.started_at < $2"
        "   AND ($3::text IS NULL OR c.metadata->>'agent_id' = $3)"
    )
    args = (start_utc, end_utc, agent_id or None)
    tm = " FROM turn_metrics t JOIN conversations c ON c.room_name = t.room_name" + scope

    # 1. Latency by stage, model and provider.
    model_rows = await pool.fetch(
        "SELECT t.metric_type, t.model, t.provider, count(*) AS n,"
        " percentile_cont(0.5) WITHIN GROUP (ORDER BY t.stage_ms) AS p50_ms,"
        " percentile_cont(0.95) WITHIN GROUP (ORDER BY t.stage_ms) AS p95_ms"
        + tm
        + " AND t.stage_ms IS NOT NULL AND t.metric_type = ANY($4::text[])"
        " GROUP BY 1, 2, 3 ORDER BY 1, 5 DESC",
        *args, list(STAGE_LABEL),
    )
    models = [
        {
            "stage": STAGE_LABEL[r["metric_type"]],
            "model": r["model"] or "(unknown)",
            "provider": provider_label(r["provider"], hosts),
            "n": r["n"],
            "p50_ms": round(r["p50_ms"]) if r["p50_ms"] is not None else None,
            "p95_ms": round(r["p95_ms"]) if r["p95_ms"] is not None else None,
        }
        for r in model_rows
    ]

    # 2. Documents. Used = document citations on answers (the used-in-answer
    # gate already applied by the worker). Retrieved = the files each
    # lookup injected, once rag_events.files exists. Never used = documents
    # in the knowledge base with no citation in the window.
    used_rows = await pool.fetch(
        "SELECT e->>'filename' AS filename, count(*) AS used,"
        " count(DISTINCT m.conversation_id) AS calls"
        " FROM messages m JOIN conversations c ON c.id = m.conversation_id,"
        " jsonb_array_elements(m.citations) e"
        + scope
        + " AND m.role = 'assistant' AND m.citations IS NOT NULL"
        "   AND (e->>'type') IS DISTINCT FROM 'web' AND e->>'filename' IS NOT NULL"
        " GROUP BY 1 ORDER BY used DESC",
        *args,
    )
    used = {r["filename"]: {"filename": r["filename"], "used": r["used"], "calls": r["calls"]} for r in used_rows}
    retrieved = {}
    references = {"files": [], "other": None}
    has_files = await _has_column(pool, "rag_events", "files")
    if has_files:
        ret_rows = await pool.fetch(
            "SELECT f AS filename, count(*) AS retrieved"
            " FROM rag_events r, jsonb_array_elements_text(r.files) f"
            " WHERE r.created_at >= $1 AND r.created_at < $2"
            "   AND ($3::text IS NULL OR r.agent_id = $3)"
            " GROUP BY 1",
            *args,
        )
        retrieved = {r["filename"]: r["retrieved"] for r in ret_rows}
        # The same count per bucket and per file, for the references chart:
        # how often each document was pulled into an answer over time.
        ref_rows = await pool.fetch(
            "SELECT date_trunc($5::text, r.created_at - $4::interval) AS b,"
            " f AS filename, count(*) AS n"
            " FROM rag_events r, jsonb_array_elements_text(r.files) f"
            " WHERE r.created_at >= $1 AND r.created_at < $2"
            "   AND ($3::text IS NULL OR r.agent_id = $3)"
            " GROUP BY 1, 2",
            *args, off, bucket,
        )
        per_file = {}
        for r in ref_rows:
            i = index.get(r["b"].replace(tzinfo=None))
            if i is None:
                continue
            per_file.setdefault(r["filename"], [0] * n)[i] += r["n"]
        ranked = sorted(per_file.items(), key=lambda kv: (-sum(kv[1]), kv[0]))
        top, rest = ranked[:MAX_REF_LINES], ranked[MAX_REF_LINES:]
        references["files"] = [{"filename": k, "values": v} for k, v in top]
        if rest:
            references["other"] = [sum(v[i] for _, v in rest) for i in range(n)]
    kb_rows = await pool.fetch(
        "SELECT filename, agent_id FROM knowledge_base"
        " WHERE status = 'ready' AND ($1::text IS NULL OR agent_id = $1)",
        agent_id or None,
    )
    kb_files = sorted({r["filename"] for r in kb_rows})
    documents = {
        "used": sorted(
            (
                {**v, "retrieved": retrieved.get(k)} for k, v in used.items()
            ),
            key=lambda d: -d["used"],
        ),
        "never_used": [f for f in kb_files if f not in used],
        "retrieved_known": has_files,
        "knowledge_base_files": len(kb_files),
        "references": references,
    }

    # 3. Pipeline errors by stage per bucket (worker-fed).
    err_rows = await pool.fetch(
        "SELECT date_trunc($5::text, t.created_at - $4::interval) AS b,"
        " t.model AS stage, bool_or(NOT t.cancelled) AS any_recoverable,"
        " count(*) AS n, count(*) FILTER (WHERE t.cancelled) AS fatal"
        + tm
        + " AND t.metric_type = 'ErrorEvent' GROUP BY 1, 2",
        *args, off, bucket,
    )
    pipeline = {"by_stage": {}, "total": 0, "fatal": 0}
    for r in err_rows:
        i = index.get(r["b"].replace(tzinfo=None))
        if i is None:
            continue
        stage = r["stage"] or "unknown"
        pipeline["by_stage"].setdefault(stage, [0] * n)[i] += r["n"]
        pipeline["total"] += r["n"]
        pipeline["fatal"] += r["fatal"]

    # 4. Interruption latency (worker-fed): how fast the agent stopped.
    int_rows = await pool.fetch(
        "SELECT date_trunc($5::text, t.created_at - $4::interval) AS b, t.stage_ms"
        + tm
        + " AND t.metric_type = 'Interruption' AND t.stage_ms IS NOT NULL",
        *args, off, bucket,
    )
    per_bucket = [[] for _ in range(n)]
    all_vals = []
    for r in int_rows:
        i = index.get(r["b"].replace(tzinfo=None))
        if i is None:
            continue
        per_bucket[i].append(r["stage_ms"])
        all_vals.append(r["stage_ms"])
    interruption = {
        "p50_ms": [round(statistics.median(v)) if v else None for v in per_bucket],
        "n": [len(v) for v in per_bucket],
        "overall_p50_ms": round(statistics.median(all_vals)) if all_vals else None,
        "overall_p95_ms": (
            round(sorted(all_vals)[int((len(all_vals) - 1) * 0.95)]) if all_vals else None
        ),
        "count": len(all_vals),
    }

    return {
        "bucket": bucket,
        "days": days,
        "agent_id": agent_id,
        "labels": [lab.isoformat(timespec="minutes") for lab in labels],
        "models": models,
        "documents": documents,
        "pipeline": pipeline,
        "interruption": interruption,
    }


def register(api, db, dsn):
    import config

    @api.get("/insights/breakdown")
    async def insights_breakdown(
        agent_id: str | None = None,
        days: int = 7,
        start: str | None = None,
        end: str | None = None,
        tz_offset_min: int = 0,
        bucket: str | None = None,
    ):
        start_utc, end_utc, off, days_ = parse_window(start, end, days, tz_offset_min)
        pool = await db.get_pool(dsn())
        return await breakdown(
            pool, agent_id=agent_id, start_utc=start_utc, end_utc=end_utc,
            off=off, days=days_, bucket=pick_bucket(bucket, days_),
            hosts=provider_hosts(config),
        )

    return insights_breakdown
