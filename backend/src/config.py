"""Agent configuration: templates, the active agent, provider keys, and
system-prompt generation.

Everything the UI configures lands in two gitignored files under
backend/data/:

  active-agent.json  — the one agent the worker runs (written by
                       PUT /api/agents/active; read fresh at the start of
                       every job so UI edits apply to the next call)
  keys.json          — provider API keys typed into the UI
                       (written by POST /api/setup/keys)

Infrastructure endpoints (Postgres, Qdrant, Ollama, S3, LiveKit) come from
environment variables with docker-compose-friendly defaults.
"""

import json
import os
import pathlib
import re

import catalog

BACKEND_DIR = pathlib.Path(__file__).parent.parent
DATA_DIR = BACKEND_DIR / "data"
TEMPLATES_DIR = BACKEND_DIR / "agents" / "templates"
ACTIVE_PATH = DATA_DIR / "active-agent.json"
KEYS_PATH = DATA_DIR / "keys.json"
AGENTS_DIR = DATA_DIR / "agents"

# Keys are identified by provider slug in keys.json and the UI. The two
# google_oauth_* entries are not a provider key: they are the OAuth app
# (client id + secret) the Google Calendar connector signs users in with.
# Same store on purpose: masking, env fallback and the Options key fields
# all come for free, and one Google Cloud app serves every Google connector.
KEY_NAMES = (
    "openai", "groq", "gemini", "serpapi", "ares", "tavily", "mem0",
    "google_oauth_client_id", "google_oauth_client_secret",
)

# Env fallbacks for technical users who prefer exporting keys over the UI;
# a key typed into the UI (keys.json) always wins.
KEY_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "serpapi": "SERPAPI_API_KEY",
    "ares": "ARES_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "mem0": "MEM0_API_KEY",
    "google_oauth_client_id": "GOOGLE_OAUTH_CLIENT_ID",
    "google_oauth_client_secret": "GOOGLE_OAUTH_CLIENT_SECRET",
}

# Model lists, provider defaults, the search registry, and embedding
# profiles all live in catalog.py (DB-backed, seeded from catalog.SEEDS,
# editable via PUT /api/catalogs/{name} without a rebuild).


def search_provider(provider: str) -> dict | None:
    """The search registry entry for a provider id, or None if unknown."""
    return next(
        (p for p in catalog.get("search_providers") if p["id"] == provider),
        None,
    )


def search_ready(cfg: dict, keys: dict) -> tuple[bool, str]:
    """(is the configured web-search provider usable, why-not text)."""
    search = cfg.get("search", {}) or {}
    provider = search.get("provider", "serpapi")
    entry = search_provider(provider)
    if entry is None:
        return False, f"unknown search provider '{provider}'"
    if not keys.get(entry["key"]):
        return False, (
            f"Web search is on but the {entry['label']} API key is missing."
        )
    return True, ""

ALLOWED_FORMATS = [".pdf", ".docx", ".md", ".txt"]


def _normalize_base_url(url: str | None, fallback: str = "") -> str:
    """Blank -> fallback (a bundled container's URL); otherwise strip a
    trailing slash and ensure the path carries /v1 exactly once. Kills the
    most common paste error (http://host:11434 vs .../v1)."""
    url = (url or "").strip()
    if not url:
        return fallback
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    return url


# ─── infra endpoints (env with local defaults) ──────────────────────────────

def database_url() -> str:
    # No credentialed default — the repo ships no working secrets. Compose
    # injects this; host runs get it from backend/.env.local (scripts/init.sh).
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set — run ./scripts/init.sh once"
            " (it writes backend/.env.local for source runs)"
        )
    return url


def qdrant_url() -> str:
    return os.environ.get("QDRANT_URL", "http://localhost:6333")


def ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", "http://localhost:11434")


def embeddings_url() -> str:
    """Where keyless embedding profiles (nomic) run. Deliberately separate
    from OLLAMA_URL: repointing the chat LLM at a remote Ollama/vLLM must
    never move RAG embeddings off the bundled container (compose pins this
    to the bundled service). Falls back to OLLAMA_URL so an unset var
    behaves exactly as before."""
    return os.environ.get("EMBEDDINGS_URL") or ollama_url()


def speaches_url() -> str:
    """Local Whisper STT + Kokoro TTS (Speaches, OpenAI-compatible on both
    /v1/audio/transcriptions and /v1/audio/speech)."""
    return os.environ.get("SPEACHES_URL", "http://localhost:8010")


