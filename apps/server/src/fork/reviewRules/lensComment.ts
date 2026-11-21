/**
 * Port of lens.py's header rules and lens.Review.lens_problem/lens_scan (fork-ci/actions/pr-queue), held to the shared
 * vectors and the gate's lens corpus. A comment that only looks like a report, or holds a second header, is a
 * NO_HEADER entry that reads nothing, as in the gate. The caller picks the posts that count, in the gate's order.
 */
import {
  ALNUM,
  B,
  S,
  WS,
  cut,
  isBlank,
  pyI,
  repr,
  split,
  splitlines,
  strip,
  stripChars,
} from "./pyText.ts";

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
const LOOKS_HEADING_RE = new RegExp(`^ {0,3}#{1,6}[ \t][^\n]*${B}lens${B}`, "iu");
const HEX_RE =
  /(?<![0-9a-z\u0130\u0131])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}(?![0-9a-z\u0130\u0131])/iu;
const TOP_LINES = 5;

export const NO_HEADER = "comment without a first-line lens header";
export const EDITED = "edited after posting: post a new comment instead";
export const HIDDEN = "hidden: post a new comment instead";
export const EMBEDDED = "holds a lens header line";
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
    plain.slice(0, TOP_LINES).some((line) => LOOKS_HEADING_RE.test(line))
  )
    return start;
  return null;
}

/**
 * lens.Review.lens_problem: why a comment that looks like a lens report is not one valid read, or null. Every line
 * counts, in fences and quotes too; read both ways (narrow or not). An override comment (`<prefix> <40-hex sha>:`)
 * never counts.
 */
export function lensProblem(body: string, overridePrefix?: string): string | null {
  if (
    overridePrefix !== undefined &&
    body.startsWith(`${overridePrefix} `) &&
    /^[0-9a-f]{40}:/u.test(body.slice(overridePrefix.length + 1))
  )
    return null;
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

/** A pull request comment or review whose author counts (a lens author association, no bot), in posting order. */
export interface LensPost {
  readonly body: string;
  /** GitHub's includesCreatedEdit: anything but a recorded false is an edit. */
  readonly edited: boolean;
  /** GitHub's isMinimized. */
  readonly hidden: boolean;
  /** How the gate names the post in a NO_HEADER key: its url, else `by <login> at <createdAt>`. Default `post <n>`. */
  readonly where?: string;
}

export interface LensScan {
  /** [lens name, sha or null] of every post with a lens header, old, edited and hidden ones too: the rounds. */
  readonly history: Array<[string, string | null]>;
  /** Each lens key's newest [sha or null, why it reads nothing or null]. */
  readonly entries: Record<string, [string | null, string | null]>;
}

/** lens.Review.lens_scan on posts that count, with the repository's override prefix. */
export function lensScan(posts: ReadonlyArray<LensPost>, overridePrefix?: string): LensScan {
  const history: Array<[string, string | null]> = [];
  const entries: Record<string, [string | null, string | null]> = {};
  posts.forEach((post, i) => {
    const problem = lensProblem(post.body, overridePrefix);
    if (problem) {
      const where = post.where ?? `post ${i + 1}`;
      entries[`${NO_HEADER} (${where}, ${problem}${post.hidden ? ", hidden" : ""})`] = [null, null];
      return;
    }
    const header = lensHeader(post.body);
    if (!header) return;
    history.push(header);
    const why = post.hidden ? HIDDEN : post.edited ? EDITED : null;
    entries[lensKey(header[0])] = why ? [null, why] : [header[1], null];
  });
  return { history, entries };
}
