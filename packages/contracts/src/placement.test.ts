// Fork (placement row P2): the `placement` and `placementNeeds` settings decode with their
// defaults, read a bad value as off or as fitting no machine (kept as written, with the reason),
// and read again the same.
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { ProjectId } from "./baseSchemas.ts";
import {
  DEFAULT_PLACEMENT_SETTINGS,
  PlacementSettingsPatch,
  canonicalPlacementLabels,
  isKeptPlacementNeedsUnchanged,
  placementFactsCommandProblem,
  placementOnPlatform,
  readPlacementNeeds,
} from "./placement.ts";
import { ProjectSettingsOverrides, ServerSettings, ServerSettingsPatch } from "./settings.ts";

const decodeSettings = Schema.decodeUnknownSync(ServerSettings);
const encodeSettings = Schema.encodeSync(ServerSettings);
const decodeOverrides = Schema.decodeUnknownSync(ProjectSettingsOverrides);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const encodePlacementPatch = Schema.encodeSync(PlacementSettingsPatch);
const PROJECT = ProjectId.make("project-1");

/** The file as the server writes it and reads it back, through JSON. */
const reread = (settings: ServerSettings) =>
  decodeSettings(JSON.parse(JSON.stringify(encodeSettings(settings))));

const OFF = { mode: "off", labels: [], factsCommand: "", never: [], weights: {} };

/** Other fork settings beside it, so a bad placement shows it breaks none of them. */
const NEIGHBOURS = {
  agentSlots: { enabled: true, maxPerMachine: 3 },
  waitFor: { enabled: true },
  usagePacingGate: "advise",
  defaultAutoPull: false,
};

const GOOD = {
  mode: "enforce",
  labels: ["gpu", "ci-runner"],
  factsCommand: "/usr/local/bin/placement-facts",
  never: [["ci-runner", "owner-logins"]],
  weights: { "env-desk": "prefer", "env-laptop": "manual" },
};

