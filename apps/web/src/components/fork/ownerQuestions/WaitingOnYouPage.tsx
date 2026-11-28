/**
 * Fork: "Waiting on you", the open owner questions of every project on every connected
 * server, each with its recommendation and an answer box (OwnerQuestionAnswer). Upstream's Issues
 * page files are not changed: this page reads the same issue listing and rows, and the rule in
 * contracts (forkOwnerQuestions.ts) picks the questions.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this page is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { useAtomValue } from "@effect/atom-react";
import type { EnvironmentIssueEntry } from "@t3tools/client-runtime/issue-list";
import { DEFAULT_OWNER_QUESTION_RULE, type IssueComment } from "@t3tools/contracts";
import { Atom } from "effect/reactivity";
import { CircleCheckIcon } from "lucide-react";
import type { ReactNode } from "react";

import { useOpenLink } from "~/browser/useOpenLink";
import { IssueRow, type IssueRowTarget } from "~/components/issue/IssueRow";
import { PullRequestListGhost } from "~/components/pullRequest/PullRequestGhosts";
import { PullRequestsUnavailableState } from "~/components/pullRequest/PullRequestsUnavailableState";
import { RefreshIcon } from "~/components/ui/refresh-icon";
import { WorkspaceBreadcrumb, WorkspaceBreadcrumbItem } from "~/components/WorkspaceBreadcrumb";
import { WorkspacePageContainer } from "~/components/WorkspacePageContainer";
import { WorkspacePageHeader } from "~/components/WorkspacePageHeader";
import { Button } from "~/components/ui/button";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "~/components/ui/empty";
import { SidebarInset } from "~/components/ui/sidebar";
import { isElectron } from "~/env";
import { useLiveRefresh } from "~/hooks/useLiveRefresh";
import { useEnvironments, useIssuesSupported } from "~/state/environments";
import { issueEnvironment, useIssueList } from "~/state/issues";

import { OwnerQuestionAnswer } from "./OwnerQuestionAnswer";

import {
  commentsOfDetail,
  type CutScope,
  listingGaps,
  OWNER_QUESTION_LIST_LIMIT,
  ownerQuestionCandidates,
  ownerQuestionRuleText,
  ownerQuestionTargets,
  readInFull,
  waitingOnYou,
} from "./waitingOnYou.logic";

const RULE = DEFAULT_OWNER_QUESTION_RULE;

type CommentsByCandidate = ReadonlyArray<ReadonlyArray<IssueComment> | null>;
type CandidateRef = Pick<
  EnvironmentIssueEntry,
  "environmentId" | "projectId" | "tracker" | "host" | "scope" | "key"
>;

// The comments of each candidate, read through the detail query the issue panel shares, in one
// atom per candidate list: React cannot subscribe to a list of atoms whose length changes. A read
// that failed counts as no answer (commentsOfDetail), so the question shows.
const candidateComments = Atom.family((key: string) =>
  Atom.make((get): CommentsByCandidate =>
    (JSON.parse(key) as ReadonlyArray<CandidateRef>).map((candidate) =>
      commentsOfDetail(
        get(
          issueEnvironment.detail({
            environmentId: candidate.environmentId,
            input: {
              projectId: candidate.projectId,
              tracker: candidate.tracker,
              host: candidate.host,
              scope: candidate.scope,
              key: candidate.key,
            },
          }),
        ),
      ),
    ),
  ).pipe(Atom.withLabel(`fork:owner-questions:comments:${key}`)),
);

function candidatesKey(candidates: ReadonlyArray<EnvironmentIssueEntry>): string {
  return JSON.stringify(
    candidates.map(({ environmentId, projectId, tracker, host, scope, key }): CandidateRef => ({
      environmentId,
      projectId,
      tracker,
      host,
      scope,
      key,
    })),
  );
}

function uniqueErrors<E extends { readonly projectId: string; readonly message: string }>(
  errors: ReadonlyArray<E>,
): ReadonlyArray<E> {
  const seen = new Set<string>();
  return errors.filter((error) => {
    const id = `${error.projectId}\u0000${error.message}`;
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

/** Says which repository was cut and what that may hide. */
function cutText(cut: CutScope): string {
  const where = cut.host === "" ? cut.scope : `${cut.scope} (${cut.host})`;
  return cut.labelled
    ? `${where}: only the ${OWNER_QUESTION_LIST_LIMIT} most recently updated open issues labelled ${RULE.label} were read; an older labelled question there is not listed.`
    : `${where}: only the ${OWNER_QUESTION_LIST_LIMIT} most recently updated open issues were read; an older question there is listed only if it carries the label ${RULE.label}.`;
}

