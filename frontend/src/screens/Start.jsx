import { useCallback, useEffect, useState } from "react";
import { getTemplates } from "../api.js";
import ErrorNote from "../components/ErrorNote.jsx";
import StepDots from "../components/StepDots.jsx";
import { IndustryIcon, TemplateIcon, Wand2 } from "../components/AppIcons.jsx";
import { labelForIndustry, loadIndustries } from "./IndustryPicker.jsx";

// Industries with no authored template yet show ONE inert example card —
// it sets the expectation that templates are per-industry without faking a
// clickable product. Food & hospitality and home services have real
// templates (travel, booking) and never reach this map.
const PLACEHOLDER_EXAMPLES = {
  "retail and e-commerce": {
    name: "Order & Returns Helper",
    desc: "Answers product questions, checks order status, explains returns.",
  },
  "healthcare and wellness": {
    name: "Clinic Front Desk",
    desc: "Books appointments and answers common patient questions.",
  },
  "professional services": {
    name: "Client Intake Assistant",
    desc: "Qualifies new enquiries and schedules consultations.",
  },
  "technology and software": {
    name: "Product Support Line",
    desc: "Troubleshoots common issues and files tickets by voice.",
  },
  "education and training": {
    name: "Admissions Assistant",
    desc: "Answers course questions and books campus visits.",
  },
  "finance and insurance": {
    name: "Customer Support",
    desc: "Answers policy and account questions, routes to advisors.",
  },
  "": {
    name: "Voice Assistant",
    desc: "A general-purpose example for your business.",
  },
};

export default function Start({ cfg, onScratch, onPickTemplate, onBack }) {
  const [templates, setTemplates] = useState(null);
  const [err, setErr] = useState("");
  const [industryLabel, setIndustryLabel] = useState(
    labelForIndustry(cfg?.business?.industry)
  );

  const load = useCallback(
    () =>
      getTemplates()
        .then((list) => {
          setTemplates(list);
          setErr("");
        })
        .catch((e) => {
          setTemplates(null);
          setErr(`Couldn't load the templates: ${e.message}`);
        }),
    []
  );

  useEffect(() => {
    load();
    // The catalog is usually warm (App loads it at boot); this covers a
    // deep-link straight to #/new where the label may not be cached yet.
    loadIndustries().then(() =>
      setIndustryLabel(labelForIndustry(cfg?.business?.industry))
    );
  }, [load, cfg?.business?.industry]);

  const industry = cfg?.business?.industry || "";
  const mine = (templates || []).filter(
    (t) => (t.industry || "") === industry
  );
  const placeholder =
    templates && mine.length === 0
      ? PLACEHOLDER_EXAMPLES[industry] || PLACEHOLDER_EXAMPLES[""]
      : null;

  return (
    <div>
      <h1 className="h-serif">
        Pick a <em>starting point</em>
      </h1>
      <p className="sub">
        {/* The industry is the one part of this line that changes with the
            choice you just made, so it carries the emphasis instead of
            sitting flat in the sentence. */}
        {industryLabel ? (
          <>
            Templates for <b>{industryLabel}</b>, or start from scratch.
          </>
        ) : (
          "Start from scratch or pick a ready-made template."
        )}
      </p>
      <StepDots total={5} current={2} />

      {/* The lime band is the one primary action on the screen; it carries
          its own heading, so no section heading above it. */}
      <div className="cards" style={{ marginBottom: 26 }}>
        <button className="card primary" onClick={onScratch}>
          <div className="card-band">
            <div>
              <div className="card-icon" aria-hidden="true"><Wand2 size={26} /></div>
              <div className="card-title">Build agent from scratch</div>
              <div className="card-desc">
                Add a couple of details and we build the agent for you.
              </div>
            </div>
            <span className="btn on-lime card-cta" aria-hidden="true">
              Start
            </span>
          </div>
        </button>
      </div>

      <h2 className="section-h">Use a template</h2>
      {err && <ErrorNote message={err} onRetry={load} />}
      {templates === null && !err && <div className="spin" />}
      {templates && (
        <div className="tlist">
          {mine.map((t) => (
            <button
              className="tlist-row"
              key={t.id}
              onClick={() => onPickTemplate(t.id)}
            >
              <span className="tlist-main">
                <span className="tlist-name">
                  <TemplateIcon id={t.id} size={16} />
                  {t.name}
                </span>
                <span className="tlist-desc">{t.description}</span>
              </span>
              {industryLabel && <span className="tlist-ind">{industryLabel}</span>}
            </button>
          ))}
          {placeholder && (
            <div className="tlist-row planned" aria-disabled="true">
              <span className="tlist-main">
                <span className="tlist-name">
                  <IndustryIcon value={industry} size={16} />
                  {placeholder.name}
                </span>
                <span className="tlist-desc">{placeholder.desc}</span>
              </span>
              <span className="tag planned">Example: coming soon</span>
            </div>
          )}
        </div>
      )}

      <div className="btn-row">
        <button className="btn ghost" onClick={onBack}>
          Back
        </button>
      </div>
    </div>
  );
}
