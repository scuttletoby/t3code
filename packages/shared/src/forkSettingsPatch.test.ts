/// <reference types="vite-plus/client" />

import {
  DEFAULT_SERVER_SETTINGS,
  MCP_THREAD_PROFILES_KEEP_NEWEST,
  ProviderInstanceId,
  type ServerSettings,
  ThreadId,
} from "@t3tools/contracts";
import { WaitCheck } from "@t3tools/contracts/waitFor";
import * as Duration from "effect/Duration";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { applyForkSettingsPatch, splitForkSettingsPatch } from "./forkSettingsPatch.ts";
import { applyServerSettingsPatch } from "./serverSettings.ts";
// The source text, to pin the fork's lines in upstream's file.
import SERVER_SETTINGS_SOURCE from "./serverSettings.ts?raw";

const claude = ProviderInstanceId.make("claude-a");
const codex = ProviderInstanceId.make("codex-b");

const greenCheck = Schema.decodeSync(WaitCheck)({ argv: ["/bin/true"] });

const current: ServerSettings = {
  ...DEFAULT_SERVER_SETTINGS,
  usagePacing: {
    [claude]: { reservePercent: 20, burnFirst: true, noLimits: false },
    [codex]: { reservePercent: 5, burnFirst: false, noLimits: true },
  },
  usagePacingLowerEffort: { high: "medium", xhigh: "high" },
  waitFor: { ...DEFAULT_SERVER_SETTINGS.waitFor, enabled: true, maxPerThread: 7 },
  waitChecks: { "ci-green": greenCheck },
  agentSlots: { ...DEFAULT_SERVER_SETTINGS.agentSlots, maxPerAccount: { [claude]: 2 } },
};

