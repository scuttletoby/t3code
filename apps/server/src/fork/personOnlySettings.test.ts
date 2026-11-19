// @effect-diagnostics nodeBuiltinImport:off - Lists the server sources and toolkit modules to find every T3 tool.
/**
 * Fork: only a person changes fork settings. No T3 tool's input may name a fork setting
 * (`FORK_PERSON_ONLY_KEYS`: every fork key not in `FORK_AGENT_WRITABLE_KEYS`), so an agent has no
 * tool path to write one. The list is derived from forkSettings.ts, so a new fork setting is
 * covered without a row here.
 */
import * as NodeFS from "node:fs";
import * as NodePath from "node:path";

import {
  FORK_AGENT_WRITABLE_KEYS,
  FORK_PERSON_ONLY_KEYS,
  MCP_T3_TOOL_GROUPS,
  MCP_T3_UNGROUPED_TOOLS,
} from "@t3tools/contracts";
import { Tool, Toolkit } from "effect/ai";
import * as Schema from "effect/Schema";
import { beforeAll, describe, expect, it } from "vite-plus/test";

import {
  MCP_T3_TOOLKIT_GROUPS,
  MCP_T3_TOOL_NOT_ALLOWED,
  MCP_T3_UNGROUPED_TOOL_KITS,
  mcpT3ToolGroup,
} from "./mcpProfile.ts";

const TOOLKITS_DIR = NodePath.resolve(import.meta.dirname, "../mcp/toolkits");
/** Every toolkit folder; each must define its tools in `tools.ts` and export its toolkits there. */
const toolkitFolders = (): ReadonlyArray<string> =>
  NodeFS.readdirSync(TOOLKITS_DIR, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name);
const toolsFile = (folder: string) => NodePath.join(TOOLKITS_DIR, folder, "tools.ts");

const escapeRegExp = (text: string) => text.replaceAll(/[.*+?^${}()|[\]\\]/g, "\\$&");
/**
 * How a source file can call effect/ai's `Tool.make`, read from its imports so an alias counts:
 * `Tool.make`, `T.make` (`{ Tool as T }`), `Ai.Tool.make` (`* as Ai`), or `make` from effect/ai/Tool.
 */
