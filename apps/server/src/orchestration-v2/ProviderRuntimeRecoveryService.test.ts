import { assert, describe, it, vi } from "@effect/vitest";
import {
  MessageId,
  NodeId,
  ProviderDriverKind,
  ProviderInstanceId,
  ProviderSessionId,
  ProviderThreadId,
  ProviderTurnId,
  RunAttemptId,
  RunId,
  RuntimeRequestId,
  ThreadId,
  TurnItemId,
  type OrchestrationV2ThreadProjection,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as TestClock from "effect/testing/TestClock";

import * as EffectWorker from "./EffectWorker.ts";
import * as EffectOutbox from "./EffectOutbox.ts";
import * as EventSink from "./EventSink.ts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import * as ProjectionStore from "./ProjectionStore.ts";
import * as ProviderRuntimeRecovery from "./ProviderRuntimeRecoveryService.ts";
import * as ServerSettings from "../serverSettings.ts";
import {
  makeWorkAdmissionGate,
  memoryUpdateHoldStore,
  UPDATE_HOLD_MAX_AGE_MS,
  type UpdateHold,
  UpdateHoldMarkerError,
  type UpdateHoldStore,
  WorkAdmission,
} from "../workAdmission.ts";

it.effect("expires orphaned runtime requests before command readiness", () => {
  const threadId = ThreadId.make("thread_recovery_requests");
  let committedInput: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0] | null =
    null;
  const committed = vi.fn(
    (input: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0]) => {
      committedInput = input;
      return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
    },
  );
  const projection = {
    thread: { id: threadId },
    runtimeRequests: [
      {
        id: RuntimeRequestId.make("request_orphaned"),
        nodeId: NodeId.make("node_orphaned"),
        status: "pending",
        responseCapability: { type: "not_resumable", reason: "old process" },
      },
    ],
    providerSessions: [],
    providerThreads: [],
    runs: [],
    nodes: [],
  } as unknown as OrchestrationV2ThreadProjection;
  const layer = ProviderRuntimeRecovery.layer.pipe(
    Layer.provide(ServerSettings.layerTest()),
    Layer.provide(
      Layer.mergeAll(
        Layer.mock(ProjectionStore.ProjectionStoreV2)({
          getRecoveryThreadIds: () => Effect.succeed([threadId]),
          getRuntimeRecoveryProjection: () => Effect.succeed(projection),
        }),
        Layer.mock(EventSink.EventSinkV2)({ commitCommand: committed }),
        IdAllocator.layer,
        Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
          runRecoveryOnce: Effect.succeed(false),
        }),
        Layer.mock(EffectOutbox.EffectOutboxV2)({
          reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
        }),
      ),
    ),
  );
  return Effect.gen(function* () {
    yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService).recover;
    const command = committedInput;
    assert.isNotNull(command);
    if (command === null) return;
    assert.equal(command?.events[0]?.type, "runtime-request.updated");
    if (command?.events[0]?.type === "runtime-request.updated") {
      assert.equal(command.events[0].payload.status, "expired");
      assert.equal(command.events[0].payload.responseCapability.type, "not_resumable");
    }
  }).pipe(Effect.provide(layer));
});

it.effect("preserves async questions across startup and shutdown", () => {
  const threadId = ThreadId.make("async-recovery-thread");
  const nodeId = NodeId.make("async-recovery-node");
  const projection = {
    thread: { id: threadId },
    runtimeRequests: [
      {
        id: RuntimeRequestId.make("async-recovery-request"),
        nodeId,
        status: "pending",
        responseCapability: { type: "message" },
      },
    ],
    providerSessions: [],
    providerThreads: [],
    runs: [],
    nodes: [],
    turnItems: [],
  } as unknown as OrchestrationV2ThreadProjection;
  const commitCommand = vi.fn(() => Effect.die("an async question needs no process-loss write"));
  const layer = ProviderRuntimeRecovery.layer.pipe(
    Layer.provide(ServerSettings.layerTest()),
    Layer.provide(
      Layer.mergeAll(
        Layer.mock(ProjectionStore.ProjectionStoreV2)({
          getRecoveryThreadIds: () => Effect.succeed([threadId]),
          getRuntimeRecoveryProjection: () => Effect.succeed(projection),
        }),
        Layer.mock(EventSink.EventSinkV2)({ commitCommand }),
        IdAllocator.layer,
        Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
          runRecoveryOnce: Effect.succeed(false),
        }),
        Layer.mock(EffectOutbox.EffectOutboxV2)({
          reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
          cancelUnsettled: () => Effect.succeed([]),
          signalCancellations: () => Effect.void,
        }),
      ),
    ),
  );
  return Effect.gen(function* () {
    const recovery = yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService;
    assert.equal((yield* recovery.reconcile("startup")).closedRequests, 0);
    assert.equal((yield* recovery.reconcile("startup")).closedRequests, 0);
    assert.isFalse(commitCommand.mock.calls.length > 0);
  }).pipe(Effect.provide(layer));
});

it.effect(
  "preserves a replayable waiting run while cancelling its process-bound background work",
  () => {
    const threadId = ThreadId.make("thread_waiting_checkpoint");
    const runId = RunId.make("run_waiting_checkpoint");
    const providerThreadId = ProviderThreadId.make("provider_thread_waiting_checkpoint");
    const providerInstanceId = ProviderInstanceId.make("claude");
    const backgroundItemId = TurnItemId.make("turn_item_waiting_checkpoint");
    let committedInput: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0] | null =
      null;
    const projection = {
      thread: { id: threadId },
      runtimeRequests: [],
      providerSessions: [],
      providerThreads: [
        {
          id: providerThreadId,
          driver: ProviderDriverKind.make("claude"),
          providerInstanceId,
          status: "idle",
          pendingBackgroundTasks: [
            { taskId: String(backgroundItemId), description: "Finish background task" },
          ],
        },
      ],
      runs: [{ id: runId, status: "waiting", providerInstanceId, providerThreadId }],
      attempts: [],
      nodes: [],
      subagents: [],
      messages: [],
      turnItems: [
        {
          id: backgroundItemId,
          runId,
          nodeId: null,
          providerThreadId,
          type: "command_execution",
          status: "running",
        },
      ],
    } as unknown as OrchestrationV2ThreadProjection;
    const layer = ProviderRuntimeRecovery.layer.pipe(
      Layer.provide(ServerSettings.layerTest()),
      Layer.provide(
        Layer.mergeAll(
          Layer.mock(ProjectionStore.ProjectionStoreV2)({
            getRecoveryThreadIds: () => Effect.succeed([threadId]),
            getRuntimeRecoveryProjection: () => Effect.succeed(projection),
          }),
          Layer.mock(EventSink.EventSinkV2)({
            commitCommand: (input) => {
              committedInput = input;
              return Effect.succeed({
                committed: true,
                cancelledEffectCount: 0,
              } as never);
            },
          }),
          IdAllocator.layer,
          Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
            runRecoveryOnce: Effect.succeed(false),
          }),
          Layer.mock(EffectOutbox.EffectOutboxV2)({
            listByCommandId: () =>
              Effect.succeed([
                {
                  request: { type: "checkpoint.capture", runId },
                  status: "running",
                },
              ] as never),
            reconcileAfterProcessLoss: Effect.succeed({ requeued: 1, cancelled: 0 }),
          }),
        ),
      ),
    );

    return Effect.gen(function* () {
      const summary =
        yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService).reconcile("startup");
      assert.equal(summary.terminalizedRuns, 0);
      assert.equal(summary.requeuedEffects, 1);

      const command = committedInput;
      assert.isNotNull(command);
      if (command === null) return;

      assert.isFalse(command.events.some((event) => event.type === "run.updated"));
      const recorded = command.events.flatMap((event) =>
        event.type === "run.background-work-cancelled" ? [event.payload] : [],
      );

      assert.deepEqual(
        recorded.map((entry) => ({
          runId: entry.runId,
          kinds: entry.restartCancelledBackgroundWork.map((work) => work.kind),
        })),
        [{ runId, kinds: ["shell"] }],
      );
      assert.isTrue(
        command.events.some(
          (event) =>
            event.type === "turn-item.updated" &&
            event.payload.id === backgroundItemId &&
            event.payload.status === "cancelled",
        ),
      );
      assert.isTrue(
        command.events.some(
          (event) =>
            event.type === "provider-thread.updated" &&
            event.payload.id === providerThreadId &&
            event.payload.pendingBackgroundTasks?.length === 0,
        ),
      );
    }).pipe(Effect.provide(layer));
  },
);

