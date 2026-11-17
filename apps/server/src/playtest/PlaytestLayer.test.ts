/**
 * Fork (playtest): what the worker takes from thread events, and its end notice on the real
 * thread path (orchestrator and event sink over an in-memory database).
 */
import { assert, describe, it } from "@effect/vitest";
import {
  CommandId,
  EventId,
  MessageId,
  type OrchestrationV2DomainEvent,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ThreadId,
  TurnItemId,
} from "@t3tools/contracts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import type * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import { CodexProviderCapabilitiesV2 } from "../orchestration-v2/Adapters/CodexAdapterV2.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as EffectOutbox from "../orchestration-v2/EffectOutbox.ts";
import * as EventStore from "../orchestration-v2/EventStore.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ProviderAdapterRegistry from "../orchestration-v2/ProviderAdapterRegistry.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as ProviderReplayHarness from "../orchestration-v2/testkit/ProviderReplayHarness.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ServerSettings from "../serverSettings.ts";
import { makeNotify, personActivity } from "./PlaytestLayer.ts";

const THREAD = ThreadId.make("thread-a");
const AT = DateTime.makeUnsafe("2026-11-17T11:00:00.000Z");

const message = (
  role: "user" | "assistant" | "system",
  createdBy: "user" | "agent" | "system",
): OrchestrationV2DomainEvent => ({
  id: EventId.make("event-1"),
  type: "message.updated",
  threadId: THREAD,
  occurredAt: AT,
  payload: {
    id: MessageId.make("message-1"),
    threadId: THREAD,
    runId: null,
    nodeId: null,
    role,
    text: "hello",
    attachments: [],
    streaming: false,
    createdBy,
    creationSource: "web",
    createdAt: AT,
    updatedAt: AT,
  },
});

describe("PlaytestLayer", () => {
  it("only a message the person wrote is activity, at the time it was written", () => {
    assert.deepStrictEqual(personActivity(message("user", "user")), {
      threadId: THREAD,
      atMs: DateTime.toEpochMillis(AT),
    });
    // An agent's message (a delegate's report, T3's own playtest message) and replies are not.
    assert.isNull(personActivity(message("user", "agent")));
    assert.isNull(personActivity(message("user", "system")));
    assert.isNull(personActivity(message("assistant", "user")));
    assert.isNull(
      personActivity({
        id: EventId.make("event-2"),
        type: "thread.archived",
        threadId: THREAD,
        occurredAt: AT,
      } as unknown as OrchestrationV2DomainEvent),
    );
  });
});

const instanceId = ProviderInstanceId.make("codex");
const modelSelection = { instanceId, model: "gpt-5.1-codex" };
const PROJECT = ProjectId.make("project:playtest-notice");
const providerAdapter = {
  instanceId,
  driver: ProviderDriverKind.make("codex"),
  getCapabilities: () => Effect.succeed(CodexProviderCapabilitiesV2),
  planSelectionTransition: () => Effect.succeed({ type: "apply_on_next_turn" as const }),
  openSession: () => Effect.die("Runs here never reach a provider"),
} as ProviderAdapter.ProviderAdapterV2["Service"];
const layerDatabase = SqlitePersistence.layerMemory;
const layerThreads = ThreadManagementService.layer.pipe(
  Layer.provideMerge(
    Layer.mergeAll(
      layerDatabase,
      ProjectionStore.layer.pipe(Layer.provide(layerDatabase)),
      EventStore.layer.pipe(Layer.provide(layerDatabase)),
      EffectOutbox.layer.pipe(Layer.provide(layerDatabase)),
      ProviderReplayHarness.layerWithRegistry(
        { name: "playtest-notice" },
        ProviderAdapterRegistry.layerFromAdapters([providerAdapter]),
        { databaseLayer: layerDatabase, runEffectWorker: false },
      ),
    ),
  ),
  Layer.provideMerge(ServerSettings.layerTest()),
  Layer.provideMerge(CommandReceiptStore.layer.pipe(Layer.provide(layerDatabase))),
  Layer.provideMerge(IdAllocator.layer),
);

