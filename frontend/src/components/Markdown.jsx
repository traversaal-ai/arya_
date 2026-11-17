/* A deliberately partial markdown renderer for the operator docs.

   It renders exactly what docs/*.md actually uses: #/##/### headings,
   paragraphs, "-" lists, ``` fences, | tables |, **bold**, `code`, and
   [text](url). Anything else falls through as literal text — never a
   crash, never innerHTML (React elements only, so there is no XSS
   surface by construction).

   Why not react-markdown: the tables alone would need remark-gfm too,
   which is ~30 kB gzipped plus a large micromark tree, for one builder
   screen in a starter kit whose only deps are React, LiveKit and icons.
   If the docs ever grow real markdown (nested lists, footnotes, HTML),
   swap this file for react-markdown + remark-gfm — nothing outside it
   knows the difference. */

// **bold** · `code` · [text](url) — everything else passes through.
// Exported: ConnectorCard reuses this alone to linkify plain strings that
// come from the DB-backed catalog (docs_hint/docs_steps), without pulling
// in the block-level parser below.
export function inline(text) {
  const parts = String(text).split(
    /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g
  );
  return parts.map((p, i) => {
    if (p.startsWith("**") && p.endsWith("**")) return <b key={i}>{p.slice(2, -2)}</b>;
    if (p.startsWith("`") && p.endsWith("`")) return <code key={i}>{p.slice(1, -1)}</code>;
    const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(p);
    if (link) {
      return (
        <a key={i} href={link[2]} target="_blank" rel="noreferrer">
          {link[1]}
        </a>
      );
    }
    return p;
  });
}

// "| a | b |" -> ["a", "b"] (the split leaves empty edge cells)
function cells(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim());
}

const isTableSep = (line) => /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && line.includes("-");

export default function Markdown({ text }) {
  const lines = String(text || "").split("\n");
  const out = [];
  let para = [];
  let list = null;
  let i = 0;

  const flushPara = () => {
    if (para.length) {
      out.push(<p key={out.length}>{inline(para.join(" "))}</p>);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      out.push(
        <ul key={out.length}>
          {list.map((item, n) => (
            <li key={n}>{inline(item)}</li>
          ))}
        </ul>
      );
      list = null;
    }
  };
  const flushAll = () => {
    flushPara();
    flushList();
  };

  while (i < lines.length) {
    const line = lines[i];

    // fenced code
    if (line.trim().startsWith("```")) {
      flushAll();
      const body = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // closing fence
      out.push(
        <pre key={out.length}>
          <code>{body.join("\n")}</code>
        </pre>
      );
      continue;
    }

    // table: a header row followed by a |---|---| separator
    if (line.trim().startsWith("|") && isTableSep(lines[i + 1] || "")) {
      flushAll();
      const head = cells(line);
      i += 2;
      const body = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        body.push(cells(lines[i]));
        i += 1;
      }
      out.push(
        // wrapper so a wide table scrolls itself instead of the page
        <div className="md-wrap" key={out.length}>
          <table>
            <thead>
              <tr>
                {head.map((h, n) => (
                  <th key={n}>{inline(h)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((row, rn) => (
                <tr key={rn}>
                  {row.map((c, cn) => (
                    <td key={cn}>{inline(c)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
      continue;
    }

    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      flushAll();
      const level = heading[1].length;
      const H = `h${level}`;
      out.push(<H key={out.length}>{inline(heading[2])}</H>);
      i += 1;
      continue;
    }

    const item = /^\s*[-*]\s+(.*)$/.exec(line);
    if (item) {
      flushPara();
      list = list || [];
      // "- [ ] task" reads fine as literal text; no checkbox handling.
      list.push(item[1]);
      i += 1;
      continue;
    }

    if (!line.trim()) {
      flushAll();
      i += 1;
      continue;
    }

    flushList();
    para.push(line.trim());
    i += 1;
  }
  flushAll();

  return <>{out}</>;
}
