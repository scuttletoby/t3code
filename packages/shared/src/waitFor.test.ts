import {
  DEFAULT_SERVER_SETTINGS,
  ProjectId,
  ProviderInstanceId,
  ThreadId,
  WaitChecks,
  WaitRecord,
  type PullRequestCheck,
  type WaitForInput,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { resolveProjectSettings } from "./projectSettings.ts";
import { applyServerSettingsPatch } from "./serverSettings.ts";
import {
  MAX_HAND_OVERS,
  deliveryCommandId,
  deliveryGroups,
  deliveryRetryDelayMs,
  judgeExit,
  judgePrChecks,
  joinedWakeMessage,
  nextWaitState,
  shouldPruneWait,
  validateWaitSpec,
  waitDeadline,
  waitKey,
  wakeMessage,
  type Judgement,
  type PrSeen,
  type WaitStateSettings,
} from "./waitFor.ts";

const T0 = Date.parse("2026-11-14T10:00:00.000Z");
const SECOND = 1000;
const HOUR = 3_600_000;
const iso = (ms: number) => DateTime.formatIso(DateTime.makeUnsafe(ms));
const thread = (name: string) => ThreadId.make(name);

const settings = DEFAULT_SERVER_SETTINGS.waitFor;
const stateSettings: WaitStateSettings = {
  errorGraceSeconds: settings.errorGraceSeconds,
  keepEndedHours: settings.keepEndedHours,
  stoppedHandOverHours: settings.stoppedHandOverHours,
  reserveSeconds: DEFAULT_SERVER_SETTINGS.agentSlots.reserveSeconds,
};

const decodeRecord = Schema.decodeUnknownSync(WaitRecord);
const decodeChecks = Schema.decodeSync(WaitChecks);
const makeWait = (fields: Record<string, unknown> = {}): WaitRecord =>
  decodeRecord({
    id: "w_aaaaaaaaaa",
    threadId: "thread-child",
    projectId: "project-a",
    key: "aaaaaaaaaaaaaaaa",
    createdAt: iso(T0),
    deadlineAt: iso(T0 + 24 * HOUR),
    state: "waiting",
    attempt: 0,
    kind: "check",
    spec: { name: "lease-free", args: [] },
    ...fields,
  });
const delivering = (fields: Record<string, unknown> = {}) =>
  makeWait({
    state: "delivering",
    result: { outcome: "met", detail: "Lease is free.", at: iso(T0) },
    ...fields,
  });

const checks = decodeChecks({
  "lease-free": { argv: ["/usr/local/bin/lease-free"], maxArgs: 2 },
  "no-args": { argv: ["/usr/bin/true"] },
});
const validate = (input: WaitForInput, platform: NodeJS.Platform = "linux") =>
  validateWaitSpec({ input, checks, platform, accounts: new Set(["claude"]) });

const check = (name: string, status: PullRequestCheck["status"]): PullRequestCheck => ({
  name,
  status,
  description: null,
  url: null,
});
const prSettings = { prSweepSeconds: 120, noChecksGraceSeconds: 900 };
const judgePr = (input: {
  readonly checks: ReadonlyArray<PullRequestCheck> | null;
  readonly seen?: PrSeen | null;
  readonly now: number;
  readonly headSha?: string;
  readonly state?: "open" | "closed" | "merged";
  readonly until?: "checks" | "merged";
}) =>
  judgePrChecks({
    detail: { state: input.state ?? "open", headSha: input.headSha ?? "1a2b3c4d5e6f" },
    checks: input.checks === null ? null : { checks: input.checks },
    until: input.until ?? "checks",
    seen: input.seen ?? null,
    now: input.now,
    settings: prSettings,
  });

describe("validateWaitSpec (T9)", () => {
  it("fills defaults so equal conditions give equal targets", () => {
    expect(validate({ kind: "pr", pr: { number: 12 } })).toEqual({
      ok: true,
      target: { kind: "pr", spec: { number: 12, until: "checks" } },
    });
    expect(validate({ kind: "check", check: { name: "no-args" } })).toEqual({
      ok: true,
      target: { kind: "check", spec: { name: "no-args", args: [] } },
    });
    expect(validate({ kind: "unit", unit: { name: "t3.service" } })).toEqual({
      ok: true,
      target: { kind: "unit", spec: { name: "t3.service", scope: "system" } },
    });
  });

  it("accepts arguments within the limit that match the pattern", () => {
    expect(
      validate({ kind: "check", check: { name: "lease-free", args: ["vm-1", "a/b"] } }).ok,
    ).toBe(true);
  });

  it.each([
    ["a space", ["a b"]],
    ["a semicolon", ["a;b"]],
    ["a leading dash", ["-rf"]],
    ["an empty argument", [""]],
    ["one argument too many", ["a", "b", "c"]],
  ])("refuses %s", (_label, args) => {
    const result = validate({ kind: "check", check: { name: "lease-free", args } });
    expect(result).toMatchObject({ ok: false, code: "invalid" });
  });

  it("refuses a leading dash even when the pattern allows it", () => {
    const loose = decodeChecks({
      loose: { argv: ["x"], maxArgs: 1, argPattern: ".*" },
    });
    const result = validateWaitSpec({
      input: { kind: "check", check: { name: "loose", args: ["--force"] } },
      checks: loose,
      platform: "linux",
      accounts: new Set(),
    });
    expect(result).toMatchObject({ ok: false, code: "invalid" });
  });

  it("matches the whole argument even when the pattern has no anchors", () => {
    const loose = decodeChecks({
      digits: { argv: ["x"], maxArgs: 1, argPattern: "[0-9]+" },
    });
    const run = (arg: string) =>
      validateWaitSpec({
        input: { kind: "check", check: { name: "digits", args: [arg] } },
        checks: loose,
        platform: "linux",
        accounts: new Set(),
      }).ok;
    expect(run("123")).toBe(true);
    expect(run("12;rm")).toBe(false);
  });

  it("names the project's checks when the check is unknown", () => {
    expect(validate({ kind: "check", check: { name: "nope" } })).toEqual({
      ok: false,
      code: "unknown_check",
      message: 'No check "nope". Checks: lease-free, no-args.',
    });
    expect(
      validateWaitSpec({
        input: { kind: "check", check: { name: "toString" } },
        checks: {},
        platform: "linux",
        accounts: new Set(),
      }),
    ).toMatchObject({ ok: false, code: "unknown_check" });
  });

  it("refuses relative paths, units off Linux, bad unit names, unknown accounts and mixed kinds", () => {
    expect(validate({ kind: "file", file: { path: "relative/x" } })).toMatchObject({
      code: "invalid",
    });
    expect(validate({ kind: "unit", unit: { name: "t3.service" } }, "darwin")).toMatchObject({
      code: "not_supported",
    });
    expect(validate({ kind: "unit", unit: { name: "-bad" } })).toMatchObject({ code: "invalid" });
    expect(
      validate({ kind: "slot", slot: { account: ProviderInstanceId.make("nobody") } }),
    ).toMatchObject({ code: "invalid" });
    expect(validate({ kind: "pr", pr: { number: 1 }, file: { path: "/x" } })).toMatchObject({
      code: "invalid",
    });
    expect(validate({ kind: "pr" })).toMatchObject({ code: "invalid" });
  });
});

describe("waitDeadline and waitKey", () => {
  it("uses the default timeout and refuses more than the maximum", () => {
    expect(waitDeadline(T0, undefined, settings)).toEqual({ ok: true, deadlineAt: T0 + 24 * HOUR });
    expect(waitDeadline(T0, 30, settings)).toEqual({ ok: true, deadlineAt: T0 + 30 * 60_000 });
    expect(waitDeadline(T0, 168 * 60 + 1, settings)).toMatchObject({ ok: false, code: "invalid" });
  });

  it("gives the same key for the same thread and condition only", () => {
    const target = { kind: "pr", spec: { number: 12, until: "checks" } } as const;
    const key = waitKey(thread("a"), target);
    expect(key).toMatch(/^[0-9a-f]{16}$/u);
    expect(waitKey(thread("a"), { kind: "pr", spec: { until: "checks", number: 12 } })).toBe(key);
    expect(waitKey(thread("b"), target)).not.toBe(key);
    expect(waitKey(thread("a"), { kind: "pr", spec: { number: 12, until: "merged" } })).not.toBe(
      key,
    );
  });
});

describe("judgeExit", () => {
  it("maps exit codes", () => {
    expect(judgeExit({ exitCode: 0, stdout: "\nLease is free\nmore", stderr: "" })).toEqual({
      type: "met",
      outcome: "met",
      detail: "Lease is free",
    });
    expect(judgeExit({ exitCode: 1, stdout: "", stderr: "" })).toEqual({ type: "notYet" });
    expect(judgeExit({ exitCode: 3, stdout: "", stderr: "connection refused\n" })).toEqual({
      type: "failed",
      message: "exit 3: connection refused",
      permanent: false,
    });
    expect(judgeExit({ exitCode: 0, stdout: "", stderr: "", timedOut: true })).toMatchObject({
      type: "failed",
    });
    expect(judgeExit({ exitCode: 0, stdout: "", stderr: "", outputTruncated: true })).toMatchObject(
      {
        type: "failed",
      },
    );
  });

  it("clips the detail to 300 characters", () => {
    const result = judgeExit({ exitCode: 0, stdout: "x".repeat(400), stderr: "" });
    expect(result.type === "met" && Array.from(result.detail).length).toBe(300);
  });
});

describe("judgePrChecks (T12)", () => {
  const finished = [check("build", "success"), check("lint", "skipped")];

  it("is met only after a second read at least prSweepSeconds later saw the same head finished", () => {
    const first = judgePr({ checks: finished, now: T0 });
    expect(first).toMatchObject({
      type: "notYet",
      seen: { headSha: "1a2b3c4d5e6f", finishedAt: T0 },
    });
    const early = judgePr({ checks: finished, seen: first.seen, now: T0 + 119 * SECOND });
    expect(early).toMatchObject({ type: "notYet", seen: first.seen });
    const settled = judgePr({ checks: finished, seen: first.seen, now: T0 + 120 * SECOND });
    expect(settled).toMatchObject({
      type: "met",
      outcome: "passed",
      detail: "Head 1a2b3c4. All 2 checks passed.",
    });
  });

  it("starts again when the head changes or a check runs again", () => {
    const seen = { headSha: "1a2b3c4d5e6f", finishedAt: T0 };
    expect(
      judgePr({ checks: finished, seen, headSha: "9999999aaaa", now: T0 + 10 * 60 * SECOND }),
    ).toMatchObject({ type: "notYet", seen: { headSha: "9999999aaaa" } });
    expect(
      judgePr({ checks: [...finished, check("e2e", "pending")], seen, now: T0 + 600 * SECOND }),
    ).toMatchObject({
      type: "notYet",
      seen: null,
      detail: "Head 1a2b3c4. 1 of 3 checks still running.",
    });
  });

  it("orders outcomes failed, then needs_action, then passed", () => {
    const seen = { headSha: "1a2b3c4d5e6f", finishedAt: T0 };
    const now = T0 + 200 * SECOND;
    const failed = judgePr({
      checks: [
        check("build", "failure"),
        check("lint", "cancelled"),
        check("deploy", "action-required"),
      ],
      seen,
      now,
    });
    expect(failed).toMatchObject({
      type: "met",
      outcome: "failed",
      detail: "Head 1a2b3c4. 2 of 3 failed: build, lint.",
    });
    const action = judgePr({
      checks: [check("build", "success"), check("deploy", "action-required")],
      seen,
      now,
    });
    expect(action).toMatchObject({ type: "met", outcome: "needs_action" });
    const many = judgePr({
      checks: ["a", "b", "c", "d", "e", "f"].map((name) => check(name, "failure")),
      seen,
      now,
    });
    expect(many).toMatchObject({
      detail: "Head 1a2b3c4. 6 of 6 failed: a, b, c, d, e, ….",
      outcome: "failed",
    });
  });

  it("treats unreadable checks as an error, not as no checks", () => {
    expect(judgePr({ checks: null, now: T0 + HOUR })).toMatchObject({
      type: "failed",
      permanent: false,
    });
  });

  it("waits while some checks could not be listed", () => {
    expect(
      judgePr({
        checks: [{ ...check("unread", "pending"), placeholder: true }, check("build", "success")],
        now: T0,
      }),
    ).toMatchObject({ type: "notYet", seen: null });
  });

  it("reports no_checks only after the grace from when this head was first seen with none (T10)", () => {
    const first = judgePr({ checks: [], now: T0 + HOUR });
    expect(first).toMatchObject({
      type: "notYet",
      seen: { headSha: "1a2b3c4d5e6f", finishedAt: T0 + HOUR, noChecks: true },
    });
    expect(judgePr({ checks: [], seen: first.seen, now: T0 + HOUR + 899 * SECOND })).toMatchObject({
      type: "notYet",
    });
    expect(judgePr({ checks: [], seen: first.seen, now: T0 + HOUR + 900 * SECOND })).toMatchObject({
      type: "met",
      outcome: "no_checks",
      detail: "Head 1a2b3c4. No checks.",
    });
  });

  it("does not end on one read of a new head that lists no checks yet (push race)", () => {
    // Registered 20 min ago; the old head had finished once; a push lands and the host lists no
    // checks for the new head until its workflows are queued.
    const read = judgePr({
      checks: [],
      seen: { headSha: "aaaaaaaaaaaa", finishedAt: T0 + 19 * 60 * SECOND },
      headSha: "bbbbbbbbbbbb",
      now: T0 + 20 * 60 * SECOND,
    });
    expect(read).toMatchObject({
      type: "notYet",
      seen: { headSha: "bbbbbbbbbbbb", noChecks: true },
    });
  });

  it("settles no_checks no sooner than the sweep, even with a shorter grace", () => {
    const short = (now: number, seen: PrSeen | null) =>
      judgePrChecks({
        detail: { state: "open", headSha: "1a2b3c4d5e6f" },
        checks: { checks: [] },
        until: "checks",
        seen,
        now,
        settings: { prSweepSeconds: 120, noChecksGraceSeconds: 0 },
      });
    const first = short(T0, null);
    expect(first.type).toBe("notYet");
    expect(short(T0 + 119 * SECOND, first.seen).type).toBe("notYet");
    expect(short(T0 + 120 * SECOND, first.seen).type).toBe("met");
  });

  it("starts the settle again when checks appear on a head that had none", () => {
    const none = { headSha: "1a2b3c4d5e6f", finishedAt: T0, noChecks: true as const };
    const read = judgePr({ checks: finished, seen: none, now: T0 + HOUR });
    expect(read).toMatchObject({
      type: "notYet",
      seen: { headSha: "1a2b3c4d5e6f", finishedAt: T0 + HOUR },
    });
    expect(read.seen?.noChecks).toBeUndefined();
  });

  it("meets merged and closed pull requests at once", () => {
    expect(judgePr({ checks: null, state: "merged", now: T0 })).toMatchObject({
      type: "met",
      outcome: "merged",
    });
    expect(judgePr({ checks: null, state: "closed", now: T0 })).toMatchObject({
      type: "met",
      outcome: "closed",
    });
    expect(judgePr({ checks: finished, until: "merged", now: T0 + HOUR })).toMatchObject({
      type: "notYet",
    });
  });
});

const judged = (judgement: Judgement) => ({ type: "judged", judgement }) as const;

describe("nextWaitState: judging", () => {
  it("freezes a met result and moves to delivering", () => {
    const next = nextWaitState(
      makeWait(),
      judged({ type: "met", outcome: "met", detail: "ok" }),
      T0,
      stateSettings,
    );
    expect(next?.record).toMatchObject({
      state: "delivering",
      result: { outcome: "met", detail: "ok", at: iso(T0) },
    });
  });

  it("writes nothing when nothing changed", () => {
    expect(
      nextWaitState(makeWait(), judged({ type: "notYet" }), T0 + HOUR, stateSettings),
    ).toBeNull();
    expect(
      nextWaitState(
        delivering(),
        judged({ type: "met", outcome: "met", detail: "" }),
        T0,
        stateSettings,
      ),
    ).toBeNull();
  });

  it("ends in check_error only after errorGraceSeconds of failed reads (T10)", () => {
    const failure = judged({
      type: "failed",
      message: "exit 3: connection refused",
      permanent: false,
    });
    const first = nextWaitState(makeWait(), failure, T0, stateSettings)!.record;
    expect(first).toMatchObject({
      state: "waiting",
      errorSince: iso(T0),
      lastError: "exit 3: connection refused",
    });
    expect(nextWaitState(first, failure, T0 + 899 * SECOND, stateSettings)).toBeNull();
    const ended = nextWaitState(first, failure, T0 + 900 * SECOND, stateSettings)!.record;
    expect(ended).toMatchObject({
      state: "delivering",
      result: {
        outcome: "check_error",
        detail: "No good answer for 15 min. Last: exit 3: connection refused.",
      },
    });
    expect(ended.errorSince).toBeUndefined();
  });

  it("clears the error run on a good read", () => {
    const failing = makeWait({ errorSince: iso(T0), lastError: "x" });
    const next = nextWaitState(
      failing,
      judged({ type: "notYet" }),
      T0 + 60 * SECOND,
      stateSettings,
    )!.record;
    expect(next.errorSince).toBeUndefined();
    expect(next.lastError).toBeUndefined();
  });

  it("tests the deadline after the judge", () => {
    const pastDeadline = T0 + 25 * HOUR;
    expect(
      nextWaitState(
        makeWait(),
        judged({ type: "met", outcome: "met", detail: "" }),
        pastDeadline,
        stateSettings,
      )?.record.result?.outcome,
    ).toBe("met");
    expect(
      nextWaitState(makeWait(), judged({ type: "notYet" }), pastDeadline, stateSettings)?.record
        .result?.outcome,
    ).toBe("timeout");
  });

  it("cancels only waiting waits and grants slots only to slot waits", () => {
    expect(nextWaitState(makeWait(), { type: "cancel" }, T0, stateSettings)?.record.state).toBe(
      "cancelled",
    );
    expect(nextWaitState(delivering(), { type: "cancel" }, T0, stateSettings)).toBeNull();
    expect(
      nextWaitState(makeWait(), { type: "slotGranted", detail: "" }, T0, stateSettings),
    ).toBeNull();
    const slot = makeWait({ kind: "slot", spec: {} });
    const granted = nextWaitState(
      slot,
      { type: "slotGranted", detail: "Call delegate_task." },
      T0,
      stateSettings,
    )!;
    expect(granted.record.result?.outcome).toBe("free");
    const sent = nextWaitState(granted.record, { type: "delivered" }, T0, stateSettings)!.record;
    expect(sent).toMatchObject({ state: "delivered", reservedUntil: iso(T0 + 120 * SECOND) });
    expect(shouldPruneWait(sent, T0 + 25 * HOUR, settings)).toBe(true);
    expect(
      shouldPruneWait({ ...sent, reservedUntil: iso(T0 + 30 * HOUR) }, T0 + 25 * HOUR, settings),
    ).toBe(false);
  });
});

describe("nextWaitState: delivery", () => {
  it("records the message id once and counts refused sends", () => {
    const started = nextWaitState(
      delivering(),
      { type: "sendStarted", messageId: "wait:w_aaaaaaaaaa" },
      T0,
      stateSettings,
    )!;
    expect(started.record.messageId).toBe("wait:w_aaaaaaaaaa");
    expect(
      nextWaitState(started.record, { type: "sendStarted", messageId: "other" }, T0, stateSettings),
    ).toBeNull();
    expect(
      nextWaitState(started.record, { type: "rejected" }, T0, stateSettings)?.record.attempt,
    ).toBe(1);
  });

  it("drops a wait only when it is refused long after the deadline, and logs an error", () => {
    const late = T0 + 24 * HOUR + 24 * HOUR;
    const dropped = nextWaitState(delivering(), { type: "rejected" }, late, stateSettings)!;
    expect(dropped.record.state).toBe("dropped");
    expect(dropped.log?.level).toBe("error");
  });

  it("backs off delivery retries up to five minutes", () => {
    expect([0, 1, 2, 6, 7, 50].map(deliveryRetryDelayMs)).toEqual([
      5_000, 10_000, 20_000, 300_000, 300_000, 300_000,
    ]);
  });
});

describe("nextWaitState: hand-over (T24)", () => {
  it("hands a gone thread's wake to its parent with the result", () => {
    const next = nextWaitState(
      delivering({ messageId: "wait:w_aaaaaaaaaa", attempt: 2 }),
      { type: "threadGone", parentThreadId: thread("thread-parent") },
      T0,
      stateSettings,
    )!;
    expect(next.record).toMatchObject({
      state: "delivering",
      threadId: "thread-parent",
      via: ["thread-child"],
      handedOverBecause: "gone",
      attempt: 0,
      result: { outcome: "met" },
    });
    expect(next.record.messageId).toBeUndefined();
    expect(next.log?.level).toBe("warn");
    expect(wakeMessage(next.record)).toBe(
      "[wait w_aaaaaaaaaa] Thread thread-child, which you started, is gone (archived or deleted), so its wait comes to you: check lease-free: met. Lease is free.",
    );
  });

  it("hands a gone thread's still-open wait to its parent at once, marked not ended (check note 3.3)", () => {
    const waiting = makeWait({
      parentThreadId: "thread-parent",
      errorSince: iso(T0),
      lastError: "gh: timeout",
    });
    const next = nextWaitState(
      waiting,
      { type: "threadGone", parentThreadId: thread("thread-parent") },
      T0 + HOUR,
      stateSettings,
    )!;
    expect(next.record).toMatchObject({
      state: "delivering",
      threadId: "thread-parent",
      via: ["thread-child"],
      handedOverBecause: "gone",
      attempt: 0,
    });
    expect(next.record.result).toBeUndefined();
    expect(next.record.errorSince).toBeUndefined();
    expect(next.record.lastError).toBeUndefined();
    expect(next.log?.level).toBe("warn");
    expect(wakeMessage(next.record)).toBe(
      "[wait w_aaaaaaaaaa] Thread thread-child, which you started, is gone (archived or deleted), so its wait comes to you: check lease-free: not ended yet. This wait ends here; check it yourself if you still need it.",
    );
    // It is judged no more, and it is delivered like any other wake.
    expect(
      nextWaitState(
        next.record,
        { type: "judged", judgement: { type: "met", outcome: "met", detail: "" } },
        T0 + HOUR,
        stateSettings,
      ),
    ).toBeNull();
    expect(
      nextWaitState(next.record, { type: "delivered" }, T0 + HOUR, stateSettings)?.record.state,
    ).toBe("delivered");
  });

  it("ends a gone thread's still-open wait as thread_gone when there is no parent", () => {
    const next = nextWaitState(
      makeWait(),
      { type: "threadGone", parentThreadId: null },
      T0,
      stateSettings,
    )!;
    expect(next.record).toMatchObject({ state: "thread_gone", endedAt: iso(T0) });
    expect(next.log?.level).toBe("error");
  });

  it("leaves an ended wait alone when its thread is gone", () => {
    const cancelled = makeWait({ state: "cancelled", endedAt: iso(T0) });
    expect(
      nextWaitState(
        cancelled,
        { type: "threadGone", parentThreadId: thread("p") },
        T0,
        stateSettings,
      ),
    ).toBeNull();
  });

  it("uses the parent kept at registration when the child's lineage is gone", () => {
    const next = nextWaitState(
      delivering({ parentThreadId: "thread-parent" }),
      { type: "threadGone", parentThreadId: null },
      T0,
      stateSettings,
    )!;
    expect(next.record.threadId).toBe("thread-parent");
    expect(next.record.parentThreadId).toBeUndefined();
  });

  it("ends as thread_gone with an error when there is no parent (a fork)", () => {
    const next = nextWaitState(
      delivering(),
      { type: "threadGone", parentThreadId: null },
      T0,
      stateSettings,
    )!;
    expect(next.record).toMatchObject({ state: "thread_gone", endedAt: iso(T0) });
    expect(next.log?.level).toBe("error");
  });

  it(`stops the chain after ${MAX_HAND_OVERS} hand-overs`, () => {
    const via = ["t1", "t2", "t3", "t4", "t5"];
    const next = nextWaitState(
      delivering({ threadId: "t6", via }),
      { type: "threadGone", parentThreadId: thread("t7") },
      T0,
      stateSettings,
    )!;
    expect(next.record.state).toBe("thread_gone");
    expect(next.log?.level).toBe("error");
    const four = nextWaitState(
      delivering({ threadId: "t5", via: via.slice(0, 4) }),
      { type: "threadGone", parentThreadId: thread("t6") },
      T0,
      stateSettings,
    )!;
    expect(four.record.via).toEqual(via);
  });

  it("never goes round a loop or back to itself", () => {
    const loop = nextWaitState(
      delivering({ threadId: "t2", via: ["t1"] }),
      { type: "threadGone", parentThreadId: thread("t1") },
      T0,
      stateSettings,
    )!;
    expect(loop.record.state).toBe("thread_gone");
    const self = nextWaitState(
      delivering(),
      { type: "threadGone", parentThreadId: thread("thread-child") },
      T0,
      stateSettings,
    )!;
    expect(self.record.state).toBe("thread_gone");
  });

  it("hands over a thread still stopped after the limit, and resets when it runs again", () => {
    const parent = thread("thread-parent");
    const stopped = nextWaitState(
      delivering(),
      { type: "threadStopped", parentThreadId: parent },
      T0,
      stateSettings,
    )!;
    expect(stopped.record.stoppedSince).toBe(iso(T0));
    expect(
      nextWaitState(
        stopped.record,
        { type: "threadStopped", parentThreadId: parent },
        T0 + 24 * HOUR - 1,
        stateSettings,
      ),
    ).toBeNull();
    const running = nextWaitState(
      stopped.record,
      { type: "threadRunning" },
      T0 + HOUR,
      stateSettings,
    )!;
    expect(running.record.stoppedSince).toBeUndefined();
    const handed = nextWaitState(
      stopped.record,
      { type: "threadStopped", parentThreadId: parent },
      T0 + 24 * HOUR,
      stateSettings,
    )!;
    expect(handed.record).toMatchObject({
      threadId: "thread-parent",
      via: ["thread-child"],
      handedOverBecause: "stopped",
    });
    expect(handed.record.stoppedSince).toBeUndefined();
    expect(wakeMessage(handed.record)).toContain(
      "Thread thread-child, which you started, was stopped and has not run again, so its wake comes to you (it was not told): ",
    );
  });

  it("uses a short stop limit from settings", () => {
    const short = { ...stateSettings, stoppedHandOverHours: 1 };
    const stopped = delivering({ stoppedSince: iso(T0) });
    expect(
      nextWaitState(stopped, { type: "threadStopped", parentThreadId: null }, T0 + HOUR, short)
        ?.record.state,
    ).toBe("thread_stopped");
  });

  it("names the thread that registered the wait when the chain is longer than one", () => {
    const record = delivering({ threadId: "t3", via: ["t1", "t2"], handedOverBecause: "gone" });
    expect(wakeMessage(record)).toContain("Thread t2, which you started, is gone");
    expect(wakeMessage(record)).toContain("(registered by t1)");
  });
});

describe("wake messages (T25)", () => {
  const ids = Array.from(
    { length: 12 },
    (_, index) => `w_${String.fromCharCode(97 + index).repeat(10)}`,
  );
  const waits = ids.map((id) => delivering({ id }));

  it("puts one wait in one line with the note below", () => {
    const pr = delivering({
      id: "w_k3f7a2m7qx",
      kind: "pr",
      spec: { number: 123, until: "checks" },
      result: {
        outcome: "failed",
        detail: "Head 1a2b3c4. 2 of 14 failed: build, lint.",
        at: iso(T0),
      },
      note: "then rebase",
    });
    expect(wakeMessage(pr)).toBe(
      "[wait w_k3f7a2m7qx] pr 123 checks: failed. Head 1a2b3c4. 2 of 14 failed: build, lint.\nNote: then rebase",
    );
  });

  it("joins up to ten waits, each line starting with its own marker", () => {
    const text = joinedWakeMessage(
      waits.slice(0, 10).map((wait, index) => (index === 0 ? { ...wait, note: "n" } : wait)),
    );
    const lines = text.split("\n");
    expect(lines).toHaveLength(10);
    lines.forEach((line, index) => expect(line.startsWith(`[wait ${ids[index]}] `)).toBe(true));
    expect(lines[0]).toContain(" Note: n");
    expect(() => joinedWakeMessage(waits.slice(0, 11))).toThrow(RangeError);
    expect(() => joinedWakeMessage([])).toThrow(RangeError);
  });

  it("groups by receiver, ten per message, with stable ids", () => {
    const other = delivering({ id: "w_zzzzzzzzzz", threadId: "thread-other" });
    const groups = deliveryGroups([
      ...[...waits].reverse(),
      other,
      makeWait({ id: "w_yyyyyyyyyy" }),
    ]);
    expect(groups.map((group) => [group.threadId, group.messageId, group.waits.length])).toEqual([
      ["thread-child", `wait:${ids[0]}:joined:10`, 10],
      ["thread-child", `wait:${ids[10]}:joined:2`, 2],
      ["thread-other", "wait:w_zzzzzzzzzz", 1],
    ]);
    expect(deliveryCommandId(groups[2]!)).toBe("wait:w_zzzzzzzzzz:0");
  });

  it("keeps the message id of a started send so a retry is the same message", () => {
    const started = waits
      .slice(0, 2)
      .map((wait) => ({ ...wait, messageId: "wait:old:joined:2", attempt: 1 }));
    const groups = deliveryGroups([...started, waits[2]!]);
    expect(groups[0]).toMatchObject({ messageId: "wait:old:joined:2", fresh: false });
    expect(groups[0]!.waits).toHaveLength(2);
    expect(deliveryCommandId(groups[0]!)).toBe("wait:old:joined:2:1");
    expect(groups[1]).toMatchObject({ messageId: `wait:${ids[2]}`, fresh: true });
  });

  it("gives a handed-over wake a new message id", () => {
    const handed = delivering({
      threadId: "thread-parent",
      via: ["thread-child"],
      handedOverBecause: "gone",
    });
    expect(deliveryGroups([handed])[0]!.messageId).toBe("wait:w_aaaaaaaaaa:via:1");
  });
});

describe("wait settings (T20)", () => {
  const withChecks = applyServerSettingsPatch(DEFAULT_SERVER_SETTINGS, {
    waitChecks: decodeChecks({ a: { argv: ["a"] }, b: { argv: ["b"] } }),
    agentSlots: { maxPerAccount: { claude: 2, codex: 1 } as never, maxPerMachine: 4 },
  });

  it("replaces the check map and the account limits whole, and merges other fields", () => {
    const next = applyServerSettingsPatch(withChecks, {
      waitChecks: decodeChecks({ b: { argv: ["b2"] } }),
      agentSlots: { maxPerAccount: { claude: 3 } as never },
      waitFor: { enabled: true },
    });
    expect(Object.keys(next.waitChecks)).toEqual(["b"]);
    expect(next.agentSlots.maxPerAccount).toEqual({ claude: 3 });
    expect(next.agentSlots.maxPerMachine).toBe(4);
    expect(next.waitFor).toEqual({ ...DEFAULT_SERVER_SETTINGS.waitFor, enabled: true });
  });

  it("lets a project override replace the whole check map", () => {
    const projectId = ProjectId.make("project-a");
    const settings = {
      ...withChecks,
      projectSettingsOverrides: {
        [projectId]: { waitChecks: decodeChecks({ c: { argv: ["c"] } }) },
      },
    };
    const resolved = resolveProjectSettings(settings, projectId);
    expect(Object.keys(resolved.settings.waitChecks)).toEqual(["c"]);
    expect(resolved.sources.waitChecks).toBe("project");
    expect(
      Object.keys(
        resolveProjectSettings(settings, ProjectId.make("project-b")).settings.waitChecks,
      ),
    ).toEqual(["a", "b"]);
  });
});
