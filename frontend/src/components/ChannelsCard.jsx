import { useCallback, useEffect, useState } from "react";

import {
  addChannel,
  deleteChannel,
  engineAddChannel,
  engineDeleteChannel,
  engineListChannels,
  enginePatchChannel,
  engineResubscribeChannel,
  engineTestChannel,
  listChannels,
  patchChannel,
  resubscribeChannel,
  testChannel,
} from "../api.js";
import ConfirmDialog from "./ConfirmDialog.jsx";
import CopyField from "./CopyField.jsx";
import ErrorNote from "./ErrorNote.jsx";

// WhatsApp on a published agent. A channel is not a connector: connectors
// are the agent reaching out to a system, this is a customer arriving. It
// hangs off the deployment and inherits that deployment's caps and gating
// exactly as the widget does. One card per place the agent answers: the
// Channels screen renders it for this builder and again for each engine
// the agent is pushed to; `engine` ({id, name}) sends every call through
// the relay to that engine, which does the probing and answers as it
// would locally.
//
// Deliberately shows the webhook URL and verify token before anything can
// work: those two go into Meta's console, and the Add form is useless
// until the operator has been handed them. Credentials come back masked
// from the server and are never re-displayed.

function channelApi(engine) {
  if (!engine) {
    return {
      list: listChannels,
      add: addChannel,
      patch: patchChannel,
      remove: deleteChannel,
      resubscribe: resubscribeChannel,
      test: testChannel,
    };
  }
  return {
    list: (agentId) => engineListChannels(engine.id, agentId),
    add: (agentId, body) => engineAddChannel(engine.id, agentId, body),
    patch: (id, body) => enginePatchChannel(engine.id, id, body),
    remove: (id) => engineDeleteChannel(engine.id, id),
    resubscribe: (id) => engineResubscribeChannel(engine.id, id),
    test: (id, to) => engineTestChannel(engine.id, id, to),
  };
}

const EMPTY = {
  phone_number_id: "",
  waba_id: "",
  access_token: "",
  app_secret: "",
  idle_minutes: 10,
  images: "auto",
  voice_reply: "match",
  history_turns: 6,
};

