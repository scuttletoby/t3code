// @effect-diagnostics-next-line nodeBuiltinImport:off -- The account hash must be a deterministic digest.
import * as NodeCrypto from "node:crypto";

import {
  IsoDateTime,
  ProviderInstanceId,
  ServerProviderUsageWindow,
  TrimmedNonEmptyString,
  type ServerProvider,
} from "@t3tools/contracts";
import { writeFileStringAtomically } from "@t3tools/shared/atomicWrite";
import { providerAccountIdentity } from "@t3tools/shared/usageLimits";
import * as Clock from "effect/Clock";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";
import * as Semaphore from "effect/Semaphore";
import * as Stream from "effect/Stream";

import { ServerConfig } from "../config.ts";
import { ProviderRegistry } from "../provider/ProviderRegistry.ts";
import { forkParked } from "../serverActivation.ts";

/** One meter reading of a provider instance, as its usage limits stood at `checkedAt`. */
export const UsageReading = Schema.Struct({
  checkedAt: IsoDateTime,
  windows: Schema.Array(ServerProviderUsageWindow),
  /**
   * Limits UI account as hashed `<base>` or `<base>.<workspace>`; a failed probe keeps the last
   * known one (`resolveAccount`). Absent = never identified. Swap: see `isAccountSwap`; a change
   * between `<base>` and `<base>.<workspace>` of one base, or to or from a borrowed one, is none.
   */
  account: Schema.optional(TrimmedNonEmptyString),
  /**
   * The workspace in `account` was taken from another instance of the same login in the same
   * snapshot, as the Limits UI groups it. Such an account is never carried as last known.
   */
  borrowed: Schema.optional(Schema.Literal(true)),
  unavailable: Schema.optional(Schema.Literals(["unsupported", "probeFailed"])),
  /**
   * A full read: a provider check brought these limits (both its stamp and the usage stamp
   * moved since the snapshot before), so every window in it was read now. A streamed update
   * moves one window and carries the others' old values; it is never full. Absent = not known.
   */
  full: Schema.optional(Schema.Literal(true)),
});
export type UsageReading = typeof UsageReading.Type;

/**
 * A served reading, in append order. `segment` goes up by one at each reading stamped earlier than
 * the one appended before it (the clock changed). Part 3 computes pace per segment, never across a
 * break. Numbers count per instance and may shift after compaction: compare within one result.
 */
export type HistoryReading = UsageReading & { readonly segment: number };

export interface AccountReading {
  readonly instanceId: ProviderInstanceId;
  /** `segment` counts within `instanceId`'s history. */
  readonly reading: HistoryReading;
}

/** A history file exists but could not be read; it is left untouched. */
export class UsageHistoryUnreadable extends Schema.TaggedError<UsageHistoryUnreadable>()(
  "UsageHistoryUnreadable",
  { instanceId: Schema.String, detail: Schema.String },
) {
  override get message(): string {
    return `usage readings of ${this.instanceId} cannot be read: ${this.detail}`;
  }
}

const decodeReading = Schema.decodeUnknownOption(UsageReading);
const decodeInstanceId = Schema.decodeUnknownOption(ProviderInstanceId);

const HISTORY_DIRECTORY = "usage-readings";
const MINUTE_MS = 60 * 1000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
/** Covers a monthly window plus its predecessor's last days. */
export const DEFAULT_RETENTION_MS = 35 * DAY_MS;
/** An unchanged meter is still recorded this often, so the history shows it was read. */
const DEFAULT_HEARTBEAT_MS = 30 * MINUTE_MS;
/** Hard bound per instance; compaction keeps the newest. */
const DEFAULT_MAX_READINGS_PER_INSTANCE = 20_000;
const DEFAULT_APPENDS_BETWEEN_COMPACTIONS = 1_000;
/** Readings older than this are thinned at compaction... */
const DEFAULT_THIN_AFTER_MS = 2 * DAY_MS;
/** ...to one per this interval, plus every reading that changed more than the meter. */
const DEFAULT_THIN_INTERVAL_MS = 15 * MINUTE_MS;
/** A reading stamped further ahead of the clock than this is not used until the clock gets there. */
const FUTURE_TOLERANCE_MS = 5 * MINUTE_MS;
/** A meter move smaller than this is jitter; pacing acts on whole points. */
const METER_STEP = 0.1;
/** Reset times that differ by less than this are the same reset. */
const RESET_STEP_MS = MINUTE_MS;
const FLOAT_SLACK = 1e-9;

