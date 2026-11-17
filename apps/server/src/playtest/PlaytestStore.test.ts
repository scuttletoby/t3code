import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";

import * as PlaytestStore from "./PlaytestStore.ts";

const NOW = Date.parse("2026-11-17T12:00:00.000Z");
const STEP = { argv: ["/opt/game/playtest", "step"], timeoutSeconds: 120, description: "" };

/** A stored session as an older run of this version wrote it. */
const session = (overrides: Record<string, unknown> = {}) => ({
  id: "pt_aaaaaaaaaa",
  threadId: "thread-a",
  projectId: "project-a",
  target: "dev-1",
  note: "",
  worktree: "/srv/work/a",
  adapter: {
    claim: STEP,
    renew: STEP,
    push: [{ ...STEP, name: "deploy" }],
    release: STEP,
    renewMinutes: 10,
    idleMinutes: 90,
    maxHours: 6,
  },
  state: "active",
  startedAt: "2026-11-17T11:00:00.000Z",
  lastActivityAt: "2026-11-17T11:00:00.000Z",
  rounds: 0,
  ...overrides,
});

const storeFile = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const root = yield* fs.makeTempDirectoryScoped({ prefix: "playtest-store-" });
  return { root, filePath: path.join(root, PlaytestStore.PLAYTEST_STORE_FILE) };
});

const listCopies = (root: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    return (yield* fs.readDirectory(root)).filter((name) => name.includes(".corrupt-")).sort();
  });

const json = (value: unknown) => `${JSON.stringify(value)}\n`;

