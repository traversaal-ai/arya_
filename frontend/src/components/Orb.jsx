// Aurora gradient sphere (ElevenLabs-style), driven by LiveKit's agent
// state. data-state: idle | listening | thinking | speaking — states
// modulate the swirl speed in CSS. `level` (0..1) is live agent-audio
// volume; the orb swells with speech via the --level custom property.
const STATE_MAP = {
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
};

export default function Orb({ state, level }) {
  const s = STATE_MAP[state] || "idle";
  return (
    // aria-hidden on purpose: this used to be role="img" with a changing
    // aria-label, which assistive tech does not reliably re-announce. The
    // state is carried instead by the visible .herostate text and by
    // AgentWindow's live region, so the orb is decoration here rather than
    // a second, unreliable copy of the same information.
    //
    // --level only set when the prop is given — otherwise it INHERITS, so a
    // wrapper element can drive the swell via CSS variable without React
    // re-renders (the widget's OrbBridge does exactly that).
    <div
      className="bigorb"
      data-state={s}
      style={
        level != null
          ? { "--level": Math.min(1, Math.max(0, level)) }
          : undefined
      }
      aria-hidden="true"
    >
      <span className="glow"></span>
      <span className="ringp p1"></span>
      <span className="ringp p2"></span>
      <span className="ringp p3"></span>
      <span className="sphere">
        <span className="fluid f1"></span>
        <span className="fluid f2"></span>
        <span className="fluid f3"></span>
        <span className="sheen"></span>
      </span>
    </div>
  );
}
