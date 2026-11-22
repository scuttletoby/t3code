/**
 * Fork (placement P5): pure rule engine. Hard rules H1 to H9, each failing closed on its own, then
 * upstream's `chooseLoadBalancedEnvironment` for the rank, called, not moved.
 * P6 follow-up: `packages/shared` has no `./forkPlacement` export and `apps/server` does not
 * depend on `client-runtime`, so P6 imports this file by relative path or announces a seam first.
 */
import {
  type AuthMcpClientAccess,
  canonicalPlacementLabels,
  type HostResourcesSnapshot,
  isRuntimeModeAbove,
  type PlacementNeeds,
  readPlacementNeeds,
  type RuntimeMode,
} from "@t3tools/contracts";

import { chooseLoadBalancedEnvironment } from "../../client-runtime/src/load-balancing.ts";

/** The `placement.mode` setting. Anything else (absent, old file, bad value) is `off`. */
export type PlacementMode = "off" | "advise" | "enforce";

/** Upstream's four load-balancing words, with the numbers its Settings page writes for them. */
export type PlacementWeight = "prefer" | "normal" | "less" | "manual";
export const UPSTREAM_WEIGHTS: Readonly<Record<PlacementWeight, number>> = {
  prefer: 100,
  normal: 50,
  less: 25,
  manual: 0,
};

/** Upstream's freshness window for a host reading (`chooseLoadBalancedEnvironment`). */
const FACTS_FRESH_MS = 15_000;
/** A reading received further ahead of `now` than this is not trusted (upstream's bound). */
const FACTS_FUTURE_SLACK_MS = 5_000;
/** H8: CPU at or above this share is full (upstream's bound). */
const CPU_FULL = 0.95;

/**
 * The `placement` facts a machine's `t3_environment_read` returns (row P3), as received. Optional
 * fields are optional there too: an older or newer build may leave them out.
 */
export interface PlacementFacts {
  readonly labels: ReadonlyArray<string>;
  /** The machine says it can take work (`factsCommand` exit 0, slot gate not full). */
  readonly available: boolean;
  readonly reason?: string;
  readonly runningAgents?: number;
  /** `null`: the gate is on with no machine limit. Absent: slots are off, or the gate was unread. */
  readonly slotsFree?: number | null;
  /** `null`: no memory floor set there. `0` keeps nothing back. At the floor is available. */
  readonly memoryFloorMb?: number | null;
  readonly freeMemoryMb?: number | null;
  /** The machine's own clock; never used for age (clocks differ). */
  readonly factsAt: number;
}

/**
 * One `t3_environment_read` answer, as the server's `factsFromAnswer`
 * (apps/server/src/fork/environmentFacts.ts) decodes it. No answer, or one with neither
 * `hostResources` nor `placement` (an older build), is `null` there, never this.
 */
export interface MachineFacts {
  readonly hostResources?: HostResourcesSnapshot;
  readonly placement?: PlacementFacts;
  /**
   * Set when the answer did not decode with P3's schema: the machine is refused (H2) with this as
   * the reason, so a bad answer reads as bad, not as a busy or unfit machine. Named, too.
   */
  readonly problem?: string;
  /** When this machine received the answer (ms, the reader's clock), like upstream's `receivedAt`. */
  readonly receivedAt: number;
}

/** The link to a machine, from `PeerLinks`. "Here" has none. */
export interface PlacementLink {
  /** Answered the facts read within its deadline, at pick time. */
  readonly reachable: boolean;
  /** ms since the epoch, this machine's clock. */
  readonly expiresAt: number;
  readonly access: AuthMcpClientAccess;
  readonly betaHere: boolean;
  readonly betaThere: boolean;
}

/** What the pick-time lookups found there (`resolveRemoteProject`, `resolveRemoteInstance`). */
export interface PlacementWorkspace {
  readonly matchingCheckouts: number;
  readonly providerPresent: boolean;
  readonly modelPresent: boolean;
}

