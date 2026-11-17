// Same convention as the arya builder: point at a different backend with
// ?api=, default to same-origin otherwise (the FastAPI backend serves this
// SPA in production; the Vite dev server proxies /api).
const params = new URLSearchParams(location.search);
export const API_BASE = params.get("api") || "";

// Admin token for a deployed builder (ARYA_ADMIN_TOKEN on the backend).
// `?admin=<token>` stashes it once and is stripped from the URL so the
// token never lingers in history or shared links. Locally (no token set
// server-side) none of this is ever exercised.
if (params.get("admin")) {
  localStorage.setItem("arya_admin_token", params.get("admin"));
  const u = new URL(location.href);
  u.searchParams.delete("admin");
  history.replaceState({}, "", u.pathname + u.search + u.hash);
}
export const adminToken = () => localStorage.getItem("arya_admin_token") || "";
export function setAdminToken(t) {
  localStorage.setItem("arya_admin_token", (t || "").trim());
}

// Stable per-browser user id — becomes the LiveKit participant identity and
// the Mem0 user, so the agent can remember you across calls.
export function userId() {
  let id = localStorage.getItem("arya_user_id");
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem("arya_user_id", id);
  }
  return id;
}

async function req(path, options = {}) {
  // Headers are always an object now: the admin header must also ride on
  // FormData uploads (the old ternary set headers to undefined for those).
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  if (adminToken()) headers["X-Arya-Admin-Token"] = adminToken();
  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (res.status === 401) {
    // Central signal: App shows the one unlock prompt, whichever screen's
    // fetch tripped the gate.
    window.dispatchEvent(new Event("arya:admin-required"));
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body.detail) detail = body.detail;
    } catch {
      /* not json */
    }
    const err = new Error(detail);
    // Callers need to tell "404, legitimately absent" from "the backend is
    // down" — Publish used to render a dead backend as "not published".
    err.status = res.status;
    throw err;
  }
  return res.json();
}

export const getHealth = () => req("/api/health");
export const getSetupStatus = () => req("/api/setup/status");
export const setKeys = (keys) =>
  req("/api/setup/keys", { method: "POST", body: JSON.stringify(keys) });
export const getModels = () => req("/api/models");
export const checkLocalEndpoint = (url) =>
  req("/api/local/check", { method: "POST", body: JSON.stringify({ url }) });

export const getTemplates = () => req("/api/agents/templates");
export const getTemplate = (id) => req(`/api/agents/templates/${id}`);
export const getAgents = () => req("/api/agents");
export const activateAgent = (id) =>
  req(`/api/agents/${id}/activate`, { method: "POST" });
export const deleteAgent = (id) =>
  req(`/api/agents/${id}`, { method: "DELETE" });
export const getActive = () => req("/api/agents/active");
export const putActive = (body) =>
  req("/api/agents/active", { method: "PUT", body: JSON.stringify(body) });
export const generatePrompt = (business, ragEnabled) =>
  req("/api/agents/generate-prompt", {
    method: "POST",
    body: JSON.stringify({ business, rag_enabled: ragEnabled }),
  });
