import { useMemo } from "react";
import { hostOf, safeHref } from "../safeHref.js";
import { isPlain, parseRichText } from "../richtext.js";

// Agent text with its links, emails, bold, lists and paragraphs rendered.
// Used by the live transcript and by History; user bubbles stay plain.
// Plain text takes the fast path and renders exactly as before, so the
// change is invisible wherever there is nothing to mark up. Shared with
// the widget, so no admin-API import here.

const MAX_PATH = 28;

function linkLabel(href) {
  const host = hostOf(href);
  try {
    const u = new URL(href);
    const path = (u.pathname + u.search).replace(/\/$/, "");
    if (!path || path === "/") return host;
    const short = path.length > MAX_PATH ? path.slice(0, MAX_PATH - 1) + "…" : path;
    return host + short;
  } catch {
    return href;
  }
}

function Runs({ runs }) {
  return runs.map((r, i) => {
    switch (r.t) {
      case "bold":
        return <b key={i}>{r.v}</b>;
      case "italic":
        return <i key={i}>{r.v}</i>;
      case "code":
        return <code key={i} className="rt-code">{r.v}</code>;
      case "link": {
        const href = safeHref(r.href);
        if (!href) return r.v;
        return (
          <a
            key={i}
            className="rt-link"
            href={href}
            target="_blank"
            rel="noreferrer"
            title={href}
          >
            {linkLabel(href)}
          </a>
        );
      }
      case "email":
        return (
          <a key={i} className="rt-link" href={r.href}>
            {r.v}
          </a>
        );
      default:
        return r.v;
    }
  });
}

export default function RichText({ text }) {
  const blocks = useMemo(() => parseRichText(text), [text]);
  if (isPlain(blocks)) return text || null;
  return blocks.map((b, i) => {
    if (b.type === "list") {
      const Tag = b.ordered ? "ol" : "ul";
      return (
        <Tag key={i} className="rt-list">
          {b.items.map((runs, j) => (
            <li key={j}>
              <Runs runs={runs} />
            </li>
          ))}
        </Tag>
      );
    }
    return (
      <p key={i} className="rt-p">
        <Runs runs={b.runs} />
      </p>
    );
  });
}
