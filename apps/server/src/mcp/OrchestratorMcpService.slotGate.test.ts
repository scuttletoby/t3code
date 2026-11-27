/**
 * Fork (waits PR 7): the agent slot gate as `delegate_task` applies it. The rules are in
 * `shared/agentSlots.test.ts` and the gate's own state in `slots/SlotGate.test.ts`; here: the
 * failure the caller gets, what is dispatched, the order after the usage pacing gate, retries,
 * a failed dispatch, and that nothing is read while the gate is off.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  EnvironmentId,
  NodeId,
  type OrchestratorMcpTarget,
  type OrchestratorMcpTaskRole,
  type OrchestrationV2ThreadProjection,
  type OrchestrationV2ThreadShell,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  RunId,
  type ServerProvider,
  type ServerSettings as ServerSettingsValue,
  ThreadId,
} from "@t3tools/contracts";
import type * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import type { DeepPartial } from "@t3tools/shared/Struct";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import { TestClock } from "effect/testing";

import * as OrchestratorV2 from "../orchestration-v2/Orchestrator.ts";
import * as ProviderAdapterRegistry from "../orchestration-v2/ProviderAdapterRegistry.ts";
import * as ThreadLaunchService from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as ScheduledTaskService from "../scheduledTasks/ScheduledTaskService.ts";
import * as SecretRequests from "../secrets/SecretRequests.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as FillerDelegate from "../fork/fillerDelegate.ts";
import * as ActiveRunShells from "../slots/ActiveRunShells.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as UsagePacer from "../usage/UsagePacer.ts";
import * as GitVcsDriver from "../vcs/GitVcsDriver.ts";
import type { McpInvocationScope } from "./McpInvocationContext.ts";
import * as OrchestratorMcpService from "./OrchestratorMcpService.ts";

const CODEX = ProviderDriverKind.make("codex");
const MAIN = ProviderInstanceId.make("codex");
const SECOND = ProviderInstanceId.make("codex-second");
const MODEL = "gpt-5.4";
const NOW_MS = Date.parse("2026-11-14T12:00:00.000Z");
const MINUTE = 60 * 1000;
const DAY = 24 * 60 * MINUTE;
const MB = 1024 * 1024;

const parentThreadId = ThreadId.make("thread:slot-gate-parent");
const parentRunId = RunId.make("run:slot-gate-parent");
const projectId = ProjectId.make("project:slot-gate");

const at = (offsetMs: number) => DateTime.formatIso(DateTime.makeUnsafe(NOW_MS + offsetMs));

/** One account; `usedPercent` of a weekly window half way through (the even pace is 50%). */
const provider = (instanceId: ProviderInstanceId, usedPercent: number): ServerProvider => ({
  instanceId,
  driver: CODEX,
  enabled: true,
  installed: true,
  version: "test",
  status: "ready",
  auth: { status: "authenticated", email: `${instanceId}@example.com` },
  checkedAt: at(-MINUTE),
  models: [{ slug: MODEL, name: MODEL, isCustom: false, capabilities: null }],
  slashCommands: [],
  skills: [],
  usageLimits: {
    checkedAt: at(-MINUTE),
    windows: [
      {
        id: "seven_day",
        kind: "weekly",
        label: "Weekly",
        usedPercent,
        resetsAt: at(3.5 * DAY),
        windowDurationMins: 7 * 24 * 60,
      },
    ],
  },
});
const roomy = [provider(MAIN, 10), provider(SECOND, 10)];

const scope: McpInvocationScope = {
  environmentId: EnvironmentId.make("environment:slot-gate"),
  requestNamespace: "provider-session:slot-gate",
  thread: {
    threadId: parentThreadId,
    providerSessionId: "provider-session:slot-gate",
    providerInstanceId: MAIN,
  },
  client: undefined,
  capabilities: new Set(["orchestration"]),
  issuedAt: 1,
};

const parentProjection = (subagents: ReadonlyArray<unknown>): OrchestrationV2ThreadProjection =>
  ({
    thread: {
      id: parentThreadId,
      projectId,
      title: "Parent",
      createdBy: "user",
      creationSource: "web",
      modelSelection: { instanceId: MAIN, model: MODEL },
      runtimeMode: "full-access",
      interactionMode: "default",
    },
    runs: [
      {
        id: parentRunId,
        ordinal: 1,
        status: "running",
        rootNodeId: NodeId.make("node:slot-gate-root"),
        providerInstanceId: MAIN,
        modelSelection: { instanceId: MAIN, model: MODEL },
      },
    ],
    contextTransfers: [],
    subagents,
  }) as unknown as OrchestrationV2ThreadProjection;

