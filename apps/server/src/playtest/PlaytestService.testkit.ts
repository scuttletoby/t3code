// @effect-diagnostics nodeBuiltinImport:off - fake adapter steps are shell scripts that meet the test at named pipes.
/**
 * Fork (playtest): shared test kit for the service with a fake adapter. Every step is one shell
 * script that logs the facts it was given, waits at a named pipe when the test made one, prints
 * `<step>.out` and exits with `<step>.code`. No sleeps: a test meets a running step at its pipes.
 */
import * as NodeChildProcess from "node:child_process";
import * as NodeFS from "node:fs";

import * as NodeServices from "@effect/platform-node/NodeServices";
import { type PlaytestFailure, ProjectId, ThreadId } from "@t3tools/contracts";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";
import * as Scope from "effect/Scope";

import * as ProcessRunner from "../processRunner.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import * as PlaytestService from "./PlaytestService.ts";
import { PLAYTEST_STORE_FILE } from "./PlaytestStore.ts";

export const THREAD = ThreadId.make("thread-a");
export const OTHER_THREAD = ThreadId.make("thread-b");
export const PROJECT = ProjectId.make("project-a");

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
export const fixture = Effect.gen(function* () {
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
export type Fixture = Effect.Success<typeof fixture>;

export const step = (fx: Fixture, dir: string, timeoutSeconds = 30) => ({
  argv: [fx.script, dir],
  timeoutSeconds,
  description: "",
});
/** A complete adapter whose steps log to `dir`. */
export const adapter = (fx: Fixture, dir: string, overrides: Record<string, unknown> = {}) => ({
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

export const settingsWith = (playtest: unknown) =>
  Effect.service(ServerSettingsService).pipe(
    Effect.provide(ServerSettingsService.layerTest({ playtest } as never)),
  );

const okPlace = (worktree: string) => () =>
  Effect.succeed<PlaytestService.PlaytestPlace>({ type: "ok", projectId: PROJECT, worktree });

export const makeService = (
  fx: Fixture,
  settings: ServerSettingsService["Service"],
  options: {
    readonly placeOf?: (threadId: ThreadId) => Effect.Effect<PlaytestService.PlaytestPlace>;
    readonly fs?: FileSystem.FileSystem;
    readonly runner?: ProcessRunner.ProcessRunner["Service"];
    readonly threadGone?: (threadId: ThreadId) => Effect.Effect<boolean | null>;
    readonly notify?: (
      notice: PlaytestService.PlaytestNotice,
    ) => Effect.Effect<PlaytestService.PlaytestNoticeResult>;
    readonly onChange?: Effect.Effect<void>;
  } = {},
) => {
  const made = PlaytestService.make({
    filePath: fx.filePath,
    stepsFolder: fx.stepsFolder,
    placeOf: options.placeOf ?? okPlace(fx.worktree),
    ...(options.threadGone === undefined ? {} : { threadGone: options.threadGone }),
    ...(options.notify === undefined ? {} : { notify: options.notify }),
    ...(options.onChange === undefined ? {} : { onChange: options.onChange }),
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
export const fsControl = Effect.gen(function* () {
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
export const runnerControl = Effect.gen(function* () {
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
export const storedSession = (
  fx: Fixture,
  dir: string,
  overrides: Record<string, unknown> = {},
) => {
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
export const writeStore = (fx: Fixture, sessions: ReadonlyArray<unknown>) => {
  const text = `${JSON.stringify({ version: 1, sessions })}\n`;
  return Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    yield* fs.writeFileString(fx.filePath, text);
  });
};
/** A push step that runs `/bin/sh -c script`. */
export const shellStep = (name: string, script: string) => ({
  argv: ["/bin/sh", "-c", script],
  timeoutSeconds: 30,
  description: "",
  name,
});

/** The steps that ran, as `step:round`, and `finished|step` for those that got to their end. */
export const ran = (dir: string) =>
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
export const logLines = (dir: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    return (yield* fs.readFileString(`${dir}/log`)).split("\n").filter((line) => line !== "");
  });

export const setFile = (dir: string, name: string, text: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    yield* fs.writeFileString(`${dir}/${name}`, text);
  });
export const removeFile = (dir: string, name: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    yield* fs.remove(`${dir}/${name}`);
  });
/** Makes `<step>` stop at its pipes: it says when it started and waits for `openGate`. */
export const holdStep = (dir: string, name: string) =>
  Effect.sync(() => {
    NodeChildProcess.execFileSync("mkfifo", [`${dir}/${name}.started`, `${dir}/${name}.gate`]);
  });
export const stepStarted = (dir: string, name: string) =>
  Effect.promise(() => NodeFS.promises.readFile(`${dir}/${name}.started`, "utf8"));
export const openGate = (dir: string, name: string) =>
  Effect.promise(() => NodeFS.promises.writeFile(`${dir}/${name}.gate`, "go\n"));
/** Process ids of steps that log to `dir`. */
export const stepProcesses = (dir: string) => {
  try {
    return NodeChildProcess.execFileSync("pgrep", ["-f", dir]).toString().trim();
  } catch {
    return "";
  }
};

export const refusal = <A>(effect: Effect.Effect<A, PlaytestFailure>) =>
  Effect.flip(effect).pipe(Effect.map((error) => ({ code: error.code, message: error.message })));

/** Sets (undefined: removes) the server's env vars; returns what they were. */
export function setServerEnv(values: Record<string, string | undefined>) {
  const before = Object.fromEntries(Object.keys(values).map((key) => [key, process.env[key]]));
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  return before;
}

export const live = <A, E>(effect: Effect.Effect<A, E, NodeServices.NodeServices | Scope.Scope>) =>
  effect.pipe(Effect.scoped, Effect.provide(NodeServices.layer));
