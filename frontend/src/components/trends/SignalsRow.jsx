import TrendChart from "./TrendChart.jsx";

/* The Errors headline card. Calls and Response speed already exist as
   cards in Insights.jsx, so this completes the golden signals in the same
   grid with the same .stat markup.

   Errors is the composite from the series endpoint (failed tool calls,
   knowledge-base errors and timeouts, failed eval runs, connection-lost
   endings, over answers plus lookups plus tool calls). The live "Open now"
   count against the cap lives on the Publish screen; the Calls tab's
   Simultaneous calls chart covers it over time.                           */

export function ErrorsStat({ trend }) {
  if (!trend) return null;
  const e = trend.series.errors;
  const total = e.errors.reduce((a, b) => a + b, 0);
  const denom = e.denominator.reduce((a, b) => a + b, 0);
  const rate = denom ? total / denom : null;
  const warn = rate != null && rate > 0.05;
  return (
    <div className={warn ? "stat warn" : "stat"}>
      <div className="stat-label">Errors</div>
      <div className="stat-num">
        {rate == null ? "–" : `${(rate * 100).toFixed(1)}%`}
      </div>
      <div className="stat-sub">
        {total ? `${total} of ${denom} actions` : "none in this window"}
      </div>
      <TrendChart
        sparkline
        labels={trend.labels}
        bucket={trend.bucket}
        unit="%"
        series={[{ key: "e", label: "Error rate", values: e.rate }]}
      />
    </div>
  );
}
