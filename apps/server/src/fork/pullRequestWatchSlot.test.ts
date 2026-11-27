/**
 * Fork: agent slots (issue 711) for upstream's pull request watch wake-ups
 * (fork/pullRequestWatchSlot.ts). The wrapper alone over a recording orchestrator, then
 * upstream's own watch reactor and orchestrator through it: a refused wake waits for a slot, is
 * sent once when one frees, survives a restart, and a refused "stopped watching" is not lost.
 */
import * as McpProviderSessions from "@t3tools/provider-core/server/McpProviderSessions";
import type * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import type { ProviderInstance } from "@t3tools/provider-core/server/driver";
import { PullRequestProviderError } from "@t3tools/source-control-core/server/PullRequestProvider";
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, expect, it } from "@effect/vitest";
import {
  CommandId,
  EventId,
  MessageId,
  type ModelSelection,
  type OrchestrationV2ServerCommand,
  ProjectId,
  type PullRequestDetail,
  PullRequestOperationError,
  ProviderDriverKind,
  ProviderInstanceId,
  SourceControlProviderKind,
  ThreadId,
} from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Stream from "effect/Stream";
import * as TestClock from "effect/testing/TestClock";

import * as CheckpointStore from "../checkpointing/CheckpointStore.ts";
import * as ServerConfig from "../config.ts";
import * as GitWorkflow from "../git/GitWorkflowService.ts";
import * as McpSessionRegistryTestkit from "../mcp/McpSessionRegistry.testkit.ts";
import { CodexProviderCapabilitiesV2 } from "../orchestration-v2/Adapters/CodexAdapterV2.ts";
import * as EffectOutbox from "../orchestration-v2/EffectOutbox.ts";
import * as EffectWorker from "../orchestration-v2/EffectWorker.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ProjectStore from "../orchestration-v2/ProjectStore.ts";
import * as PullRequestWatchReactor from "../orchestration-v2/PullRequestWatchReactor.ts";
import { layerNoTaskFolders } from "../orchestration-v2/ProviderTurnStartService.testkit.ts";
import * as RuntimeLayer from "../orchestration-v2/runtimeLayer.ts";
import * as ThreadCommandExecutor from "../orchestration-v2/ThreadCommandExecutor.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as ProjectSetupScriptRunner from "../project/ProjectSetupScriptRunner.ts";
import * as ModelManifest from "../provider/ModelManifest.ts";
import * as ProviderInstanceRegistry from "../provider/ProviderInstanceRegistry.ts";
import * as PullRequestService from "../pullRequest/PullRequestService.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as SourceControlProviderRegistry from "../sourceControl/SourceControlProviderRegistry.ts";
import * as VcsDriverRegistry from "../vcs/VcsDriverRegistry.ts";
import * as VcsProcess from "../vcs/VcsProcess.ts";
import { makeSlots } from "./delegatedResults/delegatedResults.testkit.ts";
import {
  FORGET_WATCH_KEY_MS,
  gatePullRequestWatchWakes,
  layerOver,
  PullRequestWatchWakeWaitsForSlot,
} from "./pullRequestWatchSlot.ts";

const THREAD = ThreadId.make("pr-watch-slot-thread");
const INSTANCE = ProviderInstanceId.make("codex");

