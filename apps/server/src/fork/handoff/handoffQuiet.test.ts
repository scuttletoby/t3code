/**
 * Fork: a completed handoff run does not alert through the relay; every other run alerts as before.
 * The relay stand-in uses upstream's `projectThreadAwarenessV2` over upstream's own awareness read
 * (the shell carries the mark), wired as server.ts wires it, over a store that outlives each "server".
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
} from "@t3tools/contracts";
import { projectThreadAwarenessV2 } from "@t3tools/shared/agentAwareness";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as SqlClient from "effect/sql/SqlClient";

import * as ServerConfig from "../../config.ts";
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
import { HANDOFF_LOUD_SUFFIX, HANDOFF_RETRY_SUFFIX } from "./handoffFacts.ts";
import { HANDOFF_SETTLE_MS } from "./handoffQuiet.ts";
import { isQuietHandoffRun, RELAY_COMPLETION_HOLD_MS } from "./handoffShell.ts";

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
/** Sent after the settle, as the service does: run-1's alert went out first. */
const handoffRun = run({
  id: handoffRunId,
  ordinal: 2,
  userMessageId: handoffMessageId,
  requestedAt: at(HANDOFF_SETTLE_MS),
  startedAt: at(HANDOFF_SETTLE_MS),
  completedAt: at(MINUTE_MS),
});

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

