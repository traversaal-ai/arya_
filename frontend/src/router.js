import { useCallback, useEffect, useState } from "react";

/* Hash routing, deliberately.

   The FastAPI backend serves this SPA as a single index.html, so real paths
   (/new/industry) would 404 on refresh unless the server grew a catch-all —
   a deployment change this starter kit shouldn't need. Hashes never reach
   the server, so Back, Forward, Refresh and bookmarking all work as-is.

   Before this existed, `screen` was useState in App: the browser Back button
   and the Android back gesture left the app entirely and discarded the whole
   in-progress build. */

const PATHS = {
  home: "/",
  start: "/new",
  industry: "/new/industry",
  details: "/new/details",
  options: "/options",
  preview: "/preview",
  publish: "/publish",
  channels: "/channels",
  call: "/call",
  history: "/history",
  insights: "/insights",
  visitors: "/visitors",
  deployments: "/deployments",
  docs: "/docs/deployment",
  docsConnectors: "/docs/connectors",
};

const SCREEN_FOR_PATH = Object.fromEntries(
  Object.entries(PATHS).map(([screen, path]) => [path, screen])
);

// Human-readable screen names, announced on navigation for screen readers
// (the visual <h1> change is silent to them) and used as the document title.
export const SCREEN_TITLES = {
  home: "Home",
  start: "Create a voice agent",
  industry: "Choose your industry",
  details: "Complete your agent",
  options: "Agent abilities",
  preview: "Preview agent",
  publish: "Publish and deploy",
  channels: "Channels",
  call: "Talk to your agent",
  history: "Past conversations",
  insights: "Dashboard",
  visitors: "Visitors",
  deployments: "Deployments",
  docs: "Deploying an engine",
  docsConnectors: "Connecting external tools",
};

// Screens that cannot render without a config in flight. Landing on one
// directly — fresh tab, stale bookmark, cleared session — redirects home
// instead of rendering a blank shell.
// `publish` is NOT here: it keys off publishFor (an agent id), not the
// in-flight cfg, so it can be opened from the deployments list for any
// saved agent. A stale #/publish link lands on the list instead of home.
export const NEEDS_CFG = new Set([
  "industry",
  "start", // renders the industry-filtered template gallery from cfg
  "details",
  "options",
  "preview",
  "call",
]);

export function pathFor(screen) {
  return PATHS[screen] || PATHS.home;
}

function readHash() {
  // A screen may keep its own state after "?" (the dashboard's tab); that
  // part is the screen's business, not the route's.
  const raw = window.location.hash.replace(/^#/, "").split("?")[0];
  if (!raw) return null;
  // Tolerate a missing leading slash and any trailing slash, so a
  // hand-typed or hand-edited URL still resolves.
  const path = ("/" + raw.replace(/^\/+/, "")).replace(/(.)\/+$/, "$1");
  return SCREEN_FOR_PATH[path] || null;
}

/* Returns [screen, navigate]. `navigate(screen, { replace })` writes the
   hash; the hashchange listener is the ONLY place `screen` is assigned, so
   the URL and the rendered screen can never disagree. */
export function useHashRoute(fallback = "home") {
  const [screen, setScreen] = useState(() => readHash() || fallback);

  useEffect(() => {
    function onHashChange() {
      setScreen(readHash() || fallback);
    }
    window.addEventListener("hashchange", onHashChange);
    // Give a hash-less first load its own history entry, so the first
    // navigation leaves something to come back to.
    if (!window.location.hash) {
      window.history.replaceState(null, "", `#${pathFor(readHash() || fallback)}`);
    }
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [fallback]);

  const navigate = useCallback((next, { replace = false } = {}) => {
    const target = `#${pathFor(next)}`;
    if (window.location.hash === target) {
      setScreen(next); // identical hash fires no event
      return;
    }
    if (replace) {
      // replaceState fires no hashchange — set state directly. Used for
      // guard redirects, which shouldn't add a history entry the user can
      // press Back into and bounce off again.
      window.history.replaceState(null, "", target);
      setScreen(next);
    } else {
      window.location.hash = target;
    }
  }, []);

  return [screen, navigate];
}

/* ── in-flight config: survives refresh, Back, and Forward ─────────────── */

const CFG_KEY = "arya_cfg";

export function loadCfg() {
  try {
    const raw = sessionStorage.getItem(CFG_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null; // private mode, quota, or a corrupt value — start clean
  }
}

export function storeCfg(cfg) {
  try {
    if (cfg) sessionStorage.setItem(CFG_KEY, JSON.stringify(cfg));
    else sessionStorage.removeItem(CFG_KEY);
  } catch {
    /* persistence is a convenience — never block navigation on it */
  }
}

/* Which agent the Publish screen is showing, and where Back goes.
   Persisted for the same reason cfg is: without it, a refresh on #/publish
   would fall back to the in-flight cfg, which can be a DIFFERENT agent
   than the deployments row you opened — silently showing another agent's
   keys. Shape: { agentId, backTo }. */

const PUB_KEY = "arya_publish_for";

export function loadPublishFor() {
  try {
    const raw = sessionStorage.getItem(PUB_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function storePublishFor(value) {
  try {
    if (value) sessionStorage.setItem(PUB_KEY, JSON.stringify(value));
    else sessionStorage.removeItem(PUB_KEY);
  } catch {
    /* convenience only */
  }
}