const shortHash = (text: string) =>
  NodeCrypto.createHash("sha256").update(text).digest("hex").slice(0, 16);

/** Hashed account of one provider report, so no email or workspace id lands on disk. */
export function accountOf(provider: ServerProvider): string | undefined {
  const identity = providerAccountIdentity(provider);
  if (identity === null) return undefined;
  const base = shortHash(identity.base);
  return identity.workspaceId === undefined
    ? base
    : `${base}.${shortHash(`${identity.base}:${identity.workspaceId}`)}`;
}

const baseOf = (account: string) => account.split(".")[0];
const hasWorkspace = (account: string) => account.includes(".");

/**
 * Whether two readings of one instance belong to different accounts, so a meter series must not
 * run across them. Knowing less (no account, no workspace, a borrowed workspace) is no swap.
 */
export function isAccountSwap(
  previous: Pick<UsageReading, "account" | "borrowed">,
  next: Pick<UsageReading, "account" | "borrowed">,
): boolean {
  if (previous.account === undefined || next.account === undefined) return false;
  if (baseOf(previous.account) !== baseOf(next.account)) return true;
  if (previous.borrowed === true || next.borrowed === true) return false;
  return (
    hasWorkspace(previous.account) &&
    hasWorkspace(next.account) &&
    previous.account !== next.account
  );
}

interface ResolvedAccount {
  readonly account: string | undefined;
  readonly borrowed: boolean;
}

/**
 * The account recorded for `own`. Knowing less is not another account: no identity keeps
 * `lastKnown` (never a borrowed one); no workspace keeps `lastKnown` of the same base, or else
 * borrows the workspace the snapshot reports for that base, as the Limits UI does, only if it
 * reports exactly one: two workspaces never merge.
 */
function resolveAccount(
  own: string | undefined,
  lastKnown: string | undefined,
  snapshot: ReadonlyArray<string>,
): ResolvedAccount {
  if (own === undefined) return { account: lastKnown, borrowed: false };
  if (hasWorkspace(own)) return { account: own, borrowed: false };
  if (lastKnown !== undefined && hasWorkspace(lastKnown) && baseOf(lastKnown) === own) {
    return { account: lastKnown, borrowed: false };
  }
  const workspaces = new Set(
    snapshot.filter((account) => hasWorkspace(account) && baseOf(account) === own),
  );
  return workspaces.size === 1
    ? { account: [...workspaces][0], borrowed: true }
    : { account: own, borrowed: false };
}

/** The stamps of the last snapshot of an instance seen by this process. */
interface SeenStamps {
  readonly provider: string;
  readonly usage: string | undefined;
}

/**
 * Whether a snapshot's limits are a full read: a provider check ran (its stamp moved) and its
 * limits were taken (the usage stamp moved too). A streamed update moves only the usage stamp; a
 * check that kept the published limits moves only the provider stamp. The first snapshot after
 * start has nothing to compare with, so it is not known to be full.
 */
function isFullRead(provider: ServerProvider, seen: SeenStamps | undefined): boolean {
  const limits = provider.usageLimits;
  return (
    seen !== undefined &&
    limits !== undefined &&
    limits.unavailable === undefined &&
    provider.checkedAt !== seen.provider &&
    limits.checkedAt !== seen.usage
  );
}

function readingOf(
  provider: ServerProvider,
  resolved: ResolvedAccount,
  full: boolean,
): UsageReading | undefined {
  const limits = provider.usageLimits;
  if (limits === undefined) return undefined;
  return {
    checkedAt: limits.checkedAt,
    windows: limits.windows.map((window) => ({ ...window })),
    ...(resolved.account === undefined ? {} : { account: resolved.account }),
    ...(resolved.account !== undefined && resolved.borrowed ? { borrowed: true as const } : {}),
    ...(limits.unavailable === undefined ? {} : { unavailable: limits.unavailable.reason }),
    ...(full ? { full: true as const } : {}),
  };
}

