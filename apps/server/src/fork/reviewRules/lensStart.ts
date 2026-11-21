/**
 * Fork: `lens_start` as pure logic. From what the caller read (pull requests, lens comments, the base's `review`
 * block and prompts) it plans the read-only lens children, or refuses. Nothing is stored: same input, same answer.
 * One child per lens; a batch child reads every pull request needing it and reports once per pull request.
 */
// @effect-diagnostics-next-line nodeBuiltinImport:off -- the batch request id must be a deterministic digest.
import * as NodeCrypto from "node:crypto";
import * as Schema from "effect/Schema";

import { type EffectiveReviewRules, type ReviewRunner, reviewLensRunner } from "@t3tools/contracts";

import { lensKey, lensScan, type LensPost } from "./lensComment.ts";
import { ReviewPlanError, planReview, type ReviewBlock, type ReviewPlan } from "./plan.ts";
import { judgeRounds, requiredLenses, type Rounds } from "./rounds.ts";

const isPlanError = Schema.is(ReviewPlanError);

type OnRules = Extract<EffectiveReviewRules, { readonly preset: "recommended" }>;

/** The most pull requests one call reads. */
export const LENS_START_MAX_PRS = 10;
/** The longest request id `delegate_task` takes. */
const REQUEST_ID_MAX = 256;
const SHA = /^[0-9a-f]{40}$/u;
const REPO = /^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9._-]+$/u;

/** The lens a repo without a `review` block gets, and its built-in prompt. */
export const FALLBACK_LENS = "review";
const FALLBACK_APPROACH_LENS = "approach";
const FALLBACK_PROMPT = "fallback:review";
const FALLBACK_APPROACH_PROMPT = "fallback:approach";
const FALLBACK_PROMPTS: Readonly<Record<string, string>> = {
  [FALLBACK_PROMPT]:
    "Review the whole change for correctness, data safety and security. Read the code the change " +
    "touches and the code that calls it, not only the diff.",
  [FALLBACK_APPROACH_PROMPT]:
    "This pull request is in its third review round or later. Do not review line by line: judge " +
    "whether its approach can reach a mergeable state, and name the simplest way that can.",
};

/** The `review` block a repo without one is judged by: every pull request is normal, one lens. */
export const FALLBACK_REVIEW: ReviewBlock = {
  rules: [{ name: "all", weight: "normal", paths: ["**"], lenses: [FALLBACK_LENS] }],
  lenses: {
    [FALLBACK_LENS]: { prompt: FALLBACK_PROMPT },
    [FALLBACK_APPROACH_LENS]: { prompt: FALLBACK_APPROACH_PROMPT },
  },
  rounds: { approach_at: 3, max: 4, approach_lens: FALLBACK_APPROACH_LENS },
  findings: true,
  override_authors: [],
};

const FALLBACK_NOTE =
  `the repo's config has no \`review\` block, so every pull request is normal with one lens ` +
  `\`${FALLBACK_LENS}\` (rounds ${FALLBACK_REVIEW.rounds.approach_at} / ${FALLBACK_REVIEW.rounds.max}); ` +
  "T3 does not guess critical paths: the repo's config names them";

/** One pull request as the caller read it from GitHub, its head first. */
export interface LensStartPullRequest {
  readonly number: number;
  readonly state: "open" | "closed" | "merged";
  /** The head commit GitHub names now. */
  readonly head: string;
  /** `git diff --name-status -M <merge-base> <head>`, one [status, path] or [R|C<score>, old, new] per line. */
  readonly changes: ReadonlyArray<ReadonlyArray<string>>;
  /** The comments and reviews that count, in the gate's order (lensComment.ts). */
  readonly posts: ReadonlyArray<LensPost>;
  /** A person's override for this head is newer than every lens comment (the gate's `decided`). */
  readonly decided?: boolean;
  /** What the pull request itself changes at a commit, so a read before a clean base merge still read the head. */
  readonly changeOf?: (sha: string) => string;
}

export interface LensStartInput {
  /** This project's effective review rules. */
  readonly rules: EffectiveReviewRules;
  /** owner/name. */
  readonly repo: string;
  /** The `review` block of the config at the base; null: the config has none. */
  readonly review: ReviewBlock | null;
  /** Each lens prompt file's text at the base, by its path in the config; a file not read is absent. */
  readonly prompts: Readonly<Record<string, string>>;
  readonly overridePrefix?: string;
  /** The call: each pull request, and the head the caller expects it at, if it names one. */
  readonly prs: ReadonlyArray<{ readonly number: number; readonly head?: string }>;
  /** What the caller read, by number; a pull request GitHub does not know is absent. */
  readonly pulls: ReadonlyMap<number, LensStartPullRequest>;
}

