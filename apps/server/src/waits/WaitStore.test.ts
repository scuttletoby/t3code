import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import { ThreadId } from "@t3tools/contracts";
import { WaitRecord } from "@t3tools/contracts/waitFor";
import {
  clipStored,
  MAX_NOTE_BYTES,
  MAX_OPEN_WAITS,
  storedBytes,
  validateWaitSpec,
} from "@t3tools/shared/waitFor";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";
import * as Schema from "effect/Schema";

import * as WaitStore from "./WaitStore.ts";

const isWaitRecord = Schema.is(WaitRecord);
const NOW = Date.parse("2026-11-14T12:00:00.000Z");

/** A stored record as an older run of this version wrote it. */
const record = (overrides: Record<string, unknown> = {}) => ({
  id: "w_aaaaaaaaaa",
  threadId: "thread-a",
  projectId: "project-a",
  key: "0123456789abcdef",
  createdAt: "2026-11-14T11:00:00.000Z",
  deadlineAt: "2026-11-15T11:00:00.000Z",
  state: "waiting",
  attempt: 0,
  kind: "file",
  spec: { path: "/srv/ready", until: "exists" },
  ...overrides,
});

const storeFile = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const root = yield* fs.makeTempDirectoryScoped({ prefix: "wait-store-" });
  return { root, filePath: path.join(root, WaitStore.WAIT_STORE_FILE) };
});

const listCopies = (root: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    return (yield* fs.readDirectory(root)).filter((name) => name.includes(".corrupt-")).sort();
  });

const failure = (method: string, pathOrDescriptor: string) =>
  PlatformError.systemError({
    _tag: "Unknown",
    module: "FileSystem",
    method,
    description: "disk full",
    pathOrDescriptor,
  });

/** The real file system, except that renames onto a path that `fails` picks fail. */
const failingRenames = (fails: (to: string) => boolean) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const faulty: FileSystem.FileSystem = {
      ...fs,
      rename: (from, to) => (fails(to) ? Effect.fail(failure("rename", to)) : fs.rename(from, to)),
    };
    return faulty;
  });

const makeStore = (filePath: string, fs?: FileSystem.FileSystem) =>
  fs === undefined
    ? WaitStore.make({ filePath })
    : WaitStore.make({ filePath }).pipe(Effect.provideService(FileSystem.FileSystem, fs));

