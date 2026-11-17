import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  addEngine,
  deleteEngine,
  engineDeleteAgent,
  engineDeployments,
  listDeployments,
  listEngines,
  publishAgent,
} from "../api.js";
import ConfirmDialog from "../components/ConfirmDialog.jsx";
import ErrorNote from "../components/ErrorNote.jsx";
import { IndustryIcon } from "../components/AppIcons.jsx";
import { useFocusTrap } from "../focus.js";

/* Every published agent in one dashboard, per source.

   The Source dropdown is the boundary made visible: "This computer" lists
   the LOCAL deployments (test rows), picking a saved engine lists THAT
   engine's real rows through the local backend's proxy, and "Add engine"
   lives in the same dropdown so the screen looks identical whether zero or
   five engines are saved. The two sources are never merged.

   No keys are rendered on this screen at all (user decision: a list page
   is the wrong place for credentials) — keys live on the per-agent Share &
   embed screen. Whitelisting also happens there (Manage), so rows carry the
   stored domains as read-only chips only when some exist. */

const ADD_SENTINEL = "__add-engine";

function AddEngineDialog({ open, busy, onSave, onCancel }) {
  const boxRef = useRef(null);
  const nameRef = useRef(null);
  const titleId = useId();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [tok, setTok] = useState("");

  useFocusTrap(boxRef, open, { initialFocus: nameRef });

  if (!open) return null;

  const ready = name.trim() && url.trim() && tok;

  return (
    <div
      className="dialog-veil"
      onMouseDown={(e) => {
        if (boxRef.current && !boxRef.current.contains(e.target)) onCancel();
      }}
    >
      <div
        className="dialog"
        ref={boxRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={(e) => {
          if (e.key === "Escape" && !busy) {
            e.stopPropagation();
            onCancel();
          }
        }}
      >
        <div className="dialog-title" id={titleId}>
          Add an engine
        </div>
        <div className="dialog-body">
          <div className="field">
            <label className="label" htmlFor="eng-name">Name</label>
            <input
              id="eng-name"
              className="input"
              ref={nameRef}
              placeholder="Production VM"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="eng-url">Engine URL</label>
            <input
              id="eng-url"
              className="input"
              placeholder="https://1.2.3.4.sslip.io"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="eng-tok">Admin token</label>
            <input
              id="eng-tok"
              className="input"
              type="password"
              value={tok}
              onChange={(e) => setTok(e.target.value)}
            />
          </div>
          <div className="hint">
            Checked before saving: the engine must answer with this token.
            The token stays on this machine and is never shown again.
          </div>
        </div>
        <div className="btn-row" style={{ marginTop: 18 }}>
          <button className="btn ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            className="btn"
            onClick={() => onSave({ name: name.trim(), base_url: url.trim(), admin_token: tok })}
            disabled={busy || !ready}
          >
            {busy ? "Checking…" : "Save engine"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function Deployments({ onBack, onOpen }) {
  const [engines, setEngines] = useState([]);
  const [source, setSource] = useState(""); // "" = this computer
  const [data, setData] = useState(null); // null = not loaded
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(null); // agent id with an action in flight
  const [addOpen, setAddOpen] = useState(false);
  const [addBusy, setAddBusy] = useState(false);
  const [removeEng, setRemoveEng] = useState(null); // engine pending removal
  const [removeAgent, setRemoveAgent] = useState(null); // agent to wipe off the engine
  const sourceSelId = useId();

  const loadEngines = useCallback(
    () => listEngines().then(setEngines).catch(() => {}),
    []
  );
  useEffect(() => {
    loadEngines();
  }, [loadEngines]);

  const load = useCallback(() => {
    setErr("");
    setData(null);
    return (source ? engineDeployments(source) : listDeployments())
      .then(setData)
      .catch((e) => setErr(`Couldn't load deployments: ${e.message}`));
  }, [source]);

  useEffect(() => {
    load();
  }, [load]);

  const engine = source ? engines.find((e) => e.id === source) || null : null;

  async function doPublish(agent) {
    setBusy(agent.id);
    setErr("");
    try {
      await publishAgent(agent.id);
      await load();
    } catch (e) {
      // The endpoint refuses while config.validate() has problems, and its
      // message names them — surfacing it beats a generic failure.
      setErr(`Couldn't publish "${agent.name}": ${e.message}`);
    } finally {
      setBusy(null);
    }
  }

  async function saveEngine(body) {
    setAddBusy(true);
    setErr("");
    try {
      const created = await addEngine(body);
      setAddOpen(false);
      await loadEngines();
      setSource(created.id); // show the engine you just connected
    } catch (e) {
      setErr(`Couldn't add this engine: ${e.message}`);
      setAddOpen(false);
    } finally {
      setAddBusy(false);
    }
  }

  const deployments = data?.deployments || [];
  const unpublished = data?.unpublished || [];
  const remote = !!source;

  return (
    <div>
      <h1 className="h-serif">
        Live <em>deployments</em>
      </h1>
      <p className="sub">
        Every published agent and the websites allowed to embed it. Local
        rows are test deployments; the real ones live on your engines.
      </p>

      {/* Always visible, so the screen has one consistent shape whether
          zero or five engines are saved. "Add engine" lives inside the
          dropdown; the controlled value snaps back after opening it. */}
      <div className="ins-filters">
        <label className="label" htmlFor={sourceSelId}>
          Source
        </label>
        <select
          id={sourceSelId}
          className="select slim"
          value={source}
          onChange={(e) => {
            const v = e.target.value;
            if (v === ADD_SENTINEL) {
              setAddOpen(true);
              return;
            }
            setSource(v);
          }}
        >
          <option value="">Local</option>
          {engines.map((e) => (
            <option key={e.id} value={e.id}>
              {e.name}
            </option>
          ))}
          <option value={ADD_SENTINEL}>Add engine…</option>
        </select>
      </div>

      {err && <ErrorNote message={err} onRetry={load} />}
      {!data && !err && <div className="spin" />}

      {data && (
        <p className="hint" role="status">
          {remote
            ? `${deployments.length} published on ${engine?.name || "this engine"}`
            : `${deployments.length} published · ${unpublished.length} not published`}
        </p>
      )}

      {remote && engine && (
        <>
          <h2 className="section-h">Engine details</h2>
          <div className="docs">
            <div className="doc">
              <span className="doc-name">{engine.name}</span>
              <span className="doc-when">{engine.base_url}</span>
              <button
                className="btn ghost slim"
                type="button"
                onClick={() => setRemoveEng(engine)}
              >
                Remove
              </button>
            </div>
          </div>
        </>
      )}

      {data && deployments.length === 0 && !remote && (
        <p className="sub">
          Nothing is published yet. Publishing an agent mints a restricted
          public key (<code>pk_…</code>) that can only start calls with that
          one agent; you paste a single line of script on your website and
          it's live. A builder running on localhost can't serve a real
          website, so you'll want an engine first:{" "}
          <a className="chip chip-btn" href="#/docs/deployment">
            Read the engine setup guide
          </a>
        </p>
      )}
      {data && deployments.length === 0 && remote && (
        <p className="sub">
          Nothing is published on this engine yet. Open an agent's Share &
          embed screen on this builder and push it to {engine?.name}.
        </p>
      )}

      {deployments.length > 0 && (
        <>
          <h2 className="section-h">Published</h2>
          <div className="docs">
            {deployments.map((d) => (
              <div className="doc" key={d.id}>
                <span className="doc-name">
                  <span className="dep-ico" aria-hidden="true">
                    <IndustryIcon value={d.agent_industry} size={15} />
                  </span>
                  {d.orphaned ? <i>Agent deleted</i> : d.agent_name}
                </span>
                <span
                  className={
                    d.status === "active"
                      ? "doc-status ready"
                      : d.status === "revoked"
                        ? "doc-status failed"
                        : "doc-status"
                  }
                >
                  {d.status}
                </span>
                <button
                  className="btn ghost slim"
                  type="button"
                  onClick={() => onOpen(d.agent_id, engine)}
                >
                  Manage
                </button>
                {d.status === "active" && !d.public_base_is_local && (
                  /* "View live", not "Preview": Preview means the local
                     review screen everywhere else in the app, and one word
                     with two meanings is worse than a longer label. */
                  <a
                    className="btn ghost slim"
                    href={d.share_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    View live
                  </a>
                )}
                {/* Stored domains as read-only context, only when some
                    exist. Whitelisting happens on Manage (Share & embed),
                    and local rows never show domains at all — a localhost
                    allowlist protects nothing. */}
                {!d.public_base_is_local && d.allowed_origins.length > 0 && (
                  <span className="dep-origins">
                    {d.allowed_origins.map((o) => (
                      <span className="chip" key={o}>
                        {o}
                      </span>
                    ))}
                  </span>
                )}
              </div>
            ))}
          </div>
        </>
      )}

      {unpublished.length > 0 && (
        <>
          <h2 className="section-h">Not published yet</h2>
          <div className="docs">
            {unpublished.map((a) => (
              <div className="doc" key={a.id}>
                <span className="doc-name">
                  <span className="dep-ico" aria-hidden="true">
                    <IndustryIcon value={a.industry} size={15} />
                  </span>
                  {a.name}
                </span>
                {remote ? (
                  <>
                    <span className="doc-when">
                      push it from your local builder to publish here
                    </span>
                    {/* The only way to clear an agent off an engine. Lives
                        here, on the engine's own screen, rather than riding
                        along with a local delete: this deletes files and
                        transcripts on another machine and cannot be undone.
                        Published rows don't offer it (unpublish first), so a
                        live agent is never one mis-tap from being wiped. */}
                    <button
                      className="btn ghost slim"
                      type="button"
                      disabled={busy === a.id}
                      onClick={() => setRemoveAgent(a)}
                    >
                      {busy === a.id ? "Removing…" : "Remove from engine"}
                    </button>
                  </>
                ) : (
                  <button
                    className="btn ghost slim"
                    type="button"
                    disabled={busy === a.id}
                    onClick={() => doPublish(a)}
                  >
                    {busy === a.id ? "Publishing…" : "Publish"}
                  </button>
                )}
              </div>
            ))}
          </div>
        </>
      )}

      <div className="btn-row">
        <button className="btn ghost" onClick={onBack}>
          Back
        </button>
      </div>

      {/* key remounts the dialog per open, so a cancelled attempt's typed
          values (including the token) never linger into the next one */}
      <AddEngineDialog
        key={String(addOpen)}
        open={addOpen}
        busy={addBusy}
        onSave={saveEngine}
        onCancel={() => setAddOpen(false)}
      />

      <ConfirmDialog
        open={!!removeEng}
        title={`Remove engine "${removeEng?.name}"?`}
        body="Only this saved connection is removed. The engine itself, its agents, and its keys keep running untouched; you can re-add it anytime with the same URL and token."
        confirmLabel="Remove"
        danger
        onConfirm={async () => {
          const eng = removeEng;
          setRemoveEng(null);
          try {
            await deleteEngine(eng.id);
            if (source === eng.id) setSource("");
            await loadEngines();
          } catch (e) {
            setErr(`Couldn't remove this engine: ${e.message}`);
          }
        }}
        onCancel={() => setRemoveEng(null)}
      />

      <ConfirmDialog
        open={!!removeAgent}
        title={`Remove "${removeAgent?.name}" from ${engine?.name || "this engine"}?`}
        body={`This deletes its files, its documents, and the conversations recorded on ${
          engine?.name || "this engine"
        }. Nothing on this computer changes. To bring it back you would push it from your local builder again.`}
        confirmLabel="Remove"
        danger
        busy={busy === removeAgent?.id}
        onConfirm={async () => {
          const a = removeAgent;
          setRemoveAgent(null);
          setBusy(a.id);
          setErr("");
          try {
            await engineDeleteAgent(source, a.id);
            await load();
          } catch (e) {
            setErr(`Couldn't remove "${a.name}" from this engine: ${e.message}`);
          } finally {
            setBusy(null);
          }
        }}
        onCancel={() => setRemoveAgent(null)}
      />
    </div>
  );
}
