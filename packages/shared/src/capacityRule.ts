/**
 * Fork: the capacity rule of dynamic slots. One budget per machine; pure (no I/O, no clock). Numbers:
 * `capacity/capacity-rule.json`; every evaluator passes `capacity/capacity-vectors.json` (README.md
 * beside them). `...Mb` is whole MB (1048576 bytes), `...CentiCores` hundredths of a core.
 */
import { capacityFormatErrors, isFiniteNumber, isPlainObject } from "./capacityFormat.ts";
import {
  type CapacityLevel,
  type CapacityPsi,
  nextPressure,
  type PressureReadings,
  type PressureRule,
  type PressureState,
  rawLevel,
  readPsi,
} from "./capacityPressure.ts";
import ruleJson from "./capacity/capacity-rule.json" with { type: "json" };

export type { CapacityLevel, CapacityPsi, PressureState } from "./capacityPressure.ts";
export type CapacityGroup = "agent" | "job";
export type CapacityReason =
  | "fits"
  | "floor"
  | "unreadable"
  | "ceiling"
  | "severe"
  | "kind-limit"
  | "elevated"
  | "writers"
  | "cores"
  | "budget";

export interface CapacityKindSpec {
  readonly group: CapacityGroup;
  readonly startMb: number;
  readonly startCentiCores: number;
  readonly maxAtOnce?: number;
  /** The cost is `startMb` plus the size the piece declares (a test server's heap). */
  readonly plusDeclared?: boolean;
  readonly declaredFallbackMb?: number;
}

export type CapacityPerGroup = { readonly agent: number; readonly job: number };

export interface CapacityRule {
  readonly format: "capacity-rule/1";
  readonly margin: {
    readonly minMb: number;
    readonly totalPercent: number;
    readonly noSwapOrNoCapsPercent: number;
  };
  readonly readingBounds: {
    readonly minTotalMb: number;
    readonly maxTotalMb: number;
    readonly maxCpuCores: number;
  };
  readonly kinds: Readonly<Record<string, CapacityKindSpec>>;
  readonly unknownKinds: {
    readonly jobPrefix: string;
    readonly job: string;
    readonly other: string;
  };
  readonly learnedBounds: { readonly minPercent: number; readonly maxPercent: number };
  readonly jobCoresPercent: number;
  readonly writers: {
    readonly kind: string;
    readonly buildKind: string;
    readonly perBuildThatFits: number;
    readonly min: number;
  };
  readonly floors: CapacityPerGroup;
  readonly ceilings: CapacityPerGroup;
  readonly pressure: PressureRule;
  /** The CPU brake for one more job; read by the host kit's evaluator only (README.md "CPU brake"), not by this one. */
  readonly cpu?: { readonly pressureSomeAvg10Pct: number; readonly loadPerCorePercent: number };
}

/** The rule from parsed JSON, or the reasons it cannot be used (shape, then cross-references). */
export function parseCapacityRule(
  value: unknown,
):
  | { readonly ok: true; readonly rule: CapacityRule }
  | { readonly ok: false; readonly errors: ReadonlyArray<string> } {
  const errors = [...capacityFormatErrors(value, "rule")];
  if (errors.length > 0) return { ok: false, errors };
  const parsed = value as CapacityRule;
  // Notes (`//` keys) are skipped by the format check; they are never kinds.
  const rule: CapacityRule = {
    ...parsed,
    kinds: Object.fromEntries(
      Object.entries(parsed.kinds).filter(([name]) => !name.startsWith("//")),
    ),
  };
  const needKind = (name: string, group: CapacityGroup, where: string) => {
    if (rule.kinds[name]?.group !== group)
      errors.push(`rule.${where}: "${name}" is not a ${group} kind`);
  };
  needKind(rule.unknownKinds.job, "job", "unknownKinds.job");
  needKind(rule.unknownKinds.other, "agent", "unknownKinds.other");
  needKind(rule.writers.kind, "agent", "writers.kind");
  needKind(rule.writers.buildKind, "job", "writers.buildKind");
  for (const [name, spec] of Object.entries(rule.kinds)) {
    if (spec.startMb < 1) errors.push(`rule.kinds.${name}.startMb: must be at least 1`);
  }
  if (rule.learnedBounds.minPercent > rule.learnedBounds.maxPercent) {
    errors.push("rule.learnedBounds: minPercent is above maxPercent");
  }
  if (rule.readingBounds.minTotalMb > rule.readingBounds.maxTotalMb) {
    errors.push("rule.readingBounds: minTotalMb is above maxTotalMb");
  }
  return errors.length > 0 ? { ok: false, errors } : { ok: true, rule };
}

