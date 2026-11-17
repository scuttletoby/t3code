/**
 * Fork: the handoff busy check in `Orchestrator.ts`, against the real orchestrator. A handoff lands
 * only at a quiet thread still at its source run; a racing message wins and nothing is lost. After
 * a handoff, child results and PR wakes reach the new session.
 */
import { assert, it } from "@effect/vitest";
import {
  CommandId,
  EventId,
  MessageId,
  type OrchestrationV2Run,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import type * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import * as NodeServices from "@effect/platform-node/NodeServices";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";

import * as SqlitePersistence from "../../persistence/Sqlite.ts";
import * as ServerSettings from "../../serverSettings.ts";
import { CodexProviderCapabilitiesV2 } from "../../orchestration-v2/Adapters/CodexAdapterV2.ts";
import * as CommandReceiptStore from "../../orchestration-v2/CommandReceiptStore.ts";
import * as EffectOutbox from "../../orchestration-v2/EffectOutbox.ts";
import * as EventStore from "../../orchestration-v2/EventStore.ts";
import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ProviderAdapterRegistry from "../../orchestration-v2/ProviderAdapterRegistry.ts";
import * as ProviderReplayHarness from "../../orchestration-v2/testkit/ProviderReplayHarness.ts";

const instanceId = ProviderInstanceId.make("codex");
const modelSelection = { instanceId, model: "gpt-5.1-codex" };
const adapter = {
  instanceId,
  driver: ProviderDriverKind.make("codex"),
  getCapabilities: () => Effect.succeed(CodexProviderCapabilitiesV2),
  planSelectionTransition: () => Effect.succeed({ type: "apply_on_next_turn" as const }),
  openSession: () => Effect.die("Runs here never reach a provider"),
} as ProviderAdapter.ProviderAdapterV2["Service"];
const layerDatabase = SqlitePersistence.layerMemory;
const layerTest = Layer.mergeAll(
  layerDatabase,
  ProjectionStore.layer.pipe(Layer.provide(layerDatabase)),
  EventStore.layer.pipe(Layer.provide(layerDatabase)),
  EffectOutbox.layer.pipe(Layer.provide(layerDatabase)),
  ProviderReplayHarness.layerWithRegistry(
    { name: "handoff-busy-check" },
    ProviderAdapterRegistry.layerFromAdapters([adapter]),
    { databaseLayer: layerDatabase, runEffectWorker: false },
  ),
).pipe(
  Layer.provideMerge(ServerSettings.layerTest()),
  Layer.provideMerge(CommandReceiptStore.layer.pipe(Layer.provide(layerDatabase))),
);

const runs = (threadId: ThreadId) =>
  Orchestrator.OrchestratorV2.use((orchestrator) =>
    orchestrator.getThreadProjection(threadId),
  ).pipe(Effect.map((projection) => projection.runs));

const send = (threadId: ThreadId, text: string) =>
  Orchestrator.OrchestratorV2.use((orchestrator) =>
    orchestrator.dispatch({
      type: "message.dispatch",
      commandId: CommandId.make(`send:${threadId}:${text}`),
      threadId,
      messageId: MessageId.make(`message:${threadId}:${text}`),
      text,
      attachments: [],
      dispatchMode: { type: "start_immediately" },
      createdBy: "user",
      creationSource: "web",
    }),
  );

/** What the handoff service sends; `command` changes the command id only (a second trigger). */
const sendHandoff = (threadId: ThreadId, sourceRunId: string, command = "") =>
  Orchestrator.OrchestratorV2.use((orchestrator) =>
    orchestrator.dispatch({
      type: "message.dispatch",
      commandId: CommandId.make(`server:handoff:${threadId}:${sourceRunId}${command}`),
      threadId,
      messageId: MessageId.make(`handoff:${threadId}:${sourceRunId}`),
      text: "[T3 handoff 1] context",
      attachments: [],
      createdBy: "agent",
      creationSource: "server",
      freshContext: true,
      modelSelection,
      dispatchMode: { type: "start_immediately" },
    }),
  );

const complete = (run: OrchestrationV2Run, patch: Partial<OrchestrationV2Run> = {}) =>
  Effect.gen(function* () {
    const projections = yield* ProjectionStore.ProjectionStoreV2;
    const now = yield* DateTime.now;
    yield* projections.apply({
      id: EventId.make(`event:${run.id}:completed:${JSON.stringify(patch)}`),
      type: "run.updated",
      threadId: run.threadId,
      runId: run.id,
      occurredAt: now,
      payload: { ...run, status: "completed", completedAt: now, ...patch },
    });
  });

/** A thread whose first run, from the user, has completed; `whileRunning` acts before that. */
const quietThread = <E = never>(
  threadId: ThreadId,
  whileRunning: (run: OrchestrationV2Run) => Effect.Effect<unknown, E> = () => Effect.void,
) =>
  Effect.gen(function* () {
    yield* Orchestrator.OrchestratorV2.use((orchestrator) =>
      orchestrator.dispatch({
        type: "thread.create",
        commandId: CommandId.make(`create:${threadId}`),
        threadId,
        projectId: ProjectId.make("project:handoff"),
        title: threadId,
        modelSelection,
        runtimeMode: "full-access",
        interactionMode: "default",
        branch: null,
        worktreePath: null,
        createdBy: "user",
        creationSource: "web",
      }),
    );
    yield* send(threadId, "first");
    yield* whileRunning((yield* runs(threadId))[0]!);
    const first = (yield* runs(threadId))[0]!;
    yield* complete(first);
    return first;
  });

const handoffRuns = (all: ReadonlyArray<OrchestrationV2Run>) =>
  all.filter((run) => run.userMessageId.startsWith("handoff:"));

it.effect("calls the busy check from exactly one place in Orchestrator.ts (K14)", () =>
  Effect.gen(function* () {
    const path = yield* Path.Path;
    const fileSystem = yield* FileSystem.FileSystem;
    const source = yield* fileSystem.readFileString(
      yield* path.fromFileUrl(new URL("../../orchestration-v2/Orchestrator.ts", import.meta.url)),
    );
    assert.equal(source.match(/handoffDispatchDropped\(/g)?.length, 1);
  }).pipe(Effect.provide(NodeServices.layer)),
);

it.effect("a racing user message wins and nothing is lost", () =>
  Effect.gen(function* () {
    const threadId = ThreadId.make("thread:handoff-race");
    const first = yield* quietThread(threadId);
    // The service read a quiet thread; the user's message lands before the handoff does.
    yield* send(threadId, "second");
    yield* sendHandoff(threadId, first.id);
    const after = yield* runs(threadId);
    assert.deepEqual(handoffRuns(after), []);
    assert.equal(after.length, 2);
    assert.equal(after[1]?.userMessageId, `message:${threadId}:second`);
    assert.notEqual(after[1]?.status, "interrupted");
    const projection = yield* Orchestrator.OrchestratorV2.use((orchestrator) =>
      orchestrator.getThreadProjection(threadId),
    );
    assert.isTrue(projection.messages.some((message) => message.text === "second"));
    assert.isFalse(projection.messages.some((message) => message.id.startsWith("handoff:")));
  }).pipe(Effect.provide(layerTest)),
);

it.effect("a handoff made from an older run is dropped once a newer run finished", () =>
  Effect.gen(function* () {
    const threadId = ThreadId.make("thread:handoff-older");
    const first = yield* quietThread(threadId);
    yield* send(threadId, "second");
    yield* complete((yield* runs(threadId))[1]!);
    // Quiet again, but the latest run is not the one the handoff was made from.
    yield* sendHandoff(threadId, first.id);
    assert.deepEqual(handoffRuns(yield* runs(threadId)), []);
  }).pipe(Effect.provide(layerTest)),
);

it.effect("a child result due before the handoff lands wins", () =>
  Effect.gen(function* () {
    const threadId = ThreadId.make("thread:handoff-child-first");
    const first = yield* quietThread(threadId);
    // The child finished and its result is on its way: the latest run is still the source run.
    yield* complete(first, {
      delegatedCompletion: {
        disposition: "open",
        nextGeneration: 2,
        delivery: { generation: 1, messageId: MessageId.make("message:due"), taskIds: [] },
      },
    });
    yield* sendHandoff(threadId, first.id);
    assert.deepEqual(handoffRuns(yield* runs(threadId)), []);
  }).pipe(Effect.provide(layerTest)),
);

it.effect("the same run never hands off twice, and the new session gets wakes and results", () =>
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    const threadId = ThreadId.make("thread:handoff-once");
    // A child of the first run, still running at the handoff; it does not block it.
    const first = yield* quietThread(threadId, (running) =>
      orchestrator.dispatch({
        type: "delegated_task.request",
        commandId: CommandId.make("delegate:handoff-child"),
        parentThreadId: threadId,
        parentRunId: running.id,
        parentNodeId: running.rootNodeId!,
        task: "a child still running",
        modelSelection,
        runtimeMode: "full-access",
        interactionMode: "default",
        completionWake: "always",
        createdBy: "agent",
        creationSource: "mcp",
      }),
    );
    yield* sendHandoff(threadId, first.id);
    // The same trigger again (a replayed receipt) and a second trigger (another command id).
    yield* sendHandoff(threadId, first.id);
    yield* sendHandoff(threadId, first.id, ":again");
    const handoffs = handoffRuns(yield* runs(threadId));
    assert.equal(handoffs.length, 1);
    const handoff = handoffs[0]!;
    assert.isTrue(handoff.freshContext);
    assert.notEqual(handoff.providerThreadId, first.providerThreadId);
    const thread = (yield* orchestrator.getThreadProjection(threadId)).thread;
    assert.equal(thread.activeProviderThreadId, handoff.providerThreadId);
    yield* complete(handoff);

    // The child reports: its result goes into the new session.
    const task = (yield* orchestrator.getThreadProjection(threadId)).subagents[0]!;
    const resultId = MessageId.make("message:handoff-child-result");
    yield* complete(first, {
      delegatedCompletion: {
        disposition: "open",
        nextGeneration: 2,
        delivery: { generation: 1, messageId: resultId, taskIds: [task.id] },
      },
    });
    yield* orchestrator.dispatch({
      type: "message.dispatch",
      commandId: CommandId.make("server:handoff-child-result"),
      threadId,
      messageId: resultId,
      text: "Delegated task completed",
      attachments: [],
      createdBy: "agent",
      creationSource: "server",
      dispatchMode: { type: "queue_after_active" },
      delegatedCompletion: { parentRunId: first.id, generation: 1, taskIds: [task.id] },
    });
    const result = (yield* runs(threadId)).find((run) => run.userMessageId === resultId)!;
    assert.equal(result.providerThreadId, handoff.providerThreadId);
    yield* complete(result);

    // A watched PR's wake goes into the new session too.
    const wakeId = MessageId.make("message:pr-watch:handoff");
    yield* orchestrator.dispatch({
      type: "message.dispatch",
      commandId: CommandId.make("server:pr-watch:handoff"),
      threadId,
      messageId: wakeId,
      text: "Pull request #7 checks passed.",
      attachments: [],
      createdBy: "agent",
      creationSource: "server",
      dispatchMode: { type: "queue_after_active" },
      notification: { source: { kind: "monitor" }, outcome: "completed", summary: "#7: passed" },
    });
    const wake = (yield* runs(threadId)).find((run) => run.userMessageId === wakeId)!;
    assert.equal(wake.providerThreadId, handoff.providerThreadId);
  }).pipe(Effect.provide(layerTest)),
);

