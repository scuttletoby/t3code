import {
  IsoDateTime,
  ProviderInstanceId,
  ServerProviderUsageWindow,
  TrimmedNonEmptyString,
  type ServerProvider,
} from "@t3tools/contracts";
import { writeFileStringAtomically } from "@t3tools/shared/atomicWrite";
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
  /** A new fingerprint means another account now sits behind the instance. */
  credentialFingerprint: Schema.optional(TrimmedNonEmptyString),
  unavailable: Schema.optional(Schema.Literals(["unsupported", "probeFailed"])),
});
export type UsageReading = typeof UsageReading.Type;

const decodeReadingLine = Schema.decodeUnknownOption(Schema.fromJsonString(UsageReading));
const decodeInstanceId = Schema.decodeUnknownOption(ProviderInstanceId);

const HISTORY_DIRECTORY = "usage-readings";
const DAY_MS = 24 * 60 * 60 * 1000;
/** Covers a monthly window plus its predecessor's last days. */
export const DEFAULT_RETENTION_MS = 35 * DAY_MS;
/** An unchanged meter is still recorded this often, so the history shows it was read. */
const DEFAULT_HEARTBEAT_MS = 30 * 60 * 1000;
/** Hard bound per instance; compaction keeps the newest. */
const MAX_READINGS_PER_INSTANCE = 20_000;
const APPENDS_BETWEEN_COMPACTIONS = 1_000;

function readingOf(provider: ServerProvider): UsageReading | undefined {
  const limits = provider.usageLimits;
  if (limits === undefined) return undefined;
  return {
    checkedAt: limits.checkedAt,
    windows: limits.windows.map((window) => ({ ...window })),
    ...(limits.credentialFingerprint === undefined
      ? {}
      : { credentialFingerprint: limits.credentialFingerprint }),
    ...(limits.unavailable === undefined ? {} : { unavailable: limits.unavailable.reason }),
  };
}

// Stream updates carry fractional percentages; a tenth of a point is below anything pacing acts on.
const meterSignature = (reading: UsageReading): string =>
  JSON.stringify([
    reading.credentialFingerprint ?? null,
    reading.unavailable ?? null,
    reading.windows
      .map((window) => [
        window.id,
        window.kind,
        Math.round(window.usedPercent * 10),
        window.resetsAt ?? null,
        window.windowDurationMins ?? null,
      ])
      .toSorted((left, right) => String(left[0]).localeCompare(String(right[0]))),
  ]);

/** Whether `next` adds information to a history that ends with `previous`. */
export function shouldRecord(
  previous: UsageReading | undefined,
  next: UsageReading,
  heartbeatMs: number = DEFAULT_HEARTBEAT_MS,
): boolean {
  if (previous === undefined) return true;
  const previousMs = Date.parse(previous.checkedAt);
  const nextMs = Date.parse(next.checkedAt);
  // A republished or older snapshot (cache hydration, out-of-order events) is never history.
  if (!(nextMs > previousMs)) return false;
  return meterSignature(previous) !== meterSignature(next) || nextMs - previousMs >= heartbeatMs;
}

/** Valid readings of one history file, oldest first. Torn or foreign lines are skipped. */
function parseHistory(contents: string): Array<UsageReading> {
  const readings: Array<UsageReading> = [];
  for (const line of contents.split("\n")) {
    if (line.trim().length === 0) continue;
    const decoded = decodeReadingLine(line);
    if (decoded._tag === "Some") readings.push(decoded.value);
  }
  return readings.toSorted(
    (left, right) => Date.parse(left.checkedAt) - Date.parse(right.checkedAt),
  );
}

/** Readings inside the retention window, at most `MAX_READINGS_PER_INSTANCE`, newest kept. */
function retainReadings(
  readings: ReadonlyArray<UsageReading>,
  nowMs: number,
  retentionMs: number,
): Array<UsageReading> {
  const kept = readings.filter((reading) => Date.parse(reading.checkedAt) >= nowMs - retentionMs);
  return kept.slice(Math.max(0, kept.length - MAX_READINGS_PER_INSTANCE));
}

const encodeLine = (reading: UsageReading) => `${JSON.stringify(reading)}\n`;
const HISTORY_FILE = /^(.+)\.jsonl$/;

export class UsageReadingsHistory extends Context.Service<
  UsageReadingsHistory,
  {
    /** Appends each instance's reading when it changed, or when the heartbeat is due. */
    readonly record: (providers: ReadonlyArray<ServerProvider>) => Effect.Effect<void>;
    /** Readings of one instance, oldest first; empty when there is no history. */
    readonly readings: (
      instanceId: ProviderInstanceId,
      options?: { readonly sinceMs?: number },
    ) => Effect.Effect<ReadonlyArray<UsageReading>>;
    /** Applies retention to every history file; also drops torn lines left by a crash. */
    readonly compact: Effect.Effect<void>;
  }