const msOf = (reading: UsageReading) => Date.parse(reading.checkedAt);

function resetMoved(previous: string | undefined, next: string | undefined): boolean {
  if (previous === undefined || next === undefined) return previous !== next;
  return Math.abs(Date.parse(next) - Date.parse(previous)) >= RESET_STEP_MS;
}

/** Window set, kind, length or reset changed, or a meter moved by at least `meterStep` points. */
function windowsChanged(previous: UsageReading, next: UsageReading, meterStep: number): boolean {
  if (previous.windows.length !== next.windows.length) return true;
  const before = new Map(previous.windows.map((window) => [window.id, window]));
  return next.windows.some((window) => {
    const old = before.get(window.id);
    return (
      old === undefined ||
      old.kind !== window.kind ||
      (old.windowDurationMins ?? null) !== (window.windowDurationMins ?? null) ||
      resetMoved(old.resetsAt, window.resetsAt) ||
      Math.abs(window.usedPercent - old.usedPercent) >= meterStep - FLOAT_SLACK
    );
  });
}

/** Changes other than meter moves: account, availability, windows or their resets. */
const structureChanged = (previous: UsageReading, next: UsageReading) =>
  previous.account !== next.account ||
  previous.borrowed !== next.borrowed ||
  previous.unavailable !== next.unavailable ||
  windowsChanged(previous, next, Number.POSITIVE_INFINITY);

/** Whether `next` adds information to a history that ends with `previous`. */
export function shouldRecord(
  previous: UsageReading | undefined,
  next: UsageReading,
  heartbeatMs: number = DEFAULT_HEARTBEAT_MS,
  /** The previous reading is known to be partial (a streamed update or a failed read). */
  previousPartial: boolean = false,
): boolean {
  if (previous === undefined) return true;
  const previousMs = msOf(previous);
  const nextMs = msOf(next);
  // A republished or older snapshot (cache hydration, out-of-order events) is never history.
  if (!(nextMs > previousMs)) return false;
  return (
    // A full read after a partial one confirms the windows the partial one only carried, also
    // those that did not move (part 3 paces on it). Full after full adds only time: the heartbeat.
    (next.full === true && previousPartial) ||
    structureChanged(previous, next) ||
    windowsChanged(previous, next, METER_STEP) ||
    nextMs - previousMs >= heartbeatMs
  );
}

/** One line of a history file; `reading` is absent when the line no longer decodes. */
interface HistoryLine {
  readonly ms: number;
  readonly text: string;
  readonly reading: UsageReading | undefined;
}

/**
 * Lines of one history file in append order; torn lines dropped, undecodable ones kept as text.
 * No line is dropped or hidden because its stamp disagrees with another line or with the clock:
 * either clock may be the wrong one. Stamps that go backwards start a new segment instead.
 */
function parseHistory(contents: string): Array<HistoryLine> {
  const lines: Array<HistoryLine> = [];
  for (const raw of contents.split("\n")) {
    const text = raw.trim();
    if (text.length === 0) continue;
    let json: unknown;
    try {
      json = JSON.parse(text);
    } catch {
      continue;
    }
    const decoded = decodeReading(json);
    if (decoded._tag === "Some") {
      lines.push({ ms: msOf(decoded.value), text, reading: decoded.value });
      continue;
    }
    const checkedAt =
      typeof json === "object" && json !== null && "checkedAt" in json ? json.checkedAt : undefined;
    const ms = typeof checkedAt === "string" ? Date.parse(checkedAt) : Number.NaN;
    if (Number.isFinite(ms)) lines.push({ ms, text, reading: undefined });
  }
  return lines;
}

/** The segment of `reading` appended after `previous` (see `HistoryReading`). */
const segmentAfter = (previous: HistoryReading | undefined, reading: UsageReading) =>
  previous === undefined ? 0 : previous.segment + (msOf(reading) < msOf(previous) ? 1 : 0);