it.effect.each(["failed", "completed"] as const)(
  "the retry lands only after its own first send failed (first send %s)",
  (outcome) =>
    Effect.gen(function* () {
      const threadId = ThreadId.make(`thread:handoff-retry-${outcome}`);
      const first = yield* quietThread(threadId);
      const retry = (suffix: string) =>
        Orchestrator.OrchestratorV2.use((orchestrator) =>
          orchestrator.dispatch({
            type: "message.dispatch",
            commandId: CommandId.make(`server:handoff:${threadId}:${first.id}:retry1${suffix}`),
            threadId,
            messageId: MessageId.make(`handoff:${threadId}:${first.id}:retry1`),
            text: "[T3 handoff 1] context",
            attachments: [],
            createdBy: "agent",
            creationSource: "server",
            freshContext: true,
            modelSelection,
            dispatchMode: { type: "start_immediately" },
          }),
        );
      // No first send yet: the retry has nothing to follow.
      yield* retry("");
      assert.deepEqual(handoffRuns(yield* runs(threadId)), []);
      yield* sendHandoff(threadId, first.id);
      const handoff = handoffRuns(yield* runs(threadId))[0]!;
      yield* complete(handoff, { status: outcome });
      yield* retry(":after");
      assert.equal(handoffRuns(yield* runs(threadId)).length, outcome === "failed" ? 2 : 1);
      // Only one retry: a failed retry is not followed by another.
      const latest = handoffRuns(yield* runs(threadId)).at(-1)!;
      if (latest.id !== handoff.id) yield* complete(latest, { status: "failed" });
      yield* retry(":again");
      assert.equal(handoffRuns(yield* runs(threadId)).length, outcome === "failed" ? 2 : 1);
    }).pipe(Effect.provide(layerTest)),
);
