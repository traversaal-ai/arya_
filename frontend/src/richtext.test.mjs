// node --test src/richtext.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { isPlain, parseInline, parseRichText, trimUrl } from "./richtext.js";

const types = (runs) => runs.map((r) => r.t);

test("plain text stays one paragraph of text", () => {
  const b = parseRichText("Thanks for calling. How can I help?");
  assert.equal(b.length, 1);
  assert.deepEqual(types(b[0].runs), ["text"]);
  assert.ok(isPlain(b));
});

test("a url at the end of a sentence keeps its full stop outside the link", () => {
  const runs = parseInline("See https://example.com/pricing?x=1.");
  assert.deepEqual(types(runs), ["text", "link", "text"]);
  assert.equal(runs[1].href, "https://example.com/pricing?x=1");
  assert.equal(runs[2].v, ".");
});

test("a closing parenthesis is kept only when the url opened one", () => {
  assert.deepEqual(trimUrl("https://en.wikipedia.org/wiki/Foo_(bar)"), ["https://en.wikipedia.org/wiki/Foo_(bar)", ""]);
  assert.deepEqual(trimUrl("https://example.com/a)"), ["https://example.com/a", ")"]);
});

test("www hosts get https and emails become mailto", () => {
  const runs = parseInline("Try www.acme.com or mail hi@acme.com today");
  const link = runs.find((r) => r.t === "link");
  const mail = runs.find((r) => r.t === "email");
  assert.equal(link.href, "https://www.acme.com");
  assert.equal(mail.href, "mailto:hi@acme.com");
});

test("a javascript scheme is never a link run", () => {
  const runs = parseInline("do not click javascript:alert(1) please");
  assert.deepEqual(types(runs), ["text"]);
});

test("bold, italic and code inside a sentence", () => {
  const runs = parseInline("Your **order** is *shipping* via `DHL`.");
  assert.deepEqual(types(runs), ["text", "bold", "text", "italic", "text", "code", "text"]);
  assert.equal(runs[1].v, "order");
});

test("arithmetic asterisks are not italics", () => {
  const runs = parseInline("5 * 3 * 2 is 30");
  assert.deepEqual(types(runs), ["text"]);
});

test("numbered and dashed lists become list blocks", () => {
  const b = parseRichText("Two options:\n1. Morning\n2. Afternoon\n\n- bring ID\n- arrive early");
  assert.deepEqual(b.map((x) => x.type), ["p", "list", "list"]);
  assert.equal(b[1].ordered, true);
  assert.equal(b[1].items.length, 2);
  assert.equal(b[2].ordered, false);
  assert.ok(!isPlain(b));
});

test("blank lines split paragraphs, single newlines stay inside one", () => {
  const b = parseRichText("line one\nline two\n\nsecond paragraph");
  assert.equal(b.length, 2);
  assert.equal(b[0].runs[0].v, "line one\nline two");
});

test("empty or non-string input renders nothing", () => {
  assert.deepEqual(parseRichText(""), []);
  assert.deepEqual(parseRichText(null), []);
});
