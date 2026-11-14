/**
 * Fork (usage pacing part 4): what happens to a task an agent delegates, from the `UsagePacer`
 * verdict of its account. Unknown is never a stop: an unreadable account would block all work.
 * Nothing is stored: a held task is refused and the caller asks again, so a restart loses nothing.
 */
import type {
  ProviderInstanceId,
  ProviderOptionDescriptor,
  ProviderOptionSelection,
  ServerProvider,
  ServerSettings,
} from "@t3tools/contracts";
import { providerAccountIdentity } from "@t3tools/shared/usageLimits";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";

import {
  type InstancePace,
  type PaceUnknownReason,
  RESERVE_RELEASE_MS,
  type WindowPace,
} from "./UsagePacer.ts";

const MINUTE_MS = 60 * 1000;
/** Upstream's band around the even pace (`paceOfShares`): over it by more than this is ahead. */
const PACE_BAND_POINTS = 5;

/** How one account stands, as the gate reads a pace verdict. */
export type PaceStanding =
  /** On or behind pace. `headroomPercent`: points left to its target in its tightest window. */
  | { readonly kind: "room"; readonly headroomPercent: number }
  /** The owner's setting says this account has no limits. */
  | { readonly kind: "noLimits" }
  | { readonly kind: "unknown"; readonly reason: PaceUnknownReason }
  | {
      readonly kind: "ahead";
      /** In the owner's reserve, or full. Not hard: only over the even pace. */
      readonly hard: boolean;
      readonly windows: ReadonlyArray<WindowPace>;
      /** The earliest moment every ahead window can clear with nothing more spent; null if unknown. */
      readonly notBeforeMs: number | null;
    };

const isAhead = (window: WindowPace) => window.verdict === "ahead" && window.usedPercent !== null;
const ceilingOf = (window: WindowPace) => 100 - (window.effectiveReservePercent ?? 0);

/**
 * The earliest moment one ahead window is no longer ahead if nothing more is spent, by the pacer's
 * own two lines: the reserve (released over the window's last hours) and the even pace plus its
 * band. Both only rise with time, so the later of the two is exact, and the reset bounds both.
 */
function clearsAtMs(window: WindowPace, pace: InstancePace, nowMs: number): number | null {
  const resetMs = window.resetsAt === null ? Number.NaN : Date.parse(window.resetsAt);
  const used = window.usedPercent;
  if (!Number.isFinite(resetMs) || used === null) return null;
  let clearsAt = nowMs;
  if (used >= ceilingOf(window)) {
    const room = 100 - used;
    clearsAt =
      room <= 0 || pace.reservePercent <= 0
        ? resetMs
        : resetMs - (RESERVE_RELEASE_MS * room) / pace.reservePercent;
  }
  const even = window.pacePercent;
  if (!pace.burnFirst && even !== null && used - even > PACE_BAND_POINTS) {
    // The window's length, from how much of it has passed.
    const lengthMs = even >= 100 ? 0 : (resetMs - nowMs) / (1 - even / 100);
    clearsAt = Math.max(clearsAt, resetMs - lengthMs * (1 - (used - PACE_BAND_POINTS) / 100));
  }
  // The lines are met exactly at that moment and cleared after it: the next whole minute.
  return Math.min(resetMs, (Math.floor(clearsAt / MINUTE_MS) + 1) * MINUTE_MS);
}

export function standingOf(pace: InstancePace, nowMs: number): PaceStanding {
  if (pace.verdict === "unknown") {
    return pace.unknownReason === "noLimits"
      ? { kind: "noLimits" }
      : { kind: "unknown", reason: pace.unknownReason ?? "noReading" };
  }
  if (pace.verdict === "ahead") {
    const windows = pace.windows.filter(isAhead);
    const clears = windows.map((window) => clearsAtMs(window, pace, nowMs));
    return {
      kind: "ahead",
      hard: windows.some((window) => window.usedPercent! >= ceilingOf(window)),
      windows,
      notBeforeMs:
        clears.length === 0 || clears.includes(null) ? null : Math.max(...(clears as number[])),
    };
  }
  const rooms = pace.windows.flatMap((window) =>
    window.verdict !== "unknown" && window.usedPercent !== null && window.targetPercent !== null
      ? [window.targetPercent - window.usedPercent]
      : [],
  );
  return { kind: "room", headroomPercent: rooms.length === 0 ? 0 : Math.min(...rooms) };
}

/** The same login counts once: two instances of one account share one set of limits. */
function sameAccount(left: ServerProvider, right: ServerProvider): boolean {
  const a = providerAccountIdentity(left);
  const b = providerAccountIdentity(right);
  if (a === null || b === null || a.base !== b.base) return false;
  return a.workspaceId === undefined || b.workspaceId === undefined
    ? true
    : a.workspaceId === b.workspaceId;
}

