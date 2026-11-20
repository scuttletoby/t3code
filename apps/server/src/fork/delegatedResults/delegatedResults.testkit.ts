/** Fork (issue 386): delegated task rows for the tests of `task_list` and the reminder. */
import { NodeId, type OrchestrationV2Subagent, ThreadId } from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";

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