const shipped = parseCapacityRule(ruleJson);
if (!shipped.ok) throw new Error(`capacity-rule.json: ${shipped.errors.join("; ")}`);
/** The rule this build ships (checked when the module loads). */
export const CAPACITY_RULE: CapacityRule = shipped.rule;

/** A live piece. `demandMb`: anon + shmem + swap now; `null` while not measured (owes its full cost). */
export interface CapacityPiece {
  readonly kind: string;
  readonly demandMb: number | null;
  readonly declaredMb?: number;
}

/** MB held for a folder or a name; `useMb` is what the holder uses now (`null`: unknown, holds all). */
export interface CapacityReservation {
  readonly mb: number;
  readonly useMb: number | null;
}

/**
 * What the machine said. A missing or broken `totalMb`, `availableMb`, `cpuCores` or `pieces` means
 * nothing can be admitted ("unreadable"). `swap`/`psi`/`boxPsi`/`osLevel`: `null` or absent when there
 * is none, `"unreadable"` when there is one that could not be read (level at least "elevated").
 */
export interface CapacityFacts {
  readonly totalMb?: number | null;
  readonly availableMb?: number | null;
  readonly cpuCores?: number | null;
  /** Each piece runs under its own memory cap (full tier). Without it the margin is wider. */
  readonly pieceCaps: boolean;
  readonly swap?: { readonly totalMb: number; readonly freeMb: number } | null | "unreadable";
  /** The machine's memory PSI (`/proc/pressure/memory`). */
  readonly psi?: CapacityPsi | null | "unreadable";
  /** The memory PSI of the box (cgroup, slice) the asked piece goes into; absent without boxes. */
  readonly boxPsi?: CapacityPsi | null | "unreadable";
  readonly osLevel?: CapacityLevel | null | "unreadable";
  readonly pieces?: ReadonlyArray<CapacityPiece> | null;
  readonly reservations?: ReadonlyArray<CapacityReservation> | null;
}

/** Learned costs by kind (later PR). Clamped to the rule's bounds around the start cost. */
export type CapacityLearned = Readonly<
  Record<string, { readonly mb: number; readonly centiCores?: number }>
>;

interface Readings extends PressureReadings {
  readonly totalMb: number;
  readonly centiCores: number;
  readonly pieces: ReadonlyArray<{
    readonly kind: string;
    readonly demandMb: number;
    readonly declaredMb: unknown;
  }>;
  readonly demandSumMb: number;
  readonly reservedMb: number;
}

/** Whole MB, rounded down (a reading never claims more than it saw). */
const floorMb = (value: number): number => Math.floor(value);
const ceilDiv = (numerator: number, denominator: number): number =>
  Math.ceil(numerator / denominator);

