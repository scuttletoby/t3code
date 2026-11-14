/**
 * Fork (waits): the stored waits file. Missing or blank is empty; a newer version is never read
 * or written; a bad file is copied aside first, then replaced by what still decodes, with a
 * notice (kept until sent, same id on a rerun) for each thread that lost an open wait.
 */
// @effect-diagnostics-next-line nodeBuiltinImport:off -- The notice ids must be a deterministic digest.
import * as NodeCrypto from "node:crypto";

import { ThreadId } from "@t3tools/contracts";
import {
  type WaitRecord,
  WaitRecord as WaitRecordSchema,
  type WaitStoreFile,
  type WaitStoreNotice,
  WaitStoreNotice as WaitStoreNoticeSchema,
} from "@t3tools/contracts/waitFor";
import { writeFileStringAtomically } from "@t3tools/shared/atomicWrite";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";

export const WAIT_STORE_FILE = "waits.json";
const STORE_VERSION: WaitStoreFile["version"] = 1;
/** A bad file gives notices to at most this many threads; the rest are named in the log. */
const MAX_NOTICES = 50;

const decodeRecord = Schema.decodeUnknownOption(WaitRecordSchema);
const decodeNotice = Schema.decodeUnknownOption(WaitStoreNoticeSchema);
const decodeThreadId = Schema.decodeUnknownOption(ThreadId);
const parseJson = Schema.decodeUnknownOption(Schema.fromJsonString(Schema.Unknown));

export interface WaitStoreContents {
  readonly waits: ReadonlyArray<WaitRecord>;
  /** Notices not sent yet; see `WaitStoreNotice`. */
  readonly notices: ReadonlyArray<WaitStoreNotice>;
}

/** A thread that had an open wait in a file that could not be read in full. */
export interface LostThread {
  readonly threadId: ThreadId;
  /** Ids of its lost waits, when the file still showed them. */
  readonly waitIds: ReadonlyArray<string>;
}

export type WaitStoreLoad =
  | ({ readonly _tag: "Loaded" } & WaitStoreContents)
  | { readonly _tag: "Newer"; readonly version: number }
  /** A bad file found by a load that may not change files (waits are off). Nothing was changed. */
  | { readonly _tag: "Damaged" }
  | ({
      readonly _tag: "Repaired";
      /** Where the raw file was copied. */
      readonly copyPath: string;
      readonly lost: ReadonlyArray<LostThread>;
      /** Records and notices that did not decode; open waits among them are in `lost`. */
      readonly dropped: number;
      /** Writing the kept contents failed; the next change writes them. */
      readonly writeError?: string;
    } & WaitStoreContents);

/** The file could not be read, or a bad file could not be copied aside. Nothing was changed. */
export class WaitStoreError extends Schema.TaggedError<WaitStoreError>()("WaitStoreError", {
  detail: Schema.String,
}) {
  override get message(): string {
    return `The wait store cannot be used: ${this.detail}`;
  }
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const OPEN_STATES = new Set(["waiting", "delivering"]);
/** Thread ids in text that is not a store: every `"threadId": "<id>"` pair. */
const THREAD_ID_TEXT = /"threadId"\s*:\s*"([^"\\]{1,200})"/gu;

function collectLost(
  entries: ReadonlyArray<{ readonly threadId: unknown; readonly waitId?: string }>,
): ReadonlyArray<LostThread> {
  const byThread = new Map<ThreadId, string[]>();
  for (const entry of entries) {
    const threadId = decodeThreadId(entry.threadId);
    if (threadId._tag === "None") continue;
    const ids = byThread.get(threadId.value) ?? [];
    if (entry.waitId !== undefined && !ids.includes(entry.waitId)) ids.push(entry.waitId);
    byThread.set(threadId.value, ids);
  }
  return [...byThread].map(([threadId, waitIds]) => ({ threadId, waitIds }));
}

/** What survives of a file that is not a readable store of this version. */
function salvage(
  text: string,
  json: unknown,
): WaitStoreContents & { readonly lost: ReadonlyArray<LostThread>; readonly dropped: number } {
  const entries = isObject(json) && Array.isArray(json.waits) ? (json.waits as unknown[]) : null;
  if (entries === null) {
    // Not a store at all (torn, or not JSON): every thread it names may have waited.
    const threads = [...text.matchAll(THREAD_ID_TEXT)].map((match) => ({ threadId: match[1] }));
    return { waits: [], notices: [], lost: collectLost(threads), dropped: threads.length };
  }
  const waits: WaitRecord[] = [];
  const seen = new Set<string>();
  const lost: Array<{ threadId: unknown; waitId?: string }> = [];
  let dropped = 0;
  for (const entry of entries) {
    const decoded = decodeRecord(entry);
    if (decoded._tag === "Some" && !seen.has(decoded.value.id)) {
      seen.add(decoded.value.id);
      waits.push(decoded.value);
      continue;
    }
    dropped += 1;
    if (!isObject(entry)) continue;
    // An ended wait has nothing left to send; only one that was open is lost.
    if (typeof entry.state === "string" && !OPEN_STATES.has(entry.state)) continue;
    // A second record with a kept id is not lost: the first one is still there.
    if (typeof entry.id === "string" && seen.has(entry.id)) continue;
    lost.push({
      threadId: entry.threadId,
      ...(typeof entry.id === "string" ? { waitId: entry.id } : {}),
    });
  }
  const notices: WaitStoreNotice[] = [];
  const rawNotices = isObject(json) ? json.notices : undefined;
  if (rawNotices !== undefined) {
    for (const entry of Array.isArray(rawNotices) ? rawNotices : [rawNotices]) {
      const decoded = decodeNotice(entry);
      if (decoded._tag === "Some") notices.push(decoded.value);
      else dropped += 1;
    }
  }
  return { waits, notices, lost: collectLost(lost), dropped };
}