def arya_mcp_url() -> str:
    """The arya-mcp sidecar (src/mcp_server.py), which hosts the MCP servers
    Arya runs itself: connectors whose catalog entry says
    `url_source: "sidecar"` are reached at this base plus their `path`.
    Compose sets it per service (http://arya-mcp:8020 from api, localhost
    from the host-network worker), same split as OLLAMA_URL."""
    return os.environ.get("ARYA_MCP_URL", "http://localhost:8020").rstrip("/")


def s3_config() -> dict:
    # Endpoint/bucket have safe local defaults; the CREDENTIALS do not —
    # missing keys fail loudly here instead of auth-failing mysteriously.
    if not (os.environ.get("S3_ACCESS_KEY") and os.environ.get("S3_SECRET_KEY")):
        raise RuntimeError(
            "S3_ACCESS_KEY / S3_SECRET_KEY are not set — run ./scripts/init.sh"
            " once (it writes backend/.env.local for source runs)"
        )
    return {
        "endpoint": os.environ.get("S3_ENDPOINT", "localhost:8333"),
        "access_key": os.environ["S3_ACCESS_KEY"],
        "secret_key": os.environ["S3_SECRET_KEY"],
        "bucket": os.environ.get("S3_BUCKET", "arya-docs"),
        # TLS off by default (bundled SeaweedFS is plain http); any real
        # S3/R2 endpoint needs S3_SECURE=true.
        "secure": os.environ.get("S3_SECURE", "").lower() in ("1", "true", "yes"),
    }


# ─── keys ────────────────────────────────────────────────────────────────────

def load_keys() -> dict:
    try:
        keys = json.loads(KEYS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        keys = {}
    for name, env_var in KEY_ENV_VARS.items():
        if not keys.get(name) and os.environ.get(env_var):
            keys[name] = os.environ[env_var]
    return keys


def save_keys(patch: dict) -> dict:
    """Merge non-empty values into keys.json; empty string deletes a key."""
    DATA_DIR.mkdir(exist_ok=True)
    keys = load_keys()
    for name, value in patch.items():
        if name not in KEY_NAMES:
            continue
        if value:
            keys[name] = value.strip()
        elif name in keys and value == "":
            del keys[name]
    # Atomic write: a crash mid-write must never truncate keys.json (a
    # corrupt file silently reads back as "no keys saved").
    tmp = KEYS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(keys, indent=2) + "\n")
    # Owner-only: this file holds live provider keys in plaintext (the
    # documented single-user tradeoff) — it should never be world-readable.
    tmp.chmod(0o600)
    tmp.replace(KEYS_PATH)
    return keys


def key_status() -> dict:
    """Presence booleans only — actual key values never leave the server."""
    keys = load_keys()
    return {name: bool(keys.get(name)) for name in KEY_NAMES}


def mask_secret(value: str) -> str:
    """Redacted display form (first5…last5); short values degrade to a
    two-character stub so nothing useful leaks.

    PRESENTATION ONLY — never a security boundary. Provider keys are
    genuinely secret (and this is all the UI ever sees of them), but a
    deployment's public_key is plaintext by design: it ships inside
    customers' page source. Masking it only keeps a screenshare or a
    shoulder-surfer from walking away with a live key.
    """
    value = value or ""
    if not value:
        return ""
    return f"{value[:5]}…{value[-5:]}" if len(value) > 12 else f"{value[:2]}…"


def masked_keys() -> dict:
    """Redacted display forms for the settings UI."""
    keys = load_keys()
    out = {}
    for name in KEY_NAMES:
        value = keys.get(name) or ""
        if not value:
            continue
        out[name] = mask_secret(value)
    return out


# ─── templates + active agent ────────────────────────────────────────────────

def list_templates() -> list[dict]:
    out = []
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        if path.stem.startswith("_"):
            continue  # hidden bases (e.g. _base.json for the custom flow)
        try:
            cfg = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        out.append(
            {
                "id": cfg.get("id", path.stem),
                "name": cfg.get("name", path.stem),
                "description": cfg.get("description", ""),
                "icon": cfg.get("icon", "🤖"),
                # Gallery grouping only (which industry's page shows this
                # template) — distinct from business.industry, which is
                # prompt grammar for the example itself.
                "industry": cfg.get("industry", ""),
            }
        )
    return out


