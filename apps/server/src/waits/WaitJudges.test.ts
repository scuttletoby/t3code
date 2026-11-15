// @effect-diagnostics nodeBuiltinImport:off - looks for a BusyBox on the test machine.
import * as NodeFS from "node:fs";

import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import { ProjectId, ThreadId } from "@t3tools/contracts";
import type { WaitCheck } from "@t3tools/contracts/waitFor";
import * as HostProcess from "@t3tools/shared/HostProcess";
import { SpawnExecutableResolution } from "@t3tools/shared/shell";
import type { Judgement } from "@t3tools/shared/waitFor";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Path from "effect/Path";
import { TestClock } from "effect/testing";

import * as ProcessRunner from "../processRunner.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import {
  type CheckPlace,
  checkCommandLine,
  findTimeoutProgram,
  makeCheckJudge,
  makeChildFileJudge,
  makeUnitJudge,
  testTimeoutProgram,
} from "./WaitJudges.ts";

const fileTarget = (path: string) => ({ kind: "file", spec: { path, until: "exists" } }) as const;
const PROJECT = ProjectId.make("project-a");
const OTHER_PROJECT = ProjectId.make("project-b");
const context = {
  waitId: "w_aaaaaaaaaa",
  now: 0,
  threadId: ThreadId.make("thread-a"),
  projectId: PROJECT,
};

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

const checkTarget = (name: string, args: ReadonlyArray<string> = []) =>
  ({ kind: "check", spec: { name, args } }) as const;
const unitTarget = (name: string, scope: "user" | "system" = "user") =>
  ({ kind: "unit", spec: { name, scope } }) as const;
/** A check that runs this test's node with a script. */
const nodeCheck = (script: string, fields: Partial<WaitCheck> = {}): WaitCheck => ({
  argv: [process.execPath, "-e", script],
  maxArgs: 0,
  argPattern: "^[a-z ;]+$",
  intervalSeconds: 30,
  timeoutSeconds: 20,
  description: "",
  ...fields,
});
const PRINT_ARGS =
  "console.log(JSON.stringify({ args: process.argv.slice(1), cwd: process.cwd() }))";
const failedText = (answer: Judgement) => (answer.type === "failed" ? answer.message : "");
const runnerLayer = ProcessRunner.layer.pipe(Layer.provideMerge(NodeServices.layer));