it.effect(
  "records cancelled work on the provider thread that lost it after a provider switch",
  () => {
    const threadId = ThreadId.make("thread_switch_note");
    const claudeRunId = RunId.make("run_switch_note_claude");
    const codexRunId = RunId.make("run_switch_note_codex");
    const claudeThreadId = ProviderThreadId.make("provider_thread_switch_note_claude");
    const codexThreadId = ProviderThreadId.make("provider_thread_switch_note_codex");
    const claude = ProviderInstanceId.make("claude");
    const codex = ProviderInstanceId.make("codex");
    let committedInput: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0] | null =
      null;

    const projection = {
      thread: { id: threadId },
      runtimeRequests: [],
      providerSessions: [],
      providerThreads: [
        {
          id: claudeThreadId,
          ownerNodeId: null,
          driver: ProviderDriverKind.make("claudeAgent"),
          providerInstanceId: claude,
          status: "idle",
          pendingBackgroundTasks: [{ taskId: "task-claude", description: "Watch the build" }],
        },
        {
          id: codexThreadId,
          ownerNodeId: null,
          driver: ProviderDriverKind.make("codex"),
          providerInstanceId: codex,
          status: "idle",
        },
      ],
      providerTurns: [],
      runs: [
        {
          id: claudeRunId,
          ordinal: 1,
          status: "completed",
          providerInstanceId: claude,
          providerThreadId: claudeThreadId,
        },
        {
          id: codexRunId,
          ordinal: 2,
          status: "completed",
          providerInstanceId: codex,
          providerThreadId: codexThreadId,
        },
      ],
      attempts: [],
      nodes: [],
      subagents: [],
      messages: [],
      turnItems: [
        {
          id: TurnItemId.make("turn_item_switch_note_subagent"),
          runId: claudeRunId,
          nodeId: null,
          providerThreadId: claudeThreadId,
          nativeItemRef: null,
          type: "subagent",
          title: "Background subagent",
          status: "running",
        },
      ],
    } as unknown as OrchestrationV2ThreadProjection;
    const layer = ProviderRuntimeRecovery.layer.pipe(
      Layer.provide(ServerSettings.layerTest()),
      Layer.provide(
        Layer.mergeAll(
          Layer.mock(ProjectionStore.ProjectionStoreV2)({
            getRecoveryThreadIds: () => Effect.succeed([threadId]),
            getRuntimeRecoveryProjection: () => Effect.succeed(projection),
          }),
          Layer.mock(EventSink.EventSinkV2)({
            commitCommand: (input) => {
              committedInput = input;
              return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
            },
          }),
          IdAllocator.layer,
          Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
            runRecoveryOnce: Effect.succeed(false),
          }),
          Layer.mock(EffectOutbox.EffectOutboxV2)({
            reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
          }),
        ),
      ),
    );

    return Effect.gen(function* () {
      yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService).reconcile("startup");
      const recorded = (committedInput?.events ?? []).flatMap((event) =>
        event.type === "run.background-work-cancelled" ? [event.payload] : [],
      );

      assert.deepEqual(
        recorded.map((entry) => ({
          runId: entry.runId,
          kinds: entry.restartCancelledBackgroundWork.map((work) => work.kind),
        })),
        [{ runId: claudeRunId, kinds: ["subagent", "task"] }],
      );
    }).pipe(Effect.provide(layer));
  },
);

it.effect("cancels a stale waiting run when no checkpoint capture can finish it", () => {
  const threadId = ThreadId.make("thread_stale_waiting");
  const runId = RunId.make("run_stale_waiting");
  let committedInput: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0] | null =
    null;
  const projection = {
    thread: { id: threadId },
    runtimeRequests: [],
    providerSessions: [],
    providerThreads: [],
    providerTurns: [],
    runs: [
      {
        id: runId,
        status: "waiting",
        providerInstanceId: ProviderInstanceId.make("codex"),
      },
    ],
    attempts: [],
    nodes: [],
    subagents: [],
    messages: [],
    turnItems: [],
  } as unknown as OrchestrationV2ThreadProjection;
  const layer = ProviderRuntimeRecovery.layer.pipe(
    Layer.provide(ServerSettings.layerTest()),
    Layer.provide(
      Layer.mergeAll(
        Layer.mock(ProjectionStore.ProjectionStoreV2)({
          getRecoveryThreadIds: () => Effect.succeed([threadId]),
          getRuntimeRecoveryProjection: () => Effect.succeed(projection),
        }),
        Layer.mock(EventSink.EventSinkV2)({
          commitCommand: (input) => {
            committedInput = input;
            return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
          },
        }),
        IdAllocator.layer,
        Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
          runRecoveryOnce: Effect.succeed(false),
        }),
        Layer.mock(EffectOutbox.EffectOutboxV2)({
          listByCommandId: () => Effect.succeed([]),
          reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
        }),
      ),
    ),
  );

  return Effect.gen(function* () {
    const summary =
      yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService).reconcile("startup");
    assert.equal(summary.terminalizedRuns, 1);
    const runEvent = committedInput?.events.find((event) => event.type === "run.updated");
    assert.equal(runEvent?.type === "run.updated" ? runEvent.payload.status : null, "cancelled");
  }).pipe(Effect.provide(layer));
});

it.effect("recovery closes a secret card's form but leaves a link card open", () => {
  const threadId = ThreadId.make("thread_secret_recovery");
  const runId = RunId.make("run_secret_recovery");
  let committedInput: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0] | null =
    null;
  const projection = {
    thread: { id: threadId },
    runtimeRequests: [],
    providerSessions: [],
    providerThreads: [],
    providerTurns: [],
    runs: [{ id: runId, status: "running", providerInstanceId: ProviderInstanceId.make("codex") }],
    attempts: [],
    nodes: [],
    subagents: [],
    messages: [],
    turnItems: [
      {
        id: "turn-item:secret-request:recovery",
        threadId,
        runId,
        nodeId: null,
        type: "secret_request",
        status: "waiting",
        secretStatus: "pending",
        label: "GitHub token",
        reason: "Used as GH_TOKEN.",
      },
      {
        id: "turn-item:link-request:recovery",
        threadId,
        runId,
        nodeId: null,
        type: "link_request",
        status: "waiting",
        linkStatus: "pending",
        url: "https://box.example.ts.net",
        reason: "Run the suite there.",
      },
    ],
  } as unknown as OrchestrationV2ThreadProjection;
  const layer = ProviderRuntimeRecovery.layer.pipe(
    Layer.provide(ServerSettings.layerTest()),
    Layer.provide(
      Layer.mergeAll(
        Layer.mock(ProjectionStore.ProjectionStoreV2)({
          getRecoveryThreadIds: () => Effect.succeed([threadId]),
          getRuntimeRecoveryProjection: () => Effect.succeed(projection),
        }),
        Layer.mock(EventSink.EventSinkV2)({
          commitCommand: (input) => {
            committedInput = input;
            return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
          },
        }),
        IdAllocator.layer,
        Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
          runRecoveryOnce: Effect.succeed(false),
        }),
        Layer.mock(EffectOutbox.EffectOutboxV2)({
          listByCommandId: () => Effect.succeed([]),
          reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
        }),
      ),
    ),
  );

  return Effect.gen(function* () {
    yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService).reconcile("startup");
    const cards = (committedInput?.events ?? []).flatMap((event) =>
      event.type === "turn-item.updated" ? [event.payload] : [],
    );
    const secret = cards.find((card) => card.type === "secret_request");
    assert.equal(secret?.status, "cancelled");
    assert.equal(secret?.type === "secret_request" ? secret.secretStatus : null, "cancelled");
    // Nothing in memory waits on a link card; the user can still answer it.
    assert.equal(
      cards.some((card) => card.type === "link_request"),
      false,
    );
  }).pipe(Effect.provide(layer));
});

