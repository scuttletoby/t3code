// @effect-diagnostics nodeBuiltinImport:off - fake adapter steps are shell scripts that meet the test at named pipes.
/**
 * Fork (playtest): the service with a fake adapter. Every step is one shell script that logs the
 * facts it was given, waits at a named pipe when the test made one, prints `<step>.out` and exits
 * with `<step>.code`. No sleeps: a test meets a running step at its pipes.
 */
import * as NodeChildProcess from "node:child_process";
import * as NodeFS from "node:fs";

import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import { type PlaytestFailure, ProjectId, ThreadId } from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import { SpawnExecutableResolution } from "@t3tools/shared/shell";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";
import * as Scope from "effect/Scope";

import * as ProcessRunner from "../processRunner.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import * as PlaytestService from "./PlaytestService.ts";
import { PLAYTEST_STORE_FILE } from "./PlaytestStore.ts";

const THREAD = ThreadId.make("thread-a");
const OTHER_THREAD = ThreadId.make("thread-b");
const PROJECT = ProjectId.make("project-a");

const SCRIPT = `#!/bin/sh
d="$1"; s="$T3_PLAYTEST_STEP"
printf '%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' "$s" "$T3_PLAYTEST_ROUND" "$T3_PLAYTEST_SESSION" "$T3_PLAYTEST_TARGET" "$T3_PLAYTEST_NOTE" "$T3_PLAYTEST_PROJECT" "$T3_PLAYTEST_THREAD" "$T3_PLAYTEST_WORKTREE" "$(pwd)" >> "$d/log"
if [ -p "$d/$s.started" ]; then echo started > "$d/$s.started"; fi
if [ -p "$d/$s.gate" ]; then read -r _ < "$d/$s.gate"; fi
if [ -f "$d/$s.out" ]; then cat "$d/$s.out"; fi
echo "finished|$s" >> "$d/log"
if [ -f "$d/$s.code" ]; then exit "$(cat "$d/$s.code")"; fi
exit 0
`;

/** A temp folder with the step script, the store file's path and a worktree. */
const fixture = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const root = yield* fs.makeTempDirectoryScoped({ prefix: "playtest-service-" });
  const script = path.join(root, "step.sh");
  yield* fs.writeFileString(script, SCRIPT);
  yield* fs.chmod(script, 0o755);
  const worktree = path.join(root, "work");
  yield* fs.makeDirectory(worktree);
  /** A folder for one adapter's log and switches. */
  const adapterDir = (name: string) =>
    Effect.gen(function* () {
      const dir = path.join(root, name);
      yield* fs.makeDirectory(dir);
      return dir;
    });
  return {
    root,
    script,
    worktree,
    adapterDir,
    filePath: path.join(root, PLAYTEST_STORE_FILE),
    // Not made yet: the service makes it when a release runs.
    stepsFolder: path.join(root, "state", "playtest"),
  };
});
type Fixture = Effect.Success<typeof fixture>;

const step = (fx: Fixture, dir: string, timeoutSeconds = 30) => ({
  argv: [fx.script, dir],
  timeoutSeconds,
  description: "",
});
/** A complete adapter whose steps log to `dir`. */
const adapter = (fx: Fixture, dir: string, overrides: Record<string, unknown> = {}) => ({
  enabled: true,
  claim: step(fx, dir),
  renew: step(fx, dir),
  push: [
    { ...step(fx, dir), name: "build" },
    { ...step(fx, dir), name: "deploy" },
  ],
  release: step(fx, dir),
  renewMinutes: 10,
  idleMinutes: 90,
  maxHours: 6,
  ...overrides,
});

const settingsWith = (playtest: unknown) =>
  Effect.service(ServerSettingsService).pipe(
    Effect.provide(ServerSettingsService.layerTest({ playtest } as never)),
  );

const okPlace = (worktree: string) => () =>
  Effect.succeed<PlaytestService.PlaytestPlace>({ type: "ok", projectId: PROJECT, worktree });

const makeService = (
  fx: Fixture,
  settings: ServerSettingsService["Service"],
  options: {
    readonly placeOf?: (threadId: ThreadId) => Effect.Effect<PlaytestService.PlaytestPlace>;
    readonly fs?: FileSystem.FileSystem;
    readonly runner?: ProcessRunner.ProcessRunner["Service"];
  } = {},
) => {
  const made = PlaytestService.make({
    filePath: fx.filePath,
    stepsFolder: fx.stepsFolder,
    placeOf: options.placeOf ?? okPlace(fx.worktree),
  }).pipe(Effect.provideService(ServerSettingsService, settings));
  const withRunner =
    options.runner === undefined
      ? made.pipe(Effect.provide(ProcessRunner.layer))
      : made.pipe(Effect.provideService(ProcessRunner.ProcessRunner, options.runner));
  return options.fs === undefined
    ? withRunner
    : withRunner.pipe(Effect.provideService(FileSystem.FileSystem, options.fs));
};

/**
 * A FileSystem whose renames (the last part of every store write) a test can hold or fail. A held
 * write cannot be interrupted, so an interrupt sent meanwhile lands right after it.
 */
