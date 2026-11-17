import { useCallback, useEffect, useState } from "react";
import {
  engineGetDeployment,
  getDeployment,
  listEngines,
} from "../api.js";
import ChannelsCard from "../components/ChannelsCard.jsx";
import ErrorNote from "../components/ErrorNote.jsx";

/* Every place one agent answers on WhatsApp, one card each.

   "This builder" is the local deployment's number, the card that used to
   sit on Publish. Below it, one card per saved engine the agent has been
   pushed to: the same card, with every call relayed to that engine, so
   connecting a number on a VM is the same form as connecting one here.
   An engine the agent is not published on is listed as such, since the
   fix for that lives on Publish (push it), not here.

   Takes an agentId, not a cfg, for the same reason Publish does: opening
   this screen must not activate the agent. */

export default function Channels({ agentId, agentName, onBack, onPublish }) {
  const [local, setLocal] = useState(null); // deployment | "none" | null
  const [engines, setEngines] = useState(null); // [{engine, dep|null}] | null
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    setErr("");
    try {
      setLocal(await getDeployment(agentId));
    } catch (e) {
      if (e.status === 404) setLocal("none");
      else setErr(`Couldn't load this agent's deployment: ${e.message}`);
    }
    try {
      const list = await listEngines();
      const rows = await Promise.all(
        list.map(async (engine) => {
          try {
            return { engine, dep: await engineGetDeployment(engine.id, agentId) };
          } catch {
            return { engine, dep: null }; // not published there, or unreachable
          }
        })
      );
      setEngines(rows);
    } catch {
      setEngines([]);
    }
  }, [agentId]);

  useEffect(() => {
    load();
  }, [load]);

  const published = local && local !== "none";

  return (
    <div>
      <h1 className="h-serif">
        <em>Channels</em>
      </h1>
      <p className="sub">
        Where {agentName || "this agent"} answers on WhatsApp. A number
        belongs to one place: the builder you are using, or an engine the
        agent has been pushed to.
      </p>

      {err && <ErrorNote message={err} onRetry={load} />}
      {local === null && !err && <div className="spin" />}

      {local === "none" && (
        <div className="note" style={{ marginBottom: 18 }}>
          <b>This agent isn't published here.</b> A WhatsApp number hangs
          off a published agent, so publish it first.
          <div className="btn-row" style={{ marginTop: 12 }}>
            <button className="btn ghost slim" onClick={onPublish}>
              Open Publish
            </button>
          </div>
        </div>
      )}

      {published && (
        <ChannelsCard
          agentId={agentId}
          capConcurrent={local.max_concurrent_sessions}
          heading="This builder"
        />
      )}

      {engines && engines.length > 0 && (
        <>
          <h2 className="section-h" style={{ marginTop: 28 }}>
            Engines
          </h2>
          <p className="hint" style={{ marginTop: -4 }}>
            An engine keeps its own numbers. Connecting one here sets it on
            the engine, which answers Meta directly, no tunnel involved.
          </p>
          {engines.map(({ engine, dep }) =>
            dep ? (
              <ChannelsCard
                key={engine.id}
                agentId={agentId}
                engine={engine}
                capConcurrent={dep.max_concurrent_sessions}
                heading={engine.name}
              />
            ) : (
              <div className="note" key={engine.id} style={{ marginTop: 14 }}>
                <b>{engine.name}</b>: this agent isn't published there. Push it
                from Publish, then its number can be connected here.
              </div>
            )
          )}
        </>
      )}

      <div className="btn-row" style={{ marginTop: 24 }}>
        <button className="btn ghost" onClick={onBack}>
          Back
        </button>
      </div>
    </div>
  );
}
