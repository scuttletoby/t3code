/**
 * Fork (work panel): one shared, in-memory read of repositories' workflow runs per server.
 * GitHub is asked at most once a minute in all, later on a low budget, a refusal or no answer.
 * A pull request's CI comes from the same read (its head's runs): no call of its own.
 */
import type { ForkPullRequestRuns, ForkRepoQueue, ThreadPullRequestLink } from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import * as Schema from "effect/Schema";
import * as Semaphore from "effect/Semaphore";

/** The latest runs one read lists (GitHub's page maximum). */
const RUNS_PER_READ = 100;
/** The fewest milliseconds between two GitHub calls of this server, for all repositories. */
export const CALL_INTERVAL_MS = 60_000;
/** A repository's runs are read again only once they are this old. */
const FRESH_MS = 60_000;
/** Counts older than this are not shown: the line says unknown. */
const STALE_MS = 10 * 60_000;
/** Below this share of the budget left, the server waits for the budget's reset. */
const LOW_BUDGET_SHARE = 0.2;
/** The longest wait for any reason (a reset GitHub names further away is capped). */
const MAX_WAIT_MS = 60 * 60_000;
/** After a refused credential or a repository GitHub cannot see. */
const REFUSED_WAIT_MS = 15 * 60_000;
/** GitHub did not answer: 2, 4, 8 then 15 minutes. */
const DOWN_WAITS_MS = [2 * 60_000, 4 * 60_000, 8 * 60_000, 15 * 60_000] as const;
/** Repositories kept in the cache; the one read longest ago goes first. */
const MAX_REPOSITORIES = 20;

/** What GitHub (or the way to it) gave for one read. */
export type GitHubRunsAnswer =
  | {
      readonly _tag: "Response";
      readonly status: number;
      /** Lower-case names. */
      readonly headers: Readonly<Record<string, string>>;
      readonly body: string;
    }
  | { readonly _tag: "Unreachable"; readonly reason: string };

/** One read of `repos/<repository>/actions/runs` on `host`. */
export type GitHubRunsSource = (input: {
  readonly host: string;
  readonly repository: string;
  readonly perPage: number;
}) => Effect.Effect<GitHubRunsAnswer>;

const WorkflowRun = Schema.Struct({
  id: Schema.Number,
  name: Schema.optional(Schema.NullOr(Schema.String)),
  workflow_id: Schema.optional(Schema.NullOr(Schema.Number)),
  head_branch: Schema.optional(Schema.NullOr(Schema.String)),
  head_sha: Schema.optional(Schema.NullOr(Schema.String)),
  status: Schema.optional(Schema.NullOr(Schema.String)),
  conclusion: Schema.optional(Schema.NullOr(Schema.String)),
  created_at: Schema.String,
  html_url: Schema.optional(Schema.NullOr(Schema.String)),
});
const decodeRuns = Schema.decodeUnknownOption(
  Schema.fromJsonString(Schema.Struct({ workflow_runs: Schema.Array(WorkflowRun) })),
);

/** The part of a run the panel counts. */
interface Run {
  readonly workflow: string;
  readonly headBranch: string | null;
  readonly headSha: string | null;
  readonly kind: "queued" | "running" | "failed" | "passed" | "other";
  readonly createdAt: number;
  readonly url: string | null;
}

// `waiting`, `requested`, `pending` and `action_required` have not started: they wait in line or
// on a person, so they count as queued.
const QUEUED_STATUSES = new Set(["queued", "waiting", "requested", "pending", "action_required"]);
const FAILED_CONCLUSIONS = new Set(["failure", "timed_out", "startup_failure"]);

const runKind = (status: string | null, conclusion: string | null): Run["kind"] => {
  if (status === "in_progress") return "running";
  if (status !== null && QUEUED_STATUSES.has(status)) return "queued";
  if (status === "completed") {
    if (conclusion === "success") return "passed";
    if (conclusion !== null && FAILED_CONCLUSIONS.has(conclusion)) return "failed";
  }
  return "other";
};

