/**
 * Fork: a completed handoff run does not alert through the relay; every other run alerts as before.
 * The relay stand-in decides with upstream's `projectThreadAwarenessV2`, wired as server.ts wires
 * the real one, over a real projection store (SQLite in memory) that outlives each "server".
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { describe, expect, it } from "@effect/vitest";
import {
  EnvironmentId,
  EventId,
  MessageId,
  type OrchestrationV2ConversationMessage,
  type OrchestrationV2Run,
  RunId,
  type ThreadId,
} from "@t3tools/contracts";
import { projectThreadAwarenessV2 } from "@t3tools/shared/agentAwareness";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as SqlClient from "effect/sql/SqlClient";

import * as ServerConfig from "../../config.ts";
import type { AgentAwarenessThread } from "../../orchestration-v2/ProjectionStore.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as SqlitePersistence from "../../persistence/Sqlite.ts";
import * as AgentAwarenessRelay from "../../relay/AgentAwarenessRelay.ts";
import * as WorkAdmission from "../../workAdmission.ts";
import * as ForkLayers from "../forkLayers.ts";
import {
  at,
  handoffMessageId,
  MINUTE_MS,
  projection,
  run,
  runId,
  threadId,
} from "./handoff.testkit.ts";
import { HANDOFF_RETRY_SUFFIX } from "./handoffFacts.ts";
import { isQuietHandoffRun, quietHandoffAwareness } from "./handoffQuiet.ts";

const environmentId = EnvironmentId.make("environment");
const base = projection({ usedTokens: 0 });
const userMessage = base.messages[0]!;
const handoffRunId = RunId.make("run-2");
const userRunId = RunId.make("run-3");

const message = (
  patch: Partial<OrchestrationV2ConversationMessage>,
): OrchestrationV2ConversationMessage => ({ ...userMessage, ...patch });

/** The handoff message the service sends for run-1, which starts run-2 (`HandoffService`). */
const handoffMessage = message({
  id: handoffMessageId,
  runId: handoffRunId,
  text: "Handoff",
  createdBy: "agent",
  creationSource: "server",
});
const handoffRun = run({ id: handoffRunId, ordinal: 2, userMessageId: handoffMessageId });

const testDatabase = ProjectionStore.layer.pipe(Layer.provideMerge(SqlitePersistence.layerMemory));

/** Stores the thread (once), runs and messages as the orchestrator's events do. */
const seed = (input: {
  readonly runs: ReadonlyArray<OrchestrationV2Run>;
  readonly messages: ReadonlyArray<OrchestrationV2ConversationMessage>;
}) =>
  Effect.gen(function* () {
    const store = yield* ProjectionStore.ProjectionStoreV2;
    const occurredAt = at();
    yield* store.apply({
      id: EventId.make("seed:thread"),
      type: "thread.created",
      threadId,
      occurredAt,
      payload: base.thread,
    });
    for (const one of input.runs) {
      yield* store.apply({
        id: EventId.make(`seed:run:${one.id}:${one.status}:${one.userMessageId}`),
        type: "run.created",
        threadId,
        occurredAt,
        payload: one,
      });
    }
    for (const one of input.messages) {
      yield* store.apply({
        id: EventId.make(`seed:message:${one.id}:${one.runId}:${one.createdBy}`),
        type: "message.updated",
        threadId,
        occurredAt,
        payload: one,
      });
    }
  });

/** The awareness row upstream reads for the thread: its latest run completed. */
const awarenessThread = (
  latestRunId: RunId | null,
  patch: Partial<AgentAwarenessThread> = {},
): AgentAwarenessThread => ({
  id: threadId,
  projectId: base.thread.projectId,
  title: "Long thread",
  lineage: base.thread.lineage,
  modelSelection: base.thread.modelSelection,
  creationSource: "web",
  status: "completed",
  activityRunStatus: null,
  latestRunId,
  latestRunCompletedAt: at(),
  latestRunStopRequested: false,
  latestRunBackgroundWake: false,
  activeRunId: null,
  pendingRuntimeRequest: null,
  pendingBackgroundTasks: [],
  pendingGuestMessageCount: 0,
  hasActionableProposedPlan: false,
  settledOverride: null,
  archivedAt: null,
  snoozedUntil: null,
  snoozedAt: null,
  updatedAt: at(),
  ...patch,
});

/** Upstream's thread reads, stubbed to the one awareness row. */
const threadsOf = (row: AgentAwarenessThread) =>
  ({
    getAgentAwarenessSnapshot: (requested?: ThreadId) =>
      Effect.succeed({
        snapshotSequence: 1,
        threads: requested === undefined || requested === threadId ? [row] : [],
      }),
  }) as unknown as ThreadManagement.ThreadManagementService["Service"];

