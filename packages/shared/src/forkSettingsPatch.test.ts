/// <reference types="vite-plus/client" />

import {
  DEFAULT_SERVER_SETTINGS,
  ProviderInstanceId,
  type ServerSettings,
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
