"""Worker-side collection for Phase C (plan graphs 14, 17, 26).

Three additions, all fire-and-forget through the worker's existing
_write_turn_metric helper, so a database hiccup never touches a call:

  pipeline errors   the session's `error` event (STT, LLM, TTS, realtime)
                    becomes an ErrorEvent row: stage in `model`, fatal or
                    recovered in `cancelled`, details in `raw`
  interruptions     how fast the agent stopped once the caller talked over
                    it, from the user and agent state events: an
                    Interruption row with the latency in `stage_ms`
  document files    rag_files() picks the filenames a lookup actually
                    injected, for the new rag_events.files column

Wiring in agent.py, after the metrics and tool handlers:

    import worker_observability
    ...
    worker_observability.attach(session, _write_turn_metric, logger)

and in _log_rag's INSERT, one extra column (see INTEGRATION-C.md).
"""

import asyncio
import time

# A caller speaking over the agent counts as an interruption only if the
# agent actually stops within this window; a longer overlap is a
# backchannel ("mm-hm") the agent talked through, which is not a stop.
INTERRUPT_WINDOW_S = 3.0
# How soon before the agent stopped the caller may have stopped talking and
# still count as the cause: a one-word "stop" ends almost as the agent does.
USER_GRACE_S = 0.6

# In-flight writes, so none is garbage-collected before it finishes.
_tasks: set = set()


def _stage_of(err) -> str:
    name = type(err).__name__
    return {
        "LLMError": "llm",
        "STTError": "stt",
        "TTSError": "tts",
        "RealtimeModelError": "realtime",
        "InterruptionDetectionError": "interruption_model",
    }.get(name, "unknown")


def rag_files(record: dict) -> list[str]:
    """Filenames a lookup injected. Hits are sorted by score and both gates
    keep a prefix, so the kept files are the first `kept` of the raw list."""
    names = record.get("filenames") or []
    kept = record.get("kept") or 0
    return list(dict.fromkeys(names[:kept])) if kept else []


def attach(session, write_turn_metric, logger=None) -> None:
    state = {
        "agent_speaking": False,
        "user_over_at": None,   # when the caller began talking over the agent
        "user_speaking": False,
        "user_stopped_at": None,
    }

    def _write(row, raw):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop (unit tests): nothing to write to
        # Hold a reference until the write finishes: the loop keeps only a
        # weak one, so a fire-and-forget task can be collected mid-flight.
        task = loop.create_task(write_turn_metric(row, raw))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    @session.on("error")
    def _on_error(ev):
        err = getattr(ev, "error", None)
        src = getattr(ev, "source", None)
        stage = _stage_of(err)
        recoverable = bool(getattr(err, "recoverable", False))
        exc = getattr(err, "error", None)
        message = (str(exc) if exc is not None else str(err))[:300]
        if logger:
            logger.warning(
                f"pipeline error ({stage}, {'recovered' if recoverable else 'fatal'}): {message}"
            )
        _write(
            {
                "metric_type": "ErrorEvent",
                "model": stage,
                "provider": getattr(src, "provider", None) if src is not None else None,
                "cancelled": not recoverable,
            },
            {
                "stage": stage,
                "label": getattr(err, "label", None),
                "recoverable": recoverable,
                "message": message,
                "model": getattr(src, "model", None) if src is not None else None,
                "kind": type(exc).__name__ if exc is not None else None,
            },
        )

    @session.on("agent_state_changed")
    def _on_agent_state(ev):
        new, old = getattr(ev, "new_state", None), getattr(ev, "old_state", None)
        now = getattr(ev, "created_at", None) or time.time()
        if new == "speaking":
            state["agent_speaking"] = True
            state["user_over_at"] = None
            return
        if old == "speaking":
            state["agent_speaking"] = False
            t_user = state["user_over_at"]
            state["user_over_at"] = None
            if t_user is None or not 0 <= now - t_user <= INTERRUPT_WINDOW_S:
                return
            # The caller must still be talking when the agent stops, or have
            # stopped only an instant earlier. A short "mm-hm" that ended
            # well before the reply finished on its own is not what made the
            # agent stop, and counting it would invent an interruption.
            if not (
                state["user_speaking"]
                or (
                    state["user_stopped_at"] is not None
                    and now - state["user_stopped_at"] <= USER_GRACE_S
                )
            ):
                return
            # round, not int: 101.35 - 101.0 is 349.999... in floating point.
            ms = round((now - t_user) * 1000)
            if logger:
                logger.info(f"interruption: agent stopped {ms}ms after the caller started")
            _write(
                {"metric_type": "Interruption", "stage_ms": ms},
                {"stopped_after_ms": ms, "from_state": old, "to_state": new},
            )

    @session.on("user_state_changed")
    def _on_user_state(ev):
        new = getattr(ev, "new_state", None)
        at = getattr(ev, "created_at", None) or time.time()
        if new == "speaking":
            state["user_speaking"] = True
            if state["agent_speaking"] and state["user_over_at"] is None:
                state["user_over_at"] = at
        elif state["user_speaking"]:
            state["user_speaking"] = False
            state["user_stopped_at"] = at
