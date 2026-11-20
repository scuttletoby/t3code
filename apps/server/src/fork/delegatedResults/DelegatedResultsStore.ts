/**
 * Fork (issues 386, 625): the reads of the delegated result reminder that upstream's stores do not
 * offer. They read only indexed columns and the payload of the few rows they match, never a
 * parent's whole task list or history.
 */
import { EnvironmentId, ThreadId } from "@t3tools/contracts";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as SqlClient from "effect/sql/SqlClient";

export interface RemoteChildKey {
  readonly environmentId: EnvironmentId;
  readonly threadId: ThreadId;
}

export class DelegatedResultsStore extends Context.Service<
  DelegatedResultsStore,
  {
    /**
     * Parents with a `delegate_task` task that ended at or after `sinceIso` and whose result is
     * still owed or has no delivery state. Found at start, also when no wake was reserved for it.
     */
    readonly parentsWithUnreadResults: (sinceIso: string) => Effect.Effect<ReadonlyArray<ThreadId>>;
    /** The linked threads this parent's tasks follow, settled ones too; null: unreadable now. */
    readonly remoteChildrenOf: (
      parentThreadId: ThreadId,
    ) => Effect.Effect<ReadonlyArray<RemoteChildKey> | null>;
    /**
     * The ids of this thread's messages, and of its accepted command receipts, that start with
     * `prefix`: what was sent to it under ids of that kind. Null: unreadable now.
     */
    readonly sentIds: (
      threadId: ThreadId,
      prefix: string,
    ) => Effect.Effect<ReadonlyArray<string> | null>;
  }
>()("t3/fork/delegatedResults/DelegatedResultsStore") {}

const make = Effect.gen(function* () {
  const sql = yield* SqlClient.SqlClient;

  const parentsWithUnreadResults: DelegatedResultsStore["Service"]["parentsWithUnreadResults"] = (
    sinceIso,
  ) =>
    sql<{ readonly thread_id: string }>`
      SELECT DISTINCT thread_id FROM orchestration_v2_projection_subagents
      WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted')
        AND COALESCE(completion_delivery_state, '') IN ('pending', 'claimed', '')
        AND origin = 'app_owned'
        AND COALESCE(completed_at, updated_at) >= ${sinceIso}
      ORDER BY thread_id
    `.pipe(
      Effect.map((rows) => rows.map((row) => ThreadId.make(row.thread_id))),
      Effect.catchCause((cause) =>
        Effect.logWarning("delegated result reminder: unread results cannot be read", {
          cause,
        }).pipe(Effect.as([])),
      ),
    );

  const remoteChildrenOf: DelegatedResultsStore["Service"]["remoteChildrenOf"] = (parentThreadId) =>
    sql<{ readonly environment_id: string | null; readonly remote_thread_id: string | null }>`
      SELECT
        json_extract(payload_json, '$.remoteChild.environmentId') AS environment_id,
        json_extract(payload_json, '$.remoteChild.threadId') AS remote_thread_id
      FROM orchestration_v2_projection_subagents
      WHERE thread_id = ${parentThreadId}
        AND origin = 'app_owned'
        AND child_thread_id IS NULL
    `.pipe(
      Effect.map((rows) =>
        rows.flatMap((row) =>
          row.environment_id === null || row.remote_thread_id === null
            ? []
            : [
                {
                  environmentId: EnvironmentId.make(row.environment_id),
                  threadId: ThreadId.make(row.remote_thread_id),
                },
              ],
        ),
      ),
      Effect.catchCause((cause) =>
        Effect.logWarning("delegated result reminder: a parent's linked tasks cannot be read", {
          threadId: parentThreadId,
          cause,
        }).pipe(Effect.as(null)),
      ),
    );

  const sentIds: DelegatedResultsStore["Service"]["sentIds"] = (threadId, prefix) =>
    sql<{ readonly id: string }>`
      SELECT message_id AS id FROM orchestration_v2_projection_messages
      WHERE thread_id = ${threadId} AND substr(message_id, 1, ${prefix.length}) = ${prefix}
      UNION
      SELECT command_id AS id FROM orchestration_v2_command_receipts
      WHERE thread_id = ${threadId}
        AND status = 'accepted'
        AND substr(command_id, 1, ${prefix.length}) = ${prefix}
    `.pipe(
      Effect.map((rows) => rows.map((row) => row.id)),
      Effect.catchCause((cause) =>
        Effect.logWarning(
          "delegated result reminder: a thread's earlier reminders cannot be read",
          {
            threadId,
            cause,
          },
        ).pipe(Effect.as(null)),
      ),
    );

  return DelegatedResultsStore.of({ parentsWithUnreadResults, remoteChildrenOf, sentIds });
});

export const layer = Layer.effect(DelegatedResultsStore, make);
