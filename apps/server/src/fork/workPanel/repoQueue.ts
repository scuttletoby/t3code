/**
 * Fork (work panel): one shared, in-memory read of repositories' workflow runs per server.
 * GitHub is asked at most once a minute in all, later on a low budget, a refusal or no answer.
 * A pull request's CI comes from the same read (its head's runs): no call of its own.
 */
// Each repository is read three ways in turn (the latest runs, then the queued and the running
// ones, which also find a re-run of a run older than the latest page), one call a minute for the
// whole server. So one panel's three repositories are each read fully every nine minutes, within
// `STALE_MS`; more repositories in view at once make lines go unknown for part of each cycle.
import type { ForkPullRequestRuns, ForkRepoQueue, ThreadPullRequestLink } from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Deferred from "effect/Deferred";
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

/** The three reads of a repository: the latest page, and every queued and every running run. */
const READ_KINDS = ["latest", "queued", "in_progress"] as const;
type ReadKind = (typeof READ_KINDS)[number];

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
  /** Only runs with this status; absent: the latest runs of every status. */
  readonly status?: "queued" | "in_progress";
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
  // A re-run keeps `created_at`; its own start is `run_started_at`.
  run_started_at: Schema.optional(Schema.NullOr(Schema.String)),
  html_url: Schema.optional(Schema.NullOr(Schema.String)),
  head_repository: Schema.optional(
    Schema.NullOr(Schema.Struct({ full_name: Schema.optional(Schema.NullOr(Schema.String)) })),
  ),
  pull_requests: Schema.optional(
    Schema.NullOr(Schema.Array(Schema.Struct({ number: Schema.optional(Schema.Number) }))),
  ),
});
const decodeRuns = Schema.decodeUnknownOption(
  Schema.fromJsonString(Schema.Struct({ workflow_runs: Schema.Array(WorkflowRun) })),
);

/** The part of a run the panel counts. */
interface Run {
  readonly id: number;
  readonly workflow: string;
  readonly headBranch: string | null;
  readonly headSha: string | null;
  /** `owner/name` of the head's repository; another fork's for a pull request from a fork. */
  readonly headRepository: string | null;
  /** The pull requests GitHub ties the run to (none for one from a fork). */
  readonly pullRequests: ReadonlyArray<number>;
  readonly kind: "queued" | "running" | "failed" | "passed" | "other";
  /** When this attempt started: `run_started_at`, else `created_at` (a re-run keeps that one). */
  readonly at: number;
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
        const started = run.run_started_at == null ? Number.NaN : Date.parse(run.run_started_at);
        const at = Number.isFinite(started) ? started : Date.parse(run.created_at);
        if (!Number.isFinite(at)) return [];
        return [
          {
            id: run.id,
            workflow: String(run.workflow_id ?? run.name ?? run.id),
            headBranch: run.head_branch ?? null,
            headSha: run.head_sha ?? null,
            headRepository: run.head_repository?.full_name ?? null,
            pullRequests: (run.pull_requests ?? []).flatMap((pr) =>
              pr.number === undefined ? [] : [pr.number],
            ),
            kind: runKind(run.status ?? null, run.conclusion ?? null),
            at,
            url: run.html_url ?? null,
          },
        ];
      }),
  });

const oldestQueued = (runs: ReadonlyArray<Run>) =>
  runs
    .filter((run) => run.kind === "queued")
    .reduce<Run | null>(
      (oldest, run) => (oldest === null || run.at < oldest.at ? run : oldest),
      null,
    );

/**
 * A pull request's head runs: the latest run of each workflow on its head commit; without a
 * watched commit, the runs GitHub ties to it, or its branch in its own repository (a branch of the
 * same name in another fork is not its).
 */
export const pullRequestRuns = (
  runs: ReadonlyArray<Run>,
  link: Pick<ThreadPullRequestLink, "host" | "repository" | "number" | "snapshot" | "watch">,
): ForkPullRequestRuns | null => {
  const headSha = link.watch?.headSha ?? null;
  const headBranch = link.snapshot?.headBranch ?? null;
  const onHead = runs.filter((run) =>
    headSha !== null
      ? run.headSha === headSha
      : run.pullRequests.includes(link.number) ||
        (headBranch !== null &&
          run.headBranch === headBranch &&
          run.headRepository?.toLowerCase() === link.repository.toLowerCase()),
  );
  if (onHead.length === 0) return null;
  const latest = new Map<string, Run>();
  for (const run of onHead) {
    const kept = latest.get(run.workflow);
    if (kept === undefined || run.at > kept.at) latest.set(run.workflow, run);
  }
  const heads = [...latest.values()];
  const count = (kind: Run["kind"]) => heads.filter((run) => run.kind === kind).length;
  const queued = oldestQueued(heads);
  const pick =
    queued ??
    heads.find((run) => run.kind === "running") ??
    heads.find((run) => run.kind === "failed") ??
    heads.toSorted((a, b) => b.at - a.at)[0] ??
    null;
  return {
    host: link.host,
    repository: link.repository,
    number: link.number,
    queued: count("queued"),
    running: count("running"),
    failed: count("failed"),
    passed: count("passed"),
    oldestQueuedAt: queued?.at ?? null,
    url: pick?.url ?? null,
  };
};

