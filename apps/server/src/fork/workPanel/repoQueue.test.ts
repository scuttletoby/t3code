// Fork (work panel, fork issue 1136): the shared repository read against a fake GitHub. Each test
// names the question it answers.
import { assert, describe, it } from "@effect/vitest";
import type { OrchestrationV2ThreadShell, ThreadPullRequestLink } from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as TestClock from "effect/testing/TestClock";

import {
  CALL_INTERVAL_MS,
  type GitHubRunsAnswer,
  makeRepoQueueCache,
  pullRequestRuns,
  REASON_NOT_GITHUB,
  REASON_NOT_READ,
  REASON_PARTIAL,
  parseRuns,
} from "./repoQueue.ts";
import { makeRead, parseGhIncludeOutput } from "./workPanel.ts";

const T0 = Date.parse("2026-11-27T12:00:00Z");
const iso = (ms: number) => DateTime.formatIso(DateTime.makeUnsafe(ms));
const MINUTE = 60_000;
const REPO = { host: "github.com", repository: "owner/repo" } as const;

const run = (over: Record<string, unknown>) => ({
  id: 1,
  name: "CI",
  workflow_id: 10,
  head_branch: "feature",
  head_sha: "abc",
  head_repository: { full_name: "owner/repo" },
  status: "completed",
  conclusion: "success",
  created_at: iso(T0 - 30 * MINUTE),
  html_url: "https://github.com/owner/repo/actions/runs/1",
  ...over,
});

const ok = (
  runs: ReadonlyArray<unknown>,
  headers: Record<string, string> = {},
): GitHubRunsAnswer => ({
  _tag: "Response",
  status: 200,
  headers: {
    "x-ratelimit-limit": "5000",
    "x-ratelimit-remaining": "4900",
    "x-ratelimit-reset": String(Math.floor((T0 + 30 * MINUTE) / 1000)),
    ...headers,
  },
  body: JSON.stringify({ total_count: runs.length, workflow_runs: runs }),
});

const RUNS = [
  run({
    id: 1,
    status: "queued",
    conclusion: null,
    created_at: iso(T0 - 12 * MINUTE),
    html_url: "u1",
  }),
  run({
    id: 2,
    status: "queued",
    conclusion: null,
    workflow_id: 11,
    created_at: iso(T0 - 3 * MINUTE),
  }),
  run({
    id: 3,
    status: "in_progress",
    conclusion: null,
    workflow_id: 12,
    head_branch: "other",
    head_sha: "def",
  }),
  run({ id: 4, workflow_id: 13, conclusion: "failure", html_url: "u4" }),
];

/** A fake GitHub that answers from a list and counts its calls. */
const fakeGitHub = (answers: Array<GitHubRunsAnswer>) => {
  const calls: Array<string> = [];
  // It yields before answering, like a real call, so a second viewer could slip in meanwhile.
  const source = ({ host, repository }: { host: string; repository: string }) =>
    Effect.sync(() => calls.push(`${host}/${repository}`)).pipe(
      Effect.andThen(Effect.yieldNow),
      Effect.andThen(Effect.sync(() => (answers.length > 1 ? answers.shift()! : answers[0]!))),
    );
  return { calls, source };
};