def normalize_tools(cfg: dict) -> dict:
    """Fill in toggles that did not exist when an agent was saved.

    `hotels` and `places` split off `travel` (PLAN-Flight) so a flights
    connector can replace the flight desk while the hotel rates and the
    things-to-do planner stay. An older agent has `travel` alone; without
    this it would still get the other two at runtime (tools.build_tools
    defaults the same way) but Preview, which reads the config, would not
    show them. Filled on load so the two agree and a re-save writes it."""
    tools = cfg.get("tools")
    if isinstance(tools, dict) and "travel" in tools:
        for split in ("hotels", "places"):
            if split not in tools:
                tools[split] = bool(tools.get("travel"))
    return cfg


def load_template(template_id: str) -> dict | None:
    path = TEMPLATES_DIR / f"{template_id}.json"
    if not path.is_file():
        return None
    cfg = normalize_tools(json.loads(path.read_text()))
    # Templates store only their role-specific preamble; the shared voice
    # rules are appended here from the prompt store — so a rules edit no
    # longer has to be hand-synced into every template file. The guard
    # keeps an old-style template (full copy inlined) from doubling up.
    ins = (cfg.get("instructions") or "").rstrip()
    if ins and "# Output rules" not in ins:
        cfg["instructions"] = (
            ins + "\n\n" + catalog.get("prompt_blocks")["voice_rules"]
        )
    return cfg