export interface PlacementMachine {
  readonly environmentId: string;
  /** This machine. Checked first, with its local facts and the same rules. */
  readonly here: boolean;
  /** `null` for "here"; required for any other machine (a machine without one fails H1). */
  readonly link: PlacementLink | null;
  /**
   * `null`: no answer, or an older build (an answer with neither `placement` nor `hostResources`
   * decodes to `null`). Named, a machine without facts meets only the rules that need none.
   */
  readonly facts: MachineFacts | null;
  /** `null`: not looked up. */
  readonly workspace: PlacementWorkspace | null;
  /** Absent: `normal`, as upstream. */
  readonly weight?: PlacementWeight;
}

export interface PlacementTask {
  /** The child's runtime mode, after `inherit` is resolved. */
  readonly runtimeMode: RuntimeMode;
  /**
   * The project's `placementNeeds` as the settings hold it: labels, or a kept bad value, which
   * fits no machine (read through contracts' `readPlacementNeeds`, never as `[]`).
   */
  readonly needs: PlacementNeeds;
  /** The `never` label groups of the machine that checks (sender, then receiver). */
  readonly never: ReadonlyArray<ReadonlyArray<string>>;
  /** The task asking is itself a placed child (H9). */
  readonly fromPlacedChild: boolean;
  /** An explicit `environmentId`: only the hard rules apply, never the ranking. */
  readonly named?: string;
}

export interface PlacementInput {
  /** Absent or unknown: `off`. */
  readonly mode?: unknown;
  readonly task: PlacementTask;
  readonly machines: ReadonlyArray<PlacementMachine>;
  readonly now: number;
}

export type HardRule = "H1" | "H2" | "H3" | "H4" | "H5" | "H6" | "H7" | "H8" | "H9";

export interface RuleFailure {
  readonly rule: HardRule;
  readonly reason: string;
}

export interface MachineVerdict {
  readonly environmentId: string;
  readonly here: boolean;
  readonly failures: ReadonlyArray<RuleFailure>;
  /** 1 = best by upstream's score; `null` when a hard rule failed or upstream's score leaves it out. */
  readonly rank: number | null;
  /** Why it is not ranked although every hard rule passed. */
  readonly unranked?: string;
}

export type PlacementResult =
  | { readonly kind: "off"; readonly reason: string }
  | {
      readonly kind: "picked";
      readonly mode: "advise" | "enforce";
      readonly environmentId: string;
      /** Every machine that fits, best first; the sender tries the next when one refuses. */
      readonly order: ReadonlyArray<string>;
      readonly why: string;
      readonly verdicts: ReadonlyArray<MachineVerdict>;
    }
  | {
      readonly kind: "none";
      readonly mode: "advise" | "enforce";
      readonly reason: string;
      readonly verdicts: ReadonlyArray<MachineVerdict>;
    };

const decodeMode = (mode: unknown): PlacementMode =>
  mode === "advise" || mode === "enforce" ? mode : "off";

/** A number that can be compared: not NaN, not infinite. */
const isFiniteNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

/** Set, but not a finite number at or above 0. `null` and absent are "not set". */
const isBadCount = (value: unknown) =>
  value !== null && value !== undefined && !(isFiniteNumber(value) && value >= 0);

/**
 * Labels, needs and `never` groups are compared in contracts' one form
 * (`canonicalPlacementLabels`), the form P2 stores, so the two never differ; comparing both sides
 * in it keeps H5 and H6 right for a value from another machine or an older file. A value that is
 * not a string (another machine's answer) is dropped, so it never matches.
 */
export const canonicalLabels = (labels: ReadonlyArray<unknown>): ReadonlyArray<string> =>
  canonicalPlacementLabels(labels.filter((label): label is string => typeof label === "string"));

/** One label in that form; blank (never met) for a blank or a value that is not a string. */
const canonicalLabel = (label: unknown): string => canonicalLabels([label])[0] ?? "";

