import { assert, it } from "@effect/vitest";
import {
  CommandId,
  MessageId,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import type * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";

import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ServerSettings from "../serverSettings.ts";
import { CodexProviderCapabilitiesV2 } from "../orchestration-v2/Adapters/CodexAdapterV2.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as EffectOutbox from "../orchestration-v2/EffectOutbox.ts";
import * as EventStore from "../orchestration-v2/EventStore.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ProviderAdapterRegistry from "../orchestration-v2/ProviderAdapterRegistry.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as ProviderReplayHarness from "../orchestration-v2/testkit/ProviderReplayHarness.ts";
import * as WaitDelivery from "./WaitDelivery.ts";

/** The real send path: thread tools, orchestrator and receipts over an in-memory database. */
const instanceId = ProviderInstanceId.make("codex");
const modelSelection = { instanceId, model: "gpt-5.1-codex" };
const PROJECT = ProjectId.make("project:wait-delivery");
const adapter = {
  instanceId,
  driver: ProviderDriverKind.make("codex"),
  getCapabilities: () => Effect.succeed(CodexProviderCapabilitiesV2),
  planSelectionTransition: () => Effect.succeed({ type: "apply_on_next_turn" as const }),
  openSession: () => Effect.die("Runs here never reach a provider"),
} as ProviderAdapter.ProviderAdapterV2["Service"];
const layerDatabase = SqlitePersistence.layerMemory;
const layerTest = ThreadManagementService.layer.pipe(
  Layer.provideMerge(
    Layer.mergeAll(
      layerDatabase,
      ProjectionStore.layer.pipe(Layer.provide(layerDatabase)),
      EventStore.layer.pipe(Layer.provide(layerDatabase)),
      EffectOutbox.layer.pipe(Layer.provide(layerDatabase)),
      ProviderReplayHarness.layerWithRegistry(
        { name: "wait-delivery" },
        ProviderAdapterRegistry.layerFromAdapters([adapter]),
        { databaseLayer: layerDatabase, runEffectWorker: false },
      ),
    ),
  ),
  Layer.provideMerge(ServerSettings.layerTest()),
  Layer.provideMerge(CommandReceiptStore.layer.pipe(Layer.provide(layerDatabase))),
);

const createThread = (threadId: ThreadId) =>
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    yield* orchestrator.dispatch({
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
  });

const userSend = (threadId: ThreadId, text: string) =>
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    yield* orchestrator.dispatch({
      type: "message.dispatch",
      commandId: CommandId.make(`send:${threadId}:${text}`),
      threadId,
      messageId: MessageId.make(`message:${threadId}:${text}`),
      text,
      attachments: [],
      dispatchMode: { type: "start_immediately" },
      createdBy: "user",
      creationSource: "web",
    });
  });

const delegate = (parentThreadId: ThreadId, task: string) =>
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    const parentRun = (yield* orchestrator.getThreadProjection(parentThreadId)).runs.at(-1)!;
    yield* orchestrator.dispatch({
      type: "delegated_task.request",
      commandId: CommandId.make(`delegate:${task}`),
      parentThreadId,
      parentRunId: parentRun.id,
      parentNodeId: parentRun.rootNodeId!,
      task,
      modelSelection,
      runtimeMode: "full-access",
      interactionMode: "default",
      completionWake: "always",
      createdBy: "agent",
      creationSource: "mcp",
    });
    const projection = yield* orchestrator.getThreadProjection(parentThreadId);
    return projection.subagents.find((candidate) => candidate.prompt === task)!.childThreadId!;
  });

const archive = (threadId: ThreadId) =>
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    yield* orchestrator.dispatch({
      type: "thread.archive",
      commandId: CommandId.make(`archive:${threadId}`),
      threadId,
    });
  });

/** The messages of a thread with this id (0 or 1). */
const copies = (threadId: ThreadId, messageId: string) =>
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    const projection = yield* orchestrator.getThreadProjection(threadId);
    return projection.messages.filter((message) => message.id === messageId).length;
  });

const wake = (threadId: ThreadId, messageId: string, attempt = 0) => ({
  threadId,
  messageId,
  commandId: `${messageId}:${attempt}`,
  text: `[wait ${messageId.slice(5)}] file /x: exists.`,
});

it.effect("U2: the same message sent twice, or with a new command id, is one message", () =>
  Effect.gen(function* () {
    const threadId = ThreadId.make("thread:wait-twice");
    yield* createThread(threadId);
    const delivery = yield* WaitDelivery.make();

    assert.deepStrictEqual(yield* delivery.receiver(threadId), { type: "ok" });
    const message = wake(threadId, "wait:w_aaaaaaaaaa");
    assert.deepStrictEqual(yield* delivery.deliver(message), { type: "delivered" });
    assert.deepStrictEqual(yield* delivery.deliver(message), { type: "delivered" });
    assert.deepStrictEqual(yield* delivery.deliver(wake(threadId, "wait:w_aaaaaaaaaa", 1)), {
      type: "delivered",
    });
    assert.strictEqual(yield* copies(threadId, "wait:w_aaaaaaaaaa"), 1);
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    const [stored] = (yield* orchestrator.getThreadProjection(threadId)).messages;
    assert.strictEqual(stored?.text, message.text);
  }).pipe(Effect.provide(layerTest)),
);

