/**
 * Fork: what the "Waiting on you" page lists. It reads upstream's issue listing (the
 * repositories T3 already lists for each project, none named here) and keeps the issues the
 * owner-question rule in contracts (forkOwnerQuestions.ts) says wait on the owner.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the help text of the fork's own page, English only like the page; upstream's translation catalogs hold no fork keys. */
import type { EnvironmentIssueEntry } from "@t3tools/client-runtime/issue-list";
import { selectEnvironmentQueryData } from "@t3tools/client-runtime/state/runtime";
import {
  type EnvironmentId,
  type IssueListInput,
  type IssueListResult,
  isOwnerQuestion,
  type OwnerQuestionRule,
  ownerQuestionStatus,
  ownerQuestionTitleForms,
} from "@t3tools/contracts";
import { AsyncResult } from "effect/reactivity";

import type { IssueListTarget } from "~/state/issues";

/** Rows per repository a listing reads, the most the tracker API gives in one page. */
export const OWNER_QUESTION_LIST_LIMIT = 200;

/** Every open issue, newest first; the rule picks the questions, as titles cannot be searched. */
const OWNER_QUESTION_LIST_INPUT: IssueListInput = {
  state: "open",
  limit: OWNER_QUESTION_LIST_LIMIT,
  sort: "updated",
};

/**
 * Two listings per connected server that can list issues, across all of its projects: every open
 * issue (`all`, which finds the questions asked by title), and the open issues carrying the rule's
 * label (`labelled`), so a labelled question is found even in a repository with more open issues
 * than one listing reads.
 */
export function ownerQuestionTargets(
  environmentIds: ReadonlyArray<EnvironmentId>,
  rule: OwnerQuestionRule,
): {
  readonly all: ReadonlyArray<IssueListTarget>;
  readonly labelled: ReadonlyArray<IssueListTarget>;
} {
  const labelledInput: IssueListInput = { ...OWNER_QUESTION_LIST_INPUT, labels: [rule.label] };
  return {
    all: environmentIds.map((environmentId) => ({
      environmentId,
      input: OWNER_QUESTION_LIST_INPUT,
    })),
    labelled: environmentIds.map((environmentId) => ({ environmentId, input: labelledInput })),
  };
}

/** One listing as the page holds it: what it asked, what answered, and the first server error. */
export interface OwnerQuestionListing {
  readonly targets: ReadonlyArray<IssueListTarget>;
  readonly answers: ReadonlyArray<readonly [EnvironmentId, IssueListResult]>;
  readonly error: string | null;
}

/** A repository whose listing stopped at the limit; `labelled` when it was the labelled one. */
export interface CutScope {
  readonly host: string;
  readonly scope: string;
  readonly labelled: boolean;
}

/**
 * What the listings did not read: servers with no answer yet, a server or project that failed, and
 * repositories cut at the limit (a listing names each one with more rows in `nextCursors`).
 */
export interface ListingGaps {
  readonly unread: ReadonlyArray<EnvironmentId>;
  readonly failed: boolean;
  readonly cut: ReadonlyArray<CutScope>;
}

export function listingGaps(listings: {
  readonly all: OwnerQuestionListing;
  readonly labelled: OwnerQuestionListing;
}): ListingGaps {
  const unread = new Set<EnvironmentId>();
  let failed = false;
  const cut: CutScope[] = [];
  const seenCuts = new Set<string>();
  for (const [which, listing] of [
    ["all", listings.all],
    ["labelled", listings.labelled],
  ] as const) {
    const answered = new Set(listing.answers.map(([environmentId]) => environmentId));
    for (const target of listing.targets) {
      if (!answered.has(target.environmentId)) unread.add(target.environmentId);
    }
    if (listing.error !== null) failed = true;
    for (const [, answer] of listing.answers) {
      if (answer.errors.length > 0) failed = true;
      // A cursor's key lowercases the scope; the listing's scopes keep the name as it is written.
      const named = new Map(
        (answer.scopes ?? []).flatMap((ref) =>
          ref.host === undefined
            ? []
            : [[`${ref.host} ${ref.scope}`.toLowerCase(), { host: ref.host, scope: ref.scope }]],
        ),
      );
      for (const key of Object.keys(answer.nextCursors)) {
        // Keyed `"<host> <scope>"`.
        const space = key.indexOf(" ");
        const ref = named.get(key.toLowerCase());
        const host = ref?.host ?? (space < 0 ? "" : key.slice(0, space));
        const scope = ref?.scope ?? key.slice(space + 1);
        const id = `${which}\u0000${key.toLowerCase()}`;
        if (seenCuts.has(id)) continue;
        seenCuts.add(id);
        cut.push({ host, scope, labelled: which === "labelled" });
      }
    }
  }
  return { unread: [...unread], failed, cut };
}

/** True when every server answered every listing in full: only then can nothing be waiting. */
export function readInFull(gaps: ListingGaps): boolean {
  return gaps.unread.length === 0 && !gaps.failed && gaps.cut.length === 0;
}

/**
 * The comments of one question from its detail read. `null` while the first read runs. A read that
 * failed counts as no answer (`[]`), so the question shows rather than hiding behind the error.
 */
export function commentsOfDetail<C>(
  result: AsyncResult.AsyncResult<{ readonly comments: ReadonlyArray<C> }, unknown>,
): ReadonlyArray<C> | null {
  const comments = selectEnvironmentQueryData(result)?.comments;
  if (comments !== undefined) return comments;
  return AsyncResult.isFailure(result) && !result.waiting ? [] : null;
}

/** The page's help text: which issues ask the owner, worded from the rule itself. */
export function ownerQuestionRuleText(rule: OwnerQuestionRule): string {
  const forms = ownerQuestionTitleForms(rule).map((form) => `"${form}"`);
  const titles =
    forms.length <= 1
      ? (forms[0] ?? "")
      : `${forms.slice(0, -1).join(", ")} or ${forms[forms.length - 1]}`;
  return titles === ""
    ? `An issue asks you when it carries the label ${rule.label}.`
    : `An issue asks you when its title starts with ${titles}, or it carries the label ${rule.label}.`;
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
