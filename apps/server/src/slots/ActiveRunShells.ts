/**
 * Fork (agent slots): shells of every thread with an active run, settled or archived too. The gate
 * is a guard, so the pre-select drops only upstream's finished run statuses (a new status is still
 * read), and upstream's own shell read decides by `activeRunId`. Kept out of upstream's store.
 */
import {
  type OrchestrationV2ThreadShellSnapshot,
  OrchestrationV2RunStatus,
  ThreadId,
} from "@t3tools/contracts";
import * as NodeSqliteClient from "@t3tools/shared/nodeSqliteClient";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as SqlClient from "effect/sql/SqlClient";
import type * as SqlError from "effect/sql/SqlError";

import type * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";

/** Statuses a run never leaves (upstream's `isTerminalRunStatus`): the only ones the pre-select drops. */
export const FINISHED_RUN_STATUSES = OrchestrationV2RunStatus.literals.filter(
  ThreadManagementService.isTerminalRunStatus,
);

/**
 * The threads with a run in a status other than a finished one. It names no index: SQLite picks
 * upstream's `(thread_id, status)` index on its own (a test checks the plan), and an index that is
 * renamed, dropped or made partial only makes the read slower, never fails it. Exported for that test.
 */
export const unfinishedRunThreadIds = (sql: SqlClient.SqlClient) => sql<{
  readonly thread_id: string;
}>`
  SELECT DISTINCT thread_id
  FROM orchestration_v2_projection_runs
  WHERE status NOT IN ${sql.in(FINISHED_RUN_STATUSES)}
`;

export class ActiveRunShells extends Context.Service<
  ActiveRunShells,
  {
    /** Every non-deleted thread with an active run: `threads` and `archivedThreads`, as upstream's. */
    readonly read: Effect.Effect<
      OrchestrationV2ThreadShellSnapshot,
      SqlError.SqlError | Orchestrator.OrchestratorV2Error
    >;
  }
>()("t3/slots/ActiveRunShells") {}

const hasActiveRun = (thread: { readonly activeRunId: unknown }) => thread.activeRunId !== null;

const make = Effect.gen(function* () {
  const sql = yield* SqlClient.SqlClient;
  const threadManagement = yield* ThreadManagementService.ThreadManagementService;
  const read = Effect.gen(function* () {
    const decode = yield* sql
      .withTransaction(
        // One read transaction: the shells come from the same state of the store as the ids.
        Effect.gen(function* () {
          const rows = yield* unfinishedRunThreadIds(sql);
          return yield* threadManagement.readShellSnapshot({
            threadIds: rows.map((row) => ThreadId.make(row.thread_id)),
          });
        }),
      )
      .pipe(NodeSqliteClient.readOnly);
    const snapshot = yield* decode;
    return {
      ...snapshot,
      threads: snapshot.threads.filter(hasActiveRun),
      archivedThreads: snapshot.archivedThreads.filter(hasActiveRun),
    } satisfies OrchestrationV2ThreadShellSnapshot;
  });
  return { read };
});

/** Reads through the server's thread management and its database. */
export const layer = Layer.effect(ActiveRunShells, make);
