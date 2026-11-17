import { useCallback, useEffect, useState } from "react";
import {
  engineAddConnection,
  engineGetConnectors,
  engineListConnections,
  engineSetConnectors,
} from "../api.js";
import ErrorNote from "./ErrorNote.jsx";

/* The business database a pushed agent reads on an engine.

   Connections never travel with a push (they hold credentials), so an
   agent lands on an engine with none, and the engine has no Options
   screen to add one. This card does the two steps over the relay: save a
   Postgres connection on the engine, attach it to this agent there. The
   engine runs the same check as locally, so a database with the shop's
   tables is reported as one the order tools will use.

   Only Postgres, only one per agent: that is what a support agent needs
   today. Other connector types are still attached on the builder before a
   push, or over the API. */

export default function EngineDatabaseCard({ engine, agentId }) {
  const [conns, setConns] = useState(null);
  const [attached, setAttached] = useState(null); // full connectors list
  const [pick, setPick] = useState("");
  const [name, setName] = useState("");
  const [dsn, setDsn] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    setErr("");
    try {
      const [list, mine] = await Promise.all([
        engineListConnections(engine.id, "postgres"),
        engineGetConnectors(engine.id, agentId),
      ]);
      setConns(list);
      setAttached(mine.connectors || []);
    } catch (e) {
      setErr(e.message);
    }
  }, [engine.id, agentId]);

  useEffect(() => {
    load();
  }, [load]);

  const current = (attached || []).find((a) =>
    (conns || []).some((c) => c.id === a.connection_id)
  );
  const currentConn = current && (conns || []).find((c) => c.id === current.connection_id);
  const others = (attached || []).filter((a) => a !== current);

  async function attach(conn) {
    setBusy(true);
    setErr("");
    setNote("");
    try {
      const next = [
        ...others,
        {
          connection_id: conn.id,
          type: conn.type,
          allowed_tools: (conn.tools || []).filter((t) => !t.write).map((t) => t.name),
          public_tools: [],
        },
      ];
      await engineSetConnectors(engine.id, agentId, next);
      setNote(
        conn.shop_desk
          ? `Attached. The order tools on ${engine.name} now read and write ${conn.name}.`
          : `Attached. The agent on ${engine.name} can query ${conn.name}.`
      );
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function detach() {
    setBusy(true);
    setErr("");
    setNote("");
    try {
      await engineSetConnectors(engine.id, agentId, others);
      setNote("Detached. The agent falls back to its sample order tools.");
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function addAndAttach() {
    setBusy(true);
    setErr("");
    setNote("");
    try {
      const conn = await engineAddConnection(engine.id, {
        type: "postgres",
        name: name.trim(),
        credential: dsn.trim(),
      });
      setName("");
      setDsn("");
      await attach(conn);
    } catch (e) {
      setErr(e.message);
      setBusy(false);
    }
  }

  return (
    <>
      <h2 className="section-h">Business database</h2>
      <p className="hint" style={{ marginTop: -4 }}>
        The database this agent reads on {engine.name}. Connections stay on
        the engine, so this is set here rather than carried by the push.
      </p>

      {err && <ErrorNote message={err} onDismiss={() => setErr("")} />}
      {note && (
        <p className="hint" role="status" style={{ marginTop: 8 }}>
          {note}
        </p>
      )}
      {!conns && !err && <div className="spin" />}

      {currentConn && (
        <div className="docs">
          <div className="doc">
            <span className="doc-name">
              {currentConn.name}
              {currentConn.detail && <span className="doc-when"> · {currentConn.detail}</span>}
            </span>
            <span className={currentConn.status === "connected" ? "doc-status ready" : "doc-status failed"}>
              {currentConn.status}
            </span>
            <button className="btn ghost slim" type="button" onClick={detach} disabled={busy}>
              Detach
            </button>
          </div>
          <div className="hint" style={{ marginTop: 6 }}>
            {currentConn.shop_desk
              ? "This database has the tables the order tools need, so they stay on and use it. WhatsApp users are known by their number."
              : "Attached as a read-only database. It has no shop tables, so the sample order tools are switched off."}
          </div>
        </div>
      )}

      {conns && !currentConn && (
        <div className="chan-card">
          {conns.length > 0 && (
            <div className="field">
              <label className="label" htmlFor={`edb-pick-${engine.id}`}>
                A database already on {engine.name}
              </label>
              <div className="key-row">
                <select
                  id={`edb-pick-${engine.id}`}
                  className="select"
                  value={pick}
                  onChange={(e) => setPick(e.target.value)}
                >
                  <option value="">Choose…</option>
                  {conns.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}{c.shop_desk ? " (shop)" : ""}
                    </option>
                  ))}
                </select>
                <button
                  className="btn ghost slim"
                  type="button"
                  disabled={busy || !pick}
                  onClick={() => attach(conns.find((c) => c.id === pick))}
                >
                  Attach
                </button>
              </div>
            </div>
          )}
          <div className="field">
            <label className="label" htmlFor={`edb-name-${engine.id}`}>
              {conns.length > 0 ? "Or add a new one: name" : "Name"}
            </label>
            <input
              id={`edb-name-${engine.id}`}
              className="input"
              placeholder="Shop"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor={`edb-dsn-${engine.id}`}>
              Connection string
            </label>
            <input
              id={`edb-dsn-${engine.id}`}
              className="input"
              type="password"
              placeholder="postgresql://user:password@host:5432/dbname?sslmode=require"
              value={dsn}
              onChange={(e) => setDsn(e.target.value)}
            />
            <div className="hint">
              Checked on the engine before anything is stored. A hosted
              database (Supabase) needs no host change; a database on this
              machine is not reachable from an engine.
            </div>
          </div>
          <div className="btn-row">
            <button
              className="btn slim"
              type="button"
              disabled={busy || !name.trim() || !dsn.trim()}
              onClick={addAndAttach}
            >
              {busy ? "Checking…" : "Add and attach"}
            </button>
          </div>
        </div>
      )}
    </>
  );
}
