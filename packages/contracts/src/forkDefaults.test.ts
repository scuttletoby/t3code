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

  it("keeps every fork switch off by default but the read-only usage refresh", () => {
    expect(FORK_SETTING_DEFAULTS.filter((row) => !row.off).map(rowId)).toEqual([
      "server.headlessUsageRefreshInterval",
    ]);
  });
});
