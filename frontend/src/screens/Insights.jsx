import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  engineConvMessages,
  engineConvTimeline,
  engineInsights,
  engineInsightsBreakdown,
  engineInsightsSeries,
  getAgents,
  getInsights,
  getInsightsBreakdown,
  getInsightsSeries,
  listEngines,
} from "../api.js";
import ConvDetail, {
  endingLabel,
  fmtS,
  fmtWhen,
  evalScore,
  scoreClass,
} from "../components/ConvDetail.jsx";
import ErrorNote from "../components/ErrorNote.jsx";
import InfoIcon from "../components/InfoIcon.jsx";
import RangeControls from "../components/trends/RangeControls.jsx";
import { ErrorsStat } from "../components/trends/SignalsRow.jsx";
import Trends, { tabsWithData } from "../components/trends/Trends.jsx";

// What a chart click narrowed the Conversations table to, in words.
const TAG_LABEL = {
  text: "text sessions",
  non_engaged: "non-engaged calls",
  interrupted: "calls with a cut-off reply",
  scored: "scored calls",
  eval_failed: "calls where scoring failed",
};

/* Insights: "understand the agent" (History remains "read one call").
   Everything here is pre-aggregated by GET /api/insights/summary over data
   the stack already records — the screen renders numbers, it never
   computes them. Charts are tiny inline SVGs on purpose: ~3 small visuals
   don't justify a chart dependency, and stroke/fill ride the existing
   theme tokens so dark mode needs no extra work.

   The screen is split into tabs, each a handful of cards and charts, so
   the first view is the four numbers that matter and nothing scrolls for
   a mile. The call list is a tab of its own (Conversations) rather than a
   table under every chart tab: a chart click opens it narrowed to the
   clicked bucket, and the filter stays while the user moves between tabs.
   The open tab rides in the URL hash after "?" so a link to the Knowledge
   base tab opens on it. */

const TABS = [
  ["overview", "Overview"],
  ["calls", "Calls"],
  ["speed", "Speed"],
  ["quality", "Quality"],
  ["kb", "Knowledge base"],
  ["tools", "Tools"],
  ["errors", "Errors"],
  ["usage", "Usage"],
  ["conversations", "Conversations"],
];

function readTab() {
  const q = window.location.hash.split("?")[1];
  const t = q ? new URLSearchParams(q).get("tab") : null;
  return TABS.some(([k]) => k === t) ? t : "overview";
}

