"""Fetch a business website's homepage and boil it down to plain text.

Used once, at wizard time: the extracted text is stored on the agent
config (business.website_context) and folded into the generated system
prompt. Single-user local tool — the URL is typed by the person running
the app, so scheme validation is enough; no private-IP blocklist.

Never raises: every outcome is a dict, ok or not, so the API route can
return HTTP 200 either way and the wizard proceeds without the site.
"""

import asyncio
import re
from html.parser import HTMLParser

import httpx

FETCH_TIMEOUT_S = 8.0
MAX_BYTES = 1_500_000  # stop reading after ~1.5 MB of HTML
# Keep essentially the whole homepage: after junk-stripping, real sites
# rarely exceed this. The cap exists only as a context-window safety net —
# the text rides in the system prompt on every turn (~7.5k tokens at the
# cap: trivial for OpenAI/Groq 128k windows; the small local models are
# the reason a cap exists at all).
MAX_TEXT_CHARS = 30_000

# Boilerplate containers: menus, cookie banners, footers, forms — their
# text is link soup that pollutes the prompt, so it's skipped entirely.
_SKIP_TAGS = {
    "script", "style", "noscript", "svg", "template", "head",
    "nav", "footer", "header", "aside", "form", "button", "select", "iframe",
}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.title = ""
        self.description = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            # The site's own summary is the cleanest "about us" available —
            # captured regardless of skip depth (meta lives inside <head>).
            a = dict(attrs)
            name = (a.get("name") or a.get("property") or "").lower()
            if name in ("description", "og:description") and a.get("content"):
                if len(a["content"]) > len(self.description):
                    self.description = a["content"]
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.chunks.append(data)


def _normalize(url: str) -> str | None:
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    if not url.startswith(("http://", "https://")):
        return None
    return url


def _extract_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass  # keep whatever was parsed before the hiccup
    title = re.sub(r"\s+", " ", parser.title).strip()
    desc = re.sub(r"\s+", " ", parser.description).strip()
    body = re.sub(r"\s+", " ", " ".join(parser.chunks)).strip()
    # Description first (the site's own summary), then body text.
    text = " ".join(p for p in (desc, body) if p)
    if len(text) > MAX_TEXT_CHARS:
        # Cut at a word boundary and SAY it's cut — a silent mid-word crop
        # reads to the LLM like a complete (wrong) sentence.
        cut = text.rfind(" ", 0, MAX_TEXT_CHARS)
        text = text[: cut if cut > 0 else MAX_TEXT_CHARS].rstrip()
        text += " …[text truncated — the website has more content than shown here]"
    return title, text


async def fetch_website_text(url: str) -> dict:
    normalized = _normalize(url)
    if normalized is None:
        return {"ok": False, "error": "invalid url"}
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": "Arya/1.0 (+local voice-agent starter kit)"},
        ) as client:
            async with client.stream("GET", normalized) as resp:
                if resp.status_code >= 400:
                    return {"ok": False, "error": f"http {resp.status_code}"}
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype and "text" not in ctype:
                    return {"ok": False, "error": "not an html page"}
                raw = b""
                async for chunk in resp.aiter_bytes():
                    raw += chunk
                    if len(raw) >= MAX_BYTES:
                        break
                final_url = str(resp.url)
        html = raw.decode("utf-8", errors="replace")
    except (httpx.TimeoutException, asyncio.TimeoutError):
        return {"ok": False, "error": "timeout"}
    except httpx.HTTPError:
        return {"ok": False, "error": "unreachable"}
    except Exception as e:  # truly never raise into the route
        return {"ok": False, "error": str(e) or "fetch failed"}

    title, text = _extract_text(html)
    if not text:
        return {"ok": False, "error": "no readable text on the page"}
    return {"ok": True, "url": final_url, "title": title, "text": text}
