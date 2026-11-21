/**
 * Fork: backstop for upstream's completion wake. An idle parent with results unread is reminded once
 * per task (message ids name the task keys); at start, the last day's are checked once. Off with
 * `server.waitFor`. A reminder takes an agent slot; refused, it is asked again and never given up.
 */
import { type OrchestrationV2Subagent, type ThreadId } from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FiberMap from "effect/FiberMap";
import * as Layer from "effect/Layer";
import * as Stream from "effect/Stream";

import * as CommandReceiptStore from "../../orchestration-v2/CommandReceiptStore.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as SlotGate from "../../slots/SlotGate.ts";
import { forkParked } from "../../serverActivation.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import * as WaitDelivery from "../../waits/WaitDelivery.ts";
import { WAKE_SLOT_RETRY_MS } from "../../waits/WaitService.ts";
import * as DelegatedResultsStore from "./DelegatedResultsStore.ts";
import {
  isDelegatedTask,
  isEndedTask,
  isRemindable,
  legacyReminderMessageIds,
  parentBusy,
  REMINDER_MAX_AGE_MS,
  parentGone,
  REMINDER_ID_PREFIX,
  reminderBatches,
  reminderBatchIds,
  reminderText,
  resultDelivery,
} from "./delegatedResults.ts";

export interface ReminderOptions {
  /** From the event to the check: upstream's own wake goes first. */
  readonly delay: Duration.Input;
  /** Between tries after a send that may succeed later. */
  readonly retryDelay: Duration.Input;
  /** Tries of one check before it waits for the parent's next event. */
  readonly attempts: number;
  /**
   * Between tries while the agent slot gate refuses (the wait service's `WAKE_SLOT_RETRY_MS` by
   * default). Not counted in `attempts`: an idle parent sends no next event, so a reminder that
   * waits for a slot is never given up.
   */
  readonly slotRetryDelay?: Duration.Input;
}

const DEFAULT_OPTIONS: ReminderOptions = {
  delay: Duration.minutes(2),
  retryDelay: Duration.minutes(1),
  attempts: 5,
};

/** What one check did; tests read it. */
export type CheckOutcome =
  | { readonly type: "none" }
  | { readonly type: "busy" }
  | { readonly type: "off" }
  | { readonly type: "gone" }
  | { readonly type: "unreadable" }
  /** The agent slot gate refused: asked again after `slotRetryDelay`. */
  | { readonly type: "waitsForSlot"; readonly detail: string }
  | { readonly type: "sent"; readonly taskIds: ReadonlyArray<string> }
  /** Stopped by a person, or a batch refused (`taskIds`: the other batches, sent). */
  | { readonly type: "held"; readonly detail: string; readonly taskIds?: ReadonlyArray<string> };

const TERMINAL_RUN = new Set(["completed", "failed", "interrupted", "cancelled", "rolled_back"]);

