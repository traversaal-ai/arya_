import { useCallback, useEffect, useId, useState } from "react";
import {
  deleteVisitorMemory,
  editVisitorMemory,
  engineConvMessages,
  engineConvTimeline,
  engineDeleteConversation,
  engineDeleteVisitorMemory,
  engineEditVisitorMemory,
  engineForgetVisitor,
  engineVisitor,
  engineVisitors,
  forgetVisitor,
  getVisitor,
  listEngines,
  listVisitors,
} from "../api.js";
import ConvDetail, {
  evalScore,
  fmtDuration,
  fmtWhen,
  scoreClass,
} from "../components/ConvDetail.jsx";
import ConfirmDialog from "../components/ConfirmDialog.jsx";
import CopyField from "../components/CopyField.jsx";
import ErrorNote from "../components/ErrorNote.jsx";

/* Visitors: the people a published agent remembers. One row per browser
   (signed widget token) or channel customer (phone number via a bridge),
   grouped from conversations.metadata by the backend. Detail shows what
   Mem0 holds about them and every transcript, with the one button that
   forgets a person everywhere.

   Modelled on History: same Source dropdown (Local or an engine, through
   the backend relay), same row styling, same shared ConvDetail. The
   topbar menu hides while a detail is open, reported through onDetail.

   `focus` ({userId, source}) arrives from a transcript's "See all calls
   from this visitor" link; it seeds the initial state and is consumed
   once so a later visit through the menu lands on the list. */

const shortId = (uid) => (uid || "").slice(0, 8);

// Memories grouped by the agent that stored them. One group per agent, in
// first-seen order; memories saved under "all my agents" scope carry no
// agent and get their own group. A deleted agent falls back to its id.
function groupByAgent(memories) {
  const groups = new Map();
  for (const m of memories) {
    const key = m.agent_id || "";
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        label: key
          ? m.agent_name || key
          : "Shared across all your agents",
        items: [],
      });
    }
    groups.get(key).items.push(m);
  }
  return [...groups.values()];
}

