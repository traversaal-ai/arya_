/* Visually-hidden polite announcer.

   Some of this app's most important state is conveyed only by animation or
   by a swapped subtree — the orb's four agent states, and the whole-screen
   change on navigation. Both are silent to a screen reader. Render this with
   the current message and assistive tech reads the change.

   Kept `polite` on purpose: `assertive` would interrupt the agent's own
   speech being read out, which is the opposite of helpful mid-call. */
export default function LiveRegion({ message, id }) {
  return (
    <div className="sr-only" id={id} role="status" aria-live="polite" aria-atomic="true">
      {message}
    </div>
  );
}