const watchSync = (input: {
  readonly id: string;
  readonly wake: boolean;
  readonly ends?: boolean;
  readonly number?: number;
  readonly others?: ReadonlyArray<{ readonly number: number; readonly ends: boolean }>;
}): OrchestrationV2ServerCommand => {
  const watch = (ends: boolean) =>
    ends
      ? null
      : {
          startedAt: "2026-10-01T00:00:00.000Z",
          headSha: null,
          failedChecks: [],
          passed: false,
        };
  return {
    type: "thread.pull-request-watch.sync",
    commandId: CommandId.make(`pr-watch-slot:${input.id}`),
    threadId: THREAD,
    host: "github.com",
    repository: "acme/widgets",
    number: input.number ?? 1,
    startedAt: "2026-10-01T00:00:00.000Z",
    watch: watch(input.ends ?? false),
    ...(input.others === undefined
      ? {}
      : {
          others: input.others.map((other) => ({
            host: "github.com",
            repository: "acme/widgets",
            number: other.number,
            startedAt: "2026-10-01T00:00:00.000Z",
            watch: watch(other.ends),
          })),
        }),
    ...(input.wake
      ? {
          wake: {
            messageId: MessageId.make(`message:pr-watch:${input.id}`),
            text: "news",
            notification: {
              source: { kind: "monitor" },
              outcome: "succeeded",
              summary: "#1: news",
            },
          },
        }
      : {}),
  } as unknown as OrchestrationV2ServerCommand;
};

/** A recording orchestrator, the mock gate and the wrapper over both. */
const makeWrapped = (instance: () => Effect.Effect<ProviderInstanceId | null, unknown>) =>
  Effect.gen(function* () {
    const sent: Array<OrchestrationV2ServerCommand> = [];
    const slots = makeSlots();
    const inner = {
      dispatch: (command: OrchestrationV2ServerCommand) =>
        Effect.sync(() => {
          sent.push(command);
          return { sequence: sent.length, storedEvents: [] };
        }),
    } as unknown as Orchestrator.OrchestratorV2["Service"];
    const gate = yield* Effect.serviceOption(SlotGate.SlotGate).pipe(Effect.provide(slots.layer));
    const wrapped = gatePullRequestWatchWakes(inner, gate, instance);
    return { sent, slots, wrapped };
  });

const failureOf = <A, E>(exit: Exit.Exit<A, E>): unknown =>
  Exit.isFailure(exit) ? Option.getOrUndefined(Cause.findErrorOption(exit.cause)) : undefined;

const infoLogs = (into: Array<string>) =>
  Logger.layer(
    [
      Logger.make(({ message, logLevel }) => {
        if (logLevel === "Info") into.push(JSON.stringify(message));
      }),
    ],
    { mergeWithExisting: false },
  );

