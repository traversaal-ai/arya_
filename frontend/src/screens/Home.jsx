import { useCallback, useEffect, useState } from "react";
import { activateAgent, getAgents } from "../api.js";
import { IndustryIcon, Mic, industryTint } from "../components/AppIcons.jsx";
import ErrorNote from "../components/ErrorNote.jsx";

export default function Home({ onCreateNew, onOpenSaved }) {
  // null means "not loaded yet", distinct from []. The old code started at
  // [] and swallowed fetch errors with `.catch(() => {})`, so a backend
  // outage rendered as the confident claim that you have no agents.
  const [agents, setAgents] = useState(null);
  const [listErr, setListErr] = useState("");
  const [openErr, setOpenErr] = useState(null); // {message, retry}

  const load = useCallback(
    () =>
      getAgents()
        .then((list) => {
          setAgents(list);
          setListErr("");
        })
        .catch((e) => {
          setAgents(null);
          setListErr(`Couldn't load your saved agents: ${e.message}`);
        }),
    []
  );

  useEffect(() => {
    load();
  }, [load]);

  async function open(agent) {
    setOpenErr(null);
    try {
      const res = await activateAgent(agent.id);
      onOpenSaved(res.config);
    } catch (e) {
      setOpenErr({
        message: `Couldn't open "${agent.name}": ${e.message}`,
        retry: () => open(agent),
      });
    }
  }

  // The hills illustration from the landing site keeps the first visit from
  // being a lone lime band on an empty page. Only when there is nothing
  // else to show: saved agents are the content once they exist.
  const empty = agents !== null && agents.length === 0;

  return (
    <div>
      <h1 className="h-serif hero">
        Build a <em>voice agent</em> in minutes
      </h1>
      <p className="sub">
        Create a voice agent for your business and integrate it in your
        website.
      </p>

      {listErr && (
        <ErrorNote
          message={listErr}
          onRetry={load}
          style={{ marginBottom: 26 }}
        />
      )}
      {openErr && (
        <ErrorNote
          message={openErr.message}
          onRetry={openErr.retry}
          style={{ marginBottom: 26 }}
        />
      )}

      {agents === null && !listErr && <div className="spin" />}

      {agents && agents.length > 0 && (
        <>
          <h2 className="section-h">Your agents</h2>
          <div className="cards" style={{ marginBottom: 26 }}>
            {agents.map((a) => (
              <button
                className={`card ${industryTint(a.industry)}`.trim()}
                key={a.id}
                onClick={() => open(a)}
              >
                <div className="card-icon" aria-hidden="true">
                  <IndustryIcon value={a.industry} />
                </div>
                <div className="card-title">{a.name}</div>
                <div className="card-desc">{a.description}</div>
              </button>
            ))}
          </div>
        </>
      )}

      <div className={empty ? "home-build with-art" : "home-build"}>
        <div>
          {/* The heading separates this band from the saved agents above;
              with no saved agents the band stands alone and needs none. */}
          {agents && agents.length > 0 && (
            <h2 className="section-h">New agent</h2>
          )}
          <div className="cards">
            <button className="card primary" onClick={onCreateNew}>
              <div className="card-band">
                <div>
                  <div className="card-icon" aria-hidden="true"><Mic size={26} /></div>
                  <div className="card-title">Create a voice agent</div>
                  <div className="card-desc">
                    Build your own or start from a template.
                  </div>
                </div>
                {/* Decorative: the whole band is the button. */}
                <span className="btn on-lime card-cta" aria-hidden="true">
                  Get started
                </span>
              </div>
            </button>
          </div>
        </div>
        {empty && (
          <img
            className="home-art"
            src="/hero-landscape.svg"
            alt=""
            width="300"
            height="309"
          />
        )}
      </div>
    </div>
  );
}