describe("placement setting", () => {
  it("reads an old or empty settings file as off, with no needs", () => {
    for (const file of [{}, { defaultAutoPull: false }, { placement: {} }]) {
      const settings = decodeSettings(file);
      expect(settings.placement).toEqual(OFF);
      expect(settings.placement).toEqual(DEFAULT_PLACEMENT_SETTINGS);
      expect(settings.placementNeeds).toEqual([]);
      // Nothing to report: no kept text, no problem.
      expect(Object.hasOwn(settings.placement, "unreadable")).toBe(false);
      expect(Object.hasOwn(settings.placement, "problem")).toBe(false);
    }
    // A missing field takes its default; the others keep theirs.
    expect(decodeSettings({ placement: { mode: "advise" } }).placement).toEqual({
      ...OFF,
      mode: "advise",
    });
  });

  it("reads a good value as written, trimmed and without repeats", () => {
    expect(decodeSettings({ placement: GOOD }).placement).toEqual(GOOD);
    const tidy = decodeSettings({
      placement: {
        labels: [" gpu ", "gpu", "ci-runner"],
        factsCommand: "  /opt/facts  ",
        never: [["a", "b", "a"]],
      },
    }).placement;
    expect(tidy.labels).toEqual(["gpu", "ci-runner"]);
    expect(tidy.factsCommand).toBe("/opt/facts");
    expect(tidy.never).toEqual([["a", "b"]]);
    // A Windows path is a full path too (the settings do not know the server's OS).
    expect(
      decodeSettings({ placement: { factsCommand: "C:\\tools\\facts.exe" } }).placement
        .factsCommand,
    ).toBe("C:\\tools\\facts.exe");
  });

  const BAD: ReadonlyArray<readonly [string, unknown]> = [
    ["bad mode", { ...GOOD, mode: "on" }],
    ["relative factsCommand", { ...GOOD, factsCommand: "bin/facts" }],
    ["factsCommand with no path", { ...GOOD, factsCommand: "facts" }],
    ["factsCommand with a newline", { ...GOOD, factsCommand: "/opt/facts\n/bin/sh" }],
    ["factsCommand with a NUL", { ...GOOD, factsCommand: "/opt/facts\u0000x" }],
    ["factsCommand with a tab", { ...GOOD, factsCommand: "/opt/fa\tcts" }],
    ["weight key with spaces", { ...GOOD, weights: { " env-desk": "prefer" } }],
    [
      "weight keys that trim to one",
      { ...GOOD, weights: { "env-desk": "less", "env-desk ": "prefer" } },
    ],
    ["bad weight word", { ...GOOD, weights: { "env-desk": "high" } }],
    ["one-label never group", { ...GOOD, never: [["ci-runner"]] }],
    ["never group that is one label twice", { ...GOOD, never: [["gpu", " gpu"]] }],
    ["never group that is one label in two cases", { ...GOOD, never: [["GPU", "gpu"]] }],
    ["blank label", { ...GOOD, labels: ["gpu", "  "] }],
    ["label with a space", { ...GOOD, labels: ["two words"] }],
    ["too long a label", { ...GOOD, labels: ["x".repeat(65)] }],
    ["too many labels", { ...GOOD, labels: Array.from({ length: 65 }, (_, i) => `l${i}`) }],
    ["unknown field", { ...GOOD, label: ["gpu"] }],
    ["wrong type: placement", "enforce"],
    ["wrong type: placement array", [GOOD]],
    ["wrong type: placement null", null],
    ["wrong type: labels", { ...GOOD, labels: "gpu" }],
    ["wrong type: never", { ...GOOD, never: ["ci-runner", "owner-logins"] }],
    ["wrong type: weights", { ...GOOD, weights: ["env-desk"] }],
    ["wrong type: factsCommand", { ...GOOD, factsCommand: ["/bin/facts"] }],
    ["wrong type: mode", { ...GOOD, mode: 1 }],
  ];

  it.each(BAD)("reads a %s as off, kept as written with its problem", (_name, bad) => {
    const settings = decodeSettings({ ...NEIGHBOURS, placement: bad });
    const { unreadable, problem, ...rules } = settings.placement;
    expect(rules).toEqual(OFF);
    expect(unreadable).toEqual(bad);
    // The reason, on one line, for Settings to show.
    expect(problem).toMatch(/^Expected \S/u);
    expect(problem).not.toContain("\n");
    // The rest of the file still loads.
    expect(settings.agentSlots.enabled).toBe(true);
    expect(settings.agentSlots.maxPerMachine).toBe(3);
    expect(settings.waitFor.enabled).toBe(true);
    expect(settings.usagePacingGate).toBe("advise");
    expect(settings.defaultAutoPull).toBe(false);
    // A save of another setting writes the person's text back unchanged...
    expect(encodeSettings(settings).placement).toEqual(bad);
    // ...and reading it again gives the same value (runs again).
    expect(reread(settings)).toEqual(settings);
    expect(reread(reread(settings))).toEqual(settings);
  });

  it("names the field in the problem", () => {
    const problem = (placement: unknown) => decodeSettings({ placement }).placement.problem;
    expect(problem({ mode: "on" })).toBe('Expected "off" | "advise" | "enforce" at ["mode"]');
    expect(problem({ factsCommand: "bin/facts" })).toBe(
      'Expected an empty factsCommand or an absolute path at ["factsCommand"]',
    );
    expect(problem({ weights: { "env-desk": "high" } })).toContain('at ["weights"]["env-desk"]');
    expect(problem({ never: [["gpu"]] })).toContain('at ["never"][0]');
    expect(problem({ label: [] })).toBe('Expected no excess property at ["label"]');
  });

  it("stores labels trimmed, lower-cased and without repeats, and reads them again the same", () => {
    const settings = decodeSettings({
      placement: {
        labels: ["GPU", " gpu ", "Ci-Runner", "ci-runner", "CUDA"],
        never: [[" CI-runner", "Owner-Logins", "owner-logins "]],
      },
      placementNeeds: [" GPU", "gpu", "Cuda "],
    });
    expect(settings.placement.labels).toEqual(["gpu", "ci-runner", "cuda"]);
    expect(settings.placement.never).toEqual([["ci-runner", "owner-logins"]]);
    expect(settings.placementNeeds).toEqual(["gpu", "cuda"]);
    // The file then holds the stored form, and reading it again changes nothing (runs again).
    expect(encodeSettings(settings).placement).toMatchObject({
      labels: ["gpu", "ci-runner", "cuda"],
    });
    expect(encodeSettings(settings).placementNeeds).toEqual(["gpu", "cuda"]);
    expect(reread(settings)).toEqual(settings);
    // A save is stored in that form too.
    expect(decodePatch({ placement: { labels: ["GPU"] } }).placement?.labels).toEqual(["gpu"]);
    expect(decodePatch({ placementNeeds: ["Gpu", "GPU"] }).placementNeeds).toEqual(["gpu"]);
    // ["GPU", "gpu"] is one label: not a never group, neither read nor saved.
    expect(decodeSettings({ placement: { never: [["GPU", "gpu"]] } }).placement.problem).toBe(
      'Expected a never group of at least two different labels at ["never"][0]',
    );
    expect(() => decodePatch({ placement: { never: [["GPU", "gpu"]] } })).toThrow();
    // The one form P5 compares in (its canonicalLabels): the same function, for later merging.
    expect(canonicalPlacementLabels([" GPU", "gpu", "", "Cuda"])).toEqual(["gpu", "cuda"]);
  });

  it("reads a good value again the same, and writes it the same", () => {
    const once = decodeSettings({ ...NEIGHBOURS, placement: GOOD, placementNeeds: ["gpu"] });
    const twice = reread(once);
    expect(twice).toEqual(once);
    expect(encodeSettings(twice).placement).toEqual(encodeSettings(once).placement);
    expect(encodeSettings(twice).placementNeeds).toEqual(["gpu"]);
  });

  it("refuses a bad save and an unknown field, never reads it as off", () => {
    const patch = (placement: unknown) => decodePatch({ placement });
    expect(patch({ mode: "advise" }).placement).toEqual({ mode: "advise" });
    expect(patch({ labels: [] }).placement).toEqual({ labels: [] });
    for (const [name, bad] of [
      ["bad mode", { mode: "on" }],
      ["relative factsCommand", { factsCommand: "bin/facts" }],
      ["bad weight word", { weights: { "env-desk": "high" } }],
      ["one-label never group", { never: [["ci-runner"]] }],
      ["unknown field", { mode: "advise", label: ["gpu"] }],
      ["kept text", { unreadable: "x" }],
      ["wrong type", "enforce"],
    ] as const) {
      expect(() => patch(bad), name).toThrow();
    }
    // The patch is sent as given; a client's save goes over the wire unchanged.
    const sent: PlacementSettingsPatch = { mode: "advise", labels: ["gpu"] };
    expect(encodePlacementPatch(sent)).toEqual(sent);
  });
});

