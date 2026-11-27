/**
 * Fork: wakes a parent here once per prompt its task's thread on a linked machine waits on, as
 * upstream's DelegatedTaskQuestionReactor does for a local question: a blocking wait is ended (its
 * wake set to `always`, only while its run lasts), else one queued message, cancelled while queued
 * once answered there.
 */
import {
  CommandId,
  type EnvironmentId,
  MessageId,
  type NodeId,
  type OrchestrationV2Run,
  type OrchestrationV2Subagent,
  type ThreadId,
} from "@t3tools/contracts";
import { reportLabel } from "@t3tools/provider-core/server/notification";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import { HttpClient } from "effect/http";

import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import { forkParked } from "../serverActivation.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as WorkAdmission from "../workAdmission.ts";
import { admitSend, leaveLine } from "./agentSlotRoutes.ts";
import * as RemoteTaskPrompts from "./remoteTaskPrompts.ts";

/** Between two reads of every open remote task. */
const POLL_INTERVAL = Duration.seconds(5);

/**
 * Message wakes; not upstream's question prefix, whose held wakes are checked against local
 * requests. Fixed by parent, machine and prompt: a restart sends no prompt twice.
 */
const WAKE_COMMAND = "server:remote-task-prompt:";
const WAKE_MESSAGE = "message:remote-task-prompt:";
const CANCEL_COMMAND = "server:remote-task-prompt-cancel:";
/** The wake-policy change that ends a blocking wait on a prompt. */
const WAIT_COMMAND = "server:remote-task-prompt-wait:";

/** One prompt there: its kind and id, as the machine there names it. */
interface Prompt {
  readonly kind: "question" | "approval" | "secret";
  readonly key: string;
  readonly text: (task: OrchestrationV2Subagent) => string;
  readonly summary: string;
}

const ACTIVE_RUN = new Set<OrchestrationV2Run["status"]>(["preparing", "starting", "running"]);
const SETTLED_TASK = new Set(["completed", "failed", "cancelled", "interrupted"]);

const promptsIn = (prompts: RemoteTaskPrompts.TaskPrompts): ReadonlyArray<Prompt> => [
  ...prompts.questions.map((question): Prompt => ({
    kind: "question",
    key: `question:${question.requestId}`,
    summary: "has a question",
    text: (task) =>
      `Delegated task ${task.id} on ${task.remoteChild!.label} is blocked on a question and cannot continue until it is answered. Read it with t3_pending_request action read, environmentId ${task.remoteChild!.environmentId}, threadId ${task.remoteChild!.threadId}, and requestId ${question.requestId}. If it is no longer pending, it was answered or dismissed.`,
  })),
  ...(prompts.approval === null
    ? []
    : [
        {
          kind: "approval",
          key: `approval:${prompts.approval.requestId}`,
          summary: "waits for an approval",
          text: (task: OrchestrationV2Subagent) =>
            `Delegated task ${task.id} on ${task.remoteChild!.label} waits for a ${prompts.approval!.kind} approval (request ${prompts.approval!.requestId}) and cannot continue until it is approved or declined on ${task.remoteChild!.label}. Tell the user if they need to act there.`,
        } satisfies Prompt,
      ]),
  ...prompts.secrets.map((secret): Prompt => ({
    kind: "secret",
    key: `secret:${secret.itemId}`,
    summary: "asks for a secret",
    text: (task) =>
      `Delegated task ${task.id} on ${task.remoteChild!.label} asks for a secret (${RemoteTaskPrompts.secretWords(secret)}) and waits until the user gives it on ${task.remoteChild!.label}. Tell the user if they need to act there.`,
  })),
];

/** The id part shared by a prompt's commands and message: parent, machine and prompt. */
const idOf = (parentThreadId: ThreadId, environmentId: EnvironmentId, key: string) =>
  `${parentThreadId}:${environmentId}:${key}`;

export interface WakeOptions {
  readonly interval: Duration.Input;
}

