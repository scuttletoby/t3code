/**
 * Fork (HO5): "Hand off now" in the thread menu (threadActionMenu.logic.ts builds it; its hook
 * and the sidebar's menu read the state first and run the click). The server decides as at a turn
 * end and uses the same ids, so a press during an automatic handoff, or a second press, sends one.
 * The calls load on use, so the pure menu builder stays free of the app's network code.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import {
  HANDOFF_MIN_TOKENS,
  type ContextMenuItem,
  type HandoffRouteAnswer,
  type HandoffRouteBlocker,
  type HandoffRouteState,
  type ScopedThreadRef,
} from "@t3tools/contracts";

import type { MessageResolver } from "../../../i18n/messages";
import type { ThreadActionMenuId } from "../../threadActionMenu.logic";

/**
 * Typed as a thread menu id without a line in upstream's id union: that union ends in upstream's
 * own line, which a fork line after it would have to change.
 */
export const HAND_OFF_NOW = "fork:hand-off-now" as unknown as ThreadActionMenuId;

/** Null or absent: no item (another environment, handoff off, an older server, a failed read). */
export type HandoffMenuState = HandoffRouteState | null;

/** How long the menu waits for the state before it opens without the item. */
export const HANDOFF_MENU_READ_TIMEOUT_MS = 300;

export function blockerText(blocker: HandoffRouteBlocker): string {
  switch (blocker.kind) {
    case "run-active":
      return "a turn still running";
    case "request-pending":
      return "an approval or question for you";
    case "child-result-waiting":
      return "a child's result not read yet";
    case "fork-or-merge":
      return "a fork or merge going on";
    case "background-work":
      return blocker.detail ? `background work (${blocker.detail})` : "background work";
    case "goal-active":
      return "an active goal";
  }
}

export function waitingOnText(blockers: ReadonlyArray<HandoffRouteBlocker>): string {
  return blockers.map(blockerText).join(", ");
}

const OFF_TEXT = "Handoff is not on for this thread.";

const SKIP_TEXT: Readonly<Record<string, string>> = {
  "mode-not-handoff": OFF_TEXT,
  "thread-unavailable": "The thread is archived or gone.",
  "no-runs": "The thread has no turns yet.",
  "newer-run": "A newer turn is running.",
  "last-run-handoff": "The thread was just handed off.",
  "last-run-compact": "The thread was just compacted.",
  "too-small": `The thread is under ${HANDOFF_MIN_TOKENS / 1000}K tokens.`,
};

/** The toast after a press: what happens now, or why nothing does. */
export function answerToast(answer: HandoffRouteAnswer): {
  readonly type: "success" | "info" | "warning";
  readonly title: string;
  readonly description?: string;
} {
  switch (answer.kind) {
    case "started":
      return {
        type: "success",
        title: "Handing off",
        description: "A fresh session starts once the thread has been quiet for a moment.",
      };
    case "wait":
      return {
        type: "info",
        title: "Not handed off: the thread is busy",
        description: `It waits on ${waitingOnText(answer.blockers)}. Try again once that is done.`,
      };
    case "skip":
      return {
        type: "info",
        title: "Not handed off",
        description: SKIP_TEXT[answer.reason] ?? `The server said no (${answer.reason}).`,
      };
    case "off":
      return { type: "info", title: "Not handed off", description: OFF_TEXT };
  }
}

/**
 * The item: shown while a press would hand off (enabled) or the thread is busy (disabled, with
 * what it waits on); hidden when off or when a press would skip.
 */
export function handOffNowItems(
  state: HandoffMenuState | undefined,
  _resolve?: MessageResolver,
): ReadonlyArray<ContextMenuItem<ThreadActionMenuId>> {
  if (!state?.on) return [];
  const { ask } = state;
  if (ask.kind === "started") {
    return [{ id: HAND_OFF_NOW, label: "Hand off now", icon: "refresh-cw" }];
  }
  if (ask.kind === "wait") {
    return [
      {
        id: HAND_OFF_NOW,
        label: "Hand off now",
        icon: "refresh-cw",
        detail: `Waits on ${waitingOnText(ask.blockers)}`,
        disabled: true,
      },
    ];
  }
  return [];
}

export async function readHandoffMenuState(
  threadRef: ScopedThreadRef | null,
): Promise<HandoffMenuState> {
  if (threadRef === null) return null;
  const api = await import("./handoffApi");
  return api.readHandoffState(threadRef, { timeoutMs: HANDOFF_MENU_READ_TIMEOUT_MS });
}

export async function handOffNow(threadRef: ScopedThreadRef): Promise<void> {
  const [api, toast] = await Promise.all([import("./handoffApi"), import("../../ui/toast")]);
  try {
    const answer = await api.askHandoffNow(threadRef);
    toast.toastManager.add(toast.stackedThreadToast(answerToast(answer)));
  } catch (error) {
    toast.toastManager.add(
      toast.stackedThreadToast({
        type: "error",
        title: "Hand off now failed",
        description: error instanceof Error ? error.message : String(error),
      }),
    );
  }
}
