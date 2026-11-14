/**
 * Fork (waits): sends a wait's message through the thread tools' send, at most once. A retry
 * first looks for its message in the thread, then reads its command's receipt, and only then
 * sends; so a send that was accepted but whose answer was lost is never sent a second time.
 */
import {
  CommandId,
  MessageId,
  type OrchestrationV2ThreadShell,
  type ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import type { WaitMessage } from "./WaitService.ts";

/** Where a receiver stands now. Only a subagent's parent is a parent; a fork has none. */
export type WaitReceiver =
  | { readonly type: "ok" }
  /** Archived, deleted or unknown. */
  | { readonly type: "gone"; readonly parentThreadId: ThreadId | null }
  /**
   * Its latest run was stopped by a person or its parent; its messages are held. `stoppedAt`
   * (epoch ms) is when that run last started or ended, if the shell says.
   */
  | {
      readonly type: "stopped";
      readonly parentThreadId: ThreadId | null;
      readonly stoppedAt?: number;
    };

export type WaitDeliveryResult =
  /** The message is in the thread: sent now, sent before, or replayed from its receipt. */
  | { readonly type: "delivered" }
  /** This command id was refused and stays refused; the next try needs a new one. */
  | { readonly type: "rejected"; readonly detail: string }
  /** Nothing was recorded for this command id; the same id is tried again later. */
  | { readonly type: "retry"; readonly detail: string }
  | Exclude<WaitReceiver, { readonly type: "ok" }>;

export interface WaitDelivery {
  readonly receiver: (threadId: ThreadId) => Effect.Effect<WaitReceiver | null>;
  readonly deliver: (message: WaitMessage) => Effect.Effect<WaitDeliveryResult>;
}

/** A thread's subagent parent from its lineage; the only source of a wait's parent. */
export const parentOf = (shell: OrchestrationV2ThreadShell | null): ThreadId | null =>
  shell?.lineage.relationshipToParent === "subagent" ? shell.lineage.parentThreadId : null;

/** The receiver as its shell shows it; null is a deleted or never-known thread. */
function receiverOf(shell: OrchestrationV2ThreadShell | null): WaitReceiver {
  if (shell === null || shell.deletedAt !== null || shell.archivedAt !== null) {
    return { type: "gone", parentThreadId: parentOf(shell) };
  }
  if (shell.status === "interrupted") {
    const times = [shell.latestRunStartedAt, shell.latestRunCompletedAt]
      .filter((time) => time !== undefined && time !== null)
      .map((time) => DateTime.toEpochMillis(time));
    return {
      type: "stopped",
      parentThreadId: parentOf(shell),
      ...(times.length === 0 ? {} : { stoppedAt: Math.max(...times) }),
    };
  }
  return { type: "ok" };
}

/**
 * Failures after which nothing was recorded for the command id and a later try may succeed: the
 * workspace could not be restored (no receipt is written then), or a read failed.
 */
const RETRY_SAME_ID: ReadonlySet<string> = new Set([
  "ThreadManagementWorktreeRestoreError",
  "ThreadManagementProjectionLoadError",
  "ThreadManagementDurableRunProjectionError",
  "OrchestratorProjectionError",
  "OrchestratorDomainEventStreamError",
]);

const describe = (error: unknown) =>
  error instanceof Error ? error.message : typeof error === "string" ? error : String(error);

export const make = Effect.fn("WaitDelivery.make")(function* () {
  const threads = yield* ThreadManagement.ThreadManagementService;
  const receipts = yield* CommandReceiptStore.CommandReceiptStoreV2;

  /** Null when the thread cannot be read now (try again later). */
  const shellOf = (threadId: ThreadId) =>
    threads.getThreadShell(threadId).pipe(
      Effect.map((shell): OrchestrationV2ThreadShell | null | undefined => shell),
      Effect.catch((error) =>
        Effect.logWarning(`waits: thread ${threadId} cannot be read: ${describe(error)}`).pipe(
          Effect.as(undefined),
        ),
      ),
    );

  const receiver: WaitDelivery["receiver"] = (threadId) =>
    shellOf(threadId).pipe(Effect.map((shell) => (shell === undefined ? null : receiverOf(shell))));

  /** True when the message is in the thread; null when that cannot be read now. */
  const found = (message: WaitMessage) =>
    threads.getMessagesById(message.threadId, [MessageId.make(message.messageId)]).pipe(
      Effect.map((messages) => messages.has(MessageId.make(message.messageId))),
      Effect.orElseSucceed(() => null),
    );

  /** What the receipt of this command id says; null when it cannot be read now. */
  const receiptOf = (message: WaitMessage) =>
    receipts.getByCommandId(CommandId.make(message.commandId)).pipe(
      Effect.map((receipt): WaitDeliveryResult | "none" =>
        Option.match(receipt, {
          onNone: () => "none" as const,
          onSome: (stored) =>
            stored.status === "rejected"
              ? { type: "rejected", detail: stored.error ?? "Refused." }
              : // A receipt held by another thread is a refusal for this one.
                stored.threadId === message.threadId
                ? { type: "delivered" }
                : {
                    type: "rejected",
                    detail: `Command ${message.commandId} belongs to thread ${stored.threadId}.`,
                  },
        }),
      ),
      Effect.orElseSucceed(() => null),
    );

  /** Looks for the message, then the receipt; the send's own error decides only when both are silent. */
  const settle = Effect.fnUntraced(function* (message: WaitMessage, error: unknown) {
    if ((yield* found(message)) === true) return { type: "delivered" } as const;
    const receipt = yield* receiptOf(message);
    if (receipt === null) return { type: "retry", detail: describe(error) } as const;
    if (receipt !== "none") return receipt;
    const tag = (error as { readonly _tag?: unknown } | null)?._tag;
    return typeof tag === "string" && RETRY_SAME_ID.has(tag)
      ? ({ type: "retry", detail: describe(error) } as const)
      : ({ type: "rejected", detail: describe(error) } as const);
  });

  const deliver: WaitDelivery["deliver"] = Effect.fn("WaitDelivery.deliver")(function* (message) {
    const shell = yield* shellOf(message.threadId);
    if (shell === undefined) return { type: "retry", detail: "The thread cannot be read." };
    // A message sent before the thread was archived or stopped stays delivered.
    const already = yield* found(message);
    if (already === null) return { type: "retry", detail: "The thread's messages cannot be read." };
    if (already) return { type: "delivered" };
    // Replays of an accepted command answer from the receipt; a refused one stays refused. Read
    // before the receiver's state, so a message accepted just before the thread was archived,
    // deleted or stopped counts as delivered (also if its message rows were removed).
    const receipt = yield* receiptOf(message);
    if (receipt === null) return { type: "retry", detail: "The command receipt cannot be read." };
    if (receipt !== "none" && receipt.type === "delivered") return receipt;
    const state = receiverOf(shell);
    if (state.type !== "ok") return state;
    if (receipt !== "none") return receipt;
    // shell is not null here: receiverOf answers "gone" for null.
    const projectId = shell!.projectId;
    return yield* threads
      .sendToThread({
        projectId,
        commandId: CommandId.make(message.commandId),
        threadId: message.threadId,
        messageId: MessageId.make(message.messageId),
        text: message.text,
        attachments: [],
        mode: "queue",
        createdBy: "agent",
        creationSource: "mcp",
      })
      .pipe(
        Effect.as<WaitDeliveryResult>({ type: "delivered" }),
        Effect.catchTags({
          ThreadManagementThreadArchivedError: () =>
            Effect.succeed<WaitDeliveryResult>({ type: "gone", parentThreadId: parentOf(shell!) }),
          // Moved to another project, or gone, since the shell was read.
          ThreadManagementThreadNotFoundError: (error) =>
            shellOf(message.threadId).pipe(
              Effect.map((again): WaitDeliveryResult => {
                if (again === undefined) return { type: "retry", detail: error.message };
                const now = receiverOf(again);
                return now.type === "gone" ? now : { type: "retry", detail: error.message };
              }),
            ),
        }),
        Effect.catch((error) => settle(message, error)),
      );
  });

  return { receiver, deliver } satisfies WaitDelivery;
});
