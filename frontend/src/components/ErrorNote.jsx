/* One failure surface for the whole app: names the problem and offers the
   recovery in the same element.

   Every error in the build used to be a dead-end line of red text with no
   way forward, so a transient fetch failure meant reloading the page. Pass
   `onRetry` wherever the action is repeatable.

   role="alert" so it's announced when it appears: these mount in response
   to a user action whose result is otherwise invisible to a screen reader.
   `className` lets a caller add a spacing utility (e.g. "mb-4") instead of
   an inline margin. */
export default function ErrorNote({
  message,
  onRetry,
  retryLabel = "Try again",
  busy = false,
  style,
  className = "",
}) {
  if (!message) return null;
  return (
    <div className={`note warn note-row ${className}`.trim()} role="alert" style={style}>
      <span className="note-text">{message}</span>
      {onRetry && (
        <button
          type="button"
          className="btn ghost slim note-retry"
          onClick={onRetry}
          disabled={busy}
        >
          {busy ? "…" : retryLabel}
        </button>
      )}
    </div>
  );
}
