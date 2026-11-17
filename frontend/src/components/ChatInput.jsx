import { useState } from "react";
import { useRoomContext } from "@livekit/components-react";
import Icon from "./Icon.jsx";

/* Typed messages ride the lk.chat text-stream topic — the agent watches it
   natively, interrupts its speech, and answers with the same tools, RAG,
   and memory as voice.

   Renders bare (input + send, no wrapper) so the call screen can compose
   it into one row with the mic and end-call buttons. */
/* The one way a typed message leaves the browser, shared by the chat box,
   by tapping an option card and by tapping a quick reply (both via
   Transcript): optimistic echo first, latency matters mid-call, then the
   lk.chat publish. `onAck` fires once the publish resolved, so the echoed
   bubble can show its sent tick. */
export async function sendTyped(room, value, onSent, onFail, onAck) {
  onSent(value);
  try {
    await room.localParticipant.sendText(value, { topic: "lk.chat" });
    onAck?.(value);
  } catch (e) {
    console.error("sendText failed", e);
    onFail?.(value);
  }
}

export default function ChatInput({ onSent, onFail, onAck }) {
  const room = useRoomContext();
  const [text, setText] = useState("");

  function send() {
    const value = text.trim();
    if (!value) return;
    setText("");
    sendTyped(room, value, onSent, onFail, onAck);
  }

  return (
    <>
      <input
        className="chat-input"
        placeholder="Type instead of talking…"
        // The placeholder is the only visible caption, and placeholders are
        // not an accessible name — this is the field's real label.
        aria-label="Type a message to the agent"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.isComposing) send();
        }}
      />
      <button className="isend" onClick={send} title="Send" aria-label="Send">
        <Icon name="send" />
      </button>
    </>
  );
}
