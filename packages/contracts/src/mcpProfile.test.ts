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

  it("keeps every other setting when an MCP key holds a bad value", () => {
    const settings = decodeFile({
      enableAgentBrowserAccess: false,
      waitFor: { enabled: true },
      mcpProfile: 5,
      mcpSubagentProfile: ["lean"],
      mcpThreadProfiles: { t1: null, t2: 7, t3: " lean ", t4: { profile: "lean" } },
      projectSettingsOverrides: {
        [PROJECT]: { mcpProfile: 5, mcpSubagentProfile: null, waitChecks: {} },
      },
    });
    expect(settings.enableAgentBrowserAccess).toBe(false);
    expect(settings.waitFor.enabled).toBe(true);
    expect(settings.projectSettingsOverrides[PROJECT]?.waitChecks).toEqual({});
    // A bad value is kept as a marker no profile has, so a thread that uses it is refused (never
    // widened to `all`); `null` means "not set", as an empty value does.
    expect(mcpKeys(settings)).toEqual({
      mcpProfiles: {},
      mcpProfile: "(unreadable number)",
      mcpSubagentProfile: "(unreadable list)",
      mcpThreadProfiles: { t2: "(unreadable number)", t3: "lean", t4: "(unreadable object)" },
    });
    expect(settings.projectSettingsOverrides[PROJECT]).toMatchObject({
      mcpProfile: "(unreadable number)",
      mcpSubagentProfile: "",
    });
  });

  it("reads a map that is not an object as empty and keeps every other setting", () => {
    const settings = decodeFile({
      enableAgentBrowserAccess: false,
      mcpProfiles: "lean",
      mcpThreadProfiles: ["t1"],
    });
    expect(settings.enableAgentBrowserAccess).toBe(false);
    expect(settings.mcpProfiles).toEqual({});
    expect(settings.mcpThreadProfiles).toEqual({});
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

  it("takes a project's reference as reading keeps it, so other project saves still work", () => {
    // Clients resend a project's whole entry; the server refuses only a changed reference.
    for (const mcpProfile of ["", "Lean", "(unreadable number)", "inherit"]) {
      expect(
        decodePatch({ projectSettingsOverrides: { [PROJECT]: { mcpProfile } } })
          .projectSettingsOverrides?.[PROJECT]?.mcpProfile,
      ).toBe(mcpProfile);
    }
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
    ["`inherit` as the default profile", { mcpProfile: "inherit" }],
    ["`inherit` as a thread pick", { mcpThreadProfiles: { "thread-1": "inherit" } }],
    ["a profile that is not an object", { mcpProfiles: { lean: "all" } }],
    ["a server that is not text", { mcpProfiles: { lean: { servers: [1], t3Tools: [] } } }],
  ])("refuses %s", (_name, patch) => {
    expect(() => decodePatch(patch)).toThrow();
  });
});
