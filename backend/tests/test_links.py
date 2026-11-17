"""links.resolve_links: the server-side verifier that turns a spoken or
typed mention into at most one real href, never the full result list.
See Plan-UIUX-Improvements_v3.md, Track B."""

import links


def _cit(title, link, snippet="", source=""):
    return {"type": "web", "title": title, "snippet": snippet,
            "source": source or link.split("://")[-1].split("/")[0], "link": link}


FASTAPI = _cit("FastAPI", "https://github.com/tiangolo/fastapi",
               "FastAPI is a modern, fast web framework for building APIs.")
DJANGO = _cit("The Django framework", "https://github.com/django/django",
              "Django makes it easier to build better web apps.")
DOCS = _cit("FastAPI documentation", "https://fastapi.tiangolo.com/tutorial/")
NO_LINK = {"type": "web", "title": "", "snippet": "some answer text", "source": "tavily", "link": ""}


def test_named_domain_resolves_to_the_matching_citation():
    answer = "You can find it on github dot com, I've put the link in the chat."
    question = "what's the repo for fastapi"
    out = links.resolve_links(answer, question, [FASTAPI, DJANGO])
    assert len(out) == 1
    assert out[0]["href"] == "https://github.com/tiangolo/fastapi"
    assert out[0]["why"] == "named"


def test_named_domain_with_path_prefers_exact_page():
    answer = "The docs are at fastapi dot tiangolo dot com slash tutorial."
    out = links.resolve_links(answer, "", [FASTAPI, DOCS])
    assert len(out) == 1
    assert out[0]["href"] == "https://fastapi.tiangolo.com/tutorial/"


def test_literal_typed_url_resolves_to_the_real_citation_even_if_wrong():
    # The local-model case from v2: it types a URL that is close but wrong
    # (missing the repo owner). The href must still be the citation's own
    # link, never the model's guess.
    answer = "It's at https://github.com/fastapi, the FastAPI framework."
    out = links.resolve_links(answer, "the fastapi repo", [FASTAPI, DJANGO])
    assert len(out) == 1
    assert out[0]["href"] == "https://github.com/tiangolo/fastapi"


def test_asked_but_not_named_picks_best_scoring_result():
    answer = "It's a fast Python web framework with automatic docs."
    question = "send me the link to the fastapi repo"
    out = links.resolve_links(answer, question, [DJANGO, FASTAPI])
    assert len(out) == 1
    assert out[0]["href"] == "https://github.com/tiangolo/fastapi"
    assert out[0]["why"] == "asked"


def test_unverifiable_literal_url_is_dropped():
    answer = "Check out https://not-a-real-source.example.com for more."
    out = links.resolve_links(answer, "", [FASTAPI])
    assert out == []


def test_unverifiable_spoken_domain_yields_nothing():
    answer = "You could also try examplesite dot com for alternatives."
    out = links.resolve_links(answer, "", [FASTAPI])
    assert out == []


def test_no_mention_and_no_intent_yields_nothing():
    answer = "FastAPI is fast because it's built on Starlette and Pydantic."
    out = links.resolve_links(answer, "tell me about fastapi", [FASTAPI])
    assert out == []


def test_cap_of_two_distinct_hosts():
    answer = "Compare github dot com and fastapi dot tiangolo dot com for yourself."
    out = links.resolve_links(answer, "", [FASTAPI, DOCS], cap=2)
    assert len(out) == 2
    hosts = {o["host"] for o in out}
    assert hosts == {"github.com", "fastapi.tiangolo.com"}


def test_default_cap_is_one_even_with_two_mentions():
    answer = "Compare github dot com and fastapi dot tiangolo dot com for yourself."
    out = links.resolve_links(answer, "", [FASTAPI, DOCS])
    assert len(out) == 2  # cap defaults to 2 per the function signature
    # but a single-mention answer never returns more than one:
    single = links.resolve_links("It's on github dot com.", "", [FASTAPI, DOCS])
    assert len(single) == 1


def test_empty_web_citations_yields_nothing():
    assert links.resolve_links("It's on github.com.", "", []) == []


def test_answer_box_result_with_no_link_is_skipped():
    out = links.resolve_links("Here's a quick summary.", "tell me more, send the link",
                               [NO_LINK])
    assert out == []


def test_dedupe_same_page_different_query_string():
    dup = _cit("FastAPI", "https://github.com/tiangolo/fastapi?tab=readme")
    answer = "It's on github dot com, twice: github dot com again."
    out = links.resolve_links(answer, "", [FASTAPI, dup])
    assert len(out) == 1
