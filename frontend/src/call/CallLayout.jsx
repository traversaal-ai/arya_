// Shared call-surface layout, used by BOTH the builder's AgentWindow and
// the embeddable widget/embed page. Like CallControls.jsx, it must stay
// free of any admin-API import (src/api.js must never reach the widget
// bundle — the widget build's forbidAdminApi guard enforces it).
import { useTrackVolume } from "@livekit/components-react";
import Orb from "../components/Orb.jsx";
import LiveRegion from "../components/LiveRegion.jsx";
import Icon from "../components/Icon.jsx";
import BrandMark from "../components/BrandMark.jsx";

// Audio-reactive wrapper: the orb swells with the agent's live voice
// amplitude. Isolated in its own component so the ~30fps volume updates
// re-render only the orb, never the whole layout.
function ReactiveOrb({ state, track }) {
  const level = useTrackVolume(track);
  return <Orb state={state} level={level} />;
}

export function TopBar({ name, topRight = null }) {
  return (
    <header className="topbar">
      <div className="brand">
        <BrandMark />
        <span className="brand-name">{name}</span>
      </div>
      <div className="spacer"></div>
      {topRight}
    </header>
  );
}

// One geometry for every call state (Ready / Live / Ended) — no layout
// jump. `controls` renders under the orb; `right` fills the chat pane.
// Orb-left / chat-right on wide viewports; the ≤760px stack also kicks in
// automatically inside the ~400px widget iframe.
//
// The live region lives here rather than in the live-only subtree so it
// also covers the Ready branch: "Ready" → "Connecting…" is the most
// anxious moment in the product and it used to announce nothing.
export default function CallLayout({
  name,
  topRight = null,
  orbState,
  stateLabel,
  announce,
  controls,
  startErr,
  right,
  audioTrack = null, // agent's audio TrackReference: orb reacts to speech
}) {
  return (
    <>
      <a className="skip-link" href="#call-main">
        Skip to the conversation
      </a>
      <TopBar name={name} topRight={topRight} />
      <main className="call-cols" id="call-main" tabIndex={-1}>
        <div className="call-left">
          <div className="voicehero stack">
            {audioTrack ? (
              <ReactiveOrb state={orbState} track={audioTrack} />
            ) : (
              <Orb state={orbState} />
            )}
            {/* An <h1>: the agent's name anchors the document outline. */}
            <h1 className="heroname">{name}</h1>
            <div className="herostate">{stateLabel}</div>
          </div>
          <div className="call-controls">{controls}</div>
          {startErr && <div className="err" role="alert">{startErr}</div>}
        </div>
        <div className="call-right">{right}</div>
      </main>
      <LiveRegion message={announce} />
    </>
  );
}

// Geometry-preserving footer when there's no live room to type into —
// ChatInput needs room context, so Ready/Ended states render this stand-in.
// The reason is real text, not only a placeholder: placeholders on disabled
// inputs are skipped by assistive tech, so the explanation was invisible.
export function DisabledInputRow({ message }) {
  return (
    <div className="input-area">
      <p className="hint" style={{ margin: "0 0 6px" }}>{message}</p>
      <div className="input-row" aria-hidden="true">
        <input className="chat-input" disabled placeholder="" tabIndex={-1} />
        <button className="isend" disabled tabIndex={-1}><Icon name="send" /></button>
      </div>
    </div>
  );
}
