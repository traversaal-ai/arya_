"""A WhatsApp thread runs a text-only session: the language model and no
speech stages. Seen live on the engine with Gemini: the streaming STT
opened a live connection at session start, Google cut it after sixty
silent seconds, and the plugin reconnected every minute for the life of
the thread. Also: a thread's idle wait must end at the idle deadline, not
a whole period after the last wait began."""

import providers
from whatsapp_bridge import remaining_idle


async def test_a_text_only_session_has_no_speech_stages():
    s = providers.build_session(
        {"provider": "openai"}, {"openai": "sk-test"}, has_tools=False, text_only=True
    )
    # The SDK still attaches its built-in VAD by default; with no audio
    # track it never runs, so only the network-bound stages matter here.
    assert s.stt is None and s.tts is None
    assert s.llm is not None


async def test_a_gemini_text_only_session_never_opens_the_live_stream():
    s = providers.build_session(
        {"provider": "gemini"}, {"gemini": "AIza-test"}, has_tools=True, text_only=True
    )
    assert s.stt is None and s.tts is None
    assert type(s.llm).__module__.startswith("livekit.plugins.google")


def test_a_thinking_level_google_refuses_is_replaced():
    # Seen live: an engine's stored catalog still pinned "minimal" on
    # 3.8-flash, Google 400ed every turn, and the customer got silence.
    assert providers.gemini_thinking(
        "gemini-3.8-flash", {"thinking_level": "minimal"}
    ) == {"thinking_level": "low"}
    assert providers.gemini_thinking(
        "gemini-3.8-flash", {"thinking_level": "MINIMAL"}
    ) == {"thinking_level": "low"}
    # The engine's actual stored row: an old budget pin and no level, which
    # the plugin turns into "minimal".
    assert providers.gemini_thinking(
        "gemini-3.8-flash", {"thinking_budget": 0}
    ) == {"thinking_level": "low"}
    # Accepted levels and other models pass through untouched.
    assert providers.gemini_thinking(
        "gemini-3.8-flash", {"thinking_level": "high"}
    ) == {"thinking_level": "high"}
    assert providers.gemini_thinking(
        "gemini-3.5-flash", {"thinking_level": "minimal"}
    ) == {"thinking_level": "minimal"}


def test_the_idle_wait_ends_at_the_deadline():
    # Last activity 9.5 minutes ago on a 10 minute setting: wait 30s more,
    # not another 10 minutes.
    assert remaining_idle(600, last_activity=1000.0, now=1570.0) == 30.0
    # Already past the deadline: a short wait, then the idle check runs.
    assert remaining_idle(600, last_activity=1000.0, now=1700.0) == 5.0
    # Fresh activity: the whole period.
    assert remaining_idle(600, last_activity=1000.0, now=1000.0) == 600.0
