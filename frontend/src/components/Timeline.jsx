import { useEffect, useState } from "react";

/* Per-session timeline (plan graph 19): every turn of a call as bars on
   one time axis, rebuilt from the stored stage metrics. Shown behind a
   "Timeline" toggle on the transcript view.

   Two lanes per turn. Lane 0 is the critical path the caller feels:
   Waiting (deciding they finished), Thinking (first token), Streaming
   (rest of the reply), Voice (first audio), Audio (the reply playing).
   Lane 1 holds what ran alongside: Hearing (speech to text, in parallel
   with Waiting) and tool calls. A reply the caller cut off gets a red
   end cap and a "cut off" mark.

   Long calls scroll horizontally at a fixed pixels-per-second so a
   2-second thinking span looks the same on a 30s and a 5-minute call.

   props
     fetch   () => Promise<timeline response>  (local or engine-proxied)
     ended   optional; when false, shows "call still in progress"        */

const PX_PER_S = 26;
const KINDS = [
  ["waiting", "Waiting"],
  ["hearing", "Hearing"],
  ["thinking", "Thinking"],
  ["streaming", "Streaming"],
  ["voice", "Voice"],
  ["audio", "Speaking"],
  ["tool", "Tool"],
];

function tickStep(total) {
  for (const s of [1, 2, 5, 10, 15, 30, 60, 120, 300]) {
    if (total / s <= 14) return s;
  }
  return 600;
}

function fmtT(s) {
  if (s >= 60) return `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}`;
  return `${s}s`;
}

export default function Timeline({ fetch }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let live = true;
    setErr("");
    fetch()
      .then((d) => live && setData(d))
      .catch((e) => live && setErr(`Couldn't load the timeline: ${e.message}`));
    return () => {
      live = false;
    };
  }, [fetch]);

  if (err) return <p className="trend-empty">{err}</p>;
  if (!data) return <div className="spin" />;
  return <TimelineView data={data} />;
}

// The drawing, kept pure (data in, markup out) so it can be rendered with a
// stored response without a browser or a network call.
export function TimelineView({ data }) {
  if (!data.has_data) {
    return (
      <p className="trend-empty">
        No stage timings were recorded for this call. Typed sessions have
        none, and calls made before timing storage existed are not
        rebuilt.
      </p>
    );
  }

  const total = Math.max(1, data.total_s);
  const width = Math.max(600, Math.round(total * PX_PER_S));
  const step = tickStep(total);
  const ticks = [];
  for (let t = 0; t <= total; t += step) ticks.push(t);
  const pct = (s) => `${((s / total) * 100).toFixed(3)}%`;

  return (
    <div className="tl">
      <div className="tl-legend">
        {KINDS.map(([k, label]) => (
          <span key={k} className="tl-key">
            <i className={`tl-span ${k}`} /> {label}
          </span>
        ))}
        <span className="tl-key">
          <i className="tl-span audio cut" /> cut off by the caller
        </span>
      </div>
      <div className="tl-scroll">
        <div className="tl-grid" style={{ width }}>
          <div className="tl-row tl-ruler">
            <div className="tl-label" />
            <div className="tl-track">
              {ticks.map((t) => (
                <span key={t} className="tl-tick" style={{ left: pct(t) }}>
                  {fmtT(t)}
                </span>
              ))}
            </div>
          </div>
          {data.turns.map((turn, i) => (
            <div className={`tl-row${turn.interrupted ? " cut" : ""}`} key={turn.speech_id}>
              <div className="tl-label">
                <div className="tl-n">Turn {i + 1}</div>
                {turn.user && <div className="tl-user" title={turn.user}>{turn.user}</div>}
                {turn.agent && <div className="tl-agent" title={turn.agent}>{turn.agent}</div>}
                {turn.interrupted && <span className="tmark cut">cut off</span>}
              </div>
              <div className="tl-track">
                {ticks.map((t) => (
                  <i key={t} className="tl-gridline" style={{ left: pct(t) }} />
                ))}
                {turn.spans.map((s, j) => (
                  <span
                    key={j}
                    className={`tl-span ${s.kind}${turn.interrupted && s.kind === "audio" ? " cut" : ""}`}
                    style={{
                      left: pct(s.start_s),
                      width: `max(2px, ${pct(s.end_s - s.start_s)})`,
                      top: s.lane === 0 ? 6 : 26,
                    }}
                    title={`${s.text} · ${s.start_s.toFixed(1)}s to ${s.end_s.toFixed(1)}s`}
                    role="img"
                    aria-label={s.text}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
      <p className="tl-note">
        Bars are rebuilt from the timing the worker recorded for each stage.
        Thinking can start before Waiting ends: the model begins on the
        interim transcript while the turn is still being confirmed.
      </p>
    </div>
  );
}
