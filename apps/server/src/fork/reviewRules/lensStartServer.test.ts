/**
 * Fork: lens_start's reads on a server (lensStartServer.ts), over a fake `gh`/`git` runner: files at the base ref and
 * a 404, GraphQL pages (missing, capped, pending reviews and comments), GitHub's compare (its cap, a missing or
 * nameless file list), whether a stored child still runs, the after-CI start waiting for its thread's run, and
 * lens_report's post (once, also for a repeat, two at once or a post whose answer was lost).
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import { ProjectId, ThreadId } from "@t3tools/contracts";
import type { WaitRecord } from "@t3tools/contracts/waitFor";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import { TestClock } from "effect/testing";

import { ServerConfig } from "../../config.ts";
import * as McpInvocationContext from "../../mcp/McpInvocationContext.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as ProcessRunner from "../../processRunner.ts";
import * as ProjectEnrichmentService from "../../project/ProjectEnrichmentService.ts";
import * as ProjectService from "../../project/ProjectService.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import type { LensStartChild } from "./lensStart.ts";
import { lensStartNamespace, type StartAnswer } from "./lensStartRun.ts";
import { installLensStartServer, makeLensStartServer } from "./lensStartServer.ts";

const THREAD = ThreadId.make("thread-orchestrator");
const CHILD_THREAD = "thread-lens-child";
const PROJECT = ProjectId.make("project-a");
const HEAD = "a".repeat(40);

type Answer = Partial<Omit<ProcessRunner.ProcessRunOutput, "code">> & { readonly code?: number };
type Script = (command: string, args: ReadonlyArray<string>, stdin?: string) => Answer;

const output = (answer: Answer) =>
  ({
    stdout: "",
    stderr: "",
    code: 0,
    timedOut: false,
    stdoutTruncated: false,
    stderrTruncated: false,
    stdoutInvalidUtf8: false,
    stderrInvalidUtf8: false,
    ...answer,
  }) as ProcessRunner.ProcessRunOutput;

/** The server's dependencies, with `gh`/`git` answered by `script` and thread shells by `shells`. */
const scene = (script: { current: Script }, shells: Map<string, unknown>) =>
  Layer.mergeAll(
    Layer.mock(ProcessRunner.ProcessRunner)({
      // A few turns before each answer, as a real process takes: two calls at once interleave.
      run: (input) =>
        Effect.yieldNow.pipe(
          Effect.repeat({ times: 3 }),
          Effect.andThen(
            Effect.sync(() => output(script.current(input.command, input.args, input.stdin))),
          ),
        ),
    }),
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadShell: (threadId) => Effect.succeed((shells.get(threadId) ?? null) as never),
    }),
    Layer.mock(ProjectService.ProjectService)({
      getShell: () =>
        Effect.succeed(
          Option.some({
            workspaceRoot: "/nowhere/repo",
            repositoryIdentity: { provider: "github", owner: "owner", name: "repo" },
          }) as never,
        ),
    }),
    Layer.mock(ProjectEnrichmentService.ProjectEnrichmentService)({}),
    ServerSettingsService.layerTest({ reviewRules: { preset: "recommended" } } as never),
    ServerConfig.layerTest(process.cwd(), { prefix: "lens-start-server-" }),
  );

const make = (script: Script, shells = new Map<string, unknown>()) => {
  const current = { current: script };
  return {
    current,
    server: makeLensStartServer().pipe(Effect.provide(scene(current, shells))),
  };
};

const worldOf = (server: Effect.Success<ReturnType<typeof make>["server"]>) =>
  server.worldFor({
    threadId: THREAD,
    projectId: PROJECT,
    start: () => Effect.succeed({ type: "refused", code: "x", message: "unused" }),
  });