it.effect.each([
  "queued",
  "preparing",
  "starting",
  "starting-other-request",
  "starting-own-request",
  "starting-scheduled",
] as const)("recovers $0 work after restart", (scenario) => {
  const status = scenario.startsWith("starting") ? "starting" : scenario;
  const terminalized =
    status === "preparing" ||
    scenario === "starting-own-request" ||
    scenario === "starting-scheduled";
  const threadId = ThreadId.make("thread_queued_restart");
  const runId = RunId.make("run_queued_restart");
  const attemptId = RunAttemptId.make("attempt_queued_restart");
  const rootNodeId = NodeId.make("node_queued_restart");
  let committedInput: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0] | null =
    null;
  const projection = {
    thread: { id: threadId },
    runtimeRequests: scenario.endsWith("request")
      ? [
          {
            id: RuntimeRequestId.make("pending-recovery-request"),
            nodeId:
              scenario === "starting-own-request" ? rootNodeId : NodeId.make("other-run-request"),
            kind: "user_input",
            status: "pending",
            responseCapability: { type: "message" },
          },
        ]
      : [],
    providerSessions: [],
    providerThreads: [],
    providerTurns: [],
    runs: [
      {
        id: runId,
        status,
        startedAt: null,
        activeAttemptId: attemptId,
        userMessageId: MessageId.make("message_queued_restart"),
        queuePosition: 1,
        providerInstanceId: ProviderInstanceId.make("codex"),
        ...(status === "preparing"
          ? {
              workspacePreparation: {
                type: "worktree",
                baseRef: "parent-local-commit",
                startFromOrigin: false,
              },
            }
          : {}),
      },
    ],
    attempts: [
      {
        id: attemptId,
        runId,
        rootNodeId,
        status: "pending",
      },
    ],
    nodes: [
      ...(scenario === "starting-other-request"
        ? [
            {
              id: NodeId.make("other-run-request"),
              runId: RunId.make("earlier-run"),
              status: "waiting",
            },
          ]
        : []),
      {
        id: rootNodeId,
        runId,
        status: "pending",
      },
    ],
    subagents: [],
    messages:
      status === "starting"
        ? [
            {
              id: MessageId.make("message_queued_restart"),
              createdBy: "user",
              ...(scenario === "starting-scheduled" ? { scheduledTaskId: "scheduled-task" } : {}),
            },
          ]
        : [],
    turnItems: [],
  } as unknown as OrchestrationV2ThreadProjection;
  const layer = ProviderRuntimeRecovery.layer.pipe(
    Layer.provide(ServerSettings.layerTest()),
    Layer.provide(
      Layer.mergeAll(
        Layer.mock(ProjectionStore.ProjectionStoreV2)({
          getRecoveryThreadIds: () => Effect.succeed([threadId]),
          getRuntimeRecoveryProjection: () => Effect.succeed(projection),
          getMessagesById: (requestedThreadId, messageIds) =>
            Effect.succeed(
              new Map(
                projection.messages
                  .filter(
                    (message) => requestedThreadId === threadId && messageIds.includes(message.id),
                  )
                  .map((message) => [message.id, message]),
              ),
            ),
          getThreadRecords: () => Effect.succeed(projection),
        }),
        Layer.mock(EventSink.EventSinkV2)({
          commitCommand: (input) => {
            committedInput = input;
            return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
          },
        }),
        IdAllocator.layer,
        Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
          runRecoveryOnce: Effect.succeed(false),
        }),
        Layer.mock(EffectOutbox.EffectOutboxV2)({
          reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
        }),
      ),
    ),
  );

  return Effect.gen(function* () {
    const summary =
      yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService).reconcile("startup");
    assert.equal(summary.terminalizedRuns, terminalized ? 1 : 0);
    const command = committedInput;
    assert.isNotNull(command);
    if (command === null) return;
    const runEvent = command.events.find((event) => event.type === "run.updated");
    const attemptEvent = command.events.find((event) => event.type === "run-attempt.updated");
    const nodeEvent = command.events.find((event) => event.type === "node.updated");
    assert.equal(
      runEvent?.type === "run.updated" ? runEvent.payload.status : null,
      terminalized ? "cancelled" : "queued",
    );
    assert.isEmpty(command.effects);
    if (terminalized) {
      assert.equal(
        attemptEvent?.type === "run-attempt.updated" ? attemptEvent.payload.status : null,
        "cancelled",
      );
      assert.equal(
        nodeEvent?.type === "node.updated" ? nodeEvent.payload.status : null,
        scenario === "starting-own-request" ? null : "cancelled",
      );
    } else {
      assert.equal(runEvent?.type === "run.updated" ? runEvent.payload.queuePosition : null, 1);
      assert.equal(runEvent?.type === "run.updated" ? runEvent.payload.queueHeld : false, true);
      assert.isUndefined(attemptEvent);
      assert.isUndefined(nodeEvent);
    }
  }).pipe(Effect.provide(layer));
});

it.effect(
  "cancels the complete in-flight subtree and stops its persisted session without reopening it",
  () => {
    const threadId = ThreadId.make("thread_recovery_cancel");
    const runId = RunId.make("run_recovery_cancel");
    const attemptId = RunAttemptId.make("attempt_recovery_cancel");
    const rootNodeId = NodeId.make("node_recovery_cancel");
    const providerThreadId = ProviderThreadId.make("provider_thread_recovery_cancel");
    const providerTurnId = ProviderTurnId.make("provider_turn_recovery_cancel");
    const providerSessionId = ProviderSessionId.make("provider_session_recovery_cancel");
    let committedInput: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0] | null =
      null;
    const projection = {
      thread: { id: threadId },
      runtimeRequests: [],
      providerSessions: [
        {
          id: providerSessionId,
          driver: ProviderDriverKind.make("codex"),
          providerInstanceId: ProviderInstanceId.make("codex"),
          status: "ready",
          extensionStatuses: [{ key: "watch", text: "Watching" }],
        },
      ],
      providerThreads: [
        {
          id: providerThreadId,
          driver: ProviderDriverKind.make("codex"),
          providerInstanceId: ProviderInstanceId.make("codex"),
          status: "active",
        },
      ],
      providerTurns: [
        {
          id: providerTurnId,
          runAttemptId: attemptId,
          nodeId: rootNodeId,
          status: "running",
        },
      ],
      runs: [
        {
          id: runId,
          status: "starting",
          providerThreadId,
          providerInstanceId: ProviderInstanceId.make("codex"),
        },
      ],
      attempts: [
        {
          id: attemptId,
          runId,
          rootNodeId,
          status: "running",
        },
      ],
      nodes: [{ id: rootNodeId, runId, status: "running" }],
      subagents: [],
      messages: [{ id: MessageId.make("message_recovery_cancel"), runId, streaming: true }],
      turnItems: [
        {
          id: TurnItemId.make("turn_item_recovery_cancel"),
          runId,
          nodeId: rootNodeId,
          status: "running",
        },
        ...(["assistant_message", "reasoning", "proposed_plan"] as const).map((type) => ({
          id: TurnItemId.make(`turn_item_recovery_cancel_${type}`),
          runId,
          nodeId: rootNodeId,
          type,
          status: "running",
          streaming: true,
        })),
      ],
    } as unknown as OrchestrationV2ThreadProjection;
    const layer = ProviderRuntimeRecovery.layer.pipe(
      Layer.provide(ServerSettings.layerTest()),
      Layer.provide(
        Layer.mergeAll(
          Layer.mock(ProjectionStore.ProjectionStoreV2)({
            getRecoveryThreadIds: () => Effect.succeed([threadId]),
            getRuntimeRecoveryProjection: () => Effect.succeed(projection),
          }),
          Layer.mock(EventSink.EventSinkV2)({
            commitCommand: (input) => {
              committedInput = input;
              return Effect.succeed({ committed: true, cancelledEffectCount: 2 } as never);
            },
          }),
          IdAllocator.layer,
          Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
            runRecoveryOnce: Effect.succeed(false),
          }),
          Layer.mock(EffectOutbox.EffectOutboxV2)({
            reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
          }),
        ),
      ),
    );

    return Effect.gen(function* () {
      const summary = yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService)
        .recover;
      assert.equal(summary.terminalizedRuns, 1);
      assert.equal(summary.stoppedSessions, 1);
      assert.equal(summary.retiredEffects, 2);
      assert.deepEqual(committedInput?.cancelUnsettledEffects?.effectTypes, [
        "provider-session.apply-model-selection",
        "provider-turn.start",
        "provider-turn.interrupt",
        "provider-turn.steer",
        "provider-turn.restart",
        "runtime-request.respond",
      ]);
      const events = committedInput?.events ?? [];
      assert.deepEqual(
        events.map((event) => [
          event.type,
          "status" in event.payload ? event.payload.status : null,
        ]),
        [
          ["run.updated", "cancelled"],
          ["run-attempt.updated", "cancelled"],
          ["node.updated", "cancelled"],
          ["provider-turn.updated", "cancelled"],
          ["message.updated", null],
          ["turn-item.updated", "cancelled"],
          ["turn-item.updated", "cancelled"],
          ["turn-item.updated", "cancelled"],
          ["turn-item.updated", "cancelled"],
          ["turn-item.updated", "completed"],
          ["provider-thread.updated", "idle"],
          ["provider-session.updated", "stopped"],
        ],
      );
      const messageEvent = events.find((event) => event.type === "message.updated");
      assert.isFalse(messageEvent?.type === "message.updated" && messageEvent.payload.streaming);
      const stoppedSession = events.find((event) => event.type === "provider-session.updated");
      assert.deepEqual(
        stoppedSession?.type === "provider-session.updated"
          ? stoppedSession.payload.extensionStatuses
          : undefined,
        [],
      );
      assert.deepEqual(
        events.flatMap((event) =>
          event.type === "turn-item.updated" && "streaming" in event.payload
            ? [[event.payload.type, event.payload.streaming]]
            : [],
        ),
        [
          ["assistant_message", false],
          ["reasoning", false],
          ["proposed_plan", false],
        ],
      );
    }).pipe(Effect.provide(layer));
  },
);

