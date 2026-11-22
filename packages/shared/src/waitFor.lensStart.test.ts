/**
 * Fork (review rules, lens_start after CI): a `pr` wait narrowed to some checks at one head, and the
 * wake line of a wait that starts the review lenses when it passes.
 */
import type { PullRequestCheck } from "@t3tools/contracts";
import { WaitRecord } from "@t3tools/contracts/waitFor";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { judgePrChecks, type PrSeen, waitKey, wakeMessage } from "./waitFor.ts";
import { ThreadId } from "@t3tools/contracts";

const HEAD = "a".repeat(40);
const NEWER = "b".repeat(40);
const T0 = Date.parse("2026-11-22T10:00:00.000Z");
const prSettings = { prSweepSeconds: 120, noChecksGraceSeconds: 900 };

const check = (name: string, status: PullRequestCheck["status"]): PullRequestCheck => ({
  name,
  status,
  description: null,
  url: null,
});

const judge = (input: {
  readonly checks: ReadonlyArray<PullRequestCheck>;
  readonly headSha?: string;
  readonly seen?: PrSeen | null;
  readonly now: number;
}) =>
  judgePrChecks({
    detail: { state: "open", headSha: input.headSha ?? HEAD },
    checks: { checks: input.checks },
    until: "checks",
    seen: input.seen ?? null,
    now: input.now,
    settings: prSettings,
    only: { checks: ["ci-result"], head: HEAD },
  });

/** Two reads `prSweepSeconds` apart, as the settle rule needs. */
const settled = (checks: ReadonlyArray<PullRequestCheck>, headSha?: string) => {
  const first = judge({ checks, now: T0, ...(headSha === undefined ? {} : { headSha }) });
  return judge({
    checks,
    seen: first.seen,
    now: T0 + 121_000,
    ...(headSha === undefined ? {} : { headSha }),
  });
};

describe("judgePrChecks narrowed to named checks at one head", () => {
  it("passes on the named check alone: a red gate check that needs the lenses does not count", () => {
    const answer = settled([check("ci-result", "success"), check("head_check", "failure")]);
    expect(answer.type).toBe("met");
    expect(answer.type === "met" && answer.outcome).toBe("passed");
  });

  it("fails when the named check is red", () => {
    const answer = settled([check("ci-result", "failure"), check("head_check", "failure")]);
    expect(answer.type === "met" && answer.outcome).toBe("failed");
  });

  it("is not met while the named check has not appeared, whatever the others say", () => {
    const answer = settled([check("other", "success")]);
    expect(answer.type).toBe("notYet");
  });

  it("ends as moved when the head is no longer the one asked for", () => {
    const answer = judge({ checks: [check("ci-result", "success")], headSha: NEWER, now: T0 });
    expect(answer.type).toBe("met");
    expect(answer.type === "met" && answer.outcome).toBe("moved");
    expect(answer.type === "met" && answer.detail).toContain(NEWER.slice(0, 7));
  });
});

describe("a wait that starts the lenses", () => {
  const decode = Schema.decodeUnknownSync(WaitRecord);
  const base = {
    id: "w_aaaaaaaaaa",
    threadId: "thread-a",
    projectId: "project-a",
    key: "aaaaaaaaaaaaaaaa",
    createdAt: "2026-11-22T10:00:00.000Z",
    deadlineAt: "2026-11-23T10:00:00.000Z",
    state: "delivering",
    attempt: 0,
    kind: "pr",
    spec: { number: 7, repository: "o/r", until: "checks", checks: ["ci-result"], head: HEAD },
    action: "lens_start",
  };

  it("keeps the narrowing and the action in the stored record", () => {
    const wait = decode({
      ...base,
      result: { outcome: "passed", detail: "ok", at: base.createdAt },
    });
    expect(wait.action).toBe("lens_start");
    expect(wait.kind === "pr" && wait.spec.checks).toEqual(["ci-result"]);
    expect(wait.kind === "pr" && wait.spec.head).toBe(HEAD);
  });

  it("says in its wake that the server starts the lenses, only when it passed", () => {
    const passed = wakeMessage(
      decode({ ...base, result: { outcome: "passed", detail: "ok", at: base.createdAt } }),
    );
    expect(passed).toContain("The server starts the review lenses");
    const red = wakeMessage(
      decode({ ...base, result: { outcome: "failed", detail: "red", at: base.createdAt } }),
    );
    expect(red).toContain("No review lens starts for this head");
    expect(red).not.toContain("The server starts the review lenses");
  });

  it("is another wait than the plain pull request wait (another key)", () => {
    const thread = ThreadId.make("thread-a");
    const plain = waitKey(thread, { kind: "pr", spec: { number: 7, until: "checks" } });
    const narrowed = waitKey(thread, {
      kind: "pr",
      spec: { number: 7, until: "checks", checks: ["ci-result"], head: HEAD },
    });
    expect(narrowed).not.toBe(plain);
  });
});
