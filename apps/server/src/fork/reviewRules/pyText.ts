/**
 * Python's text rules for the review-rules port: JavaScript's `\s`, `\w`, `\b`, `trim`, `split`, case folding and
 * string indexing differ from Python's, and the gate is Python, so patterns here are spelled as Python reads them.
 */
import { characterEntities } from "character-entities";

/** Python's whitespace (str.isspace, re `\s`), as the body of a character class. */
export const WS =
  "\\t-\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
export const S = `[${WS}]`;
/** Python re `\w` and `[^\W_]` (letters and numbers; marks are neither). */
const W = "[\\p{L}\\p{N}_]";
export const ALNUM = "[\\p{L}\\p{N}]";
/** Python re `\b`. */
export const B = `(?:(?<=${W})(?!${W})|(?<!${W})(?=${W}))`;

/**
 * A Python pattern source for re.I: Python's case-insensitive `i` also matches `İ` and `ı`, JavaScript's does not
 * (its `s` and `k` already match `ſ` and the Kelvin sign).
 */
export function pyI(source: string): string {
  let out = "";
  let inClass = false;
  for (let k = 0; k < source.length; k++) {
    const ch = source[k]!;
    if (ch === "\\") {
      out += ch + (source[k + 1] ?? "");
      k++;
    } else if (inClass) {
      out += ch === "]" ? "]" : ch === "i" ? "i\\u0130\\u0131" : ch;
      inClass = ch !== "]";
    } else {
      inClass = ch === "[";
      out += ch === "i" ? "[i\\u0130\\u0131]" : ch;
    }
  }
  return out;
}

const SPACE = new RegExp(`^${S}$`, "u");
/** str.strip(), by index: a `\s+$` pattern retries at every space, quadratic on a long run of them. */
export function strip(text: string): string {
  let a = 0;
  let b = text.length;
  while (a < b && SPACE.test(text[a]!)) a++;
  while (b > a && SPACE.test(text[b - 1]!)) b--;
  return text.slice(a, b);
}
export const isBlank = (text: string): boolean => strip(text) === "";
export const split = (text: string): string[] =>
  text.split(new RegExp(`${S}+`, "u")).filter(Boolean);

/** str.strip(chars). */
export function stripChars(text: string, chars: string, left = true, right = true): string {
  let a = 0;
  let b = text.length;
  if (left) while (a < b && chars.includes(text[a]!)) a++;
  if (right) while (b > a && chars.includes(text[b - 1]!)) b--;
  return text.slice(a, b);
}

/** str.splitlines(). */
export function splitlines(text: string): string[] {
  const lines = text.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/u);
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/** text[:n], by code point. */
export const cut = (text: string, n: number): string => Array.from(text).slice(0, n).join("");

/** The value of one decimal digit of any script (Python's int() reads them all): its place in its run of digits. */
export function digitValue(ch: string): number {
  let cp = ch.codePointAt(0)!;
  let n = 0;
  while (n < 64 && /\p{Nd}/u.test(String.fromCodePoint(cp - 1))) {
    cp--;
    n++;
  }
  return n % 10;
}

/** repr(text) of a Python str. */
export function repr(text: string): string {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = "";
  for (const ch of text) {
    const cp = ch.codePointAt(0)!;
    if (ch === "\\" || ch === quote) out += "\\" + ch;
    else if (ch === "\t" || ch === "\n" || ch === "\r")
      out += { "\t": "\\t", "\n": "\\n", "\r": "\\r" }[ch];
    else if (ch !== " " && /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Z}]/u.test(ch)) {
      const [p, w] = cp < 0x100 ? ["\\x", 2] : cp < 0x10000 ? ["\\u", 4] : ["\\U", 8];
      out += p + cp.toString(16).padStart(w as number, "0");
    } else out += ch;
  }
  return quote + out + quote;
}

// html.unescape: the entity names Python reads without a `;` (html.entities.html5), and its numeric replacements.
const LEGACY = new Set(
  (
    "AElig AMP Aacute Acirc Agrave Aring Atilde Auml COPY Ccedil ETH Eacute Ecirc Egrave Euml GT Iacute Icirc Igrave " +
    "Iuml LT Ntilde Oacute Ocirc Ograve Oslash Otilde Ouml QUOT REG THORN Uacute Ucirc Ugrave Uuml Yacute aacute acirc " +
    "acute aelig agrave amp aring atilde auml brvbar ccedil cedil cent copy curren deg divide eacute ecirc egrave eth " +
    "euml frac12 frac14 frac34 gt iacute icirc iexcl igrave iquest iuml laquo lt macr micro middot nbsp not ntilde " +
    "oacute ocirc ograve ordf ordm oslash otilde ouml para plusmn pound quot raquo reg sect shy sup1 sup2 sup3 szlig " +
    "thorn times uacute ucirc ugrave uml uuml yacute yen yuml"
  ).split(" "),
);
// oxlint-disable-next-line t3code/no-em-dash-copy -- Windows-1252 bytes 0x80-0x9f, as html.unescape maps them.
const CP1252 = "€\x81‚ƒ„…†‡ˆ‰Š‹Œ\x8dŽ\x8f\x90‘’“”•\u2013\u2014˜™š›œ\x9džŸ";
const ENTITY_RE = /&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)/gu;
const entities = characterEntities as Record<string, string>;

function charref(s: string): string {
  if (s[0] === "#") {
    const hex = s[1] === "x" || s[1] === "X";
    const num = Number.parseInt(s.slice(hex ? 2 : 1).replace(/;$/u, ""), hex ? 16 : 10);
    if (num === 0) return "�";
    if (num === 13) return "\r";
    if (num >= 0x80 && num <= 0x9f) return CP1252[num - 0x80]!;
    if ((num >= 0xd800 && num <= 0xdfff) || num > 0x10ffff) return "�";
    const invalid =
      (num >= 1 && num <= 8) ||
      num === 11 ||
      (num >= 14 && num <= 31) ||
      (num >= 0x7f && num <= 0x9f);
    if (invalid || (num >= 0xfdd0 && num <= 0xfdef) || (num & 0xfffe) === 0xfffe) return "";
    return String.fromCodePoint(num);
  }
  if (s.endsWith(";") && Object.hasOwn(entities, s.slice(0, -1))) return entities[s.slice(0, -1)]!;
  if (LEGACY.has(s)) return entities[s]!;
  const cps = Array.from(s);
  for (let x = cps.length - 1; x > 1; x--) {
    const name = cps.slice(0, x).join("");
    if (LEGACY.has(name)) return entities[name]! + cps.slice(x).join("");
  }
  return "&" + s;
}

/** Python's html.unescape: GitHub renders entities, so `ma&#106;or` is a label. */
export const htmlUnescape = (text: string): string =>
  text.includes("&") ? text.replace(ENTITY_RE, (_m, s: string) => charref(s)) : text;
