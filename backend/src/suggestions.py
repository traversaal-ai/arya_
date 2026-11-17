"""Quick replies derived from the agent's own words, with no model call.

The transcript shows them as tappable chips under the latest agent bubble;
tapping one sends the text as the caller's turn. Three shapes are
recognised, in this order, and anything else yields nothing (an empty
list means no chips, which is the right answer for an open question):

  "morning or afternoon?"            -> ["Morning", "Afternoon"]
  "Would you like me to book that?"  -> ["Yes, please", "Not now"]
  "Is the address still the same?"   -> ["Yes", "No"]

Pure and deterministic, so it is unit-tested without a worker. Keep the
rules narrow: a wrong chip under a question is worse than none.
"""

from __future__ import annotations

import re

MAX_CHIPS = 4
_MAX_WORDS = 3

_OFFER = re.compile(
    r"^(would you like me to|shall i|should i|do you want me to|can i|may i|"
    r"want me to|would you like to|do you want to)\b",
    re.I,
)
_YES_NO = re.compile(
    r"^(is|are|was|were|do|does|did|can|could|would|will|shall|should|have|has|"
    r"had|may|might|am)\b",
    re.I,
)
# "... X or Y?" where X and Y are short (a word or two each). The X side is
# the tail of the sentence after the last comma/colon/verb-ish break, so
# "would you prefer morning or afternoon?" gives "morning", not the whole
# clause.
_EITHER = re.compile(
    r"(?:^|[,:]|\b(?:prefer|want|like|choose|pick|rather|be|is|it|have|take|go|for|in|on|at|by|the|a|an)\s+)"
    r"([A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,2})\s+or\s+"
    r"([A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,2})\s*\?$",
    re.I,
)


def _last_sentence(text: str) -> str:
    t = " ".join((text or "").split()).strip()
    if not t or not t.endswith("?"):
        return ""
    parts = re.split(r"(?<=[.!?])\s+", t)
    return parts[-1].strip() if parts else ""


# Function words that can lead a captured side ("it for today", "will you
# collect") and are not part of the answer; stripped from the front only.
_LEAD = {
    "it", "its", "you", "i", "we", "they", "he", "she", "will", "would", "do",
    "does", "is", "are", "be", "the", "a", "an", "to", "for", "in", "on", "at",
    "by", "me", "my", "your", "our", "that", "this", "with", "rather", "prefer",
    "want", "like", "have", "take", "go", "get",
}


def _cap(s: str) -> str:
    s = s.strip(" ,.;:")
    return s[:1].upper() + s[1:] if s else s


def _side(raw: str) -> str:
    words = raw.strip(" ,.;:").split()
    while words and words[0].lower() in _LEAD:
        words.pop(0)
    if not words or len(words) > _MAX_WORDS:
        return ""
    return _cap(" ".join(words))


def derive(text: str) -> list[str]:
    """Chips for the question this text ends with, or [] when unsure."""
    q = _last_sentence(text)
    if not q:
        return []
    low = q.lower()
    m = _EITHER.search(q)
    if m:
        a, b = _side(m.group(1)), _side(m.group(2))
        if a and b and a.lower() != b.lower():
            if not {a.lower(), b.lower()} & {"yes", "no", "not"}:
                return [a, b]
    if re.search(r"\bor\b", low):
        # Alternatives were offered but could not be turned into short
        # chips; a Yes/No pair under "X or Y?" would be the wrong answer.
        return []
    if _OFFER.match(low):
        return ["Yes, please", "Not now"]
    if _YES_NO.match(low):
        return ["Yes", "No"]
    return []


def clean_openers(values) -> list[str]:
    """The template's or owner's openers: short, deduplicated, at most four."""
    out: list[str] = []
    for v in values or []:
        s = " ".join(str(v or "").split()).strip()
        if not s or len(s) > 48:
            continue
        if s.lower() in (o.lower() for o in out):
            continue
        out.append(s)
        if len(out) == MAX_CHIPS:
            break
    return out
