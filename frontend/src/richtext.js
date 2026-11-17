/* Light formatting for agent text: links, emails, bold, italic, code,
   lists and paragraphs. Nothing else, on purpose: no headings, tables or
   raw HTML, because a voice agent's words should read like speech with a
   few helpful marks, not like a document.

   Pure and dependency-free so it runs in node for tests and in the widget
   bundle without pulling in a markdown library. The output is a list of
   blocks; RichText.jsx turns them into elements. The href of a link is a
   candidate only: the renderer passes it through safeHref() before it
   becomes an anchor, so "javascript:" and friends never get through.

   Shape:
     block = {type: "p", runs} | {type: "list", ordered, items: [runs]}
     run   = {t: "text"|"bold"|"italic"|"code"|"link"|"email", v, href?} */

const INLINE = new RegExp(
  [
    "(`[^`\\n]+`)", // 1 code
    "(\\*\\*[^*\\n]+\\*\\*)", // 2 bold
    "(\\*(?!\\s)[^*\\n]+?(?<!\\s)\\*)", // 3 italic (no space just inside)
    "((?:https?:\\/\\/|www\\.)[^\\s<>\"'`]+)", // 4 url
    "([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\\.[A-Za-z0-9-]+)+)", // 5 email
  ].join("|"),
  "g"
);

const LIST_ITEM = /^\s*(?:([-*•])|(\d{1,3})[.)])\s+(.*)$/;
const TRAIL = /[.,;:!?'"’]+$/;

// A URL at the end of a sentence drags the full stop with it; give it back.
// A closing parenthesis is kept only when the URL opened one itself.
export function trimUrl(raw) {
  let url = raw;
  let tail = "";
  for (;;) {
    const m = url.match(TRAIL);
    if (m) {
      url = url.slice(0, -m[0].length);
      tail = m[0] + tail;
      continue;
    }
    if (url.endsWith(")")) {
      const opens = (url.match(/\(/g) || []).length;
      const closes = (url.match(/\)/g) || []).length;
      if (closes > opens) {
        url = url.slice(0, -1);
        tail = ")" + tail;
        continue;
      }
    }
    break;
  }
  return [url, tail];
}

export function parseInline(text) {
  const runs = [];
  let last = 0;
  const push = (t, v, extra) => {
    if (!v) return;
    runs.push(extra ? { t, v, ...extra } : { t, v });
  };
  for (const m of text.matchAll(INLINE)) {
    const i = m.index;
    if (i > last) push("text", text.slice(last, i));
    if (m[1]) push("code", m[1].slice(1, -1));
    else if (m[2]) push("bold", m[2].slice(2, -2));
    else if (m[3]) push("italic", m[3].slice(1, -1));
    else if (m[4]) {
      const [url, tail] = trimUrl(m[4]);
      const href = url.startsWith("www.") ? "https://" + url : url;
      push("link", url, { href });
      push("text", tail);
    } else if (m[5]) push("email", m[5], { href: "mailto:" + m[5] });
    last = i + m[0].length;
  }
  if (last < text.length) push("text", text.slice(last));
  return runs;
}

export function parseRichText(text) {
  const src = typeof text === "string" ? text.replace(/\r\n?/g, "\n") : "";
  if (!src.trim()) return [];
  const blocks = [];
  let para = [];
  let list = null;
  const flushPara = () => {
    if (para.length) blocks.push({ type: "p", runs: parseInline(para.join("\n")) });
    para = [];
  };
  const flushList = () => {
    if (list) blocks.push(list);
    list = null;
  };
  for (const line of src.split("\n")) {
    const item = line.match(LIST_ITEM);
    if (item) {
      flushPara();
      const ordered = !!item[2];
      if (!list || list.ordered !== ordered) {
        flushList();
        list = { type: "list", ordered, items: [] };
      }
      list.items.push(parseInline(item[3]));
      continue;
    }
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();
  return blocks;
}

// True when the text would render exactly as it does today (one paragraph,
// nothing to mark up), so the renderer can hand back the plain string.
export function isPlain(blocks) {
  return (
    blocks.length === 0 ||
    (blocks.length === 1 &&
      blocks[0].type === "p" &&
      blocks[0].runs.every((r) => r.t === "text"))
  );
}
