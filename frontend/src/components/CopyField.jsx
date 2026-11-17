import { useId, useState } from "react";

/* A read-only value with a Copy button. Lifted out of Publish so the
   deployments list can reuse it.

   Single-line values render as a field with ghost buttons beside it.
   Multiline values (snippets) render as the landing site's run-block: a
   sage code box whose header row carries the label and the Copy button.

   `display` renders a masked form (e.g. a public key) behind Show/Hide.
   That is PRESENTATION ONLY: public keys ship in customers' page source
   by design; masking just keeps a screenshare from handing one out. Copy
   always writes the real `value`, and does it synchronously from state:
   awaiting a fetch first would lose the user-activation that Safari
   requires for clipboard writes. */
export default function CopyField({ label, value, display, multiline, hint }) {
  const id = useId();
  const [copied, setCopied] = useState(false);
  const [shown, setShown] = useState(false);
  const secret = display != null && display !== value;
  const text = secret && !shown ? display : value;

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // Older browsers / insecure contexts: the textarea+execCommand path.
      const ta = document.createElement("textarea");
      ta.value = value;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div className="field">
      {multiline ? (
        <div className="code-box">
          <div className="code-top">
            <label htmlFor={id}>{label}</label>
            <div className="code-actions">
              {secret && (
                <button
                  type="button"
                  className="copy-cmd"
                  onClick={() => setShown((s) => !s)}
                >
                  {shown ? "Hide" : "Show"}
                </button>
              )}
              <button
                type="button"
                className="copy-cmd"
                data-state={copied ? "done" : ""}
                onClick={copy}
              >
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
          </div>
          <textarea id={id} className="textarea mono" rows={3} readOnly value={text} />
        </div>
      ) : (
        <>
          <label className="label" htmlFor={id}>{label}</label>
          <div className="key-row">
            <input id={id} className="input" readOnly value={text} />
            {secret && (
              <button
                type="button"
                className="btn ghost slim"
                onClick={() => setShown((s) => !s)}
              >
                {shown ? "Hide" : "Show"}
              </button>
            )}
            <button type="button" className="btn ghost slim" onClick={copy}>
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </>
      )}
      {hint && <div className="hint">{hint}</div>}
      {copied && <span className="sr-only" role="status">Copied</span>}
    </div>
  );
}
