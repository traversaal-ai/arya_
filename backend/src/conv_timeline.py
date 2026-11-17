"""Per-session timeline (Phase B, plan graph 19).

Rebuilds each turn of a call as time spans from the durable stage stream
in turn_metrics, the way LiveKit Cloud's Agent insights draws a session,
without OpenTelemetry. Read-only; nothing new is collected.

How spans are reconstructed. Every SDK metric is emitted when its stage
finishes, and carries the stage's duration, so each one is an END time
plus a length:

  EOUMetrics   emitted when the caller's turn is declared over.
               speech_end = t - end_of_utterance_delay
               waiting  = [speech_end, t]                (deciding the caller finished)
               hearing  = [speech_end, speech_end + transcription_delay]
               The two overlap (they run in parallel), so hearing sits on a
               second lane.
  LLMMetrics   t = end of the model reply. start = t - duration.
               thinking = [start, start + ttft]          (time to first token)
               streaming = [start + ttft, t]             (rest of the reply)
  TTSMetrics   t = end of synthesis. start = t - duration.
               voice     = [start, start + ttfb]          (time to first audio)
               audio     = [start + ttfb, start + ttfb + audio_duration]
               (a reply can have several segments; each becomes spans)
  ToolCall     t = end of the tool, start = t - stage_ms. Tools carry no
               speech id, so they attach to the reply whose model spans
               bracket them, else the nearest reply that follows.

Spans sharing a speech_id form one turn. The user and agent messages
(by time) label the turn, and the agent message's `interrupted` flag marks
a reply the caller cut off.

Precision note: `raw.timestamp` (the SDK's own clock, seconds) is used
when present; `created_at` (DB write time) is the fallback. Both are
end-of-stage times, so positions are honest to within the write delay.
"""

import json

from fastapi import HTTPException


def _ts(row):
    raw = row["raw"]
    if raw:
        try:
            r = json.loads(raw) if isinstance(raw, str) else raw
            t = r.get("timestamp")
            if isinstance(t, (int, float)) and t > 0:
                return float(t), r
            return row["created_at"].timestamp(), r
        except Exception:
            pass
    return row["created_at"].timestamp(), {}


def _head(text, n=70):
    return " ".join((text or "").split())[:n]


