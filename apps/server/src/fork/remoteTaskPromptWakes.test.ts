/**
 * Fork: the parent of a task on a linked machine is woken once per prompt its thread there starts
 * waiting on (fork/remoteTaskPromptWakes.ts). A fake engine keeps the thread's runs and its
 * command receipts (a command id already accepted changes nothing again, as the engine's receipts
 * do), so a poll again and a restart are read against the same state.
 */
import { assert, describe, it } from "@effect/vitest";
import {
  type CommandId,
  type EnvironmentId,
  NodeId,
  type OrchestrationV2ServerCommand,
  ProviderInstanceId,
  RunId,
  RuntimeRequestId,
  ThreadId,
  TurnItemId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";

import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import type * as RemoteTaskPrompts from "./remoteTaskPrompts.ts";
import * as RemoteTaskPromptWakes from "./remoteTaskPromptWakes.ts";

const parentThreadId = ThreadId.make("thread:prompt-wakes-parent");
const taskId = NodeId.make("node:prompt-wakes-task");
const parentRun = RunId.make("run:prompt-wakes-parent");
const box = "environment:prompt-wakes-box" as EnvironmentId;
const childThread = ThreadId.make("thread:prompt-wakes-child");
const question = RuntimeRequestId.make("request:prompt-wakes-question");

const NOTHING: RemoteTaskPrompts.TaskPrompts = {
  found: true,
  questions: [],
  approval: null,
  secrets: [],
};
const asking = (requestId = question): RemoteTaskPrompts.TaskPrompts => ({
  ...NOTHING,
  questions: [{ requestId, runId: null }],
});

type Command = OrchestrationV2ServerCommand;

/** The parent's thread and the engine's receipts, shared by every watcher (each start). */
const makeWorld = (options: { readonly blockingWait?: boolean } = {}) => {
  const world = {
    there: { type: "known", prompts: NOTHING } as RemoteTaskPrompts.PromptsRead,
    asks: 0,
    open: true,
    /** The wait already set the wake itself, after the watcher read the task. */
    waitEndedThere: false,
    task: {
      id: taskId,
      threadId: parentThreadId,
      runId: parentRun,
      origin: "app_owned",
      childThreadId: null,
      remoteChild: { environmentId: box, threadId: childThread, label: "box" },
      providerInstanceId: ProviderInstanceId.make("codex"),
      prompt: "Ship it on the box.",
      title: "Ship",
      completionWake: options.blockingWait === true ? "settled_only" : "always",
      status: "running",
      result: null as string | null,
    },
    runs: [
      {
        id: parentRun,
        status: options.blockingWait === true ? "running" : "completed",
        userMessageId: null as string | null,
      },
    ],
    receipts: new Map<CommandId, Command>(),
    /** Every command id sent, accepted or not. */
    sent: [] as Array<CommandId>,
    /** Accepted commands, in order: what the engine did. */
    accepted: [] as Array<Command>,
  };
  const dispatch = (command: Command) =>
    Effect.suspend(() => {
      world.sent.push(command.commandId);
      if (world.receipts.has(command.commandId)) return Effect.succeed({ sequence: 1 } as never);
      if (command.type === "delegated_task.wake-policy") {
        if (world.waitEndedThere || world.task.completionWake === command.completionWake) {
          return Effect.fail({ _tag: "OrchestratorCommandInvariantError" } as never);
        }
        world.task.completionWake = command.completionWake;
      }
      if (command.type === "message.dispatch") {
        world.runs.push({
          id: RunId.make(`run:${command.messageId}`),
          status: "queued",
          userMessageId: command.messageId,
        });
      }
      if (command.type === "queued-run.cancel") {
        const run = world.runs.find((candidate) => candidate.id === command.runId);
        if (run !== undefined) run.status = "cancelled";
      }
      world.receipts.set(command.commandId, command);
      world.accepted.push(command);
      return Effect.succeed({ sequence: 1 } as never);
    });
  const layer = Layer.mergeAll(
    Layer.mock(ProjectionStore.ProjectionStoreV2)({
      getOpenRemoteDelegatedTasks: Effect.sync(() =>
        world.open ? [{ parentThreadId, taskId }] : [],
      ),
    }),
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadRecords: () =>
        Effect.sync(
          () =>
            ({
              thread: {
                id: parentThreadId,
                archivedAt: null,
                deletedAt: null,
                modelSelection: { instanceId: ProviderInstanceId.make("codex"), model: "gpt-5.4" },
              },
              subagents: [{ ...world.task }],
              runs: world.runs.map((run) => ({ ...run })),
            }) as never,
        ),
      dispatch: dispatch as never,
    }),
    Layer.mock(CommandReceiptStore.CommandReceiptStoreV2)({
      getByCommandId: (commandId) =>
        Effect.succeed(
          world.receipts.has(commandId)
            ? Option.some({ status: "accepted" } as never)
            : Option.none(),
        ),
    }),
  );
  const read: RemoteTaskPrompts.ReadRemotePrompts = () =>
    Effect.sync(() => {
      world.asks += 1;
      return world.there;
    });
  /** A start of the server: a new watcher on the same thread and receipts. */
  const start = RemoteTaskPromptWakes.make(read).pipe(Effect.provide(layer));
  const of = <T extends Command["type"]>(type: T) =>
    world.accepted.filter(
      (command): command is Extract<Command, { type: T }> => command.type === type,
    );
  return { world, start, of };
};

