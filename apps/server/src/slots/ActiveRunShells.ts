/**
 * Fork (agent slots): the shells of every thread with an active run, settled or archived too
 * (settling does not stop a run). Kept out of upstream's ProjectionStore: one read transaction
 * finds the active-run threads, then reads only their shells and keeps those with `activeRunId`.
 */
import { type OrchestrationV2ThreadShellSnapshot, ThreadId } from "@t3tools/contracts";
import * as NodeSqliteClient from "@t3tools/shared/nodeSqliteClient";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as SqlClient from "effect/sql/SqlClient";
import type * as SqlError from "effect/sql/SqlError";

import type * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";

/** Run statuses upstream's shell takes as active (`active` in `selectShellThreadRows`). */
const ACTIVE_RUN_STATUSES = ["preparing", "starting", "running"] as const;

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
        Effect.gen(function* () {
          const rows = yield* sql<{ readonly thread_id: string }>`
            SELECT DISTINCT thread_id
            FROM orchestration_v2_projection_runs
              INDEXED BY orchestration_v2_projection_runs_thread_status_idx
            WHERE status IN ${sql.in(ACTIVE_RUN_STATUSES)}
          `;
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
