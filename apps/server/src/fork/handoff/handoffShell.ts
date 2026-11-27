/**
 * Fork: a completed quiet handoff run, or the `agent` source's write turn before it (completed or
 * failed), shows in the thread shell as upstream's stopped run (`latestRunStopRequested`), so the
 * browser's notice and the relay stay quiet through upstream's own gates. One rule, read in the
 * shell's JavaScript path and mirrored in its SQL path.
 */
import type {
  OrchestrationV2ConversationMessage,
  OrchestrationV2Run,
  OrchestrationV2ThreadProjection,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import type * as SqlClient from "effect/sql/SqlClient";

import {
  HANDOFF_FALLBACK_SUFFIX,
  HANDOFF_LOUD_SUFFIX,
  HANDOFF_MESSAGE_PREFIX,
  HANDOFF_RETRY_SUFFIX,
  HANDOFF_WRITE_PREFIX,
  parseHandoffMessageId,
  parseHandoffWrite,
} from "./handoffFacts.ts";

/**
 * Upstream's relay holds a "finished" alert this long, because a run can end moments before its
 * continuation starts; a run that starts in the hold drops it (`AgentAwarenessRelay.ts`).
 */
export const RELAY_COMPLETION_HOLD_MS = 5_000;

type QuietMessage = Pick<
  OrchestrationV2ConversationMessage,
  "id" | "runId" | "createdBy" | "creationSource"
>;

/** The source run a handoff run or a write turn names, when it may be quiet (not loud, no retry). */
function quietCandidate(threadId: ThreadId, messageId: string): string | null {
  const handoff = parseHandoffMessageId(threadId, messageId);
  if (handoff !== null) return handoff.retry || handoff.loud ? null : handoff.runId;
  const write = parseHandoffWrite(threadId, messageId);
  return write === null || write.loud ? null : write.runId;
}

/**
 * A run that may end quiet: a completed run, or a failed write turn marked `:fallback`, as the
 * fallback follows it with the reason. Unmarked (idle trigger) or a failed handoff run: nothing
 * follows, so it alerts. The relay and the browser read the mark for a failed run through seam
 * lines.
 */
const endsQuiet = (threadId: ThreadId, run: Pick<OrchestrationV2Run, "status" | "userMessageId">) =>
  run.status === "completed" ||
  (run.status === "failed" && parseHandoffWrite(threadId, run.userMessageId)?.fallback === true);

/**
 * A completed first-send handoff run, a completed write turn or a failed one the fallback follows,
 * not loud, sent by the server, with no message of the person's, requested at least the relay's
 * hold after the person's run finished. Anything else (a retry, a steered write turn, a failed
 * write turn at the idle trigger, a failed handoff run) alerts.
 */
export function isQuietHandoffRun(
  threadId: ThreadId,
  run: Pick<OrchestrationV2Run, "id" | "status" | "userMessageId" | "requestedAt">,
  messages: ReadonlyArray<QuietMessage>,
  sourceRun: Pick<OrchestrationV2Run, "id" | "completedAt"> | undefined,
): boolean {
  if (!endsQuiet(threadId, run)) return false;
  const sourceRunId = quietCandidate(threadId, run.userMessageId);
  if (sourceRunId === null) return false;
  const prompt = messages.find((message) => message.id === run.userMessageId);
  if (prompt?.createdBy !== "agent" || prompt.creationSource !== "server") return false;
  if (messages.some((message) => message.runId === run.id && message.createdBy === "user")) {
    return false;
  }
  if (sourceRun?.id !== sourceRunId || sourceRun.completedAt == null) return false;
  return (
    DateTime.toEpochMillis(run.requestedAt) - DateTime.toEpochMillis(sourceRun.completedAt) >=
    RELAY_COMPLETION_HOLD_MS
  );
}

/**
 * Spread after the shell's own `latestRunStopRequested`: `{ latestRunStopRequested: true }` when
 * the shell's latest run is a quiet handoff run, else nothing, so upstream's value stands.
 */
export function quietHandoffShellMark(
  projection: Pick<OrchestrationV2ThreadProjection, "thread" | "runs" | "messages">,
  latestRun: OrchestrationV2Run | null | undefined,
): { readonly latestRunStopRequested?: true } {
  if (latestRun == null) return {};
  const sourceRunId = quietCandidate(projection.thread.id, latestRun.userMessageId);
  if (sourceRunId === null) return {};
  const sourceRun = projection.runs.find((run) => run.id === sourceRunId);
  return isQuietHandoffRun(projection.thread.id, latestRun, projection.messages, sourceRun)
    ? { latestRunStopRequested: true }
    : {};
}

/**
 * `isQuietHandoffRun` in SQL, for the run row `alias` of the shell read, followed by `OR`: it goes
 * right before upstream's stop-request `EXISTS`, so the column is true for a quiet handoff run or
 * write turn too. The id checks come first (a CASE), so any other run skips the lookups.
 */
export const quietHandoffRunOrSql = (sql: SqlClient.SqlClient, alias: string) => {
  const run = sql.literal(alias);
  const messageId = sql`json_extract(${run}.payload_json, '$.userMessageId')`;
  const prefixOf = (kind: string) => sql`(${kind} || ${run}.thread_id || ':')`;
  const startsWith = (kind: string) =>
    sql`substr(${messageId}, 1, length(${prefixOf(kind)})) = ${prefixOf(kind)}`;
  // What follows the handoff or write-turn prefix: the source run's id when no suffix ends it.
  const rest = sql`(CASE
    WHEN ${startsWith(HANDOFF_MESSAGE_PREFIX)}
      THEN substr(${messageId}, length(${prefixOf(HANDOFF_MESSAGE_PREFIX)}) + 1)
    WHEN ${startsWith(HANDOFF_WRITE_PREFIX)}
      THEN substr(${messageId}, length(${prefixOf(HANDOFF_WRITE_PREFIX)}) + 1)
    ELSE '' END)`;
  const endsWith = (suffix: string) => sql`substr(${rest}, ${-suffix.length}) = ${suffix}`;
  // The source run's id: `rest` without a write turn's `:fallback` (a loud id never reads quiet).
  const source = sql`(CASE WHEN ${startsWith(HANDOFF_WRITE_PREFIX)} AND ${endsWith(HANDOFF_FALLBACK_SUFFIX)}
    THEN substr(${rest}, 1, length(${rest}) - ${HANDOFF_FALLBACK_SUFFIX.length}) ELSE ${rest} END)`;
  return sql`COALESCE((CASE
    WHEN (${run}.status = 'completed' OR (${run}.status = 'failed' AND ${startsWith(HANDOFF_WRITE_PREFIX)} AND ${endsWith(HANDOFF_FALLBACK_SUFFIX)}))
      AND ${rest} <> ''
      AND NOT ${endsWith(HANDOFF_RETRY_SUFFIX)}
      AND NOT ${endsWith(HANDOFF_LOUD_SUFFIX)}
    THEN EXISTS (
      SELECT 1 FROM orchestration_v2_projection_messages prompt
      WHERE prompt.message_id = ${messageId} AND prompt.thread_id = ${run}.thread_id
        AND json_extract(prompt.payload_json, '$.createdBy') = 'agent'
        AND json_extract(prompt.payload_json, '$.creationSource') = 'server'
    ) AND NOT EXISTS (
      SELECT 1 FROM orchestration_v2_projection_messages steer
      WHERE steer.thread_id = ${run}.thread_id AND steer.run_id = ${run}.run_id
        AND json_extract(steer.payload_json, '$.createdBy') = 'user'
    ) AND EXISTS (
      SELECT 1 FROM orchestration_v2_projection_runs source
      WHERE source.thread_id = ${run}.thread_id AND source.run_id = ${source}
        AND source.completed_at IS NOT NULL
        AND CAST(round(
          (julianday(${run}.requested_at) - julianday(source.completed_at)) * 86400000
        ) AS INTEGER) >= ${RELAY_COMPLETION_HOLD_MS}
    )
    ELSE 0
  END), 0) OR`;
};