it.layer(NodeServices.layer)("WaitStore", (it) => {
  it.effect("a missing file has no waits and is not created", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { root, filePath } = yield* storeFile;
      const store = yield* makeStore(filePath);

      const missing = yield* store.load({ nowMs: NOW, repair: true });
      assert.deepStrictEqual(missing, { _tag: "Loaded", waits: [], notices: [] });
      assert.isFalse(yield* fs.exists(filePath));
      assert.deepStrictEqual(yield* listCopies(root), []);
    }),
  );

  it.effect(
    "an empty or blank file (a power cut after the rename) is damage: copied aside, no notices",
    () =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const { root, filePath } = yield* storeFile;
        const store = yield* makeStore(filePath);

        for (const [index, text] of ["", "  \n\t\n", "\u0000".repeat(64)].entries()) {
          yield* fs.writeFileString(filePath, text);
          // Waits off: reported, nothing changed.
          assert.deepStrictEqual(yield* store.load({ nowMs: NOW, repair: false }), {
            _tag: "Damaged",
          });
          assert.strictEqual(yield* fs.readFileString(filePath), text);

          const loaded = yield* store.load({ nowMs: NOW + index * 1000, repair: true });
          assert.strictEqual(loaded._tag, "Repaired", JSON.stringify(text));
          if (loaded._tag !== "Repaired") return;
          assert.deepStrictEqual([loaded.waits, loaded.notices, loaded.lost], [[], [], []]);
          assert.strictEqual(yield* fs.readFileString(loaded.copyPath), text);
          assert.deepStrictEqual(JSON.parse(yield* fs.readFileString(filePath)), {
            version: 1,
            waits: [],
          });
        }
        assert.strictEqual((yield* listCopies(root)).length, 3);
      }),
  );

  it.effect("a write over the size cap is refused and leaves the file as it was", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { filePath } = yield* storeFile;
      const store = yield* makeStore(filePath);
      yield* store.write({ waits: [record()] as never, notices: [] });
      const before = yield* fs.readFileString(filePath);

      const huge = {
        threadId: "thread-b",
        messageId: "wait-store:x:thread-b",
        text: "x".repeat(WaitStore.MAX_STORE_BYTES),
      };
      const error = yield* store
        .write({ waits: [record()] as never, notices: [huge] as never })
        .pipe(Effect.flip);
      assert.include(error.detail, `more than the ${WaitStore.MAX_STORE_BYTES} allowed`);
      assert.strictEqual(yield* fs.readFileString(filePath), before);
    }),
  );

  it.effect("a bad copy BEFORE a good record of the same id is not reported as lost", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { filePath } = yield* storeFile;
      const good = record();
      yield* fs.writeFileString(
        filePath,
        JSON.stringify({ version: 1, waits: [{ ...good, kind: "nonsense" }, good] }),
      );
      const loaded = yield* (yield* makeStore(filePath)).load({ nowMs: NOW, repair: true });
      assert.strictEqual(loaded._tag, "Repaired");
      if (loaded._tag !== "Repaired") return;
      assert.deepStrictEqual(loaded.waits, [good] as never);
      assert.deepStrictEqual(loaded.lost, []);
      assert.deepStrictEqual(loaded.notices, []);
    }),
  );

  it.effect("writes and loads waits and notices; an empty notice list is left out", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { filePath } = yield* storeFile;
      const store = yield* makeStore(filePath);
      const wait = record();

      yield* store.write({ waits: [wait] as never, notices: [] });
      assert.deepStrictEqual(JSON.parse(yield* fs.readFileString(filePath)), {
        version: 1,
        waits: [wait],
      });

      const notice = { threadId: "thread-b", messageId: "wait-store:x:thread-b", text: "Lost." };
      yield* store.write({ waits: [wait] as never, notices: [notice] as never });
      const loaded = yield* store.load({ nowMs: NOW, repair: true });
      assert.deepStrictEqual(loaded, { _tag: "Loaded", waits: [wait], notices: [notice] } as never);
    }),
  );

  it.effect(
    "a newer version is not read, copied or changed, even when this build cannot decode it",
    () =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const { root, filePath } = yield* storeFile;
        const text = JSON.stringify({ version: 2, waits: [{ id: "new-shape" }], future: true });
        yield* fs.writeFileString(filePath, text);
        const store = yield* makeStore(filePath);

        const loaded = yield* store.load({ nowMs: NOW, repair: true });
        assert.deepStrictEqual(loaded, { _tag: "Newer", version: 2 });
        assert.strictEqual(yield* fs.readFileString(filePath), text);
        assert.deepStrictEqual(yield* listCopies(root), []);
      }),
  );

  it.effect(
    "a file that is not JSON is copied aside, emptied, and each named thread gets a notice",
    () =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const { root, filePath } = yield* storeFile;
        // Torn mid-write: two threads still readable, one id that is not a thread id.
        const text = `{"version":1,"waits":[{"id":"w_aaaaaaaaaa","threadId":"thread-a"},{"threadId":"thread-b","x":1},{"threadId":" "},{"threadId":"thread-a"`;
        yield* fs.writeFileString(filePath, text);
        const store = yield* makeStore(filePath);

        const loaded = yield* store.load({ nowMs: NOW, repair: true });
        assert.strictEqual(loaded._tag, "Repaired");
        if (loaded._tag !== "Repaired") return;
        assert.deepStrictEqual(loaded.waits, []);
        assert.deepStrictEqual(
          loaded.lost.map((lost) => lost.threadId),
          ["thread-a", "thread-b"],
        );
        assert.deepStrictEqual(
          loaded.notices.map((notice) => notice.threadId),
          ["thread-a", "thread-b"],
        );
        assert.isUndefined(loaded.writeError);
        const copies = yield* listCopies(root);
        assert.deepStrictEqual(copies, ["waits.json.corrupt-2026-11-14T12-00-00.000Z"]);
        assert.strictEqual(yield* fs.readFileString(loaded.copyPath), text);
        assert.include(loaded.notices[0]!.text, copies[0]!);

        // The repaired file loads as it is; the notices stay until the service sends them.
        const again = yield* store.load({ nowMs: NOW + 1000, repair: true });
        assert.deepStrictEqual(again, {
          _tag: "Loaded",
          waits: [],
          notices: loaded.notices,
        });
      }),
  );

  it.effect("keeps the records that decode and names only the open waits that did not", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { filePath } = yield* storeFile;
      const good = record();
      const notice = { threadId: "thread-z", messageId: "wait-store:old:thread-z", text: "Old." };
      yield* fs.writeFileString(
        filePath,
        JSON.stringify({
          version: 1,
          waits: [
            good,
            // Open but broken: lost, and named with its id.
            record({ id: "w_bbbbbbbbbb", threadId: "thread-b", spec: { path: 7 } }),
            // Ended and broken: nothing left to send.
            record({ id: "w_cccccccccc", threadId: "thread-c", state: "delivered", attempt: -1 }),
            // A second copy of a kept id: the first one is still there.
            record({ state: "bogus" }),
          ],
          notices: [notice, { threadId: "" }],
        }),
      );
      const store = yield* makeStore(filePath);

      const loaded = yield* store.load({ nowMs: NOW, repair: true });
      assert.strictEqual(loaded._tag, "Repaired");
      if (loaded._tag !== "Repaired") return;
      assert.deepStrictEqual(loaded.waits, [good] as never);
      assert.deepStrictEqual(loaded.lost, [
        { threadId: ThreadId.make("thread-b"), waitIds: ["w_bbbbbbbbbb"] },
      ]);
      assert.strictEqual(loaded.dropped, 4);
      assert.deepStrictEqual(
        loaded.notices.map((entry) => entry.threadId),
        ["thread-z", "thread-b"],
      );
      assert.include(loaded.notices[1]!.text, "Lost: w_bbbbbbbbbb.");

      const stored = JSON.parse(yield* fs.readFileString(filePath));
      assert.deepStrictEqual(stored.waits, [good]);
      assert.strictEqual(stored.notices.length, 2);
    }),
  );

  it.effect("a version 1 file without a waits list, or without a version, is repaired", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { root, filePath } = yield* storeFile;
      const store = yield* makeStore(filePath);
      for (const [index, value] of [
        { version: 1, waits: { threadId: "thread-a" } },
        { waits: [record()] },
        [record()],
      ].entries()) {
        yield* fs.writeFileString(filePath, JSON.stringify(value));
        const loaded = yield* store.load({ nowMs: NOW + index * 1000, repair: true });
        assert.strictEqual(loaded._tag, "Repaired", JSON.stringify(value));
      }
      assert.strictEqual((yield* listCopies(root)).length, 3);
    }),
  );

  it.effect("without repair a bad file is reported and left exactly as it was", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { root, filePath } = yield* storeFile;
      yield* fs.writeFileString(filePath, "{not json");
      const store = yield* makeStore(filePath);

      assert.deepStrictEqual(yield* store.load({ nowMs: NOW, repair: false }), { _tag: "Damaged" });
      assert.strictEqual(yield* fs.readFileString(filePath), "{not json");
      assert.deepStrictEqual(yield* listCopies(root), []);
    }),
  );

  it.effect("a copy that cannot be saved fails the load and leaves the bad file alone", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { root, filePath } = yield* storeFile;
      yield* fs.writeFileString(filePath, "{not json");
      const faulty = yield* failingRenames((to) => to.includes(".corrupt-"));
      const store = yield* makeStore(filePath, faulty);

      const error = yield* store.load({ nowMs: NOW, repair: true }).pipe(Effect.flip);
      assert.strictEqual(error._tag, "WaitStoreError");
      assert.include(error.detail, "a copy could not be saved");
      assert.strictEqual(yield* fs.readFileString(filePath), "{not json");
      assert.deepStrictEqual(yield* listCopies(root), []);
    }),
  );

  it.effect(
    "stopped half-way (copy saved, file not replaced): the next load repairs again with the same notice ids",
    () =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const { root, filePath } = yield* storeFile;
        const text = `{"waits":[{"threadId":"thread-a"`;
        yield* fs.writeFileString(filePath, text);
        const faulty = yield* failingRenames((to) => to === filePath);

        const first = yield* (yield* makeStore(filePath, faulty)).load({
          nowMs: NOW,
          repair: true,
        });
        assert.strictEqual(first._tag, "Repaired");
        if (first._tag !== "Repaired") return;
        assert.include(first.writeError ?? "", "disk full");
        assert.strictEqual(yield* fs.readFileString(filePath), text);

        // A restart: the bad file is still there, so it is repaired again.
        const second = yield* (yield* makeStore(filePath)).load({
          nowMs: NOW + 60_000,
          repair: true,
        });
        assert.strictEqual(second._tag, "Repaired");
        if (second._tag !== "Repaired") return;
        assert.isUndefined(second.writeError);
        assert.deepStrictEqual(
          second.notices.map((notice) => notice.messageId),
          first.notices.map((notice) => notice.messageId),
        );
        assert.strictEqual((yield* listCopies(root)).length, 2);
        assert.strictEqual(
          (yield* (yield* makeStore(filePath)).load({ nowMs: NOW + 120_000, repair: true }))._tag,
          "Loaded",
        );
      }),
  );
});

