import { useId, useRef, useState } from "react";
import TrendChart, { ExpandedContext, PanelTitleContext } from "./TrendChart.jsx";
import InfoIcon from "../InfoIcon.jsx";
import { useFocusTrap } from "../../focus.js";
import { ByModel, DocReferences, Documents, InterruptionLatency, PipelineErrors } from "./Breakdown.jsx";

/* The charts of one dashboard tab, every one fed by the /api/insights/series
   response (and the breakdown response where a chart needs it).

   props
     tab        which tab is open: overview, calls, speed, quality, kb,
                tools, errors, usage
     data       the series response (labels, series, ranking, bucket)
     breakdown  the breakdown response, optional (older engines have none)
     onPick     (bucketIndex, tag | null) => void. The screen filters its
                Conversations table to the calls in that bucket, narrowed by
                the tag when one is given (see flags in the response).        */

const ENDING_LABEL = {
  user_ended: "caller ended",
  inactivity: "ended after silence",
  time_limit: "hit the time limit",
  connection_lost: "connection lost",
  unknown: "unknown",
};
const CHECK_LABEL = {
  task_completion: "Task completion",
  accuracy: "Accuracy",
  relevancy: "Relevancy",
  conciseness: "Conciseness",
  coherence: "Consistency",
  tool_use: "Tool usage",
  faithfulness: "Faithfulness",
  context_relevance: "Context relevance",
  grounding: "Used your documents",
  voice_format: "Spoken format",
};
// Checks that are shown on transcripts but never scored stay off the
// Quality tab's pass-rate chart; context relevance has its own chart on the
// Knowledge base tab, beside faithfulness.
const UNSCORED = ["grounding", "voice_format", "context_relevance"];
const REFUSAL_LABEL = {
  concurrent_limit: "all lines busy",
  daily_limit: "daily limit",
  rate_limited: "too many attempts",
};

// `figure` is the headline for the range (a total or a peak), so the chart
// answers "how much" before the eye reads the line. Rates and latencies have
// no figure here: the summary cards above already carry the exact numbers.
// `info` is the one-line definition for a chart whose name is not enough.
// The expand control at the right of the title opens the same content in a
// dialog over the page, where TrendChart draws itself taller and as wide as
// the window (ExpandedContext). Dense charts (document references, pass
// rate per check) are read there; the panel stays the overview.
function Panel({ title, figure, info, children }) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef(null);
  const titleId = useId();
  useFocusTrap(boxRef, open);
  return (
    <div className="trend-panel">
      <div className="trend-panel-title">
        {title}
        {info && <InfoIcon>{info}</InfoIcon>}
        <button type="button" className="trend-expand" onClick={() => setOpen(true)} aria-label={`Expand ${title}`} title="Expand">
          <svg className="trend-ic" viewBox="0 0 24 24" aria-hidden="true">
            <polyline points="15 3 21 3 21 9" />
            <polyline points="9 21 3 21 3 15" />
            <line x1="21" y1="3" x2="14" y2="10" />
            <line x1="3" y1="21" x2="10" y2="14" />
          </svg>
        </button>
      </div>
      {figure != null && <div className="trend-figure">{figure}</div>}
      <PanelTitleContext.Provider value={title}>{children}</PanelTitleContext.Provider>
      {open && (
        <div
          className="dialog-veil"
          onMouseDown={(e) => {
            if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
          }}
        >
          <div
            className="dialog chart-dialog"
            ref={boxRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.stopPropagation();
                setOpen(false);
              }
            }}
          >
            <div className="trend-panel-title" id={titleId}>
              {title}
              {info && <InfoIcon>{info}</InfoIcon>}
              <button type="button" className="trend-expand" onClick={() => setOpen(false)} aria-label="Close" title="Close">
                <svg className="trend-ic" viewBox="0 0 24 24" aria-hidden="true">
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
            {figure != null && <div className="trend-figure">{figure}</div>}
            <PanelTitleContext.Provider value={title}>
              <ExpandedContext.Provider value={true}>{children}</ExpandedContext.Provider>
            </PanelTitleContext.Provider>
          </div>
        </div>
      )}
    </div>
  );
}

const sum = (arr) => arr.reduce((s, v) => s + (v || 0), 0);
const peak = (arr) => arr.reduce((m, v) => Math.max(m, v || 0), 0);
function fmtN(v) {
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e5) return `${Math.round(v / 1e3)}K`;
  return Math.round(v).toLocaleString();
}

