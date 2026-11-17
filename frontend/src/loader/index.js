// arya-widget.js — the ONE line customers paste. Zero dependencies, no
// network on page load; renders a launcher bubble in a shadow root and
// lazily opens an iframe to {origin}/embed/{key} on first click. The
// origin comes from this script's own src, so the pasted tag is the only
// configuration a page needs.
//
// View modes: closed (launcher only) ↔ open (panel) ↔ mini (pill with the
// live orb + controls, call keeps running). The iframe is created once and
// NEVER destroyed — a live LiveKit call survives every mode change.
(function () {
  if (window.__aryaWidget) return; // double-load guard (tag managers)
  const script =
    document.currentScript ||
    document.querySelector("script[data-arya-agent-key]");
  if (!script) return;
  const key = script.getAttribute("data-arya-agent-key");
  if (!key) return;
  const ORIGIN = new URL(script.src).origin;
  // Same sage as --orb-hi in styles.css: the orb's default hue, never the
  // brand green (which mixes to mud through the gradient stops).
  const DEFAULT_ACCENT = "#6f9d82";
  const accent = script.getAttribute("data-arya-accent") || DEFAULT_ACCENT;
  const pos = script.getAttribute("data-arya-position") || "bottom-right";
  const zIndex = script.getAttribute("data-arya-z-index") || "2147483000";
  // Light default, matching the app; data-arya-theme="dark" opts in. The
  // value is forwarded to the iframe as ?theme= so panel chrome and app
  // agree. Panel colors are hardcoded pairs (this file is token-less
  // vanilla JS): #ffffff = light --panel, #0f1412 = dark --paper, both from
  // the token blocks at the top of styles.css. Retune those, sync these.
  const theme =
    script.getAttribute("data-arya-theme") === "dark" ? "dark" : "light";
  const panelBg = theme === "dark" ? "#0f1412" : "#ffffff";
  const side = pos.includes("left") ? "left" : "right";

  // Launcher = a miniature of the app's aurora orb (same conic stop
  // patterns), derived from the accent via color-mix; the plain ${accent}
  // fill declared first is the automatic fallback on old browsers.
  const deep = `color-mix(in oklab, ${accent}, #0c1a13 40%)`;
  const soft = `color-mix(in oklab, ${accent}, #ffffff 45%)`;
  const pale = `color-mix(in oklab, ${accent}, #ffffff 80%)`;

  const CSS = `
    :host { all: initial; }
    .wrap { position: fixed; bottom: 20px; ${side}: 20px; z-index: ${zIndex};
      font-family: system-ui, sans-serif; }
    .launcher { width: 60px; height: 60px; border-radius: 50%; border: 0;
      padding: 0; cursor: pointer; position: relative; overflow: hidden;
      color: #fff;
      background: ${accent};
      background: radial-gradient(circle at 33% 28%, #ffffff 0%, ${pale} 30%, ${soft} 62%, ${accent} 100%);
      box-shadow: 0 8px 24px rgba(17,17,17,.18); display: flex;
      align-items: center; justify-content: center;
      transition: transform .15s ease; }
    .launcher:hover { transform: scale(1.06); }
    .l-f { position: absolute; inset: -20%; border-radius: 50%;
      filter: blur(3px); animation: aryaspin 16s linear infinite; }
    .l-f1 { background: conic-gradient(from 15deg, ${deep} 0deg, ${deep} 28deg,
      ${pale} 55deg, ${accent} 85deg, #fff 115deg, ${accent} 145deg,
      ${deep} 185deg, ${soft} 225deg, ${pale} 250deg, ${accent} 285deg,
      ${deep} 330deg, ${deep} 360deg); }
    .l-f2 { animation-duration: 27s; animation-direction: reverse;
      mix-blend-mode: screen; opacity: .85;
      background: conic-gradient(from 230deg, transparent 0deg, #fff 35deg,
      transparent 80deg, ${pale} 135deg, transparent 185deg, #fff 265deg,
      transparent 315deg); }
    .l-sheen { position: absolute; inset: 0; border-radius: 50%;
      background: radial-gradient(circle at 32% 24%, rgba(255,255,255,.55),
      rgba(255,255,255,0) 46%); }
    .launcher svg { position: relative; z-index: 1; color: #111111;
      opacity: .8; filter: drop-shadow(0 1px 1px rgba(255,255,255,.5)); }
    @keyframes aryaspin { to { transform: rotate(360deg); } }
    .wrap.mini .launcher { display: none; }
    .panel { position: fixed; bottom: 92px; ${side}: 20px;
      width: min(400px, calc(100vw - 32px));
      height: min(640px, calc(100vh - 120px));
      border-radius: 18px; overflow: hidden; background: ${panelBg};
      box-shadow: 0 12px 40px rgba(17,17,17,.22);
      transform-origin: bottom ${side};
      opacity: 0; transform: translateY(10px) scale(.85);
      pointer-events: none; visibility: hidden;
      transition: transform .2s cubic-bezier(.4,0,1,1), opacity .18s ease,
        visibility 0s linear .2s,
        width .3s cubic-bezier(.4,0,.2,1), height .3s cubic-bezier(.4,0,.2,1),
        border-radius .3s cubic-bezier(.4,0,.2,1),
        bottom .3s cubic-bezier(.4,0,.2,1); }
    .panel.open, .panel.mini { opacity: 1; transform: none;
      visibility: visible; pointer-events: auto;
      transition: transform .25s cubic-bezier(.22,1,.36,1), opacity .2s ease,
        width .3s cubic-bezier(.4,0,.2,1), height .3s cubic-bezier(.4,0,.2,1),
        border-radius .3s cubic-bezier(.4,0,.2,1),
        bottom .3s cubic-bezier(.4,0,.2,1); }
    .panel iframe { border: 0; width: 100%; height: 100%; display: block; }
    @media (max-width: 640px) {
      .panel.open { inset: 0; width: 100%; height: 100dvh; border-radius: 0;
        bottom: 0; ${side}: 0; }
    }
    .panel.mini { inset: auto; width: 232px; height: 84px;
      border-radius: 42px; bottom: 20px; ${side}: 20px; }
    @media (prefers-reduced-motion: reduce) {
      .l-f { animation: none; }
      .launcher, .panel { transition: none; }
    }`;

  const host = document.createElement("div");
  const root = host.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  style.textContent = CSS;
  const wrap = document.createElement("div");
  wrap.className = "wrap";
  const panel = document.createElement("div");
  panel.className = "panel";
  const btn = document.createElement("button");
  btn.className = "launcher";
  btn.setAttribute("aria-label", "Open voice assistant");
  btn.setAttribute("aria-expanded", "false");
  // Waveform glyph over the mini aurora — keeps the "voice button"
  // affordance (a bare orb reads as decoration).
  btn.innerHTML =
    '<span class="l-f l-f1"></span><span class="l-f l-f2"></span><span class="l-sheen"></span>' +
    '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 10v3"/><path d="M6 6v11"/><path d="M10 3v18"/><path d="M14 8v7"/><path d="M18 5v13"/><path d="M22 10v3"/></svg>';

  let iframe = null;
  let mode = "closed"; // closed | open | mini
  let live = false; // tracked from widget:conversationStarted/Ended

  function postView(m) {
    if (iframe && iframe.contentWindow) {
      iframe.contentWindow.postMessage(
        { source: "arya-host", v: 1, type: "host:view", payload: { mode: m } },
        ORIGIN
      );
    }
  }

  function setMode(next) {
    mode = next;
    if (next !== "closed" && !iframe) {
      // Lazy: a visitor who never clicks pays ~5 KB, not the React app.
      iframe = document.createElement("iframe");
      const params = new URLSearchParams();
      if (theme === "dark") params.set("theme", "dark");
      if (accent !== DEFAULT_ACCENT) params.set("accent", accent);
      const qs = params.toString();
      iframe.src =
        ORIGIN + "/embed/" + encodeURIComponent(key) + (qs ? "?" + qs : "");
      iframe.allow = "microphone; autoplay";
      iframe.title = "Voice assistant";
      panel.appendChild(iframe);
    }
    postView(next);
    // Mini: let the app flip to the pill layout first, then shrink the
    // panel around the already-compact content on the next frame.
    if (next === "mini") {
      requestAnimationFrame(() => {
        panel.classList.remove("open");
        panel.classList.add("mini");
        wrap.classList.add("mini");
      });
    } else {
      panel.classList.toggle("open", next === "open");
      panel.classList.remove("mini");
      wrap.classList.remove("mini");
    }
    btn.setAttribute("aria-expanded", String(next === "open"));
    btn.setAttribute(
      "aria-label",
      next === "open" ? "Close voice assistant" : "Open voice assistant"
    );
  }

  btn.addEventListener("click", () => {
    if (mode === "closed") setMode("open");
    // Mid-call, "get it out of my way" means the pill — hiding the iframe
    // entirely would leave a live mic with zero visible UI.
    else setMode(live ? "mini" : "closed");
  });

  // Events from the iframe (origin-checked) → re-dispatched on document
  // as CustomEvents so host pages can react (analytics, etc.).
  window.addEventListener("message", (e) => {
    if (e.origin !== ORIGIN) return;
    if (!iframe || e.source !== iframe.contentWindow) return;
    const d = e.data;
    if (!d || d.source !== "arya-widget" || d.v !== 1) return;
    document.dispatchEvent(
      new CustomEvent("arya:" + d.type.replace("widget:", ""), {
        detail: d.payload || {},
      })
    );
    if (d.type === "widget:conversationStarted") live = true;
    if (d.type === "widget:conversationEnded") live = false;
    if (d.type === "widget:minimize") setMode("mini");
    if (d.type === "widget:restore") setMode("open");
    if (d.type === "widget:close") setMode("closed");
    if (d.type === "widget:error" && d.payload && d.payload.fatal) {
      setMode("closed");
      wrap.style.display = "none"; // dead key must not leave a dead bubble
    }
  });

  // Restore the panel by clicking anywhere on the pill's edge (the iframe
  // content has its own expand button; this covers the rounded border).
  panel.addEventListener("click", () => {
    if (mode === "mini") setMode("open");
  });

  wrap.appendChild(panel);
  wrap.appendChild(btn);
  root.appendChild(style);
  root.appendChild(wrap);
  document.body.appendChild(host);

  window.__aryaWidget = {
    version: 1,
    open: () => setMode("open"),
    // close() during a live call ENDS the call (host:view "closed" tells
    // the app to hang up first) — never an invisible hot mic.
    close: () => setMode("closed"),
    minimize: () => setMode(live ? "mini" : "closed"),
    toggle: () => setMode(mode === "closed" ? "open" : live ? "mini" : "closed"),
  };
})();
