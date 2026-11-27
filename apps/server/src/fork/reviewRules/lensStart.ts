/**
 * Fork: `lens_start` as pure logic: from what the caller read it plans the read-only lens children, or refuses; same
 * input, same answer. It reads what the gate reads (head_check.decide, verdict, lens.Review.lens_reads), so when
 * anything starts, every lens entry that does not name the head starts again there (the gate needs one head).
 */
// @effect-diagnostics-next-line nodeBuiltinImport:off -- the batch request id must be a deterministic digest.
import * as NodeCrypto from "node:crypto";
import * as Schema from "effect/Schema";

import { type EffectiveReviewRules, type ReviewRunner, reviewLensRunner } from "@t3tools/contracts";

import {
  NO_HEADER,
  REPOST,
  lensEntries,
  lensKey,
  namesALens,
  olderThan,
  type LensPost,
  type PostAt,
} from "./lensComment.ts";
import { readFindings } from "./findings.ts";
import { ReviewPlanError, planReview, type ReviewBlock, type ReviewPlan } from "./plan.ts";
import { PyTextError } from "./pyText.ts";
import { judgeRounds, type Rounds } from "./rounds.ts";

const isPlanError = Schema.is(ReviewPlanError);
const isPyTextError = Schema.is(PyTextError);

type OnRules = Extract<EffectiveReviewRules, { readonly preset: "recommended" }>;

/** The most pull requests one call reads. */
export const LENS_START_MAX_PRS = 10;
/**
 * The most children one lens gets on one pull request's head, in whatever batches they ran: after this many ended with
 * no read the gate counts there, a person looks.
 */
export const LENS_START_MAX_ATTEMPTS = 3;
/** The longest request id `delegate_task` takes. */
const REQUEST_ID_MAX = 256;
const SHA = /^[0-9a-f]{40}$/u;
const REPO = /^[A-Za-z0-9][A-Za-z0-9-]*\/[A-Za-z0-9._-]+$/u;
/** The preset's runner key for the approach lens's role (the "stuck in circles" audit). */
const APPROACH_RUNNER = "approach";

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

/**
 * The rounds a repo without a `review` block is held to by T3 (the gate judges no rounds there): 3 / 4, and the
 * approach lens from round 3. Its lenses are the gate's required-lenses input, each with the built-in prompt.
 */
export const FALLBACK_REVIEW: ReviewBlock = {
  rules: [],
  lenses: { [FALLBACK_APPROACH_LENS]: { prompt: FALLBACK_APPROACH_PROMPT } },
  rounds: { approach_at: 3, max: 4, approach_lens: FALLBACK_APPROACH_LENS },
  findings: true,
  override_authors: [],
};

/** How a person clears an entry lens_start cannot start again (lens.py NO_HEADER_WAY_OUT, plus the override). */
const NO_HEADER_WAY_OUT =
  "delete that comment (hiding it is not enough), or edit it to start with `not a lens` and a colon and hold no " +
  "lens header line and no sha on a line with a lens field, or";

/** One post in the gate's order: a lens comment of a counted author, or a valid override comment. */
export interface LensStartPost extends LensPost {
  /**
   * The sha a valid override comment vouches for (lens.Review.override_sha: the override author association, never
   * edited, not hidden), lower-case; absent on every other post. Any such override newer than a lens's entry covers
   * it in the gate (lens_reads), a person's or not.
   */
  readonly override?: string;
  /**
   * The login of an override comment's author. An override by a login in the base's `review.override_authors` that
   * vouches for this head (`sameChange`) is a person's: it clears the findings of every older entry (head_check
   * review_rounds); any other override covers an entry's read but clears nothing. Absent: no person's override.
   */
  readonly author?: string;
}

/** One pull request as the caller read it from GitHub, its head first. */
export interface LensStartPullRequest {
  readonly number: number;
  readonly state: "open" | "closed" | "merged";
  /** The head commit GitHub names now. */
  readonly head: string;
  /** The branch it merges into: its rules plan it (`LensStartInput.bases`). */
  readonly base: string;
  /** `git diff --name-status -M <merge-base> <head>`, one [status, path] or [R|C<score>, old, new] per line. */
  readonly changes: ReadonlyArray<ReadonlyArray<string>>;
  /** The lens comments that count and the valid override comments, in the gate's order (lensComment.ts). */
  readonly posts: ReadonlyArray<LensStartPost>;
  /** A person's override for this head is newer than every lens comment (the gate's `decided`). */
  readonly decided?: boolean;
  /**
   * For round counting only: what the pull request itself changes at a commit (head_check.own_change), so a read of
   * the same change is no new round. Never used for "this lens read the head": an amended or rebased commit has the
   * same change but is not the commit read, and the gate fails it.
   */
  readonly changeOf?: (sha: string) => string;
  /**
   * The gate's own check that a lens read of `read` reads this head (head_check.same_change): the head is `read`, or
   * `read` merged with the base and nothing else (pr_queue.holds). Absent: only the head itself counts.
   */
  readonly sameChange?: (read: string) => boolean;
}

