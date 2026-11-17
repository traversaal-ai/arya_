// The embeddable call window (runs inside the loader's iframe, or
// standalone via the share link /embed/{pk}). Reuses the builder's call
// surface pieces — Orb, Transcript, ChatInput, CallControls — over the
// PUBLIC API only. States: boot → idle → live → ended, plus error.
//
// Two render paths:
// - FRAMED (inside the loader's iframe): a persistent single-column morph
//   layout (.wgt) — the orb starts big and centered, then smoothly shrinks
//   and docks to the top when the call connects (no unmount blink). Also
//   supports the loader's minimized PILL mode (host:view messages).
// - STANDALONE (full browser tab): centered hero → the shared CallLayout
//   split view, as before.
import { useEffect, useRef, useState } from "react";
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useIsSpeaking,
  useLocalParticipant,
  useRoomContext,
  useTrackVolume,
  useVoiceAssistant,
} from "@livekit/components-react";
import Orb from "../components/Orb.jsx";
import Transcript from "../components/Transcript.jsx";
import ChatInput from "../components/ChatInput.jsx";
import LiveRegion from "../components/LiveRegion.jsx";
import CallLayout, { DisabledInputRow } from "../call/CallLayout.jsx";
import {
  CallButton,
  Connecting,
  EndCallButton,
  MicToggle,
  STATE_ANNOUNCE,
  STATE_TEXT,
} from "../call/CallControls.jsx";
import { endSession, getAgentInfo, publicKey, startSession } from "./api.js";

const ERROR_COPY = {
  key_revoked: "This assistant is no longer available.",
  origin_not_allowed: "This assistant isn't enabled for this website.",
  quota_exceeded: "This assistant has reached its limit for now. Please try again later.",
  agent_gone: "This assistant no longer exists.",
  agent_offline: "The assistant is offline right now.",
  network: "Couldn't connect. Check your connection and try again.",
};

const FRAMED = window.self !== window.top;

// The publish of a typed or tapped message resolved: mark the newest unsent
// echo of that text as sent, so its bubble shows the small tick.
function markSent(list, text) {
  const i = list.map((r) => !r.sent && r.text === text).lastIndexOf(true);
  if (i < 0) return list;
  const next = list.slice();
  next[i] = { ...next[i], sent: true };
  return next;
}

function notifyHost(type, payload) {
  // Origin enforcement is the server's frame-ancestors header; the loader
  // additionally checks event.origin against its own script origin.
  if (window.parent !== window) {
    window.parent.postMessage({ source: "arya-widget", v: 1, type, payload }, "*");
  }
}

/* Bridges the live room out to the persistent framed layout: reports the
   agent state up (low-frequency), and pipes the ~30fps audio volume AROUND
   React by writing --level onto the hero element directly — the persistent
   <Orb> outside the room inherits it (Orb only sets its own --level when
   given a level prop). Renders nothing. */
function VolumeVar({ track, varEl }) {
  const level = useTrackVolume(track);
  useEffect(() => {
    varEl.current?.style.setProperty("--level", String(Math.min(1, level)));
  }, [level, varEl]);
  return null;
}

function OrbBridge({ onState, varEl, active }) {
  const { state, audioTrack } = useVoiceAssistant();
  useEffect(() => {
    onState(state);
  }, [state, onState]);
  useEffect(() => {
    if (!active) varEl.current?.style.setProperty("--level", "0");
  }, [active, varEl]);
  return active && audioTrack ? (
    <VolumeVar track={audioTrack} varEl={varEl} />
  ) : null;
}

/* Inner live subtree for the FRAMED layout — everything that needs room
   context. The transcript/input (full panel) or mini controls (pill). */
