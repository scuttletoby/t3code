// Fork (work panel, fork issue 1136): what the panel shows, from shells and the server's answer.
import type { ThreadId, ThreadPullRequestLink } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import {
  familyPullRequests,
  formatAge,
  pullRequestCi,
  pullRequestCiText,
  repoQueueLines,
  repoQueueText,
} from "./workPanel.logic";

const id = (value: string) => value as ThreadId;
const link = (number: number, over: Partial<ThreadPullRequestLink> = {}) =>
  ({
    host: "github.com",
    repository: "owner/repo",
    number,
    url: `https://github.com/owner/repo/pull/${number}`,
    source: "agent",
    snapshot: null,
    ...over,
  }) as unknown as ThreadPullRequestLink;
const shell = (
  name: string,
  parent: string | null,
  pullRequests: Array<ThreadPullRequestLink>,
) => ({
  id: id(name),
  title: name,
  lineage: { parentThreadId: parent === null ? null : id(parent) },
  pullRequests,
});

describe("familyPullRequests", () => {
  it("takes this thread's and every descendant's PRs once, not a sibling's or a dismissed one", () => {
    const prs = familyPullRequests(id("a"), [
      shell("a", null, [link(1)]),
      shell("b", "a", [link(1), link(2)]),
      shell("c", "b", [link(3), link(4, { source: "stack-dismissed" } as never)]),
      shell("sibling", null, [link(5)]),
    ]);
    expect(prs.map((pr) => [pr.link.number, pr.threadTitle, pr.fromChild])).toEqual([
      [1, "a", false],
      [2, "b", true],
      [3, "c", true],
    ]);
  });

  it("a thread the client does not hold has none", () => {
    expect(familyPullRequests(id("missing"), [shell("a", null, [link(1)])])).toEqual([]);
  });
});

describe("pullRequestCi", () => {
  it("falls back from runs to the watch to the snapshot to unknown", () => {
    const runs = {
      host: "github.com",
      repository: "owner/repo",
      number: 1,
      queued: 0,
      running: 1,
      failed: 0,
      passed: 2,
      oldestQueuedAt: null,
      url: null,
    };
    expect(pullRequestCi(link(1), runs).source).toBe("runs");
    expect(
      pullRequestCi(
        link(1, {
          watch: { failedChecks: [], passedChecks: ["a"], checksAwaitedSince: null } as never,
        }),
        undefined,
      ),
    ).toEqual({ source: "watch", failed: 0, passed: 1, pending: false, waitingSince: null });
    expect(
      pullRequestCi(link(1, { snapshot: { checksState: "failing" } as never }), undefined),
    ).toEqual({ source: "snapshot", state: "failing" });
    expect(pullRequestCi(link(1), undefined)).toEqual({ source: "unknown" });
  });

  it("words a watch still waiting for its first check with the wait", () => {
    const now = Date.parse("2026-11-27T12:00:00Z");
    const ci = pullRequestCi(
      link(1, {
        watch: {
          failedChecks: [],
          passedChecks: [],
          checksAwaitedSince: new Date(now - 12 * 60_000).toISOString(),
        } as never,
      }),
      undefined,
    );
    expect(pullRequestCiText(ci, now)).toBe("waiting for checks (12 min)");
  });
});

describe("repoQueueLines", () => {
  it("is unknown with the given reason for a repository the server did not answer", () => {
    const lines = repoQueueLines(
      familyPullRequests(id("a"), [
        shell("a", null, [link(1), link(2, { repository: "owner/other" })]),
      ]),
      {
        answeredAt: 0,
        repoQueues: [
          {
            host: "github.com",
            repository: "owner/repo",
            state: "answered",
            queued: 0,
            running: 0,
          },
        ],
        pullRequests: [],
      },
      "not read",
    );
    expect(lines.map((line) => [line.repository, line.state, line.reason])).toEqual([
      ["owner/repo", "answered", undefined],
      ["owner/other", "unknown", "not read"],
    ]);
    expect(repoQueueText(lines[0]!, 0)).toBe("0 queued · 0 running");
    expect(repoQueueText(lines[1]!, 0)).toBe("unknown: not read");
  });
});

describe("formatAge", () => {
  it.each([
    [10_000, "less than a minute"],
    [12 * 60_000, "12 min"],
    [120 * 60_000, "2 h"],
    [125 * 60_000, "2 h 5 min"],
  ])("%d ms reads %s", (ms, text) => {
    expect(formatAge(ms)).toBe(text);
  });
});
