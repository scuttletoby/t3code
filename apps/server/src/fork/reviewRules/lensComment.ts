/**
 * Port of lens.py's lens rules (fork-ci/actions/pr-queue), held to the shared vectors and the gate's corpus. A lens
 * report (one with the lens marker) that is not one valid read is a NO_HEADER entry, and so is a look-alike without
 * the marker that holds an open finding; one with no open finding is no report (notLens). The caller picks the posts
 * that count, in the gate's order.
 */
import * as Schema from "effect/Schema";

import { fold, readFindings } from "./findings.ts";

import {
  ALNUM,
  B,
  S,
  WS,
  byCodePoint,
  cut,
  isBlank,
  LINE_END,
  LINE_ENDS,
  memo,
  nfkc,
  PyTextError,
  pyI,
  pyUnicodeCheck,
  repr,
  split,
  splitlines,
  strip,
  stripChars,
} from "./pyText.ts";

const isPyTextError = Schema.is(PyTextError);
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
// lens_problem's patterns (lens.py): Python's re.I `[a-z]` also holds `İ` and `ı`.
const DASHES = `${/\u2014/u.source}${/\u2013/u.source}`;
const FIELD_START_RE = new RegExp(`^lens(?:${S}*[:${DASHES}]|=)${S}*([^\n]*)`, "iu");
const LOOSE_RE = new RegExp(`${B}lens${S}*[:=${DASHES}]([^\n]*)`, "iu");
const NOT_A_LENS_SUB = new RegExp(`${B}not${S}+a${S}+lens${S}*:`, "giu");
const NOT_A_LENS_RE = new RegExp(`^not${S}+a${S}+lens${S}*:`, "iu");
const LOOKS_RE = new RegExp(
  `${B}lens${S}*:|${B}read[${WS}-]*at${B}${S}*(?::|[0-9a-f]{7})|${B}safe${S}+to${S}+merge${B}`,
  "iu",
);
const FIELD_LINE_RE = new RegExp(`^(?:covered|checks)${S}*:`, "iu");
// lens.looks_heading: the heading marks as GitHub reads them, on the raw line (a full-width `＃` is no heading, as
// GitHub renders it as plain text), then the lens word in NFKC.
const HEADING_START_RE = /^ {0,3}#{1,6}[ \t]/u;
const LENS_WORD_RE = new RegExp(`${B}lens${B}`, "iu");
const HEX_RE =
  /(?<![0-9a-z\u0130\u0131])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}(?![0-9a-z\u0130\u0131])/iu;
const TOP_LINES = 5;

export const NO_HEADER = "comment without a first-line lens header";
export const EDITED = "edited after posting: post a new comment instead";
export const HIDDEN = "hidden: post a new comment instead";
const FLAGS_UNREAD =
  "a review whose edit and hidden flags were not read from GitHub: re-run, or post a new comment";
export const EMBEDDED = "holds a lens header line";
/** The one way out of a held comment (lens.HELD_WAY_OUT): delete it or opt it out. */
const HELD_WAY_OUT = "delete the old one or edit it to start with `not a lens` and a colon";
/**
 * lens.LOOKALIKE: a report whose header or marker shows only once folded as GitHub renders it (lookalikeProblem): a
 * broken report, never a read and never silent. Keyed by its comment, so a new comment alone does not clear it: its
 * reason gives the held reasons' way out too.
 */
const LOOKALIKE =
  "reads as a lens report only with its look-alike letters, invisible characters, HTML references or backslash " +
  `escapes read as GitHub shows them (post a NEW comment in plain letters, and ${HELD_WAY_OUT})`;
export const NO_MARKER =
  "no lens marker (no line starts with `lens:`, and no `read at` or lens field names a sha)";
/** How a lens fixes a report without the marker (lens.REPOST): every reason that names a look-alike says it. */
export const REPOST =
  "post a NEW comment whose first line is `lens: <name> · reviewer: <who> · read at: <sha>` (editing the " +
  "marker into an old comment does not count)";
/**
 * reportProblem's kinds (lens.BROKEN, HELD, NOT_LENS): a lens report (with the marker) that is not one valid read; a
 * comment without the marker that holds an open finding (both block); one without either (no report, never blocks).
 */
export type ReportKind = "broken" | "held" | "notLens";
const OPT_OUT_READ =
  "says on its first line that it is no lens report, but holds a hex read-at sha outside a `>` quote";

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

/** One line in NFKC, cleaned, its leading quote, list, heading and fence markers gone (lens.norm_line). */
export const normLine: (line: string, narrow?: boolean) => string = memo((line, narrow) =>
  strip(cleanInline(nfkc(line), narrow).replace(LEADING_MARKERS, "")),
);

