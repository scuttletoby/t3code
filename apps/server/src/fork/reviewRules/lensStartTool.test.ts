/**
 * Fork: the `lens_start` handler (lensStartTool.ts): it builds the caller's world from the wait service and, with
 * afterCi, registers the CI wait there: narrowed to the config's checks at the head, carrying the lens_start action.
 */
import { assert, it } from "@effect/vitest";
import {
  EnvironmentId,
  ProjectId,
  ProviderInstanceId,
  ThreadId,
  effectiveReviewRules,
} from "@t3tools/contracts";
import { WaitToolFailure } from "@t3tools/contracts/waitFor";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as McpInvocationContext from "../../mcp/McpInvocationContext.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import { Waits } from "../../waits/WaitWorker.ts";
import type { LensStartWorld } from "./lensStartRun.ts";
import type { LensStartServer } from "./lensStartServer.ts";
import { lensStartCall } from "./lensStartTool.ts";

const THREAD = ThreadId.make("thread-orchestrator");
const PROJECT = ProjectId.make("project-a");
const HEAD = "a".repeat(40);
const CONFIG = JSON.stringify({
  required_checks: ["ci-result", "head_check"],
  head_check: "head_check",
  bot_logins: [],
  lens_author_associations: ["OWNER"],
  override: { prefix: "override for head", author_association: "OWNER" },
  review: {
    version: 1,
    mode: "enforce",
    rules: [{ name: "code", weight: "normal", paths: ["**"], lenses: ["code"] }],
    lenses: { code: { prompt: "lenses/code.md" }, approach: { prompt: "lenses/approach.md" } },
    min_lenses: { critical: 2, normal: 1, light: 0 },
    rounds: { approach_at: 3, max: 4, approach_lens: "approach" },
    findings: true,
    override_authors: [],
    state_line: false,
  },
});

const scope: McpInvocationContext.McpInvocationScope = {
  environmentId: EnvironmentId.make("environment-a"),
  requestNamespace: "session-1",
  thread: {
    threadId: THREAD,
    providerSessionId: "session-1",
    providerInstanceId: ProviderInstanceId.make("claude"),
  },
  client: undefined,
  capabilities: new Set(["orchestration"]),
  issuedAt: 1,
};

/** A lens_start server whose world reads a made-up pull request; records what it was asked. */
const fakeServer = (started: Array<unknown>, scopes: Array<unknown>) =>
  ({
    waitForCiAction: "lens_start",
    startInRequest: (given: unknown) => {
      scopes.push(given);
      return () => {
        started.push(1);
        return Effect.succeed({ type: "started", taskId: "task-1", childThreadId: null });
      };
    },
    worldFor: (args: Parameters<LensStartServer["worldFor"]>[0]) =>
      Effect.succeed({
        rules: effectiveReviewRules({ preset: "recommended" }),
        repo: "owner/repo",
        readPull: () =>
          Effect.succeed({
            number: 7,
            state: "open",
            head: HEAD,
            base: "main",
            headBranch: "feat/x",
            posts: [],
          }),
        readFile: (_ref, path) =>
          Effect.succeed(path === ".github/pr-queue.json" ? CONFIG : `prompt of ${path}`),
        readChanges: () => Effect.succeed([["M", "src/x.ts"]]),
        checkout: () => Effect.succeed(null),
        children: { list: Effect.succeed([]), add: () => Effect.void },
        childRunning: () => Effect.succeed(false),
        start: args.start,
        ...(args.waitForCi === undefined ? {} : { waitForCi: args.waitForCi }),
      } satisfies LensStartWorld),
  }) as unknown as LensStartServer;

const run = (
  register: (request: unknown) => Effect.Effect<unknown, WaitToolFailure>,
  lensStart: LensStartServer | undefined,
  input: { pr: number; afterCi?: boolean },
) =>
  lensStartCall(input).pipe(
    Effect.provideService(McpInvocationContext.McpInvocationContext, scope),
    Effect.provide(
      Layer.mergeAll(
        Layer.mock(ThreadManagement.ThreadManagementService)({
          getThreadShell: () =>
            Effect.succeed({
              id: THREAD,
              projectId: PROJECT,
              deletedAt: null,
              runtimeMode: "full-access",
              interactionMode: "default",
              lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: THREAD },
            } as never),
        }),
        Layer.mock(Waits)({
          register: register as never,
          // Named even when absent: a mock answers a field it was not given.
          lensStart: lensStart as LensStartServer,
        }),
      ),
    ),
  );

it.effect("afterCi: the wait is registered as the caller, narrowed and carrying the action", () =>
  Effect.gen(function* () {
    const asked: Array<unknown> = [];
    const started: Array<unknown> = [];
    const result = yield* run(
      (request) =>
        Effect.sync(() => {
          asked.push(request);
          return { state: "waiting", waitId: "w_aaaaaaaaaa", deadlineAt: "x", next: "" };
        }),
      fakeServer(started, []),
      { pr: 7, afterCi: true },
    );
    assert.strictEqual(result.state, "waiting");
    assert.strictEqual(result.waitId, "w_aaaaaaaaaa");
    assert.deepStrictEqual(started, []);
    assert.deepStrictEqual(asked, [
      {
        threadId: THREAD,
        projectId: PROJECT,
        input: { kind: "pr", pr: { number: 7 }, note: "lens_start #7 after CI" },
        narrow: { checks: ["ci-result"], head: HEAD },
        action: "lens_start",
      },
    ]);
  }),
);

it.effect("afterCi with waits refused: nothing starts, the reason is given", () =>
  Effect.gen(function* () {
    const started: Array<unknown> = [];
    const result = yield* run(
      () => Effect.fail(new WaitToolFailure({ code: "waits_off", message: "Waits are off." })),
      fakeServer(started, []),
      { pr: 7, afterCi: true },
    );
    assert.strictEqual(result.state, "refused");
    assert.include(result.refused.join(" "), "Waits are off.");
    assert.deepStrictEqual(started, []);
  }),
);

it.effect("without afterCi: starts in the caller's own request scope", () =>
  Effect.gen(function* () {
    const started: Array<unknown> = [];
    const scopes: Array<unknown> = [];
    const result = yield* run(() => Effect.die("no wait"), fakeServer(started, scopes), { pr: 7 });
    assert.strictEqual(result.state, "started");
    assert.lengthOf(started, 1);
    assert.deepStrictEqual(scopes, [scope]);
  }),
);

it.effect("a server without the lens_start service refuses plainly", () =>
  Effect.gen(function* () {
    const failure = yield* run(() => Effect.die("no wait"), undefined, { pr: 7 }).pipe(Effect.flip);
    assert.strictEqual(failure.code, "capability_denied");
  }),
);
