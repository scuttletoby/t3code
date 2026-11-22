/**
 * Fork: the thread shell shows a completed quiet handoff run as upstream's stopped run, in both of
 * its reads: the JavaScript one (`threadShellFromProjection`) and the SQL one (the shell and
 * awareness snapshots), over a real projection store (SQLite in memory). Every other run is as before.
 */
import { assert, describe, it } from "@effect/vitest";
import {
  EnvironmentId,
  EventId,
  MessageId,
  type OrchestrationV2ConversationMessage,
  type OrchestrationV2Run,
  RunId,
  ThreadId,
} from "@t3tools/contracts";
import { projectThreadAwarenessV2 } from "@t3tools/shared/agentAwareness";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as SqlClient from "effect/sql/SqlClient";

import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as SqlitePersistence from "../../persistence/Sqlite.ts";
import {
  at,
  handoffMessageId,
  MINUTE_MS,
  projection,
  run,
  runId,
  threadId,
} from "./handoff.testkit.ts";
import { HANDOFF_LOUD_SUFFIX, HANDOFF_RETRY_SUFFIX } from "./handoffFacts.ts";
import { HANDOFF_SETTLE_MS } from "./handoffQuiet.ts";
import { quietHandoffShellMark, RELAY_COMPLETION_HOLD_MS } from "./handoffShell.ts";

const base = projection({ usedTokens: 0 });
const userMessage = base.messages[0]!;
const handoffRunId = RunId.make("run-2");
const nextRunId = RunId.make("run-3");

const message = (
  patch: Partial<OrchestrationV2ConversationMessage>,
): OrchestrationV2ConversationMessage => ({ ...userMessage, ...patch });

/** The handoff message the service sends for run-1, which starts run-2 (`HandoffService`). */
const handoffMessage = (id: MessageId = handoffMessageId) =>
  message({
    id,
    runId: handoffRunId,
    text: "Handoff",
    createdBy: "agent",
    creationSource: "server",
  });

/** Sent after the settle, as the service does: run-1's alert went out first. */
const handoffRun = (patch: Partial<OrchestrationV2Run> = {}) =>
  run({
    id: handoffRunId,
    ordinal: 2,
    userMessageId: handoffMessageId,
    requestedAt: at(HANDOFF_SETTLE_MS),
    startedAt: at(HANDOFF_SETTLE_MS),
    completedAt: at(MINUTE_MS),
    ...patch,
  });

/** The person's next turn after the handoff run. */
const nextRun = run({
  id: nextRunId,
  ordinal: 3,
  userMessageId: MessageId.make("message-3"),
  requestedAt: at(2 * MINUTE_MS),
  startedAt: at(2 * MINUTE_MS),
  completedAt: at(3 * MINUTE_MS),
});
const nextMessage = message({ id: MessageId.make("message-3"), runId: nextRunId });

const storeLayer = ProjectionStore.layer.pipe(Layer.provideMerge(SqlitePersistence.layerMemory));

interface World {
  readonly runs: ReadonlyArray<OrchestrationV2Run>;
  readonly messages: ReadonlyArray<OrchestrationV2ConversationMessage>;
}

const quietWorld: World = {
  runs: [run(), handoffRun()],
  messages: [userMessage, handoffMessage()],
};

/** Stores the thread, runs and messages as the orchestrator's events do; again is a no-op. */
const seed = (world: World, id: ThreadId = threadId) =>
  Effect.gen(function* () {
    const store = yield* ProjectionStore.ProjectionStoreV2;
    const occurredAt = at();
    yield* store.apply({
      id: EventId.make(`seed:thread:${id}`),
      type: "thread.created",
      threadId: id,
      occurredAt,
      payload: { ...base.thread, id, lineage: { ...base.thread.lineage, rootThreadId: id } },
    });
    for (const one of world.runs) {
      yield* store.apply({
        id: EventId.make(`seed:run:${id}:${one.id}:${one.status}:${one.userMessageId}`),
        type: "run.created",
        threadId: id,
        occurredAt,
        payload: { ...one, threadId: id },
      });
    }
    for (const one of world.messages) {
      yield* store.apply({
        id: EventId.make(`seed:message:${id}:${one.id}:${one.runId}:${one.createdBy}`),
        type: "message.updated",
        threadId: id,
        occurredAt,
        payload: { ...one, threadId: id },
      });
    }
  });

