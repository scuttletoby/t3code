// Fork (placement row P2): saving `placement` and `placementNeeds` through the settings patch, as
// the server does (apply the patch, check it, then write and read the file again).
import {
  DEFAULT_SERVER_SETTINGS,
  ProjectId,
  ServerSettings,
  ServerSettingsPatch,
  readPlacementNeeds,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  clearProjectSettingsOverridesWithForkKeys,
  forkSettingsWriteProblem,
  keepLeftOutForkProjectKeys,
  placementFactsCommandSaveProblem,
} from "./forkSettingsPatch.ts";
import { resolveProjectSettings } from "./projectSettings.ts";
import { applyServerSettingsPatch } from "./serverSettings.ts";

const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const decodeSettings = Schema.decodeUnknownSync(ServerSettings);
const encodeSettings = Schema.encodeSync(ServerSettings);
const PROJECT = ProjectId.make("project-1");

/** The file as the server writes it and reads it back. */
const throughFile = (settings: ServerSettings): ServerSettings =>
  decodeSettings(JSON.parse(JSON.stringify(encodeSettings(settings))));

/**
 * One save as the server makes it: the patch over the wire, applied, checked (a refused save
 * throws its problem), written and read back. `platform` is the server's OS.
 */
const save = (current: ServerSettings, patch: unknown, platform = "linux"): ServerSettings => {
  const decoded = decodePatch(JSON.parse(JSON.stringify(patch)));
  const next = keepLeftOutForkProjectKeys(
    current,
    decoded,
    applyServerSettingsPatch(current, decoded),
  );
  const problem =
    forkSettingsWriteProblem(current, next) ??
    placementFactsCommandSaveProblem(current, next, platform);
  if (problem !== undefined) throw new Error(problem);
  return throughFile(next);
};

/** The settings as a client holds them (sent over the wire, read again). */
const onClient = (settings: ServerSettings): ServerSettings => throughFile(settings);

/** Settings with other fork settings on, as one save leaves them (upstream adds derived keys). */
const START = save(
  throughFile({
    ...DEFAULT_SERVER_SETTINGS,
    agentSlots: { ...DEFAULT_SERVER_SETTINGS.agentSlots, enabled: true, maxPerMachine: 3 },
    waitFor: { ...DEFAULT_SERVER_SETTINGS.waitFor, enabled: true },
    usagePacingGate: "advise",
    defaultAutoPull: false,
  }),
  {},
);

/** Every setting but the ones named, to show a save changed nothing else. */
const others = (settings: ServerSettings, ...keys: ReadonlyArray<string>) =>
  Object.fromEntries(Object.entries(settings).filter(([key]) => !keys.includes(key)));

const FULL = {
  mode: "advise",
  labels: ["gpu", "ci-runner"],
  factsCommand: "/usr/local/bin/placement-facts",
  never: [["ci-runner", "owner-logins"]],
  weights: { "env-desk": "prefer" },
};

