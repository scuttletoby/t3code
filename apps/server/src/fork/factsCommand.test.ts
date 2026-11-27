// @effect-diagnostics nodeBuiltinImport:off - real scratch files for the real-program tests.
/**
 * Fork (placement row P4's runner): `factsCommand` is checked again on the host that
 * runs it and run as one path, with no shell. Real programs on Linux and macOS; the Windows rules
 * with a stand-in runner, so they run on any machine.
 */
import * as NodeFS from "node:fs";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";

import * as NodeServices from "@effect/platform-node/NodeServices";
import { describe, expect, it } from "@effect/vitest";
import * as HostProcess from "@t3tools/shared/HostProcess";
import { SpawnExecutableResolution } from "@t3tools/shared/shell";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as ChildProcessSpawner from "effect/process/ChildProcessSpawner";

import * as ProcessRunner from "../processRunner.ts";
import { FACTS_COMMAND_TIMEOUT, runFactsCommand } from "./factsCommand.ts";

/** A stand-in runner that records what it was asked and answers `answer`. */
const recording = (answer: Partial<ProcessRunner.ProcessRunOutput> = {}) => {
  const asked: Array<ProcessRunner.ProcessRunInput> = [];
  const layer = Layer.mock(ProcessRunner.ProcessRunner)({
    run: (input) =>
      Effect.sync(() => {
        asked.push(input);
        return {
          stdout: "",
          stderr: "",
          code: 0,
          timedOut: false,
          stdoutTruncated: false,
          stderrTruncated: false,
          stdoutInvalidUtf8: false,
          stderrInvalidUtf8: false,
          ...answer,
        } as ProcessRunner.ProcessRunOutput;
      }),
  });
  return { asked, layer };
};

const on =
  (platform: NodeJS.Platform, layer: Layer.Layer<ProcessRunner.ProcessRunner>) =>
  <A, E>(effect: Effect.Effect<A, E, ProcessRunner.ProcessRunner>) =>
    effect.pipe(Effect.provide(layer), Effect.provideService(HostProcess.Platform, platform));

