import { DEFAULT_SERVER_SETTINGS, ProjectId, type ServerSettings } from "@t3tools/contracts";
import { describe, expect, it } from "@effect/vitest";

import { readMcpProfileKeysFromText, withMcpProfileFileKeys } from "./mcpProfileSettingsText.ts";

const asIs = (persisted: unknown) => persisted;
const read = (raw: string, withDefaults: (persisted: unknown) => unknown = asIs) =>
  readMcpProfileKeysFromText(raw, withDefaults);
const keysOf = (raw: string) => {
  const result = read(raw);
  if (!result.ok) throw new Error(`unreadable: ${result.issue}`);
  return result.keys;
};

describe("readMcpProfileKeysFromText (#681)", () => {
  it("text that names no MCP profile key sets none, however broken it is", () => {
    // An empty file, one cut off half-way through a write, a list: none can hold a profile.
    for (const raw of ["", "{ not json", '{ "theme": "dark", ', "[1, 2]", "null", "5"]) {
      expect(read(raw)).toEqual({ ok: true, keys: undefined });
    }
  });

  it("refuses text that names a profile key but cannot be read, never reading it as all", () => {
    const halfWritten = '{ "mcpProfile": "t3-only", "theme": ';
    expect(read(halfWritten)).toEqual({ ok: false, issue: "it is not valid JSON" });
    expect(read('["mcpProfile"]')).toEqual({ ok: false, issue: "it holds a list, not an object" });
    expect(read('"mcpThreadProfiles"')).toEqual({
      ok: false,
      issue: "it holds string, not an object",
    });
    expect(read('{ "mcpProfile": "docs", "projectSettingsOverrides": 5 }')).toEqual({
      ok: false,
      issue: "its projectSettingsOverrides holds number, not a map",
    });
    // A key spelled with an escape counts as named.
    expect(read('{ "\\u006dcpProfile": "t3-only", ')).toEqual({
      ok: false,
      issue: "it is not valid JSON",
    });
  });

  it("reads the profile keys of a file another setting breaks, as a full decode reads them", () => {
    const keys = keysOf(
      JSON.stringify({
        // A setting a full decode refuses; the profile keys do not depend on it.
        providers: 5,
        mcpProfile: "docs",
        mcpSubagentProfile: "t3-only",
        mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } },
        mcpThreadProfiles: { "thread:a": "t3-only" },
        projectSettingsOverrides: {
          "project:a": { mcpProfile: "t3-only", defaultModelSelection: 7 },
          "project:b": { autoPull: true },
        },
      }),
    );
    expect(keys).toMatchObject({
      mcpProfile: "docs",
      mcpSubagentProfile: "t3-only",
      mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } },
      mcpThreadProfiles: { "thread:a": "t3-only" },
    });
    expect(keys?.projectSettingsOverrides["project:a"]?.mcpProfile).toBe("t3-only");
    expect(keys?.projectSettingsOverrides["project:b"]?.mcpProfile).toBeUndefined();
    // JSON with comments and a trailing comma reads as the load reads it.
    expect(keysOf('{ // owner\n "mcpProfile": "t3-only", }')?.mcpProfile).toBe("t3-only");
    expect(keysOf('{ "\\u006dcpProfile": "t3-only" }')?.mcpProfile).toBe("t3-only");
  });

  it("a project entry or value it cannot read becomes a marker no profile has", () => {
    const keys = keysOf(
      JSON.stringify({
        mcpProfile: "all",
        projectSettingsOverrides: { "project:a": 5, "project:b": { mcpProfile: 9 } },
      }),
    );
    expect(keys?.projectSettingsOverrides["project:a"]).toEqual({
      mcpProfile: "(unreadable number)",
      mcpSubagentProfile: "(unreadable number)",
    });
    expect(keys?.projectSettingsOverrides["project:b"]?.mcpProfile).toMatch(/^\(unreadable/);
    // No projects, or null: none.
    expect(keysOf('{ "mcpProfile": "all", "projectSettingsOverrides": null }')).toMatchObject({
      projectSettingsOverrides: {},
    });
  });

  it("fills in the managed policy's defaults as a load does", () => {
    const withDefaults = (persisted: unknown) => ({
      mcpProfile: "t3-only",
      ...(persisted as object),
    });
    expect(read('{ "mcpThreadProfiles": {} }', withDefaults)).toMatchObject({
      ok: true,
      keys: { mcpProfile: "t3-only" },
    });
  });
});

describe("withMcpProfileFileKeys (#681)", () => {
  it("lays the file's profile keys over the service's, keeping each project's other overrides", () => {
    const base = {
      ...DEFAULT_SERVER_SETTINGS,
      mcpProfile: "all",
      projectSettingsOverrides: {
        [ProjectId.make("project:a")]: { mcpProfile: "all", autoPull: true },
        [ProjectId.make("project:gone")]: { mcpProfile: "all" },
      },
    } as unknown as ServerSettings;
    const keys = keysOf(
      JSON.stringify({
        mcpProfile: "t3-only",
        projectSettingsOverrides: { "project:a": { mcpProfile: "t3-only" } },
      }),
    )!;
    const laid = withMcpProfileFileKeys(base, keys);
    expect(laid.mcpProfile).toBe("t3-only");
    expect(laid.projectSettingsOverrides[ProjectId.make("project:a")]).toEqual({
      autoPull: true,
      mcpProfile: "t3-only",
    });
    // A project the file does not list has no profile of its own, as the file says.
    expect(
      laid.projectSettingsOverrides[ProjectId.make("project:gone")]?.mcpProfile,
    ).toBeUndefined();
    // Pure: the service's settings are left as they were.
    expect(base.mcpProfile).toBe("all");
  });
});
