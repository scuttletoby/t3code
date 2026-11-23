/**
 * Fork: lens_start's children in `<stateDir>/lens-children.json` (a long request id cannot be read back). A missing or
 * unreadable file is none known; the newest 500 are kept. A failed write is only logged: the stable request id is the
 * guard against a duplicate (delegate_task returns the task it started for that id).
 */
import { writeFileStringAtomically } from "@t3tools/shared/atomicWrite";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";
import * as Semaphore from "effect/Semaphore";

import type { StoredChild } from "./lensStartRun.ts";

const LENS_CHILDREN_FILE = "lens-children.json";
/** The most children kept; the oldest go first. */
const MAX_LENS_CHILDREN = 500;

const StoredChildSchema = Schema.Struct({
  requestId: Schema.String,
  key: Schema.String,
  prs: Schema.Array(Schema.Struct({ number: Schema.Number, head: Schema.String })),
  taskId: Schema.String,
  childThreadId: Schema.NullOr(Schema.String),
  parentThreadId: Schema.String,
  startedAt: Schema.String,
});
const StoreFile = Schema.Struct({
  version: Schema.Literal(1),
  children: Schema.Array(StoredChildSchema),
});
const decodeFile = Schema.decodeUnknownOption(Schema.fromJsonString(StoreFile));
const encodeFile = Schema.encodeEffect(Schema.fromJsonString(StoreFile));

export const make = Effect.fn("LensChildren.make")(function* (stateDir: string) {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const filePath = path.join(stateDir, LENS_CHILDREN_FILE);
  const lock = yield* Semaphore.make(1);

  const read = Effect.gen(function* () {
    const text = yield* fs.readFileString(filePath).pipe(Effect.option);
    if (text._tag === "None") return [] as ReadonlyArray<StoredChild>;
    const file = decodeFile(text.value);
    if (file._tag === "None") {
      yield* Effect.logWarning(
        `lens_start: ${LENS_CHILDREN_FILE} cannot be read; it starts as no children known (a dead child is then started again only by its next attempt)`,
      );
      return [] as ReadonlyArray<StoredChild>;
    }
    return file.value.children;
  });

  const list = lock.withPermits(1)(read);

  /** Adds or replaces (same request id) one child; the newest last. */
  const add = (child: StoredChild) =>
    lock.withPermits(1)(
      Effect.gen(function* () {
        const children = [...(yield* read).filter((c) => c.requestId !== child.requestId), child];
        const kept = children.slice(-MAX_LENS_CHILDREN);
        yield* encodeFile({ version: 1, children: kept }).pipe(
          Effect.flatMap((contents) => writeFileStringAtomically({ filePath, contents })),
          Effect.provideService(FileSystem.FileSystem, fs),
          Effect.provideService(Path.Path, path),
          // Not kept: the next call starts the same id again, and delegate_task returns this task.
          Effect.catch((error) =>
            Effect.logWarning(`lens_start: ${LENS_CHILDREN_FILE} not written: ${String(error)}`),
          ),
        );
      }),
    );

  return { list, add };
});
