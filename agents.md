# agents.md - guide for AI coding assistants (and curious humans)

You are working in **Arya**, an open-source voice-agent starter kit
built on LiveKit Agents (Python) with a Vite + React frontend. This file
maps the repo and the intended extension points. Read it before changing
anything.

## Repo map

```
docker-compose.yml     11 services (9 long-running + `ollama-init`/`speaches-init`,
                       one-shot model pullers); `api` serves UI+API on :8000, `agent-worker`
                       runs the voice agent under network_mode: host.
                       WEIGHT: ollama 8 GB + speaches 3 GB of images serve
                       ONLY the local provider's Ollama backend (and nomic
                       embeddings) - speaches alone covers BOTH speech
                       stages (Whisper STT + Kokoro TTS on the same
                       OpenAI-compatible /v1); api/agent-worker have no
                       depends_on to them - they're reached over plain HTTP
                       - so a cloud (OpenAI/Groq) user can start without
                       them: `docker compose up api agent-worker arya-mcp
                       livekit postgres qdrant seaweedfs` (~6 GB vs ~18 GB).
                       Keep that true when adding services. `arya-mcp` is
                       the same image as api/worker, so it adds no download;
                       it hosts Arya's own MCP servers (the read-only
                       Postgres connector) on 127.0.0.1:8020.
                       `arya-whatsapp` is the same image again: the WhatsApp
                       channel bridge on 127.0.0.1:8030, network_mode: host
                       for the same ICE reason as agent-worker (gotcha 1).
                       Nothing depends_on it either.
Dockerfile             one image for api + worker; stage 1 builds frontend/dist
scripts/init.sh        generates every local secret ONCE: .env, livekit.yaml,
                       seaweedfs-s3.json, backend/.env.local. All four are
                       gitignored (the repo ships no working credentials) and
                       none are overwritten if present - values are read back
                       out of .env so a partial state regenerates consistently.
                       If one goes missing, docker creates a DIRECTORY at the
                       bind-mount path and that service dies with a config
                       error; re-run the script. livekit.yaml also owns the
                       RTC port range (must match compose's published range).
backend/
  pyproject.toml       uv project (python >=3.10,<3.13 - docling/mem0 lag newer)
  agents/templates/    agent templates, one per industry: retail,
                       food-beverage, travel-concierge, appointments, fitness,
                       intake, home-visits, support, ticketing, admissions,
                       finance, + _base.json (_-prefixed files are hidden
                       from the gallery). None ship a business/agent
                       identity - instructions and greeting use the
                       `{agent_name}` / `{business_name}` variables instead,
                       which config.render_vars() fills from the config's
                       `name` / `business.business_name` on EVERY call
                       (agent.py), never at save time, so a rename follows
                       through. Keep it that way when adding a template: a
                       hardcoded business
                       name would survive into every agent built from it.
                       No sample KB docs ship either: RAG is off on every
                       template and is the customer's own opt-in, so the
                       four knowledge-base templates (retail, support,
                       admissions, finance) are written two-mode - strict
                       when excerpts exist, honest "I don't have that
                       detail" when none do - and stay useful with nothing
                       uploaded.
  data/                RUNTIME STATE, gitignored: active-agent.json, keys.json,
                       agents/<id>.json (every created agent, listed on Home)
  src/
    agent.py           the worker. One LiveKit job = one call. Reads
                       active-agent.json + keys.json FRESH per job - config
                       changes apply to the next call, no restart needed.
                       transcription_node collapses hyphen-grouped digits -
                       numeric or spelled out ("2-2-9…", "two-two-nine…") -
                       to a continuous number for the transcript only; TTS
                       keeps the digit-by-digit spelling.
    api.py             FastAPI under /api/* + serves frontend/dist at /.
                       Token server: POST /api/conversations (fresh room +
                       JWT per call; stamps agent_id/agent_name into the
                       row's metadata), POST …/{id}/end (deletes the room),
                       DELETE …/{id} (purges one conversation + transcript -
                       History's Delete chat). KB endpoints + Postgres
                       job-queue ingestion worker live here too. Saved agents:
                       GET /api/agents, POST /api/agents/{id}/activate,
                       DELETE /api/agents/{id} (purges its docs, memories,
                       AND conversations - rows carry agent_id/agent_name
                       in conversations.metadata). Wizard helper:
                       POST /api/agents/fetch-website (homepage → plain text
                       for prompt personalization; always 200, ok true/false).
                       DEPLOY SURFACE (all admin-gated): deployments mint a
                       per-agent `pk_` (publish / rotate / revoke, plus caps
                       and an origin allowlist), the public API v1 exchanges a
                       pk for a short-lived LiveKit token under those limits
                       and mints a signed per-browser `visitor_token` the
                       widget returns on later calls (visitors.py: the server
                       picks the identity, never the page; channel fields need
                       the X-Arya-Bridge-Token header), /visitors lists, shows
                       and forgets one person (Mem0 across all agents plus
                       every Postgres row, relayed to engines like History;
                       single memories can be edited or deleted, each checked
                       against that visitor's own listing first),
                       /embed/{pk} + arya-widget.js serve the widget with
                       frame-ancestors from the allowlist, /agents/{id}/push
                       exports an agent to an engine (import → publish →
                       allowlist), /engines/* relay to SAVED engines with the
                       stored admin token so it never reaches the browser,
                       and /insights/summary aggregates calls, latency,
                       endings and eval scores. Revoked deployments read as
                       "not published" everywhere; unpublish and agent-delete
                       both cascade a revoke to every saved engine.
    providers.py       provider dispatch → AgentSession. Cascaded only:
                       "openai" = openai STT → LLM → TTS (one key);
                       "groq"   = Groq Whisper STT + Groq GPT-OSS 120B LLM
                       + Groq Orpheus TTS (one GROQ_API_KEY, free tier);
                       "gemini" = beta.GeminiSTT (Live API, streaming) +
                       google.LLM + beta.GeminiTTS, one GOOGLE_API_KEY for
                       all three; models live in the config's own `gemini`
                       group. NOT google.STT (that is Cloud STT/Chirp and
                       needs service-account credentials);
                       "local"  = _build_local_session(), two NON-symmetric
                       backends (config's local.backend): "ollama" overrides
                       the LLM only (Ollama has no usable STT/TTS route at
                       all - those stay pinned to Speaches :8010, which
                       serves BOTH Whisper STT and Kokoro TTS); "vllm" can
                       override all three stages independently (one model
                       per vLLM server process - core vLLM has no TTS route,
                       so its TTS falls back to Speaches too unless
                       overridden). All through the same openai.STT/LLM/TTS
                       plugin with local base_urls - no key, ~8 GB RAM for
                       the bundled containers, patient conn_options.
                       Realtime/S2S models are deliberately not wired up.
    tools.py           function tools + build_tools(cfg, keys, ..., runtime)
                       registry. The order desk has two bodies behind one
                       toggle: _fake_order_tools (canned, per-call memory) and
                       _shop_order_tools (a small Postgres shop, demo/shop),
                       chosen by whether the agent has a shop-shaped Postgres
                       connector attached (tool_runtime["shop_dsn"], set by
                       agent.py from connectors.served_toolsets) or, as a
                       fallback, ARYA_SHOP_DSN. The real one
                       identifies a WhatsApp user from runtime["external_user"]
                       and filters every read by customer id in SQL; its only
                       write is INSERT INTO returns, which is also the only
                       write the shop_agent role is granted. Own asyncpg pool:
                       db.get_pool is a singleton that bootstraps Arya's schema
                       and must never touch somebody else's database.
    mcp_server.py      arya-mcp: the sidecar hosting the MCP servers ARYA
                       provides itself, for systems nobody hosts one for.
                       Starlette parent, one FastMCP app mounted per module
                       under src/mcp_tools/ (postgres at /postgres/mcp),
                       plus a plain check route per module the API calls when
                       a connection is saved (a static tool list would report
                       a wrong password as "connected"). Holds NO
                       credentials: each request carries the one it should
                       use, so it can only reach what the caller already
                       could - which is why compose publishes it on
                       127.0.0.1 only (the host-network worker needs a
                       localhost port). Same image, no depends_on.
    mcp_tools/         one module per such system. calcom.py: real
                       appointment booking (list_services, find_slots,
                       book/reschedule/cancel, find_my_bookings). Cal.com
                       owns the hours, slot lengths and buffers a raw
                       calendar does not, and emails the confirmation. Split
                       into testable `do_*` functions plus thin @app.tool
                       wrappers that hold the docstrings (the LLM contract)
                       and pull the key off the Authorization header. Voice
                       shape: services by NAME not id, IANA time zone
                       demanded, times spoken in the caller's zone,
                       references returned but never spoken. API shapes come
                       from Cal.com's public examples (their docs and spec
                       are behind Cloudflare), so parsing is defensive and
                       /calcom/check catches a mismatch at save time.
                       postgres_readonly.py: the
                       OWNER's own database (not Arya's), read-only by four
                       layers - single SELECT/WITH parsed, session
                       default_transaction_read_only, an explicit READ ONLY
                       transaction always rolled back, statement_timeout +
                       row/byte caps. DSN arrives in the X-Arya-Dsn header.
                       GOTCHA both modules share: pools and asyncio.Lock
                       belong to the loop that made them, so the registry is
                       rebuilt per loop (_state()); reusing one across loops
                       deadlocks the shutdown trying to close it.
    meta_wa.py         WhatsApp Cloud API: X-Hub-Signature-256 checking,
                       webhook payload -> InboundMessage/StatusUpdate, and a
                       MetaClient for sending. No FastAPI, no DB, no LiveKit,
                       so tests/test_whatsapp.py exercises the two things
                       that actually break (a webhook we wrongly accept, a
                       payload shape we misread) against recorded envelopes.
                       normalize() is TOTAL: an unknown type becomes
                       kind="unsupported", never an exception, because a
                       raising webhook is one Meta retries for a week.
    whatsapp_bridge.py arya-whatsapp: the webhook on one side, a LiveKit room
                       on the other. Starts a session through the ordinary
                       public API with the deployment's pk_ (so caps, rate
                       limits and refusal accounting are shared with the
                       widget), joins as a data-only participant, writes the
                       customer's text on lk.chat and relays lk.transcription
                       back. One asyncio queue per thread keeps answers in
                       order; channel_events.wamid is the dedupe gate.
                       ALL media I/O lives here, never in the worker:
                       download, shrink, transcribe, synthesize, encode and
                       upload are network- and CPU-bound, and doing them on
                       the worker's turn path would tax every text-only
                       conversation too. decide_image() and reply_mode()
                       are pure so the rules an operator actually sets are
                       table-tested.
    media.py           bytes in, bytes out: shrink_image (Pillow, EXIF
                       rotate applied not carried, alpha flattened onto
                       white), transcribe and synthesize (the OpenAI-shaped
                       /audio routes all three providers serve, reached via
                       providers.speech_endpoint), to_ogg_opus (PyAV, in
                       process: WhatsApp renders a voice note ONLY for
                       OGG/Opus with voice=true, anything else is a file
                       card). No FastAPI, no LiveKit, no DB, so a photo a
                       phone produced and a codec a provider changed are
                       both testable against real bytes.
    connectors.py      connectors (MCP): probe a server + list its tools
                       (API side), OAuth code exchange + refresh, and
                       build_toolsets(cfg) for the worker: one LiveKit
                       MCPToolset per attached connection, connected under a
                       4 s budget, dead ones dropped, duplicate tool names
                       filtered. Shared by api.py and agent.py; no FastAPI.
                       call_tool(conn, tool, args) runs ONE tool on an open
                       connector from worker code, outside the model's turn,
                       over a fresh short-lived MCP session (the same
                       primitives probe() uses, so nothing depends on
                       LiveKit's private tool internals). Live entries carry
                       url/headers/transport for it. SHAPERS, keyed by
                       (connector type, tool), rebuilds a vendor result
                       BEFORE the 1500-char cap: Google Maps answered with
                       ids and five URLs per place and all its prose in one
                       trailing field, so the model got identifiers and
                       never a word of description.
                       RULE: a tool added to an Arya-hosted module is not
                       live for an existing connection until it is probed
                       again. The probe then ticks new READ tools on every
                       agent holding that connection (api._offer_new_tools_
                       to_agents); writes are never auto-ticked.
    day_plan.py        one layout for the day-by-day plan, shared by both
                       trip writers: which day each looked-up place lands on
                       and at what time. build_days() for the sample desk's
                       rendered day dicts, lay_out() for flat connector plan
                       items. Pure functions; invents no place.
    itinerary_email.py the written itinerary emailed AS THE PAGE. Reads the
                       stored artifact, strips the JS-filled Download button,
                       adds a real PDF link when PUBLIC_BASE_URL is set, and
                       sends it through whichever live connector declares
                       `email_via` in the catalog. The 8 KB body never passes
                       through the model: it only supplies the address. Also
                       the booking confirmation (the vendor sends the
                       passenger nothing on a test token) and its agency
                       copy, addressed to the mailbox the email connector
                       sends from, learned on Test. Add-to-calendar links
                       are ALL-DAY by design: Google's template URL cannot
                       carry a timezone, so a timed event would be read in
                       the reader's own; the local times sit inside it.
                       Arya writes to nobody's calendar, because a
                       vendor-created event can only live on the account
                       the AGENCY connected.
    trip_planner.py    the day plan, looked up and laid out by Python the way
                       the sample desk always did it. Several differently
                       worded search_places calls at once, deduped by place
                       id, round-robined, then the connector's own plan_trip.
                       Built only when both halves are live, and it appends
                       its own instruction line because a catalog prompt
                       block cannot know whether it exists.
    config.py          templates, active config, keys, env endpoints,
                       validation, prompt generation (deterministic template).
    memory.py          hosted Mem0: recall at session start (2s timeout),
                       fire-and-forget add per user turn; list_for_user and
                       purge_user behind the Visitors screen.
    visitors.py        widget visitor identity: HMAC-signed per-deployment
                       tokens (mint/verify), user_id derivation, the bridge
                       token check, and the SQL that forgets one person
                       (messages, artifacts, tool_events, rag_events,
                       conversations, channel rows). Pure, no FastAPI.
    rag.py             extract (docling) → chunk → embed → Qdrant. Pure fns.
    website.py         fetch a business homepage (httpx, 8s cap) → plain text
                       (stdlib HTMLParser). Never raises; ok/error dicts.
    db.py              asyncpg pool + schema bootstrap (CREATE IF NOT EXISTS).
  tests/               pytest (uv run pytest from backend/): connectors
                       helpers, config.validate connector problems, and a
                       real MCP round trip against an in-process FastMCP
                       server. asyncio_mode=auto. The postgres-connector
                       tests (SQL guard, packing, plus a live round trip
                       through mcp_server proving Postgres itself refuses a
                       write) use ARYA_TEST_DSN or DATABASE_URL and SKIP
                       when no database answers - `docker compose up -d
                       postgres` before judging a green run.
frontend/src/
  App.jsx              owns every screen + the cross-screen state (cfg,
                       publishFor, nav stack). Build flow: home → start →
                       industry → details → options → preview → call. Standalone
                       screens: publish, deployments, insights, history, docs.
  router.js            HASH router (#/preview, #/publish) - the backend serves
                       one index.html, so real paths would 404 on refresh.
                       Owns PATHS, SCREEN_TITLES, NEEDS_CFG (screens that
                       cannot render without a config in flight) and the
                       sessionStorage persistence for cfg + publishFor.
  api.js               fetch wrapper; ?api= overrides backend origin. Attaches
                       err.status so callers can tell 404 from a dead backend.
  screens/ components/ Start is the scratch-vs-template chooser; IndustryPicker
                       + BusinessForm are the new-agent wizard; Options puts
                       identity (name, business, first message) and the
                       provider + its key up front, everything else (models,
                       local backend, RAG, search, connectors, memory, evals,
                       system prompt) under one collapsed "Advanced settings"
                       <details> that auto-opens only when a hidden field
                       would block Test agent (advNeedsAttention mirrors
                       config.validate);
                       Preview is a read-only workflow map; Publish is one
                       screen for publish + deploy + engine manage (remote mode
                       renders NO credentials on load); Deployments lists per
                       source (local or an engine); Insights + History +
                       Visitors are the monitoring set, all opening the shared
                       ConvDetail transcript (Visitors: who the agent
                       remembers, their memories and calls, Forget button).
                       Orb/Transcript/ChatInput/Menu/KbUpload/
                       StepDots/InfoIcon/CopyField/ConfirmDialog/ErrorNote.
  widget/              the embeddable build: its own entry, api.js (pk-only,
                       no admin routes - vite.config.widget's forbidAdminApi
                       guard fails the build if one creeps in; stores the
                       server-minted visitor_token in localStorage
                       `arya_wt_<pk12>` and posts it on every session start)
                       and widget.css.
```

