/**
 * Fork (agent slots): `ActiveRunShells.read`, what the agent slot gate counts. Settling a thread
 * does not stop its run (`thread.settle` has no active-run guard), so a settled thread whose run
 * goes on is still a running agent; one without an active run is not. The cases of the old
 * `getShellSnapshot({ activeRunOnly })` test, over a real projection store (SQLite in memory).
 */
import { assert, describe, it } from "@effect/vitest";
import {
  EventId,
  IDLE_COMPACTION_TEXT,
  MessageId,
  type OrchestrationV2AppThread,
  type OrchestrationV2Run,
  ProjectId,
  ProviderInstanceId,
  RunId,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ActiveRunShells from "./ActiveRunShells.ts";

const now = DateTime.makeUnsafe("2026-09-04T12:00:00Z");
const old = DateTime.subtract(now, { days: 10 });
const providerInstanceId = ProviderInstanceId.make("codex");
const modelSelection = { instanceId: providerInstanceId, model: "test-model" };

/** The thread ids each shell read asked for, in order. */
const shellReads: Array<ReadonlyArray<ThreadId> | undefined> = [];

/** Upstream's thread management for this read: the store's own shell read. */
const layerThreads = Layer.effect(
  ThreadManagementService.ThreadManagementService,
  Effect.gen(function* () {
    const store = yield* ProjectionStore.ProjectionStoreV2;
    return {
      readShellSnapshot: (options) => {
        shellReads.push(options?.threadIds);
        return store.readShellSnapshot(options).pipe(Effect.map(Effect.orDie), Effect.orDie);
      },
    } as Pick<
      ThreadManagementService.ThreadManagementService["Service"],
      "readShellSnapshot"
    > as ThreadManagementService.ThreadManagementService["Service"];
  }),
);

const layerSql = ActiveRunShells.layer.pipe(
  Layer.provideMerge(layerThreads),
  Layer.provideMerge(ProjectionStore.layer),
  Layer.provideMerge(SqlitePersistence.layerMemory),
);

const readActive = ActiveRunShells.ActiveRunShells.use((shells) => shells.read);

const ids = (threads: ReadonlyArray<{ readonly id: ThreadId }>) =>
  threads.map((thread) => thread.id).toSorted();

const createThread = Effect.fn(function* (
  name: string,
  overrides: Partial<OrchestrationV2AppThread> = {},
) {
  const store = yield* ProjectionStore.ProjectionStoreV2;
  const threadId = ThreadId.make(`thread:active-run:${name}`);
  const thread: OrchestrationV2AppThread = {
    createdBy: "user",
    creationSource: "web",
    id: threadId,
    projectId: ProjectId.make("project:active-run"),
    title: name,
    providerInstanceId,
    modelSelection,
    runtimeMode: "full-access",
    interactionMode: "default",
    branch: "feature",
    worktreePath: null,
    activeProviderThreadId: null,
    lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: threadId },
    forkedFrom: null,
    createdAt: old,
    updatedAt: old,
    archivedAt: null,
    settledOverride: null,
    settledAt: null,
    lastVisitedAt: null,
    deletedAt: null,
    ...overrides,
  };
  yield* store.apply({
    id: EventId.make(`event:${threadId}`),
    type: "thread.created",
    threadId,
    occurredAt: old,
    payload: thread,
  });
  return threadId;
});

const createRun = Effect.fn(function* (
  threadId: ThreadId,
  status: OrchestrationV2Run["status"] = "completed",
  ordinal = 1,
) {
  const store = yield* ProjectionStore.ProjectionStoreV2;
  const runId = RunId.make(`run:${threadId}:${ordinal}`);
  yield* store.apply({
    id: EventId.make(`event:${runId}:${status}`),
    type: "run.created",
    threadId,
    occurredAt: old,
    payload: {
      id: runId,
      threadId,
      ordinal,
      providerInstanceId,
      modelSelection,
      providerThreadId: null,
      userMessageId: MessageId.make(`message:${runId}`),
      rootNodeId: null,
      activeAttemptId: null,
      status,
      requestedAt: old,
      startedAt: status === "queued" ? null : old,
      completedAt: ["completed", "rolled_back", "failed"].includes(status) ? old : null,
      checkpointId: null,
      contextHandoffId: null,
    },
  });
  return runId;
});

