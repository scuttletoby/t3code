/**
 * Fork: one `lens_start` call over what it reads (a world: GitHub, the base's files, the caller's checkout, the lens
 * children started earlier) and how it starts a child (the delegate_task path, role `review`). lensStart.ts plans or
 * refuses; this file reads what the plan needs, starts the children it names, stores each one, and with `afterCi`
 * hands the start to a wait on the head's CI checks instead. The world is injected, so the tests read no GitHub.
 */
import type { EffectiveReviewRules, OrchestratorMcpDelegateTaskInput } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import {
  confirmLensStart,
  planLensStart,
  type LensStartChild,
  type LensStartEarlierChild,
  type LensStartPlan,
  type LensStartPost,
} from "./lensStart.ts";
import type { ReviewBlock } from "./plan.ts";

/** One comment, review or inline review comment as GitHub gave it, in the gate's list order. */
export interface RawPost {
  readonly body: string;
  /** createdAt, or a review's submittedAt; "" when GitHub gave none. */
  readonly when: string;
  readonly url: string | null;
  readonly login: string | null;
  /** GitHub says its author is a Bot (`__typename`). */
  readonly bot: boolean;
  /** GitHub's authorAssociation. */
  readonly association: string | null;
  /** includesCreatedEdit; null: not read. */
  readonly edited: boolean | null;
  /** isMinimized; null: not read. */
  readonly hidden: boolean | null;
}

export interface PullRead {
  readonly number: number;
  readonly state: "open" | "closed" | "merged";
  readonly head: string;
  readonly base: string;
  readonly headBranch: string;
  readonly posts: ReadonlyArray<RawPost>;
}

/** The caller's checkout: its branch (null: detached), commit, uncommitted changes, commits the PR head lacks. */
export interface CheckoutState {
  readonly branch: string | null;
  readonly head: string;
  readonly dirty: boolean;
  /** True when it holds commits the pull request's head does not (not pushed). */
  readonly ahead: boolean;
}

/** A lens child as lens_start stores it: lensStart's `earlierChild` plus the task it is. */
export interface StoredChild {
  readonly requestId: string;
  readonly key: string;
  readonly prs: ReadonlyArray<{ readonly number: number; readonly head: string }>;
  readonly taskId: string;
  readonly childThreadId: string | null;
  readonly parentThreadId: string;
  readonly startedAt: string;
}

export type StartAnswer =
  | { readonly type: "started"; readonly taskId: string; readonly childThreadId: string | null }
  | { readonly type: "refused"; readonly code: string; readonly message: string };

export type WaitForCiAnswer =
  | { readonly type: "waiting"; readonly waitId: string }
  | { readonly type: "met"; readonly outcome: string; readonly detail: string }
  | { readonly type: "refused"; readonly message: string };

/** A read that failed: lens_start starts nothing and says why. */
export class LensReadError extends Schema.TaggedError<LensReadError>()("LensReadError", {
  message: Schema.String,
}) {}

export interface LensStartWorld {
  readonly rules: EffectiveReviewRules;
  /** owner/name of the calling thread's project; null: it has none lens_start can read. */
  readonly repo: string | null;
  readonly readPull: (number: number) => Effect.Effect<PullRead | null, LensReadError>;
  /** A file's text at a ref; null: there is no such file. */
  readonly readFile: (ref: string, path: string) => Effect.Effect<string | null, LensReadError>;
  /** `[status, path]` or `[R|C<score>, old, new]` of base...head (the merge base, as the gate reads it). */
  readonly readChanges: (
    base: string,
    head: string,
  ) => Effect.Effect<ReadonlyArray<ReadonlyArray<string>>, LensReadError>;
  /** The caller's checkout; null: none, or not a git checkout. */
  readonly checkout: (prHead: string) => Effect.Effect<CheckoutState | null>;
  readonly children: {
    readonly list: Effect.Effect<ReadonlyArray<StoredChild>>;
    readonly add: (child: StoredChild) => Effect.Effect<void>;
  };
  /** Read from the child's thread at each call: one that stopped unseen is then ended. */
  readonly childRunning: (child: StoredChild) => Effect.Effect<boolean>;
  /** Starts one child through the delegate_task path (role `review`). */
  readonly start: (child: LensStartChild) => Effect.Effect<StartAnswer>;
  /** Registers the wait on the head's CI checks that starts the lenses (lens_start {afterCi}). */
  readonly waitForCi?: (args: {
    readonly number: number;
    readonly head: string;
    readonly checks: ReadonlyArray<string>;
  }) => Effect.Effect<WaitForCiAnswer>;
  /** The thread that starts the children; stored with each one. */
  readonly parentThreadId?: string;
  readonly now?: Effect.Effect<string>;
}

