/**
 * Port of review_judge.read_findings (fork-ci/actions/pr-queue), held to the shared vectors. A report has a findings
 * section only with both a blocker and a major label: with one alone, a major in a form not read would pass as clean.
 */
import { fenceStep, lensHeader, normLine } from "./lensComment.ts";
import {
  ALNUM,
  B,
  S,
  WS,
  cut,
  htmlUnescape,
  isBlank,
  memo,
  nfkc,
  pyI,
  pyUnicodeCheck,
  splitlines,
  strip,
  stripChars,
} from "./pyText.ts";

const CONFUSABLE_FROM = "АВЕКМНОРСТХІЈЅаеорсхуіјѕкԁӏһԛԝΑΒΕΖΗΙΚΜΝΟΡΤΥΧοικναρıℓǀ∶";
const CONFUSABLE_TO = "ABEKMHOPCTXIJSaeopcxyijskdlhqwABEZHIKMNOPTYXoikvapill:";

// The dashes as regex escapes: a pattern needs them, copy must not.
const EM = /\u2014/u.source;
const EN = /\u2013/u.source;
const kw = (group: string) => `(?<${group}>bl[o0]ck(?:ers?|ing)|ma[j][o0]rs?|m[i1]n[o0]rs?|nits?)`;
const label = (group: string) =>
  `${kw(group)}(?!${ALNUM})(?:${S}+(?:findings?|issues?|problems?|items?|points?|bugs?|ones?)(?!${ALNUM}))?` +
  `(?:${S}*#?\\p{Nd}{1,4}(?!${ALNUM}))?(?:${S}*\\([^()\\n]{0,80}\\))?`;
