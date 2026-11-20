import { describe, expect, it } from "@effect/vitest";
import { EnvironmentId, NodeId, ThreadId } from "@t3tools/contracts";

import {
  countByPlace,
  isRemindable,
  localActivity,
  parentBusy,
  REMINDER_MAX_AGE_MS,
  reminderIds,
  reminderText,
  remoteActivity,
  resultDelivery,
} from "./delegatedResults.ts";
import { at, delegatedTask, NOW, PARENT } from "./delegatedResults.testkit.ts";

describe("resultDelivery", () => {
  it("is null while the task runs", () => {
    expect(resultDelivery(delegatedTask({ status: "running" }))).toBeNull();
  });
  it("owes pending and claimed results", () => {
    for (const state of ["pending", "claimed"] as const) {
      expect(
        resultDelivery(delegatedTask({ completionDelivery: { state, observedByRunId: null } })),
      ).toBe("owed");
    }
  });
  it("settles delivered, acknowledged and disposed results", () => {
    for (const state of ["delivered", "acknowledged", "disposed"] as const) {
      expect(
        resultDelivery(delegatedTask({ completionDelivery: { state, observedByRunId: null } })),
      ).toBe("settled");
    }
  });
  it("says none for an old row with no delivery record", () => {
    expect(resultDelivery(delegatedTask({ completionDelivery: undefined }))).toBe("none");
  });
});

describe("isRemindable", () => {
  it("reminds a recent owed result, local or remote", () => {
    expect(isRemindable(delegatedTask(), NOW)).toBe(true);
    const remote = delegatedTask({
      childThreadId: null,
      remoteChild: {
        environmentId: EnvironmentId.make("env-b"),
        threadId: ThreadId.make("remote-1"),
        label: "env b",
      },
    });
    expect(isRemindable(remote, NOW)).toBe(true);
  });
  it("never reminds an old row from before delivery records", () => {
    expect(isRemindable(delegatedTask({ completionDelivery: undefined }), NOW)).toBe(false);
  });
  it("never reminds a result older than a day (an abandoned thread)", () => {
    const old = delegatedTask({ completedAt: at(NOW - REMINDER_MAX_AGE_MS - 1) });
    expect(isRemindable(old, NOW)).toBe(false);
  });
  it("never reminds a delivered or read result (delivered twice is not owed twice)", () => {
    expect(
      isRemindable(
        delegatedTask({ completionDelivery: { state: "delivered", observedByRunId: null } }),
        NOW,
      ),
    ).toBe(false);
    expect(
      isRemindable(
        delegatedTask({ completionDelivery: { state: "acknowledged", observedByRunId: null } }),
        NOW,
      ),
    ).toBe(false);
  });
  it("never reminds the provider's own subagents", () => {
    expect(isRemindable(delegatedTask({ origin: "provider_native" }), NOW)).toBe(false);
  });
  it("never reminds a task still running", () => {
    expect(isRemindable(delegatedTask({ status: "running" }), NOW)).toBe(false);
  });
});

describe("reminderIds", () => {
  it("is the same for the same owed tasks in any order, so a repeat is deduped", () => {
    const one = reminderIds(PARENT, [NodeId.make("a"), NodeId.make("b")]);
    const two = reminderIds(PARENT, [NodeId.make("b"), NodeId.make("a"), NodeId.make("a")]);
    expect(two).toEqual(one);
    expect(one.messageId.startsWith(`delegated-reminder:${PARENT}:`)).toBe(true);
    expect(one.commandId).toBe(`${one.messageId}:0`);
  });
  it("differs when another result becomes owed", () => {
    expect(reminderIds(PARENT, [NodeId.make("a")])).not.toEqual(
      reminderIds(PARENT, [NodeId.make("a"), NodeId.make("b")]),
    );
  });
});

describe("activity and slots", () => {
  it("reads a remote child that does not answer as unknown, never as live", () => {
    expect(remoteActivity(null)).toBe("unknown");
    expect(remoteActivity("running")).toBe("live");
    expect(remoteActivity("waiting")).toBe("live");
    expect(remoteActivity("completed")).toBe("ended");
  });
  it("reads a missing local child as unknown", () => {
    expect(localActivity(null)).toBe("unknown");
  });
  it("counts live and unknown children per place, from activity only", () => {
    const envB = EnvironmentId.make("env-b");
    expect(
      countByPlace([
        { where: "local", activity: "live" },
        { where: "local", activity: "ended" },
        { where: envB, activity: "unknown" },
        { where: envB, activity: "live" },
        { where: EnvironmentId.make("env-c"), activity: "ended" },
      ]),
    ).toEqual([
      { where: "local", live: 1, unknown: 0 },
      { where: envB, live: 1, unknown: 1 },
    ]);
    expect(countByPlace([])).toEqual([]);
  });
  it("treats a parent with a queued run or open question as busy", () => {
    const idle = {
      activityRunStatus: null,
      status: "idle",
      pendingRuntimeRequest: null,
      pendingBackgroundTasks: [],
    } as const;
    expect(parentBusy(idle)).toBe(false);
    expect(parentBusy({ ...idle, status: "queued" })).toBe(true);
  });
});

describe("reminderText", () => {
  it("names each task and the tool that reads it", () => {
    const text = reminderText([delegatedTask({ id: "t-2" }), delegatedTask({ id: "t-1" })]);
    expect(text).toContain("2 delegated tasks");
    expect(text.indexOf("t-1")).toBeLessThan(text.indexOf("t-2"));
    expect(text).toContain("task_status");
  });
});