it.effect(
  "U3: a command accepted before a lost answer is not sent again; one held by another thread is refused",
  () =>
    Effect.gen(function* () {
      const threadId = ThreadId.make("thread:wait-receipt");
      const otherThreadId = ThreadId.make("thread:wait-receipt-other");
      yield* createThread(threadId);
      yield* createThread(otherThreadId);
      const orchestrator = yield* Orchestrator.OrchestratorV2;
      const delivery = yield* WaitDelivery.make();
      const dispatch = (target: ThreadId, commandId: string, messageId: string) =>
        orchestrator.dispatch({
          type: "message.dispatch",
          commandId: CommandId.make(commandId),
          threadId: target,
          messageId: MessageId.make(messageId),
          text: "sent before the crash",
          attachments: [],
          dispatchMode: { type: "queue_after_active" },
          createdBy: "agent",
          creationSource: "mcp",
        });

      // The send went through; the server died before it learned so.
      yield* dispatch(threadId, "wait:w_bbbbbbbbbb:0", "wait:w_bbbbbbbbbb");
      assert.deepStrictEqual(yield* delivery.deliver(wake(threadId, "wait:w_bbbbbbbbbb")), {
        type: "delivered",
      });
      assert.strictEqual(yield* copies(threadId, "wait:w_bbbbbbbbbb"), 1);

      // Only the receipt is left (the message is under another id): it still counts as sent.
      yield* dispatch(threadId, "wait:w_cccccccccc:0", "message:elsewhere");
      assert.deepStrictEqual(yield* delivery.deliver(wake(threadId, "wait:w_cccccccccc")), {
        type: "delivered",
      });
      assert.strictEqual(yield* copies(threadId, "wait:w_cccccccccc"), 0);

      // A command id another thread holds is a refusal; the next attempt's id goes through.
      yield* dispatch(otherThreadId, "wait:w_dddddddddd:0", "message:other");
      const refused = yield* delivery.deliver(wake(threadId, "wait:w_dddddddddd"));
      assert.strictEqual(refused.type, "rejected");
      assert.deepStrictEqual(yield* delivery.deliver(wake(threadId, "wait:w_dddddddddd")), refused);
      assert.deepStrictEqual(yield* delivery.deliver(wake(threadId, "wait:w_dddddddddd", 1)), {
        type: "delivered",
      });
      assert.strictEqual(yield* copies(threadId, "wait:w_dddddddddd"), 1);
    }).pipe(Effect.provide(layerTest)),
);

it.effect(
  "an archived subagent hands back to its parent; an archived top thread or an unknown one has none",
  () =>
    Effect.gen(function* () {
      const parentThreadId = ThreadId.make("thread:wait-parent");
      yield* createThread(parentThreadId);
      yield* userSend(parentThreadId, "start");
      const childThreadId = yield* delegate(parentThreadId, "child task");
      const delivery = yield* WaitDelivery.make();
      assert.deepStrictEqual(yield* delivery.receiver(childThreadId), { type: "ok" });

      // Sent before the archive: stays delivered.
      yield* delivery.deliver(wake(childThreadId, "wait:w_eeeeeeeeee"));
      yield* archive(childThreadId);
      const gone = { type: "gone", parentThreadId } as const;
      assert.deepStrictEqual(yield* delivery.receiver(childThreadId), gone);
      assert.deepStrictEqual(
        yield* delivery.deliver(wake(childThreadId, "wait:w_ffffffffff")),
        gone,
      );
      assert.deepStrictEqual(yield* delivery.deliver(wake(childThreadId, "wait:w_eeeeeeeeee")), {
        type: "delivered",
      });
      assert.strictEqual(yield* copies(childThreadId, "wait:w_ffffffffff"), 0);

      const topThreadId = ThreadId.make("thread:wait-top");
      yield* createThread(topThreadId);
      yield* archive(topThreadId);
      const orphan = { type: "gone", parentThreadId: null } as const;
      assert.deepStrictEqual(yield* delivery.receiver(topThreadId), orphan);
      assert.deepStrictEqual(
        yield* delivery.deliver(wake(topThreadId, "wait:w_gggggggggg")),
        orphan,
      );
      const unknown = ThreadId.make("thread:wait-unknown");
      assert.deepStrictEqual(yield* delivery.receiver(unknown), orphan);
      assert.deepStrictEqual(yield* delivery.deliver(wake(unknown, "wait:w_hhhhhhhhhh")), orphan);
    }).pipe(Effect.provide(layerTest)),
);

it.effect("a stopped thread holds the message; a busy one queues it behind its turn", () =>
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    const delivery = yield* WaitDelivery.make();

    const busyThreadId = ThreadId.make("thread:wait-busy");
    yield* createThread(busyThreadId);
    yield* userSend(busyThreadId, "working");
    assert.deepStrictEqual(yield* delivery.deliver(wake(busyThreadId, "wait:w_iiiiiiiiii")), {
      type: "delivered",
    });
    const busy = yield* orchestrator.getThreadProjection(busyThreadId);
    assert.strictEqual(busy.runs.length, 2);
    assert.strictEqual(busy.runs.at(-1)?.status, "queued");
    assert.notStrictEqual(busy.runs[0]?.status, "interrupted");

    const stoppedThreadId = ThreadId.make("thread:wait-stopped");
    yield* createThread(stoppedThreadId);
    yield* userSend(stoppedThreadId, "working");
    yield* orchestrator.dispatch({
      type: "thread.stop",
      commandId: CommandId.make("stop:wait-stopped"),
      threadId: stoppedThreadId,
    });
    const stopped = { type: "stopped", parentThreadId: null } as const;
    assert.deepStrictEqual(yield* delivery.receiver(stoppedThreadId), stopped);
    assert.deepStrictEqual(
      yield* delivery.deliver(wake(stoppedThreadId, "wait:w_jjjjjjjjjj")),
      stopped,
    );
    assert.strictEqual(yield* copies(stoppedThreadId, "wait:w_jjjjjjjjjj"), 0);
  }).pipe(Effect.provide(layerTest)),
);