describe("the shared repository read", () => {
  it.effect("answers every viewer in the same minute from one GitHub call", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(T0);
      const github = fakeGitHub([ok(RUNS)]);
      const cache = yield* makeRepoQueueCache(github.source);
      const [first, second] = yield* Effect.all([cache.read([REPO]), cache.read([REPO])], {
        concurrency: "unbounded",
      });
      assert.strictEqual(github.calls.length, 1);
      for (const answer of [first, second]) {
        const line = answer.lines[0]!;
        assert.strictEqual(line.state, "answered");
        assert.strictEqual(line.queued, 2);
        assert.strictEqual(line.running, 1);
        assert.strictEqual(line.oldestQueuedAt, T0 - 12 * MINUTE);
        assert.strictEqual(line.oldestQueuedUrl, "u1");
      }
      yield* TestClock.adjust(CALL_INTERVAL_MS - 1);
      yield* cache.read([REPO]);
      assert.strictEqual(github.calls.length, 1);
      yield* TestClock.adjust(1);
      yield* cache.read([REPO]);
      assert.strictEqual(github.calls.length, 2);
    }),
  );

  it.effect("asks GitHub once a minute in all, not once per repository", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(T0);
      const github = fakeGitHub([ok(RUNS)]);
      const cache = yield* makeRepoQueueCache(github.source);
      const other = { host: "github.com", repository: "owner/other" };
      const answer = yield* cache.read([REPO, other]);
      assert.strictEqual(github.calls.length, 1);
      assert.strictEqual(answer.lines[1]!.state, "unknown");
      assert.strictEqual(answer.lines[1]!.reason, REASON_NOT_READ);
      yield* TestClock.adjust(CALL_INTERVAL_MS);
      const later = yield* cache.read([REPO, other]);
      assert.deepStrictEqual(github.calls, ["github.com/owner/repo", "github.com/owner/other"]);
      assert.strictEqual(later.lines[1]!.state, "answered");
    }),
  );

  it.effect("rate-limited: says unknown and waits for the reset GitHub names", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(T0);
      const resetAt = T0 + 20 * MINUTE;
      const github = fakeGitHub([
        {
          _tag: "Response",
          status: 403,
          headers: {
            "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": String(resetAt / 1000),
          },
          body: '{"message":"API rate limit exceeded"}',
        },
        ok(RUNS),
      ]);
      const cache = yield* makeRepoQueueCache(github.source);
      const line = (yield* cache.read([REPO])).lines[0]!;
      assert.strictEqual(line.state, "unknown");
      assert.match(line.reason ?? "", /rate limit/);
      assert.strictEqual(line.nextReadAt, resetAt);
      assert.strictEqual(line.queued, undefined);
      yield* TestClock.adjust(19 * MINUTE);
      yield* cache.read([REPO]);
      assert.strictEqual(github.calls.length, 1);
      yield* TestClock.adjust(MINUTE);
      assert.strictEqual((yield* cache.read([REPO])).lines[0]!.state, "answered");
      assert.strictEqual(github.calls.length, 2);
    }),
  );

  it.effect("a low budget keeps the counts but waits for the reset before the next call", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(T0);
      const github = fakeGitHub([ok(RUNS, { "x-ratelimit-remaining": "100" })]);
      const cache = yield* makeRepoQueueCache(github.source);
      const line = (yield* cache.read([REPO])).lines[0]!;
      assert.strictEqual(line.state, "answered");
      assert.strictEqual(line.nextReadAt, T0 + 30 * MINUTE);
      yield* TestClock.adjust(5 * MINUTE);
      yield* cache.read([REPO]);
      assert.strictEqual(github.calls.length, 1);
    }),
  );

  it.effect(
    "down: says unknown, waits longer each time, and drops counts older than 10 minutes",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(T0);
        const down: GitHubRunsAnswer = { _tag: "Unreachable", reason: "connection refused" };
        const github = fakeGitHub([ok(RUNS), down]);
        const cache = yield* makeRepoQueueCache(github.source);
        yield* cache.read([REPO]);
        yield* TestClock.adjust(MINUTE);
        const kept = (yield* cache.read([REPO])).lines[0]!;
        // Counts of a minute ago stay, with the reason the newer read failed.
        assert.strictEqual(kept.state, "answered");
        assert.match(kept.reason ?? "", /did not answer/);
        assert.strictEqual(kept.nextReadAt, T0 + 3 * MINUTE);
        yield* TestClock.adjust(2 * MINUTE);
        const again = (yield* cache.read([REPO])).lines[0]!;
        assert.strictEqual(github.calls.length, 3);
        assert.strictEqual(again.nextReadAt, T0 + 7 * MINUTE);
        yield* TestClock.adjust(8 * MINUTE);
        const stale = (yield* cache.read([REPO])).lines[0]!;
        assert.strictEqual(stale.state, "unknown");
        assert.strictEqual(stale.queued, undefined);
      }),
  );

  it.effect("a repository it cannot read is unknown without a call", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(T0);
      const github = fakeGitHub([ok(RUNS)]);
      const cache = yield* makeRepoQueueCache(github.source);
      const answer = yield* cache.read([
        { host: "gitlab.com", repository: "owner/repo" },
        { host: "github.com", repository: "--hostname" },
      ]);
      assert.strictEqual(github.calls.length, 0);
      assert.strictEqual(answer.lines[0]!.reason, REASON_NOT_GITHUB);
      assert.strictEqual(answer.lines[1]!.state, "unknown");
    }),
  );
});

const link = (over: Partial<ThreadPullRequestLink>): ThreadPullRequestLink =>
  ({
    host: "github.com",
    repository: "owner/repo",
    number: 7,
    url: "https://github.com/owner/repo/pull/7",
    source: "agent",
    linkedAt: iso(T0),
    snapshot: { headBranch: "feature" },
    stack: null,
    ...over,
  }) as unknown as ThreadPullRequestLink;