describe("pull request watch wake-ups take an agent slot", () => {
  it.effect("asks the gate for the thread's own start; an allowed wake goes through", () =>
    Effect.gen(function* () {
      const { sent, slots, wrapped } = yield* makeWrapped(() => Effect.succeed(INSTANCE));
      const command = watchSync({ id: "a", wake: true });
      yield* wrapped.dispatch(command);
      expect(sent).toEqual([command]);
      expect(slots.asks).toEqual([
        {
          callerThreadId: THREAD,
          targetInstanceId: INSTANCE,
          intoThreadId: THREAD,
          keepsHeld: true,
          // Its place in the gate's line, kept over 2 missed sweeps (2 min each).
          place: { key: `pull-request-watch:${THREAD}`, staleAfter: Duration.minutes(5) },
        },
      ]);
    }),
  );

  const logs: Array<string> = [];
  it.effect("a refused wake fails its dispatch and sends nothing; it goes once a slot frees", () =>
    Effect.gen(function* () {
      const { sent, slots, wrapped } = yield* makeWrapped(() => Effect.succeed(INSTANCE));
      slots.state.all = true;
      const first = yield* Effect.exit(wrapped.dispatch(watchSync({ id: "b1", wake: true })));
      const again = yield* Effect.exit(wrapped.dispatch(watchSync({ id: "b2", wake: true })));
      for (const exit of [first, again]) {
        const error = failureOf(exit);
        expect(error).toBeInstanceOf(Orchestrator.OrchestratorDispatchError);
        expect((error as Orchestrator.OrchestratorDispatchError).cause).toBeInstanceOf(
          PullRequestWatchWakeWaitsForSlot,
        );
      }
      expect(sent).toEqual([]);
      slots.state.all = false;
      const landed = watchSync({ id: "b3", wake: true });
      yield* wrapped.dispatch(landed);
      expect(sent).toEqual([landed]);
      // Logged once while it waited; a later wait of the same watch logs again.
      slots.state.all = true;
      yield* Effect.exit(wrapped.dispatch(watchSync({ id: "b4", wake: true })));
      expect(logs.filter((log) => log.includes("wake-up waits for an agent slot"))).toHaveLength(2);
    }).pipe(Effect.provide(infoLogs(logs))),
  );

  it.effect("updates with no wake and other commands pass without asking the gate", () =>
    Effect.gen(function* () {
      const { sent, slots, wrapped } = yield* makeWrapped(() => Effect.succeed(INSTANCE));
      slots.state.all = true;
      const update = watchSync({ id: "c1", wake: false });
      const end = watchSync({ id: "c2", wake: false, ends: true });
      const stop = {
        type: "thread.stop",
        threadId: THREAD,
      } as unknown as OrchestrationV2ServerCommand;
      yield* wrapped.dispatch(update);
      yield* wrapped.dispatch(end);
      yield* wrapped.dispatch(stop);
      expect(sent).toEqual([update, end, stop]);
      expect(slots.asks).toEqual([]);
    }),
  );

  it.effect("after a refused ending wake, its watch's next silent end is refused once", () =>
    Effect.gen(function* () {
      const { sent, slots, wrapped } = yield* makeWrapped(() => Effect.succeed(INSTANCE));
      slots.state.all = true;
      // Upstream's give-up: the ending wake, then on failure the same end with no wake.
      yield* Effect.exit(wrapped.dispatch(watchSync({ id: "d1", wake: true, ends: true })));
      const fallback = yield* Effect.exit(
        wrapped.dispatch(watchSync({ id: "d2", wake: false, ends: true })),
      );
      expect(failureOf(fallback)).toBeInstanceOf(Orchestrator.OrchestratorDispatchError);
      expect(sent).toEqual([]);
      // Once: a later silent end (the agent's own merge, say) goes through.
      const later = watchSync({ id: "d3", wake: false, ends: true });
      yield* wrapped.dispatch(later);
      expect(sent).toEqual([later]);
      // A refused wake that does not end the watch leaves its silent end alone.
      yield* Effect.exit(wrapped.dispatch(watchSync({ id: "d4", wake: true, number: 2 })));
      const other = watchSync({ id: "d5", wake: false, ends: true, number: 2 });
      yield* wrapped.dispatch(other);
      expect(sent).toEqual([later, other]);
      // An ending watch recorded together with others counts too.
      yield* Effect.exit(
        wrapped.dispatch(
          watchSync({ id: "d6", wake: true, number: 3, others: [{ number: 4, ends: true }] }),
        ),
      );
      const fourEnds = watchSync({ id: "d7", wake: false, ends: true, number: 4 });
      expect(failureOf(yield* Effect.exit(wrapped.dispatch(fourEnds)))).toBeDefined();
      // A wake that lands clears it.
      yield* Effect.exit(
        wrapped.dispatch(watchSync({ id: "d8", wake: true, ends: true, number: 5 })),
      );
      slots.state.all = false;
      yield* wrapped.dispatch(watchSync({ id: "d9", wake: true, ends: true, number: 5 }));
      const fiveEnds = watchSync({ id: "d10", wake: false, ends: true, number: 5 });
      yield* wrapped.dispatch(fiveEnds);
      expect(sent.at(-1)).toEqual(fiveEnds);
    }),
  );

  const forgetLogs: Array<string> = [];
  it.effect(
    "a watch no refusal renews for 10 minutes (removed, its thread gone) is forgotten",
    () =>
      Effect.gen(function* () {
        const { sent, slots, wrapped } = yield* makeWrapped(() => Effect.succeed(INSTANCE));
        slots.state.all = true;
        yield* Effect.exit(wrapped.dispatch(watchSync({ id: "f1", wake: true, ends: true })));
        yield* Effect.exit(wrapped.dispatch(watchSync({ id: "f2", wake: true, number: 2 })));
        // Retried each sweep (2 min): still known.
        yield* TestClock.adjust(FORGET_WATCH_KEY_MS - 60_000);
        yield* Effect.exit(wrapped.dispatch(watchSync({ id: "f3", wake: true, number: 2 })));
        yield* TestClock.adjust(FORGET_WATCH_KEY_MS + 1);
        // The ending watch's key is gone: its silent end is no longer held back.
        const end = watchSync({ id: "f4", wake: false, ends: true });
        yield* wrapped.dispatch(end);
        expect(sent).toEqual([end]);
        // The waiting watch's key too: a new wait of it is logged again.
        yield* Effect.exit(wrapped.dispatch(watchSync({ id: "f5", wake: true, number: 2 })));
        expect(
          forgetLogs.filter((log) => log.includes("wake-up waits for an agent slot")),
        ).toHaveLength(3);
      }).pipe(Effect.provide(infoLogs(forgetLogs))),
  );

  it.effect("a thread that cannot be read waits; one that is gone is upstream's to refuse", () =>
    Effect.gen(function* () {
      const unreadable = yield* makeWrapped(() => Effect.fail("unreadable"));
      const exit = yield* Effect.exit(
        unreadable.wrapped.dispatch(watchSync({ id: "e1", wake: true })),
      );
      expect(failureOf(exit)).toBeInstanceOf(Orchestrator.OrchestratorDispatchError);
      expect(unreadable.sent).toEqual([]);
      expect(unreadable.slots.asks).toEqual([]);
      const gone = yield* makeWrapped(() => Effect.succeed(null));
      const command = watchSync({ id: "e2", wake: true });
      yield* gone.wrapped.dispatch(command);
      expect(gone.sent).toEqual([command]);
      expect(gone.slots.asks).toEqual([]);
      // Its wake is not owed any more: its place in the gate's line goes with it.
      expect(gone.slots.left).toEqual([`pull-request-watch:${THREAD}`]);
    }),
  );

  it("with no gate it is upstream's own orchestrator", () => {
    const inner = {
      dispatch: () => Effect.die("unused"),
    } as unknown as Orchestrator.OrchestratorV2["Service"];
    expect(gatePullRequestWatchWakes(inner, Option.none(), () => Effect.succeed(null))).toBe(inner);
  });

  it.effect(
    "server wiring: the runtime workers' orchestrator, which the watch reactor reads, is gated",
    () =>
      Effect.gen(function* () {
        const path = yield* Path.Path;
        const fileSystem = yield* FileSystem.FileSystem;
        const read = (relative: string) =>
          Effect.flatMap(path.fromFileUrl(new URL(relative, import.meta.url)), (file) =>
            fileSystem.readFileString(file),
          );
        const server = yield* read("../server.ts");
        const start = server.indexOf("const layerRuntimeCoreDependenciesBase = Layer.mergeAll(");
        const pipe = server.indexOf("\n).pipe(\n", start) + 1;
        expect(server.slice(start, pipe)).toContain(
          "PullRequestWatchReactor.PullRequestWatchReactor",
        );
        const forkLayers = yield* read("./forkLayers.ts");
        expect(forkLayers).toMatch(
          /export const handoffThreadsSkipIdleCompaction = PullRequestWatchSlot\.layer\.pipe\(\s*Layer\.provide\(HandoffOwner\.layer\),\s*Layer\.provide\(WorkerWakeSlots\.layer\),\s*\);/,
        );
        expect(yield* read("./workerWakeSlots.ts")).toMatch(
          /export const layer = layerOver\.pipe\(\s*Layer\.provide\(Layer\.merge\(SlotGate\.layer, ProjectionStore\.layer\)\),\s*\);/,
        );
        expect(yield* read("./handoff/HandoffService.ts")).toContain(
          "  Layer.provide(SlotGate.layer),\n);",
        );
        const own = yield* read("./pullRequestWatchSlot.ts");
        expect(own).toMatch(
          /export const layer = layerOver\.pipe\(\s*Layer\.provide\(Layer\.merge\(SlotGate\.layer, ProjectionStore\.layer\)\),\s*\);/,
        );
      }).pipe(Effect.provide(NodeServices.layer)),
  );
});