function RankBars({ rows, label, value, format }) {
  if (!rows || rows.length === 0) return <p className="trend-empty">Nothing to rank yet</p>;
  const max = Math.max(1, ...rows.map(value));
  return (
    <div className="end-bars">
      {rows.map((r) => (
        <div className="end-row" key={label(r)}>
          <span className="end-name" title={label(r)}>{label(r)}</span>
          <span className="end-bar"><i style={{ width: `${(value(r) / max) * 100}%` }} /></span>
          <span className="end-n">{format(r)}</span>
        </div>
      ))}
    </div>
  );
}

// Which tabs have something to show for this window. Quality, Knowledge
// base and Tools disappear rather than open on an empty state; the rest
// always exist because every call feeds them.
export function tabsWithData(data, breakdown) {
  const s = data?.series;
  return {
    quality: !!s && s.quality.n.some((x) => x > 0),
    kb: !!s && (s.kb.lookups.some((x) => x > 0) || (breakdown?.documents?.knowledge_base_files || 0) > 0),
    tools: !!s && s.tools.calls.some((x) => x > 0),
  };
}

export default function Trends({ tab, data, onPick, breakdown }) {
  if (!data) return null;
  const { labels, series: s, ranking, bucket } = data;
  const pick = (tag) => (onPick ? (i) => onPick(i, tag) : undefined);
  const hasRefusals = Object.values(s.refusals).some((v) => v.some((x) => x > 0));

  const calls = (
    <Panel title="Calls" figure={`${fmtN(sum(s.calls))} calls`}>
      <TrendChart
        labels={labels} bucket={bucket} csvName="calls" onPick={pick(null)}
        series={[
          { key: "calls", label: "Calls", values: s.calls },
          { key: "text", label: "Text sessions", values: s.text_sessions },
          { key: "ne", label: "Non-engaged", values: s.non_engaged },
        ]}
        emptyText="Not enough calls for a trend yet"
      />
    </Panel>
  );
  const responseTime = (
    <Panel title="Response time" info="How long the caller waits for the agent to start answering: the typical answer (p50) and the slower tail (p90, p95).">
      <TrendChart labels={labels} bucket={bucket} csvName="response-time" unit="s" onPick={pick(null)}
        series={[
          { key: "p50", label: "Typical (p50)", values: s.latency.p50 },
          { key: "p90", label: "p90", values: s.latency.p90 },
          { key: "p95", label: "Worst (p95)", values: s.latency.p95 },
        ]} />
    </Panel>
  );
  const errorRate = (
    <Panel title="Error rate" info="Failed tool calls, knowledge-base errors and timeouts, failed evaluation runs, and calls that lost their connection, as a share of all answers, lookups and tool calls in the bucket.">
      <TrendChart labels={labels} bucket={bucket} csvName="error-rate" unit="%" height={140} onPick={pick(null)}
        series={[{ key: "er", label: "Error rate", values: s.errors.rate, tone: "bad" }]}
        emptyText="No calls or lookups in this window" zeroText="No errors in this window" />
    </Panel>
  );

  let body = null;
  switch (tab) {
    case "overview":
      body = (
        <>
          {calls}
          <div className="trend-pair">
            {responseTime}
            {errorRate}
          </div>
        </>
      );
      break;

    case "calls":
      body = (
        <>
          <div className="trend-pair">
            <Panel title="Talk time (minutes)" figure={`${fmtN(sum(s.talk_min))} min`}>
              <TrendChart labels={labels} bucket={bucket} csvName="talk-time" mode="bars" height={140}
                series={[{ key: "talk", label: "Minutes", values: s.talk_min }]} />
            </Panel>
            <Panel title={`Simultaneous calls${data.cap != null ? ` (cap ${data.cap})` : ""}`} figure={`peak ${peak(s.concurrency)}`}>
              <TrendChart labels={labels} bucket={bucket} csvName="concurrency" height={140}
                series={[{ key: "cc", label: "At once", values: s.concurrency }]} />
            </Panel>
          </div>
          <Panel title="How calls end" figure={`${fmtN(sum(Object.values(s.endings).flat()))} calls`}>
            <TrendChart labels={labels} bucket={bucket} csvName="endings" mode="bars" onPick={pick(null)}
              series={Object.entries(s.endings)
                .filter(([, v]) => v.some((x) => x > 0))
                .map(([k, v]) => ({ key: k, label: ENDING_LABEL[k] || k, values: v, tone: k === "connection_lost" ? "bad" : undefined }))} />
          </Panel>
          <div className="trend-pair">
            <Panel title="Calls with a cut-off reply" info="Share of voice calls where the caller spoke over the agent mid-reply. Some is normal; a high share means replies run too long.">
              <TrendChart labels={labels} bucket={bucket} csvName="interruptions" unit="%" height={140} onPick={pick("interrupted")}
                series={[{ key: "ir", label: "Share of voice calls", values: s.interruptions.rate }]}
                emptyText="No voice calls in this window" />
            </Panel>
            {hasRefusals && (
              <Panel title="Refused calls" figure={`${fmtN(sum(Object.values(s.refusals).flat()))} refused`} info="Visitors turned away by the simultaneous-calls cap, the daily cap, or the rate limiter.">
                <TrendChart labels={labels} bucket={bucket} csvName="refusals" mode="bars" height={140}
                  series={Object.entries(s.refusals).map(([k, v]) => ({ key: k, label: REFUSAL_LABEL[k] || k, values: v }))} />
              </Panel>
            )}
          </div>
        </>
      );
      break;

    case "speed":
      body = (
        <>
          {responseTime}
          <Panel title="Where the time goes" info="A typical answer split into its stages: Waiting (deciding the caller finished), Hearing (speech to text), Thinking (first word from the model) and Speaking (first audio).">
            <TrendChart labels={labels} bucket={bucket} csvName="stages" unit="s" mode="bars"
              series={[
                { key: "eou", label: "Waiting", values: s.stages.eou },
                { key: "stt", label: "Hearing", values: s.stages.stt },
                { key: "llm", label: "Thinking", values: s.stages.llm },
                { key: "tts", label: "Speaking", values: s.stages.tts },
              ]} />
          </Panel>
          {breakdown && (
            <div className="trend-pair">
              <Panel title="How fast the agent stops when interrupted">
                <InterruptionLatency data={breakdown} />
              </Panel>
              <Panel title="By model" info="Typical and worst time per stage, split by the model and provider that produced it. Run the same questions on two configurations and compare rows.">
                <ByModel data={breakdown} />
              </Panel>
            </div>
          )}
        </>
      );
      break;

    case "quality":
      body = (
        <>
          <Panel title="Quality score" info="The AI judge's score per call, drawn only for calls where every selected check ran. A hollow point on hover means fewer than 3 scored calls in that bucket.">
            <TrendChart labels={labels} bucket={bucket} csvName="quality" unit="%" onPick={pick("scored")}
              series={[{ key: "q", label: "Score (p50)", values: s.quality.p50, hollow: s.quality.n.map((n) => n > 0 && n < 3) }]} />
          </Panel>
          <div className="trend-pair">
            <Panel title="Pass rate per check">
              <TrendChart labels={labels} bucket={bucket} csvName="checks" unit="%" height={140}
                series={Object.entries(s.checks)
                  .filter(([k]) => !UNSCORED.includes(k))
                  .map(([k, v]) => ({ key: k, label: CHECK_LABEL[k] || k, values: v.pass_rate }))} />
            </Panel>
            <Panel title="Weakest checks first">
              <RankBars
                rows={ranking.checks}
                label={(r) => CHECK_LABEL[r.name] || r.name}
                value={(r) => r.pass_rate}
                format={(r) => `${Math.round(r.pass_rate * 100)}% · ${r.n}`} />
            </Panel>
          </div>
        </>
      );
      break;

    case "kb":
      body = (
        <>
          <div className="trend-pair">
            <Panel title="Lookups, errors and timeouts" figure={`${fmtN(sum(s.kb.lookups))} lookups`}>
              <TrendChart labels={labels} bucket={bucket} csvName="kb-lookups" mode="bars" height={140}
                series={[
                  { key: "ok", label: "Lookups", values: s.kb.lookups.map((v, i) => Math.max(0, v - s.kb.errors[i])) },
                  { key: "err", label: "Errors", values: s.kb.errors, tone: "bad" },
                ]} />
            </Panel>
            <Panel title="Match quality (median score)" info="How closely the best document chunk matched what the caller asked, 0 to 1, median per bucket.">
              <TrendChart labels={labels} bucket={bucket} csvName="kb-score" height={140} format={(v) => (v == null ? "–" : v.toFixed(2))}
                series={[{ key: "sc", label: "Median match", values: s.kb.med_score }]} />
            </Panel>
          </div>
          <div className="trend-pair">
            <Panel title="Lookup time (median)" info="How long a document lookup took, from the caller finishing to the excerpts being ready, median per bucket. This time sits inside the response time, so a slow lookup shows up as a slow agent.">
              <TrendChart labels={labels} bucket={bucket} csvName="kb-latency" unit="ms" height={140}
                series={[{ key: "lat", label: "Median lookup", values: s.kb.med_latency_ms }]} />
            </Panel>
            {s.checks.faithfulness && (
              <Panel title="Answers against the documents" info="Pass rate per bucket of the AI judge's two document checks, over calls with evaluation on: faithfulness (no claim about the business that the excerpts contradict or do not contain; the worst answer in a call counts) and context relevance (the excerpts the lookup brought back covered the question; averaged over the call).">
                <TrendChart labels={labels} bucket={bucket} csvName="doc-checks" unit="%" height={140}
                  series={[
                    { key: "faith", label: "Faithfulness", values: s.checks.faithfulness.pass_rate },
                    ...(s.checks.context_relevance
                      ? [{ key: "ctx", label: "Context relevance", values: s.checks.context_relevance.pass_rate }]
                      : []),
                  ]} />
              </Panel>
            )}
          </div>
          {breakdown && (
            <>
              <DocReferences data={breakdown} Panel={Panel} fmtN={fmtN} />
              <Panel title="Documents cited" info="Which documents answers actually drew on, and which were never cited in this window. A document that is never cited is either not asked about or not being found by lookups.">
                <Documents data={breakdown} />
              </Panel>
            </>
          )}
        </>
      );
      break;

    case "tools":
      body = (
        <div className="trend-pair">
          <Panel title="Tool calls and failures" figure={`${fmtN(sum(s.tools.calls))} calls`}>
            <TrendChart labels={labels} bucket={bucket} csvName="tools" mode="bars" height={140}
              series={[
                { key: "ok", label: "Succeeded", values: s.tools.calls.map((v, i) => Math.max(0, v - s.tools.failed[i])) },
                { key: "failed", label: "Failed", values: s.tools.failed, tone: "bad" },
              ]} />
          </Panel>
          <Panel title="Slowest tools (median)" info="Time inside each tool sits inside the response time, so a slow tool looks like a slow agent.">
            <RankBars
              rows={ranking.tools}
              label={(r) => r.name}
              value={(r) => r.p50_ms || 0}
              format={(r) => `${((r.p50_ms || 0) / 1000).toFixed(1)}s · ${r.calls}${r.failed ? ` (${r.failed} failed)` : ""}`} />
          </Panel>
        </div>
      );
      break;

    case "errors":
      body = (
        <>
          <div className="trend-pair">
            {errorRate}
            <Panel title="Errors by kind" figure={`${fmtN(sum(Object.values(s.errors.by).flat()))} errors`}>
              <TrendChart labels={labels} bucket={bucket} csvName="errors-by-kind" mode="bars" tone="bad" height={140}
                series={[
                  { key: "tools", label: "Tool failures", values: s.errors.by.tools },
                  { key: "kb", label: "Knowledge base", values: s.errors.by.kb },
                  { key: "evals", label: "Evaluation runs", values: s.errors.by.evals },
                  { key: "conn", label: "Connection lost", values: s.errors.by.connection },
                ]}
                emptyText="No errors in this window" />
            </Panel>
          </div>
          {breakdown && (
            <Panel title="Pipeline errors by stage" info="Errors inside the speech, model and voice stages. Fatal ones ended the turn; the rest were retried and recovered.">
              <PipelineErrors data={breakdown} />
            </Panel>
          )}
        </>
      );
      break;

    case "usage":
      body = (
        <div className="trend-pair">
          <Panel title="LLM tokens" figure={`${fmtN(sum(s.usage.tokens_in) + sum(s.usage.tokens_out))} tokens`} info="Prompt and completion tokens reported by the model per answer. The shape of the line is what catches a runaway prompt.">
            <TrendChart labels={labels} bucket={bucket} csvName="tokens" mode="bars" height={140}
              series={[
                { key: "in", label: "In", values: s.usage.tokens_in },
                { key: "out", label: "Out", values: s.usage.tokens_out },
              ]} />
          </Panel>
          <Panel title="Spoken audio (minutes)" figure={`${fmtN(sum(s.usage.tts_minutes))} min`}>
            <TrendChart labels={labels} bucket={bucket} csvName="tts-minutes" mode="bars" height={140}
              series={[{ key: "tts", label: "Minutes", values: s.usage.tts_minutes }]} />
          </Panel>
        </div>
      );
      break;

    default:
      body = null;
  }

  return <div className="trends">{body}</div>;
}
