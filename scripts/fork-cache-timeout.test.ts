// @effect-diagnostics nodeBuiltinImport:off
// Fork: the Rosetta limit for warming the server compile cache, alone and through upstream's
// afterPack hook (scripts/prepare-server-compile-cache.cjs, its seam lines).
import * as NodeChildProcess from "node:child_process";
import * as NodeModule from "node:module";
import * as NodeURL from "node:url";
import { expect, it } from "vite-plus/test";

const require = NodeModule.createRequire(import.meta.url);
const { ROSETTA_TIMEOUT_MS, withRosetta, withRosettaTimeout } = require("./fork-cache-timeout.cjs");

it("raises a shorter limit to 360 s and leaves a longer one or none alone", () => {
  expect(ROSETTA_TIMEOUT_MS).toBe(360_000);
  expect(withRosettaTimeout({ encoding: "utf8", timeout: 120_000 })).toEqual({
    encoding: "utf8",
    timeout: 360_000,
  });
  const longer = { timeout: 600_000 };
  expect(withRosettaTimeout(longer)).toBe(longer);
  const none = { encoding: "utf8" };
  expect(withRosettaTimeout(none)).toBe(none);
  const unlimited = { timeout: 0 };
  expect(withRosettaTimeout(unlimited)).toBe(unlimited);
  expect(withRosettaTimeout(undefined)).toBeUndefined();
});

it("wraps spawnSync only under Rosetta", () => {
  const calls: unknown[][] = [];
  const spawnSync = (...args: unknown[]) => {
    calls.push(args);
    return { status: 0 };
  };
  expect(withRosetta(spawnSync, false)).toBe(spawnSync);
  expect(withRosetta(spawnSync, true)("engine", ["runner"], { timeout: 120_000 })).toEqual({
    status: 0,
  });
  expect(calls).toEqual([["engine", ["runner"], { timeout: 360_000 }]]);
});

// The hook runs in a child Node with the host faked and spawnSync replaced by a recorder, so only
// the limit it passes is checked (upstream's own test runs the cache generation itself).
it.each([
  { case: "x64 under Rosetta", platform: "darwin", hostArch: "arm64", arch: 1, want: 360_000 },
  { case: "native macOS arm64", platform: "darwin", hostArch: "arm64", arch: 3, want: 120_000 },
  { case: "native Linux x64", platform: "linux", hostArch: "x64", arch: 1, want: 120_000 },
])("the warm step of the $case build gets $want ms", ({ platform, hostArch, arch, want }) => {
  const hook = NodeURL.fileURLToPath(
    new URL("./prepare-server-compile-cache.cjs", import.meta.url),
  );
  const result = NodeChildProcess.spawnSync(
    process.execPath,
    [
      "-e",
      `
      require("node:child_process").spawnSync = (executable, args, options) => {
        console.log("timeout=" + options.timeout);
        return { status: 0 };
      };
      Object.defineProperty(process, "platform", { value: ${JSON.stringify(platform)} });
      Object.defineProperty(process, "arch", { value: ${JSON.stringify(hostArch)} });
      require(${JSON.stringify(hook)})({
        appOutDir: "/nonexistent", electronPlatformName: ${JSON.stringify(platform)}, arch: ${arch},
        packager: { appInfo: { productFilename: "Fixture" }, executableName: "fixture" }
      }).catch(error => { console.error(error); process.exitCode = 1; });
    `,
    ],
    { encoding: "utf8", env: { ...process.env, T3CODE_DESKTOP_UNTRUSTED_BUNDLE: "" } },
  );
  expect(result.status, result.stderr).toBe(0);
  expect(result.stdout).toContain(`timeout=${want}\n`);
  expect(result.stdout).toContain("Prepared server bytecode");
});
