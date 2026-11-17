import { useEffect, useRef, useState } from "react";

/* ⋮ dropdown (call screen top-right). `items` is [{ label, onPick }] with
   optional { heading } entries — non-interactive group labels rendered with
   a separator, skipped by the keyboard roving (they never get refs).
   Outside mousedown + Escape dismiss, same as InfoIcon.

   This is the ONLY navigation on the call screen, so its keyboard story has
   to be real. It previously declared role="menu"/role="menuitem" while
   implementing none of the behaviour those roles promise — no arrow keys, no
   focus on open, no focus return — which is worse than shipping plain
   buttons, because assistive tech announces a menu and then the arrow keys
   do nothing. Now it follows the pattern: roving focus via arrows, Home/End,
   Escape closes and hands focus back to the trigger. */
export default function Menu({ items }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);
  const btnRef = useRef(null);
  const itemRefs = useRef([]);

  useEffect(() => {
    if (!open) return;
    function onDown(e) {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  // Focus the first item on open so the arrow keys have somewhere to start.
  useEffect(() => {
    if (open) itemRefs.current[0]?.focus();
  }, [open]);

  function close({ restoreFocus = true } = {}) {
    setOpen(false);
    if (restoreFocus) btnRef.current?.focus();
  }

  function moveFocus(delta) {
    const list = itemRefs.current.filter(Boolean);
    if (!list.length) return;
    const at = list.indexOf(document.activeElement);
    // Wrap in both directions; from outside the list, step in from the edge.
    const next = (at + delta + list.length) % list.length;
    list[at === -1 ? (delta > 0 ? 0 : list.length - 1) : next]?.focus();
  }

  function onMenuKeyDown(e) {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        moveFocus(1);
        break;
      case "ArrowUp":
        e.preventDefault();
        moveFocus(-1);
        break;
      case "Home":
        e.preventDefault();
        itemRefs.current.filter(Boolean)[0]?.focus();
        break;
      case "End": {
        e.preventDefault();
        const list = itemRefs.current.filter(Boolean);
        list[list.length - 1]?.focus();
        break;
      }
      case "Escape":
        e.preventDefault();
        e.stopPropagation();
        close();
        break;
      case "Tab":
        // Tabbing out of an open menu closes it, but let focus continue on
        // its way rather than yanking it back to the trigger.
        close({ restoreFocus: false });
        break;
      default:
        break;
    }
  }

  return (
    <span className="info-wrap" ref={wrapRef}>
      <button
        type="button"
        ref={btnRef}
        className="icon-btn plain"
        aria-label="Menu"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          // ArrowDown opens and lands on the first item, per the menu-button
          // pattern; Escape closes without moving focus off the trigger.
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            setOpen(true);
          } else if (e.key === "Escape" && open) {
            e.preventDefault();
            setOpen(false);
          }
        }}
      >
        <svg className="ic" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="5" r="1.6" />
          <circle cx="12" cy="12" r="1.6" />
          <circle cx="12" cy="19" r="1.6" />
        </svg>
      </button>
      {open && (
        <span className="info-pop menu-pop" role="menu" onKeyDown={onMenuKeyDown}>
          {items.map((it, i) =>
            it.heading ? (
              <span
                key={`h-${it.heading}`}
                className="menu-head"
                role="presentation"
                aria-hidden="true"
              >
                {it.heading}
              </span>
            ) : (
              <button
                key={it.label}
                type="button"
                ref={(el) => {
                  itemRefs.current[i] = el;
                }}
                className="menu-item"
                role="menuitem"
                // Roving focus: items are reached with the arrow keys, not Tab.
                tabIndex={-1}
                onClick={() => {
                  // Don't restore focus to the trigger — the pick navigates
                  // away, and App moves focus to the new screen.
                  setOpen(false);
                  it.onPick();
                }}
              >
                {it.label}
              </button>
            )
          )}
        </span>
      )}
    </span>
  );
}
