import { useEffect, useRef, useState } from "react";
import { deleteDoc, listDocs, uploadDoc } from "../api.js";
import Icon from "./Icon.jsx";

// Upload limits and allowed formats come from the server
// (GET /api/setup/status → limits) via props — never mirrored here.
// A doc row's `busy` flag also comes from the server, so this file no
// longer enumerates the ingestion status state machine.
const FALLBACK_LIMITS = { max_upload_mb: 20, max_docs_per_agent: 5 };
const isBusy = (d) => d.busy ?? !["ready", "failed"].includes(d.status);

// Displayed percentage per stage. Queued docs sit at 0%; active stages
// show a single "processing" label with the percentage — extracting
// creeps client-side toward 70% (docling reports no page progress),
// embedding reports real progress (80→99%) from the server's batches.
function displayProgress(doc, firstSeen) {
  if (doc.status === "ready") return 100;
  if (doc.status === "failed") return null;
  if (doc.status === "extracting") {
    const started = firstSeen[doc.id] || Date.now();
    const creep = 15 + Math.floor((Date.now() - started) / 2500); // ~+1%/poll
    return Math.min(70, Math.max(doc.progress || 15, creep));
  }
  const base = { pending: 0, uploaded: 0, chunking: 75, embedding: 80 };
  return Math.max(doc.progress || 0, base[doc.status] ?? 0);
}

function statusLabel(doc, pct) {
  if (doc.status === "ready") return `ready · ${doc.chunk_count} chunks`;
  if (doc.status === "failed") return "failed";
  if (doc.status === "pending" || doc.status === "uploaded") return "queued";
  return `processing · ${pct}%`;
}

export default function KbUpload({ agentId, limits, formats }) {
  const maxMb = limits?.max_upload_mb ?? FALLBACK_LIMITS.max_upload_mb;
  const maxDocs = limits?.max_docs_per_agent ?? FALLBACK_LIMITS.max_docs_per_agent;
  // Per-agent rag.allowed_formats override wins; server default otherwise.
  const accept = (formats && formats.length ? formats : [".pdf", ".docx", ".md", ".txt"]);
  const [docs, setDocs] = useState([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef(null);
  const firstSeen = useRef({}); // doc id -> when we first saw it extracting

  async function refresh() {
    try {
      // Scoped view: this agent's docs + shared (untagged) ones — exactly
      // what the agent can retrieve.
      const ds = await listDocs(agentId);
      ds.forEach((d) => {
        if (d.status === "extracting" && !firstSeen.current[d.id]) {
          firstSeen.current[d.id] = Date.now();
        }
      });
      setDocs(ds);
    } catch {
      /* backend may not be up yet */
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  // Poll while anything is still ingesting.
  useEffect(() => {
    if (!docs.some(isBusy)) return;
    const t = setInterval(refresh, 2500);
    return () => clearInterval(t);
  }, [docs]);

  async function onPick(e) {
    const files = [...(e.target.files || [])];
    e.target.value = "";
    if (!files.length) return;
    setErr("");

    // Client-side guardrails — friendly message, no request fired.
    const problems = [];
    const live = docs.filter((d) => d.status !== "failed").length;
    if (live + files.length > maxDocs) {
      problems.push(
        `Limit is ${maxDocs} documents per agent; you have ${live} and picked ${files.length}.`
      );
    }
    const tooBig = files.filter((f) => f.size > maxMb * 1024 * 1024);
    tooBig.forEach((f) =>
      problems.push(
        `"${f.name}" is ${(f.size / 1024 / 1024).toFixed(1)} MB; max is ${maxMb} MB per file.`
      )
    );
    if (problems.length) {
      setErr(problems.join(" "));
      return;
    }

    setBusy(true);
    const failures = [];
    for (const file of files) {
      try {
        await uploadDoc(file, agentId); // sequential — keeps the queue readable
      } catch (ex) {
        failures.push(`"${file.name}": ${ex.message}`);
      }
    }
    if (failures.length) setErr(failures.join(" "));
    await refresh();
    setBusy(false);
  }

  return (
    <div>
      <input
        ref={fileRef}
        type="file"
        accept={accept.join(",")}
        multiple
        style={{ display: "none" }}
        onChange={onPick}
      />
      <button
        className="btn ghost"
        onClick={(e) => {
          e.preventDefault();
          fileRef.current?.click();
        }}
        disabled={busy}
        type="button"
      >
        {busy ? "Uploading…" : <><Icon name="plus" className="inline" /> Upload documents</>}
      </button>
      <span className="hint" style={{ marginLeft: 10 }}>
        {accept.map((f) => f.replace(".", "").toUpperCase()).join(", ")}
      </span>
      {err && <div className="err" role="alert">{err}</div>}
      {docs.length > 0 && (
        <div className="docs">
          {docs.map((d) => {
            const pct = displayProgress(d, firstSeen.current);
            const working = isBusy(d);
            const label = `${d.filename}${!d.agent_id && agentId ? " (shared)" : ""}`;
            return (
              <div className="doc" key={d.id}>
                <span className="doc-name">
                  {d.filename}
                  {!d.agent_id && agentId ? " · shared" : ""}
                </span>
                {working && pct !== null && (
                  <span
                    className="pbar"
                    role="progressbar"
                    aria-valuenow={pct}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-label={`Processing ${d.filename}`}
                  >
                    <i style={{ transform: `scaleX(${pct / 100})` }} />
                  </span>
                )}
                <span
                  className={
                    d.status === "ready"
                      ? "doc-status ready"
                      : d.status === "failed"
                        ? "doc-status failed"
                        : "doc-status"
                  }
                >
                  {statusLabel(d, pct)}
                </span>
                <button
                  className="doc-x"
                  type="button"
                  // Was title-only, so it announced as a bare glyph —
                  // for a control that deletes data, on a row where several
                  // identical ones sit stacked.
                  aria-label={`${working ? "Cancel and delete" : "Delete"} ${label}`}
                  title={working ? "Cancel and delete" : "Delete"}
                  onClick={async (e) => {
                    e.preventDefault();
                    setErr("");
                    try {
                      await deleteDoc(d.id);
                    } catch (ex) {
                      // Was swallowed, so a failed delete looked exactly like
                      // a successful one until the next poll put the row back.
                      setErr(`Couldn't delete "${d.filename}": ${ex.message}`);
                    }
                    refresh();
                  }}
                >
                  <Icon name="x" className="tiny" />
                </button>
                {/* The reason a document failed used to live in a title=
                    attribute — unreachable by keyboard, unread by screen
                    readers, so the user just re-uploaded the same bad file. */}
                {d.status === "failed" && d.error && (
                  <span className="doc-error">{d.error}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
