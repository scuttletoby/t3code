/**
 * Fork (work panel, fork issue 1136): what the panel shows, from the thread shells the client
 * already holds and the server's `fork.workPanel` answer. A part with no answer says unknown and
 * why; it never shows a guessed count.
 */
import { AsyncResult } from "effect/reactivity";
import type {
  ForkPullRequestRuns,
  ForkRepoQueue,
  ForkWorkPanelAnswer,
  ThreadId,
  ThreadPullRequestLink,
} from "@t3tools/contracts";

/** The shell fields the panel reads. */
interface WorkPanelShell {
  readonly id: ThreadId;
  readonly title: string;
  readonly lineage: { readonly parentThreadId: ThreadId | null };
  readonly pullRequests: ReadonlyArray<ThreadPullRequestLink>;
}

interface WorkPanelPullRequest {
  readonly key: string;
  readonly link: ThreadPullRequestLink;
  /** The thread that links it: this one or one of its sub-agents. */
  readonly threadTitle: string;
  readonly fromChild: boolean;
}

export type PullRequestCi =
  | {
      readonly source: "runs";
      readonly queued: number;
      readonly running: number;
      readonly failed: number;
      readonly passed: number;
      readonly oldestQueuedAt: number | null;
      readonly url: string | null;
    }
  | {
      readonly source: "watch";
      readonly failed: number;
      readonly passed: number;
      readonly pending: boolean;
      /** When the watch first saw the head without any check. */
      readonly waitingSince: number | null;
    }
  | { readonly source: "snapshot"; readonly state: "passing" | "failing" | "pending" }
  | { readonly source: "unknown" };

const repoKey = (host: string, repository: string) =>
  `${host.toLowerCase()}\0${repository.toLowerCase()}`;
const prKey = (link: Pick<ThreadPullRequestLink, "host" | "repository" | "number">) =>
  `${repoKey(link.host, link.repository)}#${link.number}`;

/** The pull requests linked to `threadId` or to any thread under it, each once, its own first. */
export function familyPullRequests(
  threadId: ThreadId,
  shells: ReadonlyArray<WorkPanelShell>,
): ReadonlyArray<WorkPanelPullRequest> {
  const children = new Map<ThreadId, Array<WorkPanelShell>>();
  for (const shell of shells) {
    const parent = shell.lineage.parentThreadId;
    if (parent === null) continue;
    children.set(parent, [...(children.get(parent) ?? []), shell]);
  }
  const root = shells.find((shell) => shell.id === threadId);
  if (root === undefined) return [];
  const seen = new Set<ThreadId>();
  const order: Array<WorkPanelShell> = [];
  const visit = (shell: WorkPanelShell) => {
    if (seen.has(shell.id)) return;
    seen.add(shell.id);
    order.push(shell);
    for (const child of children.get(shell.id) ?? []) visit(child);
  };
  visit(root);
  const byKey = new Map<string, WorkPanelPullRequest>();
  for (const shell of order) {
    for (const link of shell.pullRequests) {
      if (link.source === "stack-dismissed") continue;
      const key = prKey(link);
      if (byKey.has(key)) continue;
      byKey.set(key, { key, link, threadTitle: shell.title, fromChild: shell.id !== threadId });
    }
  }
  return [...byKey.values()];
}

/** One pull request's CI: the shared runs read first, then what the PR watcher saw, then the snapshot. */
export function pullRequestCi(
  link: ThreadPullRequestLink,
  runs: ForkPullRequestRuns | undefined,
): PullRequestCi {
  if (runs !== undefined) {
    return {
      source: "runs",
      queued: runs.queued,
      running: runs.running,
      failed: runs.failed,
      passed: runs.passed,
      oldestQueuedAt: runs.oldestQueuedAt,
      url: runs.url,
    };
  }
  const watch = link.watch;
  if (watch !== undefined) {
    const since = watch.checksAwaitedSince == null ? null : Date.parse(watch.checksAwaitedSince);
    return {
      source: "watch",
      failed: watch.failedChecks.length,
      passed: watch.passedChecks.length,
      pending: watch.checksPending ?? watch.checksAwaitedSince != null,
      waitingSince: since !== null && Number.isFinite(since) ? since : null,
    };
  }
  const state = link.snapshot?.checksState;
  return state == null ? { source: "unknown" } : { source: "snapshot", state };
}

export function runsByPullRequest(
  answer: ForkWorkPanelAnswer | null,
): ReadonlyMap<string, ForkPullRequestRuns> {
  return new Map((answer?.pullRequests ?? []).map((runs) => [prKey(runs), runs]));
}

/**
 * One queue line per repository the pull requests are in. A repository the server did not answer
 * for is unknown with `missingReason`.
 */