## The agent config (single source of truth)

`backend/data/active-agent.json` - written by the UI (`PUT /api/agents/active`),
read by the worker at the start of every job. Shape (see any template):

```jsonc
{
  "id": "agent-1a2b3c4d",                   // UNIQUE per created agent (minted
                                            // by the frontend at flow start) -
                                            // memories + documents are scoped
                                            // to it. Template files keep their
                                            // template ids.
  "template_id": "travel",                  // provenance (which template)
  "name": "…", "description": "…", "icon": "🌍",
  "provider": "openai" | "groq" | "local" | "gemini",  // providers.py dispatches on this
  "stt":  { "model": "gpt-4o-mini-transcribe",      // openai path; curated list in
            "language": "en", "vocabulary": [],     // config.OPENAI_MODELS; vocabulary → STT prompt biasing (batch only)
            "realtime": true },                     // openai only: streaming STT over the realtime
                                                    // transcription websocket (default on; env
                                                    // OPENAI_STT_REALTIME=0 is the global kill switch)
  "llm":  { "model": "gpt-4o-mini", "temperature": 0.4,     // temperature/reasoning_effort
            "max_response_tokens": 150 },                   // sent per-model (openai_models catalog).
                                                            // max_response_tokens: output cap, a runaway
                                                            // guard on every provider (default 150, 0=off;
                                                            // floored to 400/500 on reasoning models -
                                                            // brevity itself comes from prompt_blocks.
                                                            // voice_rules' 25-word budget)
  "tts":  { "model": "gpt-4o-mini-tts", "voice": "alloy" }, // openai path; voice options
                                                            // depend on the TTS model
  "local": {                                     // local path - see providers.py
    "backend": "ollama" | "vllm",                // which backend serves the LLM
    "ollama": { "llm_url": "", "llm_model": "qwen3.5:2b" },    // blank url = bundled Ollama
    "vllm": { "stt_url": "", "stt_model": "",    // vLLM: all three overridable;
              "llm_url": "", "llm_model": "",    // llm has NO bundled fallback -
              "tts_url": "", "tts_model": "" },  // blank stt/tts url = bundled Speaches
    "stt_model": "Systran/faster-whisper-small", // bundled Speaches STT model
    "voice": "af_heart"                          // bundled Speaches/Kokoro voice
  },
  "gemini": {                                    // gemini path - its OWN group, not
    "llm_model": "gemini-3.8-flash",             // the shared stt/llm/tts blocks above,
    "tts_model": "gemini-3.8-flash-tts",         // which hold whatever provider was
    "voice": "Zephyr",                           // configured last. No stt_model: ears are
    "expressive": false                          // fixed (gemini-3.5-transcribe-live).
  },                                             // expressive: LiveKit expressive mode on the
                                                 // 3.8 TTS (pauses, mood, pacing); steering
                                                 // lives in gemini_defaults.expressive_options.
                                                 // Curated lists in the gemini_models catalog;
                                                 // llm.temperature is IGNORED on Gemini 3
                                                 // (Google deprecated it, it degrades output).
                                                 // Thinking is pinned off per model there
                                                 // (thinking_config on each llm entry; the
                                                 // accepted knob differs by model, probed live)
  "turn_detection": { "version": "v1-mini", "unlikely_threshold": null },
  "session": { "inactivity_timeout_s": 180 },   // watchdog; calls never end on verbal cues
  "search": { "provider": "serpapi" },          // "serpapi" | "ares" | "tavily";
                                                // endpoints fixed in tools.py, key in keys.json
  "tools": { "weather": true, "web_search": true, "booking": false },
  "connectors": [                           // MCP connectors ATTACHED to this agent
    { "connection_id": "conn-3f9a1b2c4d5e", // row in the connections table (global)
      "allowed_tools": ["list_events", "suggest_time", "create_event"],
      "public_tools": ["suggest_time", "create_event"],  // subset embed visitors may
                                                         // trigger; [] = owner calls only
      "purpose": "appointment booking" }    // fills the type's prompt_block
  ],
  "memory": { "enabled": false,             // off by default; needs mem0 key when on
              "scope": "agent",             // "agent" = private to this agent;
                                            // "user" = shared across agents;
                                            // no UI: every template ships
                                            // "agent" until visitors are
                                            // identified across agents
              "max_writes_per_call": 20 },
  "rag": { "enabled": false, "embedding_profile": "nomic" | "openai",
           "top_k": 5, "query_context_turns": 1, "min_score": null,
           "timeout_s": 2.0, "ocr": "auto" },
  "business": { "business_name": "…", "industry": "…", "agent_purpose": "…",
                "website": "…", "website_context": "…" },  // wizard answers; website_context
                                                           // = fetched homepage text, folded
                                                           // into the generated prompt
  "greeting": "…",   // first message, spoken word-for-word via session.say();
                     // generated from `business`, editable on Options;
                     // empty -> the LLM improvises a greeting
  "instructions": "…"                               // the full system prompt
}
```

