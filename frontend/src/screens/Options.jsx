import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  addConnection, checkLocalEndpoint, deleteConnection, generatePrompt,
  getConnectors, getModels, getSetupStatus, listConnections, setKeys,
  startConnectionOauth, testConnection, websiteContext,
} from "../api.js";
import { storeCfg } from "../router.js";
import KbUpload from "../components/KbUpload.jsx";
import InfoIcon from "../components/InfoIcon.jsx";
import StepDots from "../components/StepDots.jsx";
import ConfirmDialog from "../components/ConfirmDialog.jsx";
import ErrorNote from "../components/ErrorNote.jsx";
import Icon from "../components/Icon.jsx";
import CopyField from "../components/CopyField.jsx";
import VoicePreview from "../components/VoicePreview.jsx";
import ConnectorCard from "../components/ConnectorCard.jsx";
import AddConnectionDialog from "../components/AddConnectionDialog.jsx";

/* One selectable option card.

   This used to be a <label> wrapping the radio AND its entire expanded body:
   invalid HTML (a label can't contain selects, file inputs or buttons), and
   it made the radio's accessible name the concatenation of its whole
   model-picker subtree. The label is now just the title, and the card keeps
   its click-anywhere behaviour through the handler below — which ignores
   clicks that land on the expanded controls, and on the label itself, since
   htmlFor already toggles the input there. */
function OptCard({ on, inputId, input, title, info, onSelect, children }) {
  return (
    <div
      className={on ? "opt on" : "opt"}
      onClick={(e) => {
        // .info-pop is in the list because its body is plain prose: a click
        // on the text between its links would otherwise bubble out and flip
        // the card's radio while someone is reading the instructions.
        if (
          e.target.closest(
            ".opt-extra, .info-pop, label, a, button, input, select, textarea"
          )
        ) {
          return;
        }
        onSelect();
      }}
    >
      {input}
      <div className="opt-body">
        <div className="opt-title">
          <label htmlFor={inputId}>{title}</label>
          {info}
        </div>
        {children}
      </div>
    </div>
  );
}

// Search providers (id, label, endpoint, key name) and embedding profiles
// come from GET /api/models — the server's catalog registry — so nothing
// here mirrors tools.py or config.py anymore.

// The AI judge's quality checks (LiveKit's built-in judges, one LLM call
// each). The document checks (faithfulness, context relevance, used your
// documents) aren't listed: they ride in the per-answer call whenever RAG
// is on, so there is nothing to pay for and nothing to pick.
const EVAL_JUDGES = [
  { id: "task_completion", label: "Task completion", hint: "did the agent do what the caller asked" },
  { id: "accuracy", label: "Accuracy", hint: "no made-up facts; judged per answer, the worst answer counts" },
  { id: "relevancy", label: "Relevancy", hint: "answers stay on topic; judged per answer, averaged" },
  { id: "conciseness", label: "Conciseness", hint: "no rambling" },
  { id: "coherence", label: "Consistency", hint: "does not contradict itself across the conversation" },
  { id: "tool_use", label: "Tool usage", hint: "used web search and other tools correctly (skipped on conversations without tools)" },
];

const JUDGE_PROVIDER_LABEL = { openai: "OpenAI", groq: "Groq", gemini: "Gemini" };

/* API key field. A saved key renders redacted (first5…last5, from the
   server) in a locked input with an Update button; Update unlocks the
   input and becomes Save, which stores the key immediately and relocks.
   Unsaved edits are reported up via onPending so Continue/Back can guard
   against losing them. The buttons still preventDefault because this field
   is also used inside a form-ish context; the option cards themselves are
   plain divs now, so there is no surrounding label left to toggle. */
function ApiKeyField({ label, name, masked, onSave, onPending }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const inputId = useId();
  const saved = !!masked;
  const open = editing || !saved; // input unlocked

  function change(v) {
    setValue(v);
    onPending(name, v.trim() ? v : null);
  }

  async function save(e) {
    e.preventDefault();
    if (!value.trim()) return;
    setBusy(true);
    try {
      await onSave({ [name]: value.trim() });
      setValue("");
      setEditing(false);
      onPending(name, null);
    } finally {
      setBusy(false);
    }
  }

  function cancel(e) {
    e.preventDefault();
    setEditing(false);
    setValue("");
    onPending(name, null);
  }

  return (
    <div className="field" style={{ marginBottom: 0 }}>
      <label className="label" htmlFor={inputId}>{label}</label>
      <div className="key-row">
        <input
          id={inputId}
          className="input"
          type={open ? "password" : "text"}
          placeholder={open ? "Paste your key" : ""}
          value={open ? value : masked}
          onChange={(e) => change(e.target.value)}
          disabled={!open || busy}
          autoComplete="off"
        />
        {/* A locked field is removed from the tab order, so the fact that a
            key IS stored needs saying in text, not just by rendering a
            greyed-out redaction. */}
        {!open && <span className="sr-only">Key saved</span>}
        {open ? (
          <>
            <button
              type="button"
              className="btn slim"
              onClick={save}
              disabled={busy || !value.trim()}
            >
              {busy ? "…" : "Save"}
            </button>
            {saved && (
              <button type="button" className="btn ghost slim" onClick={cancel} disabled={busy}>
                Cancel
              </button>
            )}
          </>
        ) : (
          <button
            type="button"
            className="btn ghost slim"
            onClick={(e) => {
              e.preventDefault();
              setEditing(true);
            }}
          >
            Update
          </button>
        )}
      </div>
    </div>
  );
}

/* <details> that starts open or closed from a prop but is then the user's
   to toggle. A controlled `open={!ready}` collapsed the OAuth setup panel
   the instant the second key was saved, before the redirect URI under it
   could be copied. */
function Collapsible({ defaultOpen, summary, children }) {
  const [open, setOpen] = useState(!!defaultOpen);
  return (
    <details className="adv" open={open} onToggle={(e) => setOpen(e.target.open)}>
      <summary>{summary}</summary>
      {children}
    </details>
  );
}

// A saved config may reference a model that fell out of the curated list
// (an older default, or a hand-edited config) — keep it selectable instead
// of the select silently rendering blank and rewriting a real value away.
function catalogOptions(list, currentId) {
  const opts = list || [];
  if (currentId && !opts.some((m) => m.id === currentId)) {
    return [...opts, { id: currentId, label: currentId, hint: "from config" }];
  }
  return opts;
}