function noticeText(lost: LostThread, copyName: string): string {
  const what =
    lost.waitIds.length === 0
      ? "Any wait this thread had there was lost"
      : `Lost: ${lost.waitIds.join(", ")}`;
  return `[wait store] The server's wait file could not be read; it was saved as ${copyName}. ${what}. Register them again with wait_for if you still need them.`;
}

const fileTime = (nowMs: number) =>
  DateTime.formatIso(DateTime.makeUnsafe(nowMs)).replaceAll(":", "-");

export const make = Effect.fn("WaitStore.make")(function* (input: { readonly filePath: string }) {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const { filePath } = input;

  const writeText = (target: string, contents: string) =>
    writeFileStringAtomically({ filePath: target, contents }).pipe(
      Effect.provideService(FileSystem.FileSystem, fs),
      Effect.provideService(Path.Path, path),
    );

  const write = (contents: WaitStoreContents) => {
    const file: WaitStoreFile = {
      version: STORE_VERSION,
      waits: contents.waits,
      ...(contents.notices.length === 0 ? {} : { notices: contents.notices }),
    };
    return writeText(filePath, `${JSON.stringify(file, null, 2)}\n`).pipe(
      Effect.mapError((error) => new WaitStoreError({ detail: error.message })),
    );
  };

  const repair = Effect.fnUntraced(function* (text: string, json: unknown, nowMs: number) {
    // The same bad file gives the same notice ids, so a repair that runs again sends nothing new.
    const fingerprint = NodeCrypto.createHash("sha256").update(text).digest("hex").slice(0, 12);
    const copyPath = `${filePath}.corrupt-${fileTime(nowMs)}`;
    // The copy first: if it fails, the bad file stays as it is and nothing is lost.
    yield* writeText(copyPath, text).pipe(
      Effect.mapError(
        (error) =>
          new WaitStoreError({
            detail: `${filePath} cannot be read, and a copy could not be saved: ${error.message}`,
          }),
      ),
    );
    const kept = salvage(text, json);
    const known = new Set(kept.notices.map((notice) => notice.messageId));
    const fresh = kept.lost
      .slice(0, MAX_NOTICES)
      .map((lost) => ({
        threadId: lost.threadId,
        messageId: `wait-store:${fingerprint}:${lost.threadId}`,
        text: noticeText(lost, path.basename(copyPath)),
      }))
      .filter((notice) => !known.has(notice.messageId));
    const contents = { waits: kept.waits, notices: [...kept.notices, ...fresh] };
    const writeError = yield* write(contents).pipe(
      Effect.as(undefined),
      Effect.catch((error) => Effect.succeed(error.detail)),
    );
    return {
      _tag: "Repaired",
      ...contents,
      copyPath,
      lost: kept.lost,
      dropped: kept.dropped,
      ...(writeError === undefined ? {} : { writeError }),
    } satisfies WaitStoreLoad;
  });

  /**
   * Reads the file. With `repair` false a bad file is reported, not copied or replaced. Only a read
   * error (other than a missing file) or a failed copy fails.
   */
  const load = Effect.fn("WaitStore.load")(function* (input: {
    readonly nowMs: number;
    readonly repair: boolean;
  }) {
    const text = yield* fs.readFileString(filePath).pipe(
      Effect.catchTags({
        PlatformError: (error) =>
          error.reason._tag === "NotFound"
            ? Effect.succeed("")
            : Effect.fail(new WaitStoreError({ detail: error.message })),
      }),
    );
    if (text.trim() === "") {
      return { _tag: "Loaded", waits: [], notices: [] } satisfies WaitStoreLoad;
    }
    const json = Option.getOrUndefined(parseJson(text));
    // The version is read before anything else, so a newer file is never taken for a bad one.
    const version = isObject(json) ? json.version : undefined;
    if (typeof version === "number" && Number.isInteger(version) && version > STORE_VERSION) {
      return { _tag: "Newer", version } satisfies WaitStoreLoad;
    }
    if (version === STORE_VERSION && isObject(json) && Array.isArray(json.waits)) {
      const kept = salvage(text, json);
      if (kept.dropped === 0) {
        return { _tag: "Loaded", waits: kept.waits, notices: kept.notices } satisfies WaitStoreLoad;
      }
    }
    if (!input.repair) return { _tag: "Damaged" } satisfies WaitStoreLoad;
    return yield* repair(text, json, input.nowMs);
  });

  return { filePath, load, write };
});

export type WaitStore = Effect.Success<ReturnType<typeof make>>;
