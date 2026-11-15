// @effect-diagnostics nodeBuiltinImport:off - Lists the server sources and toolkit modules to find every T3 tool.
/**
 * Fork: only a person changes fork settings. No T3 tool's input may name a fork setting
 * (`FORK_PERSON_ONLY_KEYS`: every fork key not in `FORK_AGENT_WRITABLE_KEYS`), so an agent has no
 * tool path to write one. The list is derived from forkSettings.ts, so a new fork setting is
 * covered without a row here.
 */
import * as NodeFS from "node:fs";
import * as NodePath from "node:path";

import { FORK_AGENT_WRITABLE_KEYS, FORK_PERSON_ONLY_KEYS } from "@t3tools/contracts";
import { Tool, Toolkit } from "effect/ai";
import * as Schema from "effect/Schema";
import { beforeAll, describe, expect, it } from "vite-plus/test";

const TOOLKITS_DIR = NodePath.resolve(import.meta.dirname, "../mcp/toolkits");
/** Every toolkit module; each defines its tools with `Tool.make` and exports its toolkits. */
const loadToolkitModules = (): Promise<ReadonlyArray<Record<string, unknown>>> =>
  Promise.all(
    NodeFS.readdirSync(TOOLKITS_DIR, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => NodePath.join(TOOLKITS_DIR, entry.name, "tools.ts"))
      .filter((path) => NodeFS.existsSync(path))
      .map((path) => import(path) as Promise<Record<string, unknown>>),
  );
/** Server sources (relative to src/) that call `Tool.make` outside the toolkit modules. */
const toolsDefinedElsewhere = (): ReadonlyArray<string> => {
  const root = NodePath.resolve(import.meta.dirname, "..");
  return NodeFS.readdirSync(root, { recursive: true, encoding: "utf8" })
    .filter((path) => path.endsWith(".ts") && !path.endsWith(".test.ts"))
    .map((path) => path.split(NodePath.sep).join("/"))
    .filter((path) => !/^mcp\/toolkits\/[^/]+\/tools\.ts$/.test(path))
    .filter((path) =>
      /\bTool\.make\(/.test(NodeFS.readFileSync(NodePath.join(root, path), "utf8")),
    );
};
/** Files that call `Tool.make` but advertise nothing: each with the reason. */
const NOT_ADVERTISED: Readonly<Record<string, string>> = {
  // Describes the linked environment's `orchestrator_capabilities` to call it there.
  "peer/RemoteDelegation.ts": "a client of another environment's tool",
};

// A toolkit: an object whose `tools` are all `Tool`s (Toolkit exports no type guard).
const isToolkit = (value: unknown): value is Toolkit.Toolkit<Record<string, Tool.Any>> => {
  if ((typeof value !== "object" && typeof value !== "function") || value === null) return false;
  if (!("tools" in value)) return false;
  const toolsOf: unknown = value.tools;
  return (
    typeof toolsOf === "object" &&
    toolsOf !== null &&
    Object.values(toolsOf).every(
      (tool: unknown) => typeof tool === "object" && tool !== null && Tool.TypeId in tool,
    )
  );
};

const tools = new Map<string, Tool.Any>();
beforeAll(async () => {
  for (const module of await loadToolkitModules()) {
    for (const value of Object.values(module)) {
      if (!isToolkit(value)) continue;
      for (const tool of Object.values(value.tools)) tools.set(tool.name, tool);
    }
  }
});

/** Lower case without separators, so `mcp_profile` or `MCP-Profile` still count as `mcpProfile`. */
const normalize = (name: string) => name.toLowerCase().replaceAll(/[^a-z0-9]/g, "");
const PERSON_ONLY = new Map(FORK_PERSON_ONLY_KEYS.map((key) => [normalize(key), key]));

/** Every property name anywhere in a JSON schema. */
const propertyNames = (schema: unknown): ReadonlyArray<string> => {
  if (schema === null || typeof schema !== "object") return [];
  const record = schema as Record<string, unknown>;
  const own =
    record.properties !== null && typeof record.properties === "object"
      ? Object.keys(record.properties)
      : [];
  return [...own, ...Object.values(record).flatMap(propertyNames)];
};

/** The person-only settings a tool's input names. */
const personOnlyKeysIn = (tool: Tool.Any): ReadonlyArray<string> =>
  propertyNames(Tool.getJsonSchema(tool)).flatMap((name) => {
    const key = PERSON_ONLY.get(normalize(name));
    return key === undefined ? [] : [key];
  });

describe("person-only fork settings", () => {
  it("lists every fork setting but the agent-writable ones", () => {
    expect(FORK_PERSON_ONLY_KEYS).toEqual(
      expect.arrayContaining([
        "automaticUpdates",
        "waitFor",
        "waitChecks",
        "agentSlots",
        "mcpProfiles",
        "mcpProfile",
        "mcpSubagentProfile",
        "mcpThreadProfiles",
      ]),
    );
    for (const key of FORK_AGENT_WRITABLE_KEYS) expect(FORK_PERSON_ONLY_KEYS).not.toContain(key);
  });

  it("finds every T3 tool", () => {
    // Spot checks across toolkits, upstream's and the fork's, plus the always-loaded ones.
    for (const name of [
      "delegate_task",
      "t3_environment_preferences_update",
      "t3_project_update",
      "t3_thread_configure",
      "wait_for",
      "tool_search",
      "request_secret",
      "send_to_user",
    ]) {
      expect(tools.has(name), name).toBe(true);
    }
    expect(tools.size).toBeGreaterThanOrEqual(70);
    // A tool defined outside the toolkit modules would be missed above.
    expect(toolsDefinedElsewhere().toSorted()).toEqual(Object.keys(NOT_ADVERTISED).toSorted());
  });

  it("finds no person-only setting in any T3 tool's input", () => {
    const found = [...tools.values()].flatMap((tool) =>
      personOnlyKeysIn(tool).map((key) => `${tool.name}: ${key}`),
    );
    expect(found).toEqual([]);
  });

  it("catches a tool whose input names one, in any spelling or depth", () => {
    for (const field of ["mcpProfile", "mcp_profile", "MCP-PROFILE"]) {
      const tool = Tool.make("fake_settings_update", {
        parameters: Schema.Struct({
          projectId: Schema.String,
          patch: Schema.Struct({ [field]: Schema.optionalKey(Schema.String) }),
        }),
      });
      expect(personOnlyKeysIn(tool), field).toEqual(["mcpProfile"]);
    }
  });
});
