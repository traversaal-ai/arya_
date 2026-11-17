"""Provider dispatch: one function that turns the active agent config into
a runnable AgentSession.

Cascaded pipelines only (STT → LLM → TTS as separate stages):

  openai — openai.STT → openai.LLM → openai.TTS. One key covers all three.
  groq   — open models, hosted free tier, one GROQ_API_KEY covers all
           three: Groq Whisper STT + Groq GPT-OSS LLM + Groq Orpheus TTS.
           The best quality/speed of the open-model options.
  gemini — one GOOGLE_API_KEY covers all three: beta.GeminiSTT (Live API,
           streaming) + google.LLM + beta.GeminiTTS. Models live in the
           config's own `gemini` group, never the shared stt/llm/tts
           blocks, so switching providers can't hand it a gpt-* id.
           NOT google.STT: that class is Google Cloud STT (Chirp), which
           takes service-account credentials and no API key, and would
           break the one-key promise.
  local  — fully open source, no API key, fully private. Two selectable
           backends (config's `local.backend`), NOT symmetric:
             ollama — LLM only (`local.ollama.llm_url`/`llm_model`); no
                      usable STT/TTS route exists on Ollama at all, so
                      those two stages always use the bundled Speaches
                      container (Whisper STT + Kokoro TTS, both served
                      from the same OpenAI-compatible /v1).
             vllm   — all three stages independently overridable
                      (`local.vllm.{stt,llm,tts}_{url,model}`); vLLM
                      serves ONE model per process, so a full cascade is
                      typically two+ separate `vllm serve` invocations.
                      Core vLLM has no TTS route at all — its TTS stage
                      falls back to the bundled Speaches endpoint too,
                      unless explicitly pointed elsewhere.
           Needs ~8 GB free RAM for the bundled containers.

All use Silero VAD + the SDK's local turn detector. STT is streaming on
openai (realtime transcription websocket, interim results,
OPENAI_STT_REALTIME=0 to fall back) and on gemini (Live API), and batch on
groq/local, whose endpoints only offer whole-utterance transcription. Speech-to-speech
models are deliberately NOT wired up: cascaded keeps every stage swappable
and observable (per-stage metrics, RAG injection between STT and LLM). To
add a provider: add a branch here and a template `provider` value;
nothing else in the worker changes.
"""

import inspect
import logging
import os

import httpx
from livekit.agents import (
    AgentSession,
    APIConnectOptions,
    TurnHandlingOptions,
    inference,
)
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import google, groq, openai, silero
from openai import AsyncClient as _RawOpenAIAsyncClient

import catalog
import config

logger = logging.getLogger("providers")

# Model defaults live in catalog.py (DB-backed, seeded; e.g. local:
# faster-whisper-base + qwen3.5:2b + Kokoro for CPU-only ~8 GB machines,
# groq: hosted open models behind one key). Read fresh per session build
# so a catalog PUT applies to the next call without a worker restart.

# The LLM temperature every provider branch falls back to when the agent
# config doesn't set llm.temperature — one definition, three consumers.
DEFAULT_TEMPERATURE = 0.3

# Output-token ceiling (llm.max_response_tokens overrides; 0 disables).
# This is a RUNAWAY GUARD, not the brevity mechanism: the prompt word budget
# (prompt_blocks.voice_rules) is what keeps answers to 5 to 10 seconds of
# speech, and the cap only stops a model that ignores it from monologuing.
#
# It was 150, which is ample for SPEECH and far too small for a tool call,
# because arguments are output tokens too. Seen live twice: the travel agent
# fetched a written itinerary, went to email it through Composio, and had to
# emit the whole itinerary as the body argument. The JSON was cut off
# mid-string, the SDK got an unparseable tool call, and the caller heard
# nothing at all: no error, no reply, the agent simply went back to
# listening. Any tool taking a paragraph hits this (plan_days sending a whole
# day-by-day plan is the other one). 1000 still stops a runaway.
DEFAULT_MAX_RESPONSE_TOKENS = 1000
# Reasoning-capable models (OpenAI GPT-5.x, Groq GPT-OSS) count hidden
# reasoning tokens against the same cap — a tight cap starves reasoning
# and yields EMPTY replies, so their ceiling is floored generously.
REASONING_MIN_CAP = 400
GROQ_REASONING_MIN_CAP = 500