/** What the gate reads at one base branch's tip. */
export interface LensStartBase {
  /** The `review` block of the config at the base; null: the config has none. */
  readonly review: ReviewBlock | null;
  /** Each lens prompt file's text at the base, by its path in the config; a file not read is absent. */
  readonly prompts: Readonly<Record<string, string>>;
  /** The gate caller's `required-lenses` input, one lens name per entry (blank ones skipped); null: not read. */
  readonly requiredLenses: ReadonlyArray<string> | null;
}

/**
 * A lens child started earlier (`earlierChild`; a long request id is a digest, so it cannot be read back). The caller
 * passes every one it stored, as `delegate_task` returned it (a reused id returns a task that may have ended), never
 * as planned; the attempt cap counts them per lens, pull request and head, so a lost one allows one more start there.
 */
export interface LensStartEarlierChild {
  readonly requestId: string;
  readonly key: string;
  readonly prs: ReadonlyArray<{ readonly number: number; readonly head: string }>;
  /**
   * true: still running, so what it reads is returned, not started twice; false: it ended. Read from the task's own
   * state at each call, not kept from its start: a child that stopped without the caller seeing it is then ended, not
   * running for ever (there is no clock here to time one out).
   */
  readonly running: boolean;
}

export interface LensStartInput {
  /** This project's effective review rules. */
  readonly rules: EffectiveReviewRules;
  /** owner/name. */
  readonly repo: string;
  /** What the gate reads at each base branch, by branch name. */
  readonly bases: ReadonlyMap<string, LensStartBase>;
  readonly overridePrefix?: string;
  /** The call: each pull request, and the head the caller expects it at, if it names one. */
  readonly prs: ReadonlyArray<{ readonly number: number; readonly head?: string }>;
  /** What the caller read, by number; a pull request GitHub does not know is absent. */
  readonly pulls: ReadonlyMap<number, LensStartPullRequest>;
  /**
   * The lens children started earlier; absent: none known. Read them before `pulls`: a child that posts its read and
   * ends between the two reads is then seen running (returned) or its read is seen, never neither (a second child).
   */
  readonly children?: ReadonlyArray<LensStartEarlierChild>;
}

/** One pull request a child reads. `round` is the review round this read makes. */
export interface LensStartRead {
  readonly number: number;
  readonly head: string;
  readonly round: number;
}

/** One read-only child to start through `delegate_task` (role `review`). */
export interface LensStartChild {
  /** The same lens on the same heads and attempt gives the same id: a second call returns the same task. */
  readonly requestId: string;
  /**
   * 1, or one more than the most children of this lens that ended at one of its heads with no read the gate counts
   * (`replaces`), in whatever batches they ran.
   */
  readonly attempt: number;
  /** The ids of the earlier children of this lens at these heads that ended; empty on a first attempt. */
  readonly replaces: ReadonlyArray<string>;
  /** The lens's name as the config (or the required-lenses input) writes it. */
  readonly lens: string;
  readonly key: string;
  readonly runner: ReviewRunner;
  readonly title: string;
  readonly prompt: string;
  readonly prs: ReadonlyArray<LensStartRead>;
}

export interface LensStartRefusal {
  /** null: the whole call, or a batch child of several pull requests (the reason names them). */
  readonly pr: number | null;
  readonly reason: string;
}

export interface LensStartPlan {
  readonly starts: ReadonlyArray<LensStartChild>;
  /** Children still running that already read what this call needs: returned, never started twice. */
  readonly running: ReadonlyArray<{
    readonly requestId: string;
    readonly key: string;
    readonly prs: ReadonlyArray<{ readonly number: number; readonly head: string }>;
  }>;
  readonly refused: ReadonlyArray<LensStartRefusal>;
  /** Required lenses the gate already counts as reading the head: not started again. */
  readonly alreadyRead: ReadonlyArray<{ readonly pr: number; readonly lens: string }>;
  readonly notes: ReadonlyArray<string>;
}

