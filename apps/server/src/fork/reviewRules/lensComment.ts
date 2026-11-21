/**
 * Port of lens.py's header rules (fork-ci/actions/pr-queue), held to the shared vectors and the gate's lens corpus.
 * Not ported: lens.Review.lens_problem (a comment that only looks like a report); lensScan reads it as no lens comment.
 */
import { ALNUM, B, S, WS, isBlank, pyI, split, splitlines, strip, stripChars } from "./pyText.ts";

const TAG_OPEN = /<\/?[A-Za-z]/gu;
const TAG_NARROW = /<\/?[A-Za-z][^<>]*>/gu;
const LINK_NARROW = /!?\[([^[\]]*)\]\([^()]*\)/gu;
const EDGE_UNDERSCORES = new RegExp(`(?<!${ALNUM})_+|_+(?!${ALNUM})`, "gu");
const LEADING_MARKERS = new RegExp(
  `^${S}*(?:(?:>|[-+•](?=${S}|$)|\\p{Nd}{1,9}[.)](?=${S}|$)|#{1,6}(?=${S}|$)|~{3,}|\\[[ xX]\\](?=${S}|$))${S}*)*`,
  "u",
);
const LENS_RE = new RegExp(`^lens${S}*:${S}*([\\s\\S]*)`, "iu");
const FIELD_RE = new RegExp(
  pyI(
    `(?<!${S})${S}*(?:[·,${/\u2014/u.source}]${S}*)?${B}(?:reviewer${S}*:|read[${WS}-]*at${B}${S}*(?::|[0-9a-f]{7}))`,
  ),
  "iu",
);
const READ_AT_RE = new RegExp(`${B}read[${WS}-]*at${B}${S}*(?::${S}*)?([0-9a-f]{7,40})${B}`, "iu");
const MARKER = /^(?:r\p{Nd}+|lens|re-?check)$/u;

export const NO_HEADER = "comment without a first-line lens header";
export const EDITED = "edited after posting: post a new comment instead";
export const HIDDEN = "hidden: post a new comment instead";

/** Each tag to the first `>` after its start, in linear time; no `>` left: no later tag closes either. */
function stripTags(text: string): string {
  let out = "";
  let pos = 0;
  for (const m of text.matchAll(TAG_OPEN)) {
    if (m.index < pos) continue;
    const end = text.indexOf(">", m.index + m[0].length);
    if (end < 0) break;
    out += text.slice(pos, m.index);
    pos = end + 1;
  }
  return out + text.slice(pos);
}

/** `[x](y)` -> x in linear time; no `]` or `)` left: no later link closes either. */
function stripLinks(text: string): string {
  let out = "";
  let pos = 0;
  let i = text.indexOf("[");
  while (i >= 0) {
    const close = text.indexOf("]", i + 1);
    if (close < 0) break;
    if (text[close + 1] !== "(") {
      i = text.indexOf("[", close + 1);
      continue;
    }
    const end = text.indexOf(")", close + 2);
    if (end < 0) break;
    const start = i > pos && text[i - 1] === "!" ? i - 1 : i;
    out += text.slice(pos, start) + text.slice(i + 1, close);
    pos = end + 1;
    i = text.indexOf("[", pos);
  }
  return out + text.slice(pos);
}

