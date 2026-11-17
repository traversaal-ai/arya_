import { useCallback, useEffect, useState } from "react";
import { deleteAgent, getModels, listConnections, putActive } from "../api.js";
import StepDots from "../components/StepDots.jsx";
import ConfirmDialog from "../components/ConfirmDialog.jsx";
import ErrorNote from "../components/ErrorNote.jsx";
import { IndustryIcon } from "../components/AppIcons.jsx";
import { renderVars } from "../promptVars.js";
// Same lucide family as the identity icons — passed the existing `ic map`
// classes so the map's sizing/dimming CSS applies unchanged.
import {
  Calendar,
  CalendarCheck,
  CalendarClock,
  ClipboardCheck,
  Cloud,
  Cpu,
  Database,
  FileText,
  MessageSquare,
  Package,
  Plane,
  Play,
  Plug,
  Search,
} from "lucide-react";

const PROVIDER_LABEL = {
  openai: "OpenAI",
  groq: "Groq · open models",
  gemini: "Gemini",
};

// Icon names the tool_catalog may reference, resolved against the lucide
// set already imported above. Unknown names get the generic cog.
const TOOL_ICONS = {
  plane: Plane,
  "calendar-clock": CalendarClock,
  "calendar-check": CalendarCheck,
  cloud: Cloud,
  package: Package,
  // connector types (tool_catalog "connector:<type>" entries)
  calendar: Calendar,
  database: Database,
  "message-square": MessageSquare,
  plug: Plug,
  _default: Cpu,
};

const CONN_STATUS = {
  connected: "connected",
  needs_auth: "needs sign-in",
  pending_auth: "needs sign-in",
  error: "error",
};

