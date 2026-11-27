/**
 * Fork (work panel, fork issue 1136): the `fork.workPanel` read of one environment, refreshed
 * once a minute while the panel shows. The server answers every viewer from one shared cache, so
 * this costs no GitHub call of its own.
 */
import { useAtomValue } from "@effect/atom-react";
import { createEnvironmentRpcQueryAtomFamily } from "@t3tools/client-runtime/state/runtime";
import { type EnvironmentId, FORK_WORK_PANEL_METHOD, type ThreadId } from "@t3tools/contracts";
import { collectLimitAccounts } from "@t3tools/shared/usageLimits";
import { AsyncResult, Atom } from "effect/reactivity";

import { connectionAtomRuntime } from "../../../connection/runtime";
import { environmentPresentations } from "../../../state/presentation";
import type { FleetMachine } from "./capacity.logic";

const WORK_PANEL_REFRESH_MS = 60_000;

export const forkWorkPanelAtom = createEnvironmentRpcQueryAtomFamily(connectionAtomRuntime, {
  label: "environment-data:fork:work-panel",
  tag: FORK_WORK_PANEL_METHOD,
  staleTimeMs: WORK_PANEL_REFRESH_MS / 2,
  refreshIntervalMs: WORK_PANEL_REFRESH_MS,
  idleTtlMs: 0,
});

/**
 * Every machine the client knows, with its `fork.workPanel` answer. The thread's own machine is
 * read with the thread (the CI read above shares the same query); every other machine without
 * one, for its capacity only. A machine that is not connected is not asked.
 */
export const forkFleetAtom = Atom.family((key: string) =>
  Atom.make((get): ReadonlyArray<FleetMachine> => {
    const { environmentId: here, threadId } = JSON.parse(key) as {
      readonly environmentId: EnvironmentId;
      readonly threadId: ThreadId;
    };
    const machines: Array<FleetMachine> = [];
    for (const [environmentId, presentation] of get(environmentPresentations.presentationsAtom)) {
      const base = {
        environmentId,
        label: presentation.entry.target.label,
        isThisMachine: environmentId === here,
      };
      if (presentation.connection.phase !== "connected") {
        machines.push({ ...base, status: { kind: "unreachable" } });
        continue;
      }
      const result = get(
        forkWorkPanelAtom({
          environmentId,
          input: environmentId === here ? { threadId } : {},
        }),
      );
      const answer = AsyncResult.getOrElse(result, () => null);
      machines.push({
        ...base,
        status:
          answer !== null
            ? { kind: "answered", answer }
            : result._tag === "Failure"
              ? { kind: "failed" }
              : { kind: "pending" },
      });
    }
    return machines;
  }).pipe(Atom.withLabel(`web-fork-work-panel-fleet:${key}`)),
);

export const fleetKey = (environmentId: EnvironmentId, threadId: ThreadId) =>
  JSON.stringify({ environmentId, threadId });

const forkUsageAccountsAtom = Atom.make((get) =>
  collectLimitAccounts(get(environmentPresentations.presentationsWithProvidersAtom)),
).pipe(Atom.withLabel("web-fork-work-panel-usage"));

/** Each provider account's limits, from the provider status every machine already reports. */
export function useForkUsageAccounts() {
  return useAtomValue(forkUsageAccountsAtom);
}
