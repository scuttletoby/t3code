/**
 * Fork: the owner-question rule, written once. Which issues ask the owner something, and which
 * comment answers one. The web's "Waiting on you" page reads it, and later the server's agent tools
 * do too; neither keeps a copy. The defaults are the owner's wording; a project will be able to
 * rename them in its own config.
 */

/** How a project marks an owner question and its answer. */
export interface OwnerQuestionRule {
  /** An issue carrying this label waits on the owner, whatever its title. Case does not matter. */
  readonly label: string;
  /** A title starting with one of these asks the owner (`Owner question: ...`). */
  readonly titlePrefixes: ReadonlyArray<string>;
  /** A title like `<Topic> Q<n>: ...` (`Issues page Q1: ...`) asks the owner too. */
  readonly topicQuestionTitles: boolean;
  /** A comment starting with this answers the question (`Owner decision: ...`). */
  readonly decisionPrefix: string;
}

export const DEFAULT_OWNER_QUESTION_RULE: OwnerQuestionRule = {
  label: "status:needs-owner-decision",
  titlePrefixes: ["Owner question"],
  topicQuestionTitles: true,
  decisionPrefix: "Owner decision",
};

/** What the rule reads of an issue; a listing's row and a detail both have it. */
export interface OwnerQuestionIssue {
  readonly title: string;
  readonly labels: ReadonlyArray<{ readonly name: string }>;
  readonly state: { readonly category: "open" | "closed" };
}

// A topic of a few words, then ` Q<n>:`. Bounded, so a long title costs nothing.
const TOPIC_QUESTION = /^[^\s:][^:\n]{0,80} Q\d{1,3}:/;

const startsWithWord = (text: string, prefix: string): boolean => {
  const head = text.slice(0, prefix.length);
  if (head.toLowerCase() !== prefix.toLowerCase()) return false;
  // `Owner question:` asks; `Owner questions are...` does not.
  return !/[\p{L}\p{N}]/u.test(text.charAt(prefix.length));
};

/** True when the issue asks the owner something, open or not. */
export function isOwnerQuestion(issue: OwnerQuestionIssue, rule: OwnerQuestionRule): boolean {
  const label = rule.label.toLowerCase();
  if (issue.labels.some((entry) => entry.name.toLowerCase() === label)) return true;
  const title = issue.title.trim();
  if (rule.titlePrefixes.some((prefix) => startsWithWord(title, prefix))) return true;
  return rule.topicQuestionTitles && TOPIC_QUESTION.test(title);
}

/** True when the comment answers an owner question; markdown bold around the prefix is allowed. */
export function isOwnerDecision(body: string, rule: OwnerQuestionRule): boolean {
  const text = body.trimStart().replace(/^\*\*|^__/, "");
  return startsWithWord(text, rule.decisionPrefix);
}

/**
 * `waiting`: an open owner question no comment answers. `answered`: a comment answers it.
 * `unknown`: an open owner question whose comments are not read yet. `not-waiting`: closed, or not
 * an owner question.
 */
export type OwnerQuestionStatus = "waiting" | "answered" | "unknown" | "not-waiting";

export function ownerQuestionStatus(
  issue: OwnerQuestionIssue,
  comments: ReadonlyArray<{ readonly body: string }> | undefined,
  rule: OwnerQuestionRule,
): OwnerQuestionStatus {
  if (issue.state.category !== "open" || !isOwnerQuestion(issue, rule)) return "not-waiting";
  if (comments === undefined) return "unknown";
  return comments.some((comment) => isOwnerDecision(comment.body, rule)) ? "answered" : "waiting";
}