it.layer(NodeServices.layer)("PlaytestStore", (it) => {
  it.effect("a missing file has no sessions and is not created", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { root, filePath } = yield* storeFile;
      const store = yield* PlaytestStore.make({ filePath });

      assert.deepStrictEqual(yield* store.load({ nowMs: NOW }), { _tag: "Loaded", sessions: [] });
      assert.isFalse(yield* fs.exists(filePath));
      assert.deepStrictEqual(yield* listCopies(root), []);
    }),
  );

  it.effect("a written file loads back as it was, again and again", () =>
    Effect.gen(function* () {
      const { filePath } = yield* storeFile;
      const store = yield* PlaytestStore.make({ filePath });
      const fs = yield* FileSystem.FileSystem;
      yield* fs.writeFileString(filePath, json({ version: 1, sessions: [session()] }));

      const first = yield* store.load({ nowMs: NOW });
      assert.strictEqual(first._tag, "Loaded");
      if (first._tag !== "Loaded") return;
      yield* store.write(first.sessions);
      assert.deepStrictEqual(yield* store.load({ nowMs: NOW }), first);
    }),
  );

  it.effect("a file from a newer T3 is left as it is (not read, not copied, not written)", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { root, filePath } = yield* storeFile;
      const store = yield* PlaytestStore.make({ filePath });
      const text = json({ version: 2, sessions: [{ id: "new-shape" }] });
      yield* fs.writeFileString(filePath, text);

      assert.deepStrictEqual(yield* store.load({ nowMs: NOW }), { _tag: "Newer", version: 2 });
      assert.strictEqual(yield* fs.readFileString(filePath), text);
      assert.deepStrictEqual(yield* listCopies(root), []);
    }),
  );

  it.effect("an empty, blank or torn file is damage: copied aside, then written empty", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { root, filePath } = yield* storeFile;
      const store = yield* PlaytestStore.make({ filePath });

      for (const [index, text] of ["", "  \n", '{"version":1,"sessions":[{"id'].entries()) {
        yield* fs.writeFileString(filePath, text);
        const loaded = yield* store.load({ nowMs: NOW + index * 1000 });
        assert.strictEqual(loaded._tag, "Repaired", JSON.stringify(text));
        if (loaded._tag !== "Repaired") return;
        assert.deepStrictEqual(loaded.sessions, []);
        assert.strictEqual(yield* fs.readFileString(loaded.copyPath), text);
        assert.deepStrictEqual(JSON.parse(yield* fs.readFileString(filePath)), {
          version: 1,
          sessions: [],
        });
      }
      assert.strictEqual((yield* listCopies(root)).length, 3);
    }),
  );

  it.effect(
    "a file with no version, or an older or odd one, is copied aside; its sessions stay",
    () =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const { root, filePath } = yield* storeFile;
        const store = yield* PlaytestStore.make({ filePath });
        const good = session();

        const versions = [undefined, 0, -1, 1.5, "1", null];
        for (const [index, version] of versions.entries()) {
          const text = json(
            version === undefined ? { sessions: [good] } : { version, sessions: [good] },
          );
          yield* fs.writeFileString(filePath, text);
          const loaded = yield* store.load({ nowMs: NOW + index * 1000 });
          assert.strictEqual(loaded._tag, "Repaired", String(version));
          if (loaded._tag !== "Repaired") return;
          assert.deepStrictEqual(
            loaded.sessions.map((entry) => entry.id),
            [good.id],
          );
          assert.strictEqual(loaded.dropped, 0);
          assert.strictEqual(yield* fs.readFileString(loaded.copyPath), text);
          assert.strictEqual(JSON.parse(yield* fs.readFileString(filePath)).version, 1);
        }
        assert.strictEqual((yield* listCopies(root)).length, versions.length);
      }),
  );

  it.effect("a stored note with control characters still loads (steps get it cleaned)", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { filePath } = yield* storeFile;
      const store = yield* PlaytestStore.make({ filePath });
      yield* fs.writeFileString(
        filePath,
        json({ version: 1, sessions: [session({ note: "old\u0000note\nhere" })] }),
      );
      const loaded = yield* store.load({ nowMs: NOW });
      assert.strictEqual(loaded._tag, "Loaded");
      if (loaded._tag !== "Loaded") return;
      assert.strictEqual(loaded.sessions[0]?.note, "old\u0000note\nhere");
    }),
  );

  it.effect("keeps the sessions that still decode and names the lost ones not ended", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { filePath } = yield* storeFile;
      const store = yield* PlaytestStore.make({ filePath });
      const good = session();
      yield* fs.writeFileString(
        filePath,
        json({
          version: 1,
          sessions: [
            good,
            // The same id twice: the first one stands.
            session({ target: "dev-2" }),
            session({ id: "pt_bbbbbbbbbb", state: "claiming", target: "bad target" }),
            session({ id: "pt_cccccccccc", state: "ended", rounds: -1 }),
            "not a session",
          ],
        }),
      );

      const loaded = yield* store.load({ nowMs: NOW });
      assert.strictEqual(loaded._tag, "Repaired");
      if (loaded._tag !== "Repaired") return;
      assert.deepStrictEqual(
        loaded.sessions.map((entry) => [entry.id, entry.target]),
        [["pt_aaaaaaaaaa", "dev-1"]],
      );
      assert.strictEqual(loaded.dropped, 4);
      assert.deepStrictEqual(loaded.lost, ["pt_bbbbbbbbbb"]);
      // Repaired once: the next load reads a good file.
      assert.strictEqual((yield* store.load({ nowMs: NOW }))._tag, "Loaded");
    }),
  );

  it.effect("a bad file that cannot be copied aside is not overwritten", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const { filePath } = yield* storeFile;
      yield* fs.writeFileString(filePath, "{");
      const faulty: FileSystem.FileSystem = {
        ...fs,
        rename: (from, to) =>
          to.includes(".corrupt-")
            ? Effect.fail(
                PlatformError.systemError({
                  _tag: "Unknown",
                  module: "FileSystem",
                  method: "rename",
                  description: "disk full",
                  pathOrDescriptor: to,
                }),
              )
            : fs.rename(from, to),
      };
      const store = yield* PlaytestStore.make({ filePath }).pipe(
        Effect.provideService(FileSystem.FileSystem, faulty),
      );

      const error = yield* Effect.flip(store.load({ nowMs: NOW }));
      assert.include(error.message, "a copy could not be saved");
      assert.strictEqual(yield* fs.readFileString(filePath), "{");
    }),
  );

  it.effect("drops only the oldest ended sessions past the cap", () =>
    Effect.sync(() => {
      const ended = Array.from({ length: PlaytestStore.MAX_ENDED_SESSIONS + 2 }, (_, index) =>
        session({
          id: `pt_${String(index).padStart(10, "a")}`,
          state: "ended",
          endedAt: DateTime.formatIso(DateTime.makeUnsafe(NOW + index * 1000)),
        }),
      ).reverse();
      const live = session({ id: "pt_zzzzzzzzzz" });
      const kept = PlaytestStore.trimEnded([...ended, live] as never);
      assert.strictEqual(kept.length, PlaytestStore.MAX_ENDED_SESSIONS + 1);
      const ids = new Set(kept.map((entry) => entry.id));
      assert.isTrue(ids.has("pt_zzzzzzzzzz"));
      assert.isFalse(ids.has(`pt_${"0".padStart(10, "a")}`));
      assert.isFalse(ids.has(`pt_${"1".padStart(10, "a")}`));
      // The session being worked on stays, even when its end reads oldest.
      const keep = `pt_${"0".padStart(10, "a")}`;
      const kept2 = PlaytestStore.trimEnded([...ended, live] as never, keep);
      assert.strictEqual(kept2.length, PlaytestStore.MAX_ENDED_SESSIONS + 2);
      assert.isTrue(new Set(kept2.map((entry) => entry.id)).has(keep));
    }),
  );
});