// Upstream's watch reactor and orchestrator, as runtimeLayer.test.ts builds them.
const layerPlatformTest = Layer.merge(
  Layer.mergeAll(NodeServices.layer, NodeCrypto.layer),
  Layer.mock(SourceControlProviderRegistry.SourceControlProviderRegistry)({
    resolveLink: () => Effect.die("unused title link"),
  }),
);
const layerServerConfig = ServerConfig.layerTest(process.cwd(), {
  prefix: "t3-fork-pr-watch-slot-",
});
const modelSelection = { instanceId: INSTANCE, model: "gpt-5.4" } satisfies ModelSelection;
const driver = ProviderDriverKind.make("codex");
const providerInstance = {
  instanceId: INSTANCE,
  driverKind: driver,
  continuationIdentity: { driverKind: driver, continuationKey: "codex:test" },
  displayName: "Codex test",
  enabled: true,
  snapshot: { getSnapshot: Effect.succeed({}) } as unknown as ProviderInstance["snapshot"],
  orchestrationAdapter: {
    instanceId: INSTANCE,
    driver,
    getCapabilities: () => Effect.succeed(CodexProviderCapabilitiesV2),
    planSelectionTransition: () => Effect.succeed({ type: "apply_on_next_turn" }),
    openSession: () => Effect.die("sessions are not used here"),
  } as ProviderAdapter.ProviderAdapterV2["Service"],
  textGeneration: {} as ProviderInstance["textGeneration"],
} satisfies ProviderInstance;
const layerRuntimeTest = Layer.mergeAll(
  RuntimeLayer.layer,
  RuntimeLayer.layerEventSink,
  ProjectStore.layer,
  ProjectionStore.layer,
  EffectOutbox.layer,
  ThreadCommandExecutor.layer,
).pipe(
  Layer.provide(McpSessionRegistryTestkit.layer),
  Layer.provide(McpProviderSessions.layer),
  Layer.provide(SqlitePersistence.layerMemory),
  Layer.provide(
    CheckpointStore.layer.pipe(
      Layer.provide(
        VcsDriverRegistry.layer.pipe(
          Layer.provide(VcsProcess.layer),
          Layer.provide(layerServerConfig),
          Layer.provide(layerPlatformTest),
        ),
      ),
    ),
  ),
  Layer.provide(layerServerConfig),
  Layer.provide(ServerSettings.layerTest()),
  Layer.provide(
    Layer.succeed(ProviderInstanceRegistry.ProviderInstanceRegistry, {
      getInstance: (instanceId) =>
        Effect.succeed(instanceId === INSTANCE ? providerInstance : undefined),
      listInstances: Effect.succeed([providerInstance]),
      listUnavailable: Effect.succeed([]),
      streamChanges: Stream.empty,
      subscribeChanges: Effect.never,
    }),
  ),
  Layer.provide(
    Layer.mock(GitWorkflow.GitWorkflowService)({
      pruneWorktrees: () => Effect.void,
      createWorktree: () => Effect.succeed({} as never),
    }),
  ),
  Layer.provide(Layer.mock(ProjectService.ProjectService)({ getById: () => Effect.succeedNone })),
  // Upstream's runtime layer now needs task folders and the model manifest (as in its own test).
  Layer.provide(Layer.merge(layerNoTaskFolders, ModelManifest.layerTest)),
  // And, since v0.1.14, the setup script runner (this test runs no setup script).
  Layer.provide(
    Layer.mock(ProjectSetupScriptRunner.ProjectSetupScriptRunner)({
      runForThread: () => Effect.succeed({ status: "no-script" } as const),
    }),
  ),
  Layer.provide(layerPlatformTest),
);