export function WaitingOnYouPage() {
  const { environments } = useEnvironments();
  const issuesSupported = useIssuesSupported();
  const openLink = useOpenLink(null);

  const targets = ownerQuestionTargets(
    environments
      .filter((environment) => environment.serverConfig?.environment.capabilities.issues === true)
      .map((environment) => environment.environmentId),
    RULE,
  );
  const allList = useIssueList(targets.all);
  const labelledList = useIssueList(targets.labelled);
  const isPending = allList.isPending || labelledList.isPending;
  const listError = allList.error ?? labelledList.error;
  // Refreshing a list still loading would cancel it and start over, losing the scopes already read.
  useLiveRefresh(
    () => {
      if (!allList.isPending) allList.reload();
      if (!labelledList.isPending) labelledList.reload();
    },
    { key: JSON.stringify(targets) },
  );
  const refresh = () => {
    allList.refresh();
    labelledList.refresh();
  };
  const retry = () => {
    allList.retry();
    labelledList.retry();
  };

  const answers = [...allList.answers, ...labelledList.answers];
  const gaps = listingGaps({
    all: { targets: targets.all, answers: allList.answers, error: allList.error },
    labelled: {
      targets: targets.labelled,
      answers: labelledList.answers,
      error: labelledList.error,
    },
  });
  const complete = readInFull(gaps);
  const candidates = ownerQuestionCandidates(answers, RULE);
  const comments = useAtomValue(candidateComments(candidatesKey(candidates)));
  const commentsOf = new Map(candidates.map((candidate, index) => [candidate, comments[index]]));
  const view = waitingOnYou(
    candidates,
    (candidate) => commentsOf.get(candidate) ?? undefined,
    RULE,
  );
  const environmentLabels = new Map(environments.map((e) => [e.environmentId, e.label] as const));
  const showEnvironment = targets.all.length > 1;
  // Both listings read the same projects, so a project that failed fails in both.
  const errors = uniqueErrors(answers.flatMap(([, answer]) => answer.errors));

  const open = (entry: IssueRowTarget) => {
    void openLink(entry.url).catch((error: unknown) => console.error(error));
  };

  const noneAnswered = answers.length === 0;
  const ruleText = ownerQuestionRuleText(RULE);
  const body: ReactNode = !issuesSupported ? (
    <PullRequestsUnavailableState
      title="Issues unavailable"
      error="Update this environment's T3 Code server to browse issues."
    />
  ) : noneAnswered && isPending ? (
    <PullRequestListGhost />
  ) : noneAnswered && listError ? (
    <PullRequestsUnavailableState error={listError} onRetry={retry} refreshing={isPending} />
  ) : view.waiting.length === 0 && view.checking === 0 ? (
    complete ? (
      <Empty>
        <EmptyMedia variant="icon">
          <CircleCheckIcon />
        </EmptyMedia>
        <EmptyHeader>
          <EmptyTitle>Nothing is waiting on you</EmptyTitle>
          <EmptyDescription>
            No open owner question in any project's repository. {ruleText}
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    ) : (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>No owner question found in what was read</EmptyTitle>
          <EmptyDescription>
            Not every repository was read in full (see above), so a question may still be waiting.{" "}
            {ruleText}
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  ) : (
    <div className="flex flex-col gap-1">
      {view.waiting.map((entry) => (
        <div key={`${entry.environmentId}\u0000${entry.url}`} className="flex flex-col">
          <IssueRow
            entry={entry}
            selected={false}
            showScope
            showTracker={false}
            environmentLabel={
              showEnvironment ? environmentLabels.get(entry.environmentId) : undefined
            }
            onSelect={open}
          />
          <OwnerQuestionAnswer entry={entry} rule={RULE} />
        </div>
      ))}
      {view.checking > 0 ? (
        <p className="px-3 py-2 text-xs text-muted-foreground">
          Checking {view.checking} more for an answer…
        </p>
      ) : null}
    </div>
  );

  return (
    <SidebarInset className="h-dvh min-h-0 overflow-hidden overscroll-y-none">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-x-hidden bg-background">
        <WorkspacePageHeader electron={isElectron} className="relative bg-background">
          <WorkspaceBreadcrumb ariaLabel="Waiting on you breadcrumb">
            <WorkspaceBreadcrumbItem current>
              <h1 className="truncate">Waiting on you</h1>
            </WorkspaceBreadcrumbItem>
          </WorkspaceBreadcrumb>
          <div className="min-w-0 flex-1" />
        </WorkspacePageHeader>
        <div className="topbar-scroll-fade scrollbar-gutter-both min-h-0 flex-1 overflow-y-auto">
          <WorkspacePageContainer width="expanded" className="min-h-full gap-4">
            <div className="flex items-center gap-2">
              <p className="min-w-0 flex-1 text-sm text-muted-foreground">
                {view.waiting.length === 1
                  ? "1 open owner question across your projects."
                  : `${view.waiting.length} open owner questions across your projects.`}
              </p>
              <Button
                size="icon"
                variant="outline"
                aria-label="Refresh owner questions"
                onClick={refresh}
                disabled={isPending}
              >
                <RefreshIcon size="md" refreshing={isPending} />
              </Button>
            </div>
            {listError && !noneAnswered ? (
              <p className="text-xs text-muted-foreground">
                {listError} Showing the questions that loaded.
              </p>
            ) : null}
            {gaps.unread.length > 0 && isPending && !noneAnswered ? (
              <p className="text-xs text-muted-foreground">
                Still reading{" "}
                {gaps.unread.map((id) => environmentLabels.get(id) ?? "a server").join(", ")}…
              </p>
            ) : null}
            {gaps.cut.map((cut) => (
              <p
                key={`${cut.labelled ? "labelled" : "all"}\u0000${cut.host}\u0000${cut.scope}`}
                className="text-xs text-muted-foreground"
              >
                {cutText(cut)}
              </p>
            ))}
            {errors.map((error) => (
              <p
                key={`${error.projectId}\u0000${error.message}`}
                className="text-xs text-muted-foreground"
              >
                {error.projectTitle}: {error.message}
              </p>
            ))}
            {body}
          </WorkspacePageContainer>
        </div>
      </div>
    </SidebarInset>
  );
}