# The hosted /v1 roots. The plugins know these themselves; they are named
# here for speech_endpoint(), whose callers talk to the REST routes
# directly and would otherwise have to hardcode them a second time.
OPENAI_BASE_URL = "https://api.openai.com/v1"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def _extra_llm_kwargs(target, wanted: dict) -> dict:
    """Keep only the kwargs `target`'s signature actually accepts.

    The token-cap kwarg is version- and path-dependent (max_completion_tokens
    on newer plugin/OpenAI, max_tokens elsewhere; with_ollama has its own
    signature), and `verbosity` only exists on recent plugin releases.
    Feature-detecting keeps one code path working across plugin upgrades —
    a missing kwarg degrades to "prompt-only brevity" instead of a
    TypeError that kills the call. `cap` in `wanted` is resolved to
    whichever cap parameter exists, first match wins."""
    try:
        params = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return {}
    out = {}
    cap = wanted.pop("cap", None)
    if cap:
        # max_output_tokens is the Gemini plugin's spelling; without it the
        # cap would silently degrade to prompt-only on that provider.
        for name in ("max_completion_tokens", "max_tokens", "max_output_tokens"):
            if name in params:
                out[name] = int(cap)
                break
        else:
            logger.info("LLM %s accepts no token-cap kwarg — prompt-only", target)
    for name, value in wanted.items():
        if name in params:
            out[name] = value
    return out


def _response_cap(llm_cfg: dict, *, floor: int = 0) -> int:
    """The configured output cap, floored for reasoning models; 0 = off."""
    cap = llm_cfg.get("max_response_tokens", DEFAULT_MAX_RESPONSE_TOKENS)
    if not cap:
        return 0
    return max(int(cap), floor)

# Local inference under memory/CPU pressure regularly exceeds the SDK's
# 10s default per-request timeout — the plugins then abort work that was
# genuinely progressing (seen live: whisper 18s on a 7s clip, everything
# retried into failure). Patience over retries for the local path. The
# timeouts are hardware-dependent by nature — env-overridable.
LOCAL_CONN_OPTIONS = SessionConnectOptions(
    stt_conn_options=APIConnectOptions(
        timeout=float(os.environ.get("LOCAL_STT_TIMEOUT_S", "60")), max_retry=1
    ),
    llm_conn_options=APIConnectOptions(
        timeout=float(os.environ.get("LOCAL_LLM_TIMEOUT_S", "90")), max_retry=1
    ),
    tts_conn_options=APIConnectOptions(
        timeout=float(os.environ.get("LOCAL_TTS_TIMEOUT_S", "60")), max_retry=1
    ),
)


class _LocalTTS(openai.TTS):
    """Patience fix for the local TTS path against a self-hosted
    OpenAI-compatible server (Speaches/Kokoro, vllm-omni, a custom vLLM TTS
    endpoint).

    The plugin's default httpx client used to cap reads at 5s — far tighter
    than LOCAL_CONN_OPTIONS' 60s TTS patience. A cold/CPU-bound Speaches
    instance (no CUDA lib in that container, falls back to CPU) commonly
    takes 4-8s+ per request, which tripped the plugin's inner timeout before
    the outer retry wrapper ever got a chance ("Request timed out." even
    though Speaches eventually returned valid audio). We build our own
    client with matching patience instead.

    HISTORY (plugin 1.8): this class also used to override synthesize() to
    force the binary decoder, because openai.TTS picked its decoder from a
    hardcoded model allowlist (AUDIO_STREAM_MODELS) and assumed anything
    else spoke OpenAI's SSE protocol — which no local backend does, so the
    emitter silently got zero audio frames. The plugin now decides from the
    response's Content-Type instead, which is correct for these servers, so
    the override is gone. Note the plugin also passes its own
    `timeout=httpx.Timeout(30, connect=...)` per request now, so the read
    ceiling on this path is effectively 30s rather than the 60s below; raise
    LOCAL_TTS_TIMEOUT_S alone and it will not lift that inner limit.
    """

    def __init__(
        self, *, model: str, voice: str, base_url: str, api_key: str, response_format: str
    ) -> None:
        client = _RawOpenAIAsyncClient(
            max_retries=0,
            api_key=api_key,
            base_url=base_url,
            http_client=httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0),
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=50, max_keepalive_connections=50, keepalive_expiry=120
                ),
            ),
        )
        super().__init__(
            model=model, voice=voice, response_format=response_format, client=client
        )
        # openai.TTS.__init__ sets _owns_client = (client is None) -- False
        # here since we passed our own, which would skip closing it on
        # aclose(). We built it, so we own its cleanup too.
        self._owns_client = True