const pullRequestDetail = (input: {
  readonly projectId: ProjectId;
  readonly number: number;
  readonly passed: boolean;
}): PullRequestDetail => ({
  provider: SourceControlProviderKind.make("github"),
  capabilities: {
    diff: true,
    comment: true,
    actions: [],
    mergeMethods: [],
    search: false,
    checksAlwaysReport: true,
    review: { inlineComment: false, reply: false, resolve: false, verdicts: [] },
    reviewers: { request: false, listCandidates: false },
  },
  viewerPermissions: {
    actions: [],
    comment: true,
    resolve: true,
    verdicts: [],
    requestReviewers: false,
  },
  projectId: input.projectId,
  projectTitle: "Watch wake",
  workspaceRoot: "/workspace/watch",
  repository: "acme/widgets",
  number: input.number,
  title: "Watched pull request",
  body: "",
  url: `https://github.com/acme/widgets/pull/${input.number}`,
  author: { login: "agent-user", name: null, avatarUrl: null },
  state: "open",
  isDraft: false,
  mergeability: "mergeable",
  additions: 1,
  deletions: 0,
  changedFiles: 1,
  headBranch: "feature",
  headSha: "abc1234def",
  baseBranch: "main",
  createdAt: "2026-10-02T12:00:00.000Z",
  updatedAt: "2026-10-02T12:00:00.000Z",
  mergedAt: null,
  closedAt: null,
  reviewers: [],
  labels: [],
  checks: [
    {
      name: "test",
      status: input.passed ? "success" : "pending",
      description: null,
      url: null,
      required: true,
    },
  ],
  mergeCapabilities: { merge: true, squash: true, rebase: true },
  viewer: "agent-user",
});