Provider keys are **never** in this file - they live in `backend/data/keys.json`
(`{"openai": "sk-…", "groq": "gsk_…", "gemini": "…", "serpapi": "…",
"ares": "…", "tavily": "…", "mem0": "…"}`), written by `POST /api/setup/keys`
(the UI shows them server-masked, `first5…last5`), with env-var fallbacks
(`OPENAI_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY`, `SERPAPI_API_KEY`,
`MEM0_API_KEY`). The `gemini` slot is the Gemini provider's single key,
covering its STT, LLM and TTS.
The same file holds `google_oauth_client_id` / `google_oauth_client_secret`
(the OAuth app the Google Calendar connector signs in with; env fallbacks
`GOOGLE_OAUTH_CLIENT_ID/SECRET`). Connector credentials themselves (tokens,
sign-in grants) live in the Postgres `connections` table, plaintext JSONB
for now (same posture as keys.json; an encrypted column is the planned
follow-up), and `GET /api/agents/{id}/export` strips `connectors` from the
zip. Keep it that way: configs are shareable, keys are not.

## How to extend

**Add a tool** - in `backend/src/tools.py`: write a factory returning a
`@function_tool`-decorated async fn (the *docstring is the contract* the LLM
sees), register it in `build_tools()` behind a `cfg["tools"]["<name>"]`
toggle, and add the toggle to the templates. A larger toolset can live in
its own module: `travel_tools.py` (the `tools.travel` toggle, used by the
Travel Planning Agent template) holds a nine-tool gated flight/hotel/day-plan flow
with per-call closure state - live SerpAPI searches when a `serpapi` key is
stored, canned fixtures otherwise, same spoken output either way. Smaller
toolsets stay in tools.py: `_booking_tools()` (`tools.booking`, shared by
the Reservations Agent and Ticketing Agent templates) is a
check-availability/create-booking fake desk, the pattern every other
toolset below follows (confirm-before-acting, digit-by-digit reference
readback); `_appointment_tools()` (`tools.appointments`, shared by the
Patient Scheduling Agent, Class Scheduling Agent, Client Intake Agent,
Home Visit Agent, Student Admissions Agent and Customer Support Agent
templates) is a
book/reschedule/cancel fake desk with per-call reference state;
`_order_tools()` (`tools.orders`, Order & Returns Agent) is the same
pattern for order status and returns. The travel concierge's written
itinerary is themed at generation (builder calls get the builder-matched
palette, widget calls a neutral one, keyed off deployment_id in the job
metadata) and is announced to embedding host pages as an `arya:itinerary`
DOM event (payload: id, url, title, stage, data) with a re-fetch endpoint
at GET /api/public/v1/itinerary/{id}; a properly paginated PDF of any
artifact is served at GET /api/artifacts/{id}/pdf (WeasyPrint, same HTML
and print CSS as the page). There is
deliberately NO end_call tool: the model cannot end a session on any
verbal cue. Calls end only via the UI's End-call button or the worker's
inactivity watchdog (`session.inactivity_timeout_s` in the config, default
180 s - warning 30 s before the cutoff, then a sign-off and room delete);
the frontend just reacts to the disconnect.