# LiveKit defaults to 3 consecutive tool calls per turn, and Composio's tool
# router spends exactly that on one action: SEARCH_TOOLS, GET_TOOL_SCHEMAS,
# MULTI_EXECUTE_TOOL. At the default a turn that needed a retry, or a second
# lookup before answering, hit the cap and the SDK generated its final reply
# with tool_choice="none". The model was left holding fetched data it could
# no longer act on, so it asked the caller a pointless clarifying question.
# Seen live on a calendar read. Eight leaves room for a retry and a follow-up
# call; the turn still ends on its own well inside the inactivity watchdog.
MAX_TOOL_STEPS = 8


def _turn_handling(cfg: dict) -> TurnHandlingOptions:
    td_cfg = cfg.get("turn_detection", {})
    td_kwargs = {"version": td_cfg.get("version", "v1-mini")}
    if td_cfg.get("unlikely_threshold") is not None:
        td_kwargs["unlikely_threshold"] = td_cfg["unlikely_threshold"]
    # v1-mini runs locally on CPU, free anywhere; pinning it skips the
    # cloud v1 attempt that 401s on self-hosted servers.
    return TurnHandlingOptions(
        turn_detection=inference.TurnDetector(**td_kwargs),
        # A single word spoken while the agent is busy ("sure", "okay",
        # "yes") is a backchannel, not an interruption. Seen live: "Sure"
        # during a 15 s flight search cancelled the tool turn, the result
        # never reached the model, and it invented three flights, then
        # booked a real one the caller had never heard. Two words is the
        # smallest change that stops that; "stop" still needs "please stop".
        interruption={"min_words": 2},
    )


def _stt_prompt(stt_cfg: dict) -> str | None:
    # STT vocabulary biasing: domain entity names fed as the transcription
    # prompt so proper nouns survive transcription — nothing downstream can
    # recover an entity the STT never wrote.
    if stt_cfg.get("vocabulary"):
        return "Domain vocabulary: " + ", ".join(stt_cfg["vocabulary"]) + "."
    return None


def build_judge_llm(cfg: dict, keys: dict, *, fallback: bool = False):
    """A fresh LLM instance for the post-call eval judges, or None when no
    usable judge provider is configured.

    Deliberately NOT the session's LLM object: that one carries the voice
    output cap (~150 tokens), which would truncate a judge's verdict
    tool-call mid-JSON.

    cfg.evals.judge_provider: "agent" (default) follows the agent's own
    provider and key; "openai"/"groq" override it, so a local-provider
    agent can still be judged by a cloud model.

    The MODEL is pinned per provider, never inherited from the agent.
    Verdicts are extracted via forced tool calls (tool_choice="required"),
    which reasoning-family models fail at: a gpt-5.6 judge spent its
    budget reasoning and never emitted the verdict, so a whole call was
    scored off one surviving check. Groq pins llama (not GPT-OSS) for the
    same reason: GPT-OSS reasons in-band. Env-overridable for experiments.

    Local never judges: 3B-class local models fail forced tool calls too
    often to be worth the misleading scores. config.validate() surfaces
    this before the toggle can be saved without a cloud judge.
    """
    evals_cfg = cfg.get("evals", {}) or {}
    choice = evals_cfg.get("judge_provider", "agent")
    provider = cfg.get("provider", "openai") if choice == "agent" else choice
    # fallback=True returns the second-chance model for the same provider.
    # The worker re-runs only the checks the primary failed on it. Each
    # pair is chosen for how the primary tends to fail on that provider:
    # OpenAI project keys block specific models (4o-mini vs 4.1-mini goes
    # either way per org), and on Groq the call itself drains the primary
    # model's per-model TPM budget, so the fallback's job is to be a
    # DIFFERENT model with its own fresh bucket.
    if provider == "groq":
        if not keys.get("groq"):
            return None
        # GPT-OSS over llama: llama-3.3-70b-versatile was retired by Groq
        # (observed live: every judge 404ed model_not_found), while GPT-OSS
        # is the catalog's own Groq default so any working Groq key has it.
        # Its in-band reasoning is a tool-call risk in principle; watch the
        # Judge-failed log lines if verdicts start dropping.
        # model = os.environ.get("EVALS_JUDGE_MODEL_GROQ", "llama-3.3-70b-versatile")
        model = (
            os.environ.get("EVALS_JUDGE_FALLBACK_GROQ", "openai/gpt-oss-20b")
            if fallback
            else os.environ.get("EVALS_JUDGE_MODEL_GROQ", "openai/gpt-oss-120b")
        )
        return groq.LLM(model=model, api_key=keys["groq"])
    if provider == "openai":
        if not keys.get("openai"):
            return None
        # 4.1-mini over 4o-mini: equally cheap, stronger tool-caller, and
        # observed project keys that block 4o-mini while allowing 4.1-mini.
        # 4o-mini stays as the fallback for orgs where it is the reverse.
        model = (
            os.environ.get("EVALS_JUDGE_FALLBACK_OPENAI", "gpt-4o-mini")
            if fallback
            else os.environ.get("EVALS_JUDGE_MODEL_OPENAI", "gpt-4.1-mini")
        )
        return openai.LLM(model=model, api_key=keys["openai"])
    if provider == "gemini":
        if not keys.get("gemini"):
            return None
        # Flash Lite over the voice model: judging is a forced tool call, not
        # a conversation, so the cheapest Gemini 3 is enough. thinking_level
        # is left at the model default here: unlike the voice path, a judge
        # has no latency budget to protect and reasoning helps the verdict.
        model = (
            os.environ.get("EVALS_JUDGE_FALLBACK_GEMINI", "gemini-3.5-flash")
            if fallback
            else os.environ.get("EVALS_JUDGE_MODEL_GEMINI", "gemini-3.5-flash-lite")
        )
        return google.LLM(model=model, api_key=keys["gemini"])
    return None


