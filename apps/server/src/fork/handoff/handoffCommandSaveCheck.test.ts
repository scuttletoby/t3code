/**
 * Fork: a save that sets a handoff command T3 cannot start, or one an agent could edit, is
 * refused with the fix; one already stored is not, so it blocks no other save.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  DEFAULT_HANDOFF_SETTINGS,
  DEFAULT_SERVER_SETTINGS,
  type ServerSettings,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";

import { check } from "../serverSettingsWriteCheck.ts";
import { handoffCommandSaveProblem } from "./handoffCommandSaveCheck.ts";

const withCommand = (
  command: ReadonlyArray<string> | undefined,
  project?: ReadonlyArray<string>,
): ServerSettings =>
  ({
    ...DEFAULT_SERVER_SETTINGS,
    handoff: {
      ...DEFAULT_HANDOFF_SETTINGS,
      ...(command === undefined ? {} : { source: "command", command }),
    },
    projectSettingsOverrides:
      project === undefined
        ? {}
        : {
            "project-1": {
              handoff: { ...DEFAULT_HANDOFF_SETTINGS, source: "command", command: project },
            },
          },
  }) as unknown as ServerSettings;

/**
 * A temporary folder with `outside/` (a program and a script, in no checkout), `repo/` (a clone:
 * `.git` is a folder) and `tree/` (a worktree: `.git` is a file), each with a script.
 */
const layout = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const root = yield* fs.makeTempDirectoryScoped({ prefix: "handoff-save-" });
  const at = (...parts: ReadonlyArray<string>) => path.join(root, ...parts);
  for (const folder of [
    "outside/bin",
    "outside/tool",
    "repo/.git",
    "repo/scripts",
    "repo/tool",
    "tree",
  ]) {
    yield* fs.makeDirectory(at(folder), { recursive: true });
  }
  yield* fs.writeFileString(at("tree", ".git"), "gitdir: /elsewhere\n");
  for (const file of [
    "outside/run",
    "outside/notes.py",
    "outside/handoff",
    "outside/tool/__main__.py",
    "repo/scripts/notes.py",
    "repo/scripts/handoff",
    "repo/tool/__main__.py",
    "tree/notes",
    // Stand-ins for the programs, by name: the check reads names and the disk, it runs nothing.
    ...["python3.12", "sh", "node", "env", "make", "npm", "bun", "deno", "Rscript", "R"].map(
      (name) => `outside/bin/${name}`,
    ),
  ]) {
    yield* fs.writeFileString(at(file), "#!/bin/sh\n");
  }
  yield* fs.symlink(at("repo", "scripts", "notes.py"), at("outside", "link"));
  return at;
});

const problemOf = (next: ServerSettings, current: ServerSettings = withCommand(undefined)) =>
  handoffCommandSaveProblem(current, next, "linux");