describe("saving placement", () => {
  it("sets it, takes a partial save, clears a field, and keeps every other setting", () => {
    const set = save(START, { placement: FULL });
    expect(set.placement).toEqual(FULL);
    expect(others(set, "placement")).toEqual(others(START, "placement"));

    // A save that names only the mode keeps the labels, the command, the groups and the weights.
    const partial = save(set, { placement: { mode: "enforce" } });
    expect(partial.placement).toEqual({ ...FULL, mode: "enforce" });
    expect(others(partial, "placement")).toEqual(others(START, "placement"));

    // A cleared field is its empty value; the rest stays.
    const cleared = save(partial, { placement: { labels: [], weights: {}, factsCommand: "" } });
    expect(cleared.placement).toEqual({
      ...FULL,
      mode: "enforce",
      labels: [],
      weights: {},
      factsCommand: "",
    });
    expect(others(cleared, "placement")).toEqual(others(START, "placement"));

    // Back to off, and a save of another setting leaves placement as it was.
    const off = save(cleared, { placement: { mode: "off" } });
    expect(off.placement.mode).toBe("off");
    expect(off.placement.never).toEqual(FULL.never);
    const other = save(off, { usagePacingGate: "off" });
    expect(other.placement).toEqual(off.placement);
  });

  it("gives the same settings when the same save runs again", () => {
    for (const patch of [
      { placement: FULL },
      { placement: { mode: "enforce" } },
      { placement: { labels: [] } },
      { placementNeeds: ["gpu"] },
    ]) {
      const once = save(START, patch);
      expect(save(once, patch), JSON.stringify(patch)).toEqual(once);
    }
  });

  it("keeps a bad value as written through other saves; only a whole save replaces it", () => {
    const bad = {
      mode: "enforce",
      labels: ["gpu"],
      never: [["ci-runner", "owner-logins"]],
      weights: { "env-desk": "bad" },
    };
    const stored = decodeSettings({ ...encodeSettings(START), placement: bad });
    expect(stored.placement.mode).toBe("off");
    expect(stored.placement.unreadable).toEqual(bad);

    // A save of another setting (stopped half-way and run again, too) keeps the person's text.
    const other = save(stored, { usagePacingGate: "off" });
    expect(other.placement).toEqual(stored.placement);
    expect(encodeSettings(save(other, { usagePacingGate: "off" })).placement).toEqual(bad);

    // A save of some fields is refused: merged onto off it would drop the never group (and here
    // switch enforce on). Nothing changes; the text is kept. A no-op one is refused too.
    for (const partial of [
      { mode: "enforce" },
      { mode: "off" },
      { mode: "advise", labels: ["gpu"], factsCommand: "", never: [] },
    ]) {
      expect(() => save(stored, { placement: partial }), JSON.stringify(partial)).toThrow(
        "placement is not valid; save the whole setting",
      );
    }
    expect(encodeSettings(stored).placement).toEqual(bad);

    // A save of every field replaces it: the kept text and its problem are gone.
    const whole = { ...FULL, mode: "enforce" };
    const fixed = save(stored, { placement: whole });
    expect(fixed.placement).toEqual(whole);
    expect(Object.hasOwn(fixed.placement, "unreadable")).toBe(false);
    expect(Object.hasOwn(fixed.placement, "problem")).toBe(false);
    expect(others(fixed, "placement")).toEqual(others(stored, "placement"));
    // Runs again the same; after it, a partial save merges as usual.
    expect(save(fixed, { placement: whole })).toEqual(fixed);
    expect(save(fixed, { placement: { mode: "off" } }).placement).toEqual({
      ...whole,
      mode: "off",
    });
    // A whole save that is not valid is refused, as any bad save.
    expect(() => save(stored, { placement: { ...whole, mode: "on" } })).toThrow();
  });

  // [factsCommand, the server's OS, refused]
  const FACTS_SAVES: ReadonlyArray<readonly [string, string, boolean]> = [
    ["/usr/local/bin/facts", "linux", false],
    ["/usr/local/bin/facts", "darwin", false],
    ["/usr/bin/x", "win32", true],
    ["C:\\facts.exe", "linux", true],
    ["C:\\facts.exe", "darwin", true],
    ["C:\\facts.exe", "win32", false],
    ["", "win32", false],
  ];

  it.each(FACTS_SAVES)("saving factsCommand %j on %s: refused %s", (command, platform, refused) => {
    const patch = { placement: { ...FULL, factsCommand: command } };
    if (refused) {
      expect(() => save(START, patch, platform)).toThrow(
        `placement: factsCommand "${command}" is not a full path on this server's OS`,
      );
    } else {
      expect(save(START, patch, platform).placement.factsCommand).toBe(command);
    }
  });

  it("never refuses another save over a stored factsCommand of another OS", () => {
    // A hand edit the load did not catch (it reads it as off on the server): another save passes.
    const stored = save(START, { placement: { ...FULL, factsCommand: "C:\\facts.exe" } }, "win32");
    expect(save(stored, { usagePacingGate: "off" }, "linux").placement).toEqual(stored.placement);
    expect(save(stored, { placement: { mode: "off" } }, "linux").placement.factsCommand).toBe(
      "C:\\facts.exe",
    );
  });
});

