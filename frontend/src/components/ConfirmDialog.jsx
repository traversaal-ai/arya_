import { useId, useRef } from "react";
import { useFocusTrap } from "../focus.js";

/* In-shell replacement for window.confirm(): centered card over a dimmed
   veil. Escape and outside-mousedown cancel (same dismiss pattern as
   InfoIcon/Menu). Render it unconditionally and drive it with `open`.

   Focus is trapped inside the card and restored to whatever opened it. It
   declares aria-modal, so containment has to be real: without the trap, Tab
   walked the page behind the veil and a keyboard user could operate the very
   controls the dialog was guarding — including, on Preview, the delete
   button that opened it. Cancel takes initial focus so the safe choice is
   the default on a destructive prompt. */
export default function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "Confirm",
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}) {
  const boxRef = useRef(null);
  const cancelRef = useRef(null);
  const titleId = useId();
  const bodyId = useId();

  useFocusTrap(boxRef, open, { initialFocus: cancelRef });

  if (!open) return null;

  return (
    <div
      className="dialog-veil"
      onMouseDown={(e) => {
        if (boxRef.current && !boxRef.current.contains(e.target)) onCancel();
      }}
    >
      <div
        className="dialog"
        ref={boxRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        onKeyDown={(e) => {
          // Scoped to the dialog rather than the document: a global Escape
          // listener also fired while a nested popover was open.
          if (e.key === "Escape" && !busy) {
            e.stopPropagation();
            onCancel();
          }
        }}
      >
        <div className="dialog-title" id={titleId}>{title}</div>
        <div className="dialog-body" id={bodyId}>{body}</div>
        <div className="btn-row" style={{ marginTop: 18 }}>
          <button
            className="btn ghost"
            ref={cancelRef}
            onClick={onCancel}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            className={danger ? "btn danger" : "btn"}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
