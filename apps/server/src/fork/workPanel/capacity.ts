/**
 * Fork (work panel): this machine's capacity, from the same facts placement reads
 * (environmentFacts.ts): the slot gate's counts and the host sampler's memory. A count the facts
 * do not give is unknown with their reason, never 0.
 */
import type { ForkMachineCapacity } from "@t3tools/contracts";
import type * as Effect from "effect/Effect";

import type * as EnvironmentFacts from "../environmentFacts.ts";

/** The parts of the facts read it uses; each may be missing. */
type Facts = Partial<
  Pick<Effect.Success<typeof EnvironmentFacts.read>, "hostResources" | "placement">
>;

const SOURCE_AGENTS = "This machine's agent slot gate (server.agentSlots).";
const SOURCE_MEMORY = "This machine's host resource sampler.";
export const REASON_NO_HOST_READ = "This machine's memory could not be read now.";

const MB = 1024 * 1024;

export const REASON_OVER_LIMIT = "More agents hold a slot than the machine's limit allows now.";

/**
 * `slotLimit` is the gate's own machine limit (`null`: none), which `slotsFree` (clamped at 0)
 * cannot give back when more agents hold a slot than the limit (it was lowered while they ran).
 */
export function capacityFromFacts(
  facts: Facts,
  slotLimit?: number | null | undefined,
): ForkMachineCapacity {
  const { placement, hostResources } = facts;
  const agents: ForkMachineCapacity["agents"] =
    placement?.runningAgents === undefined || placement.slotsFree === undefined
      ? {
          state: "unknown",
          reason: placement?.reason ?? "The agent slot gate gave no count.",
          source: SOURCE_AGENTS,
        }
      : {
          state: "answered",
          used: placement.runningAgents,
          total:
            slotLimit !== undefined
              ? slotLimit
              : placement.slotsFree === null
                ? null
                : placement.runningAgents + placement.slotsFree,
          ...(slotLimit != null && placement.runningAgents > slotLimit
            ? { reason: REASON_OVER_LIMIT }
            : placement.reason === undefined
              ? {}
              : { reason: placement.reason }),
          source: SOURCE_AGENTS,
        };
  const memoryMb: ForkMachineCapacity["memoryMb"] =
    hostResources === undefined || hostResources.totalMemoryBytes === 0
      ? { state: "unknown", reason: REASON_NO_HOST_READ, source: SOURCE_MEMORY }
      : {
          state: "answered",
          used: Math.round(
            Math.max(0, hostResources.totalMemoryBytes - hostResources.availableMemoryBytes) / MB,
          ),
          total: Math.round(hostResources.totalMemoryBytes / MB),
          source: SOURCE_MEMORY,
        };
  return {
    agents,
    memoryMb,
    ...(placement?.memoryFloorMb === undefined ? {} : { memoryFloorMb: placement.memoryFloorMb }),
    ...(placement?.factsAt !== undefined
      ? { factsAt: placement.factsAt }
      : hostResources?.sampledAt !== undefined
        ? { factsAt: hostResources.sampledAt }
        : {}),
  };
}
