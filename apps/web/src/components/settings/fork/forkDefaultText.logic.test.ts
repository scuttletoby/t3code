// @effect-diagnostics nodeBuiltinImport:off - reads the Fork settings rows' source to find hand-written defaults.
import * as NodeFS from "node:fs";
import * as NodePath from "node:path";

import { DEFAULT_SERVER_SETTINGS } from "@t3tools/contracts";
import { FORK_SETTING_DEFAULTS } from "@t3tools/contracts/forkDefaults";
import { describe, expect, it } from "vite-plus/test";

import { defaultIs, FORK_DEFAULT_TEXT } from "./forkDefaultText.logic";

const forkDir = import.meta.dirname;
const settingsDir = NodePath.join(forkDir, "..");
// Every file that writes a Fork settings row's help.
const rowSources = [
  ...NodeFS.readdirSync(forkDir)
    .filter(
      (name) =>
        /\.tsx?$/.test(name) && !name.includes(".test.") && !name.startsWith("forkDefaultText"),
    )
    .map((name) => NodePath.join(forkDir, name)),
  ...[
    "WaitsAndSlotsSettings.tsx",
    "waitsAndSlotsSettings.logic.ts",
    "ReviewRulesSettings.tsx",
    "reviewRulesSettings.logic.ts",
  ].map((name) => NodePath.join(settingsDir, name)),
];

const pinned = (key: string) => {
  const row = FORK_SETTING_DEFAULTS.find((r) => r.scope === "server" && r.key === key);
  if (row === undefined) throw new Error(`no pinned default for ${key}`);
  return row.default as Record<string, unknown>;
};

describe("Fork settings help: the default it names", () => {
  it("is never written by hand, so it cannot drift from the code's default", () => {
    const handWritten = rowSources.flatMap((file) =>
      NodeFS.readFileSync(file, "utf8")
        .split("\n")
        .flatMap((line, index) =>
          /Default: (?!\$\{)/.test(line)
            ? [`${NodePath.relative(settingsDir, file)}:${index + 1}`]
            : [],
        ),
    );
    // Write `${defaultIs("<key>")}`, or `Default: ${<the default constant>}`, instead.
    expect(handWritten).toEqual([]);
    expect(rowSources.length).toBeGreaterThan(10);
  });

  it("matches each switch's pinned default", () => {
    for (const key of ["waitFor", "agentSlots", "playtest", "assistant", "stopFinishedChildren"]) {
      expect(FORK_DEFAULT_TEXT[key as keyof typeof FORK_DEFAULT_TEXT]).toBe(
        pinned(key).enabled ? "on" : "off",
      );
    }
    expect(FORK_DEFAULT_TEXT.automaticUpdates).toBe(pinned("automaticUpdates") ? "on" : "off");
  });

  it("names the dropdowns' default option by its label", () => {
    expect(FORK_DEFAULT_TEXT.usagePacingGate.toLowerCase()).toBe(
      DEFAULT_SERVER_SETTINGS.usagePacingGate,
    );
    expect(FORK_DEFAULT_TEXT.modelPolicy.toLowerCase()).toBe(pinned("modelPolicy").mode);
    expect(FORK_DEFAULT_TEXT.handoff.toLowerCase()).toBe(pinned("handoff").mode);
    expect(FORK_DEFAULT_TEXT.reviewRules).toBe(pinned("reviewRules").preset);
    expect(FORK_DEFAULT_TEXT.mcpProfile).toBe(DEFAULT_SERVER_SETTINGS.mcpProfile);
    expect(FORK_DEFAULT_TEXT.mcpSubagentProfile).toBe(DEFAULT_SERVER_SETTINGS.mcpSubagentProfile);
    expect(defaultIs("handoff")).toBe("Default: Off.");
  });
});