/** A relay stand-in: on publish it reads the thread and keeps the phase upstream would publish. */
const relayStandIn = (published: Array<string | null>) =>
  Layer.effect(
    AgentAwarenessRelay.AgentAwarenessRelay,
    Effect.gen(function* () {
      const threads = yield* ThreadManagement.ThreadManagementService;
      return AgentAwarenessRelay.AgentAwarenessRelay.of({
        publishThread: (id) =>
          threads.getAgentAwarenessSnapshot(id).pipe(
            Effect.map((snapshot) => {
              const state = projectThreadAwarenessV2({
                environmentId,
                project: { title: "Project" },
                thread: snapshot.threads[0]!,
              });
              published.push(state?.phase ?? null);
            }),
            Effect.orDie,
          ),
        drain: Effect.void,
        requestCatchUp: () => Effect.void,
        start: () => Effect.void,
      });
    }),
  );

/**
 * Starts a "server" (a fresh layer graph over the same database) and publishes the thread once
 * through the relay as server.ts wires it; `wired: false` is upstream's relay alone.
 */
const publishOnce = (row: AgentAwarenessThread, wired = true) =>
  Effect.gen(function* () {
    const published: Array<string | null> = [];
    const sql = yield* SqlClient.SqlClient;
    const gate = yield* WorkAdmission.makeWorkAdmissionGate({ heldFromBoot: false });
    const relayLayer = relayStandIn(published);
    yield* AgentAwarenessRelay.AgentAwarenessRelay.use((relay) =>
      relay.publishThread(threadId),
    ).pipe(
      Effect.provide(
        (wired ? ForkLayers.holdWorkAgentActivity(relayLayer) : relayLayer).pipe(
          Layer.provide(Layer.succeed(ThreadManagement.ThreadManagementService, threadsOf(row))),
          Layer.provide(Layer.succeed(SqlClient.SqlClient, sql)),
          Layer.provide(Layer.succeed(WorkAdmission.WorkAdmission, gate)),
          Layer.provide(ServerConfig.layerTest(process.cwd(), { prefix: "t3-handoff-quiet-" })),
          Layer.provide(NodeServices.layer),
        ),
      ),
    );
    return published[0];
  });

describe("handoff run: no relay alert", () => {
  it.effect("a completed handoff run does not alert; upstream's relay alone would", () =>
    Effect.gen(function* () {
      yield* seed({ runs: [run(), handoffRun], messages: [userMessage, handoffMessage] });
      const row = awarenessThread(handoffRunId);
      expect(yield* publishOnce(row)).toBeNull();
      expect(yield* publishOnce(row, false)).toBe("completed");
    }).pipe(Effect.provide(testDatabase)),
  );

  it.effect("runs again after a restart: the mark is stored, so it stays quiet", () =>
    Effect.gen(function* () {
      yield* seed({ runs: [run(), handoffRun], messages: [userMessage, handoffMessage] });
      // Two servers in turn over the same database: nothing is kept in memory between them.
      expect(yield* publishOnce(awarenessThread(handoffRunId))).toBeNull();
      expect(yield* publishOnce(awarenessThread(handoffRunId))).toBeNull();
    }).pipe(Effect.provide(testDatabase)),
  );

  it.effect("the next real turn alerts as before, after a restart too", () =>
    Effect.gen(function* () {
      yield* seed({ runs: [run(), handoffRun], messages: [userMessage, handoffMessage] });
      expect(yield* publishOnce(awarenessThread(handoffRunId))).toBeNull();
      const userMessageId = MessageId.make("message-3");
      yield* seed({
        runs: [run({ id: userRunId, ordinal: 3, userMessageId })],
        messages: [message({ id: userMessageId, runId: userRunId })],
      });
      expect(yield* publishOnce(awarenessThread(userRunId))).toBe("completed");
    }).pipe(Effect.provide(testDatabase)),
  );

  it.effect("its one retry is quiet too; a failed handoff run still alerts", () =>
    Effect.gen(function* () {
      const retryId = MessageId.make(`${handoffMessageId}${HANDOFF_RETRY_SUFFIX}`);
      const retryRunId = RunId.make("run-2-retry");
      yield* seed({
        runs: [run(), { ...handoffRun, status: "failed" }],
        messages: [userMessage, handoffMessage],
      });
      expect(yield* publishOnce(awarenessThread(handoffRunId, { status: "failed" }))).toBe(
        "failed",
      );
      yield* seed({
        runs: [run({ id: retryRunId, ordinal: 3, userMessageId: retryId })],
        messages: [{ ...handoffMessage, id: retryId, runId: retryRunId }],
      });
      expect(yield* publishOnce(awarenessThread(retryRunId))).toBeNull();
    }).pipe(Effect.provide(testDatabase)),
  );

  it.effect("the person steering into the handoff run makes it theirs: it alerts", () =>
    Effect.gen(function* () {
      yield* seed({
        runs: [run(), handoffRun],
        messages: [
          userMessage,
          handoffMessage,
          message({ id: MessageId.make("steer"), runId: handoffRunId, updatedAt: at(MINUTE_MS) }),
        ],
      });
      expect(yield* publishOnce(awarenessThread(handoffRunId))).toBe("completed");
    }).pipe(Effect.provide(testDatabase)),
  );

  it.effect("old, empty or half-written state alerts as before", () =>
    Effect.gen(function* () {
      // Empty: no thread stored at all (deleted, or never projected).
      expect(yield* publishOnce(awarenessThread(handoffRunId))).toBe("completed");
      // Half-written: the run is stored but its handoff message is not.
      yield* seed({ runs: [run(), handoffRun], messages: [userMessage] });
      expect(yield* publishOnce(awarenessThread(handoffRunId))).toBe("completed");
      // Old: a run from before handoffs, with an ordinary message.
      expect(yield* publishOnce(awarenessThread(runId))).toBe("completed");
      // No run yet: upstream publishes nothing, and nothing changes that.
      expect(yield* publishOnce(awarenessThread(null, { status: "idle" }))).toBeNull();
    }).pipe(Effect.provide(testDatabase)),
  );
});

