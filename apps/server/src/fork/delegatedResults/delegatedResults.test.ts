import { describe, expect, it } from "@effect/vitest";
import { EnvironmentId, NodeId, ThreadId } from "@t3tools/contracts";

import {
  countByPlace,
  isRemindable,
  type LinkedThread,
  localActivity,
  orphanReminderIds,
  orphanReminderText,
  orphansOf,
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
  it("says none for an old row, or a wait task, with no delivery record", () => {
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
  it("reminds a recent result with no delivery record: a wait whose acknowledgement failed", () => {
    expect(isRemindable(delegatedTask({ completionDelivery: undefined }), NOW)).toBe(true);
  });
  it("never reminds an old row from before delivery records (older than a day)", () => {
    const old = delegatedTask({
      completionDelivery: undefined,
      completedAt: at(NOW - REMINDER_MAX_AGE_MS - 1),
    });
    expect(isRemindable(old, NOW)).toBe(false);
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
    expect(parentBusy({ ...idle, pendingRuntimeRequest: {} as never })).toBe(true);
  });
  it("does not treat background work the parent left running as busy", () => {
    const idle = {
      activityRunStatus: null,
      status: "idle",
      pendingRuntimeRequest: null,
      pendingBackgroundTasks: [{} as never],
    } as const;
    // Upstream counts it as a live child shell; no run end may come for hours.
    expect(localActivity(idle as never)).toBe("live");
    expect(parentBusy(idle)).toBe(false);
  });
});

describe("reminderText", () => {
  it("names each task and the tool that reads it", () => {
    const text = reminderText([delegatedTask({ id: "t-2" }), delegatedTask({ id: "t-1" })]);
    expect(text).toContain("2 delegated tasks");
    expect(text.indexOf("t-1")).toBeLessThan(text.indexOf("t-2"));
    expect(text).toContain("task_status");
    // Upstream's wake may land just before it.
    expect(text).toContain("may not have reached");
  });
});

const HERE = EnvironmentId.make("env-here");
const THERE = EnvironmentId.make("env-b");
const linked = (
  threadId: string,
  from: { environmentId?: EnvironmentId; threadId?: ThreadId } = {},
  environmentId = THERE,
): LinkedThread => ({
  environmentId,
  threadId: ThreadId.make(threadId),
  title: `title ${threadId}`,
  delegatedFrom: {
    environmentId: from.environmentId ?? HERE,
    threadId: from.threadId ?? PARENT,
  },
});

describe("orphansOf", () => {
  it("keeps a thread there launched from this parent that no task follows", () => {
    const listed = [linked("r-1"), linked("r-2")];
    const followed = [{ environmentId: THERE, threadId: ThreadId.make("r-1") }];
    expect(orphansOf(listed, HERE, PARENT, followed).map((t) => t.threadId)).toEqual(["r-2"]);
  });
  it("never counts a thread launched from another environment's thread of the same id", () => {
    const listed = [linked("r-1", { environmentId: EnvironmentId.make("env-c") })];
    expect(orphansOf(listed, HERE, PARENT, [])).toEqual([]);
  });
  it("never counts another parent's thread, or one followed in another environment's name", () => {
    expect(
      orphansOf([linked("r-1", { threadId: ThreadId.make("other") })], HERE, PARENT, []),
    ).toEqual([]);
    // Same thread id, but the task follows it in another environment: still an orphan here.
    const followedElsewhere = [
      { environmentId: EnvironmentId.make("env-c"), threadId: ThreadId.make("r-1") },
    ];
    expect(orphansOf([linked("r-1")], HERE, PARENT, followedElsewhere)).toHaveLength(1);
  });
});

describe("orphan reminder", () => {
  it("has the same ids for the same orphans in any order, other ids than a task reminder", () => {
    const a = orphanReminderIds(PARENT, [linked("r-1"), linked("r-2")]);
    const b = orphanReminderIds(PARENT, [linked("r-2"), linked("r-1")]);
    expect(a).toEqual(b);
    expect(orphanReminderIds(PARENT, [linked("r-1")])).not.toEqual(a);
    expect(a.messageId).not.toEqual(
      reminderIds(PARENT, [NodeId.make("r-1"), NodeId.make("r-2")]).messageId,
    );
  });
  it("names each orphan with its environment and the tools that read and stop it", () => {
    const text = orphanReminderText([linked("r-2"), linked("r-1")]);
    expect(text).toContain("2 threads");
    expect(text.indexOf("r-1")).toBeLessThan(text.indexOf("r-2"));
    expect(text).toContain(`in ${THERE}`);
    expect(text).toContain("t3_thread_read");
    expect(text).toContain("t3_thread_interrupt");
  });
});
