/**
 * Fork (agent slots): `ActiveRunShells.read`, what the slot gate counts, over a real projection store.
 * A settled thread whose run goes on is still running. Checked against upstream's own shell read for
 * every run status, so drift turns a test red; the reader path runs once over a file database.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  EventId,
  IDLE_COMPACTION_TEXT,
  MessageId,
  type OrchestrationV2AppThread,
  type OrchestrationV2Run,
  OrchestrationV2RunStatus,
  type OrchestrationV2ThreadShellSnapshot,
  ProjectId,
  ProviderInstanceId,
  RunId,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Path from "effect/Path";
import * as SqlClient from "effect/sql/SqlClient";

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

const hasActiveRun = (thread: { readonly activeRunId: unknown }) => thread.activeRunId !== null;

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
      completedAt: ThreadManagementService.isTerminalRunStatus(status) ? old : null,
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

  it.effect(
    "every run status: the same threads as upstream's own shell read, nobody left out",
    () =>
      Effect.gen(function* () {
        const store = yield* ProjectionStore.ProjectionStoreV2;
        // Each status a run can have, on a plain, a settled and an archived thread.
        for (const status of OrchestrationV2RunStatus.literals) {
          yield* createRun(yield* createThread(`status-${status}`), status);
          yield* createRun(
            yield* createThread(`status-${status}-settled`, {
              settledOverride: "settled",
              settledAt: old,
            }),
            status,
          );
          yield* createRun(
            yield* createThread(`status-${status}-archived`, { archivedAt: old }),
            status,
          );
        }
        // An older finished run under a newer one of each status, and an idle compaction's run.
        for (const status of OrchestrationV2RunStatus.literals) {
          const thread = yield* createThread(`later-${status}`);
          yield* createRun(thread, "completed");
          yield* createRun(thread, status, 2);
        }
        const compacting = yield* createThread("oracle-compacting");
        yield* idleCompactionPrompt(compacting, yield* createRun(compacting, "running"));

        // Upstream's own read of every thread: what has `activeRunId` is a running agent.
        const upstream = yield* store.getShellSnapshot();
        const snapshot = yield* readActive;
        assert.deepStrictEqual(ids(snapshot.threads), ids(upstream.threads.filter(hasActiveRun)));
        assert.deepStrictEqual(
          ids(snapshot.archivedThreads),
          ids(upstream.archivedThreads.filter(hasActiveRun)),
        );
        // Not equal by being empty: today's active statuses are in, whatever upstream adds later.
        const counted = ids([...snapshot.threads, ...snapshot.archivedThreads]);
        for (const status of ["preparing", "starting", "running"]) {
          assert.include(counted, ThreadId.make(`thread:active-run:status-${status}`));
          assert.include(counted, ThreadId.make(`thread:active-run:later-${status}`));
        }
      }).pipe(Effect.provide(layerSql)),
  );

  it("drops only upstream's finished statuses; every other one, a new one too, is read", () => {
    assert.deepStrictEqual([...ActiveRunShells.FINISHED_RUN_STATUSES].toSorted(), [
      "cancelled",
      "completed",
      "failed",
      "interrupted",
      "rolled_back",
    ]);
    for (const status of ActiveRunShells.FINISHED_RUN_STATUSES) {
      assert.isTrue(ThreadManagementService.isTerminalRunStatus(status));
    }
  });

  it.effect("upstream's run status index exists under the name the read uses", () =>
    Effect.gen(function* () {
      const sql = yield* SqlClient.SqlClient;
      const rows = yield* sql<{ readonly name: string }>`
        SELECT name FROM sqlite_schema WHERE type = 'index' AND name = ${ActiveRunShells.RUN_STATUS_INDEX}
      `;
      // Red when upstream renames or drops it: update ActiveRunShells.ts (it reads on without it).
      assert.lengthOf(rows, 1);
    }).pipe(Effect.provide(layerSql)),
  );

  it.effect("without the index it still counts every running agent, and logs an error once", () =>
    Effect.gen(function* () {
      const store = yield* ProjectionStore.ProjectionStoreV2;
      const sql = yield* SqlClient.SqlClient;
      const busy = yield* createThread("unindexed-busy");
      yield* createRun(busy, "running");
      const archived = yield* createThread("unindexed-archived", { archivedAt: old });
      yield* createRun(archived, "starting");
      yield* createRun(yield* createThread("unindexed-idle"), "completed");
      // Upstream's shell read names the index too: read it before it goes, and serve that.
      const all = yield* store.getShellSnapshot();
      yield* sql.unsafe(`DROP INDEX ${ActiveRunShells.RUN_STATUS_INDEX}`);
      const only = (
        threads: OrchestrationV2ThreadShellSnapshot["threads"],
        wanted: ReadonlyArray<ThreadId>,
      ) => threads.filter((thread) => wanted.includes(thread.id));
      const threads = {
        readShellSnapshot: (options?: { readonly threadIds?: ReadonlyArray<ThreadId> }) =>
          Effect.succeed(
            Effect.succeed({
              ...all,
              threads: only(all.threads, options?.threadIds ?? []),
              archivedThreads: only(all.archivedThreads, options?.threadIds ?? []),
            }),
          ),
      } as unknown as ThreadManagementService.ThreadManagementService["Service"];
      const errors: Array<string> = [];
      const logger = Logger.make(({ logLevel, message }) => {
        if (logLevel === "Error")
          errors.push(String(Array.isArray(message) ? message[0] : message));
      });

      yield* Effect.gen(function* () {
        const shells = yield* ActiveRunShells.ActiveRunShells;
        for (let read = 0; read < 2; read++) {
          const snapshot = yield* shells.read;
          assert.deepStrictEqual(ids(snapshot.threads), [busy]);
          assert.deepStrictEqual(ids(snapshot.archivedThreads), [archived]);
        }
      }).pipe(
        Effect.provide(
          Layer.mergeAll(
            // Fresh: not the store-backed instance this test's own layer already built.
            Layer.fresh(ActiveRunShells.layer).pipe(
              Layer.provide(
                Layer.succeed(ThreadManagementService.ThreadManagementService, threads),
              ),
              Layer.provide(Layer.succeed(SqlClient.SqlClient, sql)),
            ),
            Logger.layer([logger], { mergeWithExisting: false }),
          ),
        ),
      );
      assert.deepStrictEqual(errors, [ActiveRunShells.MISSING_INDEX_MESSAGE]);
    }).pipe(Effect.provide(layerSql)),
  );
});

describe("ActiveRunShells.read over a file database", () => {
  it.effect("reads on the reader while a write is half-way, and sees only what was committed", () =>
    Effect.gen(function* () {
      const fileSystem = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const directory = yield* fileSystem.makeTempDirectoryScoped({ prefix: "t3-active-runs-" });
      const layerFile = ActiveRunShells.layer.pipe(
        Layer.provideMerge(layerThreads),
        Layer.provideMerge(ProjectionStore.layer),
        Layer.provideMerge(SqlitePersistence.layerFromPath(path.join(directory, "state.sqlite"))),
        Layer.provide(NodeServices.layer),
      );

      yield* Effect.gen(function* () {
        const sql = yield* SqlClient.SqlClient;
        const busy = yield* createThread("file-busy");
        yield* createRun(busy, "running");
        const later = yield* createThread("file-later");
        yield* createRun(later, "completed");
        assert.deepStrictEqual(ids((yield* readActive).threads), [busy]);

        // A write stopped half-way: the writer holds a transaction that started a run.
        const writerHeld = yield* Deferred.make<void>();
        const releaseWriter = yield* Deferred.make<void>();
        const writer = yield* Effect.forkChild(
          sql.withTransaction(
            createRun(later, "running", 2).pipe(
              Effect.andThen(Deferred.succeed(writerHeld, undefined)),
              Effect.andThen(Deferred.await(releaseWriter)),
            ),
          ),
        );
        yield* Deferred.await(writerHeld);
        // On the writer this read would wait for the transaction above, and the test would hang.
        assert.deepStrictEqual(ids((yield* readActive).threads), [busy]);
        yield* Deferred.succeed(releaseWriter, undefined);
        yield* Fiber.join(writer);
        // Read again after the commit: the new run counts.
        assert.deepStrictEqual(ids((yield* readActive).threads), [busy, later].toSorted());
      }).pipe(Effect.provide(layerFile));
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );
});
