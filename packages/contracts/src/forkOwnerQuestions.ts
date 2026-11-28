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
  /**
   * A comment starting with this, then `:` or ` (`, answers the question (`Owner decision: ...`,
   * `Owner decision (in chat): ...`). `Owner decision needed` does not.
   */
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
/** How the help text writes the topic-question form that `TOPIC_QUESTION` matches. */
const TOPIC_QUESTION_FORM = "<Topic> Q<n>:";

/** The title forms the rule asks by, as help text names them: the prefixes, then the topic form. */
export function ownerQuestionTitleForms(rule: OwnerQuestionRule): ReadonlyArray<string> {
  return rule.topicQuestionTitles
    ? [...rule.titlePrefixes, TOPIC_QUESTION_FORM]
    : rule.titlePrefixes;
}

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

/**
 * True when the comment answers an owner question: the prefix, then `:` or ` (`. Markdown bold
 * around the prefix is allowed. A status note (`Owner decision needed ...`) is not an answer.
 */
export function isOwnerDecision(body: string, rule: OwnerQuestionRule): boolean {
  const text = body.trimStart().replace(/^\*\*|^__/, "");
  const prefix = rule.decisionPrefix;
  if (text.slice(0, prefix.length).toLowerCase() !== prefix.toLowerCase()) return false;
  const rest = text.slice(prefix.length).replace(/^\*\*|^__/, "");
  return rest.startsWith(":") || rest.startsWith(" (");
}

/**
 * Who may decide: GitHub's `author_association` of the repository's owner, its organisation's
 * members and its collaborators. Anyone can comment on a public repository; their "Owner decision:"
 * answers nothing.
 */
const OWNER_DECISION_ASSOCIATIONS: ReadonlyArray<string> = ["OWNER", "MEMBER", "COLLABORATOR"];

/** What the rule reads of a comment. */
export interface OwnerDecisionComment {
  readonly body: string;
  /**
   * GitHub's `author_association`. Absent when the source does not say who wrote the comment (the
   * issue reads this page uses have none), and then the words alone count; null decides nothing.
   */
  readonly authorAssociation?: string | null;
}

/** True when the comment answers an owner question: the decision's words, from someone who may decide. */
export function isOwnerDecisionComment(
  comment: OwnerDecisionComment,
  rule: OwnerQuestionRule,
): boolean {
  if (
    comment.authorAssociation !== undefined &&
    !OWNER_DECISION_ASSOCIATIONS.includes(comment.authorAssociation ?? "")
  ) {
    return false;
  }
  return isOwnerDecision(comment.body, rule);
}

/**
 * `waiting`: an open owner question no comment answers. `answered`: a comment answers it.
 * `unknown`: an open owner question whose comments are not read yet. `not-waiting`: closed, or not
 * an owner question.
 */
export type OwnerQuestionStatus = "waiting" | "answered" | "unknown" | "not-waiting";

export function ownerQuestionStatus(
  issue: OwnerQuestionIssue,
  comments: ReadonlyArray<OwnerDecisionComment> | undefined,
  rule: OwnerQuestionRule,
): OwnerQuestionStatus {
  if (issue.state.category !== "open" || !isOwnerQuestion(issue, rule)) return "not-waiting";
  if (comments === undefined) return "unknown";
  return comments.some((comment) => isOwnerDecisionComment(comment, rule)) ? "answered" : "waiting";
}
