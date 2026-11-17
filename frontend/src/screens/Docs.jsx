import { useCallback, useEffect, useState } from "react";
import { getDoc } from "../api.js";
import ErrorNote from "../components/ErrorNote.jsx";
import Markdown from "../components/Markdown.jsx";

/* Operator docs, served by the running build (see _DOCS in api.py) rather
   than linked to GitHub: the repo is private, so a link would 404 for
   anyone outside the org, and an in-app page always matches the version
   actually running. */
export default function Docs({ name = "deployment", onBack }) {
  const [doc, setDoc] = useState(null);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    setErr("");
    return getDoc(name)
      .then(setDoc)
      .catch((e) => setErr(`Couldn't load the guide: ${e.message}`));
  }, [name]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div>
      {err && <ErrorNote message={err} onRetry={load} />}
      {!doc && !err && <div className="spin" />}
      {doc && (
        <div className="md">
          <Markdown text={doc.markdown} />
        </div>
      )}
      <div className="btn-row">
        {/* Reachable from the topbar menu, Publish's local warning and the
            deployments empty state, so "back" means whichever of those you
            came from — App's nav stack knows, and unlike history.back() it
            can't walk out of the app. */}
        <button
          className="btn ghost"
          onClick={onBack || (() => window.history.back())}
        >
          Back
        </button>
      </div>
    </div>
  );
}
