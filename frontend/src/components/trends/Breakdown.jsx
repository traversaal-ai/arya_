import TrendChart from "./TrendChart.jsx";

/* Breakdown panels (Phase C): views fed by /api/insights/breakdown, placed
   by Trends.jsx on the Speed, Knowledge base and Errors tabs. Reuses
   TrendChart from Phase A.

   props
     data   the breakdown response (labels, bucket, models, documents,
            pipeline, interruption)                                      */

const STAGE_LABEL = {
  waiting: "Waiting",
  stt: "Hearing",
  thinking: "Thinking",
  speaking: "Speaking",
  tool: "Tool",
};
const ERR_STAGE = { stt: "Hearing (STT)", llm: "Thinking (LLM)", tts: "Speaking (TTS)", realtime: "Realtime", unknown: "Other" };

function fmtMs(ms) {
  if (ms == null) return "–";
  return ms >= 10000 ? `${Math.round(ms / 1000)}s` : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

export function ByModel({ data }) {
  if (!data || data.models.length === 0) {
    return <p className="trend-empty">No stage timings in this window.</p>;
  }
  return (
    <div className="trend-tablewrap">
      <table className="trend-table bym">
        <thead>
          <tr>
            <th>Stage</th>
            <th>Model</th>
            <th>Provider</th>
            <th>Turns</th>
            <th>Typical</th>
            <th>Worst</th>
          </tr>
        </thead>
        <tbody>
          {data.models.map((m, i) => (
            <tr key={i}>
              <td>{STAGE_LABEL[m.stage] || m.stage}</td>
              <td className="bym-model" title={m.model}>{m.model}</td>
              <td className="bym-prov" title={m.provider}>{m.provider}</td>
              <td>{m.n}</td>
              <td>{fmtMs(m.p50_ms)}</td>
              <td className="muted">{fmtMs(m.p95_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Documents({ data }) {
  if (!data) return null;
  const d = data.documents;
  if (d.knowledge_base_files === 0 && d.used.length === 0) {
    return <p className="trend-empty">No documents in the knowledge base for this selection.</p>;
  }
  const max = Math.max(1, ...d.used.map((u) => u.used));
  return (
    <div className="docs-break">
      {d.used.length === 0 ? (
        <p className="trend-empty">No answer cited a document in this window.</p>
      ) : (
        <div className="end-bars">
          {d.used.map((u) => (
            <div className="end-row" key={u.filename}>
              <span className="end-name" title={u.filename}>{u.filename}</span>
              <span className="end-bar"><i style={{ width: `${(u.used / max) * 100}%` }} /></span>
              <span className="end-n" title={`${u.used} citations across ${u.calls} calls${u.retrieved != null ? `, retrieved ${u.retrieved} times` : ""}`}>
                {u.used}{u.retrieved != null ? ` / ${u.retrieved}` : ""}
              </span>
            </div>
          ))}
        </div>
      )}
      {d.retrieved_known && d.used.length > 0 && (
        <p className="trend-empty">Numbers are "used / retrieved": how often an answer cited the document against how often a lookup pulled it in.</p>
      )}
      {d.never_used.length > 0 && (
        <p className="docs-never">
          Never cited in this window:{" "}
          {d.never_used.map((f) => (
            <span className="chip" key={f} title={f}>{f}</span>
          ))}
        </p>
      )}
    </div>
  );
}

/* Document references over time: one line per document for the most
   retrieved files, the rest folded into "Other" by the server. Rendered
   inside the caller's Panel so the headline total sits with the title. */
export function DocReferences({ data, Panel, fmtN }) {
  const refs = data?.documents?.references;
  if (!refs) return null;
  const series = refs.files.map((f) => ({ key: f.filename, label: f.filename, values: f.values }));
  if (refs.other) series.push({ key: "other", label: "Other documents", values: refs.other, neutral: true });
  const total = series.reduce((t, s) => t + s.values.reduce((a, b) => a + (b || 0), 0), 0);
  return (
    <Panel
      title="Document references"
      figure={data.documents.retrieved_known ? `${fmtN(total)} references` : undefined}
      info="How often each document was pulled into an answer. Counts every lookup that injected the document, whether or not the answer then used it."
    >
      {data.documents.retrieved_known ? (
        <TrendChart labels={data.labels} bucket={data.bucket} csvName="document-references" series={series}
          emptyText="No document was pulled into an answer in this window" />
      ) : (
        <p className="trend-empty">Which documents each lookup pulled in is recorded from this build on; older lookups have no file list.</p>
      )}
    </Panel>
  );
}

export function PipelineErrors({ data }) {
  if (!data) return null;
  const p = data.pipeline;
  const series = Object.entries(p.by_stage).map(([k, v]) => ({ key: k, label: ERR_STAGE[k] || k, values: v }));
  return (
    <>
      <TrendChart labels={data.labels} bucket={data.bucket} csvName="pipeline-errors" mode="bars" tone="bad" height={140}
        series={series} emptyText="No pipeline errors recorded in this window" />
      {p.total > 0 && (
        <p className="trend-empty">{p.total} error{p.total === 1 ? "" : "s"}, {p.fatal} ended a turn; the rest were retried and recovered.</p>
      )}
    </>
  );
}

export function InterruptionLatency({ data }) {
  if (!data) return null;
  const it = data.interruption;
  return (
    <>
      <TrendChart labels={data.labels} bucket={data.bucket} csvName="interruption-latency" unit="ms" height={140}
        series={[{ key: "il", label: "Time to stop (p50)", values: it.p50_ms }]}
        emptyText="No interruptions recorded in this window" />
      {it.count > 0 && (
        <p className="trend-empty">
          Across {it.count} interruption{it.count === 1 ? "" : "s"} the agent stopped within {fmtMs(it.overall_p50_ms)} typically, {fmtMs(it.overall_p95_ms)} at worst.
        </p>
      )}
    </>
  );
}
