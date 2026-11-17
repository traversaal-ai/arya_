import { useId, useState } from "react";
import { fetchWebsite } from "../api.js";
import StepDots from "../components/StepDots.jsx";
import { labelForIndustry } from "./IndustryPicker.jsx";

/* Shape check only, and deliberately permissive: the field accepts
   "peachtree.com" as well as a full URL, the server does the real fetch, and
   a dead site is already handled by proceeding without context. This exists
   to catch the typo class — spaces, no dot, an obvious non-host — before the
   user waits 8 seconds to find out. It never blocks submission. */
function websiteWarning(value) {
  const v = (value || "").trim();
  if (!v) return null;
  if (/\s/.test(v)) return "That looks like it has a space in it. Check the address.";
  const host = v.replace(/^https?:\/\//i, "").split("/")[0];
  if (!host.includes(".") || /^[.-]|[.-]$/.test(host)) {
    return "That doesn't look like a website address (for example: peachtree.com).";
  }
  return null;
}

// Window 3 of the wizard: three fields instead of the old five-question
// chat. The optional website is fetched server-side (8 s cap) and its text
// personalizes the generated prompt — a dead site never blocks the flow.
export default function BusinessForm({ initial, onDone, onBack }) {
  // "Other" was picked on the industry screen (industry is "" or a custom
  // value typed here earlier) — show a free-text Industry Name field. The
  // typed value feeds the generated prompt ("a business in the {industry}
  // space"), so a niche category still grounds the agent.
  const customIndustryMode = !labelForIndustry(initial.industry);
  const [customIndustry, setCustomIndustry] = useState(
    customIndustryMode ? initial.industry || "" : ""
  );
  const [name, setName] = useState(initial.business_name || "");
  const [websiteInput, setWebsiteInput] = useState(initial.website || "");
  const [goal, setGoal] = useState(initial.agent_purpose || "");
  const [fetching, setFetching] = useState(false);
  const industryId = useId();
  const nameId = useId();
  const siteId = useId();
  const siteHintId = useId();
  const goalId = useId();

  const ready = name.trim() && goal.trim();
  const siteWarning = websiteWarning(websiteInput);

  async function submit() {
    if (!ready || fetching) return;
    let website = websiteInput.trim();
    let website_context = initial.website_context || "";
    if (website && website !== initial.website) {
      website_context = "";
      setFetching(true);
      try {
        const r = await fetchWebsite(website);
        if (r.ok) {
          website = r.url;
          website_context = r.text;
        }
      } catch {
        /* proceed without site context — same as leaving the field blank */
      }
      setFetching(false);
    }
    onDone({
      business_name: name.trim(),
      industry: customIndustryMode
        ? customIndustry.trim()
        : initial.industry || "",
      agent_purpose: goal.trim(),
      website,
      website_context,
    });
  }

  return (
    <div>
      <h1 className="h-serif">
        Complete your <em>agent</em>
      </h1>
      <p className="sub">
        Name your business, describe the goal, and optionally add your website.
      </p>
      <StepDots total={5} current={3} />

      <div className="form-narrow">
      {customIndustryMode && (
        <div className="field">
          <label className="label" htmlFor={industryId}>Industry name</label>
          <input
            id={industryId}
            className="input"
            placeholder="Any other industry type or category that isn't available in the existing ones"
            value={customIndustry}
            onChange={(e) => setCustomIndustry(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.isComposing) submit();
            }}
            autoFocus
          />
        </div>
      )}

      <div className="field">
        <label className="label" htmlFor={nameId}>Business name</label>
        <input
          id={nameId}
          className="input"
          placeholder="e.g. Peachtree Appliance Co."
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.isComposing) submit();
          }}
          required
          autoFocus={!customIndustryMode}
        />
        {/* Seen live: "New Fitness Gym Agent" typed here made the agent
            introduce itself as "calling New Fitness Gym Agent". Non-
            blocking — a real business can legitimately end in "Agents". */}
        {/\bagents?$/i.test(name.trim()) && (
          <div className="hint">
            Just the business's own name is enough (used in the greeting:
            "Thank you for calling {name.trim()}!") — we add "Agent" to the
            agent's name for you.
          </div>
        )}
      </div>

      <div className="field">
        <label className="label" htmlFor={siteId}>
          Website <span className="label-opt">Optional</span>
        </label>
        <input
          id={siteId}
          className="input"
          type="url"
          inputMode="url"
          autoComplete="url"
          placeholder="e.g. peachtree.com"
          aria-describedby={siteHintId}
          value={websiteInput}
          onChange={(e) => setWebsiteInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.isComposing) submit();
          }}
        />
        <div id={siteHintId}>
          {siteWarning && (
            <div className="hint warn">{siteWarning}</div>
          )}
          <div className="hint">
            We'll only access publicly available information from your website
            to personalize your agent.
          </div>
        </div>
      </div>

      <div className="field">
        <label className="label" htmlFor={goalId}>Main goal</label>
        <textarea
          id={goalId}
          className="textarea"
          rows={3}
          placeholder="What should the agent do for your customers? e.g. answer product questions and handle returns"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          required
        />
      </div>
      </div>

      <div className="btn-row">
        <button className="btn ghost" onClick={onBack} disabled={fetching}>
          Back
        </button>
        <button className="btn" onClick={submit} disabled={!ready || fetching}>
          {fetching ? "Reading your website…" : "Continue"}
        </button>
      </div>
    </div>
  );
}
