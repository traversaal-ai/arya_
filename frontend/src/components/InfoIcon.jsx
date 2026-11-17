import { useEffect, useId, useRef, useState } from "react";

/* Small ⓘ button that toggles an inline panel of help.

   It's a disclosure, not a tooltip. It previously declared role="tooltip"
   while also setting aria-expanded — a contradiction: tooltips are passive
   descriptions, and this holds headings, ordered lists and links. It's now
   an expanded/collapsed region wired to the button with aria-controls.

   The old click interception (preventDefault + a manual window.open for
   links) existed only because these cards were <label> elements, so any
   click inside toggled the card's radio. The cards are divs now and skip
   clicks originating in .info-pop, so the links are just links again. */
export default function InfoIcon({ children, label = "More info" }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);
  const panelId = useId();

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
    <span className="info-wrap" ref={wrapRef}>
      <button
        type="button"
        className="info-btn"
        aria-label={label}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((o) => !o)}
      >
        i
      </button>
      {open && (
        <span className="info-pop" id={panelId} role="group" aria-label={label}>
          {children}
        </span>
      )}
    </span>
  );
}
