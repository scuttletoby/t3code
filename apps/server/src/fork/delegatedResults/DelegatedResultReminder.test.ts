/**
 * Fork (issues 386, 625): the reminder of delegated results that never reached their parent. It
 * runs again, restarts, grows its owed set, stops half-way, meets an unreadable or first-format
 * record, a busy or stopped parent, and rows with no delivery state; a task is reminded once.
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
import * as Deferred from "effect/Deferred";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Option from "effect/Option";
import * as PubSub from "effect/PubSub";
import * as Scope from "effect/Scope";
import * as Stream from "effect/Stream";
import * as SqlClient from "effect/sql/SqlClient";
import { TestClock } from "effect/testing";

import * as McpToolAccessTestkit from "../../mcp/McpToolAccess.testkit.ts";
import * as CommandReceiptStore from "../../orchestration-v2/CommandReceiptStore.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as SqlitePersistence from "../../persistence/Sqlite.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import { legacyReminderSetId, REMINDER_MAX_AGE_MS, reminderBatchIds } from "./delegatedResults.ts";
import {
  at,
  delegatedTask,
  firstFormatReminder,
  makeSlots,
  NOW,
  PARENT,
  reminderMessageEvent,
} from "./delegatedResults.testkit.ts";
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
  // Commands the thread refuses, and their refused receipts (never counted as sent).
  const refusing: { commandIds: ReadonlyArray<string> } = { commandIds: [] };
  const refused = new Map<string, ThreadId>();
  // While `latch` is set, a send waits for it before it lands; `inFlight` counts sends under way.
  const sends: { latch: Deferred.Deferred<void> | null; inFlight: number; mostAtOnce: number } = {
    latch: null,
    inFlight: 0,
    mostAtOnce: 0,
  };
  let okSends = 0;
  let failSends = 0;
  // The record of earlier reminders cannot be read while this is set.
  const record = { unreadable: false, messagesUnreadable: false };
  // Set: the thread's messages are read from the real projection, not from `sent`.
  const realMessages: {
    read: ThreadManagement.ThreadManagementService["Service"]["getMessagesById"] | null;
  } = { read: null };
  // Parents whose wake upstream reserved (null: every parent with tasks), and those the table names.
  const recovery: { ids: ReadonlyArray<ThreadId> | null } = { ids: null };
  const unread: { ids: ReadonlyArray<ThreadId>; since: Array<string> } = { ids: [], since: [] };
  // The agent slot gate, which lets every reminder through unless a test fills it.
  const slots = makeSlots();
  return {
    shells,
    slots,
    tasks,
    sent,
    recovery,
    unread,
    record,
    receipts,
    refusing,
    refused,
    sends,
    realMessages,
    /** The next `count` sends fail, after `after` more succeed. */
    failNextSends: (count: number, after = 0) => {
      okSends = after;
      failSends = count;
    },
    layer: (events: Stream.Stream<{ type: string; threadId: ThreadId; payload: unknown }>) =>
      Layer.mergeAll(
        ServerSettingsService.layerTest({ waitFor }),
        slots.layer,
        Layer.mock(ThreadManagement.ThreadManagementService)({
          getThreadShell: (threadId) => Effect.succeed(shells.get(threadId) ?? null),
          getThreadRecords: ((threadId: ThreadId) =>
            Effect.succeed({ subagents: tasks.get(threadId) ?? [] })) as never,
          getMessagesById: (threadId, ids) =>
            realMessages.read !== null
              ? realMessages.read(threadId, ids)
              : record.messagesUnreadable
                ? Effect.fail({ _tag: "OrchestratorProjectionError", message: "busy" } as never)
                : Effect.succeed(
                    new Map(
                      sent
                        .filter((message) => message.threadId === threadId)
                        .filter((message) => ids.includes(MessageId.make(message.messageId)))
                        .map((message) => [
                          MessageId.make(message.messageId),
                          { text: message.text } as never,
                        ]),
                    ),
                  ),
          sendToThread: (input) =>
            Effect.gen(function* () {
              if (refusing.commandIds.includes(input.commandId)) {
                refused.set(input.commandId, input.threadId);
                return yield* Effect.fail({
                  _tag: "ThreadManagementCommandInvariantError",
                  message: "refused",
                } as never);
              }
              sends.inFlight += 1;
              sends.mostAtOnce = Math.max(sends.mostAtOnce, sends.inFlight);
              yield* Effect.ensuring(
                sends.latch === null ? Effect.void : Deferred.await(sends.latch),
                Effect.sync(() => {
                  sends.inFlight -= 1;
                }),
              );
              if (okSends > 0) okSends -= 1;
              else if (failSends > 0) {
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
                : refused.has(commandId)
                  ? Option.some({
                      status: "rejected",
                      threadId: refused.get(commandId)!,
                      error: "refused",
                    } as never)
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
          // As the store reads them: message ids and accepted receipts' command ids.
          sentIds: (threadId, prefix) =>
            Effect.sync(() =>
              record.unreadable
                ? null
                : [
                    ...sent
                      .filter((message) => message.threadId === threadId)
                      .map((message) => message.messageId),
                    ...[...receipts]
                      .filter(([, receiptThread]) => receiptThread === threadId)
                      .map(([commandId]) => commandId),
                  ].filter((id) => id.startsWith(prefix)),
            ),
          // As the real read: the text of each of the thread's messages asked for.
          messageTexts: (threadId, ids) =>
            Effect.sync(() =>
              record.messagesUnreadable
                ? null
                : new Map(
                    sent
                      .filter((message) => message.threadId === threadId)
                      .filter((message) => ids.includes(message.messageId))
                      .map((message) => [message.messageId, message.text]),
                  ),
            ),
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

const reminderIdsOf = (tasks: ReadonlyArray<OrchestrationV2Subagent>) =>
  reminderBatchIds(
    PARENT,
    tasks.map((task) => task.id),
  );

const warningLogs = (into: Array<string>) =>
  Logger.layer(
    [
      Logger.make(({ message, logLevel }) => {
        if (logLevel === "Warn") into.push(JSON.stringify(message));
      }),
    ],
    { mergeWithExisting: false },
  );

// Upstream's projection and the reminder's own reads, over one real database.
const realStores = Layer.mergeAll(DelegatedResultsStore.layer, ProjectionStore.layer).pipe(
  Layer.provideMerge(SqlitePersistence.layerMemory),
);

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
      expect(yield* restarted.check(PARENT)).toEqual({ type: "none" });
      expect(world.sent).toHaveLength(1);
    }),
  );

  it.effect("another result owed later is reminded alone: the first is never reminded twice", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const reminder = yield* service(world);
      yield* reminder.check(PARENT);
      // Task 2 ends while task 1 is still unread.
      world.tasks.set(PARENT, [delegatedTask(), delegatedTask({ id: "task-2" })]);
      expect(yield* reminder.check(PARENT)).toEqual({ type: "sent", taskIds: ["task-2"] });
      expect(world.sent).toHaveLength(2);
      expect(world.sent[1]!.text).toContain("task-2");
      expect(world.sent[1]!.text).not.toContain("task-1");
      expect(world.sent[1]!.text).toContain("1 delegated task ended");
      // Again, and after a restart: nothing more for either.
      expect(yield* reminder.check(PARENT)).toEqual({ type: "none" });
      expect(yield* (yield* service(world)).check(PARENT)).toEqual({ type: "none" });
      expect(world.sent).toHaveLength(2);
    }),
  );

  it.effect("a reminder whose message is not projected yet is known by its receipt", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      yield* (yield* service(world)).check(PARENT);
      // The message is gone from the projection (not written yet, or pruned); its receipt stays.
      world.sent.splice(0);
      world.tasks.set(PARENT, [delegatedTask(), delegatedTask({ id: "task-2" })]);
      expect(yield* (yield* service(world)).check(PARENT)).toEqual({
        type: "sent",
        taskIds: ["task-2"],
      });
    }),
  );

  it.effect("sends nothing while the record of earlier reminders cannot be read", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.record.unreadable = true;
      const reminder = yield* service(world);
      expect(yield* reminder.check(PARENT)).toEqual({ type: "unreadable" });
      expect(world.sent).toHaveLength(0);
      expect(reminder.watched.has(PARENT)).toBe(true);
      world.record.unreadable = false;
      expect(yield* reminder.check(PARENT)).toMatchObject({ type: "sent" });
    }),
  );

  it.effect("takes a reminder of the first id format for the owed set as sent", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      // A server before this change reminded task 1 with one id for its whole set.
      const legacy = legacyReminderSetId(PARENT, [delegatedTask().id]);
      world.sent.push({ threadId: PARENT, messageId: legacy, commandId: `${legacy}:0`, text: "" });
      expect(yield* (yield* service(world)).check(PARENT)).toEqual({ type: "none" });
      world.tasks.set(PARENT, [delegatedTask(), delegatedTask({ id: "task-2" })]);
      expect(yield* (yield* service(world)).check(PARENT)).toEqual({
        type: "sent",
        taskIds: ["task-2"],
      });
    }),
  );

  it.effect(
    "a first-format reminder is split per task: a set grown after it names the new one",
    () =>
      Effect.gen(function* () {
        yield* setClock;
        const world = makeWorld();
        // A server before the upgrade reminded tasks 1 and 2 with one id for the set.
        const old = firstFormatReminder(PARENT, ["task-1", "task-2"]);
        world.sent.push({ threadId: PARENT, ...old });
        // Task 3 ends after the upgrade, while 1 and 2 are still unread.
        world.tasks.set(PARENT, [
          delegatedTask(),
          delegatedTask({ id: "task-2" }),
          delegatedTask({ id: "task-3" }),
        ]);
        const reminder = yield* service(world);
        expect(yield* reminder.check(PARENT)).toEqual({ type: "sent", taskIds: ["task-3"] });
        expect(world.sent[1]!.text).toContain("task-3");
        expect(world.sent[1]!.text).not.toContain("task-1");
        // Runs again, and after a restart: nothing more.
        expect(yield* reminder.check(PARENT)).toEqual({ type: "none" });
        expect(yield* (yield* service(world)).check(PARENT)).toEqual({ type: "none" });
        // Task 2 is read; task 4 ends: only task 4.
        world.tasks.set(PARENT, [
          delegatedTask(),
          delegatedTask({ id: "task-3" }),
          delegatedTask({ id: "task-4" }),
        ]);
        expect(yield* (yield* service(world)).check(PARENT)).toEqual({
          type: "sent",
          taskIds: ["task-4"],
        });
        expect(world.sent).toHaveLength(3);
      }),
  );

  it.effect("sends nothing while a first-format reminder's text cannot be read", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const old = firstFormatReminder(PARENT, ["task-1", "task-2"]);
      world.sent.push({ threadId: PARENT, ...old });
      world.tasks.set(PARENT, [delegatedTask(), delegatedTask({ id: "task-3" })]);
      world.record.messagesUnreadable = true;
      const reminder = yield* service(world);
      expect(yield* reminder.check(PARENT)).toEqual({ type: "unreadable" });
      expect(world.sent).toHaveLength(1);
      expect(reminder.watched.has(PARENT)).toBe(true);
      world.record.messagesUnreadable = false;
      expect(yield* reminder.check(PARENT)).toEqual({ type: "sent", taskIds: ["task-3"] });
    }),
  );

  it.effect("a refused batch does not hold the next ones back; it stays refused, sent once", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const many = Array.from({ length: 25 }, (_, index) =>
        delegatedTask({ id: `task-${String(index).padStart(2, "0")}` }),
      );
      world.tasks.set(PARENT, many);
      // The thread refuses the first batch (tasks 00 to 09) only.
      const first = reminderIdsOf(many.slice(0, 10));
      world.refusing.commandIds = [first.commandId];
      const reminder = yield* service(world);
      expect(yield* reminder.check(PARENT)).toEqual({
        type: "held",
        detail: "refused",
        taskIds: many.slice(10).map((task) => task.id),
      });
      expect(world.sent).toHaveLength(2);
      // Runs again, and after a restart: the refused batch is answered from its receipt, and
      // nothing is sent twice.
      expect(yield* reminder.check(PARENT)).toEqual({
        type: "held",
        detail: "refused",
        taskIds: [],
      });
      expect(yield* (yield* service(world)).check(PARENT)).toMatchObject({ type: "held" });
      expect(world.sent).toHaveLength(2);
      expect(world.refused.size).toBe(1);
    }),
  );

  it.effect("warns of a refused batch once, not on every check; a new batch warns again", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const many = Array.from({ length: 12 }, (_, index) =>
        delegatedTask({ id: `task-${String(index).padStart(2, "0")}` }),
      );
      world.tasks.set(PARENT, many);
      const first = reminderIdsOf(many.slice(0, 10));
      world.refusing.commandIds = [first.commandId];
      const warnings: Array<string> = [];
      const refusals = () => warnings.filter((log) => log.includes("reminder refused"));
      const reminder = yield* service(world);
      const check = reminder.check(PARENT).pipe(Effect.provide(warningLogs(warnings)));
      expect(yield* check).toMatchObject({ type: "held", taskIds: ["task-10", "task-11"] });
      // Runs again, and after a restart: answered from its refused receipt, no new warning.
      expect(yield* check).toMatchObject({ type: "held", taskIds: [] });
      yield* (yield* service(world)).check(PARENT).pipe(Effect.provide(warningLogs(warnings)));
      expect(refusals()).toHaveLength(1);
      // A task that sorts first ends: the refused tasks go in a new batch (new ids), refused too.
      const extra = delegatedTask({ id: "task-0" });
      world.tasks.set(PARENT, [extra, ...many]);
      const next = reminderIdsOf([extra, ...many.slice(0, 9)]);
      world.refusing.commandIds = [first.commandId, next.commandId];
      expect(yield* check).toEqual({ type: "held", detail: "refused", taskIds: ["task-09"] });
      expect(refusals()).toHaveLength(2);
      expect(yield* check).toEqual({ type: "held", detail: "refused", taskIds: [] });
      expect(refusals()).toHaveLength(2);
    }),
  );

  it.effect(
    "a first-format reminder that cannot be decoded is left out alone (real message table)",
    () =>
      Effect.gen(function* () {
        yield* setClock;
        const world = makeWorld();
        const projections = yield* ProjectionStore.ProjectionStoreV2;
        const store = yield* DelegatedResultsStore.DelegatedResultsStore;
        const sql = yield* SqlClient.SqlClient;
        // Every message read goes to the real table (upstream's decode included).
        world.realMessages.read = projections.getMessagesById as never;
        // Before the upgrade: tasks 1 and 2 told in one message, written by upstream's projection.
        const good = firstFormatReminder(PARENT, ["task-1", "task-2"]);
        yield* projections.apply(reminderMessageEvent(PARENT, good.messageId, good.text));
        // Tasks 3 and 4 told in another, whose stored payload upstream can no longer decode.
        const broken = firstFormatReminder(PARENT, ["task-3", "task-4"]);
        yield* sql`
          INSERT INTO orchestration_v2_projection_messages (
            message_id, thread_id, run_id, node_id, role, streaming, created_at, updated_at,
            payload_json
          ) VALUES (
            ${broken.messageId}, ${PARENT}, NULL, NULL, 'user', 0, '2026-11-20T11:59:00.000Z',
            '2026-11-20T11:59:00.000Z', '{"obsolete":"transcript"}'
          )
        `;
        world.tasks.set(
          PARENT,
          ["task-1", "task-2", "task-3", "task-4", "task-5"].map((id) => delegatedTask({ id })),
        );
        const reminder = yield* DelegatedResultReminder.make(OPTIONS).pipe(
          Effect.provideService(DelegatedResultsStore.DelegatedResultsStore, store),
          Effect.provide(world.layer(Stream.empty)),
        );
        // Tasks 1 and 2 are split from the readable text; 3 and 4 fall back to their receipt's
        // rule (not the owed set, not one task), so they are reminded once more.
        expect(yield* reminder.check(PARENT)).toEqual({
          type: "sent",
          taskIds: ["task-3", "task-4", "task-5"],
        });
        expect(world.sent).toHaveLength(1);
        // Runs again, once the projection has its message: nothing more.
        yield* projections.apply(
          reminderMessageEvent(PARENT, world.sent[0]!.messageId, world.sent[0]!.text),
        );
        expect(yield* reminder.check(PARENT)).toEqual({ type: "none" });
        expect(world.sent).toHaveLength(1);
      }).pipe(Effect.provide(realStores)),
  );

  it.effect("many owed results go in batches; one stopped half-way sends only the rest", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const many = Array.from({ length: 12 }, (_, index) =>
        delegatedTask({ id: `task-${String(index).padStart(2, "0")}` }),
      );
      world.tasks.set(PARENT, many);
      // The first batch lands, the second fails.
      world.failNextSends(1, 1);
      const reminder = yield* service(world);
      expect(yield* reminder.check(PARENT)).toEqual({ type: "unreadable" });
      expect(world.sent).toHaveLength(1);
      const second = yield* reminder.check(PARENT);
      expect(second).toEqual({ type: "sent", taskIds: ["task-10", "task-11"] });
      expect(world.sent).toHaveLength(2);
      expect(world.sent.map((message) => message.text).join("\n")).not.toContain("more");
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

  it.effect("a startup check slower than the delay never runs beside an event's check", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const pubsub = yield* PubSub.unbounded<Event>();
      const reminder = yield* service(world, Stream.fromPubSub(pubsub));
      // The scan's send of task 1 hangs (a slow database), far past the delay.
      world.sends.latch = yield* Deferred.make<void>();
      const fiber = yield* reminder.start().pipe(Effect.andThen(Effect.never), Effect.forkScoped);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      expect(world.sends.inFlight).toBe(1);
      // Task 2 ends meanwhile; its check comes after the delay, while the scan's still hangs.
      const second = delegatedTask({ id: "task-2" });
      world.tasks.set(PARENT, [delegatedTask(), second]);
      yield* PubSub.publish(pubsub, {
        type: "subagent.updated",
        threadId: PARENT,
        payload: second,
      });
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      yield* Deferred.succeed(world.sends.latch, undefined);
      yield* settle;
      yield* TestClock.adjust(Duration.minutes(10));
      yield* settle;
      // One check at a time, and each task named in one message only.
      expect(world.sends.mostAtOnce).toBe(1);
      const named = world.sent.flatMap((message) =>
        ["task-1", "task-2"].filter((id) => message.text.includes(id)),
      );
      expect(named.toSorted()).toEqual(["task-1", "task-2"]);
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

/**
 * Fork (issue 711): a reminder starts a run in an idle parent, so it asks the agent slot gate. One
 * refused waits for a slot: asked again every few seconds for as long as it takes, sent once a
 * slot frees, never twice, and found again by the startup scan after a restart.
 */
describe("DelegatedResultReminder and agent slots", () => {
  const SLOT_RETRY = Duration.seconds(5);
  const infoLogs = (into: Array<string>) =>
    Logger.layer(
      [
        Logger.make(({ message, logLevel }) => {
          if (logLevel === "Info") into.push(JSON.stringify(message));
        }),
      ],
      { mergeWithExisting: false },
    );

  it.effect("asks the gate as a wake-up of the parent, and sends nothing while it refuses", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.slots.state.all = true;
      const reminder = yield* service(world);
      expect(yield* reminder.check(PARENT)).toMatchObject({ type: "waitsForSlot" });
      expect(world.sent).toHaveLength(0);
      // The parent's own held slot stays held for the run this reminder wakes up.
      expect(world.slots.asks).toEqual([
        expect.objectContaining({
          callerThreadId: PARENT,
          intoThreadId: PARENT,
          keepsHeld: true,
        }),
      ]);
      // A refusal is no reason to drop the parent: its next run end checks too.
      expect(reminder.watched.has(PARENT)).toBe(true);
      world.slots.state.all = false;
      expect(yield* reminder.check(PARENT)).toMatchObject({ type: "sent" });
      // Runs again: the same result is never reminded twice.
      expect(yield* reminder.check(PARENT)).toEqual({ type: "none" });
      expect(world.sent).toHaveLength(1);
    }),
  );

  it.effect("waits for a slot past every attempt, logs it once, and sends once a slot frees", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.tasks.set(PARENT, []);
      world.slots.state.all = true;
      const logs: Array<string> = [];
      const pubsub = yield* PubSub.unbounded<Event>();
      const reminder = yield* service(world, Stream.fromPubSub(pubsub));
      const fiber = yield* reminder
        .start()
        .pipe(Effect.andThen(Effect.never), Effect.provide(infoLogs(logs)), Effect.forkScoped);
      yield* settle;
      const ended = delegatedTask();
      world.tasks.set(PARENT, [ended]);
      yield* PubSub.publish(pubsub, { type: "subagent.updated", threadId: PARENT, payload: ended });
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      expect(world.slots.asks).toHaveLength(1);
      // Far more tries than `attempts` (3), every 5 seconds: a refusal is never given up.
      for (let step = 0; step < 24; step++) {
        yield* TestClock.adjust(SLOT_RETRY);
        yield* settle;
      }
      expect(world.slots.asks).toHaveLength(25);
      expect(world.sent).toHaveLength(0);
      expect(logs.filter((log) => log.includes("waits for an agent slot"))).toHaveLength(1);
      world.slots.state.all = false;
      yield* TestClock.adjust(SLOT_RETRY);
      yield* settle;
      expect(world.sent).toHaveLength(1);
      // Done: no more asks, and never a second message.
      yield* TestClock.adjust(Duration.minutes(10));
      yield* settle;
      expect(world.slots.asks).toHaveLength(26);
      expect(world.sent).toHaveLength(1);
      yield* Fiber.interrupt(fiber);
    }).pipe(Effect.scoped),
  );

  it.effect(
    "a restart while one waits loses nothing: the startup scan waits and sends it once",
    () =>
      Effect.gen(function* () {
        yield* setClock;
        const world = makeWorld();
        world.slots.state.all = true;
        // The first server lives in its own scope: a restart closes all of it, timers too.
        const firstServer = yield* Scope.make();
        const first = yield* service(world).pipe(Scope.provide(firstServer));
        yield* first
          .start()
          .pipe(
            Effect.andThen(Effect.never),
            Scope.provide(firstServer),
            Effect.forkIn(firstServer),
          );
        yield* settle;
        yield* TestClock.adjust(OPTIONS.delay);
        yield* settle;
        yield* TestClock.adjust(SLOT_RETRY);
        yield* settle;
        expect(world.slots.asks).toHaveLength(2);
        // Stopped half-way through its wait.
        yield* Scope.close(firstServer, Exit.void);
        const asked = world.slots.asks.length;
        yield* TestClock.adjust(Duration.minutes(5));
        yield* settle;
        expect(world.slots.asks).toHaveLength(asked);

        // The next start: the scan finds the result still owed, and its timer waits for a slot.
        const again = yield* service(world);
        const after = yield* again.start().pipe(Effect.andThen(Effect.never), Effect.forkScoped);
        yield* settle;
        yield* TestClock.adjust(OPTIONS.delay);
        yield* settle;
        yield* TestClock.adjust(SLOT_RETRY);
        yield* settle;
        expect(world.slots.asks).toHaveLength(asked + 2);
        expect(world.sent).toHaveLength(0);
        world.slots.state.all = false;
        yield* TestClock.adjust(SLOT_RETRY);
        yield* settle;
        expect(world.sent).toHaveLength(1);
        yield* TestClock.adjust(Duration.minutes(10));
        yield* settle;
        expect(world.sent).toHaveLength(1);
        yield* Fiber.interrupt(after);
      }).pipe(Effect.scoped),
  );

  it.effect("the startup scan does not hold the next parent up while one waits for a slot", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const other = ThreadId.make("thread-other");
      world.shells.set(other, idleShell(other));
      world.tasks.set(other, [delegatedTask({ threadId: other })]);
      world.recovery.ids = [PARENT, other];
      world.slots.full.add(PARENT);
      const reminder = yield* service(world);
      const fiber = yield* reminder.start().pipe(Effect.andThen(Effect.never), Effect.forkScoped);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      expect(world.sent.map((message) => message.threadId)).toEqual([other]);
      world.slots.full.delete(PARENT);
      yield* TestClock.adjust(SLOT_RETRY);
      yield* settle;
      expect(world.sent.map((message) => message.threadId)).toEqual([other, PARENT]);
      yield* Fiber.interrupt(fiber);
    }).pipe(Effect.scoped),
  );
});
