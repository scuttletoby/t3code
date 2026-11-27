/**
 * Fork (fleet usage, fork issue 1146): the `fork.fleetUsage` read of the server this client is
 * connected to, refreshed once a minute while something shows it. The server answers every reader
 * from one round, so a second reader (the work panel's usage row) asks no machine again.
 */
import { useAtomValue } from "@effect/atom-react";
import { createEnvironmentRpcQueryAtomFamily } from "@t3tools/client-runtime/state/runtime";
import { FORK_FLEET_USAGE_METHOD, type ForkFleetUsageAnswer } from "@t3tools/contracts";
import { AsyncResult, Atom } from "effect/reactivity";

import { connectionAtomRuntime } from "../../../connection/runtime";
import { primaryEnvironmentIdAtom } from "../../../state/primaryEnvironment";

const FLEET_USAGE_REFRESH_MS = 60_000;

export const forkFleetUsageQueryAtom = createEnvironmentRpcQueryAtomFamily(connectionAtomRuntime, {
  label: "environment-data:fork:fleet-usage",
  tag: FORK_FLEET_USAGE_METHOD,
  staleTimeMs: FLEET_USAGE_REFRESH_MS / 2,
  refreshIntervalMs: FLEET_USAGE_REFRESH_MS,
  idleTtlMs: 0,
});

/** The answer, or `null` while there is none (no server, not yet, or a version without it). */
export const forkFleetUsageAtom = Atom.make((get): ForkFleetUsageAnswer | null => {
  const environmentId = get(primaryEnvironmentIdAtom);
  if (environmentId === null) return null;
  return AsyncResult.getOrElse(
    get(forkFleetUsageQueryAtom({ environmentId, input: {} })),
    () => null,
  );
}).pipe(Atom.withLabel("web-fork-fleet-usage"));

export function useForkFleetUsage(): ForkFleetUsageAnswer | null {
  return useAtomValue(forkFleetUsageAtom);
}
