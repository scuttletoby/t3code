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
  ScheduledTask,
  ScheduledTaskError,
  type ThreadId,
} from "@t3tools/contracts";
import {
  capacityRefusalReason,
  slotRefusalReason,
  slotRunningText,
  type SlotDecision,
} from "@t3tools/shared/agentSlots";
import * as Cause from "effect/Cause";
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
import { checkLinkedLaunchPacing } from "./linkedLaunchPacing.ts";
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
  /** Its place in the gate's one line: a start that asks again while refused. */
  readonly place?: SlotGate.SlotPlace | undefined;
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
            ...(start.place === undefined ? {} : { place: start.place }),
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

const unreadReason = (problem: SlotGate.CountReadProblem) =>
  problem === "timeout"
    ? `the read took longer than ${Duration.toSeconds(SlotGate.COUNT_READ_TIMEOUT)} seconds`
    : "the read failed with an error";

/** {@link slotRefusalReason}, for a reader on another machine ("there", not "this machine"). */
const reasonThere = (decision: FullDecision) =>
  decision.rule === "machine"
    ? `${slotRunningText(decision, " there")}, and the limit is ${decision.max}`
    : decision.rule === "account"
      ? `${slotRunningText(decision, ` there on account ${decision.instanceId}`)}, and the limit is ${decision.max}`
      : decision.rule === "capacity"
        ? capacityRefusalReason(decision.capacity, decision.used, decision.max, "that machine")
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

/** What `t3_thread_launch` gives its gate: the launch as asked. */
interface LaunchInput {
  readonly message?: string | undefined;
  readonly delegatedFrom?: unknown;
  readonly modelSelection?: unknown;
}

/**
 * `t3_thread_launch`: a launch that starts a run takes a slot, or is refused `slots_full`; a replay
 * is not counted again. A linked machine's delegated task is paced first (`linkedLaunchPacing.ts`);
 * the result, spread over the launch, carries the effort the owner's setting lowered.
 */
