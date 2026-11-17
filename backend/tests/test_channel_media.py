"""Media capability and speech routing for the channels.

Two questions this file settles, both of which decide what a WhatsApp
customer sees rather than anything internal: can this agent's model read a
photo at all, and where does a voice note go to be transcribed. Both are
resolved from the catalog, so both have to survive an operator editing it
and an older stored row that predates the keys.

The Graph calls that close the undocumented subscription trap are here
too: they have no user interface anywhere in Meta's console, so an
assertion on the exact request shape is the only check there is.
"""

import httpx
import pytest

import catalog
import config
import meta_wa
import providers

PHONE_ID = "106540352242922"
WABA = "102290129340398"


# ── vision capability ────────────────────────────────────────────────────────


def test_default_openai_agent_can_see_images():
    assert config.vision_capable({"provider": "openai"}) is True


def test_named_openai_vision_model_is_recognised():
    assert config.vision_capable(
        {"provider": "openai", "llm": {"model": "gpt-4.1-mini"}}
    ) is True


def test_unknown_openai_model_is_assumed_text_only():
    # A hand-edited config naming a model we know nothing about must
    # decline photos rather than send an image the API will reject.
    assert config.vision_capable(
        {"provider": "openai", "llm": {"model": "gpt-9-imaginary"}}
    ) is False


def test_groq_is_text_only():
    assert config.vision_capable({"provider": "groq"}) is False


def test_local_vision_model_matches_on_family_prefix():
    cfg = {"provider": "local",
           "local": {"backend": "ollama", "ollama": {"llm_model": "qwen3-vl:8b"}}}
    assert config.vision_capable(cfg) is True


def test_local_text_model_does_not_match():
    cfg = {"provider": "local",
           "local": {"backend": "ollama", "ollama": {"llm_model": "qwen3.5:2b"}}}
    assert config.vision_capable(cfg) is False


def test_local_vllm_reads_its_own_model():
    cfg = {"provider": "local",
           "local": {"backend": "vllm", "vllm": {"llm_model": "Qwen3-VL-8B"}}}
    assert config.vision_capable(cfg) is True


def test_catalog_edit_can_switch_vision_off(monkeypatch):
    # The flag is data, not code: an operator who finds a model rejecting
    # images can turn it off without a redeploy, and an explicit false
    # must beat the shipped seed that says true.
    edited = {
        **catalog.SEEDS["openai_models"],
        "llm": [{"id": "gpt-4o-mini", "vision": False}],
    }
    monkeypatch.setattr(
        catalog, "get",
        lambda name: edited if name == "openai_models" else catalog.SEEDS[name],
    )
    assert config.vision_capable(
        {"provider": "openai", "llm": {"model": "gpt-4o-mini"}}
    ) is False


def test_stored_catalog_predating_the_flag_falls_back_to_the_seed(monkeypatch):
    # The live row was written before `vision` existed. It carries no
    # opinion, so the seed answers instead of everything being declined.
    old = {**catalog.SEEDS["openai_models"],
           "llm": [{"id": "gpt-4o-mini", "label": "GPT-4o mini"}]}
    monkeypatch.setattr(
        catalog, "get",
        lambda name: old if name == "openai_models" else catalog.SEEDS[name],
    )
    assert config.vision_capable({"provider": "openai"}) is True


def test_missing_seed_keys_are_backfilled_not_rejected():
    # A prompt_blocks row written before channel_text_rules existed would
    # otherwise fail validation and discard every edit in it.
    stored = {k: v for k, v in catalog.SEEDS["prompt_blocks"].items()
              if k != "channel_media_rules"}
    stored["voice_rules"] = "operator's own wording"
    merged = catalog._with_seed_defaults("prompt_blocks", stored)
    catalog._validate("prompt_blocks", merged)
    assert merged["voice_rules"] == "operator's own wording"
    assert merged["channel_media_rules"].strip()


def test_keys_added_inside_a_seed_entry_are_backfilled_too():
    # Seen live: a stored connectors row predated postgres `keeps_when`, so
    # the worker replaced the order desk and the card kept the old line
    # although the connection was marked shop_desk. Same for a key added to
    # one tool_catalog entry. The operator's own keys still win.
    stored = [
        {**e, "label": "My database"} if e["id"] == "postgres" else e
        for e in catalog.SEEDS["connectors"]
    ]
    for e in stored:
        e.pop("keeps_when", None)
    stored.append({"id": "mine", "label": "Mine", "auth": "none", "url": "http://x"})
    merged = catalog._with_seed_defaults("connectors", stored)
    pg = next(e for e in merged if e["id"] == "postgres")
    assert pg["keeps_when"] == {"orders": "shop_desk"}
    assert pg["label"] == "My database"
    assert merged[-1] == {"id": "mine", "label": "Mine", "auth": "none", "url": "http://x"}

    tools = {k: dict(v) for k, v in catalog.SEEDS["tool_catalog"].items()}
    tools["orders"].pop("live_rows")
    tools["orders"]["label"] = "Orders"
    merged = catalog._with_seed_defaults("tool_catalog", tools)
    assert merged["orders"]["live_rows"] and merged["orders"]["label"] == "Orders"


# ── speech endpoint ──────────────────────────────────────────────────────────