describe("a parent whose task on a linked machine waits there", () => {
  it.effect("is woken once for a question, however often it is read", () =>
    Effect.gen(function* () {
      const { world, start, of } = makeWorld();
      const watcher = yield* start;
      yield* watcher.tick;
      assert.deepStrictEqual(of("message.dispatch"), []);
      world.there = { type: "known", prompts: asking() };
      yield* watcher.tick;
      yield* watcher.tick;
      yield* watcher.tick;
      const [wake, ...more] = of("message.dispatch");
      assert.deepStrictEqual(more, []);
      assert.strictEqual(
        wake?.commandId,
        `server:remote-task-prompt:${parentThreadId}:${box}:question:${question}`,
      );
      assert.deepStrictEqual(wake?.dispatchMode, { type: "queue_after_active" });
      assert.deepStrictEqual(wake?.notification?.source, {
        kind: "delegated_task",
        taskIds: [taskId],
      });
      assert.include(
        wake?.text,
        `environmentId ${box}, threadId ${childThread}, and requestId ${question}`,
      );
      assert.strictEqual(world.asks, 4);
      // Sent once, not resent on each poll for the engine to drop.
      assert.deepStrictEqual(world.sent, [wake?.commandId]);
    }),
  );

  it.effect("a restart reads it again and sends no second wake", () =>
    Effect.gen(function* () {
      const { world, start, of } = makeWorld();
      world.there = { type: "known", prompts: asking() };
      yield* (yield* start).tick;
      // The server stops and starts: its new watcher remembers nothing, the engine its receipts.
      yield* (yield* start).tick;
      assert.strictEqual(of("message.dispatch").length, 1);
      assert.strictEqual(world.runs.filter((run) => run.status === "queued").length, 1);
    }),
  );

  it.effect("answered there: its queued wake is cancelled, none is sent again", () =>
    Effect.gen(function* () {
      const { world, start, of } = makeWorld();
      const watcher = yield* start;
      world.there = { type: "known", prompts: asking() };
      yield* watcher.tick;
      world.there = { type: "known", prompts: NOTHING };
      yield* watcher.tick;
      yield* watcher.tick;
      assert.strictEqual(of("message.dispatch").length, 1);
      assert.strictEqual(of("queued-run.cancel").length, 1);
      assert.deepStrictEqual(
        world.runs.map((run) => run.status),
        ["completed", "cancelled"],
      );
      // A new question later is a new prompt: one wake for it.
      world.there = {
        type: "known",
        prompts: asking(RuntimeRequestId.make("request:prompt-wakes-second")),
      };
      yield* watcher.tick;
      yield* watcher.tick;
      assert.strictEqual(of("message.dispatch").length, 2);
    }),
  );

  it.effect.each([
    { reason: "unreachable" as const },
    { reason: "older" as const },
    { reason: "unlinked" as const },
  ])("a machine that cannot be asked ($reason) wakes no one and clears nothing", ({ reason }) =>
    Effect.gen(function* () {
      const { world, start, of } = makeWorld();
      const watcher = yield* start;
      world.there = { type: "unknown", reason };
      yield* watcher.tick;
      assert.deepStrictEqual(world.accepted, []);
      // Woken before the link went down: the wake stays queued while nothing is known.
      world.there = { type: "known", prompts: asking() };
      yield* watcher.tick;
      world.there = { type: "unknown", reason };
      yield* watcher.tick;
      assert.strictEqual(of("message.dispatch").length, 1);
      assert.deepStrictEqual(of("queued-run.cancel"), []);
      // Back, still asking: still the one wake.
      world.there = { type: "known", prompts: asking() };
      yield* watcher.tick;
      assert.strictEqual(of("message.dispatch").length, 1);
    }),
  );

  it.effect("a thread there that is gone wakes no one and clears nothing either", () =>
    Effect.gen(function* () {
      const { world, start, of } = makeWorld();
      const watcher = yield* start;
      world.there = { type: "known", prompts: asking() };
      yield* watcher.tick;
      world.there = { type: "known", prompts: { ...NOTHING, found: false } };
      yield* watcher.tick;
      assert.strictEqual(of("message.dispatch").length, 1);
      assert.deepStrictEqual(of("queued-run.cancel"), []);
    }),
  );

  it.effect("an approval and a secret each wake once; the secret by label and reason only", () =>
    Effect.gen(function* () {
      const { world, start, of } = makeWorld();
      const watcher = yield* start;
      world.there = {
        type: "known",
        prompts: {
          ...NOTHING,
          approval: {
            requestId: RuntimeRequestId.make("request:prompt-wakes-approval"),
            kind: "command",
            runId: RunId.make("run:there"),
          },
          secrets: [
            {
              itemId: TurnItemId.make("item:prompt-wakes-secret"),
              label: "Deploy token",
              reason: "to ship",
            },
          ],
        },
      };
      yield* watcher.tick;
      yield* watcher.tick;
      const wakes = of("message.dispatch");
      assert.strictEqual(wakes.length, 2);
      assert.include(wakes[0]?.text, "waits for a command approval");
      assert.include(wakes[1]?.text, 'asks for the secret "Deploy token" (to ship)');
    }),
  );

  it.effect("its task ended: a wake still queued is cancelled", () =>
    Effect.gen(function* () {
      const { world, start, of } = makeWorld();
      const watcher = yield* start;
      world.there = { type: "known", prompts: asking() };
      yield* watcher.tick;
      world.open = false;
      yield* watcher.tick;
      assert.strictEqual(of("queued-run.cancel").length, 1);
      assert.strictEqual(watcher.handled().size, 0);
    }),
  );

  it.effect("a blocking wait is ended instead: no message, also after a restart", () =>
    Effect.gen(function* () {
      const { world, start, of } = makeWorld({ blockingWait: true });
      world.there = { type: "known", prompts: asking() };
      yield* (yield* start).tick;
      const [ended, ...more] = of("delegated_task.wake-policy");
      assert.deepStrictEqual(more, []);
      assert.strictEqual(ended?.completionWake, "always");
      assert.strictEqual(
        ended?.commandId,
        `server:remote-task-prompt-wait:${parentThreadId}:${box}:question:${question}`,
      );
      // The wait read it and ended; the server restarts while the question is still open there.
      world.runs[0]!.status = "completed";
      yield* (yield* start).tick;
      assert.deepStrictEqual(of("message.dispatch"), []);
    }),
  );

  it.effect("a wait that already ended by itself: the prompt goes as a message", () =>
    Effect.gen(function* () {
      const { world, start, of } = makeWorld({ blockingWait: true });
      // The watcher still sees the wait, but it set the wake itself in between: the change is refused.
      world.waitEndedThere = true;
      const watcher = yield* start;
      world.there = { type: "known", prompts: asking() };
      yield* watcher.tick;
      yield* watcher.tick;
      assert.deepStrictEqual(of("delegated_task.wake-policy"), []);
      assert.strictEqual(of("message.dispatch").length, 1);
    }),
  );
});