/** The runs in a 200 answer's body; `null` when the body is not GitHub's runs list. */
export const parseRuns = (body: string): ReadonlyArray<Run> | null =>
  Option.match(decodeRuns(body), {
    onNone: () => null,
    onSome: ({ workflow_runs }) =>
      workflow_runs.flatMap((run): ReadonlyArray<Run> => {
        const createdAt = Date.parse(run.created_at);
        if (!Number.isFinite(createdAt)) return [];
        return [
          {
            workflow: String(run.workflow_id ?? run.name ?? run.id),
            headBranch: run.head_branch ?? null,
            headSha: run.head_sha ?? null,
            kind: runKind(run.status ?? null, run.conclusion ?? null),
            createdAt,
            url: run.html_url ?? null,
          },
        ];
      }),
  });

const oldestQueued = (runs: ReadonlyArray<Run>) =>
  runs
    .filter((run) => run.kind === "queued")
    .reduce<Run | null>(
      (oldest, run) => (oldest === null || run.createdAt < oldest.createdAt ? run : oldest),
      null,
    );

/** A pull request's head runs: the latest run of each workflow on its head commit, else branch. */
export const pullRequestRuns = (
  runs: ReadonlyArray<Run>,
  link: Pick<ThreadPullRequestLink, "host" | "repository" | "number" | "snapshot" | "watch">,
): ForkPullRequestRuns | null => {
  const headSha = link.watch?.headSha ?? null;
  const headBranch = link.snapshot?.headBranch ?? null;
  const onHead = runs.filter((run) =>
    headSha !== null
      ? run.headSha === headSha
      : headBranch !== null && run.headBranch === headBranch,
  );
  if (onHead.length === 0) return null;
  const latest = new Map<string, Run>();
  for (const run of onHead) {
    const kept = latest.get(run.workflow);
    if (kept === undefined || run.createdAt > kept.createdAt) latest.set(run.workflow, run);
  }
  const heads = [...latest.values()];
  const count = (kind: Run["kind"]) => heads.filter((run) => run.kind === kind).length;
  const queued = oldestQueued(heads);
  const pick =
    queued ??
    heads.find((run) => run.kind === "running") ??
    heads.find((run) => run.kind === "failed") ??
    heads.toSorted((a, b) => b.createdAt - a.createdAt)[0] ??
    null;
  return {
    host: link.host,
    repository: link.repository,
    number: link.number,
    queued: count("queued"),
    running: count("running"),
    failed: count("failed"),
    passed: count("passed"),
    oldestQueuedAt: queued?.createdAt ?? null,
    url: pick?.url ?? null,
  };
};

interface Entry {
  readonly host: string;
  readonly repository: string;
  readonly good: { readonly readAt: number; readonly runs: ReadonlyArray<Run> } | null;
  /** Why the latest read gave no counts; `null` once one did. */
  readonly problem: string | null;
  readonly attemptedAt: number;
  /** This repository waits this long whatever the server's next call (a refusal of its own). */
  readonly retryAt: number;
}

interface CacheState {
  readonly entries: ReadonlyMap<string, Entry>;
  readonly nextCallAt: number;
  /** GitHub has not answered this many reads in a row. */
  readonly downStreak: number;
}

export const REASON_NOT_READ =
  "Not read yet: this server asks GitHub at most once a minute for all repositories.";
export const REASON_NOT_GITHUB = "Only github.com repositories are read.";
const REASON_BAD_NAME = "The repository name is not owner/name.";
const REASON_STALE = "The last counts GitHub gave are more than 10 minutes old.";

const keyOf = (host: string, repository: string) =>
  `${host.toLowerCase()}\0${repository.toLowerCase()}`;
const REPOSITORY_NAME = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

const header = (headers: Readonly<Record<string, string>>, name: string) => {
  const value = Number(headers[name]);
  return Number.isFinite(value) ? value : null;
};