function readFacts(rule: CapacityRule, facts: CapacityFacts): Readings | null {
  const bounds = rule.readingBounds;
  const { totalMb, availableMb, cpuCores } = facts;
  if (!isFiniteNumber(totalMb) || !isFiniteNumber(availableMb) || !isFiniteNumber(cpuCores))
    return null;
  const total = floorMb(totalMb);
  // Bounds catch unit mix-ups: bytes or kB passed as MB (any machine of 1 GB or more read in kB is
  // above maxTotalMb), or GB passed as MB (below minTotalMb).
  if (total < bounds.minTotalMb || total > bounds.maxTotalMb) return null;
  if (availableMb < 0 || availableMb > totalMb) return null;
  if (cpuCores <= 0 || cpuCores > bounds.maxCpuCores) return null;
  if (!Array.isArray(facts.pieces)) return null;
  const pieces = facts.pieces.map((piece: unknown) => {
    const record = isPlainObject(piece) ? piece : {};
    const demand = record.demandMb;
    return {
      kind: typeof record.kind === "string" ? record.kind : "",
      // An impossible demand (negative, larger than the machine) is not trusted: it owes in full.
      demandMb: isFiniteNumber(demand) && demand >= 0 && demand <= totalMb ? floorMb(demand) : 0,
      declaredMb: record.declaredMb,
    };
  });
  const reservations: unknown = facts.reservations ?? [];
  if (!Array.isArray(reservations)) return null;
  let reservedMb = 0;
  for (const reservation of reservations as Array<unknown>) {
    const record = isPlainObject(reservation) ? reservation : {};
    // A size above the largest machine is a unit slip: it holds that much (keeps sums exact).
    const mb =
      isFiniteNumber(record.mb) && record.mb > 0
        ? Math.min(Math.ceil(record.mb), bounds.maxTotalMb)
        : 0;
    const use = record.useMb;
    const useMb = isFiniteNumber(use) && use >= 0 && use <= totalMb ? floorMb(use) : 0;
    reservedMb += Math.max(0, mb - useMb);
  }
  const swap: Readings["swap"] = (() => {
    const value: unknown = facts.swap;
    if (value === null || value === undefined) return null;
    if (!isPlainObject(value)) return "unreadable";
    const { totalMb: swapTotal, freeMb: swapFree } = value;
    if (!isFiniteNumber(swapTotal) || !isFiniteNumber(swapFree)) return "unreadable";
    if (swapTotal < 0 || swapFree < 0 || swapFree > swapTotal || swapTotal > bounds.maxTotalMb)
      return "unreadable";
    return swapTotal === 0 ? null : { totalMb: floorMb(swapTotal), freeMb: floorMb(swapFree) };
  })();
  const os: unknown = facts.osLevel;
  const osLevel: Readings["osLevel"] =
    os === null || os === undefined
      ? null
      : os === "ok" || os === "elevated" || os === "severe"
        ? os
        : "unreadable";
  return {
    totalMb: total,
    availableMb: floorMb(availableMb),
    centiCores: Math.floor(cpuCores * 100),
    pieces,
    demandSumMb: pieces.reduce((sum, piece) => sum + piece.demandMb, 0),
    reservedMb,
    swap,
    psi: readPsi(facts.psi),
    boxPsi: readPsi(facts.boxPsi),
    osLevel,
  };
}

/** The kind a name stands for: itself when known, else the rule's expensive default for its group. */
function capacityKind(rule: CapacityRule, kind: string): string {
  if (Object.hasOwn(rule.kinds, kind) && !kind.startsWith("//")) return kind;
  return kind.startsWith(rule.unknownKinds.jobPrefix)
    ? rule.unknownKinds.job
    : rule.unknownKinds.other;
}

interface Cost {
  readonly mb: number;
  readonly centiCores: number;
  /** The cost before learning (still with the declared size): what a floor may count. */
  readonly unlearnedMb: number;
}

function costOf(
  rule: CapacityRule,
  kind: string,
  learned: CapacityLearned | undefined,
  declaredMb: unknown,
): Cost {
  const spec = rule.kinds[kind]!;
  const sane =
    isFiniteNumber(declaredMb) && declaredMb > 0 && declaredMb <= rule.readingBounds.maxTotalMb
      ? Math.ceil(declaredMb)
      : null;
  if (spec.plusDeclared === true) {
    const mb = spec.startMb + (sane ?? spec.declaredFallbackMb ?? 0);
    return { mb, centiCores: spec.startCentiCores, unlearnedMb: mb };
  }
  const { minPercent, maxPercent } = rule.learnedBounds;
  const clamp = (value: unknown, start: number): number =>
    isFiniteNumber(value) && value > 0
      ? Math.min(
          Math.max(Math.ceil(value), ceilDiv(start * minPercent, 100)),
          Math.floor((start * maxPercent) / 100),
        )
      : start;
  const own = learned !== undefined && Object.hasOwn(learned, kind) ? learned[kind] : undefined;
  // A declared size is a floor of any other kind's cost: a piece that says it needs more (heavy's
  // reservation for a job: its --mb or its kind's learned peak) owes that, never only its kind's cost.
  const floor = sane ?? 0;
  return {
    mb: Math.max(clamp(own?.mb, spec.startMb), floor),
    centiCores: clamp(own?.centiCores, spec.startCentiCores),
    unlearnedMb: Math.max(spec.startMb, floor),
  };
}

/** `max(minMb, totalPercent of total)`, x `noSwapOrNoCapsPercent` without swap or per-piece caps. */
function marginOf(rule: CapacityRule, readings: Readings, pieceCaps: boolean): number {
  const base100 = Math.max(rule.margin.minMb * 100, readings.totalMb * rule.margin.totalPercent);
  const wide = !pieceCaps || readings.swap === null || readings.swap === "unreadable";
  return wide
    ? ceilDiv(base100 * rule.margin.noSwapOrNoCapsPercent, 10_000)
    : ceilDiv(base100, 100);
}

