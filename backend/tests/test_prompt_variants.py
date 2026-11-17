"""The written variant of an agent's prompt, used on WhatsApp.

Every agent's saved instructions carry a copy of the shared voice rules,
whose first section is pure text-to-speech wording. The chat variant must
lose that section whether or not the owner has edited their copy: seen
live, an owner trimmed the email and URL lines out of it and the removal
silently stopped, so the rest of the speech rules reached WhatsApp again.
"""

import agent

VOICE = (
    "# Output rules\n\n- Say email addresses as words.\n- Omit https://.\n"
    "\n# Conversational flow\n\n- One question at a time.\n"
    "\n# Guardrails\n\n- Be honest.\n"
)
BLOCKS = {"voice_rules": VOICE}


def test_the_verbatim_section_is_removed():
    text = "You are Flux.\n\n" + VOICE
    out = agent._without_spoken_output_rules(text, BLOCKS)
    assert "# Output rules" not in out and "email addresses" not in out
    assert "# Conversational flow" in out and "# Guardrails" in out
    assert out.startswith("You are Flux.")


def test_an_edited_section_is_still_removed_by_its_headings():
    edited = VOICE.replace("- Say email addresses as words.\n- Omit https://.\n", "- Keep it short.\n")
    text = "You are Flux.\n\n" + edited
    out = agent._without_spoken_output_rules(text, BLOCKS)
    assert "# Output rules" not in out and "Keep it short" not in out
    assert "# Conversational flow" in out and "One question at a time" in out
    assert "# Guardrails" in out


def test_an_always_group_goes_under_the_next_bubble():
    # The order desk's lists are pinned to whatever the model says next,
    # since the reply is told to say "four orders" and not name them.
    pinned = [{"kind": "orders", "always": True,
               "items": [{"label": "Whirlpool top load"}]}]
    loose = [{"kind": "orders", "items": [{"label": "Whirlpool top load"}]}]
    assert agent._names_an_option("Hi Usama! You have four orders.", pinned)
    assert not agent._names_an_option("Hi Usama! You have four orders.", loose)
    assert agent._names_an_option("The Whirlpool top load washer is here.", loose)


def test_excerpts_ride_inside_the_user_turn_and_are_cut_for_the_record():
    # The RAG step appends the knowledge excerpts to the person's own
    # message. The model sees one user turn, question then material; the
    # record keeps only the question. Both earlier shapes (a separate
    # assistant note, a separate user note) were misread by Gemini live.
    from livekit.agents import llm as lk_llm

    import catalog

    preamble = catalog.get("prompt_blocks")["kb_injection_preamble"]
    msg = lk_llm.ChatMessage(role="user", content=["Panasonic countertop"])
    msg.content.append(preamble + " ...\n[source: policy.md]\nReturns take 30 days.")
    seen = msg.text_content
    assert seen.startswith("Panasonic countertop") and "Returns take 30 days" in seen
    assert agent._without_excerpts(seen) == "Panasonic countertop"
    # A turn without excerpts is untouched, and an empty one stays empty.
    assert agent._without_excerpts("Where is my washer?") == "Where is my washer?"
    assert agent._without_excerpts("") == ""


def test_without_the_headings_nothing_is_touched():
    text = "You are Flux.\n\n- Keep it short.\n\n# Guardrails\n\n- Be honest.\n"
    assert agent._without_spoken_output_rules(text, BLOCKS) == text
    assert agent._without_spoken_output_rules(text, {}) == text
