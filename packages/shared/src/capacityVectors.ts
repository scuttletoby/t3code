/** Fork: how one vector of `capacity/capacity-vectors.json` is run (README.md beside it). */
import {
  capacityDecision,
  capacityFill,
  type CapacityFacts,
  type CapacityFillStep,
  type CapacityLearned,
  type CapacityPerGroup,
  type CapacityRule,
  type PressureState,
} from "./capacityRule.ts";

export interface CapacityVector {
  readonly name: string;
  readonly why: string;
  readonly facts: string | CapacityFacts;
  readonly patch?: Partial<CapacityFacts>;
  readonly ask?: { readonly kind: string; readonly declaredMb?: number };
  readonly fill?: ReadonlyArray<CapacityFillStep>;
  readonly previous?: PressureState | null;
  readonly nowMs?: number;
  readonly learned?: CapacityLearned;
  readonly floors?: CapacityPerGroup;
  readonly ceilings?: CapacityPerGroup;
  readonly expect: Readonly<Record<string, unknown>>;
}

export interface CapacityVectorsFile {
  readonly format: "capacity-vectors/1";
  readonly factSets: Readonly<Record<string, CapacityFacts>>;
  readonly vectors: ReadonlyArray<CapacityVector>;
}

/**
 * The actual answer to one vector, with the same keys as its `expect` (a caller compares them).
 * Facts: a named fact set or an object, then `patch` replaces whole top-level keys. `floors` and
 * `ceilings` replace the rule's. A vector has exactly one of `ask` and `fill`.
 */
export function runCapacityVector(
  rule: CapacityRule,
  file: CapacityVectorsFile,
  vector: CapacityVector,
): Record<string, unknown> {
  const base =
    typeof vector.facts === "string"
      ? vector.facts.startsWith("//") || !Object.hasOwn(file.factSets, vector.facts)
        ? undefined
        : file.factSets[vector.facts]
      : vector.facts;
  if (base === undefined)
    throw new Error(`${vector.name}: unknown fact set ${String(vector.facts)}`);
  if ((vector.ask === undefined) === (vector.fill === undefined)) {
    throw new Error(`${vector.name}: needs exactly one of ask and fill`);
  }
  const vectorRule: CapacityRule = {
    ...rule,
    floors: vector.floors ?? rule.floors,
    ceilings: vector.ceilings ?? rule.ceilings,
  };
  const input = {
    facts: { ...base, ...vector.patch },
    ...(vector.learned === undefined ? {} : { learned: vector.learned }),
    ...(vector.previous === undefined ? {} : { previous: vector.previous }),
    ...(vector.nowMs === undefined ? {} : { nowMs: vector.nowMs }),
  };
  if (vector.fill !== undefined) {
    const { admitted, stoppedBy } = capacityFill(vectorRule, input, vector.fill);
    return { admitted, stoppedBy };
  }
  const decision = capacityDecision(vectorRule, { ...input, ask: vector.ask! });
  return {
    admit: decision.admit,
    reason: decision.reason,
    level: decision.level,
    calmSinceMs: decision.pressure.calmSinceMs,
    freeMb: decision.freeMb,
    marginMb: decision.marginMb,
    owedMb: decision.owedMb,
    reservedMb: decision.reservedMb,
    costMb: decision.costMb,
  };
}