/** The prompt of T3 Code's own idle compaction, which makes its run silent in the shell. */
const idleCompactionPrompt = Effect.fn(function* (threadId: ThreadId, runId: RunId) {
  const store = yield* ProjectionStore.ProjectionStoreV2;
  yield* store.apply({
    id: EventId.make(`event:prompt:${runId}`),
    type: "message.updated",
    threadId,
    occurredAt: old,
    payload: {
      id: MessageId.make(`message:${runId}`),
      threadId,
      runId,
      nodeId: null,
      role: "user",
      text: IDLE_COMPACTION_TEXT,
      attachments: [],
      streaming: false,
      createdBy: "agent",
      creationSource: "server",
      createdAt: old,
      updatedAt: old,
    },
  });
});

describe("ActiveRunShells.read", () => {
  it.effect(
    "returns every thread with an active run, settled or archived too, and nothing else",
    () =>
      Effect.gen(function* () {
        const store = yield* ProjectionStore.ProjectionStoreV2;
        const settledBusy = yield* createThread("settled-busy", {
          settledOverride: "settled",
          settledAt: old,
        });
        const settledBusyRun = yield* createRun(settledBusy, "running");
        const autoSettledBusy = yield* createThread("auto-settled-busy", { settledAt: old });
        yield* createRun(autoSettledBusy, "starting");
        const archivedBusy = yield* createThread("archived-busy", { archivedAt: old });
        yield* createRun(archivedBusy, "preparing");
        const busy = yield* createThread("busy");
        yield* createRun(busy, "running");
        const idle = yield* createThread("idle");
        yield* createRun(idle, "completed");
        const queued = yield* createThread("queued");
        yield* createRun(queued, "queued");
        const settledIdle = yield* createThread("settled-idle", { settledOverride: "settled" });
        yield* createRun(settledIdle, "completed");

        const snapshot = yield* readActive;
        assert.deepStrictEqual(
          ids(snapshot.threads),
          [autoSettledBusy, busy, settledBusy].toSorted(),
        );
        assert.deepStrictEqual(ids(snapshot.archivedThreads), [archivedBusy]);
        assert.equal(
          snapshot.threads.find((thread) => thread.id === settledBusy)?.activeRunId,
          settledBusyRun,
        );
        // What the gate read before: the unsettled view drops the settled agents still running.
        const unsettled = yield* store.getShellSnapshot({ unsettledOnly: true });
        assert.notInclude(ids(unsettled.threads), settledBusy);
      }).pipe(Effect.provide(layerSql)),
  );

  it.effect("an empty store has no running agents", () =>
    Effect.gen(function* () {
      const snapshot = yield* readActive;
      assert.deepStrictEqual([...snapshot.threads, ...snapshot.archivedThreads], []);
    }).pipe(Effect.provide(layerSql)),
  );

  it.effect("reads the shells of the threads with an active run only", () =>
    Effect.gen(function* () {
      const busy = yield* createThread("narrow-busy");
      yield* createRun(busy, "running");
      for (const name of ["narrow-idle-1", "narrow-idle-2"]) {
        yield* createRun(yield* createThread(name), "completed");
      }
      shellReads.length = 0;
      yield* readActive;
      assert.deepStrictEqual(shellReads, [[busy]]);
    }).pipe(Effect.provide(layerSql)),
  );

  it.effect("an idle compaction's run, a waiting run or a deleted thread is no running agent", () =>
    Effect.gen(function* () {
      const compacting = yield* createThread("compacting");
      yield* idleCompactionPrompt(compacting, yield* createRun(compacting, "running"));
      const waiting = yield* createThread("waiting");
      yield* createRun(waiting, "waiting");
      const deleted = yield* createThread("deleted", { deletedAt: old });
      yield* createRun(deleted, "running");
      const busy = yield* createThread("still-busy");
      yield* createRun(busy, "running");

      const snapshot = yield* readActive;
      assert.deepStrictEqual(ids([...snapshot.threads, ...snapshot.archivedThreads]), [busy]);
    }).pipe(Effect.provide(layerSql)),
  );

  it.effect("read again, it follows the store: a run that ends or starts in between", () =>
    Effect.gen(function* () {
      const first = yield* createThread("again-first");
      yield* createRun(first, "running");
      const second = yield* createThread("again-second");
      yield* createRun(second, "completed");
      const before = yield* readActive;
      assert.deepStrictEqual(ids(before.threads), [first]);
      assert.deepStrictEqual(ids((yield* readActive).threads), [first]);

      yield* createRun(first, "completed");
      yield* createRun(second, "running", 2);
      assert.deepStrictEqual(ids((yield* readActive).threads), [second]);
    }).pipe(Effect.provide(layerSql)),
  );
});