export interface LensStartRequest {
  readonly pr: number;
  /** The head the caller expects; another head is refused. */
  readonly head?: string | undefined;
  readonly afterCi?: boolean | undefined;
}

export interface LensStartResult {
  readonly pr: number;
  readonly head: string | null;
  readonly state: "started" | "nothing" | "refused" | "waiting";
  readonly started: ReadonlyArray<{
    readonly lens: string;
    readonly requestId: string;
    readonly taskId: string;
    readonly attempt: number;
  }>;
  readonly running: ReadonlyArray<{ readonly requestId: string; readonly taskId: string | null }>;
  readonly alreadyRead: ReadonlyArray<string>;
  readonly refused: ReadonlyArray<string>;
  readonly notes: ReadonlyArray<string>;
  readonly waitId?: string;
  readonly next: string;
}

/** What lens_start reads from the queue config at the base (pr-queue README, Config). */
export interface QueueConfig {
  readonly requiredChecks: ReadonlyArray<string>;
  readonly headCheck: string;
  readonly botLogins: ReadonlyArray<string>;
  readonly lensAssociations: ReadonlyArray<string>;
  readonly overridePrefix: string;
  readonly overrideAssociation: string;
  readonly review: ReviewBlock | null;
}

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isStrings = (value: unknown): value is ReadonlyArray<string> =>
  Array.isArray(value) && value.every((item) => typeof item === "string");

/**
 * The keys lens_start uses, read leniently (the gate's own parser is strict and refuses what this lets through, so a
 * bad config fails at the gate, never passes): a missing or wrong key is a refusal text.
 */
export function parseQueueConfig(text: string): QueueConfig | string {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (error) {
    return `it is not JSON (${error instanceof Error ? error.message : String(error)})`;
  }
  if (!isRecord(raw)) return "it is not a JSON object";
  const override = raw.override;
  if (!isStrings(raw.required_checks) || raw.required_checks.length === 0) {
    return "`required_checks` is not a list of check names";
  }
  if (typeof raw.head_check !== "string") return "`head_check` is not a check name";
  if (!isStrings(raw.bot_logins)) return "`bot_logins` is not a list of logins";
  if (!isStrings(raw.lens_author_associations)) {
    return "`lens_author_associations` is not a list";
  }
  if (
    !isRecord(override) ||
    typeof override.prefix !== "string" ||
    typeof override.author_association !== "string"
  ) {
    return "`override` has no prefix and author_association";
  }
  const review = raw.review;
  if (review !== undefined) {
    const rounds = isRecord(review) ? review.rounds : undefined;
    if (
      !isRecord(review) ||
      !Array.isArray(review.rules) ||
      !isRecord(review.lenses) ||
      !isRecord(rounds) ||
      typeof rounds.approach_at !== "number" ||
      typeof rounds.max !== "number" ||
      typeof rounds.approach_lens !== "string" ||
      Object.values(review.lenses).some(
        (lens) => !isRecord(lens) || typeof lens.prompt !== "string",
      )
    ) {
      return "its `review` block is not one the gate reads";
    }
  }
  return {
    requiredChecks: raw.required_checks,
    headCheck: raw.head_check,
    botLogins: raw.bot_logins,
    lensAssociations: raw.lens_author_associations,
    overridePrefix: override.prefix,
    overrideAssociation: override.author_association,
    review: review === undefined ? null : (review as unknown as ReviewBlock),
  };
}

