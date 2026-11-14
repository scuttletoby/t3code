import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import { ThreadId } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";

import * as WaitStore from "./WaitStore.ts";

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
  it.effect("a missing, empty or blank file has no waits and is not created or changed", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { root, filePath } = yield* storeFile;
      const store = yield* makeStore(filePath);

      const missing = yield* store.load({ nowMs: NOW, repair: true });
      assert.deepStrictEqual(missing, { _tag: "Loaded", waits: [], notices: [] });
      assert.isFalse(yield* fs.exists(filePath));

      for (const text of ["", "  \n\t\n"]) {
        yield* fs.writeFileString(filePath, text);
        const loaded = yield* store.load({ nowMs: NOW, repair: true });
        assert.deepStrictEqual(loaded, { _tag: "Loaded", waits: [], notices: [] });
        assert.strictEqual(yield* fs.readFileString(filePath), text);
      }
      assert.deepStrictEqual(yield* listCopies(root), []);
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
