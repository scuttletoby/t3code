// @effect-diagnostics nodeBuiltinImport:off - real check processes, found by their command lines.
/**
 * Fork (waits): real check processes. A check still running when the server stops is
 * killed with its process group; one left behind by a server that crashed ends at its own time
 * limit, because on Linux it runs under coreutils `timeout`.
 */
import * as NodeChildProcess from "node:child_process";
import * as NodeFS from "node:fs";
import * as NodeOS from "node:os";

import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import { ProjectId, ThreadId } from "@t3tools/contracts";
import type { WaitCheck } from "@t3tools/contracts/waitFor";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";

import * as ProcessRunner from "../processRunner.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import type { WaitDelivery } from "./WaitDelivery.ts";
import { type CheckPlace, checkCommandLine, makeCheckJudge } from "./WaitJudges.ts";
import * as WaitService from "./WaitService.ts";
import { memoryFileSystem } from "./WaitService.testkit.ts";
import { WAIT_STORE_FILE } from "./WaitStore.ts";

const PROJECT = ProjectId.make("project-a");
const TIMEOUT = ["/usr/bin/timeout", "/bin/timeout"].find((path) => NodeFS.existsSync(path));

const check = (fields: Partial<WaitCheck> = {}): WaitCheck => ({
  argv: ["sleep", "1"],
  maxArgs: 0,
  argPattern: "^[a-z]+$",
  intervalSeconds: 30,
  timeoutSeconds: 20,
  description: "",
  ...fields,
});

/** Process ids whose command line holds `mark`. */
const running = (mark: string) => {
  try {
    return NodeChildProcess.execFileSync("pgrep", ["-f", mark]).toString().trim();
  } catch {
    return "";
  }
};
const cleanUp = (mark: string) => {
  if (running(mark) !== "") NodeChildProcess.execFileSync("pkill", ["-f", mark]);
};
/** Resolves when `child` exits, with its exit code and signal. */
const exitOf = (child: NodeChildProcess.ChildProcess) =>
  Effect.callback<{ readonly code: number | null; readonly signal: string | null }>((resume) => {
    child.once("exit", (code, signal) => resume(Effect.succeed({ code, signal })));
  });

const delivery: WaitDelivery = {
  receiver: () => Effect.succeed({ type: "ok" }),
  deliver: () => Effect.succeed({ type: "delivered" } as const),
};

it.live("a running check process is stopped when the wait service stops (server shutdown)", () => {
  const mark = "41.731";
  return Effect.gen(function* () {
    const fs = memoryFileSystem();
    yield* Effect.scoped(
      Effect.gen(function* () {
        const judge = yield* makeCheckJudge({
          placeOf: () =>
            Effect.succeed<CheckPlace>({ type: "ok", projectId: PROJECT, cwd: NodeOS.tmpdir() }),
        });
        const service = yield* WaitService.make({
          filePath: `/waits/state/${WAIT_STORE_FILE}`,
          delivery,
          fileChildJudge: null,
          judges: { check: judge },
        }).pipe(Effect.provideService(FileSystem.FileSystem, fs.fs), Effect.provide(Path.layer));
        const result = yield* service.register({
          threadId: ThreadId.make("thread-a"),
          projectId: PROJECT,
          input: { kind: "check", check: { name: "slow" } },
        });
        assert.strictEqual(result.state, "waiting");
        assert.notStrictEqual(running(`sleep ${mark}`), "", "the check is running");
      }),
    );
    // The service's scope waits for the killed process group to exit before it closes.
    const left = running(`sleep ${mark}`);
    cleanUp(`sleep ${mark}`);
    assert.strictEqual(left, "", "the check still runs after the service stopped");
  }).pipe(
    Effect.provide(
      Layer.mergeAll(
        ProcessRunner.layer.pipe(Layer.provide(NodeServices.layer)),
        NodeServices.layer,
        ServerSettingsService.layerTest({
          waitFor: { enabled: true },
          waitChecks: { slow: check({ argv: ["sleep", mark], timeoutSeconds: 300 }) },
        }),
      ),
    ),
  );
});

it.live.skipIf(TIMEOUT === undefined)(
  "a check left behind by a server that is gone ends at its own time limit",
  () => {
    const mark = "38.517";
    return Effect.gen(function* () {
      const line = checkCommandLine(
        check({ argv: ["sleep", mark], timeoutSeconds: 1 }),
        [],
        TIMEOUT!,
      );
      // Started the way the runner starts it (its own process group), then never stopped by us,
      // as when the server that started it crashed.
      const child = NodeChildProcess.spawn(line.command, [...line.args], {
        detached: true,
        stdio: "ignore",
      });
      const exit = yield* exitOf(child);
      const left = running(`sleep ${mark}`);
      cleanUp(`sleep ${mark}`);
      // 124: `timeout` ended the command at its limit; nothing else stopped it.
      assert.deepStrictEqual(exit, { code: 124, signal: null });
      assert.strictEqual(left, "", "the orphaned check outlived its own time limit");
    });
  },
);