async def timeline(pool, conversation_id):
    conv = await pool.fetchrow(
        "SELECT id, room_name, started_at, ended_at FROM conversations WHERE id = $1",
        conversation_id,
    )
    if conv is None:
        raise HTTPException(404, "unknown conversation_id")
    rows = await pool.fetch(
        "SELECT metric_type, speech_id, stage_ms, duration_ms, audio_ms,"
        " cancelled, model, raw, created_at"
        " FROM turn_metrics WHERE room_name = $1 ORDER BY created_at, id",
        conv["room_name"],
    )
    msgs = await pool.fetch(
        "SELECT role, content, created_at, metrics FROM messages"
        " WHERE conversation_id = $1 ORDER BY id",
        conversation_id,
    )
    t0 = conv["started_at"].timestamp()

    groups = {}   # speech_id -> {"spans": [], "start": s, "end": s}
    order = []    # speech ids in first-seen order
    tools = []    # (start, end, name, ok)

    def group(sid):
        if sid not in groups:
            groups[sid] = {"spans": [], "start": None, "end": None}
            order.append(sid)
        return groups[sid]

    def add(sid, kind, lane, start, end, text):
        if end <= start:
            end = start + 0.01
        g = group(sid)
        g["spans"].append({
            "kind": kind, "lane": lane,
            "start_s": round(start - t0, 3), "end_s": round(end - t0, 3),
            "text": text,
        })
        g["start"] = start if g["start"] is None else min(g["start"], start)
        g["end"] = end if g["end"] is None else max(g["end"], end)

    for r in rows:
        t, raw = _ts(r)
        kind = r["metric_type"]
        sid = r["speech_id"] or raw.get("speech_id")
        if kind == "EOUMetrics":
            eou = float(raw.get("end_of_utterance_delay") or (r["stage_ms"] or 0) / 1000)
            stt = float(raw.get("transcription_delay") or (r["duration_ms"] or 0) / 1000)
            speech_end = t - eou
            sid = sid or f"eou-{int(t * 1000)}"
            add(sid, "waiting", 0, speech_end, t, f"Waiting {eou:.2f}s to decide the caller finished")
            if stt > 0:
                add(sid, "hearing", 1, speech_end, speech_end + stt, f"Hearing {stt:.2f}s (speech to text)")
        elif kind == "LLMMetrics":
            dur = float(raw.get("duration") or (r["duration_ms"] or 0) / 1000)
            ttft = float(raw.get("ttft") or (r["stage_ms"] or 0) / 1000)
            start = t - dur
            sid = sid or f"llm-{int(t * 1000)}"
            add(sid, "thinking", 0, start, start + ttft, f"Thinking {ttft:.2f}s to the first token")
            if dur > ttft:
                add(sid, "streaming", 0, start + ttft, t, f"Streaming the reply {dur - ttft:.2f}s")
        elif kind == "TTSMetrics":
            dur = float(raw.get("duration") or (r["duration_ms"] or 0) / 1000)
            ttfb = float(raw.get("ttfb") or (r["stage_ms"] or 0) / 1000)
            audio = float(raw.get("audio_duration") or (r["audio_ms"] or 0) / 1000)
            start = t - dur
            sid = sid or f"tts-{int(t * 1000)}"
            add(sid, "voice", 0, start, start + ttfb, f"Voice {ttfb:.2f}s to the first audio")
            if audio > 0:
                add(sid, "audio", 0, start + ttfb, start + ttfb + audio,
                    f"Speaking {audio:.1f}s of audio" + (" (cancelled)" if r["cancelled"] else ""))
        elif kind == "ToolCall":
            ms = r["stage_ms"] or 0
            tools.append((t - ms / 1000, t, r["model"] or "tool", not r["cancelled"]))

    # Attach tools to the reply whose spans bracket them, else the nearest
    # following reply, else a row of their own.
    for ts, te, name, ok in tools:
        best = None
        for sid in order:
            g = groups[sid]
            if g["start"] is not None and g["start"] - 0.5 <= ts and te <= g["end"] + 0.5:
                best = sid
                break
        if best is None:
            after = [sid for sid in order if groups[sid]["start"] is not None and groups[sid]["start"] >= te - 0.5]
            best = after[0] if after else f"tool-{int(te * 1000)}"
        add(best, "tool", 1, ts, te, f"Tool {name} {te - ts:.2f}s" + ("" if ok else " (failed)"))

    # Label turns with the messages around them.
    users = [(m["created_at"].timestamp(), _head(m["content"])) for m in msgs if m["role"] == "user" and (m["content"] or "").strip()]
    agents = []
    for m in msgs:
        if m["role"] != "assistant" or not (m["content"] or "").strip():
            continue
        met = m["metrics"]
        met = json.loads(met) if isinstance(met, str) else (met or {})
        agents.append((m["created_at"].timestamp(), _head(m["content"]), bool(met.get("interrupted"))))

    # Message matching. A user message row is written when the caller's
    # turn is declared over, which is the END of the waiting span, so it
    # matches on that instant (not on the turn's first span: with
    # preemptive generation the model can start on the interim transcript
    # before the turn is declared over, so thinking may begin earlier than
    # waiting ends). An assistant row is written when the reply commits,
    # at or after the turn's last span. Each message is used once.
    turns = []
    used_users, used_agents = set(), set()
    ordered = sorted(
        (s for s in order if groups[s]["start"] is not None),
        key=lambda s: groups[s]["start"],
    )
    for sid in ordered:
        g = groups[sid]
        waits = [s for s in g["spans"] if s["kind"] == "waiting"]
        turn_over = (max(s["end_s"] for s in waits) + t0) if waits else None
        user_head = None
        if turn_over is not None:
            cands = [
                (abs(t - turn_over), i) for i, (t, _) in enumerate(users)
                if i not in used_users and abs(t - turn_over) <= 6.0
            ]
            if cands:
                _, i = min(cands)
                used_users.add(i)
                user_head = users[i][1]
        agent = None
        cands = [
            (abs(a[0] - g["end"]), i) for i, a in enumerate(agents)
            if i not in used_agents and g["start"] - 1.0 <= a[0] <= g["end"] + 8.0
        ]
        if cands:
            _, i = min(cands)
            used_agents.add(i)
            agent = agents[i]
        turns.append({
            "speech_id": sid,
            "start_s": round(g["start"] - t0, 3),
            "end_s": round(g["end"] - t0, 3),
            "user": user_head,
            "agent": agent[1] if agent else None,
            "interrupted": bool(agent and agent[2]),
            "spans": sorted(g["spans"], key=lambda s: (s["lane"], s["start_s"])),
        })

    total = max([t["end_s"] for t in turns] + [
        (conv["ended_at"].timestamp() - t0) if conv["ended_at"] else 0.0
    ])
    return {
        "conversation_id": str(conv["id"]),
        "started_at": conv["started_at"],
        "total_s": round(total, 1),
        "turns": turns,
        "has_data": bool(turns),
    }


def register(api, db, dsn):
    """Mount GET /conversations/{id}/timeline on the admin router."""
    import uuid

    @api.get("/conversations/{conversation_id}/timeline")
    async def conversation_timeline(conversation_id: uuid.UUID):
        pool = await db.get_pool(dsn())
        return await timeline(pool, conversation_id)

    return conversation_timeline
