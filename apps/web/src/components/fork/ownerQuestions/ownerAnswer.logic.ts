/**
 * Fork (Inbox, fork issue 887 row 5): what the answer box shows and posts. The comment starts with
 * the rule's decision prefix, so the rule in contracts (forkOwnerQuestions.ts) reads the question
 * as answered; the waiting label, if the issue carries it, comes off (owner decision in issue 829).
 */
import { isOwnerDecision, type OwnerQuestionRule } from "@t3tools/contracts";

// `Recommendation: x`, `**Recommendation: x**`, `**Recommendation:** x`, `## Recommendation: x`.
const RECOMMENDATION = /^\s{0,3}(?:#{1,6}\s+)?(?:\*\*|__)?Recommendation(?:\*\*|__)?\s*:\s*(.*)$/im;

/** The issue's recommendation line, as its text, or null when the body has none. */
export function recommendationOf(body: string): string | null {
  const match = RECOMMENDATION.exec(body);
  if (match === null) return null;
  const text = (match[1] ?? "")
    .replace(/^(?:\*\*|__)\s*/, "")
    .replace(/\s*(?:\*\*|__)$/, "")
    .trim();
  return text === "" ? null : text;
}

/** The comment the answer box posts, or null for an empty answer. */
export function ownerDecisionBody(answer: string, rule: OwnerQuestionRule): string | null {
  const text = answer.trim();
  if (text === "") return null;
  return isOwnerDecision(text, rule) ? text : `${rule.decisionPrefix}: ${text}`;
}

/**
 * The issue's labels once answered, the whole set as trackers write it; null when the issue does
 * not carry the rule's label (a question asked by title), so no label write is sent.
 */
export function labelsAfterAnswer(
  labels: ReadonlyArray<{ readonly name: string }>,
  rule: OwnerQuestionRule,
): ReadonlyArray<string> | null {
  const waiting = rule.label.toLowerCase();
  const kept = labels.filter((label) => label.name.toLowerCase() !== waiting);
  return kept.length === labels.length ? null : kept.map((label) => label.name);
}

/** What posting an answer did; `labelLeft` says why the waiting label is still on, if it is. */
export type OwnerAnswerOutcome<F> =
  | { readonly kind: "not-posted"; readonly failure: F }
  | {
      readonly kind: "posted";
      readonly labelLeft: null | { readonly reason: "not-allowed" } | { readonly failure: F };
    };

/**
 * Posts the answer, then takes the waiting label off: the comment first, so a failed post never
 * strips the label from a question still waiting; no label write when the issue has no label.
 */
export async function postOwnerAnswer<F extends { readonly _tag: "Failure" }>(input: {
  readonly body: string;
  readonly labels: ReadonlyArray<{ readonly name: string }>;
  readonly rule: OwnerQuestionRule;
  readonly mayChangeLabels: boolean;
  readonly comment: (body: string) => Promise<F | { readonly _tag: "Success" }>;
  readonly setLabels: (labels: ReadonlyArray<string>) => Promise<F | { readonly _tag: "Success" }>;
}): Promise<OwnerAnswerOutcome<F>> {
  const posted = await input.comment(input.body);
  if (posted._tag === "Failure") return { kind: "not-posted", failure: posted };
  const labels = labelsAfterAnswer(input.labels, input.rule);
  if (labels === null) return { kind: "posted", labelLeft: null };
  if (!input.mayChangeLabels) return { kind: "posted", labelLeft: { reason: "not-allowed" } };
  const written = await input.setLabels(labels);
  return {
    kind: "posted",
    labelLeft: written._tag === "Failure" ? { failure: written } : null,
  };
}
