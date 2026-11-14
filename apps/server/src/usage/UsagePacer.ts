/**
 * Fork (usage pacing part 3): how far each provider instance's usage runs ahead of an even pace,
 * from its live snapshot and the readings history (`UsageReadingsHistory`). Reads only; nothing
 * acts on a verdict yet. Anything it cannot know is `unknown`, never free.
 */
import {
  DEFAULT_USAGE_PACING_INSTANCE_SETTINGS,
  type ProviderInstanceId,
  type ServerProvider,
  type ServerProviderUsageWindow,
  type ServerSettings,
  type ServerSettingsError,
  type UsagePacingInstanceSettings,
} from "@t3tools/contracts";
import { elapsedShare, paceOfShares } from "@t3tools/shared/usageLimits";
import * as Clock from "effect/Clock";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import { ProviderRegistry } from "../provider/ProviderRegistry.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import { effectiveIntervalMs, oneFailureGapMs } from "./HeadlessUsageRefresher.ts";
import {
  accountOf,
  isAccountSwap,
  type UsageHistoryUnreadable,
  type UsageReading,
  UsageReadingsHistory,
} from "./UsageReadingsHistory.ts";

const MINUTE_MS = 60 * 1000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/** The owner's reserve is released over a window's last this-many hours. */
const RESERVE_RELEASE_MS = 12 * HOUR_MS;
/** Never trust a value confirmed longer ago than this (the history's heartbeat; 6 UI checks). */
const STALE_FLOOR_MS = 30 * MINUTE_MS;
/** A value confirmed this far ahead of the clock came from a clock that was ahead. */
const FUTURE_TOLERANCE_MS = 5 * MINUTE_MS;
/** History read back this far: a weekly window plus a day, for resets and missing windows. */
const LOOKBACK_MS = 8 * DAY_MS;
/** A meter that falls by this many points started a new window (less is rounding noise). */
const RESET_DROP_POINTS = 5;
/** A reset time that jumps later by this much started a new window (less is jitter). */
const RESET_JUMP_MS = 10 * MINUTE_MS;
/** Burn rate over this recent span... */
const DEFAULT_BURN_SPAN_MS = HOUR_MS;
/** ...and only when the confirmed values in it cover at least this long. */
const MIN_BURN_COVER_MS = 10 * MINUTE_MS;

/** `ahead`: spending faster than the target allows. `behind`: room to spend. */
export type PaceVerdict = "ahead" | "onPace" | "behind" | "unknown";

export type PaceUnknownReason =
  /** The instance is not configured (any more). */
  | "noInstance"
  /**
   * Confirmed: the provider marks the account `unsupported` (upstream's only marker for an account
   * without usage limits). Nothing else ever gives this reason, whatever the history holds.
   */
  | "noLimits"
  /** Not known: no usage was read yet, or the read failed with nothing known before. */
  | "noReading"
  /**
   * Not known: a read worked but returned no windows (a reply shape the builders do not know), with
   * no window known in the current series. Never free: a new instance, an account swap or a clock
   * step back starts with nothing known, and the account's limits may still be near full.
   */
  | "emptyRead"
  /** Its history file exists but cannot be read. */
  | "historyUnreadable"
  /** Seen in the recent history, absent now, and no non-empty full read since confirmed it gone. */
  | "missing"
  /** Its value was last confirmed longer ago than the stale bound. */
  | "stale"
  /**
   * Its value was confirmed by a reading stamped ahead of the clock, or the live reading is
   * stamped before the history's newest (the clock stepped back): neither is trusted alone.
   */
  | "aheadOfClock"
  /** Its reset time passed: the value belongs to the window before. */
  | "resetPassed"
  /** No reset time or no window length, so no pace or reserve can be placed. */
  | "noResetClock"
  /** The live snapshot reports two windows with this id. */
  | "ambiguous";

export interface WindowPace {
  readonly id: string;
  readonly kind: ServerProviderUsageWindow["kind"];
  readonly label: string;
  readonly verdict: PaceVerdict;
  readonly unknownReason?: PaceUnknownReason;
  readonly usedPercent: number | null;
  /** Even pace: the share of the window that has passed, in percent. */
  readonly pacePercent: number | null;
  /** reserve × min(1, hours left / 12). */
  readonly effectiveReservePercent: number | null;
  /** What `usedPercent` is held to: min(pace, 100 − reserve), or 100 − reserve to burn first. */
  readonly targetPercent: number | null;
  /** Points per hour over the recent span, within the current window; null when unknown. */
  readonly burnPercentPerHour: number | null;
  readonly resetsAt: string | null;
  /** When this window's value was last confirmed (not merely carried by another window's update). */
  readonly checkedAt: string | null;
  /** When the history last saw this window start again (meter drop or reset-time jump). */
  readonly resetDetectedAt: string | null;
}