export interface SwitchCandidate {
  readonly instanceId: ProviderInstanceId;
  readonly headroomPercent: number;
}

/**
 * Accounts a task may move to, most room first: the same provider, another account, and a pace that
 * is known to have room. So never one that is ahead, unknown or marked `noLimits` (work that runs
 * on a subscription is not moved to a pay-per-use key by itself).
 */
export function switchCandidates(input: {
  readonly current: ServerProvider;
  readonly providers: ReadonlyArray<ServerProvider>;
  readonly paces: ReadonlyArray<InstancePace>;
  readonly nowMs: number;
}): ReadonlyArray<SwitchCandidate> {
  return input.providers
    .flatMap((provider) => {
      if (provider.instanceId === input.current.instanceId) return [];
      if (provider.driver !== input.current.driver || sameAccount(provider, input.current)) {
        return [];
      }
      const pace = input.paces.find((entry) => entry.instanceId === provider.instanceId);
      const standing = pace === undefined ? undefined : standingOf(pace, input.nowMs);
      return standing?.kind === "room"
        ? [{ instanceId: provider.instanceId, headroomPercent: standing.headroomPercent }]
        : [];
    })
    .toSorted((left, right) => right.headroomPercent - left.headroomPercent);
}

/** Ids a model's effort control goes by; a select its agent marks `role: "reasoning"` counts too. */
const EFFORT_OPTION_IDS: ReadonlySet<string> = new Set([
  "effort",
  "reasoningEffort",
  "reasoning_effort",
  "reasoning",
  "thinking_level",
]);

export interface EffortStep {
  readonly optionId: string;
  readonly from: string;
  readonly to: string;
}

/**
 * The effort the owner's `usagePacingLowerEffort` gives this task, or undefined when the model has
 * no effort control, the task's level is not known or not listed, or the model lacks the listed
 * level. `from === to` means the owner lets this level run as it is.
 */
export function effortStep(input: {
  readonly descriptors: ReadonlyArray<ProviderOptionDescriptor> | undefined;
  readonly options: ReadonlyArray<ProviderOptionSelection> | undefined;
  readonly lowerEffort: ServerSettings["usagePacingLowerEffort"];
}): EffortStep | undefined {
  for (const descriptor of input.descriptors ?? []) {
    if (descriptor.type !== "select") continue;
    if (descriptor.role !== "reasoning" && !EFFORT_OPTION_IDS.has(descriptor.id)) continue;
    const selected = input.options?.find((option) => option.id === descriptor.id)?.value;
    const from =
      typeof selected === "string"
        ? selected
        : (descriptor.currentValue ??
          descriptor.options.find((choice) => choice.isDefault === true)?.id);
    if (from === undefined || !Object.hasOwn(input.lowerEffort, from)) return undefined;
    const to = input.lowerEffort[from]!;
    return descriptor.options.some((choice) => choice.id === to)
      ? { optionId: descriptor.id, from, to }
      : undefined;
  }
  return undefined;
}

const percent = (value: number) => `${Math.round(value)}%`;

function describeWindows(windows: ReadonlyArray<WindowPace>): string {
  return windows
    .map((window) => {
      const target =
        window.targetPercent === null ? "" : `, target ${percent(window.targetPercent)}`;
      return `${window.label} ${percent(window.usedPercent ?? 0)} used${target}`;
    })
    .join("; ");
}

export type GateOutcome<Target> =
  /** Run the task as asked. */
  | { readonly action: "pass"; readonly note?: string }
  /** Run it on another account of the same provider. */
  | { readonly action: "switch"; readonly target: Target; readonly note: string }
  /** Run it at the effort the owner allows while ahead of pace. */
  | { readonly action: "lowerEffort"; readonly step: EffortStep; readonly note: string }
  /** Do not start it now. */
  | { readonly action: "hold"; readonly message: string; readonly notBefore: string | null };

export interface GateInput<Target, R> {
  readonly mode: ServerSettings["usagePacingGate"];
  readonly lowerEffort: ServerSettings["usagePacingLowerEffort"];
  readonly nowMs: number;
  /** The instance the task would run on. */
  readonly instanceId: ProviderInstanceId;
  /** The caller named this instance itself: it is never swapped for another. */
  readonly pinned: boolean;
  readonly providers: ReadonlyArray<ServerProvider>;
  readonly descriptors: ReadonlyArray<ProviderOptionDescriptor> | undefined;
  readonly options: ReadonlyArray<ProviderOptionSelection> | undefined;
  /** The pace of one instance; undefined when it cannot be read. */
  readonly paceOf: (
    instanceId: ProviderInstanceId,
  ) => Effect.Effect<InstancePace | undefined, never, R>;
  /** The same task on another instance, or undefined when it cannot run there. */
  readonly targetOn: (
    instanceId: ProviderInstanceId,
  ) => Effect.Effect<Target | undefined, never, R>;
}