function FramedLive({ ended, mini, onEnd, onRestore, present }) {
  const room = useRoomContext();
  const { state } = useVoiceAssistant();
  const { localParticipant, isMicrophoneEnabled } = useLocalParticipant();
  const speaking = useIsSpeaking(localParticipant);
  const [typed, setTyped] = useState([]);
  const [sendErr, setSendErr] = useState(false);
  // Optimistic echo for anything typed, from the chat box or by tapping an
  // option card; the bubble shows before the publish resolves.
  const echoTyped = (text) => {
    setSendErr(false);
    setTyped((m) => [...m, { id: `typed-${Date.now()}`, text, ts: Date.now() }]);
  };
  const ackTyped = (text) => setTyped((m) => markSent(m, text));
  const userSpeaking =
    !ended && isMicrophoneEnabled && state === "listening" && speaking;
  const connected = ["listening", "thinking", "speaking"].includes(state);

  const hangUp = () => {
    onEnd();
    room?.disconnect();
  };

  const empty = ended ? (
    <div className="pm-empty">Conversation ended.</div>
  ) : connected ? (
    <Connecting text="Starting the conversation…" />
  ) : (
    <Connecting />
  );

  return (
    <>
      {/* Mini controls: CSS-shown only in pill mode. The transcript below
          stays MOUNTED in mini (hidden via CSS) — unmounting it dropped
          every message that streamed while minimized (seen live). */}
      <div className="wgt-mini-controls">
        {!ended && <MicToggle big={false} />}
        {!ended && <EndCallButton big={false} onEnd={hangUp} />}
        {mini && (
          <button
            type="button"
            className="icon-btn"
            onClick={onRestore}
            aria-label="Expand the assistant"
            title="Expand"
            autoFocus
          >
            <svg className="ic" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M18 15l-6-6-6 6" />
            </svg>
          </button>
        )}
      </div>
      <Transcript
        typed={typed}
        localIdentity={localParticipant?.identity}
        empty={empty}
        userSpeaking={userSpeaking}
        onArtifact={(a) => notifyHost("widget:itinerary", a)}
        present={present}
        onPickSent={ended ? undefined : echoTyped}
        onPickFail={() => setSendErr(true)}
        onPickAck={ackTyped}
      />
      {ended ? (
        <DisabledInputRow message="Conversation ended. Start a new one to chat." />
      ) : (
        <div className="input-area">
          {sendErr && (
            <div className="err" role="alert" style={{ margin: "0 0 8px" }}>
              Message didn't send. Check the conversation is still connected.
            </div>
          )}
          {/* One row: input grows, send stays glued to it, mic next,
              end-call at the far corner — max distance from send. */}
          <div className="input-row">
            <ChatInput onSent={echoTyped} onFail={() => setSendErr(true)} onAck={ackTyped} />
            <MicToggle big={false} />
            <EndCallButton big={false} onEnd={hangUp} />
          </div>
        </div>
      )}
    </>
  );
}

