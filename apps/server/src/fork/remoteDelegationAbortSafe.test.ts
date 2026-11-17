/**
 * Fork: a remote `delegate_task` interrupted once its launch there may have been sent still
 * records and follows its task; interrupted before, it leaves nothing. A recorded launch with no
 * task here is stopped there. Upstream's `RemoteDelegation.test.ts` covers the rest.
 */
import { assert, it } from "@effect/vitest";
import {
  type EnvironmentId,
  NodeId,
  type OrchestratorMcpDelegateTaskInput,
  ProjectId,
  ProviderInstanceId,
  RunId,
  ThreadId,
} from "@t3tools/contracts";
import * as Context from "effect/Context";
import * as Deferred from "effect/Deferred";
import * as SecureRandom from "effect/crypto/SecureRandom";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";

import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import type * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as RepositoryIdentityResolver from "../project/RepositoryIdentityResolver.ts";

const box = "environment-box" as EnvironmentId;
const parentThreadId = ThreadId.make("thread:parent");
const childThreadId = ThreadId.make("thread:box-child");
const taskId = NodeId.make("node:task");
const instanceId = ProviderInstanceId.make("codex");
const modelSelection = { instanceId, model: "gpt-5.4" };
const repository = "github.com/acme/app";

const scope: McpInvocationContext.McpThreadInvocationScope = {
  environmentId: "environment-here" as EnvironmentId,
  requestNamespace: "provider-session:parent",
  thread: {
    threadId: parentThreadId,
    providerSessionId: "provider-session:parent",
    providerInstanceId: instanceId,
  },
  client: undefined,
  capabilities: new Set(["orchestration"]),
  issuedAt: 1,
};

const input = {
  task: "Review the change on the box.",
  target: { environmentId: box, providerInstanceId: instanceId, model: "gpt-5.4" },
  mode: "async",
  clientRequestId: "review-on-box",
} as OrchestratorMcpDelegateTaskInput as Parameters<
  RemoteDelegation.RemoteDelegation["Service"]["delegate"]
>[1];

/** A point a delegating call stops at until the test lets it go on. */
const makePause = Effect.gen(function* () {
  const reached = yield* Deferred.make<void>();
  const release = yield* Deferred.make<void>();
  return {
    reached,
    release,
    hold: Deferred.succeed(reached, undefined).pipe(Effect.andThen(Deferred.await(release))),
  };
});
type Pause = Effect.Success<typeof makePause>;

/**
 * The delegation service with everything around it faked: the box (its project list and launch),
 * the records here, and the follower, which only notes the task it follows and then waits.
 */
const makeHarness = (
  options: {
    /** Holds the read of the box's projects, the last remote read before the launch. */
    readonly beforeLaunch?: Pause;
    /** Holds the launch's answer: the request reached the box, the reply has not come. */
    readonly duringLaunch?: Pause;
    /** Holds the record here, after the launch answered. */
    readonly beforeRecord?: Pause;
    /** The record succeeds, yet names no task, and the parent's task list has none. */
    readonly recordWithoutTask?: boolean;
  } = {},
) =>
  Effect.gen(function* () {
    const launches: Array<string> = [];
    const records: Array<string> = [];
    const followed: Array<NodeId> = [];
    const stops: Array<ThreadId> = [];
    const warnings: Array<string> = [];
    const following = yield* Deferred.make<void>();

    const forwarding = Layer.mock(PeerForwarding.PeerForwarding)({
      call: ((_scope: unknown, tool: { readonly name: string }, _env: unknown, params: never) => {
        if (tool.name === "t3_project_list") {
          return (options.beforeLaunch?.hold ?? Effect.void).pipe(
            Effect.as({
              projects: [
                {
                  id: ProjectId.make("project:box"),
                  title: "app",
                  repositoryIdentity: { canonicalKey: repository },
                },
              ],
              nextCursor: null,
            }),
          );
        }
        if (tool.name === "t3_thread_launch") {
          return Effect.sync(() => {
            launches.push((params as { readonly clientRequestId: string }).clientRequestId);
          }).pipe(
            Effect.andThen(options.duringLaunch?.hold ?? Effect.void),
            Effect.as({ threadId: childThreadId, runId: null, modelSelection }),
          );
        }
        return Effect.die(`unexpected call ${tool.name}`);
      }) as never,
      interruptThread: (_scope, _env, request) =>
        Effect.sync(() => {
          stops.push(request.threadId);
          return {} as never;
        }),
    });

    const threads = Layer.mock(ThreadManagement.ThreadManagementService)({
      // A follower's first step: here it notes the task it follows, then waits for good.
      getThreadEventSequence: () =>
        Effect.sync(() => followed.push(taskId)).pipe(
          Effect.andThen(Deferred.succeed(following, undefined)),
          Effect.andThen(Effect.never),
        ),
      getThreadRecords: ((threadId: ThreadId) =>
        Effect.succeed({
          thread: {
            id: threadId,
            title: "Parent",
            projectId: ProjectId.make("project:here"),
            modelSelection,
            runtimeMode: "full-access",
            interactionMode: "default",
          },
          runs: [
            {
              id: RunId.make("run:parent"),
              status: "running",
              ordinal: 1,
              rootNodeId: NodeId.make("node:parent-root"),
              providerInstanceId: instanceId,
            },
          ],
          subagents:
            records.length === 0 || options.recordWithoutTask
              ? []
              : [{ id: taskId, remoteChild: { threadId: childThreadId } }],
        })) as never,
      dispatch: (command) =>
        (command.type === "delegated_task.remote.request"
          ? (options.beforeRecord?.hold ?? Effect.void)
          : Effect.void
        ).pipe(
          Effect.andThen(
            Effect.sync(() => {
              records.push(command.type);
              return {
                storedEvents: options.recordWithoutTask
                  ? []
                  : [{ event: { type: "subagent.updated", payload: { id: taskId } } }],
              } as never;
            }),
          ),
        ),
    });

    const remote = yield* Layer.build(
      RemoteDelegation.layer.pipe(
        Layer.provide(
          Layer.mergeAll(
            forwarding,
            threads,
            Layer.mock(PeerLinks.PeerLinks)({ get: () => Effect.succeedNone }),
            Layer.mock(ProjectionStore.ProjectionStoreV2)({}),
            Layer.mock(ProjectService.ProjectService)({
              getById: () => Effect.succeedSome({ workspaceRoot: "/srv/app" } as never),
            }),
            Layer.mock(RepositoryIdentityResolver.RepositoryIdentityResolver)({
              resolve: () => Effect.succeed({ canonicalKey: repository } as never),
            }),
            Layer.mock(ServerEnvironment.ServerEnvironment)({
              getEnvironmentId: Effect.succeed("environment-here" as EnvironmentId),
            }),
            Layer.succeed(SecureRandom.SecureRandom, {
              uuidV4: Effect.succeed("00000000-0000-4000-8000-000000000000"),
            } as never),
          ),
        ),
      ),
    ).pipe(Effect.map(Context.get(RemoteDelegation.RemoteDelegation)));
    // Warnings the call logs, on its own fiber.
    const logged = Logger.layer(
      [
        Logger.make(({ message, logLevel }) => {
          if (logLevel === "Warn") warnings.push(String([message].flat()[0]));
        }),
      ],
      { mergeWithExisting: false },
    );

    return {
      delegate: remote.delegate(scope, input).pipe(Effect.provide(logged)),
      launches,
      records,
      following,
      followed,
      stops,
      warnings,
    };
  });

