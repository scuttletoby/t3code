/**
 * Fork: every route that starts an agent takes a slot from the gate `delegate_task` uses
 * (`slots/SlotGate.ts`): same count. A start no thread here asks for (a scheduled run, the owner's
 * client, a linked machine) has no caller. Each route words a refusal for who reads it: only an
 * agent that asked is told to wait for a slot. Routes: docs/fork/README.md.
 */
import {
  type ModelSelection,
  type OrchestratorMcpCreateThreadRequest,
  OrchestratorMcpFailure,
  type ProviderInstanceId,
  type ScheduledTask,
  ScheduledTaskError,
  type ThreadId,
} from "@t3tools/contracts";
import { slotRefusalReason, type SlotDecision } from "@t3tools/shared/agentSlots";
import * as Context from "effect/Context";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";

import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import { encodePendingTask } from "../scheduledTasks/webhookDedupe.ts";
import type { WebhookRequest } from "../scheduledTasks/webhookTemplate.ts";
import * as Scheduler from "../scheduling/Scheduler.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import { makeScheduledTaskMcpProfiles } from "./scheduledTaskMcpProfile.ts";

type Refused = Extract<SlotGate.SlotAcquire, { readonly type: "refuse" }>;
type FullDecision = Extract<SlotDecision, { readonly type: "refuse" }>;

/** One start through the gate. */
export type RouteSlot =
  | Refused
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
  /** More starts right after this one: allowed only when they all fit (`SlotGate.acquire`). */
  readonly alsoNeeds?: ReadonlyArray<ProviderInstanceId> | undefined;
  /** A message into this existing thread: it takes a slot only if the thread shows no run. */
  readonly intoThreadId?: ThreadId | undefined;
  /** The caller is the woken thread itself: its held slot stays held (`SlotGate.acquire`). */
  readonly keepsHeld?: boolean | undefined;
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
            ...(start.alsoNeeds === undefined ? {} : { alsoNeeds: start.alsoNeeds }),
            ...(start.intoThreadId === undefined ? {} : { intoThreadId: start.intoThreadId }),
            ...(start.keepsHeld === undefined ? {} : { keepsHeld: start.keepsHeld }),
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

const agents = (count: number) => (count === 1 ? "1 agent is" : `${count} agents are`);

const unreadReason = (problem: SlotGate.CountReadProblem) =>
  problem === "timeout"
    ? `the read took longer than ${Duration.toSeconds(SlotGate.COUNT_READ_TIMEOUT)} seconds`
    : "the read failed with an error";

/** {@link slotRefusalReason}, for a reader on another machine ("there", not "this machine"). */
const reasonThere = (decision: FullDecision) =>
  decision.rule === "machine"
    ? `${agents(decision.used)} running there, and the limit is ${decision.max}`
    : decision.rule === "account"
      ? `${agents(decision.used)} running there on account ${decision.instanceId}, and the limit is ${decision.max}`
      : `only ${decision.used} MB of memory is free there, and a new agent needs at least ${decision.max} MB`;

/** Why, in one clause, for a reader on this machine (a scheduled task's history, run now, logs). */
export const reasonHere = (refusal: SlotGate.SlotRefusal) =>
  refusal.type === "full"
    ? slotRefusalReason(refusal.decision)
    : `the running agents could not be counted (${unreadReason(refusal.problem)})`;

/**
 * The refusal of a start no agent here asked for (a linked machine or the owner's client): it
 * says what happened on the machine that was asked, and gives no advice to wait or retry.
 */
export const noCallerRefusalText = (refusal: SlotGate.SlotRefusal): string =>
  refusal.type === "full"
    ? `No agent was started: the machine asked to start it has no free agent slot (${reasonThere(refusal.decision)}).`
    : `No agent was started: the machine asked to start it could not count its running agents (${unreadReason(refusal.problem)}).`;

/** The refusal for a route: an agent that asked gets the gate's own text (it may wait for a slot). */
const refusalFor = (callerThreadId: ThreadId | null, slot: Refused) =>
  callerThreadId === null ? noCallerRefusalText(slot.refusal) : slot.message;