describe("WaitJudges: the check judge runs a command from settings", () => {
  const withChecks = (
    checks: Record<string, WaitCheck>,
    projectChecks?: Record<string, WaitCheck>,
  ) =>
    Layer.mergeAll(
      runnerLayer,
      ServerSettingsService.layerTest({
        waitChecks: checks,
        ...(projectChecks === undefined
          ? {}
          : { projectSettingsOverrides: { [OTHER_PROJECT]: { waitChecks: projectChecks } } }),
      }),
    );
  const placeIn =
    (cwd: string, projectId = PROJECT) =>
    () =>
      Effect.succeed<CheckPlace>({ type: "ok", projectId, cwd });

  it.live(
    "T9: the arguments reach the process one by one, without a shell, in the project root",
    () =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const root = yield* fs.makeTempDirectoryScoped({ prefix: "wait-check-" });
        const judge = yield* makeCheckJudge({ placeOf: placeIn(root) });
        const answer = yield* judge(checkTarget("args", ["a b", "c;d"]), context);
        assert.strictEqual(answer.type, "met");
        const detail = answer.type === "met" ? JSON.parse(answer.detail) : null;
        assert.deepStrictEqual(detail.args, ["fixed", "a b", "c;d"]);
        assert.strictEqual(yield* fs.realPath(detail.cwd), yield* fs.realPath(root));
      }).pipe(
        Effect.scoped,
        Effect.provide(
          withChecks({
            args: nodeCheck("", {
              argv: [process.execPath, "-e", PRINT_ARGS, "fixed"],
              maxArgs: 2,
            }),
          }),
        ),
      ),
  );

  it.live(
    "T9: exit 0, 1 and 3 are met, not yet and a failed read; a hanging check is killed at its limit",
    () =>
      Effect.gen(function* () {
        const judge = yield* makeCheckJudge({ placeOf: placeIn(process.cwd()) });
        const met = yield* judge(checkTarget("zero"), context);
        assert.deepStrictEqual(met, { type: "met", outcome: "met", detail: "green" });
        assert.strictEqual((yield* judge(checkTarget("one"), context)).type, "notYet");
        const three = yield* judge(checkTarget("three"), context);
        assert.isTrue(three.type === "failed" && !three.permanent);
        const hang = yield* judge(checkTarget("hang"), context);
        assert.isTrue(hang.type === "failed" && !hang.permanent);
        assert.include(failedText(hang), "Timed out");
      }).pipe(
        Effect.provide(
          withChecks({
            zero: nodeCheck("console.log('green')"),
            one: nodeCheck("process.exit(1)"),
            three: nodeCheck("console.error('connection refused'); process.exit(3)"),
            hang: nodeCheck("setInterval(() => {}, 1000)", { timeoutSeconds: 1 }),
          }),
        ),
      ),
  );

  it.live("a check that prints megabytes is stopped and is a failed read, not met", () =>
    Effect.gen(function* () {
      const judge = yield* makeCheckJudge({ placeOf: placeIn(process.cwd()) });
      const answer = yield* judge(checkTarget("loud"), context);
      assert.deepStrictEqual(answer, {
        type: "failed",
        message: "Printed too much output.",
        permanent: false,
      });
    }).pipe(
      Effect.provide(
        withChecks({ loud: nodeCheck("process.stdout.write('x'.repeat(4 * 1024 * 1024))") }),
      ),
    ),
  );

  it.live("a check removed or narrowed in settings is a permanent error and runs nothing", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const root = yield* fs.makeTempDirectoryScoped({ prefix: "wait-check-" });
      const judge = yield* makeCheckJudge({ placeOf: placeIn(root) });
      const removed = yield* judge(checkTarget("gone"), context);
      assert.deepStrictEqual(removed, {
        type: "failed",
        message: 'Check "gone" is no longer in this project\'s settings.',
        permanent: true,
      });
      // `narrow` now takes no arguments; the stored wait has one.
      const narrowed = yield* judge(checkTarget("narrow", ["abc"]), context);
      assert.isTrue(narrowed.type === "failed" && narrowed.permanent);
      assert.include(failedText(narrowed), "was changed in settings");
      assert.deepStrictEqual(yield* fs.readDirectory(root), [], "nothing ran");
    }).pipe(
      Effect.scoped,
      Effect.provide(withChecks({ narrow: nodeCheck("require('fs').writeFileSync('ran', '')") })),
    ),
  );

  it.live(
    "the checks come from the project the thread is in now; a gone project is permanent",
    () =>
      Effect.gen(function* () {
        const moved = yield* makeCheckJudge({ placeOf: placeIn(process.cwd(), OTHER_PROJECT) });
        // Registered in PROJECT, where "build" exits 1; the thread moved to OTHER_PROJECT, where it is met.
        assert.strictEqual((yield* moved(checkTarget("build"), context)).type, "met");
        const gone = yield* makeCheckJudge({
          placeOf: () =>
            Effect.succeed<CheckPlace>({ type: "gone", message: "The project is gone." }),
        });
        assert.deepStrictEqual(yield* gone(checkTarget("build"), context), {
          type: "failed",
          message: "The project is gone.",
          permanent: true,
        });
        const unreadable = yield* makeCheckJudge({
          placeOf: () => Effect.succeed<CheckPlace>({ type: "unreadable", message: "No read." }),
        });
        const answer = yield* unreadable(checkTarget("build"), context);
        assert.isTrue(answer.type === "failed" && !answer.permanent);
      }).pipe(
        Effect.provide(
          withChecks(
            { build: nodeCheck("process.exit(1)") },
            { build: nodeCheck("console.log('ok')") },
          ),
        ),
      ),
  );

  it.live("a check whose program would need a shell on Windows is refused, never run", () =>
    Effect.gen(function* () {
      const judge = yield* makeCheckJudge({ placeOf: placeIn(process.cwd()) });
      const answer = yield* judge(checkTarget("shim"), context);
      assert.isTrue(answer.type === "failed" && answer.permanent);
      assert.include(failedText(answer), "needs a shell");
    }).pipe(
      Effect.provideService(HostProcess.Platform, "win32"),
      Effect.provideService(SpawnExecutableResolution, () => "C:\\tools\\deploy-check.cmd"),
      Effect.provide(withChecks({ shim: { ...nodeCheck(""), argv: ["deploy-check"] } })),
    ),
  );
});