>()("t3/usage/UsageReadingsHistory") {}

export const make = Effect.fn("UsageReadingsHistory.make")(function* (options: {
  readonly directory: string;
  readonly retentionMs?: number;
  readonly heartbeatMs?: number;
}) {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const retentionMs = options.retentionMs ?? DEFAULT_RETENTION_MS;
  const heartbeatMs = options.heartbeatMs ?? DEFAULT_HEARTBEAT_MS;
  const lock = yield* Semaphore.make(1);
  const latest = new Map<ProviderInstanceId, UsageReading>();
  const appendsSinceCompaction = new Map<ProviderInstanceId, number>();

  const fileOf = (instanceId: ProviderInstanceId) =>
    path.join(options.directory, `${instanceId}.jsonl`);

  const readFile = (instanceId: ProviderInstanceId) =>
    fs.readFileString(fileOf(instanceId)).pipe(
      Effect.map(parseHistory),
      Effect.catchTags({
        PlatformError: (error) =>
          error.reason._tag === "NotFound"
            ? Effect.succeed<Array<UsageReading>>([])
            : Effect.logWarning("usage readings: history unreadable", {
                instanceId,
                error: error.message,
              }).pipe(Effect.as<Array<UsageReading>>([])),
      }),
    );

  const compactInstance = Effect.fnUntraced(function* (instanceId: ProviderInstanceId) {
    const nowMs = yield* Clock.currentTimeMillis;
    const kept = retainReadings(yield* readFile(instanceId), nowMs, retentionMs);
    appendsSinceCompaction.set(instanceId, 0);
    const last = kept.at(-1);
    if (last === undefined) {
      latest.delete(instanceId);
      yield* fs.remove(fileOf(instanceId), { force: true });
      return;
    }
    latest.set(instanceId, last);
    yield* writeFileStringAtomically({
      filePath: fileOf(instanceId),
      contents: kept.map(encodeLine),
    }).pipe(
      Effect.provideService(FileSystem.FileSystem, fs),
      Effect.provideService(Path.Path, path),
    );
  });

  const logFailure = (operation: string, instanceId?: ProviderInstanceId) =>
    Effect.catchCause((cause) =>
      Effect.logWarning(`usage readings: ${operation} failed`, { instanceId, cause }),
    );

  const compact = lock.withPermits(1)(
    Effect.gen(function* () {
      yield* fs.makeDirectory(options.directory, { recursive: true });
      const names = yield* fs.readDirectory(options.directory);
      for (const name of names) {
        const match = HISTORY_FILE.exec(name);
        const instanceId = match ? decodeInstanceId(match[1]) : null;
        if (instanceId?._tag !== "Some") continue;
        yield* compactInstance(instanceId.value).pipe(logFailure("compaction", instanceId.value));
      }
    }).pipe(logFailure("compaction")),
  );

  const record = (providers: ReadonlyArray<ServerProvider>) =>
    lock.withPermits(1)(
      Effect.forEach(
        providers,
        (provider) =>
          Effect.gen(function* () {
            const reading = readingOf(provider);
            if (reading === undefined) return;
            const instanceId = provider.instanceId;
            if (!latest.has(instanceId)) {
              const last = (yield* readFile(instanceId)).at(-1);
              if (last !== undefined) latest.set(instanceId, last);
            }
            if (!shouldRecord(latest.get(instanceId), reading, heartbeatMs)) return;
            yield* fs.makeDirectory(options.directory, { recursive: true });
            yield* fs.writeFileString(fileOf(instanceId), encodeLine(reading), { flag: "a" });
            latest.set(instanceId, reading);
            const appends = (appendsSinceCompaction.get(instanceId) ?? 0) + 1;
            appendsSinceCompaction.set(instanceId, appends);
            if (appends >= APPENDS_BETWEEN_COMPACTIONS) yield* compactInstance(instanceId);
          }).pipe(logFailure("append", provider.instanceId)),
        { discard: true },
      ),
    );

  const readings = (
    instanceId: ProviderInstanceId,
    readOptions?: { readonly sinceMs?: number },
  ) => {
    const sinceMs = readOptions?.sinceMs ?? Number.NEGATIVE_INFINITY;
    return lock
      .withPermits(1)(readFile(instanceId))
      .pipe(Effect.map((all) => all.filter((reading) => Date.parse(reading.checkedAt) >= sinceMs)));
  };

  return UsageReadingsHistory.of({ record, readings, compact });
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
