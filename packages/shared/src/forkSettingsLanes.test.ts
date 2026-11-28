/// <reference types="vite-plus/client" />

// Fork (issue 1156): a save's `lanes` patch changes the switch and each lane's definition on its
// own, so two saves for different lanes keep both, and one lane is removed with null.
import {
  DEFAULT_LANE_DEFINITIONS,
  DEFAULT_SERVER_SETTINGS,
  ServerSettings,
  ServerSettingsPatch,
  type ServerSettings as ServerSettingsType,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { applyServerSettingsPatch } from "./serverSettings.ts";

const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const decodeFile = Schema.decodeUnknownSync(ServerSettings);
const encodeFile = Schema.encodeSync(ServerSettings);
const save = (current: ServerSettingsType, json: unknown): ServerSettingsType =>
  applyServerSettingsPatch(current, decodePatch(json));

const ops = { description: "Machines and CI.", owns: [], driver: "agent" };

describe("lanes patch", () => {
  it("starts off with the built-in lanes", () => {
    expect(DEFAULT_SERVER_SETTINGS.lanes).toEqual({
      enabled: false,
      definitions: DEFAULT_LANE_DEFINITIONS,
    });
  });

  it("switches lanes on without touching the definitions", () => {
    expect(save(DEFAULT_SERVER_SETTINGS, { lanes: { enabled: true } }).lanes).toEqual({
      enabled: true,
      definitions: DEFAULT_LANE_DEFINITIONS,
    });
  });

  it("keeps both of two saves for different lanes, and removes one with null", () => {
    const first = save(DEFAULT_SERVER_SETTINGS, { lanes: { definitions: { ops } } });
    const second = save(first, { lanes: { definitions: { issues: null } } });
    expect(second.lanes).toEqual({
      enabled: false,
      definitions: { "pr-queue": DEFAULT_LANE_DEFINITIONS["pr-queue"], ops },
    });
    // What the save wrote reads back the same.
    expect(decodeFile(encodeFile(second)).lanes).toEqual(second.lanes);
  });

  it("refuses a save with a lane name that is not one", () => {
    expect(() =>
      save(DEFAULT_SERVER_SETTINGS, { lanes: { definitions: { "PR Queue": ops } } }),
    ).toThrow();
  });
});