const whole = (reason: string): LensStartPlan => ({
  starts: [],
  running: [],
  refused: [{ pr: null, reason }],
  alreadyRead: [],
  notes: [],
});

/** What the gate counts one lens entry as reading (lens.Review.lens_reads). */
export interface GateRead {
  /** The full sha it reads, or the newer override's; null: it reads nothing. */
  readonly sha: string | null;
  /** The lens name of its newest header; null for a comment that reads nothing (a NO_HEADER entry). */
  readonly name: string | null;
}

/**
 * lens.Review.lens_reads on the posts: each lens key's newest entry, and what it reads. An override newer than an
 * entry makes it read the override's sha; a NO_HEADER entry, an edited or hidden newest entry or a short sha reads
 * nothing.
 */
export function gateReads(
  posts: ReadonlyArray<LensStartPost>,
  overridePrefix?: string,
): Map<string, GateRead> {
  const { entries } = lensEntries(posts, overridePrefix, isOverride);
  const over = newestOverride(posts);
  const reads = new Map<string, GateRead>();
  for (const [key, { at, sha, name }] of entries) {
    if (over !== null && olderThan(at, over.at)) reads.set(key, { sha: over.sha, name });
    else reads.set(key, { sha: sha !== null && SHA.test(sha) ? sha : null, name });
  }
  return reads;
}

const isOverride = (post: LensStartPost) => post.override !== undefined;

/** The newest override with a full sha (lens_reads: max over [when, index]), and its post; null: none. */
function newestOverride(
  posts: ReadonlyArray<LensStartPost>,
  only: (post: LensStartPost) => boolean = () => true,
): { at: PostAt; sha: string } | null {
  let newest: { at: PostAt; sha: string } | null = null;
  posts.forEach((post, i) => {
    if (post.override === undefined || !SHA.test(post.override) || !only(post)) return;
    const at: PostAt = [post.when, i];
    if (newest === null || !olderThan(at, newest.at)) newest = { at, sha: post.override };
  });
  return newest;
}

/**
 * The lens part of the gate's verdict (head_check.verdict): every required key has an entry, every entry reads one
 * full sha, and that sha is the head or holds its change (head_check.same_change, never the own-change hash).
 */
export function gateReadsPass(
  reads: ReadonlyMap<string, GateRead>,
  required: Iterable<string>,
  head: string,
  sameChange: (read: string) => boolean = () => false,
): boolean {
  const keys = [...required];
  if (reads.size === 0) return keys.length === 0;
  if (keys.some((key) => !reads.has(key))) return false;
  const shas = new Set([...reads.values()].map((r) => r.sha));
  if (shas.size !== 1) return false;
  const [sha] = shas;
  return sha !== null && sha !== undefined && (sha === head || sameChange(sha));
}

/** What the gate says of a lens whose newest entry fails it until that lens posts again. */
const UNREAD_SAID = "reads nothing (only an override that is no person's covers it)";
const NO_SECTION_SAID = "has a newest report with no findings section";

/**
 * The lens keys whose newest entry the gate fails in enforce with findings on until that lens posts again (or a person
 * overrides), however the reads look (head_check.review_rounds, review_judge.judge_rounds), each with what it says:
 * UNREAD (no full sha of its own, and an override newer than it covers it), or a REPORT (a full sha) with no finding
 * and no findings section. A person's override for this head newer than an entry clears it (CLEARED).
 */
export function postAgainKeys(
  posts: ReadonlyArray<LensStartPost>,
  review: ReviewBlock,
  head: string,
  sameChange: (read: string) => boolean = () => false,
  overridePrefix?: string,
): Map<string, string> {
  const people = new Set((review.override_authors ?? []).map((a) => a.toLowerCase()));
  const newest = newestOverride(posts);
  const person = newestOverride(
    posts,
    (post) =>
      people.has((post.author ?? "").toLowerCase()) &&
      (post.override === head || sameChange(post.override!)),
  );
  const again = new Map<string, string>();
  for (const [key, { at, sha, body }] of lensEntries(posts, overridePrefix, isOverride).entries) {
    if ((person !== null && olderThan(at, person.at)) || key.startsWith(NO_HEADER)) continue;
    if (sha !== null && SHA.test(sha)) {
      const [said, section] = readFindings(body);
      if (said.length === 0 && !section) again.set(key, NO_SECTION_SAID);
    } else if (newest !== null && olderThan(at, newest.at)) again.set(key, UNREAD_SAID);
  }
  return again;
}

