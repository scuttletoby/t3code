/**
 * Fork (playtest): the stored sessions file. Missing is empty; a newer version is never read or
 * written; a bad file (also an empty or blank one) is copied aside first, then replaced by the
 * sessions that still decode. A session lost that way still ends on its target: the adapter's
 * claim expires by itself there (docs/fork/playtest.md).
 */
import {
  PLAYTEST_STORE_VERSION,
  type PlaytestSessionRecord,
  PlaytestSessionRecord as PlaytestSessionRecordSchema,
  type PlaytestStoreFile,
} from "@t3tools/contracts";
import { writeFileStringAtomically } from "@t3tools/shared/atomicWrite";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";

export const PLAYTEST_STORE_FILE = "playtest.json";
/** Ended sessions kept for status; older ones are dropped at the next write. */
export const MAX_ENDED_SESSIONS = 100;

const decodeRecord = Schema.decodeUnknownOption(PlaytestSessionRecordSchema);
const parseJson = Schema.decodeUnknownOption(Schema.fromJsonString(Schema.Unknown));

export type PlaytestStoreLoad =
  | { readonly _tag: "Loaded"; readonly sessions: ReadonlyArray<PlaytestSessionRecord> }
  | { readonly _tag: "Newer"; readonly version: number }
  | {
      readonly _tag: "Repaired";
      readonly sessions: ReadonlyArray<PlaytestSessionRecord>;
      /** Where the raw file was copied. */
      readonly copyPath: string;
      /** Entries that did not decode. */
      readonly dropped: number;
      /** Ids of dropped entries that were not ended, when the file still showed them. */
      readonly lost: ReadonlyArray<string>;
    };

/** The file could not be read or written, or a bad file could not be copied aside. */
export class PlaytestStoreError extends Schema.TaggedError<PlaytestStoreError>()(
  "PlaytestStoreError",
  { detail: Schema.String },
) {
  override get message(): string {
    return `The playtest store cannot be used: ${this.detail}`;
  }
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function salvage(json: unknown) {
  const entries = isObject(json) && Array.isArray(json.sessions) ? json.sessions : null;
  if (entries === null) return { sessions: [], dropped: 1, lost: [] as string[] };
  const sessions: PlaytestSessionRecord[] = [];
  const seen = new Set<string>();
  const lost: string[] = [];
  let dropped = 0;
  for (const entry of entries as unknown[]) {
    const decoded = decodeRecord(entry);
    if (decoded._tag === "Some" && !seen.has(decoded.value.id)) {
      seen.add(decoded.value.id);
      sessions.push(decoded.value);
      continue;
    }
    dropped += 1;
    if (isObject(entry) && entry.state !== "ended" && typeof entry.id === "string") {
      lost.push(entry.id.slice(0, 40));
    }
  }
  return { sessions, dropped, lost: lost.filter((id) => !seen.has(id)) };
}

/**
 * The sessions as written: without the oldest ended ones past `MAX_ENDED_SESSIONS`. The session
 * `keep` names (the one just worked on) stays even when its end reads oldest (the clock went back).
 */
export function trimEnded(
  sessions: ReadonlyArray<PlaytestSessionRecord>,
  keep?: string,
): ReadonlyArray<PlaytestSessionRecord> {
  const ended = sessions.filter((session) => session.state === "ended" && session.id !== keep);
  if (ended.length <= MAX_ENDED_SESSIONS) return sessions;
  const drop = new Set(
    [...ended]
      .sort((a, b) => (a.endedAt ?? "").localeCompare(b.endedAt ?? ""))
      .slice(0, ended.length - MAX_ENDED_SESSIONS),
  );
  return sessions.filter((session) => !drop.has(session));
}

const serialise = (sessions: ReadonlyArray<PlaytestSessionRecord>) =>
  `${JSON.stringify({ version: PLAYTEST_STORE_VERSION, sessions } satisfies PlaytestStoreFile, null, 2)}\n`;

const fileTime = (nowMs: number) =>
  DateTime.formatIso(DateTime.makeUnsafe(nowMs)).replaceAll(":", "-");

export const make = Effect.fn("PlaytestStore.make")(function* (input: {
  readonly filePath: string;
}) {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const { filePath } = input;

  const writeText = (target: string, contents: string) =>
    writeFileStringAtomically({ filePath: target, contents }).pipe(
      Effect.provideService(FileSystem.FileSystem, fs),
      Effect.provideService(Path.Path, path),
      Effect.mapError((error) => new PlaytestStoreError({ detail: error.message })),
    );

  /** Writes the sessions (see `trimEnded`) and returns what was written. */
  const write = Effect.fn("PlaytestStore.write")(function* (
    sessions: ReadonlyArray<PlaytestSessionRecord>,
    keep?: string,
  ) {
    const kept = trimEnded(sessions, keep);
    yield* writeText(filePath, serialise(kept));
    return kept;
  });

  /** Reads the file; a bad one is copied aside and replaced by what still decodes. */
  const load = Effect.fn("PlaytestStore.load")(function* (input: { readonly nowMs: number }) {
    const read = yield* fs.readFileString(filePath).pipe(
      Effect.asSome,
      Effect.catchTags({
        PlatformError: (error) =>
          error.reason._tag === "NotFound"
            ? Effect.succeedNone
            : Effect.fail(new PlaytestStoreError({ detail: error.message })),
      }),
    );
    if (Option.isNone(read)) return { _tag: "Loaded", sessions: [] } satisfies PlaytestStoreLoad;
    const text = read.value;
    const json = Option.getOrUndefined(parseJson(text));
    // The version first, so a newer file is never taken for a bad one and overwritten.
    const version = isObject(json) ? json.version : undefined;
    if (
      typeof version === "number" &&
      Number.isInteger(version) &&
      version > PLAYTEST_STORE_VERSION
    ) {
      return { _tag: "Newer", version } satisfies PlaytestStoreLoad;
    }
    const kept = salvage(json);
    if (version === PLAYTEST_STORE_VERSION && kept.dropped === 0) {
      return { _tag: "Loaded", sessions: kept.sessions } satisfies PlaytestStoreLoad;
    }
    // This store never writes an empty or blank file, so one is damage: copied aside, not lost.
    const copyPath = `${filePath}.corrupt-${fileTime(input.nowMs)}`;
    yield* writeText(copyPath, text).pipe(
      Effect.mapError(
        (error) =>
          new PlaytestStoreError({
            detail: `${filePath} cannot be read, and a copy could not be saved: ${error.detail}`,
          }),
      ),
    );
    const sessions = yield* write(kept.sessions);
    return {
      _tag: "Repaired",
      sessions,
      copyPath,
      dropped: kept.dropped,
      lost: kept.lost,
    } satisfies PlaytestStoreLoad;
  });

  return { filePath, load, write };
});

export type PlaytestStore = Effect.Success<ReturnType<typeof make>>;