/** Typed as a record so a mode upstream adds must be listed here before it can pass H3. */
const KNOWN_RUNTIME_MODES: Readonly<Record<RuntimeMode, true>> = {
  "approval-required": true,
  "auto-accept-edits": true,
  auto: true,
  "full-access": true,
};
const isKnownRuntimeMode = (value: unknown): value is RuntimeMode =>
  typeof value === "string" && Object.hasOwn(KNOWN_RUNTIME_MODES, value);

const isWeightWord = (value: unknown): value is PlacementWeight =>
  typeof value === "string" && Object.hasOwn(UPSTREAM_WEIGHTS, value);

type Check = (machine: PlacementMachine, task: PlacementTask, now: number) => string | null;

const labelsOf = (machine: PlacementMachine) => machine.facts?.placement?.labels;

/**
 * This machine without a CPU sample (no sampler, a failed read, CPU not measured yet). A sampler
 * failure here does not stop all delegation: it passes H2 and H8 without one and is tried after
 * every ranked machine (`fitsAsFallback`). A linked machine without one is still refused.
 */
const isHereWithoutHostFacts = (machine: PlacementMachine) =>
  machine.here &&
  machine.facts !== null &&
  (machine.facts.hostResources === undefined ||
    machine.facts.hostResources.cpuUtilization === null);

const H1: Check = (machine, _task, now) => {
  if (machine.here) return null;
  const link = machine.link;
  if (link === null) return "There is no link to this machine.";
  if (!link.reachable) return "The machine did not answer now.";
  if (!isFiniteNumber(link.expiresAt)) return "The link's expiry time is not a valid number.";
  if (link.expiresAt <= now) return "The link to this machine has expired.";
  if (!link.betaHere || !link.betaThere) return "Linked machines beta is off at one end.";
  return null;
};

const H2: Check = (machine, _task, now) => {
  const facts = machine.facts;
  if (facts === null) return "The machine sent no facts (no answer, or an older build).";
  if (facts.problem !== undefined) return `The machine's facts are not valid: ${facts.problem}`;
  if (facts.placement === undefined) return "The machine sent no placement facts.";
  const host = facts.hostResources;
  if (host === undefined && !machine.here) return "The machine sent no CPU and memory facts.";
  if (
    host !== undefined &&
    (!(isFiniteNumber(host.cpuCount) && host.cpuCount > 0) ||
      !(isFiniteNumber(host.totalMemoryBytes) && host.totalMemoryBytes > 0) ||
      !(isFiniteNumber(host.availableMemoryBytes) && host.availableMemoryBytes >= 0))
  ) {
    return "The machine's CPU and memory facts are incomplete or not valid numbers.";
  }
  if (!isFiniteNumber(facts.receivedAt)) {
    return "The facts have no valid receipt time, so they count as too old.";
  }
  if (facts.receivedAt > now + FACTS_FUTURE_SLACK_MS) return "The facts' receipt time is ahead.";
  if (now - facts.receivedAt > FACTS_FRESH_MS) return "The machine's facts are too old.";
  return null;
};

const H3: Check = (machine, task) => {
  if (machine.here) return null;
  const access = machine.link?.access;
  if (access === undefined) return "There is no link, so its access is unknown.";
  if (!isKnownRuntimeMode(task.runtimeMode)) {
    return `The task's runtime mode ${String(task.runtimeMode)} is not one this build knows.`;
  }
  if (access !== "read-only" && !isKnownRuntimeMode(access)) {
    return `The link allows ${String(access)}, which this build does not know.`;
  }
  if (access === "read-only" || isRuntimeModeAbove(task.runtimeMode, access)) {
    return `The link allows ${access}, lower than the task's ${task.runtimeMode}.`;
  }
  return null;
};

const H4: Check = (machine) => {
  const placement = machine.facts?.placement;
  if (placement === undefined) return "No facts say whether the machine is free.";
  if (placement.available !== true) {
    return `The machine says it is not available: ${placement.reason ?? "no reason given"}.`;
  }
  return null;
};

