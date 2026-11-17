/* Quick replies under the latest agent bubble: the template's openers
   under the greeting, or chips derived from the question the reply ends
   with (backend/src/suggestions.py). Tapping one sends its text as the
   caller's turn, the same path as typing it; the row disappears as soon
   as the caller speaks or types. Shared by the builder and the widget,
   so no admin-API import here. */

export default function SuggestionChips({ chips, onPick, disabled = false }) {
  if (!chips || chips.length === 0) return null;
  return (
    <div className="sugg-row" role="group" aria-label="Quick replies">
      {chips.map((c) => (
        <button
          type="button"
          className="sugg-chip"
          key={c}
          disabled={disabled}
          onClick={() => onPick(c)}
          onKeyDown={(e) => {
            // Arrow keys move along the row; Tab leaves it as usual.
            if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
            const row = e.currentTarget.parentElement;
            const items = [...row.querySelectorAll("button:not(:disabled)")];
            const i = items.indexOf(e.currentTarget);
            const next = items[(i + (e.key === "ArrowRight" ? 1 : -1) + items.length) % items.length];
            next?.focus();
            e.preventDefault();
          }}
        >
          {c}
        </button>
      ))}
    </div>
  );
}
