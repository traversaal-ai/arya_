// Shared call-surface pieces, used by BOTH the builder's AgentWindow and
// the embeddable widget. Deliberately free of any admin-API import
// (src/api.js must never reach the widget bundle) — anything needing
// backend calls stays in the screen that owns them.
import { useLocalParticipant } from "@livekit/components-react";

export const STATE_TEXT = {
  disconnected: "Disconnected",
  connecting: "Connecting…",
  initializing: "Getting ready…",
  listening: "Listening",
  thinking: "Thinking…",
  speaking: "Speaking",
};

// Spoken form of the same states. The orb conveys them through animation
// speed alone, which is silent to a screen reader and invisible to anyone
// who asked for reduced motion, so they are announced as sentences too.
export const STATE_ANNOUNCE = {
  connecting: "Connecting to your agent",
  initializing: "Agent is getting ready",
  listening: "Agent is listening",
  thinking: "Agent is thinking",
  speaking: "Agent is speaking",
};

export const PHONE_PATH =
  "M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.79 19.79 0 0 1 2.08 4.18 2 2 0 0 1 4.06 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z";

export function fmtDuration(ms) {
  if (ms == null) return "0s";
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

// Green circular call button — starts the first call and any "call again".
export function CallButton({ busy, onClick, label, big = true }) {
  return (
    <button
      type="button"
      className={big ? "icon-btn call big" : "icon-btn call"}
      disabled={busy}
      onClick={onClick}
      aria-label={label}
      title={label}
    >
      <svg className="ic" viewBox="0 0 24 24" aria-hidden="true">
        <path d={PHONE_PATH} />
      </svg>
    </button>
  );
}

export function Connecting({ text = "Connecting…" }) {
  return (
    <div className="pm-empty">
      <div className="spin" style={{ marginBottom: 10 }} />
      <div>{text}</div>
    </div>
  );
}

// Red hang-up circle. `onEnd` should both tell the server to delete the
// room AND drop the local connection (belt and braces — the call is over
// for the user even if the API request fails).
export function EndCallButton({ onEnd, big = true }) {
  return (
    <button
      type="button"
      className={big ? "icon-btn danger big" : "icon-btn danger"}
      onClick={onEnd}
      aria-label="End conversation"
      title="End conversation"
    >
      <svg className="ic hangup" viewBox="0 0 24 24" aria-hidden="true">
        <path d={PHONE_PATH} />
      </svg>
    </button>
  );
}

export function MicToggle({ big = true }) {
  const { localParticipant, isMicrophoneEnabled } = useLocalParticipant();
  const label = isMicrophoneEnabled ? "Mute microphone" : "Unmute microphone";
  const size = big ? " big" : "";
  return (
    <button
      type="button"
      className={(isMicrophoneEnabled ? "icon-btn" : "icon-btn muted") + size}
      onClick={() => localParticipant?.setMicrophoneEnabled(!isMicrophoneEnabled)}
      aria-label={label}
      aria-pressed={!isMicrophoneEnabled}
      title={label}
    >
      {isMicrophoneEnabled ? (
        <svg className="ic" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
          <path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v4M8 23h8" />
        </svg>
      ) : (
        <svg className="ic" viewBox="0 0 24 24" aria-hidden="true">
          <line x1="1" y1="1" x2="23" y2="23" />
          <path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6" />
          <path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2c0 .41-.04.82-.11 1.23" />
          <path d="M12 19v4M8 23h8" />
        </svg>
      )}
    </button>
  );
}
