import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { ProjectId } from "./baseSchemas.ts";
import { ProjectSettingsOverrides, ServerSettings, ServerSettingsPatch } from "./settings.ts";

const decodeFile = Schema.decodeUnknownSync(ServerSettings);
const encodeFile = Schema.encodeSync(ServerSettings);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const PROJECT = ProjectId.make("project-1");

const mcpKeys = (settings: ServerSettings) => ({
  mcpProfiles: settings.mcpProfiles,
  mcpProfile: settings.mcpProfile,
  mcpSubagentProfile: settings.mcpSubagentProfile,
  mcpThreadProfiles: settings.mcpThreadProfiles,
});

describe("MCP profile settings file", () => {
  it("reads an old file (no keys) as off", () => {
    expect(mcpKeys(decodeFile({}))).toEqual({
      mcpProfiles: {},
      mcpProfile: "all",
      mcpSubagentProfile: "inherit",
      mcpThreadProfiles: {},
    });
  });

  it("loads a file whose profile this build cannot read, and keeps it on save", () => {
    // Written by a newer build: an unknown field, an unknown group, a missing field.
    const profiles = {
      future: { servers: [], t3Tools: ["mail"], builtinTools: "none" },
      half: { servers: ["docs"] },
      odd: "not a profile",
    };
    const settings = decodeFile({ mcpProfiles: profiles, mcpProfile: "future" });
    expect(settings.mcpProfiles).toEqual(profiles);
    // A save writes what was read: a newer build's profile is not cut or widened.
    expect(encodeFile(settings).mcpProfiles).toEqual(profiles);
  });

  it("trims profile references", () => {
    expect(decodeFile({ mcpProfile: " lean ", mcpSubagentProfile: " " })).toMatchObject({
      mcpProfile: "lean",
      mcpSubagentProfile: "",
    });
  });

  it("reads a project's overrides; absent means the server's", () => {
    const decode = Schema.decodeUnknownSync(ProjectSettingsOverrides);
    expect(decode({})).toEqual({});
    expect(decode({ mcpProfile: "lean", mcpSubagentProfile: "inherit" })).toEqual({
      mcpProfile: "lean",
      mcpSubagentProfile: "inherit",
    });
  });
});

describe("MCP profile settings patch", () => {
  it("takes good profiles, references and picks", () => {
    const patch = decodePatch({
      mcpProfiles: { lean: { servers: ["docs"], t3Tools: ["threads", "waits"] }, gone: null },
      mcpProfile: "lean",
      mcpSubagentProfile: "inherit",
      mcpThreadProfiles: { "thread-1": "t3-only", "thread-2": null },
      projectSettingsOverrides: { [PROJECT]: { mcpProfile: "all", mcpSubagentProfile: "lean" } },
    });
    expect(patch.mcpProfiles?.lean).toEqual({ servers: ["docs"], t3Tools: ["threads", "waits"] });
    expect(patch.projectSettingsOverrides?.[PROJECT]).toEqual({
      mcpProfile: "all",
      mcpSubagentProfile: "lean",
    });
  });

  it.each([
    ["a built-in name", { mcpProfiles: { all: { servers: [], t3Tools: [] } } }],
    ["the name `inherit`", { mcpProfiles: { inherit: { servers: [], t3Tools: [] } } }],
    ["a bad name", { mcpProfiles: { "Bad Name": { servers: [], t3Tools: [] } } }],
    ["an unknown field", { mcpProfiles: { lean: { servers: [], t3Tools: [], extra: 1 } } }],
    ["a missing field", { mcpProfiles: { lean: { servers: [] } } }],
    ["an unknown group", { mcpProfiles: { lean: { servers: [], t3Tools: ["mail"] } } }],
    ["a blank server", { mcpProfiles: { lean: { servers: [" "], t3Tools: [] } } }],
    ["a bad reference", { mcpProfile: "Not A Name" }],
    ["an empty reference", { mcpSubagentProfile: "" }],
    ["a bad pick", { mcpThreadProfiles: { "thread-1": "Not A Name" } }],
    ["a blank thread id", { mcpThreadProfiles: { " ": "lean" } }],
    ["an untrimmed thread id", { mcpThreadProfiles: { " thread-1": "lean" } }],
    ["a bad project reference", { projectSettingsOverrides: { [PROJECT]: { mcpProfile: "" } } }],
  ])("refuses %s", (_name, patch) => {
    expect(() => decodePatch(patch)).toThrow();
  });
});
