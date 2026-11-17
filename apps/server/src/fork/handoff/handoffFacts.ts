/**
 * Fork: the handoff facts read from a thread's records. Pure, and the one source of "quiet" for both
 * the handoff service (before it sends) and the busy check in `Orchestrator.ts` (under the thread
 * lock, when the message lands), so the two can never disagree.
 */
import type {
  OrchestrationV2Run,
  OrchestrationV2ThreadProjection,
  ThreadId,
} from "@t3tools/contracts";
import { derivePendingBackgroundWork } from "@t3tools/shared/orchestrationV2PendingBackgroundWork";

import { isActiveRun } from "../../orchestration-v2/CommandPolicy.ts";
import type { HandoffBlocker, HandoffFacts } from "./handoffDecision.ts";

export type HandoffRecords = Pick<
  OrchestrationV2ThreadProjection,
  | "thread"
  | "runs"
  | "messages"
  | "providerThreads"
  | "providerTurns"
  | "runtimeRequests"
  | "turnItems"
  | "contextTransfers"
>;

/** A handoff message's id: `handoff:<thread>:<run>`, and `:retry1` for its one retry. */
const HANDOFF_MESSAGE_PREFIX = "handoff:";
export const HANDOFF_RETRY_SUFFIX = ":retry1";

/** The source run a handoff message names, and whether it is the retry; null for any other id. */
export function parseHandoffMessageId(
  threadId: ThreadId,
  messageId: string,
): { readonly runId: string; readonly retry: boolean } | null {
  const prefix = `${HANDOFF_MESSAGE_PREFIX}${threadId}:`;
  if (!messageId.startsWith(prefix)) return null;
  const rest = messageId.slice(prefix.length);
  const retry = rest.endsWith(HANDOFF_RETRY_SUFFIX);
  const runId = retry ? rest.slice(0, -HANDOFF_RETRY_SUFFIX.length) : rest;
  return runId === "" ? null : { runId, retry };
}

/** The newest run that is part of the thread (a removed queued message leaves a cancelled one). */
function latestStartedRun(runs: ReadonlyArray<OrchestrationV2Run>): OrchestrationV2Run | undefined {
  return runs
    .filter((run) => run.status !== "cancelled" || run.startedAt !== null)
    .reduce<OrchestrationV2Run | undefined>(
      (latest, run) => (latest === undefined || run.ordinal > latest.ordinal ? run : latest),
      undefined,
    );
}

/** What keeps the thread from being quiet (design 4.2). Running T3 children never block. */
export function handoffBlockers(
  records: Omit<HandoffRecords, "providerTurns">,
): ReadonlyArray<HandoffBlocker> {
  const { thread, runs, messages } = records;
  const blockers: Array<HandoffBlocker> = [];
  if (runs.some((run) => run.status === "queued" || isActiveRun(run))) {
    blockers.push({ kind: "run-active" });
  }
  if (records.runtimeRequests.some((request) => request.status === "pending")) {
    blockers.push({ kind: "request-pending" });
  }
  // As upstream's `/compact` check: a delivered child result whose message has not landed yet.
  if (
    runs.some(
      (run) =>
        run.delegatedCompletion?.disposition === "open" &&
        run.delegatedCompletion.delivery !== null &&
        !messages.some((message) => message.id === run.delegatedCompletion?.delivery?.messageId),
    )
  ) {
    blockers.push({ kind: "child-result-waiting" });
  }
  if (records.contextTransfers.some((transfer) => transfer.status === "pending")) {
    blockers.push({ kind: "fork-or-merge" });
  }
  // The session's own work only; a PR watch is a T3 wait on the thread, so it is not passed.
  const background = derivePendingBackgroundWork({
    latestRun: latestStartedRun(runs) ?? null,
    runs,
    providerThreads: records.providerThreads,
    turnItems: records.turnItems,
    activeProviderThreadId: thread.activeProviderThreadId,
  });
  if (background.length > 0) {
    blockers.push({
      kind: "background-work",
      detail: background.map((task) => task.description ?? task.taskId).join(", "),
    });
  }
  const active = records.providerThreads.find((p) => p.id === thread.activeProviderThreadId);
  if (active?.goal?.status === "active") blockers.push({ kind: "goal-active" });
  return blockers;
}

/** The decision's facts, less the settings and the trigger, which the service adds. */
export function handoffThreadFacts(
  records: HandoffRecords,
): Pick<
  HandoffFacts,
  "threadAvailable" | "lastRun" | "usedTokens" | "cacheExpiresAtMs" | "blockers"
> {
  const { thread } = records;
  const run = latestStartedRun(records.runs);
  const text = records.messages.find((message) => message.id === run?.userMessageId)?.text;
  const usage = records.providerTurns
    .filter((turn) => turn.providerThreadId === thread.activeProviderThreadId)
    .flatMap((turn) => (turn.tokenUsage === undefined ? [] : [turn.tokenUsage]))
    .toSorted((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt))[0];
  return {
    threadAvailable: thread.deletedAt === null && thread.archivedAt === null,
    lastRun:
      run === undefined
        ? undefined
        : {
            id: run.id,
            kind: run.userMessageId.startsWith(HANDOFF_MESSAGE_PREFIX)
              ? "handoff"
              : text?.trim().toLowerCase() === "/compact"
                ? "compact"
                : "turn",
            state:
              run.status === "completed" || run.status === "failed" ? run.status : "interrupted",
          },
    usedTokens: usage?.usedTokens,
    cacheExpiresAtMs:
      usage?.promptCacheExpiresAt === undefined
        ? undefined
        : Date.parse(usage.promptCacheExpiresAt),
    blockers: handoffBlockers(records),
  };
}

/**
 * The busy check: true drops a handoff message that lands at a thread no longer quiet, or no
 * longer at the run it was made from (a newer turn ran first). Only the message landing decides,
 * under the thread lock, so a racing user message or child result always wins. Any other message
 * passes untouched; a forged handoff id can only be dropped, never let through.
 */
export function handoffDispatchDropped(
  command: { readonly threadId: ThreadId; readonly messageId: string },
  projection: Omit<HandoffRecords, "providerTurns">,
): boolean {
  const handoff = parseHandoffMessageId(command.threadId, command.messageId);
  if (handoff === null) return false;
  if (handoffBlockers(projection).length > 0) return true;
  const latest = latestStartedRun(projection.runs);
  // The retry follows only its own first send, failed or interrupted.
  if (handoff.retry) {
    return (
      latest?.userMessageId !== `${HANDOFF_MESSAGE_PREFIX}${command.threadId}:${handoff.runId}` ||
      latest.status === "completed"
    );
  }
  return latest?.id !== handoff.runId;
}