describe("WaitJudges: the unit judge reads systemd", () => {
  /** A fake `systemctl`: a shell script that prints what a test gives it. */
  const fakeSystemctl = (body: string) =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const root = yield* fs.makeTempDirectoryScoped({ prefix: "wait-unit-" });
      const file = path.join(root, "systemctl");
      yield* fs.writeFileString(file, `#!/bin/sh\n${body}\n`);
      yield* fs.chmod(file, 0o755);
      return file;
    });
  const judgeWith = (body: string) =>
    Effect.gen(function* () {
      const command = yield* fakeSystemctl(body);
      return yield* makeUnitJudge({ command });
    });

  it.live(
    "a unit systemd does not know counts as ended; inactive and failed are met; active is not yet",
    () =>
      Effect.gen(function* () {
        const unknown = yield* judgeWith(
          '[ "$1 $2 $3 $4 $5" = "--user show --property=ActiveState,LoadState -- nope.service" ] || exit 9\n' +
            "printf 'ActiveState=inactive\\nLoadState=not-found\\n'",
        );
        const answer = yield* unknown(unitTarget("nope.service"), context);
        assert.strictEqual(answer.type, "met");
        assert.isTrue(answer.type === "met" && answer.outcome === "ended");
        assert.include(answer.type === "met" ? answer.detail : "", "does not know this unit");

        const failed = yield* judgeWith("printf 'ActiveState=failed\\nLoadState=loaded\\n'");
        assert.deepStrictEqual(yield* failed(unitTarget("x.service", "system"), context), {
          type: "met",
          outcome: "ended",
          detail: "It is failed.",
        });
        const active = yield* judgeWith("printf 'ActiveState=active\\nLoadState=loaded\\n'");
        assert.deepStrictEqual(yield* active(unitTarget("x.service"), context), {
          type: "notYet",
          detail: "It is active.",
        });
      }).pipe(Effect.scoped, Effect.provide(runnerLayer)),
  );

  it.live("without systemd the answer is a permanent 'not available', never met", () =>
    Effect.gen(function* () {
      const notBooted = yield* judgeWith(
        "echo 'System has not been booted with systemd as init system (PID 1).' >&2\n" +
          "printf 'ActiveState=inactive\\n'\nexit 1",
      );
      const missing = yield* makeUnitJudge({ command: "/nonexistent/wait-systemctl" });
      for (const judge of [notBooted, missing]) {
        const answer = yield* judge(unitTarget("x.service"), context);
        assert.isTrue(answer.type === "failed" && answer.permanent, JSON.stringify(answer));
        assert.include(failedText(answer), "not available");
      }
      // Off Linux nothing is run at all.
      const elsewhere = yield* makeUnitJudge({ command: "/nonexistent/wait-systemctl" }).pipe(
        Effect.provideService(HostProcess.Platform, "darwin"),
      );
      const answer = yield* elsewhere(unitTarget("x.service"), context);
      assert.isTrue(answer.type === "failed" && answer.permanent);
      assert.include(failedText(answer), "not available");
    }).pipe(Effect.scoped, Effect.provide(runnerLayer)),
  );

  it.live("an answer systemctl cannot give is a failed read that is tried again", () =>
    Effect.gen(function* () {
      const broken = yield* judgeWith(
        "echo 'Failed to connect to bus: No medium found' >&2\nexit 1",
      );
      const answer = yield* broken(unitTarget("x.service"), context);
      assert.isTrue(answer.type === "failed" && !answer.permanent);
      assert.include(failedText(answer), "Failed to connect to bus");
      const empty = yield* judgeWith("exit 0");
      const nothing = yield* empty(unitTarget("x.service"), context);
      assert.isTrue(nothing.type === "failed" && !nothing.permanent);
      const loud = yield* judgeWith("yes ActiveState=inactive | head -c 3000000");
      const tooMuch = yield* loud(unitTarget("x.service"), context);
      assert.isTrue(tooMuch.type === "failed" && !tooMuch.permanent);
    }).pipe(Effect.scoped, Effect.provide(runnerLayer)),
  );
});

