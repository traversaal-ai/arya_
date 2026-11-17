import { useEffect, useRef, useState } from "react";
import { adminHeaders, voicePreviewUrl } from "../api.js";

// A play button beside a voice picker. The clip is synthesised by the
// server the first time (with the owner's saved provider key) and cached on
// disk, so every later play is instant and free. One shared <audio> element
// across every instance: starting a voice stops the one already playing.
//
// Admin-only screen, so importing api.js here is fine; the widget never
// renders this.
let shared = null;
let sharedOwner = null; // the setter of the instance that is playing
function audio() {
  if (!shared) shared = new Audio();
  return shared;
}

export default function VoicePreview({ provider, model, voice, keyLabel }) {
  const [state, setState] = useState("idle"); // idle | loading | playing | nokey | error
  const [err, setErr] = useState("");
  const me = useRef(null);
  me.current = setState;

  // A voice or model change while playing stops the clip: the button would
  // otherwise claim to be playing a voice it is not.
  useEffect(() => {
    if (sharedOwner === me.current && shared && !shared.paused) {
      shared.pause();
      sharedOwner = null;
      setState("idle");
    }
  }, [provider, model, voice]);

  useEffect(() => () => {
    if (sharedOwner === me.current && shared) shared.pause();
  }, []);

  async function play() {
    const a = audio();
    if (state === "playing") {
      a.pause();
      sharedOwner = null;
      setState("idle");
      return;
    }
    if (sharedOwner && sharedOwner !== me.current) {
      a.pause();
      sharedOwner("idle");
    }
    sharedOwner = me.current;
    setErr("");
    setState("loading");
    try {
      // Fetch first (not <audio src>): a 409 or 502 arrives as JSON with a
      // reason, and the bytes are cached by the server anyway.
      const res = await fetch(voicePreviewUrl(provider, model, voice), {
        headers: adminHeaders(),
      });
      if (res.status === 409) {
        setState("nokey");
        return;
      }
      if (!res.ok) {
        let msg = `HTTP ${res.status}`;
        try {
          const j = await res.json();
          msg = j.detail || j.error || msg;
        } catch {
          /* not json */
        }
        setErr(msg);
        setState("error");
        return;
      }
      const blob = await res.blob();
      if (sharedOwner !== me.current) return; // another voice took over
      const url = URL.createObjectURL(blob);
      a.src = url;
      a.onended = () => {
        if (sharedOwner === me.current) {
          sharedOwner = null;
          setState("idle");
        }
        URL.revokeObjectURL(url);
      };
      await a.play();
      setState("playing");
    } catch (e) {
      setErr(e.message || "could not play");
      setState("error");
    }
  }

  const disabled = state === "loading" || !voice;
  const label =
    state === "playing" ? `Stop the sample of ${voice}` : `Play a sample of ${voice}`;
  return (
    <span className="voice-preview">
      <button
        type="button"
        className={"icon-btn voice-play" + (state === "playing" ? " on" : "")}
        onClick={play}
        disabled={disabled}
        aria-label={label}
        title={label}
      >
        {state === "loading" ? (
          <span className="spin tiny" aria-hidden="true" />
        ) : state === "playing" ? (
          <svg className="ic" viewBox="0 0 24 24" aria-hidden="true">
            <rect x="7" y="6" width="3.5" height="12" className="solid" />
            <rect x="13.5" y="6" width="3.5" height="12" className="solid" />
          </svg>
        ) : (
          <svg className="ic" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M8 5v14l11-7z" className="solid" />
          </svg>
        )}
      </button>
      {state === "nokey" && (
        <span className="hint voice-note">
          Save {keyLabel || "the provider"} key above to preview voices.
        </span>
      )}
      {state === "error" && (
        <span className="hint warn voice-note">Couldn't fetch a sample: {err}</span>
      )}
    </span>
  );
}