const childProjection = (childThreadId: ThreadId, hasRun = false) =>
  ({
    thread: {
      id: childThreadId,
      title: "Child",
      modelSelection: { instanceId: MAIN, model: MODEL },
    },
    runs: hasRun ? [{ id: RunId.make(`run:${childThreadId}`), status: "running" }] : [],
    contextTransfers: [],
    messages: [],
    subagents: [],
    providerThreads: [],
    providerTurns: [],
    runtimeRequests: [],
    turnItems: [],
  }) as unknown as OrchestrationV2ThreadProjection;

/** What the projection shows and what the test saw. Shared by the calls of one test. */
interface World {
  /** Threads with an active run: id and account. */
  running: Array<{ readonly id: string; readonly instanceId: ProviderInstanceId }>;
  readonly tasks: Array<{ readonly id: NodeId; readonly childThreadId: ThreadId }>;
  readonly dispatched: Array<{ readonly instanceId: string }>;
  snapshotReads: number;
  /** The count read fails (the thread list cannot be read). */
  snapshotFails: boolean;
  /** Set: the count read signals it began, then never answers (a hung store). */
  snapshotHangs?: Deferred.Deferred<void> | undefined;
  memoryReads: number;
  freeMemoryBytes: number;
  /** The next dispatches fail (a rejected command). */
  failDispatches: number;
  /** The next dispatches take this long each (ms), in order. */
  readonly dispatchDelaysMs: Array<number>;
  /** The next dispatches return no task event. */
  noTaskEvents: number;
  /** Done when a dispatch began (a test waits on a slow one). */
  readonly dispatchBegun: Deferred.Deferred<void>;
  /** Requests whose delegation was accepted before (their receipt exists). */
  readonly accepted: Set<string>;
  /** Threads `create_threads` dispatched (`thread.create`), and whether each started a run. */
  readonly created: Array<{ readonly threadId: ThreadId; readonly startsRun: boolean }>;
  /** Runs after each `thread.create` (another caller takes a slot mid-batch, say). */
  afterCreate: (() => void) | undefined;
}

const newWorld = (): World => ({
  running: [],
  tasks: [],
  dispatched: [],
  snapshotReads: 0,
  snapshotFails: false,
  memoryReads: 0,
  freeMemoryBytes: 16_000 * MB,
  failDispatches: 0,
  dispatchDelaysMs: [],
  noTaskEvents: 0,
  dispatchBegun: Deferred.makeUnsafe<void>(),
  accepted: new Set(),
  created: [],
  afterCreate: undefined,
});

