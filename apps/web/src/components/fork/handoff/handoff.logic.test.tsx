import type { HandoffRouteState } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import {
  buildThreadActionMenuItems,
  threadActionRequiresOperate,
  type ThreadActionMenuState,
} from "../../threadActionMenu.logic";
import { answerToast, HAND_OFF_NOW, handOffNowItems } from "./handoffMenu";
import { handoffWaitingItem } from "./useForkHandoffWaitingItems";

const menuState: ThreadActionMenuState = {
  canOperate: true,
  branch: null,
  projectFilter: null,
  isPinned: false,
  isSettled: false,
  autoSettleEnabled: true,
  isSnoozed: false,
  canSnoozeNow: true,
  isRegeneratingTitle: false,
  isRunning: false,
  hasCompletedRun: false,
  supports: {
    settlement: true,
    autoSettleOptOut: true,
    snooze: true,
    pinning: true,
    titleRegeneration: true,
  },
  snoozePresets: [],
};

const handoffState = (ask: HandoffRouteState["ask"]): HandoffRouteState => ({
  on: true,
  usedTokens: 120_000,
  ask,
  waitingOn: [],
});

describe("Hand off now in the thread menu (HO5)", () => {
  it("shows an enabled item when a press would hand off", () => {
    const items = buildThreadActionMenuItems({
      ...menuState,
      forkHandoff: handoffState({ kind: "started" }),
    });
    const item = items.find((one) => one.id === HAND_OFF_NOW);
    expect(item).toMatchObject({ label: "Hand off now" });
    expect(item?.disabled).toBeFalsy();
  });

  it("shows a disabled item naming what a busy thread waits on", () => {
    const items = handOffNowItems(
      handoffState({
        kind: "wait",
        blockers: [{ kind: "request-pending" }, { kind: "background-work", detail: "2 shells" }],
      }),
    );
    expect(items).toEqual([
      expect.objectContaining({
        id: HAND_OFF_NOW,
        disabled: true,
        detail: "Waits on an approval or question for you, background work (2 shells)",
      }),
    ]);
  });

  it("shows no item when off, unread, or when a press would skip", () => {
    expect(handOffNowItems(undefined)).toEqual([]);
    expect(handOffNowItems(null)).toEqual([]);
    expect(handOffNowItems({ ...handoffState({ kind: "started" }), on: false })).toEqual([]);
    expect(handOffNowItems(handoffState({ kind: "skip", reason: "too-small" }))).toEqual([]);
    expect(buildThreadActionMenuItems(menuState).some((one) => one.id === HAND_OFF_NOW)).toBe(
      false,
    );
  });

  it("needs the operate right, like the server's route", () => {
    expect(threadActionRequiresOperate(HAND_OFF_NOW)).toBe(true);
  });

  it("says after a press what happens, or why nothing does", () => {
    expect(answerToast({ kind: "started" })).toMatchObject({ type: "success" });
    expect(answerToast({ kind: "skip", reason: "too-small" }).description).toBe(
      "The thread is under 20K tokens.",
    );
    expect(answerToast({ kind: "skip", reason: "some-new-reason" }).description).toBe(
      "The server said no (some-new-reason).",
    );
    expect(answerToast({ kind: "wait", blockers: [{ kind: "run-active" }] }).description).toBe(
      "It waits on a turn still running. Try again once that is done.",
    );
    expect(answerToast({ kind: "off" }).description).toBe("Handoff is not on for this thread.");
  });
});

describe("the handoff waiting chip (HO5)", () => {
  it("names what the due handoff waits on, and is gone with nothing to wait on", () => {
    expect(handoffWaitingItem([])).toBeNull();
    expect(
      handoffWaitingItem([{ kind: "child-result-waiting" }, { kind: "goal-active" }]),
    ).toMatchObject({
      id: "fork-handoff-waiting",
      title: "Handoff waiting on a child's result not read yet, an active goal",
    });
  });
});
