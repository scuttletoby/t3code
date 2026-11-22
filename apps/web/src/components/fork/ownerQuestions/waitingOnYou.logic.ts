/**
 * Fork: what the "Waiting on you" page lists. It reads upstream's issue listing (the
 * repositories T3 already lists for each project, none named here) and keeps the issues the
 * owner-question rule in contracts (forkOwnerQuestions.ts) says wait on the owner.
 */
import type { EnvironmentIssueEntry } from "@t3tools/client-runtime/issue-list";
import {
  type EnvironmentId,
  type IssueListInput,
  type IssueListResult,
  isOwnerQuestion,
  type OwnerQuestionRule,
  ownerQuestionStatus,
} from "@t3tools/contracts";

import type { IssueListTarget } from "~/state/issues";

/** Every open issue, newest first; the rule picks the questions, as titles cannot be searched. */
const OWNER_QUESTION_LIST_INPUT: IssueListInput = { state: "open", limit: 200, sort: "updated" };

/** One listing per connected server that can list issues, across all of its projects. */
export function ownerQuestionTargets(
  environmentIds: ReadonlyArray<EnvironmentId>,
): ReadonlyArray<IssueListTarget> {
  return environmentIds.map((environmentId) => ({
    environmentId,
    input: OWNER_QUESTION_LIST_INPUT,
  }));
}

/**
 * The open owner questions among the listed issues, newest first. An issue that two projects or two
 * servers both list (the same repository) is kept once, from the first server that listed it.
 */
export function ownerQuestionCandidates(
  answers: ReadonlyArray<readonly [EnvironmentId, IssueListResult]>,
  rule: OwnerQuestionRule,
): ReadonlyArray<EnvironmentIssueEntry> {
  const seen = new Set<string>();
  const candidates: EnvironmentIssueEntry[] = [];
  for (const [environmentId, answer] of answers) {
    for (const entry of answer.entries) {
      if (entry.state.category !== "open" || !isOwnerQuestion(entry, rule)) continue;
      const id = entry.url.toLowerCase();
      if (seen.has(id)) continue;
      seen.add(id);
      candidates.push({ ...entry, environmentId });
    }
  }
  return candidates.toSorted((a, b) => b.updatedAt.localeCompare(a.updatedAt));
}

/**
 * The questions still waiting: a comment answering one (`Owner decision ...`) takes it off.
 * `checking` counts the ones whose comments are not read yet; they are not shown until read.
 */
export function waitingOnYou(
  candidates: ReadonlyArray<EnvironmentIssueEntry>,
  commentsOf: (
    candidate: EnvironmentIssueEntry,
  ) => ReadonlyArray<{ readonly body: string }> | undefined,
  rule: OwnerQuestionRule,
): { readonly waiting: ReadonlyArray<EnvironmentIssueEntry>; readonly checking: number } {
  const waiting: EnvironmentIssueEntry[] = [];
  let checking = 0;
  for (const candidate of candidates) {
    const status = ownerQuestionStatus(candidate, commentsOf(candidate), rule);
    if (status === "waiting") waiting.push(candidate);
    if (status === "unknown") checking += 1;
  }
  return { waiting, checking };
}
