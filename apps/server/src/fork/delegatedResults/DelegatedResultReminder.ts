/**
 * Fork (issue 386): backstop for upstream's completion wake. A parent's own events (a task ending
 * owed, a run ending) start one delayed check; an idle parent with results still owed gets one queued
 * message, ids from the task ids, so reruns and restarts never repeat it. Off with `server.waitFor`.
 */
import type { OrchestrationV2Subagent, ThreadId } from "@t3tools/contracts";
import * as Clock from "effect/Clock";
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
import {
  isDelegatedTask,
  isEndedTask,
  isRemindable,
  parentBusy,
  parentGone,
  reminderIds,
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
    const ids = reminderIds(
      parentThreadId,
      owed.map((task) => task.id),
    );
    const result = yield* delivery.deliver({
      threadId: parentThreadId,
      messageId: ids.messageId,
      commandId: ids.commandId,
      text: reminderText(owed),
    });
    switch (result.type) {
      case "delivered":
        watched.delete(parentThreadId);
        return { type: "sent", taskIds: owed.map((task) => task.id) } satisfies CheckOutcome;
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
      case "retry":
        watched.add(parentThreadId);
        return { type: "unreadable" } satisfies CheckOutcome;
    }
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

  /** A task that ended with its result owed, or a run end of a watched parent. */
  const triggers = (event: {
    readonly type: string;
    readonly threadId: ThreadId;
    readonly payload: unknown;
  }): boolean => {
    if (event.type === "subagent.updated") {
      const task = event.payload as OrchestrationV2Subagent;
      return isDelegatedTask(task) && isEndedTask(task) && resultDelivery(task) === "owed";
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
    // Results owed when the server stopped: upstream offers their wakes again at start; a parent
    // that still has one owed after the delay gets its reminder. One parent at a time.
    yield* forkParked(
      Effect.gen(function* () {
        const parents = yield* projections
          .getRecoveryThreadIds("delegated-completions")
          .pipe(Effect.orElseSucceed((): ReadonlyArray<ThreadId> => []));
        yield* Effect.sleep(options.delay);
        for (const parentThreadId of new Set(parents)) {
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
    Layer.merge(ProjectionStore.layer, CommandReceiptStore.layerFromApplicationReceipts),
  ),
);
