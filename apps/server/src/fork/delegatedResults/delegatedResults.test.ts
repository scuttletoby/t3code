import { describe, expect, it } from "@effect/vitest";
import { EnvironmentId, NodeId, ThreadId } from "@t3tools/contracts";

import {
  countByPlace,
  isRemindable,
  type LinkedThread,
  localActivity,
  legacyOrphanSetId,
  orphanBatches,
  orphanBatchIds,
  orphanKey,
  orphanReminderText,
  ORPHANS_PER_MESSAGE,
  orphansOf,
  parentBusy,
  legacyReminderKeys,
  legacyReminderMessageIds,
  legacyReminderSetId,
  REMINDER_MAX_AGE_MS,
  reminderBatches,
  reminderBatchIds,
  reminderKey,
  REMINDERS_PER_MESSAGE,
  reminderText,
  remoteActivity,
  resultDelivery,
  toldOrphanKeys,
  toldReminderKeys,
} from "./delegatedResults.ts";
import { at, delegatedTask, firstFormatReminder, NOW, PARENT } from "./delegatedResults.testkit.ts";

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

describe("reminder ids", () => {
  const ids = (...names: ReadonlyArray<string>) => names.map((name) => NodeId.make(name));
  const tasks = (...names: ReadonlyArray<string>) => names.map((id) => delegatedTask({ id }));
  const named = (batches: ReadonlyArray<ReadonlyArray<{ readonly id: string }>>) =>
    batches.map((batch) => batch.map((task) => task.id));

  it("is the same for the same tasks in any order, so a repeat is deduped", () => {
    const one = reminderBatchIds(PARENT, ids("a", "b"));
    expect(reminderBatchIds(PARENT, ids("b", "a", "a"))).toEqual(one);
    expect(one.messageId).toBe(
      `delegated-reminder:${PARENT}:${[reminderKey(NodeId.make("a")), reminderKey(NodeId.make("b"))].toSorted().join(".")}`,
    );
    expect(one.commandId).toBe(`${one.messageId}:0`);
  });

  it("reads back each task a message or command id named, for this parent only", () => {
    const sent = reminderBatchIds(PARENT, ids("a", "b"));
    const keys = new Set([reminderKey(NodeId.make("a")), reminderKey(NodeId.make("b"))]);
    expect(toldReminderKeys(PARENT, [sent.messageId])).toEqual(keys);
    expect(toldReminderKeys(PARENT, [sent.commandId])).toEqual(keys);
    const other = reminderBatchIds(ThreadId.make("other"), ids("c"));
    const orphan = orphanBatchIds(PARENT, [linked("a")]);
    expect(toldReminderKeys(PARENT, [other.messageId, orphan.messageId])).toEqual(new Set());
  });

  it("reminds only tasks not reminded yet: a grown set names just the new one", () => {
    // Task a was reminded, then b ended while a was still unread: a is not reminded again.
    const first = reminderBatchIds(PARENT, ids("a"));
    expect(named(reminderBatches(PARENT, tasks("a", "b"), [first.messageId]))).toEqual([["b"]]);
    // Its receipt alone (the message not projected yet, a restart) says the same.
    expect(named(reminderBatches(PARENT, tasks("a", "b"), [first.commandId]))).toEqual([["b"]]);
    // Run again with both told: nothing. An empty or missing record: all of them.
    const second = reminderBatchIds(PARENT, ids("b"));
    expect(reminderBatches(PARENT, tasks("a", "b"), [first.messageId, second.messageId])).toEqual(
      [],
    );
    expect(named(reminderBatches(PARENT, tasks("b", "a", "a"), []))).toEqual([["a", "b"]]);
    expect(reminderBatches(PARENT, [], [])).toEqual([]);
  });

  it("splits many tasks into messages that each name every one of theirs", () => {
    const many = Array.from({ length: REMINDERS_PER_MESSAGE + 2 }, (_, index) =>
      delegatedTask({ id: `t-${String(index).padStart(2, "0")}` }),
    );
    const batches = reminderBatches(PARENT, many, []);
    expect(batches.map((batch) => batch.length)).toEqual([REMINDERS_PER_MESSAGE, 2]);
    expect(reminderText(batches[0]!)).not.toContain("more");
  });

  it("takes a reminder of the first format for exactly the owed set, or one task, as told", () => {
    // That format's id, as the first reminder made it (sha256 of the sorted ids, 16 hex digits).
    expect(legacyReminderSetId(PARENT, ids("a", "b"))).toBe(
      "delegated-reminder:thread-parent:7e18f737311b2dc3",
    );
    expect(legacyReminderSetId(PARENT, ids("a"))).toBe(
      "delegated-reminder:thread-parent:ca978112ca1bbdca",
    );
    expect(toldReminderKeys(PARENT, [legacyReminderSetId(PARENT, ids("a"))])).toEqual(new Set());
    const legacyBoth = `${legacyReminderSetId(PARENT, ids("a", "b"))}:0`;
    expect(reminderBatches(PARENT, tasks("a", "b"), [legacyBoth])).toEqual([]);
    // One task told the old way: a grown set names only the new task.
    const legacyA = legacyReminderSetId(PARENT, ids("a"));
    expect(named(reminderBatches(PARENT, tasks("a", "b"), [legacyA]))).toEqual([["b"]]);
  });

  it("splits a first-format reminder into its tasks from its text (issue 695)", () => {
    // Told {a, b} the old way; b was read since, and c ended after the upgrade.
    const old = firstFormatReminder(PARENT, ["a", "b"]);
    const texts = new Map([[old.messageId, old.text]]);
    expect(legacyReminderKeys(PARENT, old.messageId, old.text)).toEqual([
      reminderKey(NodeId.make("a")),
      reminderKey(NodeId.make("b")),
    ]);
    expect(named(reminderBatches(PARENT, tasks("a", "c"), [old.commandId], texts))).toEqual([
      ["c"],
    ]);
    expect(named(reminderBatches(PARENT, tasks("a", "b", "c"), [old.messageId], texts))).toEqual([
      ["c"],
    ]);
    // Without its text (only the receipt is left) the set no longer matches: all are reminded.
    expect(named(reminderBatches(PARENT, tasks("a", "c"), [old.commandId]))).toEqual([["a", "c"]]);
    // The first wording ("never reached") reads the same.
    const first = firstFormatReminder(PARENT, ["a"], "never");
    expect(legacyReminderKeys(PARENT, first.messageId, first.text)).toEqual([
      reminderKey(NodeId.make("a")),
    ]);
  });

  it("trusts a first-format text only when it reads back exactly", () => {
    const old = firstFormatReminder(PARENT, ["a", "b"]);
    // Another parent's id, or a text that names another set: the digest does not match.
    expect(legacyReminderKeys(ThreadId.make("other"), old.messageId, old.text)).toEqual([]);
    const other = firstFormatReminder(PARENT, ["a", "c"]);
    expect(legacyReminderKeys(PARENT, old.messageId, other.text)).toEqual([]);
    // A title that broke its line adds a task line: the count no longer matches.
    const injected = old.text.replace('- b "a task"', '- b "x"\n- c "y"');
    expect(legacyReminderKeys(PARENT, old.messageId, injected)).toEqual([]);
    // Unsorted, duplicated, or another kind of message.
    const swapped = old.text
      .replace("- a ", "- tmp ")
      .replace("- b ", "- a ")
      .replace("- tmp ", "- b ");
    expect(legacyReminderKeys(PARENT, old.messageId, swapped)).toEqual([]);
    expect(legacyReminderKeys(PARENT, old.messageId, old.text.replace("- b ", "- a "))).toEqual([]);
    expect(legacyReminderKeys(PARENT, old.messageId, "hello")).toEqual([]);
    expect(legacyReminderKeys(PARENT, old.messageId, `${old.text}\n- and 2 more`)).toEqual([]);
  });

  it("over ten tasks, takes the ten a first-format text listed; the rest are reminded once", () => {
    const names = Array.from({ length: 12 }, (_, index) => `t-${String(index).padStart(2, "0")}`);
    const old = firstFormatReminder(PARENT, names);
    expect(old.text).toContain("- and 2 more");
    const listed = names.slice(0, 10).map((name) => reminderKey(NodeId.make(name)));
    expect(legacyReminderKeys(PARENT, old.messageId, old.text)).toEqual(listed);
    const texts = new Map([[old.messageId, old.text]]);
    // A new task ends: the set changed, so the exact match is gone; the two unlisted and the
    // new one are reminded, the ten listed are not.
    expect(named(reminderBatches(PARENT, tasks(...names, "t-12"), [old.messageId], texts))).toEqual(
      [["t-10", "t-11", "t-12"]],
    );
    // A count that does not match the listed ones gives nothing.
    expect(
      legacyReminderKeys(PARENT, old.messageId, old.text.replace("and 2 more", "and 3 more")),
    ).toEqual([]);
    expect(
      legacyReminderKeys(PARENT, old.messageId, old.text.replace(/\n- t-09 [^\n]*/, "")),
    ).toEqual([]);
    // Over ten there is no digest to match (it covers the unnamed rest too), so only this
    // parent's own first-format id counts: another parent's, or a per-task id, gives nothing.
    expect(legacyReminderKeys(ThreadId.make("other"), old.messageId, old.text)).toEqual([]);
    const perTask = reminderBatchIds(PARENT, ids("t-00"));
    expect(legacyReminderKeys(PARENT, perTask.messageId, old.text)).toEqual([]);
  });

  it("finds this parent's first-format ids, from messages and receipts", () => {
    const old = firstFormatReminder(PARENT, ["a", "b"]);
    const otherParent = firstFormatReminder(ThreadId.make("other"), ["a"]);
    const perTask = reminderBatchIds(PARENT, ids("c"));
    expect(
      legacyReminderMessageIds(PARENT, [
        old.commandId,
        old.messageId,
        otherParent.messageId,
        perTask.messageId,
        perTask.commandId,
      ]),
    ).toEqual([old.messageId]);
    expect(legacyReminderMessageIds(PARENT, [])).toEqual([]);
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
    const a = orphanBatchIds(PARENT, [linked("r-1"), linked("r-2")]);
    const b = orphanBatchIds(PARENT, [linked("r-2"), linked("r-1")]);
    expect(a).toEqual(b);
    expect(orphanBatchIds(PARENT, [linked("r-1")])).not.toEqual(a);
    // The same thread id in another environment is another orphan.
    expect(orphanKey(linked("r-1", {}, EnvironmentId.make("env-c")))).not.toBe(
      orphanKey(linked("r-1")),
    );
    expect(a.messageId).not.toEqual(
      reminderBatchIds(PARENT, [NodeId.make("r-1"), NodeId.make("r-2")]).messageId,
    );
  });

  it("reads back each orphan a message or command id named, for this parent only", () => {
    const ids = orphanBatchIds(PARENT, [linked("r-1"), linked("r-2")]);
    const other = orphanBatchIds(ThreadId.make("other"), [linked("r-3")]);
    expect(toldOrphanKeys(PARENT, [ids.messageId])).toEqual(
      new Set([orphanKey(linked("r-1")), orphanKey(linked("r-2"))]),
    );
    // A receipt's command id says the same; another parent's ids and other kinds say nothing.
    expect(toldOrphanKeys(PARENT, [ids.commandId])).toEqual(
      toldOrphanKeys(PARENT, [ids.messageId]),
    );
    expect(toldOrphanKeys(PARENT, [other.messageId, "delegated-reminder:x:0123456789ab"])).toEqual(
      new Set(),
    );
  });

  it("names only the orphans not told yet: a grown set sends just the new ones", () => {
    const first = orphanBatchIds(PARENT, [linked("r-1")]);
    const grown = [linked("r-1"), linked("r-2"), linked("r-3")];
    const batches = orphanBatches(PARENT, grown, [first.messageId]);
    expect(batches.map((batch) => batch.map((orphan) => orphan.threadId))).toEqual([
      ["r-2", "r-3"],
    ]);
    // Told in two messages, it names none; an empty record names all.
    const second = orphanBatchIds(PARENT, batches[0]!);
    expect(orphanBatches(PARENT, grown, [first.commandId, second.messageId])).toEqual([]);
    expect(orphanBatches(PARENT, grown, []).flat()).toHaveLength(3);
  });

  it("splits many orphans into messages that each name every one of theirs", () => {
    const many = Array.from({ length: ORPHANS_PER_MESSAGE + 2 }, (_, index) =>
      linked(`r-${String(index).padStart(2, "0")}`),
    );
    const batches = orphanBatches(PARENT, many, []);
    expect(batches.map((batch) => batch.length)).toEqual([ORPHANS_PER_MESSAGE, 2]);
    expect(orphanReminderText(batches[0]!)).not.toContain("more");
  });

  it("takes the first scan's one message for exactly this set as told", () => {
    const set = [linked("r-1"), linked("r-2")];
    // That format's id, as the first scan made it (sha256 of the sorted keys, 16 hex digits).
    expect(legacyOrphanSetId(PARENT, set)).toBe("delegated-orphans:thread-parent:9f652a107b5cc2f3");
    expect(orphanBatches(PARENT, set, [legacyOrphanSetId(PARENT, set)])).toEqual([]);
    // A grown set has another digest: each orphan is named once more, then by its own key.
    expect(
      orphanBatches(PARENT, [...set, linked("r-3")], [legacyOrphanSetId(PARENT, set)]).flat(),
    ).toHaveLength(3);
  });

  it("names each orphan with its environment and the tools that read and stop it", () => {
    const text = orphanReminderText([linked("r-2"), linked("r-1")]);
    expect(text).toContain("2 threads");
    expect(text.indexOf("r-1")).toBeLessThan(text.indexOf("r-2"));
    expect(text).toContain(`in ${THERE}`);
    expect(text).toContain("t3_thread_read");
    expect(text).toContain("t3_thread_interrupt");
  });

  it("does not say why the record is missing: a stop and a failed record read the same", () => {
    const one = orphanReminderText([linked("r-1")]);
    expect(one).toContain("1 thread in linked environments was launched");
    expect(one).toContain("delegate_task did not record it here");
    expect(one).not.toContain("server stopped");
  });
});