export const launchGate = Effect.fn("AgentSlotRoutes.launchGate")(function* (
  caller: { readonly id: ThreadId } | undefined,
  threadId: ThreadId,
  modelSelection: ModelSelection,
  replaying: boolean,
  input: LaunchInput,
  attachments: ReadonlyArray<unknown>,
): Effect.fn.Return<{ readonly modelSelection?: ModelSelection }, OrchestratorMcpFailure> {
  if (replaying) return {};
  if (input.message === undefined && attachments.length === 0) return {};
  const lowered = yield* checkLinkedLaunchPacing({
    modelSelection,
    delegatedFrom: input.delegatedFrom,
    namesModel: input.modelSelection !== undefined,
  });
  yield* admitNamed(yield* Effect.serviceOption(SlotGate.SlotGate), {
    callerThreadId: caller?.id ?? null,
    targetInstanceId: modelSelection.instanceId,
    childThreadId: threadId,
  });
  return lowered === undefined ? {} : { modelSelection: lowered };
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
  /** A wake-up that asks again while refused: its place in the gate's one line. */
  readonly place?: SlotGate.SlotPlace | undefined;
  /**
   * The caller ends the slot (`end`) when its dispatch ended. Until then the start counts whatever
   * the start grace, so a send that waits after the gate step still has its slot when it starts.
   */
  readonly endsItself?: boolean | undefined;
}

/** Waiters that no longer need a slot leave the gate's line (nothing without a gate). */
export const leaveLine = (
  gate: Option.Option<SlotGate.SlotGate["Service"]>,
  keys: ReadonlyArray<string>,
): Effect.Effect<void> =>
  Option.match(gate, { onNone: () => Effect.void, onSome: (slots) => slots.leave(keys) });

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
    ...(send.endsItself === true ? {} : { childThreadId: send.threadId }),
    ...(send.keepsHeld === undefined ? {} : { keepsHeld: send.keepsHeld }),
    ...(send.place === undefined ? {} : { place: send.place }),
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
 * Retries in a row (5 minutes) a waiting webhook delivery's task may stay unreadable before it is
 * skipped: it holds the task's webhook permit. Counted in retries, not clock time, so a host
 * asleep between two retries does not use it up.
 */
export const WEBHOOK_UNREADABLE_RETRIES = 60;

/** Why a waiting webhook delivery whose task stayed unreadable was skipped. */
export const UNREADABLE_WHILE_WAITING = `Not run: this task could not be read at ${WEBHOOK_UNREADABLE_RETRIES} retries in a row (${Duration.toMinutes(Duration.times(WEBHOOK_SLOT_RETRY, WEBHOOK_UNREADABLE_RETRIES))} minutes) while this delivery waited for an agent slot (the server log says why), so it was not started on a copy that could not be checked. The next webhook delivery reads the task again.`;

/** Why a waiting webhook delivery whose task's row is gone was skipped (as upstream says it). */
export const DELETED_WHILE_WAITING = "The task was deleted before this delivery ran.";

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

/**
 * Line places (`SlotGate.SlotPlace`): a waiting scheduled run's per task, at its due time; a webhook
 * delivery's per delivery, from its first ask. Each leaves as its run starts, given up or deleted
 * (`waitForWebhookSlot`, `leaveEndedPlaces` every 5 s), refused for its profile (`admitRun`), or as
 * its worker removes its stored row, after its run or before it (`keepStored`, `leaveStored`).
 */
const SCHEDULED_PLACE = "scheduled-run:";
const DELIVERY_PLACE = "webhook-delivery:";
/** A stored delivery whose row cannot be read: its task's (one stored delivery runs at a time). */
const DELIVERY_TASK_PLACE = "webhook-task:";

/**
 * What a run takes from its task's row, decoded as `ScheduledTaskService` decodes it (edge spaces
 * trimmed). Not run bookkeeping, the webhook token or what `webhookGone` checks: writing those is
 * not a save. A test holds this to the task's fields minus a named list left out.
 */
export const RunCopy = Schema.Struct({
  title: ScheduledTask.fields.title,
  prompt: ScheduledTask.fields.prompt,
  schedule: ScheduledTask.fields.schedule,
  projectId: ScheduledTask.fields.projectId,
  threadId: ScheduledTask.fields.threadId,
  threadMode: ScheduledTask.fields.threadMode,
  workspaceStrategy: ScheduledTask.fields.workspaceStrategy,
  modelSelection: ScheduledTask.fields.modelSelection,
  runtimeMode: ScheduledTask.fields.runtimeMode,
  interactionMode: ScheduledTask.fields.interactionMode,
  createdBy: ScheduledTask.fields.createdBy,
  creationSource: ScheduledTask.fields.creationSource,
});
/** The row's column for each `RunCopy` field: `savedSince` reads exactly these. */
const RUN_COPY_COLUMNS = {
  title: "title",
  prompt: "prompt",
  schedule: "schedule_json",
  projectId: "project_id",
  threadId: "thread_id",
  threadMode: "thread_mode",
  workspaceStrategy: "workspace_strategy_json",
  modelSelection: "model_selection_json",
  runtimeMode: "runtime_mode",
  interactionMode: "interaction_mode",
  createdBy: "created_by",
  creationSource: "creation_source",
} as const satisfies Record<keyof typeof RunCopy.fields, string>;
/** The same fields as the row stores them (its JSON columns as text). */
const decodeStoredCopy = Schema.decodeUnknownEffect(
  Schema.Struct({
    ...RunCopy.fields,
    schedule: Schema.fromJsonString(ScheduledTask.fields.schedule),
    workspaceStrategy: Schema.fromJsonString(ScheduledTask.fields.workspaceStrategy),
    modelSelection: Schema.fromJsonString(ScheduledTask.fields.modelSelection),
  }),
);
/** One text for a copy, to compare two. */
const runCopyText = Schema.encodeEffect(Schema.fromJsonString(RunCopy));
const decodePrompt = Schema.decodeUnknownEffect(ScheduledTask.fields.prompt);

/**
 * Whether a webhook delivery's task changed since its run read it (`savedSince`), `gone` when its
 * row is deleted, or `unknown` (why) when its row cannot be read.
 */
type SinceRead = "same" | "saved" | "gone" | { readonly unknown: Cause.Cause<unknown> };

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
  const take = (
    target: {
      readonly instanceId: ProviderInstanceId;
      readonly intoThreadId: ThreadId | undefined;
    },
    place: SlotGate.SlotPlace | undefined,
  ) =>
    admit(gate, {
      callerThreadId: null,
      targetInstanceId: target.instanceId,
      intoThreadId: target.intoThreadId,
      ...(place === undefined ? {} : { place }),
    });

  /** Webhook deliveries waiting in `waitForWebhookSlot` now: their wait leaves the line itself. */
  const waitingDeliveries = new Set<string>();

  /**
   * The place of the stored delivery its task's worker runs now (the first of its rows, in the
   * worker's order); unreadable or none, its task's place.
   */
  const storedPlace = (taskId: string) =>
    sql<{ delivery_id: string; due_at: number }>`
      SELECT delivery_id, due_at FROM scheduled_task_webhook_pending
      WHERE task_id = ${taskId} AND state IN ('dispatching', 'running', 'completed', 'failed')
      ORDER BY due_at, rowid LIMIT 1
    `.pipe(
      Effect.catchCause((cause) =>
        Effect.logWarning("Could not read a waiting webhook delivery's place in line", {
          taskId,
          cause,
        }).pipe(Effect.as([])),
      ),
      Effect.map(([stored]) =>
        stored === undefined
          ? ({ key: `${DELIVERY_TASK_PLACE}${taskId}` } satisfies SlotGate.SlotPlace)
          : ({
              key: `${DELIVERY_PLACE}${stored.delivery_id}`,
              sinceMs: Number(stored.due_at),
            } satisfies SlotGate.SlotPlace),
      ),
    );

  /**
   * The run's place in line: a run on its schedule at its due time, a webhook delivery at the
   * time it was stored. "Run now" asks once and has none. A stored delivery is the one its task's
   * worker runs now (one at a time per task, in the worker's order); unreadable, it is logged and
   * keeps its task's place from this ask (`waitForWebhookSlot` asks in it, and leaves it).
   */
  const placeOf = Effect.fnUntraced(function* (
    task: ScheduledTask,
    trigger: "scheduled" | "manual" | "webhook",
  ) {
    if (trigger === "manual") return undefined;
    if (trigger === "scheduled") {
      const dueMs = task.nextRunAt === null ? Number.NaN : Date.parse(task.nextRunAt);
      return {
        key: `${SCHEDULED_PLACE}${task.id}`,
        ...(Number.isFinite(dueMs) ? { sinceMs: dueMs } : {}),
      } satisfies SlotGate.SlotPlace;
    }
    const direct = yield* CurrentDirectDelivery;
    if (direct !== undefined) return { key: `${DELIVERY_PLACE}${direct.deliveryId}` };
    return yield* storedPlace(task.id);
  });

  const leave = (place: SlotGate.SlotPlace | undefined) =>
    place === undefined ? Effect.void : leaveLine(gate, [place.key]);

  /**
   * Places whose wait ended without the gate seeing it: a run on its schedule no longer due
   * (paused, deleted, edited, rescheduled), a stored delivery no longer pending (its task deleted).
   */
  const leaveEndedPlaces = Effect.gen(function* () {
    if (Option.isNone(gate)) return;
    const keys = yield* gate.value.line;
    const taskIds = keys.flatMap((key) =>
      key.startsWith(SCHEDULED_PLACE) ? [key.slice(SCHEDULED_PLACE.length)] : [],
    );
    const deliveryIds = keys.flatMap((key) =>
      key.startsWith(DELIVERY_PLACE) && !waitingDeliveries.has(key)
        ? [key.slice(DELIVERY_PLACE.length)]
        : [],
    );
    if (taskIds.length === 0 && deliveryIds.length === 0) return;
    const nowIso = DateTime.formatIso(DateTime.toUtc(yield* DateTime.now));
    const due = new Set(
      taskIds.length === 0
        ? []
        : (yield* sql<{ task_id: string }>`
            SELECT task_id FROM scheduled_tasks
            WHERE task_id IN ${sql.in(taskIds)} AND enabled = 1 AND next_run_at IS NOT NULL
              AND next_run_at <= ${nowIso} AND last_run_status <> 'running'
          `).map((row) => row.task_id),
    );
    const pending = new Set(
      deliveryIds.length === 0
        ? []
        : (yield* sql<{ delivery_id: string }>`
            SELECT delivery_id FROM scheduled_task_webhook_pending
            WHERE delivery_id IN ${sql.in(deliveryIds)} AND state IN ('pending', 'dispatching')
          `).map((row) => row.delivery_id),
    );
    const ended = [
      ...taskIds.filter((id) => !due.has(id)).map((id) => `${SCHEDULED_PLACE}${id}`),
      ...deliveryIds.filter((id) => !pending.has(id)).map((id) => `${DELIVERY_PLACE}${id}`),
    ];
    if (ended.length > 0) yield* gate.value.leave(ended);
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
      ? DELETED_WHILE_WAITING
      : row.created_at !== task.createdAt
        ? "The task was replaced before this delivery ran."
        : row.trigger !== "webhook"
          ? "The task's trigger changed before this delivery ran."
          : !row.enabled
            ? "The task was paused before this delivery ran."
            : null;
  });

  /**
   * A stored delivery whose prompt was rendered from another copy of the task than `task` (a save
   * between the stored-delivery worker's read and `runTask`'s, `task`): the worker writes the prompt
   * it rendered from into the row it takes (`task_json`), the first of the task's rows in its order.
   * A `running` row was sent already and keeps its prompt; a direct delivery has no row yet.
   */
  const renderedFromOther = Effect.fnUntraced(function* (task: ScheduledTask) {
    if ((yield* CurrentDirectDelivery) !== undefined) return false;
    const [row] = yield* sql<{ state: string; prompt: unknown }>`
      SELECT state, json_extract(task_json, '$.prompt') AS prompt
      FROM scheduled_task_webhook_pending
      WHERE task_id = ${task.id} AND state IN ('dispatching', 'running', 'completed', 'failed')
      ORDER BY due_at, rowid LIMIT 1
    `;
    if (row?.state !== "dispatching") return false;
    return (yield* decodePrompt(row.prompt)) !== task.prompt;
  });

  /**
   * Whether `task`, the copy a waiting webhook run holds, is still the task as saved: `saved` when
   * a field a run uses changed since it was read (a save in the same millisecond too), `gone`
   * when the row is deleted, `unknown` when it cannot be read. A bind writes the thread, but not during a wait: a webhook task's
   * runs take turns. (A save before it was read is `renderedFromOther`'s, as the run starts.)
   */
  const savedSince = Effect.fn("AgentSlotRoutes.savedSince")(
    function* (task: ScheduledTask) {
      const [row] = yield* sql<Record<string, unknown>>`
        SELECT ${sql.literal(Object.values(RUN_COPY_COLUMNS).join(", "))}
        FROM scheduled_tasks WHERE task_id = ${task.id}
      `;
      // Gone between `webhookGone` and here (or that check could not read): ended as deleted.
      if (row === undefined) return "gone" as SinceRead;
      const now = yield* decodeStoredCopy(
        Object.fromEntries(
          Object.entries(RUN_COPY_COLUMNS).map(([field, column]) => [field, row[column]]),
        ),
      );
      const [was, is] = yield* Effect.all([runCopyText(task), runCopyText(now)]);
      return was === is ? "same" : "saved";
    },
    (effect) =>
      effect.pipe(Effect.catchCause((cause) => Effect.succeed<SinceRead>({ unknown: cause }))),
  );

  /** A webhook delivery the stored-delivery worker owns now: this run of it ends unstarted. */
  const handedToWorker = (task: ScheduledTask): ScheduledSlot => ({
    skipped: { task, threadId: null, dispatchFailed: false },
    end: () => Effect.void,
  });

  /** A stored delivery handed back: the worker keeps its row and takes it again at once. */
  const keepForWorker = (task: ScheduledTask) =>
    Effect.sync(() => {
      keep.add(task.id);
      return handedToWorker(task);
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
   * webhook), or when its task's row stays unreadable (`WEBHOOK_UNREADABLE_RETRIES`). Saved while
   * it waits, it is handed back to the worker to run on the task as saved.
   */
  const waitForWebhookSlot = Effect.fnUntraced(
    function* (task: ScheduledTask, first: Refused, place: SlotGate.SlotPlace | undefined) {
      yield* Effect.logInfo("Webhook delivery waits for an agent slot", {
        taskId: task.id,
        reason: reasonHere(first.refusal),
      });
      // Retries in a row that could not read its task's row (0: the last one could).
      let failedReads = 0;
      while (true) {
        yield* Effect.sleep(WEBHOOK_SLOT_RETRY);
        const gone = yield* webhookGone(task).pipe(Effect.orElseSucceed(() => null));
        if (gone !== null) return yield* Effect.fail(new WebhookSkipped({ reason: gone }));
        const since = yield* savedSince(task);
        if (since === "gone")
          return yield* Effect.fail(new WebhookSkipped({ reason: DELETED_WHILE_WAITING }));
        // Not known: asked again at the next retry, with no slot taken. A row that cannot be read
        // is logged once (again only after a good read), and skipped once it stays so too long,
        // logging the last cause.
        if (typeof since === "object") {
          failedReads += 1;
          if (failedReads === 1) {
            yield* Effect.logWarning(
              "A webhook delivery waiting for a slot cannot read its task; it asks again at each retry",
              { taskId: task.id, cause: since.unknown },
            );
          }
          if (failedReads >= WEBHOOK_UNREADABLE_RETRIES) {
            yield* Effect.logWarning(
              "A webhook delivery waiting for a slot is skipped: its task stayed unreadable",
              { taskId: task.id, failedReads, cause: since.unknown },
            );
            return yield* Effect.fail(new WebhookSkipped({ reason: UNREADABLE_WHILE_WAITING }));
          }
          continue;
        }
        if (failedReads > 0) {
          failedReads = 0;
          yield* Effect.logInfo("A waiting webhook delivery can read its task again", {
            taskId: task.id,
          });
        }
        // Handed back, its place goes on release; the worker's ask rejoins at the row's due time.
        if (since === "saved") {
          if ((yield* CurrentDirectDelivery) === undefined) return yield* keepForWorker(task);
          if (!(yield* storeDirect(task, yield* DateTime.now)))
            return yield* Effect.fail(
              new WebhookSkipped({ reason: SAVED_WHILE_WAITING_NOT_STORED }),
            );
          return handedToWorker(task);
        }
        // Checked again as it would start: the owner may have widened the project meanwhile.
        const refused = task.threadId === null ? yield* profiles.runRefusal(task) : null;
        if (refused !== null) return yield* Effect.fail(new WebhookSkipped({ reason: refused }));
        // A bound thread that shows a run only queues: the gate lets it pass (`intoThreadId`).
        // Asked in its place, so a waiter that joined later never passes it.
        const slot = yield* take(yield* targetOf(task), place);
        if (slot.type === "allow") return allowed(task, slot.end);
      }
    },
    // Skipped, failed, stopped or handed back: its place goes too (let through, the gate dropped
    // it already).
    (effect, _task, _first, place) =>
      place === undefined
        ? effect
        : Effect.acquireUseRelease(
            Effect.sync(() => waitingDeliveries.add(place.key)),
            () => effect,
            () =>
              Effect.sync(() => waitingDeliveries.delete(place.key)).pipe(
                Effect.andThen(leave(place)),
              ),
          ),
  );

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
    // A stored delivery rendered from a copy a save overtook runs again on the task as saved, wait
    // or not. A read that fails here goes on, as upstream's run did.
    if (
      trigger === "webhook" &&
      (yield* renderedFromOther(task).pipe(Effect.orElseSucceed(() => false)))
    )
      return yield* keepForWorker(task);
    // A new thread wider than the profile the task was saved with starts nothing: on its schedule
    // a failed run with the reason (moved to its next time), "run now" fails, a webhook skips.
    const profileRefusal = task.threadId === null ? yield* profiles.runRefusal(task) : null;
    // Refused for its profile, a place it had from an earlier ask (a stored delivery's first,
    // direct ask) leaves now: left until the sweep, it would refuse another run a free slot.
    if (profileRefusal !== null) {
      if (trigger !== "manual" && Option.isSome(gate)) yield* leave(yield* placeOf(task, trigger));
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
    const place = yield* placeOf(task, trigger);
    const slot = yield* take(yield* targetOf(task), place);
    if (slot.type === "allow") return allowed(task, slot.end);
    if (trigger === "manual") {
      return yield* new ScheduledTaskError({
        message: `${RUN_NOW_REFUSED_PREFIX}${reasonHere(slot.refusal)}.`,
        taskId: task.id,
      });
    }
    if (trigger === "webhook") {
      if (yield* storeDirect(task, now)) return handedToWorker(task);
      return yield* waitForWebhookSlot(task, slot, place);
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
   * A stored delivery its worker removes: its own place in line goes now, not at the sweep. Named
   * by its id, so it never frees another delivery's place (one stored for its task after a delete
   * and a new task with the same id). A run that asked in its task's place (its row unreadable)
   * left that place as its wait ended, or as the gate let it through.
   */
  const leaveStored = (deliveryId: string) => leaveLine(gate, [`${DELIVERY_PLACE}${deliveryId}`]);

  /**
   * The stored-delivery worker, after its delivery's run returned: `true` when its task was saved
   * while it waited (the worker keeps the row, runs it again on the task as saved; read once). Else
   * the worker removes the row: its place in line goes now, run, skipped or failed before it asked.
   */
  const keepStored = (taskId: string, deliveryId: string) =>
    Effect.suspend(() =>
      keep.delete(taskId) ? Effect.succeed(true) : leaveStored(deliveryId).pipe(Effect.as(false)),
    );

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
  yield* scheduler.register(
    "agent-slot-notes",
    Effect.andThen(
      leaveEndedPlaces.pipe(
        Effect.catchCause((cause) =>
          Effect.logWarning("Could not check the line places of waiting scheduled runs", { cause }),
        ),
      ),
      clearStaleNotes,
    ),
  );

  return {
    admit: admitRun,
    ended,
    directDelivery,
    keepStored,
    /** The stored-delivery worker removes a delivery before its run (payload, prompt too long). */
    leaveStored,
    /** `ScheduledTaskService.upsert`, last before it writes the row (`scheduledTaskMcpProfile.ts`). */
    saving: profiles.saving,
  };
});
