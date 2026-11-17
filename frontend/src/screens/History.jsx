import { useCallback, useEffect, useId, useState } from "react";
import {
  engineConvMessages,
  engineConvTimeline,
  engineConversations,
  engineDeleteConversation,
  getConversations,
  listEngines,
} from "../api.js";
import ConvDetail, {
  evalScore,
  fmtDuration,
  fmtWhen,
  scoreClass,
} from "../components/ConvDetail.jsx";
import ErrorNote from "../components/ErrorNote.jsx";

/* The list half of the old History screen; the detail (transcript) view
   moved to components/ConvDetail.jsx so Insights can open the exact same
   conversation view from its table.

   Source dropdown mirrors Insights/Deployments: "Local" reads this
   machine's Postgres, an engine reads that engine's conversations through
   the backend proxy — including delete, which acts on the engine. */

// onVisitor(userId, source): open the Visitors screen on that person
// (source "" = local, else the engine id the row came from).
export default function History({ onBack, onDetail, onOpenAgent, onVisitor }) {
  const [convs, setConvs] = useState(null);
  const [selected, setSelected] = useState(null); // conversation row
  const [engines, setEngines] = useState([]);
  const [source, setSource] = useState(""); // "" = local
  const [err, setErr] = useState("");
  const sourceSelId = useId();

  // The topbar (owned by App) hides its menu while a transcript is open —
  // report which mode this screen is in.
  useEffect(() => {
    onDetail?.(!!selected);
    return () => onDetail?.(false);
  }, [selected, onDetail]);

  useEffect(() => {
    listEngines().then(setEngines).catch(() => {});
  }, []);

  const loadList = useCallback(
    () =>
      (source ? engineConversations(source) : getConversations())
        .then((list) => {
          setConvs(list);
          setErr("");
        })
        .catch((e) => setErr(`Couldn't load your conversations: ${e.message}`)),
    [source]
  );

  useEffect(() => {
    setConvs(null);
    loadList();
  }, [loadList]);

  if (selected) {
    return (
      <ConvDetail
        conv={selected}
        onBack={() => setSelected(null)}
        fetchMessages={
          source ? (cid) => engineConvMessages(source, cid) : undefined
        }
        fetchTimeline={
          source ? (cid) => engineConvTimeline(source, cid) : undefined
        }
        deleteFn={
          source ? (cid) => engineDeleteConversation(source, cid) : undefined
        }
        // Agent ids survive a push, so an engine transcript still opens
        // the local copy of that agent (404 handled upstream).
        onOpenAgent={onOpenAgent}
        onVisitor={onVisitor ? (uid) => onVisitor(uid, source) : undefined}
        onDeleted={() => {
          setSelected(null);
          // Refresh in place. This used to null the list first, which
          // unmounted every other row and flashed a spinner over the whole
          // screen to delete one item.
          loadList();
        }}
      />
    );
  }

  return (
    <div>
      <h1 className="h-serif">
        Past <em>conversations</em>
      </h1>
      <p className="sub">
        {source
          ? "Conversations recorded on the selected engine: transcripts, sources cited, and per-turn metrics."
          : "Every conversation is stored locally (Postgres): transcripts, sources cited, and per-turn metrics."}
      </p>
      {engines.length > 0 && (
        <div className="ins-filters">
          <label className="label" htmlFor={sourceSelId}>
            Source
          </label>
          <select
            id={sourceSelId}
            className="select slim"
            value={source}
            onChange={(e) => setSource(e.target.value)}
          >
            <option value="">Local</option>
            {engines.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
              </option>
            ))}
          </select>
        </div>
      )}
      {err && <ErrorNote message={err} onRetry={loadList} />}
      {!convs && !err && <div className="spin" />}
      {convs && convs.length === 0 && (
        <p className="sub">
          {source
            ? "No conversations on this engine yet."
            : "No conversations yet. Go talk to an agent!"}
        </p>
      )}
      {convs && convs.length > 0 && (
        <div className="docs">
          {convs.map((c) => (
            <button className="doc" key={c.id} onClick={() => setSelected(c)}>
              <span className="doc-name">
                {c.agent_name || c.room_name}
                <span className="doc-when"> · {fmtWhen(c.started_at)}</span>
                {/* Short visitor tag: enough to spot the same person across
                    rows, the full id lives on the transcript. */}
                {c.user_id && (
                  <span className="doc-when">
                    {" "}· visitor{" "}
                    <span className="vis-tag">{c.user_id.slice(0, 8)}</span>
                  </span>
                )}
              </span>
              {evalScore(c.evals) != null && (
                <span className={scoreClass(c.evals.score)}>
                  {Math.round(c.evals.score * 100)}%
                </span>
              )}
              <span className="doc-status">
                {fmtDuration(c.started_at, c.ended_at || c.last_message_at)}
              </span>
            </button>
          ))}
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