describe("WaitJudges: a check runs under timeout only where timeout works", () => {
  /** Like GNU coreutils `timeout`: takes `-k N` and `--kill-after=N`, marks that it ran. */
  const GNU_LIKE = `#!/bin/sh
touch "$0.used"
case "$1" in
  -k) shift 2 ;;
  --kill-after=*) shift ;;
  *) echo "timeout: invalid option" >&2; exit 125 ;;
esac
shift
exec "$@"
`;
  /** Like an older BusyBox `timeout` (\`-t SECS\`, no \`-k\`): its usage error exits 1. */
  const OLD_BUSYBOX_LIKE = `#!/bin/sh
touch "$0.used"
if [ "$1" = "-t" ]; then shift 2; exec "$@"; fi
echo "timeout: unrecognized option '$1'" >&2
echo "Usage: timeout [-t SECS] [-s SIG] PROG ARGS" >&2
exit 1
`;
  const check = (argv: ReadonlyArray<string>): WaitCheck => ({
    argv: [...argv],
    maxArgs: 0,
    argPattern: "^[a-z]+$",
    intervalSeconds: 30,
    timeoutSeconds: 20,
    description: "",
  });
  const layer = Layer.mergeAll(
    runnerLayer,
    ServerSettingsService.layerTest({
      waitChecks: {
        yes: check(["/bin/sh", "-c", "exit 0"]),
        no: check(["/bin/sh", "-c", "exit 1"]),
      },
    }),
  );
  /** Like GNU coreutils `timeout` on a very busy host: its first run hangs, later ones work. */
  const HANGS_ONCE = `#!/bin/sh
if [ ! -e "$0.hung" ]; then touch "$0.hung"; exec sleep 30; fi
${GNU_LIKE.split("\n").slice(1).join("\n")}`;
  const fakes = Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const path = yield* Path.Path;
    const root = yield* fs.makeTempDirectoryScoped({ prefix: "wait-timeout-" });
    const write = (name: string, script: string) =>
      Effect.gen(function* () {
        const file = path.join(root, name);
        yield* fs.writeFileString(file, script);
        yield* fs.chmod(file, 0o755);
        return file;
      });
    return {
      gnu: yield* write("gnu-timeout", GNU_LIKE),
      old: yield* write("old-busybox-timeout", OLD_BUSYBOX_LIKE),
      hangsOnce: yield* write("busy-timeout", HANGS_ONCE),
      used: (file: string) => fs.exists(`${file}.used`),
    };
  });
  const place = () => Effect.succeed<CheckPlace>({ type: "ok", projectId: PROJECT, cwd: "/" });

  // The fakes are shell scripts; the judge is told it runs on Linux, where it uses `timeout`.
  const SH = NodeFS.existsSync("/bin/sh");
  it.live.skipIf(!SH)(
    "a timeout that refuses -k is not used, so a check that exits 0 is met, never 'not yet'",
    () =>
      Effect.gen(function* () {
        const { gnu, old, used } = yield* fakes;
        assert.strictEqual(yield* testTimeoutProgram(gnu), "works");
        assert.strictEqual(yield* testTimeoutProgram(old), "refused");
        assert.deepStrictEqual(yield* findTimeoutProgram([old, gnu]), {
          program: gnu,
          known: true,
        });
        assert.deepStrictEqual(yield* findTimeoutProgram([old, "/nonexistent/timeout"]), {
          program: null,
          known: true,
        });

        const plain = yield* makeCheckJudge({ placeOf: place, timeoutPrograms: [old] });
        assert.strictEqual((yield* plain(checkTarget("yes"), context)).type, "met");
        assert.strictEqual((yield* plain(checkTarget("no"), context)).type, "notYet");

        const wrapped = yield* makeCheckJudge({ placeOf: place, timeoutPrograms: [old, gnu] });
        assert.strictEqual((yield* wrapped(checkTarget("yes"), context)).type, "met");
        assert.strictEqual((yield* wrapped(checkTarget("no"), context)).type, "notYet");
        assert.isTrue(yield* used(gnu), "the check ran under the working timeout");
      }).pipe(
        Effect.scoped,
        Effect.provide(layer),
        Effect.provideService(HostProcess.Platform, "linux"),
      ),
  );

  it.effect.skipIf(!SH)(
    "a timeout whose test run hangs is tested again later, and the log names a hang (#108)",
    () => {
      const warnings: string[] = [];
      const logger = Logger.make(({ logLevel, message }) => {
        if (logLevel === "Warn")
          warnings.push(String(Array.isArray(message) ? message[0] : message));
      });
      return Effect.gen(function* () {
        const { hangsOnce, used } = yield* fakes;
        const timing = { limitMs: 300, retryMs: 1_000 };
        const judge = yield* makeCheckJudge({
          placeOf: place,
          timeoutPrograms: [hangsOnce],
          timeoutTest: timing,
        });
        // The test run hangs: the check runs without the wrapper, and is answered all the same.
        assert.strictEqual((yield* judge(checkTarget("yes"), context)).type, "met");
        assert.isFalse(yield* used(hangsOnce), "no wrapper while its test run said nothing");
        assert.strictEqual(warnings.length, 1);
        assert.include(warnings[0] ?? "", "did not answer its test run");
        assert.notInclude(warnings[0] ?? "", "does not take");
        // Before the retry time it is not tested again.
        assert.strictEqual((yield* judge(checkTarget("no"), context)).type, "notYet");
        assert.isFalse(yield* used(hangsOnce));
        // After it, the test run works, and from then on checks run under it.
        yield* TestClock.adjust(1_100);
        assert.strictEqual((yield* judge(checkTarget("yes"), context)).type, "met");
        assert.isTrue(yield* used(hangsOnce), "the check ran under the timeout found later");
        assert.strictEqual((yield* judge(checkTarget("no"), context)).type, "notYet");
        assert.strictEqual(warnings.length, 1, "nothing more is logged once it works");
      }).pipe(
        Effect.scoped,
        Effect.provide(Layer.merge(layer, Logger.layer([logger], { mergeWithExisting: false }))),
        Effect.provideService(HostProcess.Platform, "linux"),
      );
    },
  );

  const BUSYBOX = ["/usr/bin/busybox", "/bin/busybox"].find((file) => NodeFS.existsSync(file));
  it.live.skipIf(!SH || BUSYBOX === undefined)(
    "BusyBox's own timeout runs a check, and a check that exits 0 is met",
    () =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const path = yield* Path.Path;
        const root = yield* fs.makeTempDirectoryScoped({ prefix: "wait-busybox-" });
        const timeout = path.join(root, "timeout");
        yield* fs.symlink(BUSYBOX!, timeout);
        const judge = yield* makeCheckJudge({ placeOf: place, timeoutPrograms: [timeout] });
        assert.strictEqual((yield* judge(checkTarget("yes"), context)).type, "met");
        assert.strictEqual((yield* judge(checkTarget("no"), context)).type, "notYet");
        const line = checkCommandLine(check(["/bin/sh", "-c", "exit 0"]), [], timeout);
        assert.deepStrictEqual(line.args.slice(0, 3), ["-k", "2", "25"]);
      }).pipe(
        Effect.scoped,
        Effect.provide(layer),
        Effect.provideService(HostProcess.Platform, "linux"),
      ),
  );
});
