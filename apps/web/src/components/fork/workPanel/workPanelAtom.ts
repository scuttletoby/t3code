/**
 * Fork (work panel, fork issue 1136): the `fork.workPanel` read of one environment, refreshed
 * once a minute while the panel shows. The server answers every viewer from one shared cache, so
 * this costs no GitHub call of its own.
 */
import { useAtomValue } from "@effect/atom-react";
import { createEnvironmentRpcQueryAtomFamily } from "@t3tools/client-runtime/state/runtime";
import { type EnvironmentId, FORK_WORK_PANEL_METHOD } from "@t3tools/contracts";
import { collectLimitAccounts } from "@t3tools/shared/usageLimits";
import { Atom } from "effect/reactivity";

import { connectionAtomRuntime } from "../../../connection/runtime";
import { environmentPresentations } from "../../../state/presentation";
import { type FleetMachine, machineAnswerOf } from "./capacity.logic";

const WORK_PANEL_REFRESH_MS = 60_000;

export const forkWorkPanelAtom = createEnvironmentRpcQueryAtomFamily(connectionAtomRuntime, {
  label: "environment-data:fork:work-panel",
  tag: FORK_WORK_PANEL_METHOD,
  staleTimeMs: WORK_PANEL_REFRESH_MS / 2,
  refreshIntervalMs: WORK_PANEL_REFRESH_MS,
  idleTtlMs: 0,
});

/**
 * Every machine the client knows, with its capacity read (`fork.workPanel` without a thread; the
 * thread's own CI read is a separate query, so neither waits on the other). A machine that is not
 * connected is not asked. Keyed by the thread's machine, which the bar marks.
 */
export const forkFleetAtom = Atom.family((here: EnvironmentId) =>
  Atom.make((get): ReadonlyArray<FleetMachine> => {
    const machines: Array<FleetMachine> = [];
    for (const [environmentId, presentation] of get(environmentPresentations.presentationsAtom)) {
      const base = {
        environmentId,
        label: presentation.entry.target.label,
        isThisMachine: environmentId === here,
      };
      machines.push({
        ...base,
        status:
          presentation.connection.phase !== "connected"
            ? { kind: "unreachable" }
            : machineAnswerOf(get(forkWorkPanelAtom({ environmentId, input: {} }))),
      });
    }
    return machines;
  }).pipe(Atom.withLabel(`web-fork-work-panel-fleet:${here}`)),
);

const forkUsageAccountsAtom = Atom.make((get) =>
  collectLimitAccounts(get(environmentPresentations.presentationsWithProvidersAtom)),
).pipe(Atom.withLabel("web-fork-work-panel-usage"));

/**
 * Each provider account's limits, from the provider status every machine already reports. The
 * panel's one usage source: a fleet-wide source (fork issue 1146) replaces only this function.
 */
export function useForkUsageAccounts() {
  return useAtomValue(forkUsageAccountsAtom);
}
