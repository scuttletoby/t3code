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
  for (const folder of ["outside", "repo/.git", "repo/scripts", "tree"]) {
    yield* fs.makeDirectory(at(folder), { recursive: true });
  }
  yield* fs.writeFileString(at("tree", ".git"), "gitdir: /elsewhere\n");
  for (const file of ["outside/run", "outside/notes.py", "repo/scripts/notes.py", "tree/notes"]) {
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
