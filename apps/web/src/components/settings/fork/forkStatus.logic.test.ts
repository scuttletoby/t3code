import { FORK_FEATURES, ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { type ForkStatusTarget, forkFeatureStatuses } from "./forkStatus.logic";

const fromFile = Schema.decodeUnknownSync(ServerSettings);
const server = (file: unknown): ForkStatusTarget => ({ projectId: null, settings: fromFile(file) });
const lines = (...targets: ReadonlyArray<ForkStatusTarget>) =>
  forkFeatureStatuses(targets).map((status) => `${status.label}: ${status.detail}`);

describe("forkFeatureStatuses", () => {
  it("lists every feature the fork report counts, each off from an empty settings file", () => {
    expect(
      forkFeatureStatuses([server({})])
        .map((status) => status.key)
        .toSorted(),
    ).toEqual([...FORK_FEATURES].toSorted());
    expect(lines(server({}))).toEqual([
      "Plan usage pacing: Off",
      "Account order: Off",
      "Limit on running agents: Off",
      "Stop finished sub-agents: Off",
      "Agent waits: Off",
      "Armed pull request sweep: Off",
      "Models agents may use: Off",
      "Handoff instead of compaction: Off",
      "Review rules: Off",
      "Playtest sessions: Off",
      "Automatic updates: Off",
      "Busy check (settings file only): Off",
    ]);
  });

  it("reads each switch as the server does", () => {
    const statuses = lines(
      server({
        waitFor: { enabled: true },
        agentSlots: { enabled: true },
        usagePacingGate: "advise",
        accountOrder: ["claude_second"],
        modelPolicy: { mode: "enforce", allow: [] },
        reviewRules: { preset: "recommended" },
        automaticUpdates: true,
        activityProbe: true,
        stopFinishedChildren: { enabled: true },
        armedSweep: { mode: "dry" },
        handoff: { mode: "compact", threads: { "thread-1": "handoff", "thread-2": "compact" } },
      }),
    );
    expect(statuses).toEqual([
      "Plan usage pacing: On: advise",
      "Account order: On",
      "Limit on running agents: On",
      "Stop finished sub-agents: On",
      "Agent waits: On",
      "Armed pull request sweep: On: dry (lists, wakes no thread)",
      "Models agents may use: On: enforce",
      "Handoff instead of compaction: On for 1 thread",
      "Review rules: On",
      "Playtest sessions: Off",
      "Automatic updates: On",
      "Busy check (settings file only): On",
    ]);
  });

  it("counts a project's override as the fork report does, and a project reads its own value", () => {
    const settings = fromFile({
      projectSettingsOverrides: {
        "project-1": { handoff: { mode: "handoff" }, reviewRules: { preset: "recommended" } },
        "project-2": { handoff: { mode: "compact", threads: { "thread-9": "handoff" } } },
      },
    });
    expect(forkFeatureStatuses([{ projectId: null, settings }])).toContainEqual({
      key: "handoff",
      label: "Handoff instead of compaction",
      on: true,
      detail: "On for 1 project, 1 thread",
    });
    expect(lines({ projectId: null, settings })).toContain("Review rules: On for 1 project");
    // A project screen reads that project's effective value only.
    const project = fromFile({ handoff: { mode: "compact" } });
    expect(
      lines({
        projectId: "project-3",
        settings: { ...project, projectSettingsOverrides: settings.projectSettingsOverrides },
      }),
    ).toContain("Handoff instead of compaction: Off");
  });

  it("shows Mixed when the selected servers read differently", () => {
    expect(lines(server({ waitFor: { enabled: true } }), server({}))).toContain(
      "Agent waits: Mixed: on for 1 of 2",
    );
    expect(lines(server({}), server({}))).toContain("Agent waits: Off");
  });

  it("reads an unreadable hand-edited value as the load does, never half on", () => {
    expect(lines(server({ reviewRules: { preset: "newer-preset" } }))).toContain(
      "Review rules: Off",
    );
    expect(lines(server({ handoff: { mode: 7 } }))).toContain("Handoff instead of compaction: Off");
    expect(lines(server({ modelPolicy: { mode: "enforce", typo: 1 } }))).toContain(
      "Models agents may use: On: enforce (unreadable)",
    );
  });
});