/** Unreadable readings: at least "elevated" ("severe" stays). */
function unreadableLevel(previous: PressureState | null | undefined): {
  readonly level: CapacityLevel;
  readonly pressure: PressureState;
} {
  const level: CapacityLevel = previous?.level === "severe" ? "severe" : "elevated";
  return { level, pressure: { level, calmSinceMs: null } };
}

function pressureOf(
  rule: CapacityRule,
  readings: Readings,
  marginMb: number,
  input: { readonly previous?: PressureState | null; readonly nowMs?: number },
): PressureState {
  const next = nextPressure(
    rule.pressure,
    rawLevel(rule.pressure, readings, marginMb, input.previous?.swapFreeMb),
    input.previous,
    input.nowMs,
  );
  // The swap free of this read, for the next one's trend (`swapFillingMb`).
  const swap = readings.swap;
  return { ...next, swapFreeMb: typeof swap === "object" && swap !== null ? swap.freeMb : null };
}

/**
 * The pressure level alone, exactly as `capacityDecision` gives it (a status line, the brake).
 * The caller keeps `pressure` for the next read and passes the clock; nothing is kept here.
 */
export function capacityLevel(
  rule: CapacityRule,
  input: {
    readonly facts: CapacityFacts;
    readonly previous?: PressureState | null;
    readonly nowMs?: number;
  },
): {
  readonly level: CapacityLevel;
  readonly pressure: PressureState;
  readonly marginMb: number | null;
} {
  const readings = readFacts(rule, input.facts);
  if (readings === null) return { ...unreadableLevel(input.previous), marginMb: null };
  const marginMb = marginOf(rule, readings, input.facts.pieceCaps === true);
  const pressure = pressureOf(rule, readings, marginMb, input);
  return { level: pressure.level, pressure, marginMb };
}

export interface CapacityInput {
  readonly facts: CapacityFacts;
  readonly ask: { readonly kind: string; readonly declaredMb?: number };
  readonly learned?: CapacityLearned;
  readonly previous?: PressureState | null;
  readonly nowMs?: number;
}

export interface CapacityDecision {
  readonly admit: boolean;
  readonly reason: CapacityReason;
  /** The kind the ask was counted as. */
  readonly kind: string;
  readonly level: CapacityLevel;
  /** Keep this for the next read. */
  readonly pressure: PressureState;
  /** `null` when the readings were unreadable. Can be negative. */
  readonly freeMb: number | null;
  readonly marginMb: number | null;
  readonly owedMb: number | null;
  readonly reservedMb: number | null;
  readonly costMb: number;
}

/**
 * Admit one piece of `ask.kind` or not, and why. Absolute refusals first (unreadable, ceiling,
 * severe, the kind's own limit); then the rule (level ok, writers, job cores, budget). A refused
 * piece still starts under its group's floor when that keeps the machine above the severe line;
 * the floor never passes a ceiling, "severe" or an unreadable reading.
 */
