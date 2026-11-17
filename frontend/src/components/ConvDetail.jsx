import { useCallback, useEffect, useMemo, useState } from "react";
import {
  deleteConversation,
  getConvMessages,
  getConvTimeline,
} from "../api.js";
import LinkChips from "./LinkChips.jsx";
import RichText from "./RichText.jsx";
import Timeline from "./Timeline.jsx";
import OptionCards from "./OptionCards.jsx";
import SourceCards from "./SourceCards.jsx";
import ConfirmDialog from "./ConfirmDialog.jsx";
import CopyField from "./CopyField.jsx";
import ErrorNote from "./ErrorNote.jsx";
import { hostOf, safeHref } from "../safeHref.js";
import { withWrittenAddresses } from "../spokenEmail.js";

/* One conversation, fully opened: transcript + per-turn latency badges +
   the call's eval verdicts. Shared by History (its old inline detail view
   lifted out verbatim) and Insights (drill-down from the table), so the
   two screens can never drift apart. */

export function fmtWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// end falls back to the conversation's last message when ended_at is
// missing; with neither (abandoned, message-less rooms) the call never
// really happened — render a zero duration in the same s/m format as
// every other row, rather than "ongoing" forever.
export function fmtDuration(start, end) {
  if (!start || !end) return "0s";
  const s = Math.max(0, Math.round((new Date(end) - new Date(start)) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export function fmtS(x) {
  if (x == null || Number.isNaN(x)) return null;
  return x >= 10 ? `${Math.round(x)}s` : `${x.toFixed(1)}s`;
}

export function scoreClass(score) {
  return score >= 0.75 ? "score-chip ok" : score >= 0.5 ? "score-chip mid" : "score-chip bad";
}

// The one gate for showing a quality number anywhere: null unless every
// selected check actually ran. A score computed off a partial run (judge
// errors drop checks silently) is a confident-looking wrong number, so it
// is hidden rather than shown; the verdict panel still explains why.
export function evalScore(evals) {
  if (!evals || typeof evals.score !== "number" || evals.partial) return null;
  return evals.score;
}

/* Call endings, labeled only as strongly as the data supports.

   caller ended    an explicit leave reached us: the End button, or the
                   browser SDK's clean leave on a closed tab
   after silence   the inactivity watchdog closed the call (exact)
   time limit      the deployment's wall-clock cap (exact)
   connection lost a timeout or media failure with no leave. Best effort,
                   not a fact: a backgrounded mobile browser can look the
                   same, hence the softer wording
   unknown         nothing was recorded (crash, restart, or a call from
                   before this tracking existed). Deliberately not a claim
                   about what happened, which is why the old
                   "disconnected" label was retired. */
const ENDING_LABEL = {
  user_ended: "caller ended",
  inactivity: "ended after silence",
  time_limit: "hit the time limit",
  connection_lost: "connection lost",
  unknown: "unknown",
};

export function endingLabel(reason) {
  return ENDING_LABEL[reason] || ENDING_LABEL.unknown;
}

function median(xs) {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

const VERDICT_GLYPH = { pass: "✓", fail: "✕", maybe: "~" };

// The judges' snake_case names, humanized for the panel.
const JUDGE_LABEL = {
  task_completion: "Task completion",
  accuracy: "Accuracy",
  relevancy: "Relevancy",
  conciseness: "Conciseness",
  tool_use: "Tool usage",
  coherence: "Consistency",
  faithfulness: "Faithfulness",
  context_relevance: "Context relevance",
  grounding: "Used your documents",
  voice_format: "Spoken format",
};

// "accuracy, relevancy and faithfulness": the per-answer checks, in words.
function listWords(words) {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

// Same opening-words key the worker stores per judged answer, so a
// verdict attaches to its bubble by content rather than by position.
const headOf = (s) => (s || "").trim().split(/\s+/).join(" ").slice(0, 80);

/* Download the open transcript as a Markdown file (readable as plain text
   too). Built client-side from the already-loaded messages, so it works
   identically for local and remote (proxied) conversations. */
function exportChat(conv, messages, stats) {
  const name = conv.agent_name || conv.room_name || "conversation";
  const lines = [
    `# Conversation with ${name}`,
    "",
    `- Started: ${fmtWhen(conv.started_at)}`,
    `- Duration: ${fmtDuration(conv.started_at, conv.ended_at || conv.last_message_at)}`,
  ];
  if (stats?.turns) lines.push(`- Turns: ${stats.turns}`);
  const exportScore = evalScore(conv.evals);
  if (exportScore != null) {
    lines.push(`- Quality score: ${Math.round(exportScore * 100)}%`);
  }
  lines.push("", "---", "");
  for (const m of messages) {
    if (!["user", "assistant"].includes(m.role) || !m.content) continue;
    lines.push(`**${m.role === "user" ? "Caller" : "Agent"}:** ${m.content}`);
    const docs = (m.citations || []).filter((c) => c.type !== "web");
    const web = (m.citations || []).filter((c) => c.type === "web");
    if (docs.length > 0) {
      lines.push(
        `> Sources: ${[...new Set(docs.map((c) => c.filename))].join(", ")}`
      );
    }
    for (const c of web) {
      const href = safeHref(c.link);
      const host = href ? hostOf(href) : c.source || "";
      lines.push(`> Web: ${c.title || host || "untitled"}${host ? ` (${host})` : ""}`);
    }
    for (const l of m.links || []) {
      if (l.href) lines.push(`> Link: ${l.href}`);
    }
    lines.push("");
  }
  const day = (conv.started_at || "").slice(0, 10) || "chat";
  const safe = name.replace(/[^\w-]+/g, "-").replace(/^-+|-+$/g, "");
  const blob = new Blob([lines.join("\n")], {
    type: "text/markdown;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${safe}-${day}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/* Latency badge on an agent bubble. The headline is end-to-end ("how long
   the caller waited") when the SDK recorded it; otherwise the sum of the
   measured stages, marked with ≈. Streaming STT often leaves e2e unset
   while every stage is present, and gating on e2e hid the badge on about
   half of all spoken turns. The expansion lists the pipeline stages and
   any tools that ran to produce this reply. Native <details>: keyboard
   and screen-reader behavior for free. */
function TurnLatency({ metrics, prevUserMetrics }) {
  const m = metrics || {};
  const stages = [
    ["Waiting (end of turn)", prevUserMetrics?.end_of_turn_delay],
    ["Hearing (speech to text)", prevUserMetrics?.transcription_delay],
    ["Thinking (first token)", m.llm_node_ttft],
    ["Speaking (first audio)", m.tts_node_ttfb],
  ].filter(([, v]) => v != null);
  const tools = (m.tools || []).map((t) => [
    `Tool: ${t.name}${t.ok === false ? " (failed)" : ""}`,
    t.ms / 1000,
  ]);
  const e2e = m.e2e_latency;
  const approx =
    e2e == null && stages.length > 0
      ? stages.reduce((s, [, v]) => s + v, 0)
      : null;
  if (e2e == null && approx == null) return null;
  const rows = [...stages, ...tools];
  return (
    <details className="tlat">
      <summary
        className={e2e == null ? "tlat-badge approx" : "tlat-badge"}
        title={
          e2e == null
            ? "Sum of the measured stages. End-to-end time was not recorded for this turn."
            : "Time from the caller finishing to the agent starting to answer"
        }
      >
        {e2e == null ? `≈ ${fmtS(approx)}` : fmtS(e2e)}
      </summary>
      {rows.length > 0 && (
        <div className="tlat-rows">
          {rows.map(([label, v]) => (
            <div className="tlat-row" key={label}>
              <span>{label}</span>
              <span className="tlat-val">{fmtS(v)}</span>
            </div>
          ))}
        </div>
      )}
    </details>
  );
}

// fetchMessages / deleteFn: optional overrides so a transcript living on a
// remote engine works through the backend proxy; defaults are the local
// API. Callers that want the transcript read-only omit onDeleted, which
// hides the delete button. onVisitor(userId) opens the Visitors screen on
// this call's visitor; omitted where that screen is already the context.
export default function ConvDetail({
  conv,
  onBack,
  onDeleted,
  fetchMessages,
  fetchTimeline,
  deleteFn,
  onOpenAgent,
  onVisitor,
}) {
  const [messages, setMessages] = useState(null);
  // "transcript" (the default) or "timeline": the same call as bars per
  // pipeline stage, rebuilt from the stored stage metrics.
  const [view, setView] = useState("transcript");
  const timelineFetch = useCallback(
    () => (fetchTimeline || getConvTimeline)(conv.id),
    [conv.id, fetchTimeline]
  );
  const [busy, setBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [scoreOpen, setScoreOpen] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setErr("");
    try {
      // Spoken email addresses written back as addresses, but only
      // ones already written elsewhere in the same conversation, so
      // nothing is guessed. Done at load so the Markdown export gets
      // them too, not just the screen. See spokenEmail.js.
      setMessages(withWrittenAddresses(
        await (fetchMessages || getConvMessages)(conv.id),
        "content"
      ));
    } catch (e) {
      setErr(`Couldn't load this transcript: ${e.message}`);
    }
  }, [conv.id, fetchMessages]);

  useEffect(() => {
    load();
  }, [load]);

  // The per-call stats line, computed from the transcript itself so it
  // agrees with what's on screen (not with a row cached before reload).
  const stats = useMemo(() => {
    if (!messages || messages.length === 0) return null;
    const turns = messages.filter((m) =>
      ["user", "assistant"].includes(m.role)
    ).length;
    // Response time per answer: end to end when recorded, else the sum of
    // the measured stages (same rule as the badge and the dashboard).
    const resp = (mm) =>
      mm?.e2e_latency ??
      (mm?.llm_node_ttft != null && mm?.tts_node_ttfb != null
        ? mm.llm_node_ttft + mm.tts_node_ttfb
        : null);
    const lats = messages
      .filter((m) => m.role === "assistant")
      .map((m) => resp(m.metrics))
      .filter((v) => v != null);
    const sourced = messages.filter(
      (m) => m.role === "assistant" && m.citations && m.citations.length > 0
    ).length;
    // A spoken call has voice timings on its answers; a typed chat has
    // none, and saying so beats a blank that reads as a failure.
    const voice = messages.some(
      (m) =>
        m.role === "assistant" &&
        (m.metrics?.tts_node_ttfb != null || m.metrics?.e2e_latency != null)
    );
    const cut = messages.filter(
      (m) => m.role === "assistant" && m.metrics?.interrupted
    ).length;
    return { turns, med: median(lats), sourced, voice, cut };
  }, [messages]);

  const evals = conv.evals || null;
  // Per-answer verdicts and spoken-format flags, keyed by opening words.
  const turnMap = useMemo(() => {
    const map = new Map();
    for (const t of evals?.turns || []) map.set(t.head, t);
    return map;
  }, [evals]);
  const flagMap = useMemo(() => {
    const map = new Map();
    for (const f of evals?.voice_flags || []) map.set(f.head, f);
    return map;
  }, [evals]);
  // The score card owns the quality number when there are per-check
  // verdicts to show; otherwise the stats line keeps the plain chip.
  const checks = evals?.judges ? Object.entries(evals.judges) : [];
  const passed = checks.filter(([, j]) => j.verdict === "pass").length;

  async function removeConv() {
    setBusy(true);
    setErr("");
    try {
      await (deleteFn || deleteConversation)(conv.id);
      setConfirmOpen(false);
      onDeleted?.();
    } catch (e) {
      setErr(`Couldn't delete this conversation: ${e.message}`);
      setConfirmOpen(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      {/* Two columns: the call's identity on the left, its score on the
          right. The right half of this block was empty space, and the score
          belongs beside the call it describes rather than under the
          transcript. The per-check reasoning stays collapsed and opens
          full-width below, so the header never changes height. */}
      <div className="conv-head">
      <div className="conv-head-main">
      <h1 className="h-serif">
        Conversation <em>transcript</em>
      </h1>
      <p className="sub">
        {/* The agent's name is the way back to the agent itself — a
            transcript otherwise names it and leaves you stranded. */}
        {onOpenAgent && conv.agent_id ? (
          <button
            type="button"
            className="linkish"
            onClick={() => onOpenAgent(conv.agent_id)}
            title="Open this agent"
          >
            {conv.agent_name || conv.room_name}
          </button>
        ) : (
          conv.agent_name || conv.room_name
        )}{" "}
        · {fmtWhen(conv.started_at)} ·{" "}
        {fmtDuration(conv.started_at, conv.ended_at || conv.last_message_at)}
        {conv.ended_reason && <> · {endingLabel(conv.ended_reason)}</>}
        {conv.user_id && (
          <>
            {" "}· visitor{" "}
            <span className="vis-tag">{conv.user_id.slice(0, 8)}</span>
            {conv.channel && <span className="vis-chan">{conv.channel}</span>}
          </>
        )}
      </p>
      {/* The full id with a copy affordance, and the way to everything
          else this person said. Only where the Visitors screen is not
          already the context. Visitor ids are not secrets. */}
      {onVisitor && conv.user_id && (
        <div className="conv-visitor">
          <CopyField label="Visitor id" value={conv.user_id} />
          <button
            type="button"
            className="linkish"
            onClick={() => onVisitor(conv.user_id)}
          >
            See all conversations from this visitor
          </button>
        </div>
      )}
      {stats && (
        <p className="conv-stats">
          {stats.turns} turns
          {!stats.voice && stats.turns > 0 && (
            <> · text session, no voice timings</>
          )}
          {stats.voice && stats.med != null && (
            <> · median response {fmtS(stats.med)}</>
          )}
          {stats.cut > 0 && (
            <>
              {" "}· cut off {stats.cut} time{stats.cut === 1 ? "" : "s"}
            </>
          )}
          {stats.sourced > 0 && (
            <>
              {" "}· {stats.sourced} answer{stats.sourced === 1 ? "" : "s"}{" "}
              backed by sources
            </>
          )}
          {checks.length === 0 && evalScore(evals) != null && (
            <>
              {" "}·{" "}
              <span className={scoreClass(evals.score)}>
                quality {Math.round(evals.score * 100)}%
              </span>
            </>
          )}
          {checks.length === 0 && evals && evals.partial && (
            <> · quality not scored ({evals.checks_run} of {evals.checks_expected} checks ran)</>
          )}
          {evals && evals.error && (
            <span title={evals.error}>
              {" "}· quality scoring failed ({evals.error.split(":")[0]})
            </span>
          )}
        </p>
      )}
      </div>
      {checks.length > 0 && (
        <aside className="qual-card">
          <div className="qual-top">
            <span className="qual-lab">Conversation quality</span>
            {evalScore(evals) != null ? (
              <span className={scoreClass(evals.score)}>
                {Math.round(evals.score * 100)}%
              </span>
            ) : (
              <span className="qual-none">not scored</span>
            )}
          </div>
          <p className="qual-sum">
            {evals.partial
              ? `${evals.checks_run} of ${evals.checks_expected} checks ran`
              : `${passed} of ${checks.length} checks passed`}
          </p>
          <button
            type="button"
            className="qual-more"
            aria-expanded={scoreOpen}
            onClick={() => setScoreOpen((v) => !v)}
          >
            {scoreOpen ? "Hide details" : "How this conversation was scored"}
          </button>
        </aside>
      )}
      </div>
      {scoreOpen && checks.length > 0 && (
        <div className="eval-panel">
          {checks.map(([name, j]) => (
            <div className="eval-row" key={name}>
              <span className={`eval-verdict ${j.verdict}`}>
                {VERDICT_GLYPH[j.verdict] || "?"}
              </span>
              <span className="eval-name">{JUDGE_LABEL[name] || name}</span>
              <span className="eval-why">{j.reasoning}</span>
            </div>
          ))}
          <p className="eval-note">
            {evals.model
              ? `Each check was evaluated by ${evals.model} after the conversation.`
              : "Each check was evaluated by an AI model after the conversation."}
            {evals.turns_total
              ? ` ${listWords((evals.per_turn || ["accuracy", "relevancy"]).map((k) => (JUDGE_LABEL[k] || k).toLowerCase())).replace(/^./, (ch) => ch.toUpperCase())} were judged per answer (${evals.turns_judged} of ${evals.turns_total}); look for the marks under each reply.`
              : ""}
            {evals.context
              ? ` ${evals.context.answers} answer${evals.context.answers === 1 ? "" : "s"} received document excerpts, which the judge read alongside the answer.`
              : ""}
            {evals.fallback_model && evals.fallback_checks?.length
              ? ` ${evals.fallback_checks.length} check${evals.fallback_checks.length === 1 ? "" : "s"} used the backup judge ${evals.fallback_model}.`
              : ""}
            {evals.partial
              ? " Some checks did not run, so no overall score is shown."
              : ""}
            {" "}
            {checks.some(([n]) => n === "context_relevance")
              ? "Used your documents, Context relevance and Spoken format"
              : "Used your documents and Spoken format"}{" "}
            are shown but never counted in the score.
          </p>
        </div>
      )}
      {err && <ErrorNote message={err} onRetry={load} />}
      {!messages && !err && <div className="spin" />}
      {messages && messages.length === 0 && (
        <p className="sub">No messages were recorded for this call.</p>
      )}
      {messages && messages.length > 0 && (
        <div className="conv-view" role="group" aria-label="Show the call as">
          {[
            ["transcript", "Transcript"],
            ["timeline", "Timeline"],
          ].map(([k, label]) => (
            <button
              key={k}
              type="button"
              className={view === k ? "chip chip-btn on" : "chip chip-btn"}
              aria-pressed={view === k}
              onClick={() => setView(k)}
            >
              {label}
            </button>
          ))}
        </div>
      )}
      {messages && view === "timeline" && <Timeline fetch={timelineFetch} />}
      {messages && view === "transcript" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {messages.map((m, i) => {
            // The stage breakdown pairs an answer with the user turn it
            // responded to — "hearing" time lives on the USER message.
            const prev =
              m.role === "assistant" && messages[i - 1]?.role === "user"
                ? messages[i - 1].metrics
                : null;
            const key = m.role === "assistant" ? headOf(m.content) : null;
            const tv = key ? turnMap.get(key) : null;
            const vf = key ? flagMap.get(key) : null;
            const cut = m.role === "assistant" && m.metrics?.interrupted;
            const perTurn = evals?.per_turn || [];
            return (
              <div
                key={i}
                className={m.role === "user" ? "pm-row user" : "pm-row"}
              >
                <div className="pm-line">
                  <div className="pm-bub" title={fmtWhen(m.created_at)}>
                    {m.role === "assistant" ? <RichText text={m.content} /> : m.content}
                  </div>
                  {m.role === "assistant" && (
                    <TurnLatency metrics={m.metrics} prevUserMetrics={prev} />
                  )}
                </div>
                {/* Per-answer marks: the judge's accuracy and relevancy for
                    THIS reply, a spoken-format flag, and whether the caller
                    cut it off. Hover for the reasoning. */}
                {(tv || vf || cut) && (
                  <div className="turn-marks">
                    {cut && (
                      <span
                        className="tmark cut"
                        title="The caller started talking and this reply was cut short"
                      >
                        cut off
                      </span>
                    )}
                    {tv && perTurn.includes("accuracy") && (
                      <span
                        className={`tmark ${tv.accuracy.verdict}`}
                        title={tv.accuracy.reasoning}
                      >
                        accuracy {VERDICT_GLYPH[tv.accuracy.verdict] || "?"}
                      </span>
                    )}
                    {tv && perTurn.includes("relevancy") && (
                      <span
                        className={`tmark ${tv.relevancy.verdict}`}
                        title={tv.relevancy.reasoning}
                      >
                        relevancy {VERDICT_GLYPH[tv.relevancy.verdict] || "?"}
                      </span>
                    )}
                    {/* The document checks, only on answers that received
                        excerpts: faithful to them, excerpts fit the
                        question, and the judge's reading of whether the
                        answer drew on them. */}
                    {tv && tv.faithfulness && (
                      <span
                        className={`tmark ${tv.faithfulness.verdict}`}
                        title={tv.faithfulness.reasoning}
                      >
                        faithful {VERDICT_GLYPH[tv.faithfulness.verdict] || "?"}
                      </span>
                    )}
                    {tv && tv.context_relevance && (
                      <span
                        className={`tmark ${tv.context_relevance.verdict}`}
                        title={tv.context_relevance.reasoning}
                      >
                        context {VERDICT_GLYPH[tv.context_relevance.verdict] || "?"}
                      </span>
                    )}
                    {tv && "used_context" in tv && (
                      <span
                        className={`tmark ${tv.used_context ? "pass" : "flag"}`}
                        title={
                          tv.used_context
                            ? "The judge read this answer as drawing on the document excerpts it was given"
                            : "The judge read this answer as not drawing on the document excerpts it was given"
                        }
                      >
                        documents {tv.used_context ? "used" : "not used"}
                      </span>
                    )}
                    {vf && (
                      <span
                        className="tmark flag"
                        title="This text reads badly when spoken aloud"
                      >
                        spoken format: {vf.issues.join(", ")}
                      </span>
                    )}
                  </div>
                )}
                {/* Same components as the builder's live transcript, in
                    their default presentation: plain option rows, doc
                    chips and the "Web sources" chip popover. The clickable
                    cards are a widget-only presentation. */}
                <LinkChips links={m.links} />
                <OptionCards groups={m.options} />
                <SourceCards cits={m.citations} exclude={m.links} />
              </div>
            );
          })}
        </div>
      )}
      {/* The itinerary sits AFTER the transcript: it is something you reach
          for once you have read the call, and at the top it pushed the
          conversation itself below the fold. */}
      {conv.artifact_id && (
        <div className="sum" style={{ marginTop: 18 }}>
          <a
            className="chip chip-btn itin-btn"
            href={`/api/artifacts/${conv.artifact_id}`}
            target="_blank"
            rel="noreferrer"
            title={conv.artifact_title || "Itinerary"}
          >
            ✈️ View itinerary
          </a>
        </div>
      )}
      <div className="btn-row">
        <button className="btn ghost" onClick={onBack} disabled={busy}>
          Back to list
        </button>
        {messages && messages.length > 0 && (
          <button
            className="btn"
            onClick={() => exportChat(conv, messages, stats)}
            disabled={busy}
          >
            Export chat
          </button>
        )}
        {onDeleted && (
          <button
            className="btn danger"
            onClick={() => setConfirmOpen(true)}
            disabled={busy}
          >
            {busy ? "Deleting…" : "Delete chat"}
          </button>
        )}
      </div>
      <ConfirmDialog
        open={confirmOpen}
        title="Delete this conversation?"
        body="The transcript can't be recovered."
        confirmLabel="Delete"
        danger
        busy={busy}
        onConfirm={removeConv}
        onCancel={() => setConfirmOpen(false)}
      />
    </div>
  );
}