export default function Visitors({
  onBack,
  onDetail,
  onOpenAgent,
  focus,
  onFocusConsumed,
}) {
  const [engines, setEngines] = useState([]);
  const [source, setSource] = useState(focus?.source || ""); // "" = local
  const [list, setList] = useState(null);
  const [selected, setSelected] = useState(focus?.userId || null); // user_id
  const [detail, setDetail] = useState(null); // {conversations, memories, note}
  const [conv, setConv] = useState(null); // open transcript row
  const [err, setErr] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  // One memory at a time: which is being edited ({id, text}), which is
  // awaiting a delete confirmation, and whether a change is in flight.
  const [editing, setEditing] = useState(null);
  const [memDelete, setMemDelete] = useState(null);
  const [memBusy, setMemBusy] = useState(false);
  const sourceSelId = useId();
  const editId = useId();

  useEffect(() => {
    if (focus) onFocusConsumed?.();
    // Consume once, on mount: the seed is already in state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    onDetail?.(!!selected || !!conv);
    return () => onDetail?.(false);
  }, [selected, conv, onDetail]);

  useEffect(() => {
    listEngines().then(setEngines).catch(() => {});
  }, []);

  const loadList = useCallback(
    () =>
      (source ? engineVisitors(source) : listVisitors())
        .then((rows) => {
          setList(rows);
          setErr("");
        })
        .catch((e) => setErr(`Couldn't load visitors: ${e.message}`)),
    [source]
  );

  const loadDetail = useCallback(
    (uid) =>
      (source ? engineVisitor(source, uid) : getVisitor(uid))
        .then((d) => {
          setDetail(d);
          setErr("");
        })
        .catch((e) => setErr(`Couldn't load this visitor: ${e.message}`)),
    [source]
  );

  useEffect(() => {
    setList(null);
    loadList();
  }, [loadList]);

  useEffect(() => {
    setDetail(null);
    if (selected) loadDetail(selected);
  }, [selected, loadDetail]);

  function changeSource(next) {
    // A visitor id means nothing on another source: back to that list.
    setSelected(null);
    setConv(null);
    setSource(next);
  }

  async function saveMemory() {
    const text = editing.text.trim();
    if (!text) return;
    setMemBusy(true);
    setErr("");
    try {
      await (source
        ? engineEditVisitorMemory(source, selected, editing.id, text)
        : editVisitorMemory(selected, editing.id, text));
      setEditing(null);
      await loadDetail(selected);
    } catch (e) {
      setErr(`Couldn't save this memory: ${e.message}`);
    } finally {
      setMemBusy(false);
    }
  }

  async function removeMemory() {
    setMemBusy(true);
    setErr("");
    try {
      await (source
        ? engineDeleteVisitorMemory(source, selected, memDelete.id)
        : deleteVisitorMemory(selected, memDelete.id));
      setMemDelete(null);
      await loadDetail(selected);
    } catch (e) {
      setErr(`Couldn't delete this memory: ${e.message}`);
      setMemDelete(null);
    } finally {
      setMemBusy(false);
    }
  }

  async function forget() {
    setBusy(true);
    setErr("");
    try {
      await (source ? engineForgetVisitor(source, selected) : forgetVisitor(selected));
      setConfirmOpen(false);
      setSelected(null);
      loadList();
    } catch (e) {
      setErr(`Couldn't forget this visitor: ${e.message}`);
      setConfirmOpen(false);
    } finally {
      setBusy(false);
    }
  }

  if (conv) {
    return (
      <ConvDetail
        conv={conv}
        onBack={() => setConv(null)}
        fetchMessages={
          source ? (cid) => engineConvMessages(source, cid) : undefined
        }
        fetchTimeline={
          source ? (cid) => engineConvTimeline(source, cid) : undefined
        }
        deleteFn={
          source ? (cid) => engineDeleteConversation(source, cid) : undefined
        }
        onOpenAgent={onOpenAgent}
        onDeleted={() => {
          setConv(null);
          // The visitor may have no calls left; the detail reloads to
          // show that honestly rather than a stale list.
          if (selected) loadDetail(selected);
        }}
      />
    );
  }

  if (selected) {
    const convs = detail?.conversations || [];
    const row = list?.find((v) => v.user_id === selected);
    const channel = row?.channel || convs.find((c) => c.channel)?.channel;
    return (
      <div>
        <h1 className="h-serif">
          Visitor <em>{shortId(selected)}</em>
          {channel && <span className="vis-chan">{channel}</span>}
        </h1>
        <p className="sub">
          {source
            ? "Everything the selected engine holds about this person: what the agent remembers, and every conversation."
            : "Everything this machine holds about this person: what the agent remembers, and every conversation."}
        </p>
        <CopyField label="Visitor id" value={selected} />
        {err && <ErrorNote message={err} onRetry={() => loadDetail(selected)} />}
        {!detail && !err && <div className="spin" />}
        {detail && (
          <>
            <h3 className="section-h">What the agent remembers</h3>
            {detail.memories === null && (
              <p className="sub">{detail.note || "Memories could not be listed."}</p>
            )}
            {detail.memories && detail.memories.length === 0 && (
              <p className="sub">
                Nothing yet. Memories appear once an agent with memory on has
                talked to this visitor.
              </p>
            )}
            {detail.memories &&
              groupByAgent(detail.memories).map((g) => (
                <section
                  key={g.key || "shared"}
                  className="vis-group"
                  aria-label={`Memories from ${g.label}`}
                >
                  <h4 className="vis-group-h">
                    {g.label}
                    <span className="doc-when">
                      {" "}· {g.items.length} memor{g.items.length === 1 ? "y" : "ies"}
                    </span>
                  </h4>
                  <ul className="vis-memories">
                    {g.items.map((m) => (
                      <li key={m.id} className="vis-mem">
                        {editing?.id === m.id ? (
                          <div className="vis-edit">
                            <label className="label" htmlFor={editId}>
                              Edit memory
                            </label>
                            <textarea
                              id={editId}
                              className="textarea"
                              rows={3}
                              maxLength={2000}
                              value={editing.text}
                              autoFocus
                              onChange={(e) =>
                                setEditing({ id: m.id, text: e.target.value })
                              }
                            />
                            <div className="btn-row" style={{ marginTop: 8 }}>
                              <button
                                className="btn slim"
                                onClick={saveMemory}
                                disabled={memBusy || !editing.text.trim()}
                              >
                                {memBusy ? "Saving…" : "Save"}
                              </button>
                              <button
                                className="btn ghost slim"
                                onClick={() => setEditing(null)}
                                disabled={memBusy}
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <>
                            <span className="vis-mem-text">{m.memory}</span>
                            <span className="vis-mem-actions">
                              <button
                                type="button"
                                className="linkish"
                                onClick={() => setEditing({ id: m.id, text: m.memory })}
                                aria-label={`Edit memory: ${m.memory}`}
                              >
                                Edit
                              </button>
                              <button
                                type="button"
                                className="linkish danger-link"
                                onClick={() => setMemDelete(m)}
                                aria-label={`Delete memory: ${m.memory}`}
                              >
                                Delete
                              </button>
                            </span>
                          </>
                        )}
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
            <h3 className="section-h">Conversations</h3>
            {convs.length === 0 && (
              <p className="sub">No conversations are left for this visitor.</p>
            )}
            {convs.length > 0 && (
              <div className="docs">
                {convs.map((c) => (
                  <button className="doc" key={c.id} onClick={() => setConv(c)}>
                    <span className="doc-name">
                      {c.agent_name || c.room_name}
                      <span className="doc-when"> · {fmtWhen(c.started_at)}</span>
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
          </>
        )}
        <div className="btn-row">
          <button className="btn ghost" onClick={() => setSelected(null)} disabled={busy}>
            Back to list
          </button>
          <button
            className="btn danger push-end"
            onClick={() => setConfirmOpen(true)}
            disabled={busy || !detail}
          >
            {busy ? "Forgetting…" : "Forget this visitor"}
          </button>
        </div>
        <ConfirmDialog
          open={!!memDelete}
          title="Delete this memory?"
          body={
            memDelete
              ? `The agent will no longer remember: "${memDelete.memory}". This cannot be undone.`
              : ""
          }
          confirmLabel="Delete"
          danger
          busy={memBusy}
          onConfirm={removeMemory}
          onCancel={() => setMemDelete(null)}
        />
        <ConfirmDialog
          open={confirmOpen}
          title="Forget this visitor?"
          body="Deletes everything remembered about this visitor and all of their conversations. This cannot be undone."
          confirmLabel="Forget"
          danger
          busy={busy}
          onConfirm={forget}
          onCancel={() => setConfirmOpen(false)}
        />
      </div>
    );
  }

  return (
    <div>
      <h1 className="h-serif">
        Your <em>visitors</em>
      </h1>
      <p className="sub">
        {source
          ? "Everyone who has talked to an agent on the selected engine. Open one to see what the agent remembers, or to forget them."
          : "Everyone who has talked to one of your agents. Open one to see what the agent remembers, or to forget them."}
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
            onChange={(e) => changeSource(e.target.value)}
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
      {!list && !err && <div className="spin" />}
      {list && list.length === 0 && (
        <p className="sub">
          No visitors yet. Visitors appear after the first conversation on a published
          agent.
        </p>
      )}
      {list && list.length > 0 && (
        <div className="docs">
          {list.map((v) => (
            <button className="doc" key={v.user_id} onClick={() => setSelected(v.user_id)}>
              <span className="doc-name">
                <span className="vis-tag">{shortId(v.user_id)}</span>
                {v.channel && <span className="vis-chan">{v.channel}</span>}
                <span className="doc-when">
                  {" "}· {v.calls} conversation{v.calls === 1 ? "" : "s"} · last{" "}
                  {fmtWhen(v.last_seen)}
                </span>
                {v.agent_names && v.agent_names.length > 0 && (
                  <span className="doc-when"> · {v.agent_names.join(", ")}</span>
                )}
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