// replaceState: the tab is state within the screen, not a history entry
// the Back button should step through.
function writeTab(tab) {
  const path = window.location.hash.replace(/^#/, "").split("?")[0] || "/insights";
  window.history.replaceState(null, "", `#${path}${tab === "overview" ? "" : `?tab=${tab}`}`);
}

function Spark({ series }) {
  if (!series || series.length < 2) return null;
  const w = 120;
  const h = 30;
  const max = Math.max(1, ...series.map((p) => p.calls));
  const pts = series
    .map(
      (p, i) =>
        `${((i / (series.length - 1)) * (w - 4) + 2).toFixed(1)},${(
          h - 3 - (p.calls / max) * (h - 6)
        ).toFixed(1)}`
    )
    .join(" ");
  return (
    <svg
      className="spark"
      viewBox={`0 0 ${w} ${h}`}
      width={w}
      height={h}
      aria-hidden="true"
    >
      <polyline points={pts} fill="none" strokeWidth="1.5" />
    </svg>
  );
}

function Stat({ label, info, value, sub, children }) {
  return (
    <div className="stat">
      <div className="stat-label">
        {label}
        {info && <InfoIcon>{info}</InfoIcon>}
      </div>
      <div className="stat-num">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
      {children}
    </div>
  );
}

// One number and one line of context. The stage split lives on the Speed
// tab (Where the time goes, By model), not on the card.
function SpeedCard({ data }) {
  const l = data.latency;
  const notes = [];
  if (l.approximated > 0) {
    notes.push(
      `${l.approximated} answer${l.approximated === 1 ? "" : "s"} approximated from stage timings where the end-to-end time was not recorded`
    );
  }
  if (data.text_sessions > 0) {
    notes.push(
      `${data.text_sessions} text session${data.text_sessions === 1 ? "" : "s"} excluded, typed chats have no voice timings`
    );
  }
  return (
    <Stat
      label="Response speed"
      info={
        <>
          Time from the caller finishing to the agent starting to speak, for
          a typical answer. Worst-case is the slowest 5%.
          {notes.length > 0 && ` ${notes.join(". ")}.`}
        </>
      }
      value={l.p50 != null ? fmtS(l.p50) : "–"}
      sub={
        l.p95 != null
          ? `worst-case ${fmtS(l.p95)} · ${l.turns} answers`
          : data.text_sessions > 0
            ? "text sessions only, no voice timings"
            : "no timing data yet"
      }
    />
  );
}

// Horizontal rank bars inside a card (how calls end, tool time).
function BarsCard({ label, info, rows }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <div className="stat">
      <div className="stat-label">
        {label}
        {info && <InfoIcon>{info}</InfoIcon>}
      </div>
      <div className="end-bars">
        {rows.map((r) => (
          <div className="end-row" key={r.key}>
            <span className="end-name" title={r.title || r.name}>{r.name}</span>
            <span className="end-bar">
              <i style={{ width: `${(r.value / max) * 100}%` }} />
            </span>
            <span className="end-n">{r.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function fmtAvgDur(s) {
  if (!s) return "0s";
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

// Most informative first; "unknown" last because it is the residual, not
// a finding. Rows with a zero count are dropped, so a deployment that
// never hits its time cap simply has no time-limit row.
const ENDING_ORDER = [
  "user_ended",
  "inactivity",
  "time_limit",
  "connection_lost",
  "unknown",
];

// Local calendar date as YYYY-MM-DD ("sv" locale formats exactly that way).
function localDay(offsetDays = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toLocaleDateString("sv");
}

export default function Insights({ onBack, onOpenAgent, initialAgentId = "" }) {
  const [agents, setAgents] = useState([]);
  // Arriving with an agent in context (e.g. from its call window) starts
  // filtered to that agent — its numbers, not everything's.
  const [agentId, setAgentId] = useState(initialAgentId);
  // Inclusive local-day range; same day twice = just that day. The
  // browser's timezone offset rides with the request so the server maps
  // these to its UTC storage correctly.
  const [range, setRange] = useState(() => ({
    start: localDay(-6),
    end: localDay(0),
  }));
  // "" = this computer; an engine id = that engine's numbers via the local
  // backend's proxy. Never merged: test traffic and production traffic
  // answer different questions.
  const [engines, setEngines] = useState([]);
  const [source, setSource] = useState("");
  const [data, setData] = useState(null);
  const [selected, setSelected] = useState(null); // conversation row
  const [err, setErr] = useState("");
  // Trends: the same window as the summary, over time. Fetched separately
  // so a trends failure (an engine on an older build, say) never blanks the
  // summary cards, and a granularity change refetches only these two.
  const [bucket, setBucket] = useState("auto");
  const [trend, setTrend] = useState(null);
  const [breakdown, setBreakdown] = useState(null);
  const [trendErr, setTrendErr] = useState("");
  // A chart click narrows the Conversations tab to that slice.
  const [filter, setFilter] = useState(null);
  const [tab, setTab] = useState(readTab);
  const tabsRef = useRef(null);
  const agentSelId = useId();
  const daysSelId = useId();
  const endSelId = useId();
  const sourceSelId = useId();

  useEffect(() => {
    getAgents()
      .then((list) => setAgents(list || []))
      .catch(() => {}); // the filter degrades to "All agents" only
    // Agent ids survive a push unchanged, so the same agent filter works
    // against an engine too.
    listEngines().then(setEngines).catch(() => {});
  }, []);

  const load = useCallback(() => {
    setErr("");
    setData(null);
    (source
      ? engineInsights(source, agentId, range)
      : getInsights(agentId, range))
      .then(setData)
      .catch((e) => setErr(`Couldn't load insights: ${e.message}`));
  }, [agentId, range, source]);

  useEffect(() => {
    load();
  }, [load]);

  const loadTrends = useCallback(() => {
    setTrendErr("");
    setTrend(null);
    setBreakdown(null);
    setFilter(null);
    // Stale responses must not land on a newer selection: the closure flag
    // is flipped by the effect cleanup below.
    let live = true;
    (source
      ? engineInsightsSeries(source, agentId, range, bucket)
      : getInsightsSeries(agentId, range, bucket)
    )
      .then((t) => live && setTrend(t))
      .catch((e) => live && setTrendErr(`Couldn't load trends: ${e.message}`));
    (source
      ? engineInsightsBreakdown(source, agentId, range, bucket)
      : getInsightsBreakdown(agentId, range, bucket)
    )
      .then((b) => live && setBreakdown(b))
      .catch(() => live && setBreakdown(null)); // the panels just stay away
    return () => {
      live = false;
    };
  }, [agentId, range, source, bucket]);

  useEffect(() => loadTrends(), [loadTrends]);

  function selectTab(k) {
    setTab(k);
    writeTab(k);
  }

  // Chart click: open the Conversations tab narrowed to the calls in that
  // bucket, optionally to one tag (cut-off replies, scored calls). Labels
  // are caller-local wall-clock times and Date parses them as local, the
  // same clock the server bucketed with. The click may have happened far
  // down a chart tab, so the strip scrolls back into view.
  function pickBucket(i, tag) {
    if (!trend) return;
    const start = new Date(trend.labels[i]);
    const end = new Date(start);
    if (trend.bucket === "hour") end.setHours(end.getHours() + 1);
    else if (trend.bucket === "week") end.setDate(end.getDate() + 7);
    else end.setDate(end.getDate() + 1);
    setFilter({ start, end, tag: tag || null });
    selectTab("conversations");
    tabsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  if (selected) {
    return (
      <ConvDetail
        conv={selected}
        onBack={() => setSelected(null)}
        // Remote transcripts are read-only (no onDeleted = no delete
        // button): deleting production conversations belongs on the
        // engine, not one mis-click away in a viewer.
        fetchMessages={
          source ? (cid) => engineConvMessages(source, cid) : undefined
        }
        fetchTimeline={
          source ? (cid) => engineConvTimeline(source, cid) : undefined
        }
        onOpenAgent={onOpenAgent}
        onDeleted={
          source
            ? undefined
            : () => {
                setSelected(null);
                load();
                loadTrends();
              }
        }
      />
    );
  }

  // Any reason this build does not recognize folds into "unknown": a
  // silently dropped bucket would make the rows stop summing to the call
  // count, so the card would under-report by omission.
  const endings = (() => {
    if (!data) return [];
    const counts = {};
    let extra = 0;
    for (const [k, n] of Object.entries(data.endings || {})) {
      if (ENDING_ORDER.includes(k)) counts[k] = (counts[k] || 0) + n;
      else extra += n;
    }
    if (extra) counts.unknown = (counts.unknown || 0) + extra;
    return ENDING_ORDER.map((k) => [k, counts[k] || 0]).filter(([, n]) => n);
  })();
  // The table, narrowed by a chart click: calls whose start falls in the
  // clicked bucket, and carrying the tag when one was given. Tags come from
  // the series response, keyed by conversation id.
  const visibleConvs = !data
    ? []
    : filter
      ? data.conversations.filter((c) => {
          const t = new Date(c.started_at);
          return (
            t >= filter.start &&
            t < filter.end &&
            (!filter.tag || (trend?.flags?.[c.id] || []).includes(filter.tag))
          );
        })
      : data.conversations;

  // Tabs with nothing behind them for this window stay out of the strip.
  // Until the series has loaded every tab shows, so the strip does not
  // jump as the answers land.
  const has = tabsWithData(trend, breakdown);
  const visibleTabs = TABS.filter(([k]) => !trend || !(k in has) || has[k]);
  const tabOn = visibleTabs.some(([k]) => k === tab) ? tab : "overview";

  // The cards of the open tab. Overview is the four headline numbers; the
  // other tabs carry only the cards that belong to their question, and
  // Conversations has none.
  const cards = [];
  if (data && data.calls > 0) {
    const refusals = data.refusals && data.refusals.total > 0 && (
      <Stat
        key="refusals"
        label="Turned away"
        info={`Visitors who tried to call and were refused. "All lines busy" and "daily limit" mean your caps on Share and embed were full; raise them if this keeps happening. "Too many attempts" is the abuse guard slowing down a single source of rapid-fire attempts.`}
        value={data.refusals.total}
        sub={
          [
            ["concurrent_limit", "all lines busy"],
            ["daily_limit", "daily limit"],
            ["rate_limited", "too many attempts"],
          ]
            .filter(([k]) => data.refusals.by_reason[k])
            .map(([k, label]) => `${data.refusals.by_reason[k]} ${label}`)
            .join(" · ") || "refused calls"
        }
      />
    );
    const interrupted = data.interruptions && data.interruptions.voice_calls > 0 && (
      <Stat
        key="interrupted"
        label="Interrupted"
        info="How often callers cut the agent off mid-reply. Some is normal (the caller got what they needed). A high share on a support agent means replies run too long. Near zero alongside many calls that ended after silence means callers never get a word in."
        value={`${Math.round(data.interruptions.share * 100)}%`}
        sub={`of ${data.interruptions.voice_calls} voice call${data.interruptions.voice_calls === 1 ? "" : "s"} had a cut-off reply · ${data.interruptions.total} in total`}
      />
    );
    switch (tabOn) {
      case "overview":
        cards.push(
          <Stat
            key="calls"
            label="Calls"
            value={data.calls + (data.capped ? "+" : "")}
            sub={
              (range.start === range.end
                ? range.start
                : `${range.start} to ${range.end}`) +
              // Greeting-only and one-word calls, so a flat average is
              // not read as a quiet agent. Counted from the series.
              (trend && trend.series.non_engaged.some((n) => n > 0)
                ? ` · ${trend.series.non_engaged.reduce((a, b) => a + b, 0)} non-engaged`
                : "")
            }
          >
            <Spark series={data.series} />
          </Stat>,
          <Stat
            key="avg"
            label="Avg call"
            value={fmtAvgDur(data.avg_duration_s)}
            sub={`${data.avg_turns} turns per call`}
          />,
          <SpeedCard key="speed" data={data} />,
          <ErrorsStat key="errors" trend={trend} />
        );
        break;
      case "calls":
        if (endings.length > 0) {
          cards.push(
            <BarsCard
              key="endings"
              label="How calls end"
              info="Caller ended: pressed End or closed the tab. Ended after silence: nobody spoke for 3 minutes. Hit the time limit: reached the maximum call length. Connection lost: dropped without a goodbye. Unknown: nothing was recorded (older calls, server restarts)."
              rows={endings.map(([k, n]) => ({ key: k, name: endingLabel(k), value: n, text: n }))}
            />
          );
        }
        if (interrupted) cards.push(interrupted);
        if (refusals) cards.push(refusals);
        break;
      case "speed":
        cards.push(<SpeedCard key="speed" data={data} />);
        if (data.cold_start?.penalty_s > 0.3) {
          cards.push(
            <Stat
              key="cold"
              label="First answer"
              info="How much slower the first answer of a call is than the rest. Model load and warm-up show up here."
              value={`+${fmtS(data.cold_start.penalty_s)}`}
              sub="slower than the other answers"
            />
          );
        }
        if (data.speaking_rate) {
          cards.push(
            <Stat
              key="rate"
              label="Speaking rate"
              value={`${data.speaking_rate} chars/s`}
              sub="characters spoken per second of audio"
            />
          );
        }
        break;
      case "kb":
        if (data.grounding) {
          const g = data.grounding;
          cards.push(
            <Stat
              key="grounding"
              label="Document usage"
              info="Of the answers that received document context, the share that drew on it. Read it as whether the knowledge base is being used, not as answer quality: a correct answer phrased from the prompt or web search counts as not used. The main number is the word-overlap check, which runs on every call. The judge line, when evaluation is on, is the AI judge's reading of the answers it saw together with their excerpts, which also catches paraphrase."
              value={g.rate != null ? `${Math.round(g.rate * 100)}%` : "–"}
              sub={
                `${g.used} of ${g.injected} answers drew on your documents` +
                (g.judged ? ` · judge: ${Math.round(g.judged.rate * 100)}% of ${g.judged.answers}` : "")
              }
            />
          );
        }
        if (data.doc_checks) {
          const f = data.doc_checks.faithfulness;
          const c = data.doc_checks.context_relevance;
          cards.push(
            <Stat
              key="faithfulness"
              label="Faithfulness"
              info="Of the judged answers that received document context, the share where every claim about the business (prices, hours, policies, products) was supported by the excerpts or by a tool result. A fail is an answer that stated something the documents contradict or do not contain. Judged per answer by the AI judge, in the same call as accuracy, so it costs nothing extra."
              value={`${Math.round(f.rate * 100)}%`}
              sub={`${f.pass} of ${f.answers} answers stuck to the documents`}
            />
          );
          if (c.rate != null) {
            cards.push(
              <Stat
                key="context"
                label="Context relevance"
                info="Of the same answers, the share where the excerpts the lookup brought back contained what was needed to answer the question. A low number points at retrieval or the documents, not at the agent, so it is shown on each transcript but never counted in the quality score."
                value={`${Math.round(c.rate * 100)}%`}
                sub={`${c.pass} of ${c.answers} lookups fit the question`}
              />
            );
          }
        }
        if (data.rag) {
          cards.push(
            <Stat
              key="kb"
              label="Knowledge base"
              info="How closely document lookups matched what callers asked, 0 to 1, median over the window."
              value={data.rag.med_top_score != null ? data.rag.med_top_score.toFixed(2) : "–"}
              sub={`median match · ${data.rag.retrievals} lookups · ${data.rag.timeouts} timeout${data.rag.timeouts === 1 ? "" : "s"}${
                data.rag.med_latency_ms != null ? ` · ${data.rag.med_latency_ms}ms` : ""
              }`}
            />
          );
        }
        break;
      case "tools":
        if (data.tools && data.tools.length > 0) {
          cards.push(
            <BarsCard
              key="tools"
              label="Tool calls"
              info="Time spent inside each tool (search, bookings, connectors), median per call. This time sits inside the response speed, so a slow tool shows up as a slow agent."
              rows={data.tools.map((t) => ({
                key: t.name,
                name: t.name,
                title: `${t.calls} call${t.calls === 1 ? "" : "s"}${t.failed ? `, ${t.failed} failed` : ""}`,
                value: t.p50_ms || 0,
                text: fmtS((t.p50_ms || 0) / 1000),
              }))}
            />
          );
        }
        break;
      case "errors":
        cards.push(<ErrorsStat key="errors" trend={trend} />);
        break;
      default:
        break;
    }
  }

  return (
    <div>
      <h1 className="h-serif">
        Agent <em>dashboard</em>
      </h1>

      <div className="ins-tabs" role="tablist" aria-label="Dashboard sections" ref={tabsRef}>
        {visibleTabs.map(([k, label]) => (
          <button
            key={k}
            type="button"
            role="tab"
            className={`ins-tab${tabOn === k ? " on" : ""}`}
            aria-selected={tabOn === k}
            onClick={() => selectTab(k)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="ins-filters">
        {engines.length > 0 && (
          <>
            <label className="label" htmlFor={sourceSelId}>
              Source
            </label>
            <select
              id={sourceSelId}
              className="select slim"
              value={source}
              onChange={(e) => setSource(e.target.value)}
            >
              <option value="">Local</option>
              {engines.map((e) => (
                <option key={e.id} value={e.id}>
                  {e.name}
                </option>
              ))}
            </select>
          </>
        )}
        <label className="label" htmlFor={agentSelId}>
          Agent
        </label>
        <select
          id={agentSelId}
          className="select slim"
          value={agentId}
          onChange={(e) => setAgentId(e.target.value)}
        >
          <option value="">All agents</option>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
        <label className="label" htmlFor={daysSelId}>
          From
        </label>
        <input
          id={daysSelId}
          className="select slim"
          type="date"
          value={range.start}
          max={range.end}
          onChange={(e) =>
            e.target.value && setRange((r) => ({ ...r, start: e.target.value }))
          }
        />
        <label className="label" htmlFor={endSelId}>
          To
        </label>
        <input
          id={endSelId}
          className="select slim"
          type="date"
          value={range.end}
          min={range.start}
          onChange={(e) =>
            e.target.value && setRange((r) => ({ ...r, end: e.target.value }))
          }
        />
      </div>
      <RangeControls
        range={range}
        onRange={setRange}
        bucket={bucket}
        onBucket={setBucket}
      />

      {err && <ErrorNote message={err} onRetry={load} />}
      {!data && !err && <div className="spin" />}

      {data && data.calls === 0 && (
        <div className="note">
          <b>No calls in this window yet.</b> Make a few test calls. Every
          one is measured automatically (speed, sources used, how it ended)
          and this screen fills in. No setup needed.
        </div>
      )}

      {data && data.calls > 0 && tabOn !== "conversations" && (
        <>
          {cards.length > 0 && <div className="stat-grid">{cards}</div>}

          {trendErr && (
            <p className="trend-empty">
              {trendErr}
              {source ? " The engine may need an update (git pull, then rebuild)." : ""}
            </p>
          )}
          {!trend && !trendErr && <div className="spin" />}
          <Trends tab={tabOn} data={trend} breakdown={breakdown} onPick={pickBucket} />
        </>
      )}

      {data && data.calls > 0 && tabOn === "conversations" && (
        <>
          {/* One line above the list: what it holds, or what a chart click
              narrowed it to and the way back to everything. */}
          <p className="trend-filter">
            {filter ? (
              <>
                Showing {visibleConvs.length} call{visibleConvs.length === 1 ? "" : "s"} from{" "}
                {trend && trend.bucket === "hour"
                  ? fmtWhen(filter.start)
                  : filter.start.toLocaleDateString([], { month: "short", day: "numeric" })}
                {trend && trend.bucket === "week" ? " (week)" : ""}
                {filter.tag ? ` · ${TAG_LABEL[filter.tag] || filter.tag}` : ""}
                <button type="button" className="chip chip-btn" onClick={() => setFilter(null)}>
                  Clear filter
                </button>
              </>
            ) : (
              <>
                {data.conversations.length} call{data.conversations.length === 1 ? "" : "s"}
                {data.capped ? ", the most recent in this window" : " in this window"}
                {" · "}click a row to read it
              </>
            )}
          </p>
          {filter && visibleConvs.length === 0 && (
            <p className="trend-empty">
              No calls match. Older calls beyond the 500 most recent in this window are not listed.
            </p>
          )}
          <div className="itable-wrap">
            <div className="itable" role="table" aria-label="Conversations">
              <div className="itable-head" role="row">
                <span role="columnheader">When</span>
                <span role="columnheader">Agent</span>
                <span role="columnheader">Length</span>
                <span role="columnheader">Turns</span>
                <span role="columnheader">Response</span>
                <span role="columnheader">Sourced</span>
                <span role="columnheader">Quality</span>
              </div>
              {visibleConvs.map((c) => (
                <button
                  className="itable-row"
                  role="row"
                  key={c.id}
                  onClick={() => setSelected(c)}
                >
                  <span role="cell">{fmtWhen(c.started_at)}</span>
                  <span role="cell" className="itable-agent">
                    {c.agent_name || c.room_name}
                  </span>
                  <span role="cell">{fmtAvgDur(c.duration_s)}</span>
                  <span role="cell">{c.turns}</span>
                  <span role="cell">
                    {c.med_latency_s != null ? fmtS(c.med_latency_s) : "–"}
                  </span>
                  <span role="cell">{c.cited_turns || "–"}</span>
                  <span role="cell">
                    {evalScore(c.evals) != null ? (
                      <span className={scoreClass(c.evals.score)}>
                        {Math.round(c.evals.score * 100)}%
                      </span>
                    ) : (
                      "–"
                    )}
                  </span>
                </button>
              ))}
            </div>
          </div>
        </>
      )}

      <div className="btn-row">
        <button className="btn ghost" onClick={onBack}>
          Back
        </button>
      </div>
    </div>
  );
}
