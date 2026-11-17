// The widget's ONLY backend surface: the public v1 API, same-origin.
// Never imports src/api.js (admin client) and never honors ?api= — the
// key in the iframe path is the sole credential.

const m = location.pathname.match(/\/embed\/(pk_[A-Za-z0-9_-]{8,})/);
export const publicKey =
  (m && m[1]) || new URLSearchParams(location.search).get("k") || "";

async function req(path, options = {}) {
  const headers = { "X-Arya-Key": publicKey, ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    // Codes, not server prose — customer visitors get friendly copy.
    const code =
      res.status === 401 ? "key_revoked"
      : res.status === 403 ? "origin_not_allowed"
      : res.status === 429 ? "quota_exceeded"
      : res.status === 410 ? "agent_gone"
      : res.status === 503 ? "agent_offline"
      : "network";
    throw Object.assign(new Error(code), { code, status: res.status });
  }
  return res.json();
}

export const getAgentInfo = () => req("/api/public/v1/agent");

// Per-key visitor token so the agent's memory works across visits. The
// SERVER mints and signs it on the first session; this side only stores it
// and hands it back. A token edited here, or copied from another site's
// widget, is rejected server-side and the visitor simply starts over, so a
// page can never choose whose memories it reads. Keyed per public key so
// one site's visitors are never correlated with another's. localStorage
// can throw inside a third-party iframe (storage blocked): then every call
// is a new visitor, which is the safe failure.
const tokenKey = () => `arya_wt_${publicKey.slice(0, 12)}`;

function visitorToken() {
  try {
    return localStorage.getItem(tokenKey()) || null;
  } catch {
    return null;
  }
}

function storeVisitorToken(token) {
  try {
    if (token) localStorage.setItem(tokenKey(), token);
    // The pre-token id (client-chosen UUID). Nothing reads it any more.
    localStorage.removeItem(`arya_wv_${publicKey.slice(0, 12)}`);
  } catch {
    /* storage unavailable: the visitor is new next time, see above */
  }
}

export async function startSession() {
  const s = await req("/api/public/v1/sessions", {
    method: "POST",
    body: JSON.stringify({ visitor_token: visitorToken() }),
  });
  storeVisitorToken(s.visitor_token);
  return s;
}

export const endSession = (id) =>
  req(`/api/public/v1/sessions/${id}/end`, { method: "POST" });
