/**
 * Fork (issue 386): a remote child's end reaches its parent at least once and wakes it once. The
 * real orchestrator and follower on one SQLite database (rebuilt on it for a restart); the machine
 * there is faked at the link. Upstream's follower is the reconcile, its delivery the dedupe.
 */
import { expect, it } from "@effect/vitest";
import {
  CommandId,
  type EnvironmentId,
  EventId,
  MessageId,
  NodeId,
  type OrchestratorMcpDelegateTaskInput,
  OrchestratorMcpFailure,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ProviderThreadId,
  RunId,
  ThreadId,
  TurnItemId,
} from "@t3tools/contracts";
import type * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import * as Context from "effect/Context";
import * as SecureRandom from "effect/crypto/SecureRandom";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Queue from "effect/Queue";
import * as Schedule from "effect/Schedule";
import * as Stream from "effect/Stream";
import * as SubscriptionRef from "effect/SubscriptionRef";
import type * as SqlClient from "effect/sql/SqlClient";
import { HttpClient } from "effect/http";

import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import type * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import { CodexProviderCapabilitiesV2 } from "../orchestration-v2/Adapters/CodexAdapterV2.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as EventSink from "../orchestration-v2/EventSink.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ProviderAdapterRegistry from "../orchestration-v2/ProviderAdapterRegistry.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ProviderReplayHarness from "../orchestration-v2/testkit/ProviderReplayHarness.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as RepositoryIdentityResolver from "../project/RepositoryIdentityResolver.ts";
import * as ServerSettings from "../serverSettings.ts";

const here = "environment-here" as EnvironmentId;
const box = "environment-box" as EnvironmentId;
const instanceId = ProviderInstanceId.make("codex");
const driver = ProviderDriverKind.make("codex");
const modelSelection = { instanceId, model: "gpt-5.4" };
const repository = "github.com/acme/app";
const parentThreadId = ThreadId.make("thread:parent");
const parentRunId = RunId.make("run:parent");
const parentRootNode = NodeId.make("node:parent:root");
const childThreadId = ThreadId.make("thread:box-child");
const boxRunId = RunId.make("run:box");
/** The run the box's restart recovery continues the task's run with. */
const continuedRunId = RunId.make("run:box:continued");
const reply = "All 412 tests pass on the box.";