/** The server's layers for one test: the gate and /mcp share one settings service and world. */
const serverLayer = (
  world: World,
  settings: DeepPartial<ServerSettingsValue>,
  providers: ReadonlyArray<ServerProvider>,
  options: {
    readonly withGate?: boolean;
    /** A gate of the test's own in place of the real one. */
    readonly gate?: Layer.Layer<SlotGate.SlotGate>;
  } = {},
) => {
  const taskOf = (task: World["tasks"][number]) => ({
    id: task.id,
    threadId: parentThreadId,
    origin: "app_owned",
    childThreadId: task.childThreadId,
    providerInstanceId: MAIN,
    model: MODEL,
    status: "running",
    result: null,
  });
  const dependencies = Layer.mergeAll(
    NodeServices.layer,
    NodeCrypto.layer,
    Layer.mock(ThreadManagementService.ThreadManagementService)({
      hasReceipt: (commandId) => Effect.succeed(world.accepted.has(commandId)),
      getThreadShell: (threadId) =>
        Effect.succeed({
          id: threadId,
          modelSelection: { instanceId: MAIN, model: MODEL },
        } as unknown as OrchestrationV2ThreadShell),
      getThreadRecords: (threadId) =>
        Effect.succeed(
          threadId === parentThreadId
            ? parentProjection(world.tasks.map(taskOf))
            : childProjection(
                threadId,
                world.created.some((entry) => entry.threadId === threadId && entry.startsRun),
              ),
        ),
      isProviderInstanceEnabledForProject: () => Effect.succeed(true),
      assertProviderInstanceEnabled: () => Effect.void,
      dispatch: (command) =>
        Effect.gen(function* () {
          // Lets concurrent calls interleave between the gate and the dispatch.
          yield* Effect.yieldNow;
          // create_threads: the thread (with its run when it has a prompt), then its record.
          if (command.type === "thread.create") {
            const startsRun = command.initialMessage !== undefined;
            world.created.push({ threadId: command.threadId, startsRun });
            world.accepted.add(command.commandId);
            world.afterCreate?.();
            return {
              sequence: world.created.length,
              storedEvents: startsRun ? [{ event: { type: "run.created" } }] : [],
            } as never;
          }
          if (command.type === "thread.created.record") return { sequence: 0, storedEvents: [] };
          yield* Deferred.succeed(world.dispatchBegun, undefined);
          const delayMs = world.dispatchDelaysMs.shift();
          if (delayMs !== undefined) yield* Effect.sleep(delayMs);
          if (world.noTaskEvents > 0) {
            world.noTaskEvents -= 1;
            return { sequence: 0, storedEvents: [] } as never;
          }
          if (world.failDispatches > 0) {
            world.failDispatches -= 1;
            return yield* new ThreadManagementService.ThreadManagementProviderInstanceDisabledError(
              {
                projectId,
                instanceId: MAIN,
              },
            );
          }
          const n = world.tasks.length + 1;
          const task = {
            id: NodeId.make(`node:slot-gate-task-${n}`),
            childThreadId: ThreadId.make(`thread:slot-gate-child-${n}`),
          };
          world.tasks.push(task);
          world.dispatched.push({
            instanceId: (command as { modelSelection: { instanceId: string } }).modelSelection
              .instanceId,
          });
          world.accepted.add(command.commandId);
          return {
            sequence: n,
            storedEvents: [{ event: { type: "subagent.updated", payload: taskOf(task) } }],
          } as never;
        }),
    }),
    Layer.mock(ProviderRegistry.ProviderRegistry)({
      getProviders: Effect.succeed(providers),
      refreshInstance: () => Effect.succeed(providers),
    }),
    Layer.succeed(
      ProviderAdapterRegistry.ProviderAdapterRegistryV2,
      ProviderAdapterRegistry.ProviderAdapterRegistryV2.of({
        list: () => Effect.succeed(providers.map((entry) => entry.instanceId)),
        get: (instanceId) =>
          Effect.succeed({ instanceId } as unknown as ProviderAdapter.ProviderAdapterV2["Service"]),
      }),
    ),
    Layer.mock(HostResources.HostResources)({
      read: Effect.gen(function* () {
        yield* Effect.yieldNow;
        world.memoryReads += 1;
        return {
          sampledAt: 0,
          cpuUtilization: null,
          cpuCount: 4,
          availableMemoryBytes: world.freeMemoryBytes,
          totalMemoryBytes: 32_000 * MB,
        };
      }),
    }),
    Layer.mock(UsagePacer.UsagePacer)({
      paceOf: (instanceId) =>
        Effect.succeed(
          UsagePacer.paceInstance({
            instanceId,
            provider: providers.find((entry) => entry.instanceId === instanceId),
            history: [],
            pacing: UsagePacer.DEFAULT_PACING,
            nowMs: NOW_MS,
            staleAfterMs: 30 * MINUTE,
          }),
        ),
    }),
    Layer.mock(ActiveRunShells.ActiveRunShells)({
      read: Effect.gen(function* () {
        world.snapshotReads += 1;
        const hangs = world.snapshotHangs;
        if (hangs !== undefined) {
          yield* Deferred.succeed(hangs, undefined);
          return yield* Effect.never;
        }
        yield* Effect.yieldNow;
        if (world.snapshotFails) {
          return yield* new OrchestratorV2.OrchestratorProjectionError({
            threadId: parentThreadId,
          });
        }
        return {
          schemaVersion: 1,
          snapshotSequence: 1,
          threads: world.running.map(
            (thread) =>
              ({
                id: ThreadId.make(thread.id),
                providerInstanceId: thread.instanceId,
                activeRunId: RunId.make(`run:${thread.id}`),
              }) as unknown as OrchestrationV2ThreadShell,
          ),
          archivedThreads: [],
        };
      }),
    }),
    Layer.mock(ProjectService.ProjectService)({ getById: () => Effect.succeedNone }),
    Layer.mock(SecretRequests.SecretRequests)({}),
    Layer.mock(RemoteDelegation.RemoteDelegation)({}),
    Layer.mock(ScheduledTaskService.ScheduledTaskService)({}),
    Layer.mock(ThreadLaunchService.ThreadLaunchService)({}),
    Layer.mock(GitVcsDriver.GitVcsDriver)({}),
    ServerSettings.layerTest(settings),
  );
  const withGate =
    options.gate !== undefined
      ? Layer.merge(dependencies, options.gate)
      : options.withGate === false
        ? dependencies
        : SlotGate.layerOver.pipe(Layer.provideMerge(dependencies));
  return OrchestratorMcpService.layer.pipe(Layer.provideMerge(withGate));
};

const delegate = (
  clientRequestId: string,
  target?: OrchestratorMcpTarget,
  role?: OrchestratorMcpTaskRole,
) =>
  OrchestratorMcpService.OrchestratorMcpService.pipe(
    Effect.flatMap((service) =>
      service.delegateTask(scope, {
        task: "Review the change.",
        ...(target === undefined ? {} : { target }),
        ...(role === undefined ? {} : { role }),
        mode: "async",
        clientRequestId,
      }),
    ),
    Effect.result,
  );