describe("handoff command save check (#785)", () => {
  it.effect("takes a program and script outside any checkout, and other arguments", () =>
    Effect.gen(function* () {
      const at = yield* layout;
      assert.strictEqual(yield* problemOf(withCommand([at("outside/run")])), undefined);
      assert.strictEqual(
        yield* problemOf(
          withCommand([at("outside/run"), at("outside/notes.py"), "--since", "1h", "/no/such"]),
        ),
        undefined,
      );
      assert.strictEqual(yield* problemOf(withCommand(undefined)), undefined);
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("refuses a program by a relative path, or one that does not exist", () =>
    Effect.gen(function* () {
      const at = yield* layout;
      assert.include(
        yield* problemOf(withCommand(["scripts/notes.sh"])),
        'handoff.command runs "scripts/notes.sh", which is not a full path',
      );
      assert.include(
        yield* problemOf(withCommand([at("outside/missing")])),
        "which is not a file on this server",
      );
      assert.include(
        yield* problemOf(withCommand([at("outside")])),
        "which is not a file on this server",
      );
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("refuses a program inside a clone, a worktree, or linked from one", () =>
    Effect.gen(function* () {
      const at = yield* layout;
      for (const program of [at("repo/scripts/notes.py"), at("tree/notes"), at("outside/link")]) {
        assert.include(
          yield* problemOf(withCommand([program])),
          "which is inside the git checkout",
          program,
        );
      }
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("checks the script an interpreter is given too", () =>
    Effect.gen(function* () {
      const at = yield* layout;
      assert.include(
        yield* problemOf(withCommand([at("outside/run"), at("repo/scripts/notes.py")])),
        `runs "${at("repo/scripts/notes.py")}", which is inside the git checkout ${at("repo")}`,
      );
      assert.include(
        yield* problemOf(withCommand([at("outside/run"), "scripts/notes.py"])),
        'gives "scripts/notes.py", a script by a relative path',
      );
      assert.include(
        yield* problemOf(withCommand([at("outside/run"), at("outside/missing.py")])),
        "not a file on this server: give the full path of a script that exists",
      );
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("follows an interpreter to its script, whatever its ending, or a module by name", () =>
    Effect.gen(function* () {
      const at = yield* layout;
      const [python, sh, node] = ["python3.12", "sh", "node"].map((name) =>
        at(`outside/bin/${name}`),
      );
      const refused: ReadonlyArray<readonly [ReadonlyArray<string>, string]> = [
        [[python!, "-m", "notes"], 'gives "-m notes", a module found by name'],
        [[python!, "-mnotes"], 'gives "-m notes", a module found by name'],
        [[node!, "-r", "./setup"], 'gives "-r ./setup", a module found by name'],
        [[node!, "--import=setup"], 'gives "--import setup", a module found by name'],
        [[sh!, "scripts/handoff"], 'gives "scripts/handoff", a script by a relative path'],
        [[sh!, at("repo/scripts/handoff")], `inside the git checkout ${at("repo")}`],
        // A folder Python runs by its __main__.py, and the checkout's own folder.
        [[python!, at("repo/tool")], `inside the git checkout ${at("repo")}`],
        [[python!, at("repo")], `inside the git checkout ${at("repo")}`],
        [[python!, "-W", "ignore", at("outside/missing")], "not a file on this server"],
      ];
      for (const [command, problem] of refused) {
        assert.include(yield* problemOf(withCommand(command)), problem, command.join(" "));
      }
      const taken: ReadonlyArray<ReadonlyArray<string>> = [
        [python!, "-W", "ignore", at("outside/notes.py")],
        [python!, at("outside/tool")],
        [sh!, at("outside/handoff"), "--since", "1h"],
        [sh!, "-c", "echo notes"],
        [node!, "-r", at("outside/notes.py"), at("outside/handoff")],
      ];
      for (const command of taken) {
        assert.strictEqual(yield* problemOf(withCommand(command)), undefined, command.join(" "));
      }
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("checks the program env runs, and refuses programs that run the project's files", () =>
    Effect.gen(function* () {
      const at = yield* layout;
      const [python, env, make, npm] = ["python3.12", "env", "make", "npm"].map((name) =>
        at(`outside/bin/${name}`),
      );
      const refused: ReadonlyArray<readonly [ReadonlyArray<string>, string]> = [
        [[env!, "notes"], 'runs "notes" through env, which finds it on the PATH'],
        [[env!, "-i", "A=1", python!, at("repo/scripts/notes.py")], "inside the git checkout"],
        [[env!, "-u", "HOME", at("repo/scripts/handoff")], "inside the git checkout"],
        [[env!, "-S", "python3 -m notes"], 'gives env "-S", which this check cannot follow'],
        [[make!, "handoff"], "runs what a file in the project folder"],
        [[npm!, "run", "handoff"], "runs what a file in the project folder"],
      ];
      for (const [command, problem] of refused) {
        assert.include(yield* problemOf(withCommand(command)), problem, command.join(" "));
      }
      assert.strictEqual(
        yield* problemOf(withCommand([env!, "-i", "A=1", at("outside/run"), "--quiet"])),
        undefined,
      );
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("refuses bun and deno, which read their config from the project folder (#973)", () =>
    Effect.gen(function* () {
      const at = yield* layout;
      const [bun, deno, env] = ["bun", "deno", "env"].map((name) => at(`outside/bin/${name}`));
      // bunfig.toml's `preload` and deno.json's import map run project code before the script,
      // however safe the script itself is.
      for (const command of [
        [bun!, at("outside/handoff")],
        [bun!, "run", at("outside/handoff")],
        [deno!, "run", at("outside/handoff")],
        [deno!, at("outside/handoff")],
        [env!, "-i", deno!, "run", at("outside/handoff")],
      ]) {
        assert.include(
          yield* problemOf(withCommand(command)),
          "reads its config (bunfig.toml, deno.json and the like) from the project folder",
          command.join(" "),
        );
      }
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect(
    "refuses R, and Rscript without --vanilla, which read .Rprofile from the project folder (#973)",
    () =>
      Effect.gen(function* () {
        const at = yield* layout;
        const [rscript, r, env] = ["Rscript", "R", "env"].map((name) => at(`outside/bin/${name}`));
        // R sources `.Rprofile` and reads `.Renviron` from its working folder before the script,
        // unless `--vanilla` comes among its options (after the script it is the script's argument).
        for (const command of [
          [rscript!, at("outside/handoff")],
          [rscript!, "--no-save", at("outside/handoff")],
          [rscript!, at("outside/handoff"), "--vanilla"],
          [rscript!, "-e", "cat(1)", "--vanilla"],
          [env!, "-i", rscript!, at("outside/handoff")],
          [r!, "--vanilla", "-f", at("outside/handoff")],
          [r!, "CMD", "BATCH", "--vanilla", at("outside/handoff")],
        ]) {
          assert.include(
            yield* problemOf(withCommand(command)),
            "reads .Rprofile and .Renviron from the project folder",
            command.join(" "),
          );
        }
        for (const command of [
          [rscript!, "--vanilla", at("outside/handoff")],
          [rscript!, "--verbose", "--vanilla", at("outside/handoff"), "--since", "1h"],
          [rscript!, "--vanilla", "-e", "cat(1)"],
        ]) {
          assert.strictEqual(yield* problemOf(withCommand(command)), undefined, command.join(" "));
        }
        // With --vanilla, its script is still checked as any interpreter's.
        assert.include(
          yield* problemOf(withCommand([rscript!, "--vanilla", at("repo/scripts/handoff")])),
          `inside the git checkout ${at("repo")}`,
        );
      }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("refuses env's -S and -C however they are written, and follows env's -a (#785)", () =>
    Effect.gen(function* () {
      const at = yield* layout;
      const env = at("outside/bin/env");
      const refused: ReadonlyArray<readonly [ReadonlyArray<string>, string]> = [
        // GNU env runs these: -S attached to its string, or grouped after other short options.
        [[env, "-S/usr/bin/python3 -m notes"], 'gives env "-S/usr/bin/python3 -m notes"'],
        [[env, "-iS/usr/bin/python3 -m notes"], 'gives env "-iS/usr/bin/python3 -m notes"'],
        [[env, "-i", "-vS", "/usr/bin/python3 -m notes"], 'gives env "-vS"'],
        [[env, "--split=/usr/bin/python3 -m notes"], 'gives env "--split=/usr/bin/python3'],
        [[env, "--s", "/usr/bin/python3 -m notes"], 'gives env "--s"'],
        [[env, "-C/tmp", at("outside/run")], 'gives env "-C/tmp"'],
        [[env, "-iC", "/tmp", at("outside/run")], 'gives env "-iC"'],
        [[env, "--ch=/tmp", at("outside/run")], 'gives env "--ch=/tmp"'],
        // -a takes the next word as the program's name: the program is the word after it.
        [[env, "-a", at("outside/run"), at("repo/scripts/handoff")], "inside the git checkout"],
        [[env, "--argv0", "x", at("repo/scripts/handoff")], "inside the git checkout"],
        [[env, "-ia", "x", at("repo/scripts/handoff")], "inside the git checkout"],
      ];
      for (const [command, problem] of refused) {
        assert.include(yield* problemOf(withCommand(command)), problem, command.join(" "));
      }
      // An S or C that is the value of -u (a variable's name) is no -S or -C.
      for (const command of [
        [env, "-uSHELL", at("outside/run")],
        [env, "-iuCDPATH", at("outside/run")],
        [env, "--unset=S", at("outside/run")],
      ]) {
        assert.strictEqual(yield* problemOf(withCommand(command)), undefined, command.join(" "));
      }
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("checks a project's command; leaves one already stored at the same place", () =>
    Effect.gen(function* () {
      const at = yield* layout;
      const bad = [at("repo/scripts/notes.py")];
      assert.include(
        yield* problemOf(withCommand(undefined, bad)),
        "projectSettingsOverrides.project-1.handoff.command runs",
      );
      // Stored before (a hand edit, or a file moved since): an unrelated save goes through.
      assert.strictEqual(
        yield* problemOf(withCommand(undefined, bad), withCommand(undefined, bad)),
        undefined,
      );
      // The same command set at another place is new there.
      assert.include(
        yield* problemOf(withCommand(bad, bad), withCommand(undefined, bad)),
        "handoff.command runs",
      );
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("is part of the server's save check", () =>
    Effect.gen(function* () {
      const at = yield* layout;
      const refused = yield* check(
        withCommand(undefined),
        withCommand([at("repo/scripts/notes.py")]),
        "settings.json",
      ).pipe(Effect.flip);
      assert.include(String(refused.cause), "which is inside the git checkout");
      yield* check(withCommand(undefined), withCommand([at("outside/run")]), "settings.json");
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );
});
