/**
 * Fork: every route that starts an agent takes a slot from the gate `delegate_task` uses
 * (`slots/SlotGate.ts`): same count, same refusal. A start no thread here asks for (a scheduled
 * run, the owner's client, a linked machine) has no caller. Routes: docs/fork/README.md.
 */
import {
  type ModelSelection,
  OrchestratorMcpFailure,
  type ProviderInstanceId,
  type ScheduledTask,
  ScheduledTaskError,
  type ThreadId,
} from "@t3tools/contracts";
import type * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as SlotGate from "../slots/SlotGate.ts";

/** One start through the gate. */
export type RouteSlot =
  | { readonly type: "refuse"; readonly message: string }
  | {
      readonly type: "allow";
      /**
       * The dispatch ended: `childThreadId` is the thread it started, or `null` when it started
       * none. Until then the start counts (at most `DISPATCH_SAFETY_MS` if never called). Safe to
       * call more than once.
       */
      readonly end: (childThreadId: ThreadId | null) => Effect.Effect<void>;
    };

export interface RouteStart {
  readonly callerThreadId: ThreadId | null;
  readonly targetInstanceId: ProviderInstanceId;
  /**
   * The thread the start will run in, when known before its dispatch: the slot is named for it at
   * once, and counts until its run shows (for the start grace at most), so nothing is left to end.
   */
  readonly childThreadId?: ThreadId | undefined;
}

const ENDED: RouteSlot = { type: "allow", end: () => Effect.void };

/**
 * Takes a slot for one start, or says why not. No gate (a host that runs none) allows at once.
 * Uninterruptible from the acquire to the naming of the child, so a slot is never taken unnamed
 * by a call that stopped in between; a call stopped after it counts as a start never released.
 */
const admit = (
  gate: Option.Option<SlotGate.SlotGate["Service"]>,
  start: RouteStart,
): Effect.Effect<RouteSlot> =>
  Option.match(gate, {
    onNone: () => Effect.succeed(ENDED),
    onSome: (slots) =>
      Effect.uninterruptible(
        Effect.gen(function* () {
          const outcome = yield* slots.acquire({
            callerThreadId: start.callerThreadId,
            targetInstanceId: start.targetInstanceId,
          });
          if (outcome.type === "refuse") return outcome;
          const token = outcome.token;
          const end = (childThreadId: ThreadId | null) =>
            (childThreadId === null ? Effect.void : slots.started(token, childThreadId)).pipe(
              Effect.andThen(slots.release(token)),
            );
          if (start.childThreadId === undefined) return { type: "allow", end } satisfies RouteSlot;
          yield* end(start.childThreadId);
          return ENDED;
        }),
      ),
  });

const slotsFull = (message: string) => new OrchestratorMcpFailure({ code: "slots_full", message });

/** {@link admit} for a start whose thread is known, refused with `slots_full`. */
export const admitNamed = (
  gate: Option.Option<SlotGate.SlotGate["Service"]>,
  start: RouteStart & { readonly childThreadId: ThreadId },
): Effect.Effect<void, OrchestratorMcpFailure> =>
  Effect.flatMap(admit(gate, start), (slot) =>
    slot.type === "refuse" ? Effect.fail(slotsFull(slot.message)) : Effect.void,
  );

export interface LaunchSlot {
  readonly threadId: ThreadId;
  readonly modelSelection: Pick<ModelSelection, "instanceId">;
  /** A retry of an accepted launch: it starts nothing new, so it is not counted again. */
  readonly replaying: boolean;
  readonly input: { readonly message?: string | undefined };
  readonly attachments: ReadonlyArray<unknown>;
}

/**
 * `t3_thread_launch`, last before the launch: a launch that starts a run (a message or an
 * attachment) takes a slot on its model's instance, or is refused `slots_full`. `caller` is the
 * thread here that asks, if any; a launch from a linked machine or the owner's client has none.
 * The gate is the one the handlers were built with (none in a host that runs none).
 */
