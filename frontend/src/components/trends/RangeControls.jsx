import { useId } from "react";

/* Date range presets and granularity (plan graph 29).

   Sits in the Insights controls row next to the existing date inputs.
   `range` is the screen's {start, end} (YYYY-MM-DD, local days, both
   inclusive); `bucket` is "auto" | "hour" | "day". Presets just set the
   range; the Insights screen's existing date inputs keep working for
   custom spans. Prev/next shift the whole window by its own length.      */

function localDay(offsetDays = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toLocaleDateString("sv");
}

function shift(range, dir) {
  const a = new Date(range.start);
  const b = new Date(range.end);
  const len = Math.round((b - a) / 86400000) + 1;
  a.setDate(a.getDate() + dir * len);
  b.setDate(b.getDate() + dir * len);
  return { start: a.toLocaleDateString("sv"), end: b.toLocaleDateString("sv") };
}

export function spanDays(range) {
  return Math.round((new Date(range.end) - new Date(range.start)) / 86400000) + 1;
}

export function effectiveBucket(range, bucket) {
  if (bucket === "hour" || bucket === "day" || bucket === "week") return bucket;
  return spanDays(range) <= 2 ? "hour" : "day";
}

const PRESETS = [
  ["today", "Today", 0],
  ["7d", "Last 7 days", 6],
  ["30d", "Last 30 days", 29],
  ["90d", "Last 90 days", 89],
];

export default function RangeControls({ range, onRange, bucket, onBucket }) {
  const selId = useId();
  const active = PRESETS.find(([, , back]) => range.start === localDay(-back) && range.end === localDay(0))?.[0];
  const future = range.end >= localDay(0);
  return (
    <div className="range-controls">
      <div className="range-presets" role="group" aria-label="Date range presets">
        {PRESETS.map(([key, label, back]) => (
          <button
            key={key}
            type="button"
            className={active === key ? "chip chip-btn on" : "chip chip-btn"}
            aria-pressed={active === key}
            onClick={() => onRange({ start: localDay(-back), end: localDay(0) })}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="range-step" role="group" aria-label="Shift the window">
        <button type="button" className="icon-btn" title="Previous period" onClick={() => onRange(shift(range, -1))}>
          ‹
        </button>
        <button type="button" className="icon-btn" title="Next period" disabled={future} onClick={() => onRange(shift(range, 1))}>
          ›
        </button>
      </div>
      <label className="label" htmlFor={selId}>Granularity</label>
      <select id={selId} className="select slim" value={bucket} onChange={(e) => onBucket(e.target.value)}>
        <option value="auto">Auto ({effectiveBucket(range, "auto") === "hour" ? "hourly" : "daily"})</option>
        <option value="hour">Hourly</option>
        <option value="day">Daily</option>
        <option value="week">Weekly (Mon to Sun)</option>
      </select>
    </div>
  );
}