/** One lens a base knows how to start: its name, and its prompt (a config path, or a built-in one). */
interface KnownLens {
  readonly name: string;
  readonly prompt: string;
  readonly builtIn: boolean;
  readonly approach: boolean;
}

/** A required lens the config names but gives no `lenses` entry (the gate's parser refuses that config). */
const NO_ENTRY: KnownLens = { name: "", prompt: "", builtIn: false, approach: false };

/** What one pull request needs: the lenses still to read its head, by key, or why it is refused. */
type PullNeed =
  | { readonly refused: string }
  | {
      readonly read: LensStartRead;
      readonly need: ReadonlyArray<KnownLens & { readonly key: string }>;
      readonly done: ReadonlyArray<string>;
      /** Each lens key's earlier reads, oldest first, each sha once. */
      readonly earlier: ReadonlyMap<string, ReadonlyArray<string>>;
      /**
       * Each lens key's reads of this head that the gate counted until a newer post of that lens replaced them: a
       * head read again after a force-push back to it (A, B, A) has them. The restart cap does not count them.
       */
      readonly replaced: ReadonlyMap<string, number>;
      readonly notes: ReadonlyArray<string>;
    };

function readPull(
  rules: OnRules,
  base: LensStartBase,
  overridePrefix: string | undefined,
  pull: LensStartPullRequest,
): PullNeed {
  const head = pull.head.toLowerCase();
  if (pull.state !== "open") return { refused: `it is ${pull.state}: only an open one is read` };
  if (!SHA.test(head))
    return { refused: `its head ${JSON.stringify(pull.head)} is not a full sha` };
  // The gate's required-lenses input (head_check.required_names): one name per line, blank ones skipped.
  if (base.requiredLenses === null) {
    return {
      refused:
        `the gate's required-lenses input at ${pull.base} was not read: no lens is started rather than ` +
        "too few",
    };
  }
  const named = new Map<string, string>();
  for (const raw of base.requiredLenses) {
    const name = raw.trim();
    if (name === "") continue;
    if (!namesALens(name)) {
      return {
        refused:
          `the gate's required-lenses input names no lens (${JSON.stringify(name)}), so the gate fails every ` +
          "head: a person fixes the caller workflow",
      };
    }
    if (!named.has(lensKey(name))) named.set(lensKey(name), name);
  }
  const review = base.review;
  if (review !== null && review.mode !== "shadow" && review.mode !== "enforce") {
    return {
      refused:
        `the \`review\` block at ${pull.base} has mode ${JSON.stringify(review.mode ?? null)}, not shadow ` +
        "or enforce, so the gate refuses its config: a person fixes the config",
    };
  }
  const enforce = review?.mode === "enforce";
  if (named.size === 0 && !enforce) {
    return {
      refused:
        "the gate's required-lenses input names no lens and the config has no `review` block in mode " +
        "enforce, so the gate requires no lens here (it fails a head only when the lens comments there do not " +
        "all read it); lens_start starts only lenses the gate requires, so none: a person names the lenses in " +
        "the caller workflow or the config",
    };
  }
  const block = review ?? FALLBACK_REVIEW;
  let plan: ReviewPlan;
  try {
    plan =
      review === null
        ? { weight: "normal", lenses: [], hits: [], protected: [] }
        : planReview(review, pull.changes, rules.configPath);
  } catch (error) {
    if (!isPlanError(error)) throw error;
    return { refused: `no plan: ${error.reason}; no lens is started rather than too few` };
  }
  const reads = gateReads(pull.posts, overridePrefix);
  const { history, notLens, entries, headers } = lensEntries(
    pull.posts,
    overridePrefix,
    isOverride,
  );
  const rounds = startRounds(block, history, head, pull.changeOf);
  // The lenses this base can start, by key: the config's, then the input's names (built-in prompt).
  const approachKey = lensKey(block.rounds.approach_lens);
  const known = new Map<string, KnownLens>();
  for (const [name, { prompt }] of Object.entries(block.lenses)) {
    const key = lensKey(name);
    if (!known.has(key)) {
      known.set(key, {
        name,
        prompt,
        builtIn: review === null,
        approach: key === approachKey,
      });
    }
  }
  // A look-alike with no lens marker and no open finding: the gate ignores it, so the lens that posted it has not
  // reported. Said, so an agent does not take its post for a read.
  const notes: string[] = notLens.map(
    ([where, why]) =>
      `#${pull.number}: the comment ${where} looks like a lens report, but the gate ignores it and counts no read ` +
      `from it (${why}); if it was meant as a report: ${REPOST}`,
  );
  const inputOnly: string[] = [];
  for (const [key, name] of named) {
    if (known.has(key)) continue;
    known.set(key, { name, prompt: FALLBACK_PROMPT, builtIn: true, approach: key === approachKey });
    inputOnly.push(name);
  }
  if (review === null) {
    notes.push(
      `#${pull.number}: the config at ${pull.base} has no \`review\` block, so the gate requires only its ` +
        `required-lenses input (${[...named.values()].join(", ")}), each read with a built-in prompt; T3 ` +
        `holds it to rounds ${FALLBACK_REVIEW.rounds.approach_at} / ${FALLBACK_REVIEW.rounds.max}. T3 does ` +
        "not guess critical paths: the repo's config names them",
    );
  } else if (inputOnly.length > 0) {
    notes.push(
      `#${pull.number}: the gate's required-lenses input names ${inputOnly.join(", ")}, which the config's ` +
        "lenses do not, so each is read with a built-in prompt",
    );
  }
  // What the gate requires (decide): the input, the path lenses in enforce; T3 adds the path lenses in shadow too
  // (the trial the shadow mode is for) and the approach lens from its round.
  const required = new Map<string, string>();
  const add = (name: string) => {
    const key = lensKey(name);
    if (!required.has(key)) required.set(key, known.get(key)?.name ?? name);
  };
  plan.lenses.forEach(add);
  for (const name of named.values()) add(name);
  if (pull.decided) {
    // The gate requires no approach lens once a person decided (judgeRounds), but still every other one.
    const missing = [...required].filter(([key]) => !reads.has(key)).map(([, name]) => name);
    return {
      refused:
        "a person's override already lets this head through; no lens is started, because a new lens " +
        "comment would be newer than the override and end it" +
        (missing.length > 0
          ? `. The gate still misses lens ${missing.join(", ")}: an override adds no lens, so a person ` +
            "decides how it is read"
          : ""),
    };
  }
  if (rounds.approach) add(block.rounds.approach_lens);
  const sameChange = pull.sameChange ?? (() => false);
  // In enforce, with findings judged, the gate fails these until that lens posts again.
  const again =
    enforce && block.findings
      ? postAgainKeys(pull.posts, block, head, sameChange, overridePrefix)
      : new Map<string, string>();
  const passes = again.size === 0 && gateReadsPass(reads, required.keys(), head, sameChange);
  if (rounds.round > block.rounds.max) {
    // The gate judges rounds only in enforce. Elsewhere T3 holds its own cap, and a head the gate already passes
    // (its own required lenses: the input) needs no person: nothing more starts.
    if (!enforce && gateReadsPass(reads, named.keys(), head, sameChange)) {
      return {
        read: { number: pull.number, head, round: rounds.round },
        need: [],
        done: [...named.values()],
        earlier: new Map(),
        replaced: new Map(),
        notes: [
          `#${pull.number}: a read of this head would be review round ${rounds.round}, past T3's cap of ` +
            `${block.rounds.max}; the gate already passes it and judges no rounds here, so nothing starts`,
        ],
      };
    }
    return {
      refused:
        `a read of this head would be review round ${rounds.round}, past the cap of ${block.rounds.max} ` +
        "rounds: no more rounds; a person decides: close the pull request, or write an override comment",
    };
  }
  const done: string[] = [];
  const need: Array<KnownLens & { key: string }> = [];
  if (passes) {
    done.push(...required.values());
  } else {
    // Something starts at the head, so every entry that does not name the head itself starts again too.
    const stuck: string[] = [];
    for (const [key, name] of required) {
      if (reads.get(key)?.sha === head && !again.has(key)) {
        done.push(name);
        continue;
      }
      need.push({ key, ...(known.get(key) ?? { ...NO_ENTRY, name }) });
      if (again.has(key)) {
        notes.push(
          `#${pull.number}: lens ${name} ${again.get(key)}, so the gate fails this head until it posts again: ` +
            "it reads the head again",
        );
      }
    }
    for (const [key, read] of reads) {
      if (required.has(key) || (read.sha === head && !again.has(key))) continue;
      const lens = known.get(key);
      const said = again.get(key) ?? (read.sha === null ? "reads nothing" : `read ${read.sha}`);
      if (lens !== undefined) {
        need.push({ key, ...lens });
        notes.push(
          `#${pull.number}: lens ${lens.name} is no longer required but ${said}` +
            (again.has(key)
              ? ", so the gate fails this head until it posts again"
              : ", not this head; the gate needs every lens comment to name one head") +
            ", so it reads the head again",
        );
      } else if (read.name === null) {
        stuck.push(`a comment that reads nothing (${key}): ${NO_HEADER_WAY_OUT}`);
      } else {
        stuck.push(
          `lens ${read.name} ${said}, and neither the config nor the required-lenses input names it, so ` +
            "lens_start has no prompt for it: post that lens again at this head, delete its comment, or",
        );
      }
    }
    if (stuck.length > 0) {
      return {
        refused:
          "the gate needs every lens comment to name one head, and lens_start cannot start every lens " +
          `that does not: ${stuck.map((s) => `${s} a person writes a newer override comment for this head`).join("; ")}`,
      };
    }
  }
  if (review?.mode === "shadow") {
    const extra = need.filter((lens) => !named.has(lens.key)).map((lens) => lens.name);
    if (extra.length > 0) {
      notes.push(
        `#${pull.number}: in shadow the gate requires only its required-lenses input; lens_start also starts ` +
          `${extra.join(", ")} of the path rules on purpose (the trial shadow is for); each costs a child slot ` +
          "and its runner's usage, Fable usage for a lens that runs on Fable",
      );
    }
  }
  const earlier = new Map<string, string[]>();
  for (const [name, sha] of history) {
    // A read of this very head that does not count (edited or hidden) is no earlier round.
    if (sha === null || sha === head) continue;
    const list = earlier.get(lensKey(name)) ?? [];
    if (!list.includes(sha)) list.push(sha);
    earlier.set(lensKey(name), list);
  }
  // A read of this head that counted (no edit, hide or unread flag; in enforce with findings judged, a finding or a
  // findings section) and that a newer post of its lens replaced: the child that posted it did read the head.
  const replaced = new Map<string, number>();
  for (const post of headers) {
    if (post.sha?.toLowerCase() !== head || entries.get(post.key)?.at === post.at) continue;
    if (enforce && block.findings) {
      const [said, section] = readFindings(post.body);
      if (said.length === 0 && !section) continue;
    }
    replaced.set(post.key, (replaced.get(post.key) ?? 0) + 1);
  }
  return {
    read: { number: pull.number, head, round: rounds.round },
    need,
    done,
    earlier,
    replaced,
    notes,
  };
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
  return judgeRounds(review, [...history, ["lens_start", head]], {}, false, changeOf);
}

