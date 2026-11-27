/**
 * Fork (issue 880): the armed PR sweep's judge, one row per rule. Each row asks: given this pull
 * request now and what the last sweep remembered, does it wake (and why), wait, or drop, and what
 * is remembered for the next sweep?
 */
import { describe, expect, it } from "@effect/vitest";

import {
  type ArmedCheck,
  type ArmedPull,
  DISARM_GRACE_MS,
  judgePull,
  latestChecks,
  type Tracked,
  wakeKey,
} from "./armedJudge.ts";

const MIN = 60_000;
const NOW = 1_000 * MIN;
const settings = { stallMinutes: 30, rerunGraceMinutes: 12 };

const pull = (overrides: Partial<ArmedPull> = {}): ArmedPull => ({
  id: "PR_1",
  repository: "acme/app",
  number: 7,
  url: "https://github.com/acme/app/pull/7",
  title: "Add thing",
  state: "open",
  armed: true,
  head: "a".repeat(40),
  mergeable: "mergeable",
  checks: [{ name: "test", state: "success", completedAt: NOW - 5 * MIN }],
  ...overrides,
});

const tracked = (overrides: Partial<Tracked> = {}): Tracked => ({
  id: "PR_1",
  repository: "acme/app",
  number: 7,
  url: "https://github.com/acme/app/pull/7",
  title: "Add thing",
  head: "a".repeat(40),
  notGreenSince: null,
  offSince: null,
  ...overrides,
});

const failed = (name: string, endedMinutesAgo: number): ArmedCheck => ({
  name,
  state: "failure",
  completedAt: NOW - endedMinutesAgo * MIN,
});
const pending = (name: string): ArmedCheck => ({ name, state: "pending", completedAt: null });

interface Row {
  readonly name: string;
  readonly pull: ArmedPull;
  readonly previous?: Tracked;
  readonly verdict: string;
  readonly notGreenSince?: number | null;
  readonly forgotten?: true;
}

const rows: ReadonlyArray<Row> = [
  { name: "green armed pull request", pull: pull(), verdict: "green", notGreenSince: null },
  {
    name: "green again clears the since-when",
    pull: pull(),
    previous: tracked({ notGreenSince: NOW - 20 * MIN }),
    verdict: "green",
    notGreenSince: null,
  },
  {
    name: "conflict wakes at once",
    pull: pull({ mergeable: "conflicting" }),
    verdict: "wake:conflict",
    notGreenSince: NOW,
  },
  {
    name: "red inside the re-run grace waits",
    pull: pull({ checks: [failed("test", 5)] }),
    verdict: "waiting",
    notGreenSince: NOW,
  },
  {
    name: "red at the end of the grace wakes",
    pull: pull({ checks: [failed("test", 12)] }),
    verdict: "wake:red",
  },
  {
    name: "red with a check still running waits for the run to finish",
    pull: pull({ checks: [failed("lint", 40), pending("test")] }),
    previous: tracked({ notGreenSince: NOW - 10 * MIN }),
    verdict: "waiting",
    notGreenSince: NOW - 10 * MIN,
  },
  {
    name: "a re-run in progress (same name pending) is not red",
    pull: pull({ checks: [failed("test", 40), pending("test")] }),
    verdict: "waiting",
  },
  {
    name: "a re-run that passed replaces the failed attempt",
    pull: pull({
      checks: [failed("test", 40), { name: "test", state: "success", completedAt: NOW - MIN }],
    }),
    verdict: "green",
  },
  {
    name: "red with no end time counts from the first sweep that saw it",
    pull: pull({ checks: [{ name: "test", state: "failure", completedAt: null }] }),
    previous: tracked({ notGreenSince: NOW - 13 * MIN }),
    verdict: "wake:red",
  },
  {
    name: "red never counts as stalled inside its grace",
    pull: pull({ checks: [failed("test", 1)] }),
    previous: tracked({ notGreenSince: NOW - 60 * MIN }),
    verdict: "waiting",
  },
  {
    name: "pending under the stall time waits",
    pull: pull({ checks: [pending("test")] }),
    previous: tracked({ notGreenSince: NOW - 29 * MIN }),
    verdict: "waiting",
  },
  {
    name: "pending for the stall time wakes stalled",
    pull: pull({ checks: [pending("test")] }),
    previous: tracked({ notGreenSince: NOW - 30 * MIN }),
    verdict: "wake:stalled",
    notGreenSince: NOW - 30 * MIN,
  },
  {
    name: "no checks at all for the stall time wakes stalled",
    pull: pull({ checks: [] }),
    previous: tracked({ notGreenSince: NOW - 31 * MIN }),
    verdict: "wake:stalled",
  },
  {
    name: "a new head starts its since-when again",
    pull: pull({ head: "b".repeat(40), checks: [pending("test")] }),
    previous: tracked({ notGreenSince: NOW - 60 * MIN }),
    verdict: "waiting",
    notGreenSince: NOW,
  },
  {
    name: "auto-merge off, never followed: not ours",
    pull: pull({ armed: false }),
    verdict: "drop",
    forgotten: true,
  },
  {
    name: "auto-merge just turned off waits (the queue turns it off for a moment itself)",
    pull: pull({ armed: false }),
    previous: tracked(),
    verdict: "waiting",
  },
  {
    name: "auto-merge off for the grace wakes disarmed and forgets it",
    pull: pull({ armed: false }),
    previous: tracked({ offSince: NOW - DISARM_GRACE_MS }),
    verdict: "wake:disarmed",
    forgotten: true,
  },
  {
    name: "armed again clears the off time",
    pull: pull(),
    previous: tracked({ offSince: NOW - 5 * MIN }),
    verdict: "green",
  },
  {
    name: "closed while followed wakes closed",
    pull: pull({ state: "closed" }),
    previous: tracked(),
    verdict: "wake:closed",
    forgotten: true,
  },
  {
    name: "closed, never followed: not ours",
    pull: pull({ state: "closed" }),
    verdict: "drop",
    forgotten: true,
  },
  {
    name: "merged: forgotten without a word",
    pull: pull({ state: "merged" }),
    previous: tracked({ notGreenSince: NOW - 60 * MIN }),
    verdict: "drop",
    forgotten: true,
  },
];

