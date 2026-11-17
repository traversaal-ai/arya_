import { useCallback, useEffect, useRef, useState } from "react";
import {
  activateAgent,
  getHealth,
  getTemplate,
  setAdminToken,
  startConversation,
} from "./api.js";
import {
  NEEDS_CFG,
  SCREEN_TITLES,
  loadCfg,
  loadPublishFor,
  storeCfg,
  storePublishFor,
  useHashRoute,
} from "./router.js";
import ErrorNote from "./components/ErrorNote.jsx";
import LiveRegion from "./components/LiveRegion.jsx";
import ConfirmDialog from "./components/ConfirmDialog.jsx";
import ThemeToggle from "./components/ThemeToggle.jsx";
import Menu from "./components/Menu.jsx";
import BrandMark from "./components/BrandMark.jsx";
import Home from "./screens/Home.jsx";
import Deployments from "./screens/Deployments.jsx";
import Docs from "./screens/Docs.jsx";
import Start from "./screens/Start.jsx";
import IndustryPicker, { labelForIndustry, loadIndustries } from "./screens/IndustryPicker.jsx";
import BusinessForm from "./screens/BusinessForm.jsx";
import Options from "./screens/Options.jsx";
import Preview from "./screens/Preview.jsx";
import Publish from "./screens/Publish.jsx";
import Channels from "./screens/Channels.jsx";
import AgentWindow from "./screens/AgentWindow.jsx";
import History from "./screens/History.jsx";
import Insights from "./screens/Insights.jsx";
import Visitors from "./screens/Visitors.jsx";

// Every flow (template or custom) creates a NEW agent with its own id —
// documents and memories are scoped to it. The source template's id is
// kept as template_id for provenance.
function mintId() {
  return `agent-${crypto.randomUUID().slice(0, 8)}`;
}

// Screens where the agent exists only in memory: Preview is what commits it
// (PUT on mount), so leaving before then really does discard the build and
// deserves a confirmation.
const UNSAVED_SCREENS = new Set(["industry", "start", "details", "options"]);