const digest = (text: string, n: number) =>
  NodeCrypto.createHash("sha256").update(text).digest("hex").slice(0, n);

/** The lens key in an id: URI-encoded; a name with a lone surrogate gets a digest mark no encoding can make. */
function idKey(key: string): string {
  if (key.isWellFormed()) return encodeURIComponent(key);
  return `${encodeURIComponent(key.toWellFormed())}%!${digest(JSON.stringify(key), 16)}`;
}

/**
 * The request id of one lens child: `lens/<repo>#<pr>/<head>/<lens key>` for one pull request; for a
 * batch, a digest of its sorted `<pr>@<head>` lines. The repo and heads are read lower-case, as GitHub
 * does. The same lens on the same heads gives the same id; a moved head or another set of pull requests
 * gives a new one. `attempt` (2, 3, ...) starts a lens again after its child ended without a report.
 */
export function lensRequestId(
  repo: string,
  key: string,
  reads: ReadonlyArray<Pick<LensStartRead, "number" | "head">>,
  attempt = 1,
): string {
  const lens = idKey(key);
  const again = attempt > 1 ? `/again-${attempt}` : "";
  const owner = repo.toLowerCase();
  const sorted = reads
    .map((r) => ({ number: r.number, head: r.head.toLowerCase() }))
    .toSorted((a, b) => a.number - b.number);
  const id =
    sorted.length === 1
      ? `lens/${owner}#${sorted[0]!.number}/${sorted[0]!.head}/${lens}${again}`
      : `lens/${owner}/batch-${digest(sorted.map((r) => `${r.number}@${r.head}`).join("\n"), 24)}/${lens}${again}`;
  return id.length <= REQUEST_ID_MAX ? id : `lens/${digest(id, 64)}`;
}