const SEP = `${S}*(?::|[${EM}${EN}=]|-(?=${S}|$))`;
const LABEL = `(?:^|(?<=[,;.!?|/])${S}*|${S}+(?:and|but)${S}+)(?:\\[${label("kw")}\\]|\\(${label("kw2")}\\)|${label("kw3")}${SEP})`;
const re = (source: string, flags = "iu") => new RegExp(pyI(source), flags);
const LABEL_RE = re(LABEL, "giu");
const LABEL_AT_START = re(`^(?:${LABEL})`);
const BARE_RE = re(`^[\\[(]?${label("kw")}[\\])]?${SEP}?${S}*$`);
const CELL_RE = re(`^${label("kw")}${S}*:?${S}*$`);
const NONE_RE = re(
  `^[(\\[]?${S}*(?:none|nothing|nil|no|zero|0|n/?a|-+|${EM}|${EN})(?:${S}+(?:found|open|left|remaining|seen|here|known|` +
    `so${S}+far|at${S}+all|this${S}+round|blocking|blockers?|majors?|findings?|issues?|problems?))*${S}*` +
    `(?:$|[.,;:!?)(\\[]|[^\\p{L}\\p{N}_${WS}\\x00-\\x7f]|-(?=${S}|$))`,
);
const FINDING_LINE_RE = re(`^findings?${S}*:${S}*[\\[(]?${kw("kw")}(?!${ALNUM})`);
const FINDINGS_NONE_RE = re(
  `^(?:findings|issues|problems)${S}*:${S}*(?:none|nothing|no|0|n/?a)${S}*\\.?${S}*$`,
);
const NOT_SAFE_RE = re(`${B}not${S}+safe${B}`);
/** readFindings(unmarked): `not safe to merge` on any line (review_judge.NOT_SAFE_TO_MERGE_RE). */
const NOT_SAFE_TO_MERGE_RE = re(`${B}not${S}+safe${S}+to${S}+merge${B}`);
const VERDICT_NOT_SAFE_RE = re(`^verdict${S}*[:${EM}${EN}-]?${S}*not${S}+safe${B}`);
const RULE_LINE_RE = new RegExp(`^${S}*(?:[-*_]${S}*){3,}$`, "u");
const HEADING_RE = new RegExp(`^ {0,3}#{1,6}(?:${S}|$)`, "u");
const TABLE_RE = new RegExp(`^${S}*\\|`, "u");
const TABLE_RULE_RE = new RegExp(`^\\|?${S}*:?-+:?${S}*(?:\\|${S}*:?-+:?${S}*)*\\|?$`, "u");
const LEAD_PLAIN = /^[^\p{L}\p{N}_[(]+/u;
const LEAD_NONE = /^[^\p{L}\p{N}_\-\u2014\u2013([]+/u;
const WS_RUN = new RegExp(`${S}+`, "gu");
const SHOW_FINDING = 120;

export type FindingKind = "blocker" | "major";
type Kind = FindingKind | "minor";
const blocking = (kind: Kind): kind is FindingKind => kind !== "minor";

/** The text as the findings rules read it. */
export function fold(text: string | null | undefined): string {
  pyUnicodeCheck(text ?? "");
  return folded(text ?? "");
}

/**
 * fold, remembered (pyText.memo). The Unicode check runs again on the text the HTML references write: `&#x1C89;` is
 * read as U+1C89, so it drifts as the letter itself does.
 */
const folded = memo((text) => {
  const unescaped = htmlUnescape(nfkc(text));
  pyUnicodeCheck(unescaped);
  const t = unescaped.replace(/\\([!-/:-@[-`{-~])/gu, "$1").replace(/\p{Cf}/gu, "");
  return Array.from(t, (ch) => {
    const k = CONFUSABLE_FROM.indexOf(ch);
    return k < 0 ? ch : CONFUSABLE_TO[k]!;
  }).join("");
});

function kindOf(m: RegExpExecArray | RegExpMatchArray): Kind {
  const word = (m.groups!.kw ?? m.groups!.kw2 ?? m.groups!.kw3)!
    .toLowerCase()
    .replaceAll("0", "o")
    .replaceAll("1", "i");
  return word.startsWith("block") ? "blocker" : word.startsWith("major") ? "major" : "minor";
}

const plain = (line: string, narrow: boolean) =>
  strip(normLine(fold(line), narrow).replace(LEAD_PLAIN, "").replace(WS_RUN, " "));
const saysNothing = (text: string) => NONE_RE.test(strip(text).replace(LEAD_NONE, ""));

/** The first content line under a bare label, or null when a heading, another label or the end comes first. */
function under(lines: ReadonlyArray<string>, start: number, narrow: boolean): string | null {
  for (let i = start; i < lines.length; i++) {
    const raw = lines[i]!;
    if (isBlank(raw) || RULE_LINE_RE.test(raw)) continue;
    if (HEADING_RE.test(raw)) return null;
    const line = plain(raw, narrow);
    if (BARE_RE.test(line) || LABEL_AT_START.test(line)) return null;
    return line || strip(raw);
  }
  return null;
}

type Read = { out: Array<[number, FindingKind, string]>; seen: Set<Kind> };

function read(lines: ReadonlyArray<string>, narrow: boolean): Read {
  const out: Read["out"] = [];
  const seen = new Set<Kind>();
  let fence: string | null = null;
  let columns: Map<number, Kind> | null = null;
  for (const [i, raw] of lines.entries()) {
    let fenced: boolean;
    [fence, fenced] = fenceStep(fence, raw);
    if (fenced) {
      columns = null;
      continue;
    }
    if (TABLE_RE.test(raw)) {
      const cells = stripChars(strip(fold(raw)), "|")
        .split("|")
        .map((c) => plain(strip(c), narrow));
      if (i + 1 < lines.length && TABLE_RULE_RE.test(strip(lines[i + 1]!))) {
        columns = new Map();
        for (const [j, c] of cells.entries()) {
          const m = CELL_RE.exec(c);
          if (m) seen.add(columns.set(j, kindOf(m)).get(j)!);
        }
        continue;
      }
      if (TABLE_RULE_RE.test(strip(raw))) continue;
      // Once per row: per cell, a wide row took minutes. after[j]: the first non-empty cell after cell j.
      const row = plain(raw, narrow);
      const after: string[] = Array.from({ length: cells.length });
      let said = "";
      for (let j = cells.length - 1; j >= 0; j--) {
        after[j] = said;
        said = cells[j] || said;
      }
      for (const [j, c] of cells.entries()) {
        const m = CELL_RE.exec(c);
        const kind = m && kindOf(m);
        if (kind && blocking(kind)) {
          seen.add(kind);
          if (!saysNothing(after[j]!)) out.push([i, kind, row]);
        }
      }
      for (const [j, kind] of columns ?? []) {
        if (blocking(kind) && j < cells.length && !saysNothing(cells[j]!)) out.push([i, kind, row]);
      }
      continue;
    }
    columns = null;
    const line = plain(raw, narrow);
    if (FINDINGS_NONE_RE.test(line)) {
      seen.add("blocker").add("major");
      continue;
    }
    const finding = FINDING_LINE_RE.exec(line);
    if (finding) {
      seen.add("blocker").add("major");
      const kind = kindOf(finding);
      if (blocking(kind)) out.push([i, kind, line]);
      continue;
    }
    const bare = BARE_RE.exec(line);
    if (bare) {
      const kind = kindOf(bare);
      if (!blocking(kind)) continue;
      seen.add(kind);
      const said = under(lines, i + 1, narrow);
      if (said === null || !saysNothing(said)) {
        out.push([i, kind, said === null ? `${line} (nothing under it)` : `${line} ${said}`]);
      }
      continue;
    }
    const marks = [...line.matchAll(LABEL_RE)];
    for (const [n, m] of marks.entries()) {
      const kind = kindOf(m);
      if (!blocking(kind)) continue;
      seen.add(kind);
      if (!saysNothing(line.slice(m.index + m[0].length, marks[n + 1]?.index ?? line.length))) {
        out.push([i, kind, line]);
        break;
      }
    }
  }
  return { out, seen };
}

/**
 * review_judge.read_findings: [[kind, line] of each blocker and major in line order, has a findings section]. A first
 * line (the header or its title line) or a `verdict:` line saying NOT SAFE is a blocker. unmarked (no lens marker): the
 * first two lines are read as heading and title, and `not safe to merge` outside a fence is a blocker on any line.
 */
export function readFindings(
  body: string,
  unmarked = false,
): [Array<[FindingKind, string]>, boolean] {
  pyUnicodeCheck(body);
  const lines = splitlines(body);
  const found = new Map<number, [FindingKind, string]>();
  const seen = new Set<Kind>();
  for (const narrow of [false, true]) {
    const r = read(lines, narrow);
    r.seen.forEach((k) => seen.add(k));
    for (const [i, kind, text] of r.out)
      if (!found.has(i)) found.set(i, [kind, cut(text, SHOW_FINDING)]);
  }
  const top = [...lines.keys()].filter((i) => !isBlank(lines[i]!)).slice(0, 2);
  const first = new Set(unmarked || lensHeader(body) ? top : top.slice(0, 1));
  let fence: string | null = null;
  for (const [i, raw] of lines.entries()) {
    let fenced: boolean;
    [fence, fenced] = fenceStep(fence, raw);
    if (fenced || found.has(i)) continue;
    const line = plain(raw, false);
    if (
      (first.has(i) && NOT_SAFE_RE.test(line)) ||
      VERDICT_NOT_SAFE_RE.test(line) ||
      (unmarked && NOT_SAFE_TO_MERGE_RE.test(line))
    ) {
      found.set(i, ["blocker", cut(`the report says NOT SAFE: ${line}`, SHOW_FINDING)]);
    }
  }
  const said = [...found.keys()].sort((a, b) => a - b).map((i) => found.get(i)!);
  return [said, seen.has("blocker") && seen.has("major")];
}
