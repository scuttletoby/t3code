/** Fork (issue 386): delegated task rows for the tests of `task_list` and the reminder. */
import { NodeId, type OrchestrationV2Subagent, ThreadId } from "@t3tools/contracts";
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