/** {@link admit} for a start whose thread is known, refused with `slots_full`. */
const admitNamed = (
  gate: Option.Option<SlotGate.SlotGate["Service"]>,
  start: RouteStart & { readonly childThreadId: ThreadId },
): Effect.Effect<void, OrchestratorMcpFailure> =>
  Effect.flatMap(admit(gate, start), (slot) =>
    slot.type === "refuse"
      ? Effect.fail(slotsFull(refusalFor(start.callerThreadId, slot)))
      : Effect.void,
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

/** A message into an existing thread, as the gate is asked about it. */
export interface SendSlot {
  /** The thread here that sends it; `null` for the owner's client, a linked machine or the server. */
  readonly callerThreadId: ThreadId | null;
  readonly threadId: ThreadId;
  /** The thread's provider instance, where its next run starts. */
  readonly instanceId: ProviderInstanceId;
  /** The receiver is the caller (a wait's message): its held slot stays held. */
  readonly keepsHeld?: boolean | undefined;
}

/**
 * A message into an existing thread: into one that shows a run it only queues; into one that
 * shows none it takes a slot like `delegate_task`. One gate step decides both, so a run that
 * ends between the check and the send never starts one without a slot. Asked last before the send.
 */
export const admitSend = (
  gate: Option.Option<SlotGate.SlotGate["Service"]>,
  send: SendSlot,
): Effect.Effect<RouteSlot> =>
  admit(gate, {
    callerThreadId: send.callerThreadId,
    targetInstanceId: send.instanceId,
    intoThreadId: send.threadId,
    childThreadId: send.threadId,
    ...(send.keepsHeld === undefined ? {} : { keepsHeld: send.keepsHeld }),
  });

/**
 * `t3_thread_send` and `t3_thread_send_attachments`, last before the send: {@link admitSend},
 * refused `slots_full` with nothing sent (an agent that asked is told to wait for a slot; a
 * caller without a thread here is told what happened only). A thread that is gone, archived or
 * unknown is left to the send, which refuses it with its own error.
 */
export const checkSendSlot = Effect.fn("AgentSlotRoutes.checkSendSlot")(function* (
  callerThreadId: ThreadId | undefined,
  threadId: ThreadId,
) {
  const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
  if (Option.isNone(gate)) return;
  const threads = yield* ThreadManagementService.ThreadManagementService;
  const shell = yield* threads.getThreadShell(threadId).pipe(
    Effect.mapError(
      (error) =>
        new OrchestratorMcpFailure({
          code: "orchestration_error",
          message: `This call sent nothing to thread ${threadId}: it could not be read to check for a free agent slot (${error.message}).`,
        }),
    ),
  );
  if (shell === null || shell.deletedAt !== null || shell.archivedAt !== null) return;
  const caller = callerThreadId ?? null;
  const slot = yield* admitSend(gate, {
    callerThreadId: caller,
    threadId,
    instanceId: shell.modelSelection.instanceId,
  });
  if (slot.type === "refuse") {
    return yield* slotsFull(
      `This call sent nothing to thread ${threadId}. ${refusalFor(caller, slot)}`,
    );
  }
});

/** One thread of a `create_threads` call, as its seat sees it. */
export interface CreateThreadSlot {
  readonly callerThreadId: ThreadId;
  /** Its id; the batch's ids differ only in the index at the end. */
  readonly threadId: ThreadId;
  readonly index: number;
  readonly batch: ReadonlyArray<OrchestratorMcpCreateThreadRequest>;
  /** Its final target's instance. */
  readonly targetInstanceId: ProviderInstanceId;
  /** The instance a later thread without its own target is counted on (the parent's). */
  readonly defaultInstanceId: ProviderInstanceId;
  /** A retry of an accepted creation: `hasRun` says whether its run already started. */
  readonly retry: { readonly hasRun: Effect.Effect<boolean> } | null;
}

const startsRun = (request: OrchestratorMcpCreateThreadRequest) => request.prompt !== undefined;

/**
 * `create_threads`, per thread before its creation: the first checks the whole batch fits; each
 * thread with a prompt then takes its own slot. A later refusal names the threads already created;
 * a retried thread whose run never started takes a slot like a new one.
 */
export const admitCreateThread = Effect.fn("AgentSlotRoutes.admitCreateThread")(function* (
  gate: Option.Option<SlotGate.SlotGate["Service"]>,
  input: CreateThreadSlot,
) {
  if (Option.isNone(gate)) return;
  const own = input.batch[input.index];
  if (own === undefined) return;
  const later = input.batch
    .slice(input.index + 1)
    .filter(startsRun)
    .map((request) => request.target?.providerInstanceId ?? input.defaultInstanceId);
  const created = Array.from({ length: input.index }, (_, index) =>
    String(input.threadId).replace(/\d+$/, String(index)),
  );
  const refuse = (slot: Refused) => {
    const batch = slot.refusal.type === "full" ? slot.refusal.batch : undefined;
    const head =
      created.length > 0
        ? `Thread ${input.index + 1} was not created. Created before it in this call: ${created.join(", ")}.`
        : batch === undefined
          ? "No thread was created."
          : `No thread was created: the ${batch.needed} threads with a prompt need ${batch.needed} free agent slots, and ${batch.free} ${batch.free === 1 ? "is" : "are"} free.`;
    return Effect.fail(slotsFull(`${head} ${slot.message}`));
  };
  if (input.retry !== null) {
    if (!startsRun(own) || (yield* input.retry.hasRun)) return;
  } else if (!startsRun(own)) {
    // A thread without a prompt takes no slot; the first one still checks the whole batch.
    if (input.index > 0 || later.length === 0) return;
    const [first, ...rest] = later;
    const check = yield* admit(gate, {
      callerThreadId: input.callerThreadId,
      targetInstanceId: first!,
      alsoNeeds: rest,
    });
    if (check.type === "refuse") return yield* refuse(check);
    // Only a check: the threads with a prompt take their own slots.
    yield* check.end(null);
    return;
  }
  const slot = yield* admit(gate, {
    callerThreadId: input.callerThreadId,
    targetInstanceId: input.targetInstanceId,
    childThreadId: input.threadId,
    ...(later.length === 0 ? {} : { alsoNeeds: later }),
  });
  if (slot.type === "refuse") return yield* refuse(slot);
});

/** What a scheduled run that was let through owes the gate, or the run's result when skipped. */
export interface ScheduledSlot {
  /** `runTask`'s result for a run on its schedule that waits for a slot; else `null`. */
  readonly skipped: {
    readonly task: ScheduledTask;
    readonly threadId: null;
    readonly dispatchFailed: false;
  } | null;
  /** The dispatch ended, with the thread it ran in or `null`. */
  readonly end: (threadId: ThreadId | null) => Effect.Effect<void>;
}

/** How a waiting run's history note starts; one note per due time (a run clears it). */
export const WAITING_NOTE_PREFIX = "Waiting for an agent slot: ";

/** The history note of a run on its schedule that waits for a slot. */
export const waitingNote = (refusal: SlotGate.SlotRefusal) =>
  `${WAITING_NOTE_PREFIX}${reasonHere(refusal)}. The run starts when a slot is free.`;

/** How a refused "run now" starts; `runNowSlotRefusal` answers it `slots_full`. */
export const RUN_NOW_REFUSED_PREFIX = "Not run, no agent slot: ";

/** `run_scheduled_task_now`: a run the gate refused answers `slots_full`, its text unchanged. */
export const runNowSlotRefusal = (error: OrchestratorMcpFailure): OrchestratorMcpFailure =>
  error.code === "orchestration_error" && error.message.startsWith(RUN_NOW_REFUSED_PREFIX)
    ? slotsFull(error.message)
    : error;

/**
 * A direct webhook delivery waiting in memory (its store failed) whose task was saved meanwhile, and
 * that could not be stored again: its prompt is the one from before the save, so it is not run.
 */
export const SAVED_WHILE_WAITING_NOT_STORED =
  "Not run: this task was saved again while this delivery waited for an agent slot, and the delivery could not be stored to run on the task as it is now (the server log says why), so it was not started: it would have used the task as it was before. The next webhook delivery uses the task as it is now.";

/** How often a webhook delivery that waits for a slot asks again (the scheduler's poll). */
export const WEBHOOK_SLOT_RETRY = Duration.seconds(5);

/**
 * A fixed-time run is dropped as missed once it is 10 minutes late (`isMissedFixedTimeRun`); one
 * that waits for a slot moves its due time up to now after half of that, so it stays due.
 */
const FIXED_TIME_KEEP_DUE_MS = 5 * 60 * 1000;

/**
 * A direct webhook delivery (no coalescing window) on its way to its run, set by
 * `directDelivery` around that run: what storing it needs, if the gate refuses it.
 */
interface DirectDelivery {
  readonly deliveryId: string;
  readonly request: WebhookRequest;
  /** Wakes the stored-delivery worker (its due hint), so it looks at the new row. */
  readonly wake: Effect.Effect<void>;
}

const CurrentDirectDelivery = Context.Reference<DirectDelivery | undefined>(
  "t3/fork/agentSlotRoutes/CurrentDirectDelivery",
  { defaultValue: () => undefined },
);

/** A stored delivery's request, in the shape the stored-delivery worker reads (`decodePendingRequest`). */
const encodeStoredRequest = Schema.encodeEffect(
  Schema.fromJsonString(
    Schema.Struct({
      method: Schema.String,
      path: Schema.String,
      query: Schema.String,
      headers: Schema.Record(Schema.String, Schema.String),
      bodyText: Schema.String,
    }),
  ),
);

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
  const sql = yield* SqlClient.SqlClient;
  const scheduler = yield* Scheduler.Scheduler;
  // The production wiring gives scheduled runs the gate; a boot test looks for this span.
  if (Option.isSome(gate))
    yield* Effect.void.pipe(Effect.withSpan("AgentSlotRoutes.scheduledRunsGated"));
  // Who saved each task, and the run check on it: built here, where the service's one fork
  // line already builds the slots, so the service needs no second build line.
  const profiles = yield* makeScheduledTaskMcpProfiles(threads);

  /**
   * The slot each running task holds until its dispatch ends (one run per task at a time), so
   * `ended` frees it on a path that never reached the end (a failure before the dispatch).
   */
  const open = new Map<string, (threadId: ThreadId | null) => Effect.Effect<void>>();

  /**
   * Tasks whose stored webhook delivery was not started because the task was saved while it waited
   * for a slot (`waitForWebhookSlot`): the stored-delivery worker keeps its row (`keepStored`).
   */
  const keep = new Set<string>();

  /**
   * Where a run starts: a bound task runs on its thread's current provider (a thread that cannot
   * be read: the task's stored provider). Whether a bound run starts one is the gate's to say,
   * from the same count it decides on (`intoThreadId`): into a thread that shows a run the prompt
   * only queues (no slot); into one that shows none it is a start.
   */
  const targetOf = (task: ScheduledTask) =>
    task.threadId === null
      ? Effect.succeed({ instanceId: task.modelSelection.instanceId, intoThreadId: undefined })
      : threads.getThreadShell(task.threadId).pipe(
          Effect.map((shell) => shell?.modelSelection.instanceId ?? task.modelSelection.instanceId),
          Effect.orElseSucceed(() => task.modelSelection.instanceId),
          Effect.map((instanceId) => ({ instanceId, intoThreadId: task.threadId ?? undefined })),
        );

  /**
   * The slot is named for the run's thread when its dispatch ends (`end`), bound or new, so a
   * dispatch that failed frees it at once; `ended` frees it on a path that never got there.
   */
  const take = (target: {
    readonly instanceId: ProviderInstanceId;
    readonly intoThreadId: ThreadId | undefined;
  }) =>
    admit(gate, {
      callerThreadId: null,
      targetInstanceId: target.instanceId,
      intoThreadId: target.intoThreadId,
    });

  const allowed = (
    task: ScheduledTask,
    end: (threadId: ThreadId | null) => Effect.Effect<void>,
  ) => {
    const once = (threadId: ThreadId | null) =>
      Effect.suspend(() => {
        if (open.get(task.id) !== once) return Effect.void;
        open.delete(task.id);
        return end(threadId);
      });
    open.set(task.id, once);
    return { skipped: null, end: once } satisfies ScheduledSlot;
  };

  /**
   * A run on its schedule that waits: the task stays due (the next poll asks again), with one
   * history note that says why (rewritten when the reason changes; the run that starts clears it,
   * and so does `clearStaleNotes` once the task is paused or no longer due). Its
   * status stays as it is, so nothing counts as a run. A fixed-time run keeps its due time recent.
   */
  const keepDue = Effect.fnUntraced(function* (
    task: ScheduledTask,
    now: DateTime.DateTime,
    refusal: SlotGate.SlotRefusal,
  ) {
    const nowIso = DateTime.formatIso(DateTime.toUtc(now));
    const note = waitingNote(refusal);
    // Written only when its text changes (a new reason), so a poll that changes nothing writes nothing.
    const noted = yield* sql<{ task_id: string }>`
      UPDATE scheduled_tasks SET last_run_error = ${note}, updated_at = ${nowIso}
      WHERE task_id = ${task.id} AND created_at = ${task.createdAt}
        AND next_run_at IS ${task.nextRunAt} AND last_run_status <> 'running'
        AND last_run_error IS NOT ${note}
      RETURNING task_id
    `;
    if (noted.length > 0) {
      yield* Effect.logInfo("Scheduled run waits for an agent slot", {
        taskId: task.id,
        dueAt: task.nextRunAt,
        reason: reasonHere(refusal),
      });
    }
    if (task.schedule.type !== "fixed_time" || task.nextRunAt === null) return;
    const dueMs = Date.parse(task.nextRunAt);
    if (!(DateTime.toEpochMillis(now) - dueMs > FIXED_TIME_KEEP_DUE_MS)) return;
    yield* sql`
      UPDATE scheduled_tasks SET next_run_at = ${nowIso}
      WHERE task_id = ${task.id} AND created_at = ${task.createdAt}
        AND next_run_at IS ${task.nextRunAt} AND last_run_status <> 'running'
    `;
  });

  /** Why a webhook delivery that waited no longer runs, or `null` while its task still takes it. */
  const webhookGone = Effect.fnUntraced(function* (task: ScheduledTask) {
    const [row] = yield* sql<{ created_at: string; enabled: number; trigger: string | null }>`
      SELECT created_at, enabled, json_extract(schedule_json, '$.type') AS trigger
      FROM scheduled_tasks WHERE task_id = ${task.id}
    `;
    return row === undefined
      ? "The task was deleted before this delivery ran."
      : row.created_at !== task.createdAt
        ? "The task was replaced before this delivery ran."
        : row.trigger !== "webhook"
          ? "The task's trigger changed before this delivery ran."
          : !row.enabled
            ? "The task was paused before this delivery ran."
            : null;
  });

  /**
   * The row was written since `task` was read (a save while a delivery waited; the prompt too, as a
   * save in the same millisecond keeps `updated_at`), or cannot be read: never run on this copy. A
   * bind writes only the thread, but not during a wait: a webhook task's runs take turns.
   */
  const writtenSince = (task: ScheduledTask) =>
    sql<{ updated_at: string; prompt: string }>`
      SELECT updated_at, prompt FROM scheduled_tasks WHERE task_id = ${task.id}
    `.pipe(
      Effect.map(
        ([row]) =>
          row === undefined || row.updated_at !== task.updatedAt || row.prompt !== task.prompt,
      ),
      Effect.orElseSucceed(() => true),
    );

  /** A webhook delivery the stored-delivery worker owns now: this run of it ends unstarted. */
  const handedToWorker = (task: ScheduledTask): ScheduledSlot => ({
    skipped: { task, threadId: null, dispatchFailed: false },
    end: () => Effect.void,
  });

  /**
   * A direct delivery the gate refused is stored as a pending delivery the stored-delivery worker
   * owns (due now), so a restart keeps it; its in-memory copy then ends without running, and the
   * worker runs it once. `false` when there is nothing to store (a stored delivery already) or the
   * write failed: the delivery then waits in memory.
   */
  const storeDirect = Effect.fn("AgentSlotRoutes.storeDirect")(
    function* (task: ScheduledTask, now: DateTime.DateTime) {
      const direct = yield* CurrentDirectDelivery;
      if (direct === undefined) return false;
      const { method, path, query, headers, bodyText } = direct.request;
      const requestJson = yield* encodeStoredRequest({ method, path, query, headers, bodyText });
      const stored = yield* sql<{ delivery_id: string }>`
        INSERT INTO scheduled_task_webhook_pending
          (delivery_id, task_id, coalesce_key, due_at, state, task_json, request_json, delivery_ids_json)
        VALUES (${direct.deliveryId}, ${task.id}, '', ${DateTime.toEpochMillis(now)}, 'dispatching',
          ${yield* encodePendingTask(task)}, ${requestJson}, json_array(${direct.deliveryId}))
        ON CONFLICT (delivery_id) DO NOTHING
        RETURNING delivery_id
      `;
      if (stored.length === 0) return false;
      yield* direct.wake;
      yield* Effect.logInfo("Webhook delivery waits for an agent slot (stored)", {
        taskId: task.id,
        deliveryId: direct.deliveryId,
      });
      return true;
    },
    (effect, task) =>
      effect.pipe(
        Effect.catchCause((cause) =>
          Effect.logWarning("Could not store a webhook delivery waiting for a slot", {
            taskId: task.id,
            cause,
          }).pipe(Effect.as(false)),
        ),
      ),
  );

  /**
   * A stored webhook delivery waits for a slot, asked again every few seconds (a restart picks its
   * row up again, once). Dropped only as a queued delivery is (task deleted, replaced, paused, not a
   * webhook). Saved while it waits, it is handed back to the worker to run on the task as saved.
   */
  const waitForWebhookSlot = Effect.fnUntraced(function* (task: ScheduledTask, first: Refused) {
    yield* Effect.logInfo("Webhook delivery waits for an agent slot", {
      taskId: task.id,
      reason: reasonHere(first.refusal),
    });
    while (true) {
      yield* Effect.sleep(WEBHOOK_SLOT_RETRY);
      const gone = yield* webhookGone(task).pipe(Effect.orElseSucceed(() => null));
      if (gone !== null) return yield* Effect.fail(new WebhookSkipped({ reason: gone }));
      if (yield* writtenSince(task)) {
        if ((yield* CurrentDirectDelivery) === undefined) keep.add(task.id);
        else if (!(yield* storeDirect(task, yield* DateTime.now)))
          return yield* Effect.fail(new WebhookSkipped({ reason: SAVED_WHILE_WAITING_NOT_STORED }));
        return handedToWorker(task);
      }
      // Checked again as it would start: the owner may have widened the project meanwhile.
      const refused = task.threadId === null ? yield* profiles.runRefusal(task) : null;
      if (refused !== null) return yield* Effect.fail(new WebhookSkipped({ reason: refused }));
      // A bound thread that shows a run only queues: the gate lets it pass (`intoThreadId`).
      const slot = yield* take(yield* targetOf(task));
      if (slot.type === "allow") return allowed(task, slot.end);
    }
  });

  /**
   * Last before the run is marked running. Refused, a run on its schedule stays due (asked again
   * at the next poll, never dropped, one run when it starts), "run now" fails and writes nothing,
   * a webhook delivery waits for a slot.
   */
  const admitRun = Effect.fnUntraced(function* <E, R>(
    task: ScheduledTask,
    trigger: "scheduled" | "manual" | "webhook",
    now: DateTime.DateTime,
    // The missed-run rescheduler: a run that waits is not rescheduled; a refused one is.
    reschedule: (
      task: ScheduledTask,
      now: DateTime.DateTime,
      reason: string,
      runError: string,
    ) => Effect.Effect<void, E, R>,
  ) {
    // A new thread wider than the profile the task was saved with starts nothing: on its schedule
    // a failed run with the reason (moved to its next time), "run now" fails, a webhook skips.
    const profileRefusal = task.threadId === null ? yield* profiles.runRefusal(task) : null;
    if (profileRefusal !== null) {
      if (trigger === "manual") {
        return yield* new ScheduledTaskError({ message: profileRefusal, taskId: task.id });
      }
      if (trigger === "webhook")
        return yield* Effect.fail(new WebhookSkipped({ reason: profileRefusal }));
      yield* reschedule(task, now, "MCP profile", profileRefusal).pipe(
        Effect.catchCause((cause) =>
          Effect.logWarning("Could not record a scheduled run refused for its MCP profile", {
            taskId: task.id,
            cause,
          }),
        ),
      );
      return {
        skipped: { task, threadId: null, dispatchFailed: false },
        end: () => Effect.void,
      } as ScheduledSlot;
    }
    if (Option.isNone(gate)) return { skipped: null, end: () => Effect.void } as ScheduledSlot;
    const slot = yield* take(yield* targetOf(task));
    if (slot.type === "allow") return allowed(task, slot.end);
    if (trigger === "manual") {
      return yield* new ScheduledTaskError({
        message: `${RUN_NOW_REFUSED_PREFIX}${reasonHere(slot.refusal)}.`,
        taskId: task.id,
      });
    }
    if (trigger === "webhook") {
      if (yield* storeDirect(task, now)) return handedToWorker(task);
      return yield* waitForWebhookSlot(task, slot);
    }
    yield* keepDue(task, now, slot.refusal).pipe(
      Effect.catchCause((cause) =>
        Effect.logWarning("Could not note a scheduled run waiting for a slot", {
          taskId: task.id,
          cause,
        }),
      ),
    );
    return {
      skipped: { task, threadId: null, dispatchFailed: false },
      end: () => Effect.void,
    } as ScheduledSlot;
  });

  /**
   * The stored-delivery worker, after a delivery's run returned: `true` when that run was not
   * started because its task was saved while it waited, so the worker keeps the row and runs it
   * again on the task as it is now, instead of removing it. Read once.
   */
  const keepStored = (taskId: string) => Effect.sync(() => keep.delete(taskId));

  /** The task's run ended, any way: a slot its dispatch never ended is free again. */
  const ended = (taskId: string) => Effect.suspend(() => open.get(taskId)?.(null) ?? Effect.void);

  /**
   * Around a direct webhook delivery's run (inside its task's webhook permit): refused for a slot,
   * the delivery is stored for the stored-delivery worker (`storeDirect`), woken with `wake`.
   */
  const directDelivery =
    (deliveryId: string, request: WebhookRequest, wake: Effect.Effect<void>) =>
    <A, E, R>(effect: Effect.Effect<A, E, R>) =>
      Effect.provideService(effect, CurrentDirectDelivery, { deliveryId, request, wake });

  /**
   * A waiting note is cleared once its task no longer waits: paused, or its due time moved
   * later (an edit). The next due time that waits writes a fresh note.
   */
  const clearStaleNotes = Effect.gen(function* () {
    const nowIso = DateTime.formatIso(DateTime.toUtc(yield* DateTime.now));
    yield* sql`
      UPDATE scheduled_tasks SET last_run_error = NULL
      WHERE substr(last_run_error, 1, ${WAITING_NOTE_PREFIX.length}) = ${WAITING_NOTE_PREFIX}
        AND last_run_status <> 'running'
        AND (enabled = 0 OR next_run_at IS NULL OR next_run_at > ${nowIso})
    `;
  });
  yield* scheduler.register("agent-slot-notes", clearStaleNotes);

  return {
    admit: admitRun,
    ended,
    directDelivery,
    keepStored,
    /** `ScheduledTaskService.upsert`, last before it writes the row (`scheduledTaskMcpProfile.ts`). */
    saving: profiles.saving,
  };
});
