// Fork (placement row P2): saving `placement` and `placementNeeds` through the settings patch, as
// the server does (apply the patch, then write and read the file again).
import {
  DEFAULT_PLACEMENT_SETTINGS,
  DEFAULT_SERVER_SETTINGS,
  ProjectId,
  ServerSettings,
  ServerSettingsPatch,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  clearProjectSettingsOverridesWithForkKeys,
  keepLeftOutForkProjectKeys,
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

/** One save as the server makes it: the patch over the wire, applied, written and read back. */
const save = (current: ServerSettings, patch: unknown): ServerSettings => {
  const decoded = decodePatch(JSON.parse(JSON.stringify(patch)));
  return throughFile(
    keepLeftOutForkProjectKeys(current, decoded, applyServerSettingsPatch(current, decoded)),
  );
};

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

  it("keeps a bad value as written through other saves; saving the key replaces it from off", () => {
    const bad = { mode: "always", labels: ["gpu"] };
    const stored = decodeSettings({ ...encodeSettings(START), placement: bad });
    expect(stored.placement.mode).toBe("off");
    expect(stored.placement.unreadable).toEqual(bad);

    // A save of another setting (stopped half-way and run again, too) keeps the person's text.
    const other = save(stored, { usagePacingGate: "off" });
    expect(other.placement).toEqual(stored.placement);
    expect(encodeSettings(save(other, { usagePacingGate: "off" })).placement).toEqual(bad);

    // A save of one field starts from off: the kept text and its problem are gone.
    const fixed = save(stored, { placement: { mode: "advise" } });
    expect(fixed.placement).toEqual({ ...DEFAULT_PLACEMENT_SETTINGS, mode: "advise" });
    expect(Object.hasOwn(fixed.placement, "unreadable")).toBe(false);
    expect(Object.hasOwn(fixed.placement, "problem")).toBe(false);
    expect(save(fixed, { placement: { mode: "advise" } })).toEqual(fixed);
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