/** What each shell read says about the latest run: the JavaScript read, the shell snapshot and awareness. */
const readStop = (id: ThreadId = threadId) =>
  Effect.gen(function* () {
    const store = yield* ProjectionStore.ProjectionStoreV2;
    const js = ProjectionStore.threadShellFromProjection(yield* store.getThreadProjection(id));
    const shell = (yield* store.getShellSnapshot()).threads.find((thread) => thread.id === id);
    const narrowed = (yield* yield* store.readShellSnapshot({ threadIds: [id] })).threads[0];
    const one = yield* store.getThreadShell(id);
    const awareness = (yield* store.getAgentAwarenessSnapshot(id)).threads[0];
    return {
      latestRunId: js.latestRunId,
      js: js.latestRunStopRequested,
      sql: shell?.latestRunStopRequested,
      narrowed: narrowed?.latestRunStopRequested,
      one: one?.latestRunStopRequested,
      awareness: awareness?.latestRunStopRequested,
      relayPhase:
        awareness === undefined
          ? undefined
          : (projectThreadAwarenessV2({
              environmentId: EnvironmentId.make("environment"),
              project: { title: "Project" },
              thread: awareness,
            })?.phase ?? null),
    };
  });

/** Every read agrees: the latest run reads as stopped (quiet) or not. */
const expectStop = (
  read: unknown,
  stopped: boolean,
  latestRunId: RunId,
  phase: "completed" | "failed" | "running" = "completed",
) => {
  assert.deepStrictEqual(read, {
    latestRunId,
    js: stopped,
    sql: stopped,
    narrowed: stopped,
    one: stopped,
    awareness: stopped,
    relayPhase: stopped ? null : phase,
  });
};

const caseOf = (
  world: World,
  stopped: boolean,
  latestRunId = handoffRunId,
  phase: "completed" | "failed" | "running" = "completed",
) =>
  Effect.gen(function* () {
    yield* seed(world);
    expectStop(yield* readStop(), stopped, latestRunId, phase);
  }).pipe(Effect.provide(storeLayer));