/** Who runs a lens: its own runner by key; else the approach lens's role runner for the config's approach lens. */
function lensRunner(rules: OnRules, key: string, approach: boolean): ReviewRunner {
  if (Object.hasOwn(rules.lensRunners, key)) return reviewLensRunner(rules, key);
  return approach ? reviewLensRunner(rules, APPROACH_RUNNER) : rules.runner;
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
      "Read only: change no file, and never commit or push. Your report comment (below) is the only thing " +
      "you post; never edit or delete a comment.",
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
      ? "Post one report per pull request with the T3 tool `lens_report` (`pr`: its number, `body`: the report). " +
        "Never one report for several."
      : `Post your report with the T3 tool \`lens_report\` (\`pr: ${reads[0]!.number}\`, \`body\`: the report).`,
    `T3 writes its first line (\`lens: ${lens} · reviewer: <your model> · read at: <the head>\`) and posts it ` +
      "as a new comment while the head is the one above: write no `lens:` or `read at` line yourself, also where " +
      "the lens text above says to. Never post a report another way (not with `gh`).",
    "If lens_report refuses, fix what it names and call it again; if it cannot be fixed, post nothing and say " +
      "why in your answer.",
    "Write one line per finding, starting `blocker:`, `major:` or `minor:`; write `blocker: none` and " +
      "`major: none` when there is none.",
    `Keep each report under ${rules.reportMaxChars} characters.`,
    "If a head is not the one above when you read it, report nothing for it and say so in your answer.",
  ];
  return lines.join("\n");
}