describe("placementNeeds", () => {
  it("defaults to none on the server and is not set in a project", () => {
    expect(decodeSettings({}).placementNeeds).toEqual([]);
    expect(Object.hasOwn(decodeOverrides({}), "placementNeeds")).toBe(false);
    const settings = decodeSettings({
      projectSettingsOverrides: { [PROJECT]: { defaultAutoPull: false } },
    });
    expect(Object.hasOwn(settings.projectSettingsOverrides[PROJECT] ?? {}, "placementNeeds")).toBe(
      false,
    );
  });

  it("reads a project's needs, trimmed and without repeats, and again the same", () => {
    const settings = decodeSettings({
      placementNeeds: ["ci-runner"],
      projectSettingsOverrides: { [PROJECT]: { placementNeeds: [" gpu", "gpu", "cuda"] } },
    });
    expect(settings.projectSettingsOverrides[PROJECT]?.placementNeeds).toEqual(["gpu", "cuda"]);
    expect(settings.placementNeeds).toEqual(["ci-runner"]);
    expect(reread(settings)).toEqual(settings);
    // `[]` is the project's own value: it needs nothing, whatever the server's says.
    expect(decodeOverrides({ placementNeeds: [] }).placementNeeds).toEqual([]);
  });

  const BAD_NEEDS: ReadonlyArray<unknown> = [
    "gpu",
    [""],
    ["two words"],
    ["gpu", "cuda 12"],
    [1],
    { gpu: true },
    Array.from({ length: 65 }, (_, i) => `l${i}`),
    // Text shaped like a kept value is still the person's text, never read as needs.
    { unreadable: ["gpu"], problem: "x" },
  ];

  it.each(BAD_NEEDS)(
    "reads bad needs %j as fitting no machine, never as none or the server's, kept as written",
    (bad) => {
      const settings = decodeSettings({
        ...NEIGHBOURS,
        placementNeeds: bad,
        projectSettingsOverrides: { [PROJECT]: { placementNeeds: bad, defaultAutoPull: true } },
      });
      const server = settings.placementNeeds;
      const project = settings.projectSettingsOverrides[PROJECT]?.placementNeeds;
      for (const needs of [server, project]) {
        // Never `[]`: that is "needs nothing", which every machine fits.
        expect(needs).not.toEqual([]);
        expect(needs).toMatchObject({
          unreadable: bad,
          problem: expect.stringMatching(/^Expected \S/u),
        });
        const read = readPlacementNeeds(needs!);
        expect(read.fits).toBe("none");
        expect(read).toMatchObject({
          reason: expect.stringMatching(
            /^this project's placementNeeds setting is not valid: Expected .+; fix it in Settings$/u,
          ),
        });
      }
      expect(settings.projectSettingsOverrides[PROJECT]?.defaultAutoPull).toBe(true);
      expect(settings.agentSlots.maxPerMachine).toBe(3);
      // A save of another setting writes the person's text back unchanged, and it reads again the
      // same (no growth, no loop).
      expect(encodeSettings(settings).placementNeeds).toEqual(bad);
      expect(
        (
          encodeSettings(settings).projectSettingsOverrides as Record<
            string,
            { placementNeeds: unknown }
          >
        )[PROJECT]?.placementNeeds,
      ).toEqual(bad);
      expect(reread(settings)).toEqual(settings);
      expect(reread(reread(settings))).toEqual(settings);
    },
  );

  it("reads the server's null needs as fitting no machine (a project's null is not set)", () => {
    expect(readPlacementNeeds(decodeSettings({ placementNeeds: null }).placementNeeds).fits).toBe(
      "none",
    );
    expect(decodeOverrides({ placementNeeds: null })).toEqual({});
  });

  it("reads good needs as the labels a machine must have", () => {
    expect(readPlacementNeeds(decodeSettings({ placementNeeds: ["GPU"] }).placementNeeds)).toEqual({
      fits: "labels",
      labels: ["gpu"],
    });
    expect(readPlacementNeeds(decodeSettings({}).placementNeeds)).toEqual({
      fits: "labels",
      labels: [],
    });
  });

  it("takes a project's kept bad needs back in a save only as text that does not read as needs", () => {
    const stored = decodeOverrides({ placementNeeds: "gpu" }).placementNeeds;
    // A client resends the project's whole entry, the kept value as it holds it.
    const resent = decodePatch(
      JSON.parse(
        JSON.stringify({ projectSettingsOverrides: { [PROJECT]: { placementNeeds: stored } } }),
      ),
    ).projectSettingsOverrides?.[PROJECT]?.placementNeeds;
    expect(resent).toEqual(stored);
    expect(isKeptPlacementNeedsUnchanged(stored, resent)).toBe(true);
    // Another kept text decodes, but the server refuses it (not the stored one).
    const other = decodePatch({
      projectSettingsOverrides: { [PROJECT]: { placementNeeds: { unreadable: "cuda" } } },
    }).projectSettingsOverrides?.[PROJECT]?.placementNeeds;
    expect(isKeptPlacementNeedsUnchanged(stored, other)).toBe(false);
    expect(isKeptPlacementNeedsUnchanged(undefined, other)).toBe(false);
    expect(isKeptPlacementNeedsUnchanged(stored, ["gpu"])).toBe(true);
    // Text that would read as needs is no kept value; nor is one with another field.
    for (const placementNeeds of [{ unreadable: ["gpu"] }, { unreadable: "gpu", labels: [] }]) {
      expect(() =>
        decodePatch({ projectSettingsOverrides: { [PROJECT]: { placementNeeds } } }),
      ).toThrow();
    }
    // The server's own key takes labels only: a save of it replaces the kept text.
    expect(() => decodePatch({ placementNeeds: { unreadable: "gpu" } })).toThrow();
  });

  it("reads a project's hand-written null as not set, and a save's null as the clear", () => {
    expect(decodeOverrides({ placementNeeds: null })).toEqual({});
    const clear = { projectSettingsOverrides: { [PROJECT]: { placementNeeds: null } } };
    expect(decodePatch(clear).projectSettingsOverrides?.[PROJECT]).toEqual({
      placementNeeds: null,
    });
    expect(() =>
      decodePatch({ projectSettingsOverrides: { [PROJECT]: { placementNeeds: ["a b"] } } }),
    ).toThrow();
    expect(() => decodePatch({ placementNeeds: [""] })).toThrow();
  });
});

