/**
 * Fork (issues 386, 625): the reminder of delegated results that never reached their parent. The
 * state question: it runs again (no second message), the server restarted (a fresh service finds
 * the message it sent, and the table finds a parent with no wake reserved), the parent is busy or
 * was stopped, and rows with no delivery state (old ones, or a wait that never acknowledged).
 */
import { describe, expect, it } from "@effect/vitest";
import {
  type CommandId,
  MessageId,
  type OrchestrationV2Subagent,
  type OrchestrationV2ThreadShell,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as PubSub from "effect/PubSub";
import * as Stream from "effect/Stream";
import { TestClock } from "effect/testing";

import * as McpToolAccessTestkit from "../../mcp/McpToolAccess.testkit.ts";
import * as CommandReceiptStore from "../../orchestration-v2/CommandReceiptStore.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import { REMINDER_MAX_AGE_MS } from "./delegatedResults.ts";
import { at, delegatedTask, NOW, PARENT } from "./delegatedResults.testkit.ts";
import * as DelegatedResultReminder from "./DelegatedResultReminder.ts";
import * as DelegatedResultsStore from "./DelegatedResultsStore.ts";

const OPTIONS: DelegatedResultReminder.ReminderOptions = {
  delay: Duration.minutes(2),
  retryDelay: Duration.minutes(1),
  attempts: 3,
};

const idleShell = (
  id: ThreadId,
  overrides: Partial<OrchestrationV2ThreadShell> = {},
): OrchestrationV2ThreadShell => ({
  ...McpToolAccessTestkit.liveThreadShell(id, { activeRunId: null }),
  status: "idle",
  ...overrides,
});

interface Sent {
  readonly threadId: ThreadId;
  readonly messageId: string;
  readonly commandId: string;
  readonly text: string;
}

/**
 * The parent's records as the server keeps them, in memory. `sent` is shared across services, as
 * the database is across a restart; the services themselves keep nothing.
 */
const makeWorld = (waitFor: { readonly enabled: boolean } = { enabled: true }) => {
  const shells = new Map<ThreadId, OrchestrationV2ThreadShell>([[PARENT, idleShell(PARENT)]]);
  const tasks = new Map<ThreadId, Array<OrchestrationV2Subagent>>([[PARENT, [delegatedTask()]]]);
  const sent: Array<Sent> = [];
  const receipts = new Map<string, ThreadId>();
  let failSends = 0;
  // Parents whose wake upstream reserved (null: every parent with tasks), and those the table names.
  const recovery: { ids: ReadonlyArray<ThreadId> | null } = { ids: null };
  const unread: { ids: ReadonlyArray<ThreadId>; since: Array<string> } = { ids: [], since: [] };
  return {
    shells,
    tasks,
    sent,
    recovery,
    unread,
    failNextSends: (count: number) => {
      failSends = count;
    },
    layer: (events: Stream.Stream<{ type: string; threadId: ThreadId; payload: unknown }>) =>
      Layer.mergeAll(
        ServerSettingsService.layerTest({ waitFor }),
        Layer.mock(ThreadManagement.ThreadManagementService)({
          getThreadShell: (threadId) => Effect.succeed(shells.get(threadId) ?? null),
          getThreadRecords: ((threadId: ThreadId) =>
            Effect.succeed({ subagents: tasks.get(threadId) ?? [] })) as never,
          getMessagesById: (threadId, ids) =>
            Effect.succeed(
              new Map(
                sent
                  .filter((message) => message.threadId === threadId)
                  .filter((message) => ids.includes(MessageId.make(message.messageId)))
                  .map((message) => [MessageId.make(message.messageId), {} as never]),
              ),
            ),
          sendToThread: (input) =>
            Effect.gen(function* () {
              if (failSends > 0) {
                failSends -= 1;
                return yield* Effect.fail({
                  _tag: "ThreadManagementProjectionLoadError",
                  message: "busy database",
                } as never);
              }
              sent.push({
                threadId: input.threadId,
                messageId: input.messageId!,
                commandId: input.commandId,
                text: input.text,
              });
              receipts.set(input.commandId, input.threadId);
              return {} as never;
            }),
          streamDomainEvents: events as never,
        }),
        Layer.mock(CommandReceiptStore.CommandReceiptStoreV2)({
          getByCommandId: (commandId: CommandId) =>
            Effect.succeed(
              receipts.has(commandId)
                ? Option.some({ status: "accepted", threadId: receipts.get(commandId)! } as never)
                : Option.none(),
            ),
        }),
        Layer.mock(ProjectionStore.ProjectionStoreV2)({
          getRecoveryThreadIds: () => Effect.succeed(recovery.ids ?? [...tasks.keys()]),
        }),
        Layer.mock(DelegatedResultsStore.DelegatedResultsStore)({
          parentsWithUnreadResults: (sinceIso) =>
            Effect.sync(() => {
              unread.since.push(sinceIso);
              return unread.ids;
            }),
        }),
      ),
  };
};

/** A service as the server makes one, over the world's records; each call is a fresh one. */
type Event = { readonly type: string; readonly threadId: ThreadId; readonly payload: unknown };

const service = (
  world: ReturnType<typeof makeWorld>,
  events: Stream.Stream<Event> = Stream.empty,
) => DelegatedResultReminder.make(OPTIONS).pipe(Effect.provide(world.layer(events)));

const setClock = TestClock.setTime(NOW);

/** Lets forked fibers reach their next sleep (or finish) before the clock moves or a check. */
const settle = Effect.repeat(Effect.yieldNow, { times: 20 });

describe("DelegatedResultReminder.check", () => {
  it.effect("sends one reminder naming an owed result to an idle parent", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const reminder = yield* service(world);
      const outcome = yield* reminder.check(PARENT);
      expect(outcome).toEqual({ type: "sent", taskIds: ["task-1"] });
      expect(world.sent).toHaveLength(1);
      expect(world.sent[0]!.text).toContain("task-1");
      expect(world.sent[0]!.text).toContain("task_status");
    }),
  );

  it.effect("sends nothing while waits are off (a machine that never names the setting)", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld({ enabled: false });
      const reminder = yield* service(world);
      expect(yield* reminder.check(PARENT)).toEqual({ type: "off" });
      expect(world.sent).toHaveLength(0);
    }),
  );

  it.effect("runs again without a second message (the same result is never reminded twice)", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const reminder = yield* service(world);
      yield* reminder.check(PARENT);
      yield* reminder.check(PARENT);
      expect(world.sent).toHaveLength(1);
    }),
  );

  it.effect("after a restart, a fresh service finds the message it sent and sends none", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      yield* (yield* service(world)).check(PARENT);
      const restarted = yield* service(world);
      expect(yield* restarted.check(PARENT)).toMatchObject({ type: "sent" });
      expect(world.sent).toHaveLength(1);
    }),
  );

  it.effect("sends a new reminder when another result becomes owed", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const reminder = yield* service(world);
      yield* reminder.check(PARENT);
      world.tasks.set(PARENT, [delegatedTask(), delegatedTask({ id: "task-2" })]);
      yield* reminder.check(PARENT);
      expect(world.sent).toHaveLength(2);
      expect(world.sent[1]!.messageId).not.toBe(world.sent[0]!.messageId);
    }),
  );

  it.effect("sends nothing for a delivered or read result", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.tasks.set(PARENT, [
        delegatedTask({ completionDelivery: { state: "delivered", observedByRunId: null } }),
        delegatedTask({
          id: "task-2",
          completionDelivery: { state: "acknowledged", observedByRunId: null },
        }),
      ]);
      expect(yield* (yield* service(world)).check(PARENT)).toEqual({ type: "none" });
      expect(world.sent).toHaveLength(0);
    }),
  );

  it.effect("never wakes a parent for old rows: no delivery record, or ended over a day ago", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.tasks.set(PARENT, [
        delegatedTask({
          completionDelivery: undefined,
          completedAt: at(NOW - REMINDER_MAX_AGE_MS - 1),
        }),
        delegatedTask({ id: "task-2", completedAt: at(NOW - REMINDER_MAX_AGE_MS - 1) }),
        delegatedTask({ id: "task-3", origin: "provider_native" }),
      ]);
      expect(yield* (yield* service(world)).check(PARENT)).toEqual({ type: "none" });
      expect(world.sent).toHaveLength(0);
    }),
  );

  it.effect("reminds a wait task whose acknowledgement failed (no delivery state)", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const task = delegatedTask({ completionDelivery: undefined });
      world.tasks.set(PARENT, [task]);
      const reminder = yield* service(world);
      expect(reminder.triggers({ type: "subagent.updated", threadId: PARENT, payload: task })).toBe(
        true,
      );
      expect(yield* reminder.check(PARENT)).toEqual({ type: "sent", taskIds: ["task-1"] });
      // The same task read later with an acknowledgement is settled: nothing more.
      world.tasks.set(PARENT, [
        delegatedTask({ completionDelivery: { state: "acknowledged", observedByRunId: null } }),
      ]);
      expect(yield* reminder.check(PARENT)).toEqual({ type: "none" });
      expect(world.sent).toHaveLength(1);
    }),
  );

  it.effect("reminds a parent that left background work running (no run end may come)", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.shells.set(
        PARENT,
        idleShell(PARENT, { pendingBackgroundTasks: [{ id: "monitor" } as never] }),
      );
      expect(yield* (yield* service(world)).check(PARENT)).toMatchObject({ type: "sent" });
    }),
  );

  it.effect("waits while the parent works, and checks again when its run ends", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.shells.set(
        PARENT,
        idleShell(PARENT, { status: "running", activityRunStatus: "running" }),
      );
      const reminder = yield* service(world);
      expect(yield* reminder.check(PARENT)).toEqual({ type: "busy" });
      expect(world.sent).toHaveLength(0);
      expect(reminder.watched.has(PARENT)).toBe(true);
      expect(
        reminder.triggers({
          type: "run.updated",
          threadId: PARENT,
          payload: { status: "completed" },
        }),
      ).toBe(true);
      world.shells.set(PARENT, idleShell(PARENT));
      expect(yield* reminder.check(PARENT)).toMatchObject({ type: "sent" });
      expect(reminder.watched.has(PARENT)).toBe(false);
      // A run update that is no end is no trigger, even for a watched parent.
      reminder.watched.add(PARENT);
      for (const status of ["queued", "running", "waiting"]) {
        expect(
          reminder.triggers({ type: "run.updated", threadId: PARENT, payload: { status } }),
        ).toBe(false);
      }
      expect(reminder.triggers({ type: "run.updated", threadId: PARENT, payload: {} })).toBe(false);
      reminder.watched.delete(PARENT);
      // A run end of a parent with nothing owed is no trigger.
      expect(
        reminder.triggers({
          type: "run.updated",
          threadId: PARENT,
          payload: { status: "completed" },
        }),
      ).toBe(false);
    }),
  );

  it.effect("never restarts a parent a person stopped, and never writes to an archived one", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.shells.set(PARENT, idleShell(PARENT, { status: "interrupted" }));
      const reminder = yield* service(world);
      expect(yield* reminder.check(PARENT)).toEqual({ type: "held", detail: "stopped" });
      // Not restarted, but its next run end (a person sends it on) checks again.
      expect(reminder.watched.has(PARENT)).toBe(true);
      expect(
        reminder.triggers({
          type: "run.updated",
          threadId: PARENT,
          payload: { status: "completed" },
        }),
      ).toBe(true);
      world.shells.set(PARENT, idleShell(PARENT, { archivedAt: at(NOW) }));
      expect(yield* reminder.check(PARENT)).toEqual({ type: "gone" });
      world.shells.delete(PARENT);
      expect(yield* reminder.check(PARENT)).toEqual({ type: "gone" });
      expect(world.sent).toHaveLength(0);
    }),
  );

  it.effect("a send stopped half-way is tried again with the same ids, and lands once", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.failNextSends(1);
      const reminder = yield* service(world);
      expect(yield* reminder.check(PARENT)).toEqual({ type: "unreadable" });
      expect(yield* reminder.check(PARENT)).toMatchObject({ type: "sent" });
      expect(world.sent).toHaveLength(1);
    }),
  );
});

