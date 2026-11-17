import Icon from "./Icon.jsx";
import InfoIcon from "./InfoIcon.jsx";
import { inline } from "./Markdown.jsx";

/* One connector TYPE on the Options screen (Google Calendar, Custom MCP
   server): its saved connections as rows, each with a status chip, Test /
   Sign in / Remove, and an "Attach to this agent" checkbox. Attaching a
   row opens the tool checklist under it: which of the server's tools this
   agent may call, and whether visitors on a public embed may trigger them.

   Connections are global (connect once, use on any agent); only the
   attachment (`cfg.connectors`) is per agent. Options owns every piece of
   state and passes handlers down; this component only renders. */

const STATUS = {
  connected: { label: "Connected", cls: "chip conn-chip ok" },
  needs_auth: { label: "Needs sign-in", cls: "chip conn-chip warn-chip" },
  pending_auth: { label: "Signing in", cls: "chip conn-chip warn-chip" },
  error: { label: "Error", cls: "chip conn-chip err" },
};

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return url || "";
  }
}

export default function ConnectorCard({
  type,
  connections,
  attachments,
  busyId,
  setup, // optional node rendered under the header (OAuth app keys, redirect URI)
  connectDisabled, // string: why Connect is unavailable, or falsy
  onAdd,
  onSignIn,
  onTest,
  onRemove,
  onAttach,
  onDetach,
  onTools,
  onPublic,
  onPurpose,
  stubToolsets = [], // [{id, label}] sample toolsets this agent has on that
                     // attaching this connector switches off
  wanted = false, // this agent's template needs this connector type and
                  // nothing of it is attached yet
}) {
  const isOauth = type.auth === "oauth";
  return (
    <div className="opt on conn-card">
      <div className="opt-body">
        <div className="opt-title">
          <span className="conn-head">
            <Icon name={type.icon || "plug"} className="inline" />
            <span>{type.label}</span>
          </span>
          {/* Same shape as the provider and web-search info popovers: one
              framing sentence, a numbered recipe, a link to the long
              version. All three come from the connectors catalog. */}
          {(type.docs_hint || type.docs_steps?.length > 0) && (
            <InfoIcon>
              {/* docs_hint/docs_steps are plain strings from the catalog
                  (DB-backed, no JSX), so a site mention like composio.dev
                  is written as [composio.dev](https://composio.dev) and
                  turned into a real link the same way docs/*.md renders —
                  inline() is the one parser both use. */}
              {type.docs_hint && inline(type.docs_hint)}
              {type.docs_steps?.length > 0 && (
                <ol>
                  {type.docs_steps.map((s, i) => (
                    <li key={i}>{inline(s)}</li>
                  ))}
                </ol>
              )}
              {type.docs_link && (
                <>
                  <br />
                  <a href={type.docs_link} target="_blank" rel="noreferrer">
                    Step-by-step guide
                  </a>
                </>
              )}
            </InfoIcon>
          )}
          {wanted && (
            <span className="chip conn-chip warn-chip">Recommended</span>
          )}
        </div>
        {/* One line on what the connector does, so a product name such as
            Composio reads as a job and not a brand. Same catalog sentence
            the info popover opens with. */}
        {type.docs_hint && (
          <div className="conn-sub">{inline(type.docs_hint)}</div>
        )}
        {/* The template said this agent needs it. Advisory on purpose: the
            sample desk still answers, Preview marks that as demo data, and
            nothing here gates Publish. */}
        {wanted && (
          <div className="hint">
            This agent works best with {type.label.split(" (")[0]}. Until you
            add it, booking runs on demo data that nobody receives.
          </div>
        )}
        {setup}
        {connections.length > 0 && (
          <div className="docs">
            {connections.map((c) => {
              const att = attachments.find((a) => a.connection_id === c.id);
              const st = STATUS[c.status] || { label: c.status, cls: "chip conn-chip" };
              const busy = busyId === c.id;
              // What this connection points at: the signed-in account, or
              // "user@database" for a pasted connection string. A sidecar
              // type's URL is Arya's own service, so never show that.
              const sub =
                c.detail ||
                c.account_email ||
                (type.url_source === "user" && c.url ? hostOf(c.url) : "");
              const allowed = att?.allowed_tools || [];
              const publicOn = (att?.public_tools || []).length > 0;
              // Write tools this connection offers that are not ticked. Used
              // only to warn when a sample desk that COULD write has been
              // switched off, leaving the agent able to look but not act.
              const missingWrites = (c.tools || []).filter(
                (x) => x.write && !allowed.includes(x.name)
              );
              // Sample desks this connection SERVES rather than replaces:
              // the type's keeps_when names a flag the check set on it (a
              // database with the order desk's own tables is shop_desk).
              const served = stubToolsets.filter((s) => c[type.keeps_when?.[s.id]]);
              const replaced = stubToolsets.filter((s) => !served.includes(s));
              const attachId = `attach-${c.id}`;
              return (
                <div className="doc" key={c.id}>
                  <span className="doc-name">
                    {c.name}
                    {sub && <span className="doc-when"> · {sub}</span>}
                  </span>
                  <span className={st.cls}>{st.label}</span>
                  {isOauth && c.status !== "connected" && (
                    <button
                      type="button"
                      className="btn ghost slim"
                      onClick={() => onSignIn(c)}
                      disabled={busy}
                    >
                      {busy ? "…" : "Sign in"}
                    </button>
                  )}
                  {(!isOauth || c.status === "connected" || c.status === "error") && (
                    <button
                      type="button"
                      className="btn ghost slim"
                      onClick={() => onTest(c)}
                      disabled={busy}
                    >
                      {busy ? "…" : "Test"}
                    </button>
                  )}
                  <button
                    className="doc-x"
                    type="button"
                    aria-label={`Remove ${c.name}`}
                    title="Remove"
                    onClick={() => onRemove(c)}
                    disabled={busy}
                  >
                    <Icon name="x" className="tiny" />
                  </button>
                  {c.error && <span className="doc-error">{c.error}</span>}
                  {/* Connected but worth saying out loud, e.g. the pasted
                      database login is a superuser. Same amber treatment as
                      an error: it needs the owner to act, not to panic. */}
                  {!c.error && (c.warnings || []).map((w) => (
                    <span className="doc-error" key={w}>{w}</span>
                  ))}
                  <label className="check-row conn-attach" htmlFor={attachId}>
                    <input
                      id={attachId}
                      type="checkbox"
                      checked={!!att}
                      onChange={(e) => (e.target.checked ? onAttach(c) : onDetach(c))}
                    />
                    <span><b>Attach to this agent</b></span>
                  </label>
                  {att && (
                    <div className="conn-tools">
                      <span className="label">
                        Tools this agent may call
                        {/* Descriptions live here, not beside each checkbox:
                            they are the tool docstrings, addressed to the
                            model, so inline they read as instructions to the
                            owner and were truncated mid-word. */}
                        {c.tools.length > 0 && (
                          <InfoIcon label="What each tool does">
                            <ol>
                              {c.tools.map((t) => (
                                <li key={t.name}>
                                  <b>{t.name}</b>
                                  {t.write ? " (writes)" : ""}
                                  {t.description ? `: ${t.description}` : ""}
                                </li>
                              ))}
                            </ol>
                          </InfoIcon>
                        )}
                      </span>
                      {c.tools.length === 0 ? (
                        <div className="hint">
                          No tool list yet. Click Test to load it from the server.
                        </div>
                      ) : (
                        <>
                          {c.tools.map((t) => {
                            const id = `${c.id}-${t.name}`;
                            const on = allowed.includes(t.name);
                            return (
                              <label className="check-row" key={t.name} htmlFor={id}>
                                <input
                                  id={id}
                                  type="checkbox"
                                  checked={on}
                                  onChange={(e) =>
                                    onTools(
                                      c,
                                      e.target.checked
                                        ? [...allowed, t.name]
                                        : allowed.filter((n) => n !== t.name)
                                    )
                                  }
                                />
                                <span>
                                  <b>{t.name}</b>
                                  {t.write && <span className="badge conn-writes">writes</span>}
                                </span>
                              </label>
                            );
                          })}
                          <div className="conn-links">
                            <button type="button" onClick={() => onTools(c, c.tools.map((t) => t.name))}>
                              Select all
                            </button>
                            <button type="button" onClick={() => onTools(c, [])}>
                              Select none
                            </button>
                          </div>
                        </>
                      )}
                      <label className="check-row" htmlFor={`public-${c.id}`} style={{ marginTop: 6 }}>
                        <input
                          id={`public-${c.id}`}
                          type="checkbox"
                          checked={publicOn}
                          onChange={(e) => onPublic(c, e.target.checked)}
                        />
                        <span>
                          <b>Allow in public embed sessions</b>
                          <span className="check-hint">
                            {" "}Visitors on your website can trigger the selected tools.
                            Leave off if any of them can create or change data.
                          </span>
                        </span>
                      </label>
                      {/* Stated, not offered. The sample desk and the real
                          system define the same tool names, and the sample
                          one wins the dedupe, so leaving both on meant
                          quoting a real free time and writing it into demo
                          data. Detaching brings the sample desk back. */}
                      {replaced.length > 0 && (
                        <div className="hint" style={{ marginTop: 6 }}>
                          Attaching this switches the sample{" "}
                          {replaced.map((s) => s.label.toLowerCase()).join(" and ")}{" "}
                          off for this agent, so it only uses your real system.
                        </div>
                      )}
                      {served.length > 0 && (
                        <div className="hint" style={{ marginTop: 6 }}>
                          This database has the tables the{" "}
                          {served.map((s) => s.label.toLowerCase()).join(" and ")}{" "}
                          need, so they stay on and use it instead of demo data.
                          WhatsApp users are known by their number.
                        </div>
                      )}
                      {/* Writes start unticked so nothing changes an
                          owner's data unasked. With the sample desk now off,
                          that leaves an agent that can quote real times and
                          book nothing, which is not what anyone attaching a
                          booking system wants. Say so rather than silently
                          changing the safe default. */}
                      {replaced.length > 0 && missingWrites.length > 0 && (
                        <div className="doc-error" style={{ marginTop: 6 }}>
                          The sample {replaced[0].label.toLowerCase()} could
                          also make changes. Tick{" "}
                          {missingWrites.map((t) => t.name).join(", ")}{" "}
                          so this agent still can.
                        </div>
                      )}
                      {/* Goes into the system prompt ("use these tools when the
                          caller asks about ..."). Naming the exact systems or
                          repos stops the model guessing identifiers. */}
                      <div className="field" style={{ marginTop: 8, marginBottom: 0 }}>
                        <label className="label" htmlFor={`purpose-${c.id}`}>
                          What these tools cover (optional)
                        </label>
                        <input
                          id={`purpose-${c.id}`}
                          className="input"
                          value={att.purpose || ""}
                          placeholder="e.g. our booking calendar, or the repos fastapi/fastapi and livekit/agents"
                          onChange={(e) => onPurpose(c, e.target.value)}
                        />
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        <div className="btn-row" style={{ marginTop: 12 }}>
          <button
            type="button"
            className="btn ghost slim"
            onClick={onAdd}
            disabled={!!connectDisabled || busyId === type.id}
            title={connectDisabled || undefined}
          >
            {busyId === type.id ? "…" : <><Icon name="plus" className="inline" /> {type.cta || `Add ${type.label}`}</>}
          </button>
          {connectDisabled && <span className="hint">{connectDisabled}</span>}
        </div>
      </div>
    </div>
  );
}
