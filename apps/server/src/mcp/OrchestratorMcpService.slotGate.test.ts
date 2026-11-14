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
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { TestClock } from "effect/testing";

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

const childProjection = (childThreadId: ThreadId) =>
  ({
    thread: { id: childThreadId, title: "Child" },
    runs: [],
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
  memoryReads: number;
  freeMemoryBytes: number;
  /** The next dispatches fail (a rejected command). */
  failDispatches: number;
  /** Requests whose delegation was accepted before (their receipt exists). */
  readonly accepted: Set<string>;
}

const newWorld = (): World => ({
  running: [],
  tasks: [],
  dispatched: [],
  snapshotReads: 0,
  memoryReads: 0,
  freeMemoryBytes: 16_000 * MB,
  failDispatches: 0,
  accepted: new Set(),
});

/** The server's layers for one test: the gate and /mcp share one settings service and world. */
const serverLayer = (
  world: World,
  settings: DeepPartial<ServerSettingsValue>,
  providers: ReadonlyArray<ServerProvider>,
  options: { readonly withGate?: boolean } = {},
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
      getThreadRecords: (threadId) =>
        Effect.succeed(
          threadId === parentThreadId
            ? parentProjection(world.tasks.map(taskOf))
            : childProjection(threadId),
        ),
      isProviderInstanceEnabledForProject: () => Effect.succeed(true),
      assertProviderInstanceEnabled: () => Effect.void,
      getShellSnapshot: () =>
        Effect.gen(function* () {
          world.snapshotReads += 1;
          yield* Effect.yieldNow;
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
      dispatch: (command) =>
        Effect.gen(function* () {
          // Lets concurrent calls interleave between the gate and the dispatch.
          yield* Effect.yieldNow;
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
    Layer.mock(ProjectService.ProjectService)({}),
    Layer.mock(SecretRequests.SecretRequests)({}),
    Layer.mock(RemoteDelegation.RemoteDelegation)({}),
    Layer.mock(ScheduledTaskService.ScheduledTaskService)({}),
    Layer.mock(ThreadLaunchService.ThreadLaunchService)({}),
    Layer.mock(GitVcsDriver.GitVcsDriver)({}),
    ServerSettings.layerTest(settings),
  );
  const withGate =
    options.withGate === false
      ? dependencies
      : SlotGate.layer.pipe(Layer.provideMerge(dependencies));
  return OrchestratorMcpService.layer.pipe(Layer.provideMerge(withGate));
};

const delegate = (clientRequestId: string, target?: OrchestratorMcpTarget) =>
  OrchestratorMcpService.OrchestratorMcpService.pipe(
    Effect.flatMap((service) =>
      service.delegateTask(scope, {
        task: "Review the change.",
        ...(target === undefined ? {} : { target }),
        mode: "async",
        clientRequestId,
      }),
    ),
    Effect.result,
  );

const failureOf = (result: Effect.Success<ReturnType<typeof delegate>>) => {
  if (result._tag !== "Failure") return assert.fail("expected a failure");
  return result.failure;
};

const slots = (agentSlots: DeepPartial<ServerSettingsValue["agentSlots"]>) => ({
  agentSlots: { enabled: true, ...agentSlots },
});

describe("OrchestratorMcpService agent slot gate", () => {
  it.effect("off (the default): neither the projection nor memory is read (T19)", () =>
    Effect.gen(function* () {
      for (const settings of [{}, { agentSlots: { maxPerMachine: 1, minFreeMemoryMb: 1 } }]) {
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