it.effect("cancels workflow members and child roots without run ids after restart", () => {
  const parentThreadId = ThreadId.make("thread_recovery_workflow_members");
  const childThreadId = ThreadId.make("thread_recovery_workflow_member_child");
  const memberId = NodeId.make("node_recovery_workflow_member");
  const childRootId = NodeId.make("node_recovery_workflow_member_root");
  const providerInstanceId = ProviderInstanceId.make("claudeAgent");
  const driver = ProviderDriverKind.make("claudeAgent");
  const member = {
    id: memberId,
    threadId: parentThreadId,
    runId: null,
    origin: "provider_native",
    status: "running",
    driver,
    providerInstanceId,
  };
  const parentProjection = {
    thread: { id: parentThreadId, providerInstanceId },
    runs: [],
    attempts: [],
    nodes: [
      { id: memberId, threadId: parentThreadId, runId: null, kind: "subagent", status: "running" },
    ],
    subagents: [member],
    runtimeRequests: [],
    providerSessions: [],
    providerThreads: [],
    providerTurns: [],
    messages: [],
    turnItems: [],
  } as unknown as OrchestrationV2ThreadProjection;
  const childProjection = {
    ...parentProjection,
    thread: { id: childThreadId, providerInstanceId },
    nodes: [
      {
        id: childRootId,
        threadId: childThreadId,
        runId: null,
        kind: "root_turn",
        status: "running",
      },
    ],
    subagents: [],
  } as unknown as OrchestrationV2ThreadProjection;
  const committed: Array<Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0]> = [];
  const layer = ProviderRuntimeRecovery.layer.pipe(
    Layer.provide(ServerSettings.layerTest()),
    Layer.provide(
      Layer.mergeAll(
        Layer.mock(ProjectionStore.ProjectionStoreV2)({
          getRecoveryThreadIds: () => Effect.succeed([parentThreadId, childThreadId]),
          getRuntimeRecoveryProjection: (threadId) =>
            Effect.succeed(threadId === parentThreadId ? parentProjection : childProjection),
        }),
        Layer.mock(EventSink.EventSinkV2)({
          commitCommand: (input) => {
            committed.push(input);
            return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
          },
        }),
        IdAllocator.layer,
        Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
          runRecoveryOnce: Effect.succeed(false),
        }),
        Layer.mock(EffectOutbox.EffectOutboxV2)({
          reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
        }),
      ),
    ),
  );

  return Effect.gen(function* () {
    yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService).reconcile("startup");
    assert.deepEqual(
      committed.flatMap((command) =>
        command.events.map((event) => [
          command.threadId,
          event.type,
          "status" in event.payload ? event.payload.status : null,
        ]),
      ),
      [
        [parentThreadId, "subagent.updated", "cancelled"],
        [parentThreadId, "node.updated", "cancelled"],
        [childThreadId, "node.updated", "cancelled"],
      ],
    );
  }).pipe(Effect.provide(layer));
});

it.effect(
  "clears persisted pendingBackgroundTasks and terminalizes stale background items on settled runs",
  () => {
    const threadId = ThreadId.make("thread_recovery_background");
    const settledRunId = RunId.make("run_recovery_background_settled");
    const activeRunId = RunId.make("run_recovery_background_active");
    const activeAttemptId = RunAttemptId.make("attempt_recovery_background_active");
    const activeRootNodeId = NodeId.make("node_recovery_background_active");
    const idleProviderThreadId = ProviderThreadId.make("provider_thread_recovery_background_idle");
    const activeProviderThreadId = ProviderThreadId.make(
      "provider_thread_recovery_background_active",
    );
    const secondaryProviderThreadId = ProviderThreadId.make(
      "provider_thread_recovery_background_secondary",
    );
    const providerSessionId = ProviderSessionId.make("provider_session_recovery_background");
    const settledStaleItemId = TurnItemId.make("turn_item_recovery_background_stale");
    const activeRunItemId = TurnItemId.make("turn_item_recovery_background_active");
    const nullRunCommandItemId = TurnItemId.make("turn_item_recovery_background_null_run");
    const nullRunSubagentItemId = TurnItemId.make("turn_item_recovery_background_null_subagent");
    const claudeInstanceId = ProviderInstanceId.make("claude");
    const secondaryInstanceId = ProviderInstanceId.make("claude-secondary");
    const subagentInstanceId = ProviderInstanceId.make("claude-subagent");
    let committedInput: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0] | null =
      null;
    const projection = {
      thread: { id: threadId },
      runtimeRequests: [],
      providerSessions: [
        {
          id: providerSessionId,
          driver: ProviderDriverKind.make("claude"),
          providerInstanceId: claudeInstanceId,
          status: "ready",
        },
      ],
      providerThreads: [
        {
          id: idleProviderThreadId,
          driver: ProviderDriverKind.make("claude"),

          providerInstanceId: claudeInstanceId,
          status: "idle",
          pendingBackgroundTasks: [{ taskId: "bg-settled", description: "sleep 30" }],
        },
        {
          id: activeProviderThreadId,
          driver: ProviderDriverKind.make("claude"),
          providerInstanceId: claudeInstanceId,
          status: "active",
          pendingBackgroundTasks: [{ taskId: "bg-active", description: "npm test" }],
        },
        {
          id: secondaryProviderThreadId,
          driver: ProviderDriverKind.make("claude"),
          providerInstanceId: secondaryInstanceId,
          status: "idle",
          pendingBackgroundTasks: [],
        },
      ],
      providerTurns: [],
      runs: [
        {
          id: settledRunId,
          status: "completed",
          providerInstanceId: claudeInstanceId,
        },
        {
          id: activeRunId,
          status: "running",
          providerInstanceId: claudeInstanceId,
        },
      ],
      attempts: [
        {
          id: activeAttemptId,
          runId: activeRunId,
          rootNodeId: activeRootNodeId,
          status: "running",
        },
      ],
      nodes: [{ id: activeRootNodeId, runId: activeRunId, status: "running" }],
      subagents: [],
      messages: [],
      turnItems: [
        {
          id: settledStaleItemId,
          runId: settledRunId,
          nodeId: null,
          providerThreadId: idleProviderThreadId,
          type: "command_execution",
          status: "running",
        },
        {
          id: activeRunItemId,
          runId: activeRunId,
          nodeId: activeRootNodeId,
          providerThreadId: activeProviderThreadId,
          type: "dynamic_tool",
          status: "running",
        },
        {
          id: nullRunCommandItemId,
          runId: null,
          nodeId: null,
          providerThreadId: secondaryProviderThreadId,
          type: "command_execution",
          status: "running",
        },
        {
          id: nullRunSubagentItemId,
          runId: null,
          nodeId: null,
          providerThreadId: secondaryProviderThreadId,
          type: "subagent",
          status: "running",
          providerInstanceId: subagentInstanceId,
        },
      ],
    } as unknown as OrchestrationV2ThreadProjection;
    const layer = ProviderRuntimeRecovery.layer.pipe(
      Layer.provide(ServerSettings.layerTest()),
      Layer.provide(
        Layer.mergeAll(
          Layer.mock(ProjectionStore.ProjectionStoreV2)({
            getRecoveryThreadIds: () => Effect.succeed([threadId]),
            getRuntimeRecoveryProjection: () => Effect.succeed(projection),
          }),
          Layer.mock(EventSink.EventSinkV2)({
            commitCommand: (input) => {
              committedInput = input;
              return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
            },
          }),
          IdAllocator.layer,
          Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
            runRecoveryOnce: Effect.succeed(false),
          }),
          Layer.mock(EffectOutbox.EffectOutboxV2)({
            listByCommandId: () => Effect.succeed([]),
            reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
          }),
        ),
      ),
    );

    return Effect.gen(function* () {
      const summary =
        yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService).reconcile("startup");
      assert.equal(summary.terminalizedRuns, 1);
      const events = committedInput?.events ?? [];

      const turnItemCancels = events.filter(
        (event) => event.type === "turn-item.updated" && event.payload.status === "cancelled",
      );

      assert.equal(turnItemCancels.length, 4);
      assert.deepEqual(
        turnItemCancels
          .map((event) => event.type === "turn-item.updated" && event.payload.id)
          .sort(),
        [activeRunItemId, nullRunCommandItemId, nullRunSubagentItemId, settledStaleItemId].sort(),
      );

      const cancelById = (id: TurnItemId) =>
        turnItemCancels.find(
          (event) => event.type === "turn-item.updated" && event.payload.id === id,
        );
      assert.equal(cancelById(nullRunCommandItemId)?.providerInstanceId, secondaryInstanceId);

      assert.notEqual(subagentInstanceId, secondaryInstanceId);
      assert.equal(cancelById(nullRunSubagentItemId)?.providerInstanceId, subagentInstanceId);

      assert.equal(cancelById(settledStaleItemId)?.providerInstanceId, claudeInstanceId);

      const providerThreadEvents = events.filter(
        (event) => event.type === "provider-thread.updated",
      );

      assert.equal(providerThreadEvents.length, 2);
      for (const event of providerThreadEvents) {
        if (event.type !== "provider-thread.updated") continue;
        assert.deepEqual(event.payload.pendingBackgroundTasks ?? [], []);
      }
      const idleThreadEvent = providerThreadEvents.find(
        (event) =>
          event.type === "provider-thread.updated" && event.payload.id === idleProviderThreadId,
      );
      assert.equal(
        idleThreadEvent?.type === "provider-thread.updated" ? idleThreadEvent.payload.status : null,
        "idle",
      );
      const activeThreadEvent = providerThreadEvents.find(
        (event) =>
          event.type === "provider-thread.updated" && event.payload.id === activeProviderThreadId,
      );
      assert.equal(
        activeThreadEvent?.type === "provider-thread.updated"
          ? activeThreadEvent.payload.status
          : null,
        "idle",
      );
    }).pipe(Effect.provide(layer));
  },
);

