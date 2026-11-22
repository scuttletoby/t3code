import { type ProviderInstanceId, ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  burnFirstWindowsAfterToggle,
  formatLowerEffort,
  knownEffortLevels,
  parseLowerEffort,
  usagePacingAccountRows,
  usagePacingAccountSummary,
} from "./usagePacingSettings.logic";

const id = (value: string) => value as ProviderInstanceId;
const fromFile = Schema.decodeUnknownSync(ServerSettings);
const KNOWN = knownEffortLevels([]);

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
    // The window list decides what "spent first" covers.
    const listed = (burnFirstWindows: unknown) =>
      summary({ usagePacing: { a: { burnFirst: true, burnFirstWindows } } });
    expect(listed(["session", "weekly", "monthly", "other"])).toBe("No reserve, spent first");
    expect(listed(["monthly", "weekly"])).toBe("No reserve, weekly and monthly spent first");
    expect(listed(["weekly"])).toBe("No reserve, weekly spent first");
    expect(listed([])).toBe("No reserve, no window spent first");
    // An unreadable list reads as empty: nothing burns first, and the summary says so.
    expect(listed("weekly")).toBe("No reserve, no window spent first");
    // Without burnFirst the list does nothing.
    expect(summary({ usagePacing: { a: { burnFirstWindows: ["weekly"] } } })).toBe("No reserve");
  });

  it("toggles one window kind in the burn-first list, in the usual order (#975)", () => {
    const entry = (file: object) => Object.values(fromFile(file).usagePacing)[0]!;
    // No list yet: every kind burns first, so unticking one lists the other three.
    expect(
      burnFirstWindowsAfterToggle(
        entry({ usagePacing: { a: { burnFirst: true } } }),
        "session",
        false,
      ),
    ).toEqual(["weekly", "monthly", "other"]);
    const weekly = entry({ usagePacing: { a: { burnFirst: true, burnFirstWindows: ["weekly"] } } });
    expect(burnFirstWindowsAfterToggle(weekly, "monthly", true)).toEqual(["weekly", "monthly"]);
    expect(burnFirstWindowsAfterToggle(weekly, "session", true)).toEqual(["session", "weekly"]);
    expect(burnFirstWindowsAfterToggle(weekly, "weekly", false)).toEqual([]);
    // Ticking one already listed changes nothing (a double click saves the same list).
    expect(burnFirstWindowsAfterToggle(weekly, "weekly", true)).toEqual(["weekly"]);
  });
});

describe("lower effort field", () => {
  it("shows the stored map and reads it back unchanged", () => {
    const stored = fromFile({
      usagePacingLowerEffort: { medium: "low", high: "medium" },
    }).usagePacingLowerEffort;
    const text = formatLowerEffort(stored);
    expect(text).toBe("high=medium, medium=low");
    expect(parseLowerEffort(text, KNOWN)).toEqual({ value: stored });
    expect(formatLowerEffort(fromFile({}).usagePacingLowerEffort)).toBe("");
  });

  it("reads pairs split by commas or lines, and empty as no lowering", () => {
    expect(parseLowerEffort(" high = medium,\nxhigh=high ,", KNOWN)).toEqual({
      value: { high: "medium", xhigh: "high" },
    });
    expect(parseLowerEffort("  ", KNOWN)).toEqual({ value: {} });
  });

  it("refuses text that is not a list of pairs", () => {
    expect(parseLowerEffort("high", KNOWN)).toEqual({
      error: '"high" is not a pair like high=medium.',
    });
    expect(parseLowerEffort("high=medium=low", KNOWN)).toHaveProperty("error");
    expect(parseLowerEffort("high=low, high=medium", KNOWN)).toEqual({
      error: "high is listed twice.",
    });
  });

  it("refuses a level no model offers, and knows each provider model's own levels", () => {
    expect(parseLowerEffort("hgih=medium", KNOWN)).toHaveProperty("error");
    expect(parseLowerEffort("high=meduim", KNOWN)).toHaveProperty("error");
    const known = knownEffortLevels([
      {
        models: [
          {
            capabilities: {
              optionDescriptors: [
                {
                  id: "thought_level",
                  label: "Thought level",
                  type: "select",
                  role: "reasoning",
                  options: [{ id: "deep", label: "Deep" }],
                },
                {
                  id: "mode",
                  label: "Mode",
                  type: "select",
                  options: [{ id: "plan", label: "Plan" }],
                },
              ],
            },
          },
          { capabilities: null },
        ],
      },
    ]);
    expect(parseLowerEffort("deep=high", known)).toEqual({ value: { deep: "high" } });
    expect(parseLowerEffort("plan=high", known)).toHaveProperty("error");
  });
});
