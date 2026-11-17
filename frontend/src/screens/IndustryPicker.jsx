import { useEffect, useState } from "react";
import { getModels } from "../api.js";
import StepDots from "../components/StepDots.jsx";
import { IndustryIcon } from "../components/AppIcons.jsx";

// The industry list is server-owned (the `industries` catalog): value is
// prompt grammar — interpolated by the backend's generate_instructions()
// as "a business in the {industry} space", Other = "" (clause skipped) —
// so the list ships next to the grammar. This fallback only covers a dead
// backend so the wizard stays usable. (The server's emoji `icon` field is
// legacy data — icons render from AppIcons.jsx, keyed by value.)
const FALLBACK_INDUSTRIES = [{ label: "Other", value: "" }];

// Module cache: loadIndustries() (called once from App at boot) fills it
// so label lookups stay sync for screens that need them.
let industriesCache = null;

export async function loadIndustries() {
  if (industriesCache) return industriesCache;
  try {
    const m = await getModels();
    if (Array.isArray(m.industries) && m.industries.length) {
      industriesCache = m.industries;
    }
  } catch {
    /* backend may not be up yet — the fallback renders */
  }
  return industriesCache || FALLBACK_INDUSTRIES;
}

// Human label for a business.industry value ("" → undefined so callers
// can use generic copy for "Other").
export function labelForIndustry(value) {
  if (!value) return undefined;
  return (industriesCache || []).find((i) => i.value === value)?.label;
}

export default function IndustryPicker({ initial, onPick, onBack }) {
  const [industries, setIndustries] = useState(
    industriesCache || FALLBACK_INDUSTRIES
  );
  useEffect(() => {
    loadIndustries().then(setIndustries);
  }, []);
  return (
    <div>
      <h1 className="h-serif">
        What industry is <em>your business</em> in?
      </h1>
      <p className="sub">Select the industry that best describes your business.</p>
      <StepDots total={5} current={1} />

      <div className="cards three">
        {industries.map((ind) => {
          // `initial` is undefined until a choice is made and "" only once
          // "Other" has been chosen, so a plain equality check separates the
          // two. A custom industry typed on the next screen is a non-catalog
          // string — it highlights "Other" too, so Back still shows the pick.
          const selected =
            initial != null &&
            (ind.value === ""
              ? !industries.some((i) => i.value && i.value === initial)
              : initial === ind.value);
          return (
            <button
              key={ind.label}
              className={
                (selected ? "card sel" : "card") +
                (ind.value === "" ? " other" : "")
              }
              aria-pressed={selected}
              onClick={() => onPick(ind.value)}
            >
              <div className="card-icon" aria-hidden="true">
                <IndustryIcon value={ind.value} />
              </div>
              <div className="card-title">{ind.label}</div>
            </button>
          );
        })}
      </div>

      <div className="btn-row">
        <button className="btn ghost" onClick={onBack}>
          Back
        </button>
      </div>
    </div>
  );
}