**Add a connector (MCP)** - three layers, do not mix them up. (1) The
`connectors` catalog (catalog.py SEEDS, `PUT /api/catalogs/connectors`) is
the registry of TYPES: id, label, `auth` (none | bearer | api_key | oauth),
`url_source` ("user" = typed in the Add dialog, "sidecar" = arya-mcp plus
this entry's `path`, "fixed" = the entry's own
`url`, e.g. Google's hosted Calendar MCP server), `transport`,
`write_tools` (unchecked by default, "writes" badge, confirm-guarded
against duplicate calls), a `prompt_block` appended to the system prompt
for every LIVE connector on a call ({name}, {purpose}, {account}), and
`docs_hint`. A hosted vendor server with bearer auth is a catalog entry
and nothing else. (2) The `connections` table is the user's INSTANCES,
global across agents: `/api/connections` (list / create with probe-on-save
/ patch / delete with force=1 detaching everywhere / `{id}/test`), plus
`{id}/oauth/start` (admin router, returns the consent URL) and
`/api/connections/oauth/callback` on `open_rt` (the provider redirects the
browser there without our header; the row is found by a single-use,
expiring `state`). Rows go out masked (`_connection_out`). (3) Per-agent
attachment is `cfg["connectors"]` (shape above), edited on Options
(`components/ConnectorCard.jsx`), described on Preview from the
`tool_catalog` entry `connector:<type>`. Runtime: `connectors.build_toolsets`
in agent.py runs BEFORE `Assistant(...)` so `has_tools` is true only when a
connector delivered tools (gotcha 4) and duplicate tool names are filtered
(a collision raises in `Agent.__init__`). Public (embed) sessions only get
`public_tools`. Each MCP tool call lands in `tool_events`. An OAuth type
needs its app credentials in keys.json (see the catalog entry's
`oauth.keys`); to add a second OAuth provider generalize on
`oauth_provider`, the callback is already provider-agnostic. Web search is
deliberately NOT a connector: direct API calls, lower latency, and the
citation chips depend on its result shape. Operator docs:
`docs/CONNECTORS.md` (in-app at #/docs/connectors).

Decide FIRST which of the three sources a new connector uses, because it
decides how much code there is: a vendor that hosts its own MCP server with
a pasted token is a catalog entry and nothing else (add `header` when it
does not use `Authorization: Bearer`, e.g. Composio's `x-api-key`); one
that needs a sign-in adds an `oauth` block; only a system with NO hosted
server needs code, as a module in `src/mcp_tools/` (see
`postgres_readonly.py`) registered in `mcp_server.MODULES`, with
`url_source: "sidecar"`, a `path`, and optionally a `check_path` whose
route validates the credential for real. Prefer a ~150-line Python module
over running a vendor's own Node/stdio server: those take one credential
from env at startup, so they cannot serve several saved connections and
would put Node in the image. Sidecar modules must stay credential-free
(read it from the request header) so one process serves every connection
and the service holds no authority of its own.

**Serving a sample toolset** - the one exception to replacement. A
connector type may declare `keeps_when: {"orders": "shop_desk"}`: when the
connection's config carries that flag, the desk is NOT replaced but runs
over that connection. The flag is set by the type's check route (postgres
reports `shop_desk` when it finds the desk's four tables), copied onto the
connection by `api._absorb_check`, honoured by `connectors.replaced_toolsets`
and surfaced by `connectors.served_toolsets`, which agent.py uses to put the
connection string on `tool_runtime["shop_dsn"]`. Preview and ConnectorCard
read the same flag from the connection row. So "attach the database and the
order desk uses it" is decided by what the database contains, never by a
checkbox, and a database that is not the shop still replaces the desk.

**Replacing a sample toolset** - the demo desks in tools.py invent
plausible data, so a real connector must be able to switch one off or the
model can answer from the fake one. Mechanism: the catalog entry declares
`overlaps: ["appointments"]`, the attachment carries `replaces: [...]`
(pre-ticked when the agent has that toggle on, editable in ConnectorCard),
`connectors.replaced_toolsets(cfg)` unions them, and agent.py passes that
as `skip=` to `tools.build_tools`. Read from the CONFIG, never from which
connectors actually connected: if the real system is down mid-call the
agent must say it cannot look that up, and falling back to a desk that
invents an order status or a booking is worse than admitting the outage.
It also has to be knowable before any network call, since build_tools runs
first. The toggle itself stays true, so unticking brings the demo back;
Preview shows such a toolset as "replaced" rather than "on", and
`config.validate` warns while a live connector sits next to an un-replaced
fake one.

**Add a channel** - a channel is INBOUND (a customer arrives), which is
the opposite axis from a connector (the agent reaches out), so do not try
to model one as the other. Three pieces. (1) A `channels` row hanging off
a `deployments` row: the deployment owns the pk_ key, the caps and the
public/visitor gating, so a channel session inherits all of it for free
and `is_public_session` stays true by construction. Per-channel behaviour
goes in `config` JSONB, credentials in `secret` JSONB (plaintext, same
posture as connections). (2) A bridge process that owns the provider's
webhook and joins rooms as a data-only participant; it must run with
network_mode: host (gotcha 1) and it starts sessions through
`POST /api/public/v1/sessions` with `channel` set, never through a private
path, so quotas and refusals are counted once. (3) One `if channel:`
branch in agent.py: audio disabled both ways, no spoken greeting, no
inactivity watchdog (silence is normal in a chat), and the TEXT variant of
`Instructions` carrying `prompt_blocks.channel_text_rules`, which exists to
UNDO the voice rules - digit-grouped reference numbers and "the link is in
the chat" are wrong in writing. Keep the provider's payload parsing in its
own module with no FastAPI/DB/LiveKit imports (`meta_wa.py`) so it stays
testable offline; the bridge is the only place that knows about rooms.
Operator docs: `docs/CHANNELS.md` (in-app at #/docs/channels).

**Add a template** - drop `backend/agents/templates/<id>.json` (copy
travel-concierge.json). It appears in the gallery automatically. `_`-prefixed files
are hidden. Leave `business.business_name` empty and use the variables
`{business_name}` / `{agent_name}` anywhere `instructions`/`greeting` would
otherwise name the business or the agent - a customer must type both
(enforced as required fields in Options.jsx and BusinessForm.jsx), and
`config.render_vars()` fills them on every call (the saved prompt stays a
template; the legacy `<Business Name>` spelling still renders). The wizard's
`generate_instructions()`/`generate_greeting()` emit the same variables.
Never hardcode a fictional identity the way older templates used to.
`industry` (top-level)
and `business.industry` must both exactly match a `value` in catalog.py's
`industries` seed, or the gallery card falls back to the generic icon. The
Preview map describes each `tools.*` toggle from the `tool_catalog` catalog
(heading + meaningful capability rows) - add an entry for a new toggle via
`PUT /api/catalogs/tool_catalog`, no rebuild.

**Add a provider** - one branch in `providers.build_session()` + a
`provider` value + a radio option in Options.jsx. Constraints we enforce:
CASCADED pipelines only (separate STT/LLM/TTS stages - keeps every stage
swappable and lets RAG inject between STT and LLM; do NOT add
speech-to-speech models - a realtime *transcription* endpoint used only
as the STT stage is fine and is what the openai branch does), and one
provider should cover STT+LLM+TTS with
at most ONE key (the "local" provider needs zero). The "local" branch is
the reference for OpenAI-compatible self-hosted backends: same
openai.STT/LLM/TTS plugin, different base_url per stage.

Gemini was long recorded here as impossible on one key, because Google's
cascaded STT (Chirp) needs GCP service-account credentials. That is still
true of Chirp, but it is the WRONG class: `google.STT` is Cloud STT, while
`google.beta.GeminiSTT` talks to the Gemini Live API with the same plain
GOOGLE_API_KEY as the LLM and TTS (and streams, with interim results). The
`gemini` provider is built on the beta class for exactly that reason. If
you ever touch that branch, do not "simplify" it to `google.STT`: the call
would then demand service-account credentials Arya never collects, and
keys.json has nowhere to put them (it stores one-line secrets, and
mask_secret renders `first5…last5`).

**Swap the local models** - the config's `local` group, split by
`local.backend`:

- **Ollama** (`local.ollama`): only `llm_url`/`llm_model` are yours to set -
  Ollama has no usable speech route at all (an undocumented
  `/v1/audio/transcriptions` exists but isn't Whisper - it reformats audio
  into a chat prompt, and Ollama's library ships no model that can answer
  it). `llm_model` is any Ollama tag (`name:tag`, e.g. `qwen3.5:2b`), or
  `hf.co/<user>/<repo>[:quant]` to pull a GGUF straight from HuggingFace.
  Pull it first: `docker compose exec ollama ollama pull <tag>` (an unpulled
  tag 404s). **Prefer tool-capable models** - this agent registers function
  tools, and not every small model supports them: `gemma3` (all sizes)
  supports **no** tool calling at all (`gemma4` does, but its smallest size
  is 12B - too big for an 8 GB CPU box); `smollm2` claims support but is
  unreliable at its size. Good tool-capable picks, all Apache-2.0 or MIT:
  `qwen3.5:2b` (the default: non-thinking at this size, 2.7 GB pull),
  `qwen3.5:4b` (same family, noticeably smarter, ~+1 GB RAM),
  `qwen3.5:9b` (~5 GB pull, ~6 GB RAM: for 16 GB machines), `granite4.1:3b`,
  `phi4-mini:3.8b`. Not suggested: `qwen2.5:3b` (that one size is under the
  non-commercial Qwen Research License) and `llama3.2:3b` (works, but the
  Llama Community License adds "Built with Llama" conditions; see
  `docs/MODEL-LICENSES.md`). Also note:
  Ollama's `/v1/chat/completions` does **not** honor `tool_choice` - don't
  rely on forcing a specific tool through this backend.
- **vLLM** (`local.vllm`): `stt_url`/`stt_model`, `llm_url`/`llm_model`,
  `tts_url`/`tts_model` are all independently settable - model ids are
  HuggingFace repo ids (`Qwen/Qwen3-4B-Instruct-2507`,
  `openai/whisper-large-v3-turbo`), not Ollama-style tags. **vLLM serves one
  model per server process** - `vllm serve <model> --port <port>`, a
  separate invocation per stage (avoid port 8000, Arya's API owns it).
  Core vLLM has **no TTS route** (`/v1/audio/speech` 404s) - leave
  `tts_url`/`tts_model` blank to use the bundled Speaches/Kokoro instead, or
  point them at your own `vllm-omni` deployment. `llm_url`/`llm_model` have
  **no bundled fallback** (`config.validate()` blocks Test if either is
  blank when `backend == "vllm"`; `providers.py` raises the same as a
  worker-side backstop).
- **Discovery**: `GET {endpoint}/v1/models` on either engine returns exactly
  what that server serves (Ollama's pulled tags, vLLM's `--served-model-name`
  aliases) - `POST /api/local/check` (the Options screen's Check button)
  wraps this server-side, since the endpoint may be container-internal and
  unreachable from the browser.
- Both backends' bundled speech stages share one `stt_model`
  (faster-whisper, e.g. `Systran/faster-whisper-base` for lower CPU) and one
  `voice` (any Kokoro voice: af_heart, af_bella, am_michael, …) at the
  top of `local`.
- **RAG note**: `config.embedding_profile()`'s Nomic embeddings always call
  the bundled `ollama` container (`config.ollama_url()`), independent of
  `local.ollama.llm_url` / `local.vllm.llm_url` - repointing the chat LLM
  never moves embeddings. A vLLM-backend agent with RAG on still needs the
  bundled `ollama` service running.

**Swap the OpenAI models** - the dropdowns on Options (STT/LLM/TTS/voice,
under Advanced settings)
are populated from `config.OPENAI_MODELS`, served read-only via
`GET /api/models`; `config.OPENAI_DEFAULTS` is what a config with no
explicit `stt.model`/`llm.model`/`tts.model` falls back to (`providers.py`'s
openai branch reads both). To add a model, add one entry to the relevant
list - `id` is sent to the plugin verbatim, `label`/`hint` are UI-only. Two
per-model flags exist for a reason, set them deliberately:
- `temperature: false` on an LLM entry **omits** the `temperature` kwarg -
  required for reasoning-tuned models that 400 on any value but their
  default (see the `parallel_tool_calls` conditional right above it in
  `providers.py` for the same pattern).
- `reasoning_effort: "none"` on an LLM entry is **required** for any model
  the `livekit-plugins-openai` plugin doesn't auto-pin a reasoning effort
  for (gpt-5.6-* as of this writing) - otherwise every turn pays full
  reasoning latency and the agent feels broken in a live call. Check the
  installed plugin's `_supports_reasoning_effort()` before assuming a new
  model needs this.
- A TTS entry's `voices` list is model-specific (`gpt-4o-mini-tts` supports
  more than `tts-1`/`tts-1-hd`) - Options.jsx filters the Voice dropdown by
  the selected TTS model and clamps to `alloy` if the current voice isn't
  in the new list, so don't assume every voice works with every TTS model.
`config.validate()` deliberately does not reject unknown model ids -
curation is a UI affordance, not a backend restriction.

**Change RAG behavior** - retrieval knobs are all in the config's `rag`
group; excerpts are injected per turn in `Assistant.on_user_turn_completed`
(agent.py), behind a hard timeout. A chip means **used, not retrieved**:
`_used_in_answer` (agent.py) keeps a citation only when the answer visibly
overlaps the source - stemmed word/bigram overlap, matching identifiers,
or matching amounts, with numbers normalized across spellings ("five
thousand" ≡ "5,000" ≡ 5000). Grounding is then visible three ways: live
under the bubble in the call window (the worker publishes each cited
answer's sources on the `arya.citations` text-stream topic; Transcript.jsx
pairs them to bubbles by content - each event carries the answer's opening
words), the same rendering in the Conversations history, and the
`citations` JSONB on each message row. Rendering is one shared component,
`components/SourceCards.jsx`, driven by `Transcript`'s `present` prop
(`{sources, docChips}`, replacing the old `richLinks` boolean): the builder
and History pass `sources: "chips"` (one `CiteChip` per document plus the
"Web sources" chip popover, unchanged); the WIDGET and the `/embed/{pk}`
share page pass `sources: "collapsed"` (v3 default) - a one-line "Sources ·
N" `<details>` that opens into the same clickable source CARDS (domain,
title, snippet, all from the search tool, no extra fetch) `"cards"` mode
always showed, ordered so a domain the agent actually said ("github dot
com") comes first; `"off"` renders no web results at all (the
`show_sources` deployment flag, see below). `docChips: false` on both
public surfaces additionally skips document chips IN CODE, defence in
depth alongside the server-side gate. Publish gate in agent.py: web
citations go to EVERY session, doc citations only to builder sessions
(`is_public_session`), so visitors get links but never document names or
excerpts.

**The one link an answer gives** (`links.py`, `components/LinkChips.jsx`) -
a hard product rule: never expose the full search-result list as clickable
links, only the page an answer actually named or the caller asked for.
`links.resolve_links(answer, question, web_cits)` extracts literal and
spoken ("github dot com") mentions from the answer, verifies each against
the turn's own web citations (base-domain aware, so a wrong URL a small
local model typed still resolves to the real citation link - the model's
text is a hint, never the href), falls back to the single best-scoring
citation when the caller asked for a link but named no domain, and returns
at most 2 items, never more. Published on `arya.links` (same head-matching
contract as citations/options - `Transcript.jsx`'s `attachByHead` handles
all three), persisted in `messages.links`, rendered as `LinkChips` directly
under the bubble, above the option cards and the Sources control, in every
session including public ones (these are public pages; the voice rule now
tells the agent to say the site name and point at the chip rather than
read `https://` addresses aloud - `catalog.py` `voice_rules`). `SourceCards`
receives the same-bubble link list as `exclude` so a page is never shown
twice.

**Travel option cards** (`arya.options`, `components/OptionCards.jsx`)
carry NO links by design; in a live call (builder and widget) each row is
a button whose tap sends the item's `pick` string (built server-side from
`n`, kind-aware for a lone rate) as a typed turn through the same
`sendTyped` path as the chat box, so `select_flight` / `select_hotel`
handle it like a spoken pick - recorded client-side in `Transcript`'s
`pickedMap` so the chosen row keeps a check and its siblings dim,
permanently (a second tap cannot re-send). History renders the rows
static, with no picked state. `label`/`detail`/`value` stay the
spoken-shaped strings (`value` is always the price now, e.g. `$433.04`,
never "Option 1" - that already sits in the numbered badge); an item may
also carry an optional `display` object (`travel_tools.py`,
`mcp_tools/duffel.py`, formatted via the shared `display_fmt.py`) with
24-hour times, a route arrow, stops/duration and a currency-symbol price,
which `OptionCards.jsx` renders as a per-kind row (flight/hotel/rate) when
present and falls back to the generic label/detail/value row otherwise, so
an older stored card with no `display` still renders. Source card and link
chip hrefs go through `safeHref.js` (http(s) only).

**Visitor Sources default** (`deployments.show_sources`, default `true`) -
presentational, not a security gate (web citations are public pages): an
owner can turn off the collapsed Sources control for a site that should
show only the one direct link and nothing else, from a chip button on
Publish & deploy. `PATCH /agents/{id}/deployment` accepts `show_sources`;
`GET /api/public/agent` returns it under `display.show_sources`, which
`Widget.jsx` reads on boot and turns into `present.sources` (`"collapsed"`
or `"off"`).

**OCR on GPU (torch)** - torch ships CPU-only (pinned to the
`pytorch-cpu` index in `backend/pyproject.toml` - saves ~5 GB of CUDA
wheels that nothing here can use; torch only serves docling's document
OCR, never the voice pipeline). To OCR on a GPU instead: delete the
`[[tool.uv.index]]`/`[tool.uv.sources]` torch pin from pyproject.toml,
run `uv lock`, and give the api container a GPU in docker-compose.yml
(`gpus: all` + NVIDIA container toolkit on the host). That's the whole
switch-back.

**Scoping model (memory + documents)** - both are keyed to the agent's
unique `id`. Documents: uploads carry `agent_id` (Postgres column + Qdrant
chunk payload); retrieval filters `agent_id == <active> OR untagged` - an
untagged doc (uploaded with no agent_id, e.g. via curl) is a **shared** doc
visible to every agent. Memories: mem0 `agent_id` entity when
`memory.scope == "agent"` (the default; every template ships it). `"user"`
shares memories across agents, but only for the same visitor id, and a
widget or WhatsApp visitor gets a different id per agent, so it only takes
effect once identity spans agents (e.g. a site login). There is no UI for it.

**Change the generated prompt** - `config.generate_instructions()`. It's a
deterministic template (no LLM call): wizard answers + fetched website text
(`business.website_context`, when present) + voice-output rules (+ KB
grounding rules when RAG is on).

## Gotchas that cost real time (learned in the spike - don't relearn them)

1. **Worker networking**: livekit runs with `--node-ip 127.0.0.1`, so a
   worker on the compose bridge network gets ICE candidates pointing at its
   own loopback and audio silently fails. Hence `network_mode: host` for
   `agent-worker` - or run the worker on the host (`uv run python
   src/agent.py dev` from backend/).
2. **Explicit dispatch, fresh room per call**: the worker registers under
   `ARYA_AGENT_NAME` (default `arya`) and only joins rooms the API
   explicitly dispatches it to, with the agent id in the job metadata -
   this is what lets different rooms run different agents. Consequences:
   rooms created outside the API (`lk` CLI, meet.livekit.io) get NO agent
   unless you set `ARYA_AGENT_NAME=""` on BOTH services (restores the old
   auto-dispatch); API and worker must deploy together when this name
   changes. `POST /api/conversations` still mints a fresh `conv-<hex>`
   room every time - dispatch fires on room creation, never reuse names.
3. **If you ever re-add Gemini Live** (against the cascaded-only rule):
   pin `gemini-2.5-flash` - `gemini-3.1-flash-live-preview` rejects
   `generate_reply()`/`update_chat_ctx()` after the first turn, breaking
   the greeting and RAG.
4. **`parallel_tool_calls`**: OpenAI 400s if it's sent on a request with no
   tools. providers.py only sets it when tools exist.
5. **VAD + turn detector on every provider**: Silero VAD + the local
   `v1-mini` turn detector are required regardless of STT mode (cloud
   turn detector 401s against self-hosted LiveKit). openai.STT streams
   by default (`use_realtime`); groq/local are batch and lean on VAD to
   know when an utterance ended.
6. **Ollama embeddings**: call Ollama's OpenAI-compatible `/v1` with
   `encoding_format="float"` (the client's base64 default gets rejected)
   and any non-empty api_key.
7. **Never block the turn**: RAG has a hard 2s timeout; memory recall 2s;
   memory writes and transcript persistence are fire-and-forget with
   exceptions swallowed. Keep that property for anything you add to the
   hot path.
8. **E2EE off** in any external LiveKit client (meet.livekit.io): with it
   on, the agent hears ciphertext and everything fails silently.
9. **A channel bridge must wait for `arya.ready`.** The agent registers its
   `lk.chat` handler inside `session.start()`. Text written into the room
   before that is dropped silently, with no error anywhere, and the
   customer just never gets an answer. The worker publishes `arya.ready`
   once started and whatsapp_bridge waits for it (with a timeout, since a
   late message beats a lost one). Same trap for any future channel.
10. **Register byte-stream handlers BEFORE publishing `arya.ready`.**
   Same trap as 9, one layer down. A photo is published on
   `arya.attachments` as soon as the bridge sees the room is ready, so a
   handler registered after the ready signal misses the first one, and
   the customer's opening photo is the one most likely to be lost. Both
   registrations happen before the signal in agent.py.
11. **Docker Desktop does not forward ports for host-networked
   containers.** arya-whatsapp listens on 8030 inside the Docker VM's
   host namespace; neither Windows nor the WSL distro can reach it,
   because only published `ports:` are forwarded and a host-mode
   container has none. A dev tunnel must run as a container with
   `--network host` or it 502s. Linux hosts are unaffected.
12. **One webhook callback URL per Meta app.** Arya, n8n and Chatwoot
   cannot all subscribe to the same app. Either give each its own app on
   the same WhatsApp Business Account, or accept that whoever owns the URL
   owns the traffic. Worth saying out loud because the failure looks like
   "Arya stopped receiving messages" with nothing in any log.
13. **conversations.metadata is shared state - always MERGE, never assign.**
   The API stamps agent_id/agent_name/user_id at creation; the worker's
   shutdown callback adds usage. A plain `SET metadata = $x` from either
   side wipes the other's keys (this silently broke History's agent names
   once). Use `metadata = COALESCE(metadata,'{}'::jsonb) || $x::jsonb`.
14. **Identity for the order desk comes from the channel, never from the model.** `tool_runtime["external_user"]` is the WhatsApp number Meta reported, set by the worker from job metadata; the desk resolves it to a customer id and every SQL statement carries that id. A tool that took a customer id or phone as an argument would let the model pass someone else's, and on a public channel the model is repeating whatever the user typed. Same rule for any future desk or connector that serves public sessions: scope server-side, on an identity the session was born with.
15. **Visitor identity is minted by the server, per deployment.** A widget
   `visitor_token` is signed over `deployment_id:rand`, so it is rejected
   (and silently replaced) on any other published agent, and a browser can
   never choose its own `user_id`. Consequences: builder test calls (raw
   browser UUID via `POST /api/conversations`) and widget calls for the
   same agent are DIFFERENT visitors by design; rotating
   `ARYA_VISITOR_SECRET` turns every widget visitor into a new person;
   channel fields (`channel`, `external_user_id`) are honoured only with
   `X-Arya-Bridge-Token`, so arya-whatsapp and the api must share
   `ARYA_BRIDGE_TOKEN` (or both fall back to deriving it from
   `LIVEKIT_API_SECRET`, which compose passes to both). What signing does
   not do: protect a token lifted from the host page's localStorage.

## Verifying changes

- API smoke: `curl localhost:8000/api/health`, `/api/agents/templates`,
  `POST /api/conversations` returns `{token, ws_url, room}`.
- Worker: `docker compose logs -f agent-worker` - look for
  `agent '<name>' provider=…` per call, then turn metrics lines.
- Frontend build: `cd frontend && npm run build` (docker build does this too).
- Full flow: README's 5 steps, then per template: travel → ask something
  current (search tool should fire, visible in worker logs); booking → book
  a table (check_availability + create_booking round-trip); any agent with
  RAG on → upload a doc, wait for `ready`, ask about its contents.
- Metrics land in `backend/metrics/*.jsonl` (per-session) and
  `backend/metrics/rag.jsonl` (retrieval quality).
- Cal.com: `uv run pytest tests/test_calcom.py` runs the whole booking flow
  against a fake Cal.com, so it needs no account. Set `ARYA_CALCOM_KEY` to
  also check a real one. Live: attach it, then ask an agent "what do you
  offer", "when are you free Thursday", "book me in at 3".
- Sidecar: `curl localhost:8020/health` lists the hosted modules;
  `curl -sX POST localhost:8020/postgres/check -H "X-Arya-Dsn: <dsn>"`
  answers `{ok, db, user, superuser, tables, warnings}` and is exactly what
  the API calls when a database connection is saved.
- Connectors: `cd backend && uv run pytest` (helpers + a real MCP round
  trip). Live: Options → Connectors → Add server against any streamable
  HTTP MCP server (from Docker use `host.docker.internal`), attach, Test;
  a call then logs `connector '<name>': N tool(s) in <ms>` and `tool call:
  <name> (...) ok=True` per use, and `SELECT * FROM tool_events` fills.
  Stop the server: `connector '<name>' unavailable: ...` and the greeting
  still plays.