def load_active() -> dict:
    """The active agent, falling back to the first template on first run."""
    try:
        return normalize_tools(json.loads(ACTIVE_PATH.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    for tpl in list_templates():
        cfg = load_template(tpl["id"])
        if cfg:
            return cfg
    raise RuntimeError("no active agent and no templates found")


def save_active(cfg: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = ACTIVE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(ACTIVE_PATH)


# ─── saved agents (one JSON per created agent, under data/agents/) ──────────

_ID_RE = re.compile(r"^[\w][\w.-]*$")  # also blocks path traversal in ids


def _agent_path(agent_id: str) -> pathlib.Path | None:
    if not agent_id or not _ID_RE.match(agent_id):
        return None
    return AGENTS_DIR / f"{agent_id}.json"


def save_agent(cfg: dict) -> None:
    path = _agent_path(cfg.get("id", ""))
    if path is None:
        return
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def list_saved_agents() -> list[dict]:
    if not AGENTS_DIR.is_dir():
        return []
    out = []
    for path in sorted(
        AGENTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        try:
            cfg = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        out.append(
            {
                "id": cfg.get("id", path.stem),
                "name": cfg.get("name", path.stem),
                "description": cfg.get("description", ""),
                "icon": cfg.get("icon", "🤖"),
                "industry": (cfg.get("business") or {}).get("industry", ""),
                "template_id": cfg.get("template_id"),
            }
        )
    return out


def load_saved_agent(agent_id: str) -> dict | None:
    path = _agent_path(agent_id)
    if path is None or not path.is_file():
        return None
    return normalize_tools(json.loads(path.read_text()))


def delete_saved_agent(agent_id: str) -> bool:
    path = _agent_path(agent_id)
    if path is None or not path.is_file():
        return False
    path.unlink()
    return True


def deep_merge(dst: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v


# ─── embedding profiles ──────────────────────────────────────────────────────

def embedding_profile(cfg: dict, keys: dict) -> dict:
    """Resolve the active embedding profile to a runnable spec, from the
    catalog registry (model + dim + collection travel together — they are
    coupled: a model change without a new collection corrupts the index).

    A profile with no base_url uses the local embeddings endpoint
    (EMBEDDINGS_URL, falling back to OLLAMA_URL — see embeddings_url());
    one with no key_name is keyless (Ollama ignores the api_key, any
    non-empty string).
    An unknown profile name falls back to nomic — the keyless default.
    """
    name = cfg.get("rag", {}).get("embedding_profile", "nomic")
    profiles = {p["id"]: p for p in catalog.get("embedding_profiles")}
    p = profiles.get(name) or profiles["nomic"]
    return {
        "name": p["id"],
        "model": p["model"],
        "dim": p["dim"],
        "collection": p["collection"],
        "base_url": p.get("base_url") or embeddings_url() + "/v1",
        "api_key": keys.get(p["key_name"], "") if p.get("key_name") else "ollama",
    }


# How a channel answers: one way or the other, never both. The legacy
# names are the ones the first build shipped with and are still sitting in
# stored rows, so they stay readable forever.
VOICE_MODES = {
    "match": "match", "mirror": "match",
    "text": "text", "off": "text",
    "voice": "voice", "always": "voice",
}
DEFAULT_VOICE_MODE = "match"


def normalize_voice_mode(setting: str | None) -> str:
    """One of match, text or voice.

    Lives here rather than in the bridge because the API validates and
    returns the same values, and the two services must not drift on what
    a stored row means. Anything unrecognised falls back to the default
    rather than to a guess.
    """
    return VOICE_MODES.get((setting or "").strip().lower(), DEFAULT_VOICE_MODE)


def vision_capable(cfg: dict) -> bool:
    """Can this agent's LLM read an image that arrives with a turn?

    Decides whether a WhatsApp photo is forwarded to the model or declined
    politely, so it is deliberately catalog-driven rather than a model list
    hardcoded here: supporting a new vision model is a catalog edit, not a
    redeploy. A catalog row written before the `vision` flag existed
    carries no opinion on an entry, so the shipped seed answers for it; an
    explicit false in the catalog always wins.
    """
    provider = cfg.get("provider", "openai")
    if provider == "openai":
        model = (cfg.get("llm", {}) or {}).get("model") or catalog.get(
            "openai_defaults"
        )["llm_model"]
        for source in (catalog.get("openai_models"),
                       catalog.SEEDS["openai_models"]):
            entry = next(
                (m for m in source.get("llm", []) if m.get("id") == model),
                None,
            )
            if entry is not None and "vision" in entry:
                return bool(entry["vision"])
        return False
    if provider == "local":
        # Local model names are free text, so this matches the family
        # prefix. vllm and ollama each keep their own llm_model.
        lcl_cfg = cfg.get("local", {}) or {}
        defaults = catalog.get("local_defaults")
        backend = lcl_cfg.get("backend", "ollama")
        sub = lcl_cfg.get(backend, {}) or {}
        model = (sub.get("llm_model") or (defaults | lcl_cfg).get(
            "llm_model", ""
        ) or "").lower()
        prefixes = defaults.get("vision_model_prefixes") or []
        return any(model.startswith(str(p).lower()) for p in prefixes)
    # Groq serves text-only open models today.
    return False


# ─── validation ──────────────────────────────────────────────────────────────

def connector_type(type_id: str) -> dict | None:
    """The connectors registry entry for a type id, or None if unknown."""
    return next(
        (c for c in catalog.get("connectors") if c["id"] == type_id), None
    )


def _connector_problems(cfg: dict, keys: dict, connections: list[dict]) -> list[str]:
    """Problems with the agent's attached connectors, given a snapshot of the
    connections table ({id, type, name, status} per row). Wording names the
    fix, like every other problem string."""
    problems = []
    by_id = {c["id"]: c for c in connections}
    attached = cfg.get("connectors") or []
    # No clash to warn about any more: attaching a connector switches off
    # the sample desk it covers, so both can never be built for one call.
    # See connectors.replaced_toolsets.
    for att in attached:
        if not isinstance(att, dict):
            continue
        cid = att.get("connection_id", "")
        row = by_id.get(cid)
        if row is None:
            problems.append(
                f"Connector '{cid}' was removed. Detach it in Options."
            )
            continue
        entry = connector_type(row["type"]) or {}
        label = entry.get("label", row["type"])
        if row["status"] in ("needs_auth", "pending_auth"):
            problems.append(
                f"{label} '{row['name']}' needs sign-in. Open Options and"
                " click Sign in."
            )
        elif row["status"] == "error":
            problems.append(
                f"Connector '{row['name']}' failed its last check. Open"
                " Options and click Test."
            )
        if not att.get("allowed_tools"):
            problems.append(
                f"Connector '{row['name']}' has no tools selected. Pick at"
                " least one in Options."
            )
        key_names = (entry.get("oauth") or {}).get("keys") or {}
        if entry.get("auth") == "oauth" and not (
            keys.get(key_names.get("client_id"))
            and keys.get(key_names.get("client_secret"))
        ):
            problems.append(
                f"{label} is attached but its OAuth client id and secret"
                " are missing. Add them in Options."
            )
    return problems


def validate(cfg: dict, keys: dict, connections: list[dict] | None = None) -> list[str]:
    """Human-readable problems that would stop this agent from running.

    `connections` is an optional snapshot of the connections table (the API
    passes it; the worker and tests pass nothing). None skips the connector
    checks so this stays sync and DB-free."""
    problems = []
    provider = cfg.get("provider", "openai")
    if provider not in catalog.SUPPORTED_PROVIDERS:
        supported = "', '".join(catalog.SUPPORTED_PROVIDERS)
        problems.append(
            f"Provider '{provider}' isn't supported — use '{supported}'."
        )
    elif provider == "openai" and not keys.get("openai"):
        problems.append("OpenAI API key is missing — add it in Setup.")
    elif provider == "groq" and not keys.get("groq"):
        problems.append("Groq API key is missing — add it in Setup.")
    elif provider == "gemini" and not keys.get("gemini"):
        problems.append("Google API key is missing. Add it in Setup.")
    elif provider == "local":
        lcl = cfg.get("local", {}) or {}
        # Ollama always has a bundled fallback (http://localhost:11434); vLLM
        # does not — we ship no vLLM server, so a blank endpoint here would
        # otherwise fail confusingly mid-call instead of before Test.
        if lcl.get("backend") == "vllm" and not (
            lcl.get("vllm", {}).get("llm_url") or ""
        ).strip():
            problems.append(
                "vLLM backend needs an LLM endpoint URL — add it in Options."
            )
    cap = cfg.get("llm", {}).get("max_response_tokens")
    if cap is not None:
        # 0 = cap off; otherwise below ~50 tokens even a compliant one-
        # sentence answer gets truncated mid-word.
        if not isinstance(cap, (int, float)) or isinstance(cap, bool) or (
            cap != 0 and not 50 <= cap <= 2000
        ):
            problems.append(
                "llm.max_response_tokens must be 0 (off) or 50-2000."
            )
    if cfg.get("tools", {}).get("web_search"):
        ok, why = search_ready(cfg, keys)
        if not ok:
            problems.append(why)
    mem_cfg = cfg.get("memory", {})
    if mem_cfg.get("enabled") and not keys.get("mem0"):
        problems.append("Memory is on but the Mem0 API key is missing.")
    if mem_cfg.get("enabled") and mem_cfg.get("scope", "agent") not in ("agent", "user"):
        problems.append("memory.scope must be 'agent' or 'user'.")
    if (
        cfg.get("rag", {}).get("enabled")
        and cfg.get("rag", {}).get("embedding_profile") == "openai"
        and not keys.get("openai")
    ):
        problems.append(
            "RAG uses OpenAI embeddings but the OpenAI key is missing."
        )
    # AI-judged evals need a cloud judge (reliable forced tool-calls; see
    # providers.build_judge_llm). judge_provider "agent" follows the
    # agent's provider; an explicit "openai"/"groq"/"gemini" overrides it.
    evals_cfg = cfg.get("evals", {}) or {}
    if evals_cfg.get("enabled"):
        choice = evals_cfg.get("judge_provider", "agent")
        judge = provider if choice == "agent" else choice
        labels = {"openai": "OpenAI", "groq": "Groq", "gemini": "Gemini"}
        if judge == "local":
            problems.append(
                "AI evaluation needs an OpenAI, Groq or Gemini judge. Pick"
                " one as the judge in Options, or turn evaluation off."
            )
        elif judge not in labels:
            problems.append(f"evals.judge_provider '{choice}' isn't supported.")
        elif not keys.get(judge):
            problems.append(
                f"Evaluation is on but the {labels[judge]} key its judge"
                " needs is missing."
            )
        judges = evals_cfg.get("judges")
        if judges is not None and not judges:
            problems.append(
                "Evaluation is on but no quality checks are selected."
            )
    if connections is not None:
        problems.extend(_connector_problems(cfg, keys, connections))
    return problems


# ─── system-prompt generation ────────────────────────────────────────────────
# All prompt text lives in the prompt_blocks catalog (DB-backed, editable
# via PUT /api/catalogs/prompt_blocks — no redeploy for prompt iteration).


# Prompt variables. Templates and the wizard write these tokens into
# `instructions` and `greeting` instead of a literal identity; render_vars()
# fills them from the config at call time, so renaming the agent or the
# business on the Options screen takes effect on the next call with no
# prompt editing. The legacy angle-bracket spellings older templates used
# are still honored.
PROMPT_VARS = ("agent_name", "business_name")
_LEGACY_VAR_TOKENS = {
    "agent_name": "<Agent Name>",
    "business_name": "<Business Name>",
}


def prompt_vars(cfg: dict) -> dict:
    """The variable values a config provides (empty string when unset)."""
    return {
        "agent_name": (cfg.get("name") or "").strip(),
        "business_name": ((cfg.get("business") or {}).get("business_name") or "").strip(),
    }


def render_vars(text: str | None, cfg: dict) -> str:
    """Fill {agent_name} / {business_name} in prompt text from the config.

    Targeted replacement, deliberately NOT str.format: a prompt may contain
    other braces (a JSON example, a tool payload) and those must pass
    through untouched. An unset value leaves its token in place rather than
    punching a hole in the sentence; both fields are required in the UI, so
    that only happens on hand-written configs."""
    if not text:
        return text or ""
    for key, value in prompt_vars(cfg).items():
        if value:
            text = text.replace("{" + key + "}", value)
            text = text.replace(_LEGACY_VAR_TOKENS[key], value)
    return text


def generate_greeting(business: dict) -> str:
    """Default first message from the questionnaire answers: spoken
    word-for-word at call start, and editable on the Preview screen. Emits
    the {business_name} variable rather than the literal name so a later
    rename follows through (render_vars fills it per call)."""
    blocks = catalog.get("prompt_blocks")
    if (business.get("business_name") or "").strip():
        return blocks["greeting_with_name"].format(name="{business_name}")
    return blocks["greeting_generic"]


def generate_instructions(business: dict, *, rag_enabled: bool = False) -> str:
    """Deterministic prompt skeleton filled with the wizard answers.

    No LLM call — the answers are interpolated into the prompt_blocks
    templates, and the result is editable on the Preview screen.
    """
    blocks = catalog.get("prompt_blocks")
    # The variable, not the literal name: render_vars() fills it on every
    # call, so the prompt keeps following the Business name field.
    name = "{business_name}" if (business.get("business_name") or "").strip() else "the business"
    industry = (business.get("industry") or "").strip()
    purpose = (business.get("agent_purpose") or "help customers").strip()
    tone = (business.get("tone") or "friendly and professional").strip()
    examples = (business.get("example_questions") or "").strip()

    intro = blocks["intro_template"].format(name=name)
    if industry:
        intro += blocks["intro_industry"].format(industry=industry)
    intro += blocks["intro_purpose"].format(purpose=purpose)
    intro += blocks["intro_tone"].format(tone=tone)

    parts = [intro]
    if examples:
        parts.append(blocks["examples_header"] + "\n" + examples)
    site = (business.get("website_context") or "").strip()
    if site:
        parts.append(blocks["website_context_block"] + "\n\n" + site)
    parts.append(blocks["voice_rules"])
    text = "\n\n".join(parts)
    if rag_enabled:
        text += blocks["kb_rules"]
    return text


def upsert_website_context(instructions: str, site_text: str) -> str:
    """Insert, replace or remove the website block in an EXISTING prompt.

    generate_instructions() builds a prompt from scratch, which would throw
    away a template's authored instructions. This keeps the prompt as it is
    and only touches the one block: it is replaced when its heading is
    found, inserted before the output rules when not, and removed when the
    site text is empty. The heading and body text come from the catalog so
    the wording stays in one place.
    """
    blocks = catalog.get("prompt_blocks")
    block_text = blocks["website_context_block"]
    heading = block_text.split("\n", 1)[0].strip()
    text = (instructions or "").replace("\r\n", "\n")

    # Drop an existing block: from its heading up to the next "# " heading
    # (or the end of the prompt), then close the gap it leaves.
    i = text.find(heading)
    if i >= 0:
        j = text.find("\n# ", i + len(heading))
        before = text[:i].rstrip("\n")
        after = text[j + 1 :] if j >= 0 else ""
        text = before + ("\n\n" + after if after else "")

    site = (site_text or "").strip()
    if not site:
        return text.rstrip("\n") + "\n"

    block = block_text.rstrip("\n") + "\n\n" + site
    rules_heading = blocks["voice_rules"].strip().split("\n", 1)[0].strip()
    k = text.find(rules_heading) if rules_heading else -1
    if k >= 0:
        return text[:k].rstrip("\n") + "\n\n" + block + "\n\n" + text[k:]
    return text.rstrip("\n") + "\n\n" + block + "\n"
