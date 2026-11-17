import { useState } from "react";
import CiteChip from "./CiteChip.jsx";
import { hostOf, linkKey, safeHref } from "../safeHref.js";

/* Everything under an agent bubble that is a citation, in one place, for
   the live transcript and History. Three presentations:

   mode="chips" (builder call window and History): the original rendering,
   one CiteChip per document plus one aggregate "Web sources" chip whose
   popover lists the linked results. Unchanged on purpose.

   mode="collapsed" (widget and share-link page, default): a one-line
   "Sources · N" disclosure. Nothing to load or fetch: opening it reveals
   the same card list "cards" mode always showed. This is the visitor
   default from v3: the single link an answer gives (LinkChips.jsx) is
   always visible; the full list of what the agent searched is one tap
   away instead of three stacked cards eating the panel.

   mode="cards" (kept for a builder that wants it; nothing uses it as a
   default after v3): web results as always-open clickable cards.

   mode="off": renders no web results at all (the show_sources switch,
   Publish & deploy), but doc chips, when docChips is true, still render,
   since that switch is about outside links, not the agent's own files.

   `docChips` (default true): whether document citation chips render at
   all, in any mode. false on both public surfaces (widget, share link) as
   defence in depth: the worker already withholds doc citations from
   those sessions server-side; this is the second gate in code, not CSS.

   `exclude` (optional): hrefs already shown as LinkChips above this
   card list. A web result matching one is dropped here so the same page
   never appears twice under one bubble.

   `text` is the bubble's spoken text. Cards whose domain the agent
   actually named ("github dot com" is normalised to "github.com") are
   listed first, so the link the caller just heard is the one on top. */

const DOMAIN_RE = /\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b/gi;

function mentionedHosts(text) {
  const spoken = (text || "").toLowerCase().replace(/\s+dot\s+/g, ".");
  return new Set([...spoken.matchAll(DOMAIN_RE)].map((m) => m[1]));
}

// Base domain of a host so "www.docs.example.co.uk" still matches a spoken
// "example dot co dot uk". Good enough for the ordering it drives.
function bases(host) {
  const parts = host.split(".");
  const out = new Set([host]);
  for (let i = 1; i < parts.length - 1; i++) out.add(parts.slice(i).join("."));
  return out;
}

function dedupedWeb(rawWeb, text, exclude) {
  const excluded = new Set((exclude || []).map((l) => linkKey(l.href || l)).filter(Boolean));
  const seen = new Set();
  const web = [];
  for (const c of rawWeb) {
    const href = safeHref(c.link);
    if (!href) continue; // answer-box style results carry no link
    const key = linkKey(href);
    if (seen.has(key) || excluded.has(key)) continue;
    seen.add(key);
    const host = hostOf(href);
    web.push({
      href,
      host,
      title: (c.title || "").trim() || host,
      snippet: (c.snippet || "").trim(),
    });
  }
  const said = mentionedHosts(text);
  const mentioned = (h) => [...bases(h)].some((b) => said.has(b));
  web.sort((a, b) => Number(mentioned(b.host)) - Number(mentioned(a.host)));
  return web;
}

function DocChips({ files, docs }) {
  if (files.length === 0) return null;
  return (
    <div className="sum" style={{ margin: "4px 0 0" }}>
      {files.map((f) => (
        <CiteChip key={f} file={f} cits={docs.filter((c) => c.filename === f)} />
      ))}
    </div>
  );
}

// Bare list of anchors, no wrapper: callers supply the flex-column layout
// (`.srccards`) so a bordered caption can sit above it (mode="cards") or
// not (mode="collapsed", where the disclosure summary already says
// "Sources · N").
function SrcCardList({ shown }) {
  return shown.map((c) => (
    <a
      className="srccard"
      key={c.href}
      href={c.href}
      target="_blank"
      rel="noreferrer noopener"
      title={c.title}
    >
      {/* Ares returns the domain as the title; say it once. */}
      {c.title !== c.host && <span className="srccard-host">{c.host}</span>}
      <span className="srccard-title">{c.title}</span>
      {c.snippet && <span className="srccard-snip">{c.snippet}</span>}
    </a>
  ));
}

export default function SourceCards({
  cits,
  mode = "chips",
  text = "",
  max = 3,
  docChips = true,
  exclude,
}) {
  const [open, setOpen] = useState(false);
  if (!cits || cits.length === 0) return null;

  const docs = docChips ? cits.filter((c) => c.type !== "web") : [];
  const files = [...new Set(docs.map((c) => c.filename))];
  const rawWeb = mode === "off" ? [] : cits.filter((c) => c.type === "web");

  if (mode === "chips") {
    if (files.length === 0 && rawWeb.length === 0) return null;
    return (
      <div className="sum" style={{ margin: "4px 0 0" }}>
        {files.map((f) => (
          <CiteChip key={f} file={f} cits={docs.filter((c) => c.filename === f)} />
        ))}
        {rawWeb.length > 0 && <CiteChip file="Web sources" web cits={rawWeb} />}
      </div>
    );
  }

  const shown = dedupedWeb(rawWeb, text, exclude).slice(0, max);

  if (mode === "collapsed") {
    if (shown.length === 0) return <DocChips files={files} docs={docs} />;
    return (
      <>
        <details className="srcs" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
          <summary className="srcs-sum">
            Sources · {shown.length}
            <svg className="srcs-chev" viewBox="0 0 24 24" aria-hidden="true">
              <polyline
                points="9 6 15 12 9 18"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </summary>
          <div className="srccards">
            <SrcCardList shown={shown} />
          </div>
        </details>
        <DocChips files={files} docs={docs} />
      </>
    );
  }

  // mode === "cards": always-open list, kept for a builder that wants it.
  if (shown.length === 0 && files.length === 0) return null;
  return (
    <>
      {shown.length > 0 && (
        <div className="srccards">
          <div className="srccards-cap">Sources</div>
          <SrcCardList shown={shown} />
        </div>
      )}
      <DocChips files={files} docs={docs} />
    </>
  );
}