/** lens.looks_heading: a Markdown heading as GitHub renders one whose text holds the word lens, read in NFKC. */
function looksHeading(line: string): boolean {
  const m = HEADING_START_RE.exec(line);
  return m !== null && LENS_WORD_RE.test(nfkc(line.slice(m[0].length)));
}

const opensPlain = (line: string) => !/^(?: {4}|\t| {0,3}(?:>|`{3}|~{3}))/u.test(line);

/** One key per lens across rounds: the name lower-case, without a trailing round marker or emphasis. */
export function lensKey(name: string): string {
  pyUnicodeCheck(name);
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
export function namesALens(name: string): boolean {
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
  pyUnicodeCheck(body);
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

/** The lines outside fences, 4-space/tab indented blocks and quotes (lens.plain_lines). */
function plainLines(body: string): string[] {
  const out: string[] = [];
  let fence: string | null = null;
  for (const line of splitlines(body)) {
    let fenced: boolean;
    [fence, fenced] = fenceStep(fence, line);
    if (!fenced && !isBlank(line) && !/^(?: {4}|\t| {0,3}>)/u.test(line)) out.push(line);
  }
  return out;
}

/** The lines except those of a `>` quote outside a fence (lens.unquoted_lines). */
function unquotedLines(body: string): string[] {
  const out: string[] = [];
  let fence: string | null = null;
  for (const line of splitlines(body)) {
    let fenced: boolean;
    [fence, fenced] = fenceStep(fence, line);
    if (!fenced && /^ {0,3}>/u.test(line)) continue;
    out.push(line);
  }
  return out;
}

const headerLine = (line: string) => LENS_RE.test(line) && READ_AT_RE.test(line);

/** A lens field anywhere in the line naming a hex sha after it (lens.loose_header). */
function looseHeader(line: string): boolean {
  const m = LOOSE_RE.exec(line.replace(NOT_A_LENS_SUB, " "));
  return !!m && HEX_RE.test(m[1]!);
}

/** lens.marker_line: the line starts with the lens field, names a read (`read at <hex>`) or a lens field with a sha. */
const markerLine = (line: string) =>
  FIELD_START_RE.test(line) || READ_AT_RE.test(line) || looseHeader(line);

/**
 * lens.has_marker: a line of the comment, in a code block or quote too, read either way, carries the lens marker.
 * Only such a comment is a lens report (COMMENTS.md, "The lens marker").
 */
export function hasMarker(body: string): boolean {
  pyUnicodeCheck(body);
  if (marked(body)) return true;
  const folded = foldText(body);
  return folded !== body && marked(folded);
}

/**
 * lens.fold_text: fold, line by line. A line end an HTML reference writes (`&#10;`, `&#13;`, `&#x2028;`) reads as a
 * space, as GitHub shows it, never as a new line.
 */
function foldText(body: string): string {
  return body
    .split(LINE_END)
    .map((part, i) => (i % 2 === 1 ? part : fold(part).replace(LINE_ENDS, " ")))
    .join("");
}

/** hasMarker on the text as written (lens._marked). */
function marked(body: string): boolean {
  const lines = splitlines(body);
  return [false, true].some((narrow) => lines.some((line) => markerLine(normLine(line, narrow))));
}

const starts = (first: string) => `starts with ${repr(cut(strip(first), 60))}`;

/** lens.Review.read_problem: lensProblem's rules with one way of reading tags and links. */
function readProblem(body: string, narrow: boolean): string | null {
  const norm = (line: string) => normLine(line, narrow);
  const lines = splitlines(body);
  const at = Math.max(
    0,
    lines.findIndex((line) => !isBlank(line)),
  );
  const first = lines.find((line) => !isBlank(line)) ?? "";
  const normed = lines.map(norm);
  const start = starts(first);
  let headers = 0;
  for (const [i, line] of normed.entries()) {
    const m = i > at ? FIELD_START_RE.exec(line) : null;
    if (headerLine(line) || (m && HEX_RE.test(m[1]!))) headers++;
  }
  if (headers > 1) return `${EMBEDDED} ${headers} times (post one comment per lens), ${start}`;
  if (lensHeader(body, narrow)) return null;
  if (opensPlain(first) && LENS_RE.test(norm(first)))
    return `its first-line header names no lens (a placeholder, a round or nothing), ${start}`;
  if (headers)
    return `${EMBEDDED} that is not its first plain line (a later line, a code block, quote or table), ${start}`;
  if (normed.some(looseHeader))
    return `${EMBEDDED} (a lens line with a sha not written \`read at: <sha>\`), ${start}`;
  if (opensPlain(first) && NOT_A_LENS_RE.test(norm(first))) {
    return unquotedLines(body).some((line) => READ_AT_RE.test(norm(line)))
      ? `${OPT_OUT_READ}, ${start}`
      : null;
  }
  const plain = plainLines(body);
  if (plain.filter((line) => FIELD_LINE_RE.test(norm(line))).length >= 2) return start;
  if (
    normed.some((line) => READ_AT_RE.test(line)) ||
    plain.some((line) => LOOKS_RE.test(norm(line))) ||
    plain.slice(0, TOP_LINES).some(looksHeading)
  )
    return start;
  return null;
}

/**
 * lens.Review.report_problem: [why, kind] for a look-alike (looksProblem), null otherwise. Blocks with the marker
 * (broken), or without it when readFindings sees an open finding (held: an older clean read must not win). Otherwise
 * no lens report (notLens): named, never blocking.
 */
export function reportProblem(
  body: string,
  overridePrefix?: string,
): [why: string, kind: ReportKind] | null {
  const problem = looksProblem(body, overridePrefix);
  if (!problem) {
    const lookalike = lookalikeProblem(body, overridePrefix);
    return lookalike ? [lookalike, "broken"] : null;
  }
  if (hasMarker(body)) return [problem, "broken"];
  const found = readFindings(body, true)[0]; // no header: the title line and NOT SAFE prose count
  if (found.length > 0) {
    return [
      `${NO_MARKER}, but it holds an open ${found[0]![0]}: ${REPOST}, and ${HELD_WAY_OUT}; it looks like one: ` +
        problem,
      "held",
    ];
  }
  return [`${NO_MARKER}; it looks like one: ${problem}`, "notLens"];
}

/**
 * lens.Review.lookalike_problem: why a comment that, as written, is one valid read or no report at all, is a report
 * only once folded as GitHub renders it (fold), or null: the folded text has a first-line header and the text as
 * written none, or none of the same sha, or the folded text carries the marker and looksProblem flags it. The folded
 * text is read for this only: never for a read or a round.
 */
function lookalikeProblem(body: string, overridePrefix?: string): string | null {
  const folded = foldText(body);
  if (folded === body || isOverride(body, overridePrefix)) return null;
  const same = [false, true].every((narrow) => {
    const raw = lensHeader(body, narrow);
    const read = lensHeader(folded, narrow);
    return (raw === null) === (read === null) && (raw === null || raw[1] === read![1]);
  });
  if (same && !(marked(folded) && looksProblem(folded, overridePrefix))) return null;
  return `${LOOKALIKE}, ${starts(splitlines(body).find((line) => !isBlank(line)) ?? "")}`;
}

const isOverride = (body: string, overridePrefix?: string): boolean =>
  overridePrefix !== undefined &&
  body.startsWith(`${overridePrefix} `) &&
  /^[0-9a-f]{40}:/u.test(body.slice(overridePrefix.length + 1));

/**
 * lens.Review.lens_problem: why a comment that looks like a lens report blocks (reportProblem kind broken or held), or
 * null: it is one valid read, does not look like a report, or is no lens report (notLensReport).
 */
export function lensProblem(body: string, overridePrefix?: string): string | null {
  const got = reportProblem(body, overridePrefix);
  return got && got[1] !== "notLens" ? got[0] : null;
}

/**
 * lens.Review.not_lens_report: why a comment that looks like a lens report is none (no lens marker and no open
 * finding: reportProblem kind notLens), or null.
 */
export function notLensReport(body: string, overridePrefix?: string): string | null {
  const got = reportProblem(body, overridePrefix);
  return got && got[1] === "notLens" ? got[0] : null;
}

/**
 * lens.Review.looks_problem: why a comment that looks like a lens report is not one valid read, with or without the
 * marker, or null. Every line counts, in fences and quotes too; read both ways (narrow or not). An override comment
 * (`<prefix> <40-hex sha>:`) never counts.
 */
export function looksProblem(body: string, overridePrefix?: string): string | null {
  pyUnicodeCheck(body);
  if (isOverride(body, overridePrefix)) return null;
  const problem = readProblem(body, false) ?? readProblem(body, true);
  if (
    problem === null &&
    JSON.stringify(lensHeader(body)) !== JSON.stringify(lensHeader(body, true))
  ) {
    return (
      "its first line reads as a different lens header, or as one and as none, depending on how its HTML " +
      `tags and links are read; ${starts(splitlines(body).find((line) => !isBlank(line)) ?? "")}`
    );
  }
  return problem;
}

/**
 * A pull request comment, review or inline review comment whose author counts (a lens author association, no bot),
 * in the gate's list order (lens.Review.lens_posts): the comments, then the reviews, then the inline comments.
 */
export interface LensPost {
  readonly body: string;
  /** GitHub's includesCreatedEdit: anything but a recorded false is an edit. */
  readonly edited: boolean;
  /** GitHub's isMinimized. */
  readonly hidden: boolean;
  /**
   * How the gate names the post in a NO_HEADER key: its url, else `by <login> at <when or ?>`. A key holds the post's
   * text, so a caller never matches on a key's text: it reads `NO_HEADER` entries by their prefix only.
   */
  readonly where: string;
  /** createdAt of a comment or inline comment, submittedAt of a review, or "": with the list index, the post's age. */
  readonly when: string;
  /** A review whose edit and hidden flags were not read (lens.UNREAD): it reads nothing (FLAGS_UNREAD). */
  readonly unread?: boolean;
}

/** A post's age as the gate orders it: [when, list index], `when` by code point first. */
export type PostAt = readonly [when: string, index: number];

/** a is older than b (Python's tuple `<`). */
export const olderThan = (a: PostAt, b: PostAt): boolean => {
  const by = byCodePoint(a[0], b[0]);
  return by < 0 || (by === 0 && a[1] < b[1]);
};

export interface LensEntry {
  readonly at: PostAt;
  readonly sha: string | null;
  readonly why: string | null;
  /** The lens name of its newest header; null for a NO_HEADER entry. */
  readonly name: string | null;
  /** The post's body: review_judge reads the newest report's findings from it. */
  readonly body: string;
}

export interface LensScan {
  /** [lens name, sha or null] of every post with a lens header, old, edited and hidden ones too: the rounds. */
  readonly history: Array<[string, string | null]>;
  /** Each lens key's newest [sha or null, why it reads nothing or null]. */
  readonly entries: Record<string, [string | null, string | null]>;
  /**
   * [where, why] of each post that looks like a report, has no lens marker and no open finding
   * (lens.Review.not_lens_reports).
   */
  readonly notLens: Array<[string, string]>;
}

/**
 * lens.Review.lens_scan with each entry's age: the newest post per lens key by [when, index], `index` the post's place
 * in `posts`. `skip` leaves a post out (an override) but keeps its index. History stays in list order. A post the
 * rules cannot read as Python does throws PyTextError naming that post (`where`).
 */
export function lensEntries(
  posts: ReadonlyArray<LensPost>,
  overridePrefix?: string,
  skip: (post: LensPost) => boolean = () => false,
): {
  history: Array<[string, string | null]>;
  entries: Map<string, LensEntry>;
  notLens: Array<[string, string]>;
  /** Every post with a lens header, as its entry would be, with its lens key, in post order: the newest is in `entries`. */
  headers: Array<LensEntry & { readonly key: string }>;
} {
  const history: Array<[string, string | null]> = [];
  const entries = new Map<string, LensEntry>();
  const headers: Array<LensEntry & { readonly key: string }> = [];
  const notLens: Array<[string, string]> = [];
  posts.forEach((post, i) => {
    if (skip(post)) return;
    const at: PostAt = [post.when, i];
    const { body } = post;
    const hidden = !post.unread && post.hidden;
    let got: [string, ReportKind] | null;
    let header: [string, string | null] | null;
    let key: string;
    try {
      got = reportProblem(body, overridePrefix);
      header = got ? null : lensHeader(body);
      key = header ? lensKey(header[0]) : "";
    } catch (error) {
      if (isPyTextError(error) && error.where === undefined)
        throw new PyTextError({ codePoints: error.codePoints, where: post.where });
      throw error;
    }
    if (got?.[1] === "notLens") {
      notLens.push([post.where, got[0]]);
      return;
    }
    if (got) {
      key = `${NO_HEADER} (${post.where}, ${got[0]}${hidden ? ", hidden" : ""})`;
      entries.set(key, { at, sha: null, why: null, name: null, body });
      return;
    }
    if (!header) return;
    history.push(header);
    const why = post.unread ? FLAGS_UNREAD : hidden ? HIDDEN : post.edited ? EDITED : null;
    const entry: LensEntry = { at, sha: why ? null : header[1], why, name: header[0], body };
    headers.push({ ...entry, key });
    const was = entries.get(key);
    if (was && olderThan(at, was.at)) return;
    entries.set(key, entry);
  });
  return { history, entries, notLens, headers };
}

/** lens.Review.lens_scan on posts that count, with the repository's override prefix. */
export function lensScan(posts: ReadonlyArray<LensPost>, overridePrefix?: string): LensScan {
  const { history, entries, notLens } = lensEntries(posts, overridePrefix);
  const out: Record<string, [string | null, string | null]> = {};
  for (const [key, { sha, why }] of entries) out[key] = [sha, why];
  return { history, entries: out, notLens };
}
