import { useEffect, useRef, useState } from "react";
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useIsSpeaking,
  useLocalParticipant,
  useRoomContext,
  useVoiceAssistant,
} from "@livekit/components-react";
import { endConversation } from "../api.js";
import Transcript from "../components/Transcript.jsx";
import ChatInput from "../components/ChatInput.jsx";
import Menu from "../components/Menu.jsx";
import ThemeToggle from "../components/ThemeToggle.jsx";
import CallLayout, { DisabledInputRow } from "../call/CallLayout.jsx";
import {
  CallButton,
  Connecting,
  EndCallButton,
  MicToggle,
  STATE_ANNOUNCE,
  STATE_TEXT,
  fmtDuration,
} from "../call/CallControls.jsx";

/* What the call actually contained, in the slot the disabled input used to
   occupy. This is the last thing every user sees in the product's core loop;
   it used to be the word "Call ended" three times over a dead form field. */
function CallSummary({ lost, durationMs, stats }) {
  const cited = [...stats.files];
  if (stats.web) cited.push("web search");
  return (
    <div className="input-area">
      <div className="callsum">
        <div className="callsum-title">
          {lost ? "Connection lost" : "Conversation ended"}
        </div>
        <div className="callsum-grid">
          <span className="callsum-item">
            Length <b>{fmtDuration(durationMs)}</b>
          </span>
          <span className="callsum-item">
            Turns <b>{stats.turns}</b>
          </span>
        </div>
        {cited.length > 0 && (
          <div className="callsum-src">
            Answered using {cited.join(", ")}.
          </div>
        )}
        {lost && (
          <div className="callsum-src">
            The connection dropped or the agent timed out. Reconnect to pick
            up with a new conversation; this transcript stays here.
          </div>
        )}
      </div>
    </div>
  );
}