/**
 * The plan for one `lens_start` call. Each pull request is planned or refused on its own, with its own base's
 * rules, so one closed pull request in a batch does not stop the others.
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
  const refused: LensStartRefusal[] = [];
  const asked = new Map<number, string | undefined>();
  for (const { number, head } of input.prs) {
    if (!Number.isSafeInteger(number) || number < 1) {
      return whole(`${JSON.stringify(number)} is not a pull request number`);
    }
    const said = head?.toLowerCase();
    if (asked.has(number) && asked.get(number) !== said) {
      return whole(`#${number} is named twice with different heads`);
    }
    asked.set(number, said);
  }
  if (asked.size > LENS_START_MAX_PRS) {
    return whole(`at most ${LENS_START_MAX_PRS} pull requests per call, not ${asked.size}`);
  }
  const alreadyRead: Array<{ pr: number; lens: string }> = [];
  const notes: string[] = [];
  const groups = new Map<
    string,
    {
      key: string;
      lens: KnownLens;
      text: string;
      runner: ReviewRunner;
      reads: Array<LensStartRead & { earlier: ReadonlyArray<string>; replaced: number }>;
    }
  >();
  const children = input.children ?? [];
  const running = new Map<string, LensStartEarlierChild>();
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
    const base = input.bases.get(pull.base);
    if (base === undefined) {
      refused.push({
        pr: number,
        reason: `its base branch ${pull.base} was not read: no lens is started rather than too few`,
      });
      continue;
    }
    let got: PullNeed;
    try {
      got = readPull(rules, base, input.overridePrefix, pull);
    } catch (error) {
      if (!isPyTextError(error)) throw error;
      got = {
        refused:
          error.where === undefined
            ? `the config or the gate's required-lenses input at ${pull.base} cannot be read as the gate reads ` +
              `it: ${error.message}; a person changes that text`
            : `its lens comments cannot be read as the gate reads them: ${error.message}. The gate reads that ` +
              `post; lens_start does not, so it starts nothing here until someone edits the post to drop ` +
              `${error.codePoints.join(", ")} or deletes it (hiding it is not enough), then calls lens_start again`,
      };
    }
    if ("refused" in got) {
      refused.push({ pr: number, reason: got.refused });
      continue;
    }
    for (const lens of got.done) alreadyRead.push({ pr: number, lens });
    const prompts = (lens: KnownLens) => (lens.builtIn ? FALLBACK_PROMPTS : base.prompts);
    const missing = got.need.filter((lens) => !Object.hasOwn(prompts(lens), lens.prompt));
    if (missing.length > 0) {
      refused.push({
        pr: number,
        reason:
          "no plan: the prompt of lens " +
          `${missing.map((l) => `${l.name} (${l.prompt || "no entry in lenses"})`).join(", ")} could not be ` +
          "read at the base",
      });
      continue;
    }
    notes.push(...got.notes);
    for (const lens of got.need) {
      // A child still running that reads this lens on this head is returned, never started twice.
      const busy = children.find(
        (c) =>
          c.running &&
          c.key === lens.key &&
          c.prs.some((r) => r.number === number && r.head.toLowerCase() === got.read.head),
      );
      if (busy !== undefined) {
        running.set(busy.requestId, busy);
        continue;
      }
      const text = prompts(lens)[lens.prompt]!;
      const runner = lensRunner(rules, lens.key, lens.approach);
      const id = JSON.stringify([lens.key, lens.name, text, runner]);
      const group = groups.get(id) ?? { key: lens.key, lens, text, runner, reads: [] };
      group.reads.push({
        ...got.read,
        earlier: got.earlier.get(lens.key) ?? [],
        replaced: got.replaced.get(lens.key) ?? 0,
      });
      groups.set(id, group);
    }
  }
  const ended = children.filter((c) => !c.running);
  const endedIds = new Set(ended.map((c) => c.requestId));
  /** The ids of this lens's ended children that read this pull request at this head, each once, in input order. */
  const endedAt = (key: string, number: number, head: string) => [
    ...new Set(
      ended
        .filter(
          (c) =>
            c.key === key &&
            c.prs.some((r) => r.number === number && r.head.toLowerCase() === head),
        )
        .map((c) => c.requestId),
    ),
  ];
  const starts: LensStartChild[] = [];
  for (const { key, lens, text, runner, reads: all } of groups.values()) {
    // The cap is per lens, pull request and head, whatever batch each child ran in: a new batch is no new start.
    // A child whose read of this head a newer post of its lens replaced (the head came back after a force-push) did
    // read it: it is no failed attempt. Each such read is taken off once; a hard bound of twice the cap stops a child
    // that posts a read and then replaces it, again and again.
    const reads: typeof all = [];
    const replaces = new Set<string>();
    let most = 0;
    let failedAtMost = 0;
    for (const read of all) {
      const ids = endedAt(key, read.number, read.head);
      const failed = Math.max(0, ids.length - read.replaced);
      if (failed >= LENS_START_MAX_ATTEMPTS || ids.length >= 2 * LENS_START_MAX_ATTEMPTS) {
        refused.push({
          pr: read.number,
          reason:
            failed >= LENS_START_MAX_ATTEMPTS
              ? `lens ${lens.name} is not started: ${failed} of its children ended with no read the gate ` +
                "counts at this head (no report, or one edited, hidden or unread since; the last: " +
                `${ids.at(-1)}); a person looks at why before it starts again, and a new head starts it afresh`
              : `lens ${lens.name} is not started: ${ids.length} of its children ended at this head, and a ` +
                `newer post of the lens replaced each read the gate counted (the last: ${ids.at(-1)}); a person ` +
                "looks at why before it starts again, and a new head starts it afresh",
        });
        continue;
      }
      reads.push(read);
      ids.forEach((id) => replaces.add(id));
      if (ids.length > most || (ids.length === most && failed > failedAtMost))
        failedAtMost = failed;
      most = Math.max(most, ids.length);
    }
    if (reads.length === 0) continue;
    const prs = reads.map(({ number, head, round }) => ({ number, head, round }));
    const which = prs.map((r) => `#${r.number}`).join(" ");
    let attempt = most + 1;
    // A record the caller lost can leave this attempt's id ended already: a new start never reuses an ended id.
    while (endedIds.has(lensRequestId(input.repo, key, prs, attempt))) attempt++;
    if (attempt > 1) {
      const gone = most - failedAtMost;
      notes.push(
        `lens ${lens.name} for ${which}: ${most} earlier ${most === 1 ? "child" : "children"} of it ended at ` +
          `${prs.length === 1 ? "this head" : "one of these heads"} ` +
          (gone === 0
            ? "with no read the gate counts"
            : `(${failedAtMost} with no read the gate counts, ${gone} whose read a newer post of the lens ` +
              "replaced)") +
          ` (the last: ${[...replaces].at(-1)}), so it starts again as attempt ${attempt}`,
      );
    }
    starts.push({
      requestId: lensRequestId(input.repo, key, prs, attempt),
      attempt,
      replaces: [...replaces],
      lens: lens.name,
      key,
      runner,
      title: `lens ${lens.name} ${which}`,
      prompt: lensPrompt(rules, input.repo, lens.name, text, reads),
      prs,
    });
  }
  return {
    starts,
    running: [...running.values()].map(({ requestId, key, prs }) => ({ requestId, key, prs })),
    refused,
    alreadyRead,
    notes,
  };
}

/**
 * What the caller stores for a child it starts, to pass in again as `LensStartInput.children`: its id, lens key and
 * the pull requests it reads (the id alone cannot be read back: a long one is a digest). `running`: false once the
 * child ended.
 */
export function earlierChild(child: LensStartChild, running: boolean): LensStartEarlierChild {
  return {
    requestId: child.requestId,
    key: child.key,
    prs: child.prs.map(({ number, head }) => ({ number, head })),
    running,
  };
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