/** One GraphQL page answer: the pull request with `connection`'s nodes. */
const page = (connection: string, nodes: unknown[], next = false) =>
  JSON.stringify({
    data: {
      repository: {
        pullRequest: {
          state: "OPEN",
          headRefOid: HEAD,
          baseRefName: "main",
          headRefName: "feat/x",
          [connection]: {
            pageInfo: { hasNextPage: next, endCursor: next ? "cursor" : null },
            nodes,
          },
        },
      },
    },
  });

const connectionOf = (args: ReadonlyArray<string>) =>
  (args.find((arg) => arg.startsWith("query=")) ?? "").includes("reviews(")
    ? "reviews"
    : "comments";

const node = (body: string, extra: Record<string, unknown> = {}) => ({
  body,
  url: `https://example.test/${body}`,
  authorAssociation: "OWNER",
  includesCreatedEdit: false,
  isMinimized: false,
  author: { login: "owner", __typename: "User" },
  ...extra,
});

it.layer(NodeServices.layer)("lens_start's server reads", (it) => {
  it.effect("reads a file at the base ref; a 404 is a missing file, other failures refuse", () =>
    Effect.gen(function* () {
      const asked: Array<ReadonlyArray<string>> = [];
      const { current, server } = make((_, args) => {
        asked.push(args);
        return { stdout: "config text" };
      });
      const world = yield* worldOf(yield* server);
      assert.strictEqual(yield* world.readFile("main", ".github/pr-queue.json"), "config text");
      assert.include(
        asked[0]!.join(" "),
        "repos/owner/repo/contents/.github/pr-queue.json?ref=main",
      );
      current.current = () => ({ code: 1, stderr: "gh: Not Found (HTTP 404)" });
      assert.isNull(yield* world.readFile("main", "lenses/missing.md"));
      current.current = () => ({ code: 1, stderr: "gh: Server Error (HTTP 502)" });
      const failed = yield* world.readFile("main", "lenses/code.md").pipe(Effect.flip);
      assert.include(failed.message, "could not be read");
    }),
  );

  it.effect("reads comments, then submitted reviews, then their published inline comments", () =>
    Effect.gen(function* () {
      const { server } = make((_, args) =>
        connectionOf(args) === "comments"
          ? { stdout: page("comments", [node("comment", { createdAt: "2026-11-01T00:00:00Z" })]) }
          : {
              stdout: page("reviews", [
                node("review", {
                  state: "COMMENTED",
                  submittedAt: "2026-11-02T00:00:00Z",
                  comments: {
                    totalCount: 2,
                    nodes: [
                      node("inline", {
                        state: "SUBMITTED",
                        createdAt: "2026-11-01T12:00:00Z",
                        publishedAt: "2026-11-02T00:00:00Z",
                      }),
                      node("draft inline", { state: "PENDING", createdAt: "2026-11-03T00:00:00Z" }),
                    ],
                  },
                }),
                node("pending review", {
                  state: "PENDING",
                  submittedAt: null,
                  comments: { totalCount: 0, nodes: [] },
                }),
              ]),
            },
      );
      const world = yield* worldOf(yield* server);
      const pull = yield* world.readPull(7);
      assert.deepStrictEqual(
        pull?.posts.map((post) => [post.body, post.when]),
        [
          ["comment", "2026-11-01T00:00:00Z"],
          ["review", "2026-11-02T00:00:00Z"],
          // Published with its review, not drafted: the gate's time.
          ["inline", "2026-11-02T00:00:00Z"],
        ],
      );
      assert.strictEqual(pull?.head, HEAD);
      assert.strictEqual(pull?.base, "main");
    }),
  );

  it.effect("a pull request GitHub does not know is none; a missing later page refuses", () =>
    Effect.gen(function* () {
      const { current, server } = make(() => ({
        code: 1,
        stderr: "GraphQL: Could not resolve to a PullRequest with the number of 7.",
      }));
      const world = yield* worldOf(yield* server);
      assert.isNull(yield* world.readPull(7));
      let pages = 0;
      current.current = () =>
        (pages += 1) === 1
          ? { stdout: page("comments", [node("one", { createdAt: "x" })], true) }
          : { stdout: JSON.stringify({ data: { repository: null } }) };
      const missing = yield* world.readPull(7).pipe(Effect.flip);
      assert.include(missing.message, "missing from GitHub's answer");
    }),
  );

  it.effect("a review with more than 100 inline comments refuses rather than read part", () =>
    Effect.gen(function* () {
      const { server } = make((_, args) =>
        connectionOf(args) === "comments"
          ? { stdout: page("comments", []) }
          : {
              stdout: page("reviews", [
                node("review", {
                  state: "COMMENTED",
                  submittedAt: "2026-11-02T00:00:00Z",
                  comments: { totalCount: 101, nodes: [] },
                }),
              ]),
            },
      );
      const world = yield* worldOf(yield* server);
      const capped = yield* world.readPull(7).pipe(Effect.flip);
      assert.include(capped.message, "more than 100 inline comments: not all read");
    }),
  );

  it.effect("more than 20 pages refuses rather than read part", () =>
    Effect.gen(function* () {
      let pages = 0;
      const { server } = make(() => {
        pages += 1;
        return { stdout: page("comments", [], true) };
      });
      const world = yield* worldOf(yield* server);
      const capped = yield* world.readPull(7).pipe(Effect.flip);
      assert.include(capped.message, "more than 2000 comments");
      assert.strictEqual(pages, 20);
    }),
  );

  it.effect("the compare: names mapped; 300 files, no list or a nameless file refuse", () =>
    Effect.gen(function* () {
      const compare = (files: unknown) => () => ({ stdout: JSON.stringify(files) });
      const { current, server } = make(
        compare({
          files: [
            { filename: "a.ts", status: "added" },
            { filename: "b.ts", previous_filename: "old.ts", status: "renamed" },
            { filename: "c.ts", status: "modified" },
          ],
        }),
      );
      const world = yield* worldOf(yield* server);
      assert.deepStrictEqual(yield* world.readChanges("main", HEAD), [
        ["A", "a.ts"],
        ["R100", "old.ts", "b.ts"],
        ["M", "c.ts"],
      ]);
      const many = Array.from({ length: 300 }, (_, i) => ({ filename: `f${i}`, status: "added" }));
      current.current = compare({ files: many });
      assert.include(
        (yield* world.readChanges("main", HEAD).pipe(Effect.flip)).message,
        "300 files",
      );
      current.current = compare({ status: "ahead" });
      assert.include(
        (yield* world.readChanges("main", HEAD).pipe(Effect.flip)).message,
        "lists no files",
      );
      current.current = compare({ files: [{ status: "added" }] });
      assert.include(
        (yield* world.readChanges("main", HEAD).pipe(Effect.flip)).message,
        "a file without a name",
      );
    }),
  );

  it.effect("a stored child runs while new, or while its thread has a run", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(Date.parse("2026-11-22T12:00:00Z"));
      const shells = new Map<string, unknown>([
        [CHILD_THREAD, { deletedAt: null, archivedAt: null, activeRunId: null }],
      ]);
      const { server } = make(() => ({}), shells);
      const world = yield* worldOf(yield* server);
      const child = {
        requestId: "lens/x",
        key: "code",
        prs: [{ number: 7, head: HEAD }],
        taskId: "task-1",
        childThreadId: CHILD_THREAD,
        parentThreadId: THREAD,
        startedAt: "2026-11-22T11:59:30Z",
      };
      // Just started: its thread shows no run yet, and it still counts as running.
      assert.isTrue(yield* world.childRunning(child), "just started");
      const old = { ...child, startedAt: "2026-11-22T11:00:00Z" };
      assert.isFalse(yield* world.childRunning(old), "its thread has no run");
      shells.set(CHILD_THREAD, { deletedAt: null, archivedAt: null, activeRunId: "run-1" });
      assert.isTrue(yield* world.childRunning(old), "its thread runs");
      assert.isFalse(yield* world.childRunning({ ...old, childThreadId: null }), "no thread known");
    }),
  );

  it.effect("the after-CI start waits for its thread's run, once per action", () =>
    Effect.scoped(
      Effect.gen(function* () {
        const shells = new Map<string, unknown>([[THREAD, { providerInstanceId: "claude" }]]);
        const { server } = make(() => ({}), shells);
        const built = yield* server;
        let calls = 0;
        let runs = 2;
        /** The request namespace each delegate_task call ran in. */
        const namespaces: Array<string | undefined> = [];
        yield* installLensStartServer(
          {
            callTool: () =>
              Effect.gen(function* () {
                const scope = yield* Effect.serviceOption(
                  McpInvocationContext.McpInvocationContext,
                );
                namespaces.push(Option.getOrUndefined(scope)?.requestNamespace);
                calls += 1;
                return (runs -= 1) >= 0
                  ? {
                      content: [],
                      isError: true,
                      structuredContent: { code: "parent_not_active", message: "no run" },
                    }
                  : { content: [], structuredContent: { taskId: "task-1", childThreadId: "c-1" } };
              }) as never,
          },
          undefined,
        );
        const wait = { id: "w_aaaaaaaaaa", threadId: THREAD } as unknown as WaitRecord;
        const child = {
          requestId: "lens/x",
          key: "code",
          lens: "code",
          title: "lens code #7",
          prompt: "p",
          attempt: 1,
          replaces: [],
          runner: { driver: "claudeAgent", model: "m", effort: "high" },
          prs: [{ number: 7, head: HEAD, round: 1 }],
        } as unknown as LensStartChild;
        const start = built.startForWait(wait);
        const first = yield* Effect.forkChild(start(child));
        for (let second = 0; second < 10; second += 1) {
          yield* TestClock.adjust(1_000);
          for (let turn = 0; turn < 5; turn += 1) yield* Effect.yieldNow;
        }
        const answer: StartAnswer = yield* Fiber.join(first);
        assert.deepStrictEqual(answer, { type: "started", taskId: "task-1", childThreadId: "c-1" });
        assert.strictEqual(calls, 3);
        // Still no run for the next child of the same action: asked once, not waited for again.
        runs = 100;
        calls = 0;
        const second = yield* start(child);
        assert.strictEqual(second.type, "refused");
        assert.strictEqual(calls, 1);

        // Both start paths run delegate_task in the thread's own namespace, not the session's: a repeat
        // from a new session or from the after-CI wait is the same command, so it returns the same task.
        runs = 0;
        namespaces.length = 0;
        const inRequest = yield* built.startInRequest({
          environmentId: "environment-a" as never,
          requestNamespace: "provider-session-1",
          thread: {
            threadId: THREAD,
            providerSessionId: "provider-session-1",
            providerInstanceId: "claude" as never,
          },
          client: undefined,
          capabilities: new Set(["orchestration"]),
          issuedAt: 1,
        })(child);
        assert.strictEqual(inRequest.type, "started");
        yield* built.startForWait(wait)(child);
        assert.deepStrictEqual(namespaces, [
          lensStartNamespace(THREAD),
          lensStartNamespace(THREAD),
        ]);
        assert.strictEqual(lensStartNamespace(THREAD), "lens_start:thread-orchestrator");
      }),
    ),
  );

  describe("lens_report posts a lens child's report", () => {
    const BODY = "state: fine\nblocker: none\nmajor: none";
    const COMMENT = `lens: code · reviewer: model-x · read at: ${HEAD}\n\n${BODY}`;
    const child = {
      requestId: "lens/owner/repo#7/aaaaaaa/code",
      key: "code",
      prs: [{ number: 7, head: HEAD }],
      taskId: "task-1",
      childThreadId: CHILD_THREAD,
      parentThreadId: THREAD,
      startedAt: "2026-11-22T11:59:30Z",
    };
    /** GitHub with the comments posted so far; `failPost` drops the answer of a post GitHub took. */
    const github = () => {
      const comments: Array<string> = [];
      const posts: Array<{ args: ReadonlyArray<string>; stdin: string | undefined }> = [];
      const state = { failPost: false };
      const script: Script = (_, args, stdin) => {
        if (args.includes("POST")) {
          posts.push({ args, stdin });
          comments.push((JSON.parse(stdin ?? "{}") as { body: string }).body);
          return state.failPost
            ? { code: 1, stderr: "connection reset" }
            : { stdout: JSON.stringify({ html_url: "https://example.test/pull/7#c9" }) };
        }
        return connectionOf(args) === "comments"
          ? {
              stdout: page(
                "comments",
                comments.map((body) => node(body, { createdAt: "2026-11-22T12:00:00Z" })),
              ),
            }
          : { stdout: page("reviews", []) };
      };
      return { comments, posts, state, script };
    };
    const reportOf = (
      built: Effect.Success<ReturnType<typeof make>["server"]>,
      threadId = CHILD_THREAD,
    ) =>
      built.report({
        threadId: ThreadId.make(threadId),
        projectId: PROJECT,
        reviewer: "model-x",
        pr: 7,
        body: BODY,
      });

    it.effect("posts T3's first line and the report once; a repeat posts nothing new", () =>
      Effect.gen(function* () {
        const gh = github();
        const built = yield* make(gh.script).server;
        yield* (yield* worldOf(built)).children.add(child);
        const first = yield* reportOf(built);
        assert.deepStrictEqual(first, {
          type: "posted",
          head: HEAD,
          lens: "code",
          url: "https://example.test/pull/7#c9",
        });
        assert.deepStrictEqual(gh.posts, [
          {
            args: ["api", "--method", "POST", "repos/owner/repo/issues/7/comments", "--input", "-"],
            stdin: JSON.stringify({ body: COMMENT }),
          },
        ]);
        const again = yield* reportOf(built);
        assert.strictEqual(again.type, "already");
        assert.strictEqual(gh.posts.length, 1);
      }),
    );

    it.effect("two calls at once post one comment", () =>
      Effect.gen(function* () {
        const gh = github();
        const built = yield* make(gh.script).server;
        yield* (yield* worldOf(built)).children.add(child);
        const answers = yield* Effect.all([reportOf(built), reportOf(built)], {
          concurrency: "unbounded",
        });
        assert.deepStrictEqual(answers.map((a) => a.type).toSorted(), ["already", "posted"]);
        assert.strictEqual(gh.comments.length, 1);
      }),
    );

    it.effect("a post whose answer was lost is failed; the retry finds it, not a second post", () =>
      Effect.gen(function* () {
        const gh = github();
        gh.state.failPost = true;
        const built = yield* make(gh.script).server;
        yield* (yield* worldOf(built)).children.add(child);
        const lost = yield* reportOf(built);
        assert.strictEqual(lost.type, "failed");
        assert.include(lost.type === "failed" ? lost.reason : "", "connection reset");
        gh.state.failPost = false;
        assert.strictEqual((yield* reportOf(built)).type, "already");
        assert.strictEqual(gh.comments.length, 1);
      }),
    );

    it.effect("a thread no stored child has, or an empty store, posts nothing", () =>
      Effect.gen(function* () {
        const gh = github();
        const built = yield* make(gh.script).server;
        const empty = yield* reportOf(built);
        assert.strictEqual(empty.type, "refused");
        yield* (yield* worldOf(built)).children.add(child);
        const other = yield* reportOf(built, "thread-someone-else");
        assert.strictEqual(other.type, "refused");
        assert.include(other.type === "refused" ? other.reason : "", "no lens child");
        assert.strictEqual(gh.posts.length, 0);
      }),
    );
  });
});