/** The checks CI must pass before the lenses start: the config's required checks but the gate's own. */
export const ciChecksOf = (config: QueueConfig): ReadonlyArray<string> =>
  config.requiredChecks.filter((name) => name !== config.headCheck);

const escapeRegExp = (text: string) => text.replaceAll(/[.*+?^${}()|[\]\\]/gu, "\\$&");

/**
 * The posts as the gate counts them (lens.Review: is_bot, counts, override_sha), in its order: a bot's post never
 * counts; a valid override (its author association, never edited, not hidden) is kept with its sha; any other post
 * counts only with a lens author association.
 */
function lensPosts(raw: ReadonlyArray<RawPost>, config: QueueConfig): LensStartPost[] {
  const bots = config.botLogins.map((login) => login.toLowerCase());
  const overrideRe = new RegExp(`^${escapeRegExp(config.overridePrefix)} ([0-9a-f]{40}):`, "u");
  const posts: LensStartPost[] = [];
  for (const post of raw) {
    const login = (post.login ?? "").toLowerCase();
    if (post.bot || bots.includes(login) || login.endsWith("[bot]")) continue;
    const where = post.url ?? `by ${post.login ?? "?"} at ${post.when || "?"}`;
    const base: LensStartPost = {
      body: post.body,
      edited: post.edited !== false,
      hidden: post.hidden === true,
      where,
      when: post.when,
      ...(post.edited === null || post.hidden === null ? { unread: true } : {}),
    };
    const sha =
      post.association === config.overrideAssociation &&
      post.edited === false &&
      post.hidden !== true
        ? overrideRe.exec(post.body)?.[1]
        : undefined;
    if (sha !== undefined) {
      posts.push({
        ...base,
        override: sha,
        ...(post.login === null ? {} : { author: post.login }),
      });
    } else if (post.association !== null && config.lensAssociations.includes(post.association)) {
      posts.push(base);
    }
  }
  return posts;
}

/** The delegate_task call of one lens child: read-only review child, the server's prompt, its lens's runner. */
export function delegateInputOf(child: LensStartChild): OrchestratorMcpDelegateTaskInput {
  return {
    task: child.prompt,
    title: child.title,
    role: "review",
    mode: "async",
    clientRequestId: child.requestId,
    target: {
      driverKind: child.runner.driver,
      model: child.runner.model,
      options: [{ id: "effort", value: child.runner.effort }],
    },
  } as OrchestratorMcpDelegateTaskInput;
}

/**
 * The delegate_task request namespace of lens_start's children: one per orchestrator thread, not per provider session,
 * so the same lens id gives the same task after a new session or a restart (and from the after-CI wait).
 */
export const lensStartNamespace = (threadId: string) => `lens_start:${threadId}`;

const short = (sha: string) => sha.slice(0, 7);

const refusedResult = (
  pr: number,
  head: string | null,
  reasons: ReadonlyArray<string>,
): LensStartResult => ({
  pr,
  head,
  state: "refused",
  started: [],
  running: [],
  alreadyRead: [],
  refused: reasons,
  notes: [],
  next: `Nothing started. ${reasons.join(" ")}`,
});

/** What the plan needs, read from GitHub and the base; a string: why nothing can start. */
const readPlan = Effect.fn("lensStart.readPlan")(function* (
  world: LensStartWorld,
  request: LensStartRequest,
  repo: string,
) {
  const pull = yield* world.readPull(request.pr);
  if (pull === null) return { refused: `${repo} has no pull request #${request.pr}` } as const;
  const head = pull.head.toLowerCase();
  // A head that is not pushed is not the one a lens would read.
  if (pull.state === "open") {
    const local = yield* world.checkout(head);
    if (local !== null && local.branch === pull.headBranch) {
      if (local.dirty) {
        return {
          refused: `your checkout of ${pull.headBranch} has uncommitted changes: commit and push them, then call lens_start again (a lens reads the pushed head ${short(head)})`,
        } as const;
      }
      if (local.ahead) {
        return {
          refused: `your checkout of ${pull.headBranch} is at ${short(local.head)}, with commits not pushed (the pull request's head is ${short(head)}): push first, then call lens_start again`,
        } as const;
      }
    }
  }
  const rules = world.rules;
  if (rules.preset === "off") return { refused: "review rules are off" } as const;
  const configText = yield* world.readFile(pull.base, rules.configPath);
  if (configText === null) {
    return {
      refused: `the base ${pull.base} has no ${rules.configPath} (Settings: review rules, configPath): no lens is started rather than too few`,
    } as const;
  }
  const config = parseQueueConfig(configText);
  if (typeof config === "string") {
    return {
      refused: `${rules.configPath} at ${pull.base} cannot be read: ${config}; no lens is started rather than too few`,
    } as const;
  }
  const prompts: Record<string, string> = {};
  for (const { prompt } of Object.values(config.review?.lenses ?? {})) {
    if (Object.hasOwn(prompts, prompt)) continue;
    const text = yield* world.readFile(pull.base, prompt);
    if (text !== null) prompts[prompt] = text;
  }
  const changes = yield* world.readChanges(pull.base, head);
  return { pull, head, config, prompts, changes } as const;
});

