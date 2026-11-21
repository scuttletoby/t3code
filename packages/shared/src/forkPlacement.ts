/**
 * Fork (placement row P5): pure rule engine. Hard rules H1 to H9, each with a plain reason, then
 * upstream's `chooseLoadBalancedEnvironment` for the rank, called, not moved: no upstream line changes.
 */
import {
  type AuthMcpClientAccess,
  type HostResourcesSnapshot,
  isRuntimeModeAbove,
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
  /** `null`: no machine slot limit (its gate is off). */
  readonly slotsFree?: number | null;
  /** `null`: no memory floor set there. */
  readonly memoryFloorMb?: number | null;
  readonly freeMemoryMb?: number | null;
  /** The machine's own clock; never used for age (clocks differ). */
  readonly factsAt: number;
}

/** One `t3_environment_read` answer. Every field is optional: an older build sends none. */
export interface MachineFacts {
  readonly hostResources?: HostResourcesSnapshot;
  readonly placement?: PlacementFacts;
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
  /** `null`: no answer, or an older build that sends no facts. */
  readonly facts: MachineFacts | null;
  /** `null`: not looked up. */
  readonly workspace: PlacementWorkspace | null;
  /** Absent: `normal`, as upstream. */
  readonly weight?: PlacementWeight;
}

export interface PlacementTask {
  /** The child's runtime mode, after `inherit` is resolved. */
  readonly runtimeMode: RuntimeMode;
  /** The project's `placementNeeds`. */
  readonly needs: ReadonlyArray<string>;
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

type Check = (machine: PlacementMachine, task: PlacementTask, now: number) => string | null;

const labelsOf = (machine: PlacementMachine) => machine.facts?.placement?.labels;

const H1: Check = (machine, _task, now) => {
  if (machine.here) return null;
  const link = machine.link;
  if (link === null) return "There is no link to this machine.";
  if (!link.reachable) return "The machine did not answer now.";
  if (link.expiresAt <= now) return "The link to this machine has expired.";
  if (!link.betaHere || !link.betaThere) return "Linked machines beta is off at one end.";
  return null;
};

const H2: Check = (machine, _task, now) => {
  const facts = machine.facts;
  if (facts === null) return "The machine sent no facts (no answer, or an older build).";
  if (facts.placement === undefined) return "The machine sent no placement facts.";
  const host = facts.hostResources;
  if (host === undefined) return "The machine sent no CPU and memory facts.";
  if (host.cpuCount <= 0 || host.totalMemoryBytes <= 0) {
    return "The machine's CPU and memory facts are incomplete.";
  }
  if (facts.receivedAt > now + FACTS_FUTURE_SLACK_MS) return "The facts' receipt time is ahead.";
  if (now - facts.receivedAt > FACTS_FRESH_MS) return "The machine's facts are too old.";
  return null;
};

const H3: Check = (machine, task) => {
  if (machine.here) return null;
  const access = machine.link?.access;
  if (access === undefined) return "There is no link, so its access is unknown.";
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

const H5: Check = (machine, task) => {
  const labels = labelsOf(machine);
  if (labels === undefined) return "No labels to check the never groups against.";
  const met = task.never.find(
    (group) => group.length > 0 && group.every((label) => labels.includes(label)),
  );
  return met === undefined ? null : `The machine has all of a never group: ${met.join(" + ")}.`;
};

const H6: Check = (machine, task) => {
  const labels = labelsOf(machine);
  if (labels === undefined) return "No labels to check the project's needs against.";
  const missing = task.needs.filter((need) => !labels.includes(need));
  return missing.length === 0
    ? null
    : `The machine lacks what the project needs: ${missing.join(", ")}.`;
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
  if (placement === undefined || host === undefined) return "No facts to check capacity.";
  // `null` or absent: no machine limit (its gate is off), so only CPU and memory apply.
  if (typeof placement.slotsFree === "number" && placement.slotsFree <= 0) {
    return "No agent slot is free there.";
  }
  const floor = placement.memoryFloorMb ?? null;
  const freeMb = placement.freeMemoryMb ?? host.availableMemoryBytes / MB;
  if (floor !== null && floor > 0 && !(freeMb > floor)) {
    return `Free memory ${Math.floor(freeMb)} MB is not above the machine's floor of ${floor} MB.`;
  }
  if (host.cpuUtilization === null) return "The machine's CPU use is not measured.";
  if (host.cpuUtilization >= CPU_FULL) return "The machine's CPU is at 95 % or more.";
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

/** H1 and H3: the rules a named machine without facts still meets (design section 7). */
const NO_FACT_RULES: ReadonlySet<HardRule> = new Set(["H1", "H3"]);

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
    weight: UPSTREAM_WEIGHTS[machine.weight ?? "normal"],
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

export const choosePlacement = (input: PlacementInput): PlacementResult => {
  const mode = decodeMode(input.mode);
  if (mode === "off")
    return { kind: "off", reason: "Placement is off: the task runs as upstream." };
  const { task, now } = input;
  const machines = stableOrder(input.machines);

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
      why: `${machine.environmentId} was named and passes the hard rules${noFacts ? " that need no facts (H1, H3)" : ""}.`,
      verdicts,
    };
  }

  if (machines.length === 0) {
    return { kind: "none", mode, reason: "No machine to choose from.", verdicts: [] };
  }

  const failuresOf = machines.map((machine) => checkHardRules(machine, task, now));
  const order = rankWithUpstream(
    machines.filter((_, index) => failuresOf[index]?.length === 0),
    now,
  );
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
      unranked:
        machine.weight === "manual"
          ? "Set to manual: used only when named."
          : "Upstream's score leaves it out (free memory 5 % or less).",
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
    why: `${best} passes every hard rule and ranks first of ${order.length} by upstream's score.`,
    verdicts,
  };
};
