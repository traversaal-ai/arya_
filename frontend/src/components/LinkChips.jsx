import Icon from "./Icon.jsx";
import { safeHref } from "../safeHref.js";

/* The one (or two) links an answer gives, resolved server-side against the
   search results it actually drew on (backend/src/links.py) and published
   on the arya.links text-stream topic, or read from messages.links in
   History. Renders nothing when there is nothing to show.

   This is deliberately NOT the search result list; that would defeat the
   whole point of resolving a single link. The full list of what the agent
   searched stays behind the collapsed Sources control in SourceCards.jsx;
   this component only ever shows what the answer actually gave the caller.

   Title first, domain second, URL path never. The path is usually a slug
   ("/boring-bot") that tells a caller nothing, while the page title
   ("Forward Deployed Engineering Bootcamp") is exactly the "what will this
   open" answer they need before tapping. The title used to be a hover
   tooltip, which touch users never see at all. The full URL is still the
   href and still shows in the browser's own status bar on hover.

   Mount order under a bubble, top to bottom: link chips (what the agent
   gave you), option cards (what to choose), sources (what it drew on). */

export default function LinkChips({ links }) {
  if (!links || links.length === 0) return null;
  const items = links
    .map((l) => ({ ...l, href: safeHref(l.href) }))
    .filter((l) => l.href);
  if (items.length === 0) return null;
  return (
    <div className="sum link-row">
      {items.map((l) => {
        const title = (l.title || "").trim();
        // links.py falls back to the host when a result has no title;
        // show the host once in that case rather than twice.
        const showTitle = title && title.toLowerCase() !== l.host.toLowerCase();
        return (
          <a
            key={l.href}
            className="chip chip-btn link-chip"
            href={l.href}
            target="_blank"
            rel="noreferrer noopener"
            title={l.href}
            aria-label={
              showTitle
                ? `Open ${title} on ${l.host}, new tab`
                : `Open ${l.host}, new tab`
            }
          >
            <Icon name="globe" className="link-chip-globe" />
            <span className="link-chip-label">
              {showTitle && <span className="link-chip-title">{title}</span>}
              {showTitle && (
                <span className="link-chip-sep" aria-hidden="true">
                  ·
                </span>
              )}
              <span className="link-chip-host">{l.host}</span>
            </span>
            <Icon name="external" className="link-chip-ext" />
          </a>
        );
      })}
    </div>
  );
}