/** A blank member is dropped from its group (the group gets stricter); a group of blanks is none. */
const H5: Check = (machine, task) => {
  const raw = labelsOf(machine);
  if (raw === undefined) return "No labels to check the never groups against.";
  const labels = new Set(canonicalLabels(raw));
  const met = task.never
    .map(canonicalLabels)
    .find((group) => group.length > 0 && group.every((label) => labels.has(label)));
  return met === undefined ? null : `The machine has all of a never group: ${met.join(" + ")}.`;
};

/** A blank need is never met, and a broken needs value fits no machine (both fail closed). */
const H6: Check = (machine, task) => {
  const needs = needsOf(task);
  if (needs.fits === "none") return needs.reason;
  const raw = labelsOf(machine);
  if (raw === undefined) return "No labels to check the project's needs against.";
  const labels = new Set(canonicalLabels(raw));
  const missing = [...new Set(needs.labels.map(canonicalLabel))].filter(
    (need) => need.length === 0 || !labels.has(need),
  );
  return missing.length === 0
    ? null
    : `The machine lacks what the project needs: ${missing.map((need) => need || "(a blank need)").join(", ")}.`;
};

/** The task's needs through contracts' `readPlacementNeeds`; anything but labels fits nothing. */
const needsOf = (task: PlacementTask) => {
  const needs = readPlacementNeeds(task.needs);
  return needs.fits === "labels" && !Array.isArray(needs.labels)
    ? ({ fits: "none", reason: "this project's placementNeeds are not a list of labels" } as const)
    : needs;
};

const H7: Check = (machine) => {
  const workspace = machine.workspace;
  if (workspace === null) return "The checkout, provider and model there were not checked.";
  if (workspace.matchingCheckouts !== 1) {
    return `${workspace.matchingCheckouts} matching checkouts there, not exactly one.`;
  }
  if (!workspace.providerPresent) return "The provider is not there.";
  if (!workspace.modelPresent) return "The model is not there.";
  return null;
};

const MB = 1024 * 1024;

const H8: Check = (machine) => {
  const placement = machine.facts?.placement;
  const host = machine.facts?.hostResources;
  const hereWithoutHost = isHereWithoutHostFacts(machine);
  if (placement === undefined || (host === undefined && !hereWithoutHost)) {
    return "No facts to check capacity.";
  }
  // As P3 sends it: `null` means the gate is on with no machine limit; absent means slots are off
  // (or the gate was unread, and then `available` is false, so H4 refuses). Either way only memory
  // and CPU apply here.
  if (isBadCount(placement.slotsFree))
    return "The machine's free slot count is not a valid number.";
  if (isFiniteNumber(placement.slotsFree) && placement.slotsFree < 1) {
    return "No agent slot is free there.";
  }
  if (isBadCount(placement.memoryFloorMb))
    return "The machine's memory floor is not a valid number.";
  if (isBadCount(placement.freeMemoryMb)) return "The machine's free memory is not a valid number.";
  const floor = placement.memoryFloorMb ?? null;
  if (floor !== null) {
    const freeMb =
      placement.freeMemoryMb ?? (host === undefined ? null : host.availableMemoryBytes / MB);
    if (freeMb === null) return "The machine's free memory is not known.";
    if (!(isFiniteNumber(freeMb) && freeMb >= 0)) {
      return "The machine's free memory is not a valid number.";
    }
    // At the floor is available, as the slot gate (`agentSlots.ts`) and P3 say.
    if (freeMb < floor) {
      return `Free memory ${Math.floor(freeMb)} MB is below the machine's floor of ${floor} MB.`;
    }
  }
  // Here without a sample: with slots on, its gate checks again at launch; with them off, nothing
  // does, as in upstream's own here-first launch (`isHereWithoutHostFacts`).
  // `host` is always set past this line (checked above); the second test only narrows its type.
  if (hereWithoutHost || host === undefined) return null;
  const cpu = host.cpuUtilization;
  if (cpu === null) return "The machine's CPU use is not measured.";
  if (!(isFiniteNumber(cpu) && cpu >= 0)) return "The machine's CPU use is not a valid number.";
  if (cpu >= CPU_FULL) return "The machine's CPU is at 95 % or more.";
  return null;
};