describe("judgePull", () => {
  it.each(rows)("$name", (row) => {
    const judged = judgePull({ pull: row.pull, previous: row.previous, nowMs: NOW, settings });
    const verdict =
      judged.verdict.type === "wake" ? `wake:${judged.verdict.reason}` : judged.verdict.type;
    expect(verdict).toBe(row.verdict);
    if (row.forgotten === true) expect(judged.next).toBeNull();
    else expect(judged.next).not.toBeNull();
    if (row.notGreenSince !== undefined) {
      expect(judged.next?.notGreenSince).toBe(row.notGreenSince);
    }
  });

  it("a disarmed wait keeps the first off time across sweeps", () => {
    const first = judgePull({
      pull: pull({ armed: false }),
      previous: tracked(),
      nowMs: NOW,
      settings,
    });
    const second = judgePull({
      pull: pull({ armed: false }),
      previous: first.next!,
      nowMs: NOW + 5 * MIN,
      settings,
    });
    expect(second.next?.offSince).toBe(NOW);
  });

  it("names every failing check in the detail", () => {
    const judged = judgePull({
      pull: pull({ checks: [failed("lint", 20), failed("test", 15)] }),
      previous: undefined,
      nowMs: NOW,
      settings,
    });
    expect(judged.verdict).toMatchObject({
      type: "wake",
      reason: "red",
      detail: "failing: lint, test",
    });
  });

  it("a zero re-run grace wakes a red at once", () => {
    const judged = judgePull({
      pull: pull({ checks: [failed("test", 0)] }),
      previous: undefined,
      nowMs: NOW,
      settings: { ...settings, rerunGraceMinutes: 0 },
    });
    expect(judged.verdict).toMatchObject({ type: "wake", reason: "red" });
  });
});

describe("latestChecks and wakeKey", () => {
  it("keeps one check per name: pending first, else the latest end", () => {
    expect(
      latestChecks([
        failed("a", 10),
        { name: "a", state: "success", completedAt: NOW },
        pending("b"),
        failed("b", 1),
      ]),
    ).toEqual([{ name: "a", state: "success", completedAt: NOW }, pending("b")]);
  });

  it("keys a wake by pull request, head and reason", () => {
    expect(wakeKey(tracked(), "red")).toBe(`acme/app#7@${"a".repeat(40)}:red`);
  });
});