const ALPHABET = "abcdefghijklmnopqrstuvwxyz234567";
const idOf = (n: number) =>
  `w_${Array.from({ length: 10 }, (_, index) => ALPHABET[Math.floor(n / 32 ** (9 - index)) % 32]).join("")}`;
/** JSON writes it as `\u0001`: 6 bytes for one character. */
const WIDE = "\u0001";

it.layer(NodeServices.layer)("WaitStore: size (lens #90 round 2)", (it) => {
  it.effect(
    "the biggest open record the caps allow fits MAX_OPEN_WAITS times with room to spare",
    () =>
      Effect.gen(function* () {
        const { filePath } = yield* storeFile;
        const store = yield* makeStore(filePath);
        // The longest path of 6-byte characters that a wait_for call may store.
        let length = 1000;
        const accepted = (path: string, note: string) =>
          validateWaitSpec({
            input: { kind: "file", file: { path }, note },
            checks: {},
            platform: "linux",
            accounts: new Set(),
          }).ok;
        const note = WIDE.repeat(MAX_NOTE_BYTES / 6);
        while (!accepted(`/${WIDE.repeat(length)}`, note)) length -= 1;
        const path = `/${WIDE.repeat(length)}`;
        const thread = (n: number) => `thread-${String(n).padStart(29, "0")}`; // a UUID's length
        const biggest = (n: number) => ({
          ...record({ id: idOf(n), threadId: thread(n), key: n.toString(16).padStart(16, "0") }),
          projectId: `project-${String(n).padStart(28, "0")}`,
          note,
          spec: { path, until: "exists" },
          state: "delivering",
          result: {
            outcome: "check_error",
            detail: clipStored(WIDE.repeat(5000)),
            at: "2026-11-14T12:00:00.000Z",
          },
          lastError: clipStored(WIDE.repeat(5000)),
          errorSince: "2026-11-14T12:00:00.000Z",
          messageId: `wait:${idOf(n)}:${"m".repeat(60)}`,
          attempt: 999_999,
          parentThreadId: thread(n + 1),
          via: [1, 2, 3, 4, 5].map((k) => thread(n + 10 * k)),
          handedOverBecause: "stopped",
          stoppedSince: "2026-11-14T12:00:00.000Z",
          reservedUntil: "2026-11-14T12:00:00.000Z",
        });
        const waits = Array.from({ length: MAX_OPEN_WAITS }, (_, n) => biggest(n));
        // Each is a record the schema reads back.
        assert.isTrue(isWaitRecord(waits[0]));
        assert.isAtMost(storedBytes(path), 2048);
        const written = yield* store.write({ waits: waits as never, notices: [] });
        assert.strictEqual(written.waits.length, MAX_OPEN_WAITS);
        const fs = yield* FileSystem.FileSystem;
        const bytes = new TextEncoder().encode(yield* fs.readFileString(filePath)).byteLength;
        // 5,023,036 bytes when measured (about 5.0 KB a record) of the 8 MiB (8,388,608) limit.
        assert.isAtMost(bytes, 0.75 * WaitStore.MAX_STORE_BYTES, `${bytes} bytes`);
      }),
  );

  it.effect(
    "a write over the size limit leaves out the oldest ended waits, never an open one or a held slot",
    () =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const { filePath } = yield* storeFile;
        const store = yield* makeStore(filePath);
        // Ended waits an older build wrote, each about 1 MB (its last error had no cap).
        const ended = (n: number, extra: Record<string, unknown> = {}) =>
          record({
            id: idOf(n),
            key: n.toString(16).padStart(16, "0"),
            state: "delivered",
            result: { outcome: "exists", detail: "", at: "2026-11-14T10:00:00.000Z" },
            endedAt: `2026-11-14T10:${String(n).padStart(2, "0")}:00.000Z`,
            lastError: "x".repeat(1_000_000),
            ...extra,
          });
        const open = record({ id: idOf(100), lastError: "x".repeat(1_000_000) });
        const heldSlot = ended(0, {
          kind: "slot",
          spec: {},
          // Still held: a slot reservation outlives the store's clock.
          reservedUntil: "2999-01-01T00:00:00.000Z",
        });
        // Newest last; the held slot is the oldest of all.
        const waits = [heldSlot, open, ...Array.from({ length: 9 }, (_, n) => ended(n + 1))];
        const written = yield* store.write({ waits: waits as never, notices: [] });
        const kept = written.waits.map((wait) => wait.id);
        assert.include(kept, open.id);
        assert.include(kept, heldSlot.id);
        // 11 MB in: the oldest unheld ended waits go, as few as needed.
        assert.deepStrictEqual(
          kept,
          [heldSlot, open, ...waits.slice(2 + waits.length - kept.length)].map((wait) => wait.id),
        );
        assert.isAtLeast(kept.length, 8);
        const text = yield* fs.readFileString(filePath);
        assert.isAtMost(new TextEncoder().encode(text).byteLength, WaitStore.MAX_STORE_BYTES);
        assert.deepStrictEqual(
          JSON.parse(text).waits.map((wait: { id: string }) => wait.id),
          kept,
        );
      }),
  );
});