/** Upstream's thread reads for the relay: the store's own awareness read, nothing of the fork's. */
const upstreamThreads = (store: ProjectionStore.ProjectionStoreV2["Service"]) =>
  ({
    getAgentAwarenessSnapshot: store.getAgentAwarenessSnapshot,
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
              const thread = snapshot.threads[0];
              const state =
                thread === undefined
                  ? null
                  : projectThreadAwarenessV2({
                      environmentId,
                      project: { title: "Project" },
                      thread,
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
 * through the relay as server.ts wires it.
 */
const publishOnce = () =>
  Effect.gen(function* () {
    const published: Array<string | null> = [];
    const sql = yield* SqlClient.SqlClient;
    const store = yield* ProjectionStore.ProjectionStoreV2;
    const gate = yield* WorkAdmission.makeWorkAdmissionGate({ heldFromBoot: false });
    const relayLayer = relayStandIn(published);
    yield* AgentAwarenessRelay.AgentAwarenessRelay.use((relay) =>
      relay.publishThread(threadId),
    ).pipe(
      Effect.provide(
        ForkLayers.holdWorkAgentActivity(relayLayer).pipe(
          Layer.provide(
            Layer.succeed(ThreadManagement.ThreadManagementService, upstreamThreads(store)),
          ),
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
  it.effect("the person's run alerts; the completed handoff run after it does not", () =>
    Effect.gen(function* () {
      yield* seed({ runs: [run()], messages: [userMessage] });
      expect(yield* publishOnce()).toBe("completed");
      yield* seed({ runs: [handoffRun], messages: [handoffMessage] });
      expect(yield* publishOnce()).toBeNull();
    }).pipe(Effect.provide(testDatabase)),
  );

  it.effect("runs again after a restart: the mark is stored, so it stays quiet", () =>
    Effect.gen(function* () {
      yield* seed({ runs: [run(), handoffRun], messages: [userMessage, handoffMessage] });
      // Two servers in turn over the same database: nothing is kept in memory between them.
      expect(yield* publishOnce()).toBeNull();
      expect(yield* publishOnce()).toBeNull();
    }).pipe(Effect.provide(testDatabase)),
  );

  it.effect("a handoff sent as one that alerts does alert, after a restart too", () =>
    Effect.gen(function* () {
      const loudId = MessageId.make(`${handoffMessageId}${HANDOFF_LOUD_SUFFIX}`);
      yield* seed({
        runs: [run(), { ...handoffRun, userMessageId: loudId }],
        messages: [userMessage, { ...handoffMessage, id: loudId }],
      });
      // The mark is in the stored message id: each server reads it fresh.
      expect(yield* publishOnce()).toBe("completed");
      expect(yield* publishOnce()).toBe("completed");
    }).pipe(Effect.provide(testDatabase)),
  );

  it.effect("the next real turn alerts as before, after a restart too", () =>
    Effect.gen(function* () {
      yield* seed({ runs: [run(), handoffRun], messages: [userMessage, handoffMessage] });
      expect(yield* publishOnce()).toBeNull();
      const userMessageId = MessageId.make("message-3");
      yield* seed({
        runs: [run({ id: userRunId, ordinal: 3, userMessageId })],
        messages: [message({ id: userMessageId, runId: userRunId })],
      });
      expect(yield* publishOnce()).toBe("completed");
    }).pipe(Effect.provide(testDatabase)),
  );

  it.effect("a failed handoff run alerts, and so does its retry that succeeds", () =>
    Effect.gen(function* () {
      const retryId = MessageId.make(`${handoffMessageId}${HANDOFF_RETRY_SUFFIX}`);
      const retryRunId = RunId.make("run-2-retry");
      yield* seed({
        runs: [run(), { ...handoffRun, status: "failed" }],
        messages: [userMessage, handoffMessage],
      });
      expect(yield* publishOnce()).toBe("failed");
      yield* seed({
        runs: [
          run({
            id: retryRunId,
            ordinal: 3,
            userMessageId: retryId,
            requestedAt: at(MINUTE_MS),
            completedAt: at(2 * MINUTE_MS),
          }),
        ],
        messages: [{ ...handoffMessage, id: retryId, runId: retryRunId }],
      });
      // The last alert said "failed": the work went on, so the retry's finish says so.
      expect(yield* publishOnce()).toBe("completed");
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
      expect(yield* publishOnce()).toBe("completed");
    }).pipe(Effect.provide(testDatabase)),
  );

  it.effect("old, empty or half-written state alerts as before", () =>
    Effect.gen(function* () {
      // Empty: no thread stored at all (deleted, or never projected): nothing to publish.
      expect(yield* publishOnce()).toBeNull();
      // No run yet: upstream publishes nothing, and nothing changes that.
      yield* seed({ runs: [], messages: [] });
      expect(yield* publishOnce()).toBeNull();
      // Old: a run from before handoffs, with an ordinary message.
      yield* seed({ runs: [run()], messages: [userMessage] });
      expect(yield* publishOnce()).toBe("completed");
      // Half-written: the handoff run is stored but its handoff message is not.
      yield* seed({ runs: [handoffRun], messages: [] });
      expect(yield* publishOnce()).toBe("completed");
    }).pipe(Effect.provide(testDatabase)),
  );
});

describe("isQuietHandoffRun", () => {
  it("needs the handoff id, the server as sender, a completed run and no message of the person's", () => {
    const quiet = (
      patch: Partial<OrchestrationV2Run>,
      messages: ReadonlyArray<OrchestrationV2ConversationMessage> = [handoffMessage],
      source: OrchestrationV2Run | null = run(),
    ) => isQuietHandoffRun(threadId, { ...handoffRun, ...patch }, messages, source ?? undefined);
    expect(quiet({})).toBe(true);
    // Requested inside the relay's hold after the person's run: the person's alert was dropped.
    expect(quiet({ requestedAt: at(RELAY_COMPLETION_HOLD_MS) })).toBe(true);
    expect(quiet({ requestedAt: at(RELAY_COMPLETION_HOLD_MS - 1) })).toBe(false);
    expect(quiet({ requestedAt: at() })).toBe(false);
    // The source run is missing, is another run, or never finished.
    expect(quiet({}, [handoffMessage], null)).toBe(false);
    expect(quiet({}, [handoffMessage], run({ id: userRunId }))).toBe(false);
    expect(quiet({}, [handoffMessage], run({ completedAt: null }))).toBe(false);
    // The retry of a failed send: the last alert said it failed.
    const retryId = MessageId.make(`${handoffMessageId}${HANDOFF_RETRY_SUFFIX}`);
    expect(quiet({ userMessageId: retryId }, [{ ...handoffMessage, id: retryId }])).toBe(false);
    // Sent as one that alerts: the settle did not see the person's finish shown all the way.
    const loudId = MessageId.make(`${handoffMessageId}${HANDOFF_LOUD_SUFFIX}`);
    expect(quiet({ userMessageId: loudId }, [{ ...handoffMessage, id: loudId }])).toBe(false);
    const loudRetryId = MessageId.make(`${loudId}${HANDOFF_RETRY_SUFFIX}`);
    expect(quiet({ userMessageId: loudRetryId }, [{ ...handoffMessage, id: loudRetryId }])).toBe(
      false,
    );
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
