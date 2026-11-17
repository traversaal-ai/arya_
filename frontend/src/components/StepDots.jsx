// Wizard progress: the landing site's numbered stepper (01 Industry ...
// 05 Preview). `current` is 1-based. Steps before it are done, the one at
// it is active, the rest are upcoming.
//
// The circles are decoration; the position is real information, so it
// ships as text for assistive tech. Labels default to the wizard's five
// screens and can be overridden per caller.
const DEFAULT_LABELS = ["Industry", "Template", "Details", "Settings", "Preview"];

export default function StepDots({ total, current, labels = DEFAULT_LABELS }) {
  return (
    <div className="steps-wrap">
      <span className="sr-only">Step {current} of {total}</span>
      <ol className="stepper" aria-hidden="true">
        {Array.from({ length: total }, (_, i) => {
          const n = i + 1;
          const cls = n === current ? "step on" : n < current ? "step done" : "step";
          return (
            <li key={n} className={cls}>
              <span className="step-n">{String(n).padStart(2, "0")}</span>
              <span className="step-l">{labels[i] ?? ""}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