describe("the thread shell of a completed quiet handoff run", () => {
  it.effect("reads as upstream's stopped run, in the JavaScript and the SQL read", () =>
    caseOf(quietWorld, true),
  );

  it.effect("the person's next turn alerts again, and an old run without the mark alerts", () =>
    Effect.gen(function* () {
      yield* caseOf(
        { runs: [...quietWorld.runs, nextRun], messages: [...quietWorld.messages, nextMessage] },
        false,
        nextRunId,
      );
      yield* caseOf({ runs: [run()], messages: [userMessage] }, false, runId);
    }),
  );

  it.effect("a loud or retried handoff run, or one the person steered, alerts", () =>
    Effect.gen(function* () {
      for (const id of [
        MessageId.make(`${handoffMessageId}${HANDOFF_LOUD_SUFFIX}`),
        MessageId.make(`${handoffMessageId}${HANDOFF_RETRY_SUFFIX}`),
        MessageId.make(`${handoffMessageId}${HANDOFF_LOUD_SUFFIX}${HANDOFF_RETRY_SUFFIX}`),
      ]) {
        yield* caseOf(
          {
            runs: [run(), handoffRun({ userMessageId: id })],
            messages: [userMessage, handoffMessage(id)],
          },
          false,
        );
      }
      const steer = message({ id: MessageId.make("message-steer"), runId: handoffRunId });
      yield* caseOf({ ...quietWorld, messages: [...quietWorld.messages, steer] }, false);
    }),
  );

  it.effect("the relay's hold is the edge: requested inside it alerts, at it stays quiet", () =>
    Effect.gen(function* () {
      const requestedAt = (offset: number) => ({
        ...quietWorld,
        runs: [run(), handoffRun({ requestedAt: at(offset), startedAt: at(offset) })],
      });
      yield* caseOf(requestedAt(RELAY_COMPLETION_HOLD_MS), true);
      yield* caseOf(requestedAt(RELAY_COMPLETION_HOLD_MS - 1), false);
      yield* caseOf(requestedAt(0), false);
    }),
  );

  it.effect("a handoff id the person typed, or for another run or thread, alerts", () =>
    Effect.gen(function* () {
      const typed = message({ id: handoffMessageId, runId: handoffRunId });
      yield* caseOf({ ...quietWorld, messages: [userMessage, typed] }, false);
      // Sent by an agent, not by the server; or by the person, filed under no run.
      const byAgent = message({ ...handoffMessage(), creationSource: "web" });
      yield* caseOf({ ...quietWorld, messages: [userMessage, byAgent] }, false);
      const unfiled = message({ id: handoffMessageId, runId: null, creationSource: "server" });
      yield* caseOf({ ...quietWorld, messages: [userMessage, unfiled] }, false);
      // The suffix is read off the id even when a source run's own id ends the same way.
      for (const suffix of [HANDOFF_LOUD_SUFFIX, HANDOFF_RETRY_SUFFIX]) {
        const id = MessageId.make(`${handoffMessageId}${suffix}`);
        yield* caseOf(
          {
            runs: [run({ id: RunId.make(`${runId}${suffix}`) }), handoffRun({ userMessageId: id })],
            messages: [userMessage, handoffMessage(id)],
          },
          false,
        );
      }
      const otherRun = MessageId.make(`handoff:${threadId}:run-9`);
      yield* caseOf(
        {
          runs: [run(), handoffRun({ userMessageId: otherRun })],
          messages: [userMessage, handoffMessage(otherRun)],
        },
        false,
      );
      const otherThread = MessageId.make(`handoff:${threadId}-other:${runId}`);
      yield* caseOf(
        {
          runs: [run(), handoffRun({ userMessageId: otherThread })],
          messages: [userMessage, handoffMessage(otherThread)],
        },
        false,
      );
      const empty = MessageId.make(`handoff:${threadId}:`);
      yield* caseOf(
        {
          runs: [run(), handoffRun({ userMessageId: empty })],
          messages: [userMessage, handoffMessage(empty)],
        },
        false,
      );
    }),
  );

  it.effect(
    "a write turn (the agent source) ending with no handoff is quiet too; loud or steered alerts",
    () =>
      Effect.gen(function* () {
        const writeWorld = (
          id: MessageId,
          extra: ReadonlyArray<OrchestrationV2ConversationMessage> = [],
        ) => ({
          runs: [run(), handoffRun({ userMessageId: id })],
          messages: [userMessage, handoffMessage(id), ...extra],
        });
        const write = MessageId.make(`handoff-write:${threadId}:${runId}`);
        yield* caseOf(writeWorld(write), true);
        yield* caseOf(writeWorld(MessageId.make(`${write}${HANDOFF_LOUD_SUFFIX}`)), false);
        const steer = message({ id: MessageId.make("message-steer"), runId: handoffRunId });
        yield* caseOf(writeWorld(write, [steer]), false);
        // For another thread's run, or no run: not this thread's write turn.
        yield* caseOf(
          writeWorld(MessageId.make(`handoff-write:${threadId}-other:${runId}`)),
          false,
        );
        yield* caseOf(writeWorld(MessageId.make(`handoff-write:${threadId}:`)), false);
      }),
  );

  it.effect(
    "a failed quiet write turn reads as stopped (#836); loud, steered or a failed handoff run does not",
    () =>
      Effect.gen(function* () {
        const write = MessageId.make(`handoff-write:${threadId}:${runId}`);
        const writeWorld = (
          id: MessageId,
          status: OrchestrationV2Run["status"],
          extra: ReadonlyArray<OrchestrationV2ConversationMessage> = [],
        ): World => ({
          runs: [run(), handoffRun({ userMessageId: id, status })],
          messages: [userMessage, handoffMessage(id), ...extra],
        });
        /**
         * Every shell read's mark, in a fresh store. The relay's phase is left out: upstream reads
         * the mark for a failed run only once the awareness line the seam list announces is in.
         */
        const marks = (world: World) =>
          Effect.gen(function* () {
            yield* seed(world);
            const { relayPhase: _relayPhase, latestRunId, ...read } = yield* readStop();
            assert.equal(latestRunId, handoffRunId);
            return [...new Set(Object.values(read))];
          }).pipe(Effect.provide(storeLayer));
        assert.deepStrictEqual(yield* marks(writeWorld(write, "failed")), [true]);
        const loud = MessageId.make(`${write}${HANDOFF_LOUD_SUFFIX}`);
        assert.deepStrictEqual(yield* marks(writeWorld(loud, "failed")), [false]);
        const steer = message({ id: MessageId.make("message-steer"), runId: handoffRunId });
        assert.deepStrictEqual(yield* marks(writeWorld(write, "failed", [steer])), [false]);
        // A failed handoff run gets a retry, not a handoff after it: it alerts as before.
        assert.deepStrictEqual(yield* marks(writeWorld(handoffMessageId, "failed")), [false]);
        // An interrupted or cancelled write turn raises no failed alert upstream: left as it was.
        for (const status of ["interrupted", "cancelled"] as const) {
          assert.deepStrictEqual(yield* marks(writeWorld(write, status)), [false]);
        }
      }),
  );

  it.effect(
    "stopped half-way: a handoff run still running or failed, or its prompt not stored yet, alerts as before",
    () =>
      Effect.gen(function* () {
        yield* caseOf({ ...quietWorld, messages: [userMessage] }, false);
        yield* caseOf(
          { ...quietWorld, runs: [run(), handoffRun({ status: "failed" })] },
          false,
          handoffRunId,
          "failed",
        );
        yield* caseOf(
          { ...quietWorld, runs: [run({ completedAt: null, status: "running" }), handoffRun()] },
          false,
          handoffRunId,
          "running",
        );
        // The source run is missing (deleted history): nothing says the person's alert went out.
        yield* caseOf({ ...quietWorld, runs: [handoffRun()] }, false);
      }),
  );

  it.effect("for any other run the mark adds nothing, so upstream's own value stands", () =>
    Effect.gen(function* () {
      const store = yield* ProjectionStore.ProjectionStoreV2;
      yield* seed({ runs: [run()], messages: [userMessage] });
      const projected = yield* store.getThreadProjection(threadId);
      assert.deepStrictEqual(quietHandoffShellMark(projected, projected.runs[0]), {});
      assert.deepStrictEqual(quietHandoffShellMark(projected, null), {});
    }).pipe(Effect.provide(storeLayer)),
  );

  it.effect(
    "runs again and after a restart: the mark is stored, the same events change nothing",
    () =>
      Effect.gen(function* () {
        yield* seed(quietWorld);
        yield* seed(quietWorld);
        expectStop(yield* readStop(), true, handoffRunId);
        expectStop(yield* readStop(), true, handoffRunId);
        // A new store over the same database, as after a restart.
        const sql = yield* SqlClient.SqlClient;
        const restarted = yield* readStop().pipe(
          Effect.provide(
            ProjectionStore.layer.pipe(Layer.provide(Layer.succeed(SqlClient.SqlClient, sql))),
          ),
        );
        expectStop(restarted, true, handoffRunId);
      }).pipe(Effect.provide(storeLayer)),
  );

  it.effect(
    "empty or missing state: a thread without runs, beside a quiet one, reads as before",
    () =>
      Effect.gen(function* () {
        const store = yield* ProjectionStore.ProjectionStoreV2;
        const empty = ThreadId.make("handoff-thread-empty");
        yield* seed({ runs: [], messages: [] }, empty);
        yield* seed(quietWorld);
        const shells = (yield* store.getShellSnapshot()).threads;
        assert.deepStrictEqual(
          shells.map((thread) => [thread.id, thread.latestRunStopRequested]).toSorted(),
          [
            [threadId, true],
            [empty, false],
          ].toSorted(),
        );
        const js = ProjectionStore.threadShellFromProjection(
          yield* store.getThreadProjection(empty),
        );
        assert.equal(js.latestRunStopRequested, false);
      }).pipe(Effect.provide(storeLayer)),
  );
});
