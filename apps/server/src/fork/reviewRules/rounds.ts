/**
 * Port of review_judge.judge_rounds (fork-ci/actions/pr-queue), held to the shared vectors. The problem texts match
 * the gate's word for word, so T3 and the check summary say the same thing.
 */
import { lensKey } from "./lensComment.ts";
import { fold, readFindings, type FindingKind } from "./findings.ts";
import type { ReviewBlock, ReviewPlan } from "./plan.ts";
import { byCodePoint, digitValue, repr, split, stripChars } from "./pyText.ts";

const ROUND_RE = /^r(\p{Nd}{1,9})$/u;

/** How each lens's newest entry is read (the gate's head_check.review_rounds says which one it is). */
export const REPORT = "report";
const LOOSE = "loose";
const UNREAD = "unread";
export const CLEARED = "cleared";
const NOT_JUDGED = "skip";
export type EntryHow =
  | typeof REPORT
  | typeof LOOSE
  | typeof UNREAD
  | typeof CLEARED
  | typeof NOT_JUDGED;

/** The highest round marker (`r3`, also `**r3**` or `r３`) among a lens name's words after its first, or 0. */
export function roundMarker(name: string): number {
  let most = 0;
  for (const word of split(fold(name).toLowerCase()).slice(1)) {
    const m = ROUND_RE.exec(stripChars(word, "*_`"));
    if (m) most = Math.max(most, Number(Array.from(m[1]!, digitValue).join("")));
  }
  return most;
}

/**
 * The round the lens headers say. history: [lens name, sha or null] of every lens header that counts; changeOf(sha):
 * what the pull request changes at that commit (without it each different sha is one change).
 */
function reviewRound(
  history: ReadonlyArray<readonly [string, string | null]>,
  changeOf: (sha: string) => string = (sha) => sha,
): number {
  const changes = new Set(history.flatMap(([, sha]) => (sha ? [changeOf(sha.toLowerCase())] : [])));
  return history.reduce((most, [name]) => Math.max(most, roundMarker(name)), changes.size);
}

export interface Rounds {
  readonly round: number;
  /** The approach lens is required (round >= rounds.approach_at and no person decided). */
  readonly approach: boolean;
  /** round > rounds.max and no person decided. */
  readonly overCap: boolean;
  /** [lens key, kind, line] of every blocker or major no later round or person cleared. */
  readonly findings: ReadonlyArray<[string, FindingKind, string]>;
  /** Why the head fails, plain text; empty: findings and rounds pass. */
  readonly problems: ReadonlyArray<string>;
}

/** How a person lets a head through, as the reasons say it. */
function personWay(review: ReviewBlock, prefix = "override for head"): string {
  const who = review.override_authors ?? [];
  if (who.length === 0) {
    return (
      "a person's override comment, but no one is listed in review.override_authors yet, so no override is accepted " +
      "(issue #78: until agents no longer post as the person's login, an override is not proof of a person)"
    );
  }
  return `an override comment \`${prefix} <head sha>: <why>\` for this head by ${who.join(" or ")}, newer than every lens comment`;
}

/**
 * The rounds of a pull request's lens comments. reports: each lens key's newest entry [how, body]; decided: a person's
 * override for this head is newer than every lens comment.
 */
export function judgeRounds(
  review: ReviewBlock,
  history: ReadonlyArray<readonly [string, string | null]>,
  reports: Readonly<Record<string, readonly [EntryHow, string]>>,
  decided = false,
  changeOf?: (sha: string) => string,
  prefix = "override for head",
): Rounds {
  const n = reviewRound(history, changeOf);
  const cap = review.rounds.max;
  const overCap = n > cap && !decided;
  const way = personWay(review, prefix);
  const problems: string[] = [];
  if (overCap) {
    problems.push(
      `review round ${n} is past the cap of ${cap} rounds (issue #100): no more rounds; a person decides: close the ` +
        `pull request, or ${way}. A person's override alone is enough, also for a required approach lens that never posted`,
    );
  }
  const found: Array<[string, FindingKind, string]> = [];
  if (review.findings) {
    for (const key of Object.keys(reports).sort(byCodePoint)) {
      const [how, body] = reports[key]!;
      if (how === CLEARED || how === NOT_JUDGED) continue;
      if (how === UNREAD) {
        problems.push(
          `lens ${key}: its newest report reads nothing and only an override that is no person's covers it, so its ` +
            `findings cannot be read (issue #99): that lens posts a new report, or ${way}`,
        );
        continue;
      }
      const [said, section] = readFindings(body);
      for (const [kind, line] of said) found.push([key, kind, line]);
      if (how === REPORT && said.length === 0 && !section) {
        problems.push(
          `lens ${key}: its newest report has no findings section, so it cannot be judged (issue #99): write ` +
            "`blocker: none` and `major: none`, or the findings, as lines of the report body (findings in inline " +
            "review comments are not read)",
        );
      }
    }
    for (const [key, kind, line] of found) {
      const again =
        n >= cap
          ? `this is round ${n} of ${cap}: no more rounds; a person decides: close the pull request, or ${way}`
          : `fix it, and lens ${key} posts a new report on the fixed head with no blocker or major (\`major: none\`); or ${way}`;
      problems.push(
        `lens ${key} reported a ${kind} that no later round cleared (issue #99): ${repr(line)}; ${again}`,
      );
    }
  }
  return {
    round: n,
    approach: n >= review.rounds.approach_at && !decided,
    overCap,
    findings: found,
    problems,
  };
}

/** One line for the check summary. */
export function roundsText(review: ReviewBlock, rounds: Rounds): string {
  let said = `round ${rounds.round} of ${review.rounds.max}`;
  if (rounds.approach) {
    said += `, approach lens ${review.rounds.approach_lens} required from round ${review.rounds.approach_at}`;
  }
  if (!review.findings) said += "; findings are not judged (findings: false)";
  return (
    said +
    (rounds.problems.length ? `; ${rounds.problems.join("; ")}` : "; no blocker or major open")
  );
}

/** The lenses an enforced head needs, by lens key: the plan's, and the approach lens when the rounds require it. */
export function requiredLenses(
  review: ReviewBlock,
  plan: ReviewPlan,
  rounds: Rounds,
): Map<string, string> {
  const required = new Map<string, string>();
  const add = (name: string) => required.has(lensKey(name)) || required.set(lensKey(name), name);
  plan.lenses.forEach(add);
  if (rounds.approach) add(review.rounds.approach_lens);
  return required;
}
