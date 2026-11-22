/**
 * Fork: the pressure level of the capacity rule (README.md in `capacity/`): the raw level of one read
 * and the hysteresis between reads. Pure: the caller keeps the state and passes the clock.
 */
import { isFiniteNumber } from "./capacityFormat.ts";

export type CapacityLevel = "ok" | "elevated" | "severe";

/** Memory stall percentages (PSI some/full avg10), 0-100. */
export interface CapacityPsi {
  readonly someAvg10Pct: number;
  readonly fullAvg10Pct: number;
}

/** The pressure level the caller keeps between reads (hysteresis), one per box it asks for. */
export interface PressureState {
  readonly level: CapacityLevel;
  /** When the readings last became quiet enough to step down; `null` when they are not. */
  readonly calmSinceMs: number | null;
  /**
   * Swap free at the last read (`swapFillingMb` compares the next one with it); `null` or absent:
   * none read (no swap, unreadable, or no read yet), so the swap trend is unknown.
   */
  readonly swapFreeMb?: number | null;
}

export interface PressureRule {
  readonly elevated: {
    readonly psiSomeAvg10Pct: number;
    readonly psiFullAvg10Pct: number;
    readonly availableBelowMarginPercent: number;
    readonly swapFreeBelowPercent: number;
  };
  readonly severe: {
    readonly psiFullAvg10Pct: number;
    readonly availableBelowMarginPercent: number;
  };
  readonly calm: {
    readonly psiSomeAvg10Pct: number;
    readonly psiFullAvg10Pct: number;
    readonly availableAtLeastMarginPercent: number;
  };
  /**
   * Swap is filling when its free MB fell by more than this since the last read: pages are being
   * pushed out now. Swap that is full but not filling (old cold pages) is not a signal.
   */
  readonly swapFillingMb: number;
  readonly elevatedClearSeconds: number;
  readonly severeClearSeconds: number;
}

/** What the level reads: already checked (`"unreadable"` where a reading was broken). */
export interface PressureReadings {
  readonly availableMb: number;
  readonly swap: { readonly totalMb: number; readonly freeMb: number } | null | "unreadable";
  /** The machine's PSI. */
  readonly psi: CapacityPsi | null | "unreadable";
  /** The PSI of the box (cgroup, slice) the asked piece goes into. */
  readonly boxPsi: CapacityPsi | null | "unreadable";
  readonly osLevel: CapacityLevel | null | "unreadable";
}

/** A PSI reading, `null` when there is none, `"unreadable"` when it is broken. */
export function readPsi(value: unknown): CapacityPsi | null | "unreadable" {
  if (value === null || value === undefined) return null;
  if (typeof value !== "object" || Array.isArray(value)) return "unreadable";
  const { someAvg10Pct, fullAvg10Pct } = value as Record<string, unknown>;
  const valid = (pct: unknown): pct is number => isFiniteNumber(pct) && pct >= 0 && pct <= 100;
  return valid(someAvg10Pct) && valid(fullAvg10Pct) ? { someAvg10Pct, fullAvg10Pct } : "unreadable";
}

/**
 * The level of one read. PSI is the worse of machine and box; anything unreadable is elevated. Swap
 * counts by trend, not level: nearly full and filling is elevated, filling is never calm. With no
 * previous swap read the trend is unknown (`trendKnown: false`; see `nextPressure`).
 */
export function rawLevel(
  rule: PressureRule,
  readings: PressureReadings,
  marginMb: number,
  previousSwapFreeMb?: number | null,
): { readonly level: CapacityLevel; readonly calm: boolean; readonly trendKnown: boolean } {
  const { elevated, severe, calm } = rule;
  const { swap, osLevel } = readings;
  const psiReads = [readings.psi, readings.boxPsi].filter(
    (psi): psi is CapacityPsi => typeof psi === "object" && psi !== null,
  );
  const psiUnreadable = readings.psi === "unreadable" || readings.boxPsi === "unreadable";
  const available100 = readings.availableMb * 100;
  if (
    psiReads.some((psi) => psi.fullAvg10Pct >= severe.psiFullAvg10Pct) ||
    available100 < marginMb * severe.availableBelowMarginPercent ||
    osLevel === "severe"
  ) {
    return { level: "severe", calm: false, trendKnown: true };
  }
  const swapRead = typeof swap === "object" && swap !== null ? swap : null;
  const trendKnown = swapRead === null || isFiniteNumber(previousSwapFreeMb);
  const filling =
    swapRead !== null &&
    isFiniteNumber(previousSwapFreeMb) &&
    previousSwapFreeMb - swapRead.freeMb > rule.swapFillingMb;
  if (
    psiUnreadable ||
    swap === "unreadable" ||
    osLevel === "unreadable" ||
    osLevel === "elevated" ||
    psiReads.some(
      (psi) =>
        psi.someAvg10Pct >= elevated.psiSomeAvg10Pct ||
        psi.fullAvg10Pct >= elevated.psiFullAvg10Pct,
    ) ||
    available100 < marginMb * elevated.availableBelowMarginPercent ||
    (filling &&
      swapRead !== null &&
      swapRead.freeMb * 100 < swapRead.totalMb * elevated.swapFreeBelowPercent)
  ) {
    return { level: "elevated", calm: false, trendKnown: true };
  }
  const quiet =
    psiReads.every(
      (psi) => psi.someAvg10Pct < calm.psiSomeAvg10Pct && psi.fullAvg10Pct < calm.psiFullAvg10Pct,
    ) &&
    available100 >= marginMb * calm.availableAtLeastMarginPercent &&
    !filling;
  return { level: "ok", calm: quiet, trendKnown };
}

/**
 * The next level: up at once; down one step after the rule's clear time of calm reads. No previous
 * state: the raw level. No `nowMs` never steps down; a clock that went back restarts the wait. A read
 * with an unknown swap trend starts or keeps the wait but never ends it.
 */
export function nextPressure(
  rule: PressureRule,
  raw: { readonly level: CapacityLevel; readonly calm: boolean; readonly trendKnown?: boolean },
  previous: PressureState | null | undefined,
  nowMs: number | undefined,
): PressureState {
  if (raw.level === "severe") return { level: "severe", calmSinceMs: null };
  if (previous === null || previous === undefined || previous.level === "ok") {
    return { level: raw.level, calmSinceMs: null };
  }
  const stepDown = (to: CapacityLevel, seconds: number): PressureState => {
    if (!isFiniteNumber(nowMs)) return { level: previous.level, calmSinceMs: previous.calmSinceMs };
    const since =
      isFiniteNumber(previous.calmSinceMs) && previous.calmSinceMs <= nowMs
        ? previous.calmSinceMs
        : nowMs;
    return nowMs - since >= seconds * 1000
      ? { level: to, calmSinceMs: null }
      : { level: previous.level, calmSinceMs: since };
  };
  if (previous.level === "severe") return stepDown("elevated", rule.severeClearSeconds);
  if (raw.level === "elevated" || !raw.calm) return { level: "elevated", calmSinceMs: null };
  if (raw.trendKnown === false) {
    const since =
      isFiniteNumber(previous.calmSinceMs) &&
      (!isFiniteNumber(nowMs) || previous.calmSinceMs <= nowMs)
        ? previous.calmSinceMs
        : isFiniteNumber(nowMs)
          ? nowMs
          : null;
    return { level: previous.level, calmSinceMs: since };
  }
  return stepDown("ok", rule.elevatedClearSeconds);
}
