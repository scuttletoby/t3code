// Fork: a project's handoff and playtest values replace the environment's whole (the server reads
// them so), so a field the person removes on a project screen must not come back from the
// environment's value. Saved through the real writer and the real settings patch.
import {
  EnvironmentId,
  ProjectId,
  ServerSettings,
  type ServerSettingsPatch,
} from "@t3tools/contracts";
import { applyServerSettingsPatch } from "@t3tools/shared/serverSettings";
import * as Schema from "effect/Schema";
import { describe, expect, it, vi } from "vite-plus/test";

import { reviewRulesSummary } from "../reviewRulesSettings.logic";
import {
  applyPendingScopedWrites,
  createScopedSettingsWriter,
  planScopedSettingsPatch,
  resolveScopedSettingsTargets,
} from "../scopedSettings";
import { playtestMissingSteps } from "./playtestSettings.logic";

const environmentId = EnvironmentId.make("laptop");
const projectId = ProjectId.make("game");
const scope = {
  kind: "project" as const,
  group: {} as never,
  environmentId: null,
  label: "game",
  members: [{ id: projectId, environmentId } as never],
  environmentIds: [environmentId],
};
const fromFile = Schema.decodeUnknownSync(ServerSettings);
const step = (name: string) => ({ argv: ["/usr/local/bin/game", name] });
const STEPS = {
  claim: step("claim"),
  renew: step("renew"),
  push: [{ name: "deploy", ...step("deploy") }],
  release: step("release"),
};

/** One environment that stores what it is sent, and the writer the settings page saves through. */
function host(file: object) {
  let stored = fromFile(file);
  const persistServer = vi.fn(async ({ input }: { input: { patch: ServerSettingsPatch } }) => {
    stored = applyServerSettingsPatch(stored, input.patch);
    return { _tag: "Success" as const, value: stored };
  });
  const write = createScopedSettingsWriter(() => stored);
  const save = (patch: ServerSettingsPatch) =>
    write(
      [
        {
          environmentId,
          label: "Laptop",
          connection: { phase: "connected" as const },
          serverConfig: { settings: stored },
        },
      ],
      (current) => planScopedSettingsPatch(scope, current, patch),
      persistServer,
      vi.fn(),
    );
  return { save, project: () => stored.projectSettingsOverrides[projectId] };
}

describe("a project's handoff and playtest overrides", () => {
  it("keeps a playtest step the person removed removed", async () => {
    const machine = host({
      playtest: STEPS,
      projectSettingsOverrides: { [projectId]: { playtest: STEPS } },
    });
    const { release: _removed, ...withoutRelease } = machine.project()!.playtest!;

    await machine.save({ playtest: withoutRelease });

    expect(machine.project()?.playtest).toEqual(withoutRelease);
    expect(machine.project()?.playtest?.release).toBeUndefined();
  });

  it("keeps an emptied handoff command empty", async () => {
    const handoff = { mode: "handoff", source: "agent", command: ["/usr/local/bin/write"] };
    const machine = host({ handoff, projectSettingsOverrides: { [projectId]: { handoff } } });
    const { command: _removed, ...withoutCommand } = machine.project()!.handoff!;

    await machine.save({ handoff: withoutCommand });

    expect(machine.project()?.handoff?.command).toBeUndefined();
    expect(machine.project()?.handoff?.mode).toBe("handoff");
  });

  it("gives a project with no override of its own a copy of what the screen shows", async () => {
    const machine = host({ playtest: STEPS });
    const shown = fromFile({ playtest: STEPS }).playtest;

    await machine.save({ playtest: { ...shown, enabled: true } });

    expect(machine.project()?.playtest).toEqual({ ...shown, enabled: true });
  });
});

describe("a project's null for playtest, handoff or reviewRules on the project screen", () => {
  // The environment's values, each away from its default, so "the environment's" differs from off.
  const SERVER = {
    playtest: { enabled: true, ...STEPS },
    handoff: { mode: "handoff" },
    reviewRules: { preset: "recommended" },
  };
  const OWN = {
    playtest: { enabled: false, ...STEPS },
    handoff: { mode: "compact" },
    reviewRules: { preset: "off" },
  };
  const KEYS = ["playtest", "handoff", "reviewRules"] as const;
  const environment = (settings: ServerSettings) => ({
    environmentId,
    label: "Laptop",
    connection: { phase: "connected" as const },
    serverConfig: { settings },
  });
  /** What the project's panels read, and that each renders it (the calls they make on it). */
  const shown = (settings: ServerSettings, key: (typeof KEYS)[number]) => {
    const [target] = resolveScopedSettingsTargets(scope as never, [environment(settings)]);
    expect(target, key).toBeDefined();
    expect(target!.settings[key], key).toEqual(settings[key]);
    expect(target!.sources[key], key).toBe("environment");
    expect(playtestMissingSteps(target!.settings.playtest)).toEqual([]);
    expect(target!.settings.handoff.mode).toBe("handoff");
    expect(reviewRulesSummary(target!.settings.reviewRules)).toBe("On: recommended preset");
  };

  it.each(KEYS)("an old file's hand-written %s: null shows the environment's", (key) => {
    shown(fromFile({ ...SERVER, projectSettingsOverrides: { [projectId]: { [key]: null } } }), key);
  });

  it.each(KEYS)(
    "a pending clear of %s shows the environment's before the server answers",
    (key) => {
      const stored = fromFile({
        ...SERVER,
        projectSettingsOverrides: { [projectId]: { [key]: OWN[key], defaultAutoPull: false } },
      });
      const [pending] = applyPendingScopedWrites(
        [environment(stored)],
        [
          {
            save: {},
            environmentId,
            patch: {
              projectSettingsOverrides: { [projectId]: { [key]: null, defaultAutoPull: false } },
            } as ServerSettingsPatch,
          },
        ],
      );
      shown(pending!.serverConfig.settings, key);
    },
  );
});
