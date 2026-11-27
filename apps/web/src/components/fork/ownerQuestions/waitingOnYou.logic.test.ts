import {
  DEFAULT_OWNER_QUESTION_RULE,
  EnvironmentId,
  type IssueListEntry,
  type IssueListResult,
  ProjectId,
} from "@t3tools/contracts";
import { AsyncResult } from "effect/reactivity";
import { Cause } from "effect";
import { describe, expect, it } from "vite-plus/test";

import {
  commentsOfDetail,
  listingGaps,
  ownerQuestionCandidates,
  ownerQuestionRuleText,
  ownerQuestionTargets,
  readInFull,
  waitingOnYou,
} from "./waitingOnYou.logic";

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

const answer = (
  entries: ReadonlyArray<IssueListEntry>,
  overrides: Partial<IssueListResult> = {},
): IssueListResult => ({
  viewers: {},
  trackers: [],
  entries,
  errors: [],
  nextCursors: {},
  ...overrides,
});

describe("ownerQuestionTargets", () => {
  it("lists the open issues of every project on each server, with no repository named", () => {
    const targets = ownerQuestionTargets([laptop, server], DEFAULT_OWNER_QUESTION_RULE);
    expect(targets.all.map((target) => target.environmentId)).toEqual([laptop, server]);
    for (const target of targets.all) {
      expect(target.input).toEqual({ state: "open", limit: 200, sort: "updated" });
    }
  });

  it("also lists the issues carrying the rule's label, so a labelled question is never cut", () => {
    const targets = ownerQuestionTargets([laptop, server], DEFAULT_OWNER_QUESTION_RULE);
    expect(targets.labelled.map((target) => target.environmentId)).toEqual([laptop, server]);
    for (const target of targets.labelled) {
      expect(target.input).toEqual({
        state: "open",
        limit: 200,
        sort: "updated",
        labels: ["status:needs-owner-decision"],
      });
    }
  });
});

describe("listingGaps", () => {
  const targets = ownerQuestionTargets([laptop, server], DEFAULT_OWNER_QUESTION_RULE);
  const listing = (
    which: "all" | "labelled",
    answers: ReadonlyArray<readonly [typeof laptop, IssueListResult]>,
    error: string | null = null,
  ) => ({ targets: targets[which], answers, error });
  const everyServer = (result: IssueListResult = answer([])) =>
    [
      [laptop, result],
      [server, result],
    ] as const;

  it("is read in full once every server answered both listings and none was cut", () => {
    const gaps = listingGaps({
      all: listing("all", everyServer()),
      labelled: listing("labelled", everyServer()),
    });
    expect(gaps).toEqual({ unread: [], failed: false, cut: [] });
    expect(readInFull(gaps)).toBe(true);
  });

  it("names the repository whose listing was cut, so the page never claims nothing waits", () => {
    // 200 open issues and more: the owner question that waited longest is past the cut.
    const cutAnswer = answer([entry("9", "A bug")], {
      nextCursors: { "github.com acme/app": "page-2" },
    });
    const gaps = listingGaps({
      all: listing("all", [
        [laptop, cutAnswer],
        [server, cutAnswer],
      ]),
      labelled: listing("labelled", everyServer()),
    });
    expect(gaps.cut).toEqual([{ host: "github.com", scope: "acme/app", labelled: false }]);
    expect(readInFull(gaps)).toBe(false);
  });

  // Does the cut line write the repository as the project names it, not as the cursor key does?
  it("names a cut repository as the listing writes it, not lowercased", () => {
    const cutAnswer = answer([entry("9", "A bug")], {
      nextCursors: { "github.com acme/myapp": "page-2" },
      scopes: [
        {
          projectId: ProjectId.make("app"),
          tracker: "github",
          host: "github.com",
          scope: "Acme/MyApp",
        } as NonNullable<IssueListResult["scopes"]>[number],
      ],
    });
    const gaps = listingGaps({
      all: listing("all", everyServer(cutAnswer)),
      labelled: listing("labelled", everyServer()),
    });
    expect(gaps.cut).toEqual([{ host: "github.com", scope: "Acme/MyApp", labelled: false }]);
  });

  it("names a repository whose labelled listing was cut too", () => {
    const gaps = listingGaps({
      all: listing("all", everyServer()),
      labelled: listing(
        "labelled",
        everyServer(answer([], { nextCursors: { "github.com acme/app": "page-2" } })),
      ),
    });
    expect(gaps.cut).toEqual([{ host: "github.com", scope: "acme/app", labelled: true }]);
    expect(readInFull(gaps)).toBe(false);
  });

  it("is not read in full while a server has not answered yet", () => {
    const gaps = listingGaps({
      all: listing("all", [[laptop, answer([])]]),
      labelled: listing("labelled", everyServer()),
    });
    expect(gaps.unread).toEqual([server]);
    expect(readInFull(gaps)).toBe(false);
  });

  it("is not read in full after a server or a project failed", () => {
    const serverFailed = listingGaps({
      all: listing("all", [[laptop, answer([])]], "Server offline"),
      labelled: listing("labelled", everyServer()),
    });
    expect(serverFailed.failed).toBe(true);
    expect(readInFull(serverFailed)).toBe(false);
    const projectFailed = listingGaps({
      all: listing(
        "all",
        everyServer(
          answer([], {
            errors: [
              { projectId: ProjectId.make("app"), projectTitle: "App", message: "No access" },
            ],
          }),
        ),
      ),
      labelled: listing("labelled", everyServer()),
    });
    expect(readInFull(projectFailed)).toBe(false);
  });
});

describe("commentsOfDetail", () => {
  const detail = { comments: [{ body: "Owner decision: yes." }] };

  it("gives the comments a read returned", () => {
    expect(commentsOfDetail(AsyncResult.success(detail))).toEqual(detail.comments);
  });

  it("is not known yet while the first read runs", () => {
    expect(commentsOfDetail(AsyncResult.initial(true))).toBeNull();
    expect(commentsOfDetail(AsyncResult.failure(Cause.fail("offline"), { waiting: true }))).toBe(
      null,
    );
  });

  it("counts a read that failed as no answer, so the question shows rather than hides", () => {
    const comments = commentsOfDetail<{ body: string }>(AsyncResult.failure(Cause.fail("offline")));
    expect(comments).toEqual([]);
    const question = { ...entry("1", "Owner question: still there?"), environmentId: laptop };
    const view = waitingOnYou([question], () => comments ?? undefined, DEFAULT_OWNER_QUESTION_RULE);
    expect(view.waiting).toEqual([question]);
  });
});

describe("ownerQuestionRuleText", () => {
  it("is built from the rule, so a renamed rule renames the help text", () => {
    expect(ownerQuestionRuleText(DEFAULT_OWNER_QUESTION_RULE)).toBe(
      'An issue asks you when its title starts with "Owner question" or "<Topic> Q<n>:", or it carries the label status:needs-owner-decision.',
    );
    expect(
      ownerQuestionRuleText({
        label: "needs-decision",
        titlePrefixes: ["Decision needed", "Ask", "Pick"],
        topicQuestionTitles: false,
        decisionPrefix: "Decided",
      }),
    ).toBe(
      'An issue asks you when its title starts with "Decision needed", "Ask" or "Pick", or it carries the label needs-decision.',
    );
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