describe("the shared read, round 1 of #1142", () => {
  /** A fake GitHub whose call takes `ms`, answering by the status asked. */
  const slowGitHub = (ms: number, byStatus: Partial<Record<string, GitHubRunsAnswer>> = {}) => {
    const calls: Array<string> = [];
    const source = (input: { repository: string; status?: string }) =>
      Effect.sync(() => calls.push(`${input.repository}:${input.status ?? "latest"}`)).pipe(
        Effect.andThen(ms === 0 ? Effect.yieldNow : Effect.sleep(ms)),
        Effect.as(byStatus[input.status ?? "latest"] ?? ok(RUNS)),
      );
    return { calls, source };
  };

  it.effect(
    "a re-run is dated by its own start, and one older than the latest page is counted",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(T0);
        // GitHub keeps created_at on a re-run: queued a minute ago, first run two hours ago.
        const rerun = run({
          id: 70,
          workflow_id: 20,
          status: "queued",
          conclusion: null,
          run_attempt: 2,
          created_at: iso(T0 - 120 * MINUTE),
          run_started_at: iso(T0 - MINUTE),
          html_url: "rerun",
        });
        const github = slowGitHub(0, { queued: ok([rerun]) });
        const cache = yield* makeRepoQueueCache(github.source);
        const first = (yield* cache.read([REPO])).lines[0]!;
        assert.strictEqual(first.queued, 2);
        assert.strictEqual(first.reason, REASON_PARTIAL);
        yield* TestClock.adjust(MINUTE);
        const line = (yield* cache.read([REPO])).lines[0]!;
        assert.deepStrictEqual(github.calls, ["owner/repo:latest", "owner/repo:queued"]);
        // The re-run is not in the latest page; the queued read finds it, dated by its own start.
        assert.strictEqual(line.queued, 3);
        assert.strictEqual(line.oldestQueuedAt, T0 - 12 * MINUTE);
        const dated = parseRuns(JSON.stringify({ workflow_runs: [rerun] }))![0]!;
        assert.strictEqual(dated.at, T0 - MINUTE);
      }),
  );

  it.effect("viewers who leave during the call do not let the next viewer call again", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(T0);
      const github = slowGitHub(1_000);
      const cache = yield* makeRepoQueueCache(github.source);
      for (let i = 0; i < 5; i++) {
        const fiber = yield* Effect.forkChild(cache.read([REPO]));
        yield* TestClock.adjust(100);
        yield* Fiber.interrupt(fiber);
      }
      yield* TestClock.adjust(1_000);
      assert.strictEqual(github.calls.length, 1);
      // The interrupted viewer's call still finished and filled the cache.
      assert.strictEqual((yield* cache.read([REPO])).lines[0]!.state, "answered");
    }),
  );

  it.effect("a viewer of another repository answers from the cache while a call runs", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(T0);
      const github = slowGitHub(30_000);
      const cache = yield* makeRepoQueueCache(github.source);
      const other = { host: "github.com", repository: "owner/other" };
      const a = yield* Effect.forkChild(cache.read([other]));
      yield* TestClock.adjust(1_000);
      const b = yield* Effect.forkChild(cache.read([REPO]));
      yield* TestClock.adjust(0);
      const answer = yield* Fiber.join(b);
      assert.strictEqual(answer.lines[0]!.reason, REASON_NOT_READ);
      assert.strictEqual(github.calls.length, 1);
      yield* TestClock.adjust(30_000);
      assert.strictEqual((yield* Fiber.join(a)).lines[0]!.state, "answered");
    }),
  );

  it.effect(
    "the minute counts from the call's start, so a refresh each minute reads each minute",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(T0);
        const github = slowGitHub(5_000);
        const cache = yield* makeRepoQueueCache(github.source);
        const first = yield* Effect.forkChild(cache.read([REPO]));
        yield* TestClock.adjust(5_000);
        yield* Fiber.join(first);
        // The viewer's next refresh, a minute after the call began.
        yield* TestClock.adjust(55_000);
        const second = yield* Effect.forkChild(cache.read([REPO]));
        yield* TestClock.adjust(5_000);
        yield* Fiber.join(second);
        assert.strictEqual(github.calls.length, 2);
      }),
  );
});