const H9: Check = (machine, task) =>
  task.fromPlacedChild && !machine.here
    ? "This task was itself placed: its children run where it runs."
    : null;

const HARD_RULES: ReadonlyArray<readonly [HardRule, Check]> = [
  ["H1", H1],
  ["H2", H2],
  ["H3", H3],
  ["H4", H4],
  ["H5", H5],
  ["H6", H6],
  ["H7", H7],
  ["H8", H8],
  ["H9", H9],
];

const describe = (failures: ReadonlyArray<RuleFailure>) =>
  failures.map((failure) => `${failure.rule}: ${failure.reason}`).join(" ");

/**
 * The rules a named machine without facts still meets (design section 7): every rule that needs
 * no facts. H1 and H3 read the link, H7 the pick-time lookup (`resolveRemoteProject`,
 * `resolveRemoteInstance`, which an older build answers too; a lookup that did not run fails), and
 * H9 the task. Being named lifts none of them.
 */
const NO_FACT_RULES: ReadonlySet<HardRule> = new Set(["H1", "H3", "H7", "H9"]);

export const checkHardRules = (
  machine: PlacementMachine,
  task: PlacementTask,
  now: number,
  only?: ReadonlySet<HardRule>,
): ReadonlyArray<RuleFailure> =>
  HARD_RULES.flatMap(([rule, check]) => {
    if (only !== undefined && !only.has(rule)) return [];
    const reason = check(machine, task, now);
    return reason === null ? [] : [{ rule, reason }];
  });

/** Here first, then by id: the same machines in any order give the same pick. */
const stableOrder = (machines: ReadonlyArray<PlacementMachine>) =>
  [...machines].sort((a, b) =>
    a.here !== b.here ? (a.here ? -1 : 1) : a.environmentId < b.environmentId ? -1 : 1,
  );

/** Repeated upstream picks give the full order with upstream's tie rule (first listed wins). */
const rankWithUpstream = (machines: ReadonlyArray<PlacementMachine>, now: number) => {
  const left = machines.map((machine) => ({
    environmentId: machine.environmentId,
    resources: machine.facts?.hostResources ?? null,
    // A word this build does not know is NaN, which upstream leaves out.
    weight:
      machine.weight === undefined
        ? UPSTREAM_WEIGHTS.normal
        : isWeightWord(machine.weight)
          ? UPSTREAM_WEIGHTS[machine.weight]
          : Number.NaN,
    ...(machine.facts ? { receivedAt: machine.facts.receivedAt } : {}),
  }));
  const order: string[] = [];
  for (;;) {
    const best = chooseLoadBalancedEnvironment(left, now);
    if (best === null) return order;
    order.push(best);
    left.splice(
      left.findIndex((candidate) => candidate.environmentId === best),
      1,
    );
  }
};

/** Upstream's memory limit: 5 % free or less is left out. Unknown (no sample) is not low. */
const isLowOnMemory = (machine: PlacementMachine) => {
  const host = machine.facts?.hostResources;
  return host !== undefined && host.availableMemoryBytes / host.totalMemoryBytes <= 0.05;
};

const isUsableWeight = (machine: PlacementMachine) =>
  machine.weight === undefined || (isWeightWord(machine.weight) && machine.weight !== "manual");

/** The fallback keeps every limit upstream's score could check: weight and, if sampled, memory. */
const fitsAsFallback = (machine: PlacementMachine) =>
  isHereWithoutHostFacts(machine) && isUsableWeight(machine) && !isLowOnMemory(machine);

/** Why upstream's score left out a machine that passed every hard rule. */
const unrankedReason = (machine: PlacementMachine) => {
  if (machine.weight === "manual") return "Set to manual: used only when named.";
  if (machine.weight !== undefined && !isWeightWord(machine.weight)) {
    return `Its weight word "${String(machine.weight)}" is not one of prefer, normal, less or manual.`;
  }
  if (isLowOnMemory(machine)) return "Upstream's score leaves it out (free memory 5 % or less).";
  return "Upstream's score leaves it out.";
};

