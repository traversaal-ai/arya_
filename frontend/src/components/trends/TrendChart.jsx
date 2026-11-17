import { createContext, useContext, useEffect, useId, useMemo, useRef, useState } from "react";

/* TrendChart: the one chart component behind every Insights trend.

   Views (the icon switch in the tool row)
     line    one or more series as 2px lines, colour tells them apart
     bars    one bar per bucket. Stacked when the chart was authored as
             bars (its series are parts of a whole: endings, errors,
             volumes); side by side when it was authored as a line (its
             series are independent measures: a pass rate per check).
             "area" is accepted as an alias for bars
     totals  one horizontal bar per series, summed over the window.
             Offered for counts only: a sum of rates or latencies means
             nothing
     table   the same numbers as a plain table
   `mode` sets the opening view; the reader can switch at any time.

   Axis: linear, or log for counts and durations in the line and
   side-by-side views (a Linear | Log switch next to the view switch). A
   percentage is bounded and a stack stops adding up on a log axis, so
   neither offers it. On the log axis the ticks are decades, zero and
   "no data" sit on the baseline (labelled 0), and the smallest reading
   sits a little above it so a bar of 1 still has height.

   Drawn in real pixels. The component measures its container and sets the
   svg viewBox to that exact width, so nothing is stretched at any window
   size or browser zoom: text, dots and strokes keep their true size and
   the tick count adapts to the room available. Inside an expanded panel
   (ExpandedContext, see Trends.jsx) the chart is drawn taller.

   Colours come from the --chart-N tokens in styles.css: a fixed categorical
   order (blue, orange, teal, violet, pink, gold) assigned by series position,
   never cycled, so the same series keeps its colour when a neighbour is
   filtered out. A series with tone "bad" takes --chart-bad (the error red);
   a chart with tone "bad" paints its only series red, while a multi-series
   error chart keeps identity colours (the panel title already says these
   are errors). neutral: true gives the grey for an "Other" remainder. The
   legend swatch and tooltip key share the mark colour through --mark.

   Layout: one band per bucket. Lines, dots and bars all sit on the band
   centre, so the first and last bucket never touch the axis edge and a
   bar chart and a line chart over the same range line up.

   Hover and keyboard (arrow keys) move a focused bucket whose values show
   in a tooltip; the only dots on a line are the hovered ones (a sparkline
   keeps a dot on its last point). Tooltip rows are sorted by value, so the
   card reads as a ranking; a single series drops its name when the panel
   title already says it (PanelTitleContext, set by Panel in Trends.jsx).
   A bucket with nothing to measure still answers hover: its dot sits
   hollow on the baseline and the tooltip says "no data" per series, or
   one line "No data for this day" when no series has a reading, so a
   real 0% and an unmeasured day never look the same. Clicking a bucket
   with data calls onPick(index) so the screen can filter the
   conversations table to that slice. CSV downloads the numbers.

   Honest empties: no data at all shows the empty state. All-zero stacks
   show it too. A rate that is zero in every bucket draws a flat 0% line
   (or zeroText when given). In the line view a bucket with no value is
   drawn at the baseline so the line stays continuous; the bars view
   simply leaves it out.

   props
     labels      ISO strings, one per bucket (shared x axis)
     series      [{ key, label, values, tone?: "bad", neutral?: bool, hollow?: bool[] }]
     mode        "line" | "bars" ("area" is treated as "bars"): the opening view
     unit        "s" | "ms" | "%" | ""
     format      optional (v) => string, used for ticks and tooltip
     height      px including the axis band, default 160
     sparkline   bool: 30px, no axes, no tooltip, no switch
     emptyText   string
     zeroText    string shown instead of a flat zero line
     tone        "bad" for the error family
     onPick      (bucketIndex) => void
     csvName     base filename for the CSV download
     bucket      "hour" | "day" | "week" (label formatting)               */

export const ExpandedContext = createContext(false);
export const PanelTitleContext = createContext("");

const CATEGORICAL = 6; // --chart-1 .. --chart-6; anything past that is --chart-neutral
const EXPANDED_HEIGHT = 360;
const TICK_FONT = 11;
const CHAR_W = 6.3; // average tick glyph width at TICK_FONT