/** Decoded readings of a history, in append order, with their segments. */
function segmented(lines: ReadonlyArray<HistoryLine>): Array<HistoryReading> {
  const readings: Array<HistoryReading> = [];
  for (const line of lines) {
    if (line.reading === undefined) continue;
    readings.push({ ...line.reading, segment: segmentAfter(readings.at(-1), line.reading) });
  }
  return readings;
}

/** Readers use a reading only once the clock has reached its stamp; it stays stored meanwhile. */
const notAhead = (nowMs: number) => (reading: UsageReading) =>
  msOf(reading) <= nowMs + FUTURE_TOLERANCE_MS;

interface RetentionRules {
  readonly retentionMs: number;
  readonly thinAfterMs: number;
  readonly thinIntervalMs: number;
  readonly maxReadings: number;
}

/**
 * Lines to keep, oldest first. Ages count back from `baseMs`, the newest reading of any instance
 * (or now, if earlier), so a clock that is far ahead at start cannot age the whole history out.
 */
function retainLines(
  lines: ReadonlyArray<HistoryLine>,
  baseMs: number,
  rules: RetentionRules,
): Array<HistoryLine> {
  const kept: Array<HistoryLine> = [];
  let lastThinned: HistoryLine | undefined;
  for (const line of lines) {
    if (line.ms < baseMs - rules.retentionMs) continue;
    if (line.reading !== undefined && line.ms < baseMs - rules.thinAfterMs) {
      const previous = lastThinned?.reading;
      // A stamp before the last kept one starts a new segment; thinning starts again there.
      const gap = lastThinned === undefined ? Number.NaN : line.ms - lastThinned.ms;
      if (
        previous !== undefined &&
        !structureChanged(previous, line.reading) &&
        gap >= 0 &&
        gap < rules.thinIntervalMs
      ) {
        continue;
      }
      lastThinned = line;
    }
    kept.push(line);
  }
  return kept.slice(Math.max(0, kept.length - rules.maxReadings));
}

const encodeLine = (reading: UsageReading) => `${JSON.stringify(reading)}\n`;
const HISTORY_FILE = /^(.+)\.jsonl$/;
/** The temp folder `writeFileStringAtomically` makes beside a history file. */
const COMPACTION_TEMP = /^(.+)\.jsonl\.[^.]+$/;

export class UsageReadingsHistory extends Context.Service<
  UsageReadingsHistory,
  {
    /** Appends each instance's reading when it changed, or when the heartbeat is due. */
    readonly record: (providers: ReadonlyArray<ServerProvider>) => Effect.Effect<void>;
    /**
     * Readings of one instance in append order, with their clock segments (`HistoryReading`),
     * served from memory; empty when there is none. Readings stamped ahead of the clock are left
     * out until the clock reaches them.
     */
    readonly readings: (
      instanceId: ProviderInstanceId,
      options?: { readonly sinceMs?: number },
    ) => Effect.Effect<ReadonlyArray<HistoryReading>, UsageHistoryUnreadable>;
    /**
     * Readings of one account (`accountOf`) across every loaded instance, by stamp. After an
     * account swap an instance's readings move to the new account, so each account is one series.
     * Pace is computed per instance and segment.
     */
    readonly readingsOfAccount: (
      account: string,
      options?: { readonly sinceMs?: number },
    ) => Effect.Effect<ReadonlyArray<AccountReading>>;
    /**
     * Applies retention, the cap and thinning to every history file, counted from the newest
     * reading; also drops torn lines. Nothing else is ever deleted.
     */
    readonly compact: Effect.Effect<void>;
  }
>()("t3/usage/UsageReadingsHistory") {}