export const fetchWebsite = (url) =>
  req("/api/agents/fetch-website", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
// Voice previews are fetched by VoicePreview.jsx with plain fetch (it
// needs the status code and the bytes), so it builds the URL and the
// admin header here rather than through req().
export const voicePreviewUrl = (provider, model, voice) =>
  `${API_BASE}/api/voices/preview?` +
  new URLSearchParams({ provider, model: model || "", voice: voice || "" });
export const adminHeaders = () =>
  adminToken() ? { "X-Arya-Admin-Token": adminToken() } : {};
// Fetch a site and insert or replace the business-context block in the
// given prompt (empty url removes it). Returns {ok, url, text, instructions}.
export const websiteContext = (url, instructions) =>
  req("/api/agents/website-context", {
    method: "POST",
    body: JSON.stringify({ url, instructions }),
  });

export const startConversation = () =>
  req("/api/conversations", {
    method: "POST",
    body: JSON.stringify({ user_id: userId() }),
  });
export const endConversation = (id) =>
  req(`/api/conversations/${id}/end`, { method: "POST" });
export const deleteConversation = (id) =>
  req(`/api/conversations/${id}`, { method: "DELETE" });
export const getConversations = () => req("/api/conversations");
export const getConvMessages = (id) =>
  req(`/api/conversations/${id}/messages`);

// ── visitors (the people a published agent remembers) ──────────────────
// One row per browser or channel customer; detail adds Mem0's memories;
// forget deletes memories AND every transcript of that person in one call.
export const listVisitors = (agentId) =>
  req(`/api/visitors${agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ""}`);
export const getVisitor = (uid) =>
  req(`/api/visitors/${encodeURIComponent(uid)}`);
export const forgetVisitor = (uid) =>
  req(`/api/visitors/${encodeURIComponent(uid)}`, { method: "DELETE" });
// One memory at a time (the Visitors detail screen).
export const editVisitorMemory = (uid, mid, memory) =>
  req(`/api/visitors/${encodeURIComponent(uid)}/memories/${encodeURIComponent(mid)}`, {
    method: "PATCH",
    body: JSON.stringify({ memory }),
  });
export const deleteVisitorMemory = (uid, mid) =>
  req(`/api/visitors/${encodeURIComponent(uid)}/memories/${encodeURIComponent(mid)}`, {
    method: "DELETE",
  });

// ── insights (observability + evals) ───────────────────────────────────
// range: {start, end} as YYYY-MM-DD (both inclusive; same date twice = that
// one day), interpreted in THIS browser's timezone — the offset rides along
// so the server can translate to its UTC storage.
const insightsQs = (agentId, range) =>
  `?tz_offset_min=${new Date().getTimezoneOffset()}` +
  (range?.start && range?.end
    ? `&start=${range.start}&end=${range.end}`
    : `&days=7`) +
  (agentId ? `&agent_id=${encodeURIComponent(agentId)}` : "");
export const getInsights = (agentId, range) =>
  req(`/api/insights/summary${insightsQs(agentId, range)}`);
// Trends (every number over time) and the breakdown panels (latency by
// model, documents used, pipeline errors, interruption latency). bucket is
// "auto" (hourly up to 2 days, daily beyond), "hour" or "day".
const bucketQs = (bucket) =>
  bucket && bucket !== "auto" ? `&bucket=${bucket}` : "";
export const getInsightsSeries = (agentId, range, bucket) =>
  req(`/api/insights/series${insightsQs(agentId, range)}${bucketQs(bucket)}`);
export const getInsightsBreakdown = (agentId, range, bucket) =>
  req(`/api/insights/breakdown${insightsQs(agentId, range)}${bucketQs(bucket)}`);
export const getConvTimeline = (id) => req(`/api/conversations/${id}/timeline`);

export const listDocs = (agentId) =>
  req(`/api/knowledge-base${agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ""}`);
export const deleteDoc = (id) =>
  req(`/api/knowledge-base/${id}`, { method: "DELETE" });
export function uploadDoc(file, agentId) {
  const form = new FormData();
  form.append("file", file);
  if (agentId) form.append("agent_id", agentId);
  return req("/api/knowledge-base/upload", { method: "POST", body: form });
}

// ── channels (a published agent also answers on WhatsApp) ─────────────
export const listChannels = (agentId) =>
  req(`/api/agents/${agentId}/channels`);
export const addChannel = (agentId, body) =>
  req(`/api/agents/${agentId}/channels`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const patchChannel = (id, body) =>
  req(`/api/channels/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteChannel = (id) =>
  req(`/api/channels/${id}`, { method: "DELETE" });
export const resubscribeChannel = (id) =>
  req(`/api/channels/${id}/resubscribe`, { method: "POST" });
export const testChannel = (id, to) =>
  req(`/api/channels/${id}/test`, {
    method: "POST",
    body: JSON.stringify({ to }),
  });

// ── deployments (publish / share / embed) ──────────────────────────────
export const listDeployments = () => req("/api/deployments");
export const getDeployment = (id) => req(`/api/agents/${id}/deployment`);
export const getDeploymentUsage = (id) =>
  req(`/api/agents/${id}/deployment/usage`);
export const publishAgent = (id) =>
  req(`/api/agents/${id}/deployment`, { method: "POST" });
export const patchDeployment = (id, body) =>
  req(`/api/agents/${id}/deployment`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
export const rotateDeploymentKey = (id) =>
  req(`/api/agents/${id}/deployment/rotate`, { method: "POST" });
export const revokeDeployment = (id) =>
  req(`/api/agents/${id}/deployment`, { method: "DELETE" });
export const pushAgent = (id, body) =>
  req(`/api/agents/${id}/push`, { method: "POST", body: JSON.stringify(body) });

// ── engines (saved connections to deployed engines) ────────────────────
// The browser only ever talks to the local backend; it relays to the
// engine with the stored admin token (responses carry the token masked).
export const listEngines = () => req("/api/engines");
export const addEngine = (body) =>
  req("/api/engines", { method: "POST", body: JSON.stringify(body) });
export const deleteEngine = (id) =>
  req(`/api/engines/${id}`, { method: "DELETE" });
export const engineDeployments = (id) => req(`/api/engines/${id}/deployments`);
export const engineInsights = (id, agentId, range) =>
  req(`/api/engines/${id}/insights/summary${insightsQs(agentId, range)}`);
export const engineInsightsSeries = (id, agentId, range, bucket) =>
  req(`/api/engines/${id}/insights/series${insightsQs(agentId, range)}${bucketQs(bucket)}`);
export const engineInsightsBreakdown = (id, agentId, range, bucket) =>
  req(`/api/engines/${id}/insights/breakdown${insightsQs(agentId, range)}${bucketQs(bucket)}`);
export const engineConversations = (id) =>
  req(`/api/engines/${id}/conversations`);
export const engineConvMessages = (id, cid) =>
  req(`/api/engines/${id}/conversations/${cid}/messages`);
export const engineConvTimeline = (id, cid) =>
  req(`/api/engines/${id}/conversations/${cid}/timeline`);
export const engineDeleteConversation = (id, cid) =>
  req(`/api/engines/${id}/conversations/${cid}`, { method: "DELETE" });
export const engineVisitors = (id, agentId) =>
  req(`/api/engines/${id}/visitors${agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ""}`);
export const engineVisitor = (id, uid) =>
  req(`/api/engines/${id}/visitors/${encodeURIComponent(uid)}`);
export const engineForgetVisitor = (id, uid) =>
  req(`/api/engines/${id}/visitors/${encodeURIComponent(uid)}`, {
    method: "DELETE",
  });
export const engineEditVisitorMemory = (id, uid, mid, memory) =>
  req(
    `/api/engines/${id}/visitors/${encodeURIComponent(uid)}/memories/${encodeURIComponent(mid)}`,
    { method: "PATCH", body: JSON.stringify({ memory }) }
  );
export const engineDeleteVisitorMemory = (id, uid, mid) =>
  req(
    `/api/engines/${id}/visitors/${encodeURIComponent(uid)}/memories/${encodeURIComponent(mid)}`,
    { method: "DELETE" }
  );
export const engineGetDeployment = (id, agentId) =>
  req(`/api/engines/${id}/agents/${agentId}/deployment`);
export const engineGetUsage = (id, agentId) =>
  req(`/api/engines/${id}/agents/${agentId}/deployment/usage`);
export const enginePatchDeployment = (id, agentId, body) =>
  req(`/api/engines/${id}/agents/${agentId}/deployment`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
export const engineRotateKey = (id, agentId) =>
  req(`/api/engines/${id}/agents/${agentId}/deployment/rotate`, {
    method: "POST",
  });
export const engineRevokeDeployment = (id, agentId) =>
  req(`/api/engines/${id}/agents/${agentId}/deployment`, { method: "DELETE" });
// Unpublishing kills the key; this removes the agent from the engine
// altogether (files, documents, and the transcripts recorded there).
export const engineDeleteAgent = (id, agentId) =>
  req(`/api/engines/${id}/agents/${agentId}`, { method: "DELETE" });
// A pushed agent's WhatsApp number and database live on the engine (they
// hold credentials, so they never ride a push). The builder sets them
// there through the relay; the engine does the probing and answers as it
// would locally.
export const engineListChannels = (id, agentId) =>
  req(`/api/engines/${id}/agents/${agentId}/channels`);
export const engineAddChannel = (id, agentId, body) =>
  req(`/api/engines/${id}/agents/${agentId}/channels`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const enginePatchChannel = (id, chanId, body) =>
  req(`/api/engines/${id}/channels/${chanId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
export const engineDeleteChannel = (id, chanId) =>
  req(`/api/engines/${id}/channels/${chanId}`, { method: "DELETE" });
export const engineResubscribeChannel = (id, chanId) =>
  req(`/api/engines/${id}/channels/${chanId}/resubscribe`, { method: "POST" });
export const engineTestChannel = (id, chanId, to) =>
  req(`/api/engines/${id}/channels/${chanId}/test`, {
    method: "POST",
    body: JSON.stringify({ to }),
  });
export const engineListConnections = (id, type) =>
  req(`/api/engines/${id}/connections${type ? `?type=${encodeURIComponent(type)}` : ""}`);
export const engineAddConnection = (id, body) =>
  req(`/api/engines/${id}/connections`, {
    method: "POST",
    body: JSON.stringify(body),
  });
export const engineGetConnectors = (id, agentId) =>
  req(`/api/engines/${id}/agents/${agentId}/connectors`);
export const engineSetConnectors = (id, agentId, connectors) =>
  req(`/api/engines/${id}/agents/${agentId}/connectors`, {
    method: "PUT",
    body: JSON.stringify({ connectors }),
  });

// ── connectors (MCP): external systems an agent can act on ─────────────
// Connections are global (connect once), attachment is per agent in the
// config's `connectors` list. Credentials never come back: rows carry
// `secret_masked` only. OAuth start returns the provider URL to navigate
// to; the provider sends the browser back to `/?connector=…#/options`.
export const getConnectors = () => req("/api/connectors");
export const listConnections = () => req("/api/connections");
export const addConnection = (body) =>
  req("/api/connections", { method: "POST", body: JSON.stringify(body) });
export const patchConnection = (id, body) =>
  req(`/api/connections/${id}`, { method: "PATCH", body: JSON.stringify(body) });
export const deleteConnection = (id, { force } = {}) =>
  req(`/api/connections/${id}${force ? "?force=1" : ""}`, { method: "DELETE" });
export const testConnection = (id) =>
  req(`/api/connections/${id}/test`, { method: "POST" });
export const startConnectionOauth = (id, returnUrl) =>
  req(`/api/connections/${id}/oauth/start`, {
    method: "POST",
    body: JSON.stringify({ return_url: returnUrl }),
  });

// ── in-app operator docs ───────────────────────────────────────────────
export const getDoc = (name) => req(`/api/docs/${name}`);
