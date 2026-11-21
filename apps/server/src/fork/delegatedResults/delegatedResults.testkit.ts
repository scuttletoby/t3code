/** Fork (issue 386): delegated task rows for the tests of `task_list` and the reminder. */
// @effect-diagnostics-next-line nodeBuiltinImport:off -- Rebuilds the first reminder format's digest.
import * as NodeCrypto from "node:crypto";

import {
  EventId,
  MessageId,
  NodeId,
  type OrchestrationV2DomainEvent,
  type OrchestrationV2Subagent,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as SlotGate from "../../slots/SlotGate.ts";

export const NOW = Date.parse("2026-11-20T12:00:00.000Z");
export const PARENT = ThreadId.make("thread-parent");
export const at = (ms: number) => DateTime.makeUnsafe(ms);

/** A local task that ended a minute ago with its result claimed but never delivered. */
export const delegatedTask = (
  overrides: Omit<Partial<OrchestrationV2Subagent>, "id"> & { readonly id?: string } = {},
): OrchestrationV2Subagent =>
  ({
    threadId: PARENT,
    runId: null,
    origin: "app_owned",
    childThreadId: ThreadId.make(`child-${overrides.id ?? "task-1"}`),
    title: "a task",
    status: "completed",
    result: "done",
    completedAt: at(NOW - 60_000),
    updatedAt: at(NOW - 60_000),
    completionDelivery: { state: "claimed", observedByRunId: null },
    ...overrides,
    id: NodeId.make(overrides.id ?? "task-1"),
  }) as unknown as OrchestrationV2Subagent;

/**
 * Fork (issue 711): an agent slot gate that refuses a start into the threads in `full` (into any
 * thread while `all` is set), and keeps every ask.
 */
export const makeSlots = () => {
  const full = new Set<ThreadId>();
  const state = { all: false };
  const asks: Array<SlotGate.SlotAcquireInput> = [];
  const layer = Layer.mock(SlotGate.SlotGate)({
    acquire: (input) =>
      Effect.sync((): SlotGate.SlotAcquire => {
        asks.push(input);
        const refused =
          state.all || (input.intoThreadId !== undefined && full.has(input.intoThreadId));
        return refused
          ? { type: "refuse", message: "full", refusal: { type: "unread", problem: "timeout" } }
          : { type: "allow", token: asks.length };
      }),
    started: () => Effect.void,
    release: () => Effect.void,
  });
  return { full, state, asks, layer };
};

/**
 * Fork (issue 695): a reminder as the first format sent it, before each task had its own key: one
 * id for the whole set (sha256 of the sorted ids, 16 hex digits), and a text listing the first ten
 * tasks (`delegatedTask` rows). "never" is the very first wording, "may" the one after it.
 */
export const firstFormatReminder = (
  parentThreadId: ThreadId,
  taskIds: ReadonlyArray<string>,
  wording: "never" | "may" = "may",
) => {
  const sorted = [...new Set(taskIds)].toSorted();
  const digest = NodeCrypto.createHash("sha256").update(sorted.join("\n")).digest("hex");
  const messageId = `delegated-reminder:${parentThreadId}:${digest.slice(0, 16)}`;
  const count = sorted.length === 1 ? "1 delegated task" : `${sorted.length} delegated tasks`;
  const reached =
    wording === "never" ? "never reached this thread" : "may not have reached this thread";
  const text = [
    `[delegated results] ${count} ended, but the result ${reached}:`,
    ...sorted.slice(0, 10).map((id) => `- ${id} "a task" (completed)`),
    ...(sorted.length > 10 ? [`- and ${sorted.length - 10} more`] : []),
    "Read each with task_status (that marks it read). task_list lists every task with an unread result.",
  ].join("\n");
  return { messageId, commandId: `${messageId}:0`, text };
};

/**
 * Fork (issue 741): the event that projects a message the reminder sent into the real message
 * table, as upstream writes it (its own encoding of the row's payload).
 */
export const reminderMessageEvent = (
  threadId: ThreadId,
  messageId: string,
  text: string,
): OrchestrationV2DomainEvent => ({
  id: EventId.make(`event:${messageId}`),
  threadId,
  occurredAt: at(NOW - 30_000),
  type: "message.updated",
  payload: {
    id: MessageId.make(messageId),
    threadId,
    runId: null,
    nodeId: null,
    role: "user",
    text,
    attachments: [],
    streaming: false,
    createdBy: "agent",
    creationSource: "mcp",
    createdAt: at(NOW - 30_000),
    updatedAt: at(NOW - 30_000),
  },
});