/** One `lens_start` call. Every read problem is a refusal, never a partial start. */
export const runLensStart = Effect.fn("lensStart.run")(function* (
  world: LensStartWorld,
  request: LensStartRequest,
) {
  const { rules } = world;
  if (rules.preset === "off") {
    return refusedResult(request.pr, null, [
      "review rules are off for this project (Settings: review rules); lens_start starts nothing",
    ]);
  }
  if (world.repo === null) {
    return refusedResult(request.pr, null, [
      "this thread's project has no GitHub repository lens_start can read (its remote)",
    ]);
  }
  const repo = world.repo;
  const read = yield* readPlan(world, request, repo).pipe(
    Effect.catch((error: LensReadError) => Effect.succeed({ refused: error.message } as const)),
  );
  if ("refused" in read) return refusedResult(request.pr, null, [read.refused]);
  const { pull, head, config, prompts, changes } = read;

  // Earlier children of this pull request, read before the plan (lensStart.ts: a child that posts and ends between
  // the two reads is then seen running or its read is seen, never neither).
  const stored = (yield* world.children.list).filter((child) =>
    child.prs.some((pr) => pr.number === request.pr),
  );
  const children: LensStartEarlierChild[] = [];
  for (const child of stored) {
    children.push({
      requestId: child.requestId,
      key: child.key,
      prs: child.prs,
      running: yield* world.childRunning(child),
    });
  }
  const enforce = config.review?.mode === "enforce";
  const notes: string[] = [];
  if (enforce) {
    notes.push(
      `T3 does not read the gate caller's required-lenses input; in enforce the config's path rules at ${pull.base} name the lenses, so lens_start starts those`,
    );
  }
  const plan: LensStartPlan = planLensStart({
    rules,
    repo,
    bases: new Map([
      [pull.base, { review: config.review, prompts, requiredLenses: enforce ? [] : null }],
    ]),
    overridePrefix: config.overridePrefix,
    prs: [{ number: request.pr, ...(request.head === undefined ? {} : { head: request.head }) }],
    pulls: new Map([
      [
        request.pr,
        {
          number: pull.number,
          state: pull.state,
          head,
          base: pull.base,
          changes,
          posts: lensPosts(pull.posts, config),
        },
      ],
    ]),
    children,
  });
  const byId = new Map(stored.map((child) => [child.requestId, child]));
  const running = plan.running.map(({ requestId }) => ({
    requestId,
    taskId: byId.get(requestId)?.taskId ?? null,
  }));
  const alreadyRead = plan.alreadyRead.map(({ lens }) => lens);
  const refused = plan.refused.map(({ reason }) => reason);
  notes.push(...plan.notes);
  const base = { pr: request.pr, head, running, alreadyRead, notes };

  if (refused.length > 0 && plan.starts.length === 0) {
    return { ...refusedResult(request.pr, head, refused), ...base, refused };
  }
  if (plan.starts.length === 0) {
    return {
      ...base,
      state: "nothing",
      started: [],
      refused,
      next:
        running.length > 0
          ? "Lens children for this head are still running; their results wake this thread. Nothing new started."
          : `Every lens the gate needs already read ${short(head)}. Nothing started.`,
    } satisfies LensStartResult;
  }

  const checks = ciChecksOf(config);
  if (request.afterCi === true && checks.length === 0) {
    // The gate requires no check but its own: no CI to wait for, so the head is as ready as a green one.
    notes.push(
      `${rules.configPath} at ${pull.base} requires no check but ${config.headCheck}, so there is no CI to wait for: the lenses start now`,
    );
  } else if (request.afterCi === true) {
    if (world.waitForCi === undefined) {
      return {
        ...refusedResult(request.pr, head, [
          "this server cannot wait for CI (waits are not available)",
        ]),
        ...base,
      };
    }
    const waited = yield* world.waitForCi({ number: request.pr, head, checks });
    if (waited.type === "refused") {
      return {
        ...refusedResult(request.pr, head, [`the wait for CI was refused: ${waited.message}`]),
        ...base,
      };
    }
    if (waited.type === "waiting") {
      return {
        ...base,
        state: "waiting",
        started: [],
        refused,
        waitId: waited.waitId,
        next: `Wait ${waited.waitId} watches ${checks.join(", ")} at ${short(head)}. End your turn: when they pass, the server starts the lenses (${plan.starts.map((c) => c.lens).join(", ")}); red, a new head or the deadline start nothing, and the wake says why.`,
      } satisfies LensStartResult;
    }
    if (waited.outcome !== "passed") {
      return {
        ...base,
        state: "nothing",
        started: [],
        refused,
        next: `CI at ${short(head)} ended ${waited.outcome}: ${waited.detail} No lens started. Fix and push, then call lens_start {afterCi: true} for the new head.`,
      } satisfies LensStartResult;
    }
    // Already green: start now, below.
  }

  // Just before the start: the head GitHub names now; a moved head starts nothing.
  const again = yield* world.readPull(request.pr).pipe(Effect.orElseSucceed(() => undefined));
  const confirmed =
    again === undefined
      ? {
          ...plan,
          starts: [],
          refused: [
            ...plan.refused,
            {
              pr: request.pr,
              reason:
                "its head could not be read again just before the start: call lens_start again",
            },
          ],
        }
      : confirmLensStart(
          plan,
          new Map([[request.pr, again === null || again.state !== "open" ? null : again.head]]),
        );
  const allRefused = confirmed.refused.map(({ reason }) => reason);
  const started: Array<LensStartResult["started"][number]> = [];
  /** Runners the model policy refused: their other children are not started either. */
  const forbidden = new Map<string, string>();
  for (const child of confirmed.starts) {
    const runnerKey = `${child.runner.driver}\u0000${child.runner.model}\u0000${child.runner.effort}`;
    const said = forbidden.get(runnerKey);
    if (said !== undefined) {
      allRefused.push(
        `lens ${child.lens} is not started: its runner (${child.runner.model}, effort ${child.runner.effort}) was refused: ${said}`,
      );
      continue;
    }
    const answer = yield* world.start(child);
    if (answer.type === "refused") {
      if (answer.code === "model_unavailable") forbidden.set(runnerKey, answer.message);
      allRefused.push(`lens ${child.lens} is not started: ${answer.message}`);
      continue;
    }
    yield* world.children.add({
      requestId: child.requestId,
      key: child.key,
      prs: child.prs.map(({ number, head: at }) => ({ number, head: at })),
      taskId: answer.taskId,
      childThreadId: answer.childThreadId,
      parentThreadId: world.parentThreadId ?? "",
      startedAt: world.now === undefined ? "" : yield* world.now,
    });
    started.push({
      lens: child.lens,
      requestId: child.requestId,
      taskId: answer.taskId,
      attempt: child.attempt,
    });
  }
  if (started.length === 0) {
    return { ...refusedResult(request.pr, head, allRefused), ...base, refused: allRefused };
  }
  return {
    ...base,
    state: "started",
    started,
    refused: allRefused,
    next: `Started ${started.map((s) => `lens ${s.lens}`).join(", ")} for #${request.pr} at ${short(head)}. Their results wake this thread; do not wait on them or poll them.`,
  } satisfies LensStartResult;
});
