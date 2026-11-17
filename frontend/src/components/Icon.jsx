/* The interface icon set.

   The app already had a coherent icon idiom — `.ic` in styles.css is a 24×24
   viewBox with `stroke: currentColor; fill: none; stroke-width: 2` and round
   caps, i.e. the Feather geometry the mic and phone glyphs are drawn in. What
   it did NOT have was coverage: about fourteen places reached for an emoji
   instead, which meant two problems. Emoji cannot take `currentColor`, so they
   were the one part of the UI the theme tokens could not reach — a full-colour
   floppy disk sitting in a themed dark panel. And on any system without an
   emoji font they render as tofu boxes.

   Identity icons are deliberately NOT here: an agent's own icon, the industry
   markers, and the template glyphs stay emoji. Those are content rather than
   chrome, they live in saved configs and in backend/agents/templates, and the
   warmth is wanted where the product is talking about someone's business.

   `circle` needs the `ring` class because `.ic circle` is filled by default —
   that rule exists for the ⋮ menu dots. */
const PATHS = {
  // ── controls ────────────────────────────────────────────────────────────
  send: (
    <>
      <line x1="22" y1="2" x2="11" y2="13" />
      <polygon points="22 2 15 22 11 13 2 9 22 2" />
    </>
  ),
  x: (
    <>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </>
  ),
  plus: (
    <>
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </>
  ),
  play: <polygon className="solid" points="6 4 20 12 6 20 6 4" />,
  check: <polyline points="20 6 9 17 4 12" />,

  // ── the workflow map + citation chips ───────────────────────────────────
  file: (
    <>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" />
      <line x1="16" y1="17" x2="8" y2="17" />
    </>
  ),
  globe: (
    <>
      <circle className="ring" cx="12" cy="12" r="10" />
      <line x1="2" y1="12" x2="22" y2="12" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </>
  ),
  search: (
    <>
      <circle className="ring" cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </>
  ),
  // link chips (LinkChips.jsx, SourceCards.jsx): "this opens elsewhere"
  external: (
    <>
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </>
  ),
  chevron: <polyline points="9 6 15 12 9 18" />,
  // the model. `cpu` rather than a brain: it reads as "the thing doing the
  // thinking" at 17px, where a brain silhouette turns to mush.
  cpu: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <rect x="9" y="9" width="6" height="6" />
      <line x1="9" y1="1" x2="9" y2="4" />
      <line x1="15" y1="1" x2="15" y2="4" />
      <line x1="9" y1="20" x2="9" y2="23" />
      <line x1="15" y1="20" x2="15" y2="23" />
      <line x1="20" y1="9" x2="23" y2="9" />
      <line x1="20" y1="14" x2="23" y2="14" />
      <line x1="1" y1="9" x2="4" y2="9" />
      <line x1="1" y1="14" x2="4" y2="14" />
    </>
  ),
  database: (
    <>
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
      <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
    </>
  ),
  calendar: (
    <>
      <rect x="3" y="4" width="18" height="18" rx="2" />
      <line x1="16" y1="2" x2="16" y2="6" />
      <line x1="8" y1="2" x2="8" y2="6" />
      <line x1="3" y1="10" x2="21" y2="10" />
    </>
  ),
  cloud: <path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z" />,
  // connectors (MCP): an external system plugged into the agent
  plug: (
    <>
      <path d="M12 22v-5" />
      <path d="M9 8V2" />
      <path d="M15 8V2" />
      <path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z" />
    </>
  ),
};

export default function Icon({ name, className = "", title }) {
  const paths = PATHS[name];
  if (!paths) return null;
  return (
    <svg
      className={className ? `ic ${className}` : "ic"}
      viewBox="0 0 24 24"
      role={title ? "img" : undefined}
      aria-label={title || undefined}
      aria-hidden={title ? undefined : "true"}
    >
      {paths}
    </svg>
  );
}