const duplicateId = (machines: ReadonlyArray<PlacementMachine>) => {
  const seen = new Set<string>();
  for (const machine of machines) {
    if (seen.has(machine.environmentId)) return machine.environmentId;
    seen.add(machine.environmentId);
  }
  return undefined;
};

export const choosePlacement = (input: PlacementInput): PlacementResult => {
  const mode = decodeMode(input.mode);
  if (mode === "off")
    return { kind: "off", reason: "Placement is off: the task runs as upstream." };
  const { task, now } = input;
  const machines = stableOrder(input.machines);
  const duplicate = duplicateId(machines);
  if (duplicate !== undefined) {
    const reason = `Two machines share the id ${duplicate}, so none is picked.`;
    return { kind: "none", mode, reason, verdicts: [] };
  }
  // Before the name, too: a broken needs value fits no machine, also one named without facts.
  const needs = needsOf(task);
  if (needs.fits === "none") {
    return { kind: "none", mode, reason: `No machine fits: ${needs.reason}.`, verdicts: [] };
  }

  if (task.named !== undefined) {
    const machine = machines.find((candidate) => candidate.environmentId === task.named);
    if (machine === undefined) {
      return { kind: "none", mode, reason: `No machine named ${task.named}.`, verdicts: [] };
    }
    const noFacts = machine.facts === null;
    const failures = checkHardRules(machine, task, now, noFacts ? NO_FACT_RULES : undefined);
    const verdicts = [
      { environmentId: machine.environmentId, here: machine.here, failures, rank: null },
    ];
    if (failures.length > 0) {
      return {
        kind: "none",
        mode,
        reason: `${machine.environmentId} was named but fails ${describe(failures)}`,
        verdicts,
      };
    }
    return {
      kind: "picked",
      mode,
      environmentId: machine.environmentId,
      order: [machine.environmentId],
      why: `${machine.environmentId} was named and passes the hard rules${noFacts ? ` that need no facts (${[...NO_FACT_RULES].join(", ")})` : ""}.`,
      verdicts,
    };
  }

  if (machines.length === 0) {
    return { kind: "none", mode, reason: "No machine to choose from.", verdicts: [] };
  }

  const failuresOf = machines.map((machine) => checkHardRules(machine, task, now));
  const fitting = machines.filter((_, index) => failuresOf[index]?.length === 0);
  const ranked = rankWithUpstream(fitting, now);
  // Here without a CPU sample (a sampler failure) is the fallback after every ranked machine.
  const fallback = fitting.filter(
    (machine) => fitsAsFallback(machine) && !ranked.includes(machine.environmentId),
  );
  const order = [...ranked, ...fallback.map((machine) => machine.environmentId)];
  const verdicts: MachineVerdict[] = machines.map((machine, index) => {
    const failures = failuresOf[index] ?? [];
    const position = order.indexOf(machine.environmentId);
    if (failures.length > 0 || position >= 0) {
      return {
        environmentId: machine.environmentId,
        here: machine.here,
        failures,
        rank: position >= 0 ? position + 1 : null,
      };
    }
    return {
      environmentId: machine.environmentId,
      here: machine.here,
      failures,
      rank: null,
      unranked: unrankedReason(machine),
    };
  });
  const [best] = order;
  if (best === undefined) {
    const lines = verdicts.map(
      (verdict) =>
        `${verdict.environmentId}: ${verdict.failures.length > 0 ? describe(verdict.failures) : verdict.unranked}`,
    );
    return { kind: "none", mode, reason: `No machine fits. ${lines.join(" ")}`, verdicts };
  }
  return {
    kind: "picked",
    mode,
    environmentId: best,
    order,
    why:
      ranked.length > 0
        ? `${best} passes every hard rule and ranks first of ${order.length} by upstream's score.`
        : `${best} passes every hard rule; its CPU use was not measured, so it is picked only because no machine ranks.`,
    verdicts,
  };
};
