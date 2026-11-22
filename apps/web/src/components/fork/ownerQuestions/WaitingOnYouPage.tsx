/**
 * Fork: "Waiting on you", the open owner questions of every project on every connected
 * server, read-only. Upstream's Issues page files are not changed: this page reads the same issue
 * listing and rows, and the rule in contracts (forkOwnerQuestions.ts) picks the questions.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this page is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { useAtomValue } from "@effect/atom-react";
import type { EnvironmentIssueEntry } from "@t3tools/client-runtime/issue-list";
import { selectEnvironmentQueryData } from "@t3tools/client-runtime/state/runtime";
import { DEFAULT_OWNER_QUESTION_RULE, type IssueComment } from "@t3tools/contracts";
import { AsyncResult, Atom } from "effect/reactivity";
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

import { ownerQuestionCandidates, ownerQuestionTargets, waitingOnYou } from "./waitingOnYou.logic";

const RULE = DEFAULT_OWNER_QUESTION_RULE;

type CommentsByCandidate = ReadonlyArray<ReadonlyArray<IssueComment> | null>;
type CandidateRef = Pick<
  EnvironmentIssueEntry,
  "environmentId" | "projectId" | "tracker" | "host" | "scope" | "key"
>;

// The comments of each candidate, read through the detail query the issue panel shares, in one
// atom per candidate list: React cannot subscribe to a list of atoms whose length changes. A read
// that failed counts as no answer, so the question shows rather than hiding behind the error.
const candidateComments = Atom.family((key: string) =>
  Atom.make((get): CommentsByCandidate =>
    (JSON.parse(key) as ReadonlyArray<CandidateRef>).map((candidate) => {
      const result = get(
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
      );
      const comments = selectEnvironmentQueryData(result)?.comments;
      if (comments !== undefined) return comments;
      return AsyncResult.isFailure(result) && !result.waiting ? [] : null;
    }),
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

export function WaitingOnYouPage() {
  const { environments } = useEnvironments();
  const issuesSupported = useIssuesSupported();
  const openLink = useOpenLink(null);

  const targets = ownerQuestionTargets(
    environments
      .filter((environment) => environment.serverConfig?.environment.capabilities.issues === true)
      .map((environment) => environment.environmentId),
  );
  const list = useIssueList(targets);
  // Refreshing a list still loading would cancel it and start over, losing the scopes already read.
  useLiveRefresh(
    () => {
      if (!list.isPending) list.reload();
    },
    { key: JSON.stringify(targets) },
  );

  const candidates = ownerQuestionCandidates(list.answers, RULE);
  const comments = useAtomValue(candidateComments(candidatesKey(candidates)));
  const commentsOf = new Map(candidates.map((candidate, index) => [candidate, comments[index]]));
  const view = waitingOnYou(
    candidates,
    (candidate) => commentsOf.get(candidate) ?? undefined,
    RULE,
  );
  const environmentLabels = new Map(environments.map((e) => [e.environmentId, e.label] as const));
  const showEnvironment = targets.length > 1;
  const errors = list.answers.flatMap(([, answer]) => answer.errors);

  const open = (entry: IssueRowTarget) => {
    void openLink(entry.url).catch((error: unknown) => console.error(error));
  };

  const noneAnswered = list.answers.length === 0;
  const body: ReactNode = !issuesSupported ? (
    <PullRequestsUnavailableState
      title="Issues unavailable"
      error="Update this environment's T3 Code server to browse issues."
    />
  ) : noneAnswered && list.isPending ? (
    <PullRequestListGhost />
  ) : noneAnswered && list.error ? (
    <PullRequestsUnavailableState
      error={list.error}
      onRetry={list.retry}
      refreshing={list.isPending}
    />
  ) : view.waiting.length === 0 && view.checking === 0 ? (
    <Empty>
      <EmptyMedia variant="icon">
        <CircleCheckIcon />
      </EmptyMedia>
      <EmptyHeader>
        <EmptyTitle>Nothing is waiting on you</EmptyTitle>
        <EmptyDescription>
          No open owner question in any project's repository. An issue asks you when its title
          starts with "Owner question" or "Topic Q1:", or it carries the label {RULE.label}.
        </EmptyDescription>
      </EmptyHeader>
    </Empty>
  ) : (
    <div className="flex flex-col gap-1">
      {view.waiting.map((entry) => (
        <IssueRow
          key={`${entry.environmentId}\u0000${entry.url}`}
          entry={entry}
          selected={false}
          showScope
          showTracker={false}
          environmentLabel={
            showEnvironment ? environmentLabels.get(entry.environmentId) : undefined
          }
          onSelect={open}
        />
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
                onClick={list.refresh}
                disabled={list.isPending}
              >
                <RefreshIcon size="md" refreshing={list.isPending} />
              </Button>
            </div>
            {list.error && !noneAnswered ? (
              <p className="text-xs text-muted-foreground">
                {list.error} Showing the questions that loaded.
              </p>
            ) : null}
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