describe("factsCommand on the server's OS", () => {
  // [factsCommand, valid on Linux, on macOS, on Windows]
  const ROWS: ReadonlyArray<readonly [string, boolean, boolean, boolean]> = [
    ["", true, true, true],
    ["/usr/local/bin/placement-facts", true, true, false],
    ["/usr/bin/x", true, true, false],
    ["C:\\facts.exe", false, false, true],
    ["C:/tools/facts.exe", false, false, true],
    ["\\\\server\\share\\facts.exe", false, false, true],
  ];

  it.each(ROWS)("%j: Linux %s, macOS %s, Windows %s", (command, linux, darwin, win32) => {
    for (const [platform, valid] of [
      ["linux", linux],
      ["darwin", darwin],
      ["win32", win32],
    ] as const) {
      const problem = placementFactsCommandProblem(command, platform);
      expect(problem === undefined, platform).toBe(valid);
      // The settings file's value as this machine reads it: off and kept, with the problem.
      const stored = decodeSettings({
        ...NEIGHBOURS,
        placement: { ...GOOD, factsCommand: command },
      });
      const read = placementOnPlatform(stored.placement, platform);
      if (valid) {
        expect(read).toBe(stored.placement);
        continue;
      }
      const { unreadable, problem: recorded, ...rules } = read;
      expect(rules, platform).toEqual(OFF);
      expect(recorded).toBe(problem);
      expect(recorded).toContain("is not a full path on this server's OS");
      // Written back unchanged by a save of another setting; read and checked again, the same.
      const settings = { ...stored, placement: read };
      expect(encodeSettings(settings).placement).toEqual({ ...GOOD, factsCommand: command });
      expect(unreadable).toEqual({ ...GOOD, factsCommand: command });
      expect(placementOnPlatform(read, platform)).toBe(read);
      expect(placementOnPlatform(reread(settings).placement, platform)).toEqual(read);
    }
  });
});