export interface InstancePace {
  readonly instanceId: ProviderInstanceId;
  /** Worst of its windows': ahead, else unknown, else on pace, else behind. */
  readonly verdict: PaceVerdict;
  readonly unknownReason?: PaceUnknownReason;
  readonly reservePercent: number;
  readonly burnFirst: boolean;
  /** The stamp of the newest usage reading (live, or recorded after the live one was taken). */
  readonly checkedAt: string | null;
  readonly windows: ReadonlyArray<WindowPace>;
}

export const DEFAULT_PACING: UsagePacingInstanceSettings = DEFAULT_USAGE_PACING_INSTANCE_SETTINGS;

/**
 * How old a confirmed value may be: the headless refresher's gap across one failed refresh and its
 * backed-off retry (from its own interval and backoff), never under 30 minutes.
 */
export function staleAfterMs(settings: ServerSettings): number {
  const interval = effectiveIntervalMs(settings);
  return Math.max(STALE_FLOOR_MS, interval === null ? 0 : oneFailureGapMs(interval));
}

export interface PaceInput {
  readonly instanceId: ProviderInstanceId;
  readonly provider: ServerProvider | undefined;
  /** History of the instance in append order, or `unreadable`. */
  readonly history: ReadonlyArray<UsageReading> | "unreadable";
  readonly pacing: UsagePacingInstanceSettings;
  readonly nowMs: number;
  readonly staleAfterMs: number;
  readonly burnSpanMs?: number;
}

const msOf = (reading: Pick<UsageReading, "checkedAt">) => Date.parse(reading.checkedAt);
const resetMsOf = (window: ServerProviderUsageWindow) =>
  window.resetsAt === undefined ? Number.NaN : Date.parse(window.resetsAt);

const sameValue = (left: ServerProviderUsageWindow, right: ServerProviderUsageWindow) =>
  left.usedPercent === right.usedPercent && left.resetsAt === right.resetsAt;

const sameWindows = (previous: UsageReading, next: UsageReading) =>
  previous.windows.length === next.windows.length &&
  next.windows.every((window) => {
    const old = previous.windows.find((entry) => entry.id === window.id);
    return old !== undefined && sameValue(old, window);
  });

/** The live snapshot as a reading, with its own (unresolved) account. */
function liveReading(provider: ServerProvider): UsageReading | undefined {
  const limits = provider.usageLimits;
  if (limits === undefined) return undefined;
  const account = accountOf(provider);
  return {
    checkedAt: limits.checkedAt,
    windows: limits.windows,
    ...(account === undefined ? {} : { account }),
    ...(limits.unavailable === undefined ? {} : { unavailable: limits.unavailable.reason }),
  };
}

/**
 * The readings of the current series: the trailing run with no stamp going backwards (a clock
 * change, also one that thinning hid) and no account swap. Pace never runs across either.
 */
export function currentSeries(readings: ReadonlyArray<UsageReading>): ReadonlyArray<UsageReading> {
  let start = readings.length - 1;
  while (start > 0) {
    const previous = readings[start - 1]!;
    const next = readings[start]!;
    if (msOf(next) < msOf(previous) || isAccountSwap(previous, next)) break;
    start -= 1;
  }
  return readings.slice(Math.max(0, start));
}

interface WindowTrack {
  /** Last value seen. */
  last: ServerProviderUsageWindow;
  /** Index in the series of the last reading that carried this window. */
  lastIndex: number;
  /** Confirmed values since the last detected reset, oldest first. */
  confirmed: Array<{ readonly ms: number; readonly at: string; readonly usedPercent: number }>;
  resetAt: string | null;
}

function startedAgain(previous: ServerProviderUsageWindow, next: ServerProviderUsageWindow) {
  const jump = resetMsOf(next) - resetMsOf(previous);
  return (
    previous.usedPercent - next.usedPercent >= RESET_DROP_POINTS ||
    (Number.isFinite(jump) && jump >= RESET_JUMP_MS)
  );
}

/** A reading that read every window it carries (see `UsageReading.full`). */
const isFull = (reading: UsageReading) =>
  reading.full === true && reading.unavailable === undefined;