// Read-only review: the saved config rendered as a vertical workflow map
// (Agent → Brain → Abilities). All editing happens on Options.
export default function Preview({
  cfg,
  onBack,
  onUpdate,
  onStart,
  onDeleted,
  onPublish,
}) {
  const [problems, setProblems] = useState([]);
  const [saving, setSaving] = useState(true);
  const [busy, setBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [err, setErr] = useState("");
  // Model defaults for display only — a config may omit any model field
  // and inherit the server's default; GET /api/models (the catalog store)
  // is the single source of truth for what it actually falls back to.
  // `catalogs` carries all of it: defaults_by_provider, search, local.
  const [modelDefaults, setModelDefaults] = useState(null);
  const [catalogs, setCatalogs] = useState(null);
  // Connections the attached connectors point at (name, status, tool
  // descriptions). Display only; a failed load just shows the ids.
  // null until the fetch settles: an empty array would be
  // indistinguishable from "every one of them is gone", and the row
  // below would announce a healthy connector as removed.
  const [connections, setConnections] = useState(null);

  // Save on mount so the map always shows what will actually run
  // (PUT is idempotent — a StrictMode double-mount is harmless).
  const commit = useCallback(() => {
    setSaving(true);
    setErr("");
    return putActive({ config: cfg })
      .then((res) => {
        setProblems(res.problems || []);
        onUpdate(res.config);
      })
      .catch((e) => setErr(`Couldn't save this agent: ${e.message}`))
      .finally(() => setSaving(false));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    commit();
  }, [commit]);

  useEffect(() => {
    getModels()
      .then((m) => {
        setModelDefaults(m.defaults);
        setCatalogs(m);
      })
      .catch(() => setModelDefaults({}));
  }, []);

  useEffect(() => {
    if (!(cfg.connectors || []).length) return;
    listConnections()
      .then(setConnections)
      .catch(() => setConnections([]));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function test() {
    setBusy(true);
    setErr("");
    try {
      const res = await putActive({ config: cfg });
      setProblems(res.problems || []);
      if ((res.problems || []).length) {
        setBusy(false);
        return; // fix keys first — the worker would fail mid-call
      }
      onUpdate(res.config);
      // No room yet — the call window opens in a ready state and the
      // conversation is created when the green call button is pressed.
      onStart();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setErr("");
    try {
      const res = await deleteAgent(cfg.id);
      // Deleting also unpublishes on connected engines. One that couldn't
      // be reached is still serving this agent, and the agent is gone from
      // here, so the only place left to say so is Home.
      const failed = (res?.engines || []).filter((e) => !e.ok);
      onDeleted(
        failed.length > 0
          ? `"${cfg.name}" was deleted here, but it is still published on ${failed
              .map((e) => e.name)
              .join(", ")}. Open Deployments, pick that engine as the source, then unpublish it there.`
          : undefined
      );
    } catch (e) {
      setErr(e.message);
      setBusy(false);
      setConfirmOpen(false);
    }
  }

  const t = cfg.tools || {};
  const connectorTypes = catalogs?.connectors || [];
  const typeById = (id) => connectorTypes.find((c) => c.id === id);
  // Sample toolsets an attached connector has taken over, derived from the
  // connector TYPE's `overlaps`, exactly as connectors.replaced_toolsets
  // does worker side. Attachments saved before the type was recorded carry
  // the old hand-ticked `replaces` list instead.
  // A connection can SERVE a sample desk instead of replacing it: the
  // type's `keeps_when` names a flag the check set on the connection (a
  // database with the order desk's own tables is `shop_desk`), read here
  // exactly as connectors.served_toolsets reads it worker side.
  const servedBy = {};
  (cfg.connectors || []).forEach((a) => {
    const row = connections?.find((c) => c.id === a.connection_id);
    Object.entries(typeById(a.type)?.keeps_when || {}).forEach(([k, flag]) => {
      if (row?.[flag] && !servedBy[k]) servedBy[k] = row;
    });
  });
  const replacedToolsets = new Set(
    (cfg.connectors || [])
      .flatMap((a) => (a.type ? typeById(a.type)?.overlaps || [] : a.replaces || []))
      .filter((k) => !servedBy[k])
  );
  // Toolsets that invent their data: any a real connector could take over
  // is, by definition, the demo version of something real. Marked as such
  // so nobody publishes an agent that books into nothing believing it works.
  const sampleToolsets = new Set(
    connectorTypes.flatMap((c) => c.overlaps || [])
  );
  // The connector that would make a given sample desk real, named on the
  // hint. "Cal.com (appointment booking)" reads better trimmed to "Cal.com".
  const realiserFor = (k) => {
    const hit = connectorTypes.find((c) => (c.overlaps || []).includes(k));
    return (hit?.label || "").split(" (")[0];
  };
  // Open source has two non-symmetric backends (see providers.py): Ollama
  // overrides only the LLM (STT/TTS always the bundled Speaches endpoint);
  // vLLM can override all three, each falling back to bundled Speaches too.
  // Every fallback below comes from the server's catalog (never re-typed
  // here) — "…" only while /api/models is still loading.
  const localBackend = cfg.local?.backend || "ollama";
  const localVllm = cfg.local?.vllm || {};
  const groqDefaults = catalogs?.defaults_by_provider?.groq || {};
  const localDefaults = catalogs?.defaults_by_provider?.local || {};
  // Gemini keeps its models in their own config group (see Options.jsx),
  // so nothing here reads cfg.stt/llm/tts for it.
  const geminiDefaults = catalogs?.defaults_by_provider?.gemini || {};
  const stt =
    cfg.provider === "groq"
      ? cfg.groq?.stt_model || groqDefaults.stt_model || "…"
      : cfg.provider === "gemini"
        ? geminiDefaults.stt_model || "…"
      : cfg.provider === "local"
        ? (localBackend === "vllm" && localVllm.stt_model) ||
          cfg.local?.stt_model || localDefaults.stt_model || "…"
        : cfg.stt?.model || modelDefaults?.stt_model || "…";
  const model =
    cfg.provider === "groq"
      ? cfg.groq?.llm_model || groqDefaults.llm_model || "…"
      : cfg.provider === "gemini"
        ? cfg.gemini?.llm_model || geminiDefaults.llm_model || "…"
      : cfg.provider === "local"
        ? localBackend === "vllm"
          ? localVllm.llm_model || "(vLLM model not set)"
          : cfg.local?.ollama?.llm_model || cfg.local?.llm_model ||
            localDefaults.llm_model || "…"
        : cfg.llm?.model || modelDefaults?.llm_model || "…";
  const ttsModel =
    cfg.provider === "groq"
      ? cfg.groq?.tts_model || groqDefaults.tts_model || "…"
      : cfg.provider === "gemini"
        ? cfg.gemini?.tts_model || geminiDefaults.tts_model || "…"
      : cfg.provider === "local"
        ? (localBackend === "vllm" && localVllm.tts_model) ||
          localDefaults.speaches_tts_model || "…"
        : cfg.tts?.model || modelDefaults?.tts_model || "…";
  const voice =
    cfg.provider === "groq"
      ? cfg.groq?.voice || groqDefaults.voice || "…"
      : cfg.provider === "gemini"
        ? cfg.gemini?.voice || geminiDefaults.voice || "…"
      : cfg.provider === "local"
        ? cfg.local?.voice || localDefaults.voice || "…"
        : cfg.tts?.voice || modelDefaults?.voice || "…";
  const searchLabel =
    catalogs?.search?.find((p) => p.id === cfg.search?.provider)?.label ||
    cfg.search?.provider || "…";
  const localBackendLabel =
    catalogs?.local?.find((b) => b.id === localBackend)?.label ||
    localBackend;
  // Shown as it will be spoken: {business_name} etc. filled in.
  const greeting = renderVars((cfg.greeting || "").trim(), cfg);

  return (
    <div>
      <h1 className="h-serif">
        Preview your <em>agent</em>
      </h1>
      <p className="sub">
        This is exactly what will run. Test it live, or go back to edit.
      </p>
      {cfg.template_id === "custom" && <StepDots total={5} current={5} />}

      {/* The site's stacked, tinted cards: each node steps right and
          overlaps the one above, so the three read as one assembled agent. */}
      <div className="map">
        <div className="map-node sand">
          <div className="map-kicker">Agent</div>
          <div className="map-title">
            <span className="map-ico">
              <IndustryIcon value={cfg.business?.industry} size={17} />
            </span>
            {cfg.name || "My Voice Agent"}
          </div>
          <div className="map-sub">
            {greeting ? `“${greeting}”` : "Improvises a greeting from its prompt"}
          </div>
        </div>

        <div className="map-node sage">
          <div className="map-kicker">Brain</div>
          <div className="map-title">
            <span className="map-ico"><Cpu className="ic map" aria-hidden="true" /></span>
            {cfg.provider === "local"
              ? `Open source · ${localBackendLabel}`
              : PROVIDER_LABEL[cfg.provider] || cfg.provider}
          </div>
          <div className="map-sub">Ears: {stt}</div>
          <div className="map-sub">Brain: {model}</div>
          <div className="map-sub">Voice: {ttsModel} · {voice}</div>
        </div>

        <div className="map-node sky">
          <div className="map-kicker">Abilities</div>
          <div className={cfg.rag?.enabled ? "map-row on" : "map-row"}>
            <span className="map-ico"><FileText className="ic map" aria-hidden="true" /></span> Documents (RAG)
            <span className="map-val">
              {cfg.rag?.enabled ? `on · ${cfg.rag.embedding_profile}` : "off"}
            </span>
          </div>
          <div className={t.web_search ? "map-row on" : "map-row"}>
            <span className="map-ico"><Search className="ic map" aria-hidden="true" /></span> Web search
            <span className="map-val">
              {t.web_search ? `on · ${searchLabel}` : "off"}
            </span>
          </div>
          <div className={cfg.memory?.enabled ? "map-row on" : "map-row"}>
            <span className="map-ico"><Database className="ic map" aria-hidden="true" /></span> Memory
            <span className="map-val">{cfg.memory?.enabled ? "on" : "off"}</span>
          </div>
          <div className={cfg.evals?.enabled ? "map-row on" : "map-row"}>
            <span className="map-ico"><ClipboardCheck className="ic map" aria-hidden="true" /></span> Evaluation
            <span className="map-val">
              {cfg.evals?.enabled ? "on · AI judge" : "off"}
            </span>
          </div>
          {/* Toolset rows are data-driven from the tool_catalog catalog
              (GET /api/models .tools): heading + one sub-row per meaningful
              capability, helpers folded into the count. Toggles the catalog
              doesn't know (or before it loads) fall back to a plain
              consolidated row so the map never goes blank. */}
          {Object.entries(t)
            .filter(([k, on]) => on && !["web_search"].includes(k))
            .map(([k]) => {
              const entry = catalogs?.tools?.[k];
              const Icon = TOOL_ICONS[entry?.icon] || TOOL_ICONS._default;
              const served = servedBy[k];
              const rows = (served && entry?.live_rows) || entry?.rows || [];
              // A connector marked as replacing this sample toolset means it
              // is not built for the call, even though the toggle is still
              // true. It used to get a row marked "replaced" so an owner
              // could see where the demo tools went; that is informative
              // once and noise after, and it made this list longer than what
              // the agent can actually do. Options is where the owner learns
              // a connector is replacing something, next to the checkbox
              // that says so. Untick it there and the row comes back.
              if (replacedToolsets.has(k)) return null;
              // No count on the heading: the rows are grouped capabilities,
              // not a 1:1 tool list, so any number would mislead.
              const isSample = sampleToolsets.has(k) && !served;
              const realiser = isSample ? realiserFor(k) : "";
              return (
                <div key={k}>
                  <div className="map-row on">
                    <span className="map-ico"><Icon className="ic map" aria-hidden="true" /></span>{" "}
                    {entry?.label || `${k[0].toUpperCase()}${k.slice(1)} tools`}
                    <span className="map-val">
                      {served ? `live · ${served.name}` : isSample ? "sample" : "on"}
                    </span>
                  </div>
                  {rows.map((r) => (
                    <div className="map-tool" key={r.label}>
                      <span className="map-tool-name">{r.label}</span>
                      {r.hint && <span className="map-tool-hint">{r.hint}</span>}
                    </div>
                  ))}
                  {isSample && (
                    <div className="map-tool">
                      <span className="map-tool-hint">
                        Demo data, not a real system.
                        {realiser ? ` Add ${realiser} to do this for real.` : ""}
                      </span>
                    </div>
                  )}
                  {served && (
                    <div className="map-tool">
                      <span className="map-tool-hint">
                        Reads and writes {served.name}. WhatsApp users are known by their number.
                      </span>
                    </div>
                  )}
                </div>
              );
            })}
          {/* Connectors (MCP): one heading per attached connection, then the
              tools this agent selected on it. Status comes from the live
              connection row; a sign-in problem also lands in the problems
              box below, which gates Publish. */}
          {(cfg.connectors || []).map((att) => {
            const row = connections?.find((c) => c.id === att.connection_id);
            const entry = catalogs?.tools?.[`connector:${row?.type}`];
            const Icon = TOOL_ICONS[entry?.icon] || TOOL_ICONS.plug;
            const on = row?.status === "connected";
            // "removed" is a claim about the owner's system, so it waits for
            // evidence: only once the list has actually loaded is a missing
            // id really missing.
            const status = !connections
              ? "checking"
              : row ? CONN_STATUS[row.status] || row.status : "removed";
            // No descriptions here at all: Preview is a summary of what
            // will run, not a reference. The full text is in the info
            // popover on the connector card in Options.
            return (
              <div key={att.connection_id}>
                <div className={on ? "map-row on" : "map-row"}>
                  <span className="map-ico"><Icon className="ic map" aria-hidden="true" /></span>{" "}
                  {entry?.label || "Connector"}
                  {(row?.name || connections) && (
                    <> · {row?.name || att.connection_id}</>
                  )}
                  <span
                    className={
                      "map-val" +
                      (status === "needs sign-in" || status === "checking"
                        ? " warn"
                        : status === "error" || status === "removed"
                          ? " bad"
                          : "")
                    }
                  >
                    {status}
                  </span>
                </div>
                {(att.allowed_tools || []).map((name) => (
                  <div className="map-tool" key={name}>
                    <span className="map-tool-name">{name}</span>
                  </div>
                ))}
                {(att.public_tools || []).length > 0 && (
                  <div className="map-tool">
                    <span className="map-tool-hint">Public sessions: on</span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {problems.length > 0 && (
        <div className="note warn" style={{ marginTop: 16 }} role="alert">
          <b>Before this agent can run:</b>
          <ul style={{ margin: "6px 0 0 16px" }}>
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
          <div style={{ marginTop: 6 }}>
            Use <b>Edit agent</b> below to fix these.
          </div>
        </div>
      )}
      {err && <ErrorNote message={err} onRetry={commit} busy={saving} />}

      {/* Saving on mount disables Test with no visible reason otherwise —
          the primary CTA looked broken for one round-trip. */}
      {saving && !err && (
        <p className="hint" role="status" style={{ marginTop: 14 }}>
          Saving this agent…
        </p>
      )}

      <div className="btn-row">
        {/* With problems listed above, Settings opens with its Advanced
            panel expanded, since every listed problem lives in there. */}
        <button
          className="btn ghost"
          onClick={() => onBack(problems.length > 0)}
          disabled={busy}
        >
          Edit agent
        </button>
        <button
          className="btn ghost"
          onClick={onPublish}
          disabled={busy || saving || problems.length > 0}
          title={
            problems.length ? "Fix the problems above first" : undefined
          }
        >
          Publish & deploy
        </button>
        <button className="btn" onClick={test} disabled={busy || saving}>
          {busy ? "Starting…" : <><Play className="ic inline" aria-hidden="true" /> Test agent</>}
        </button>
        {/* Inline with the row but pushed to the far end — the gap plus the
            red fill separates it from the primary CTA; the confirm dialog
            below is the guard against a stray tap. */}
        <button
          className="btn danger push-end"
          onClick={() => setConfirmOpen(true)}
          disabled={busy || saving}
        >
          Delete agent
        </button>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title={`Delete "${cfg.name}"?`}
        body="This also deletes its documents, memories, and conversations, and unpublishes it on any engine you have connected, so the agent stops answering everywhere. Its files stay on those engines until you remove them from Deployments."
        confirmLabel="Delete agent"
        danger
        busy={busy}
        onConfirm={remove}
        onCancel={() => setConfirmOpen(false)}
      />
    </div>
  );
}