/** Markdown that can hide a word, gone: tags, links, `*`, backticks, `|`, `_` at a word edge. */
function cleanInline(text: string, narrow = false): string {
  const t = narrow
    ? text.replace(TAG_NARROW, "").replace(LINK_NARROW, "$1")
    : stripLinks(stripTags(text));
  return t.replace(/[*`]/gu, "").replaceAll("|", " ").replace(EDGE_UNDERSCORES, "");
}

/** One line cleaned, its leading quote, list, heading and fence markers gone. */
export const normLine = (line: string, narrow = false): string =>
  strip(cleanInline(line, narrow).replace(LEADING_MARKERS, ""));

const opensPlain = (line: string) => !/^(?: {4}|\t| {0,3}(?:>|`{3}|~{3}))/u.test(line);

/** One key per lens across rounds: the name lower-case, without a trailing round marker or emphasis. */
export function lensKey(name: string): string {
  const words = split(name.toLowerCase());
  let lo = 0;
  let hi = words.length;
  for (;;) {
    while (lo < hi && !words[lo]) lo++;
    while (lo < hi && !words[hi - 1]) hi--;
    if (lo === hi) return "unnamed";
    const before = [words[lo], words[hi - 1], hi];
    words[lo] = stripChars(words[lo]!, "*_`", true, false);
    words[hi - 1] = stripChars(words[hi - 1]!, "*_`", false, true);
    if (hi - lo >= 2 && words[hi - 1] && MARKER.test(words[hi - 1]!)) hi--;
    if (words[lo] === before[0] && words[hi - 1] === before[1] && hi === before[2]) {
      return words.slice(lo, hi).filter(Boolean).join(" ") || "unnamed";
    }
  }
}

/** The name has a letter or digit, is no `<...>` placeholder and is more than a round marker. */
function namesALens(name: string): boolean {
  const key = lensKey(name);
  return (
    new RegExp(ALNUM, "u").test(key) &&
    !/<[^<>]*>/u.test(name) &&
    !/^unnamed$/u.test(key) &&
    !MARKER.test(key)
  );
}

/** [name, sha or null] of a comment's first-line lens header, or null. */
export function lensHeader(body: string, narrow = false): [string, string | null] | null {
  const first = splitlines(body).find((line) => !isBlank(line)) ?? "";
  if (!opensPlain(first)) return null;
  const line = normLine(first, narrow);
  const rest = LENS_RE.exec(line)?.[1];
  if (rest === undefined) return null;
  const name = rest.slice(0, FIELD_RE.exec(rest)?.index ?? rest.length);
  if (!namesALens(name)) return null;
  return [name, READ_AT_RE.exec(line)?.[1]?.toLowerCase() ?? null];
}

/** [the fence open after this line or null, whether the line belongs to a fence], given the one open before it. */
export function fenceStep(fence: string | null, line: string): [string | null, boolean] {
  if (fence) {
    const m = /^ {0,3}(`{3,}|~{3,})[ \t]*$/u.exec(line);
    return [m && m[1]![0] === fence[0] && m[1]!.length >= fence.length ? null : fence, true];
  }
  const m = /^ {0,3}(?:(`{3,})[^`]*|(~{3,})[\s\S]*)$/u.exec(line);
  return m ? [m[1] ?? m[2]!, true] : [null, false];
}

/** A pull request comment or review whose author counts (a lens author association, no bot), in posting order. */
export interface LensPost {
  readonly body: string;
  /** GitHub's includesCreatedEdit: anything but a recorded false is an edit. */
  readonly edited: boolean;
  /** GitHub's isMinimized. */
  readonly hidden: boolean;
}

export interface LensScan {
  /** [lens name, sha or null] of every post with a lens header, old, edited and hidden ones too: the rounds. */
  readonly history: Array<[string, string | null]>;
  /** Each lens key's newest [sha or null, why it reads nothing or null]. */
  readonly entries: Record<string, [string | null, string | null]>;
}

/** lens.Review.lens_scan on posts that count. */
export function lensScan(posts: ReadonlyArray<LensPost>): LensScan {
  const history: Array<[string, string | null]> = [];
  const entries: Record<string, [string | null, string | null]> = {};
  posts.forEach((post, i) => {
    const header = lensHeader(post.body);
    if (JSON.stringify(header) !== JSON.stringify(lensHeader(post.body, true))) {
      entries[
        `${NO_HEADER} (post ${i + 1}: its first line reads as a different lens header, or as one and as none)`
      ] = [null, null];
      return;
    }
    if (!header) return;
    history.push(header);
    const why = post.hidden ? HIDDEN : post.edited ? EDITED : null;
    entries[lensKey(header[0])] = why ? [null, why] : [header[1], null];
  });
  return { history, entries };
}
