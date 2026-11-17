import { useCallback, useEffect, useState } from "react";
import {
  engineGetDeployment,
  engineGetUsage,
  engineListChannels,
  enginePatchDeployment,
  engineRevokeDeployment,
  engineRotateKey,
  getDeployment,
  getDeploymentUsage,
  listChannels,
  listEngines,
  patchDeployment,
  publishAgent,
  pushAgent,
  revokeDeployment,
  rotateDeploymentKey,
} from "../api.js";
import EngineDatabaseCard from "../components/EngineDatabaseCard.jsx";
import ConfirmDialog from "../components/ConfirmDialog.jsx";
import CopyField from "../components/CopyField.jsx";
import ErrorNote from "../components/ErrorNote.jsx";
import InfoIcon from "../components/InfoIcon.jsx";

function fmtWhen(iso) {
  if (!iso) return "never";
  return new Date(iso).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Share & embed for ONE agent. Deployment-aware: on a localhost builder the
// snippet would be a lie, so the screen becomes the "take it live" flow —
// push the agent to a deployed engine and render ITS snippet instead.
//
// Takes an agentId (not a cfg) so the deployments list can open any saved
// agent's keys without activating it — activating would change which agent
// the worker runs next, an invisible and unrelated side effect.
// `engine` ({id, name}) switches the screen into remote mode: every call
// operates on that saved engine's deployment through the local backend's
// proxy (usage, caps, allowlist, rotate, unpublish). The push section hides
// there — you're already looking at the engine.
export default function Publish({ agentId, onBack, onDeployments, onChannels, engine }) {
  const remoteMode = !!engine;
  const [dep, setDep] = useState(null);      // the managed deployment
  // The numbers this deployment answers on, for one status line. The card
  // itself lives on the Channels screen, which lists every place the agent
  // answers; this page stays about keys, limits and websites.
  const [chans, setChans] = useState(null);
  const [remote, setRemote] = useState(null); // engine result after a push
  // Remote manage never re-serves stored credentials, but a key you just
  // minted here is different: without its snippet, Rotate key would leave
  // you with a live agent and no way to update the website.
  const [rotated, setRotated] = useState(null);
  const [usage, setUsage] = useState(null);
  const [origins, setOrigins] = useState("");
  const [engineUrl, setEngineUrl] = useState("");
  const [engineTok, setEngineTok] = useState("");
  // push-time allowlist + saved-engine picker ("" = manual URL/token)
  const [pushOrigins, setPushOrigins] = useState("");
  const [engines, setEngines] = useState([]);
  const [selEngine, setSelEngine] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [saved, setSaved] = useState("");
  const [rotateOpen, setRotateOpen] = useState(false);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [notPublished, setNotPublished] = useState(false);
  // editable caps (strings while typing; validated on save)
  const [limitsOpen, setLimitsOpen] = useState(false);
  const [capC, setCapC] = useState("");
  const [capD, setCapD] = useState("");
  const [capM, setCapM] = useState("");
  const [savedCaps, setSavedCaps] = useState("");

  const apiGet = useCallback(
    () => (remoteMode ? engineGetDeployment(engine.id, agentId) : getDeployment(agentId)),
    [remoteMode, engine, agentId]
  );
  const apiUsage = useCallback(
    () => (remoteMode ? engineGetUsage(engine.id, agentId) : getDeploymentUsage(agentId)),
    [remoteMode, engine, agentId]
  );
  const apiPatch = useCallback(
    (body) =>
      remoteMode
        ? enginePatchDeployment(engine.id, agentId, body)
        : patchDeployment(agentId, body),
    [remoteMode, engine, agentId]
  );

  const load = useCallback(() => {
    setErr("");
    return apiGet()
      .then((d) => {
        setDep(d);
        setNotPublished(false);
        setOrigins((d.allowed_origins || []).join(", "));
        // Best-effort: usage is informational, never blocks the screen.
        apiUsage().then(setUsage).catch(() => {});
      })
      .catch((e) =>
        // A 404 means "not published". Anything else (backend down, 500)
        // used to render as that same confident claim, and then the
        // Publish button failed too.
        e.status === 404
          ? setNotPublished(true)
          : setErr(`Couldn't load this deployment: ${e.message}`)
      );
  }, [apiGet, apiUsage]);

  // Saved engines feed the push section's picker (local builders only).
  useEffect(() => {
    if (!remoteMode) listEngines().then(setEngines).catch(() => {});
  }, [remoteMode]);

  // Picking a saved engine prefills the push allowlist with what that
  // engine currently stores for this agent (best-effort: not published
  // there yet = leave the field as typed).
  useEffect(() => {
    if (!selEngine) return;
    engineGetDeployment(selEngine, agentId)
      .then((d) => setPushOrigins((d.allowed_origins || []).join(", ")))
      .catch(() => {});
  }, [selEngine, agentId]);

  useEffect(() => {
    load();
  }, [load]);

  // Best effort, informational: a failed channels read leaves the line out
  // rather than failing the screen.
  useEffect(() => {
    if (!dep) return;
    (remoteMode ? engineListChannels(engine.id, agentId) : listChannels(agentId))
      .then((r) => setChans(r.channels || []))
      .catch(() => setChans(null));
  }, [dep, remoteMode, engine, agentId]);

  async function run(fn) {
    setBusy(true);
    setErr("");
    try {
      await fn();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  const doPublish = () =>
    run(async () => {
      const d = await publishAgent(agentId);
      setDep(d);
      setOrigins((d.allowed_origins || []).join(", "));
      setNotPublished(false);
    });

  const saveOrigins = () =>
    run(async () => {
      const list = origins.split(",").map((s) => s.trim()).filter(Boolean);
      const d = await apiPatch({ allowed_origins: list });
      setDep(d);
      // Echo what the server actually stored: it normalizes (lowercase, no
      // trailing slash) and rejects scheme-less entries, so the field must
      // show the stored truth rather than what was typed.
      setOrigins((d.allowed_origins || []).join(", "));
      setSaved(
        d.allowed_origins.length
          ? `Saved: ${d.allowed_origins.length} website(s) may embed this agent.`
          : "Saved: no allowlist, so ANY site can embed this key (dev only)."
      );
    });

  const saveCaps = () =>
    run(async () => {
      const c = parseInt(capC, 10);
      const d = parseInt(capD, 10);
      const m = parseInt(capM, 10);
      if (!(c >= 1) || !(d >= 1) || !(m >= 1)) {
        throw new Error("every limit must be at least 1");
      }
      const next = await apiPatch({
        max_concurrent_sessions: c,
        max_sessions_per_day: d,
        max_session_seconds: m * 60,
      });
      setDep(next);
      setSavedCaps(
        `Saved: ${next.max_concurrent_sessions} at once, ` +
          `${next.max_sessions_per_day}/day, ` +
          `${Math.round(next.max_session_seconds / 60)}m per call.`
      );
    });

  const toggleShowSources = () =>
    run(async () => {
      const next = await apiPatch({ show_sources: !dep.show_sources });
      setDep(next);
    });

  const doPush = () =>
    run(async () => {
      // The allowlist rides the push: it only matters ON the engine. The
      // field's current value is what the engine ends up with (empty =
      // any site, dev only) — explicit beats silently keeping stale
      // domains.
      const body = {
        include_docs: true,
        allowed_origins: pushOrigins
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      };
      if (selEngine) body.engine_id = selEngine;
      else {
        body.runtime_url = engineUrl.trim();
        body.admin_token = engineTok;
      }
      const r = await pushAgent(agentId, body);
      setRemote(r.deployment);
    });

  // What this builder IS never changes with a push: a local builder keeps
  // its "Test locally" snippet (its own key only works here). The engine
  // result renders as its own section below, so the two keys never swap
  // places in the same field. In remote mode dep IS the engine's
  // deployment, so isLocal is false and the engine sections render.
  const isLocal = !!dep?.public_base_is_local;
  // The internet-facing deployment worth showing a share link/snippet for:
  // a fresh push result, or the managed deployment when it isn't
  // localhost-bound (builder running on an engine, or remote mode).
  const engineDep = remote || (isLocal ? null : dep);

  return (
    <div>
      <h1 className="h-serif">
        Publish & <em>deploy</em>
      </h1>
      <p className="sub">
        {remoteMode
          ? `Managing this agent's live deployment on ${engine.name}: its key, limits, and allowed websites.`
          : "Put this agent on a website: a share link for anyone, a one-line snippet for your site."}
      </p>

      {err && <ErrorNote message={err} onRetry={load} />}

      {notPublished && (
        <div className="note" style={{ marginBottom: 18 }}>
          {remoteMode ? (
            <>
              <b>This agent isn't published on {engine.name}.</b> Open it
              from your local builder and push it to this engine; publishing
              happens as part of the push.
            </>
          ) : (
            <>
              {/* Not "yet": this same card is what you land on right after
                  Unpublish, where "yet" would read as a lie. */}
              <b>This agent isn't published.</b> Publishing mints a
              restricted public key (pk_…) that can only start conversations with
              this agent.
              <div className="btn-row" style={{ marginTop: 12 }}>
                <button className="btn" onClick={doPublish} disabled={busy}>
                  {busy ? "Publishing…" : "Publish this agent"}
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {isLocal && !remote && (
        <div className="note warn" style={{ marginBottom: 18 }}>
          <b>This builder is running locally</b>, so a snippet pointing at
          localhost won't work on a real website. Deploy an engine, then
          push this agent to it below; the working snippet renders here.
          <div className="sum" style={{ marginTop: 10, marginBottom: 0 }}>
            <a className="chip chip-btn" href="#/docs/deployment">
              Read the engine setup guide
            </a>
          </div>
        </div>
      )}

      {dep && (
        <>
          {usage && (
            <>
              <h2 className="section-h">
                Usage & limits
                <InfoIcon>
                  Live traffic on this agent's key, against the limits that
                  cap it:
                  <ol>
                    <li><b>Open now</b>: calls in progress right now vs the
                      simultaneous-call limit. At the limit, the next
                      visitor is refused.</li>
                    <li><b>Today</b>: calls started since midnight vs the
                      daily limit, your cost ceiling for a day.</li>
                    <li><b>All time</b>: every call this key ever started.</li>
                    <li><b>Max call</b>: a single call is cut off after
                      this long.</li>
                  </ol>
                  These limits are what protect your provider spend if the
                  key is copied; the server checks them before every call.
                </InfoIcon>
              </h2>
              <div className="sum">
                <span className="chip">
                  Open now <b>{usage.sessions_open}</b> / {dep.max_concurrent_sessions}
                </span>
                <span className="chip">
                  Today <b>{usage.sessions_today}</b> / {dep.max_sessions_per_day}
                </span>
                <span className="chip">
                  All time <b>{usage.sessions_total}</b>
                </span>
                <span className="chip">
                  Max call <b>{Math.round(dep.max_session_seconds / 60)}m</b>
                </span>
                <span className="chip">Last session {fmtWhen(usage.last_session_at)}</span>
                {usage.refused_today > 0 && (
                  <span className="chip warn-chip">
                    Refused today <b>{usage.refused_today}</b>
                  </span>
                )}
                <span className="chip">
                  Sources under answers <b>{dep.show_sources ? "On" : "Off"}</b>
                  <InfoIcon>
                    When on, visitors can expand "Sources" under an answer
                    to see the pages the agent searched. The single link
                    the agent gives is always shown. Turn off for a site
                    where outside links are not wanted.
                  </InfoIcon>
                </span>
                <button
                  className="chip chip-btn"
                  type="button"
                  onClick={toggleShowSources}
                  disabled={busy}
                >
                  {dep.show_sources ? "Turn off" : "Turn on"}
                </button>
                <button
                  className="chip chip-btn"
                  type="button"
                  onClick={() => {
                    setLimitsOpen((o) => !o);
                    setCapC(String(dep.max_concurrent_sessions));
                    setCapD(String(dep.max_sessions_per_day));
                    setCapM(String(Math.round(dep.max_session_seconds / 60)));
                    setSavedCaps("");
                  }}
                >
                  {limitsOpen ? "Close" : "Edit limits"}
                </button>
              </div>
              {limitsOpen && (
                <div className="field" style={{ marginTop: 4 }}>
                  <div className="field-row">
                    <div>
                      <label className="label" htmlFor="cap-c">
                        Max simultaneous calls{" "}
                        <InfoIcon>
                          How many visitors can be on a call at the same
                          moment. When full, the next visitor is refused
                          with a busy message until a call ends. Refused
                          attempts show in the chips above and on Insights.
                        </InfoIcon>
                      </label>
                      <input
                        id="cap-c"
                        className="input"
                        type="number"
                        min="1"
                        max="1000"
                        value={capC}
                        onChange={(e) => setCapC(e.target.value)}
                      />
                    </div>
                    <div>
                      <label className="label" htmlFor="cap-d">
                        Max calls per day{" "}
                        <InfoIcon>
                          Total calls allowed per calendar day. The count
                          resets at the server's midnight, not 24 hours
                          after each call. When the day's quota is used up,
                          visitors are refused until the reset.
                        </InfoIcon>
                      </label>
                      <input
                        id="cap-d"
                        className="input"
                        type="number"
                        min="1"
                        max="100000"
                        value={capD}
                        onChange={(e) => setCapD(e.target.value)}
                      />
                    </div>
                    <div>
                      <label className="label" htmlFor="cap-m">
                        Max minutes per call{" "}
                        <InfoIcon>
                          At this point the agent politely says goodbye and
                          ends the call; audio is never cut mid-sentence.
                          Such calls show as "hit the time limit" on
                          Insights.
                        </InfoIcon>
                      </label>
                      <input
                        id="cap-m"
                        className="input"
                        type="number"
                        min="1"
                        max="120"
                        value={capM}
                        onChange={(e) => setCapM(e.target.value)}
                      />
                    </div>
                  </div>
                  <div className="hint">
                    Every call a visitor starts runs on your provider keys;
                    these limits are the spend ceiling. Keep them as low as
                    real traffic allows, and never raise them while the
                    website allowlist below is empty. They apply to visitor
                    calls only: your own test calls in the builder are never
                    limited and never counted. Rapid-fire call attempts are
                    also slowed automatically, separate from these caps.
                  </div>
                  <div className="btn-row" style={{ marginTop: 10 }}>
                    <button className="btn ghost slim" onClick={saveCaps} disabled={busy}>
                      Save limits
                    </button>
                    {savedCaps && (
                      <span className="hint" role="status">{savedCaps}</span>
                    )}
                  </div>
                </div>
              )}
            </>
          )}

          {isLocal ? (
            /* Local builder: the key alone has no standalone use here (it
               only works inside the snippet, against this machine), so
               show the actionable artifact instead. */
            <>
              <h2 className="section-h">Test locally</h2>
              <CopyField
                label="Paste before </body> in any local test page"
                value={dep.snippet}
                multiline
                hint="Try the widget on a local site before going live: serve a page on this machine (e.g. Live Server), paste this snippet, and the bubble appears. It works only on this machine; pushing to an engine below mints the real snippet for customer websites."
              />
            </>
          ) : remoteMode ? null : (
            /* Builder running ON an engine shows its own key; remote mode
               (managing another engine from here) shows NO credentials at
               all — the share link, snippet, and key are view-once at push
               time, and a manage screen that re-serves them would quietly
               break that promise. */
            <>
              <h2 className="section-h">Public key</h2>
              <CopyField
                label="Identifies this agent to the widget"
                value={dep.public_key}
                display={dep.public_key_masked}
                hint="Public by design; it ships in your page source. What protects it is the website allowlist and the caps below, so keep those tight."
              />
            </>
          )}

          {!remoteMode && (
            <div className="band green">
              <h2 className="section-h">Deploy to a live engine</h2>
              {engines.length > 0 && (
                <div className="field">
                  <label className="label" htmlFor="pub-engsel">Engine</label>
                  <select
                    id="pub-engsel"
                    className="select slim"
                    value={selEngine}
                    onChange={(e) => setSelEngine(e.target.value)}
                  >
                    <option value="">Enter URL and token manually</option>
                    {engines.map((en) => (
                      <option key={en.id} value={en.id}>
                        {en.name}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {!selEngine && (
                <>
                  <div className="field">
                    <label className="label" htmlFor="pub-eng">Engine URL</label>
                    <input
                      id="pub-eng"
                      className="input"
                      placeholder="https://1.2.3.4.sslip.io"
                      value={engineUrl}
                      onChange={(e) => setEngineUrl(e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label className="label" htmlFor="pub-tok">Engine admin token</label>
                    <input
                      id="pub-tok"
                      className="input"
                      type="password"
                      value={engineTok}
                      onChange={(e) => setEngineTok(e.target.value)}
                    />
                  </div>
                </>
              )}
              <div className="field">
                <label className="label" htmlFor="pub-worigins">
                  Websites allowed to embed (applied on the engine)
                </label>
                <input
                  id="pub-worigins"
                  className="input"
                  placeholder="https://acme.com, https://www.acme.com"
                  value={pushOrigins}
                  onChange={(e) => setPushOrigins(e.target.value)}
                />
                <div className="hint">
                  Scheme and host only. Leave empty to allow any site (dev
                  only). Re-push anytime to change this; the key stays the
                  same.
                </div>
              </div>
              <button
                className="btn"
                onClick={doPush}
                disabled={
                  busy || (!selEngine && (!engineUrl.trim() || !engineTok))
                }
              >
                {busy ? "Deploying…" : "Deploy to engine"}
              </button>
            </div>
          )}

          {/* The generated link + snippet render BELOW the push section,
              with a copy-now warning: the engine's result lives only in
              this screen's state, so leaving the page loses it until the
              next push. */}
          {!remoteMode && engineDep && (
            <div style={{ marginTop: 24 }}>
              {remote && (
                <div className="note warn" role="alert" style={{ marginBottom: 14 }}>
                  <b>Copy your share link and snippet now.</b> They aren't
                  stored on this screen: if you press Back or leave, they
                  won't be shown again until you push again.
                </div>
              )}
              <h2 className="section-h">Share link</h2>
              <CopyField
                label="Anyone with this link can talk to the agent"
                value={engineDep.share_url}
              />
              <h2 className="section-h">Embed on a website</h2>
              <CopyField label="Paste before </body>" value={engineDep.snippet} multiline />
              {remote && (
                // Echo what the engine actually stored so the push's
                // allowlist visibly took effect.
                <div className="sum" style={{ marginTop: 10 }}>
                  {(remote.allowed_origins || []).length > 0 ? (
                    remote.allowed_origins.map((o) => (
                      <span className="chip" key={o}>
                        {o}
                      </span>
                    ))
                  ) : (
                    <span className="chip dep-open">
                      Any site can embed this key (dev only)
                    </span>
                  )}
                </div>
              )}
            </div>
          )}

          {/* The standalone allowlist editor manages THIS deployment, so it
              only makes sense where this deployment is the real one: a
              builder running on an engine, or remote mode. On a local
              builder whitelisting localhost protects nothing (and can only
              break local testing) — there, the allowlist rides the push. */}
          {!isLocal && (
            <>
              <h2 className="section-h">Allowed websites</h2>
              <div className="field">
                <label className="label" htmlFor="pub-orig">
                  Comma-separated origins (empty = any site, dev only)
                </label>
                <input
                  id="pub-orig"
                  className="input"
                  placeholder="https://acme.com, https://www.acme.com"
                  value={origins}
                  onChange={(e) => {
                    setOrigins(e.target.value);
                    setSaved("");
                  }}
                />
                <div className="hint">
                  Scheme and host only, for example <code>https://acme.com</code>.
                  This allowlist is what stops other sites using your key.
                </div>
              </div>
              <button className="btn ghost slim" onClick={saveOrigins} disabled={busy}>
                Whitelist domains
              </button>
              {saved && (
                <p className="hint" role="status" style={{ marginTop: 8 }}>
                  {saved}
                </p>
              )}
            </>
          )}

          {/* One line about WhatsApp; the card moved to the Channels
              screen, which lists every place the agent answers. */}
          <h2 className="section-h">WhatsApp</h2>
          <p className="hint" style={{ marginTop: -4 }}>
            {chans === null
              ? "Numbers are managed on the Channels screen."
              : chans.length === 0
                ? "Not connected. Connect a number on the Channels screen."
                : `Answering on ${chans
                    .map((c) => c.display_number || c.phone_number_id)
                    .join(", ")}${chans.some((c) => c.status === "paused") ? " (paused)" : ""}. Settings are on the Channels screen.`}
          </p>
          <button className="btn ghost slim" onClick={onChannels} disabled={busy}>
            Channels
          </button>

          {/* On an engine, the database the pushed agent reads is set here:
              connections never ride a push, and the engine has no Options
              screen. Locally it is attached on Options as usual. */}
          {remoteMode && (
            <div style={{ marginTop: 24 }}>
              <EngineDatabaseCard engine={engine} agentId={agentId} />
            </div>
          )}
        </>
      )}

      {/* Rotation result, view-once, rendered next to the button that made
          it: this is the output of an action you just took (same posture as
          a push result), not stored credentials being re-served on load. */}
      {remoteMode && rotated && (
        <div style={{ marginTop: 24 }}>
          <div className="note warn" role="alert" style={{ marginBottom: 14 }}>
            <b>Copy the new snippet now.</b> The old one is already dead, and
            this screen won't show the new one again: leaving the page means
            rotating once more to get a snippet you can paste.
          </div>
          <h2 className="section-h">New share link</h2>
          <CopyField
            label="Anyone with this link can talk to the agent"
            value={rotated.share_url}
          />
          <h2 className="section-h">New embed snippet</h2>
          <CopyField
            label="Paste before </body> on every site using this agent"
            value={rotated.snippet}
            multiline
          />
        </div>
      )}

      <div className="btn-row">
        <button className="btn ghost" onClick={onBack} disabled={busy}>
          Back
        </button>
        {dep && (
          <button
            className="btn ghost"
            onClick={() => setRevokeOpen(true)}
            disabled={busy}
          >
            Unpublish
          </button>
        )}
        <button className="btn ghost" onClick={onDeployments} disabled={busy}>
          Deployments
        </button>
        <button className="btn ghost" onClick={onChannels} disabled={busy}>
          Channels
        </button>
        {dep && (
          <button
            className="btn danger push-end"
            onClick={() => setRotateOpen(true)}
            disabled={busy}
          >
            {/* Rotation is per instance, and on a local builder the key it
                replaces only works on this machine. Labeling it plainly
                beats hiding the button (rotation would then live nowhere
                on the screen people look at when a key leaks) and beats
                disabling it (it does work, it is just narrow). */}
            {isLocal ? "Rotate local key" : "Rotate key"}
          </button>
        )}
      </div>

      <ConfirmDialog
        open={rotateOpen}
        title={isLocal ? "Rotate the local test key?" : "Rotate the public key?"}
        body={
          isLocal
            ? "This replaces the Test locally snippet above and nothing else. Live websites are unaffected: their key belongs to the engine, so rotate it from Deployments, pick that engine as the source, then Manage."
            : remoteMode
              ? `Every site using the current snippet stops working immediately. The new snippet is shown once, here on this screen, so have somewhere ready to paste it before you rotate.`
              : "Every site using the current snippet stops working immediately, so you'll need to paste the new snippet everywhere."
        }
        confirmLabel="Rotate key"
        danger
        busy={busy}
        onConfirm={() =>
          run(async () => {
            const next = await (remoteMode
              ? engineRotateKey(engine.id, agentId)
              : rotateDeploymentKey(agentId));
            setDep(next);
            setRemote(null);
            // Remote mode renders no credentials on load, so the rotation's
            // own result is the only place the new snippet can appear.
            if (remoteMode) setRotated(next);
            setRotateOpen(false);
          })
        }
        onCancel={() => setRotateOpen(false)}
      />

      <ConfirmDialog
        open={revokeOpen}
        title="Unpublish this agent?"
        /* Names the machines this will switch off, rather than saying
           "every connected engine" and making you remember which those
           are. This is the one action on the screen that reaches past the
           instance you are looking at, so it has to be explicit. */
        body={
          remoteMode
            ? `The agent stops answering on ${engine.name}: the share link goes dead and every site using the snippet stops working. Publishing again later mints a different key, so the old snippets stay dead.`
            : `It stops answering here${
                engines.length > 0
                  ? ` and on: ${engines.map((e) => e.name).join(", ")}`
                  : ""
              }. Share links go dead and every site using the snippet stops working. Publishing again later mints a different key, so the old snippets stay dead.`
        }
        confirmLabel="Unpublish"
        danger
        busy={busy}
        onConfirm={() =>
          run(async () => {
            const res = await (remoteMode
              ? engineRevokeDeployment(engine.id, agentId)
              : revokeDeployment(agentId));
            setRevokeOpen(false);
            // An engine that couldn't be reached is still serving this
            // agent. Leaving the screen would hide that, so stay put, show
            // the not-published state (the local key IS dead) and name the
            // engines that need another try.
            const failed = (res?.engines || []).filter((e) => !e.ok);
            if (failed.length > 0) {
              setDep(null);
              setUsage(null);
              setRemote(null);
              setNotPublished(true);
              setErr(
                `Unpublished here, but not on ${failed
                  .map((e) => e.name)
                  .join(", ")}: ${failed[0].error}`
              );
              return;
            }
            onBack();
          })
        }
        onCancel={() => setRevokeOpen(false)}
      />
    </div>
  );
}
