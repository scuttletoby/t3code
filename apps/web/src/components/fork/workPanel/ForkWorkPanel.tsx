/**
 * Fork (work panel, fork issue 1136): capacity and CI for this thread in its side panel, right
 * after its sub-agents: the capacity bar and usage (CapacityBar.tsx), one repository queue line,
 * then each pull request linked to this thread or a thread under it. Shows nothing while the client fork setting `workPanelEnabled` is off (the default).
 * Upstream's ThreadDetailsPanel.tsx renders it with one seam line, passing its own props.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { useAtomValue } from "@effect/atom-react";
import { scopeThreadRef } from "@t3tools/client-runtime/environment";
import type { EnvironmentId, ThreadId } from "@t3tools/contracts";
import type { MouseEvent as ReactMouseEvent } from "react";

import { useOpenLink } from "../../../browser/useOpenLink";
import { useNowMinute } from "../../../hooks/useNowMinute";
import { useClientSettings } from "../../../hooks/useSettings";
import { cn } from "../../../lib/utils";
import { useThreadShells } from "../../../state/entities";
import { ThreadDetailsSection } from "../../chat/ThreadDetailsSection";
import { Tooltip, TooltipPopup, TooltipTrigger } from "../../ui/tooltip";
import {
  CI_SOURCE_TEXT,
  currentAnswer,
  familyPullRequests,
  pullRequestCi,
  pullRequestCiText,
  repoQueueLines,
  repoQueueSourceText,
  repoQueueText,
  runsByPullRequest,
  type PullRequestCi,
} from "./workPanel.logic";
import { CapacityBar } from "./CapacityBar";
import { forkWorkPanelAtom } from "./workPanelAtom";

const REASON_SERVER_SKIPPED = "This machine did not read this repository.";

interface ForkWorkPanelProps {
  readonly environmentId: EnvironmentId;
  readonly threadId: ThreadId;
  readonly draftId?: unknown;
  /** The side panel's density: the work panel shows only in `full`. */
  readonly density: string;
}

export function ForkWorkPanel(props: ForkWorkPanelProps) {
  const enabled = useClientSettings((settings) => settings.workPanelEnabled);
  if (!enabled || props.density !== "full" || props.draftId) return null;
  return <WorkPanelBody environmentId={props.environmentId} threadId={props.threadId} />;
}

function WorkPanelBody(props: {
  readonly environmentId: EnvironmentId;
  readonly threadId: ThreadId;
}) {
  const shells = useThreadShells();
  const result = useAtomValue(
    forkWorkPanelAtom({ environmentId: props.environmentId, input: { threadId: props.threadId } }),
  );
  const openLink = useOpenLink(scopeThreadRef(props.environmentId, props.threadId));
  // The minute tick is UTC, truncated to the minute.
  const clientNow = Date.parse(`${useNowMinute()}:00Z`);
  const current = currentAnswer(result, clientNow);
  const answer = current.answer;
  const missingReason = current.answer !== null ? REASON_SERVER_SKIPPED : current.reason;
  const pullRequests = familyPullRequests(
    props.threadId,
    shells.filter((shell) => shell.environmentId === props.environmentId),
  );
  const lines = repoQueueLines(pullRequests, answer, missingReason);
  const runs = runsByPullRequest(answer);
  const serverNow = current.answer !== null ? current.serverNow : clientNow;
  const open = (url: string) => (event: ReactMouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    void openLink(url, { event });
  };

  return (
    <ThreadDetailsSection headingId="fork-work-panel-heading" title="Work" data-fork-work-panel>
      <div className="flex flex-col gap-1.5 px-2.5 text-xs">
        <CapacityBar environmentId={props.environmentId} repoQueues={lines} />
        <p className="text-2xs font-medium text-muted-foreground">CI</p>
        {pullRequests.length === 0 ? (
          <p className="text-muted-foreground">
            No pull requests are linked to this thread or its sub-agents.
          </p>
        ) : (
          <>
            {lines.map((line) => (
              <div
                key={`${line.host}/${line.repository}`}
                data-testid="fork-work-panel-repo-queue"
                data-state={line.state}
                className={cn(
                  "flex flex-col",
                  line.state === "unknown" && "text-muted-foreground/70",
                )}
              >
                <p>
                  <span className="text-muted-foreground">Queue in {line.repository}: </span>
                  {repoQueueText(line, serverNow)}
                  {line.state === "answered" && line.oldestQueuedUrl ? (
                    <>
                      {" "}
                      <a
                        href={line.oldestQueuedUrl}
                        onClick={open(line.oldestQueuedUrl)}
                        className="underline underline-offset-2"
                      >
                        oldest run
                      </a>
                    </>
                  ) : null}
                </p>
                <p className="text-2xs text-muted-foreground">
                  {repoQueueSourceText(line, serverNow)}
                </p>
              </div>
            ))}
            <ul className="flex flex-col gap-1" aria-label="CI of linked pull requests">
              {pullRequests.map(({ key, link, threadTitle, fromChild }) => {
                const ci = pullRequestCi(link, runs.get(key));
                const now = ci.source === "runs" ? serverNow : clientNow;
                const runUrl = ci.source === "runs" ? ci.url : null;
                return (
                  <li
                    key={key}
                    data-testid="fork-work-panel-pr"
                    data-source={ci.source}
                    className={cn("flex min-w-0 flex-col", ci.source === "unknown" && "opacity-70")}
                  >
                    <span className="flex min-w-0 items-center gap-1.5">
                      <CiDot ci={ci} />
                      <a
                        href={link.url}
                        onClick={open(link.url)}
                        className="min-w-0 truncate font-medium text-foreground"
                      >
                        #{link.number}
                        {link.snapshot ? ` ${link.snapshot.title}` : ""}
                      </a>
                    </span>
                    <span className="ps-3.5 text-muted-foreground">
                      <Tooltip>
                        <TooltipTrigger delay={200} render={<span />}>
                          {pullRequestCiText(ci, now)}
                        </TooltipTrigger>
                        <TooltipPopup side="left">{CI_SOURCE_TEXT[ci.source]}</TooltipPopup>
                      </Tooltip>
                      {runUrl ? (
                        <>
                          {" · "}
                          <a
                            href={runUrl}
                            onClick={open(runUrl)}
                            className="underline underline-offset-2"
                          >
                            run
                          </a>
                        </>
                      ) : null}
                      {fromChild ? ` · from ${threadTitle}` : ""}
                    </span>
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>
    </ThreadDetailsSection>
  );
}

function CiDot({ ci }: { readonly ci: PullRequestCi }) {
  const tone =
    ci.source === "runs"
      ? ci.failed > 0
        ? "failed"
        : ci.queued + ci.running > 0
          ? "busy"
          : ci.passed > 0
            ? "passed"
            : "none"
      : ci.source === "watch"
        ? ci.failed > 0
          ? "failed"
          : ci.pending
            ? "busy"
            : ci.passed > 0
              ? "passed"
              : "none"
        : ci.source === "snapshot"
          ? ci.state === "failing"
            ? "failed"
            : ci.state === "pending"
              ? "busy"
              : "passed"
          : "none";
  return (
    <span
      aria-hidden
      className={cn(
        "size-2 shrink-0 rounded-full",
        tone === "failed" && "bg-destructive",
        tone === "busy" && "bg-warning",
        tone === "passed" && "bg-success",
        tone === "none" && "bg-muted-foreground/40",
      )}
    />
  );
}