def _build_local_session(
    cfg: dict, stt_cfg: dict, llm_cfg: dict, prompt: str | None, has_tools: bool,
    *, text_only: bool = False,
) -> AgentSession:
    lcl_cfg = cfg.get("local", {}) or {}
    local_defaults = catalog.get("local_defaults")
    speaches_tts_model = local_defaults["speaches_tts_model"]
    # `|` merge: a pre-existing saved agent's flat {stt_model, llm_model,
    # voice} (no `backend`/`ollama`/`vllm` keys) still resolves correctly —
    # every `.get("llm_model")` below falls through to this merged dict.
    lcl = local_defaults | lcl_cfg
    backend = lcl_cfg.get("backend", "ollama")

    if backend == "vllm":
        v = lcl_cfg.get("vllm", {}) or {}
        stt_base = config._normalize_base_url(
            v.get("stt_url"), config.speaches_url() + "/v1"
        )
        stt_model = v.get("stt_model") or lcl["stt_model"]
        tts_base = config._normalize_base_url(
            v.get("tts_url"), config.speaches_url() + "/v1"
        )
        tts_model = v.get("tts_model") or speaches_tts_model
        # No bundled fallback for the LLM — we ship no vLLM server, so a
        # blank endpoint/model here is a configuration error, not a default
        # to fall back to. config.validate() surfaces this before Test;
        # this is the worker-side backstop if that check is ever bypassed.
        llm_url_raw = (v.get("llm_url") or "").strip()
        llm_model = (v.get("llm_model") or "").strip()
        if not llm_url_raw or not llm_model:
            raise ValueError(
                "local.vllm needs both llm_url and llm_model — vLLM has no"
                " bundled server (see agents.md → Swap the OpenAI models)"
            )
        llm_base = config._normalize_base_url(llm_url_raw)
        llm = openai.LLM(
            model=llm_model,
            base_url=llm_base,
            api_key="not-needed",
            temperature=llm_cfg.get("temperature", DEFAULT_TEMPERATURE),
            **({"parallel_tool_calls": False} if has_tools else {}),
            **_extra_llm_kwargs(openai.LLM, {"cap": _response_cap(llm_cfg)}),
        )
    else:
        # ollama: STT/TTS are not configurable — no usable route on Ollama
        # for either — so they stay pinned to the bundled Speaches endpoint.
        stt_base = config.speaches_url() + "/v1"
        stt_model = lcl["stt_model"]
        tts_base = config.speaches_url() + "/v1"
        tts_model = speaches_tts_model
        o = lcl_cfg.get("ollama", {}) or {}
        llm_base = config._normalize_base_url(
            o.get("llm_url"), config.ollama_url() + "/v1"
        )
        llm_model = o.get("llm_model") or lcl["llm_model"]
        llm = openai.LLM.with_ollama(
            model=llm_model,
            base_url=llm_base,
            temperature=llm_cfg.get("temperature", DEFAULT_TEMPERATURE),
            # Small local models ignore "be brief" the most — the cap
            # matters most here (maps to num_predict on Ollama's /v1).
            **_extra_llm_kwargs(
                openai.LLM.with_ollama, {"cap": _response_cap(llm_cfg)}
            ),
        )

    stt_kwargs = {
        "model": stt_model,
        "language": stt_cfg.get("language", "en"),
        "base_url": stt_base,
        "api_key": "not-needed",
    }
    if prompt:
        stt_kwargs["prompt"] = prompt

    return _session(
        text_only,
        stt=openai.STT(**stt_kwargs),
        llm=llm,
        tts=_LocalTTS(
            model=tts_model,
            voice=lcl["voice"],
            base_url=tts_base,
            api_key="not-needed",
            response_format="wav",
        ),
        vad=silero.VAD.load(),
        turn_handling=_turn_handling(cfg),
        max_tool_steps=MAX_TOOL_STEPS,
        conn_options=LOCAL_CONN_OPTIONS,
    )


