import {
  DEFAULT_OWNER_QUESTION_RULE,
  EnvironmentId,
  type IssueListEntry,
  type IssueListResult,
  ProjectId,
} from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import { ownerQuestionCandidates, ownerQuestionTargets, waitingOnYou } from "./waitingOnYou.logic";

const laptop = EnvironmentId.make("laptop");
const server = EnvironmentId.make("server");

const entry = (key: string, title: string, overrides: Partial<IssueListEntry> = {}) =>
  ({
    tracker: "github",
    host: "github.com",
    scope: "acme/app",
    key,
    displayKey: `#${key}`,
    title,
    url: `https://github.com/acme/app/issues/${key}`,
    author: null,
    state: { category: "open", name: "Open" },
    createdAt: "2026-11-01T00:00:00.000Z",
    updatedAt: `2026-11-${key.padStart(2, "0")}T00:00:00.000Z`,
    closedAt: null,
    assignees: [],
    labels: [],
    commentCount: 0,
    projectId: ProjectId.make("app"),
    projectTitle: "App",
    ...overrides,
  }) as IssueListEntry;

const answer = (entries: ReadonlyArray<IssueListEntry>): IssueListResult => ({
  viewers: {},
  trackers: [],
  entries,
  errors: [],
  nextCursors: {},
});

describe("ownerQuestionTargets", () => {
  it("lists the open issues of every project on each server, with no repository named", () => {
    const targets = ownerQuestionTargets([laptop, server]);
    expect(targets.map((target) => target.environmentId)).toEqual([laptop, server]);
    for (const target of targets) {
      expect(target.input).toEqual({ state: "open", limit: 200, sort: "updated" });
    }
  });
});

describe("ownerQuestionCandidates", () => {
  it("keeps the owner questions, newest first, each issue once across servers and projects", () => {
    const question = entry("3", "Owner question: may agents merge?");
    const candidates = ownerQuestionCandidates(
      [
        [laptop, answer([entry("1", "A bug"), question])],
        [
          server,
          answer([
            entry("2", "Issues page Q1: how far?"),
            // The same issue read by a second project on another server.
            { ...question, projectId: ProjectId.make("app-2"), projectTitle: "App 2" },
            entry("4", "A feature", {
              labels: [{ name: "status:needs-owner-decision", color: null }],
            }),
          ]),
        ],
      ],
      DEFAULT_OWNER_QUESTION_RULE,
    );
    expect(candidates.map((candidate) => [candidate.environmentId, candidate.key])).toEqual([
      [server, "4"],
      [laptop, "3"],
      [server, "2"],
    ]);
  });

  it("drops a closed owner question", () => {
    const closed = entry("5", "Owner question: done?", {
      state: { category: "closed", name: "Closed" },
    });
    expect(
      ownerQuestionCandidates([[laptop, answer([closed])]], DEFAULT_OWNER_QUESTION_RULE),
    ).toEqual([]);
  });
});

describe("waitingOnYou", () => {
  const candidates = ownerQuestionCandidates(
    [
      [
        laptop,
        answer([
          entry("1", "Owner question: one?"),
          entry("2", "Owner question: two?"),
          entry("3", "Owner question: three?"),
        ]),
      ],
    ],
    DEFAULT_OWNER_QUESTION_RULE,
  );

  it("leaves out answered questions and counts the ones whose comments are still loading", () => {
    const comments = new Map<string, ReadonlyArray<{ body: string }>>([
      ["1", [{ body: "Owner decision: yes." }]],
      ["3", [{ body: "A note from an agent" }]],
    ]);
    const view = waitingOnYou(
      candidates,
      (candidate) => comments.get(candidate.key),
      DEFAULT_OWNER_QUESTION_RULE,
    );
    expect(view.waiting.map((candidate) => candidate.key)).toEqual(["3"]);
    expect(view.checking).toBe(1);
  });
});
