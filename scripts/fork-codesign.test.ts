// @effect-diagnostics nodeBuiltinImport:off
// Fork: the ad hoc signing hook for unsigned macOS builds (scripts/fork-codesign.cjs) and the
// seam line in build-desktop-artifact.ts that picks it only for unsigned builds.
import * as NodeChildProcess from "node:child_process";
import * as NodeURL from "node:url";

import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import * as Effect from "effect/Effect";

import { createBuildConfig } from "./build-desktop-artifact.ts";

const hook = NodeURL.fileURLToPath(new URL("./fork-codesign.cjs", import.meta.url));

// The hook runs in a child Node with execFileSync replaced by a recorder, so no codesign runs.
const runHook = (options: unknown) =>
  NodeChildProcess.spawnSync(
    process.execPath,
    [
      "-e",
      `
      require("node:child_process").execFileSync = (file, args) => {
        console.log("call " + JSON.stringify([file, ...args]));
      };
      require(${JSON.stringify(hook)})(${JSON.stringify(options)})
        .then(() => console.log("done"))
        .catch(error => { console.log("refused " + error.message); });
    `,
    ],
    { encoding: "utf8" },
  );

const calls = (stdout: string) =>
  stdout
    .split("\n")
    .filter((line) => line.startsWith("call "))
    .map((line) => JSON.parse(line.slice("call ".length)) as string[]);

it("signs the app ad hoc, then verifies it deep and strict", () => {
  const result = runHook({ app: "/out/mac-arm64/T3 Code.app", identity: "-" });
  assert.equal(result.status, 0, result.stderr);
  assert.include(result.stdout, "done");
  assert.deepStrictEqual(calls(result.stdout), [
    ["codesign", "--force", "--deep", "--sign", "-", "/out/mac-arm64/T3 Code.app"],
    ["codesign", "--verify", "--deep", "--strict", "--verbose=2", "/out/mac-arm64/T3 Code.app"],
  ]);
});

it("refuses a path that is not an .app", () => {
  for (const app of ["/out/mac-arm64/T3 Code", "/out/T3-Code.dmg", undefined]) {
    const result = runHook({ app, identity: "-" });
    assert.equal(result.status, 0, result.stderr);
    assert.include(result.stdout, "refused fork-codesign: expected an .app path");
    assert.deepStrictEqual(calls(result.stdout), []);
  }
});

it("refuses any identity but ad hoc, so it never replaces a Developer ID signature", () => {
  for (const identity of [
    "Developer ID Application: Someone (TEAM123456)",
    "ABCDEF0123",
    undefined,
  ]) {
    const result = runHook({ app: "/out/mac-arm64/T3 Code.app", identity });
    assert.equal(result.status, 0, result.stderr);
    assert.include(result.stdout, 'refused fork-codesign: only ad hoc signing (identity "-")');
    assert.deepStrictEqual(calls(result.stdout), []);
  }
});

it.layer(NodeServices.layer)("createBuildConfig macOS signing", (it) => {
  const macConfig = (signed: boolean) =>
    createBuildConfig("mac", "dmg", "0.0.33", signed, false, undefined, undefined).pipe(
      Effect.map((config) => config.mac as Record<string, unknown>),
    );

  it.effect("an unsigned build signs ad hoc through the fork hook", () =>
    Effect.gen(function* () {
      const mac = yield* macConfig(false);
      assert.equal(mac.identity, "-");
      assert.match(String(mac.sign), /[\\/]scripts[\\/]fork-codesign\.cjs$/);
    }),
  );

  it.effect("a signed build keeps upstream's sign-macos.ts and no ad hoc identity", () =>
    Effect.gen(function* () {
      const mac = yield* macConfig(true);
      assert.notEqual(mac.identity, "-");
      assert.match(String(mac.sign), /[\\/]scripts[\\/]sign-macos\.ts$/);
    }),
  );
});
