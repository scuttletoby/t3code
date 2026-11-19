import { type ProviderInstanceId, ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  formatLowerEffort,
  parseLowerEffort,
  usagePacingAccountRows,
  usagePacingAccountSummary,
} from "./usagePacingSettings.logic";

const id = (value: string) => value as ProviderInstanceId;
const fromFile = Schema.decodeUnknownSync(ServerSettings);

describe("usagePacingAccountRows", () => {
  it("lists every account by its name with the defaults when the file names none", () => {
    const rows = usagePacingAccountRows(
      [
        { instanceId: id("codex"), displayName: "Codex" },
        { instanceId: id("codex_work"), displayName: "Codex (work)" },
        { instanceId: id("codex"), displayName: "Codex again" },
      ],
      fromFile({}).usagePacing,
    );
    expect(rows.map((row) => [row.label, row.settings.reservePercent, row.stored])).toEqual([
      ["Codex", 0, false],
      ["Codex (work)", 0, false],
    ]);
  });

  it("reads each account's own entry, and keeps one for an account that was removed", () => {
    const rows = usagePacingAccountRows(
      [{ instanceId: id("claudeAgent") }],
      fromFile({
        usagePacing: {
          claudeAgent: { reservePercent: 20 },
          gone_account: { burnFirst: true },
        },
      }).usagePacing,
    );
    expect(rows).toEqual([
      {
        instanceId: "claudeAgent",
        label: "claudeAgent",
        settings: { reservePercent: 20, burnFirst: false, noLimits: false },
        stored: true,
        known: true,
      },
      {
        instanceId: "gone_account",
        label: "gone_account",
        settings: { reservePercent: 0, burnFirst: true, noLimits: false },
        stored: true,
        known: false,
      },
    ]);
  });

  it("says what each account's entry does", () => {
    const summary = (file: object) =>
      usagePacingAccountSummary(Object.values(fromFile(file).usagePacing)[0]!);
    expect(summary({ usagePacing: { a: {} } })).toBe("No reserve");
    expect(summary({ usagePacing: { a: { reservePercent: 15, burnFirst: true } } })).toBe(
      "15% kept free, spent first",
    );
    expect(summary({ usagePacing: { a: { reservePercent: 15, noLimits: true } } })).toBe(
      "No usage limits",
    );
  });
});

describe("lower effort field", () => {
  it("shows the stored map and reads it back unchanged", () => {
    const stored = fromFile({
      usagePacingLowerEffort: { medium: "low", high: "medium" },
    }).usagePacingLowerEffort;
    const text = formatLowerEffort(stored);
    expect(text).toBe("high=medium, medium=low");
    expect(parseLowerEffort(text)).toEqual({ value: stored });
    expect(formatLowerEffort(fromFile({}).usagePacingLowerEffort)).toBe("");
  });

  it("reads pairs split by commas or lines, and empty as no lowering", () => {
    expect(parseLowerEffort(" high = medium,\nxhigh=high ,")).toEqual({
      value: { high: "medium", xhigh: "high" },
    });
    expect(parseLowerEffort("  ")).toEqual({ value: {} });
  });

  it("refuses text that is not a list of pairs", () => {
    expect(parseLowerEffort("high")).toEqual({ error: '"high" is not a pair like high=medium.' });
    expect(parseLowerEffort("high=medium=low")).toHaveProperty("error");
    expect(parseLowerEffort("high=low, high=medium")).toEqual({ error: "high is listed twice." });
  });
});
