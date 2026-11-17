// Fork: the interval walk and checks on their own. The settings service's save and load of
// every interval is in apps/server/src/serverSettings.forkIntervals.test.ts.
import { DEFAULT_SERVER_SETTINGS, type ServerSettings } from "@t3tools/contracts";
import * as Duration from "effect/Duration";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  dropUnusableStoredIntervals,
  findDurationPaths,
  SETTINGS_INTERVAL_PATHS,
  settingsIntervalProblem,
} from "./forkSettingsIntervals.ts";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const setPath = (value: unknown, path: ReadonlyArray<string>, leaf: unknown): unknown => {
  const [key, ...rest] = path;
  if (key === undefined) return leaf;
  const node = isRecord(value) ? value : {};
  return { ...node, [key]: setPath(node[key], rest, leaf) };
};

/** `settings` with every interval set to `leaf`. */
const withEveryInterval = (settings: unknown, leaf: unknown): unknown =>
  SETTINGS_INTERVAL_PATHS.reduce((node, path) => setPath(node, path, leaf), settings);

// A recursive schema: a walk without a loop guard never ends, and the server would not start.
interface Tree {
  readonly every: Duration.Duration;
  readonly children: ReadonlyArray<Tree>;
}
interface TreeEncoded {
  readonly every: number;
  readonly children: ReadonlyArray<TreeEncoded>;
}
const Tree: Schema.Codec<Tree, TreeEncoded> = Schema.Struct({
  every: Schema.DurationFromMillis,
  children: Schema.Array(Schema.suspend((): Schema.Codec<Tree, TreeEncoded> => Tree)),
});

describe("findDurationPaths", () => {
  it("follows keys and optional keys, and puts an interval it cannot follow out of reach", () => {
    const { paths, outOfReach } = findDurationPaths(
      Schema.Struct({
        plain: Schema.DurationFromMillis,
        optionalKey: Schema.optionalKey(Schema.DurationFromMillis),
        // `optional` is a union with undefined, which a JSON file cannot hold.
        optional: Schema.optional(Schema.DurationFromMillis),
        nested: Schema.Struct({ inner: Schema.DurationFromMillis }),
        record: Schema.Record(Schema.String, Schema.DurationFromMillis),
        array: Schema.Array(Schema.DurationFromMillis),
        option: Schema.OptionFromNullOr(Schema.DurationFromMillis),
        // A union lets the same key hold a value that is not an interval: `null` or "off" for
        // "never", or another member's field. A load must not drop it, so none has a path.
        nullOr: Schema.optionalKey(Schema.NullOr(Schema.DurationFromMillis)),
        orOff: Schema.Union([Schema.DurationFromMillis, Schema.Literal("off")]),
        variants: Schema.Union([
          Schema.Struct({ every: Schema.DurationFromMillis }),
          Schema.Struct({ every: Schema.String }),
        ]),
        // Both variants hold an interval at the same key: named once.
        bothVariants: Schema.Union([
          Schema.Struct({ every: Schema.DurationFromMillis, kind: Schema.Literal("a") }),
          Schema.Struct({ every: Schema.DurationFromMillis, kind: Schema.Literal("b") }),
        ]),
        recursive: Schema.suspend((): Schema.Codec<Tree, TreeEncoded> => Tree),
      }),
    );

    expect(paths).toEqual([["plain"], ["optionalKey"], ["optional"], ["nested", "inner"]]);
    expect(outOfReach.toSorted()).toEqual(
      [
        "array.*",
        "bothVariants.every",
        "nullOr",
        "option.*",
        "orOff",
        "record.*",
        "recursive.*.children.*.*.every",
        "recursive.*.every",
        "variants.every",
      ].toSorted(),
    );
  });
});

describe("a good interval is never dropped or refused", () => {
  // Ten minutes, a day and the largest: a lowered upper bound would drop a real setting.
  const GOOD_MILLIS = [0, 600_000, 86_400_000, Number.MAX_SAFE_INTEGER];

  it.each(GOOD_MILLIS)("loads a file with every interval at %d unchanged", (millis) => {
    const file = withEveryInterval({ enableAgentBrowserAccess: false }, millis);

    const { settings, dropped } = dropUnusableStoredIntervals(file);

    expect(dropped).toEqual([]);
    expect(settings).toEqual(file);
  });

  it.each(GOOD_MILLIS)("saves every interval at %d", (millis) => {
    const next = withEveryInterval(DEFAULT_SERVER_SETTINGS, Duration.millis(millis));

    expect(settingsIntervalProblem(DEFAULT_SERVER_SETTINGS, next as ServerSettings)).toBe(
      undefined,
    );
  });

  it("loads a file without any interval, or with no object on the way, unchanged", () => {
    for (const file of [
      {},
      { enableAgentBrowserAccess: false },
      { backgroundActivity: { profile: "custom" } },
      { backgroundActivity: { profile: "custom", overrides: {} } },
      // A parent that is not an object is the decode's to refuse, never a crash here.
      { backgroundActivity: null },
      { backgroundActivity: [] },
      { backgroundActivity: "custom" },
      { backgroundActivity: { profile: "custom", overrides: null } },
      { backgroundActivity: { profile: "custom", overrides: ["hostPowerMonitorIdleInterval"] } },
      { backgroundActivity: { profile: "custom", overrides: "hostPowerMonitorIdleInterval" } },
    ]) {
      expect(dropUnusableStoredIntervals(file)).toEqual({ settings: file, dropped: [] });
    }
  });
});

describe("settingsIntervalProblem", () => {
  // As a managed policy can set it: the save did not choose it.
  const current = setPath(
    DEFAULT_SERVER_SETTINGS,
    ["headlessUsageRefreshInterval"],
    Duration.infinity,
  ) as ServerSettings;

  it("never refuses an interval the save leaves as it was", () => {
    const next = { ...current, enableAgentBrowserAccess: !current.enableAgentBrowserAccess };

    expect(settingsIntervalProblem(current, next)).toBe(undefined);
  });

  it("still refuses a save that changes it to another bad value", () => {
    const next = setPath(current, ["headlessUsageRefreshInterval"], Duration.millis(-5));

    expect(settingsIntervalProblem(current, next as ServerSettings)).toContain(
      "headlessUsageRefreshInterval",
    );
  });
});