export const make = Effect.fn("UsageReadingsHistory.make")(function* (options: {
  readonly directory: string;
  readonly retentionMs?: number;
  readonly heartbeatMs?: number;
  readonly maxReadingsPerInstance?: number;
  readonly appendsBetweenCompactions?: number;
  readonly thinAfterMs?: number;
  readonly thinIntervalMs?: number;
}) {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const heartbeatMs = options.heartbeatMs ?? DEFAULT_HEARTBEAT_MS;
  const appendsBetweenCompactions =
    options.appendsBetweenCompactions ?? DEFAULT_APPENDS_BETWEEN_COMPACTIONS;
  const rules: RetentionRules = {
    retentionMs: options.retentionMs ?? DEFAULT_RETENTION_MS,
    thinAfterMs: options.thinAfterMs ?? DEFAULT_THIN_AFTER_MS,
    thinIntervalMs: options.thinIntervalMs ?? DEFAULT_THIN_INTERVAL_MS,
    maxReadings: options.maxReadingsPerInstance ?? DEFAULT_MAX_READINGS_PER_INSTANCE,
  };
  const lock = yield* Semaphore.make(1);
  /** Loaded histories, in append order. An instance is absent until its file was read. */
  const histories = new Map<ProviderInstanceId, Array<HistoryReading>>();
  /** Per instance, the stamps of the last snapshot `record` saw, to tell full reads apart. */
  const seenStamps = new Map<ProviderInstanceId, SeenStamps>();
  /** Instances whose last recorded reading is known partial (seen after start, not full). */
  const partialRecorded = new Set<ProviderInstanceId>();
  /** Instances whose file is known to end with a newline, so an append needs none first. */
  const cleanEnd = new Set<ProviderInstanceId>();
  const appendsSinceCompaction = new Map<ProviderInstanceId, number>();
  /** Instances whose last pass failed; their next failures are not logged until a read or write works. */
  const failing = new Set<ProviderInstanceId>();
  let newestMs = Number.NEGATIVE_INFINITY;

  const fileOf = (instanceId: ProviderInstanceId) =>
    path.join(options.directory, `${instanceId}.jsonl`);

  /** File contents; a missing file is empty, any other read error is not. */
  const readContents = (instanceId: ProviderInstanceId) =>
    fs.readFileString(fileOf(instanceId)).pipe(
      Effect.catchTags({
        PlatformError: (error) =>
          error.reason._tag === "NotFound"
            ? Effect.succeed("")
            : Effect.fail(new UsageHistoryUnreadable({ instanceId, detail: error.message })),
      }),
    );

  /** Keeps a history in memory; `newestMs` only counts readings the clock has reached. */
  const remember = (
    instanceId: ProviderInstanceId,
    readings: Array<HistoryReading>,
    nowMs: number,
  ) => {
    histories.set(instanceId, readings);
    for (const reading of readings) {
      if (notAhead(nowMs)(reading)) newestMs = Math.max(newestMs, msOf(reading));
    }
  };

  const warnAhead = Effect.fnUntraced(function* (
    instanceId: ProviderInstanceId,
    lines: ReadonlyArray<HistoryLine>,
    nowMs: number,
  ) {
    const ahead = lines.filter((line) => line.ms > nowMs + FUTURE_TOLERANCE_MS).length;
    if (ahead > 0) {
      yield* Effect.logWarning(
        "usage readings: readings stamped ahead of the clock kept, not used until it gets there",
        { instanceId, ahead },
      );
    }
  });

  const recovered = (instanceId: ProviderInstanceId) =>
    Effect.suspend(() =>
      failing.delete(instanceId)
        ? Effect.logInfo("usage readings: recording works again", { instanceId })
        : Effect.void,
    );

  const load = Effect.fnUntraced(function* (instanceId: ProviderInstanceId) {
    const loaded = histories.get(instanceId);
    if (loaded !== undefined) return loaded;
    const contents = yield* readContents(instanceId);
    // A file that could not be read now can be: a real read worked.
    yield* recovered(instanceId);
    const lines = parseHistory(contents);
    const nowMs = yield* Clock.currentTimeMillis;
    yield* warnAhead(instanceId, lines, nowMs);
    if (contents.length === 0 || contents.endsWith("\n")) cleanEnd.add(instanceId);
    else cleanEnd.delete(instanceId);
    const readings = segmented(lines);
    remember(instanceId, readings, nowMs);
    return readings;
  });

  const writeCompacted = Effect.fnUntraced(function* (
    instanceId: ProviderInstanceId,
    lines: ReadonlyArray<HistoryLine>,
  ) {
    const nowMs = yield* Clock.currentTimeMillis;
    const kept = retainLines(lines, Math.min(nowMs, newestMs), rules);
    appendsSinceCompaction.set(instanceId, 0);
    if (kept.length === 0) {
      yield* fs.remove(fileOf(instanceId), { force: true });
    } else {
      yield* writeFileStringAtomically({
        filePath: fileOf(instanceId),
        contents: kept.map((line) => `${line.text}\n`),
      }).pipe(
        Effect.provideService(FileSystem.FileSystem, fs),
        Effect.provideService(Path.Path, path),
      );
    }
    cleanEnd.add(instanceId);
    histories.set(instanceId, segmented(kept));
  });

  const logFailure = (operation: string, instanceId?: ProviderInstanceId) =>
    Effect.catchCause((cause) =>
      Effect.logWarning(`usage readings: ${operation} failed`, { instanceId, cause }),
    );

  /** Logs the first failure of an instance; later ones stay quiet until `recovered`. */
  const logFailureOnce = (operation: string, instanceId: ProviderInstanceId) =>
    Effect.catchCause((cause) => {
      if (failing.has(instanceId)) return Effect.void;
      failing.add(instanceId);
      return Effect.logWarning(
        `usage readings: ${operation} failed; not logged again until it works`,
        { instanceId, cause },
      );
    });

  /** Removes temp folders a crash during compaction left; only compaction (under the lock) makes them. */
  const removeCompactionLeftovers = (names: ReadonlyArray<string>) =>
    Effect.forEach(
      names,
      (name) => {
        const match = COMPACTION_TEMP.exec(name);
        if (match === null || decodeInstanceId(match[1])._tag !== "Some") return Effect.void;
        const leftover = path.join(options.directory, name);
        return fs.stat(leftover).pipe(
          Effect.flatMap((info) =>
            info.type === "Directory" ? fs.remove(leftover, { recursive: true }) : Effect.void,
          ),
          logFailure("removing a compaction leftover"),
        );
      },
      { discard: true },
    );

  const compact = lock.withPermits(1)(
    Effect.gen(function* () {
      yield* fs.makeDirectory(options.directory, { recursive: true });
      const names = yield* fs.readDirectory(options.directory);
      yield* removeCompactionLeftovers(names);
      const nowMs = yield* Clock.currentTimeMillis;
      // Read every history before writing any: retention counts back from the newest of all.
      const parsed: Array<readonly [ProviderInstanceId, Array<HistoryLine>]> = [];
      for (const name of names) {
        const match = HISTORY_FILE.exec(name);
        const instanceId = match ? decodeInstanceId(match[1]) : null;
        if (instanceId?._tag !== "Some") continue;
        const id = instanceId.value;
        // An unreadable file is skipped, never treated as empty: compaction would delete it.
        yield* readContents(id).pipe(
          Effect.flatMap((contents) =>
            Effect.gen(function* () {
              yield* recovered(id);
              const lines = parseHistory(contents);
              yield* warnAhead(id, lines, nowMs);
              remember(id, segmented(lines), nowMs);
              parsed.push([id, lines]);
            }),
          ),
          logFailureOnce("compaction read", id),
        );
      }
      for (const [instanceId, lines] of parsed) {
        yield* writeCompacted(instanceId, lines).pipe(logFailure("compaction", instanceId));
      }
    }).pipe(logFailure("compaction")),
  );

  const compactInstance = (instanceId: ProviderInstanceId) =>
    readContents(instanceId).pipe(
      Effect.flatMap((contents) => writeCompacted(instanceId, parseHistory(contents))),
    );

  const record = (providers: ReadonlyArray<ServerProvider>) => {
    const snapshotAccounts = providers.flatMap((provider) => accountOf(provider) ?? []);
    return lock.withPermits(1)(
      Effect.forEach(
        providers,
        (provider) =>
          Effect.gen(function* () {
            const instanceId = provider.instanceId;
            const seen = seenStamps.get(instanceId);
            const full = isFullRead(provider, seen);
            seenStamps.set(instanceId, {
              provider: provider.checkedAt,
              usage: provider.usageLimits?.checkedAt,
            });
            if (provider.usageLimits === undefined) return;
            const history = yield* load(instanceId);
            const lastKnown = history.findLast(
              (entry) => entry.account !== undefined && entry.borrowed !== true,
            )?.account;
            const resolved = resolveAccount(accountOf(provider), lastKnown, snapshotAccounts);
            const reading = readingOf(provider, resolved, full);
            if (reading === undefined) return;
            // Compared with the last reading the clock has reached: a reading stamped ahead of it
            // (the clock stepped back) must not block recording. It stays; a segment starts here.
            const nowMs = yield* Clock.currentTimeMillis;
            const previous = history.findLast(notAhead(nowMs));
            const previousPartial = partialRecorded.has(instanceId);
            if (!shouldRecord(previous, reading, heartbeatMs, previousPartial)) return;
            yield* fs.makeDirectory(options.directory, { recursive: true });
            // A torn last line (crash or partial write) must not swallow this reading.
            const separator = cleanEnd.has(instanceId) ? "" : "\n";
            cleanEnd.delete(instanceId);
            yield* fs.writeFileString(fileOf(instanceId), separator + encodeLine(reading), {
              flag: "a",
            });
            cleanEnd.add(instanceId);
            yield* recovered(instanceId);
            history.push({ ...reading, segment: segmentAfter(history.at(-1), reading) });
            if (seen !== undefined && !full) partialRecorded.add(instanceId);
            else partialRecorded.delete(instanceId);
            newestMs = Math.max(newestMs, msOf(reading));
            const appends = (appendsSinceCompaction.get(instanceId) ?? 0) + 1;
            appendsSinceCompaction.set(instanceId, appends);
            if (appends >= appendsBetweenCompactions) {
              yield* compactInstance(instanceId).pipe(logFailure("compaction", instanceId));
            }
          }).pipe(logFailureOnce("append", provider.instanceId)),
        { discard: true },
      ),
    );
  };

  /** What a reader is served: reached by the clock and not before `sinceMs`. */
  const served = (nowMs: number, sinceMs: number | undefined) => (reading: UsageReading) =>
    notAhead(nowMs)(reading) && msOf(reading) >= (sinceMs ?? Number.NEGATIVE_INFINITY);

  const readings = (
    instanceId: ProviderInstanceId,
    readOptions?: { readonly sinceMs?: number },
  ) => {
    const loaded = histories.get(instanceId);
    // Loaded histories are served from memory, without waiting for a write in progress.
    const all =
      loaded !== undefined ? Effect.succeed(loaded) : lock.withPermits(1)(load(instanceId));
    return Effect.all([all, Clock.currentTimeMillis]).pipe(
      Effect.map(([history, nowMs]) => history.filter(served(nowMs, readOptions?.sinceMs))),
    );
  };

  const readingsOfAccount = (account: string, readOptions?: { readonly sinceMs?: number }) =>
    Clock.currentTimeMillis.pipe(
      Effect.map((nowMs) =>
        [...histories]
          .flatMap(([instanceId, history]) =>
            history
              .filter(
                (reading) =>
                  reading.account === account && served(nowMs, readOptions?.sinceMs)(reading),
              )
              .map((reading): AccountReading => ({ instanceId, reading })),
          )
          .toSorted((left, right) => msOf(left.reading) - msOf(right.reading)),
      ),
    );

  return UsageReadingsHistory.of({ record, readings, readingsOfAccount, compact });
});

/** Compacts once at start, then records every provider snapshot change. */
export const layer = Layer.effect(
  UsageReadingsHistory,
  Effect.gen(function* () {
    const config = yield* ServerConfig;
    const path = yield* Path.Path;
    const registry = yield* ProviderRegistry;
    const history = yield* make({ directory: path.join(config.stateDir, HISTORY_DIRECTORY) });
    yield* forkParked(
      history.compact.pipe(
        Effect.andThen(registry.getProviders),
        Effect.flatMap(history.record),
        Effect.andThen(Stream.runForEach(registry.streamChanges, history.record)),
      ),
    );
    return history;
  }),
);