const fsControl = Effect.gen(function* () {
  const real = yield* FileSystem.FileSystem;
  let hold: {
    readonly reached: Deferred.Deferred<void>;
    readonly gate: Deferred.Deferred<void>;
  } | null = null;
  let fail = false;
  const fs: FileSystem.FileSystem = {
    ...real,
    rename: (from, to) =>
      Effect.suspend(() => {
        if (fail) {
          return Effect.fail(
            PlatformError.systemError({
              _tag: "Unknown",
              module: "FileSystem",
              method: "rename",
              description: "disk full",
              pathOrDescriptor: to,
            }),
          );
        }
        const held = hold;
        if (held === null) return real.rename(from, to);
        hold = null;
        return Deferred.succeed(held.reached, undefined).pipe(
          Effect.andThen(Deferred.await(held.gate)),
          Effect.andThen(real.rename(from, to)),
          Effect.uninterruptible,
        );
      }),
  };
  return {
    fs,
    /** The next store write waits: `reached` when it does, `release` lets it finish. */
    holdNextWrite: Effect.gen(function* () {
      const reached = yield* Deferred.make<void>();
      const gate = yield* Deferred.make<void>();
      hold = { reached, gate };
      return { reached: Deferred.await(reached), release: Deferred.succeed(gate, undefined) };
    }),
    failWrites: (on: boolean) => Effect.sync(() => void (fail = on)),
  };
});

/** The real runner, which tells a test when the next step's process has returned. */
const runnerControl = Effect.gen(function* () {
  const real = yield* Effect.service(ProcessRunner.ProcessRunner).pipe(
    Effect.provide(ProcessRunner.layer),
  );
  let returned: Deferred.Deferred<void> | null = null;
  const runner = ProcessRunner.ProcessRunner.of({
    run: (input) =>
      real.run(input).pipe(
        Effect.tap(() =>
          Effect.suspend(() => {
            const signal = returned;
            returned = null;
            return signal === null ? Effect.void : Deferred.succeed(signal, undefined);
          }),
        ),
      ),
  });
  return {
    runner,
    /** Resolves once the next step returns; its fiber then goes straight on to wait for the lock. */
    nextReturn: Effect.gen(function* () {
      const signal = yield* Deferred.make<void>();
      returned = signal;
      return Deferred.await(signal);
    }),
  };
});

/** A stored session as `playtest.json` holds it. */
const storedSession = (fx: Fixture, dir: string, overrides: Record<string, unknown> = {}) => {
  const { enabled: _enabled, ...steps } = adapter(fx, dir);
  return {
    id: "pt_aaaaaaaaaa",
    threadId: THREAD,
    projectId: PROJECT,
    target: "dev-1",
    note: "",
    worktree: fx.worktree,
    adapter: steps,
    state: "active",
    startedAt: "2026-11-17T11:00:00.000Z",
    lastActivityAt: "2026-11-17T11:00:00.000Z",
    rounds: 0,
    ...overrides,
  };
};
const writeStore = (fx: Fixture, sessions: ReadonlyArray<unknown>) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    yield* fs.writeFileString(fx.filePath, `${JSON.stringify({ version: 1, sessions })}\n`);
  });
/** A push step that runs `/bin/sh -c script`. */
const shellStep = (name: string, script: string) => ({
  argv: ["/bin/sh", "-c", script],
  timeoutSeconds: 30,
  description: "",
  name,
});

/** The steps that ran, as `step:round`, and `finished|step` for those that got to their end. */
const ran = (dir: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const text = yield* fs.readFileString(`${dir}/log`).pipe(Effect.orElseSucceed(() => ""));
    return text
      .split("\n")
      .filter((line) => line !== "")
      .map((line) => {
        const [name, round] = line.split("|");
        return name === "finished" ? line : `${name}:${round}`;
      });
  });
const logLines = (dir: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    return (yield* fs.readFileString(`${dir}/log`)).split("\n").filter((line) => line !== "");
  });

const setFile = (dir: string, name: string, text: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    yield* fs.writeFileString(`${dir}/${name}`, text);
  });
const removeFile = (dir: string, name: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    yield* fs.remove(`${dir}/${name}`);
  });
/** Makes `<step>` stop at its pipes: it says when it started and waits for `openGate`. */
const holdStep = (dir: string, name: string) =>
  Effect.sync(() => {
    NodeChildProcess.execFileSync("mkfifo", [`${dir}/${name}.started`, `${dir}/${name}.gate`]);
  });
const stepStarted = (dir: string, name: string) =>
  Effect.promise(() => NodeFS.promises.readFile(`${dir}/${name}.started`, "utf8"));
const openGate = (dir: string, name: string) =>
  Effect.promise(() => NodeFS.promises.writeFile(`${dir}/${name}.gate`, "go\n"));
/** Process ids of steps that log to `dir`. */
const stepProcesses = (dir: string) => {
  try {
    return NodeChildProcess.execFileSync("pgrep", ["-f", dir]).toString().trim();
  } catch {
    return "";
  }
};

const refusal = <A>(effect: Effect.Effect<A, PlaytestFailure>) =>
  Effect.flip(effect).pipe(Effect.map((error) => ({ code: error.code, message: error.message })));

/** Sets (undefined: removes) the server's env vars; returns what they were. */
function setServerEnv(values: Record<string, string | undefined>) {
  const before = Object.fromEntries(Object.keys(values).map((key) => [key, process.env[key]]));
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  return before;
}

const live = <A, E>(effect: Effect.Effect<A, E, NodeServices.NodeServices | Scope.Scope>) =>
  effect.pipe(Effect.scoped, Effect.provide(NodeServices.layer));