/** `create_threads` from the parent: one thread per prompt (`undefined`: no prompt, no run). */
const createThreads = (clientRequestId: string, prompts: ReadonlyArray<string | undefined>) =>
  OrchestratorMcpService.OrchestratorMcpService.pipe(
    Effect.flatMap((service) =>
      service.createThreads(scope, {
        threads: prompts.map((prompt) => (prompt === undefined ? {} : { prompt })),
        clientRequestId,
      }),
    ),
    Effect.result,
  );

const failureOf = (
  result: Effect.Success<ReturnType<typeof delegate> | ReturnType<typeof createThreads>>,
) => {
  if (result._tag !== "Failure") return assert.fail("expected a failure");
  return result.failure;
};

const slots = (agentSlots: DeepPartial<ServerSettingsValue["agentSlots"]>) => ({
  agentSlots: { enabled: true, ...agentSlots },
});

describe("OrchestratorMcpService agent slot gate", () => {
  it.effect("off: neither the projection nor memory is read (T19, fixed mode)", () =>
    Effect.gen(function* () {
      for (const settings of [
        { agentSlots: { mode: "fixed" } },
        { agentSlots: { mode: "fixed", maxPerMachine: 1, minFreeMemoryMb: 1 } },
      ]) {
        const world = newWorld();
        world.running = [{ id: "busy", instanceId: MAIN }];
        world.freeMemoryBytes = 0;
        const result = yield* delegate("slot-off").pipe(
          Effect.provide(serverLayer(world, settings, roomy)),
        );
        assert.strictEqual(result._tag, "Success");
        assert.strictEqual(world.dispatched.length, 1);
        assert.strictEqual(world.snapshotReads, 0);
        assert.strictEqual(world.memoryReads, 0);
      }
    }),
  );

  it.effect("off without a mode (the default): delegates as before, never reads memory", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: "busy", instanceId: MAIN }];
      world.freeMemoryBytes = 0;
      const result = yield* delegate("slot-observe").pipe(
        Effect.provide(serverLayer(world, {}, roomy)),
      );
      // The rule only observes, off the decision path (see slots/SlotObserver.test.ts).
      assert.strictEqual(result._tag, "Success");
      assert.strictEqual(world.dispatched.length, 1);
      assert.strictEqual(world.memoryReads, 0);
    }),
  );

  it.effect("a server without the gate delegates as upstream", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: "busy", instanceId: MAIN }];
      const result = yield* delegate("slot-absent").pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy, { withGate: false })),
      );
      assert.strictEqual(result._tag, "Success");
      assert.strictEqual(world.snapshotReads, 0);
    }),
  );

  it.effect("machine, account and memory each refuse with slots_full and their numbers (T13)", () =>
    Effect.gen(function* () {
      const cases = [
        {
          settings: slots({ maxPerMachine: 2 }),
          message:
            "No free agent slot: 2 agents are running on this machine, and the limit is 2. Try again later. Or do the work yourself.",
        },
        {
          settings: slots({ maxPerAccount: { [MAIN]: 1 } }),
          message:
            "No free agent slot: 1 agent is running on account codex, and the limit is 1. Try again later. Or do the work yourself.",
        },
        {
          settings: slots({ minFreeMemoryMb: 4096 }),
          message:
            "No free agent slot: only 1024 MB of memory is free, and a new agent needs at least 4096 MB. Try again later. Or do the work yourself.",
        },
      ];
      for (const { settings, message } of cases) {
        const world = newWorld();
        world.running = [
          { id: "a", instanceId: MAIN },
          { id: "b", instanceId: SECOND },
        ];
        world.freeMemoryBytes = 1024 * MB;
        const result = yield* delegate("slot-full").pipe(
          Effect.provide(serverLayer(world, settings, roomy)),
        );
        const failure = failureOf(result);
        assert.strictEqual(failure.code, "slots_full");
        assert.strictEqual(failure.message, message);
        assert.deepStrictEqual(world.dispatched, []);
      }
    }),
  );

  it.effect(
    "a thread list that cannot be read refuses with slots_full; nothing is dispatched (#684)",
    () =>
      Effect.gen(function* () {
        const world = newWorld();
        world.snapshotFails = true;
        yield* Effect.gen(function* () {
          const failure = failureOf(yield* delegate("slot-unread"));
          assert.strictEqual(failure.code, "slots_full");
          assert.strictEqual(
            failure.message,
            "No agent was started: the slot gate could not read the thread list (the read failed " +
              "with an error), so it cannot count the running agents. Try again in a moment, or do " +
              "the work yourself.",
          );
          assert.deepStrictEqual(world.dispatched, []);
          // Runs again: once the list reads, the same request starts its child.
          world.snapshotFails = false;
          assert.strictEqual((yield* delegate("slot-unread"))._tag, "Success");
          assert.deepStrictEqual(world.dispatched, [{ instanceId: MAIN }]);
        }).pipe(Effect.provide(serverLayer(world, slots({ maxPerMachine: 2 }), roomy)));
      }),
  );

  it.effect(
    "a thread list that does not answer in time refuses with slots_full at the timeout (#684)",
    () =>
      Effect.gen(function* () {
        const world = newWorld();
        const begun = yield* Deferred.make<void>();
        world.snapshotHangs = begun;
        yield* Effect.gen(function* () {
          const slow = yield* Effect.forkChild(delegate("slot-hung"));
          yield* Deferred.await(begun);
          yield* TestClock.adjust(SlotGate.COUNT_READ_TIMEOUT);
          const failure = failureOf(yield* Fiber.join(slow));
          assert.strictEqual(failure.code, "slots_full");
          assert.strictEqual(
            failure.message,
            "No agent was started: the slot gate could not read the thread list (the read took " +
              "longer than 5 seconds), so it cannot count the running agents. Try again in a " +
              "moment, or do the work yourself.",
          );
          assert.deepStrictEqual(world.dispatched, []);
          // Runs again: the hung read was stopped, so the same request reads afresh and starts.
          world.snapshotHangs = undefined;
          assert.strictEqual((yield* delegate("slot-hung"))._tag, "Success");
          assert.deepStrictEqual(world.dispatched, [{ instanceId: MAIN }]);
          assert.strictEqual(world.snapshotReads, 2);
        }).pipe(Effect.provide(serverLayer(world, slots({ maxPerMachine: 2 }), roomy)));
      }),
  );

  it.effect(
    "the task's role reaches the slot gate as its capacity kind (fork line in upstream file)",
    () =>
      Effect.gen(function* () {
        const cases: ReadonlyArray<{ role: OrchestratorMcpTaskRole | undefined; kind: string }> = [
          { role: "review", kind: "agent-light" },
          { role: "research", kind: "agent-light" },
          { role: "implementation", kind: "agent-writer" },
          { role: undefined, kind: "agent-writer" },
        ];
        for (const entry of cases) {
          const world = newWorld();
          const kinds: Array<string | undefined> = [];
          const gate = Layer.mock(SlotGate.SlotGate)({
            acquire: (input) =>
              Effect.sync(() => {
                kinds.push(input.kind);
                return { type: "allow", token: null } as const;
              }),
            started: () => Effect.void,
            release: () => Effect.void,
          });
          const result = yield* delegate(
            `slot-role-${entry.role ?? "none"}`,
            undefined,
            entry.role,
          ).pipe(Effect.provide(serverLayer(world, {}, roomy, { gate })));
          assert.strictEqual(result._tag, "Success", entry.role);
          assert.deepStrictEqual(kinds, [entry.kind], entry.role);
        }
      }),
  );

  it.effect("below the limits the child starts", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: "a", instanceId: SECOND }];
      const result = yield* delegate("slot-room").pipe(
        Effect.provide(
          serverLayer(
            world,
            slots({ maxPerMachine: 2, maxPerAccount: { [MAIN]: 1 }, minFreeMemoryMb: 100 }),
            roomy,
          ),
        ),
      );
      assert.strictEqual(result._tag, "Success");
      assert.deepStrictEqual(world.dispatched, [{ instanceId: MAIN }]);
    }),
  );

  it.effect("the command id is the one the slot filler looks a request up by", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const result = yield* delegate("filler-item:1").pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 2 }), roomy)),
      );
      assert.strictEqual(result._tag, "Success");
      assert.isTrue(
        world.accepted.has(
          FillerDelegate.delegateCommandId(
            scope.requestNamespace,
            "filler-item:1",
            "delegate-task",
          ),
        ),
      );
    }),
  );

  it.effect("5 calls at once with 2 free slots: exactly 2 children are dispatched (T15)", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: "a", instanceId: SECOND }];
      const results = yield* Effect.all(
        Array.from({ length: 5 }, (_, index) => delegate(`slot-race-${index}`)),
        { concurrency: "unbounded" },
        // The memory floor puts a real wait (the host read) between counting and adding the entry.
      ).pipe(
        Effect.provide(
          serverLayer(world, slots({ maxPerMachine: 3, minFreeMemoryMb: 100 }), roomy),
        ),
      );
      assert.strictEqual(world.dispatched.length, 2);
      assert.strictEqual(results.filter((result) => result._tag === "Success").length, 2);
      for (const result of results.filter((entry) => entry._tag === "Failure")) {
        assert.strictEqual(failureOf(result).code, "slots_full");
      }
    }),
  );

  it.effect("runs again: a retry of an accepted delegation passes when full, unread (T16)", () =>
    Effect.gen(function* () {
      const world = newWorld();
      yield* Effect.gen(function* () {
        assert.strictEqual((yield* delegate("slot-retry"))._tag, "Success");
        // Full now: the child shows its run and another agent started.
        world.running = [
          { id: world.tasks[0]!.childThreadId, instanceId: MAIN },
          { id: "other", instanceId: MAIN },
        ];
        const reads = world.snapshotReads;
        const retry = yield* delegate("slot-retry");
        assert.strictEqual(retry._tag, "Success");
        assert.strictEqual(world.snapshotReads, reads);
        // The retry dispatches the same command again; the receipt answers it.
        assert.strictEqual(world.dispatched.length, 2);
        assert.strictEqual(failureOf(yield* delegate("slot-new")).code, "slots_full");
      }).pipe(Effect.provide(serverLayer(world, slots({ maxPerMachine: 2 }), roomy)));
    }),
  );

  it.effect("stops half-way: a failed dispatch gives its slot back at once", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.failDispatches = 1;
      yield* Effect.gen(function* () {
        const failed = yield* delegate("slot-fail-1");
        assert.strictEqual(failureOf(failed).code, "invalid_request");
        const next = yield* delegate("slot-fail-2");
        assert.strictEqual(next._tag, "Success");
        // The child of the second call holds the one slot until its run shows (start grace).
        assert.strictEqual(failureOf(yield* delegate("slot-fail-3")).code, "slots_full");
        yield* TestClock.adjust("30 seconds");
        assert.strictEqual((yield* delegate("slot-fail-4"))._tag, "Success");
      }).pipe(Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)));
    }),
  );

  it.effect("a dispatch slower than the start grace keeps its slot until it ends", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.dispatchDelaysMs.push(60_000);
      yield* Effect.gen(function* () {
        const slow = yield* Effect.forkChild(delegate("slot-slow"));
        yield* Deferred.await(world.dispatchBegun);
        yield* TestClock.adjust("31 seconds");
        // The first child is not dispatched yet, but it holds the one slot.
        assert.strictEqual(world.dispatched.length, 0);
        assert.strictEqual(failureOf(yield* delegate("slot-slow-2")).code, "slots_full");
        yield* TestClock.adjust("29 seconds");
        assert.strictEqual((yield* Fiber.join(slow))._tag, "Success");
      }).pipe(
        Effect.provide(
          serverLayer(world, slots({ maxPerMachine: 1, startGraceSeconds: 30 }), roomy),
        ),
      );
    }),
  );

  it.effect(
    "stops half-way: a call cancelled during the dispatch still starts its child, which keeps the slot",
    () =>
      Effect.gen(function* () {
        const world = newWorld();
        world.dispatchDelaysMs.push(60_000);
        yield* Effect.gen(function* () {
          const cancelled = yield* Effect.forkChild(delegate("slot-cancel"));
          yield* Deferred.await(world.dispatchBegun);
          yield* TestClock.adjust("1 second");
          assert.strictEqual(failureOf(yield* delegate("slot-cancel-2")).code, "slots_full");
          // Upstream finishes a dispatch whose caller went away, so the cancel waits for it.
          const cancelling = yield* Effect.forkChild(Fiber.interrupt(cancelled));
          yield* TestClock.adjust("59 seconds");
          yield* Fiber.join(cancelling);
          assert.strictEqual(world.dispatched.length, 1);
          // Its child holds the one slot until its run shows (start grace), then gives it back.
          assert.strictEqual(failureOf(yield* delegate("slot-cancel-3")).code, "slots_full");
          yield* TestClock.adjust("30 seconds");
          assert.strictEqual((yield* delegate("slot-cancel-4"))._tag, "Success");
        }).pipe(Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)));
      }),
  );

  it.effect("stops half-way: a dispatch without a task event gives its slot back", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.noTaskEvents = 1;
      yield* Effect.gen(function* () {
        assert.strictEqual(failureOf(yield* delegate("slot-no-task")).code, "orchestration_error");
        assert.strictEqual((yield* delegate("slot-no-task-2"))._tag, "Success");
      }).pipe(Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)));
    }),
  );

  it.effect("after the usage pacing gate: a hold wins and reads no slots", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: "a", instanceId: MAIN }];
      const result = yield* delegate("slot-hold", { providerInstanceId: MAIN }).pipe(
        Effect.provide(
          serverLayer(world, { usagePacingGate: "enforce", ...slots({ maxPerMachine: 1 }) }, [
            provider(MAIN, 60),
            provider(SECOND, 10),
          ]),
        ),
      );
      assert.strictEqual(failureOf(result).code, "usage_pacing_hold");
      assert.strictEqual(world.snapshotReads, 0);
    }),
  );

  it.effect("the account limit counts the final target, after the pacer moved the task", () =>
    Effect.gen(function* () {
      const providers = [provider(MAIN, 60), provider(SECOND, 10)];
      const enforce = { usagePacingGate: "enforce" } as const;
      // MAIN is full, but the pacer moves the task to SECOND, which has room.
      const moved = newWorld();
      moved.running = [{ id: "a", instanceId: MAIN }];
      const allowed = yield* delegate("slot-moved").pipe(
        Effect.provide(
          serverLayer(moved, { ...enforce, ...slots({ maxPerAccount: { [MAIN]: 1 } }) }, providers),
        ),
      );
      assert.strictEqual(allowed._tag, "Success");
      assert.deepStrictEqual(moved.dispatched, [{ instanceId: SECOND }]);
      // SECOND is full: the moved task is refused on SECOND's limit.
      const full = newWorld();
      full.running = [{ id: "b", instanceId: SECOND }];
      const refused = yield* delegate("slot-moved-full").pipe(
        Effect.provide(
          serverLayer(
            full,
            { ...enforce, ...slots({ maxPerAccount: { [SECOND]: 1 } }) },
            providers,
          ),
        ),
      );
      const failure = failureOf(refused);
      assert.strictEqual(failure.code, "slots_full");
      assert.include(failure.message, "on account codex-second");
    }),
  );

  it.effect("the caller is not counted", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: parentThreadId, instanceId: MAIN }];
      const result = yield* delegate("slot-caller").pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)),
      );
      assert.strictEqual(result._tag, "Success");
    }),
  );
});