/**
 * A full read that returned windows proves the ones it lacks are gone. A full read with no windows
 * proves nothing (an API shape the builders do not know reads as empty), so it drops nothing.
 */
const confirmsGone = (reading: UsageReading) => isFull(reading) && reading.windows.length > 0;

/**
 * Walks a series per window id. A full read confirms every window it returned, moved or not. Else a
 * reading confirms a window whose value changed there, or all when none changed (a re-read). Where
 * only other windows moved, it may be a streamed update carrying this one's old value: no proof.
 */
function trackWindows(series: ReadonlyArray<UsageReading>): Map<string, WindowTrack> {
  const tracks = new Map<string, WindowTrack>();
  series.forEach((reading, index) => {
    const previous = index > 0 ? series[index - 1] : undefined;
    const full = isFull(reading);
    const reread =
      previous !== undefined && reading.unavailable === undefined && sameWindows(previous, reading);
    const seen = new Set<string>();
    for (const window of reading.windows) {
      if (seen.has(window.id)) continue;
      seen.add(window.id);
      const track = tracks.get(window.id);
      const before = previous?.windows.find((entry) => entry.id === window.id);
      const confirms = full || reread || before === undefined || !sameValue(before, window);
      if (track === undefined) {
        tracks.set(window.id, {
          last: window,
          lastIndex: index,
          confirmed: [
            { ms: msOf(reading), at: reading.checkedAt, usedPercent: window.usedPercent },
          ],
          resetAt: null,
        });
        continue;
      }
      if (startedAgain(track.last, window)) {
        track.confirmed = [];
        track.resetAt = reading.checkedAt;
      }
      track.last = window;
      track.lastIndex = index;
      if (confirms) {
        track.confirmed.push({
          ms: msOf(reading),
          at: reading.checkedAt,
          usedPercent: window.usedPercent,
        });
      }
    }
  });
  return tracks;
}

function burnRate(
  confirmed: WindowTrack["confirmed"],
  untilMs: number,
  spanMs: number,
): number | null {
  const inSpan = confirmed.filter((point) => point.ms >= untilMs - spanMs && point.ms <= untilMs);
  const first = inSpan[0];
  const last = inSpan.at(-1);
  if (first === undefined || last === undefined || last.ms - first.ms < MIN_BURN_COVER_MS) {
    return null;
  }
  return Math.max(0, ((last.usedPercent - first.usedPercent) / (last.ms - first.ms)) * HOUR_MS);
}

const unknownWindow = (
  window: Pick<ServerProviderUsageWindow, "id" | "kind" | "label">,
  reason: PaceUnknownReason,
  extra?: Partial<WindowPace>,
): WindowPace => ({
  id: window.id,
  kind: window.kind,
  label: window.label,
  verdict: "unknown",
  unknownReason: reason,
  usedPercent: null,
  pacePercent: null,
  effectiveReservePercent: null,
  targetPercent: null,
  burnPercentPerHour: null,
  resetsAt: null,
  checkedAt: null,
  resetDetectedAt: null,
  ...extra,
});

const VERDICT_OF = { ahead: "ahead", on: "onPace", under: "behind" } as const;