/** The watcher: `tick` reads every open remote task once; `run` repeats it. Tests drive `tick`. */
export const make = Effect.fn("RemoteTaskPromptWakes.make")(function* (
  read: RemoteTaskPrompts.ReadRemotePrompts,
  options: WakeOptions = { interval: POLL_INTERVAL },
) {
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  const threads = yield* ThreadManagement.ThreadManagementService;
  const receipts = yield* CommandReceiptStore.CommandReceiptStoreV2;
  const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
  /** Per open task (parent:task): its machine, and the prompts a message was sent for. */
  const handled = new Map<
    string,
    { readonly parentThreadId: ThreadId; readonly environmentId: EnvironmentId; keys: Set<string> }
  >();

  const placeOf = (id: string) => `remote-task-prompt:${id}`;

  /** Cancels each prompt's wake while it is still queued: nothing is left for it to say. */
  const cancelQueued = Effect.fn("RemoteTaskPromptWakes.cancelQueued")(function* (
    parentThreadId: ThreadId,
    environmentId: EnvironmentId,
    keys: ReadonlyArray<string>,
  ) {
    if (keys.length === 0) return;
    const ids = keys.map((key) => idOf(parentThreadId, environmentId, key));
    yield* leaveLine(gate, ids.map(placeOf));
    const { runs } = yield* threads.getThreadRecords(parentThreadId, ["runs"]);
    for (const id of ids) {
      const messageId = MessageId.make(`${WAKE_MESSAGE}${id}`);
      const queued = runs.find((run) => run.status === "queued" && run.userMessageId === messageId);
      if (queued === undefined) continue;
      yield* threads.dispatch({
        type: "queued-run.cancel",
        commandId: CommandId.make(`${CANCEL_COMMAND}${id}`),
        threadId: parentThreadId,
        runId: queued.id,
      });
    }
  });

  /** The parent's run a blocking wait on this task runs in is still going. */
  const runIsActive = (task: OrchestrationV2Subagent, runs: ReadonlyArray<OrchestrationV2Run>) =>
    runs.some((run) => run.id === task.runId && ACTIVE_RUN.has(run.status));

  /** A blocking wait on this task is live: it reports the prompt itself once it reads the task. */
  const waitIsLive = (task: OrchestrationV2Subagent, runs: ReadonlyArray<OrchestrationV2Run>) =>
    task.completionWake === "settled_only" && runIsActive(task, runs);

  /**
   * `sent`: a message has it, for good. `wait`: a blocking wait has it, only while the parent's run
   * it runs in is active (its read may be unknown, or the server may stop before it reads), so it
   * is asked again next poll. `retry`: no slot now, next poll.
   */
  const wake = Effect.fn("RemoteTaskPromptWakes.wake")(function* (
    task: OrchestrationV2Subagent,
    parent: { readonly instanceId: OrchestrationV2Subagent["providerInstanceId"] },
    runs: ReadonlyArray<OrchestrationV2Run>,
    prompt: Prompt,
  ) {
    const remote = task.remoteChild!;
    const id = idOf(task.threadId, remote.environmentId, prompt.key);
    const waitCommand = CommandId.make(`${WAIT_COMMAND}${id}`);
    const wakeCommand = CommandId.make(`${WAKE_COMMAND}${id}`);
    if (waitIsLive(task, runs)) {
      // Fork note: the wait's own exit then sets the same policy again, which the engine refuses
      // as unchanged; upstream logs that as `wake-policy-failed`. Harmless noise.
      const ended = yield* threads
        .dispatch({
          type: "delegated_task.wake-policy",
          commandId: waitCommand,
          parentThreadId: task.threadId,
          taskId: task.id,
          completionWake: "always",
        })
        .pipe(
          Effect.as(true),
          // The wait ended in between and set it itself: the prompt goes as a message.
          Effect.orElseSucceed(() => false),
        );
      if (ended) return "wait" as const;
    }
    // A wait this prompt ended (also before a restart) has it while its run is active; once that
    // run ended with the prompt still open there, the prompt goes as a message after all.
    if (runIsActive(task, runs)) {
      const byWait = yield* receipts.getByCommandId(waitCommand);
      if (Option.isSome(byWait) && byWait.value.status !== "rejected") return "wait" as const;
    }
    // Sent before a restart: no slot is taken for a send the engine would drop.
    const sent = yield* receipts.getByCommandId(wakeCommand);
    if (Option.isSome(sent) && sent.value.status !== "rejected") return "sent" as const;
    const slot = yield* admitSend(gate, {
      callerThreadId: task.threadId,
      threadId: task.threadId,
      instanceId: parent.instanceId,
      keepsHeld: true,
      place: { key: placeOf(id) },
    });
    if (slot.type === "refuse") return "retry" as const;
    const label = reportLabel(task.title?.trim() || task.prompt);
    yield* threads.dispatch({
      type: "message.dispatch",
      commandId: wakeCommand,
      threadId: task.threadId,
      messageId: MessageId.make(`${WAKE_MESSAGE}${id}`),
      text: prompt.text(task),
      notification: {
        source: { kind: "delegated_task", taskIds: [task.id] },
        outcome: "updated",
        summary: `Delegated task${label === undefined ? "" : ` "${label}"`} ${prompt.summary}`,
      },
      attachments: [],
      dispatchMode: { type: "queue_after_active" },
      createdBy: "agent",
      creationSource: "server",
    });
    return "sent" as const;
  });

  /** One open task: wakes its new prompts, cancels the wakes of answered ones. */
  const checkTask = Effect.fn("RemoteTaskPromptWakes.checkTask")(function* (
    parentThreadId: ThreadId,
    taskId: NodeId,
    open: Set<string>,
    /** Machines that did not answer this round: not asked again until the next one. */
    down: Set<EnvironmentId>,
  ) {
    const parent = yield* threads.getThreadRecords(parentThreadId, ["subagents", "runs"]);
    if (parent.thread.archivedAt !== null || parent.thread.deletedAt !== null) return;
    const task = parent.subagents.find(
      (candidate) => candidate.id === taskId && candidate.origin === "app_owned",
    );
    if (
      task === undefined ||
      task.remoteChild === undefined ||
      task.result !== null ||
      SETTLED_TASK.has(task.status) ||
      task.completionDelivery?.state === "disposed"
    ) {
      return;
    }
    const taskKey = `${parentThreadId}:${taskId}`;
    open.add(taskKey);
    if (down.has(task.remoteChild.environmentId)) return;
    const result = yield* read(task.remoteChild);
    if (result.type === "unknown" && result.reason === "unreachable") {
      down.add(task.remoteChild.environmentId);
    }
    // Not known: nothing is woken or cleared on a guess.
    if (result.type === "unknown" || !result.prompts.found) return;
    const prompts = promptsIn(result.prompts);
    const now = new Set(prompts.map((prompt) => prompt.key));
    const entry = handled.get(taskKey) ?? {
      parentThreadId,
      environmentId: task.remoteChild.environmentId,
      keys: new Set<string>(),
    };
    handled.set(taskKey, entry);
    const answered = [...entry.keys].filter((key) => !now.has(key));
    for (const key of answered) entry.keys.delete(key);
    yield* cancelQueued(parentThreadId, entry.environmentId, answered);
    for (const prompt of prompts) {
      if (entry.keys.has(prompt.key)) continue;
      if ((yield* wake(task, parent.thread.modelSelection, parent.runs, prompt)) === "sent") {
        entry.keys.add(prompt.key);
      }
    }
  });

  const tick = Effect.gen(function* () {
    const open = new Set<string>();
    const down = new Set<EnvironmentId>();
    const tasks = yield* projections.getOpenRemoteDelegatedTasks;
    yield* Effect.forEach(
      tasks,
      ({ parentThreadId, taskId }) =>
        checkTask(parentThreadId, taskId, open, down).pipe(
          Effect.catchCause((cause) =>
            Effect.logWarning("a remote task's prompts could not be checked", {
              threadId: parentThreadId,
              taskId,
              cause,
            }).pipe(
              // Kept: an error says nothing about its prompts.
              Effect.andThen(Effect.sync(() => open.add(`${parentThreadId}:${taskId}`))),
            ),
          ),
        ),
      { discard: true },
    );
    // Tasks that ended or went away: their prompts' queued wakes have nothing left to say.
    for (const [taskKey, entry] of handled) {
      if (open.has(taskKey)) continue;
      handled.delete(taskKey);
      yield* cancelQueued(entry.parentThreadId, entry.environmentId, [...entry.keys]).pipe(
        Effect.catchCause((cause) =>
          Effect.logWarning("cancelling a remote task prompt's wake failed", { cause }),
        ),
      );
    }
  }).pipe(
    Effect.catchCause((cause) =>
      Effect.logWarning("remote task prompts: the open tasks could not be read", { cause }),
    ),
  );

  const run = () =>
    Effect.forever(tick.pipe(Effect.andThen(Effect.sleep(options.interval))), {
      disableYield: false,
    } as never);

  return { tick, run, handled: () => handled };
});

/**
 * In the routes beside upstream's follower of linked tasks (fork/forkLayers.ts), which has the
 * links. A held server (`serve --hold-work`) does not watch, as it does not follow.
 */
export const layer = Layer.effectDiscard(
  Effect.gen(function* () {
    if ((yield* WorkAdmission.WorkAdmission).heldFromBoot) return;
    const reader = RemoteTaskPrompts.makeReader(
      yield* PeerLinks.PeerLinks,
      yield* HttpClient.HttpClient,
    );
    const watcher = yield* make(reader);
    yield* forkParked(watcher.run());
  }),
).pipe(
  Layer.provide(
    Layer.mergeAll(
      // The server's one gate: layers memoize by reference.
      SlotGate.layer,
      CommandReceiptStore.layerFromApplicationReceipts,
      ProjectionStore.layer,
    ),
  ),
);