/** What one answer means for the cache: counts, or why not, and how long the server waits. */
const classifyAnswer = (
  answer: GitHubRunsAnswer,
  now: number,
  downStreak: number,
):
  | {
      readonly _tag: "Runs";
      readonly runs: ReadonlyArray<Run>;
      readonly waitUntil: number;
      readonly lowBudget: boolean;
    }
  | {
      readonly _tag: "Problem";
      readonly reason: string;
      /** The server's next call (`global`) or only this repository's. */
      readonly scope: "global" | "repository";
      readonly waitUntil: number;
      readonly down: boolean;
    } => {
  const cap = (at: number) => Math.min(Math.max(at, now + CALL_INTERVAL_MS), now + MAX_WAIT_MS);
  const down = (reason: string) => ({
    _tag: "Problem" as const,
    reason,
    scope: "global" as const,
    waitUntil: now + DOWN_WAITS_MS[Math.min(downStreak, DOWN_WAITS_MS.length - 1)]!,
    down: true,
  });
  if (answer._tag === "Unreachable") return down(`GitHub did not answer: ${answer.reason}`);
  const { status, headers, body } = answer;
  const remaining = header(headers, "x-ratelimit-remaining");
  const limit = header(headers, "x-ratelimit-limit");
  const resetAt = (header(headers, "x-ratelimit-reset") ?? 0) * 1_000;
  const retryAfter = header(headers, "retry-after");
  const rateLimited =
    status === 429 ||
    (status === 403 && (remaining === 0 || retryAfter !== null || /rate limit/i.test(body)));
  if (rateLimited) {
    const at = retryAfter !== null ? now + retryAfter * 1_000 : resetAt > now ? resetAt : 0;
    return {
      _tag: "Problem",
      reason: "GitHub refused the read: this server's rate limit is used up.",
      scope: "global",
      waitUntil: at === 0 ? now + REFUSED_WAIT_MS : cap(at),
      down: false,
    };
  }
  if (status === 401) {
    return {
      _tag: "Problem",
      reason: "GitHub refused this server's credential.",
      scope: "global",
      waitUntil: now + REFUSED_WAIT_MS,
      down: false,
    };
  }
  if (status === 403 || status === 404) {
    return {
      _tag: "Problem",
      reason: "GitHub cannot show this repository's runs to this server's credential.",
      scope: "repository",
      waitUntil: now + REFUSED_WAIT_MS,
      down: false,
    };
  }
  if (status < 200 || status >= 300) return down(`GitHub answered HTTP ${status}.`);
  const runs = parseRuns(body);
  if (runs === null) return down("GitHub's answer was not a list of runs.");
  const lowBudget =
    remaining !== null && limit !== null && limit > 0 && remaining < limit * LOW_BUDGET_SHARE;
  return {
    _tag: "Runs",
    runs,
    lowBudget,
    waitUntil: lowBudget && resetAt > now ? cap(resetAt) : now + CALL_INTERVAL_MS,
  };
};

interface RepoQueueCache {
  /**
   * Each repository's line, in the order asked, after at most one GitHub call (for the
   * repository that waited longest, when the server's next call is due).
   */
  readonly read: (
    repositories: ReadonlyArray<{ readonly host: string; readonly repository: string }>,
  ) => Effect.Effect<{
    readonly lines: ReadonlyArray<ForkRepoQueue>;
    /** The runs of each line that has counts, by `host\0repository` (lower case). */
    readonly runs: ReadonlyMap<string, ReadonlyArray<Run>>;
  }>;
}

export const repoQueueKey = keyOf;