function paceWindow(
  window: ServerProviderUsageWindow,
  track: WindowTrack | undefined,
  input: PaceInput,
): WindowPace {
  const { nowMs, pacing } = input;
  // Confirmations start again at a detected reset: one of the window before confirms nothing.
  const confirmed = track?.confirmed.at(-1);
  const confirmedMs = confirmed?.ms;
  const resetMs = resetMsOf(window);
  const shown = {
    usedPercent: window.usedPercent,
    resetsAt: window.resetsAt ?? null,
    checkedAt: confirmed?.at ?? null,
    resetDetectedAt: track?.resetAt ?? null,
  };
  const fresh: PaceUnknownReason | null =
    confirmedMs === undefined
      ? "stale"
      : confirmedMs > nowMs + FUTURE_TOLERANCE_MS
        ? "aheadOfClock"
        : nowMs - confirmedMs > input.staleAfterMs
          ? "stale"
          : Number.isFinite(resetMs) && resetMs <= nowMs
            ? "resetPassed"
            : null;
  if (fresh !== null) return unknownWindow(window, fresh, shown);
  const burn = burnRate(track!.confirmed, confirmedMs!, input.burnSpanMs ?? DEFAULT_BURN_SPAN_MS);
  const elapsed = elapsedShare(window, nowMs);
  const pacePercent = elapsed === null ? null : elapsed * 100;
  const hoursLeftShare = Number.isFinite(resetMs)
    ? Math.min(1, (resetMs - nowMs) / RESERVE_RELEASE_MS)
    : null;
  const effectiveReservePercent =
    pacing.reservePercent === 0
      ? 0
      : hoursLeftShare === null
        ? null
        : pacing.reservePercent * hoursLeftShare;
  const ceiling = effectiveReservePercent === null ? null : 100 - effectiveReservePercent;
  const targetPercent =
    ceiling === null
      ? null
      : pacing.burnFirst
        ? ceiling
        : pacePercent === null
          ? null
          : Math.min(pacePercent, ceiling);
  const measured = { ...shown, pacePercent, effectiveReservePercent, burnPercentPerHour: burn };
  if (targetPercent === null || ceiling === null) {
    return unknownWindow(window, "noResetClock", measured);
  }
  // Upstream's band (±5 points) is for the even pace only. The owner's reserve is a hard line: at
  // or over the ceiling is ahead, and while the ceiling is the target, anything under it is room.
  // So a full window is never on pace, and burning first with reserve R stops at 100 − R.
  const ceilingBinds = pacing.burnFirst || pacePercent === null || ceiling < pacePercent;
  const verdict: PaceVerdict =
    window.usedPercent >= ceiling
      ? "ahead"
      : ceilingBinds
        ? "behind"
        : VERDICT_OF[paceOfShares(window.usedPercent, targetPercent / 100)];
  return {
    id: window.id,
    kind: window.kind,
    label: window.label,
    verdict,
    ...measured,
    targetPercent,
  };
}

/** Ahead wins; then unknown; then on pace; then behind. Windows without a reset clock never pace. */
function instanceVerdict(
  windows: ReadonlyArray<WindowPace>,
  newest: UsageReading,
): {
  readonly verdict: PaceVerdict;
  readonly unknownReason?: PaceUnknownReason;
} {
  const timed = windows.filter((window) => window.unknownReason !== "noResetClock");
  if (timed.some((window) => window.verdict === "ahead")) return { verdict: "ahead" };
  const unknown = timed.find((window) => window.verdict === "unknown");
  if (unknown !== undefined) return { verdict: "unknown", unknownReason: unknown.unknownReason! };
  if (timed.length === 0) {
    // No limits only from the provider's own marker; an empty or failed read is never free.
    const none =
      newest.unavailable === "unsupported"
        ? "noLimits"
        : newest.unavailable === undefined
          ? "emptyRead"
          : "noReading";
    return { verdict: "unknown", unknownReason: windows.length > 0 ? "noResetClock" : none };
  }
  if (timed.some((window) => window.verdict === "onPace")) return { verdict: "onPace" };
  return { verdict: "behind" };
}

/** Pace of one instance. Pure: the clock, settings and history come in through `input`. */
export function paceInstance(input: PaceInput): InstancePace {
  const { instanceId, provider, pacing } = input;
  const base = { instanceId, reservePercent: pacing.reservePercent, burnFirst: pacing.burnFirst };
  if (provider === undefined) {
    return {
      ...base,
      verdict: "unknown",
      unknownReason: "noInstance",
      checkedAt: null,
      windows: [],
    };
  }
  const live = liveReading(provider);
  if (live === undefined || live.unavailable === "unsupported") {
    return {
      ...base,
      verdict: "unknown",
      // Unsupported is confirmed; no usage at all is not read yet, so not known to be free.
      unknownReason: live === undefined ? "noReading" : "noLimits",
      checkedAt: live?.checkedAt ?? null,
      windows: [],
    };
  }
  if (input.history === "unreadable") {
    const windows = live.windows.map((window) =>
      unknownWindow(window, "historyUnreadable", {
        usedPercent: window.usedPercent,
        resetsAt: window.resetsAt ?? null,
      }),
    );
    return {
      ...base,
      verdict: "unknown",
      unknownReason: "historyUnreadable",
      checkedAt: live.checkedAt,
      windows,
    };
  }
  const recorded = input.history;
  const last = recorded.at(-1);
  const liveMs = msOf(live);
  const steppedBack =
    last !== undefined &&
    liveMs < msOf(last) &&
    !recorded.some((entry) => entry.checkedAt === live.checkedAt);
  if (steppedBack) {
    // The live reading is older than the history's newest and was never recorded: the clock
    // stepped back (or the reading raced the history). Neither side is trusted alone.
    const windows = live.windows.map((window) =>
      unknownWindow(window, "aheadOfClock", {
        usedPercent: window.usedPercent,
        resetsAt: window.resetsAt ?? null,
      }),
    );
    return {
      ...base,
      verdict: "unknown",
      unknownReason: "aheadOfClock",
      checkedAt: live.checkedAt,
      windows,
    };
  }
  // The newest reading: the live snapshot, unless the history already holds it (then a reading
  // recorded after it, if any, is newer). The history records live only when it changed enough.
  const series = currentSeries(
    last === undefined || liveMs > msOf(last) ? [...recorded, live] : recorded,
  );
  const newest = series.at(-1)!;
  const tracks = trackWindows(series);
  const newestIds = newest.windows.map((window) => window.id);
  const windows: Array<WindowPace> = [];
  for (const window of newest.windows) {
    if (windows.some((entry) => entry.id === window.id)) continue;
    windows.push(
      newestIds.filter((id) => id === window.id).length > 1
        ? unknownWindow(window, "ambiguous", { usedPercent: null })
        : paceWindow(window, tracks.get(window.id), input),
    );
  }
  for (const [id, track] of tracks) {
    if (newestIds.includes(id)) continue;
    // A non-empty full read after its last sighting confirmed it gone (e.g. extra usage turned
    // off). A window missing because reads failed, were partial or came back empty stays
    // unknown: never free.
    if (series.slice(track.lastIndex + 1).some(confirmsGone)) continue;
    windows.push(unknownWindow(track.last, "missing", { resetDetectedAt: track.resetAt }));
  }
  return { ...base, ...instanceVerdict(windows, newest), checkedAt: newest.checkedAt, windows };
}