it.effect(
  "terminalizes a leftover nonpersistent dynamic_tool on a settled run after process loss",
  () => {
    const threadId = ThreadId.make("thread_recovery_orphan_wait");
    const settledRunId = RunId.make("run_recovery_orphan_wait_settled");
    const providerThreadId = ProviderThreadId.make("provider_thread_recovery_orphan_wait");
    const orphanWaitItemId = TurnItemId.make(
      "turn-item:provider:codex:native-item:exec-4669f3bb-78c9-4af1-b44e-daa340d2c538",
    );
    const persistentMonitorItemId = TurnItemId.make(
      "turn-item:provider:codex:native-item:exec-persistent-monitor",
    );
    const orphanWaitNodeId = NodeId.make("node_recovery_orphan_wait");
    const persistentMonitorNodeId = NodeId.make("node_recovery_persistent_monitor");
    const codexInstanceId = ProviderInstanceId.make("codex");
    let committedInput: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0] | null =
      null;
    const projection = {
      thread: { id: threadId, providerInstanceId: codexInstanceId },
      runtimeRequests: [],
      providerSessions: [],
      providerThreads: [
        {
          id: providerThreadId,
          driver: ProviderDriverKind.make("codex"),
          providerInstanceId: codexInstanceId,
          status: "idle",
          pendingBackgroundTasks: [],
        },
      ],
      providerTurns: [],
      runs: [{ id: settledRunId, status: "completed", providerInstanceId: codexInstanceId }],
      attempts: [],
      nodes: [
        { id: orphanWaitNodeId, runId: settledRunId, status: "running", kind: "tool_call" },
        {
          id: persistentMonitorNodeId,
          runId: settledRunId,
          status: "running",
          kind: "tool_call",
        },
      ],
      subagents: [],
      messages: [],
      turnItems: [
        {
          id: orphanWaitItemId,
          runId: settledRunId,
          nodeId: orphanWaitNodeId,
          providerThreadId,
          type: "dynamic_tool",
          status: "running",
          toolName: "t3-code.t3_thread_wait",
          input: {
            threadId:
              "thread:delegated-task:command%3Amcp%3Aaafffab1-e811-458a-ae83-558e542c61ff%3Adelegate-task%3Areview-mobile-reconnect-opus-20260815",
            timeoutMs: 30000,
          },
        },
        {
          id: persistentMonitorItemId,
          runId: settledRunId,
          nodeId: persistentMonitorNodeId,
          providerThreadId,
          type: "dynamic_tool",
          status: "running",
          toolName: "grok.monitor",
          input: { persistent: true, command: "tail -f" },
        },
      ],
    } as unknown as OrchestrationV2ThreadProjection;
    const layer = ProviderRuntimeRecovery.layer.pipe(
      Layer.provide(ServerSettings.layerTest()),
      Layer.provide(
        Layer.mergeAll(
          Layer.mock(ProjectionStore.ProjectionStoreV2)({
            getRecoveryThreadIds: () => Effect.succeed([threadId]),
            getRuntimeRecoveryProjection: () => Effect.succeed(projection),
          }),
          Layer.mock(EventSink.EventSinkV2)({
            commitCommand: (input) => {
              committedInput = input;
              return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
            },
          }),
          IdAllocator.layer,
          Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
            runRecoveryOnce: Effect.succeed(false),
          }),
          Layer.mock(EffectOutbox.EffectOutboxV2)({
            listByCommandId: () => Effect.succeed([]),
            reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
          }),
        ),
      ),
    );

    return Effect.gen(function* () {
      const summary =
        yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService).reconcile("startup");
      assert.equal(summary.terminalizedRuns, 0);
      const turnItemCancels = (committedInput?.events ?? []).filter(
        (event) => event.type === "turn-item.updated" && event.payload.status === "cancelled",
      );

      assert.equal(turnItemCancels.length, 2);
      assert.deepEqual(
        turnItemCancels
          .map((event) => event.type === "turn-item.updated" && event.payload.id)
          .sort(),
        [orphanWaitItemId, persistentMonitorItemId].sort(),
      );
      const nodeCancels = (committedInput?.events ?? []).filter(
        (event) => event.type === "node.updated" && event.payload.status === "cancelled",
      );
      assert.equal(nodeCancels.length, 2);
      assert.deepEqual(
        nodeCancels.map((event) => event.type === "node.updated" && event.payload.id).sort(),
        [orphanWaitNodeId, persistentMonitorNodeId].sort(),
      );
    }).pipe(Effect.provide(layer));
  },
);