export const checkLaunchSlot = Effect.fn("AgentSlotRoutes.checkLaunchSlot")(function* (
  caller: { readonly id: ThreadId } | undefined,
  launch: LaunchSlot,
) {
  if (launch.replaying) return;
  if (launch.input.message === undefined && launch.attachments.length === 0) return;
  yield* admitNamed(yield* Effect.serviceOption(SlotGate.SlotGate), {
    callerThreadId: caller?.id ?? null,
    targetInstanceId: launch.modelSelection.instanceId,
    childThreadId: launch.threadId,
  });
});

/** What a scheduled run that was let through owes the gate, or the run's result when skipped. */
export interface ScheduledSlot {
  /** `runTask`'s result for a run on its schedule that was skipped (recorded); else `null`. */
  readonly skipped: {
    readonly task: ScheduledTask;
    readonly threadId: null;
    readonly dispatchFailed: false;
  } | null;
  /** The dispatch ended, with the thread it ran in or `null`. */
  readonly end: (threadId: ThreadId | null) => Effect.Effect<void>;
}

/** The text a skipped run on its schedule records as its last error. */
export const skippedRunError = (message: string) => `Skipped: ${message}`;

/**
 * Scheduled runs through the gate, built inside `ScheduledTaskService` (the gate is read if the
 * server runs one; a test of the service alone has none). `WebhookSkipped` is the service's own
 * "delivery skipped" failure.
 */
export const makeScheduledRunSlots = Effect.fnUntraced(function* <W>(
  WebhookSkipped: new (fields: { readonly reason: string }) => W,
) {
  const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
  const threads = yield* ThreadManagementService.ThreadManagementService;

  /**
   * A bound task runs on its thread's current provider; a thread that cannot be read is counted on
   * the task's stored one (the count is the same; only the account limit can differ).
   */
  const targetOf = (task: ScheduledTask) =>
    task.threadId === null
      ? Effect.succeed(task.modelSelection.instanceId)
      : threads.getThreadShell(task.threadId).pipe(
          Effect.map((shell) => shell?.modelSelection.instanceId ?? task.modelSelection.instanceId),
          Effect.orElseSucceed(() => task.modelSelection.instanceId),
        );

  /**
   * Last before the run is marked running. Refused: a run on its schedule is recorded as skipped
   * (`reschedule` writes the reason and the next time, as a missed run does; the run count stays),
   * "run now" fails with the reason and writes nothing, a webhook delivery fails as skipped.
   */
  const admitRun = Effect.fnUntraced(function* <E, R>(
    task: ScheduledTask,
    trigger: "scheduled" | "manual" | "webhook",
    now: DateTime.DateTime,
    reschedule: (
      task: ScheduledTask,
      now: DateTime.DateTime,
      reason: string,
      runError: string,
    ) => Effect.Effect<void, E, R>,
  ) {
    if (Option.isNone(gate)) return { skipped: null, end: () => Effect.void } as ScheduledSlot;
    const slot = yield* admit(gate, {
      callerThreadId: null,
      targetInstanceId: yield* targetOf(task),
      // A bound task runs in its thread; a new thread's id is known only after its launch.
      ...(task.threadId === null ? {} : { childThreadId: task.threadId }),
    });
    if (slot.type === "allow") return { skipped: null, end: slot.end } as ScheduledSlot;
    if (trigger === "manual") {
      return yield* new ScheduledTaskError({ message: slot.message, taskId: task.id });
    }
    if (trigger === "webhook")
      return yield* Effect.fail(new WebhookSkipped({ reason: slot.message }));
    yield* reschedule(task, now, "no agent slot free", skippedRunError(slot.message));
    return {
      skipped: { task, threadId: null, dispatchFailed: false },
      end: () => Effect.void,
    } as ScheduledSlot;
  });

  return { admit: admitRun };
});
