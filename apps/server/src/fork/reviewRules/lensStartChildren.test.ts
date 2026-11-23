/**
 * Fork: the lens children lens_start stored (lensStartChildren.ts) on a temp folder: kept across a restart (a new store
 * on the same folder), replaced by request id, cut to the newest 500, and an unreadable file read as none.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";

import * as LensChildren from "./lensStartChildren.ts";
import type { StoredChild } from "./lensStartRun.ts";

const child = (n: number, taskId = `task-${n}`): StoredChild => ({
  requestId: `lens/owner/repo#${n}/${"a".repeat(40)}/code`,
  key: "code",
  prs: [{ number: n, head: "a".repeat(40) }],
  taskId,
  childThreadId: `child-${n}`,
  parentThreadId: "thread-orchestrator",
  startedAt: "2026-11-22T12:00:00.000Z",
});

const folder = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  return yield* fs.makeTempDirectoryScoped({ prefix: "lens-children-" });
});

it.layer(NodeServices.layer)("lens_start's stored children", (it) => {
  it.effect("a child stored before a restart is there after it; the same id is replaced", () =>
    Effect.scoped(
      Effect.gen(function* () {
        const dir = yield* folder;
        const before = yield* LensChildren.make(dir);
        assert.deepStrictEqual(yield* before.list, [], "empty state: none known");
        yield* before.add(child(1));
        yield* before.add(child(2));
        yield* before.add(child(1, "task-again"));
        const after = yield* LensChildren.make(dir);
        assert.deepStrictEqual(
          (yield* after.list).map((c) => c.taskId),
          ["task-2", "task-again"],
        );
      }),
    ),
  );

  it.effect("keeps the newest 500", () =>
    Effect.scoped(
      Effect.gen(function* () {
        const store = yield* LensChildren.make(yield* folder);
        for (let n = 1; n <= 501; n += 1) yield* store.add(child(n));
        const kept = yield* store.list;
        assert.lengthOf(kept, 500);
        assert.strictEqual(kept[0]!.taskId, "task-2");
        assert.strictEqual(kept.at(-1)!.taskId, "task-501");
      }),
    ),
  );

  it.effect("an unreadable file is no children known, and the next add writes a good one", () =>
    Effect.scoped(
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const path = yield* Path.Path;
        const dir = yield* folder;
        yield* fs.writeFileString(path.join(dir, "lens-children.json"), "{not json");
        const store = yield* LensChildren.make(dir);
        assert.deepStrictEqual(yield* store.list, []);
        yield* store.add(child(3));
        assert.deepStrictEqual(
          (yield* (yield* LensChildren.make(dir)).list).map((c) => c.taskId),
          ["task-3"],
        );
      }),
    ),
  );
});
