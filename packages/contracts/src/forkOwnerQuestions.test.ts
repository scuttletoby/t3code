import { describe, expect, it } from "vite-plus/test";

import {
  DEFAULT_OWNER_QUESTION_RULE,
  isOwnerDecision,
  isOwnerQuestion,
  ownerQuestionStatus,
  ownerQuestionTitleForms,
  type OwnerQuestionRule,
} from "./forkOwnerQuestions.ts";
// The cases the fork-ci action's copy of the rule runs too (owner_questions.test.py).
import RULE_CASES_SOURCE from "../../../fork-ci/actions/owner-questions/rule-cases.json?raw";

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
    ["**Owner decision**: keep it.", true],
    // An agent's status note names the decision without giving it.
    ["Owner decision needed before PR 2 lands.", false],
    ["Owner decision pending: asked in chat.", false],
    ["Owner decision - yes", false],
    ["Owner decision", false],
    ["  \n owner decision: lower case", true],
    ["> Owner decision: a quote of one is not one", false],
    ["Waiting on the owner decision for this", false],
    ["", false],
  ] as const)("%j -> %s", (body, expected) => {
    expect(isOwnerDecision(body, DEFAULT_OWNER_QUESTION_RULE)).toBe(expected);
  });
});

describe("ownerQuestionTitleForms", () => {
  it("names the title forms the rule asks by, so the page's help text has no copy of them", () => {
    expect(ownerQuestionTitleForms(DEFAULT_OWNER_QUESTION_RULE)).toEqual([
      "Owner question",
      "<Topic> Q<n>:",
    ]);
    const renamed = {
      label: "needs-decision",
      titlePrefixes: ["Decision needed", "Ask"],
      topicQuestionTitles: false,
      decisionPrefix: "Decided",
    };
    expect(ownerQuestionTitleForms(renamed)).toEqual(["Decision needed", "Ask"]);
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

// Do this rule and the fork-ci owner-questions action's copy answer every shared case alike?
describe("the cases shared with the owner-questions action", () => {
  const cases = JSON.parse(RULE_CASES_SOURCE) as {
    readonly defaultRule: OwnerQuestionRule;
    readonly renamedRule: OwnerQuestionRule;
    readonly questions: ReadonlyArray<{
      readonly rule: "default" | "renamed";
      readonly title: string;
      readonly labels: ReadonlyArray<string>;
      readonly expect: boolean;
    }>;
    readonly decisions: ReadonlyArray<{
      readonly rule: "default" | "renamed";
      readonly body: string;
      readonly expect: boolean;
    }>;
  };
  const ruleOf = (name: "default" | "renamed") =>
    name === "default" ? DEFAULT_OWNER_QUESTION_RULE : cases.renamedRule;

  it("has the defaults this file has", () => {
    expect(cases.defaultRule).toEqual(DEFAULT_OWNER_QUESTION_RULE);
  });

  it("asks the owner exactly in the shared question cases", () => {
    for (const entry of cases.questions) {
      expect(
        isOwnerQuestion(issue(entry.title, entry.labels), ruleOf(entry.rule)),
        entry.title,
      ).toBe(entry.expect);
    }
  });

  it("reads an answer exactly in the shared decision cases", () => {
    for (const entry of cases.decisions) {
      expect(isOwnerDecision(entry.body, ruleOf(entry.rule)), entry.body).toBe(entry.expect);
    }
  });
});
