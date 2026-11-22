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
      "Waits: Off",
      "Agent slots: Off",
      "Usage pacing: Off",
      "Model policy: Off",
      "Stop finished children: Off",
      "Review rules: Off",
      "Automatic updates: Off",
      "Activity probe: Off",
      "Handoff: Off",
      "Playtest: Off",
    ]);
  });

  it("reads each switch as the server does", () => {
    const statuses = lines(
      server({
        waitFor: { enabled: true },
        agentSlots: { enabled: true },
        usagePacingGate: "advise",
        modelPolicy: { mode: "enforce", allow: [] },
        reviewRules: { preset: "recommended" },
        automaticUpdates: true,
        activityProbe: true,
        stopFinishedChildren: { enabled: true },
        handoff: { mode: "compact", threads: { "thread-1": "handoff", "thread-2": "compact" } },
      }),
    );
    expect(statuses).toEqual([
      "Waits: On",
      "Agent slots: On",
      "Usage pacing: On: advise",
      "Model policy: On: enforce",
      "Stop finished children: On",
      "Review rules: On",
      "Automatic updates: On",
      "Activity probe: On",
      "Handoff: On for 1 thread",
      "Playtest: Off",
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
      label: "Handoff",
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
    ).toContain("Handoff: Off");
  });

  it("shows Mixed when the selected servers read differently", () => {
    expect(lines(server({ waitFor: { enabled: true } }), server({}))).toContain(
      "Waits: Mixed: on for 1 of 2",
    );
    expect(lines(server({}), server({}))).toContain("Waits: Off");
  });

  it("reads an unreadable hand-edited value as the load does, never half on", () => {
    expect(lines(server({ reviewRules: { preset: "newer-preset" } }))).toContain(
      "Review rules: Off",
    );
    expect(lines(server({ handoff: { mode: 7 } }))).toContain("Handoff: Off");
    expect(lines(server({ modelPolicy: { mode: "enforce", typo: 1 } }))).toContain(
      "Model policy: On: enforce (unreadable)",
    );
  });
});