def test_openai_speech_endpoint_defaults():
    ep = providers.speech_endpoint({"provider": "openai"}, {"openai": "sk-x"})
    o = catalog.get("openai_defaults")
    assert ep["base_url"] == providers.OPENAI_BASE_URL
    assert ep["api_key"] == "sk-x"
    assert ep["stt_model"] == o["stt_model"]
    assert ep["tts_model"] == o["tts_model"]
    assert ep["voice"] == o["voice"]
    assert ep["tts_format"] == "mp3"


def test_openai_speech_endpoint_honours_the_agent_choices():
    ep = providers.speech_endpoint(
        {"provider": "openai", "stt": {"model": "whisper-1"},
         "tts": {"model": "tts-1-hd", "voice": "shimmer"}},
        {"openai": "sk-x"},
    )
    assert (ep["stt_model"], ep["tts_model"], ep["voice"]) == (
        "whisper-1", "tts-1-hd", "shimmer"
    )


def test_groq_speech_endpoint_uses_the_groq_root_and_wav():
    ep = providers.speech_endpoint({"provider": "groq"}, {"groq": "gsk-x"})
    g = catalog.get("groq_defaults")
    assert ep["base_url"] == providers.GROQ_BASE_URL
    assert ep["api_key"] == "gsk-x"
    assert ep["stt_model"] == g["stt_model"]
    assert ep["tts_format"] == "wav"


def test_local_ollama_speech_endpoint_points_at_speaches():
    ep = providers.speech_endpoint(
        {"provider": "local", "local": {"backend": "ollama"}}, {}
    )
    assert ep["base_url"] == config.speaches_url() + "/v1"
    assert ep["base_url"] == ep["tts_base_url"]
    assert ep["api_key"] == "not-needed"
    assert ep["tts_model"] == catalog.get("local_defaults")["speaches_tts_model"]


def test_local_vllm_speech_endpoint_can_split_stt_and_tts():
    ep = providers.speech_endpoint(
        {"provider": "local", "local": {"backend": "vllm", "vllm": {
            "stt_url": "http://stt.internal:9000",
            "tts_url": "http://tts.internal:9001",
            "stt_model": "faster-whisper-large",
            "tts_model": "Kokoro-Custom",
        }}},
        {},
    )
    assert ep["base_url"] == "http://stt.internal:9000/v1"
    assert ep["tts_base_url"] == "http://tts.internal:9001/v1"
    assert ep["stt_model"] == "faster-whisper-large"
    assert ep["tts_model"] == "Kokoro-Custom"


# ── the account subscription, which has no console anywhere ─────────────────


def _client(handler) -> meta_wa.MetaClient:
    return meta_wa.MetaClient(
        PHONE_ID, "TOKEN", api_version="v23.0",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_waba_id_is_derived_from_the_number():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "whatsapp_business_account": {"id": WABA}, "id": PHONE_ID,
        })

    assert await _client(handler).waba_id() == WABA
    assert f"/v23.0/{PHONE_ID}" in seen["url"]
    assert "fields=whatsapp_business_account" in seen["url"]


async def test_waba_id_is_empty_when_meta_omits_it():
    def handler(request):
        return httpx.Response(200, json={"id": PHONE_ID})

    assert await _client(handler).waba_id() == ""


async def test_subscribe_posts_with_no_app_id():
    # Meta subscribes whichever app owns the token. Sending an app id is
    # not just unnecessary, there is no parameter for it.
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"success": True})

    assert await _client(handler).subscribe(WABA) is True
    assert seen["method"] == "POST"
    assert seen["url"].endswith(f"/v23.0/{WABA}/subscribed_apps")
    assert not seen["body"]


async def test_subscribed_apps_returns_the_list():
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"whatsapp_business_api_data": {"id": "1234", "name": "Arya"}},
        ]})

    apps = await _client(handler).subscribed_apps(WABA)
    assert meta_wa.subscriber_ids(apps) == ["1234"]


def test_subscriber_ids_survives_an_empty_or_odd_response():
    assert meta_wa.subscriber_ids([]) == []
    assert meta_wa.subscriber_ids([{"id": "99"}]) == ["99"]
    assert meta_wa.subscriber_ids([{"nothing": True}, "junk"]) == []


async def test_app_id_reads_the_token_back():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": {
            "app_id": "778899", "type": "SYSTEM_USER", "expires_at": 0,
        }})

    assert await _client(handler).app_id() == "778899"
    assert "debug_token" in seen["url"]
    assert "input_token=TOKEN" in seen["url"]


async def test_subscribe_raises_meta_error_on_a_refusal():
    def handler(request):
        return httpx.Response(400, json={"error": {
            "message": "Unsupported post request", "code": 100,
        }})

    with pytest.raises(meta_wa.MetaError):
        await _client(handler).subscribe(WABA)


async def test_upload_media_sends_the_documented_multipart():
    # Not exercised before, and Phase D's spoken replies depend on it.
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["type"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={"id": "media-1"})

    got = await _client(handler).upload_media(b"OggS-fake", "audio/ogg",
                                              "reply.ogg")
    assert got == "media-1"
    assert seen["url"].endswith(f"/v23.0/{PHONE_ID}/media")
    assert seen["type"].startswith("multipart/form-data")
    body = seen["body"]
    assert b"OggS-fake" in body
    assert b'name="messaging_product"' in body and b"whatsapp" in body
    assert b"audio/ogg" in body
    assert b"reply.ogg" in body
