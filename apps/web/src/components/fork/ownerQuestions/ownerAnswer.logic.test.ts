import { DEFAULT_OWNER_QUESTION_RULE, type OwnerQuestionRule } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import {
  labelsAfterAnswer,
  ownerDecisionBody,
  postOwnerAnswer,
  recommendationOf,
} from "./ownerAnswer.logic";

const RULE = DEFAULT_OWNER_QUESTION_RULE;

// Which recommendation does the Inbox show above the answer box?
describe("recommendationOf", () => {
  it("reads the bold recommendation line an owner question opens with", () => {
    expect(recommendationOf("**Recommendation: B, in two steps.**\n\n**Context:** ...")).toBe(
      "B, in two steps.",
    );
  });

  it("reads the plain and the bold-label forms, anywhere in the body", () => {
    expect(recommendationOf("Context first.\n\nRecommendation: yes")).toBe("yes");
    expect(recommendationOf("**Recommendation:** keep it\n")).toBe("keep it");
    expect(recommendationOf("## Recommendation: A\n")).toBe("A");
  });

  it("shows none when the body has no recommendation line", () => {
    expect(recommendationOf("We recommend nothing here.")).toBeNull();
    expect(recommendationOf("")).toBeNull();
    expect(recommendationOf("**Recommendation:**")).toBeNull();
  });
});

// What does the Inbox post, so the rule then reads the question as answered?
describe("ownerDecisionBody", () => {
  it("starts a plain answer with the rule's decision prefix", () => {
    const body = ownerDecisionBody("  yes, B  ", RULE);
    expect(body).toBe("Owner decision: yes, B");
  });

  it("keeps an answer that already starts as a decision", () => {
    expect(ownerDecisionBody("Owner decision (in chat): no", RULE)).toBe(
      "Owner decision (in chat): no",
    );
  });

  it("prefixes 'Owner decision needed', which is not an answer", () => {
    expect(ownerDecisionBody("Owner decision needed later", RULE)).toBe(
      "Owner decision: Owner decision needed later",
    );
  });

  it("posts nothing for an empty answer", () => {
    expect(ownerDecisionBody("   \n", RULE)).toBeNull();
  });

  it("uses a renamed rule's prefix", () => {
    const rule: OwnerQuestionRule = { ...RULE, decisionPrefix: "Decision" };
    expect(ownerDecisionBody("go", rule)).toBe("Decision: go");
  });
});

// Which labels does the issue keep once answered?
describe("labelsAfterAnswer", () => {
  const labels = (...names: string[]) => names.map((name) => ({ name }));

  it("drops the waiting label, whatever its case, and keeps the rest", () => {
    expect(labelsAfterAnswer(labels("bug", "Status:Needs-Owner-Decision"), RULE)).toEqual(["bug"]);
  });

  it("writes nothing when the issue does not carry the label (asked by title)", () => {
    expect(labelsAfterAnswer(labels("bug"), RULE)).toBeNull();
    expect(labelsAfterAnswer([], RULE)).toBeNull();
  });
});

// In which order does posting write, and what is left when a step fails?
describe("postOwnerAnswer", () => {
  const ok = { _tag: "Success" } as const;
  const refused = { _tag: "Failure", why: "refused" } as const;
  const run = (options: {
    readonly labels?: ReadonlyArray<string>;
    readonly mayChangeLabels?: boolean;
    readonly comment?: typeof ok | typeof refused;
    readonly setLabels?: typeof ok | typeof refused;
  }) => {
    const writes: string[] = [];
    const outcome = postOwnerAnswer({
      body: "Owner decision: yes",
      labels: (options.labels ?? ["bug", RULE.label]).map((name) => ({ name })),
      rule: RULE,
      mayChangeLabels: options.mayChangeLabels ?? true,
      comment: async (body) => {
        writes.push(`comment ${body}`);
        return options.comment ?? ok;
      },
      setLabels: async (labels) => {
        writes.push(`labels ${labels.join(",")}`);
        return options.setLabels ?? ok;
      },
    });
    return outcome.then((result) => ({ result, writes }));
  };

  it("posts the decision, then takes the waiting label off", async () => {
    const { result, writes } = await run({});
    expect(writes).toEqual(["comment Owner decision: yes", "labels bug"]);
    expect(result).toEqual({ kind: "posted", labelLeft: null });
  });

  it("leaves the label alone when the post fails, so the question still waits", async () => {
    const { result, writes } = await run({ comment: refused });
    expect(writes).toEqual(["comment Owner decision: yes"]);
    expect(result).toEqual({ kind: "not-posted", failure: refused });
  });

  it("sends no label write for a question asked by title", async () => {
    const { result, writes } = await run({ labels: ["bug"] });
    expect(writes).toEqual(["comment Owner decision: yes"]);
    expect(result).toEqual({ kind: "posted", labelLeft: null });
  });

  it("says why the label stayed: not allowed, or the write failed", async () => {
    const notAllowed = await run({ mayChangeLabels: false });
    expect(notAllowed.writes).toEqual(["comment Owner decision: yes"]);
    expect(notAllowed.result).toEqual({ kind: "posted", labelLeft: { reason: "not-allowed" } });
    const failed = await run({ setLabels: refused });
    expect(failed.result).toEqual({ kind: "posted", labelLeft: { failure: refused } });
  });
});