interface Entry {
  readonly host: string;
  readonly repository: string;
  readonly kind: ReadKind;
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
  /** The call under way, if any: its repository and when it is done. */
  readonly inFlight: { readonly repo: string; readonly done: Deferred.Deferred<void> } | null;
}

export const REASON_NOT_READ =
  "Not read yet: this server asks GitHub at most once a minute for all repositories.";
export const REASON_NOT_GITHUB = "Only github.com repositories are read.";
const REASON_BAD_NAME = "The repository name is not owner/name.";
const REASON_STALE = "The last counts GitHub gave are more than 10 minutes old.";
export const REASON_PARTIAL =
  "Re-runs of runs older than the latest 100 are not read yet: they come in the next minutes.";

const keyOf = (host: string, repository: string) =>
  `${host.toLowerCase()}\0${repository.toLowerCase()}`;
const entryKey = (repo: string, kind: ReadKind) => `${repo}\0${kind}`;
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
  const state = yield* Ref.make<CacheState>({
    entries: new Map(),
    nextCallAt: 0,
    downStreak: 0,
    inFlight: null,
  });
  // Held only to claim the minute's call, never during it, so a viewer whose answer is cached is
  // never held up by another viewer's call.
  const lock = yield* Semaphore.make(1);

  type Job = {
    readonly host: string;
    readonly repository: string;
    readonly kind: ReadKind;
    readonly start: number;
    readonly downStreak: number;
    readonly done: Deferred.Deferred<void>;
  };

  /** Claims the minute's call for the read that waited longest, marking it spent at once. */
  const claim = Effect.fn("ForkWorkPanel.repoQueue.claim")(function* (
    wanted: ReadonlyArray<{ readonly host: string; readonly repository: string }>,
  ) {
    const now = yield* Clock.currentTimeMillis;
    const current = yield* Ref.get(state);
    if (now < current.nextCallAt || current.inFlight !== null) return null;
    const candidates = wanted.flatMap((repo, order) =>
      READ_KINDS.map((kind, kindIndex) => {
        const key = entryKey(keyOf(repo.host, repo.repository), kind);
        return { repo, kind, key, order, kindIndex, entry: current.entries.get(key) };
      }),
    );
    // A latest page with no counts yet first (the line needs it), then the read waiting longest.
    const needed = (candidate: (typeof candidates)[number]) =>
      candidate.kind === "latest" && candidate.entry?.good == null ? 0 : 1;
    const due = candidates
      .filter(
        ({ entry }) =>
          entry === undefined || (entry.attemptedAt + FRESH_MS <= now && entry.retryAt <= now),
      )
      .toSorted(
        (a, b) =>
          needed(a) - needed(b) ||
          (a.entry?.attemptedAt ?? -1) - (b.entry?.attemptedAt ?? -1) ||
          a.kindIndex - b.kindIndex ||
          a.order - b.order,
      )[0];
    if (due === undefined) return null;
    const done = yield* Deferred.make<void>();
    const repo = keyOf(due.repo.host, due.repo.repository);
    yield* Ref.update(state, (latest) => {
      const entries = new Map(latest.entries);
      const previous = entries.get(due.key);
      entries.set(due.key, {
        host: due.repo.host,
        repository: due.repo.repository,
        kind: due.kind,
        good: previous?.good ?? null,
        problem: previous?.problem ?? null,
        attemptedAt: now,
        retryAt: previous?.retryAt ?? 0,
      });
      return { ...latest, entries, nextCallAt: now + CALL_INTERVAL_MS, inFlight: { repo, done } };
    });
    return {
      host: due.repo.host,
      repository: due.repo.repository,
      kind: due.kind,
      start: now,
      downStreak: current.downStreak,
      done,
    } satisfies Job;
  });

  /** The call and its record, whole: a viewer who leaves meanwhile does not cut it short. */
  const call = Effect.fn("ForkWorkPanel.repoQueue.callGitHub")(
    function* (job: Job) {
      const answer = yield* source({
        host: job.host,
        repository: job.repository,
        perPage: RUNS_PER_READ,
        ...(job.kind === "latest" ? {} : { status: job.kind }),
      });
      const after = yield* Clock.currentTimeMillis;
      const result = classifyAnswer(answer, after, job.downStreak);
      const key = entryKey(keyOf(job.host, job.repository), job.kind);
      yield* Ref.update(state, (latest) => {
        const entries = new Map(latest.entries);
        const previous = entries.get(key);
        entries.set(key, {
          host: job.host,
          repository: job.repository,
          kind: job.kind,
          good:
            result._tag === "Runs"
              ? { readAt: after, runs: result.runs }
              : (previous?.good ?? null),
          problem: result._tag === "Runs" ? null : result.reason,
          attemptedAt: job.start,
          retryAt:
            result._tag === "Problem" && result.scope === "repository" ? result.waitUntil : 0,
        });
        while (entries.size > MAX_REPOSITORIES * READ_KINDS.length) {
          const oldest = [...entries.entries()].toSorted(
            (a, b) => a[1].attemptedAt - b[1].attemptedAt,
          )[0];
          if (oldest === undefined) break;
          entries.delete(oldest[0]);
        }
        // The minute counts from the call's start, so a viewer refreshing every minute is not
        // pushed to every other minute; the longer waits count from its end.
        const nextCallAt =
          result._tag === "Runs"
            ? result.lowBudget
              ? result.waitUntil
              : job.start + CALL_INTERVAL_MS
            : result.scope === "repository"
              ? job.start + CALL_INTERVAL_MS
              : result.waitUntil;
        return {
          entries,
          nextCallAt,
          downStreak: result._tag === "Problem" && result.down ? latest.downStreak + 1 : 0,
          inFlight: null,
        };
      });
    },
    (effect, job) =>
      effect.pipe(
        Effect.ensuring(
          Ref.update(state, (latest) =>
            latest.inFlight?.done === job.done ? { ...latest, inFlight: null } : latest,
          ).pipe(Effect.andThen(Deferred.succeed(job.done, undefined))),
        ),
      ),
  );

  const read: RepoQueueCache["read"] = Effect.fn("ForkWorkPanel.repoQueue.read")(
    function* (repositories) {
      const readable = repositories.filter(
        (repo) => repo.host.toLowerCase() === "github.com" && REPOSITORY_NAME.test(repo.repository),
      );
      const job = yield* lock.withPermits(1)(claim(readable));
      if (job !== null) yield* Effect.forkDetach(call(job));
      // Wait only for a call about one of this viewer's repositories (its own claim included).
      const inFlight = (yield* Ref.get(state)).inFlight;
      if (
        inFlight !== null &&
        readable.some((repo) => keyOf(repo.host, repo.repository) === inFlight.repo)
      ) {
        yield* Deferred.await(inFlight.done);
      }
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
        const repoKey = keyOf(repo.host, repo.repository);
        const reads = READ_KINDS.flatMap((kind) => {
          const entry = entries.get(entryKey(repoKey, kind));
          return entry === undefined ? [] : [entry];
        });
        const latest = reads.find((entry) => entry.kind === "latest");
        const problem =
          reads.toSorted((a, b) => b.attemptedAt - a.attemptedAt).find((entry) => entry.problem)
            ?.problem ?? null;
        const next = Math.max(nextCallAt, ...reads.map((entry) => entry.retryAt), now);
        if (latest?.good == null) {
          return {
            ...base,
            state: "unknown",
            reason: problem ?? REASON_NOT_READ,
            nextReadAt: next,
          };
        }
        if (latest.good.readAt + STALE_MS < now) {
          return {
            ...base,
            state: "unknown",
            reason: problem === null ? REASON_STALE : `${problem} ${REASON_STALE}`,
            readAt: latest.good.readAt,
            nextReadAt: next,
          };
        }
        // Each run once, as the newest read saw it; a read older than STALE_MS is left out.
        const byId = new Map<number, { readonly run: Run; readonly readAt: number }>();
        for (const entry of reads) {
          if (entry.good === null || entry.good.readAt + STALE_MS < now) continue;
          for (const run of entry.good.runs) {
            const kept = byId.get(run.id);
            if (kept === undefined || entry.good.readAt > kept.readAt) {
              byId.set(run.id, { run, readAt: entry.good.readAt });
            }
          }
        }
        const merged = [...byId.values()].map(({ run }) => run);
        runs.set(repoKey, merged);
        const oldest = oldestQueued(merged);
        const missing = READ_KINDS.some(
          (kind) => !reads.some((entry) => entry.kind === kind && entry.good !== null),
        );
        const reason = problem ?? (missing ? REASON_PARTIAL : null);
        return {
          ...base,
          state: "answered",
          ...(reason === null ? {} : { reason }),
          readAt: latest.good.readAt,
          queued: merged.filter((run) => run.kind === "queued").length,
          running: merged.filter((run) => run.kind === "running").length,
          oldestQueuedAt: oldest?.at ?? null,
          oldestQueuedUrl: oldest?.url ?? null,
          scanned: merged.length,
          nextReadAt: next,
        };
      });
      return { lines, runs };
    },
  );

  return { read } satisfies RepoQueueCache;
});
