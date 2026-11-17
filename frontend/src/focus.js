import { useEffect } from "react";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

/* Visible focusable descendants, in DOM order. offsetParent is null for
   display:none subtrees, which is what keeps a collapsed <details> or an
   unmounted branch out of the tab cycle. */
export function focusableWithin(node) {
  if (!node) return [];
  return [...node.querySelectorAll(FOCUSABLE)].filter(
    (el) => el.offsetParent !== null || el === document.activeElement
  );
}

/* Contain Tab inside `ref` while `active`, move focus in on open, and put it
   back where it came from on close.

   The restore half is the part that's usually missing: without it, closing a
   dialog drops focus to <body> and a keyboard user restarts from the top of
   the page. `aria-modal` promises containment, so a trap has to actually
   deliver it — otherwise Tab walks the page behind the veil and a keyboard
   user can operate controls they were never told are still live.

   Listener is capture-phase so it wins over anything inside the dialog. */
export function useFocusTrap(ref, active, { initialFocus } = {}) {
  useEffect(() => {
    if (!active) return;
    const previous = document.activeElement;
    const node = ref.current;
    const first =
      (initialFocus && initialFocus.current) || focusableWithin(node)[0] || node;
    first?.focus();

    function onKeyDown(e) {
      if (e.key !== "Tab") return;
      const items = focusableWithin(ref.current);
      if (!items.length) {
        e.preventDefault();
        return;
      }
      const edgeFirst = items[0];
      const edgeLast = items[items.length - 1];
      const current = document.activeElement;
      const outside = !ref.current?.contains(current);
      if (e.shiftKey && (current === edgeFirst || outside)) {
        e.preventDefault();
        edgeLast.focus();
      } else if (!e.shiftKey && (current === edgeLast || outside)) {
        e.preventDefault();
        edgeFirst.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      // Guard against restoring to a node that unmounted while open.
      if (previous instanceof HTMLElement && document.contains(previous)) {
        previous.focus();
      }
    };
  }, [active]); // eslint-disable-line react-hooks/exhaustive-deps
}