def gemini_style(options: dict) -> str:
    """The agent's expressive settings as a short bracketed tag for Gemini
    TTS, placed in front of the text.

    On a call, expressive mode has the model mark its reply up and the TTS
    act on the marks. A WhatsApp reply is plain text, so the same settings
    become a delivery tag. Measured on gemini-3.8-flash-tts: a direction
    written as a sentence ("Say this warmly: ...") is READ ALOUD, adding
    two to fifteen seconds of narration before the reply, and the model
    takes no system instruction at all. A short bracketed tag is the one
    form that is not spoken (same clip length as the bare text), so that
    is what goes out. The operator's free-text `tts_instructions_append`
    is deliberately not included: a sentence in the prompt gets read.
    """
    steer = options.get("speech_steering") or {}
    words = ["warm", "professional"]
    pace = str(steer.get("pace") or "").strip().lower()
    if pace and pace != "normal":
        words.append(f"{pace} pace")
    sounds = steer.get("nonverbal_sounds") or {}
    if sounds.get("breathing"):
        words.append("natural breaths")
        words.append("short pauses")
    if not steer.get("disfluencies", False):
        words.append("no fillers")
    if not sounds.get("laughing", False):
        words.append("no laughter")
    return "[" + ", ".join(words) + "]"


def speech_endpoint(cfg: dict, keys: dict) -> dict:
    """Where to POST audio for this agent, and with which models.

    The same resolution build_session() performs when it constructs the STT
    and TTS plugins, exposed as plain values so a caller outside the voice
    pipeline can reach the operator's chosen provider without starting a
    LiveKit session: the WhatsApp bridge transcribing a customer's voice
    note, or synthesising a spoken reply. All three providers speak the
    OpenAI /audio routes, which is what makes one helper possible.

    STT and TTS carry separate base URLs because a vLLM setup can point
    them at different servers; for openai and groq they are the same.
    Keep in step with the branches below.

    A gemini agent gets a Gemini endpoint: Google's speech routes are not
    OpenAI-shaped, so the bridge calls them through the google-genai SDK
    (media._gemini_*) rather than over /audio. `style` carries the agent's
    expressive settings as one spoken-direction sentence, since a WhatsApp
    reply is plain text and never passes through the call pipeline's
    expressive markup.
    """
    provider = cfg.get("provider", "openai")

    if provider == "gemini":
        g = catalog.get("gemini_defaults") | (cfg.get("gemini", {}) or {})
        return {
            "provider": "gemini",
            "base_url": "",
            "tts_base_url": "",
            "api_key": keys.get("gemini", ""),
            "stt_model": g.get("transcribe_model") or "gemini-3.5-flash",
            "tts_model": g["tts_model"],
            "voice": g["voice"],
            "tts_format": "wav",
            "style": gemini_style(g.get("expressive_options") or {})
            if g.get("expressive") else "",
        }

    if provider == "local":
        lcl_cfg = cfg.get("local", {}) or {}
        defaults = catalog.get("local_defaults")
        lcl = defaults | lcl_cfg
        speaches = config.speaches_url() + "/v1"
        if lcl_cfg.get("backend") == "vllm":
            v = lcl_cfg.get("vllm", {}) or {}
            stt_base = config._normalize_base_url(v.get("stt_url"), speaches)
            tts_base = config._normalize_base_url(v.get("tts_url"), speaches)
            stt_model = v.get("stt_model") or lcl["stt_model"]
            tts_model = v.get("tts_model") or defaults["speaches_tts_model"]
        else:
            stt_base = tts_base = speaches
            stt_model = lcl["stt_model"]
            tts_model = defaults["speaches_tts_model"]
        return {
            "provider": provider,
            "base_url": stt_base,
            "tts_base_url": tts_base,
            "api_key": "not-needed",
            "stt_model": stt_model,
            "tts_model": tts_model,
            "voice": lcl["voice"],
            "tts_format": "wav",
        }

    if provider == "groq":
        g = catalog.get("groq_defaults") | (cfg.get("groq", {}) or {})
        return {
            "provider": provider,
            "base_url": GROQ_BASE_URL,
            "tts_base_url": GROQ_BASE_URL,
            "api_key": keys.get("groq", ""),
            "stt_model": g["stt_model"],
            "tts_model": g["tts_model"],
            "voice": g["voice"],
            "tts_format": "wav",
        }

    o = catalog.get("openai_defaults")
    stt_cfg = cfg.get("stt", {}) or {}
    tts_cfg = cfg.get("tts", {}) or {}
    return {
        "provider": "openai",
        "base_url": OPENAI_BASE_URL,
        "tts_base_url": OPENAI_BASE_URL,
        "api_key": keys.get("openai", ""),
        "stt_model": stt_cfg.get("model", o["stt_model"]),
        "tts_model": tts_cfg.get("model", o["tts_model"]),
        "voice": tts_cfg.get("voice", o["voice"]),
        "tts_format": "mp3",
    }