it.effect(
  "terminalizes the linked subagent and node for a stale subagent item on a settled run",
  () => {
    const threadId = ThreadId.make("thread_recovery_subagent");
    const settledRunId = RunId.make("run_recovery_subagent_settled");
    const providerThreadId = ProviderThreadId.make("provider_thread_recovery_subagent");
    const staleSubagentNodeId = NodeId.make("node_recovery_subagent_stale");
    const doneSubagentNodeId = NodeId.make("node_recovery_subagent_done");
    const staleItemId = TurnItemId.make("turn_item_recovery_subagent_stale");
    const doneItemId = TurnItemId.make("turn_item_recovery_subagent_done");
    const claudeInstanceId = ProviderInstanceId.make("claude");
    let committedInput: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0] | null =
      null;
    const projection = {
      thread: { id: threadId },
      runtimeRequests: [],
      providerSessions: [],
      providerThreads: [
        {
          id: providerThreadId,
          driver: ProviderDriverKind.make("claude"),
          providerInstanceId: claudeInstanceId,
          status: "idle",
          pendingBackgroundTasks: [],
        },
      ],
      providerTurns: [],

      runs: [{ id: settledRunId, status: "completed", providerInstanceId: claudeInstanceId }],
      attempts: [],
      nodes: [
        { id: staleSubagentNodeId, runId: settledRunId, status: "running" },
        { id: doneSubagentNodeId, runId: settledRunId, status: "completed" },
      ],
      subagents: [
        {
          id: staleSubagentNodeId,
          runId: settledRunId,
          origin: "provider_native",
          driver: ProviderDriverKind.make("claude"),
          providerInstanceId: claudeInstanceId,
          status: "running",
        },
        {
          id: doneSubagentNodeId,
          runId: settledRunId,
          origin: "provider_native",
          driver: ProviderDriverKind.make("claude"),
          providerInstanceId: claudeInstanceId,
          status: "completed",
          result: "done",
        },
      ],
      messages: [],
      turnItems: [
        {
          id: staleItemId,
          runId: settledRunId,
          nodeId: staleSubagentNodeId,
          providerThreadId,
          type: "subagent",
          status: "running",
          subagentId: staleSubagentNodeId,
          providerInstanceId: claudeInstanceId,
        },
        {
          id: doneItemId,
          runId: settledRunId,
          nodeId: doneSubagentNodeId,
          providerThreadId,
          type: "subagent",
          status: "completed",
          subagentId: doneSubagentNodeId,
          providerInstanceId: claudeInstanceId,
        },
      ],
    } as unknown as OrchestrationV2ThreadProjection;
    const layer = ProviderRuntimeRecovery.layer.pipe(
      Layer.provide(ServerSettings.layerTest()),
      Layer.provide(
        Layer.mergeAll(
          Layer.mock(ProjectionStore.ProjectionStoreV2)({
            getRecoveryThreadIds: () => Effect.succeed([threadId]),
            getRuntimeRecoveryProjection: () => Effect.succeed(projection),
          }),
          Layer.mock(EventSink.EventSinkV2)({
            commitCommand: (input) => {
              committedInput = input;
              return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
            },
          }),
          IdAllocator.layer,
          Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
            runRecoveryOnce: Effect.succeed(false),
          }),
          Layer.mock(EffectOutbox.EffectOutboxV2)({
            listByCommandId: () => Effect.succeed([]),
            reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
          }),
        ),
      ),
    );

    return Effect.gen(function* () {
      yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService).reconcile("startup");
      const events = committedInput?.events ?? [];

      const turnItemCancels = events.filter(
        (event) => event.type === "turn-item.updated" && event.payload.status === "cancelled",
      );
      assert.equal(turnItemCancels.length, 1);

      const subagentCancels = events.filter((event) => event.type === "subagent.updated");
      assert.equal(subagentCancels.length, 1);
      const subagentCancel = subagentCancels[0];
      assert.equal(
        subagentCancel?.type === "subagent.updated" ? subagentCancel.payload.id : null,
        staleSubagentNodeId,
      );
      assert.equal(
        subagentCancel?.type === "subagent.updated" ? subagentCancel.payload.status : null,
        "cancelled",
      );

      const nodeCancels = events.filter((event) => event.type === "node.updated");
      assert.equal(nodeCancels.length, 1);
      const nodeCancel = nodeCancels[0];
      assert.equal(
        nodeCancel?.type === "node.updated" ? nodeCancel.payload.id : null,
        staleSubagentNodeId,
      );

      assert.isFalse(
        events.some(
          (event) => event.type === "subagent.updated" && event.payload.id === doneSubagentNodeId,
        ),
      );
      assert.isFalse(
        events.some(
          (event) => event.type === "node.updated" && event.payload.id === doneSubagentNodeId,
        ),
      );
    }).pipe(Effect.provide(layer));
  },
);

it.effect("leaves delegated tasks to their own child threads after process loss", () => {
  const threadId = ThreadId.make("thread_recovery_delegation");
  const settledRunId = RunId.make("run_recovery_delegation_settled");
  const runningRunId = RunId.make("run_recovery_delegation_running");
  const providerThreadId = ProviderThreadId.make("provider_thread_recovery_delegation");
  const claudeInstanceId = ProviderInstanceId.make("claude");
  const driver = ProviderDriverKind.make("claude");
  const settledTaskId = NodeId.make("node_recovery_delegation_settled");
  const runningTaskId = NodeId.make("node_recovery_delegation_running");
  const nativeSubagentId = NodeId.make("node_recovery_native_subagent");
  const delegatedTask = (id: NodeId, runId: RunId) => ({
    id,
    runId,
    origin: "app_owned",
    childThreadId: ThreadId.make(`thread:delegated-task:${id}`),
    driver,
    providerInstanceId: claudeInstanceId,
    status: "running",
  });
  const subagentItem = (subagentId: NodeId, runId: RunId, origin: string) => ({
    id: TurnItemId.make(`turn_item:${subagentId}`),
    runId,
    nodeId: subagentId,
    providerThreadId,
    type: "subagent",
    status: "running",
    subagentId,
    origin,
    childThreadId:
      origin === "app_owned" ? ThreadId.make(`thread:delegated-task:${subagentId}`) : null,
    providerInstanceId: claudeInstanceId,
    title: `subagent ${subagentId}`,
  });
  let committedInput: Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0] | null =
    null;
  const projection = {
    thread: { id: threadId, providerInstanceId: claudeInstanceId },
    runtimeRequests: [],
    providerSessions: [],
    providerThreads: [
      {
        id: providerThreadId,
        driver,
        providerInstanceId: claudeInstanceId,
        ownerNodeId: null,
        status: "idle",
        pendingBackgroundTasks: [],
      },
    ],
    providerTurns: [],
    runs: [
      {
        id: settledRunId,
        ordinal: 1,
        status: "completed",
        providerThreadId,
        providerInstanceId: claudeInstanceId,
      },
      {
        id: runningRunId,
        ordinal: 2,
        status: "running",
        providerThreadId,
        providerInstanceId: claudeInstanceId,
      },
    ],
    attempts: [],
    nodes: [
      { id: settledTaskId, runId: settledRunId, status: "running", kind: "subagent" },
      { id: runningTaskId, runId: runningRunId, status: "running", kind: "subagent" },
      { id: nativeSubagentId, runId: settledRunId, status: "running", kind: "subagent" },
    ],
    subagents: [
      delegatedTask(settledTaskId, settledRunId),
      delegatedTask(runningTaskId, runningRunId),
      {
        ...delegatedTask(nativeSubagentId, settledRunId),
        origin: "provider_native",
        childThreadId: null,
      },
    ],
    messages: [],
    turnItems: [
      subagentItem(settledTaskId, settledRunId, "app_owned"),
      subagentItem(runningTaskId, runningRunId, "app_owned"),
      subagentItem(nativeSubagentId, settledRunId, "provider_native"),
    ],
  } as unknown as OrchestrationV2ThreadProjection;
  const layer = ProviderRuntimeRecovery.layer.pipe(
    Layer.provide(ServerSettings.layerTest()),
    Layer.provide(
      Layer.mergeAll(
        Layer.mock(ProjectionStore.ProjectionStoreV2)({
          getRecoveryThreadIds: () => Effect.succeed([threadId]),
          getRuntimeRecoveryProjection: () => Effect.succeed(projection),
        }),
        Layer.mock(EventSink.EventSinkV2)({
          commitCommand: (input) => {
            committedInput = input;
            return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
          },
        }),
        IdAllocator.layer,
        Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
          runRecoveryOnce: Effect.succeed(false),
        }),
        Layer.mock(EffectOutbox.EffectOutboxV2)({
          listByCommandId: () => Effect.succeed([]),
          reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
        }),
      ),
    ),
  );

  return Effect.gen(function* () {
    yield* (yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService).reconcile("startup");
    const events = committedInput?.events ?? [];
    const touched = events.flatMap((event) =>
      event.type === "turn-item.updated"
        ? event.payload.type === "subagent"
          ? [event.payload.subagentId]
          : []
        : event.type === "subagent.updated" || event.type === "node.updated"
          ? [event.payload.id]
          : [],
    );

    assert.isTrue(
      events.some(
        (event) =>
          event.type === "run.updated" &&
          event.payload.id === runningRunId &&
          event.payload.status === "cancelled",
      ),
    );
    assert.notInclude(touched, settledTaskId);
    assert.notInclude(touched, runningTaskId);
    assert.include(touched, nativeSubagentId);

    const noted = events.flatMap((event) =>
      event.type === "run.background-work-cancelled"
        ? event.payload.restartCancelledBackgroundWork.map((work) => work.label)
        : [],
    );
    assert.deepEqual(noted, [`subagent ${nativeSubagentId}`]);
  }).pipe(Effect.provide(layer));
});