describe("DelegatedResultReminder.start", () => {
  it.effect("a task ending with its result owed brings one reminder after the delay", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.tasks.set(PARENT, []);
      const pubsub = yield* PubSub.unbounded<{
        type: string;
        threadId: ThreadId;
        payload: unknown;
      }>();
      const reminder = yield* service(world, Stream.fromPubSub(pubsub));
      const fiber = yield* reminder.start().pipe(Effect.andThen(Effect.never), Effect.forkScoped);
      yield* Effect.yieldNow;
      // The startup scan found nothing owed.
      yield* TestClock.adjust(OPTIONS.delay);
      expect(world.sent).toHaveLength(0);

      const ended = delegatedTask();
      world.tasks.set(PARENT, [ended]);
      yield* PubSub.publish(pubsub, { type: "subagent.updated", threadId: PARENT, payload: ended });
      // A second event in the quiet window moves the one check later; it sends once.
      yield* TestClock.adjust(Duration.minutes(1));
      yield* PubSub.publish(pubsub, { type: "subagent.updated", threadId: PARENT, payload: ended });
      yield* TestClock.adjust(Duration.minutes(1));
      expect(world.sent).toHaveLength(0);
      yield* TestClock.adjust(Duration.minutes(1));
      expect(world.sent).toHaveLength(1);
      yield* Fiber.interrupt(fiber);
    }).pipe(Effect.scoped),
  );

  it.effect("after a restart, the startup scan reminds a result owed before it", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const reminder = yield* service(world);
      const fiber = yield* reminder.start().pipe(Effect.andThen(Effect.never), Effect.forkScoped);
      yield* settle;
      expect(world.sent).toHaveLength(0);
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      expect(world.sent).toHaveLength(1);
      yield* Fiber.interrupt(fiber);
      // The next start finds the same owed result and its message: nothing more is sent.
      const again = yield* service(world);
      const second = yield* again.start().pipe(Effect.andThen(Effect.never), Effect.forkScoped);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      expect(world.sent).toHaveLength(1);
      yield* Fiber.interrupt(second);
    }).pipe(Effect.scoped),
  );

  it.effect("after a restart, finds a parent whose result had no wake reserved", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.recovery.ids = [];
      world.unread.ids = [PARENT];
      const reminder = yield* service(world);
      const fiber = yield* reminder.start().pipe(Effect.andThen(Effect.never), Effect.forkScoped);
      yield* settle;
      // It asks for results from the last day only.
      expect(world.unread.since).toEqual([DateTime.formatIso(at(NOW - REMINDER_MAX_AGE_MS))]);
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      expect(world.sent).toHaveLength(1);
      yield* Fiber.interrupt(fiber);
    }).pipe(Effect.scoped),
  );

  it.effect("the startup scan leaves a parent to the check an event already moved later", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const pubsub = yield* PubSub.unbounded<Event>();
      const reminder = yield* service(world, Stream.fromPubSub(pubsub));
      const fiber = yield* reminder.start().pipe(Effect.andThen(Effect.never), Effect.forkScoped);
      yield* settle;
      // The parent works again just before the scan's delay ends: its run end comes later.
      yield* TestClock.adjust(Duration.minutes(1));
      yield* PubSub.publish(pubsub, {
        type: "subagent.updated",
        threadId: PARENT,
        payload: delegatedTask(),
      });
      yield* settle;
      world.shells.set(
        PARENT,
        idleShell(PARENT, { status: "running", activityRunStatus: "running" }),
      );
      yield* TestClock.adjust(Duration.minutes(1));
      yield* settle;
      // The scan skipped it: the parent was not marked busy (watched) by a scan of its own.
      expect(world.sent).toHaveLength(0);
      expect(reminder.watched.has(PARENT)).toBe(false);
      world.shells.set(PARENT, idleShell(PARENT));
      yield* TestClock.adjust(Duration.minutes(1));
      yield* settle;
      expect(world.sent).toHaveLength(1);
      yield* Fiber.interrupt(fiber);
    }).pipe(Effect.scoped),
  );

  it.effect("ignores events that are no trigger", () => {
    const world = makeWorld();
    return Effect.gen(function* () {
      const reminder = yield* service(world);
      expect(
        reminder.triggers({
          type: "subagent.updated",
          threadId: PARENT,
          payload: delegatedTask({ status: "running" }),
        }),
      ).toBe(false);
      expect(
        reminder.triggers({
          type: "subagent.updated",
          threadId: PARENT,
          payload: delegatedTask({ origin: "provider_native" }),
        }),
      ).toBe(false);
      expect(
        reminder.triggers({
          type: "run.updated",
          threadId: ThreadId.make("thread-other"),
          payload: { status: "completed" },
        }),
      ).toBe(false);
    });
  });
});
