/**
 * Fork (agent slots): `getShellSnapshot({ activeRunOnly })`, what the agent slot gate counts.
 * Settling a thread does not stop its run (`thread.settle` has no active-run guard), so a settled
 * thread whose run goes on is still a running agent; one without an active run is not.
 */
import { assert, describe, it } from "@effect/vitest";
import {
  EventId,
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

import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ProjectionStore from "./ProjectionStore.ts";

const layerSql = ProjectionStore.layer.pipe(Layer.provideMerge(SqlitePersistence.layerMemory));
const now = DateTime.makeUnsafe("2026-09-04T12:00:00Z");
const old = DateTime.subtract(now, { days: 10 });
const providerInstanceId = ProviderInstanceId.make("codex");
const modelSelection = { instanceId: providerInstanceId, model: "test-model" };

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
    id: EventId.make(`event:${runId}`),
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

describe("getShellSnapshot activeRunOnly", () => {
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

        const snapshot = yield* store.getShellSnapshot({ activeRunOnly: true });
        const ids = (threads: ReadonlyArray<{ readonly id: ThreadId }>) =>
          threads.map((thread) => thread.id).toSorted();
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
      const store = yield* ProjectionStore.ProjectionStoreV2;
      const snapshot = yield* store.getShellSnapshot({ activeRunOnly: true });
      assert.deepStrictEqual([...snapshot.threads, ...snapshot.archivedThreads], []);
    }).pipe(Effect.provide(layerSql)),
  );
});