const dispatch = (command: Parameters<Orchestrator.OrchestratorV2["Service"]["dispatch"]>[0]) =>
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    yield* orchestrator.dispatch(command);
  });
const createThread = (threadId: ThreadId) =>
  dispatch({
    type: "thread.create",
    commandId: CommandId.make(`create:${threadId}`),
    threadId,
    projectId: PROJECT,
    title: threadId,
    modelSelection,
    runtimeMode: "full-access",
    interactionMode: "default",
    branch: null,
    worktreePath: null,
    createdBy: "user",
    creationSource: "web",
  });
const projection = (threadId: ThreadId) =>
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    return yield* orchestrator.getThreadProjection(threadId);
  });
const notice = (threadId: ThreadId, text = "The playtest of dev-1 ended.") => ({
  threadId,
  noticeId: "playtest-pt_aaaaaaaaaa-end",
  title: "Playtest ended",
  level: "info" as const,
  text,
});

describe("PlaytestLayer end notice", () => {
  it.effect("is one system notice in the thread, starts no turn, and is no message", () =>
    Effect.gen(function* () {
      const threadId = ThreadId.make("thread:playtest-notice");
      yield* createThread(threadId);
      yield* dispatch({
        type: "message.dispatch",
        commandId: CommandId.make("send:hello"),
        threadId,
        messageId: MessageId.make("message:hello"),
        text: "hello",
        attachments: [],
        dispatchMode: { type: "start_immediately" },
        createdBy: "user",
        creationSource: "web",
      });
      const before = yield* projection(threadId);
      assert.strictEqual(before.runs.length, 1);

      const notify = yield* makeNotify();
      assert.strictEqual(yield* notify(notice(threadId)), "delivered");
      // A retry (the same id, a newer text) stays one item.
      assert.strictEqual(
        yield* notify(notice(threadId, "The playtest of dev-1 ended!")),
        "delivered",
      );

      const after = yield* projection(threadId);
      const notices = after.turnItems.filter((item) => item.type === "system_notice");
      assert.strictEqual(notices.length, 1);
      assert.deepInclude(notices[0], {
        id: TurnItemId.make("playtest-pt_aaaaaaaaaa-end"),
        title: "Playtest ended",
        level: "info",
        message: "The playtest of dev-1 ended!",
        runId: before.runs[0]!.id,
      });
      // No run, no message: nothing reaches an agent.
      assert.deepStrictEqual(
        after.runs.map((run) => [run.id, run.status]),
        before.runs.map((run) => [run.id, run.status]),
      );
      assert.deepStrictEqual(
        after.messages.map((message) => message.id),
        before.messages.map((message) => message.id),
      );
    }).pipe(Effect.provide(layerThreads)),
  );

  it.effect("goes to a thread with no run yet; an archived or unknown thread drops it", () =>
    Effect.gen(function* () {
      const fresh = ThreadId.make("thread:playtest-fresh");
      const archived = ThreadId.make("thread:playtest-archived");
      yield* createThread(fresh);
      yield* createThread(archived);
      yield* dispatch({
        type: "thread.archive",
        commandId: CommandId.make(`archive:${archived}`),
        threadId: archived,
      });
      const notify = yield* makeNotify();

      assert.strictEqual(yield* notify(notice(fresh)), "delivered");
      const shown = yield* projection(fresh);
      assert.strictEqual(shown.runs.length, 0);
      assert.strictEqual(shown.messages.length, 0);
      assert.strictEqual(shown.turnItems.filter((item) => item.type === "system_notice").length, 1);

      assert.strictEqual(yield* notify(notice(archived)), "dropped");
      assert.strictEqual(
        (yield* projection(archived)).turnItems.filter((item) => item.type === "system_notice")
          .length,
        0,
      );
      assert.strictEqual(yield* notify(notice(ThreadId.make("thread:nobody"))), "dropped");
    }).pipe(Effect.provide(layerThreads)),
  );
});