export interface UsagePacerDeps {
  readonly getProviders: Effect.Effect<ReadonlyArray<ServerProvider>>;
  readonly readings: (
    instanceId: ProviderInstanceId,
    options: { readonly sinceMs: number },
  ) => Effect.Effect<ReadonlyArray<UsageReading>, UsageHistoryUnreadable>;
  readonly getSettings: Effect.Effect<ServerSettings, ServerSettingsError>;
}

export class UsagePacer extends Context.Service<
  UsagePacer,
  {
    /** Pace of one instance; an instance that is not configured is `unknown` (`noInstance`). */
    readonly paceOf: (
      instanceId: ProviderInstanceId,
    ) => Effect.Effect<InstancePace, ServerSettingsError>;
    /** Every configured instance, plus instances with pacing settings that are gone. */
    readonly paceAll: Effect.Effect<ReadonlyArray<InstancePace>, ServerSettingsError>;
  }
>()("t3/usage/UsagePacer") {}

export const make = (deps: UsagePacerDeps) => {
  const paceWith = Effect.fn("UsagePacer.paceWith")(function* (
    instanceId: ProviderInstanceId,
    providers: ReadonlyArray<ServerProvider>,
    settings: ServerSettings,
  ) {
    const nowMs = yield* Clock.currentTimeMillis;
    const history = yield* deps.readings(instanceId, { sinceMs: nowMs - LOOKBACK_MS }).pipe(
      Effect.catchTags({
        UsageHistoryUnreadable: () => Effect.succeed("unreadable" as const),
      }),
    );
    return paceInstance({
      instanceId,
      provider: providers.find((provider) => provider.instanceId === instanceId),
      history,
      pacing: settings.usagePacing[instanceId] ?? DEFAULT_PACING,
      nowMs,
      staleAfterMs: staleAfterMs(settings),
    });
  });

  const inputs = Effect.all([deps.getProviders, deps.getSettings]);

  return UsagePacer.of({
    paceOf: Effect.fn("UsagePacer.paceOf")(function* (instanceId: ProviderInstanceId) {
      const [providers, settings] = yield* inputs;
      return yield* paceWith(instanceId, providers, settings);
    }),
    paceAll: inputs.pipe(
      Effect.flatMap(([providers, settings]) => {
        const ids = new Set<ProviderInstanceId>(providers.map((provider) => provider.instanceId));
        for (const id of Object.keys(settings.usagePacing)) ids.add(id as ProviderInstanceId);
        return Effect.forEach([...ids], (id) => paceWith(id, providers, settings));
      }),
    ),
  });
};

export const layer = Layer.effect(
  UsagePacer,
  Effect.gen(function* () {
    const registry = yield* ProviderRegistry;
    const history = yield* UsageReadingsHistory;
    const settings = yield* ServerSettingsService;
    return make({
      getProviders: registry.getProviders,
      readings: history.readings,
      getSettings: settings.getSettings,
    });
  }),
);
