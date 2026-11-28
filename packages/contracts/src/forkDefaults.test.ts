import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  FORK_SETTING_DEFAULTS,
  type ForkSettingScope,
  forkSettingAsSaved,
  pinnedPart,
} from "./forkDefaults.ts";
import {
  FORK_SERVER_SETTING_KEYS,
  ForkClientSettingsFields,
  ForkProjectOverrideFields,
} from "./forkSettings.ts";
import { ClientSettingsSchema, ProjectSettingsOverrides, ServerSettings } from "./settings.ts";

// The fork's settings are the keys of forkSettings.ts, not a list kept by hand or comments, so a
// new fork setting without a row fails here. A fork setting written straight into upstream's
// settings.ts is refused by the seam check (.github/scripts/fork-seam-check.cjs).
const discovered = [
  ...FORK_SERVER_SETTING_KEYS.map((key) => `server.${key}`),
  ...Object.keys(ForkProjectOverrideFields).map((key) => `projectOverride.${key}`),
  ...Object.keys(ForkClientSettingsFields).map((key) => `client.${key}`),
];
const rowId = (row: { readonly scope: string; readonly key: string }) => `${row.scope}.${row.key}`;

const decoded: Readonly<Record<ForkSettingScope, Readonly<Record<string, unknown>>>> = {
  server: Schema.decodeSync(ServerSettings)({}),
  projectOverride: Schema.decodeSync(ProjectSettingsOverrides)({}),
  client: Schema.decodeSync(ClientSettingsSchema)({}),
};

describe("fork setting defaults", () => {
  it("finds the fork's settings in forkSettings.ts", () => {
    expect(discovered).toContain("server.automaticUpdates");
    expect(discovered).toContain("server.waitFor");
    expect(discovered).toContain("projectOverride.waitChecks");
  });

  it("has exactly one row for each fork setting", () => {
    const rows = FORK_SETTING_DEFAULTS.map(rowId);
    expect(new Set(rows).size).toBe(rows.length);
    // A setting missing here needs a row in forkDefaults.ts; a row missing in the code is stale.
    expect([...rows].toSorted()).toEqual([...discovered].toSorted());
  });

  it.each(FORK_SETTING_DEFAULTS.map((row) => [rowId(row), row] as const))(
    "pins the default of %s",
    (_id, row) => {
      const saved = forkSettingAsSaved(row, decoded[row.scope][row.key]);
      expect(pinnedPart(row, saved)).toEqual(row.default);
      expect(row.reason.trim()).not.toBe("");
      expect(row.decidedIn).toMatch(/^#\d+$/);
    },
  );

  it("keeps every fork switch off by default but the read-only ones", () => {
    // Usage refresh reads usage; agent slots only observe by default (read only, never decide).
    // The owner-questions page only changes which page Issues opens (the owner chose it on).
    expect(FORK_SETTING_DEFAULTS.filter((row) => !row.off).map(rowId)).toEqual([
      "server.agentSlots",
      "server.headlessUsageRefreshInterval",
      "client.ownerQuestionsPage",
    ]);
  });
});

// The settings page groups and names fork settings for people, but the stored keys stay as
// they are: a settings file saved by an older build must load with every value it had.
const decodeServerFile = Schema.decodeUnknownSync(ServerSettings);
const encodeServerFile = Schema.encodeSync(ServerSettings);
const decodeProjectOverrides = Schema.decodeUnknownSync(ProjectSettingsOverrides);

describe("settings saved by an older build", () => {
  it("keeps the stored key names", () => {
    expect(FORK_SETTING_DEFAULTS.map(rowId)).toEqual(
      expect.arrayContaining([
        "server.automaticUpdates",
        "server.automaticUpdatesQuietCommand",
        "server.usagePacing",
        "server.usagePacingGate",
        "server.usagePacingLowerEffort",
        "server.waitFor",
        "server.waitChecks",
        "server.agentSlots",
        "server.modelPolicy",
        "server.reviewRules",
        "server.handoff",
        "server.playtest",
        "server.mcpProfiles",
        "server.mcpProfile",
        "server.mcpSubagentProfile",
        "server.mcpThreadProfiles",
        "server.assistant",
        "server.stopFinishedChildren",
        "server.lanes",
        "projectOverride.waitChecks",
        "projectOverride.mcpProfile",
        "projectOverride.mcpSubagentProfile",
        "projectOverride.playtest",
        "projectOverride.handoff",
        "projectOverride.reviewRules",
        "projectOverride.placementNeeds",
      ]),
    );
    // A project's saved overrides are read by these names: renaming one loses what projects saved.
    expect(Object.keys(ForkProjectOverrideFields).toSorted()).toEqual([
      "handoff",
      "mcpProfile",
      "mcpSubagentProfile",
      "placementNeeds",
      "playtest",
      "reviewRules",
      "waitChecks",
    ]);
  });

  it("loads the overrides a project saved with an older build", () => {
    const saved = {
      mcpProfile: "lean",
      mcpSubagentProfile: "lean",
      handoff: { mode: "handoff" },
      reviewRules: { preset: "recommended" },
      playtest: {
        enabled: true,
        claim: { argv: ["/usr/bin/playtest", "claim"] },
        renew: { argv: ["/usr/bin/playtest", "renew"] },
        push: [{ name: "build", argv: ["/usr/bin/playtest", "build"] }],
        release: { argv: ["/usr/bin/playtest", "release"] },
      },
    };
    // The struct drops a key it does not know, so a renamed key fails here.
    expect(decodeProjectOverrides(saved)).toMatchObject(saved);
  });

  it("loads the values an older settings file saved, not the defaults", () => {
    const saved = {
      automaticUpdates: true,
      automaticUpdatesQuietCommand: "true",
      usagePacingGate: "advise",
      waitFor: { enabled: true },
      agentSlots: { enabled: true },
      modelPolicy: {
        mode: "enforce",
        allow: [{ driver: "claudeAgent", model: "claude-opus-5-5" }],
      },
      reviewRules: { preset: "recommended" },
      handoff: { mode: "handoff" },
      playtest: {
        enabled: true,
        claim: { argv: ["/usr/bin/playtest", "claim"] },
        renew: { argv: ["/usr/bin/playtest", "renew"] },
        push: [{ name: "build", argv: ["/usr/bin/playtest", "build"] }],
        release: { argv: ["/usr/bin/playtest", "release"] },
      },
      mcpProfiles: { lean: { servers: [], t3Tools: "all" } },
      mcpProfile: "lean",
      mcpSubagentProfile: "lean",
      mcpThreadProfiles: { "thread-1": "lean" },
      assistant: { enabled: true },
      stopFinishedChildren: { enabled: true },
    };
    const settings = decodeServerFile(saved);
    expect(settings).toMatchObject(saved);
    // Written back, the same keys keep the same values.
    expect(encodeServerFile(settings)).toMatchObject(saved);
  });

  it("reads missing fork keys as defaults and ignores keys it does not know", () => {
    const settings = decodeServerFile({ notAForkSetting: 1 });
    for (const row of FORK_SETTING_DEFAULTS.filter((r) => r.scope === "server")) {
      expect(
        pinnedPart(row, forkSettingAsSaved(row, settings[row.key as keyof typeof settings])),
      ).toEqual(row.default);
    }
  });
});
