import { useEffect, useRef, useState } from "react";
import { useRoomContext, useTranscriptions } from "@livekit/components-react";
import LinkChips from "./LinkChips.jsx";
import RichText from "./RichText.jsx";
import OptionCards from "./OptionCards.jsx";
import SourceCards from "./SourceCards.jsx";
import SuggestionChips from "./SuggestionChips.jsx";
import { sendTyped } from "./ChatInput.jsx";
import { withWrittenAddresses } from "../spokenEmail.js";

const norm = (s) => (s || "").replace(/\s+/g, " ").trim();

/* Pairs a stream of head-matched events (citations, options, links: all
   three publish {seq, head, ...payload} on the same content-matching
   contract) to the agent bubble whose text they belong to. Assignments
   are STICKY (via `assignedRef`, one per event stream) so a chip can't
   drift onto a later bubble when timing races reorder things; an event
   whose bubble is still streaming simply matches on a later render, once
   enough text has arrived. `pick(ev)` reads the payload array off the
   event shape (`ev.cits` / `ev.groups` / `ev.links`). */
function attachByHead(events, agentRows, assignedRef, pick) {
  const map = {};
  const taken = new Set(Object.values(assignedRef.current));
  events.forEach((ev, idx) => {
    let key = assignedRef.current[idx];
    if (!key && ev.head) {
      const head = norm(ev.head);
      const match = agentRows.find((r) => {
        if (taken.has(r.key)) return false;
        const t = norm(r.text);
        // full text starts with the head, or (mid-stream) the short
        // bubble text is itself a prefix of the head
        return t.startsWith(head) || (t.length >= 12 && head.startsWith(t));
      });
      if (match) {
        assignedRef.current[idx] = match.key;
        key = match.key;
        taken.add(match.key);
      }
    }
    if (key) map[key] = pick(ev);
  });
  return map;
}

/* Live transcript: spoken turns arrive as lk.transcription text streams
   (both the user's STT and the agent's synced speech text); typed chat
   messages are echoed locally by ChatInput and passed in via `typed`.

   Segments are ARCHIVED into local state as they stream: the hook's data
   resets when the room disconnects, and the archive is what keeps the
   whole conversation readable on the "Call ended" screen. */
