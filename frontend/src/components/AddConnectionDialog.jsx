import { useId, useRef, useState } from "react";
import { useFocusTrap } from "../focus.js";

/* Add a connection of one connector type (Custom MCP server today; any
   non-OAuth type the catalog adds later). Same shape as Deployments'
   AddEngineDialog: name + URL + credential, Save disabled until the
   required fields are filled, the backend probes the server before it
   stores anything and its error comes back inline.

   Which fields show comes from the catalog entry: `url_source: "user"`
   asks for a URL, `credential_label` names the secret, and
   `credential_optional` lets it stay blank (a local server with no auth). */
export default function AddConnectionDialog({ type, open, busy, error, onSave, onCancel }) {
  const boxRef = useRef(null);
  const nameRef = useRef(null);
  const titleId = useId();
  const nameId = useId();
  const urlId = useId();
  const credId = useId();
  // Placeholder, not a value: pre-filling the type's own label meant most
  // rows ended up reading "Cal.com (appointment booking) · Cal.com
  // (appointment booking)". A name should say WHICH one this is.
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [cred, setCred] = useState("");

  useFocusTrap(boxRef, open, { initialFocus: nameRef });

  if (!open || !type) return null;

  const needsUrl = (type.url_source || "user") === "user";
  const credLabel = type.credential_label || "Token";
  const credOptional = !!type.credential_optional || type.auth === "none";
  const ready =
    name.trim() && (!needsUrl || url.trim()) && (credOptional || cred.trim());

  return (
    <div
      className="dialog-veil"
      onMouseDown={(e) => {
        if (boxRef.current && !boxRef.current.contains(e.target) && !busy) onCancel();
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
          {type.cta || `Add ${type.label}`}
        </div>
        <div className="dialog-body">
          <div className="field">
            <label className="label" htmlFor={nameId}>Name</label>
            <input
              id={nameId}
              className="input"
              ref={nameRef}
              placeholder={type.label}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          {needsUrl && (
            <div className="field">
              <label className="label" htmlFor={urlId}>Server URL</label>
              <input
                id={urlId}
                className="input"
                placeholder="https://example.com/mcp"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
            </div>
          )}
          {type.auth !== "none" && (
            <div className="field">
              <label className="label" htmlFor={credId}>
                {credLabel}{credOptional ? " (optional)" : ""}
              </label>
              <input
                id={credId}
                className="input"
                type="password"
                autoComplete="off"
                placeholder={type.credential_placeholder || ""}
                value={cred}
                onChange={(e) => setCred(e.target.value)}
              />
            </div>
          )}
          <div className="hint">
            {type.auth === "dsn"
              ? "Checked before saving: Arya connects and reports what it finds. The connection string stays on this machine and is never shown again."
              : "Checked before saving: the server must list its tools. The token stays on this machine and is never shown again."}
          </div>
          {error && <div className="err" role="alert">{error}</div>}
        </div>
        <div className="btn-row" style={{ marginTop: 18 }}>
          <button className="btn ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            className="btn"
            onClick={() =>
              onSave({
                type: type.id,
                name: name.trim(),
                url: needsUrl ? url.trim() : undefined,
                credential: cred.trim() || undefined,
              })
            }
            disabled={busy || !ready}
          >
            {busy ? "Checking..." : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
