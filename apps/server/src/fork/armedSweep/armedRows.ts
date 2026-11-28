/**
 * Fork (issue 880): the armed PR sweep's last report, for `wait_list`. Held in memory only: a
 * restart lists nothing until its first sweep (at most one interval). Scope `thread` lists the
 * pull requests that wake the caller; `project` every one of the caller's project.
 */
import type { ProjectId, ThreadId } from "@t3tools/contracts";
import type { WaitListResult } from "@t3tools/contracts/waitFor";
import * as DateTime from "effect/DateTime";

import type { SweepReport } from "./armedSweep.ts";

let latest: SweepReport | null = null;

const isoOf = (ms: number) => DateTime.formatIso(DateTime.makeUnsafe(ms));

/** The sweep's newest report; null while the sweep is off. */
export function publishArmedReport(report: SweepReport | null): void {
  latest = report;
}

export function armedRowsFor(request: {
  readonly threadId: ThreadId;
  readonly projectId: ProjectId;
  readonly scope?: "thread" | "project" | undefined;
}): WaitListResult["armedSweep"] {
  if (latest === null) return undefined;
  const rows = latest.pullRequests.filter((row) =>
    request.scope === "project"
      ? row.projectId === request.projectId
      : row.threadId === request.threadId,
  );
  return {
    mode: latest.mode,
    sweptAt: isoOf(latest.sweptAt),
    ...(latest.error === undefined ? {} : { error: latest.error }),
    pullRequests: rows.map(({ projectId: _projectId, notGreenSince, ...row }) => ({
      ...row,
      notGreenSince: notGreenSince === null ? null : isoOf(notGreenSince),
    })),
  };
}
