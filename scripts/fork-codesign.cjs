// Fork: electron-builder's `mac.sign` hook for a macOS build without signing secrets (#822). Given no
// identity, electron-builder signs nothing, and the app keeps only the linker signature of Electron's
// own executable, which seals none of the renamed bundle: `codesign --verify --deep --strict` fails
// with "code has no resources but signature indicates they must be present". build-desktop-artifact.ts
// sets identity "-" and this hook for unsigned builds. electron-builder calls it after afterPack and
// the fuses, as its last change to the bundle before the zip and dmg are made from it. Developer ID
// signing stays scripts/sign-macos.ts (#803).
const { execFileSync } = require("node:child_process");

module.exports = async function adHocSign(options) {
  const app = options?.app;
  if (typeof app !== "string" || !app.endsWith(".app")) {
    throw new Error(`fork-codesign: expected an .app path, got ${JSON.stringify(app)}`);
  }
  execFileSync("codesign", ["--force", "--deep", "--sign", "-", app], { stdio: "inherit" });
  execFileSync("codesign", ["--verify", "--deep", "--strict", "--verbose=2", app], {
    stdio: "inherit",
  });
};