export function repoQueueLines(
  pullRequests: ReadonlyArray<WorkPanelPullRequest>,
  answer: ForkWorkPanelAnswer | null,
  missingReason: string,
): ReadonlyArray<ForkRepoQueue> {
  const answered = new Map(
    (answer?.repoQueues ?? []).map((line) => [repoKey(line.host, line.repository), line]),
  );
  const lines = new Map<string, ForkRepoQueue>();
  for (const { link } of pullRequests) {
    const key = repoKey(link.host, link.repository);
    if (lines.has(key)) continue;
    lines.set(
      key,
      answered.get(key) ?? {
        host: link.host,
        repository: link.repository,
        state: "unknown",
        reason: missingReason,
      },
    );
  }
  return [...lines.values()];
}

/** "12 min", "2 h 5 min", "less than a minute". */
export function formatAge(ms: number): string {
  const minutes = Math.floor(Math.max(0, ms) / 60_000);
  if (minutes < 1) return "less than a minute";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`;
}

const joinParts = (parts: ReadonlyArray<string | null>) =>
  parts.filter((part): part is string => part !== null).join(" · ");

export function pullRequestCiText(ci: PullRequestCi, now: number): string {
  switch (ci.source) {
    case "runs":
      if (ci.queued + ci.running + ci.failed + ci.passed === 0)
        return "no runs counted on its head";
      return joinParts([
        ci.queued > 0
          ? `${ci.queued} queued${ci.oldestQueuedAt === null ? "" : ` (${formatAge(now - ci.oldestQueuedAt)})`}`
          : null,
        ci.running > 0 ? `${ci.running} running` : null,
        ci.failed > 0 ? `${ci.failed} failed` : null,
        ci.passed > 0 ? `${ci.passed} passed` : null,
      ]);
    case "watch":
      return (
        joinParts([
          ci.pending
            ? ci.waitingSince === null
              ? "checks pending"
              : `waiting for checks (${formatAge(now - ci.waitingSince)})`
            : null,
          ci.failed > 0 ? `${ci.failed} failed` : null,
          ci.passed > 0 ? `${ci.passed} passed` : null,
        ]) || "no checks seen yet"
      );
    case "snapshot":
      return `checks ${ci.state}`;
    case "unknown":
      return "unknown";
  }
}

export const CI_SOURCE_TEXT: Record<PullRequestCi["source"], string> = {
  runs: "From the repository's runs, the server's shared read.",
  watch: "From the pull request watch: queued and running are not told apart.",
  snapshot: "From the pull request's last sync.",
  unknown: "Neither the runs read, the watch nor a sync has an answer for this pull request.",
};

export function repoQueueText(line: ForkRepoQueue, now: number): string {
  if (line.state === "unknown" || line.queued === undefined || line.running === undefined) {
    return `unknown: ${line.reason ?? "no answer"}`;
  }
  const oldest =
    line.oldestQueuedAt == null ? null : `oldest queued ${formatAge(now - line.oldestQueuedAt)}`;
  return joinParts([`${line.queued} queued`, `${line.running} running`, oldest]);
}

export function repoQueueSourceText(line: ForkRepoQueue, now: number): string {
  const read =
    line.readAt === undefined
      ? "GitHub has not answered this server yet."
      : `GitHub answered ${formatAge(now - line.readAt)} ago, over the latest ${line.scanned ?? 0} runs.`;
  const next =
    line.nextReadAt === undefined || line.nextReadAt <= now
      ? ""
      : ` Next read in ${formatAge(line.nextReadAt - now)}.`;
  const reason = line.state === "answered" && line.reason !== undefined ? ` ${line.reason}` : "";
  return `${read}${reason}${next}`;
}

/** An answer received longer ago than three refreshes (a minute each) is not current. */
const ANSWER_STALE_MS = 3 * 60_000;

export const REASON_SERVER_UNANSWERED =
  "This machine did not answer the work panel's read (it may be offline or run an older version).";
const REASON_SERVER_PENDING = "Waiting for this machine's first answer.";

/**
 * A machine's answer as of `clientNow`: none after a failed refresh (`getOrElse` keeps the old
 * one) or one received over three refreshes ago. `serverNow` ages the answer time by the client
 * clock since it arrived, so ages keep counting whatever either clock's skew.
 */
export function currentAnswer(
  result: AsyncResult.AsyncResult<ForkWorkPanelAnswer, unknown>,
  clientNow: number,
):
  | { readonly answer: ForkWorkPanelAnswer; readonly serverNow: number }
  | { readonly answer: null; readonly reason: string } {
  if (result._tag === "Failure") return { answer: null, reason: REASON_SERVER_UNANSWERED };
  if (result._tag !== "Success") return { answer: null, reason: REASON_SERVER_PENDING };
  const age = clientNow - result.timestamp;
  if (age > ANSWER_STALE_MS) {
    return {
      answer: null,
      reason: `This machine's last answer is ${formatAge(age)} old; it has not answered since.`,
    };
  }
  return { answer: result.value, serverNow: result.value.answeredAt + Math.max(0, age) };
}
