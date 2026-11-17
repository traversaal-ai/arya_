"""The document checks the post-call judge adds for answers that received
knowledge-base excerpts: the excerpt block it reads (agent._context_block)
and the roll-up of its per-answer verdicts to the call (agent._context_checks).
The judge call itself needs a model and is exercised live; what is tested
here is everything around it that decides what the dashboard shows.
"""

import agent


def _turn(f="pass", c="pass", used=True, why="ok"):
    return {
        "head": "x",
        "accuracy": {"verdict": "pass", "reasoning": ""},
        "relevancy": {"verdict": "pass", "reasoning": ""},
        "faithfulness": {"verdict": f, "reasoning": why},
        "context_relevance": {"verdict": c, "reasoning": ""},
        "used_context": used,
    }


def test_a_call_without_document_context_keeps_its_old_checks():
    plain = {
        "head": "x",
        "accuracy": {"verdict": "pass", "reasoning": ""},
        "relevancy": {"verdict": "pass", "reasoning": ""},
    }
    assert agent._context_checks([plain]) == {}
    assert agent._context_checks([]) == {}


def test_faithfulness_is_the_worst_answer_and_is_scored():
    out = agent._context_checks(
        [_turn("pass"), _turn("fail", why="The fee is not in the excerpts."), _turn("maybe")]
    )
    f = out["faithfulness"]
    assert f["value"] == 0.0 and f["verdict"] == "fail"
    assert f["reasoning"].startswith("Worst of 3 answers with document context.")
    assert "fee" in f["reasoning"]


def test_context_relevance_is_averaged_and_never_scored():
    out = agent._context_checks(
        [_turn(c="pass"), _turn(c="fail"), _turn(c="pass"), _turn(c="pass")]
    )
    c = out["context_relevance"]
    assert c["value"] is None
    assert c["verdict"] == "pass"  # three of four: 0.75 is the pass line
    assert "Average over 4 lookups: 75%." in c["reasoning"]
    assert "1 of them brought back excerpts that did not cover the question" in c["reasoning"]


def test_used_your_documents_becomes_the_judges_reading_and_quotes_the_overlap_count():
    out = agent._context_checks([_turn(used=True), _turn(used=False)], overlap=(2, 3))
    g = out["grounding"]
    assert g["value"] is None and g["verdict"] == "maybe"
    assert "1 of 2 answers that received document context drew on it" in g["reasoning"]
    assert "word overlap said 2 of 3" in g["reasoning"]
    # No overlap tally (older worker, or nothing injected): nothing to quote.
    alone = agent._context_checks([_turn(used=True)])["grounding"]
    assert alone["verdict"] == "pass" and "word overlap" not in alone["reasoning"]


def test_the_excerpt_block_names_each_source_and_stops_at_the_limit():
    ctx = {
        "chunks": [
            {"filename": "Fees.pdf", "text": "A" * 50},
            {"filename": "Hours.md", "text": "B" * 50},
            {"filename": "", "text": ""},
        ]
    }
    block = agent._context_block(ctx, limit=70)
    assert block.startswith("[source: Fees.pdf]\n" + "A" * 50)
    assert "---\n[source: Hours.md]\n" + "B" * 20 in block
    assert "B" * 21 not in block
    assert agent._context_block(None) == ""
    assert agent._context_block({"chunks": []}) == ""


def test_the_head_key_is_the_same_80_characters_the_transcript_cuts():
    assert agent._head("  Our   fees\nstart at five thousand.  ") == "Our fees start at five thousand."
    assert len(agent._head("word " * 40)) == 80
