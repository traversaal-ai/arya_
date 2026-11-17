/* Spoken contact details, written back as the caller typed them.

   The shared voice rules have the agent SAY an email as words ("name at
   domain dot com") and a phone number digit by digit ("plus 9 2 3 3 3"),
   because that is the only way a speech engine reads them reliably. The
   typed transcript then shows the spoken form, which is wrong for
   something you would copy out of it.

   This does NOT pattern-match spoken addresses or numbers in general. That
   is a trap: "check it out at google dot com" would turn into
   "out@google.com", and "flight 2 3 3 leaves at 9" is not a phone number.
   It only writes out a detail that ALREADY appears somewhere in the same
   conversation in written form, so there is nothing to guess and nothing
   to get wrong. The case it fixes is the one that matters: you typed the
   address or number (or a tool returned it), and the agent read it back
   aloud. Rule from the product owner: once a contact detail has been
   provided in writing, every later mention renders exactly as provided.

   When nobody ever wrote it down (a spoken call, where speech recognition
   heard words on both sides), no rewrite happens. That is the safe
   outcome, and in a spoken call nobody is reading the transcript for an
   address anyway. */

const ADDRESS = /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/g;

// A candidate phone number: optional +, then digits with the usual
// separators. The regex is deliberately loose; looksWrittenPhone() below
// is the real gate. It has to tell a WRITTEN number ("+92 333 0000000",
// digit groups) from the agent's SPOKEN echo of the same number ("9 2 3 3
// 3 0 0 0 ...", one digit per group), because both sit in the same
// transcript and only the written one may be treated as the source of
// truth. Without that gate the spoken echo was collected as "written",
// and the caller's own typed number got rewritten into the echo's shape.
const PHONE = /\+?\d[\d\s().-]{5,}\d/g;
const PHONE_MIN_DIGITS = 7;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function looksWrittenPhone(raw) {
  if (raw.replace(/\D/g, "").length < PHONE_MIN_DIGITS) return false;
  if (ISO_DATE.test(raw)) return false; // 2026-10-05 has 8 digits and hyphens
  const groups = raw.replace(/^\+/, "").split(/[\s().-]+/).filter(Boolean);
  // Spoken: "9 2 3 3 3 ..." is all single-digit groups. Written numbers
  // have at most a country-code single ("+1 415 ...").
  return groups.filter((g) => g.length === 1).length < 3;
}

// How each symbol can arrive once it has been through a speech engine. The
// literal is allowed too, so a half-spoken form normalises as well.
const SPOKEN = {
  "@": "(?:\\s+at\\s+|@)",
  ".": "(?:\\s+dot\\s+|\\s+point\\s+|\\.)",
  "+": "(?:\\s+plus\\s+|\\+)",
  "_": "(?:\\s+underscore\\s+|\\s+underline\\s+|_)",
  "-": "(?:\\s+dash\\s+|\\s+hyphen\\s+|-)",
};

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/* A pattern matching how `address` would be spoken. Built from the address
   itself, so it can only ever match that one address. Whitespace is
   tolerated on either side of every digit ("online 1 1" for "online11"):
   the voice rules make the agent speak digits one at a time, with a
   space before the first one as well as between them, and that space
   used to break the match and leave the spoken form on screen. */
export function spokenPattern(address) {
  let out = "";
  for (let i = 0; i < address.length; i++) {
    const ch = address[i];
    out += SPOKEN[ch] || escapeRe(ch);
    if (/\d/.test(ch) || /\d/.test(address[i + 1] || "")) out += "\\s*";
  }
  return new RegExp(out, "gi");
}

/* A pattern matching how `phone` would be spoken: the same digits in the
   same order, each optionally followed by the separators a model puts
   between spoken digits (spaces, hyphens, dots, parentheses), with an
   optional "plus" or "+" in front. Bounded so it never matches inside a
   longer run of digits. Case-insensitive for "Plus". */
export function spokenPhonePattern(phone) {
  const digits = phone.replace(/\D/g, "");
  const body = digits.split("").map(escapeRe).join("[\\s.\\-()]*");
  return new RegExp(`(^|[^\\d+])((?:\\+\\s*|plus\\s+)?${body})(?!\\d)`, "gi");
}

/* Every address written anywhere in these rows. `field` is where the text
   lives: "text" on a live transcript row, "content" on a stored message. */
export function addressesIn(rows, field = "text") {
  const found = new Set();
  for (const r of rows) {
    for (const m of (r[field] || "").matchAll(ADDRESS)) found.add(m[0]);
  }
  return [...found];
}

/* Every phone number written anywhere in these rows, in the spelling it
   was FIRST written with (rows arrive in conversation order, so the
   caller's typed "+92 333 0000000" beats any later contiguous restatement
   of the same digits). One entry per digit sequence; a number whose
   digits sit inside another collected number is dropped, so a shorter
   suffix can never re-match the tail of a longer one after it is
   restored. */
export function phonesIn(rows, field = "text") {
  const byDigits = new Map();
  for (const r of rows) {
    for (const m of (r[field] || "").matchAll(PHONE)) {
      const raw = m[0].trim();
      if (!looksWrittenPhone(raw)) continue;
      const digits = raw.replace(/\D/g, "");
      if (!byDigits.has(digits)) byDigits.set(digits, raw);
    }
  }
  const all = [...byDigits.keys()];
  return all
    .filter((d) => !all.some((other) => other !== d && other.includes(d)))
    .map((d) => byDigits.get(d));
}

/* The rows, with any spoken form of an address or phone number that
   appears in them replaced by the written form. Rows that do not change
   are returned as-is, so React sees the same objects and does not
   re-render them. */
export function withWrittenAddresses(rows, field = "text") {
  if (!rows) return rows;
  const addresses = addressesIn(rows, field);
  const phones = phonesIn(rows, field);
  if (!addresses.length && !phones.length) return rows;
  const addrSubs = addresses.map((a) => [spokenPattern(a), a]);
  // Longest first, so "+92 333 0000000" is restored before a shorter
  // number that happens to be its suffix could claim part of it.
  const phoneSubs = phones
    .sort((a, b) => b.replace(/\D/g, "").length - a.replace(/\D/g, "").length)
    .map((p) => [spokenPhonePattern(p), p]);
  return rows.map((r) => {
    let text = r[field] || "";
    for (const [re, address] of addrSubs) text = text.replace(re, address);
    for (const [re, phone] of phoneSubs) text = text.replace(re, `$1${phone}`);
    return text === r[field] ? r : { ...r, [field]: text };
  });
}