describe("a pull request's CI from the shared read", () => {
  it("counts the latest run of each workflow on its head and opens the oldest queued", () => {
    const runs = parseRuns(
      JSON.stringify({
        workflow_runs: [
          ...RUNS,
          // An older failed run of workflow 10 on the same head: the queued one replaced it.
          run({
            id: 5,
            conclusion: "failure",
            created_at: iso(T0 - 60 * MINUTE),
          }),
        ],
      }),
    )!;
    assert.deepStrictEqual(pullRequestRuns(runs, link({})), {
      host: "github.com",
      repository: "owner/repo",
      number: 7,
      queued: 2,
      running: 0,
      failed: 1,
      passed: 0,
      oldestQueuedAt: T0 - 12 * MINUTE,
      url: "u1",
    });
  });

  it("without a watched head: its own runs, not another fork's branch of the same name", () => {
    const runs = parseRuns(
      JSON.stringify({
        workflow_runs: [
          run({ id: 1, head_repository: { full_name: "someone/fork" }, conclusion: "failure" }),
          run({ id: 2, workflow_id: 11, head_repository: { full_name: "owner/repo" } }),
          run({
            id: 3,
            workflow_id: 12,
            head_branch: "other",
            head_repository: { full_name: "someone/fork" },
            pull_requests: [{ number: 7 }],
          }),
        ],
      }),
    )!;
    const found = pullRequestRuns(runs, link({}))!;
    assert.deepStrictEqual([found.failed, found.passed], [0, 2]);
  });

  it("follows the watched head commit over the branch", () => {
    const runs = parseRuns(JSON.stringify({ workflow_runs: RUNS }))!;
    const watched = link({ watch: { headSha: "def" } as ThreadPullRequestLink["watch"] });
    assert.strictEqual(pullRequestRuns(runs, watched)?.running, 1);
    assert.strictEqual(pullRequestRuns(runs, link({ snapshot: null })), null);
  });
});

describe("the work panel read", () => {
  const shell = (pullRequests: ReadonlyArray<ThreadPullRequestLink>) =>
    ({ id: "t1", pullRequests }) as unknown as OrchestrationV2ThreadShell;

  it.effect("no pull requests: no repository lines and no GitHub call", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(T0);
      const github = fakeGitHub([ok(RUNS)]);
      const cache = yield* makeRepoQueueCache(github.source);
      const read = makeRead(() => Effect.succeed([shell([])]), cache);
      const answer = yield* read({ threadId: "t1" as never });
      assert.deepStrictEqual(answer, { answeredAt: T0, repoQueues: [], pullRequests: [] });
      const unreadable = makeRead(() => Effect.succeed(null), cache);
      assert.deepStrictEqual((yield* unreadable({ threadId: "t1" as never })).repoQueues, []);
      assert.strictEqual(github.calls.length, 0);
    }),
  );

  it.effect("a sub-agent's panel reads its own repository, not a sibling's", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(T0);
      const github = fakeGitHub([ok(RUNS)]);
      const cache = yield* makeRepoQueueCache(github.source);
      const at = (id: string, parent: string | null, links: ReadonlyArray<ThreadPullRequestLink>) =>
        ({
          id,
          lineage: { parentThreadId: parent },
          pullRequests: links,
        }) as unknown as OrchestrationV2ThreadShell;
      const sibling = (n: number) => link({ number: n, repository: "owner/sibling" });
      const read = makeRead(
        () =>
          Effect.succeed([
            at("root", null, [sibling(1)]),
            at("sib", "root", [sibling(2), sibling(3)]),
            at("me", "root", [link({ number: 4 })]),
            at("mine", "me", [link({ number: 5 })]),
          ]),
        cache,
      );
      const answer = yield* read({ threadId: "me" as never });
      assert.deepStrictEqual(
        answer.repoQueues.map((line) => line.repository),
        ["owner/repo"],
      );
      assert.deepStrictEqual(github.calls, ["github.com/owner/repo"]);
    }),
  );

  it.effect("a family's pull requests share their repository's one read", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(T0);
      const github = fakeGitHub([ok(RUNS)]);
      const cache = yield* makeRepoQueueCache(github.source);
      const read = makeRead(
        () =>
          Effect.succeed([
            shell([link({})]),
            shell([link({ number: 8, snapshot: { headBranch: "other" } as never })]),
          ]),
        cache,
      );
      const answer = yield* read({ threadId: "t1" as never });
      assert.strictEqual(github.calls.length, 1);
      assert.strictEqual(answer.repoQueues.length, 1);
      assert.deepStrictEqual(
        answer.pullRequests.map((pr) => [pr.number, pr.queued, pr.running]),
        [
          [7, 2, 0],
          [8, 0, 1],
        ],
      );
    }),
  );
});

describe("gh api -i output", () => {
  it("reads the status, the headers and the body; anything else is no answer", () => {
    const parsed = parseGhIncludeOutput(
      'HTTP/2.0 403 Forbidden\r\nX-Ratelimit-Remaining: 0\r\nContent-Type: application/json\r\n\r\n{"message":"x"}',
    );
    assert.deepStrictEqual(parsed, {
      _tag: "Response",
      status: 403,
      headers: { "x-ratelimit-remaining": "0", "content-type": "application/json" },
      body: '{"message":"x"}',
    });
    assert.strictEqual(parseGhIncludeOutput("gh: not logged in"), null);
  });
});