describe("fork settings patch", () => {
  it("splits off the fork fields and leaves every other key", () => {
    const [fork, rest] = splitForkSettingsPatch({
      enableAgentBrowserAccess: true,
      usagePacingGate: "advise",
      automaticUpdates: true,
    });
    expect(fork).toEqual({ usagePacingGate: "advise" });
    // `automaticUpdates` is inline in settings.ts, so upstream's merge applies it.
    expect(rest).toEqual({ enableAgentBrowserAccess: true, automaticUpdates: true });
    expect(applyForkSettingsPatch(current, {})).toEqual({});
  });

  it("replaces a `replace` field whole", () => {
    expect(
      applyForkSettingsPatch(current, {
        usagePacingLowerEffort: { high: "low" },
        waitChecks: {},
        headlessUsageRefreshInterval: Duration.zero,
      }),
    ).toEqual({
      usagePacingLowerEffort: { high: "low" },
      waitChecks: {},
      headlessUsageRefreshInterval: Duration.zero,
    });
  });

  it("merges a `merge-fields` field: a field left out keeps its stored value", () => {
    const next = applyForkSettingsPatch(current, {
      waitFor: { maxPerThread: 9 },
      agentSlots: { maxPerAccount: { [codex]: 1 } },
    });
    expect(next.waitFor).toEqual({ ...current.waitFor, maxPerThread: 9 });
    // `maxPerAccount` is one field, so it is replaced whole.
    expect(next.agentSlots).toEqual({ ...current.agentSlots, maxPerAccount: { [codex]: 1 } });
  });

  it("merges entries: null removes one, a new one starts from the defaults", () => {
    const next = applyForkSettingsPatch(current, {
      usagePacing: {
        [claude]: { burnFirst: false },
        [codex]: null,
        [ProviderInstanceId.make("new-c")]: { reservePercent: 10 },
      },
    });
    expect(next.usagePacing).toEqual({
      [claude]: { reservePercent: 20, burnFirst: false, noLimits: false },
      "new-c": { reservePercent: 10, burnFirst: false, noLimits: false },
    });
  });

  it("is what applyServerSettingsPatch does with a patch's fork fields", () => {
    const next = applyServerSettingsPatch(current, {
      enableAgentBrowserAccess: false,
      usagePacing: { [codex]: null },
      waitFor: { prSweepSeconds: 60 },
      headlessUsageRefreshInterval: Duration.minutes(5),
    });
    expect(next.enableAgentBrowserAccess).toBe(false);
    expect(Object.keys(next.usagePacing)).toEqual([claude]);
    expect(next.waitFor).toEqual({ ...current.waitFor, prSweepSeconds: 60 });
    expect(Duration.toMillis(next.headlessUsageRefreshInterval)).toBe(300_000);
    expect(next.waitChecks).toBe(current.waitChecks);
  });

  describe("replaces entries (MCP profiles and thread picks)", () => {
    const t = (id: string) => ThreadId.make(id);
    const withPicks: ServerSettings = {
      ...current,
      mcpProfiles: { lean: { servers: [], t3Tools: ["threads"], future: true } },
      mcpThreadProfiles: { a: "lean", b: "lean", c: "all" },
    };

    it("keeps both of two picks saved at once", () => {
      const first = {
        ...withPicks,
        ...applyForkSettingsPatch(withPicks, { mcpThreadProfiles: { [t("d")]: "lean" } }),
      };
      // The second client's patch names only its own thread, so the first pick stays.
      const second = applyForkSettingsPatch(first, { mcpThreadProfiles: { [t("e")]: "all" } });
      expect(second.mcpThreadProfiles).toEqual({
        a: "lean",
        b: "lean",
        c: "all",
        d: "lean",
        e: "all",
      });
    });

    it("removes a pick on null and moves a changed pick to the newest", () => {
      const next = applyForkSettingsPatch(withPicks, {
        mcpThreadProfiles: { [t("a")]: "all", [t("b")]: null },
      });
      expect(Object.entries(next.mcpThreadProfiles ?? {})).toEqual([
        ["c", "all"],
        ["a", "all"],
      ]);
    });

    it(`keeps the ${MCP_THREAD_PROFILES_KEEP_NEWEST} newest picks`, () => {
      const many = Object.fromEntries(
        Array.from({ length: MCP_THREAD_PROFILES_KEEP_NEWEST }, (_, index) => [
          `old-${index}`,
          "lean",
        ]),
      );
      const full: ServerSettings = { ...current, mcpThreadProfiles: many };
      const next = applyForkSettingsPatch(full, {
        mcpThreadProfiles: { [t("old-0")]: "all", [t("new")]: "lean" },
      }).mcpThreadProfiles!;
      expect(Object.keys(next)).toHaveLength(MCP_THREAD_PROFILES_KEEP_NEWEST);
      // `old-0` was picked again, so `old-1` is now the oldest and goes.
      expect(Object.keys(next).slice(0, 1)).toEqual(["old-2"]);
      expect(Object.keys(next).slice(-2)).toEqual(["old-0", "new"]);
      expect(next).not.toHaveProperty("old-1");
    });

    it("replaces a profile whole, so a save repairs one this build cannot read", () => {
      const next = applyForkSettingsPatch(withPicks, {
        mcpProfiles: { lean: { servers: ["docs"], t3Tools: "all" }, review: null },
      });
      expect(next.mcpProfiles).toEqual({ lean: { servers: ["docs"], t3Tools: "all" } });
    });

    it("goes through applyServerSettingsPatch", () => {
      const next = applyServerSettingsPatch(withPicks, {
        mcpProfile: "lean",
        mcpSubagentProfile: "t3-only",
        mcpThreadProfiles: { [t("c")]: null },
      });
      expect([next.mcpProfile, next.mcpSubagentProfile]).toEqual(["lean", "t3-only"]);
      expect(next.mcpThreadProfiles).toEqual({ a: "lean", b: "lean" });
      expect(next.mcpProfiles).toBe(withPicks.mcpProfiles);
    });
  });

  it("pins the fork's lines in serverSettings.ts", () => {
    const lines = SERVER_SETTINGS_SOURCE.split("\n");
    // The import, the split, the destructuring from the upstream half and one spread.
    expect(
      lines.filter((line) =>
        /forkSettingsPatch|ForkSettingsPatch|forkPatch|upstreamPatch/.test(line),
      ),
    ).toHaveLength(4);
    expect(lines.filter((line) => /\bFork:/.test(line))).toHaveLength(1);
  });
});
