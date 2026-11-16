/// <reference types="vite-plus/client" />

import {
  DEFAULT_SERVER_SETTINGS,
  MCP_THREAD_PROFILES_MAX,
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
  clearProjectSettingsOverridesWithForkKeys,
  forkSettingsWriteProblem,
  keepLeftOutForkProjectKeys,
  splitForkSettingsPatch,
} from "./forkSettingsPatch.ts";
import { clearProjectSettingsOverrides } from "./projectSettings.ts";
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

    describe(`the ${MCP_THREAD_PROFILES_MAX}-pick cap`, () => {
      const full: ServerSettings = {
        ...current,
        mcpThreadProfiles: Object.fromEntries(
          Array.from({ length: MCP_THREAD_PROFILES_MAX }, (_, i) => [`old-${i}`, "t3-only"]),
        ),
      };
      const after = (base: ServerSettings, patch: Parameters<typeof applyServerSettingsPatch>[1]) =>
        applyServerSettingsPatch(base, patch);

      it("never widens a live thread to make room", () => {
        const next = applyForkSettingsPatch(full, {
          mcpThreadProfiles: { [t("new")]: "t3-only" },
        }).mcpThreadProfiles!;
        expect(next["old-0"]).toBe("t3-only");
        expect(Object.keys(next)).toHaveLength(MCP_THREAD_PROFILES_MAX + 1);
      });

      it("refuses the save that would go over, saying to remove old picks first", () => {
        const patch = { mcpThreadProfiles: { [t("new")]: "t3-only" } };
        expect(forkSettingsWriteProblem(full, after(full, patch))).toBe(
          `mcpThreadProfiles would hold ${MCP_THREAD_PROFILES_MAX + 1} entries, more than the ${MCP_THREAD_PROFILES_MAX} allowed: remove old picks first, then save again.`,
        );
      });

      it("takes a save at the cap that replaces or removes picks", () => {
        // Replaces an existing pick: still 500.
        expect(
          forkSettingsWriteProblem(
            full,
            after(full, { mcpThreadProfiles: { [t("old-0")]: "all" } }),
          ),
        ).toBeUndefined();
        // Removes one and adds one in the same save: still 500.
        expect(
          forkSettingsWriteProblem(
            full,
            after(full, { mcpThreadProfiles: { [t("old-1")]: null, [t("new")]: "all" } }),
          ),
        ).toBeUndefined();
      });

      it("lets a map already over the cap (a hand edit) take saves that do not grow it", () => {
        const over: ServerSettings = {
          ...full,
          mcpThreadProfiles: { ...full.mcpThreadProfiles, extra: "t3-only" },
        };
        expect(
          forkSettingsWriteProblem(over, after(over, { enableAgentBrowserAccess: false })),
        ).toBeUndefined();
        expect(
          forkSettingsWriteProblem(
            over,
            after(over, { mcpThreadProfiles: { [t("old-0")]: null } }),
          ),
        ).toBeUndefined();
        expect(
          forkSettingsWriteProblem(over, after(over, { mcpThreadProfiles: { [t("new")]: "all" } })),
        ).toMatch(/remove old picks first/);
      });
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

describe("a project save keeps the fork keys its entry leaves out", () => {
  const project = ProjectId.make("project-1");
  const other = ProjectId.make("project-2");
  const base: ServerSettings = {
    ...current,
    mcpProfiles: { lean: { servers: [], t3Tools: [] } },
    projectSettingsOverrides: {
      [project]: { mcpProfile: "lean", mcpSubagentProfile: "t3-only", defaultProjectScripts: [] },
      [other]: { defaultAutoPull: true },
    },
  };
  type Patch = Parameters<typeof applyServerSettingsPatch>[1];
  // What the server does: upstream's patch, then the kept keys (apps/server/src/fork).
  const save = (settings: ServerSettings, patch: Patch) =>
    keepLeftOutForkProjectKeys(settings, patch, applyServerSettingsPatch(settings, patch));

  it("an older client's project save keeps the project's MCP profile", () => {
    const patch = { projectSettingsOverrides: { [project]: { defaultProjectScripts: [] } } };
    const next = save(base, patch);
    expect(next.projectSettingsOverrides[project]).toEqual({
      mcpProfile: "lean",
      mcpSubagentProfile: "t3-only",
      defaultProjectScripts: [],
    });
    expect(next.projectSettingsOverrides[other]).toEqual({ defaultAutoPull: true });
    expect(forkSettingsWriteProblem(base, next)).toBeUndefined();
    // Run again: the same result.
    expect(save(next, patch)).toEqual(next);
  });

  it("keeps them when the entry comes back empty (upstream would drop the entry)", () => {
    expect(
      save(base, { projectSettingsOverrides: { [project]: {} } }).projectSettingsOverrides,
    ).toEqual({
      [project]: { mcpProfile: "lean", mcpSubagentProfile: "t3-only" },
      [other]: { defaultAutoPull: true },
    });
  });

  it("clears a key only on an explicit empty value, and changes one that is sent", () => {
    const patch = {
      projectSettingsOverrides: { [project]: { mcpProfile: "", mcpSubagentProfile: "lean" } },
    };
    const next = save(base, patch);
    // Cleared means not stored: the project inherits again, as after upstream's clear.
    expect(next.projectSettingsOverrides[project]).toEqual({ mcpSubagentProfile: "lean" });
    expect(save(next, patch)).toEqual(next);
  });

  it("drops a project's entry that an explicit empty value leaves with nothing", () => {
    const only: ServerSettings = {
      ...base,
      projectSettingsOverrides: { [project]: { mcpProfile: "lean" } },
    };
    const next = save(only, { projectSettingsOverrides: { [project]: { mcpProfile: "" } } });
    expect(next.projectSettingsOverrides).toEqual({});
    // A new project sent with only an empty value stores nothing either.
    expect(
      save(base, { projectSettingsOverrides: { [ProjectId.make("new")]: { mcpProfile: "" } } })
        .projectSettingsOverrides,
    ).toEqual(base.projectSettingsOverrides);
  });

  it("keeps nothing for a removed project or one that had no keys", () => {
    const next = save(base, {
      projectSettingsOverrides: { [project]: null, [other]: { defaultAutoPull: false } },
    });
    expect(next.projectSettingsOverrides).toEqual({ [other]: { defaultAutoPull: false } });
  });

  it("does not keep a stored blank MCP key an older client left out", () => {
    // A hand edit's `""` or `null` reads as `""` (not set); it is not carried into the next save.
    const s = {
      ...base,
      projectSettingsOverrides: { [project]: { mcpProfile: "", defaultAutoPull: true } },
    } as ServerSettings;
    const patch = { projectSettingsOverrides: { [project]: { defaultAutoPull: false } } };
    const next = save(s, patch);
    expect(next.projectSettingsOverrides[project]).toEqual({ defaultAutoPull: false });
    expect(save(next, patch)).toEqual(next);
    // Only the blank key goes: a set one beside it is still kept.
    const mixed = {
      ...base,
      projectSettingsOverrides: { [project]: { mcpProfile: "", mcpSubagentProfile: "lean" } },
    } as ServerSettings;
    expect(
      save(mixed, { projectSettingsOverrides: { [project]: {} } }).projectSettingsOverrides[
        project
      ],
    ).toEqual({ mcpSubagentProfile: "lean" });
  });

  it("returns the settings untouched when no project entry leaves a stored key out", () => {
    const patch = { enableAgentBrowserAccess: false };
    const next = applyServerSettingsPatch(base, patch);
    expect(keepLeftOutForkProjectKeys(base, patch, next)).toBe(next);
  });
});

describe("clearing a project's MCP profile (reset to inherited)", () => {
  const p1 = ProjectId.make("project-1");
  const base = {
    ...current,
    mcpProfile: "t3-only",
    projectSettingsOverrides: { [p1]: { mcpProfile: "all", defaultAutoPull: true } },
  } as ServerSettings;
  type Patch = Parameters<typeof applyServerSettingsPatch>[1];
  const save = (settings: ServerSettings, patch: Patch) =>
    keepLeftOutForkProjectKeys(settings, patch, applyServerSettingsPatch(settings, patch));

  it("clearing a project's MCP profile with the fork's clear helper clears it", () => {
    const patch = {
      projectSettingsOverrides: {
        [p1]: clearProjectSettingsOverridesWithForkKeys(base, p1, ["mcpProfile"]),
      },
    };
    const next = save(base, patch);
    expect(next.projectSettingsOverrides[p1]).toEqual({ defaultAutoPull: true });
    expect(next.projectSettingsOverrides[p1]?.mcpProfile).toBeUndefined();
    expect(forkSettingsWriteProblem(base, next)).toBeUndefined();
    // Run again (a second click, or a retry after a lost reply): the same result.
    expect(
      save(next, {
        projectSettingsOverrides: {
          [p1]: clearProjectSettingsOverridesWithForkKeys(next, p1, ["mcpProfile"]),
        },
      }),
    ).toEqual(next);
  });

  it("clears both MCP keys and other keys at once, and removes an entry left with nothing", () => {
    const both = {
      ...base,
      projectSettingsOverrides: { [p1]: { mcpProfile: "all", mcpSubagentProfile: "all" } },
    } as ServerSettings;
    expect(clearProjectSettingsOverridesWithForkKeys(both, p1, ["mcpProfile"])).toEqual({
      mcpProfile: "",
      mcpSubagentProfile: "all",
    });
    expect(
      clearProjectSettingsOverridesWithForkKeys(both, p1, ["mcpProfile", "mcpSubagentProfile"]),
    ).toBeNull();
    expect(
      save(base, {
        projectSettingsOverrides: {
          [p1]: clearProjectSettingsOverridesWithForkKeys(base, p1, [
            "mcpProfile",
            "defaultAutoPull",
          ]),
        },
      }).projectSettingsOverrides,
    ).toEqual({});
  });

  it("does not send a stored blank MCP key back when clearing it", () => {
    const blank = {
      ...base,
      projectSettingsOverrides: { [p1]: { mcpProfile: "", defaultAutoPull: true } },
    } as ServerSettings;
    // Nothing to clear: the key is left out, and the server keeps a blank value no longer.
    expect(clearProjectSettingsOverridesWithForkKeys(blank, p1, ["mcpProfile"])).toEqual({
      defaultAutoPull: true,
    });
    const patch = {
      projectSettingsOverrides: {
        [p1]: clearProjectSettingsOverridesWithForkKeys(blank, p1, ["mcpProfile"]),
      },
    };
    expect(save(blank, patch).projectSettingsOverrides[p1]).toEqual({ defaultAutoPull: true });
  });

  it("is upstream's clear helper when no stored MCP key is cleared", () => {
    for (const keys of [
      ["defaultAutoPull"],
      ["mcpSubagentProfile"],
      ["defaultAutoPull", "mcpSubagentProfile"],
    ] as const) {
      expect(clearProjectSettingsOverridesWithForkKeys(base, p1, keys)).toEqual(
        clearProjectSettingsOverrides(base, p1, keys),
      );
    }
    const missing = ProjectId.make("missing");
    expect(clearProjectSettingsOverridesWithForkKeys(base, missing, ["mcpProfile"])).toBeNull();
    expect(clearProjectSettingsOverrides(base, missing, ["mcpProfile"])).toBeNull();
  });

  it("pins upstream's helper keeping the profile (it leaves the key out, like an older client)", () => {
    // If this turns red, upstream's clear changed: re-check the fork helper and the PR's notes.
    const patch = {
      projectSettingsOverrides: { [p1]: clearProjectSettingsOverrides(base, p1, ["mcpProfile"]) },
    };
    expect(save(base, patch).projectSettingsOverrides[p1]?.mcpProfile).toBe("all");
  });
});