/** One pull request a child reads. `round` is the review round this read makes. */
export interface LensStartRead {
  readonly number: number;
  readonly head: string;
  readonly round: number;
}

/** One read-only child to start through `delegate_task` (role `review`). */
export interface LensStartChild {
  /** The same lens on the same heads gives the same id: a second call returns the same task. */
  readonly requestId: string;
  /** The lens's name as the config writes it. */
  readonly lens: string;
  readonly key: string;
  readonly runner: ReviewRunner;
  readonly title: string;
  readonly prompt: string;
  readonly prs: ReadonlyArray<LensStartRead>;
}

export interface LensStartRefusal {
  /** null: the whole call. */
  readonly pr: number | null;
  readonly reason: string;
}

export interface LensStartPlan {
  readonly starts: ReadonlyArray<LensStartChild>;
  readonly refused: ReadonlyArray<LensStartRefusal>;
  /** Required lenses whose newest report already read the head: not started again. */
  readonly alreadyRead: ReadonlyArray<{ readonly pr: number; readonly lens: string }>;
  readonly notes: ReadonlyArray<string>;
}

const whole = (reason: string): LensStartPlan => ({
  starts: [],
  refused: [{ pr: null, reason }],
  alreadyRead: [],
  notes: [],
});

/** What one pull request needs: the lenses still to read its head, by key, or why it is refused. */
type PullNeed =
  | { readonly refused: string }
  | {
      readonly read: LensStartRead;
      readonly need: ReadonlyArray<{ readonly key: string; readonly name: string }>;
      readonly done: ReadonlyArray<string>;
      /** Each lens key's earlier reads, oldest first, each sha once. */
      readonly earlier: ReadonlyMap<string, ReadonlyArray<string>>;
    };

function readPull(
  review: ReviewBlock,
  configPath: string,
  overridePrefix: string | undefined,
  fallback: boolean,
  pull: LensStartPullRequest,
): PullNeed {
  const head = pull.head.toLowerCase();
  if (pull.state !== "open") return { refused: `it is ${pull.state}: only an open one is read` };
  if (!SHA.test(head))
    return { refused: `its head ${JSON.stringify(pull.head)} is not a full sha` };
  let plan: ReviewPlan;
  try {
    plan = fallback
      ? { weight: "normal", lenses: [FALLBACK_LENS], hits: [], protected: [] }
      : planReview(review, pull.changes, configPath);
  } catch (error) {
    if (!isPlanError(error)) throw error;
    return { refused: `no plan: ${error.reason}; no lens is started rather than too few` };
  }
  if (pull.decided) {
    return {
      refused:
        "a person's override already lets this head through; no lens is started, because a new lens " +
        "comment would be newer than the override and end it",
    };
  }
  const changeOf = pull.changeOf ?? ((sha: string) => sha);
  const { history, entries } = lensScan(pull.posts, overridePrefix);
  const rounds = startRounds(review, history, head, pull.changeOf);
  if (rounds.round > review.rounds.max) {
    return {
      refused:
        `a read of this head would be review round ${rounds.round}, past the cap of ${review.rounds.max} ` +
        "rounds: no more rounds; a person decides: close the pull request, or write an override comment",
    };
  }
  const here = changeOf(head);
  const need: Array<{ key: string; name: string }> = [];
  const done: string[] = [];
  for (const [key, name] of requiredLenses(review, plan, rounds)) {
    // An edited or hidden newest entry has no sha (lensScan): it read nothing.
    const [sha] = entries[key] ?? [null];
    if (sha !== null && SHA.test(sha) && changeOf(sha) === here) done.push(name);
    else need.push({ key, name });
  }
  const earlier = new Map<string, string[]>();
  for (const [name, sha] of history) {
    // A read of this very head that does not count (edited or hidden) is no earlier round.
    if (sha === null || changeOf(sha) === here) continue;
    const list = earlier.get(lensKey(name)) ?? [];
    if (!list.includes(sha)) list.push(sha);
    earlier.set(lensKey(name), list);
  }
  return { read: { number: pull.number, head, round: rounds.round }, need, done, earlier };
}