const scope: McpInvocationContext.McpThreadInvocationScope = {
  environmentId: here,
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

const delegateInput = (clientRequestId: string) =>
  ({
    task: "Run the full suite on the box.",
    target: { environmentId: box, providerInstanceId: instanceId, model: "gpt-5.4" },
    mode: "async",
    clientRequestId,
  }) as OrchestratorMcpDelegateTaskInput as Parameters<
    RemoteDelegation.RemoteDelegation["Service"]["delegate"]
  >[1];

const adapter = {
  instanceId,
  driver,
  getCapabilities: () => Effect.succeed(CodexProviderCapabilitiesV2),
  planSelectionTransition: () => Effect.succeed({ type: "apply_on_next_turn" as const }),
  openSession: () => Effect.die("No provider process needed"),
} as ProviderAdapter.ProviderAdapterV2["Service"];

type BoxRunStatus = "running" | "completed" | "failed" | "interrupted";

/**
 * The machine there, as the link reaches it. Down, every call fails as an unanswered one does,
 * and the follower's retry waits for the test's step. A wait on a running thread answers when
 * the box changes, as a long poll does: "timed out" while it still runs.
 */
const makeBox = Effect.gen(function* () {
  const state = {
    reachable: true,
    status: "running" as BoxRunStatus,
    reply: null as string | null,
    /** The machine restarted: its recovery continued the run, which ended with the reply. */
    restarted: false,
    /** The launch answers with no run, as one recorded there before its run was kept. */
    runless: false,
    /** The launch's run had already ended there when the launch answered. */
    endedAtLaunch: false,
    /** Distinct launches: one with a key seen before answers with the same thread, as there. */
    launches: 0,
    launchKeys: new Set<string>(),
    waits: 0,
    failedCalls: 0,
    /** Held reads of the reply: the follower stops there until it is released or interrupted. */
    holdRead: null as Deferred.Deferred<void> | null,
    readHeld: null as Deferred.Deferred<void> | null,
    version: 0,
  };
  const version = yield* SubscriptionRef.make(0);
  const failed = yield* Queue.unbounded<void>();
  const retry = yield* Queue.unbounded<void>();
  /** Changes the box, as the machine there does; a wait in progress answers. */
  const update = (patch: Partial<typeof state>) =>
    Effect.suspend(() => {
      Object.assign(state, patch);
      state.version += 1;
      return SubscriptionRef.set(version, state.version);
    });
  const changedSince = (seen: number) =>
    SubscriptionRef.changes(version).pipe(
      Stream.filter((now) => now > seen),
      Stream.runHead,
    );
  const down = Effect.suspend(() => {
    state.failedCalls += 1;
    return Effect.fail(
      new OrchestratorMcpFailure({
        code: "orchestration_error",
        message: "The linked environment stopped answering.",
      }),
    );
  });
  const reachable = <A>(effect: Effect.Effect<A, OrchestratorMcpFailure>) =>
    Effect.suspend(() => (state.reachable ? effect : down));
  const runNow = () => (state.restarted ? continuedRunId : boxRunId);
  const ended = () => state.status !== "running";
  const forwarding = Layer.mock(PeerForwarding.PeerForwarding)({
    call: ((
      _scope: unknown,
      tool: { readonly name: string },
      _environmentId: unknown,
      input?: { readonly clientRequestId?: string },
    ) =>
      reachable<unknown>(
        tool.name === "t3_project_list"
          ? Effect.succeed({
              projects: [
                {
                  id: ProjectId.make("project:box"),
                  title: "app",
                  repositoryIdentity: { canonicalKey: repository },
                },
              ],
              nextCursor: null,
            })
          : tool.name === "t3_thread_launch"
            ? Effect.sync(() => {
                const key = input?.clientRequestId ?? `call-${state.launchKeys.size}`;
                if (!state.launchKeys.has(key)) {
                  state.launchKeys.add(key);
                  state.launches += 1;
                }
                if (state.endedAtLaunch) {
                  state.status = "completed";
                  state.reply = reply;
                }
                return {
                  threadId: childThreadId,
                  runId: state.runless ? null : boxRunId,
                  modelSelection,
                };
              })
            : Effect.die(`unexpected call ${tool.name}`),
      )) as never,
    waitForThread: ((_scope: unknown, _environmentId: unknown, input: { runId?: RunId }) =>
      reachable(
        Effect.gen(function* () {
          state.waits += 1;
          if (!ended()) {
            yield* changedSince(state.version);
            if (!state.reachable) return yield* down;
          }
          // A restarted run is reported as its continuation, as the box follows the chain.
          const runId = input.runId === undefined && state.runless ? null : runNow();
          return {
            threadId: childThreadId,
            runId,
            status: ended() ? state.status : "running",
            timedOut: !ended(),
          };
        }),
      )) as never,
    readThread: (() =>
      reachable(
        Effect.gen(function* () {
          if (state.holdRead !== null) {
            if (state.readHeld !== null) yield* Deferred.succeed(state.readHeld, undefined);
            yield* Deferred.await(state.holdRead);
          }
          return {
            thread: { itemCount: 1 },
            recentRuns: [],
            items:
              state.reply === null
                ? []
                : [
                    {
                      type: "assistant_message",
                      itemId: TurnItemId.make("item:reply"),
                      runId: runNow(),
                      text: state.reply,
                      textTruncated: false,
                    },
                  ],
            nextPosition: null,
            hasMore: false,
          };
        }),
      )) as never,
    interruptThread: (() => reachable(Effect.succeed({}))) as never,
  });
  /** The follower's retries, stepped by the test: each failure is offered, then waits for a step. */
  const retrySchedule = Schedule.forever.pipe(
    Schedule.tap(() => Queue.offer(failed, undefined).pipe(Effect.andThen(Queue.take(retry)))),
  );
  /** One failed call there, then the follower tries again. */
  const step = Queue.offer(retry, undefined);
  const failedThenRetry = Queue.take(failed).pipe(Effect.andThen(step));
  return { state, update, forwarding, retrySchedule, failed, step, failedThenRetry };
});
type Box = Effect.Success<typeof makeBox>;

/** Real attempt ids: each report of a task's end is its own command, as on a server. */
const makeRandom = () => {
  let next = 0;
  return {
    uuidV4: () =>
      Effect.sync(() => {
        next += 1;
        return `00000000-0000-4000-8000-${String(next).padStart(12, "0")}`;
      }),
  } as never as SecureRandom.SecureRandom["Service"];
};

/**
 * The parent server: the real orchestrator on `database` and the real follower, built in the
 * caller's scope, so closing that scope stops it as a shutdown does and building it again on the
 * same database is a restart.
 */
const makeParentServer = (database: Context.Context<SqlClient.SqlClient>, there: Box) =>
  Effect.gen(function* () {
    const layerDatabase = Layer.succeedContext(database);
    const layerOrchestrator = Layer.mergeAll(
      layerDatabase,
      ProjectionStore.layer.pipe(Layer.provide(layerDatabase)),
      ProviderReplayHarness.layerWithRegistry(
        { name: "remote-completion-delivery" },
        ProviderAdapterRegistry.layerFromAdapters([adapter]),
        // Upstream's continuation worker takes each wake offered and writes its message.
        { databaseLayer: layerDatabase, runEffectWorker: false, runContinuationWorker: true },
      ),
    );
    const layerThreads = ThreadManagement.layer.pipe(
      Layer.provide(CommandReceiptStore.layer.pipe(Layer.provide(layerDatabase))),
      Layer.provideMerge(Layer.merge(layerOrchestrator, ServerSettings.layerTest())),
    );
    const layerServer = RemoteDelegation.layer.pipe(
      Layer.provide(
        Layer.mergeAll(
          there.forwarding,
          Layer.mock(PeerLinks.PeerLinks)({ get: () => Effect.succeedNone }),
          // Upstream's launch reads the providers to check an agent's mode pick.
          Layer.mock(ProviderRegistry.ProviderRegistry)({ getProviders: Effect.succeed([]) }),
          Layer.mock(ProjectService.ProjectService)({
            getById: () => Effect.succeedSome({ workspaceRoot: "/srv/app" } as never),
          }),
          Layer.mock(RepositoryIdentityResolver.RepositoryIdentityResolver)({
            resolve: () => Effect.succeed({ canonicalKey: repository } as never),
          }),
          Layer.mock(ServerEnvironment.ServerEnvironment)({
            getEnvironmentId: Effect.succeed(here),
          }),
          Layer.succeed(SecureRandom.SecureRandom, makeRandom()),
          // The features ask goes only with a profile narrower than `all`; none here.
          Layer.succeed(
            HttpClient.HttpClient,
            HttpClient.make(() => Effect.die("no features ask expected")),
          ),
        ),
      ),
      Layer.provideMerge(layerThreads),
      Layer.fresh,
    );
    const built = yield* Layer.build(layerServer).pipe(
      // The test steps each retry; the server's own schedule backs off to 5 min.
      Effect.provideService(RemoteDelegation.FollowRetrySchedule, there.retrySchedule),
    );
    return {
      remote: Context.get(built, RemoteDelegation.RemoteDelegation),
      orchestrator: Context.get(built, Orchestrator.OrchestratorV2),
      threads: Context.get(built, ThreadManagement.ThreadManagementService),
      sink: Context.get(built, EventSink.EventSinkV2),
      projections: Context.get(built, ProjectionStore.ProjectionStoreV2),
    };
  });
type ParentServer = Effect.Success<ReturnType<typeof makeParentServer>>;

/** The parent thread, mid-turn when its agent delegates. */
const seedParent = (server: ParentServer) =>
  Effect.gen(function* () {
    const now = yield* DateTime.now;
    const providerThreadId = ProviderThreadId.make("provider-thread:parent");
    yield* server.orchestrator.dispatch({
      type: "thread.create",
      createdBy: "user",
      creationSource: "web",
      commandId: CommandId.make("command:create:parent"),
      threadId: parentThreadId,
      projectId: ProjectId.make("project:here"),
      title: "Parent",
      modelSelection,
      runtimeMode: "full-access",
      interactionMode: "default",
      branch: null,
      worktreePath: null,
    });
    yield* server.sink.write({
      commandId: CommandId.make("command:seed:parent"),
      events: [
        {
          id: EventId.make("event:seed-provider-thread:parent"),
          type: "provider-thread.updated",
          threadId: parentThreadId,
          driver,
          providerInstanceId: instanceId,
          occurredAt: now,
          payload: {
            id: providerThreadId,
            driver,
            providerInstanceId: instanceId,
            providerSessionId: null,
            appThreadId: parentThreadId,
            ownerNodeId: parentRootNode,
            nativeThreadRef: { driver, nativeId: "native:parent", strength: "strong" },
            nativeConversationHeadRef: null,
            status: "active",
            firstRunOrdinal: 1,
            lastRunOrdinal: 1,
            handoffIds: [],
            forkedFrom: null,
            createdAt: now,
            updatedAt: now,
          },
        },
        {
          id: EventId.make("event:seed-node:parent"),
          type: "node.updated",
          threadId: parentThreadId,
          runId: parentRunId,
          nodeId: parentRootNode,
          driver,
          providerInstanceId: instanceId,
          occurredAt: now,
          payload: {
            id: parentRootNode,
            threadId: parentThreadId,
            runId: parentRunId,
            parentNodeId: null,
            rootNodeId: parentRootNode,
            kind: "root_turn",
            status: "running",
            countsForRun: true,
            providerThreadId,
            providerTurnId: null,
            nativeItemRef: null,
            runtimeRequestId: null,
            checkpointScopeId: null,
            startedAt: now,
            completedAt: null,
          },
        },
        {
          id: EventId.make("event:seed-run:parent"),
          type: "run.updated",
          threadId: parentThreadId,
          runId: parentRunId,
          nodeId: parentRootNode,
          providerInstanceId: instanceId,
          occurredAt: now,
          payload: {
            id: parentRunId,
            threadId: parentThreadId,
            ordinal: 1,
            providerInstanceId: instanceId,
            modelSelection,
            providerThreadId,
            userMessageId: MessageId.make("message:seed-user:parent"),
            rootNodeId: parentRootNode,
            activeAttemptId: null,
            status: "running",
            requestedAt: now,
            startedAt: now,
            completedAt: null,
            checkpointId: null,
            contextHandoffId: null,
          },
        },
      ],
    });
  });

/**
 * What the parent holds for the task: its result, the wake upstream reserved for it on the
 * parent's run (its message id, generation, and the generations handed out on that run), and the
 * wake messages naming it, which upstream's continuation worker writes when it takes the wake.
 */
const parentView = (server: ParentServer, taskId: NodeId) =>
  server.orchestrator.getThreadProjection(parentThreadId).pipe(
    Effect.map((projection) => {
      const task = projection.subagents.find((candidate) => candidate.id === taskId);
      const cohort = projection.runs.find((run) => run.id === parentRunId)?.delegatedCompletion;
      return {
        task,
        wake: {
          messageId: cohort?.delivery?.messageId,
          generation: cohort?.delivery?.generation,
          generations: (cohort?.nextGeneration ?? 1) - 1,
          taskIds: cohort?.delivery?.taskIds ?? [],
          messages: projection.messages
            .filter((message) => message.delegatedCompletion?.taskIds.includes(taskId) === true)
            .map((message) => message.id),
        },
      };
    }),
  );

/**
 * Waits for the task's end to be recorded here with its wake claimed, and for the wake's message
 * to be written, then reads the parent.
 */
const awaitResult = (server: ParentServer, taskId: NodeId) =>
  Effect.gen(function* () {
    yield* server.orchestrator
      .streamStoredEventsFrom({
        threadId: parentThreadId,
        afterSequence: 0,
        eventType: "subagent.updated",
      })
      .pipe(
        Stream.filter(
          ({ event }) =>
            event.type === "subagent.updated" &&
            event.payload.id === taskId &&
            event.payload.result !== null &&
            event.payload.completionDelivery?.state === "claimed",
        ),
        Stream.runHead,
      );
    yield* server.orchestrator
      .streamStoredEventsFrom({
        threadId: parentThreadId,
        afterSequence: 0,
        eventType: "message.updated",
      })
      .pipe(
        Stream.filter(
          ({ event }) =>
            event.type === "message.updated" &&
            event.payload.delegatedCompletion?.taskIds.includes(taskId) === true,
        ),
        Stream.runHead,
      );
    return yield* parentView(server, taskId);
  });

/** Nothing left for a start to follow: no open task, and its thread there is stopped. */
const expectNothingToFollow = (server: ParentServer) =>
  Effect.gen(function* () {
    expect(yield* server.projections.getOpenRemoteDelegatedTasks).toEqual([]);
  });

/** The task ended here once, with this result, and one wake for it was reserved. */
const expectDeliveredOnce = (
  view: Effect.Success<ReturnType<typeof parentView>>,
  taskId: NodeId,
  result: string,
) => {
  expect(view.task).toMatchObject({
    status: "completed",
    result,
    completionDelivery: { state: "claimed" },
  });
  expect(view.task?.remoteChild?.stoppedThere).toBe(true);
  expect(view.wake).toMatchObject({ generation: 1, generations: 1, taskIds: [taskId] });
  // The worker took the wake: exactly one message names the task, the reserved one.
  expect(view.wake.messages).toEqual([view.wake.messageId]);
  return view.wake;
};

const delegate = (server: ParentServer, key = "suite-on-box") =>
  server.remote.delegate(scope, delegateInput(key)).pipe(Effect.map(({ taskId }) => taskId));

const memoryDatabase = SqlitePersistence.layerMemory.pipe(Layer.build);

it.live("the link is down when the child ends: the end arrives once the link is back", () =>
  Effect.gen(function* () {
    const there = yield* makeBox;
    const server = yield* makeParentServer(yield* memoryDatabase, there);
    yield* seedParent(server);
    const taskId = yield* delegate(server);
    // The link drops, and the task ends there meanwhile; tries while it is down fail.
    yield* there.update({ reachable: false, status: "completed", reply });
    yield* there.failedThenRetry;
    yield* Queue.take(there.failed);
    expect(there.state.failedCalls).toBe(2);
    expect((yield* parentView(server, taskId)).task?.result).toBeNull();
    yield* there.update({ reachable: true });
    yield* there.step;
    const wake = expectDeliveredOnce(yield* awaitResult(server, taskId), taskId, reply);
    // The follower is done: nothing left to follow, and nothing more here.
    yield* expectNothingToFollow(server);
    expect(expectDeliveredOnce(yield* parentView(server, taskId), taskId, reply)).toEqual(wake);
  }).pipe(Effect.scoped),
);

it.live(
  "the parent server restarts while the child runs: the next start follows it to its end",
  () =>
    Effect.gen(function* () {
      const there = yield* makeBox;
      const database = yield* memoryDatabase;
      const taskId = yield* Effect.scoped(
        Effect.gen(function* () {
          const before = yield* makeParentServer(database, there);
          yield* seedParent(before);
          return yield* delegate(before);
        }),
      );
      // The task ends there while the parent server is down.
      yield* there.update({ status: "completed", reply });
      const after = yield* makeParentServer(database, there);
      yield* after.remote.start();
      expectDeliveredOnce(yield* awaitResult(after, taskId), taskId, reply);
      yield* expectNothingToFollow(after);
    }).pipe(Effect.scoped),
);

it.live("a parent server stopped half-way through recording the end records it once after", () =>
  Effect.gen(function* () {
    const there = yield* makeBox;
    const database = yield* memoryDatabase;
    there.state.holdRead = yield* Deferred.make<void>();
    there.state.readHeld = yield* Deferred.make<void>();
    const readHeld = there.state.readHeld;
    const taskId = yield* Effect.scoped(
      Effect.gen(function* () {
        const before = yield* makeParentServer(database, there);
        yield* seedParent(before);
        const taskId = yield* delegate(before);
        // The run ends there; the server stops while the follower reads its reply.
        yield* there.update({ status: "completed", reply });
        yield* Deferred.await(readHeld);
        expect((yield* parentView(before, taskId)).task?.result).toBeNull();
        return taskId;
      }),
    );
    there.state.holdRead = null;
    const after = yield* makeParentServer(database, there);
    yield* after.remote.start();
    const wake = expectDeliveredOnce(yield* awaitResult(after, taskId), taskId, reply);
    // Started again with the end recorded: nothing to follow, nothing to wake.
    yield* expectNothingToFollow(after);
    yield* Effect.scoped(
      Effect.gen(function* () {
        const again = yield* makeParentServer(database, there);
        yield* again.remote.start();
        yield* expectNothingToFollow(again);
        const view = yield* parentView(again, taskId);
        expect(expectDeliveredOnce(view, taskId, reply)).toEqual(wake);
      }),
    );
  }).pipe(Effect.scoped),
);

it.live("the machine there restarts: the run's continuation brings the end, once", () =>
  Effect.gen(function* () {
    const there = yield* makeBox;
    const server = yield* makeParentServer(yield* memoryDatabase, there);
    yield* seedParent(server);
    const taskId = yield* delegate(server);
    // The machine there goes down mid-run and comes back; its recovery continues the run.
    yield* there.update({ reachable: false });
    yield* Queue.take(there.failed);
    yield* there.update({ restarted: true, reachable: true });
    yield* there.step;
    expect((yield* parentView(server, taskId)).task?.result).toBeNull();
    yield* there.update({
      status: "completed",
      reply: "Continued after the restart: all tests pass.",
    });
    expectDeliveredOnce(
      yield* awaitResult(server, taskId),
      taskId,
      "Continued after the restart: all tests pass.",
    );
  }).pipe(Effect.scoped),
);

it.live("a duplicate report of the end records nothing and never wakes the parent again", () =>
  Effect.gen(function* () {
    const there = yield* makeBox;
    const server = yield* makeParentServer(yield* memoryDatabase, there);
    yield* seedParent(server);
    const taskId = yield* delegate(server);
    yield* there.update({ status: "completed", reply });
    const wake = expectDeliveredOnce(yield* awaitResult(server, taskId), taskId, reply);
    // A second report, as a follower on another start or a retry after a lost answer sends.
    for (const attempt of ["again-1", "again-2"]) {
      yield* server.threads
        .dispatch({
          type: "delegated_task.remote.complete",
          commandId: CommandId.make(`command:remote-task-complete:${taskId}:${attempt}`),
          parentThreadId,
          taskId,
          status: "failed",
          result: "A later, different report.",
          stoppedThere: "ran-out",
        })
        .pipe(Effect.ignore);
    }
    // The same delegate call again (a retry with its key) launches and records nothing new.
    expect(yield* delegate(server)).toBe(taskId);
    expect(there.state.launches).toBe(1);
    expect(expectDeliveredOnce(yield* parentView(server, taskId), taskId, reply)).toEqual(wake);
    yield* expectNothingToFollow(server);
  }).pipe(Effect.scoped),
);

it.live("a child that ended before the parent knew its id still reaches the parent once", () =>
  Effect.gen(function* () {
    const there = yield* makeBox;
    there.state.endedAtLaunch = true;
    const server = yield* makeParentServer(yield* memoryDatabase, there);
    yield* seedParent(server);
    const taskId = yield* delegate(server);
    expectDeliveredOnce(yield* awaitResult(server, taskId), taskId, reply);
  }).pipe(Effect.scoped),
);

it.live("a child launched with no run there, already ended, reaches the parent once", () =>
  Effect.gen(function* () {
    const there = yield* makeBox;
    there.state.endedAtLaunch = true;
    there.state.runless = true;
    const server = yield* makeParentServer(yield* memoryDatabase, there);
    yield* seedParent(server);
    const taskId = yield* delegate(server);
    expectDeliveredOnce(yield* awaitResult(server, taskId), taskId, reply);
  }).pipe(Effect.scoped),
);