export function capacityDecision(rule: CapacityRule, input: CapacityInput): CapacityDecision {
  const kind = capacityKind(rule, input.ask.kind);
  const spec = rule.kinds[kind]!;
  const cost = costOf(rule, kind, input.learned, input.ask.declaredMb);
  const readings = readFacts(rule, input.facts);
  if (readings === null) {
    const { level, pressure } = unreadableLevel(input.previous);
    return {
      admit: false,
      reason: "unreadable",
      kind,
      level,
      pressure,
      freeMb: null,
      marginMb: null,
      owedMb: null,
      reservedMb: null,
      costMb: cost.mb,
    };
  }
  const marginMb = marginOf(rule, readings, input.facts.pieceCaps === true);
  const live = readings.pieces.map((piece) => {
    const pieceKind = capacityKind(rule, piece.kind);
    const pieceCost = costOf(rule, pieceKind, input.learned, piece.declaredMb);
    return {
      kind: pieceKind,
      group: rule.kinds[pieceKind]!.group,
      cost: pieceCost,
      demandMb: piece.demandMb,
    };
  });
  const owedMb = live.reduce((sum, piece) => sum + Math.max(0, piece.cost.mb - piece.demandMb), 0);
  const freeMb = readings.availableMb - marginMb - owedMb - readings.reservedMb;
  const pressure = pressureOf(rule, readings, marginMb, input);
  const result = (admit: boolean, reason: CapacityReason): CapacityDecision => ({
    admit,
    reason,
    kind,
    level: pressure.level,
    pressure,
    freeMb,
    marginMb,
    owedMb,
    reservedMb: readings.reservedMb,
    costMb: cost.mb,
  });

  const groupLive = live.filter((piece) => piece.group === spec.group).length;
  if (groupLive >= rule.ceilings[spec.group]) return result(false, "ceiling");
  if (pressure.level === "severe") return result(false, "severe");
  if (
    spec.maxAtOnce !== undefined &&
    live.filter((piece) => piece.kind === kind).length >= spec.maxAtOnce
  ) {
    return result(false, "kind-limit");
  }

  const coreLimit = Math.floor((readings.centiCores * rule.jobCoresPercent) / 100);
  const fail = ((): CapacityReason | null => {
    if (pressure.level !== "ok") return "elevated";
    if (kind === rule.writers.kind) {
      const build = costOf(rule, rule.writers.buildKind, input.learned, undefined);
      const idleFreeMb =
        readings.availableMb + readings.demandSumMb - marginMb - readings.reservedMb;
      const byMemory = Math.floor(Math.max(0, idleFreeMb) / build.mb);
      const byCores = build.centiCores > 0 ? Math.floor(coreLimit / build.centiCores) : byMemory;
      const writerCap = Math.max(
        rule.writers.min,
        rule.writers.perBuildThatFits * Math.min(byMemory, byCores),
      );
      if (live.filter((piece) => piece.kind === kind).length >= writerCap) return "writers";
    }
    if (spec.group === "job") {
      const jobCores = live.reduce(
        (sum, piece) => sum + (piece.group === "job" ? piece.cost.centiCores : 0),
        0,
      );
      if (jobCores + cost.centiCores > coreLimit) return "cores";
    }
    return cost.mb <= freeMb ? null : "budget";
  })();
  if (fail === null) return result(true, "fits");

  // The floor: a badly learned cost or a full budget never locks the machine, but the floor never
  // takes it below the severe line either (on a small machine physical memory wins).
  const floorCostMb = Math.min(cost.mb, cost.unlearnedMb);
  const afterMb = readings.availableMb - owedMb - floorCostMb;
  if (
    groupLive < rule.floors[spec.group] &&
    afterMb * 100 >= marginMb * rule.pressure.severe.availableBelowMarginPercent
  ) {
    return result(true, "floor");
  }
  return result(false, fail);
}

export interface CapacityFillStep {
  readonly kind: string;
  readonly count: number;
  readonly declaredMb?: number;
}

/**
 * Ask for pieces one by one, as at second zero: each admitted piece joins the live list with no
 * demand yet. Per step: how many were admitted and the reason the first refused one gave (`null`
 * when all were). A step stops at its first refusal (nothing else changes).
 */
export function capacityFill(
  rule: CapacityRule,
  input: Omit<CapacityInput, "ask">,
  steps: ReadonlyArray<CapacityFillStep>,
): {
  readonly admitted: ReadonlyArray<number>;
  readonly stoppedBy: ReadonlyArray<CapacityReason | null>;
} {
  const pieces: Array<CapacityPiece> = Array.isArray(input.facts.pieces)
    ? [...input.facts.pieces]
    : [];
  const piecesReadable = Array.isArray(input.facts.pieces);
  const admitted: Array<number> = [];
  const stoppedBy: Array<CapacityReason | null> = [];
  for (const step of steps) {
    let count = 0;
    let stop: CapacityReason | null = null;
    for (let index = 0; index < step.count; index += 1) {
      const ask =
        step.declaredMb === undefined
          ? { kind: step.kind }
          : { kind: step.kind, declaredMb: step.declaredMb };
      const decision = capacityDecision(rule, {
        ...input,
        facts: piecesReadable ? { ...input.facts, pieces } : input.facts,
        ask,
      });
      if (!decision.admit) {
        stop = decision.reason;
        break;
      }
      count += 1;
      pieces.push({ ...ask, demandMb: null });
    }
    admitted.push(count);
    stoppedBy.push(stop);
  }
  return { admitted, stoppedBy };
}