export const makeRepoQueueCache = Effect.fn("ForkWorkPanel.makeRepoQueueCache")(function* (
  source: GitHubRunsSource,
) {
  const state = yield* Ref.make<CacheState>({ entries: new Map(), nextCallAt: 0, downStreak: 0 });
  // One read at a time, so two viewers arriving together cannot both spend the minute's call.
  const lock = yield* Semaphore.make(1);

  const callOnce = Effect.fn("ForkWorkPanel.repoQueue.call")(function* (
    wanted: ReadonlyArray<{ readonly host: string; readonly repository: string }>,
  ) {
    const now = yield* Clock.currentTimeMillis;
    const current = yield* Ref.get(state);
    if (now < current.nextCallAt) return;
    const due = wanted
      .map((repo) => ({ repo, entry: current.entries.get(keyOf(repo.host, repo.repository)) }))
      .filter(
        ({ entry }) =>
          entry === undefined || (entry.attemptedAt + FRESH_MS <= now && entry.retryAt <= now),
      )
      .toSorted((a, b) => (a.entry?.attemptedAt ?? -1) - (b.entry?.attemptedAt ?? -1))[0];
    if (due === undefined) return;
    const { host, repository } = due.repo;
    const answer = yield* source({ host, repository, perPage: RUNS_PER_READ });
    const after = yield* Clock.currentTimeMillis;
    const result = classifyAnswer(answer, after, current.downStreak);
    yield* Ref.update(state, (latest) => {
      const entries = new Map(latest.entries);
      const previous = entries.get(keyOf(host, repository));
      entries.set(keyOf(host, repository), {
        host,
        repository,
        good:
          result._tag === "Runs" ? { readAt: after, runs: result.runs } : (previous?.good ?? null),
        problem: result._tag === "Runs" ? null : result.reason,
        attemptedAt: after,
        retryAt: result._tag === "Problem" && result.scope === "repository" ? result.waitUntil : 0,
      });
      while (entries.size > MAX_REPOSITORIES) {
        const oldest = [...entries.entries()].toSorted(
          (a, b) => a[1].attemptedAt - b[1].attemptedAt,
        )[0];
        if (oldest === undefined) break;
        entries.delete(oldest[0]);
      }
      return {
        entries,
        nextCallAt:
          result._tag === "Problem" && result.scope === "repository"
            ? after + CALL_INTERVAL_MS
            : result.waitUntil,
        downStreak: result._tag === "Problem" && result.down ? latest.downStreak + 1 : 0,
      };
    });
  });

  const read: RepoQueueCache["read"] = Effect.fn("ForkWorkPanel.repoQueue.read")(
    function* (repositories) {
      const readable = repositories.filter(
        (repo) => repo.host.toLowerCase() === "github.com" && REPOSITORY_NAME.test(repo.repository),
      );
      yield* lock.withPermits(1)(callOnce(readable));
      const now = yield* Clock.currentTimeMillis;
      const { entries, nextCallAt } = yield* Ref.get(state);
      const runs = new Map<string, ReadonlyArray<Run>>();
      const lines = repositories.map((repo): ForkRepoQueue => {
        const base = { host: repo.host, repository: repo.repository };
        if (repo.host.toLowerCase() !== "github.com") {
          return { ...base, state: "unknown", reason: REASON_NOT_GITHUB };
        }
        if (!REPOSITORY_NAME.test(repo.repository)) {
          return { ...base, state: "unknown", reason: REASON_BAD_NAME };
        }
        const entry = entries.get(keyOf(repo.host, repo.repository));
        const next = Math.max(nextCallAt, entry?.retryAt ?? 0, now);
        if (entry?.good == null) {
          return {
            ...base,
            state: "unknown",
            reason: entry?.problem ?? REASON_NOT_READ,
            nextReadAt: next,
          };
        }
        if (entry.good.readAt + STALE_MS < now) {
          return {
            ...base,
            state: "unknown",
            reason: entry.problem === null ? REASON_STALE : `${entry.problem} ${REASON_STALE}`,
            readAt: entry.good.readAt,
            nextReadAt: next,
          };
        }
        const good = entry.good.runs;
        runs.set(keyOf(repo.host, repo.repository), good);
        const oldest = oldestQueued(good);
        return {
          ...base,
          state: "answered",
          ...(entry.problem === null ? {} : { reason: entry.problem }),
          readAt: entry.good.readAt,
          queued: good.filter((run) => run.kind === "queued").length,
          running: good.filter((run) => run.kind === "running").length,
          oldestQueuedAt: oldest?.createdAt ?? null,
          oldestQueuedUrl: oldest?.url ?? null,
          scanned: good.length,
          nextReadAt: next,
        };
      });
      return { lines, runs };
    },
  );

  return { read } satisfies RepoQueueCache;
});