function parseDate(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtFull(iso, bucket) {
  const d = parseDate(iso);
  if (!d) return iso;
  if (bucket === "week") return `Week of ${fmtDay(iso)}`;
  return bucket === "hour"
    ? d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function fmtDay(iso) {
  const d = parseDate(iso);
  return d ? d.toLocaleDateString([], { month: "short", day: "numeric" }) : iso;
}

function fmtHour(iso) {
  const d = parseDate(iso);
  return d ? d.toLocaleTimeString([], { hour: "numeric" }) : iso;
}

function trimNum(v, dec) {
  return Number(v.toFixed(dec)).toString();
}

function compact(v, dec) {
  const a = Math.abs(v);
  if (a >= 1e6) return `${trimNum(v / 1e6, 1)}M`;
  if (a >= 1e3) return `${trimNum(v / 1e3, a >= 1e4 ? 0 : 1)}K`;
  return trimNum(v, dec);
}

// Value formatter. `step` is the tick step, so axis labels use only as many
// decimals as the step needs (0.25s steps read 0.25s, never 0.3s).
function makeFormat(unit, format) {
  return (v, step = 0) => {
    if (v == null) return "–";
    if (format) return format(v);
    const dec = (s) => (s > 0 && s < 1 ? Math.min(3, Math.ceil(-Math.log10(s) - 1e-9)) : 0);
    if (unit === "%") {
      const ps = step * 100;
      return `${trimNum(v * 100, step ? dec(ps) : v * 100 < 10 && v * 100 % 1 ? 1 : 0)}%`;
    }
    if (unit === "ms") return `${Math.round(v).toLocaleString()}ms`;
    if (unit === "s") return `${trimNum(v, step ? dec(step) : v >= 10 ? 0 : 1)}s`;
    return compact(v, step ? dec(step) : Number.isInteger(v) ? 0 : 1);
  };
}

// Round axis: ~count ticks, step from {1, 2, 5} x 10^n, top at or above max.
// `pos` maps a value to 0..1 of the plot height.
function niceScale(max, count) {
  if (!(max > 0)) return { top: 1, step: 0.25, ticks: [0, 0.25, 0.5, 0.75, 1], pos: (v) => v };
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const top = Math.ceil(max / step - 1e-9) * step;
  const ticks = [];
  for (let v = 0; v <= top + step / 2; v += step) ticks.push(Number(v.toFixed(10)));
  return { top, step, ticks, pos: (v) => Math.max(0, Math.min(v, top)) / top };
}

// Log axis: decade ticks from the smallest reading's decade to the one at
// or above the largest. The floor sits half a decade under the smallest
// decade so the smallest reading still has height; zero (and "no data")
// stays on the baseline, which is labelled 0.
function logScale(minPos, max) {
  const lo = 10 ** Math.floor(Math.log10(minPos));
  let top = 10 ** Math.ceil(Math.log10(max) - 1e-9);
  if (top <= lo) top = lo * 10;
  const floor = lo / 2;
  const span = Math.log10(top) - Math.log10(floor);
  const ticks = [0];
  for (let d = lo; d <= top * 1.0001; d *= 10) ticks.push(Number(d.toPrecision(12)));
  const pos = (v) => (v > 0 ? Math.max(0, Math.min(1, (Math.log10(v) - Math.log10(floor)) / span)) : 0);
  return { top, step: lo, ticks, pos, log: true };
}

function downloadCsv(name, labels, series) {
  const head = ["bucket", ...series.map((s) => s.label)].join(",");
  const rows = labels.map((l, i) =>
    [l, ...series.map((s) => (s.values[i] == null ? "" : s.values[i]))].join(",")
  );
  const blob = new Blob([[head, ...rows].join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${name || "series"}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Top corners rounded, bottom square (bars grow from a single baseline).
function topRounded(x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, w / 2, h));
  if (rr === 0) return `M${x},${y}h${w}v${h}h${-w}Z`;
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}

// The view switch glyphs (Feather geometry, like the rest of the icon set).
const VIEW_ICON = {
  line: <polyline points="3 17 9 10 13 14 21 6" />,
  bars: (
    <>
      <rect x="4" y="10" width="4" height="10" rx="1" />
      <rect x="10" y="4" width="4" height="16" rx="1" />
      <rect x="16" y="13" width="4" height="7" rx="1" />
    </>
  ),
  totals: (
    <>
      <line x1="4" y1="6" x2="20" y2="6" />
      <line x1="4" y1="12" x2="14" y2="12" />
      <line x1="4" y1="18" x2="9" y2="18" />
    </>
  ),
  table: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <line x1="3" y1="10" x2="21" y2="10" />
      <line x1="9" y1="10" x2="9" y2="20" />
    </>
  ),
};
const VIEW_LABEL = { line: "Line", bars: "Bars", totals: "Totals", table: "Table" };

export default function TrendChart({
  labels = [],
  series = [],
  mode = "line",
  unit = "",
  format,
  height = 160,
  sparkline = false,
  emptyText = "Not enough data for a trend",
  zeroText,
  tone,
  onPick,
  csvName,
  bucket = "day",
}) {
  const authoredBars = mode !== "line";
  const [hover, setHover] = useState(null);
  const [view, setView] = useState(authoredBars ? "bars" : "line");
  const [axis, setAxis] = useState("linear");
  const [w, setW] = useState(600);
  const plotRef = useRef(null);
  const id = useId();
  const expanded = useContext(ExpandedContext);
  const panelTitle = useContext(PanelTitleContext);
  const bars = view === "bars";
  const stacked = bars && authoredBars;
  const views = unit === "" ? ["line", "bars", "totals", "table"] : ["line", "bars", "table"];
  const logAllowed = !sparkline && unit !== "%" && !stacked && (view === "line" || view === "bars");
  const log = axis === "log" && logAllowed;
  const fmt = useMemo(() => makeFormat(unit, format), [unit, format]);
  const H = sparkline ? 30 : expanded ? EXPANDED_HEIGHT : height;
  const n = labels.length;

  const model = useMemo(() => {
    const stack = (i) => series.reduce((s, ser) => s + (ser.values[i] || 0), 0);
    const top = (i) =>
      stacked
        ? stack(i)
        : Math.max(...series.map((ser) => ser.values[i] ?? -Infinity));
    const present = labels.map((_, i) => top(i)).filter((v) => Number.isFinite(v));
    const hasAny = labels.some((_, i) => series.some((s) => s.values[i] != null));
    const nonZero = present.some((v) => v > 0);
    const dataMax = present.length ? Math.max(...present) : 0;
    const positives = series.flatMap((s) => s.values.filter((v) => v > 0));
    const dataMin = positives.length ? Math.min(...positives) : 0;

    const padT = sparkline ? 3 : 10;
    const padB = sparkline ? 3 : 24;
    const innerH = Math.max(20, H - padT - padB);
    const count = Math.max(2, Math.min(expanded ? 8 : 5, Math.floor(innerH / 34)));
    const pct = { top: 1, step: 0.25, ticks: [0, 0.25, 0.5, 0.75, 1], pos: (v) => Math.max(0, Math.min(v, 1)) };
    let scale;
    if (unit === "%" && (dataMax > 0.5 || dataMax === 0)) scale = pct;
    else if (log && dataMin > 0) scale = logScale(dataMin, dataMax);
    else scale = niceScale(dataMax, count);

    const tickText = sparkline ? [] : scale.ticks.map((t) => fmt(t, scale.log ? t || scale.step : scale.step));
    const widest = tickText.reduce((m, t) => Math.max(m, t.length), 1);
    const padL = sparkline ? 4 : Math.max(30, Math.ceil(widest * CHAR_W) + 14);
    const padR = sparkline ? 4 : 12;
    const innerW = Math.max(40, w - padL - padR);
    const slot = innerW / Math.max(1, n);
    const x = sparkline && n > 1 ? (i) => padL + 2 + (i / (n - 1)) * (innerW - 4) : (i) => padL + slot * (i + 0.5);
    const y = (v) => padT + innerH - scale.pos(v) * innerH;
    return { scale, tickText, padL, padR, padT, padB, innerH, innerW, slot, x, y, hasAny, nonZero };
  }, [labels, series, stacked, unit, fmt, H, w, n, sparkline, expanded, log]);

  // Track the real width. Fires on window resize, browser zoom and layout
  // changes (a sidebar opening), so the chart is always drawn 1:1.
  useEffect(() => {
    const el = plotRef.current;
    if (!el) return undefined;
    const read = () => {
      const next = Math.round(el.getBoundingClientRect().width);
      if (next > 0) setW((prev) => (prev === next ? prev : next));
    };
    read();
    if (typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [view, n, model.hasAny, model.nonZero]);

  const { scale, tickText, padL, padR, padT, padB, innerH, innerW, slot, x, y } = model;

  const sparkPoints = sparkline ? series[0]?.values.filter((v) => v != null).length || 0 : 0;
  const empty = n === 0 || !model.hasAny || (stacked && !model.nonZero) || (sparkline && sparkPoints < 2);
  const none = (text) => (
    <div className="trend-none" style={{ minHeight: H }} role="img" aria-label={text}>
      <svg viewBox="0 0 28 22" aria-hidden="true">
        <rect x="2" y="10" width="6" height="12" rx="1.5" />
        <rect x="11" y="4" width="6" height="18" rx="1.5" />
        <rect x="20" y="13" width="6" height="9" rx="1.5" />
      </svg>
      <span>{text}</span>
    </div>
  );
  if (empty) return sparkline ? null : none(emptyText);
  if (!sparkline && zeroText && !model.nonZero && view !== "table") return none(zeroText);

  // Colour per series: categorical by position among the identity series,
  // red for failures, grey for an "Other" remainder.
  let k = 0;
  const colors = series.map((s) => {
    if (s.tone === "bad" || (tone === "bad" && series.length === 1)) return "var(--chart-bad)";
    if (s.neutral) return "var(--chart-neutral)";
    k += 1;
    return k <= CATEGORICAL ? `var(--chart-${k})` : "var(--chart-neutral)";
  });

  // X ticks: as many labels as fit, evenly spaced, each with room around it.
  const labelW = bucket === "hour" ? 44 : 48;
  const maxLabels = Math.max(2, Math.floor(innerW / (labelW + 18)));
  const every = Math.max(1, Math.ceil(n / maxLabels));
  const xTicks = [];
  if (!sparkline) {
    let prevDay = null;
    for (let i = 0; i < n; i += every) {
      const d = parseDate(labels[i]);
      const day = d ? d.toDateString() : labels[i];
      const text = bucket === "hour" && prevDay === day ? fmtHour(labels[i]) : fmtDay(labels[i]);
      prevDay = day;
      xTicks.push({ i, text });
    }
  }

  // A bucket is "filled" when something happened in it: a count above zero,
  // or a measured rate (a rate of 0% is a real reading, a missing one is not).
  // Every bucket answers hover; only filled buckets answer a click.
  const filled = labels.map((_, i) =>
    series.some((s) => s.values[i] != null && (s.values[i] > 0 || (unit === "%" && !stacked)))
  );
  const onKey = (e) => {
    if (e.key === "ArrowRight") setHover((h) => (h == null ? 0 : Math.min(n - 1, h + 1)));
    else if (e.key === "ArrowLeft") setHover((h) => (h == null ? n - 1 : Math.max(0, h - 1)));
    else if (e.key === "Escape") setHover(null);
    else if ((e.key === "Enter" || e.key === " ") && hover != null && filled[hover] && onPick) onPick(hover);
    else return;
    e.preventDefault();
  };
  const indexAt = (clientX) => {
    const r = plotRef.current.getBoundingClientRect();
    return Math.max(0, Math.min(n - 1, Math.floor((clientX - r.left - padL) / slot)));
  };
  const pickable = hover != null && filled[hover] && onPick && !sparkline;
  const summary = `${series.map((s) => s.label).join(", ")} over ${n} ${bucket === "hour" ? "hours" : bucket === "week" ? "weeks" : "days"}`;

  // Marks.
  let shapes = null;
  if (!bars) {
    // One continuous line per series; a bucket with nothing to measure sits
    // on the baseline. The only dots are the hovered ones, drawn further
    // down; a sparkline keeps one on its last reading.
    const paths = series.map((s) =>
      s.values
        .map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v ?? 0).toFixed(1)}`)
        .join("")
    );
    shapes = series.map((s, si) => {
      // Series with the same readings share one path. Stacking identical
      // anti-aliased strokes hardens the edge pixels into a jagged, thicker
      // line (seen on a pass-rate chart where every check agreed), so only
      // the topmost of identical paths is drawn; the legend, the hover dots
      // and the tooltip still name every series.
      const drawn = paths.lastIndexOf(paths[si]) === si;
      const present = s.values.map((v, i) => (v != null ? i : -1)).filter((i) => i >= 0);
      const last = present[present.length - 1];
      return (
        <g key={s.key} className="trend-series" style={{ "--mark": colors[si] }}>
          {drawn && <path d={paths[si]} fill="none" strokeWidth={sparkline ? 1.5 : 2} />}
          {sparkline && last != null && <circle cx={x(last)} cy={y(s.values[last])} r={2.5} />}
        </g>
      );
    });
  } else if (stacked) {
    const bw = Math.max(1.5, Math.min(24, slot - (slot > 6 ? 4 : 1)));
    const base = new Array(n).fill(0);
    const lastIdx = labels.map((_, i) => {
      let top = -1;
      series.forEach((s, si) => {
        if ((s.values[i] || 0) > 0) top = si;
      });
      return top;
    });
    shapes = series.map((s, si) => (
      <g key={s.key} className="trend-bars" style={{ "--mark": colors[si] }}>
        {s.values.map((v, i) => {
          const b = base[i];
          const t = b + (v || 0);
          base[i] = t;
          if (!(v > 0)) return null;
          const yTop = y(t);
          const yBot = y(b);
          // 2px surface gap above the segment below it
          const gap = b > 0 ? 2 : 0;
          const h = yBot - yTop - gap;
          if (h < 0.5) return null;
          return <path key={i} d={topRounded(x(i) - bw / 2, yTop, bw, h, lastIdx[i] === si ? 4 : 0)} />;
        })}
      </g>
    ));
  } else {
    // Side by side: the series are independent measures, so each gets its
    // own bar in the bucket's band, in series order, with a 1px gap.
    const m = series.length;
    const groupW = Math.max(m * 2, Math.min(slot - 4, 18 * m));
    const bw = Math.max(1.5, groupW / m - 1);
    shapes = series.map((s, si) => (
      <g key={s.key} className="trend-bars" style={{ "--mark": colors[si] }}>
        {s.values.map((v, i) => {
          if (v == null || !(v > 0)) return null;
          const yTop = y(v);
          const h = y(0) - yTop;
          if (h < 0.5) return null;
          const x0 = x(i) - groupW / 2 + si * (groupW / m);
          return <path key={i} d={topRounded(x0, yTop, bw, h, 3)} />;
        })}
      </g>
    ));
  }

  // The card sits beside the hovered bucket, never over it, so the point
  // (or bar) being read stays visible. Right of it when there is room,
  // otherwise to its left.
  const TIP_W = 240;
  const tipGap = (bars ? slot / 2 : 0) + 12;
  const tipStyle =
    hover == null
      ? undefined
      : x(hover) + tipGap + TIP_W <= w
        ? { left: x(hover) + tipGap }
        : { left: x(hover) - tipGap, transform: "translateX(-100%)" };
  const keyStyle = (si) => ({ "--mark": colors[si] });

  // Tooltip rows: highest value first (a missing reading last), the name
  // dropped on a single series when the panel title already says it.
  let tipRows = null;
  if (hover != null && !sparkline) {
    const rows = series.map((s, si) => ({ s, si, v: s.values[hover] }));
    const allNone = rows.every((r) => r.v == null);
    const sorted = rows.length > 1 ? [...rows].sort((a, b) => (b.v ?? -Infinity) - (a.v ?? -Infinity)) : rows;
    const hideName =
      series.length === 1 && !!panelTitle && panelTitle.toLowerCase().includes(series[0].label.toLowerCase());
    const span = bucket === "hour" ? "hour" : bucket === "week" ? "week" : "day";
    tipRows = allNone ? (
      <div className="trend-tip-none">No data for this {span}</div>
    ) : (
      sorted.map(({ s, si, v }) => (
        <div key={s.key} className="trend-tip-row">
          <i className="trend-tip-key" style={keyStyle(si)} />
          {!hideName && <span className="trend-tip-name">{s.label}</span>}
          <span className={`trend-tip-val${v == null ? " none" : ""}`}>{v == null ? "no data" : fmt(v)}</span>
        </div>
      ))
    );
  }

  let body;
  if (view === "table") {
    body = (
      <div className="trend-tablewrap" ref={plotRef}>
        <table className="trend-table">
          <thead>
            <tr>
              <th>Bucket</th>
              {series.map((s) => <th key={s.key}>{s.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {labels.map((l, i) => (
              <tr key={l}>
                <td>{fmtFull(l, bucket)}</td>
                {series.map((s) => <td key={s.key}>{fmt(s.values[i])}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  } else if (view === "totals") {
    const totals = series
      .map((s, si) => ({ s, si, total: s.values.reduce((t, v) => t + (v || 0), 0) }))
      .sort((a, b) => b.total - a.total);
    const max = Math.max(1, ...totals.map((t) => t.total));
    body = (
      <div className="end-bars trend-totals" ref={plotRef} style={{ minHeight: H }}>
        {totals.map(({ s, si, total }) => (
          <div className="end-row" key={s.key}>
            <span className="end-name" title={s.label}>{s.label}</span>
            <span className="end-bar"><i style={{ width: `${(total / max) * 100}%`, background: colors[si] }} /></span>
            <span className="end-n">{fmt(total)}</span>
          </div>
        ))}
      </div>
    );
  } else {
    body = (
      <div
        className="trend-plot"
        ref={plotRef}
        tabIndex={sparkline ? -1 : 0}
        role="img"
        aria-label={summary}
        aria-describedby={hover != null ? `${id}-tip` : undefined}
        style={{ height: H, cursor: pickable ? "pointer" : "default" }}
        onKeyDown={sparkline ? undefined : onKey}
        onPointerMove={sparkline ? undefined : (e) => setHover(indexAt(e.clientX))}
        onPointerDown={sparkline ? undefined : (e) => e.pointerType !== "mouse" && setHover(indexAt(e.clientX))}
        onPointerLeave={() => setHover(null)}
        onBlur={() => setHover(null)}
        onClick={() => pickable && onPick(hover)}
      >
        <svg width={w} height={H} viewBox={`0 0 ${w} ${H}`} className="trend-svg" aria-hidden="true">
          {!sparkline &&
            scale.ticks.map((t, k2) => (
              <g key={t}>
                <line className={k2 === 0 ? "trend-axis" : "trend-grid"} x1={padL} x2={w - padR} y1={y(t)} y2={y(t)} />
                <text className="trend-tick" x={padL - 8} y={y(t) + 4} textAnchor="end">{tickText[k2]}</text>
              </g>
            ))}
          {hover != null && !sparkline && bars && (
            <rect className="trend-band" x={padL + slot * hover} y={padT} width={slot} height={innerH} rx={4} />
          )}
          {hover != null && !sparkline && !bars && (
            <line className="trend-cursor" x1={x(hover)} x2={x(hover)} y1={padT} y2={padT + innerH} />
          )}
          {shapes}
          {hover != null && !sparkline && !bars &&
            series.map((s, si) => {
              const v = s.values[hover];
              const hollow = v == null || s.hollow?.[hover];
              return (
                <circle
                  key={s.key}
                  className={`trend-hoverdot${hollow ? " hollow" : ""}`}
                  style={{ "--mark": colors[si] }}
                  cx={x(hover)}
                  cy={y(v ?? 0)}
                  r={4}
                />
              );
            })}
          {xTicks.map(({ i, text }) => {
            const cx = x(i);
            const anchor = cx - labelW / 2 < 0 ? "start" : cx + labelW / 2 > w ? "end" : "middle";
            return (
              <text key={i} className="trend-tick" x={cx} y={H - 6} textAnchor={anchor}>{text}</text>
            );
          })}
        </svg>
        {hover != null && !sparkline && (
          <div id={`${id}-tip`} className="trend-tip" role="status" aria-live="polite" style={tipStyle}>
            <div className="trend-tip-when">{fmtFull(labels[hover], bucket)}</div>
            {tipRows}
            {pickable && <div className="trend-tip-hint">click to show these calls</div>}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className={sparkline ? "trend trend-spark" : "trend"}>
      {!sparkline && (
        <div className="trend-tools">
          {series.length > 1 && view !== "totals" && (
            <span className="trend-legend">
              {series.map((s, si) => (
                <span key={s.key} className={`trend-key ${bars ? "box" : "line"}`} style={keyStyle(si)}>
                  <i /> {s.label}
                </span>
              ))}
            </span>
          )}
          {logAllowed && (
            <span className="trend-view text" role="group" aria-label="Axis scale">
              {["linear", "log"].map((a) => (
                <button key={a} type="button" aria-pressed={axis === a} onClick={() => setAxis(a)}>
                  {a === "log" ? "Log" : "Linear"}
                </button>
              ))}
            </span>
          )}
          <span className="trend-view" role="group" aria-label="Chart type">
            {views.map((v) => (
              <button
                key={v}
                type="button"
                aria-pressed={view === v}
                aria-label={VIEW_LABEL[v]}
                title={VIEW_LABEL[v]}
                onClick={() => {
                  setHover(null);
                  setView(v);
                }}
              >
                <svg className={`trend-ic${v === "totals" ? " thick" : ""}`} viewBox="0 0 24 24" aria-hidden="true">{VIEW_ICON[v]}</svg>
              </button>
            ))}
          </span>
          <button type="button" className="trend-btn" onClick={() => downloadCsv(csvName, labels, series)} title="Download these numbers as CSV">
            CSV
          </button>
        </div>
      )}
      {body}
    </div>
  );
}
