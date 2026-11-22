import { describe, expect, it } from "vite-plus/test";

import {
  DEFAULT_OWNER_QUESTION_RULE,
  isOwnerDecision,
  isOwnerQuestion,
  ownerQuestionStatus,
} from "./forkOwnerQuestions.ts";

const issue = (
  title: string,
  labels: ReadonlyArray<string> = [],
  category: "open" | "closed" = "open",
) => ({
  title,
  labels: labels.map((name) => ({ name })),
  state: { category },
});

describe("isOwnerQuestion", () => {
  it.each([
    ["Owner question: may agents merge?", [], true],
    ["owner question: lower case still asks", [], true],
    ["Owner questions are tracked here", [], false],
    ["Issues page Q1: how far should we change it?", [], true],
    ["Calm sidebar Q12: one group or two?", [], true],
    ["Q1: no topic before it", [], false],
    ["Fix Q1 numbers in the report", [], false],
    ["Plain bug report", ["status:needs-owner-decision"], true],
    ["Plain bug report", ["question"], false],
    ["Plain bug report", ["Status:Needs-Owner-Decision"], true],
  ] as const)("%s %j -> %s", (title, labels, expected) => {
    expect(isOwnerQuestion(issue(title, labels), DEFAULT_OWNER_QUESTION_RULE)).toBe(expected);
  });

  it("follows a renamed rule, not the defaults", () => {
    const rule = {
      label: "needs-decision",
      titlePrefixes: ["Decision needed"],
      topicQuestionTitles: false,
      decisionPrefix: "Decided",
    };
    expect(isOwnerQuestion(issue("Decision needed: which runner?"), rule)).toBe(true);
    expect(isOwnerQuestion(issue("Owner question: which runner?"), rule)).toBe(false);
    expect(isOwnerQuestion(issue("Issues page Q1: how far?"), rule)).toBe(false);
    expect(isOwnerQuestion(issue("Anything", ["needs-decision"]), rule)).toBe(true);
  });
});

describe("isOwnerDecision", () => {
  it.each([
    ["Owner decision: yes.", true],
    ["Owner decision (in chat, 2026-11-22): a version of C.", true],
    ["**Owner decision:** keep it.", true],
    ["  \n owner decision: lower case", true],
    ["> Owner decision: a quote of one is not one", false],
    ["Waiting on the owner decision for this", false],
    ["", false],
  ] as const)("%j -> %s", (body, expected) => {
    expect(isOwnerDecision(body, DEFAULT_OWNER_QUESTION_RULE)).toBe(expected);
  });
});

describe("ownerQuestionStatus", () => {
  const question = issue("Owner question: may agents merge?");
  const decided = [{ body: "A note" }, { body: "Owner decision: no." }];

  it("is waiting while open and no comment decides it", () => {
    expect(ownerQuestionStatus(question, [{ body: "A note" }], DEFAULT_OWNER_QUESTION_RULE)).toBe(
      "waiting",
    );
  });

  it("is answered once a comment decides it, label or not", () => {
    expect(ownerQuestionStatus(question, decided, DEFAULT_OWNER_QUESTION_RULE)).toBe("answered");
    const labelled = issue("Bug", ["status:needs-owner-decision"]);
    expect(ownerQuestionStatus(labelled, decided, DEFAULT_OWNER_QUESTION_RULE)).toBe("answered");
  });

  it("is unknown while the comments are not read yet", () => {
    expect(ownerQuestionStatus(question, undefined, DEFAULT_OWNER_QUESTION_RULE)).toBe("unknown");
  });

  it("is not a question when closed or when the rule does not match", () => {
    const closed = issue("Owner question: may agents merge?", [], "closed");
    expect(ownerQuestionStatus(closed, [], DEFAULT_OWNER_QUESTION_RULE)).toBe("not-waiting");
    expect(ownerQuestionStatus(issue("Bug"), [], DEFAULT_OWNER_QUESTION_RULE)).toBe("not-waiting");
  });
});