/** A project, an idle thread in it, and a watch on pull request `number`. */
const watchedThread = (name: string, number: number) =>
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    const projectId = ProjectId.make(`pr-watch-slot-project-${name}`);
    const threadId = ThreadId.make(`pr-watch-slot-thread-${name}`);
    yield* Effect.flatMap(ProjectStore.ProjectStoreV2, (projects) =>
      projects.apply({
        sequence: 0,
        eventId: EventId.make(`seed:${projectId}`),
        aggregateKind: "project",
        aggregateId: projectId,
        occurredAt: "2026-10-01T00:00:00.000Z",
        commandId: null,
        causationEventId: null,
        correlationId: null,
        metadata: {},
        type: "project.created",
        payload: {
          projectId,
          title: "Watch slot",
          workspaceRoot: `/workspace/pr-watch-slot-${name}`,
          defaultModelSelection: null,
          scripts: [],
          createdAt: "2026-10-01T00:00:00.000Z",
          updatedAt: "2026-10-01T00:00:00.000Z",
        },
      }),
    );
    yield* orchestrator.dispatch({
      type: "thread.create",
      createdBy: "user",
      creationSource: "web",
      commandId: CommandId.make(`pr-watch-slot-create-${name}`),
      threadId,
      projectId,
      title: "Watch slot",
      modelSelection,
      runtimeMode: "full-access",
      interactionMode: "default",
      branch: null,
      worktreePath: null,
    });
    yield* orchestrator.dispatch({
      type: "thread.pull-request.watch",
      commandId: CommandId.make(`pr-watch-slot-start-${name}`),
      threadId,
      host: "github.com",
      repository: "acme/widgets",
      number,
      watching: true,
      link: { url: `https://github.com/acme/widgets/pull/${number}`, source: "agent" },
    });
    const summaries = Effect.map(
      orchestrator.getThreadRecords(threadId, ["messages"]),
      ({ messages }) => messages.flatMap((message) => message.notification?.summary ?? []),
    );
    const watch = Effect.map(
      orchestrator.getThreadShell(threadId),
      (shell) => shell?.pullRequests?.[0]?.watch,
    );
    return { orchestrator, projectId, threadId, summaries, watch };
  });

const noActivity = () =>
  Effect.succeed({
    comments: [],
    commentCount: 0,
    commentsTruncated: false,
    reviewThreads: [],
    commits: [],
  });

