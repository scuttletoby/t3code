// Fork: electron-builder's `mac.sign` hook for unsigned macOS builds (identity "-"). Unsigned, the
// app keeps only Electron's linker signature, which seals none of the bundle, so `codesign --verify
// --deep --strict` fails. It runs as the last change to the bundle, before the zip and dmg are made.
// Developer ID signing stays scripts/sign-macos.ts; this hook refuses any identity but "-".
const { execFileSync } = require("node:child_process");

// Entitlements and hardened runtime are ignored on purpose: an ad hoc signature names no team, so
// library validation would refuse Electron's own frameworks, and Gatekeeper honours neither.
module.exports = async function adHocSign(options) {
  const app = options?.app;
  if (typeof app !== "string" || !app.endsWith(".app")) {
    throw new Error(`fork-codesign: expected an .app path, got ${JSON.stringify(app)}`);
  }
  if (options?.identity !== "-") {
    throw new Error(
      `fork-codesign: only ad hoc signing (identity "-") runs here, got ${JSON.stringify(options?.identity)}`,
    );
  }
  execFileSync("codesign", ["--force", "--deep", "--sign", "-", app], { stdio: "inherit" });
  execFileSync("codesign", ["--verify", "--deep", "--strict", "--verbose=2", app], {
    stdio: "inherit",
  });
};
