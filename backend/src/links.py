"""Resolve the one link an answer gives against the search results it drew
on, so the frontend can render a single verified chip instead of exposing
every result the search returned (Plan-UIUX-Improvements_v3.md, Track B).

Design rule, restated because it is easy to get backwards: NEVER return the
whole result list. One link by default, two only when the answer names two
distinct sites, and none at all when nothing in the answer or the question
points at a page.

The model's own text is a hint, never the href: the href always comes from
a citation the worker already holds. A small local model types
plausible-looking URLs that are sometimes wrong (`github.com/fastapi`
instead of `github.com/tiangolo/fastapi`); resolving against the citation
the answer actually used, rather than trusting the typed string, fixes that
without asking the model to get it right.

Pure and deterministic (no model calls, no network), so it is unit-tested
directly rather than through a live call.
"""

import logging
import re

logger = logging.getLogger("links")

_URL_RE = re.compile(r"https?://[^\s,;)\]}\"']+", re.IGNORECASE)

_SPOKEN_SUBS = (
    (re.compile(r"\s+dot\s+", re.IGNORECASE), "."),
    (re.compile(r"\s+slash\s+", re.IGNORECASE), "/"),
    (re.compile(r"\s+dash\s+", re.IGNORECASE), "-"),
    (re.compile(r"\s+underscore\s+", re.IGNORECASE), "_"),
)

# host(/path)?, anchored on a word boundary so "3.5 million" or "9.15am"
# never matches: the final label must be letters only (a plausible TLD),
# which no version number or clock time satisfies.
_HOST_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(/[\w./-]*)?",
    re.IGNORECASE,
)
_TRAILING_PUNCT = re.compile(r"[.,;:!?]+$")

# What counts as "asked for a link" even when the answer named no domain:
# covers a direct ask ("send me the link") and a follow-up one turn later.
_INTENT_RE = re.compile(
    r"\b(link|url|website|web ?site|web ?page|webpage|site|repo|repository|github|"
    r"docs|documentation|homepage)\b|where can i (find|see|read)",
    re.IGNORECASE,
)

_STOP = frozenset(
    "the a an and or of to in for on at is are was were be been it its this that with as by "
    "from you your our we they he she not do does did have has had will would can could may "
    "might shall should about more some any send me that give".split()
)


def _mentions(text: str) -> list[dict]:
    """Every URL or domain the text names, literal or spoken, in the order
    found. {"host", "path", "literal"}; deduped so a domain named twice
    (once written, once spoken) yields one mention."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for m in _URL_RE.finditer(text or ""):
        url = _TRAILING_PUNCT.sub("", m.group(0))
        rest = url.split("://", 1)[1]
        host = rest.split("/", 1)[0].lower().removeprefix("www.")
        path = "/" + rest.split("/", 1)[1] if "/" in rest else ""
        key = (host, path)
        if key not in seen:
            seen.add(key)
            out.append({"host": host, "path": path, "literal": True})

    spoken = text or ""
    for pat, rep in _SPOKEN_SUBS:
        spoken = pat.sub(rep, spoken)
    for m in _HOST_RE.finditer(spoken):
        host = m.group(1).lower().removeprefix("www.")
        path = _TRAILING_PUNCT.sub("", (m.group(2) or ""))
        key = (host, path)
        if key not in seen:
            seen.add(key)
            out.append({"host": host, "path": path, "literal": False})

    return out


def _bases(host: str) -> set[str]:
    """Base-domain variants so "docs.example.co.uk" still matches a mention
    of "example.co.uk". Mirrors bases() in SourceCards.jsx."""
    host = (host or "").lower().removeprefix("www.")
    parts = host.split(".")
    out = {host}
    for i in range(1, len(parts) - 1):
        out.add(".".join(parts[i:]))
    return out


def _cit_host_path(cit: dict) -> tuple[str, str]:
    link = cit.get("link") or ""
    if "://" not in link:
        return "", ""
    rest = link.split("://", 1)[1]
    host = rest.split("/", 1)[0].lower().removeprefix("www.")
    path = "/" + rest.split("/", 1)[1] if "/" in rest else ""
    return host, path


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if t not in _STOP and len(t) > 2}


def _overlap_score(query: str, cit: dict) -> int:
    """Distinctive-word overlap between the conversation and a citation's
    title/snippet/link, used to break a tie between several results on the
    same domain ("the FastAPI repo" -> the tiangolo/fastapi result, not
    some other github.com hit) and to pick the best result when nothing
    named a domain at all. The snippet carries most of the signal: two
    titles can both be generic ("The Django framework" vs "FastAPI") while
    only one snippet actually echoes what the answer just said."""
    corpus = f"{cit.get('title', '')} {cit.get('snippet', '')} {cit.get('link', '')}"
    return len(_tokens(query) & _tokens(corpus))


def _best_match(host: str, path: str, web_cits: list[dict], query: str) -> dict | None:
    bases = _bases(host)
    candidates = []
    for cit in web_cits:
        c_host, c_path = _cit_host_path(cit)
        if not c_host or not (_bases(c_host) & bases):
            continue
        if path and not c_path.startswith(path.rstrip("/")):
            continue
        candidates.append(cit)
    if not candidates:
        return None
    candidates.sort(key=lambda c: _overlap_score(query, c), reverse=True)
    return candidates[0]


def resolve_links(answer: str, question: str, web_cits: list[dict], *, cap: int = 2) -> list[dict]:
    """The one (or two) links this answer gives, verified against the
    search results it actually drew on. Never the full list.

    web_cits: the {type:"web", title, snippet, source, link} rows for THIS
    answer (or, for a bare follow-up like "send me that link", the
    previous answer's; the caller decides which to pass).

    Returns [{"href", "host", "path", "title", "why": "named"|"asked"}],
    at most `cap` items, in the order the answer named them (or, for an
    "asked" match, the single best-scoring result).
    """
    if not web_cits:
        return []
    out: list[dict] = []
    seen_keys: set[str] = set()
    query = f"{question or ''} {answer or ''}"

    def _add(cit: dict, why: str) -> None:
        link = cit.get("link") or ""
        if "://" not in link:
            return
        key = link.split("?", 1)[0].rstrip("/")
        if key in seen_keys:
            return
        seen_keys.add(key)
        host, path = _cit_host_path(cit)
        out.append({
            "href": link, "host": host, "path": path,
            "title": (cit.get("title") or "").strip() or host, "why": why,
        })

    for mention in _mentions(answer):
        if len(out) >= cap:
            break
        match = _best_match(mention["host"], mention["path"], web_cits, query)
        if match:
            _add(match, "named")
        elif mention["literal"]:
            # A URL the model typed with no matching source is a guess,
            # not a fact, so drop it rather than ever using it as an href.
            logger.info(f"link dropped, no source: {mention['host']}{mention['path']}")

    if not out and _INTENT_RE.search(query):
        candidates = [c for c in web_cits if (c.get("link") or "").strip()]
        candidates.sort(key=lambda c: _overlap_score(query, c), reverse=True)
        if candidates:
            _add(candidates[0], "asked")

    return out[:cap]