const toolMakeCallees = (source: string): ReadonlyArray<string> => {
  const callees: Array<string> = [];
  for (const [, names = "", from] of source.matchAll(
    /import\s+\{([^}]*)\}\s*from\s*["'](effect\/ai(?:\/Tool)?)["']/g,
  )) {
    for (const spec of names.split(",")) {
      const [imported = "", local = imported] = spec.trim().split(/\s+as\s+/);
      if (from === "effect/ai" && imported === "Tool") callees.push(`${local}.make`);
      if (from === "effect/ai/Tool" && imported === "make") callees.push(local);
    }
  }
  for (const [, local = "", from] of source.matchAll(
    /import\s+\*\s+as\s+(\w+)\s+from\s*["'](effect\/ai(?:\/Tool)?)["']/g,
  )) {
    callees.push(from === "effect/ai" ? `${local}.Tool.make` : `${local}.make`);
  }
  return callees;
};
/** How many times a source file calls `Tool.make`, under any alias. */
const toolMakeCalls = (source: string): number =>
  toolMakeCallees(source).reduce(
    (count, callee) =>
      count +
      [...source.matchAll(new RegExp(`(?<![\\w.$])${escapeRegExp(callee)}\\(`, "g"))].length,
    0,
  );
/** Server sources (relative to src/) that call `Tool.make` outside the toolkit modules. */
const toolsDefinedElsewhere = (): ReadonlyArray<string> => {
  const root = NodePath.resolve(import.meta.dirname, "..");
  return NodeFS.readdirSync(root, { recursive: true, encoding: "utf8" })
    .filter((path) => path.endsWith(".ts") && !path.endsWith(".test.ts"))
    .map((path) => path.split(NodePath.sep).join("/"))
    .filter((path) => !/^mcp\/toolkits\/[^/]+\/tools\.ts$/.test(path))
    .filter((path) => toolMakeCalls(NodeFS.readFileSync(NodePath.join(root, path), "utf8")) > 0);
};
/** Files that call `Tool.make` but advertise nothing: each with the reason. */
const NOT_ADVERTISED: Readonly<Record<string, string>> = {
  // Describes the linked environment's `orchestrator_capabilities` to call it there.
  "peer/RemoteDelegation.ts": "a client of another environment's tool",
  // Decodes the linked environment's `t3_thread_read` without the thread's details.
  "fork/remoteReplyRead.ts": "a client of another environment's tool",
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
/** Each toolkit folder's tools, as its exported toolkits hold them. */
const toolsByFolder = new Map<string, ReadonlySet<string>>();
beforeAll(async () => {
  for (const folder of toolkitFolders()) {
    if (!NodeFS.existsSync(toolsFile(folder))) continue;
    const module = (await import(toolsFile(folder))) as Record<string, unknown>;
    const names = new Set<string>();
    for (const value of Object.values(module)) {
      if (!isToolkit(value)) continue;
      for (const tool of Object.values(value.tools)) {
        tools.set(tool.name, tool);
        names.add(tool.name);
      }
    }
    toolsByFolder.set(folder, names);
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
        "playtest",
        "handoff",
        "agentSlots",
        "mcpProfiles",
        "mcpProfile",
        "mcpSubagentProfile",
        "mcpThreadProfiles",
        "reviewRules",
        "modelPolicy",
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

  it("loads every toolkit file, and every tool each one makes is in an exported toolkit", () => {
    const folders = toolkitFolders();
    expect(folders.filter((folder) => !NodeFS.existsSync(toolsFile(folder)))).toEqual([]);
    expect([...toolsByFolder.keys()].toSorted()).toEqual(folders.toSorted());
    // A tool made but left out of every exported toolkit would never be checked.
    const counts = (count: (folder: string) => number) =>
      Object.fromEntries(folders.map((folder) => [folder, count(folder)]));
    expect(counts((folder) => toolsByFolder.get(folder)?.size ?? 0)).toEqual(
      counts((folder) => toolMakeCalls(NodeFS.readFileSync(toolsFile(folder), "utf8"))),
    );
  });

  it("counts `Tool.make` under any import alias", () => {
    const cases: ReadonlyArray<readonly [string, number]> = [
      ['import { Tool, Toolkit } from "effect/ai";\nTool.make("a", {});', 1],
      ['import { Toolkit, Tool as T } from "effect/ai";\nT.make("a", {}); T.make("b", {});', 2],
      ['import * as Ai from "effect/ai";\nAi.Tool.make("a", {});', 1],
      ['import * as AiTool from "effect/ai/Tool";\nAiTool.make("a", {});', 1],
      ['import { make as makeTool } from "effect/ai/Tool";\nmakeTool("a", {});', 1],
      ['import { Schema } from "effect";\nSchema.make(1); Tool.make("a", {});', 0],
    ];
    for (const [source, calls] of cases) expect(toolMakeCalls(source), source).toBe(calls);
  });

  it("finds no person-only setting in any T3 tool's input", () => {
    const found = [...tools.values()].flatMap((tool) =>
      personOnlyKeysIn(tool).map((key) => `${tool.name}: ${key}`),
    );
    expect(found).toEqual([]);
  });

  it("catches a tool whose input names one, in any spelling or depth", () => {
    for (const [field, key] of [
      ["mcpProfile", "mcpProfile"],
      ["mcp_profile", "mcpProfile"],
      ["MCP-PROFILE", "mcpProfile"],
      ["review_rules", "reviewRules"],
      ["model_policy", "modelPolicy"],
    ] as const) {
      const tool = Tool.make("fake_settings_update", {
        parameters: Schema.Struct({
          projectId: Schema.String,
          patch: Schema.Struct({ [field]: Schema.optionalKey(Schema.String) }),
        }),
      });
      expect(personOnlyKeysIn(tool), field).toEqual([key]);
    }
  });
});

describe("MCP profile T3 tool groups", () => {
  it("maps every T3 tool to a group or to the ungrouped list", () => {
    const unmapped = [...toolsByFolder].flatMap(([folder, names]) =>
      [...names].filter((name) => mcpT3ToolGroup(folder, name) === MCP_T3_TOOL_NOT_ALLOWED),
    );
    expect(unmapped).toEqual([]);
  });

  it("has a row for each toolkit and uses every group", () => {
    expect(Object.keys(MCP_T3_TOOLKIT_GROUPS).toSorted()).toEqual(toolkitFolders().toSorted());
    const used = new Set(
      [...toolsByFolder].flatMap(([folder, names]) =>
        [...names].map((name) => mcpT3ToolGroup(folder, name)),
      ),
    );
    expect(MCP_T3_TOOL_GROUPS.filter((group) => !used.has(group))).toEqual([]);
    expect(MCP_T3_UNGROUPED_TOOLS.filter((name) => !tools.has(name))).toEqual([]);
  });

  it("pins each ungrouped tool to the toolkit that has it", () => {
    expect(Object.keys(MCP_T3_UNGROUPED_TOOL_KITS).toSorted()).toEqual(
      [...MCP_T3_UNGROUPED_TOOLS].toSorted(),
    );
    for (const [tool, toolkit] of Object.entries(MCP_T3_UNGROUPED_TOOL_KITS)) {
      expect(toolsByFolder.get(toolkit)?.has(tool), `${toolkit} has ${tool}`).toBe(true);
      expect(mcpT3ToolGroup(toolkit, tool)).toBe("ungrouped");
    }
  });

  it("does not take an ungrouped tool's name in another toolkit as ungrouped", () => {
    // Same name, other toolkit: that toolkit's group, or not allowed in a toolkit with no group.
    expect(mcpT3ToolGroup("waits", "send_to_user")).toBe("waits");
    expect(mcpT3ToolGroup("orchestrator", "tool_search")).toBe("orchestration");
    expect(mcpT3ToolGroup("message", "request_secret")).toBe(MCP_T3_TOOL_NOT_ALLOWED);
    expect(mcpT3ToolGroup("toolSearch", "send_to_user")).toBe(MCP_T3_TOOL_NOT_ALLOWED);
  });

  it("does not allow a tool this build does not know", () => {
    expect(mcpT3ToolGroup("someNewToolkit", "anything")).toBe(MCP_T3_TOOL_NOT_ALLOWED);
    expect(mcpT3ToolGroup("someNewToolkit", "send_to_user")).toBe(MCP_T3_TOOL_NOT_ALLOWED);
    expect(mcpT3ToolGroup("message", "new_message_tool")).toBe(MCP_T3_TOOL_NOT_ALLOWED);
  });
});