# Thinking levels Google refuses per model family. The catalog pins the
# right level per model, but a stored catalog row written by an older
# build keeps the old pin. Seen live on an engine: 3.8-flash with
# `thinking_budget: 0`, which the plugin discards before defaulting the
# level to "minimal", which Google 400s on that model; every turn failed
# and the customer got "still working" and then nothing. This is the
# floor the code enforces whatever the row says: a refused OR absent
# level on these models becomes the cheapest accepted one.
_GEMINI_REFUSED_LEVELS = {"gemini-3.8": ({"", "minimal"}, "low")}


def gemini_thinking(model: str, thinking_config: dict) -> dict:
    """The thinking_config Google will accept for this model."""
    out = dict(thinking_config)
    level = str(out.get("thinking_level") or "").lower()
    for prefix, (refused, instead) in _GEMINI_REFUSED_LEVELS.items():
        if model.startswith(prefix) and level in refused:
            out["thinking_level"] = instead
            # The plugin discards this on Gemini 3 anyway, with a warning
            # per turn; a level is the knob that counts.
            out.pop("thinking_budget", None)
    return out


def _session(text_only: bool, **kw) -> AgentSession:
    """The one place an AgentSession is made.

    A text-only session (a WhatsApp thread: text in, text out, no audio in
    either direction) gets the language model and nothing else. The speech
    stages are not merely idle without audio: Gemini's streaming STT opens
    a live connection the moment the session starts, Google aborts it after
    sixty silent seconds, and the plugin reconnects every minute for the
    life of the thread, rebuilding an SSL context on the loop each time
    (seen live on the engine: a day of "Gemini STT request failed" with a
    blocked event loop around each). OpenAI's stream happens to idle
    quietly, which is the only reason this went unnoticed. Voice notes on
    the channel are transcribed and spoken by the bridge itself, so the
    worker loses nothing here.
    """
    if text_only:
        for k in ("stt", "tts", "vad", "turn_handling", "expressive"):
            kw.pop(k, None)
    return AgentSession(**kw)