it.layer(layerRuntimeTest)("upstream's watch reactor through the slot gate", (it) => {
  it.effect(
    "a wake into an idle thread waits for a slot, survives a restart, and is sent once",
    () =>
      Effect.gen(function* () {
        const { projectId, threadId, summaries } = yield* watchedThread("checks", 31);
        const slots = makeSlots();
        let passed = false;
        const host = Layer.mergeAll(
          NodeServices.layer,
          NodeCrypto.layer,
          Layer.mock(PullRequestService.PullRequestService)({
            watchFingerprint: () => Effect.succeed(null),
            detail: () => Effect.succeed(pullRequestDetail({ projectId, number: 31, passed })),
            activity: noActivity,
          }),
          layerOver.pipe(Layer.provide(slots.layer)),
        );
        const worker = yield* EffectWorker.OrchestrationEffectWorkerV2;
        const reactor = yield* PullRequestWatchReactor.make.pipe(Effect.provide(host));
        const sweepOf = (r: typeof reactor) =>
          TestClock.adjust("2 minutes").pipe(
            Effect.andThen(r.sweep),
            Effect.andThen(worker.drain()),
          );
        yield* sweepOf(reactor);
        assert.deepEqual(yield* summaries, []);
        passed = true;
        slots.full.add(threadId);
        for (let pass = 0; pass < 3; pass += 1) yield* sweepOf(reactor);
        assert.deepEqual(yield* summaries, []);
        expect(slots.asks.filter((ask) => ask.intoThreadId === threadId)).toHaveLength(3);
        // A restart: a new reactor finds the same news from the stored watch.
        const restarted = yield* PullRequestWatchReactor.make.pipe(Effect.provide(host));
        yield* sweepOf(restarted);
        assert.deepEqual(yield* summaries, []);
        slots.full.delete(threadId);
        yield* sweepOf(restarted);
        assert.deepEqual(yield* summaries, ["#31: checks passed"]);
        for (let pass = 0; pass < 3; pass += 1) yield* sweepOf(restarted);
        assert.deepEqual(yield* summaries, ["#31: checks passed"]);
      }),
  );

  it.effect("a refused 'stopped watching' keeps the watch, and is sent once a slot frees", () =>
    Effect.gen(function* () {
      const { threadId, summaries, watch } = yield* watchedThread("unreadable", 32);
      const slots = makeSlots();
      const reactor = yield* PullRequestWatchReactor.make.pipe(
        Effect.provide(
          Layer.mergeAll(
            NodeServices.layer,
            NodeCrypto.layer,
            Layer.mock(PullRequestService.PullRequestService)({
              watchFingerprint: () => Effect.succeed(null),
              detail: () =>
                Effect.fail(
                  new PullRequestOperationError({
                    operation: "detail",
                    detail: "host read failed",
                    cause: new PullRequestProviderError({
                      provider: SourceControlProviderKind.make("github"),
                      operation: "detail",
                      reason: "not-found",
                      detail: "host read failed",
                    }),
                  }),
                ),
              activity: noActivity,
            }),
            layerOver.pipe(Layer.provide(slots.layer)),
          ),
        ),
      );
      const worker = yield* EffectWorker.OrchestrationEffectWorkerV2;
      slots.full.add(threadId);
      for (let pass = 0; pass < 15; pass += 1) {
        yield* TestClock.adjust("2 minutes");
        yield* reactor.sweep.pipe(Effect.andThen(worker.drain()));
      }
      assert.isDefined(yield* watch);
      assert.deepEqual(yield* summaries, []);
      slots.full.delete(threadId);
      for (let pass = 0; pass < 3; pass += 1) {
        yield* TestClock.adjust("2 minutes");
        yield* reactor.sweep.pipe(Effect.andThen(worker.drain()));
      }
      assert.isUndefined(yield* watch);
      assert.deepEqual(yield* summaries, ["#32: stopped watching, could not read it"]);
    }),
  );
});