/**
 * Starts a delegate call, interrupts it at `pause` as the parent's provider does when its turn
 * moves on mid-call, then lets the held step go on.
 */
const interruptedAt = <A, E>(delegate: Effect.Effect<A, E>, pause: Pause) =>
  Effect.gen(function* () {
    const call = yield* delegate.pipe(Effect.forkChild);
    yield* Deferred.await(pause.reached);
    call.interruptUnsafe();
    yield* Deferred.succeed(pause.release, undefined);
    return yield* Fiber.await(call);
  });

it.effect.each(["beforeRecord", "duringLaunch"] as const)(
  "a delegate call interrupted %s still records and follows its task",
  (step) =>
    Effect.scoped(
      Effect.gen(function* () {
        const pause = yield* makePause;
        const harness = yield* makeHarness({ [step]: pause });

        const exit = yield* interruptedAt(harness.delegate, pause);
        // The caller hears nothing back; the thread there is still this parent's task.
        assert.isTrue(Exit.hasInterrupts(exit));
        assert.deepStrictEqual(harness.launches, [`delegate:${parentThreadId}:review-on-box`]);
        assert.deepStrictEqual(harness.records, ["delegated_task.remote.request"]);
        yield* Deferred.await(harness.following);
        assert.deepStrictEqual(harness.followed, [taskId]);
        assert.deepStrictEqual(harness.stops, []);
      }),
    ),
);

it.effect("a delegate call interrupted before its launch there launches and records nothing", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const pause = yield* makePause;
      const harness = yield* makeHarness({ beforeLaunch: pause });

      const exit = yield* interruptedAt(harness.delegate, pause);
      assert.isTrue(Exit.hasInterrupts(exit));
      assert.deepStrictEqual(harness.launches, []);
      assert.deepStrictEqual(harness.records, []);
      assert.deepStrictEqual(harness.followed, []);
    }),
  ),
);

it.effect("an uninterrupted delegate call launches, records and follows once", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness();
      const result = yield* harness.delegate;
      assert.deepStrictEqual(result, { taskId });
      yield* Deferred.await(harness.following);
      assert.strictEqual(harness.launches.length, 1);
      assert.deepStrictEqual(harness.followed, [taskId]);
    }),
  ),
);

it.effect("a recorded launch with no task here to follow is stopped there, with a warning", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const harness = yield* makeHarness({ recordWithoutTask: true });
      const exit = yield* Effect.exit(harness.delegate);
      assert.isTrue(Exit.isFailure(exit));
      assert.include(JSON.stringify(exit), "The delegated task was not recorded.");
      assert.deepStrictEqual(harness.followed, []);
      assert.deepStrictEqual(harness.stops, [childThreadId]);
      assert.include(harness.warnings, "remote-delegation.record-found-no-task");
    }),
  ),
);