def build_session(cfg: dict, keys: dict, *, has_tools: bool,
                  text_only: bool = False) -> AgentSession:
    provider = cfg.get("provider", "openai")
    stt_cfg = cfg.get("stt", {})
    llm_cfg = cfg.get("llm", {})
    prompt = _stt_prompt(stt_cfg)

    if provider == "local":
        return _build_local_session(
            cfg, stt_cfg, llm_cfg, prompt, has_tools, text_only=text_only)

    if provider == "groq":
        g = catalog.get("groq_defaults") | cfg.get("groq", {})
        api_key = keys["groq"]
        stt_kwargs = {
            "model": g["stt_model"],
            "language": stt_cfg.get("language", "en"),
            "api_key": api_key,
        }
        if prompt:
            stt_kwargs["prompt"] = prompt
        llm_kwargs = {
            "model": g["llm_model"],
            "api_key": api_key,
            "temperature": llm_cfg.get("temperature", DEFAULT_TEMPERATURE),
            # GPT-OSS reasons in-band (harmony) — the cap covers reasoning
            # tokens too, hence the generous floor.
            **_extra_llm_kwargs(
                groq.LLM,
                {"cap": _response_cap(llm_cfg, floor=GROQ_REASONING_MIN_CAP)},
            ),
        }
        if has_tools:
            llm_kwargs["parallel_tool_calls"] = False
        return _session(
            text_only,
            stt=groq.STT(**stt_kwargs),
            llm=groq.LLM(**llm_kwargs),
            tts=groq.TTS(
                model=g["tts_model"],
                voice=g["voice"],
                api_key=api_key,
            ),
            vad=silero.VAD.load(),
            turn_handling=_turn_handling(cfg),
            max_tool_steps=MAX_TOOL_STEPS,
        )

    if provider == "gemini":
        g = catalog.get("gemini_defaults") | cfg.get("gemini", {})
        gm = catalog.get("gemini_models")
        api_key = keys["gemini"]

        # beta.GeminiSTT, NOT the top-level google.STT: they are different
        # backends behind similar names. google.STT is Google Cloud STT
        # (Chirp), which takes service-account credentials and no API key at
        # all; GeminiSTT talks to the Gemini Live API with the same
        # GOOGLE_API_KEY as the LLM and TTS, which is what makes this a
        # one-key provider. It is also genuinely streaming (interim results,
        # no post-utterance round trip), so this stream keeps the openai
        # provider's latency profile rather than the batch penalty that
        # groq/local pay.
        stt_kwargs = {
            "model": g["stt_model"],
            "api_key": api_key,
            "language": stt_cfg.get("language", "en"),
        }
        # Vocabulary biasing has a first-class parameter here, so the shared
        # `prompt` string (built for the OpenAI/Groq transcription-prompt
        # path) is deliberately not reused: the raw terms go in directly.
        if stt_cfg.get("vocabulary"):
            stt_kwargs["custom_vocabulary"] = list(stt_cfg["vocabulary"])

        # Models come from the `gemini` config group, never the shared
        # stt/llm/tts blocks: those carry whichever provider was configured
        # last, so a user switching from openai would otherwise hand Gemini a
        # gpt-* id. Same reason `local` keeps its own group. The generic LLM
        # knobs (temperature, max_response_tokens) stay shared.
        llm_model = g["llm_model"]
        llm_entry = next((m for m in gm["llm"] if m["id"] == llm_model), None)
        llm_kwargs = {"model": llm_model, "api_key": api_key}
        # `temperature: False` here means "Google deprecated this kwarg on
        # Gemini 3 and warns it degrades output" — NOT the reasoning marker it
        # is in openai_models. The cap floor below is decided separately.
        if llm_entry is None or llm_entry.get("temperature", True):
            llm_kwargs["temperature"] = llm_cfg.get("temperature", DEFAULT_TEMPERATURE)
        # Gemini 3 reasons by default; the catalog entry pins it off with a
        # model-specific thinking_config (3.8-flash and 3.5-flash-lite reject
        # each other's knob, so this cannot be one shared value). A model
        # outside the catalog keeps its own default, which reasons, so the
        # cap stays floored like the other reasoning-capable providers.
        # gemini_thinking() runs even with NO pin: on a 3.8 model the plugin
        # would otherwise fill in "minimal" itself, which Google refuses.
        wanted = {"cap": _response_cap(llm_cfg, floor=REASONING_MIN_CAP)}
        thinking = gemini_thinking(
            llm_model, (llm_entry or {}).get("thinking_config") or {}
        )
        if thinking:
            wanted["thinking_config"] = thinking
        if has_tools:
            wanted["parallel_tool_calls"] = False
        llm_kwargs |= _extra_llm_kwargs(google.LLM, wanted)

        # Expressive delivery is opt-in per agent (gemini.expressive) with the
        # steering dict from the catalog. Only the 3.8 TTS family declares the
        # markup dialect; on any other TTS model the SDK silently leaves the
        # mode off, so a stale flag cannot make the agent read tags aloud.
        expressive = dict(g.get("expressive_options") or {}) if g.get("expressive") else False

        return _session(
            text_only,
            stt=google.beta.GeminiSTT(**stt_kwargs),
            llm=google.LLM(**llm_kwargs),
            # GeminiTTS is still under `beta` in the plugin: keep the only
            # reference to it here so a future move is a one-line change.
            tts=google.beta.GeminiTTS(
                model=g["tts_model"],
                voice_name=g["voice"],
                api_key=api_key,
            ),
            vad=silero.VAD.load(),
            turn_handling=_turn_handling(cfg),
            max_tool_steps=MAX_TOOL_STEPS,
            expressive=expressive,
        )

    if provider != "openai":
        supported = "', '".join(catalog.SUPPORTED_PROVIDERS)
        raise ValueError(
            f"unsupported provider '{provider}' — use '{supported}'"
            " (see agents.md → How to extend → Add a provider)"
        )

    tts_cfg = cfg.get("tts", {})
    api_key = keys["openai"]
    o = catalog.get("openai_defaults")

    # Streaming STT over OpenAI's realtime transcription websocket: audio is
    # transcribed WHILE the caller speaks, so the transcript is ready when
    # they stop instead of costing a round trip after (measured: batch mode
    # put an average 2.0s of transcription_delay on the critical path of
    # every turn across 373 real turns). Also yields interim results, which
    # the transcript UI already renders. OpenAI only: Groq and Speaches have
    # no realtime endpoint and stay batch. Kill switch for cost or quality
    # comparisons: OPENAI_STT_REALTIME=0 (global) or stt.realtime=false
    # (per agent). Realtime bills the whole audio stream, silence included,
    # where batch only uploads detected speech.
    realtime_default = os.environ.get("OPENAI_STT_REALTIME", "1") != "0"
    stt_kwargs = {
        "model": stt_cfg.get("model", o["stt_model"]),
        "language": stt_cfg.get("language", "en"),
        "api_key": api_key,
        "use_realtime": bool(stt_cfg.get("realtime", realtime_default)),
    }
    # The vocabulary hint is batch-only. On the realtime websocket every
    # pause is transcribed too, and on near-silent audio the model echoes
    # the hint back as if the caller had said it (seen live: "Domain
    # vocabulary: policy, premium, ..." committed as a user turn three times
    # in one call, and the agent answered each one). Batch mode only sends
    # detected speech, where the hint earns its keep. agent.py's stt_node
    # additionally scrubs any echo that slips through on either path.
    if prompt and not stt_kwargs["use_realtime"]:
        stt_kwargs["prompt"] = prompt

    llm_model = llm_cfg.get("model", o["llm_model"])
    # Look up per-model quirks in the curated catalog; a hand-edited config
    # with a model outside the catalog falls back to always sending
    # temperature (the plugin/API default for ordinary chat models).
    llm_entry = next(
        (m for m in catalog.get("openai_models")["llm"] if m["id"] == llm_model),
        None,
    )
    llm_kwargs = {"model": llm_model, "api_key": api_key}
    if llm_entry is None or llm_entry.get("temperature", True):
        llm_kwargs["temperature"] = llm_cfg.get("temperature", DEFAULT_TEMPERATURE)
    if llm_entry and llm_entry.get("reasoning_effort"):
        # gpt-5.6-* isn't in the plugin's auto-pin list — without this every
        # turn pays full reasoning latency, which reads as a broken agent.
        llm_kwargs["reasoning_effort"] = llm_entry["reasoning_effort"]
    # Voice brevity: output cap (floored for reasoning-capable models —
    # `temperature: False` marks the GPT-5.x family, whose hidden reasoning
    # tokens count against the cap) + the GPT-5 `verbosity` knob when the
    # catalog entry carries one and the plugin supports it.
    reasoning_capable = llm_entry is not None and not llm_entry.get(
        "temperature", True
    )
    wanted = {
        "cap": _response_cap(
            llm_cfg, floor=REASONING_MIN_CAP if reasoning_capable else 0
        )
    }
    if llm_entry and llm_entry.get("verbosity"):
        wanted["verbosity"] = llm_entry["verbosity"]
    llm_kwargs |= _extra_llm_kwargs(openai.LLM, wanted)
    # OpenAI 400s if parallel_tool_calls is sent on a request with no tools.
    if has_tools:
        llm_kwargs["parallel_tool_calls"] = False

    return _session(
        text_only,
        stt=openai.STT(**stt_kwargs),
        llm=openai.LLM(**llm_kwargs),
        tts=openai.TTS(
            model=tts_cfg.get("model", o["tts_model"]),
            voice=tts_cfg.get("voice", o["voice"]),
            api_key=api_key,
        ),
        vad=silero.VAD.load(),
        turn_handling=_turn_handling(cfg),
        max_tool_steps=MAX_TOOL_STEPS,
    )