describe("work held for an automatic update", () => {
  const MINUTE = 60_000;
  const HELD_AT = 30 * MINUTE;
  type Kind =
    | "user message"
    | "scheduled run"
    | "t3_thread_send"
    | "delegated completion"
    | "worktree preparation";
  const messageFor = (kind: Kind, id: MessageId) => {
    switch (kind) {
      case "scheduled run":
        return { id, createdBy: "user", creationSource: "web", scheduledTaskId: "task-1" };
      case "t3_thread_send":
        return { id, createdBy: "agent", creationSource: "mcp" };
      case "delegated completion":
        return {
          id,
          createdBy: "agent",
          creationSource: "server",
          delegatedCompletion: { childThreadId: "child-thread", status: "completed" },
        };
      default:
        return { id, createdBy: "user", creationSource: "web" };
    }
  };

  interface ThreadInput {
    readonly kind: Kind;
    /** Request time of the unstarted run; "send now", a resumed queue and a worktree retry keep
     * one from before the hold. Defaults to a minute after it. */
    readonly requestedAtMs?: number;
    readonly queuedBehind?: boolean;
    /** The user held this queue: the head run, or the run queued behind it. */
    readonly queueHeld?: "head" | "queued";
  }

  /** A thread with an unstarted run, and optionally a message queued behind it. */
  const threadFor = (input: ThreadInput, index: number) => {
    const suffix = `${input.kind.replaceAll(" ", "_")}_${index}`;
    const threadId = ThreadId.make(`thread_update_hold_${suffix}`);
    const runId = RunId.make(`run_update_hold_${suffix}`);
    const queuedRunId = RunId.make(`run_update_hold_queued_${suffix}`);
    const attemptId = RunAttemptId.make(`attempt_update_hold_${suffix}`);
    const rootNodeId = NodeId.make(`node_update_hold_${suffix}`);
    const messageId = MessageId.make(`message_update_hold_${suffix}`);
    const queuedMessageId = MessageId.make(`message_update_hold_queued_${suffix}`);
    const preparing = input.kind === "worktree preparation";
    const requestedAtMs = input.requestedAtMs ?? HELD_AT + MINUTE;
    const projection = {
      thread: { id: threadId },
      runtimeRequests: [],
      providerSessions: [],
      providerThreads: [],
      providerTurns: [],
      runs: [
        {
          id: runId,
          status: preparing ? "preparing" : "starting",
          startedAt: null,
          requestedAt: DateTime.makeUnsafe(requestedAtMs),
          activeAttemptId: attemptId,
          userMessageId: messageId,
          queuePosition: null,
          providerInstanceId: ProviderInstanceId.make("codex"),
          ...(input.queueHeld === "head" ? { queueHeld: true } : {}),
          ...(preparing
            ? {
                workspacePreparation: { type: "worktree", baseRef: "main", startFromOrigin: false },
              }
            : {}),
        },
        ...(input.queuedBehind
          ? [
              {
                id: queuedRunId,
                status: "queued",
                startedAt: null,
                requestedAt: DateTime.makeUnsafe(requestedAtMs + MINUTE),
                activeAttemptId: null,
                userMessageId: queuedMessageId,
                queuePosition: 1,
                providerInstanceId: ProviderInstanceId.make("codex"),
                ...(input.queueHeld === "queued" ? { queueHeld: true } : {}),
              },
            ]
          : []),
      ],
      attempts: [{ id: attemptId, runId, rootNodeId, status: "pending" }],
      nodes: [{ id: rootNodeId, runId, status: "pending" }],
      subagents: [],
      messages: [
        messageFor(input.kind, messageId),
        ...(input.queuedBehind ? [messageFor("t3_thread_send", queuedMessageId)] : []),
      ],
      turnItems: [],
    } as unknown as OrchestrationV2ThreadProjection;
    return { threadId, runId, queuedRunId, projection };
  };

  type MarkerState = "none" | "fresh" | "stale" | "invalid" | Partial<UpdateHold>;

  /**
   * The database a held update leaves behind, and processes that start on it. Every process sees
   * the same database: what a failed update's rollback restores, or what a crash leaves.
   */
  const heldWorld = Effect.fn("test.heldWorld")(function* (
    threads: ReadonlyArray<ThreadInput>,
    options: { readonly marker?: MarkerState } = {},
  ) {
    const built = threads.map(threadFor);
    const projections = new Map(built.map((thread) => [thread.threadId, thread.projection]));
    const commits: Array<Parameters<EventSink.EventSinkV2["Service"]["commitCommand"]>[0]> = [];
    const starts: Array<RunId> = [];
    const prepared: Array<RunId> = [];
    let failStartWrites = false;
    yield* TestClock.setTime(HELD_AT + 10 * MINUTE);
    const memory = yield* memoryUpdateHoldStore;
    let failStartedRecordsAfter = Number.POSITIVE_INFINITY;
    let startedRecords = 0;
    // The marker file; a write that adds started runs can be made to fail.
    const marker: UpdateHoldStore = {
      read: memory.read,
      clear: memory.clear,
      write: (hold) =>
        Effect.gen(function* () {
          const before = yield* memory.current;
          const addsStarted =
            (hold.started?.length ?? 0) >
            (before === undefined || before === "invalid" ? 0 : (before.started?.length ?? 0));
          if (addsStarted && startedRecords++ >= failStartedRecordsAfter) {
            return yield* new UpdateHoldMarkerError({ reason: "disk full" });
          }
          yield* memory.write(hold);
        }),
    };
    const fresh = {
      heldAt: DateTime.formatIso(DateTime.makeUnsafe(HELD_AT)),
      targetVersion: "next",
    };
    const state = options.marker ?? "fresh";
    if (state === "fresh") yield* memory.write(fresh);
    else if (state === "stale") {
      yield* memory.write({
        ...fresh,
        heldAt: DateTime.formatIso(DateTime.makeUnsafe(HELD_AT - UPDATE_HOLD_MAX_AGE_MS)),
      });
    } else if (state === "invalid") yield* memory.set("invalid");
    else if (state !== "none") yield* memory.write({ ...fresh, ...state });

    /** One server process: startup recovery, which keeps held work but starts none of it. */
    const startProcess = Effect.fn("test.startProcess")(function* () {
      const gate = yield* makeWorkAdmissionGate({ marker });
      yield* gate.setPreparationResumer((preparation) =>
        Effect.sync(() => {
          prepared.push(preparation.runId);
        }),
      );
      const layer = ProviderRuntimeRecovery.layer.pipe(
        Layer.provide(ServerSettings.layerTest()),
        Layer.provide(
          Layer.mergeAll(
            Layer.mock(ProjectionStore.ProjectionStoreV2)({
              getRecoveryThreadIds: () => Effect.succeed([...projections.keys()]),
              getRuntimeRecoveryProjection: (threadId) =>
                Effect.succeed(projections.get(threadId)!),
              getMessagesById: (threadId, ids) =>
                Effect.succeed(
                  new Map(
                    projections
                      .get(threadId)!
                      .messages.filter((message) => ids.includes(message.id))
                      .map((message) => [message.id, message]),
                  ),
                ),
              getThreadRecords: (threadId) => Effect.succeed(projections.get(threadId)!),
            }),
            Layer.mock(EventSink.EventSinkV2)({
              commitCommand: (command) => {
                commits.push(command);
                return Effect.succeed({ committed: true, cancelledEffectCount: 0 } as never);
              },
              writeWithEffects: (write) => {
                if (failStartWrites) return Effect.die("the database went away");
                for (const effect of write.effects) {
                  if (effect.request.type === "provider-turn.start") {
                    starts.push(effect.request.runId);
                  }
                }
                return Effect.succeed({} as never);
              },
            }),
            IdAllocator.layer,
            Layer.mock(EffectWorker.OrchestrationEffectWorkerV2)({
              runRecoveryOnce: Effect.succeed(false),
            }),
            Layer.mock(EffectOutbox.EffectOutboxV2)({
              reconcileAfterProcessLoss: Effect.succeed({ requeued: 0, cancelled: 0 }),
              cancelUnsettled: () => Effect.succeed([]),
              signalCancellations: () => Effect.void,
            }),
          ),
        ),
      );
      const recovery = yield* ProviderRuntimeRecovery.ProviderRuntimeRecoveryService.pipe(
        Effect.provide(layer),
        Effect.provideService(WorkAdmission, gate),
      );
      const recover = recovery.reconcile("startup");
      return { gate, recover, summary: yield* recover };
    });

    return {
      threads: built,
      startProcess,
      marker: memory.current,
      runUpdates: () =>
        commits.flatMap((command) =>
          command.events.flatMap((event) => (event.type === "run.updated" ? [event.payload] : [])),
        ),
      starts: () => [...starts],
      prepared: () => [...prepared],
      failStartWrites: (fail: boolean) => {
        failStartWrites = fail;
      },
      failStartedRecordsAfter: (count: number) => {
        failStartedRecordsAfter = count;
        startedRecords = 0;
      },
    };
  });

  it.effect.each([
    "user message",
    "scheduled run",
    "t3_thread_send",
    "delegated completion",
  ] as const)("starts a held %s once the update is committed, not before", (kind) =>
    Effect.gen(function* () {
      const world = yield* heldWorld([{ kind, queuedBehind: true }]);
      const [thread] = world.threads;
      const process = yield* world.startProcess();
      assert.equal(process.summary.terminalizedRuns, 0);
      // Neither cancelled nor put in the queue the user has to release.
      assert.deepEqual(
        world
          .runUpdates()
          .filter((update) => update.status === "cancelled" || update.queueHeld === true),
        [],
      );
      // The launcher has not committed the update yet: a rollback must find nothing started.
      assert.deepEqual(world.starts(), []);
      assert.deepEqual(yield* world.marker, {
        heldAt: DateTime.formatIso(DateTime.makeUnsafe(HELD_AT)),
        targetVersion: "next",
        runs: [thread!.runId, thread!.queuedRunId],
      });
      assert.isFalse(yield* process.gate.heldWorkSettled);
      yield* process.gate.startHeldWork;
      // A fresh start for the held run; the message queued behind it follows it as usual.
      assert.deepEqual(world.starts(), [thread!.runId]);
      assert.deepEqual(world.prepared(), []);
      assert.isUndefined(yield* world.marker);
      assert.isTrue(yield* process.gate.heldWorkSettled);
    }),
  );

  it.effect("prepares a held worktree again once the update is committed", () =>
    Effect.gen(function* () {
      const world = yield* heldWorld([{ kind: "worktree preparation" }]);
      const process = yield* world.startProcess();
      assert.equal(process.summary.terminalizedRuns, 0);
      assert.deepEqual(world.prepared(), []);
      yield* process.gate.startHeldWork;
      assert.deepEqual(world.starts(), []);
      assert.deepEqual(world.prepared(), [world.threads[0]!.runId]);
    }),
  );

  it.effect.each(["user message", "worktree preparation"] as const)(
    "starts a held %s whatever its request time: send now, a resumed queue or a retry keep an old one",
    (kind) =>
      Effect.gen(function* () {
        // Saved after the hold (anything saved before it stopped the update), stamped before it.
        const world = yield* heldWorld([{ kind, requestedAtMs: HELD_AT - 10 * MINUTE }]);
        const process = yield* world.startProcess();
        assert.equal(process.summary.terminalizedRuns, 0);
        yield* process.gate.startHeldWork;
        assert.deepEqual([...world.starts(), ...world.prepared()], [world.threads[0]!.runId]);
      }),
  );

  it.effect("leaves a queue the user held alone", () =>
    Effect.gen(function* () {
      const head = yield* heldWorld([{ kind: "user message", queueHeld: "head" }]);
      yield* (yield* head.startProcess()).gate.startHeldWork;
      assert.deepEqual(head.starts(), []);
      const queued = yield* heldWorld([
        { kind: "user message", queuedBehind: true, queueHeld: "queued" },
      ]);
      const process = yield* queued.startProcess();
      // The held head starts; the queue the user held behind it is not part of the update's work.
      assert.deepEqual((yield* queued.marker) as UpdateHold, {
        heldAt: DateTime.formatIso(DateTime.makeUnsafe(HELD_AT)),
        targetVersion: "next",
        runs: [queued.threads[0]!.runId],
      });
      yield* process.gate.startHeldWork;
      assert.deepEqual(queued.starts(), [queued.threads[0]!.runId]);
      assert.isFalse(
        queued
          .runUpdates()
          .some((update) => update.id === queued.threads[0]!.queuedRunId && !update.queueHeld),
      );
    }),
  );

  it.effect.each([
    { name: "a failed update's rollback", second: "old version" },
    { name: "a crash between the commit and the start", second: "same version" },
  ])("starts held work exactly once after $name", () =>
    Effect.gen(function* () {
      const world = yield* heldWorld([
        { kind: "user message", queuedBehind: true },
        { kind: "worktree preparation" },
      ]);
      // The trial (or the committed process) keeps the work and stops before starting any.
      yield* world.startProcess();
      assert.deepEqual([...world.starts(), ...world.prepared()], []);
      // The next process finds the same database and the marker's list.
      const next = yield* world.startProcess();
      yield* next.gate.startHeldWork;
      assert.deepEqual(world.starts(), [world.threads[0]!.runId]);
      assert.deepEqual(world.prepared(), [world.threads[1]!.runId]);
      assert.isUndefined(yield* world.marker);
      // A later startup on that database starts nothing again.
      const later = yield* world.startProcess();
      yield* later.gate.startHeldWork;
      assert.equal(world.starts().length + world.prepared().length, 2);
    }),
  );

  it.effect("starts held work once when recovery and the start run twice in one process", () =>
    Effect.gen(function* () {
      const world = yield* heldWorld([{ kind: "user message", queuedBehind: true }]);
      const process = yield* world.startProcess();
      yield* process.recover;
      yield* process.gate.startHeldWork;
      yield* process.gate.startHeldWork;
      assert.deepEqual(world.starts(), [world.threads[0]!.runId]);
    }),
  );

  it.effect("keeps the marker when starting fails part-way, and a retry starts only the rest", () =>
    Effect.gen(function* () {
      const world = yield* heldWorld([
        { kind: "user message" },
        { kind: "scheduled run" },
        { kind: "t3_thread_send" },
      ]);
      const [first, second, third] = world.threads;
      const process = yield* world.startProcess();
      world.failStartedRecordsAfter(1);
      yield* process.gate.startHeldWork;
      assert.deepEqual(world.starts(), [first!.runId]);
      assert.isFalse(yield* process.gate.heldWorkSettled);
      assert.deepInclude(yield* world.marker, { started: [first!.runId] });
      // The same process tries again.
      world.failStartedRecordsAfter(1);
      yield* process.gate.startHeldWork;
      assert.deepEqual(world.starts(), [first!.runId, second!.runId]);
      // A restart finishes the job.
      world.failStartedRecordsAfter(Number.POSITIVE_INFINITY);
      const next = yield* world.startProcess();
      yield* next.gate.startHeldWork;
      assert.deepEqual(world.starts(), [first!.runId, second!.runId, third!.runId]);
      assert.isUndefined(yield* world.marker);
      assert.isTrue(yield* next.gate.heldWorkSettled);
    }),
  );

  it.effect("never starts a run twice when the process stops while starting it", () =>
    Effect.gen(function* () {
      const world = yield* heldWorld([{ kind: "user message" }]);
      const process = yield* world.startProcess();
      // Its start is recorded, then the write is lost with the process.
      world.failStartWrites(true);
      yield* process.gate.startHeldWork;
      assert.deepEqual(world.starts(), []);
      world.failStartWrites(false);
      const next = yield* world.startProcess();
      yield* next.gate.startHeldWork;
      // At most once: it takes the upstream path, the queue the user can release.
      assert.deepEqual(world.starts(), []);
      assert.isTrue(
        world
          .runUpdates()
          .some((update) => update.id === world.threads[0]!.runId && update.queueHeld === true),
      );
      assert.isUndefined(yield* world.marker);
    }),
  );

  it.effect("only starts the runs the first startup kept", () =>
    Effect.gen(function* () {
      // The first startup found nothing held; this run was saved after it (and lost by a crash).
      const world = yield* heldWorld([{ kind: "user message" }], { marker: { runs: [] } });
      const process = yield* world.startProcess();
      yield* process.gate.startHeldWork;
      assert.deepEqual(world.starts(), []);
      assert.isTrue(world.runUpdates().some((update) => update.queueHeld === true));
      assert.isUndefined(yield* world.marker);
    }),
  );

  it.effect.each(["none", "stale", "invalid"] as const)(
    "keeps the upstream restart behaviour with marker %s",
    (marker) =>
      Effect.gen(function* () {
        const world = yield* heldWorld(
          [{ kind: "user message" }, { kind: "t3_thread_send" }, { kind: "worktree preparation" }],
          { marker },
        );
        const process = yield* world.startProcess();
        yield* process.gate.startHeldWork;
        assert.deepEqual(world.starts(), []);
        assert.deepEqual(world.prepared(), []);
        // The user message waits in the held queue; the agent message and preparation stop.
        assert.isTrue(
          world
            .runUpdates()
            .some((update) => update.id === world.threads[0]!.runId && update.queueHeld === true),
        );
        assert.equal(process.summary.terminalizedRuns, 2);
        assert.isUndefined(yield* world.marker);
        assert.isTrue(yield* process.gate.heldWorkSettled);
      }),
  );
});
