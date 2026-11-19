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

import { createScopedSettingsWriter, planScopedSettingsPatch } from "../scopedSettings";

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
