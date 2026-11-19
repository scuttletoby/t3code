import { ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { forkFeatureStatuses } from "./forkStatus.logic";

const fromFile = Schema.decodeUnknownSync(ServerSettings);
const lines = (file: unknown) =>
  forkFeatureStatuses(fromFile(file)).map((status) => `${status.label}: ${status.detail}`);

describe("forkFeatureStatuses", () => {
  it("reads every feature as off from an empty settings file", () => {
    expect(lines({})).toEqual([
      "Waits: Off",
      "Agent slots: Off",
      "Usage pacing: Off",
      "Review rules: Off",
      "Automatic updates: Off",
      "Handoff: Off",
      "Playtest: Off",
    ]);
  });

  it("reads each switch as the server does", () => {
    const statuses = lines({
      waitFor: { enabled: true },
      agentSlots: { enabled: true },
      usagePacingGate: "advise",
      reviewRules: { preset: "recommended" },
      automaticUpdates: true,
      handoff: { mode: "compact", threads: { "thread-1": "handoff", "thread-2": "compact" } },
    });
    expect(statuses).toEqual([
      "Waits: On",
      "Agent slots: On",
      "Usage pacing: On: advise",
      "Review rules: On",
      "Automatic updates: On",
      "Handoff: On for 1 thread",
      "Playtest: Off",
    ]);
  });

  it("reads an unreadable hand-edited value as off, never half on", () => {
    expect(lines({ reviewRules: { preset: "newer-preset" }, handoff: { mode: 7 } })).toContain(
      "Review rules: Off",
    );
    expect(lines({ handoff: { mode: 7 } })).toContain("Handoff: Off");
  });
});