export default function App() {
  // The screen lives in the URL hash, so Back, Forward, Refresh and
  // bookmarking all work. See router.js for why hashes and not paths.
  const [screen, navigate] = useHashRoute("home");
  // …and the config being built is mirrored into sessionStorage, so a
  // refresh or an accidental Back-then-Forward restores it instead of
  // dropping an uploaded knowledge base and a hand-edited prompt.
  const [cfg, setCfgState] = useState(loadCfg);
  const [conv, setConv] = useState(null); // {conversation_id, room, token, ws_url}
  const [apiUp, setApiUp] = useState(null);
  // How Options was reached: "create" = wizard/template flow (Back walks the
  // wizard, button says Continue); "preview" = editing an existing agent
  // (Back returns to Preview, button says Update).
  const [optionsFrom, setOptionsFrom] = useState("create");
  // Preview's Edit agent asks Settings to open its Advanced panel when the
  // problem list is showing; every other route into Settings leaves it shut.
  const [optionsAdvanced, setOptionsAdvanced] = useState(false);
  const [actionErr, setActionErr] = useState(null); // {message, retry}
  const [leaveOpen, setLeaveOpen] = useState(false);
  const [leaveDest, setLeaveDest] = useState(null); // where the guard was headed
  // Conversations shows a transcript detail view in-place; the topbar menu
  // hides there (user preference), so the screen reports which mode it's in.
  const [historyDetail, setHistoryDetail] = useState(false);
  // "See all calls from this visitor" on a transcript: which person the
  // Visitors screen should open on arrival, and from which source ("" =
  // local, else an engine id). Consumed once by the screen; a refresh on
  // #/visitors lands on the list, which is fine.
  const [visitorFocus, setVisitorFocus] = useState(null);
  // Which agent Share & embed is showing + where Back returns to. Keyed by
  // id (not cfg) and persisted, so a refresh can't silently swap agents.
  const [publishFor, setPublishFor] = useState(loadPublishFor);
  // Deployed builders are gated by ARYA_ADMIN_TOKEN; any 401 from api.js
  // raises this. Locally (no token set server-side) it never fires.
  const [adminNeeded, setAdminNeeded] = useState(false);
  const [adminInput, setAdminInput] = useState("");

  const mainRef = useRef(null);
  const mountedRef = useRef(false);

  const setCfg = useCallback((next) => {
    setCfgState((prev) => {
      const value = typeof next === "function" ? next(prev) : next;
      storeCfg(value);
      return value;
    });
  }, []);

  const checkHealth = useCallback(
    () =>
      getHealth()
        .then(() => setApiUp(true))
        .catch(() => setApiUp(false)),
    []
  );

  // Health check: every 5s while down (Docker may still be starting), every
  // 30s once up (catches the reverse transition). Re-keyed on apiUp so the
  // cadence follows the state.
  useEffect(() => {
    checkHealth();
    // Warm the server-owned industry list so Home's agent-card icons and
    // the wizard render from the catalog, not the built-in fallback.
    loadIndustries();
  }, [checkHealth]);

  // Any admin API call answered 401 → show the unlock banner once.
  useEffect(() => {
    const on = () => setAdminNeeded(true);
    window.addEventListener("arya:admin-required", on);
    return () => window.removeEventListener("arya:admin-required", on);
  }, []);
  useEffect(() => {
    const t = setInterval(checkHealth, apiUp === true ? 30000 : 5000);
    return () => clearInterval(t);
  }, [apiUp, checkHealth]);

  // A deep link or a restored tab can land on a screen whose config is gone.
  // Redirect instead of rendering an empty shell — `replace` so Back doesn't
  // bounce straight back into the same dead end.
  useEffect(() => {
    if (NEEDS_CFG.has(screen) && !cfg) navigate("home", { replace: true });
  }, [screen, cfg, navigate]);

  // Publish keys off publishFor, not cfg — a stale #/publish link lands on
  // the deployments list, which can always render.
  useEffect(() => {
    if ((screen === "publish" || screen === "channels") && !publishFor) {
      navigate("deployments", { replace: true });
    }
  }, [screen, publishFor, navigate]);

  // Deferred cfg discard (see leaveTo): clearing cfg BEFORE navigating raced
  // the NEEDS_CFG guard — but clearing it on ANY arrival at a cfg-free screen
  // broke Home outright: createNew/openSaved set cfg while the hash is still
  // "home" (hashchange lands async), and the blanket clear wiped it mid-set,
  // so the destination's guard bounced every button back to Home. The discard
  // is now an explicit intent: leaveTo records its destination, and cfg drops
  // only when that exact navigation completes.
  const discardRef = useRef(null);
  useEffect(() => {
    if (discardRef.current && discardRef.current === screen) {
      discardRef.current = null;
      setCfg(null);
    }
  }, [screen, setCfg]);

  /* Where we came from. Hash navigation keeps no in-app history of its own,
     so screens' Back buttons used to hardcode Home: reach Conversations
     from a call, press Back, land on Home. This trail makes Back mean
     "the screen I was actually on".

     In-memory only: a refresh falls back to Home rather than restoring
     into a context (cfg, publishFor, a finished call) that no longer
     exists. */
  const navStackRef = useRef([]);
  const prevScreenRef = useRef(null);
  const currScreenRef = useRef(screen);
  useEffect(() => {
    if (currScreenRef.current !== screen) {
      prevScreenRef.current = currScreenRef.current;
      navStackRef.current.push(currScreenRef.current);
      if (navStackRef.current.length > 20) navStackRef.current.shift();
      currScreenRef.current = screen;
    }
  }, [screen]);

  // Screen changes swapped the whole subtree silently before this: no focus
  // moved and nothing was announced, so a keyboard user landed on <body> and
  // a screen-reader user had no idea the page had changed.
  useEffect(() => {
    document.title = `${SCREEN_TITLES[screen] || "Arya"} · Arya`;
    if (!mountedRef.current) {
      mountedRef.current = true; // don't steal focus on first paint
      return;
    }
    mainRef.current?.focus();
  }, [screen]);

  async function pickTemplate(id) {
    try {
      const tpl = await getTemplate(id);
      // The template is a complete authored example (instructions kept
      // verbatim), but the user declared THEIR industry one screen earlier —
      // that declaration wins: it drives Home-card icons, gallery grouping,
      // and any future prompt regeneration.
      //
      // tpl.name is the template's own product label (also what the gallery
      // card shows) — it must NOT prefill the Agent name field on Options,
      // so a customer is forced to actually name their agent. tpl.business
      // ships with an empty business_name for the same reason.
      setCfg((prev) => ({
        ...tpl,
        id: mintId(),
        name: "",
        template_id: tpl.id,
        business: {
          ...tpl.business,
          industry: prev?.business?.industry ?? tpl.business?.industry,
        },
      }));
      setActionErr(null);
      setOptionsFrom("create");
      navigate("options");
    } catch (e) {
      setActionErr({
        message: `Couldn't load the template: ${e.message}`,
        retry: () => pickTemplate(id),
      });
    }
  }

  async function createNew() {
    try {
      const base = await getTemplate("_base");
      setCfg({ ...base, id: mintId(), template_id: base.id });
      setActionErr(null);
      navigate("industry");
    } catch (e) {
      setActionErr({
        message: `Couldn't start the wizard: ${e.message}`,
        retry: createNew,
      });
    }
  }

  function openSaved(config) {
    setCfg(config);
    navigate("preview");
  }

  // "Take me to the agent that ran this call" — from a transcript row or
  // header. Activates it (an explicit intent to work on it) and opens
  // Preview. A deleted agent 404s: say so instead of navigating nowhere.
  async function openAgentById(agentId) {
    if (!agentId) return;
    try {
      const res = await activateAgent(agentId);
      setCfg(res.config);
      setActionErr(null);
      navigate("preview");
    } catch (e) {
      setActionErr({
        message:
          e.status === 404
            ? "That agent no longer exists, so it can't be opened."
            : `Couldn't open that agent: ${e.message}`,
        retry: e.status === 404 ? undefined : () => openAgentById(agentId),
      });
    }
  }

  function goHome() {
    setCfg(null);
    setActionErr(null);
    navigate("home");
  }

  /* Back for the cross-cutting screens (Conversations, Dashboard,
     Deployments, Docs): return to whatever screen we were actually on.

     Pops until it finds a destination that can still render — cfg-needing
     screens are dead once cfg is gone, `publish` needs publishFor, and
     `call` is skipped outright because a finished call cannot be resumed
     (re-entering would land on a fresh ready screen, which is not "back").
     Falls back to Home when nothing qualifies. */
  function goBack(fallback = "home") {
    const stack = navStackRef.current;
    while (stack.length) {
      const dest = stack.pop();
      if (!dest || dest === screen || dest === "call") continue;
      if (NEEDS_CFG.has(dest) && !cfg) continue;
      if (dest === "publish" && !publishFor) continue;
      setActionErr(null);
      navigate(dest);
      return;
    }
    if (fallback === "home") goHome();
    else navigate(fallback);
  }

  // Leaving the wizard for anywhere (brand click, topbar menu) must warn
  // once, or the menu becomes a silent way to discard an in-progress build.
  //
  // Deliberately does NOT clear cfg here: navigate() flips the hash but the
  // hashchange lands ASYNC, so clearing cfg first re-rendered the OLD
  // screen with no cfg — and if that screen was in NEEDS_CFG, its guard
  // redirected to home, racing (and sometimes beating) the intended
  // destination. The effect below clears cfg once we actually arrive.
  function leaveTo(dest) {
    if (cfg && UNSAVED_SCREENS.has(screen)) {
      setLeaveDest(dest);
      setLeaveOpen(true);
      return;
    }
    discardRef.current = dest; // arrival effect drops the in-flight cfg
    setActionErr(null);
    navigate(dest);
  }

  // Open Share & embed for any saved agent. Deliberately does NOT activate
  // the agent: activating changes which one the worker runs next, which
  // would be an invisible side effect of just looking at a key.
  // `engine` ({id, name}, no token) switches the screen into remote mode:
  // it manages that saved engine's deployment through the backend proxy.
  function openPublish(agentId, backTo, engine = null) {
    const value = { agentId, backTo, engine };
    setPublishFor(value);
    storePublishFor(value);
    navigate("publish");
  }

  // Open THAT agent from Publish & deploy: activate it (an explicit intent
  // to work on this agent, same as opening it from Home), then land on the
  // requested screen. `dest` is "options" (Edit agent) or "preview".
  async function openFromPublish(dest) {
    if (!publishFor) return;
    try {
      const res = await activateAgent(publishFor.agentId);
      setCfg(res.config);
      if (dest === "options") setOptionsFrom("preview");
      setActionErr(null);
      navigate(dest);
    } catch (e) {
      setActionErr({
        message: `Couldn't open this agent: ${e.message}`,
        retry: () => openFromPublish(dest),
      });
    }
  }

  /* Per-screen topbar menu, grouped by intent (manager-specified IA).
     Shape is the same everywhere so the menu is learnable: Home alone at
     the top (a destination, not a category, so no heading over it), then
     Configure & Deploy, then Monitor.

     Backbone items (Home, Create new agent, Deployments, Dashboard,
     Conversations) appear on every screen that has a menu, EXCEPT where the
     screen itself already carries that action as a button: Home has the
     Create card, Preview has Publish & deploy, Publish has Deployments. A
     menu row beside an identical button on screen is noise.

     Agent actions (Edit/Preview) are HIDDEN, not disabled, where no agent
     is in context — "edit which agent?" has no answer on Home.

     Wizard steps and docs have NO menu; neither does the transcript
     detail inside Conversations. */
  function menuItemsFor(scr) {
    const item = {
      home: { label: "Home", onPick: () => leaveTo("home") },
      createNew: { label: "Create new agent", onPick: createNew },
      editAgent: { label: "Edit agent", onPick: () => openFromPublish("options") },
      previewAgent: {
        label: "Preview agent",
        // From Publish & deploy the agent must be loaded first; from the
        // call window cfg is already the agent in context.
        onPick: () =>
          scr === "publish" ? openFromPublish("preview") : leaveTo("preview"),
      },
      deployments: { label: "Deployments", onPick: () => leaveTo("deployments") },
      insights: { label: "Dashboard", onPick: () => leaveTo("insights") },
      history: { label: "Conversations", onPick: () => leaveTo("history") },
      visitors: { label: "Visitors", onPick: () => leaveTo("visitors") },
    };
    // [home, configure&deploy…, monitor…] per screen.
    const menus = {
      home: [[], ["deployments"], ["insights", "history", "visitors"]],
      preview: [
        ["home"],
        ["createNew", "deployments"],
        ["insights", "history", "visitors"],
      ],
      publish: [
        ["home"],
        ["editAgent", "previewAgent"],
        ["insights", "history", "visitors"],
      ],
      channels: [
        ["home"],
        ["editAgent", "previewAgent", "deployments"],
        ["insights", "history", "visitors"],
      ],
      deployments: [["home"], ["createNew"], ["insights", "history", "visitors"]],
      insights: [["home"], ["createNew", "deployments"], ["history", "visitors"]],
      history: historyDetail
        ? null
        : [["home"], ["createNew", "deployments"], ["insights", "visitors"]],
      // Same detail-view rule as Conversations: the menu hides while a
      // visitor or one of their transcripts is open.
      visitors: historyDetail
        ? null
        : [["home"], ["createNew", "deployments"], ["insights", "history"]],
    };
    const groups = menus[scr];
    if (!groups) return null;
    const [top, configure, monitor] = groups;
    const out = top.map((k) => item[k]);
    // A heading only appears when its section has something under it, so
    // an empty group can never leave a dangling separator.
    for (const [heading, keys] of [
      ["Configure & Deploy", configure],
      ["Monitor", monitor],
    ]) {
      if (!keys.length) continue;
      out.push({ heading });
      out.push(...keys.map((k) => item[k]));
    }
    return out;
  }

  async function startCall() {
    // Room + token are created only when the green call button is pressed —
    // dispatch fires on room creation, so no worker runs until then. Also
    // used for "Call again". Throws on failure; AgentWindow shows the error.
    const c = await startConversation();
    setConv(c);
  }

  function editAgent() {
    setConv(null);
    setOptionsFrom("preview");
    setOptionsAdvanced(false);
    navigate("options");
  }


  if (screen === "call" && cfg) {
    // conv === null → ready state (no room yet). key= remounts the window
    // per call, resetting transcript + ended state.
    return (
      <AgentWindow
        key={conv ? conv.room : "ready"}
        cfg={cfg}
        conv={conv}
        onStartCall={startCall}
        onEdit={editAgent}
        onPreview={() => {
          setConv(null);
          navigate("preview");
        }}
        onHistory={() => {
          setConv(null);
          navigate("history");
        }}
        onInsights={() => {
          setConv(null);
          navigate("insights");
        }}
        onVisitors={() => {
          setConv(null);
          navigate("visitors");
        }}
        onPublish={() => {
          // Same screen serves both states: Share & embed when published,
          // the "publish this agent" card when not.
          setConv(null);
          openPublish(cfg.id, "preview");
        }}
        onHome={() => {
          setConv(null);
          goHome();
        }}
      />
    );
  }

  return (
    <div className="app">
      <a className="skip-link" href="#screen-main">
        Skip to main content
      </a>
      <header className="topbar">
        <button
          type="button"
          className="brand brand-btn"
          onClick={() => leaveTo("home")}
        >
          <BrandMark />
          <span className="brand-word" translate="no">arya</span>
          <span className="sr-only">Arya, home</span>
        </button>
        <div className="spacer"></div>
        {/* Per-screen menu (user-specified matrix): wizard steps and the
            docs page show no ⋮ at all; the rest each carry their own set. */}
        {menuItemsFor(screen) && <Menu items={menuItemsFor(screen)} />}
        <ThemeToggle />
      </header>

      <div className="page">
        {/* full-bleed page, readable content: forms center at 760px, list
            and doc screens get a roomier column */}
        <main
          id="screen-main"
          ref={mainRef}
          tabIndex={-1}
          className={
            "content-col" +
            (["home", "history", "insights", "visitors", "deployments", "docs", "docsConnectors"].includes(screen)
              ? " wide"
              : "")
          }
        >
          {apiUp === false && (
            <ErrorNote
              message="Can't reach the backend. Make sure the stack is running (docker compose up). This clears by itself once it's up, or check the README's troubleshooting section."
              onRetry={checkHealth}
              retryLabel="Check again"
              style={{ marginBottom: 18 }}
            />
          )}
          {adminNeeded && (
            <form
              className="note warn"
              style={{ marginBottom: 18 }}
              onSubmit={(e) => {
                e.preventDefault();
                if (!adminInput.trim()) return;
                setAdminToken(adminInput);
                // Reload so every screen refetches with the header attached.
                location.reload();
              }}
            >
              <b>This builder is protected.</b> Enter the admin token
              (ARYA_ADMIN_TOKEN) to continue.
              <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                <input
                  className="input"
                  type="password"
                  value={adminInput}
                  onChange={(e) => setAdminInput(e.target.value)}
                  placeholder="Admin token"
                  aria-label="Admin token"
                />
                <button className="btn slim" type="submit">
                  Unlock
                </button>
              </div>
            </form>
          )}
          {actionErr && (
            <ErrorNote
              message={actionErr.message}
              onRetry={actionErr.retry}
              style={{ marginBottom: 18 }}
            />
          )}

          {screen === "home" && (
            <Home onCreateNew={createNew} onOpenSaved={openSaved} />
          )}
          {screen === "start" && cfg && (
            <Start
              cfg={cfg}
              onScratch={() => navigate("details")}
              onPickTemplate={pickTemplate}
              onBack={() => navigate("industry")}
            />
          )}
          {screen === "history" && (
            <History
              onBack={() => goBack()}
              onDetail={setHistoryDetail}
              onOpenAgent={openAgentById}
              onVisitor={(userId, source) => {
                setVisitorFocus({ userId, source: source || "" });
                navigate("visitors");
              }}
            />
          )}
          {screen === "insights" && (
            <Insights
              onBack={() => goBack()}
              onOpenAgent={openAgentById}
              initialAgentId={cfg?.id || ""}
            />
          )}
          {screen === "visitors" && (
            <Visitors
              onBack={() => goBack()}
              onDetail={setHistoryDetail}
              onOpenAgent={openAgentById}
              focus={visitorFocus}
              onFocusConsumed={() => setVisitorFocus(null)}
            />
          )}
          {screen === "deployments" && (
            <Deployments
              // The stack already handles the Publish & deploy round trip
              // (and everything else) — goBack pops to whatever screen we
              // actually came from, Home when there is nothing live.
              onBack={() => goBack()}
              onOpen={(id, engine) => openPublish(id, "deployments", engine)}
            />
          )}
          {screen === "docs" && (
            <Docs name="deployment" onBack={() => goBack()} />
          )}
          {screen === "docsConnectors" && (
            <Docs name="connectors" onBack={() => goBack()} />
          )}
          {screen === "publish" && publishFor && (
            <Publish
              agentId={publishFor.agentId}
              engine={publishFor.engine || null}
              // Back returns to where this screen was opened from: the list
              // for Deployments' Manage (remote manage only exists there),
              // that agent's Preview for everything else.
              onBack={() => {
                if (publishFor.engine || publishFor.backTo === "deployments") {
                  navigate("deployments");
                } else if (cfg?.id === publishFor.agentId) {
                  navigate("preview");
                } else {
                  // publishFor survives a refresh; cfg may not, or may have
                  // moved on. Load the agent this screen is about rather
                  // than showing a different agent's Preview.
                  openFromPublish("preview");
                }
              }}
              // Browse, don't leave: plain navigate keeps cfg alive so the
              // round trip publish → deployments → Back → publish → Back
              // still lands on Preview instead of bouncing to Home.
              onDeployments={() => navigate("deployments")}
              onChannels={() => navigate("channels")}
            />
          )}
          {screen === "channels" && publishFor && (
            <Channels
              agentId={publishFor.agentId}
              agentName={cfg?.id === publishFor.agentId ? cfg.name : ""}
              onBack={() => navigate("publish")}
              onPublish={() => navigate("publish")}
            />
          )}
          {screen === "industry" && cfg && (
            <IndustryPicker
              initial={cfg.business?.industry}
              onBack={goHome}
              onPick={(industry) => {
                // "Other" skips the template gallery — there is nothing to
                // offer there — and goes straight to the form, where the
                // Industry Name field is the first row. Re-clicking Other
                // must not wipe a custom industry typed earlier (a
                // non-catalog value already means Other).
                const prev = cfg.business?.industry;
                const keepCustom =
                  industry === "" && prev && !labelForIndustry(prev);
                setCfg({
                  ...cfg,
                  business: {
                    ...cfg.business,
                    industry: keepCustom ? prev : industry,
                  },
                });
                navigate(industry === "" ? "details" : "start");
              }}
            />
          )}
          {screen === "details" && cfg && (
            <BusinessForm
              initial={cfg.business || {}}
              // The Other path never visited the gallery, so Back must
              // return to the industry grid, not the skipped screen.
              onBack={() =>
                navigate(
                  labelForIndustry(cfg.business?.industry)
                    ? "start"
                    : "industry"
                )
              }
              onDone={(business) => {
                setCfg({
                  ...cfg,
                  business: { ...cfg.business, ...business },
                  // Idempotent suffix: users often type "X Agent" as the
                  // business name — never produce "X Agent Agent".
                  name: business.business_name
                    ? /\bagents?$/i.test(business.business_name)
                      ? business.business_name
                      : `${business.business_name} Agent`
                    : cfg.name,
                  instructions: "", // regenerate from the new answers
                });
                setOptionsFrom("create");
                navigate("options");
              }}
            />
          )}
          {screen === "options" && cfg && (
            <Options
              cfg={cfg}
              mode={optionsFrom === "preview" ? "edit" : "create"}
              openAdvanced={optionsAdvanced}
              onBack={() =>
                // Edit flow returns to Preview; the creation flow walks the
                // wizard back (template_id tells custom from template).
                navigate(
                  optionsFrom === "preview"
                    ? "preview"
                    : cfg.template_id === "custom"
                      ? "details"
                      : "start"
                )
              }
              onDone={(next) => {
                setCfg(next);
                navigate("preview");
              }}
            />
          )}
          {screen === "preview" && cfg && (
            <Preview
              cfg={cfg}
              onBack={(withProblems) => {
                setOptionsFrom("preview");
                setOptionsAdvanced(withProblems === true);
                navigate("options");
              }}
              onUpdate={setCfg}
              // A delete that couldn't reach a connected engine hands back a
              // warning: the agent is gone from here, so Home is the only
              // place left to tell you it is still live there.
              onDeleted={(warning) => {
                goHome();
                if (warning) setActionErr({ message: warning });
              }}
              onStart={() => {
                // Ready state — the room is created by the call button.
                setConv(null);
                navigate("call");
              }}
              onPublish={() => openPublish(cfg.id, "preview")}
            />
          )}
        </main>
      </div>

      <LiveRegion message={SCREEN_TITLES[screen] || ""} />

      <ConfirmDialog
        open={leaveOpen}
        title="Leave without saving this agent?"
        body="This agent hasn't been saved yet. Leaving discards the answers, settings, and any documents you added."
        confirmLabel="Discard and leave"
        danger
        onConfirm={() => {
          setLeaveOpen(false);
          const dest = leaveDest || "home";
          setLeaveDest(null);
          // cfg is cleared by the arrival effect — clearing it here
          // re-triggered the NEEDS_CFG redirect on the old screen.
          discardRef.current = dest;
          setActionErr(null);
          navigate(dest);
        }}
        onCancel={() => {
          setLeaveOpen(false);
          setLeaveDest(null);
        }}
      />
    </div>
  );
}