export default function ChannelsCard({ agentId, capConcurrent, engine = null, heading = "WhatsApp" }) {
  const api = channelApi(engine);
  const [channels, setChannels] = useState([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [removing, setRemoving] = useState(null);
  const [testTo, setTestTo] = useState("");
  // Unsaved edits to a connected channel's behaviour, keyed by channel id.
  // Empty means what you see is what the server has.
  const [draft, setDraft] = useState({});

  // The three settings that change how live conversations are answered.
  // Everything else on the card is either read-only or its own action.
  const EDITABLE = ["idle_minutes", "images", "voice_reply"];

  // What the control should show: the pending edit if there is one, the
  // saved value otherwise.
  const shown = (c, key) =>
    draft[c.id] && key in draft[c.id] ? draft[c.id][key] : c[key];

  const edit = (c, key, value) =>
    setDraft((d) => ({ ...d, [c.id]: { ...(d[c.id] || {}), [key]: value } }));

  const pending = (c) =>
    EDITABLE.filter((k) => shown(c, k) !== c[k]);

  const discard = (c) =>
    setDraft((d) => {
      const next = { ...d };
      delete next[c.id];
      return next;
    });

  const load = useCallback(async () => {
    try {
      const r = await api.list(agentId);
      setChannels(r.channels || []);
    } catch (e) {
      setErr(e.message);
    }
  }, [agentId, engine?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    load();
  }, [load]);

  const set = (k) => (e) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  async function create() {
    setBusy(true);
    setErr("");
    setNote("");
    try {
      await api.add(agentId, {
        ...form,
        idle_minutes: Number(form.idle_minutes) || 10,
        history_turns: Number(form.history_turns) || 0,
        waba_id: form.waba_id || null,
      });
      setForm(EMPTY);
      setOpen(false);
      setNote("Number connected. Paste the webhook URL into Meta next.");
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function update(id, patch) {
    setBusy(true);
    setErr("");
    try {
      await api.patch(id, patch);
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function save(c) {
    const changed = pending(c);
    if (!changed.length) return;
    // Only the fields that actually moved, so a concurrent edit elsewhere
    // is not silently overwritten with stale values from this page.
    const patch = {};
    for (const k of changed) patch[k] = shown(c, k);
    setBusy(true);
    setErr("");
    setNote("");
    try {
      await api.patch(c.id, patch);
      discard(c);
      setNote("Settings saved. They apply to the next message.");
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(id) {
    setBusy(true);
    setErr("");
    setRemoving(null);
    try {
      await api.remove(id);
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function resubscribe(id) {
    setBusy(true);
    setErr("");
    setNote("");
    try {
      const c = await api.resubscribe(id);
      setNote(
        c.webhook_subscribed
          ? "Subscribed. Meta will deliver this number's messages here."
          : "Meta still does not list this app on the account."
      );
      await load();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function sendTest(id) {
    setBusy(true);
    setErr("");
    setNote("");
    try {
      const r = await api.test(id, testTo);
      setNote(`Sent to ${r.to}. If it does not arrive, that number is not a
        registered test recipient and has not messaged you in 24 hours.`);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  const tight = capConcurrent && capConcurrent < 10 && channels.length > 0;

  return (
    <>
      <h2 className="section-h">{heading}</h2>
      <p className="hint" style={{ marginTop: -4 }}>
        {engine
          ? `A number connected here answers from ${engine.name}. The engine checks the credentials, subscribes the account and receives Meta's messages itself.`
          : "Answer on a WhatsApp number with this same agent. Customers get the same knowledge and tools as the website widget."}
      </p>

      {err && <ErrorNote message={err} onDismiss={() => setErr("")} />}
      {note && (
        <p className="hint" role="status" style={{ marginTop: 8 }}>
          {note}
        </p>
      )}

      {tight && (
        <div className="note warn" role="alert" style={{ marginTop: 10 }}>
          <b>Raise your concurrent limit.</b> Every WhatsApp conversation
          counts as a session. At {capConcurrent} at once, the next customer
          is turned away. Twenty or more suits a support number.
        </div>
      )}

      {channels.map((c) => (
        <div className="chan-card" key={c.id}>
          <div className="chan-head">
            <b>{c.display_number || c.phone_number_id}</b>
            {c.verified_name && (
              <span className="doc-when">{c.verified_name}</span>
            )}
            <span
              className={
                c.status === "active" ? "doc-status ready" : "doc-status"
              }
            >
              {c.status}
            </span>
          </div>

          <CopyField
            label="Callback URL for Meta"
            value={c.webhook_url}
            hint="Meta Developer console, WhatsApp, Configuration, Callback URL. Subscribe to the messages field."
          />
          <CopyField
            label="Verify token"
            value={c.verify_token}
            display={c.verify_token_masked}
          />

          <div className="field">
            <label className="label" htmlFor={`idle-${c.id}`}>
              Close a quiet thread after
            </label>
            <select
              id={`idle-${c.id}`}
              className="select"
              value={shown(c, "idle_minutes")}
              disabled={busy}
              onChange={(e) =>
                edit(c, "idle_minutes", Number(e.target.value))
              }
            >
              {[5, 10, 20, 30, 60].map((m) => (
                <option key={m} value={m}>
                  {m} minutes
                </option>
              ))}
            </select>
            <div className="hint">
              The customer can always write again; the next message continues
              where they left off.
            </div>
          </div>

          <div className="field">
            <label className="label" htmlFor={`img-${c.id}`}>
              Photos
            </label>
            <select
              id={`img-${c.id}`}
              className="select"
              value={shown(c, "images")}
              disabled={busy}
              onChange={(e) => edit(c, "images", e.target.value)}
            >
              <option value="auto">Read them when the model can</option>
              <option value="off">Always decline</option>
            </select>
            <div className="hint">
              {shown(c, "images") === "auto" && !c.agent_vision
                ? "This agent's model cannot read images, so photos are declined politely. Switch it to an OpenAI model on Options to change that."
                : "A photo reaches the agent with its caption. Anything it cannot make out, it says so."}
            </div>
          </div>

          <div className="field">
            <label className="label" htmlFor={`voice-${c.id}`}>
              Reply
            </label>
            <select
              id={`voice-${c.id}`}
              className="select"
              value={shown(c, "voice_reply")}
              disabled={busy}
              onChange={(e) => edit(c, "voice_reply", e.target.value)}
            >
              <option value="match">Same as user input</option>
              <option value="text">Always text</option>
              <option value="voice">Always voice</option>
            </select>
            <div className="hint">
              A reply goes out one way or the other, never both. Same as user
              input answers a voice note with a voice note, and a typed
              message or a photo with text. Anything too long to listen to,
              and any reply that is only a link, is sent as text whatever
              this says. Each spoken reply costs text to speech on your
              provider.
            </div>
          </div>

          {pending(c).length > 0 && (
            <div className="btn-row">
              <button
                className="btn slim"
                disabled={busy}
                onClick={() => save(c)}
              >
                Save settings
              </button>
              <button
                className="btn ghost slim"
                disabled={busy}
                onClick={() => discard(c)}
              >
                Discard
              </button>
              <span className="hint">
                Not saved yet. Customers are still answered the old way.
              </span>
            </div>
          )}

          {c.webhook_subscribed === true && (
            <div className="hint" style={{ marginTop: 10 }}>
              Meta is delivering this number's messages to Arya.
            </div>
          )}
          {c.webhook_subscribed === false && (
            <div className="note warn" role="alert" style={{ marginTop: 10 }}>
              <b>Meta is not delivering messages yet.</b>{" "}
              {c.subscribe_error ||
                "This app is not on the account's subscriber list, so nothing will arrive."}
              <div style={{ marginTop: 8 }}>
                <button
                  className="btn ghost slim"
                  disabled={busy}
                  onClick={() => resubscribe(c.id)}
                >
                  Fix subscription
                </button>
              </div>
            </div>
          )}
          {c.webhook_subscribed == null && (
            <div className="btn-row">
              <span className="hint" style={{ marginTop: 0 }}>
                Arya has not checked whether Meta will deliver to this
                number.
              </span>
              <button
                className="btn ghost slim"
                disabled={busy}
                onClick={() => resubscribe(c.id)}
              >
                Check subscription
              </button>
            </div>
          )}

          <div className="field" style={{ marginTop: 14, marginBottom: 0 }}>
            <label className="label" htmlFor={`test-${c.id}`}>
              Send a test message
            </label>
            <div className="key-row">
              <input
                id={`test-${c.id}`}
                className="input"
                placeholder="Your number, digits only"
                value={testTo}
                onChange={(e) => setTestTo(e.target.value)}
              />
              <button
                className="btn ghost slim"
                disabled={busy || !testTo}
                onClick={() => sendTest(c.id)}
              >
                Send test
              </button>
            </div>
          </div>

          <div className="btn-row">
            <button
              className="btn ghost slim"
              disabled={busy}
              onClick={() =>
                update(c.id, {
                  status: c.status === "active" ? "paused" : "active",
                })
              }
            >
              {c.status === "active" ? "Pause" : "Resume"}
            </button>
            <button
              className="btn danger slim push-end"
              disabled={busy}
              onClick={() => setRemoving(c)}
            >
              Disconnect
            </button>
          </div>
        </div>
      ))}

      {channels.length === 0 && !open && (
        <button
          className="btn ghost slim"
          style={{ marginTop: 10 }}
          onClick={() => setOpen(true)}
        >
          Connect a WhatsApp number
        </button>
      )}

      {open && (
        <div className="chan-card">
          <p className="hint">
            From your Meta app, WhatsApp, API Setup panel. A test number works
            for trying this out; it can only message numbers you register
            there.
          </p>
          <div className="field">
            <label className="label" htmlFor="wa-pid">
              Phone number ID
            </label>
            <input
              id="wa-pid"
              className="input"
              value={form.phone_number_id}
              onChange={set("phone_number_id")}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="wa-waba">
              WhatsApp Business Account ID (optional)
            </label>
            <input
              id="wa-waba"
              className="input"
              value={form.waba_id}
              onChange={set("waba_id")}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="wa-token">
              Access token
            </label>
            <input
              id="wa-token"
              className="input"
              type="password"
              value={form.access_token}
              onChange={set("access_token")}
            />
            <div className="hint">
              The temporary token expires every 24 hours. Create a system user
              token for anything beyond testing.
            </div>
          </div>
          <div className="field">
            <label className="label" htmlFor="wa-secret">
              App secret
            </label>
            <input
              id="wa-secret"
              className="input"
              type="password"
              value={form.app_secret}
              onChange={set("app_secret")}
            />
            <div className="hint">
              Meta app, Settings, Basic. Used to verify that incoming messages
              really came from Meta.
            </div>
          </div>
          <div className="btn-row">
            <button className="btn" disabled={busy} onClick={create}>
              {busy ? "Checking with Meta..." : "Connect"}
            </button>
            <button
              className="btn ghost"
              disabled={busy}
              onClick={() => {
                setOpen(false);
                setErr("");
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={!!removing}
        title="Disconnect this number?"
        body={
          removing
            ? `${removing.display_number || removing.phone_number_id} stops answering immediately. Past conversations stay in History, and you can connect it again later.`
            : ""
        }
        confirmLabel="Disconnect"
        danger
        busy={busy}
        onConfirm={() => remove(removing.id)}
        onCancel={() => setRemoving(null)}
      />
    </>
  );
}