// Non-blocking hint for the local-provider model fields: Ollama tags are
// `name:tag` (or `hf.co/...` for GGUF passthrough); vLLM/HuggingFace ids are
// `org/repo`. Deliberately imprecise — `myorg/mymodel:3b` is a legitimate
// Ollama tag that looks identical to an HF id — so this can only warn, and
// callers must never use it to block saving.
function namingWarning(style, id) {
  const v = (id || "").trim();
  if (!v || !style) return null;
  const slashes = (v.match(/\//g) || []).length;
  if (style === "ollama") {
    if (v.startsWith("hf.co/") || v.startsWith("huggingface.co/")) return null;
    if (slashes === 1 && (/[A-Z]/.test(v) || !/:[^/]+$/.test(v))) {
      return "Looks like a HuggingFace id, not an Ollama tag (expected name:tag).";
    }
    return null;
  }
  if (style === "hf" && slashes === 0 && /:[a-z0-9.]+$/i.test(v)) {
    return "Looks like an Ollama tag, not a HuggingFace id (expected org/repo).";
  }
  return null;
}

// One stage (Ears/Brain/Voice) of a local backend: either fixed (bundled,
// greyed out — Ollama can't serve STT/TTS at all, so those stay pinned) or
// an editable endpoint + model pair with a live reachability Check and a
// non-blocking naming hint.
//
// Check is deliberately NOT offered on a blank field. `stage.placeholder`
// is a human-facing example ("what does an endpoint URL look like"), not
// the address our own backend actually reaches the bundled container at —
// /api/local/check runs server-side, inside the api container, where
// "localhost" means the api container itself, not `ollama`/`speaches`
// (different containers on the compose network). Testing the placeholder
// would misreport a perfectly healthy bundled service as unreachable.
//
// checkState carries `forUrl`, the exact string it was tested against, and
// every read of it is gated on `forUrl === current url`. That one check
// is what keeps the result honest through three different ways it could
// otherwise go stale: editing the URL after checking it, switching
// backends (Ollama/vLLM use separately-keyed state, but this is a second,
// independent guard), and a slow response landing after a newer check for
// an edited value already completed (out-of-order network races can't
// surface a wrong answer if the render only trusts a result that matches
// what's currently on screen).
function LocalStageBox({ label, stage, url, model, onUrl, onModel, checkState, onCheck, preview = null }) {
  const fixedId = useId();
  const urlId = useId();
  const modelId = useId();
  const hintId = useId();
  if (!stage) return null;
  if (stage.fixed) {
    return (
      <div className="field" style={{ marginBottom: 0 }}>
        <label className="label" htmlFor={fixedId}>{label}</label>
        <div className="voice-row">
          <input id={fixedId} className="input" value={stage.label} readOnly disabled />
          {preview}
        </div>
        <span className="sr-only">Bundled service, not editable.</span>
      </div>
    );
  }
  const trimmed = (url || "").trim();
  const fresh = checkState && checkState.forUrl === trimmed;
  const warning = namingWarning(stage.naming, model);
  // Two inputs share one visible caption, so the caption is a group heading
  // (a plain span, not an orphan <label> pointing at nothing) and each input
  // carries its own accessible name derived from it.
  return (
    <div className="field" style={{ marginBottom: 0 }} role="group" aria-label={label}>
      <span className="label">{label}</span>
      <div className="key-row" style={{ marginBottom: 6 }}>
        <input
          id={urlId}
          className="input"
          placeholder={stage.placeholder}
          aria-label={`${label} endpoint URL`}
          aria-describedby={hintId}
          value={url}
          onChange={(e) => onUrl(e.target.value)}
        />
        {trimmed && (
          <button
            type="button"
            className="btn ghost slim"
            onClick={onCheck}
            disabled={fresh && checkState.busy}
            aria-label={`Check the ${label} endpoint`}
          >
            {fresh && checkState.busy ? "…" : "Check"}
          </button>
        )}
      </div>
      <input
        id={modelId}
        className="input"
        placeholder={(stage.suggestions || [])[0] || "model id"}
        aria-label={`${label} model id`}
        aria-describedby={hintId}
        value={model}
        onChange={(e) => onModel(e.target.value)}
      />
      {/* One described-by target for the whole stage: the reachability result
          lands here too, so a check's outcome is announced rather than being
          a colour change nobody hears. */}
      <div id={hintId}>
        {warning && (
          <div className="hint warn">{warning}</div>
        )}
        {!trimmed && (
          <div className="hint">
            {stage.fallback
              ? "Leave blank to use the bundled service."
              : "Required: vLLM has no bundled server for this stage."}
          </div>
        )}
        {fresh && !checkState.busy && checkState.ok && (
          <div className="hint" role="status">
            <Icon name="check" className="inline" /> reachable · {checkState.models.length} model
            {checkState.models.length === 1 ? "" : "s"}
            {checkState.models.length
              ? `: ${checkState.models.slice(0, 6).join(", ")}${checkState.models.length > 6 ? "…" : ""}`
              : ""}
          </div>
        )}
        {fresh && !checkState.busy && checkState.ok === false && (
          <div className="hint warn" role="alert">
            <Icon name="x" className="inline" /> {checkState.error}
          </div>
        )}
        {stage.note && <div className="hint">{stage.note}</div>}
      </div>
    </div>
  );
}

// `openAdvanced`: Preview passes true when its problem list is showing, so
// Edit agent lands with the panel the problems live in already open.
export default function Options({
  cfg,
  mode = "create",
  onBack,
  onDone,
  openAdvanced = false,
}) {
  const [saved, setSaved] = useState({}); // presence booleans per key
  const [masked, setMasked] = useState({}); // redacted display values
  const [pending, setPending] = useState({}); // typed-but-unsaved key edits
  // base carries every config field the controls below don't touch (models,
  // voices, booking/weather tools) — raw-JSON edits replace it whole.
  const [base, setBase] = useState(cfg);
  const [provider, setProvider] = useState(cfg.provider || "openai");
  // OpenAI model catalog (curated dropdowns) — seeded from the config, then
  // backfilled with server defaults once /api/models resolves.
  const [models, setModels] = useState(null);
  const [sttModel, setSttModel] = useState(cfg.stt?.model || "");
  const [llmModel, setLlmModel] = useState(cfg.llm?.model || "");
  const [ttsModel, setTtsModel] = useState(cfg.tts?.model || "");
  const [voice, setVoice] = useState(cfg.tts?.voice || "");
  // Gemini catalog and picks. Kept in their own `gemini` config group
  // rather than the shared stt/llm/tts blocks, which hold whichever
  // provider was configured last: switching from OpenAI would otherwise
  // hand Gemini a gpt-* model id. Ears are fixed (one streaming Live API
  // transcription model), so there is no STT picker here.
  const [geminiModels, setGeminiModels] = useState(null);
  const [geminiLlm, setGeminiLlm] = useState(cfg.gemini?.llm_model || "");
  const [geminiTts, setGeminiTts] = useState(cfg.gemini?.tts_model || "");
  const [geminiVoice, setGeminiVoice] = useState(cfg.gemini?.voice || "");
  // Expressive delivery (pauses, mood, pacing rendered by the 3.8 TTS).
  // Per agent, off unless the agent says otherwise; the steering itself
  // lives in the gemini_defaults catalog, not here.
  const [geminiExpressive, setGeminiExpressive] = useState(!!cfg.gemini?.expressive);
  // Local-provider backend catalog (Ollama / vLLM) — served alongside the
  // OpenAI catalog by the same /api/models call.
  const [localBackends, setLocalBackends] = useState(null);
  const [localBackend, setLocalBackend] = useState(cfg.local?.backend || "ollama");
  const [ollamaLlmUrl, setOllamaLlmUrl] = useState(cfg.local?.ollama?.llm_url || "");
  const [ollamaLlmModel, setOllamaLlmModel] = useState(
    // legacy configs (pre-backend-split) kept the model at local.llm_model
    cfg.local?.ollama?.llm_model || cfg.local?.llm_model || ""
  );
  const [vllmSttUrl, setVllmSttUrl] = useState(cfg.local?.vllm?.stt_url || "");
  const [vllmSttModel, setVllmSttModel] = useState(cfg.local?.vllm?.stt_model || "");
  const [vllmLlmUrl, setVllmLlmUrl] = useState(cfg.local?.vllm?.llm_url || "");
  const [vllmLlmModel, setVllmLlmModel] = useState(cfg.local?.vllm?.llm_model || "");
  const [vllmTtsUrl, setVllmTtsUrl] = useState(cfg.local?.vllm?.tts_url || "");
  const [vllmTtsModel, setVllmTtsModel] = useState(cfg.local?.vllm?.tts_model || "");
  const [localCheck, setLocalCheck] = useState({}); // stage key -> {busy, ok, models, error}
  const [searchProv, setSearchProv] = useState(
    // "custom" (removed) migrates to Tavily, its closest replacement.
    cfg.search?.provider === "custom" ? "tavily" : cfg.search?.provider || "serpapi"
  );
  const [ragOn, setRagOn] = useState(!!cfg.rag?.enabled);
  const [embProfile, setEmbProfile] = useState(cfg.rag?.embedding_profile || "nomic");
  const [searchOn, setSearchOn] = useState(!!cfg.tools?.web_search);
  // Off unless the config says on: the backend (config.validate, agent.py)
  // treats a missing memory block as off, and every template now ships
  // enabled:false so a fresh agent needs only the provider key.
  const [memoryOn, setMemoryOn] = useState(!!cfg.memory?.enabled);
  const [evalsOn, setEvalsOn] = useState(!!cfg.evals?.enabled);
  // "agent" = judge on the agent's own provider/key; "openai"/"groq"
  // override it (the only way a local-provider agent can be judged).
  const [evalJudgeProv, setEvalJudgeProv] = useState(
    cfg.evals?.judge_provider || "agent"
  );
  const [evalJudges, setEvalJudges] = useState(
    cfg.evals?.judges || EVAL_JUDGES.map((j) => j.id)
  );
  const [name, setName] = useState(cfg.name || "");
  // The brand identity the agent works under: interpolated into generated
  // prompts and printed on the written itinerary. Template-created agents
  // skip the business form, so this is their only way to edit it.
  const [businessName, setBusinessName] = useState(
    cfg.business?.business_name || ""
  );
  // The business website: its homepage text rides in the prompt as the
  // business-context block. Scratch agents set it on the Details step;
  // template agents can add it here. The prompt changes only on the button.
  const [website, setWebsite] = useState(cfg.business?.website || "");
  const [websiteContextText, setWebsiteContextText] = useState(
    cfg.business?.website_context || ""
  );
  const [siteBusy, setSiteBusy] = useState(false);
  const [siteNote, setSiteNote] = useState(""); // status or error line
  const [instructions, setInstructions] = useState(cfg.instructions || "");
  const [greeting, setGreeting] = useState(cfg.greeting || "");
  // Quick replies under the greeting, one per line in the field, at most
  // four. Stored as cfg.openers; the worker sends them with the greeting.
  const [openers, setOpeners] = useState(
    (Array.isArray(cfg.openers) ? cfg.openers : []).join("\n")
  );
  const [busy, setBusy] = useState(false);
  const [guard, setGuard] = useState(null); // null | "continue" | "back"
  const [err, setErr] = useState("");
  // Advanced settings disclosure: closed by default, opened by the user, or
  // by Preview's Edit agent when problems are listed (see openAdvanced).
  const [advOpen, setAdvOpen] = useState(!!openAdvanced);
  const [setupLoaded, setSetupLoaded] = useState(false); // /api/setup/status answered
  // Both of these used to be `.catch(() => {})`. A failed /api/setup/status
  // silently claimed no keys were stored; a failed /api/models silently left
  // every model dropdown empty. Neither said anything, so the screen looked
  // like it had loaded fine and simply lost the user's configuration.
  const [setupErr, setSetupErr] = useState("");
  const [modelsErr, setModelsErr] = useState("");
  // Server-owned registries (see loadSetup/loadModels): upload limits,
  // search providers, embedding profiles, and the model defaults.
  const [limits, setLimits] = useState(null);
  const [searchProviders, setSearchProviders] = useState([]);
  const [embProfiles, setEmbProfiles] = useState([]);
  const [modelDefaults, setModelDefaults] = useState(null);
  // tool_catalog: the Preview map's display copy per tools.* toggle. Used
  // here only to label the "replaces the sample X" checkbox the same way.
  const [toolCatalog, setToolCatalog] = useState(null);
  // Connectors (MCP). Connection rows are GLOBAL (connect once, use on any
  // agent); `attach` is this agent's slice of them (cfg.connectors: which
  // connections, which of their tools, public or not). See
  // components/ConnectorCard.jsx for the rendering.
  const [connectorTypes, setConnectorTypes] = useState([]);
  const [connectorCategories, setConnectorCategories] = useState([]);
  const [connections, setConnections] = useState([]);
  const [connLoaded, setConnLoaded] = useState(false);
  const [attach, setAttach] = useState(cfg.connectors || []);
  const [connErr, setConnErr] = useState("");
  const [connNote, setConnNote] = useState("");
  const [connBusy, setConnBusy] = useState(null); // connection or type id in flight
  const [addFor, setAddFor] = useState(null); // catalog type open in the Add dialog
  const [addErr, setAddErr] = useState("");
  const [removeConn, setRemoveConn] = useState(null); // row pending removal
  const [connectType, setConnectType] = useState(null); // OAuth type behind the key guard
  const [redirectUri, setRedirectUri] = useState("");
  // Set from the ?connector= return params after an OAuth round trip; the
  // row is attached to this agent once the connections list has loaded.
  const pendingAttachRef = useRef(null);

  // Stable ids so every control can be reached by its visible caption.
  const sttId = useId();
  const llmId = useId();
  const ttsId = useId();
  const voiceId = useId();
  const gSttId = useId();
  const gLlmId = useId();
  const gTtsId = useId();
  const gVoiceId = useId();
  const backendId = useId();
  const embId = useId();
  const searchProvId = useId();
  const endpointId = useId();
  const nameId = useId();
  const businessNameId = useId();
  const promptId = useId();
  const promptHintId = useId();
  const greetingId = useId();
  const greetingHintId = useId();
  const openersId = useId();
  const openersHintId = useId();
  const evalProvId = useId();

  const loadSetup = useCallback(
    () =>
      getSetupStatus()
        .then((s) => {
          setSaved(s.keys || {});
          setMasked(s.masked || {});
          setLimits(s.limits || null);
          setSetupLoaded(true);
          setSetupErr("");
        })
        .catch((e) =>
          setSetupErr(`Couldn't check which API keys are saved: ${e.message}.`)
        ),
    []
  );

  const loadModels = useCallback(
    () =>
      getModels()
        .then((m) => {
          setModels(m.openai);
          setGeminiModels(m.gemini || null);
          setLocalBackends(m.local || null);
          setSearchProviders(m.search || []);
          setEmbProfiles(m.embeddings || []);
          setModelDefaults(m.defaults || null);
          setToolCatalog(m.tools || null);
          setSttModel((v) => v || m.defaults.stt_model);
          setLlmModel((v) => v || m.defaults.llm_model);
          setTtsModel((v) => v || m.defaults.tts_model);
          setVoice((v) => v || m.defaults.voice);
          setModelsErr("");
        })
        .catch((e) =>
          setModelsErr(`Couldn't load the model list: ${e.message}.`)
        ),
    []
  );

  useEffect(() => {
    loadSetup();
  }, [loadSetup]);

  useEffect(() => {
    loadModels();
  }, [loadModels]);

  const loadConnectors = useCallback(
    () =>
      getConnectors()
        .then((r) => {
          setConnectorTypes(r.connectors || []);
          setConnectorCategories(r.categories || []);
          setRedirectUri(r.redirect_uri || "");
        })
        .catch((e) => setConnErr(`Couldn't load connector types: ${e.message}.`)),
    []
  );

  const loadConnections = useCallback(
    () =>
      listConnections()
        .then((rows) => {
          setConnections(rows);
          setConnLoaded(true);
          setConnErr("");
          const want = pendingAttachRef.current;
          if (want) {
            pendingAttachRef.current = null;
            const row = rows.find((c) => c.id === want);
            if (row) attachConn(row);
          }
        })
        .catch((e) => setConnErr(`Couldn't load connections: ${e.message}.`)),
    []
  );

  useEffect(() => {
    loadConnectors();
    loadConnections();
  }, [loadConnectors, loadConnections]);

  // OAuth return: the provider sent the browser back to
  // /?connector=<id>&connector_status=ok|error[&connector_error=…]#/options.
  // Read the params once, strip them from the URL (same trick api.js uses
  // for ?admin=), and attach the new connection to this agent.
  useEffect(() => {
    const p = new URLSearchParams(location.search);
    if (!p.has("connector_status")) return;
    const id = p.get("connector");
    const status = p.get("connector_status");
    const error = p.get("connector_error");
    ["connector", "connector_status", "connector_error"].forEach((k) => p.delete(k));
    const u = new URL(location.href);
    u.search = p.toString();
    history.replaceState({}, "", u.pathname + u.search + u.hash);
    if (status === "ok") {
      pendingAttachRef.current = id;
      setConnNote("Signed in. The connection is attached to this agent below.");
    } else {
      setConnErr(`Sign-in failed: ${error || "unknown error"}.`);
    }
  }, []);

  async function checkLocal(key, url) {
    // No placeholder fallback: a blank field means "use the bundled
    // service," and there's nothing meaningful for /api/local/check (which
    // runs server-side, inside the api container) to test — see
    // LocalStageBox's comment for why testing the display placeholder was
    // wrong. `forUrl` records exactly what's being tested so a render can
    // never attribute a result to a value it wasn't produced for.
    const target = (url || "").trim();
    if (!target) return;
    setLocalCheck((c) => ({ ...c, [key]: { busy: true, forUrl: target } }));
    try {
      const res = await checkLocalEndpoint(target);
      setLocalCheck((c) => ({ ...c, [key]: { busy: false, forUrl: target, ...res } }));
    } catch (e) {
      setLocalCheck((c) => ({
        ...c, [key]: { busy: false, forUrl: target, ok: false, error: e.message },
      }));
    }
  }

  // Voice options depend on the TTS model (gpt-4o-mini-tts: 13, tts-1/hd: 9)
  // — when switching models drops the current voice, clamp to a voice
  // supported by both sets rather than sending an invalid pair.
  useEffect(() => {
    if (!models) return;
    const entry = models.tts.find((m) => m.id === ttsModel);
    if (entry && voice && !entry.voices.includes(voice)) {
      // Clamp to the server's default voice (every TTS model's set
      // includes it), not a hardcoded one.
      setVoice(modelDefaults?.voice || entry.voices[0]);
    }
  }, [ttsModel, models]); // eslint-disable-line react-hooks/exhaustive-deps

  // Show the real Gemini defaults in the dropdowns instead of a blank
  // selection: the catalog lists the recommended pick first. Only fills
  // blanks, so a saved choice is never overwritten. No voice clamp is
  // needed here (unlike OpenAI): Gemini voices are prebuilt and work with
  // every TTS model.
  useEffect(() => {
    if (!geminiModels) return;
    setGeminiLlm((v) => v || geminiModels.llm?.[0]?.id || "");
    setGeminiTts((v) => v || geminiModels.tts?.[0]?.id || "");
    setGeminiVoice((v) => v || geminiModels.voices?.[0] || "");
  }, [geminiModels]);

  // Generate the system prompt from the wizard answers when the config
  // doesn't carry one yet (custom flow, or answers were re-entered).
  // Templates arrive with authored instructions and skip this.
  useEffect(() => {
    if (!cfg.instructions && cfg.business) {
      generatePrompt(cfg.business, !!cfg.rag?.enabled)
        .then((r) => {
          setInstructions(r.instructions);
          if (!cfg.greeting) setGreeting(r.greeting || "");
        })
        .catch((e) => setErr(e.message));
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const websiteId = useId();

  // Fetch the homepage and put its text into the prompt as the business
  // context block (server-side upsert, so an authored template prompt is
  // kept and the block is never duplicated). An empty field removes it.
  async function applyWebsite() {
    const url = website.trim();
    setSiteBusy(true);
    setSiteNote("");
    try {
      const r = await websiteContext(url, instructions);
      if (!r.ok) {
        setSiteNote(
          `Couldn't read ${url}: ${r.error || "fetch failed"}. The prompt is unchanged.`
        );
        return;
      }
      setInstructions(r.instructions || "");
      if (url) {
        let host = r.url || url;
        try {
          host = new URL(r.url || url).host || host;
        } catch {
          /* keep the raw value */
        }
        setWebsite(r.url || url);
        setWebsiteContextText(r.text || "");
        setSiteNote(
          `Prompt updated with ${host}. Press ${mode === "edit" ? "Update" : "Continue"} to save.`
        );
      } else {
        setWebsiteContextText("");
        setSiteNote("Website removed from the prompt.");
      }
    } catch (e) {
      setSiteNote(`Couldn't update the prompt: ${e.message}`);
    } finally {
      setSiteBusy(false);
    }
  }

  async function saveKey(patch) {
    const res = await setKeys(patch);
    setSaved(res.keys || {});
    setMasked(res.masked || {});
  }

  function keyPending(keyName, value) {
    setPending((p) => {
      const next = { ...p };
      if (value === null) delete next[keyName];
      else next[keyName] = value;
      return next;
    });
  }

  // ── connectors ──────────────────────────────────────────────────────────
  // Read tools are pre-selected on attach; write tools (create/update/
  // delete) start unticked so nothing changes data until the owner says so.
  function defaultTools(row) {
    return (row.tools || []).filter((t) => !t.write).map((t) => t.name);
  }

  function attachConn(row) {
    // The attachment records its connector TYPE, and the sample desk that
    // type covers is switched off from that. It used to be a checkbox, but
    // there was never a sound way to choose: the two desks collide on tool
    // names and the sample one wins the dedupe, so "both on" meant quoting
    // a real free slot and booking it into the demo desk. See
    // connectors.replaced_toolsets, which derives the same set worker side.
    setAttach((prev) =>
      prev.some((a) => a.connection_id === row.id)
        ? prev
        : [...prev, {
            connection_id: row.id,
            type: row.type,
            allowed_tools: defaultTools(row),
            public_tools: [],
          }]
    );
  }

  function detachConn(row) {
    setAttach((prev) => prev.filter((a) => a.connection_id !== row.id));
  }

  function setConnTools(row, names) {
    setAttach((prev) =>
      prev.map((a) =>
        a.connection_id !== row.id
          ? a
          : { ...a, allowed_tools: names, public_tools: a.public_tools?.length ? names : [] }
      )
    );
  }

  function setConnPublic(row, on) {
    setAttach((prev) =>
      prev.map((a) =>
        a.connection_id !== row.id ? a : { ...a, public_tools: on ? a.allowed_tools : [] }
      )
    );
  }

  function setConnPurpose(row, text) {
    setAttach((prev) =>
      prev.map((a) => (a.connection_id !== row.id ? a : { ...a, purpose: text }))
    );
  }

  // Sample toolsets this agent has on that attaching this connector type
  // will switch off (catalog `overlaps`), with the Preview map's own labels
  // so the two screens call them the same thing. Stated on the card as a
  // consequence, no longer offered as a checkbox.
  // Connector types this template says it needs, with nothing of that type
  // attached yet. A want, not an attachment: connection ids are minted per
  // install, so a template that ships in the repo cannot name one. Purely
  // advisory, so an agent on the sample desk stays publishable.
  const wantedTypes = base.connectors_wanted || [];
  function isWantedAndMissing(type) {
    if (!wantedTypes.includes(type.id)) return false;
    const ids = connections.filter((c) => c.type === type.id).map((c) => c.id);
    return !attach.some((a) => ids.includes(a.connection_id));
  }

  function stubToolsetsFor(type) {
    const on = base.tools || {};
    return (type.overlaps || [])
      .filter((id) => on[id])
      .map((id) => ({
        id,
        label: toolCatalog?.[id]?.label || `${id} tools`,
      }));
  }

  async function saveNewConnection(body) {
    setAddErr("");
    setConnBusy(body.type);
    try {
      const row = await addConnection(body);
      setConnections((cs) => [...cs, row]);
      attachConn(row);
      setAddFor(null);
    } catch (e) {
      setAddErr(e.message);
    } finally {
      setConnBusy(null);
    }
  }

  async function testConn(row) {
    setConnBusy(row.id);
    setConnErr("");
    try {
      const fresh = await testConnection(row.id);
      setConnections((cs) => cs.map((c) => (c.id === fresh.id ? fresh : c)));
    } catch (e) {
      setConnErr(`Test failed: ${e.message}.`);
    } finally {
      setConnBusy(null);
    }
  }

  async function removeConnNow(row) {
    setConnBusy(row.id);
    setConnErr("");
    try {
      await deleteConnection(row.id, { force: true });
      setConnections((cs) => cs.filter((c) => c.id !== row.id));
      detachConn(row);
    } catch (e) {
      setConnErr(`Couldn't remove "${row.name}": ${e.message}.`);
    } finally {
      setConnBusy(null);
      setRemoveConn(null);
    }
  }

  async function saveDirtyKeys() {
    const dirty = Object.entries(pending).filter(([, v]) => v && v.trim());
    if (dirty.length) {
      await saveKey(Object.fromEntries(dirty.map(([k, v]) => [k, v.trim()])));
      setPending({});
    }
  }

  // Connect: non-OAuth types open the Add dialog; OAuth types leave for the
  // provider's consent screen (full-page redirect, no popup to get blocked).
  // Typed-but-unsaved key edits are saved first, through the same guard the
  // Continue button uses, or the start endpoint would 400 on a missing key.
  function connect(type) {
    if (type.auth !== "oauth") {
      setAddErr("");
      setAddFor(type);
      return;
    }
    const dirty = Object.entries(pending).filter(([, v]) => v && v.trim());
    if (dirty.length) {
      setConnectType(type);
      setGuard("connect");
      return;
    }
    startOauth(type);
  }

  async function confirmConnect() {
    setGuard(null);
    setBusy(true);
    try {
      await saveDirtyKeys();
    } catch (e) {
      setConnErr(e.message);
      return;
    } finally {
      setBusy(false);
    }
    if (connectType) startOauth(connectType);
  }

  async function startOauth(type, existing = null) {
    setConnBusy(existing ? existing.id : type.id);
    setConnErr("");
    try {
      // The redirect unloads the page: park the in-progress edits where
      // App restores cfg from (sessionStorage) so nothing typed is lost.
      storeCfg(currentCfg());
      const row = existing || (await addConnection({ type: type.id, name: type.label }));
      const res = await startConnectionOauth(row.id, location.origin + location.pathname);
      window.location.assign(res.auth_url);
    } catch (e) {
      setConnErr(e.message);
      setConnBusy(null);
    }
  }

  function currentCfg() {
    const next = {
      ...base,
      provider,
      rag: { ...base.rag, enabled: ragOn, embedding_profile: embProfile },
      search: { provider: searchProv },
      tools: { ...base.tools, web_search: searchOn },
      memory: { ...base.memory, enabled: memoryOn },
      evals: {
        ...base.evals,
        enabled: evalsOn,
        judge_provider: evalJudgeProv,
        judges: evalJudges,
      },
      name,
      business: {
        ...base.business,
        business_name: businessName.trim(),
        website: website.trim(),
        website_context: websiteContextText,
      },
      instructions,
      greeting: greeting.trim(),
      openers: openers
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean)
        .slice(0, 4),
      // Drop attachments whose connection is gone, but only once the list
      // has actually loaded: a failed fetch must not silently detach.
      connectors: connLoaded
        ? attach.filter((a) => connections.some((c) => c.id === a.connection_id))
        : attach,
    };
    if (provider === "openai") {
      // Guard against blank state (e.g. /api/models never resolved) —
      // never write an empty model id over a real one.
      next.stt = { ...base.stt, ...(sttModel ? { model: sttModel } : {}) };
      next.llm = { ...base.llm, ...(llmModel ? { model: llmModel } : {}) };
      next.tts = {
        ...base.tts,
        ...(ttsModel ? { model: ttsModel } : {}),
        ...(voice ? { voice } : {}),
      };
    }
    if (provider === "gemini") {
      // Own group, not the shared stt/llm/tts blocks — see the state
      // declarations. Ears are fixed, so only brain, voice model and voice.
      next.gemini = {
        ...base.gemini,
        ...(geminiLlm ? { llm_model: geminiLlm } : {}),
        ...(geminiTts ? { tts_model: geminiTts } : {}),
        ...(geminiVoice ? { voice: geminiVoice } : {}),
        expressive: geminiExpressive,
      };
    }
    if (provider === "local") {
      // base.local's own stt_model/voice (the bundled Speaches config) pass
      // through untouched via ...base.local — only the backend choice and
      // its endpoint/model fields are edited here.
      next.local = {
        ...base.local,
        backend: localBackend,
        ollama: {
          ...base.local?.ollama,
          llm_url: ollamaLlmUrl.trim(),
          llm_model: ollamaLlmModel.trim(),
        },
        vllm: {
          ...base.local?.vllm,
          stt_url: vllmSttUrl.trim(),
          stt_model: vllmSttModel.trim(),
          llm_url: vllmLlmUrl.trim(),
          llm_model: vllmLlmModel.trim(),
          tts_url: vllmTtsUrl.trim(),
          tts_model: vllmTtsModel.trim(),
        },
      };
    }
    return next;
  }

  async function proceed() {
    setGuard(null);
    setBusy(true);
    setErr("");
    try {
      await saveDirtyKeys();
      onDone(currentCfg());
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  function next() {
    // Unsaved-key guard: nothing typed into a key field is stored until
    // its Save button (or this dialog) fires — never silently lose it.
    const dirty = Object.entries(pending).filter(([, v]) => v && v.trim());
    if (dirty.length) {
      setGuard("continue");
      return;
    }
    proceed();
  }

  function back() {
    if (Object.keys(pending).length) {
      setGuard("back");
      return;
    }
    onBack();
  }

  const hasKey = (name) => saved[name] || pending[name];

  // ── Advanced settings disclosure ────────────────────────────────────────
  // Collapsed by default, even in edit mode with abilities on: unfolding
  // seven sections because RAG is on would rebuild the wall this screen
  // just removed. The summary line says what exists; the rule below opens
  // the panel only for BLOCKERS, mirroring config.validate() so Preview can
  // never flag something the user could not see here.
  const backendEntry = (localBackends || []).find((b) => b.id === localBackend);
  const searchKeyName =
    searchProviders.find((p) => p.id === searchProv)?.key || searchProv;
  const embKeyName = embProfiles.find((p) => p.id === embProfile)?.needs_key;
  const judge = evalJudgeProv === "agent" ? provider : evalJudgeProv;
  // Every key check waits for /api/setup/status: before it resolves `saved`
  // is {} and "memory on, no Mem0 key" would be true for one render, which
  // is enough to open a panel that then never closes (the effect below is
  // open-only by design). A failed status load also stays quiet: the
  // provider section's ErrorNote already says keys could not be checked.
  // Everything inside Advanced that would stop Test agent, as a labelled
  // list: the header shows a count, each section shows its own tag, and the
  // closed panel shows the texts in one line. Same checks as the server's
  // problems list, so the wording matches Preview.
  const advProblems = [];
  const searchLabel =
    searchProviders.find((p) => p.id === searchProv)?.label || searchProv;
  if (provider === "local" && localBackend === "vllm" && !vllmLlmUrl.trim()) {
    advProblems.push({ section: "models", text: "The vLLM backend needs an LLM endpoint URL." });
  }
  if (setupLoaded && searchOn && !hasKey(searchKeyName)) {
    advProblems.push({ section: "search", text: `Web search is on but the ${searchLabel} API key is missing.` });
  }
  if (setupLoaded && memoryOn && !hasKey("mem0")) {
    advProblems.push({ section: "memory", text: "Memory is on but the Mem0 API key is missing." });
  }
  if (setupLoaded && ragOn && !!embKeyName && !hasKey(embKeyName)) {
    advProblems.push({ section: "rag", text: `Documents are on but the ${embKeyName} key for embeddings is missing.` });
  }
  if (setupLoaded && evalsOn) {
    if (judge === "local") {
      advProblems.push({ section: "evals", text: "Evaluation is on but the judge cannot run on local models." });
    } else if (!hasKey(judge)) {
      advProblems.push({ section: "evals", text: `Evaluation is on but the ${JUDGE_PROVIDER_LABEL[judge] || judge} key its judge needs is missing.` });
    }
    if (evalJudges.length === 0) {
      advProblems.push({ section: "evals", text: "Evaluation is on but no quality checks are ticked." });
    }
  }
  // Same checks as config._connector_problems, gated on both lists having
  // loaded so a slow fetch does not read as "connection removed". An
  // attachment whose connection no longer exists is NOT a blocker here:
  // currentCfg() drops it on save, so the validator never sees it, and
  // there is nothing on screen to fix (ConnectorCard lists live rows only).
  if (setupLoaded && connLoaded) {
    for (const a of attach) {
      const row = connections.find((c) => c.id === a.connection_id);
      if (!row) continue;
      const type = connectorTypes.find((t) => t.id === row.type);
      const k = type?.oauth?.keys;
      const name = row.name || type?.label || "A connector";
      if (["needs_auth", "pending_auth", "error"].includes(row.status)) {
        advProblems.push({ section: "connectors", text: `${name} needs sign-in or is in error.` });
      } else if (!a.allowed_tools?.length) {
        advProblems.push({ section: "connectors", text: `${name} is attached but has no tools ticked.` });
      } else if (!!k && !(hasKey(k.client_id) && hasKey(k.client_secret))) {
        advProblems.push({ section: "connectors", text: `${name} needs its OAuth client id and secret.` });
      }
    }
  }
  // The (hidden) required prompt was blanked by hand. `cfg.instructions`
  // keeps the custom path collapsed while generatePrompt is still running.
  if (!instructions.trim() && !!cfg.instructions) {
    advProblems.push({ section: "prompt", text: "The system prompt is empty." });
  }
  const sectionTag = (id, label = "needs attention") =>
    advProblems.some((p) => p.section === id) ? (
      <span className="tag warn sec-tag">{label}</span>
    ) : null;
  // Template agents arrive with features switched on by their author; the
  // one-click "turn off" links say what is lost before the user clicks.
  const isTemplate = !!cfg.template_id && cfg.template_id !== "custom";
  const bundledEmbProfile =
    embProfiles.find((p) => !p.needs_key)?.id || "nomic";
  const advBits = [
    ragOn && "Documents on",
    searchOn && "Web search on",
    attach.length > 0 &&
      `${attach.length} connector${attach.length === 1 ? "" : "s"}`,
    memoryOn && "Memory on",
    evalsOn && "Evaluation on",
    provider === "local" && localBackend === "vllm" && "vLLM backend",
  ].filter(Boolean);
  const advSummary = advBits.join(", ");
  // The panel never opens itself over a missing key (the header tag carries
  // that); it opens once when an OAuth sign-in lands back here, so the
  // attached row is visible rather than hidden behind a closed panel.
  useEffect(() => {
    if (connNote) setAdvOpen(true);
  }, [connNote]);

  return (
    <div>
      <h1 className="h-serif">
        Name your agent and <em>pick a voice provider</em>
      </h1>
      <p className="sub">
        Two things to fill in, one key to paste. Everything else lives under
        Advanced settings. Keys are stored on your machine only.
      </p>
      {cfg.template_id === "custom" && <StepDots total={5} current={4} />}

      {/* Core flow, deliberately short: identity, then the provider and its
          key. Everything technical sits under the Advanced disclosure
          below, which opens itself only when something in there would
          block Test agent (see advProblems). */}
      <h2 className="section-h">Agent identity</h2>

      <div className="field-row two">
      <div className="field">
        <label className="label" htmlFor={nameId}>Agent name</label>
        <input
          id={nameId}
          className="input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Aria"
        />
      </div>

      <div className="field">
        <label className="label" htmlFor={businessNameId}>Business name</label>
        <input
          id={businessNameId}
          className="input"
          value={businessName}
          onChange={(e) => setBusinessName(e.target.value)}
          placeholder="The brand the agent speaks for"
        />
      </div>
      </div>
      <div className="field">
        <div className="hint" style={{ marginTop: -10 }}>
          Printed on documents the agent produces (e.g. the travel
          itinerary) and filled into the prompt and first message wherever
          they use <code>{"{business_name}"}</code>, on every conversation. Renaming
          here follows through automatically.
        </div>
      </div>

      <div className="field">
        <label className="label" htmlFor={greetingId}>
          First message <span className="label-opt">Optional</span>
        </label>
        <input
          id={greetingId}
          className="input"
          aria-describedby={greetingHintId}
          value={greeting}
          onChange={(e) => setGreeting(e.target.value)}
          placeholder="e.g. Thank you for calling New Fitness Gym! How can I help you today?"
        />
        <div className="hint" id={greetingHintId}>
          Spoken word-for-word when the conversation starts. Leave empty and the
          agent improvises a greeting from its prompt instead. Optional
          variables, filled from the fields above:{" "}
          <code>{"{agent_name}"}</code> and <code>{"{business_name}"}</code>.
        </div>
      </div>

      <div className="field">
        <label className="label" htmlFor={openersId}>
          Quick replies <span className="label-opt">Optional</span>
        </label>
        <textarea
          id={openersId}
          className="textarea"
          rows={3}
          aria-describedby={openersHintId}
          value={openers}
          onChange={(e) => setOpeners(e.target.value)}
          placeholder={"Book an appointment\nOpening hours"}
        />
        <div className="hint" id={openersHintId}>
          Shown as tappable buttons under the first message, one per line, up to
          four. Tapping one sends it as the caller's reply. Leave empty for none.
        </div>
      </div>

      <h2 className="section-h">Voice provider</h2>
      {/* One note, not two. Both loads hit the same backend, so when it's
          down they fail together — and stacking two near-identical banners
          under the shell's own "can't reach the backend" note gave the screen
          three amber blocks saying one thing. */}
      {(modelsErr || setupErr) && (
        <ErrorNote
          message={
            modelsErr && setupErr
              ? `${modelsErr} ${setupErr}`
              : modelsErr || setupErr
          }
          onRetry={() => {
            if (modelsErr) loadModels();
            if (setupErr) loadSetup();
          }}
          style={{ marginBottom: 12 }}
        />
      )}
      <div className="opt-group" style={{ marginBottom: 24 }}>
        <OptCard
          on={provider === "openai"}
          inputId="provider-openai"
          onSelect={() => setProvider("openai")}
          input={
            <input
              id="provider-openai"
              type="radio"
              name="provider"
              checked={provider === "openai"}
              onChange={() => setProvider("openai")}
            />
          }
          title="OpenAI"
          info={
              <InfoIcon>
                OpenAI based STT, LLM and TTS. To get the key:
                <ol>
                  <li>
                    Login to{" "}
                    <a href="https://platform.openai.com/" target="_blank" rel="noreferrer">
                      platform.openai.com
                    </a>
                  </li>
                  <li>Go to API keys</li>
                  <li>Create a new secret key, set it's name and save it</li>
                  <li>Copy the API key and paste in the box</li>
                </ol>
              </InfoIcon>
          }
        >
            {provider === "openai" && (
              <div className="opt-extra">
                <ApiKeyField
                  label="OpenAI API key"
                  name="openai"
                  masked={masked.openai}
                  onSave={saveKey}
                  onPending={keyPending}
                />
                {/* Model and voice pickers moved under Advanced settings:
                    the defaults are the recommended picks, and a first
                    run needs only the key. */}
              </div>
            )}
        </OptCard>
        <OptCard
          on={provider === "groq"}
          inputId="provider-groq"
          onSelect={() => setProvider("groq")}
          input={
            <input
              id="provider-groq"
              type="radio"
              name="provider"
              checked={provider === "groq"}
              onChange={() => setProvider("groq")}
            />
          }
          title="Groq"
          info={
              <InfoIcon>
                Whisper ears, GPT-OSS 120B brain, Orpheus voice: all open
                models on Groq's free tier, very fast, one key.
                <ol>
                  <li>
                    Login to{" "}
                    <a href="https://console.groq.com/" target="_blank" rel="noreferrer">
                      console.groq.com
                    </a>
                  </li>
                  <li>Go to API Keys</li>
                  <li>Create a new API key and save it</li>
                  <li>Copy the API key and paste in the box</li>
                </ol>
              </InfoIcon>
          }
        >
            {provider === "groq" && (
              <div className="opt-extra">
                <ApiKeyField
                  label="Groq API key"
                  name="groq"
                  masked={masked.groq}
                  onSave={saveKey}
                  onPending={keyPending}
                />
              </div>
            )}
        </OptCard>
        <OptCard
          on={provider === "gemini"}
          inputId="provider-gemini"
          onSelect={() => setProvider("gemini")}
          input={
            <input
              id="provider-gemini"
              type="radio"
              name="provider"
              checked={provider === "gemini"}
              onChange={() => setProvider("gemini")}
            />
          }
          title="Gemini"
          info={
              <InfoIcon>
                Google ears, brain and voice behind one key. Streaming
                transcription, and the Gemini TTS voices are the most
                expressive of the hosted options.
                <ol>
                  <li>
                    Login to{" "}
                    <a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer">
                      aistudio.google.com
                    </a>
                  </li>
                  <li>Go to API keys</li>
                  <li>Create an API key and save it</li>
                  <li>Copy the API key and paste in the box</li>
                </ol>
              </InfoIcon>
          }
        >
            {provider === "gemini" && (
              <div className="opt-extra">
                <ApiKeyField
                  label="Google API key"
                  name="gemini"
                  masked={masked.gemini}
                  onSave={saveKey}
                  onPending={keyPending}
                />
              </div>
            )}
        </OptCard>
        <OptCard
          on={provider === "local"}
          inputId="provider-local"
          onSelect={() => setProvider("local")}
          input={
            <input
              id="provider-local"
              type="radio"
              name="provider"
              checked={provider === "local"}
              onChange={() => setProvider("local")}
            />
          }
          title="Open source"
          info={
              <InfoIcon>
                Free and private, no API key needed. Two backends:
                <ol>
                  <li>
                    <b>Ollama:</b> runs the brain (LLM) only; ears and voice
                    always use the bundled Whisper + Kokoro. Leave the
                    endpoint blank to use the bundled Ollama, or point it at
                    your own server.
                  </li>
                  <li>
                    <b>vLLM:</b> ears, brain, and voice can each run on your
                    own server. vLLM has no built-in voice model, so voice
                    defaults to the bundled Whisper/Kokoro unless you point
                    it elsewhere.
                  </li>
                </ol>
                Expect slower, less polished turns than OpenAI, and a
                one-time model download on first use.
              </InfoIcon>
          }
        >
            {provider === "local" && (
              <div className="opt-extra">
                <div className="hint">
                  Uses the bundled Ollama, Whisper and Kokoro. Pick a backend or
                  point at your own servers under Advanced settings.
                </div>
              </div>
            )}
        </OptCard>
      </div>

      {/* Everything below is optional or pre-set by the template. A plain
          disclosure: closed by default, opens and closes on click. Anything
          inside that would block Test agent is announced by the tag on the
          header and, while closed, by the line under it. */}
      {advProblems.length > 0 && (
        <ul className="adv-problems" aria-label="Needs attention">
          {advProblems.map((p) => (
            <li key={p.text}>{p.text}</li>
          ))}
        </ul>
      )}
      <details
        className="adv adv-lg"
        open={advOpen}
        onToggle={(e) => setAdvOpen(e.currentTarget.open)}
      >
        <summary>
          Advanced settings
          {advSummary && <span className="adv-sum">{advSummary}</span>}
          {advProblems.length > 0 && (
            <span
              className="tag warn adv-warn"
              title={advProblems.map((p) => p.text).join("\n")}
            >
              {advProblems.length} need{advProblems.length === 1 ? "s" : ""}{" "}
              attention
            </span>
          )}
        </summary>

        {provider !== "groq" && (
          <>
            <h3 className="section-h sm">Models and voice{sectionTag("models")}</h3>
            {provider === "openai" && (
              <div style={{ marginBottom: 24 }}>
                <p className="hint" style={{ marginTop: -8, marginBottom: 10 }}>
                  Defaults are the recommended picks; change only if you know
                  you need to.
                </p>
                <div className="field-row">
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label className="label" htmlFor={sttId}>Ears (STT)</label>
                    <select
                      id={sttId}
                      className="select"
                      value={sttModel}
                      onChange={(e) => setSttModel(e.target.value)}
                    >
                      {catalogOptions(models?.stt, sttModel).map((m) => (
                        <option key={m.id} value={m.id} title={m.id}>
                          {m.label}{m.hint ? ` (${m.hint})` : ""}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label className="label" htmlFor={llmId}>Brain (LLM)</label>
                    <select
                      id={llmId}
                      className="select"
                      value={llmModel}
                      onChange={(e) => setLlmModel(e.target.value)}
                    >
                      {catalogOptions(models?.llm, llmModel).map((m) => (
                        <option key={m.id} value={m.id} title={m.id}>
                          {m.label}{m.hint ? ` (${m.hint})` : ""}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label className="label" htmlFor={ttsId}>Voice model (TTS)</label>
                    <select
                      id={ttsId}
                      className="select"
                      value={ttsModel}
                      onChange={(e) => setTtsModel(e.target.value)}
                    >
                      {catalogOptions(models?.tts, ttsModel).map((m) => (
                        <option key={m.id} value={m.id} title={m.id}>
                          {m.label}{m.hint ? ` (${m.hint})` : ""}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
                <div className="field" style={{ marginTop: 10, marginBottom: 0 }}>
                  <label className="label" htmlFor={voiceId}>Voice</label>
                  <div className="voice-row">
                  <select
                    id={voiceId}
                    className="select"
                    value={voice}
                    onChange={(e) => setVoice(e.target.value)}
                  >
                    {(() => {
                      const ttsEntry = models?.tts?.find((m) => m.id === ttsModel);
                      const list = ttsEntry?.voices || [];
                      // Keep an out-of-catalog voice visible until the clamp
                      // effect corrects it, instead of a blank selection.
                      const opts = voice && !list.includes(voice) ? [...list, voice] : list;
                      return opts.map((v) => (
                        <option key={v} value={v}>{v}</option>
                      ));
                    })()}
                  </select>
                  <VoicePreview provider="openai" model={ttsModel} voice={voice} keyLabel="your OpenAI" />
                  </div>
                </div>
              </div>
            )}
            {provider === "gemini" && (
              <div style={{ marginBottom: 24 }}>
                <p className="hint" style={{ marginTop: -8, marginBottom: 10 }}>
                  Defaults are the recommended picks; change only if you know
                  you need to.
                </p>
                <div className="field-row">
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label className="label" htmlFor={gSttId}>Ears (STT)</label>
                    {/* Fixed: Gemini has one streaming transcription model.
                        Rendered as a disabled select rather than omitted so
                        this row lines up with the OpenAI one. */}
                    <select
                      id={gSttId}
                      className="select"
                      value={geminiModels?.stt?.[0]?.id || ""}
                      disabled
                      title="Gemini has one streaming transcription model"
                    >
                      {(geminiModels?.stt || []).map((m) => (
                        <option key={m.id} value={m.id} title={m.id}>
                          {m.label}{m.hint ? ` (${m.hint})` : ""}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label className="label" htmlFor={gLlmId}>Brain (LLM)</label>
                    <select
                      id={gLlmId}
                      className="select"
                      value={geminiLlm}
                      onChange={(e) => setGeminiLlm(e.target.value)}
                    >
                      {catalogOptions(geminiModels?.llm, geminiLlm).map((m) => (
                        <option key={m.id} value={m.id} title={m.id}>
                          {m.label}{m.hint ? ` (${m.hint})` : ""}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label className="label" htmlFor={gTtsId}>Voice model (TTS)</label>
                    <select
                      id={gTtsId}
                      className="select"
                      value={geminiTts}
                      onChange={(e) => setGeminiTts(e.target.value)}
                    >
                      {catalogOptions(geminiModels?.tts, geminiTts).map((m) => (
                        <option key={m.id} value={m.id} title={m.id}>
                          {m.label}{m.hint ? ` (${m.hint})` : ""}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
                <div className="field" style={{ marginTop: 10, marginBottom: 0 }}>
                  <label className="label" htmlFor={gVoiceId}>Voice</label>
                  <div className="voice-row">
                  <select
                    id={gVoiceId}
                    className="select"
                    value={geminiVoice}
                    onChange={(e) => setGeminiVoice(e.target.value)}
                  >
                    {(() => {
                      const list = geminiModels?.voices || [];
                      // Keep a config voice that left the catalog visible
                      // rather than rendering a blank selection.
                      const opts =
                        geminiVoice && !list.includes(geminiVoice)
                          ? [...list, geminiVoice]
                          : list;
                      return opts.map((v) => (
                        <option key={v} value={v}>{v}</option>
                      ));
                    })()}
                  </select>
                  <VoicePreview provider="gemini" model={geminiTts} voice={geminiVoice} keyLabel="your Google" />
                  </div>
                </div>
                <label className="check-row" style={{ marginTop: 12 }}>
                  <input
                    type="checkbox"
                    checked={geminiExpressive}
                    onChange={(e) => setGeminiExpressive(e.target.checked)}
                  />
                  <span>
                    <b>Expressive delivery</b> (beta): natural pauses, mood and
                    pacing in the voice. Gemini 3.8 voice models only; the
                    transcript stays clean.
                  </span>
                </label>
              </div>
            )}
            {provider === "local" && (
              <div style={{ marginBottom: 24 }}>
                <label className="label" htmlFor={backendId}>Backend</label>
                <select
                  id={backendId}
                  className="select"
                  value={localBackend}
                  onChange={(e) => setLocalBackend(e.target.value)}
                >
                  {(localBackends || [{ id: "ollama", label: "Ollama" }]).map((b) => (
                    <option key={b.id} value={b.id}>{b.label}</option>
                  ))}
                </select>
                {backendEntry?.note && (
                  <div className="hint" style={{ marginTop: 4 }}>{backendEntry.note}</div>
                )}

                <div className="field-row" style={{ marginTop: 14 }}>
                  <LocalStageBox
                    label="Ears (STT)"
                    stage={backendEntry?.stt}
                    url={vllmSttUrl}
                    model={vllmSttModel}
                    onUrl={setVllmSttUrl}
                    onModel={setVllmSttModel}
                    // Keyed by backend too (not just stage) — belt and
                    // suspenders on top of the forUrl guard, so a check
                    // from one backend can never even be looked up while
                    // the other backend's (structurally unrelated) box
                    // is showing.
                    checkState={localCheck[`${localBackend}_stt`]}
                    onCheck={() => checkLocal(`${localBackend}_stt`, vllmSttUrl)}
                  />
                  <LocalStageBox
                    label="Brain (LLM)"
                    stage={backendEntry?.llm}
                    url={localBackend === "vllm" ? vllmLlmUrl : ollamaLlmUrl}
                    model={localBackend === "vllm" ? vllmLlmModel : ollamaLlmModel}
                    onUrl={localBackend === "vllm" ? setVllmLlmUrl : setOllamaLlmUrl}
                    onModel={localBackend === "vllm" ? setVllmLlmModel : setOllamaLlmModel}
                    checkState={localCheck[`${localBackend}_llm`]}
                    onCheck={() =>
                      checkLocal(
                        `${localBackend}_llm`,
                        localBackend === "vllm" ? vllmLlmUrl : ollamaLlmUrl
                      )
                    }
                  />
                  <LocalStageBox
                    label="Voice model (TTS)"
                    preview={<VoicePreview provider="local" model="kokoro" voice="af_heart" />}
                    stage={backendEntry?.tts}
                    url={vllmTtsUrl}
                    model={vllmTtsModel}
                    onUrl={setVllmTtsUrl}
                    onModel={setVllmTtsModel}
                    checkState={localCheck[`${localBackend}_tts`]}
                    onCheck={() => checkLocal(`${localBackend}_tts`, vllmTtsUrl)}
                  />
                </div>
              </div>
            )}
          </>
        )}

      <h3 className="section-h sm">Knowledge base (RAG){sectionTag("rag")}</h3>
      {/* The website is knowledge too: its homepage text becomes the
          business-context block of the system prompt. Nothing changes until
          the button is pressed, and nothing is saved until Update. */}
      <div className="field form-narrow">
        <label className="label" htmlFor={websiteId}>
          Website <span className="label-opt">Optional</span>
        </label>
        <div className="key-row">
          <input
            id={websiteId}
            className="input"
            type="url"
            inputMode="url"
            placeholder="e.g. peachtree.com"
            value={website}
            onChange={(e) => setWebsite(e.target.value)}
            disabled={siteBusy}
          />
          <button
            type="button"
            className="btn ghost slim"
            onClick={applyWebsite}
            disabled={siteBusy || (!website.trim() && !websiteContextText)}
          >
            {siteBusy
              ? "Reading your website…"
              : website.trim()
                ? "Fetch and update prompt"
                : "Remove from prompt"}
          </button>
        </div>
        <div className="hint">
          The homepage text becomes business context in the system prompt
          below. Fetch again after the site changes.
        </div>
        {siteNote && (
          <div className="hint" role="status">
            {siteNote}
          </div>
        )}
      </div>
      <div className="opt-group" style={{ marginBottom: 24 }}>
        <OptCard
          on={ragOn}
          inputId="opt-rag"
          onSelect={() => setRagOn((v) => !v)}
          input={
            <input
              id="opt-rag"
              type="checkbox"
              checked={ragOn}
              onChange={(e) => setRagOn(e.target.checked)}
            />
          }
          title="Answer from my documents"
          info={
              <InfoIcon>
                Upload PDFs, Word docs, or text; the agent answers from them
                via grounding.
                <br /><br />
                Supported formats:{" "}
                {(cfg.rag?.allowed_formats || limits?.allowed_formats || [])
                  .map((f) => f.replace(".", ""))
                  .join(", ") || "…"}
                <br />
                Allowed file size: Up to {limits?.max_upload_mb ?? "…"} MB per file
                <br />
                Allowed documents: Up to {limits?.max_docs_per_agent ?? "…"} per agent
                <br />
                Note: Deleting a document also removes its chunks from the
                knowledge base.
              </InfoIcon>
          }
        >
            {ragOn && (
              <div className="opt-extra">
                <label className="label" htmlFor={embId}>Embeddings</label>
                <select
                  id={embId}
                  className="select"
                  value={embProfile}
                  onChange={(e) => setEmbProfile(e.target.value)}
                >
                  {(embProfiles.length
                    ? embProfiles
                    : [{ id: embProfile, label: embProfile }]
                  ).map((p) => (
                    <option key={p.id} value={p.id}>{p.label}</option>
                  ))}
                </select>
                {(() => {
                  const needsKey = embProfiles.find(
                    (p) => p.id === embProfile
                  )?.needs_key;
                  return needsKey && !hasKey(needsKey) ? (
                    <div className="opt-extra">
                      <ApiKeyField
                        label={
                          needsKey === "openai"
                            ? "OpenAI API key (for embeddings)"
                            : `${needsKey} API key (for embeddings)`
                        }
                        name={needsKey}
                        masked={masked[needsKey]}
                        onSave={saveKey}
                        onPending={keyPending}
                      />
                      {bundledEmbProfile !== embProfile && (
                        <div className="hint turnoff">
                          <button
                            type="button"
                            className="btn-link"
                            onClick={() => setEmbProfile(bundledEmbProfile)}
                          >
                            No key? Switch to the bundled profile
                          </button>
                          <span>
                            {" "}Documents are embedded locally instead; no key
                            needed.
                          </span>
                        </div>
                      )}
                    </div>
                  ) : null;
                })()}
                <div className="opt-extra">
                  <KbUpload
                    agentId={cfg.id}
                    limits={limits}
                    formats={cfg.rag?.allowed_formats || limits?.allowed_formats}
                  />
                </div>
              </div>
            )}
        </OptCard>
      </div>

      <h3 className="section-h sm">Web search{sectionTag("search")}</h3>
      <div className="opt-group" style={{ marginBottom: 24 }}>
        <OptCard
          on={searchOn}
          inputId="opt-search"
          onSelect={() => setSearchOn((v) => !v)}
          input={
            <input
              id="opt-search"
              type="checkbox"
              checked={searchOn}
              onChange={(e) => setSearchOn(e.target.checked)}
            />
          }
          title="Search the live web"
          info={
              <InfoIcon>
                Current prices, news, opening hours, pick a provider (free
                tiers available):
                <ol>
                  <li>
                    <b>SerpAPI:</b> login to{" "}
                    <a href="https://serpapi.com/" target="_blank" rel="noreferrer">
                      serpapi.com
                    </a>
                    , go to API key, generate one, paste it in the box.
                  </li>
                  <li>
                    <b>Tavily:</b> search built for AI agents. Login to{" "}
                    <a href="https://app.tavily.com/" target="_blank" rel="noreferrer">
                      app.tavily.com
                    </a>
                    , copy your API key (or create one), paste it in the box.
                  </li>
                  <li>
                    <b>Ares by Traversaal.ai:</b> login to{" "}
                    <a href="https://api.traversaal.ai/" target="_blank" rel="noreferrer">
                      api.traversaal.ai
                    </a>
                    , create an API key, paste it in the box.
                  </li>
                </ol>
              </InfoIcon>
          }
        >
            {searchOn && (
              <div className="opt-extra">
                <label className="label" htmlFor={searchProvId}>Provider</label>
                <select
                  id={searchProvId}
                  className="select"
                  value={searchProv}
                  onChange={(e) => setSearchProv(e.target.value)}
                >
                  {(searchProviders.length
                    ? searchProviders
                    : [{ id: searchProv, label: searchProv }]
                  ).map((p) => (
                    <option key={p.id} value={p.id}>{p.label}</option>
                  ))}
                </select>
                <div className="opt-extra">
                  {/* Plain text, not a filled input: a read-only box here was
                      read as a stored credential. The value is fixed per
                      provider and never editable. */}
                  <span className="label">
                    Endpoint URL <span className="tag planned">fixed</span>
                  </span>
                  <div className="fixed-value" id={endpointId}>
                    {searchProviders.find((p) => p.id === searchProv)
                      ?.endpoint || ""}
                  </div>
                  <span className="sr-only">
                    Fixed for this provider, not editable.
                  </span>
                </div>
                <div className="opt-extra">
                  {(() => {
                    const sp = searchProviders.find((p) => p.id === searchProv);
                    const keyName = sp?.key || searchProv;
                    return (
                      <>
                        <ApiKeyField
                          key={keyName}
                          label={`${sp?.label || searchProv} API key`}
                          name={keyName}
                          masked={masked[keyName]}
                          onSave={saveKey}
                          onPending={keyPending}
                        />
                        {setupLoaded && !hasKey(keyName) && (
                          <div className="hint turnoff">
                            <button
                              type="button"
                              className="btn-link"
                              onClick={() => setSearchOn(false)}
                            >
                              No key? Turn web search off
                            </button>
                            {isTemplate && (
                              <span>
                                {" "}This template expects web search; without
                                it the agent answers from its prompt and
                                documents only.
                              </span>
                            )}
                          </div>
                        )}
                      </>
                    );
                  })()}
                </div>
              </div>
            )}
        </OptCard>
      </div>

      <h3 className="section-h sm">Connectors{sectionTag("connectors")}</h3>
      <p className="hint" style={{ marginTop: -8, marginBottom: 10 }}>
        Let the agent act on your real systems during a conversation. Connect once,
        then attach to any agent and pick the tools it may use.{" "}
        <a href="#/docs/connectors" target="_blank" rel="noreferrer">
          Setup guide
        </a>
      </p>
      {connErr && (
        <ErrorNote
          message={connErr}
          onRetry={() => {
            loadConnectors();
            loadConnections();
          }}
          style={{ marginBottom: 12 }}
        />
      )}
      {connNote && (
        <div className="hint" role="status" style={{ marginBottom: 10 }}>
          {connNote}
        </div>
      )}
      <div className="opt-group" style={{ marginBottom: 24 }}>
        {(() => {
          const renderConnector = (type) => {
          const rows = connections.filter((c) => c.type === type.id);
          let setup = null;
          let connectDisabled = "";
          // Sidecar types need the arya-mcp service running (it hosts the
          // MCP server Arya provides itself). Say which command starts it
          // rather than failing on Save.
          if (type.url_source === "sidecar" && type.sidecar_ready === false) {
            connectDisabled =
              "Start the arya-mcp service first: docker compose up -d arya-mcp";
          }
          if (type.auth === "oauth") {
            // The OAuth app (client id + secret) lives in keys.json like a
            // provider key; the redirect URI is what goes into the
            // provider's console. Collapsed once both keys are saved.
            const k = type.oauth?.keys || {};
            const ready = hasKey(k.client_id) && hasKey(k.client_secret);
            if (!ready) connectDisabled = "Add the OAuth client id and secret first.";
            setup = (
              <Collapsible
                key={type.id}
                defaultOpen={!ready}
                summary="OAuth app (from your Google Cloud project)"
              >
                <div className="opt-extra">
                  <ApiKeyField
                    label={`${type.label} client id`}
                    name={k.client_id}
                    masked={masked[k.client_id]}
                    onSave={saveKey}
                    onPending={keyPending}
                  />
                </div>
                <div className="opt-extra">
                  <ApiKeyField
                    label={`${type.label} client secret`}
                    name={k.client_secret}
                    masked={masked[k.client_secret]}
                    onSave={saveKey}
                    onPending={keyPending}
                  />
                </div>
                {redirectUri && (
                  <div className="opt-extra">
                    <CopyField
                      label="Redirect URI (add it to the OAuth client)"
                      value={redirectUri}
                    />
                  </div>
                )}
              </Collapsible>
            );
          }
          return (
            <ConnectorCard
              key={type.id}
              type={type}
              connections={rows}
              attachments={attach}
              busyId={connBusy}
              setup={setup}
              connectDisabled={connectDisabled}
              onAdd={() => connect(type)}
              onSignIn={(row) => startOauth(type, row)}
              onTest={testConn}
              onRemove={setRemoveConn}
              onAttach={attachConn}
              onDetach={detachConn}
              onTools={setConnTools}
              onPublic={setConnPublic}
              onPurpose={setConnPurpose}
              stubToolsets={stubToolsetsFor(type)}
              wanted={isWantedAndMissing(type)}
            />
          );
          };
          // Group by the job a connector does (catalog category), in the
          // catalog's order. Groups the template asks for, or that already
          // have an attachment, come first and open; the rest fold under
          // "More integrations". With nothing to prioritise (a scratch
          // agent, nothing attached) every group shows open.
          const DEFAULT_ORDER = [
            "Bookings", "Flights and hotels", "Places and routes",
            "Payments", "Your data", "Anything else",
          ];
          const order = connectorCategories.length ? connectorCategories : DEFAULT_ORDER;
          const groups = new Map();
          for (const t of connectorTypes) {
            const cat = t.category || "Anything else";
            if (!groups.has(cat)) groups.set(cat, []);
            groups.get(cat).push(t);
          }
          const names = [
            ...order.filter((c) => groups.has(c)),
            ...[...groups.keys()].filter((c) => !order.includes(c)),
          ];
          const attachedTypes = new Set(
            attach
              .map((a) => connections.find((c) => c.id === a.connection_id)?.type)
              .filter(Boolean)
          );
          const relevant = (cat) =>
            groups.get(cat).some((t) => wantedTypes.includes(t.id) || attachedTypes.has(t.id));
          const primary = names.filter(relevant);
          const more = primary.length ? names.filter((c) => !relevant(c)) : [];
          const shown = primary.length ? primary : names;
          const group = (cat) => (
            <div className="conn-group" key={cat}>
              <span className="eyebrow">{cat}</span>
              {groups.get(cat).map(renderConnector)}
            </div>
          );
          return (
            <>
              {shown.map(group)}
              {more.length > 0 && (
                <details className="adv conn-more">
                  <summary>
                    More integrations ({more.reduce((n, c) => n + groups.get(c).length, 0)})
                  </summary>
                  {more.map(group)}
                </details>
              )}
            </>
          );
        })()}
        {connectorTypes.length === 0 && !connErr && (
          <div className="hint">Loading connector types…</div>
        )}
      </div>

      <h3 className="section-h sm">Memory{sectionTag("memory")}</h3>
      <div className="opt-group" style={{ marginBottom: 8 }}>
        <OptCard
          on={memoryOn}
          inputId="opt-memory"
          onSelect={() => setMemoryOn((v) => !v)}
          input={
            <input
              id="opt-memory"
              type="checkbox"
              checked={memoryOn}
              onChange={(e) => setMemoryOn(e.target.checked)}
            />
          }
          title="Remember callers between conversations"
          info={
              <InfoIcon>
                Preferences, past requests, and details carry over to the next
                conversation, via Mem0 (free tier available).
                <ol>
                  <li>
                    Login to{" "}
                    <a href="https://app.mem0.ai/" target="_blank" rel="noreferrer">
                      app.mem0.ai
                    </a>
                  </li>
                  <li>Go to API keys</li>
                  <li>Create a new api key, set it's name and save it</li>
                  <li>Copy the API key and paste in the box</li>
                </ol>
              </InfoIcon>
          }
        >
            {memoryOn && (
              <div className="opt-extra">
                <ApiKeyField
                  label="Mem0 API key"
                  name="mem0"
                  masked={masked.mem0}
                  onSave={saveKey}
                  onPending={keyPending}
                />
                {setupLoaded && !hasKey("mem0") && (
                  <div className="hint turnoff">
                    <button
                      type="button"
                      className="btn-link"
                      onClick={() => setMemoryOn(false)}
                    >
                      No key? Turn memory off
                    </button>
                    <span>
                      {" "}Callers will not be recognised on their next
                      conversation.
                    </span>
                  </div>
                )}
              </div>
            )}
        </OptCard>
        {/* Plain note, not .warn: choosing the simpler option isn't a
            warning, and amber for "you picked the default" trains people to
            ignore the styling that does matter. */}
        {!memoryOn && (
          <div className="note">
            Without memory, every conversation starts from zero: the agent
            won't recognize returning callers or remember their preferences,
            so they'll repeat themselves on every conversation.
          </div>
        )}
      </div>

      <h3 className="section-h sm">Evaluation{sectionTag("evals")}</h3>
      <div className="opt-group" style={{ marginBottom: 8 }}>
        <OptCard
          on={evalsOn}
          inputId="opt-evals"
          onSelect={() => setEvalsOn((v) => !v)}
          input={
            <input
              id="opt-evals"
              type="checkbox"
              checked={evalsOn}
              onChange={(e) => setEvalsOn(e.target.checked)}
            />
          }
          title="Evaluate my agent's answers"
          info={
              <InfoIcon>
                Free measurements are always on: response speed, whether
                answers used your documents, and how calls end. See them
                under <b>Insights</b> in the ⋮ menu. No cost, no setup.
                <br /><br />
                This checkbox adds an <b>AI judge</b>: after each conversation, the
                transcript is scored on the checks you pick below, with
                written reasons. Scores show on each transcript and as a
                quality trend on Insights. If documents (RAG) are on, the
                per-answer call also sees the excerpts the agent was given
                and judges faithfulness to them, whether they fit the
                question, and whether the answer drew on them, at no extra
                cost.
                <br /><br />
                Cost honesty: one small LLM call per check per conversation,
                plus one per answer for accuracy and relevancy (they are
                judged answer by answer so a single wrong fact cannot hide
                in an average). Typically a cent or less per conversation. The judge
                runs on OpenAI, Groq or Gemini, with a backup model that
                retries any check the first one fails. Local models can't
                run the judge reliably.
              </InfoIcon>
          }
        >
            {evalsOn && (
              <div className="opt-extra">
                {/* "Judge runs on" leaked the internal LLM-as-a-judge term
                    into the UI; the surrounding card already says this is
                    the evaluation, so the field only needs to name what it
                    picks. */}
                <label className="label" htmlFor={evalProvId}>Provider</label>
                <select
                  id={evalProvId}
                  className="select"
                  value={evalJudgeProv}
                  onChange={(e) => setEvalJudgeProv(e.target.value)}
                >
                  <option value="agent">
                    Same as agent{provider !== "local"
                      ? ` (${JUDGE_PROVIDER_LABEL[provider] || provider})`
                      : ""}
                  </option>
                  <option value="openai">OpenAI</option>
                  <option value="groq">Groq</option>
                  <option value="gemini">Gemini</option>
                </select>
                {(() => {
                  const judge =
                    evalJudgeProv === "agent" ? provider : evalJudgeProv;
                  if (judge === "local") {
                    return (
                      <div className="note warn" style={{ marginTop: 10 }}>
                        The AI judge can't run on local models. Pick OpenAI,
                        Groq or Gemini above (that key is used only for scoring,
                        your agent stays fully local), or turn this off.
                        The free measurements on Insights work either way.
                      </div>
                    );
                  }
                  return hasKey(judge) ? (
                    <div className="hint" style={{ marginTop: 6 }}>
                      Uses your saved {JUDGE_PROVIDER_LABEL[judge]} key. No
                      extra setup.
                    </div>
                  ) : (
                    <div className="opt-extra">
                      <ApiKeyField
                        label={`${JUDGE_PROVIDER_LABEL[judge]} API key (for the judge)`}
                        name={judge}
                        masked={masked[judge]}
                        onSave={saveKey}
                        onPending={keyPending}
                      />
                      <div className="hint turnoff">
                        <button
                          type="button"
                          className="btn-link"
                          onClick={() => setEvalsOn(false)}
                        >
                          No key? Turn evaluation off
                        </button>
                        <span>
                          {" "}Conversations will not be scored; the free
                          measurements on the Dashboard stay.
                        </span>
                      </div>
                    </div>
                  );
                })()}
                <div className="opt-extra check-grid">
                  <span className="label">Quality checks</span>
                  {EVAL_JUDGES.map((j) => (
                    <label className="check-row" key={j.id}>
                      <input
                        type="checkbox"
                        checked={evalJudges.includes(j.id)}
                        onChange={(e) => {
                          setEvalJudges((prev) => {
                            if (e.target.checked) return [...prev, j.id];
                            // Never allow zero checks: an enabled judge
                            // with nothing to judge is a config error the
                            // server would reject anyway.
                            const next = prev.filter((id) => id !== j.id);
                            return next.length ? next : prev;
                          });
                        }}
                      />
                      <span>
                        <b>{j.label}</b>
                        <span className="check-hint"> · {j.hint}</span>
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            )}
        </OptCard>
      </div>

      <h3 className="section-h sm">System prompt{sectionTag("prompt")}</h3>
      <div className="field">
        <label className="label" htmlFor={promptId}>
          Instructions
        </label>
        <textarea
          id={promptId}
          className="textarea mono"
          aria-describedby={promptHintId}
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          placeholder="Generating from your answers…"
        />
        <div className="hint" id={promptHintId}>
          Generated from your answers, edit freely. This is the agent's
          entire personality and rulebook.{" "}
          <code>{"{agent_name}"}</code> and <code>{"{business_name}"}</code>{" "}
          are filled from the identity fields on every conversation, so renaming
          never needs a prompt edit. Plain text without them works too.
        </div>
      </div>
      </details>

      {err && <ErrorNote message={err} onRetry={next} retryLabel="Try again" busy={busy} />}

      <div className="btn-row">
        <button className="btn ghost" onClick={back}>Back</button>
        <button
          className="btn"
          onClick={next}
          disabled={
            busy || !instructions.trim() || !name.trim() || !businessName.trim()
          }
        >
          {busy ? "Saving…" : mode === "edit" ? "Update" : "Continue"}
        </button>
      </div>

      <ConfirmDialog
        open={guard === "continue"}
        title="Save API key changes?"
        body="You have unsaved API key changes. They'll be saved before continuing."
        confirmLabel={mode === "edit" ? "Save & update" : "Save & continue"}
        busy={busy}
        onConfirm={proceed}
        onCancel={() => setGuard(null)}
      />
      <ConfirmDialog
        open={guard === "back"}
        title="Discard unsaved changes?"
        body="Your typed API key changes haven't been saved and will be lost."
        confirmLabel="Discard"
        danger
        onConfirm={() => {
          setGuard(null);
          onBack();
        }}
        onCancel={() => setGuard(null)}
      />
      <ConfirmDialog
        open={guard === "connect"}
        title="Save API key changes?"
        body="Your typed key changes will be saved before signing in."
        confirmLabel="Save & sign in"
        busy={busy}
        onConfirm={confirmConnect}
        onCancel={() => setGuard(null)}
      />
      {/* key= remounts the dialog per type so its fields start clean. */}
      <AddConnectionDialog
        key={addFor?.id || "none"}
        type={addFor}
        open={!!addFor}
        busy={!!addFor && connBusy === addFor.id}
        error={addErr}
        onSave={saveNewConnection}
        onCancel={() => setAddFor(null)}
      />
      <ConfirmDialog
        open={!!removeConn}
        title={`Remove "${removeConn?.name}"?`}
        body={(() => {
          const others = (removeConn?.attached_agents || []).filter((a) => a.id !== cfg.id);
          return others.length
            ? `Also used by ${others.map((a) => a.name).join(", ")}. It will be detached from those agents too, and its saved token or sign-in is deleted.`
            : "Its saved token or sign-in is deleted. Agents that used it lose these tools.";
        })()}
        confirmLabel="Remove"
        danger
        busy={!!removeConn && connBusy === removeConn.id}
        onConfirm={() => removeConnNow(removeConn)}
        onCancel={() => setRemoveConn(null)}
      />
    </div>
  );
}
