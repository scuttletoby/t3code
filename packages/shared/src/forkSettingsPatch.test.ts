/// <reference types="vite-plus/client" />

import {
  DEFAULT_SERVER_SETTINGS,
  MCP_THREAD_PROFILES_KEEP_NEWEST,
  ProjectId,
  ProviderInstanceId,
  type ServerSettings,
  ThreadId,
} from "@t3tools/contracts";
import { WaitCheck } from "@t3tools/contracts/waitFor";
import * as Duration from "effect/Duration";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  applyForkSettingsPatch,
  forkSettingsWriteProblem,
  splitForkSettingsPatch,
} from "./forkSettingsPatch.ts";
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

describe("fork settings save check (MCP profile references)", () => {
  const project = ProjectId.make("project-1");
  const stored: ServerSettings = {
    ...DEFAULT_SERVER_SETTINGS,
    mcpProfiles: { lean: { servers: [], t3Tools: ["threads"] } },
    mcpThreadProfiles: { "thread-old": "(unreadable number)" },
    // Odd values a hand edit or an older build left; reading keeps them.
    projectSettingsOverrides: {
      [project]: { mcpProfile: "Lean", mcpSubagentProfile: "" },
    },
  };
  const problemAfter = (patch: Parameters<typeof applyServerSettingsPatch>[1]) =>
    forkSettingsWriteProblem(stored, applyServerSettingsPatch(stored, patch));

  it("lets a project save its other settings while an odd stored reference stays as it was", () => {
    expect(
      problemAfter({
        projectSettingsOverrides: {
          [project]: {
            mcpProfile: "Lean",
            mcpSubagentProfile: "",
            enableAgentBrowserAccess: true,
          },
        },
      }),
    ).toBeUndefined();
    expect(problemAfter({ enableAgentBrowserAccess: false })).toBeUndefined();
    // An unreadable pick left as it was does not block another thread's pick.
    expect(problemAfter({ mcpThreadProfiles: { "thread-1": "lean" } })).toBeUndefined();
  });

  it.each([
    ["a default that names a profile", { mcpProfile: "lean" }],
    ["a built-in default", { mcpProfile: "t3-only" }],
    ["inherit as the sub-agent rule", { mcpSubagentProfile: "inherit" }],
    ["a pick of a built-in", { mcpThreadProfiles: { "thread-1": "all" } }],
    [
      "a default added in the same save",
      { mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } }, mcpProfile: "docs" },
    ],
    [
      "a project's reference cleared",
      { projectSettingsOverrides: { [project]: { mcpProfile: "" } } },
    ],
    [
      "inherit as a project's sub-agent rule",
      {
        projectSettingsOverrides: {
          [project]: { mcpProfile: "Lean", mcpSubagentProfile: "inherit" },
        },
      },
    ],
  ] as const)("takes %s", (_name, patch) => {
    expect(problemAfter(patch)).toBeUndefined();
  });

  it.each([
    [
      "a default that names no profile",
      { mcpProfile: "gone" },
      /^mcpProfile names MCP profile "gone", which is not set up: add that profile first, or pick "all" or "t3-only"\.$/,
    ],
    ["inherit as the default", { mcpProfile: "inherit" }, /^mcpProfile cannot be "inherit"/],
    [
      "a sub-agent rule that names no profile",
      { mcpSubagentProfile: "gone" },
      /^mcpSubagentProfile names MCP profile "gone"/,
    ],
    [
      "a pick that names no profile",
      { mcpThreadProfiles: { "thread-1": "gone" } },
      /^The pick for thread thread-1 names MCP profile "gone"/,
    ],
    [
      "inherit as a pick",
      { mcpThreadProfiles: { "thread-1": "inherit" } },
      /^The pick for thread thread-1 cannot be "inherit"/,
    ],
    [
      "a project's changed reference to no profile",
      { projectSettingsOverrides: { [project]: { mcpProfile: "Docs" } } },
      /^Project project-1's mcpProfile names MCP profile "Docs"/,
    ],
    [
      "inherit as a project's default",
      { projectSettingsOverrides: { [project]: { mcpProfile: "inherit" } } },
      /^Project project-1's mcpProfile cannot be "inherit"/,
    ],
    [
      "a new project's reference to no profile",
      {
        projectSettingsOverrides: { [ProjectId.make("project-2")]: { mcpSubagentProfile: "gone" } },
      },
      /^Project project-2's mcpSubagentProfile names MCP profile "gone"/,
    ],
  ] as const)("refuses %s, saying how to fix it", (_name, patch, message) => {
    expect(problemAfter(patch)).toMatch(message);
  });
});
