/**
 * Fork: a completed quiet handoff run shows in the thread shell as upstream's stopped run
 * (`latestRunStopRequested`), so the browser's notice and the relay stay quiet through upstream's
 * own gates. One rule, read in the shell's JavaScript path and mirrored in its SQL path.
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
  HANDOFF_LOUD_SUFFIX,
  HANDOFF_MESSAGE_PREFIX,
  HANDOFF_RETRY_SUFFIX,
  parseHandoffMessageId,
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

/**
 * A completed first-send handoff run, not marked loud (its settle saw the relay show the person's
 * finish at every check), sent by the server, no message of the person's in it, and requested at
 * least the relay's hold after the person's run finished (so their alert went out). Anything else,
 * a retry included, alerts as the turn's finish.
 */
export function isQuietHandoffRun(
  threadId: ThreadId,
  run: Pick<OrchestrationV2Run, "id" | "status" | "userMessageId" | "requestedAt">,
  messages: ReadonlyArray<QuietMessage>,
  sourceRun: Pick<OrchestrationV2Run, "id" | "completedAt"> | undefined,
): boolean {
  if (run.status !== "completed") return false;
  const handoff = parseHandoffMessageId(threadId, run.userMessageId);
  if (handoff === null || handoff.retry || handoff.loud) return false;
  const prompt = messages.find((message) => message.id === run.userMessageId);
  if (prompt?.createdBy !== "agent" || prompt.creationSource !== "server") return false;
  if (messages.some((message) => message.runId === run.id && message.createdBy === "user")) {
    return false;
  }
  if (sourceRun?.id !== handoff.runId || sourceRun.completedAt == null) return false;
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
  const handoff = parseHandoffMessageId(projection.thread.id, latestRun.userMessageId);
  if (handoff === null) return {};
  const sourceRun = projection.runs.find((run) => run.id === handoff.runId);
  return isQuietHandoffRun(projection.thread.id, latestRun, projection.messages, sourceRun)
    ? { latestRunStopRequested: true }
    : {};
}

/**
 * `isQuietHandoffRun` in SQL, for the run row `alias` of the shell read, followed by `OR`: it goes
 * right before upstream's stop-request `EXISTS`, so the column is true for a quiet handoff run too.
 * The id checks come first (a CASE), so any other run skips the lookups.
 */
export const quietHandoffRunOrSql = (sql: SqlClient.SqlClient, alias: string) => {
  const run = sql.literal(alias);
  const messageId = sql`json_extract(${run}.payload_json, '$.userMessageId')`;
  const prefix = sql`(${HANDOFF_MESSAGE_PREFIX} || ${run}.thread_id || ':')`;
  // What follows the prefix: the source run's id when neither suffix ends it.
  const rest = sql`substr(${messageId}, length(${prefix}) + 1)`;
  const endsWith = (suffix: string) => sql`substr(${rest}, ${-suffix.length}) = ${suffix}`;
  return sql`COALESCE((CASE
    WHEN ${run}.status = 'completed'
      AND substr(${messageId}, 1, length(${prefix})) = ${prefix}
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
      WHERE source.thread_id = ${run}.thread_id AND source.run_id = ${rest}
        AND source.completed_at IS NOT NULL
        AND CAST(round(
          (julianday(${run}.requested_at) - julianday(source.completed_at)) * 86400000
        ) AS INTEGER) >= ${RELAY_COMPLETION_HOLD_MS}
    )
    ELSE 0
  END), 0) OR`;
};
