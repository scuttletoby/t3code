/// <reference types="vite-plus/client" />

// Fork (issue 1156): a save's `lanes` patch changes the switch and each stored lane on its own, so
// two saves for different lanes keep both. In memory only: the file write, which leaves out what
// equals the default, is tested through the server in apps/server/src/fork/lanes/lanesSettingsFile.test.ts.
import {
  BUILT_IN_LANE_DEFINITIONS,
  DEFAULT_SERVER_SETTINGS,
  laneDefinitions,
  ServerSettingsPatch,
  type ServerSettings,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { applyServerSettingsPatch } from "./serverSettings.ts";

const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const save = (current: ServerSettings, json: unknown): ServerSettings =>
  applyServerSettingsPatch(current, decodePatch(json));

const ops = { description: "Machines and CI.", owns: ["issue"], driver: "agent" };

describe("lanes patch", () => {
  it("starts off, with nothing stored and the built-in lanes defined", () => {
    expect(DEFAULT_SERVER_SETTINGS.lanes).toEqual({ enabled: false, definitions: {} });
    expect(laneDefinitions(DEFAULT_SERVER_SETTINGS.lanes)).toEqual(BUILT_IN_LANE_DEFINITIONS);
  });

  it("switches lanes on without touching the definitions", () => {
    expect(save(DEFAULT_SERVER_SETTINGS, { lanes: { enabled: true } }).lanes).toEqual({
      enabled: true,
      definitions: {},
    });
  });

  it("keeps both of two saves for different lanes; false removes a built-in, null gives it back", () => {
    const first = save(DEFAULT_SERVER_SETTINGS, { lanes: { definitions: { ops } } });
    const second = save(first, { lanes: { definitions: { issues: false } } });
    expect(second.lanes.definitions).toEqual({ ops, issues: false });
    expect(Object.keys(laneDefinitions(second.lanes))).toEqual(["pr-queue", "ops"]);
    const back = save(second, { lanes: { definitions: { issues: null } } });
    expect(Object.keys(laneDefinitions(back.lanes))).toEqual(["pr-queue", "issues", "ops"]);
  });

  it("refuses a save with a lane name that is not one", () => {
    expect(() =>
      save(DEFAULT_SERVER_SETTINGS, { lanes: { definitions: { "PR Queue": ops } } }),
    ).toThrow();
  });
});