/**
 * Fork: `create_threads` takes the same slot as `delegate_task`, with the same count and
 * refusal. On main's code it created every thread whatever the count.
 */
describe("create_threads through the agent slot gate", () => {
  it.effect("full: refused with slots_full, and no thread is created", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: "busy", instanceId: MAIN }];
      const result = yield* createThreads("create-full", ["Work on it."]).pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)),
      );
      const failure = failureOf(result);
      assert.strictEqual(failure.code, "slots_full");
      assert.strictEqual(
        failure.message,
        "No thread was created. No free agent slot: 1 agent is running on this machine, and the limit is 1. Try again later. Or do the work yourself.",
      );
      assert.deepStrictEqual(world.created, []);
      assert.strictEqual(world.accepted.size, 0);
    }),
  );

  it.effect("free: the thread is created and started; the caller is not counted", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: parentThreadId, instanceId: MAIN }];
      const result = yield* createThreads("create-free", ["Work on it."]).pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)),
      );
      assert.strictEqual(result._tag, "Success");
      assert.deepStrictEqual(
        world.created.map((entry) => entry.startsRun),
        [true],
      );
    }),
  );

  it.effect("a thread without a prompt starts no agent: created even when full", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: "busy", instanceId: MAIN }];
      const result = yield* createThreads("create-idle", [undefined]).pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)),
      );
      assert.strictEqual(result._tag, "Success");
      assert.deepStrictEqual(
        world.created.map((entry) => entry.startsRun),
        [false],
      );
      assert.strictEqual(world.snapshotReads, 0);
    }),
  );

  it.effect("the whole batch is checked first: 2 prompts, 1 free slot, none created", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const result = yield* createThreads("create-two", ["First.", "Second."]).pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)),
      );
      const failure = failureOf(result);
      assert.strictEqual(failure.code, "slots_full");
      assert.isTrue(
        failure.message.startsWith(
          "No thread was created: the 2 threads with a prompt need 2 free agent slots, and 1 is free.",
        ),
        failure.message,
      );
      assert.deepStrictEqual(world.created, []);
      assert.strictEqual(world.accepted.size, 0);
    }),
  );

  it.effect("a batch that starts with a thread without a prompt is checked first too", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: "busy", instanceId: MAIN }];
      const result = yield* createThreads("create-idle-first", [undefined, "Work on it."]).pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)),
      );
      assert.strictEqual(failureOf(result).code, "slots_full");
      assert.deepStrictEqual(world.created, []);
    }),
  );

  it.effect("enough slots for the batch: every thread is created and started", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const result = yield* createThreads("create-batch", [undefined, "First.", "Second."]).pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 2 }), roomy)),
      );
      assert.strictEqual(result._tag, "Success");
      assert.deepStrictEqual(
        world.created.map((entry) => entry.startsRun),
        [false, true, true],
      );
    }),
  );

  it.effect("stops half-way: a slot taken mid-batch names the threads already created", () =>
    Effect.gen(function* () {
      const world = newWorld();
      // Another caller starts an agent once the first thread exists.
      world.afterCreate = () => {
        world.running = [{ id: "other", instanceId: MAIN }];
      };
      const result = yield* createThreads("create-race", ["First.", "Second."]).pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 2 }), roomy)),
      );
      const failure = failureOf(result);
      assert.strictEqual(failure.code, "slots_full");
      assert.strictEqual(world.created.length, 1);
      assert.isTrue(
        failure.message.startsWith(
          `Thread 2 was not created. Created before it in this call: ${world.created[0]!.threadId}.`,
        ),
        failure.message,
      );
    }),
  );

  it.effect("runs again after a stop: the created thread is kept, the rest are created", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.afterCreate = () => {
        world.running = [{ id: "other", instanceId: MAIN }];
      };
      yield* Effect.gen(function* () {
        assert.strictEqual(
          failureOf(yield* createThreads("create-resume", ["First.", "Second."])).code,
          "slots_full",
        );
        world.afterCreate = undefined;
        world.running = [];
        // The first thread's run counts (its grace and its run); one slot is left, for the second.
        assert.strictEqual(
          (yield* createThreads("create-resume", ["First.", "Second."]))._tag,
          "Success",
        );
        // The first thread's command is sent again (the store keeps one); two threads in all.
        assert.strictEqual(new Set(world.created.map((entry) => entry.threadId)).size, 2);
        assert.isTrue(world.created.every((entry) => entry.startsRun));
      }).pipe(Effect.provide(serverLayer(world, slots({ maxPerMachine: 2 }), roomy)));
    }),
  );

  it.effect("runs again: a created thread whose run never started is counted again", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: "busy", instanceId: MAIN }];
      // An older call created the thread, then stopped before its run (receipt, no run).
      const created = yield* Effect.gen(function* () {
        world.running = [];
        assert.strictEqual((yield* createThreads("create-norun", [undefined]))._tag, "Success");
        return world.created[0]!.threadId;
      }).pipe(Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)));
      assert.isDefined(created);
      world.running = [{ id: "busy", instanceId: MAIN }];
      const result = yield* createThreads("create-norun", ["Work on it."]).pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)),
      );
      assert.strictEqual(failureOf(result).code, "slots_full");
    }),
  );

  it.effect("runs again with the same key: refused, then created once a slot is free", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.running = [{ id: "busy", instanceId: MAIN }];
      yield* Effect.gen(function* () {
        assert.strictEqual(
          failureOf(yield* createThreads("create-again", ["Work on it."])).code,
          "slots_full",
        );
        world.running = [];
        assert.strictEqual((yield* createThreads("create-again", ["Work on it."]))._tag, "Success");
        assert.strictEqual(world.created.length, 1);
        // Full again: a retry of the accepted creation starts nothing new, so it passes unread.
        world.running = [{ id: "busy", instanceId: MAIN }];
        const reads = world.snapshotReads;
        assert.strictEqual((yield* createThreads("create-again", ["Work on it."]))._tag, "Success");
        assert.strictEqual(world.snapshotReads, reads);
      }).pipe(Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)));
    }),
  );

  it.effect("a thread list that cannot be read refuses with slots_full (#684)", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.snapshotFails = true;
      const result = yield* createThreads("create-unread", ["Work on it."]).pipe(
        Effect.provide(serverLayer(world, slots({ maxPerMachine: 2 }), roomy)),
      );
      assert.strictEqual(failureOf(result).code, "slots_full");
      assert.deepStrictEqual(world.created, []);
    }),
  );

  it.effect(
    "stops half-way: a created thread whose run never shows frees its slot after the grace",
    () =>
      Effect.gen(function* () {
        const world = newWorld();
        yield* Effect.gen(function* () {
          assert.strictEqual((yield* createThreads("create-grace-1", ["Work."]))._tag, "Success");
          // Its run does not show in the count: it holds the slot for the start grace only.
          assert.strictEqual(
            failureOf(yield* createThreads("create-grace-2", ["Work."])).code,
            "slots_full",
          );
          yield* TestClock.adjust("30 seconds");
          assert.strictEqual((yield* createThreads("create-grace-3", ["Work."]))._tag, "Success");
        }).pipe(Effect.provide(serverLayer(world, slots({ maxPerMachine: 1 }), roomy)));
      }),
  );

  it.effect("off or without a gate: created as upstream, nothing read", () =>
    Effect.gen(function* () {
      for (const options of [{ withGate: true }, { withGate: false }]) {
        const world = newWorld();
        world.running = [{ id: "busy", instanceId: MAIN }];
        // Off (fixed mode, as in T19 above), or on with a limit but no gate on the server.
        const settings = options.withGate
          ? { agentSlots: { mode: "fixed" as const, maxPerMachine: 1 } }
          : slots({ maxPerMachine: 1 });
        const result = yield* createThreads("create-off", ["Work on it."]).pipe(
          Effect.provide(serverLayer(world, settings, roomy, options)),
        );
        assert.strictEqual(result._tag, "Success");
        assert.strictEqual(world.snapshotReads, 0);
      }
    }),
  );
});
