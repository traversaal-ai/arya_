// Prompt variables, mirrored from backend/src/config.py (render_vars).
// Templates and generated prompts carry {agent_name} / {business_name}
// instead of a literal identity; the worker fills them on every call, and
// the UI uses this to show what will actually be spoken. Targeted
// replacement on purpose: other braces in a prompt pass through untouched,
// and an unset value leaves its token visible rather than a hole.
export const PROMPT_VARS = ["agent_name", "business_name"];

const LEGACY = { agent_name: "<Agent Name>", business_name: "<Business Name>" };

export function promptVars(cfg) {
  return {
    agent_name: (cfg?.name || "").trim(),
    business_name: (cfg?.business?.business_name || "").trim(),
  };
}

export function renderVars(text, cfg) {
  if (!text) return text || "";
  let out = text;
  for (const [key, value] of Object.entries(promptVars(cfg))) {
    if (!value) continue;
    out = out.split("{" + key + "}").join(value).split(LEGACY[key]).join(value);
  }
  return out;
}