describe.skipIf(HostProcess.Platform.defaultValue() === "win32")(
  "PlaytestService with a fake adapter",
  () => {
    it.live(
      "start claims, push runs the steps in order, end releases; every step gets the session facts",
      () =>
        live(
          Effect.gen(function* () {
            const fx = yield* fixture;
            const dir = yield* fx.adapterDir("a");
            const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
            yield* setFile(dir, "deploy.out", "deployed to dev-1");

            const started = yield* service.start({
              threadId: THREAD,
              target: "dev-1",
              note: "  try the\n  new  shop ",
            });
            assert.strictEqual(started.outcome, "started");
            assert.strictEqual(started.session.state, "active");
            assert.strictEqual(started.session.note, "try the new shop");
            assert.notProperty(started.session, "adapter");
            const id = started.session.id;
            assert.match(id, /^pt_[a-z2-7]{10}$/);

            const first = yield* service.push({ threadId: THREAD });
            assert.strictEqual(first.outcome, "done");
            assert.deepInclude(first.session.lastRound, {
              number: 1,
              state: "done",
              step: "deploy",
              detail: "deployed to dev-1",
            });
            assert.strictEqual((yield* service.push({ threadId: THREAD })).session.rounds, 2);

            const ended = yield* service.end({ threadId: THREAD, reason: "agent" });
            assert.strictEqual(ended.outcome, "ended");
            assert.deepInclude(ended.session, { state: "ended", endReason: "agent" });
            assert.deepStrictEqual(
              (yield* ran(dir)).filter((line) => !line.startsWith("finished")),
              ["claim:0", "build:1", "deploy:1", "build:2", "deploy:2", "release:2"],
            );
            const facts = (yield* logLines(dir))[0]!.split("|");
            assert.deepStrictEqual(facts, [
              "claim",
              "0",
              id,
              "dev-1",
              "try the new shop",
              PROJECT,
              THREAD,
              fx.worktree,
              fx.worktree,
            ]);
            // Release runs in T3's own folder; claim and push in the worktree.
            const pwds = (yield* logLines(dir))
              .filter((line) => !line.startsWith("finished"))
              .map((line) => `${line.split("|")[0]}:${line.split("|")[8]}`);
            assert.deepStrictEqual(pwds, [
              `claim:${fx.worktree}`,
              `build:${fx.worktree}`,
              `deploy:${fx.worktree}`,
              `build:${fx.worktree}`,
              `deploy:${fx.worktree}`,
              `release:${fx.stepsFolder}`,
            ]);

            // Runs again: nothing to end, and status shows the last session.
            const again = yield* service.end({ threadId: THREAD, reason: "person" });
            assert.strictEqual(again.outcome, "nothing-to-end");
            assert.strictEqual(again.session?.id, id);
            assert.strictEqual((yield* service.status({ threadId: THREAD }))?.id, id);
            assert.isNull(yield* service.status({ threadId: OTHER_THREAD }));
            assert.strictEqual(
              (yield* refusal(service.push({ threadId: THREAD }))).code,
              "no_session",
            );
            // A new session for the thread works after the old one ended.
            const next = yield* service.start({ threadId: THREAD, target: "dev-2" });
            assert.strictEqual(next.outcome, "started");
            assert.notStrictEqual(next.session.id, id);
          }),
        ),
    );

    it.live("start for the target that is on is the same session; another target is refused", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));

          const first = yield* service.start({ threadId: THREAD, target: "dev-1" });
          const second = yield* service.start({ threadId: THREAD, target: "dev-1" });
          assert.strictEqual(second.outcome, "already-on");
          assert.strictEqual(second.session.id, first.session.id);
          const other = yield* refusal(service.start({ threadId: THREAD, target: "dev-2" }));
          assert.strictEqual(other.code, "busy");
          assert.include(other.message, "already playtests dev-1");
          // Another thread has its own session.
          assert.strictEqual(
            (yield* service.start({ threadId: OTHER_THREAD, target: "dev-2" })).outcome,
            "started",
          );
          assert.deepStrictEqual(
            (yield* ran(dir)).filter((line) => !line.startsWith("finished")),
            ["claim:0", "claim:0"],
          );
        }),
      ),
    );

    it.live("a refused claim (exit 10) ends the session without a release", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          yield* setFile(dir, "claim.code", "10");
          yield* setFile(dir, "claim.out", "dev-1 is held by another session");

          const refused = yield* service.start({ threadId: THREAD, target: "dev-1" });
          assert.strictEqual(refused.outcome, "refused");
          assert.deepInclude(refused.session, { state: "ended", endReason: "claim-refused" });
          assert.include(refused.session.problem, "dev-1 is held by another session");
          assert.deepStrictEqual(yield* ran(dir), ["claim:0", "finished|claim"]);

          // Runs again once the target is free.
          yield* removeFile(dir, "claim.code");
          assert.strictEqual(
            (yield* service.start({ threadId: THREAD, target: "dev-1" })).outcome,
            "started",
          );
        }),
      ),
    );

    it.live(
      "a failed claim (other exit, timeout) runs release, as the claim may be half taken",
      () =>
        live(
          Effect.gen(function* () {
            const fx = yield* fixture;
            const dir = yield* fx.adapterDir("a");
            const service = yield* makeService(
              fx,
              yield* settingsWith(adapter(fx, dir, { claim: step(fx, dir, 1) })),
            );
            yield* setFile(dir, "claim.code", "3");

            const failed = yield* service.start({ threadId: THREAD, target: "dev-1" });
            assert.strictEqual(failed.outcome, "failed");
            assert.deepInclude(failed.session, { state: "ended", endReason: "claim-failed" });
            assert.include(failed.session.problem, "Exit code 3.");
            assert.deepStrictEqual(
              (yield* ran(dir)).filter((line) => !line.startsWith("finished")),
              ["claim:0", "release:0"],
            );

            // A claim that hangs is stopped at its limit and released the same way.
            yield* removeFile(dir, "claim.code");
            yield* holdStep(dir, "claim");
            const timedOut = yield* service.start({ threadId: THREAD, target: "dev-1" });
            assert.strictEqual(timedOut.outcome, "failed");
            assert.include(timedOut.session.problem, "Timed out after 1s.");
            assert.deepStrictEqual((yield* ran(dir)).slice(-3), [
              "claim:0",
              "release:0",
              "finished|release",
            ]);
            assert.strictEqual(stepProcesses(dir), "");
          }),
        ),
    );

    it.live("a round stops at the first step that does not succeed", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          yield* service.start({ threadId: THREAD, target: "dev-1" });

          yield* setFile(dir, "build.code", "10");
          yield* setFile(dir, "build.out", "the guard says no");
          const refused = yield* service.push({ threadId: THREAD });
          assert.strictEqual(refused.outcome, "refused");
          assert.deepInclude(refused.session.lastRound, {
            state: "refused",
            step: "build",
            detail: "the guard says no",
          });

          yield* setFile(dir, "build.code", "2");
          const failed = yield* service.push({ threadId: THREAD });
          assert.strictEqual(failed.outcome, "failed");
          assert.include(failed.session.lastRound?.detail, "Exit code 2.");

          assert.deepStrictEqual(
            (yield* ran(dir)).filter((line) => !line.startsWith("finished")),
            ["claim:0", "build:1", "build:2"],
          );
          // The session stays on: the next round runs.
          yield* removeFile(dir, "build.code");
          assert.strictEqual((yield* service.push({ threadId: THREAD })).outcome, "done");
        }),
      ),
    );

    it.live("one step at a time: a push or start while a round runs is refused as busy", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          yield* service.start({ threadId: THREAD, target: "dev-1" });
          yield* holdStep(dir, "build");

          const round = yield* Effect.forkChild(service.push({ threadId: THREAD }));
          yield* stepStarted(dir, "build");
          const second = yield* refusal(service.push({ threadId: THREAD }));
          assert.strictEqual(second.code, "busy");
          assert.include(second.message, "Round 1 runs (step build)");
          assert.strictEqual(
            (yield* refusal(service.start({ threadId: THREAD, target: "dev-2" }))).code,
            "busy",
          );
          assert.strictEqual((yield* service.status({ threadId: THREAD }))?.running, "round");

          yield* openGate(dir, "build");
          assert.strictEqual((yield* Fiber.join(round)).outcome, "done");
          assert.isUndefined((yield* service.status({ threadId: THREAD }))?.running);
        }),
      ),
    );

    it.live("two starts at once: the second is refused while the claim runs", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          yield* holdStep(dir, "claim");

          const first = yield* Effect.forkChild(
            service.start({ threadId: THREAD, target: "dev-1" }),
          );
          yield* stepStarted(dir, "claim");
          const second = yield* refusal(service.start({ threadId: THREAD, target: "dev-1" }));
          assert.strictEqual(second.code, "busy");
          assert.include(second.message, "runs; wait for it");
          assert.strictEqual((yield* refusal(service.push({ threadId: THREAD }))).code, "busy");

          yield* openGate(dir, "claim");
          assert.strictEqual((yield* Fiber.join(first)).outcome, "started");
          assert.deepStrictEqual(yield* ran(dir), ["claim:0", "finished|claim"]);
        }),
      ),
    );

    it.live("end stops a running round (its process too), then releases", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          yield* service.start({ threadId: THREAD, target: "dev-1" });
          yield* holdStep(dir, "deploy");

          const round = yield* Effect.forkChild(service.push({ threadId: THREAD }));
          yield* stepStarted(dir, "deploy");
          assert.notStrictEqual(stepProcesses(dir), "");
          const ended = yield* service.end({ threadId: THREAD, reason: "person" });
          assert.strictEqual(ended.outcome, "ended");
          assert.deepInclude(ended.session?.lastRound, { number: 1, state: "stopped" });
          // Gone when end returns, long before the step's own time limit.
          assert.strictEqual(stepProcesses(dir), "");
          const pushed = yield* Fiber.join(round);
          assert.strictEqual(pushed.outcome, "stopped");

          const lines = yield* ran(dir);
          assert.deepStrictEqual(lines.slice(-2), ["release:1", "finished|release"]);
          assert.notInclude(lines, "finished|deploy");
        }),
      ),
    );

    it.live("end during the claim stops it (its process too) and releases", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          yield* holdStep(dir, "claim");

          const starting = yield* Effect.forkChild(
            service.start({ threadId: THREAD, target: "dev-1" }),
          );
          yield* stepStarted(dir, "claim");
          const ended = yield* service.end({ threadId: THREAD, reason: "agent" });
          assert.deepInclude(ended.session, { state: "ended", endReason: "agent" });
          assert.strictEqual(stepProcesses(dir), "");
          const started = yield* Fiber.join(starting);
          assert.strictEqual(started.outcome, "stopped");
          assert.deepStrictEqual(yield* ran(dir), ["claim:0", "release:0", "finished|release"]);
        }),
      ),
    );

    it.live("two ends at once share one release", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          yield* service.start({ threadId: THREAD, target: "dev-1" });
          yield* holdStep(dir, "release");

          const first = yield* Effect.forkChild(service.end({ threadId: THREAD, reason: "agent" }));
          yield* stepStarted(dir, "release");
          const second = yield* Effect.forkChild(
            service.end({ threadId: THREAD, reason: "person" }),
            {
              startImmediately: true,
            },
          );
          assert.strictEqual((yield* service.status({ threadId: THREAD }))?.running, "release");
          // A second release would wait at no pipe and log at once.
          yield* removeFile(dir, "release.started");
          yield* openGate(dir, "release");

          const results = [yield* Fiber.join(first), yield* Fiber.join(second)];
          assert.deepStrictEqual(
            results.map((result) => [result.outcome, result.session?.endReason]),
            [
              ["ended", "agent"],
              ["ended", "agent"],
            ],
          );
          assert.deepStrictEqual(
            (yield* ran(dir)).filter((line) => line.startsWith("release")),
            ["release:0"],
          );
        }),
      ),
    );

    it.live("a session keeps its steps: edited or switched-off settings still release it", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const edited = yield* fx.adapterDir("b");
          const settings = yield* settingsWith(adapter(fx, dir));
          const service = yield* makeService(fx, settings);
          yield* service.start({ threadId: THREAD, target: "dev-1" });

          yield* settings.updateSettings({ playtest: adapter(fx, edited) } as never);
          assert.strictEqual((yield* service.push({ threadId: THREAD })).outcome, "done");

          yield* settings.updateSettings({
            playtest: adapter(fx, edited, { enabled: false }),
          } as never);
          const off = yield* refusal(service.push({ threadId: THREAD }));
          assert.strictEqual(off.code, "playtest_off");
          assert.include(off.message, "end the session");

          assert.strictEqual(
            (yield* service.end({ threadId: THREAD, reason: "agent" })).outcome,
            "ended",
          );
          assert.deepStrictEqual(
            (yield* ran(dir)).filter((line) => !line.startsWith("finished")),
            ["claim:0", "build:1", "deploy:1", "release:1"],
          );
          assert.deepStrictEqual(yield* ran(edited), []);
        }),
      ),
    );

    it.live("a failed release keeps the session ending and busy; end again retries it", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          yield* service.start({ threadId: THREAD, target: "dev-1" });
          yield* setFile(dir, "release.code", "4");

          const failed = yield* service.end({ threadId: THREAD, reason: "agent" });
          assert.strictEqual(failed.outcome, "release-failed");
          assert.deepInclude(failed.session, { state: "ending", endReason: "agent" });
          assert.include(failed.session?.problem, "release: Exit code 4.");
          const start = yield* refusal(service.start({ threadId: THREAD, target: "dev-1" }));
          assert.strictEqual(start.code, "busy");
          assert.include(start.message, "call end again");
          assert.strictEqual((yield* refusal(service.push({ threadId: THREAD }))).code, "busy");

          yield* removeFile(dir, "release.code");
          const ended = yield* service.end({ threadId: THREAD, reason: "person" });
          assert.strictEqual(ended.outcome, "ended");
          // The first reason stands; the problem went with the release that worked.
          assert.strictEqual(ended.session?.endReason, "agent");
          assert.notProperty(ended.session, "problem");
        }),
      ),
    );

    it.live("after a stop: a cut round reads stopped, and a cut claim is released by end", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const settings = yield* settingsWith(adapter(fx, dir));

          // A server stop while a round runs: the step is killed and the round stays `running`.
          yield* Effect.scoped(
            Effect.gen(function* () {
              const service = yield* makeService(fx, settings);
              yield* service.start({ threadId: THREAD, target: "dev-1" });
              yield* holdStep(dir, "deploy");
              yield* Effect.forkScoped(service.push({ threadId: THREAD }));
              yield* stepStarted(dir, "deploy");
            }),
          );
          assert.strictEqual(stepProcesses(dir), "");
          yield* removeFile(dir, "deploy.gate");
          yield* removeFile(dir, "deploy.started");
          const restarted = yield* makeService(fx, settings);
          assert.deepInclude((yield* restarted.status({ threadId: THREAD }))?.lastRound, {
            number: 1,
            state: "stopped",
            detail: "T3 stopped while this round ran.",
          });
          assert.strictEqual((yield* restarted.push({ threadId: THREAD })).session.rounds, 2);

          // A stop while a claim runs: the next start is refused until an end releases it.
          yield* holdStep(dir, "claim");
          yield* Effect.scoped(
            Effect.gen(function* () {
              const service = yield* makeService(fx, settings);
              yield* Effect.forkScoped(service.start({ threadId: OTHER_THREAD, target: "dev-2" }));
              yield* stepStarted(dir, "claim");
            }),
          );
          const again = yield* makeService(fx, settings);
          assert.strictEqual((yield* again.status({ threadId: OTHER_THREAD }))?.state, "claiming");
          const start = yield* refusal(again.start({ threadId: OTHER_THREAD, target: "dev-2" }));
          assert.strictEqual(start.code, "busy");
          assert.include(start.message, "did not finish");
          const ended = yield* again.end({ threadId: OTHER_THREAD, reason: "person" });
          assert.strictEqual(ended.outcome, "ended");
          assert.deepStrictEqual((yield* ran(dir)).slice(-2), ["release:0", "finished|release"]);
        }),
      ),
    );

    it.live("refused before any step runs: off, a gone thread, a bad target or note", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const start = { threadId: THREAD, target: "dev-1" };

          const off = yield* makeService(fx, yield* settingsWith({ enabled: false }));
          assert.strictEqual((yield* refusal(off.start(start))).code, "playtest_off");
          // Off with every step set: the steps are kept, nothing runs.
          const offWithSteps = yield* makeService(
            fx,
            yield* settingsWith(adapter(fx, dir, { enabled: false })),
          );
          assert.strictEqual((yield* refusal(offWithSteps.start(start))).code, "playtest_off");

          const settings = yield* settingsWith(adapter(fx, dir));
          const gone = yield* makeService(fx, settings, {
            placeOf: () =>
              Effect.succeed({ type: "unavailable", message: "The thread is archived or gone." }),
          });
          assert.deepStrictEqual(yield* refusal(gone.start(start)), {
            code: "thread_unavailable",
            message: "The thread is archived or gone.",
          });
          assert.strictEqual(
            (yield* refusal(gone.push({ threadId: THREAD }))).code,
            "thread_unavailable",
          );

          const service = yield* makeService(fx, settings);
          for (const target of ["", "dev 1", "-dev", "a".repeat(65), "dev/1"]) {
            const bad = yield* refusal(service.start({ ...start, target }));
            assert.strictEqual(bad.code, "invalid", target);
          }
          assert.strictEqual(
            (yield* refusal(service.start({ ...start, note: "x".repeat(201) }))).code,
            "invalid",
          );
          // A NUL would fail every spawn, release too; no control character gets in.
          for (const note of ["a\u0000b", "bell\u0007", "del\u007f", "esc\u001b[0m"]) {
            const bad = yield* refusal(service.start({ ...start, note }));
            assert.deepStrictEqual(bad, {
              code: "invalid",
              message: "The note may not hold control characters.",
            });
          }
          assert.strictEqual(
            (yield* refusal(service.push({ threadId: THREAD }))).code,
            "no_session",
          );
          assert.deepStrictEqual(yield* ran(dir), []);
          const fs = yield* FileSystem.FileSystem;
          assert.isFalse(yield* fs.exists(fx.filePath));
        }),
      ),
    );

    it.live("a store file from a newer T3 refuses every call and is left as it is", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const fs = yield* FileSystem.FileSystem;
          const text = '{"version":2,"sessions":[]}\n';
          yield* fs.writeFileString(fx.filePath, text);
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));

          const start = yield* refusal(service.start({ threadId: THREAD, target: "dev-1" }));
          assert.strictEqual(start.code, "store_unavailable");
          assert.include(start.message, "newer T3");
          assert.strictEqual(
            (yield* refusal(service.status({ threadId: THREAD }))).code,
            "store_unavailable",
          );
          assert.strictEqual(yield* fs.readFileString(fx.filePath), text);
          assert.deepStrictEqual(yield* ran(dir), []);
        }),
      ),
    );

    it.live("a step that would need a shell on Windows (.cmd, .bat) fails without running", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));

          const failed = yield* service.start({ threadId: THREAD, target: "dev-1" });
          assert.strictEqual(failed.outcome, "failed");
          // Release needs a shell too, so it can never run: the session ends, nothing ran.
          assert.deepInclude(failed.session, { state: "ended", endReason: "claim-failed" });
          assert.strictEqual(
            failed.session.problem,
            `${PlaytestService.RELEASE_COULD_NOT_RUN}Step release names a program that needs a shell (a .cmd or .bat file); steps run without one.`,
          );
          assert.deepStrictEqual(yield* ran(dir), []);
        }).pipe(
          Effect.provideService(HostProcess.Platform, "win32"),
          Effect.provideService(SpawnExecutableResolution, () => "C:\\tools\\playtest.cmd"),
        ),
      ),
    );

    it.live("a removed worktree does not block the end: release runs in T3's own folder", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          yield* service.start({ threadId: THREAD, target: "dev-1" });
          const fs = yield* FileSystem.FileSystem;
          yield* fs.remove(fx.worktree, { recursive: true });

          // A push needs the worktree, so it fails; it never blocks the end.
          assert.strictEqual((yield* service.push({ threadId: THREAD })).outcome, "failed");
          const ended = yield* service.end({ threadId: THREAD, reason: "thread-gone" });
          assert.strictEqual(ended.outcome, "ended");
          assert.deepInclude(ended.session, { state: "ended", endReason: "thread-gone" });
          const release = (yield* logLines(dir)).find((line) => line.startsWith("release|"));
          assert.strictEqual(release?.split("|")[8], fx.stepsFolder);
        }),
      ),
    );

    it.live(
      "a release that cannot start ends the session with that reason; a new one may start",
      () =>
        live(
          Effect.gen(function* () {
            const fx = yield* fixture;
            const dir = yield* fx.adapterDir("a");
            const fs = yield* FileSystem.FileSystem;
            const releaseProgram = `${fx.root}/release.sh`;
            yield* fs.copyFile(fx.script, releaseProgram);
            yield* fs.chmod(releaseProgram, 0o755);
            const service = yield* makeService(
              fx,
              yield* settingsWith(
                adapter(fx, dir, { release: { ...step(fx, dir), argv: [releaseProgram, dir] } }),
              ),
            );
            yield* service.start({ threadId: THREAD, target: "dev-1" });
            // An adapter update moved the program away after the session started.
            yield* fs.remove(releaseProgram);

            const ended = yield* service.end({ threadId: THREAD, reason: "agent" });
            assert.strictEqual(ended.outcome, "ended");
            assert.deepInclude(ended.session, { state: "ended", endReason: "agent" });
            assert.strictEqual(
              ended.session?.problem,
              `${PlaytestService.RELEASE_COULD_NOT_RUN}It could not start.`,
            );
            assert.strictEqual(
              (yield* service.start({ threadId: THREAD, target: "dev-1" })).outcome,
              "started",
            );
          }),
        ),
    );

    it.live("a stored note with a NUL (an older T3 kept it) still releases, cleaned", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          yield* writeStore(fx, [storedSession(fx, dir, { note: "old\u0000note\u0007" })]);
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));

          const ended = yield* service.end({ threadId: THREAD, reason: "person" });
          assert.strictEqual(ended.outcome, "ended");
          const release = (yield* logLines(dir)).find((line) => line.startsWith("release|"));
          assert.strictEqual(release?.split("|")[4], "old note ");
        }),
      ),
    );

    it.live(
      "a step that returns while end holds the lock does not undo the end (claim, first, last step)",
      () =>
        live(
          Effect.gen(function* () {
            for (const held of ["claim", "build", "deploy"] as const) {
              const fx = yield* fixture;
              const dir = yield* fx.adapterDir("a");
              const files = yield* fsControl;
              const runs = yield* runnerControl;
              const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)), {
                fs: files.fs,
                runner: runs.runner,
              });
              yield* holdStep(dir, held);
              if (held !== "claim") yield* service.start({ threadId: THREAD, target: "dev-1" });
              const work = yield* Effect.forkChild(
                held === "claim"
                  ? Effect.map(
                      service.start({ threadId: THREAD, target: "dev-1" }),
                      (r) => r.outcome,
                    )
                  : Effect.map(service.push({ threadId: THREAD }), (r) => r.outcome),
              );
              yield* stepStarted(dir, held);

              // `end` takes the lock and its write waits; meanwhile the step returns.
              const write = yield* files.holdNextWrite;
              const ending = yield* Effect.forkChild(
                service.end({ threadId: THREAD, reason: "agent" }),
              );
              yield* write.reached;
              const returned = yield* runs.nextReturn;
              yield* openGate(dir, held);
              yield* returned;
              yield* write.release;

              const ended = yield* Fiber.join(ending);
              assert.strictEqual(ended.outcome, "ended", held);
              assert.deepInclude(ended.session, { state: "ended", endReason: "agent" }, held);
              assert.strictEqual(yield* Fiber.join(work), "stopped", held);
              const lines = yield* ran(dir);
              assert.include(lines, `release:${held === "claim" ? 0 : 1}`, held);
              if (held !== "claim") {
                // The round reads stopped at the step that ran; the next step never ran.
                assert.deepInclude(
                  ended.session?.lastRound,
                  { state: "stopped", step: held },
                  held,
                );
                if (held === "build") assert.notInclude(lines, "deploy:1");
              }
            }
          }),
        ),
    );

    it.live("a caller cancelled between the save and the step leaves no half state", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const files = yield* fsControl;
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)), {
            fs: files.fs,
          });

          // end: cancelled while `ending` is written, the release still starts.
          yield* service.start({ threadId: THREAD, target: "dev-1" });
          yield* holdStep(dir, "release");
          let write = yield* files.holdNextWrite;
          const ending = yield* Effect.forkChild(
            service.end({ threadId: THREAD, reason: "agent" }),
          );
          yield* write.reached;
          const stopEnd = yield* Effect.forkChild(Fiber.interrupt(ending));
          yield* write.release;
          yield* Fiber.join(stopEnd);
          assert.deepInclude(yield* service.status({ threadId: THREAD }), {
            state: "ending",
            running: "release",
          });
          yield* stepStarted(dir, "release");
          const again = yield* Effect.forkChild(service.end({ threadId: THREAD, reason: "agent" }));
          yield* openGate(dir, "release");
          assert.strictEqual((yield* Fiber.join(again)).outcome, "ended");
          yield* removeFile(dir, "release.started");
          yield* removeFile(dir, "release.gate");

          // start: cancelled while its session is written, the claim still starts.
          yield* holdStep(dir, "claim");
          write = yield* files.holdNextWrite;
          const starting = yield* Effect.forkChild(
            service.start({ threadId: OTHER_THREAD, target: "dev-1" }),
          );
          yield* write.reached;
          const stopStart = yield* Effect.forkChild(Fiber.interrupt(starting));
          yield* write.release;
          yield* Fiber.join(stopStart);
          assert.deepInclude(yield* service.status({ threadId: OTHER_THREAD }), {
            state: "claiming",
            running: "claim",
          });
          yield* stepStarted(dir, "claim");
          const ended = yield* service.end({ threadId: OTHER_THREAD, reason: "agent" });
          assert.strictEqual(ended.outcome, "ended");
        }),
      ),
    );

    it.live("a step that prints more than the cap keeps the end of its output", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const chatty =
            'i=0; while [ $i -lt 4000 ]; do echo "line $i of a chatty build"; i=$((i+1)); done; echo "error: the real end"; exit 3';
          const service = yield* makeService(
            fx,
            yield* settingsWith(adapter(fx, dir, { push: [shellStep("build", chatty)] })),
          );
          yield* service.start({ threadId: THREAD, target: "dev-1" });

          const failed = yield* service.push({ threadId: THREAD });
          assert.strictEqual(failed.outcome, "failed");
          const detail = failed.session.lastRound?.detail ?? "";
          assert.isTrue(
            detail.endsWith("line 3999 of a chatty build\nerror: the real end"),
            detail.slice(-80),
          );
          assert.isTrue(detail.startsWith("…"));
        }),
      ),
    );

    it.live("steps do not get the variables T3's terminal strips (PORT, T3CODE_*)", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const probe = { PORT: "4321", T3CODE_PLAYTEST_PROBE: "t3", PLAYTEST_PROBE_KEPT: "kept" };
          yield* Effect.acquireRelease(
            Effect.sync(() => setServerEnv(probe)),
            (before) => Effect.sync(() => setServerEnv(before)),
          );
          const show =
            'printf "%s|%s|%s" "${PORT-unset}" "${T3CODE_PLAYTEST_PROBE-unset}" "${PLAYTEST_PROBE_KEPT-unset}"';
          const service = yield* makeService(
            fx,
            yield* settingsWith(adapter(fx, dir, { push: [shellStep("show", show)] })),
          );
          yield* service.start({ threadId: THREAD, target: "dev-1" });
          const pushed = yield* service.push({ threadId: THREAD });
          assert.strictEqual(pushed.session.lastRound?.detail, "unset|unset|kept");
        }),
      ),
    );

    it.live("the session being worked on is never trimmed, even when its end reads oldest", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          // 100 ended sessions dated later than now: the clock went back.
          yield* writeStore(
            fx,
            Array.from({ length: 100 }, (_, index) =>
              storedSession(fx, dir, {
                id: `pt_aaaaaaaa${String.fromCharCode(97 + Math.floor(index / 26), 97 + (index % 26))}`,
                threadId: OTHER_THREAD,
                state: "ended",
                endReason: "agent",
                endedAt: "2099-01-01T00:00:00.000Z",
              }),
            ),
          );
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          const started = yield* service.start({ threadId: THREAD, target: "dev-1" });
          // All 100 loaded as they were (none dropped as bad).
          const fs = yield* FileSystem.FileSystem;
          assert.lengthOf(JSON.parse(yield* fs.readFileString(fx.filePath)).sessions, 101);
          const ended = yield* service.end({ threadId: THREAD, reason: "agent" });
          assert.strictEqual(ended.outcome, "ended");
          assert.strictEqual(ended.session?.id, started.session.id);
        }),
      ),
    );

    it.live("end when the store cannot be written refuses and changes nothing", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const files = yield* fsControl;
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)), {
            fs: files.fs,
          });
          yield* service.start({ threadId: THREAD, target: "dev-1" });

          yield* files.failWrites(true);
          const refused = yield* refusal(service.end({ threadId: THREAD, reason: "agent" }));
          assert.strictEqual(refused.code, "store_unavailable");
          assert.strictEqual((yield* service.status({ threadId: THREAD }))?.state, "active");
          assert.notInclude(yield* ran(dir), "release:0");

          yield* files.failWrites(false);
          assert.strictEqual(
            (yield* service.end({ threadId: THREAD, reason: "agent" })).outcome,
            "ended",
          );
        }),
      ),
    );

    it.live("a restart while ending: busy until end runs release again", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const settings = yield* settingsWith(adapter(fx, dir));
          yield* Effect.scoped(
            Effect.gen(function* () {
              const service = yield* makeService(fx, settings);
              yield* service.start({ threadId: THREAD, target: "dev-1" });
              yield* setFile(dir, "release.code", "4");
              const failed = yield* service.end({ threadId: THREAD, reason: "agent" });
              assert.strictEqual(failed.outcome, "release-failed");
            }),
          );

          const restarted = yield* makeService(fx, settings);
          const status = yield* restarted.status({ threadId: THREAD });
          assert.strictEqual(status?.state, "ending");
          assert.isUndefined(status?.running);
          const start = yield* refusal(restarted.start({ threadId: THREAD, target: "dev-1" }));
          assert.strictEqual(start.code, "busy");
          assert.include(start.message, "call end again");

          yield* removeFile(dir, "release.code");
          const ended = yield* restarted.end({ threadId: THREAD, reason: "person" });
          assert.strictEqual(ended.outcome, "ended");
          assert.strictEqual(ended.session?.endReason, "agent");
          assert.deepStrictEqual(
            (yield* ran(dir)).filter((line) => line.startsWith("release")),
            ["release:0", "release:0"],
          );
        }),
      ),
    );
  },
);
