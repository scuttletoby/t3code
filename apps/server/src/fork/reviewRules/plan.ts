/**
 * Port of review_glob.py and review_judge.judge (fork-ci/actions/pr-queue), held to the shared vectors. Input it
 * cannot read, or a path no rule matches, throws ReviewPlanError: never "no lens needed".
 */
import * as Schema from "effect/Schema";

import { strip } from "./pyText.ts";

export type ReviewWeight = "critical" | "normal" | "light";
const WEIGHTS: ReadonlyArray<ReviewWeight> = ["critical", "normal", "light"];

/** A `review` block of the repo's pr-queue config, as the gate's parser accepts it. */
export interface ReviewBlock {
  /** shadow: the gate requires only its required-lenses input; enforce: the path rules' lenses join it. */
  readonly mode?: "shadow" | "enforce";
  readonly rules: ReadonlyArray<{
    readonly name: string;
    readonly weight: ReviewWeight;
    readonly paths: ReadonlyArray<string>;
    readonly lenses: ReadonlyArray<string>;
  }>;
  readonly lenses: Readonly<Record<string, { readonly prompt: string }>>;
  readonly rounds: {
    readonly approach_at: number;
    readonly max: number;
    readonly approach_lens: string;
  };
  readonly findings: boolean;
  readonly override_authors?: ReadonlyArray<string>;
}

export class ReviewPlanError extends Schema.TaggedError<ReviewPlanError>()("ReviewPlanError", {
  reason: Schema.String,
}) {
  override get message(): string {
    return this.reason;
  }
}

function refuse(reason: string): never {
  throw new ReviewPlanError({ reason });
}

/** One glob segment (no `**`) against one path segment, by code point: the wildcard walk, never exponential. */
function segmentMatches(glob: ReadonlyArray<string>, seg: ReadonlyArray<string>): boolean {
  let i = 0;
  let j = 0;
  let star = -1;
  let mark = 0;
  while (j < seg.length) {
    if (i < glob.length && glob[i] === "*") {
      star = i++;
      mark = j;
    } else if (i < glob.length && (glob[i] === "?" || glob[i] === seg[j])) {
      i++;
      j++;
    } else if (star >= 0) {
      i = star + 1;
      j = ++mark;
    } else return false;
  }
  while (i < glob.length && glob[i] === "*") i++;
  return i === glob.length;
}

/** The glob matches the whole repo-relative path: `**` takes any number of whole segments, `*` and `?` stay in one. */
export function globMatches(glob: string, path: string): boolean {
  const p = path.split("/").map((s) => Array.from(s));
  let reach = [true, ...p.map(() => false)];
  for (const seg of glob.split("/")) {
    if (seg === "**") {
      let seen = false;
      reach = reach.map((r) => (seen ||= r));
    } else {
      const g = Array.from(seg);
      reach = [false, ...p.map((s, j) => reach[j]! && segmentMatches(g, s))];
    }
    if (!reach.includes(true)) return false;
  }
  return reach[p.length]!;
}

const WORKFLOWS = ".github/workflows/**";

/** review_config.matches_every_path: a `**` segment and at most one other, which matches any segment. */
function matchesEveryPath(glob: string): boolean {
  const segs = glob.split("/");
  const rest = segs.filter((s) => s !== "**");
  const any = (s: string) => /^[*?]*$/u.test(s) && s.includes("*") && s.split("?").length <= 2;
  return rest.length < segs.length && rest.length <= 1 && rest.every(any);
}

const catchAll = (review: ReviewBlock) => review.rules.find((r) => r.paths.some(matchesEveryPath));

export interface ReviewPlan {
  readonly weight: ReviewWeight;
  /** The required lens names, in rule order, each once. */
  readonly lenses: ReadonlyArray<string>;
  /** Every rule a changed path counted for, in rule order, with those paths in change order. */
  readonly hits: ReadonlyArray<{
    readonly rule: string;
    readonly weight: ReviewWeight;
    readonly paths: string[];
  }>;
  /** The changed paths that are built-in protected: the config file, a lens prompt, a workflow. */
  readonly protected: ReadonlyArray<string>;
}

/**
 * The plan for `changes` as `git diff --name-status -M` names them ([status, path] or [R|C<score>, old, new]) under a
 * valid `review` block; `configPath` is the config file (repo-relative). A rename or copy counts under both paths; a
 * protected path also counts for the first critical rule; the heaviest weight hit is the weight; the lenses are those
 * of every rule hit, in rule order, each once; no change at all counts as the catch-all rule.
 */
export function planReview(
  review: ReviewBlock,
  changes: ReadonlyArray<ReadonlyArray<string>>,
  configPath: string,
): ReviewPlan {
  const paths: string[] = [];
  const seen = new Set<string>();
  for (const [status = "", ...named] of changes) {
    // review_judge.parse_name_status takes `status[1:].isdigit()`, which also holds `²` or `٣`; git writes a score in
    // ASCII digits only, so this reads every status git writes alike, and refuses (no plan, no lens) the rest.
    const two = /^[CR][0-9]{1,3}$/u.test(status);
    if (!two && !/^[ADMT]$/u.test(status))
      refuse(`unknown change status ${JSON.stringify(status)}`);
    if (named.length !== (two ? 2 : 1) || named.includes("")) {
      refuse(`the status ${status} without its paths`);
    }
    for (const p of named) if (!seen.has(p) && seen.add(p)) paths.push(p);
  }
  const rel = configPath.startsWith("./") ? configPath.slice(2) : configPath;
  const segs = rel.split("/");
  if (
    !rel ||
    strip(rel) !== rel ||
    /[\n\r]/u.test(rel) ||
    rel.startsWith("/") ||
    segs.some((s) => /^\.{0,2}$/u.test(s))
  ) {
    refuse(`the config path ${JSON.stringify(configPath)} is not written as git names the file`);
  }
  const exact = new Set([rel, ...Object.values(review.lenses).map((l) => l.prompt)]);
  const firstCritical = review.rules.find((r) => r.weight === "critical");
  const counted = new Map<string, string[] | null>(review.rules.map((r) => [r.name, []]));
  const isProtected: string[] = [];
  if (paths.length === 0) {
    const rule = catchAll(review);
    if (!rule) refuse("no changed path, and no catch-all rule to judge an empty change by");
    counted.set(rule.name, null);
  }
  for (const p of paths) {
    const rule = review.rules.find((r) => r.paths.some((g) => globMatches(g, p)));
    if (!rule) refuse(`no rule matches the changed path ${JSON.stringify(p)}`);
    counted.get(rule.name)!.push(p);
    if (exact.has(p) || globMatches(WORKFLOWS, p)) {
      if (!firstCritical) refuse(`${JSON.stringify(p)} is protected, but no rule is critical`);
      isProtected.push(p);
      if (firstCritical !== rule) counted.get(firstCritical.name)!.push(p);
    }
  }
  const hits = review.rules.flatMap((r) => {
    const got = counted.get(r.name);
    return got === null || got!.length
      ? [{ rule: r.name, weight: r.weight, paths: got ?? [] }]
      : [];
  });
  const weight = WEIGHTS.find((w) => hits.some((h) => h.weight === w))!;
  // Every rule hit gives its lenses, whatever its weight (owner decision: fork issue 82, one extra review).
  const lenses = review.rules
    .filter((r) => hits.some((h) => h.rule === r.name))
    .flatMap((r) => r.lenses);
  return { weight, lenses: [...new Set(lenses)], hits, protected: isProtected };
}
