import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";

import { makeChildFileJudge } from "./WaitJudges.ts";

const fileTarget = (path: string) => ({ kind: "file", spec: { path, until: "exists" } }) as const;
const context = { waitId: "w_aaaaaaaaaa", now: 0 };

describe("WaitJudges: the file check in its own process", () => {
  it.live("answers met for a file that is there and not yet for one that is not", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const root = yield* fs.makeTempDirectoryScoped({ prefix: "wait-judge-" });
      const there = path.join(root, "there");
      yield* fs.writeFileString(there, "");
      const judge = yield* makeChildFileJudge();
      const met = yield* judge(fileTarget(there), context);
      assert.strictEqual(met.type, "met");
      const notYet = yield* judge(fileTarget(path.join(root, "missing")), context);
      assert.strictEqual(notYet.type, "notYet");
      // A file where a folder should be: nothing can be at the path.
      const under = yield* judge(fileTarget(path.join(there, "x")), context);
      assert.strictEqual(under.type, "notYet");
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.live("a process that hangs is killed at its limit and answers a failed read", () =>
    Effect.gen(function* () {
      const judge = yield* makeChildFileJudge({
        limitMs: 300,
        command: { file: process.execPath, args: ["-e", "setInterval(() => {}, 1000)"] },
      });
      const answer = yield* judge(fileTarget("/anywhere"), context);
      assert.strictEqual(answer.type, "failed");
      assert.include(answer.type === "failed" ? answer.message : "", "no answer in");
    }),
  );

  it.live("a process that cannot start is a failed read", () =>
    Effect.gen(function* () {
      const judge = yield* makeChildFileJudge({
        command: { file: "/nonexistent/wait-stat-binary", args: [] },
      });
      const answer = yield* judge(fileTarget("/anywhere"), context);
      assert.strictEqual(answer.type, "failed");
      assert.include(answer.type === "failed" ? answer.message : "", "could not start");
    }),
  );
});