export const make = Effect.fn("DelegatedResultReminder.make")(function* (
  options: ReminderOptions = DEFAULT_OPTIONS,
) {
  const threads = yield* ThreadManagement.ThreadManagementService;
  // The server's one gate (`SlotGate.layer` memoizes): none in a test that gives none.
  const delivery = yield* WaitDelivery.make(yield* Effect.serviceOption(SlotGate.SlotGate));
  const slotRetryDelay = options.slotRetryDelay ?? Duration.millis(WAKE_SLOT_RETRY_MS);
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  const settings = yield* ServerSettingsService;
  const store = yield* DelegatedResultsStore.DelegatedResultsStore;
  // Parents that were busy (or could not be reached) while a result was owed: their next run end
  // checks again. In memory; a restart rebuilds it from the startup scan.
  const watched = new Set<ThreadId>();

  const check = Effect.fn("DelegatedResultReminder.check")(function* (parentThreadId: ThreadId) {
    const enabled = yield* settings.getSettings.pipe(
      Effect.map((current) => current.waitFor.enabled),
      Effect.orElseSucceed(() => false),
    );
    if (!enabled) return { type: "off" } satisfies CheckOutcome;
    const shell = yield* threads
      .getThreadShell(parentThreadId)
      .pipe(Effect.orElseSucceed(() => undefined));
    if (shell === undefined) return { type: "unreadable" } satisfies CheckOutcome;
    if (parentGone(shell)) {
      watched.delete(parentThreadId);
      return { type: "gone" } satisfies CheckOutcome;
    }
    if (parentBusy(shell!)) {
      watched.add(parentThreadId);
      return { type: "busy" } satisfies CheckOutcome;
    }
    const records = yield* threads
      .getThreadRecords(parentThreadId, ["subagents"], { omitSettledSubagents: true })
      .pipe(Effect.orElseSucceed(() => undefined));
    if (records === undefined) return { type: "unreadable" } satisfies CheckOutcome;
    const nowMs = yield* Clock.currentTimeMillis;
    const owed = records.subagents.filter((task) => isRemindable(task, nowMs));
    if (owed.length === 0) {
      watched.delete(parentThreadId);
      return { type: "none" } satisfies CheckOutcome;
    }
    // What this parent was reminded of before, in either id format.
    const sentIds = yield* store.sentIds(parentThreadId, REMINDER_ID_PREFIX);
    if (sentIds === null) {
      watched.add(parentThreadId);
      return { type: "unreadable" } satisfies CheckOutcome;
    }
    // A first-format message names its tasks in its text: read it, to split it per task. One
    // whose text cannot be read is left out alone: its tasks fall back to its receipt's rule.
    const legacyIds = legacyReminderMessageIds(parentThreadId, sentIds);
    const legacyTexts = yield* store.messageTexts(parentThreadId, legacyIds);
    if (legacyTexts === null) {
      watched.add(parentThreadId);
      return { type: "unreadable" } satisfies CheckOutcome;
    }
    const batches = reminderBatches(parentThreadId, owed, sentIds, legacyTexts);
    if (batches.length === 0) {
      watched.delete(parentThreadId);
      return { type: "none" } satisfies CheckOutcome;
    }
    const sent: Array<string> = [];
    let refused: string | undefined;
    for (const batch of batches) {
      const ids = reminderBatchIds(
        parentThreadId,
        batch.map((task) => task.id),
      );
      const result = yield* delivery.deliver({
        threadId: parentThreadId,
        messageId: ids.messageId,
        commandId: ids.commandId,
        text: reminderText(batch),
      });
      switch (result.type) {
        case "delivered":
          sent.push(...batch.map((task) => task.id));
          continue;
        case "gone":
          watched.delete(parentThreadId);
          return { type: "gone" } satisfies CheckOutcome;
        case "stopped":
          // A person stopped it: never restart it from here. Its next run end checks again.
          watched.add(parentThreadId);
          return { type: "held", detail: "stopped" } satisfies CheckOutcome;
        case "rejected":
          // Its ids stay refused, so its tasks wait for the owed set to change; the next
          // batches are other tasks, and are still sent. Logged when refused, not on each
          // later check that finds its refused receipt.
          if (result.earlier !== true) {
            yield* Effect.logWarning("delegated result reminder refused", {
              threadId: parentThreadId,
              detail: result.detail,
            });
          }
          refused = result.detail;
          continue;
        case "waitsForSlot":
          // Nothing of this batch was sent; the ones before it are recorded by their ids.
          watched.add(parentThreadId);
          return { type: "waitsForSlot", detail: result.detail } satisfies CheckOutcome;
        case "retry":
          // The batches sent so far are recorded by their ids; the next try sends the rest.
          watched.add(parentThreadId);
          return { type: "unreadable" } satisfies CheckOutcome;
      }
    }
    watched.delete(parentThreadId);
    if (refused !== undefined) {
      return { type: "held", detail: refused, taskIds: sent } satisfies CheckOutcome;
    }
    return { type: "sent", taskIds: sent } satisfies CheckOutcome;
  });

  /**
   * Checks until done: a few more tries only while the parent cannot be read, and every
   * `slotRetryDelay` while the slot gate refuses, for as long as it refuses (logged once).
   */
  const checkUntilDone = Effect.fnUntraced(function* (parentThreadId: ThreadId) {
    let waitLogged = false;
    for (let attempt = 1; attempt <= options.attempts;) {
      const outcome = yield* check(parentThreadId);
      if (outcome.type === "waitsForSlot") {
        if (!waitLogged) {
          waitLogged = true;
          yield* Effect.logInfo("delegated result reminder waits for an agent slot", {
            threadId: parentThreadId,
            detail: outcome.detail,
          });
        }
        yield* Effect.sleep(slotRetryDelay);
        continue;
      }
      if (outcome.type !== "unreadable") return;
      yield* Effect.sleep(options.retryDelay);
      attempt += 1;
    }
  });

  const logFailure = (parentThreadId: ThreadId) =>
    Effect.catchCause((cause) =>
      Effect.logWarning("delegated result reminder check failed", {
        threadId: parentThreadId,
        cause,
      }),
    );

  /** One check after the delay, then `checkUntilDone`. */
  const checkLater = Effect.fn("DelegatedResultReminder.checkLater")(
    function* (parentThreadId: ThreadId) {
      yield* Effect.sleep(options.delay);
      yield* checkUntilDone(parentThreadId);
    },
    (effect, parentThreadId) =>
      effect.pipe(
        Effect.catchCause((cause) =>
          Effect.logWarning("delegated result reminder check failed", {
            threadId: parentThreadId,
            cause,
          }),
        ),
      ),
  );

  /**
   * A task that ended with its result owed or with no delivery state (a wait that may not
   * acknowledge it), or a run end of a watched parent.
   */
  const triggers = (event: {
    readonly type: string;
    readonly threadId: ThreadId;
    readonly payload: unknown;
  }): boolean => {
    if (event.type === "subagent.updated") {
      const task = event.payload as OrchestrationV2Subagent;
      const delivery = resultDelivery(task);
      return (
        isDelegatedTask(task) && isEndedTask(task) && (delivery === "owed" || delivery === "none")
      );
    }
    if (event.type === "run.updated" && watched.has(event.threadId)) {
      const run = event.payload as { readonly status?: string };
      return run.status !== undefined && TERMINAL_RUN.has(run.status);
    }
    return false;
  };

  const start = Effect.fn("DelegatedResultReminder.start")(function* () {
    // Keyed by parent: a newer event moves its check later, so one quiet moment gets one check.
    const timers = yield* FiberMap.make<ThreadId>();
    yield* forkParked(
      Stream.runForEach(threads.streamDomainEvents, (event) =>
        triggers(event)
          ? FiberMap.run(timers, event.threadId, checkLater(event.threadId)).pipe(Effect.asVoid)
          : Effect.void,
      ),
    );
    // Results unread when the server stopped: upstream offers the wakes it had reserved again at
    // start; the table also names parents whose result had no wake reserved yet. A parent that
    // still has one unread after the delay gets its reminder. One parent at a time.
    yield* forkParked(
      Effect.gen(function* () {
        const recovering = yield* projections
          .getRecoveryThreadIds("delegated-completions")
          .pipe(Effect.orElseSucceed((): ReadonlyArray<ThreadId> => []));
        const sinceMs = (yield* Clock.currentTimeMillis) - REMINDER_MAX_AGE_MS;
        const unread = yield* store.parentsWithUnreadResults(
          DateTime.formatIso(DateTime.makeUnsafe(sinceMs)),
        );
        yield* Effect.sleep(options.delay);
        for (const parentThreadId of new Set([...recovering, ...unread])) {
          // The scan's check runs in the parent's own timer, as an event's does, so the two never
          // run at once: one an event already moved later is left to it (`onlyIfMissing`), and an
          // event meanwhile replaces this one (what it sent is in the record) and checks later.
          const firstDone = yield* Deferred.make<void>();
          const timer = yield* FiberMap.run(
            timers,
            parentThreadId,
            Effect.gen(function* () {
              const outcome = yield* check(parentThreadId);
              yield* Deferred.succeed(firstDone, undefined);
              // Refused a slot: asked again in this timer, so the next parents are not held up.
              if (outcome.type === "waitsForSlot") {
                yield* Effect.sleep(slotRetryDelay);
                yield* checkUntilDone(parentThreadId);
              }
            }).pipe(logFailure(parentThreadId)),
            { onlyIfMissing: true },
          );
          // One parent at a time: the next goes once this one's first check is done or replaced.
          yield* Effect.raceFirst(Deferred.await(firstDone), Fiber.await(timer));
        }
      }),
    );
  });

  return { check, start, triggers, watched };
});

/**
 * Started with the server's other workers, from `ForkLayers.layerRuntimeServices`
 * (fork/forkLayers.ts). Its slot gate is the server's one gate: layers memoize by reference.
 */
export const layer = Layer.effectDiscard(
  make().pipe(Effect.flatMap((service) => service.start())),
).pipe(
  Layer.provide(
    Layer.mergeAll(
      SlotGate.layer,
      ProjectionStore.layer,
      CommandReceiptStore.layerFromApplicationReceipts,
      DelegatedResultsStore.layer,
    ),
  ),
);