describe("factsCommand: checked again on the host that runs it", () => {
  it.effect.each([
    ["linux", "C:\\facts.exe"],
    ["darwin", "C:\\facts.exe"],
    ["win32", "/usr/local/bin/facts"],
  ] as const)("on %s, %s is not available and never run", ([platform, path]) =>
    Effect.gen(function* () {
      const { asked, layer } = recording();
      const result = yield* runFactsCommand(path).pipe(on(platform, layer));
      expect(result.available).toBe(false);
      expect(result.available === false && result.reason).toContain(
        "is not a full path on this server's OS",
      );
      expect(asked).toEqual([]);
    }),
  );

  it.effect("on Windows only an .exe runs: a .cmd or .bat would need a shell", () =>
    Effect.gen(function* () {
      for (const path of ["C:\\tools\\facts.cmd", "C:\\tools\\facts.BAT", "C:\\tools\\facts"]) {
        const { asked, layer } = recording();
        const result = yield* runFactsCommand(path).pipe(on("win32", layer));
        expect(result.available === false && result.reason).toContain("is not an .exe");
        expect(asked).toEqual([]);
      }
      const { asked, layer } = recording({ stdout: "GPU\r\n" });
      const path = "C:\\Program Files\\facts\\facts.EXE";
      expect(yield* runFactsCommand(path).pipe(on("win32", layer))).toEqual({
        available: true,
        labels: ["gpu"],
      });
      // One path, no arguments: the space stays part of it.
      expect(asked.map(({ command, args }) => ({ command, args }))).toEqual([
        { command: path, args: [] },
      ]);
    }),
  );

  it.effect(
    "on Windows the rule holds for the path that runs: a lookup that finds a .CMD or .COM is refused",
    () =>
      Effect.gen(function* () {
        const path = "C:\\tools\\facts.exe";
        // Stand-ins for the file lookup the runner does (shared/shell's resolveSpawnCommand) that
        // return another file than the path: the check refuses it even with .EXE in PATHEXT.
        const withPathext =
          (pathext: string) =>
          <A, E, R>(effect: Effect.Effect<A, E, R>) =>
            effect.pipe(Effect.provideService(HostProcess.Environment, { PATHEXT: pathext }));
        const planted = recording();
        const refused = yield* runFactsCommand(path).pipe(
          Effect.provideService(SpawnExecutableResolution, (command: string) => `${command}.CMD`),
          withPathext(".COM;.EXE;.BAT;.CMD"),
          on("win32", planted.layer),
        );
        expect(refused.available).toBe(false);
        expect(refused.available === false && refused.reason).toMatch(
          /^factsCommand "C:\\tools\\facts\.exe" would run through a shell on this server \(as .*facts\.exe\.CMD/u,
        );
        expect(planted.asked).toEqual([]);
        // A planted facts.exe.COM needs no shell but is not an .exe either: refused too.
        const com = recording();
        const comResult = yield* runFactsCommand(path).pipe(
          Effect.provideService(SpawnExecutableResolution, (command: string) => `${command}.COM`),
          withPathext(".COM;.EXE;.BAT;.CMD"),
          on("win32", com.layer),
        );
        expect(comResult).toEqual({
          available: false,
          reason: `factsCommand "${path}" runs as "${path}.COM" on this server, not an .exe`,
        });
        expect(com.asked).toEqual([]);
        // A lookup that finds the path itself: it runs, as the path that was checked.
        const plain = recording({ stdout: "gpu\n" });
        expect(
          yield* runFactsCommand(path).pipe(
            Effect.provideService(SpawnExecutableResolution, (command: string) => command),
            withPathext(".COM;.EXE;.BAT;.CMD"),
            on("win32", plain.layer),
          ),
        ).toEqual({ available: true, labels: ["gpu"] });
        expect(plain.asked.map(({ command, args }) => ({ command, args }))).toEqual([
          { command: path, args: [] },
        ]);
      }),
  );

  describe("on Windows with the real file lookup (shared/shell's, C:\\tools\\ mapped to a temp dir)", () => {
    const path = "C:\\tools\\facts.exe";
    const scratch = () => {
      const dir = NodeFS.mkdtempSync(NodePath.join(NodeOS.tmpdir(), "t3-facts-pathext-"));
      NodeFS.writeFileSync(NodePath.join(dir, "facts.exe"), "");
      return dir;
    };
    /** The default lookup, its result mapped back to `C:\tools\` so the runner sees what Windows would. */
    const realLookup = (dir: string) =>
      Effect.gen(function* () {
        const real = yield* SpawnExecutableResolution;
        // Upstream's resolver may answer with a promise.
        return async (command: string, platform: NodeJS.Platform, env: NodeJS.ProcessEnv) =>
          (await real(command.replace("C:\\tools\\", `${dir}/`), platform, env))?.replace(
            `${dir}/`,
            "C:\\tools\\",
          );
      });
    const withEnv =
      (dir: string, env: NodeJS.ProcessEnv) =>
      <A, E, R>(effect: Effect.Effect<A, E, R>) =>
        Effect.gen(function* () {
          const lookup = yield* realLookup(dir);
          return yield* effect.pipe(
            Effect.provideService(SpawnExecutableResolution, lookup),
            Effect.provideService(HostProcess.Environment, env),
          );
        });

    it.effect(
      "PATHEXT without .EXE, the real facts.exe present, nothing planted: refused, never run",
      () =>
        Effect.gen(function* () {
          // The check would pass here, then a facts.exe.CMD planted before the runner's own lookup
          // would run through a shell: so the path is refused whatever is on disk.
          for (const env of [
            { PATHEXT: ".COM;.BAT;.CMD" },
            { PathExt: "com; bat ;.cmd" },
            { PATHEXT: ".EXE.;.CMD" },
          ] satisfies ReadonlyArray<NodeJS.ProcessEnv>) {
            const dir = scratch();
            const { asked, layer } = recording({ stdout: "gpu\n" });
            const result = yield* runFactsCommand(path).pipe(withEnv(dir, env), on("win32", layer));
            expect(result).toEqual({
              available: false,
              reason: `factsCommand "${path}" cannot run on this server: its PATHEXT has no .EXE, so the file lookup would also start a planted "${path}.CMD" (or .BAT) through a shell, which placement never uses`,
            });
            expect(asked).toEqual([]);
            NodeFS.rmSync(dir, { recursive: true, force: true });
          }
        }),
    );

    it.effect(
      "PATHEXT with .EXE (or unset, or empty: the default list): runs the checked file, even with a facts.exe.CMD beside it",
      () =>
        Effect.gen(function* () {
          for (const env of [
            { PATHEXT: ".COM;.EXE;.BAT;.CMD" },
            { pathext: "cmd;exe" },
            { PATHEXT: "" },
            { PATHEXT: " ; " },
            {},
          ] satisfies ReadonlyArray<NodeJS.ProcessEnv>) {
            const dir = scratch();
            NodeFS.writeFileSync(NodePath.join(dir, "facts.exe.CMD"), "");
            const { asked, layer } = recording({ stdout: "gpu\n" });
            expect(
              yield* runFactsCommand(path).pipe(withEnv(dir, env), on("win32", layer)),
            ).toEqual({ available: true, labels: ["gpu"] });
            expect(asked.map(({ command, args }) => ({ command, args }))).toEqual([
              { command: path, args: [] },
            ]);
            NodeFS.rmSync(dir, { recursive: true, force: true });
          }
        }),
    );
  });

  it.effect("output cut at its limit: the last, maybe partial, line is not a label", () =>
    Effect.gen(function* () {
      const cut = recording({ stdout: "ci-runner\ngpu-large\ngpu", stdoutTruncated: true });
      expect(yield* runFactsCommand("/x/facts").pipe(on("linux", cut.layer))).toEqual({
        available: true,
        labels: ["ci-runner", "gpu-large"],
      });
      expect(cut.asked[0]?.maxOutputBytes).toBe(16 * 1024);
    }),
  );

  it.effect("empty: none asked, available with no labels", () =>
    Effect.gen(function* () {
      const { asked, layer } = recording();
      expect(yield* runFactsCommand("").pipe(on("linux", layer))).toEqual({
        available: true,
        labels: [],
      });
      expect(asked).toEqual([]);
    }),
  );

  it.effect("a timeout, a non-zero exit or a failed start: not available, with the reason", () =>
    Effect.gen(function* () {
      const late = recording({ timedOut: true, code: null });
      expect(yield* runFactsCommand("/x/facts").pipe(on("linux", late.layer))).toEqual({
        available: false,
        reason: `factsCommand did not answer within ${Duration.toSeconds(FACTS_COMMAND_TIMEOUT)} seconds`,
      });
      expect(late.asked[0]?.timeoutBehavior).toBe("timedOutResult");
      const busy = recording({
        code: ChildProcessSpawner.ExitCode(3),
        stderr: "\n  busy building\nmore",
      });
      expect(yield* runFactsCommand("/x/facts").pipe(on("linux", busy.layer))).toEqual({
        available: false,
        reason: "factsCommand exited with code 3: busy building",
      });
      // A label line on stdout does not hide the error: the error output comes first.
      const labelFirst = recording({
        code: ChildProcessSpawner.ExitCode(2),
        stdout: "gpu\n",
        stderr: "all slots used\n",
      });
      expect(yield* runFactsCommand("/x/facts").pipe(on("linux", labelFirst.layer))).toEqual({
        available: false,
        reason: "factsCommand exited with code 2: all slots used",
      });
      // No error output: the first stdout line still says why.
      const quiet = recording({ code: ChildProcessSpawner.ExitCode(4), stdout: "\nno gpu here\n" });
      expect(yield* runFactsCommand("/x/facts").pipe(on("linux", quiet.layer))).toEqual({
        available: false,
        reason: "factsCommand exited with code 4: no gpu here",
      });
      const broken = Layer.mock(ProcessRunner.ProcessRunner)({
        run: (input) =>
          Effect.fail(
            new ProcessRunner.ProcessSpawnError({
              command: input.command,
              argumentCount: 0,
              cause: new Error("ENOENT"),
            } as never),
          ),
      });
      expect(yield* runFactsCommand("/x/facts").pipe(on("linux", broken))).toEqual({
        available: false,
        reason: "factsCommand could not run (ProcessSpawnError)",
      });
      const killed = Layer.mock(ProcessRunner.ProcessRunner)({
        run: (input) =>
          Effect.fail(
            new ProcessRunner.ProcessReadError({
              command: input.command,
              argumentCount: 0,
              stream: "exitCode",
              cause: new Error("SIGKILL"),
            } as never),
          ),
      });
      expect(yield* runFactsCommand("/x/facts").pipe(on("linux", killed))).toEqual({
        available: false,
        reason: "factsCommand was stopped by a signal",
      });
    }),
  );
});

describe.skipIf(HostProcess.Platform.defaultValue() === "win32")(
  "factsCommand: a real program, no shell",
  () => {
    const layer = ProcessRunner.layer.pipe(Layer.provide(NodeServices.layer));
    const scratch = () => NodeFS.mkdtempSync(NodePath.join(NodeOS.tmpdir(), "t3-facts-command-"));
    const script = (dir: string, name: string, body: string) => {
      const folder = NodePath.join(dir, name);
      NodeFS.mkdirSync(folder, { recursive: true });
      const path = NodePath.join(folder, "facts");
      NodeFS.writeFileSync(path, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
      return path;
    };

    it.live(
      "a path with a space and shell signs runs as one path, no arguments; lines are labels",
      () =>
        Effect.gen(function* () {
          const dir = scratch();
          // A shell would split this path at the space and the `;`, so the program would not start.
          // `args-0` shows it got no arguments; `$0` is the whole path, space and `;` included.
          const path = script(
            dir,
            "with space;echo hacked",
            [
              'echo " GPU "',
              'echo "two words"',
              "echo gpu",
              "echo Cuda",
              'echo "args-$#"',
              `[ "$0" = "${NodePath.join(dir, "with space;echo hacked", "facts")}" ] && echo whole-path`,
              "exit 0",
            ].join("\n"),
          );
          const result = yield* runFactsCommand(path).pipe(Effect.provide(layer));
          expect(result).toEqual({
            available: true,
            labels: ["gpu", "cuda", "args-0", "whole-path"],
          });
          // Runs again: the same answer.
          expect(yield* runFactsCommand(path).pipe(Effect.provide(layer))).toEqual(result);
          NodeFS.rmSync(dir, { recursive: true, force: true });
        }),
    );

    it.live("a non-zero exit, a missing program or a signal is not available", () =>
      Effect.gen(function* () {
        const dir = scratch();
        const busy = script(dir, "busy", "echo gpu\necho 'all slots used' >&2\nexit 2");
        expect(yield* runFactsCommand(busy).pipe(Effect.provide(layer))).toEqual({
          available: false,
          reason: "factsCommand exited with code 2: all slots used",
        });
        const missing = yield* runFactsCommand(NodePath.join(dir, "nope")).pipe(
          Effect.provide(layer),
        );
        expect(missing.available).toBe(false);
        expect(missing.available === false && missing.reason).toContain(
          "factsCommand could not run",
        );
        const killed = script(dir, "killed", "echo gpu\nkill -9 $$");
        expect(yield* runFactsCommand(killed).pipe(Effect.provide(layer))).toEqual({
          available: false,
          reason: "factsCommand was stopped by a signal",
        });
        NodeFS.rmSync(dir, { recursive: true, force: true });
      }),
    );
  },
);