/**
 * The gate. `off` reads nothing. `advise` works out what `enforce` would do and only says it: its
 * outcome is always `pass`. Every note and message starts with "Usage pacing".
 */
export const decide = Effect.fn("UsagePacingGate.decide")(function* <Target, R>(
  input: GateInput<Target, R>,
): Effect.fn.Return<GateOutcome<Target>, never, R> {
  if (input.mode === "off") return { action: "pass" };
  const advising = input.mode === "advise";
  const lead = advising ? "Usage pacing (advice only, the task runs as asked)" : "Usage pacing";
  const from = input.instanceId;
  const pace = yield* input.paceOf(from);
  if (pace === undefined) {
    return {
      action: "pass",
      note: `${lead}: the usage pace of ${from} could not be read, so this task was not paced.`,
    };
  }
  const standing = standingOf(pace, input.nowMs);
  if (standing.kind === "room" || standing.kind === "noLimits") return { action: "pass" };
  if (standing.kind === "unknown") {
    return {
      action: "pass",
      note: `${lead}: the usage pace of ${from} is not known (${standing.reason}), so this task was not paced.`,
    };
  }

  const ahead = `${from} is ahead of its usage pace (${describeWindows(standing.windows)})`;
  const current = input.providers.find((provider) => provider.instanceId === from);
  let roomOn: ProviderInstanceId | undefined;
  if (current !== undefined) {
    const others = input.providers.filter(
      (provider) => provider.instanceId !== from && provider.driver === current.driver,
    );
    const paces = yield* Effect.forEach(others, (provider) => input.paceOf(provider.instanceId));
    const candidates = switchCandidates({
      current,
      providers: others,
      paces: paces.filter((entry) => entry !== undefined),
      nowMs: input.nowMs,
    });
    for (const candidate of candidates) {
      const target = yield* input.targetOn(candidate.instanceId);
      if (target === undefined) continue;
      roomOn = candidate.instanceId;
      if (input.pinned) break;
      return advising
        ? {
            action: "pass",
            note: `${lead}: ${ahead}. ${roomOn} has room; "enforce" would run this task there.`,
          }
        : {
            action: "switch",
            target,
            note: `${lead}: ${ahead}, so this task runs on ${roomOn}, which has room.`,
          };
    }
  }

  const noRoom = "no other account of this provider has room";
  // A named instance with room elsewhere is the caller's to change, not an effort to lower.
  const step =
    standing.hard || roomOn !== undefined
      ? undefined
      : effortStep({
          descriptors: input.descriptors,
          options: input.options,
          lowerEffort: input.lowerEffort,
        });
  if (step !== undefined && step.from === step.to) {
    return {
      action: "pass",
      note: `${lead}: ${ahead} and ${noRoom}. The usagePacingLowerEffort setting lets ${step.optionId} ${step.from} run.`,
    };
  }
  if (step !== undefined) {
    const change = `${step.optionId} ${step.to} instead of ${step.from} (usagePacingLowerEffort setting)`;
    return advising
      ? {
          action: "pass",
          note: `${lead}: ${ahead} and ${noRoom}. "enforce" would run this task at ${change}.`,
        }
      : {
          action: "lowerEffort",
          step,
          note: `${lead}: ${ahead} and ${noRoom}, so this task runs at ${change}.`,
        };
  }

  const notBefore =
    standing.notBeforeMs === null
      ? null
      : DateTime.formatIso(DateTime.makeUnsafe(standing.notBeforeMs));
  const when =
    notBefore === null
      ? "Its reset time is not known; ask again later."
      : `With nothing more spent it clears at ${notBefore} at the earliest; ask again then.`;
  const elsewhere =
    roomOn === undefined
      ? `${noRoom[0]!.toUpperCase()}${noRoom.slice(1)}.`
      : `${roomOn} has room: pass it as target.providerInstanceId, or name no instance and T3 picks it.`;
  return advising
    ? {
        action: "pass",
        note: `${lead}: ${ahead}. ${elsewhere} "enforce" would hold this task. ${when}`,
      }
    : {
        action: "hold",
        message: `Usage pacing held this task: ${ahead}. ${elsewhere} ${when}`,
        notBefore,
      };
});