export default function Transcript({
  typed,
  localIdentity,
  empty,
  userSpeaking,
  onStats,
  onArtifact,
  // How this surface shows what an answer drew on. `sources`: "chips"
  // (builder, the original chip-popover rendering), "collapsed" (widget
  // and share link, a one-line "Sources · N" disclosure, the v3
  // default), "cards" (always-open list, kept for a builder that wants
  // it), or "off" (no web results shown at all). `docChips`: whether
  // document citation chips render; false on both public surfaces,
  // defence in depth alongside the server-side gate.
  present = { sources: "chips", docChips: true },
  // Tap-to-select on option cards. The owner passes its typed-message echo
  // (the same one ChatInput gets) while the call is live; absent, rows are
  // static. Tapping row 2 sends its `pick` text exactly as typing it would.
  onPickSent,
  onPickFail,
  // Fires once a tapped pick or quick reply has actually been published,
  // so the owner can mark the echoed bubble as sent (the small tick).
  onPickAck,
}) {
  const room = useRoomContext();
  const transcriptions = useTranscriptions();
  const [archive, setArchive] = useState({});
  const [citEvents, setCitEvents] = useState([]);
  const [optEvents, setOptEvents] = useState([]);
  const [linkEvents, setLinkEvents] = useState([]);
  const [suggEvents, setSuggEvents] = useState([]);
  const [pickedMap, setPickedMap] = useState({}); // bubble key -> { groupIndex: n }
  const [artifact, setArtifact] = useState(null); // latest {url, title} — link is stable, content updates
  const [pendingUser, setPendingUser] = useState(false);
  const counter = useRef(0);
  const bottomRef = useRef(null);
  const listRef = useRef(null);
  const atBottom = useRef(true); // ref, not state — scroll shouldn't re-render
  const assignedRef = useRef({}); // citation event index -> bubble key (sticky)
  const assignedOptRef = useRef({}); // options event index -> bubble key (sticky)
  const assignedLinkRef = useRef({}); // link event index -> bubble key (sticky)
  const assignedSuggRef = useRef({}); // suggestion event index -> bubble key (sticky)
  const pendTimer = useRef(null);
  const onArtifactRef = useRef(onArtifact);
  onArtifactRef.current = onArtifact;

  // Grounding: the worker publishes each cited answer's sources on the
  // arya.citations text-stream topic, together with the answer's opening
  // words — rendered as source chips under the bubble whose text matches.
  useEffect(() => {
    if (!room) return;
    const handler = async (reader) => {
      try {
        const data = JSON.parse(await reader.readAll());
        const cits = data.citations || [];
        if (cits.length) {
          setCitEvents((ev) => [
            ...ev,
            { ts: Date.now(), seq: data.seq, head: data.head || "", cits },
          ]);
        }
      } catch {
        /* malformed event — ignore */
      }
    };
    try {
      room.registerTextStreamHandler("arya.citations", handler);
    } catch {
      /* already registered (StrictMode double-mount) */
    }
    return () => {
      try {
        room.unregisterTextStreamHandler("arya.citations");
      } catch {
        /* room already torn down */
      }
    };
  }, [room]);

  // Structured option groups (flight/hotel searches) — same contract as
  // citations: the event carries the answer's opening words and the cards
  // render under the bubble whose text matches.
  useEffect(() => {
    if (!room) return;
    const handler = async (reader) => {
      try {
        const data = JSON.parse(await reader.readAll());
        const groups = data.options || [];
        if (groups.length) {
          setOptEvents((ev) => [
            ...ev,
            { ts: Date.now(), seq: data.seq, head: data.head || "", groups },
          ]);
        }
      } catch {
        /* malformed event — ignore */
      }
    };
    try {
      room.registerTextStreamHandler("arya.options", handler);
    } catch {
      /* already registered */
    }
    return () => {
      try {
        room.unregisterTextStreamHandler("arya.options");
      } catch {
        /* room already torn down */
      }
    };
  }, [room]);

  // The one (or two) links an answer gives, resolved server-side against
  // the search results it drew on (backend/src/links.py), same
  // head-matching contract as citations and options.
  useEffect(() => {
    if (!room) return;
    const handler = async (reader) => {
      try {
        const data = JSON.parse(await reader.readAll());
        const linksPayload = data.links || [];
        if (linksPayload.length) {
          setLinkEvents((ev) => [
            ...ev,
            { ts: Date.now(), seq: data.seq, head: data.head || "", links: linksPayload },
          ]);
        }
      } catch {
        /* malformed event, ignore */
      }
    };
    try {
      room.registerTextStreamHandler("arya.links", handler);
    } catch {
      /* already registered */
    }
    return () => {
      try {
        room.unregisterTextStreamHandler("arya.links");
      } catch {
        /* room already torn down */
      }
    };
  }, [room]);

  // Quick replies (backend/src/suggestions.py): the template's openers
  // under the greeting, or chips derived from the question the reply ends
  // with. Same head-matching contract as the streams above. Rendered under
  // the LATEST agent bubble only, and gone once the caller has spoken or
  // typed, so a stale "Yes" can never sit under an old question. Remove
  // this block and the SuggestionChips line below to revert.
  useEffect(() => {
    if (!room) return;
    const handler = async (reader) => {
      try {
        const data = JSON.parse(await reader.readAll());
        const chips = (data.chips || []).filter((c) => typeof c === "string" && c.trim());
        if (chips.length) {
          setSuggEvents((ev) => [
            ...ev,
            { ts: Date.now(), seq: data.seq, head: data.head || "", chips: chips.slice(0, 4) },
          ]);
        }
      } catch {
        /* malformed event, ignore */
      }
    };
    try {
      room.registerTextStreamHandler("arya.suggestions", handler);
    } catch {
      /* already registered */
    }
    return () => {
      try {
        room.unregisterTextStreamHandler("arya.suggestions");
      } catch {
        /* room already torn down */
      }
    };
  }, [room]);

  // Written artifacts (the travel itinerary): the worker announces a stable
  // per-call link whenever the document is (re)generated — rendered as one
  // sticky "View itinerary" action, not per-bubble.
  useEffect(() => {
    if (!room) return;
    const handler = async (reader) => {
      try {
        const a = JSON.parse(await reader.readAll()).artifact;
        if (a?.url) {
          // The document exists from the first flight save, but surfacing
          // the button that early reads as noise - it appears once the trip
          // has substance (hotel or day plan saved, or a recap was asked
          // for). `ready` can only ever turn true.
          const ready =
            ["hotel", "plan", "recap"].includes(a.stage) ||
            !!(a.data && (a.data.hotel || (a.data.day_plan || []).length));
          setArtifact((prev) => ({
            url: a.url,
            title: a.title || "Itinerary",
            ready: ready || !!prev?.ready,
          }));
          // Surfaces the artifact (id, url, and its data payload) to the
          // owner component — the widget forwards it to the host page so
          // an embedding site can render its own itinerary panel.
          onArtifactRef.current?.(a);
        }
      } catch {
        /* malformed event — ignore */
      }
    };
    try {
      room.registerTextStreamHandler("arya.artifact", handler);
    } catch {
      /* already registered */
    }
    return () => {
      try {
        room.unregisterTextStreamHandler("arya.artifact");
      } catch {
        /* room already torn down */
      }
    };
  }, [room]);

  useEffect(() => {
    if (!transcriptions.length) return; // disconnect clears the hook — keep the archive
    setArchive((prev) => {
      const next = { ...prev };
      transcriptions.forEach((t, i) => {
        // STT publishes TWO streams per spoken segment (interim + final)
        // with different stream ids but a shared segment id — key by the
        // segment so the final text replaces the interim instead of
        // duplicating the bubble.
        const attrs = t.streamInfo?.attributes || {};
        const key =
          attrs["lk.segment_id"] || t.streamInfo?.id || `tr-${i}`;
        counter.current += 1;
        next[key] = {
          key,
          who: t.participantInfo?.identity === localIdentity ? "user" : "agent",
          text: t.text,
          // keep the first-seen order; streaming updates shouldn't reorder
          ts: next[key]?.ts ?? t.streamInfo?.timestamp ?? Date.now() + counter.current,
          // browser-clock arrival time — citation events are stamped with
          // the same clock, so pairing is immune to WSL/server clock drift
          seenAt: next[key]?.seenAt ?? Date.now(),
        };
      });
      return next;
    });
  }, [transcriptions, localIdentity]);

  // Spoken email addresses are written back as addresses, but only when
  // the address itself appears somewhere in this conversation, so nothing
  // is guessed. See spokenEmail.js.
  const rows = withWrittenAddresses(
    [
      ...Object.values(archive),
      ...typed.map((m) => ({ key: m.id, who: "user", text: m.text, ts: m.ts, sent: !!m.sent })),
    ]
      .filter((r) => r.text && r.text.trim())
      .sort((a, b) => a.ts - b.ts)
  );

  // Pending-speech bubble: STT text lags the actual speech, so a dots
  // bubble appears under YOU while the mic detects speech and stays up to
  // a grace period afterwards — cleared the moment the user's transcribed
  // row (or its interim text) lands. Guards: never before the agent's
  // first words have arrived (connection noise), and only after ~300ms of
  // sustained speech (mic blips otherwise flash the bubble on and off).
  const userCount = rows.filter((r) => r.who === "user").length;
  const hasAgentRow = rows.some((r) => r.who === "agent");
  const lastUserCount = useRef(userCount);
  useEffect(() => {
    if (userSpeaking) {
      clearTimeout(pendTimer.current);
      pendTimer.current = setTimeout(() => setPendingUser(true), 300);
    } else if (pendingUser) {
      clearTimeout(pendTimer.current);
      pendTimer.current = setTimeout(() => setPendingUser(false), 7000);
    } else {
      clearTimeout(pendTimer.current);
    }
    return () => clearTimeout(pendTimer.current);
  }, [userSpeaking]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (userCount > lastUserCount.current) {
      lastUserCount.current = userCount;
      clearTimeout(pendTimer.current);
      setPendingUser(false);
    }
  }, [userCount]);

  // Pair citation, option and link events to agent bubbles by CONTENT, not
  // time: each event carries the opening words (`head`) of the answer it
  // belongs to, and the chip/card goes to the bubble whose text matches.
  // Assignments are STICKY (attachByHead, above): once matched they never
  // move, so chips can't vanish or drift onto later bubbles when timing
  // races (commit vs TTS start) reorder things. An event whose bubble is
  // still streaming simply matches on a later render, when enough text has
  // arrived. Three independent maps: each event stream has its own sticky
  // ref, since the same index means different things across the streams.
  const agentRows = rows.filter((r) => r.who === "agent");
  const chipMap = attachByHead(citEvents, agentRows, assignedRef, (ev) => ev.cits);
  const optMap = attachByHead(optEvents, agentRows, assignedOptRef, (ev) => ev.groups);
  const linkMap = attachByHead(linkEvents, agentRows, assignedLinkRef, (ev) => ev.links);
  const suggMap = attachByHead(suggEvents, agentRows, assignedSuggRef, (ev) => ev.chips);

  // Auto-follow only while the user is at (or near) the bottom — scrolling
  // up to reread must hold position; scrolling back down resumes following.
  useEffect(() => {
    if (atBottom.current) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [rows.length, rows[rows.length - 1]?.text, pendingUser, optEvents.length, linkEvents.length, suggEvents.length]);

  // Report what the call actually contained, so the ended state can show a
  // summary instead of a dead input box. Keyed on the derived signature
  // rather than the arrays, which are rebuilt on every render.
  const citedFiles = [
    ...new Set(
      Object.values(chipMap)
        .flat()
        .filter((c) => c.type !== "web")
        .map((c) => c.filename)
    ),
  ];
  const citedWeb = Object.values(chipMap).flat().some((c) => c.type === "web");
  // A question that has been answered is not still asking. Every card used
  // to stay a live button for the whole call, so a hotel list from ninety
  // seconds earlier could still be tapped under a completely different
  // question, and "Option 2" arrived where a yes was expected. Only cards
  // newer than the caller's last turn stay tappable; older ones render as
  // the plain informational rows History already uses.
  const lastUserRow = rows.reduce((last, r, i) => (r.who === "user" ? i : last), -1);
  const statsKey = `${rows.length}|${citedFiles.join(",")}|${citedWeb}`;
  useEffect(() => {
    onStats?.({ turns: rows.length, files: citedFiles, web: citedWeb });
  }, [statsKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Recording a pick is a real state change (the row's look, not just an
  // outgoing message), so it lives in state rather than a sticky ref like
  // the head-matching maps above. Once a group has an entry, every row in
  // it renders static (OptionCards.jsx), so a second tap cannot send twice.
  const onOptionPick = (bubbleKey) => (it, group, groupIndex) => {
    // A multi-select choice card reports every ticked row at once.
    const picked = Array.isArray(it.multiPick) && it.multiPick.length ? it.multiPick : it.n;
    setPickedMap((prev) => ({
      ...prev,
      [bubbleKey]: { ...(prev[bubbleKey] || {}), [groupIndex]: picked },
    }));
    sendTyped(room, it.pick || `Option ${it.n}`, onPickSent, onPickFail, onPickAck);
  };

  // A quick reply is the caller's turn in words, sent exactly as if typed.
  const onSuggestionPick = (text) => {
    sendTyped(room, text, onPickSent, onPickFail, onPickAck);
  };

  // The newest agent bubble, and only when nothing from the caller follows
  // it: that is the one place quick replies make sense.
  const lastAgentIdx = rows.reduce((last, r, i) => (r.who === "agent" ? i : last), -1);
  const showSuggAt = onPickSent && !pendingUser && lastAgentIdx > lastUserRow ? lastAgentIdx : -1;

  return (
    <div
      className="messages"
      // The transcript IS the product's output, and it was previously a bare
      // div: a screen-reader user had to keep re-reading the region manually
      // to discover the agent had replied. additions-only so edits to a
      // streaming bubble don't re-announce the whole turn.
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      aria-label="Conversation transcript"
      ref={listRef}
      onScroll={() => {
        const el = listRef.current;
        if (el) {
          atBottom.current =
            el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }
      }}
    >
      {rows.length === 0 &&
        (empty || (
          <div className="pm-empty">
            Say something, or type below. The conversation appears here.
          </div>
        ))}
      {rows.map((r, i) => (
        <div key={r.key} className={r.who === "user" ? "pm-row user" : "pm-row"}>
          {/* Agent words get links and light formatting; a plain sentence
              renders exactly as before. Revert: put {r.text} back. */}
          <div className="pm-bub">
            {r.who === "agent" ? <RichText text={r.text} /> : r.text}
            {/* Typed and tapped turns: a small tick once the message has
                really left the browser (sendTyped's ack). Spoken turns
                have no such moment and show nothing. */}
            {r.who === "user" && r.sent && (
              <svg className="pm-sent" viewBox="0 0 24 24" aria-label="Sent" role="img">
                <polyline points="20 6 9 17 4 12" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            )}
          </div>
          {/* Top to bottom: the link the agent actually gave you, then the
              cards to choose from, then what it drew on. Shared with
              History (ConvDetail) so the live view and the stored
              transcript render identically; `present` flips the sources
              presentation between the builder's chips and the widget's
              collapsed disclosure. */}
          <LinkChips links={linkMap[r.key]} />
          <OptionCards
            groups={optMap[r.key]}
            picked={pickedMap[r.key] || {}}
            onPick={onPickSent && i > lastUserRow ? onOptionPick(r.key) : undefined}
          />
          <SourceCards
            cits={chipMap[r.key]}
            mode={present.sources}
            text={r.text}
            docChips={present.docChips}
            exclude={linkMap[r.key]}
          />
          {i === showSuggAt && (
            <SuggestionChips chips={suggMap[r.key]} onPick={onSuggestionPick} />
          )}
        </div>
      ))}
      {pendingUser && hasAgentRow && (
        <div className="pm-row user">
          <div className="pm-bub">
            <span className="pend-dots" aria-label="Transcribing your speech">
              <i></i><i></i><i></i>
            </span>
          </div>
        </div>
      )}
      {artifact?.ready && (
        <div className="itin-cta">
          {/* A small document card rather than an emoji chip: icon, the
              document's title, and what tapping it does. */}
          <a
            className="itin-card"
            href={new URL(artifact.url, window.location.origin).href}
            target="_blank"
            rel="noreferrer"
          >
            <span className="itin-ico" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
                <path d="M14 3v5h5M9 13h6M9 17h6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
            <span className="itin-body">
              <b>{artifact.title}</b>
              <span>View the itinerary so far. Opens in a new tab.</span>
            </span>
          </a>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  );
}
