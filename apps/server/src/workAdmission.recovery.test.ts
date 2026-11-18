// Fork: startup recovery and work held for an automatic update (the marker of workAdmission.ts).
// Held work starts once, when the update commits. Upstream's own restart paths (the visible held
// queue, the resend of unattended prompts) never reach a held run, and a run recovery rewound is
// never held.
import { assert, describe, it } from "@effect/vitest";
import {
  MessageId,
  NodeId,
  ProviderInstanceId,
  RunAttemptId,
  RunId,
  ThreadId,
  type OrchestrationV2ThreadProjection,
} from "@t3tools/contracts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as TestClock from "effect/testing/TestClock";

import * as EffectOutbox from "./orchestration-v2/EffectOutbox.ts";
import * as EffectWorker from "./orchestration-v2/EffectWorker.ts";
import * as EventSink from "./orchestration-v2/EventSink.ts";
import * as ProjectionStore from "./orchestration-v2/ProjectionStore.ts";
import * as ProviderRuntimeRecovery from "./orchestration-v2/ProviderRuntimeRecoveryService.ts";
import * as ServerSettings from "./serverSettings.ts";
import {
  makeWorkAdmissionGate,
  memoryUpdateHoldStore,
  UPDATE_HOLD_MAX_AGE_MS,
  type UpdateHold,
  UpdateHoldMarkerError,
  type UpdateHoldStore,
  WorkAdmission,
} from "./workAdmission.ts";

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
    /** An open thread, so upstream may send its unattended prompt again after a restart. */
    readonly live?: boolean;
    /** Stored as running, but no provider turn accepted it: recovery rewinds it to unstarted. */
    readonly rewound?: boolean;
    /** The same, but stored as starting with a start time: only that time shows the rewind. */
    readonly rewoundStarting?: boolean;
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
    const started = input.rewound === true || input.rewoundStarting === true;
    const projection = {
      thread: input.live
        ? {
            id: threadId,
            archivedAt: null,
            deletedAt: null,
            settledOverride: null,
            snoozedUntil: null,
            lineage: { relationshipToParent: null, parentThreadId: null },
            forkedFrom: null,
          }
        : { id: threadId },
      runtimeRequests: [],
      providerSessions: [],
      providerThreads: [],
      providerTurns: [],
      runs: [
        {
          id: runId,
          status: input.rewound === true ? "running" : preparing ? "preparing" : "starting",
          startedAt: started ? DateTime.makeUnsafe(requestedAtMs) : null,
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
      attempts: [{ id: attemptId, runId, rootNodeId, status: started ? "running" : "pending" }],
      nodes: [{ id: rootNodeId, runId, status: started ? "running" : "pending" }],
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
              listByCommandId: () => Effect.succeed([]),
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

  it.effect(
    "does not send a held scheduled run again at startup: it starts once, on the commit",
    () =>
      Effect.gen(function* () {
        const world = yield* heldWorld([{ kind: "scheduled run", live: true }]);
        const [thread] = world.threads;
        const process = yield* world.startProcess();
        // Upstream sends an unattended prompt again right here; a held one waits for the commit.
        assert.deepEqual(world.starts(), []);
        assert.deepInclude(yield* world.marker, { runs: [thread!.runId] });
        yield* process.gate.startHeldWork;
        assert.deepEqual(world.starts(), [thread!.runId]);
        yield* process.gate.startHeldWork;
        assert.deepEqual(world.starts(), [thread!.runId]);
      }),
  );

  it.effect(
    "starts a held agent message once when the server restarts twice before the commit",
    () =>
      Effect.gen(function* () {
        const world = yield* heldWorld([{ kind: "t3_thread_send", live: true }]);
        yield* world.startProcess();
        const second = yield* world.startProcess();
        assert.deepEqual(world.starts(), []);
        yield* second.gate.startHeldWork;
        assert.deepEqual(world.starts(), [world.threads[0]!.runId]);
      }),
  );

  it.effect("does not hold a rewound scheduled run: upstream sends it again once, reset", () =>
    Effect.gen(function* () {
      const world = yield* heldWorld([{ kind: "scheduled run", live: true, rewound: true }]);
      const [thread] = world.threads;
      const process = yield* world.startProcess();
      assert.deepEqual(world.starts(), [thread!.runId]);
      // Put back to unstarted, as upstream does for a run no provider accepted.
      assert.isTrue(
        world
          .runUpdates()
          .some(
            (update) =>
              update.id === thread!.runId &&
              update.status === "starting" &&
              update.startedAt === null,
          ),
      );
      assert.equal(process.summary.terminalizedRuns, 0);
      // Not held work: the commit starts nothing more, and the marker never listed it.
      assert.deepInclude(yield* world.marker, { runs: [] });
      yield* process.gate.startHeldWork;
      assert.deepEqual(world.starts(), [thread!.runId]);
    }),
  );

  it.effect("does not hold a rewound user message: it waits in the visible held queue", () =>
    Effect.gen(function* () {
      const world = yield* heldWorld([{ kind: "user message", live: true, rewound: true }]);
      const process = yield* world.startProcess();
      yield* process.gate.startHeldWork;
      assert.deepEqual(world.starts(), []);
      assert.isTrue(
        world
          .runUpdates()
          .some((update) => update.id === world.threads[0]!.runId && update.queueHeld === true),
      );
    }),
  );

  it.effect("does not hold a rewound run stored as starting with a start time: one resend", () =>
    Effect.gen(function* () {
      // Its status is the one recovery rewinds it to, so only the start time tells it was started.
      const world = yield* heldWorld([
        { kind: "scheduled run", live: true, rewoundStarting: true },
      ]);
      const [thread] = world.threads;
      const process = yield* world.startProcess();
      assert.deepEqual(world.starts(), [thread!.runId]);
      // Held, its rewind (start time cleared) would never be written.
      assert.isTrue(
        world
          .runUpdates()
          .some((update) => update.id === thread!.runId && update.startedAt === null),
      );
      assert.deepInclude(yield* world.marker, { runs: [] });
      yield* process.gate.startHeldWork;
      assert.deepEqual(world.starts(), [thread!.runId]);
    }),
  );

  it.effect(
    "holds no queued run on a thread with no held head: both wait in the visible held queue",
    () =>
      Effect.gen(function* () {
        // The rewound head is not held, so nothing would start the run queued behind it.
        const world = yield* heldWorld([
          { kind: "user message", live: true, rewound: true, queuedBehind: true },
        ]);
        const [thread] = world.threads;
        const process = yield* world.startProcess();
        assert.deepInclude(yield* world.marker, { runs: [] });
        yield* process.gate.startHeldWork;
        assert.deepEqual(world.starts(), []);
        const queueHeld = (runId: RunId) =>
          world.runUpdates().some((update) => update.id === runId && update.queueHeld === true);
        assert.isTrue(queueHeld(thread!.runId));
        assert.isTrue(queueHeld(thread!.queuedRunId));
        assert.equal(process.summary.terminalizedRuns, 0);
      }),
  );

  it.effect("sends a rewound run the marker lists again once, never as held work too", () =>
    Effect.gen(function* () {
      // An earlier startup kept it; then a process started it and stopped before its provider did.
      const world = yield* heldWorld([{ kind: "scheduled run", live: true, rewound: true }], {
        marker: { runs: ["run_update_hold_scheduled_run_0"] },
      });
      const [thread] = world.threads;
      assert.equal(thread!.runId, "run_update_hold_scheduled_run_0");
      const process = yield* world.startProcess();
      assert.deepEqual(world.starts(), [thread!.runId]);
      yield* process.gate.startHeldWork;
      assert.deepEqual(world.starts(), [thread!.runId]);
    }),
  );

  it.effect.each([
    { name: "a rewound scheduled run", kind: "scheduled run", rewound: true },
    { name: "an agent message", kind: "t3_thread_send", rewound: false },
  ] as const)("leaves upstream's resend of $name alone when no update held work", (input) =>
    Effect.gen(function* () {
      const world = yield* heldWorld([{ kind: input.kind, live: true, rewound: input.rewound }], {
        marker: "none",
      });
      const [thread] = world.threads;
      const process = yield* world.startProcess();
      assert.deepEqual(world.starts(), [thread!.runId]);
      assert.equal(process.summary.terminalizedRuns, 0);
      yield* process.gate.startHeldWork;
      assert.deepEqual(world.starts(), [thread!.runId]);
    }),
  );
});