/**
 * The rounds as a read of `head` would make them, counted as the gate counts them: the reads so far
 * plus one of this head, which is one more round only if no lens read that change yet.
 */
export function startRounds(
  review: ReviewBlock,
  history: ReadonlyArray<readonly [string, string | null]>,
  head: string,
  changeOf?: (sha: string) => string,
): Rounds {
  return judgeRounds(review, [...history, [FALLBACK_LENS, head]], {}, false, changeOf);
}

const digest = (text: string, n: number) =>
  NodeCrypto.createHash("sha256").update(text).digest("hex").slice(0, n);

/**
 * The request id of one lens child: `lens/<repo>#<pr>/<head>/<lens key>` for one pull request; for a
 * batch, a digest of its sorted `<pr>@<head>` lines. The same lens on the same heads gives the same id;
 * a moved head or another set of pull requests gives a new one. `attempt` (2, 3, ...) starts a lens
 * again after its child ended without a report.
 */
export function lensRequestId(
  repo: string,
  key: string,
  reads: ReadonlyArray<Pick<LensStartRead, "number" | "head">>,
  attempt = 1,
): string {
  const lens = encodeURIComponent(key);
  const again = attempt > 1 ? `/again-${attempt}` : "";
  const sorted = reads.toSorted((a, b) => a.number - b.number);
  const id =
    sorted.length === 1
      ? `lens/${repo}#${sorted[0]!.number}/${sorted[0]!.head}/${lens}${again}`
      : `lens/${repo}/batch-${digest(sorted.map((r) => `${r.number}@${r.head}`).join("\n"), 24)}/${lens}${again}`;
  return id.length <= REQUEST_ID_MAX ? id : `lens/${digest(id, 64)}`;
}

/** The prompt the server gives one lens child; the agent never builds it. */
function lensPrompt(
  rules: OnRules,
  repo: string,
  lens: string,
  lensText: string,
  reads: ReadonlyArray<LensStartRead & { readonly earlier: ReadonlyArray<string> }>,
): string {
  const batch = reads.length > 1;
  const lines = [
    `You are the \`${lens}\` review lens for ${batch ? "these pull requests" : "pull request"} of ${repo}. ` +
      "Read only: change no file, commit, push or comment by hand.",
    "",
    ...reads.map((r) => {
      const said = `- #${r.number} at head ${r.head}: review round ${r.round}.`;
      if (r.earlier.length === 0) return `${said} This lens has not read it before.`;
      return (
        `${said} This lens read it before at ${r.earlier.join(", ")}: check each earlier finding ` +
        `first, then the changes since ${r.earlier.at(-1)}.`
      );
    }),
    "",
    "## What this lens looks at",
    "",
    lensText.trim(),
    "",
    "## The state question",
    "",
    rules.stateQuestion,
    "Answer it in a line starting `state:`.",
    "",
    "## Your report",
    "",
    batch
      ? "Post one report per pull request with `lens_report`, each for that pull request's head above. " +
        "Never one report for several."
      : "Post your report with `lens_report`, for the head above.",
    "Write one line per finding, starting `blocker:`, `major:` or `minor:`; write `blocker: none` and " +
      "`major: none` when there is none.",
    `Keep each report under ${rules.reportMaxChars} characters.`,
    "If a head is not the one above when you read it, report nothing for it and say so in your answer.",
  ];
  return lines.join("\n");
}

/**
 * The plan for one `lens_start` call. Each pull request is planned or refused on its own, so one
 * closed pull request in a batch does not stop the others.
 */
