import { useEffect, useRef, useState } from "react";
import Icon from "./Icon.jsx";
import { safeHref } from "../safeHref.js";

/* Source chip → click → popover with the evidence.
   - RAG: the matched passage(s) with similarity score + section.
   - Web search: the result title (linked), source, and snippet.
   Rows written before excerpts were stored show the score line only. */
export default function CiteChip({ file, cits, web = false }) {
  const [open, setOpen] = useState(false);
  const [down, setDown] = useState(false);
  const wrapRef = useRef(null);

  // RAG: top 2 passages by score — the rest are echoes of the same
  // document. Web: top 3 results.
  const top = web
    ? cits.slice(0, 3)
    : [...cits].sort((a, b) => (b.score ?? 0) - (a.score ?? 0)).slice(0, 2);

  useEffect(() => {
    if (!open) return;
    function onDown(e) {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false);
    }
    function onKey(e) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span className="info-wrap cite-wrap" ref={wrapRef}>
      <button
        type="button"
        className="chip chip-btn"
        aria-expanded={open}
        title={web ? "Show the web sources" : "Show the matched passages"}
        onClick={() => {
          // flip downward when the chip sits too close to the top of the
          // viewport for the (upward-opening) popover to fit
          const r = wrapRef.current?.getBoundingClientRect();
          setDown(!!r && r.top < 240);
          setOpen((o) => !o);
        }}
      >
        <Icon name={web ? "globe" : "file"} className="inline" /> {file}
      </button>
      {open && (
        <span className={down ? "info-pop cite-pop down" : "info-pop cite-pop"} role="tooltip">
          {top.map((c, i) => {
            const href = web ? safeHref(c.link) : null;
            return web ? (
              <span className="cite-item" key={i}>
                <span className="cite-score">{c.source || "web"}</span>
                {href ? (
                  <a href={href} target="_blank" rel="noreferrer noopener">
                    {c.title || href}
                  </a>
                ) : (
                  <b>{c.title}</b>
                )}
                {c.snippet ? <> · {c.snippet}</> : null}
              </span>
            ) : (
              <span className="cite-item" key={i}>
                <span className="cite-score">
                  {typeof c.score === "number"
                    ? `${Math.round(c.score * 100)}% match`
                    : "match"}
                  {Number.isInteger(c.chunk_index)
                    ? ` · section ${c.chunk_index + 1}`
                    : ""}
                </span>
                {c.excerpt ? (
                  <>“{c.excerpt}”</>
                ) : (
                  <i>No excerpt stored for this citation.</i>
                )}
              </span>
            );
          })}
        </span>
      )}
    </span>
  );
}