function CallUI({ cfg, ended, lost, starting, startErr, onEnd, onStartCall, menuItems }) {
  const room = useRoomContext();
  const { state, audioTrack } = useVoiceAssistant();
  const { localParticipant, isMicrophoneEnabled } = useLocalParticipant();
  const speaking = useIsSpeaking(localParticipant);
  // Pending-dots gate: only during a live turn where the agent is actually
  // listening, with the mic on. Kills the false triggers seen in testing —
  // connection-phase noise, and the greeting's echo picked up by the mic
  // (state is "speaking" then, not "listening").
  const userSpeaking =
    !ended && isMicrophoneEnabled && state === "listening" && speaking;
  const [typed, setTyped] = useState([]);
  const [sendErr, setSendErr] = useState(false);
  // Optimistic echo for anything typed, from the chat box or by tapping an
  // option card; the bubble shows before the publish resolves.
  const echoTyped = (text) => {
    setSendErr(false);
    setTyped((m) => [...m, { id: `typed-${Date.now()}`, text, ts: Date.now() }]);
  };
  // The publish resolved: mark the newest unsent echo of that text as sent
  // (the small tick on the bubble).
  const ackTyped = (text) => {
    setTyped((m) => {
      const i = m.map((r) => !r.sent && r.text === text).lastIndexOf(true);
      if (i < 0) return m;
      const next = m.slice();
      next[i] = { ...next[i], sent: true };
      return next;
    });
  };
  const [stats, setStats] = useState({ turns: 0, files: [], web: false });

  const showState = ended ? "idle" : state;
  const connected = ["listening", "thinking", "speaking"].includes(state);

  // Call clock: starts at the first genuinely-connected state, stops when the
  // call ends, so the summary can report a real length.
  const [startedAt, setStartedAt] = useState(null);
  const [endedAt, setEndedAt] = useState(null);
  useEffect(() => {
    if (connected && startedAt == null) setStartedAt(Date.now());
  }, [connected, startedAt]);
  useEffect(() => {
    if (ended && endedAt == null) setEndedAt(Date.now());
  }, [ended, endedAt]);

  // No "say something" placeholder while the call is spinning up — show
  // progress until the first transcript row lands.
  const empty = ended ? (
    <div className="pm-empty">Conversation ended.</div>
  ) : connected ? (
    <Connecting text="Starting the conversation…" />
  ) : (
    <Connecting />
  );

  const controls = ended ? (
    // Home lives in the ⋮ menu — one button under the orb, centered.
    <CallButton
      busy={starting}
      onClick={onStartCall}
      label={lost ? "Reconnect" : "New conversation"}
    />
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

  const endedLabel = lost ? "Connection lost" : "Conversation ended";

  return (
      <CallLayout
        name={cfg?.name || "Arya"}
        topRight={<><Menu items={menuItems} /><ThemeToggle /></>}
        orbState={showState}
        audioTrack={ended ? null : audioTrack}
        stateLabel={ended ? endedLabel : STATE_TEXT[state] || "Ready"}
        announce={ended ? endedLabel : STATE_ANNOUNCE[state] || ""}
        controls={controls}
        startErr={ended ? startErr : ""}
        right={
          <>
            <Transcript
              typed={typed}
              localIdentity={localParticipant?.identity}
              empty={empty}
              userSpeaking={userSpeaking}
              onStats={setStats}
              onPickSent={ended ? undefined : echoTyped}
              onPickFail={() => setSendErr(true)}
              onPickAck={ackTyped}
            />
            {ended ? (
              <CallSummary
                lost={lost}
                durationMs={
                  startedAt != null && endedAt != null ? endedAt - startedAt : null
                }
                stats={stats}
              />
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

export default function AgentWindow({
  cfg,
  conv,
  onStartCall,
  onEdit,
  onPreview,
  onHistory,
  onInsights,
  onVisitors,
  onPublish,
  onHome,
}) {
  // ended flips on any disconnect: the end-call button, the worker's
  // inactivity watchdog deleting the room, or a drop. `lost` separates the
  // ones the user didn't ask for — those used to render the identical "Call
  // ended" screen, so a dropped call was indistinguishable from a hang-up.
  const [ended, setEnded] = useState(false);
  const [lost, setLost] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startErr, setStartErr] = useState("");
  // Intent, not a LiveKit reason code: this is the one signal we own outright,
  // so it can't drift with the client library's enum.
  const hungUpRef = useRef(false);

  const live = !!conv;

  async function endNow() {
    hungUpRef.current = true;
    setEnded(true);
    try {
      await endConversation(conv.conversation_id); // deletes the room
    } catch (e) {
      // Room may already be gone (watchdog, double click) — but keep the
      // evidence visible: a silent swallow here hid a real failure once.
      console.error("end conversation failed", e);
    }
  }

  // Creates the room + worker; on success App swaps `conv` and this window
  // remounts fresh (key change), so no state reset is needed here.
  async function startCall() {
    setStarting(true);
    setStartErr("");
    try {
      await onStartCall();
    } catch (e) {
      setStartErr(e.message);
      setStarting(false);
    }
  }

  // Menu navigation hangs up first — leaving the screen mid-call would
  // otherwise keep the room (and the agent) alive.
  async function navTo(go) {
    if (live && !ended) await endNow();
    go();
  }
  // Grouped by intent (manager-specified IA): agent lifecycle actions under
  // Configure & Deploy, observability under Monitor. Home leads unheaded —
  // it is a destination, not a category. Every pick hangs up first.
  const menuItems = [
    { label: "Home", onPick: () => navTo(onHome) },
    { heading: "Configure & Deploy" },
    { label: "Edit agent", onPick: () => navTo(onEdit) },
    // Back to the Preview screen this call was started from.
    { label: "Preview agent", onPick: () => navTo(onPreview) },
    // Opens Publish & deploy: keys/limits when already published, the
    // publish card when not — the destination screen tells them apart.
    { label: "Publish & deploy", onPick: () => navTo(onPublish) },
    { heading: "Monitor" },
    { label: "Dashboard", onPick: () => navTo(onInsights) },
    { label: "Conversations", onPick: () => navTo(onHistory) },
    { label: "Visitors", onPick: () => navTo(onVisitors) },
  ];

  // Ready state: no room or worker exists yet — the green button creates
  // the conversation, so the greeting plays only once someone is listening.
  if (!live) {
    return (
      <div className="callframe">
          <CallLayout
            name={cfg?.name || "Arya"}
            topRight={<><Menu items={menuItems} /><ThemeToggle /></>}
            orbState="idle"
            stateLabel={starting ? "Connecting…" : "Ready"}
            announce={
              starting ? "Connecting to your agent" : "Ready to talk to your agent"
            }
            controls={
              <CallButton busy={starting} onClick={startCall} label="Start conversation" />
            }
            startErr={startErr}
            right={
              <>
                <div className="messages">
                  {starting ? (
                    <Connecting />
                  ) : (
                    <div className="pm-empty">
                      Ready when you are. Press the green button to start a conversation.
                    </div>
                  )}
                </div>
                <DisabledInputRow message="Start a conversation to type to your agent." />
              </>
            }
          />
      </div>
    );
  }

  return (
    <div className="callframe">
        <LiveKitRoom
          serverUrl={conv.ws_url}
          token={conv.token}
          connect
          audio
          video={false}
          className="callroom"
          onDisconnected={() => {
            setEnded(true);
            if (!hungUpRef.current) setLost(true);
          }}
        >
          <RoomAudioRenderer />
          <CallUI
            cfg={cfg}
            ended={ended}
            lost={lost}
            starting={starting}
            startErr={startErr}
            onEnd={endNow}
            onStartCall={startCall}
            menuItems={menuItems}
          />
        </LiveKitRoom>
    </div>
  );
}