describe("quietHandoffAwareness", () => {
  /** A store that fails every read, or counts them. */
  const storeOf = (reads: { count: number }, fails: boolean) =>
    ({
      getThreadRecords: () =>
        Effect.suspend(() => {
          reads.count += 1;
          return fails
            ? Effect.fail(new ProjectionStore.ProjectionStoreReadError({ threadId }))
            : Effect.succeed({ runs: [run()], messages: [] });
        }),
    }) as unknown as ProjectionStore.ProjectionStoreV2["Service"];

  it.effect("a failed read leaves the row as upstream read it", () =>
    Effect.gen(function* () {
      const reads = { count: 0 };
      const threads = quietHandoffAwareness(
        threadsOf(awarenessThread(handoffRunId)),
        storeOf(reads, true),
      );
      const snapshot = yield* threads.getAgentAwarenessSnapshot(threadId);
      expect(snapshot.threads[0]?.latestRunStopRequested).toBe(false);
      expect(reads.count).toBe(1);
    }),
  );

  it.effect("an ordinary run costs one read; the catch-up list none", () =>
    Effect.gen(function* () {
      const reads = { count: 0 };
      const threads = quietHandoffAwareness(
        threadsOf(awarenessThread(runId)),
        storeOf(reads, false),
      );
      expect((yield* threads.getAgentAwarenessSnapshot(threadId)).threads[0]).toEqual(
        awarenessThread(runId),
      );
      expect(reads.count).toBe(1);
      expect((yield* threads.getAgentAwarenessSnapshot()).threads[0]).toEqual(
        awarenessThread(runId),
      );
      expect(reads.count).toBe(1);
    }),
  );
});

describe("isQuietHandoffRun", () => {
  it("needs the handoff id, the server as sender, a completed run and no message of the person's", () => {
    const quiet = (
      patch: Partial<OrchestrationV2Run>,
      messages: ReadonlyArray<OrchestrationV2ConversationMessage> = [handoffMessage],
    ) => isQuietHandoffRun(threadId, { ...handoffRun, ...patch }, messages);
    expect(quiet({})).toBe(true);
    // An old run without the mark: an ordinary message id.
    expect(quiet({ userMessageId: MessageId.make("message-1") }, [userMessage])).toBe(false);
    // The handoff id, but not sent by the server as an agent.
    expect(quiet({}, [{ ...handoffMessage, createdBy: "user", creationSource: "web" }])).toBe(
      false,
    );
    expect(quiet({}, [{ ...handoffMessage, creationSource: "mcp" }])).toBe(false);
    // Another thread's handoff id.
    const otherId = MessageId.make(`handoff:other-thread:${runId}`);
    expect(quiet({ userMessageId: otherId }, [{ ...handoffMessage, id: otherId }])).toBe(false);
    // Not completed: failed, stopped or still running.
    for (const status of ["failed", "interrupted", "running"] as const) {
      expect(quiet({ status })).toBe(false);
    }
    // Its message is missing.
    expect(quiet({}, [])).toBe(false);
  });
});