describe("saving placementNeeds", () => {
  const withProject = (needs: unknown) =>
    save(START, {
      placementNeeds: ["ci-runner"],
      projectSettingsOverrides: { [PROJECT]: { placementNeeds: needs, defaultAutoPull: true } },
    });

  it("resolves a project's own needs over the server's, and the server's without them", () => {
    const settings = withProject(["gpu"]);
    expect(resolveProjectSettings(settings, PROJECT).settings.placementNeeds).toEqual(["gpu"]);
    expect(
      resolveProjectSettings(settings, ProjectId.make("project-2")).settings.placementNeeds,
    ).toEqual(["ci-runner"]);
    // `[]` is the project's own: it needs nothing, whatever the server's says.
    expect(resolveProjectSettings(withProject([]), PROJECT).settings.placementNeeds).toEqual([]);
    // Nothing set anywhere: no needs.
    expect(resolveProjectSettings(START, PROJECT).settings.placementNeeds).toEqual([]);
  });

  it("fails closed on bad needs: kept through other saves, replaced by a save of the key", () => {
    for (const bad of ["gpu", ["gpu", "cuda 12"]]) {
      const stored = decodeSettings({
        ...encodeSettings(START),
        placementNeeds: bad,
        projectSettingsOverrides: { [PROJECT]: { placementNeeds: bad, defaultAutoPull: true } },
      });
      // Both the server's and the project's read as fitting no machine.
      expect(readPlacementNeeds(stored.placementNeeds).fits).toBe("none");
      const resolved = resolveProjectSettings(stored, PROJECT).settings.placementNeeds;
      expect(readPlacementNeeds(resolved).fits).toBe("none");
      const other = resolveProjectSettings(stored, ProjectId.make("project-2")).settings;
      expect(readPlacementNeeds(other.placementNeeds).fits).toBe("none");

      // Unrelated saves keep the person's text: a server key, an older client's project save that
      // leaves the key out, and a client that resends the project's whole entry. Run twice: the
      // same file, no growth.
      const client = onClient(stored);
      const project = client.projectSettingsOverrides[PROJECT]!;
      for (const patch of [
        { usagePacingGate: "off" },
        { projectSettingsOverrides: { [PROJECT]: { defaultAutoPull: false } } },
        { projectSettingsOverrides: { [PROJECT]: { ...project, defaultAutoPull: false } } },
      ]) {
        const once = save(stored, patch);
        expect(encodeSettings(once).placementNeeds, JSON.stringify(patch)).toEqual(bad);
        const file = encodeSettings(once).projectSettingsOverrides as Record<string, unknown>;
        expect(file[PROJECT]).toMatchObject({ placementNeeds: bad });
        expect(save(once, patch)).toEqual(once);
        expect(JSON.stringify(encodeSettings(save(once, patch)))).toBe(
          JSON.stringify(encodeSettings(once)),
        );
      }

      // A resend of other kept text (changed elsewhere) is refused.
      expect(() =>
        save(stored, {
          projectSettingsOverrides: { [PROJECT]: { placementNeeds: { unreadable: "cuda" } } },
        }),
      ).toThrow("Project project-1's placementNeeds sends back unreadable text");

      // A save of the key replaces the text and clears the problem.
      const fixed = save(stored, {
        placementNeeds: ["GPU"],
        projectSettingsOverrides: { [PROJECT]: { placementNeeds: ["cuda"] } },
      });
      expect(fixed.placementNeeds).toEqual(["gpu"]);
      expect(fixed.projectSettingsOverrides[PROJECT]?.placementNeeds).toEqual(["cuda"]);
      expect(readPlacementNeeds(fixed.placementNeeds)).toEqual({ fits: "labels", labels: ["gpu"] });
      // A project's null clears its own; it then uses the server's.
      const cleared = save(stored, {
        projectSettingsOverrides: { [PROJECT]: { placementNeeds: null } },
      });
      expect(Object.hasOwn(cleared.projectSettingsOverrides[PROJECT] ?? {}, "placementNeeds")).toBe(
        false,
      );
    }
  });

  it("keeps a project's needs when an older client's save leaves them out; null clears them", () => {
    const settings = withProject(["gpu"]);
    const older = { projectSettingsOverrides: { [PROJECT]: { defaultAutoPull: false } } };
    const kept = save(settings, older);
    expect(kept.projectSettingsOverrides[PROJECT]).toEqual({
      placementNeeds: ["gpu"],
      defaultAutoPull: false,
    });
    expect(save(kept, older)).toEqual(kept);

    const clear = { projectSettingsOverrides: { [PROJECT]: { placementNeeds: null } } };
    const cleared = save(kept, clear);
    expect(Object.hasOwn(cleared.projectSettingsOverrides[PROJECT] ?? {}, "placementNeeds")).toBe(
      false,
    );
    expect(resolveProjectSettings(cleared, PROJECT).settings.placementNeeds).toEqual(["ci-runner"]);
    expect(save(cleared, clear)).toEqual(cleared);
    // The fork's reset helper sends that null.
    expect(clearProjectSettingsOverridesWithForkKeys(kept, PROJECT, ["placementNeeds"])).toEqual({
      placementNeeds: null,
      defaultAutoPull: false,
    });
  });
});