export default function Widget() {
  const [agent, setAgent] = useState(null);
  const [err, setErr] = useState(publicKey ? "" : "key_revoked");
  // Presentational default from the deployment (Publish & deploy's Sources
  // switch): true until the agent info loads, so the collapsed control
  // does not flash away then back on a slow connection. Never a security
  // gate; doc citations are withheld server-side regardless.
  const [showSources, setShowSources] = useState(true);
  const [conv, setConv] = useState(null);
  const [starting, setStarting] = useState(false);
  const [ended, setEnded] = useState(false);
  const [mini, setMini] = useState(false);
  const [agentState, setAgentState] = useState("idle");
  const heroRef = useRef(null);
  const mainRef = useRef(null);

  const live = !!conv;
  // Both widget surfaces (framed and standalone share link) show the same
  // presentation: the collapsed "Sources" control when the deployment
  // allows it, no document chips (the server already withholds them from
  // these sessions; this is the second gate, in code).
  const present = { sources: showSources ? "collapsed" : "off", docChips: false };

  useEffect(() => {
    if (!publicKey) return;
    getAgentInfo()
      .then((r) => {
        setAgent(r.agent);
        setShowSources(r.display?.show_sources ?? true);
        // Theme precedence: the loader's ?theme= param (applied pre-paint
        // in widget/main.jsx) wins; else the deployment's stored branding;
        // else light.
        if (
          !new URLSearchParams(location.search).get("theme") &&
          r.theme?.theme === "dark"
        ) {
          document.documentElement.dataset.theme = "dark";
        }
        notifyHost("widget:ready", { agentName: r.agent.name, theme: r.theme });
      })
      .catch((e) => {
        setErr(e.code || "network");
        notifyHost("widget:error", { code: e.code, fatal: true });
      });
  }, []);

  async function start() {
    setStarting(true);
    setErr("");
    setEnded(false);
    setConv(null); // remount the room subtree fresh on "New conversation"
    setAgentState("idle");
    try {
      const s = await startSession();
      setConv(s);
      notifyHost("widget:conversationStarted", { sessionId: s.conversation_id });
    } catch (e) {
      setErr(e.code || "network");
    } finally {
      setStarting(false);
    }
  }

  const endRef = useRef(null);
  async function end() {
    // No-op without a live call — closing an idle widget must NOT flip the
    // UI into "Conversation ended" (seen live: ⌄ before any call showed an ended
    // screen with stray separators on reopen).
    if (!conv || ended) return;
    setEnded(true);
    try {
      await endSession(conv.conversation_id);
    } catch {
      /* room may already be gone */
    }
    notifyHost("widget:conversationEnded", { sessionId: conv.conversation_id });
  }
  endRef.current = end;

  // Loader → app view control. The embedder's page origin is arbitrary, so
  // e.origin can't be pinned to a constant — validated instead by
  // e.source === window.parent plus the envelope. Acceptable: host:view
  // only changes layout (no privileged action), and the server's
  // frame-ancestors header already gates who may embed at all.
  useEffect(() => {
    if (!FRAMED) return;
    const onMsg = (e) => {
      const d = e.data;
      if (e.source !== window.parent) return;
      if (!d || d.source !== "arya-host" || d.v !== 1) return;
      if (d.type === "host:view") {
        const mode = d.payload?.mode;
        if (mode === "mini") setMini(true);
        else if (mode === "open") {
          setMini(false);
          mainRef.current?.focus();
        } else if (mode === "closed") {
          setMini(false);
          // No invisible hot-mic: closing the widget ends a live call.
          endRef.current?.();
        }
      }
    };
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, []);

  // A pill with a finished call in it is dead UI — retreat to the launcher.
  useEffect(() => {
    if (mini && ended) {
      setMini(false);
      notifyHost("widget:close");
    }
  }, [mini, ended]);

  if (err) {
    return (
      <div className="callframe">
        <div className="pm-empty" style={{ margin: "auto" }} role="alert">
          {ERROR_COPY[err] || ERROR_COPY.network}
        </div>
      </div>
    );
  }
  if (!agent) {
    return (
      <div className="callframe">
        <div style={{ margin: "auto" }}>
          <Connecting text="Loading…" />
        </div>
      </div>
    );
  }

  // ── STANDALONE full tab: hero → shared CallLayout (unchanged shape) ──
  if (!FRAMED) {
    if (!conv) {
      return (
        <div className="callframe">
          <div className="embed-hero">
            <Orb state="idle" />
            <h1 className="heroname">{agent.name}</h1>
            <div className="herostate">{starting ? "Connecting…" : ""}</div>
            <CallButton
              busy={starting}
              onClick={start}
              label={`Start a conversation with ${agent.name}`}
            />
          </div>
        </div>
      );
    }
    return (
      <div className="callframe" key={conv.room}>
        <LiveKitRoom
          serverUrl={conv.ws_url}
          token={conv.token}
          connect
          audio
          video={false}
          className="callroom"
          onDisconnected={() => setEnded(true)}
        >
          <RoomAudioRenderer />
          <StandaloneLive
            agent={agent}
            ended={ended}
            starting={starting}
            onEnd={end}
            onStart={start}
            present={present}
          />
        </LiveKitRoom>
      </div>
    );
  }

  // ── FRAMED: persistent morph layout — one tree across idle/live/ended ──
  const showState = ended || !live ? "idle" : agentState;
  const stateLabel = ended
    ? "Conversation ended"
    : live
      ? STATE_TEXT[agentState] || "…"
      : starting
        ? "Connecting…"
        : "Ready";
  const announce = ended
    ? "Conversation ended"
    : live
      ? STATE_ANNOUNCE[agentState] || ""
      : starting
        ? "Connecting to your agent"
        : "";

  // Docked (compact header) once ANY call has happened — the big centered
  // orb is a first-open hero only. Ended keeps the docked geometry so the
  // transcript stays readable instead of the orb ballooning back.
  const docked = live || ended;
  const frameCls =
    "callframe wgt " +
    (docked ? "docked" : "hero") +
    (live && !ended ? " live" : "") +
    (ended ? " ended" : "") +
    (mini ? " mini" : "");

  return (
    <div className={frameCls}>
      <a className="skip-link" href="#call-main">
        Skip to the conversation
      </a>
      <div className="wgt-chrome">
        <button
          type="button"
          className="icon-btn plain"
          aria-label={live && !ended ? "Minimize the assistant" : "Close the assistant"}
          title={live && !ended ? "Minimize" : "Close"}
          onClick={() =>
            notifyHost(live && !ended ? "widget:minimize" : "widget:close")
          }
        >
          <svg className="ic" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
      </div>
      <main className="wgt-col" id="call-main" tabIndex={-1} ref={mainRef}>
        <div className="wgt-spacer" aria-hidden="true" />
        <div className="voicehero stack wgt-hero" ref={heroRef}>
          <Orb state={showState} />
          <div className="wgt-id">
            <h1 className="heroname">{agent.name}</h1>
            <div className="herostate">{stateLabel}</div>
          </div>
          <div className="wgt-cta">
            {(!live || ended) && (
              <CallButton
                busy={starting}
                onClick={start}
                big={!docked}
                label={
                  ended
                    ? "New conversation"
                    : `Start a conversation with ${agent.name}`
                }
              />
            )}
          </div>
        </div>
        <div className="wgt-spacer" aria-hidden="true" />
        <div className="call-right wgt-body">
          {conv && (
            <LiveKitRoom
              key={conv.room}
              serverUrl={conv.ws_url}
              token={conv.token}
              connect
              audio
              video={false}
              className="callroom"
              onDisconnected={() => setEnded(true)}
            >
              <RoomAudioRenderer />
              <OrbBridge onState={setAgentState} varEl={heroRef} active={!ended} />
              <FramedLive
                ended={ended}
                mini={mini}
                onEnd={end}
                onRestore={() => notifyHost("widget:restore")}
                present={present}
              />
            </LiveKitRoom>
          )}
        </div>
      </main>
      <LiveRegion message={announce} />
    </div>
  );
}

/* Standalone live view: the shared CallLayout split, as before. */
function StandaloneLive({ agent, ended, starting, onEnd, onStart, present }) {
  const room = useRoomContext();
  const { state, audioTrack } = useVoiceAssistant();
  const { localParticipant, isMicrophoneEnabled } = useLocalParticipant();
  const speaking = useIsSpeaking(localParticipant);
  const [typed, setTyped] = useState([]);
  const [sendErr, setSendErr] = useState(false);
  // Optimistic echo for anything typed, from the chat box or by tapping an
  // option card; the bubble shows before the publish resolves.
  const echoTyped = (text) => {
    setSendErr(false);
    setTyped((m) => [...m, { id: `typed-${Date.now()}`, text, ts: Date.now() }]);
  };
  const ackTyped = (text) => setTyped((m) => markSent(m, text));
  const userSpeaking =
    !ended && isMicrophoneEnabled && state === "listening" && speaking;
  const showState = ended ? "idle" : state;
  const connected = ["listening", "thinking", "speaking"].includes(state);

  const empty = ended ? (
    <div className="pm-empty">Conversation ended.</div>
  ) : connected ? (
    <Connecting text="Starting the conversation…" />
  ) : (
    <Connecting />
  );

  const controls = ended ? (
    <CallButton busy={starting} onClick={onStart} label="New conversation" />
  ) : (
    <>
      <MicToggle />
      <EndCallButton
        onEnd={() => {
          onEnd();
          room?.disconnect();
        }}
      />
    </>
  );

  return (
    <CallLayout
      name={agent.name}
      orbState={showState}
      audioTrack={ended ? null : audioTrack}
      stateLabel={ended ? "Conversation ended" : STATE_TEXT[state] || "…"}
      announce={ended ? "Conversation ended" : STATE_ANNOUNCE[state] || ""}
      controls={controls}
      right={
        <>
          <Transcript
            typed={typed}
            localIdentity={localParticipant?.identity}
            empty={empty}
            userSpeaking={userSpeaking}
            onArtifact={(a) => notifyHost("widget:itinerary", a)}
            present={present}
            onPickSent={ended ? undefined : echoTyped}
            onPickFail={() => setSendErr(true)}
            onPickAck={ackTyped}
          />
          {ended ? (
            <DisabledInputRow message="Conversation ended. Start a new one to chat." />
          ) : (
            <div className="input-area">
              {sendErr && (
                <div className="err" role="alert" style={{ margin: "0 0 8px" }}>
                  Message didn't send. Check the conversation is still connected.
                </div>
              )}
              <div className="input-row">
                <ChatInput onSent={echoTyped} onFail={() => setSendErr(true)} onAck={ackTyped} />
              </div>
            </div>
          )}
        </>
      }
    />
  );
}
