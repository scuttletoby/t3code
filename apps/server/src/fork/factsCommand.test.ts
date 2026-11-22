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

    it.live("a path with a space and shell signs runs as one path; its lines are labels", () =>
      Effect.gen(function* () {
        const dir = scratch();
        const marker = NodePath.join(dir, "MARK");
        // A shell would split this path at the space and run `touch MARK` after the `;`.
        const path = script(
          dir,
          `with space;touch ${marker}`,
          ['echo " GPU "', 'echo "two words"', "echo gpu", "echo Cuda", "exit 0"].join("\n"),
        );
        const result = yield* runFactsCommand(path).pipe(Effect.provide(layer));
        expect(result).toEqual({ available: true, labels: ["gpu", "cuda"] });
        expect(NodeFS.existsSync(marker)).toBe(false);
        // Runs again: the same answer, nothing left behind.
        expect(yield* runFactsCommand(path).pipe(Effect.provide(layer))).toEqual(result);
        NodeFS.rmSync(dir, { recursive: true, force: true });
      }),
    );

    it.live("a non-zero exit or a missing program is not available", () =>
      Effect.gen(function* () {
        const dir = scratch();
        const busy = script(dir, "busy", "echo gpu\necho 'all slots used' >&2\nexit 2");
        expect(yield* runFactsCommand(busy).pipe(Effect.provide(layer))).toEqual({
          available: false,
          reason: "factsCommand exited with code 2: gpu",
        });
        const missing = yield* runFactsCommand(NodePath.join(dir, "nope")).pipe(
          Effect.provide(layer),
        );
        expect(missing.available).toBe(false);
        expect(missing.available === false && missing.reason).toContain(
          "factsCommand could not run",
        );
        NodeFS.rmSync(dir, { recursive: true, force: true });
      }),
    );
  },
);
