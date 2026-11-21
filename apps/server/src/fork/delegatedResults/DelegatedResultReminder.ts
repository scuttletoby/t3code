/**
 * Fork (issues 386, 625): backstop for upstream's completion wake. A parent's own events start one
 * delayed check; an idle parent with results unread is reminded once per task (message ids name the
 * task keys). At start, parents with a result unread from the last day are checked once. Off with
 * `server.waitFor`.
 */
import type { OrchestrationV2Subagent, ThreadId } from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as FiberMap from "effect/FiberMap";
import * as Layer from "effect/Layer";
import * as Stream from "effect/Stream";

import * as CommandReceiptStore from "../../orchestration-v2/CommandReceiptStore.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import { forkParked } from "../../serverActivation.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import * as WaitDelivery from "../../waits/WaitDelivery.ts";
import * as DelegatedResultsStore from "./DelegatedResultsStore.ts";
import {
  isDelegatedTask,
  isEndedTask,
  isRemindable,
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
  | { readonly type: "sent"; readonly taskIds: ReadonlyArray<string> }
  | { readonly type: "held"; readonly detail: string };

const TERMINAL_RUN = new Set(["completed", "failed", "interrupted", "cancelled", "rolled_back"]);

export const make = Effect.fn("DelegatedResultReminder.make")(function* (
  options: ReminderOptions = DEFAULT_OPTIONS,
) {
  const threads = yield* ThreadManagement.ThreadManagementService;
  const delivery = yield* WaitDelivery.make();
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
    const batches = reminderBatches(parentThreadId, owed, sentIds);
    if (batches.length === 0) {
      watched.delete(parentThreadId);
      return { type: "none" } satisfies CheckOutcome;
    }
    const sent: Array<string> = [];
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
          watched.delete(parentThreadId);
          yield* Effect.logWarning("delegated result reminder refused", {
            threadId: parentThreadId,
            detail: result.detail,
          });
          return { type: "held", detail: result.detail } satisfies CheckOutcome;
        // Not given a gate (`WaitDelivery.make`): never answered here.
        case "waitsForSlot":
        case "retry":
          // The batches sent so far are recorded by their ids; the next try sends the rest.
          watched.add(parentThreadId);
          return { type: "unreadable" } satisfies CheckOutcome;
      }
    }
    watched.delete(parentThreadId);
    return { type: "sent", taskIds: sent } satisfies CheckOutcome;
  });

  /** One check after the delay; tries again a few times only while the parent cannot be read. */
  const checkLater = Effect.fn("DelegatedResultReminder.checkLater")(
    function* (parentThreadId: ThreadId) {
      yield* Effect.sleep(options.delay);
      for (let attempt = 1; attempt <= options.attempts; attempt += 1) {
        const outcome = yield* check(parentThreadId);
        if (outcome.type !== "unreadable") return;
        yield* Effect.sleep(options.retryDelay);
      }
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
          // An event already moved this parent's check later.
          if (yield* FiberMap.has(timers, parentThreadId)) continue;
          yield* check(parentThreadId).pipe(
            Effect.catchCause((cause) =>
              Effect.logWarning("delegated result reminder check failed", {
                threadId: parentThreadId,
                cause,
              }),
            ),
          );
        }
      }),
    );
  });

  return { check, start, triggers, watched };
});

/** Started with the server's other workers, from `ForkLayers.layerRuntimeServices` (fork/forkLayers.ts). */
export const layer = Layer.effectDiscard(
  make().pipe(Effect.flatMap((service) => service.start())),
).pipe(
  Layer.provide(
    Layer.mergeAll(
      ProjectionStore.layer,
      CommandReceiptStore.layerFromApplicationReceipts,
      DelegatedResultsStore.layer,
    ),
  ),
);