export function planLensStart(input: LensStartInput): LensStartPlan {
  const { rules } = input;
  if (rules.preset === "off") {
    return whole(
      "review rules are off for this project (Settings: review rules); lens_start starts nothing",
    );
  }
  if (!REPO.test(input.repo)) return whole(`${JSON.stringify(input.repo)} is not owner/name`);
  if (input.prs.length === 0) return whole("name at least one pull request");
  const fallback = input.review === null;
  const review = input.review ?? FALLBACK_REVIEW;
  const prompts = fallback ? FALLBACK_PROMPTS : input.prompts;
  const refused: LensStartRefusal[] = [];
  const asked = new Map<number, string | undefined>();
  for (const { number, head } of input.prs) {
    if (!Number.isSafeInteger(number) || number < 1) {
      return whole(`${JSON.stringify(number)} is not a pull request number`);
    }
    const before = asked.get(number);
    if (asked.has(number) && before !== head) {
      return whole(`#${number} is named twice with different heads`);
    }
    asked.set(number, head?.toLowerCase());
  }
  if (asked.size > LENS_START_MAX_PRS) {
    return whole(`at most ${LENS_START_MAX_PRS} pull requests per call, not ${asked.size}`);
  }
  const alreadyRead: Array<{ pr: number; lens: string }> = [];
  const groups = new Map<
    string,
    { name: string; reads: Array<LensStartRead & { earlier: ReadonlyArray<string> }> }
  >();
  for (const [number, expected] of asked) {
    const pull = input.pulls.get(number);
    if (pull === undefined) {
      refused.push({ pr: number, reason: `${input.repo} has no pull request #${number}` });
      continue;
    }
    if (expected !== undefined && expected !== pull.head.toLowerCase()) {
      refused.push({
        pr: number,
        reason: `its head is ${pull.head}, not ${expected}: read the pull request again`,
      });
      continue;
    }
    const got = readPull(review, rules.configPath, input.overridePrefix, fallback, pull);
    if ("refused" in got) {
      refused.push({ pr: number, reason: got.refused });
      continue;
    }
    for (const lens of got.done) alreadyRead.push({ pr: number, lens });
    const missing = got.need.filter(({ name }) => {
      const path = review.lenses[name]?.prompt;
      return path === undefined || !Object.hasOwn(prompts, path);
    });
    if (missing.length > 0) {
      const named = missing.map(({ name }) => {
        const path = review.lenses[name]?.prompt;
        return path === undefined ? `${name} (no entry in lenses)` : `${name} (${path})`;
      });
      refused.push({
        pr: number,
        reason: `no plan: the prompt of lens ${named.join(", ")} could not be read at the base`,
      });
      continue;
    }
    for (const { key, name } of got.need) {
      const group = groups.get(key) ?? { name, reads: [] };
      group.reads.push({ ...got.read, earlier: got.earlier.get(key) ?? [] });
      groups.set(key, group);
    }
  }
  const starts = [...groups].map(([key, { name, reads }]): LensStartChild => {
    const prs = reads.map(({ number, head, round }) => ({ number, head, round }));
    const which = prs.map((r) => `#${r.number}`).join(" ");
    return {
      requestId: lensRequestId(input.repo, key, prs),
      lens: name,
      key,
      runner: reviewLensRunner(rules, name),
      title: `lens ${name} ${which}`,
      prompt: lensPrompt(rules, input.repo, name, prompts[review.lenses[name]!.prompt]!, reads),
      prs,
    };
  });
  return { starts, refused, alreadyRead, notes: fallback ? [FALLBACK_NOTE] : [] };
}

/**
 * Just before the children start: the heads GitHub names now (null: the pull request is gone or no
 * longer open). A child whose pull request moved or closed since the plan is refused whole, as its
 * prompt names the old head; calling lens_start again plans the new head, and a child that started
 * keeps its id, so it is returned, not started twice.
 */
export function confirmLensStart(
  plan: LensStartPlan,
  heads: ReadonlyMap<number, string | null>,
): LensStartPlan {
  const moved = new Map<number, string>();
  for (const child of plan.starts) {
    for (const r of child.prs) {
      const now = heads.get(r.number);
      if (now === undefined || now === null) {
        moved.set(r.number, "it is no longer open since the plan; nothing is started for it");
      } else if (now.toLowerCase() !== r.head) {
        moved.set(
          r.number,
          `its head moved from ${r.head} to ${now.toLowerCase()} since the plan: call lens_start again`,
        );
      }
    }
  }
  if (moved.size === 0) return plan;
  return {
    ...plan,
    starts: plan.starts.filter((child) => child.prs.every((r) => !moved.has(r.number))),
    refused: [
      ...plan.refused,
      ...[...moved].map(([pr, reason]) => ({ pr, reason })),
      ...plan.starts
        .filter((c) => c.prs.some((r) => moved.has(r.number)) && c.prs.length > 1)
        .map((c) => ({
          pr: null,
          reason:
            `lens ${c.lens} for ${c.prs.map((r) => `#${r.number}`).join(" ")} is not started: a pull ` +
            "request of its batch moved; call lens_start again",
        })),
    ],
  };
}
