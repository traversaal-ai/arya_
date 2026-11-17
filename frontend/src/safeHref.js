/* Link hygiene for anything the agent's tools hand the UI.

   Every URL rendered as an anchor comes from tool output (search results,
   Google Flights and Maps links built server-side), never from the model's
   own text, but it still crosses a trust boundary before it becomes an
   href. Two rules, applied in one place:
   - only http(s) survives: javascript:, data:, relative strings and the
     empty `link: ""` that SerpAPI answer boxes and Ares/Tavily
     answer-only fallbacks produce all collapse to null, and callers render
     plain text instead of a dead or dangerous link;
   - the display host drops a leading www. so cards read "github.com". */

export function safeHref(url) {
  if (typeof url !== "string" || !url.trim()) return null;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

export function hostOf(href) {
  try {
    return new URL(href).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

// Dedupe key: same page with and without a trailing slash, or with a
// tracking query, is one source, not three cards.
export function linkKey(href) {
  try {
    const u = new URL(href);
    return u.origin + u.pathname.replace(/\/$/, "");
  } catch {
    return href;
  }
}
