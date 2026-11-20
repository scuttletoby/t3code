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

/** Upstream's index on runs by thread and status, which upstream's own shell read names too. */
export const RUN_STATUS_INDEX = "orchestration_v2_projection_runs_thread_status_idx";

/** Logged when the index is gone: the read goes on without it, so the gate still counts. */
export const MISSING_INDEX_MESSAGE =
  `agent slots: index ${RUN_STATUS_INDEX} is missing; reading running agents without it ` +
  "(slower). Upstream renamed or dropped it: update apps/server/src/slots/ActiveRunShells.ts";

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
  /** Logged once per server: the gate reads often, and the message names the fix. */
  let missingIndexLogged = false;
  const unfinishedRunThreadIds = Effect.gen(function* () {
    // A missing index would fail `INDEXED BY` and leave the gate with no counts, so ask first.
    const index = yield* sql`
      SELECT 1 FROM sqlite_schema WHERE type = 'index' AND name = ${RUN_STATUS_INDEX}
    `;
    if (index.length > 0) {
      return yield* sql<{ readonly thread_id: string }>`
        SELECT DISTINCT thread_id
        FROM orchestration_v2_projection_runs
          INDEXED BY ${sql.literal(RUN_STATUS_INDEX)}
        WHERE status NOT IN ${sql.in(FINISHED_RUN_STATUSES)}
      `;
    }
    if (!missingIndexLogged) {
      missingIndexLogged = true;
      yield* Effect.logError(MISSING_INDEX_MESSAGE);
    }
    return yield* sql<{ readonly thread_id: string }>`
      SELECT DISTINCT thread_id
      FROM orchestration_v2_projection_runs
      WHERE status NOT IN ${sql.in(FINISHED_RUN_STATUSES)}
    `;
  });
  const read = Effect.gen(function* () {
    const decode = yield* sql
      .withTransaction(
        Effect.gen(function* () {
          const rows = yield* unfinishedRunThreadIds;
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
