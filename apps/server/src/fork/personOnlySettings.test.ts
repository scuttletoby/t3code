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
  MCP_T3_NARROW_GROUPS,
  MCP_T3_TOOL_GROUPS,
  MCP_T3_UNGROUPED_TOOLS,
} from "@t3tools/contracts";
import { Tool, Toolkit } from "effect/ai";
import * as Schema from "effect/Schema";
import { beforeAll, describe, expect, it } from "vite-plus/test";

import { withoutMachineOnlyFields } from "./advertisedMachineFields.ts";
import { T3_TOOL_GROUPS, T3_TOOL_NARROW_GROUPS } from "./mcpT3ToolRule.ts";
import {
  MCP_T3_NARROW_GROUP_TOOLS,
  MCP_T3_TOOLKIT_GROUPS,
  MCP_T3_TOOL_NOT_ALLOWED,
  MCP_T3_TOOL_OWN_GROUPS,
  MCP_T3_UNGROUPED_TOOL_KITS,
  mcpT3ToolGroup,
  mcpT3ToolNarrowGroups,
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
  // Asks the linked environment's `orchestrator_capabilities` which driver a task's instance runs.
  "fork/remoteMcpProfile.ts": "a client of another environment's tool",
  // Reads a linked environment's `t3_environment_read` and `orchestrator_capabilities` to advise.
  "fork/placementAdvice.ts": "a client of another environment's tool",
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

/**
 * The person-only settings a tool's input names. A field only a linked machine sets is skipped: it
 * is kept only on that machine's stamped call and only narrows (see advertisedMachineFields.ts).
 */
const personOnlyKeysIn = (tool: Tool.Any): ReadonlyArray<string> =>
  propertyNames(
    withoutMachineOnlyFields(Tool.getJsonSchema(tool) as { [key: string]: unknown }),
  ).flatMap((name) => {
    const key = PERSON_ONLY.get(normalize(name));
    return key === undefined ? [] : [key];
  });

/** Keys that give a schema a shape; a node with none of them takes any value. */
const SHAPED = ["type", "$ref", "anyOf", "oneOf", "allOf", "enum", "const", "properties", "items"];

/**
 * Where a JSON schema takes keys it does not name: a record, an object open to more keys, or a value
 * of any shape. A person-only setting could pass there under no name the check above sees.
 * Paths are dotted from `$`; `*` is a record's value, `[]` an array's item.
 */
const openKeyPaths = (schema: unknown, at = "$"): ReadonlyArray<string> => {
  if (schema === null || typeof schema !== "object" || Array.isArray(schema)) return [];
  const node = schema as Record<string, unknown>;
  const open =
    // A struct says `additionalProperties: true`, but a call's decode drops a key it does not name
    // (the test below): only a record, whose value has a schema here, keeps one.
    (node.additionalProperties !== null && typeof node.additionalProperties === "object") ||
    node.patternProperties !== undefined ||
    !SHAPED.some((key) => key in node);
  const children = (value: unknown): ReadonlyArray<readonly [string, unknown]> =>
    value !== null && typeof value === "object" ? Object.entries(value) : [];
  const inner = Object.entries(node).flatMap(([key, value]) => {
    switch (key) {
      case "properties":
        return children(value).flatMap(([name, child]) => openKeyPaths(child, `${at}.${name}`));
      case "$defs":
      case "definitions":
        return children(value).flatMap(([name, child]) => openKeyPaths(child, `#${name}`));
      case "patternProperties":
        return children(value).flatMap(([, child]) => openKeyPaths(child, `${at}.*`));
      case "additionalProperties":
        return openKeyPaths(value, `${at}.*`);
      case "type":
        // An array with no item schema takes items of any shape.
        return value === "array" && node.items === undefined && node.prefixItems === undefined
          ? [`${at}[]`]
          : [];
      case "items":
        return Array.isArray(value)
          ? value.flatMap((child) => openKeyPaths(child, `${at}[]`))
          : openKeyPaths(value, `${at}[]`);
      case "prefixItems":
        return children(value).flatMap(([, child]) => openKeyPaths(child, `${at}[]`));
      case "anyOf":
      case "oneOf":
      case "allOf":
        return children(value).flatMap(([, child]) => openKeyPaths(child, at));
      // A JSON string the call decodes to a value.
      case "contentSchema":
        return openKeyPaths(value, at);
      default:
        return [];
    }
  });
  return [...(open ? [at] : []), ...inner];
};

/** Each T3 tool's open places, as `tool $.path`. */
const openKeysIn = (tool: Tool.Any): ReadonlyArray<string> => [
  ...new Set(
    openKeyPaths(
      withoutMachineOnlyFields(Tool.getJsonSchema(tool) as { [key: string]: unknown }),
    ).map((path) => `${tool.name} ${path}`),
  ),
];

/**
 * Every place a T3 tool's input takes keys it does not name, each with why a person-only setting
 * sent there never reaches the settings. A new one fails the test below until it is reviewed here.
 */
/** ModelSelection reads its fields leniently, then decodes to a closed shape. */
const MODEL_SELECTION =
  "upstream's ModelSelection (contracts modelSelection.ts) decodes these to a string instance id, a model name and options as id/value pairs; nothing else is kept";
/** A child's model options. */
const TARGET_OPTIONS =
  "model options for the new thread, each checked against its model's options (an unknown id is refused); they go to that thread's model selection only";

const OPEN_KEYS_REVIEWED: Readonly<Record<string, string>> = {
  "create_threads $.threads[].target.options": TARGET_OPTIONS,
  "delegate_task $.target.options": TARGET_OPTIONS,
  "list_issues $.cursors": "paging cursors handed back to the issue tracker; read only",
  "t3_project_create $.defaultModelSelection.instanceId": MODEL_SELECTION,
  "t3_project_create $.defaultModelSelection.model": MODEL_SELECTION,
  "t3_project_create $.defaultModelSelection.options": MODEL_SELECTION,
  "t3_project_create $.defaultModelSelection.provider": MODEL_SELECTION,
  "t3_project_update $.defaultModelSelection.instanceId": MODEL_SELECTION,
  "t3_project_update $.defaultModelSelection.model": MODEL_SELECTION,
  "t3_project_update $.defaultModelSelection.options": MODEL_SELECTION,
  "t3_project_update $.defaultModelSelection.provider": MODEL_SELECTION,
  "t3_thread_configure $.modelSelection.instanceId": MODEL_SELECTION,
  "t3_thread_configure $.modelSelection.model": MODEL_SELECTION,
  "t3_thread_configure $.modelSelection.options": MODEL_SELECTION,
  "t3_thread_configure $.modelSelection.provider": MODEL_SELECTION,
  "t3_thread_launch $.modelSelection.instanceId": MODEL_SELECTION,
  "t3_thread_launch $.modelSelection.model": MODEL_SELECTION,
  "t3_thread_launch $.modelSelection.options": MODEL_SELECTION,
  "t3_thread_launch $.modelSelection.provider": MODEL_SELECTION,
  // Upstream's workflow runs: the run's input and a stored or signalled value stay in the
  // workflow store; workflows only read settings, never write them.
  "t3_workflow $.input":
    "a workflow run's own input, kept with the run; never merged into settings",
  "t3_workflow $.value":
    "a value a workflow keeps or a wait receives, kept in the workflow store; never merged into settings",
};

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
        "mcpCarriedProfiles",
        "reviewRules",
        "modelPolicy",
        "placement",
        "placementNeeds",
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
      "playtest",
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
      ["placement", "placement"],
      ["Placement", "placement"],
      ["placement_needs", "placementNeeds"],
      ["PLACEMENT-NEEDS", "placementNeeds"],
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

  it("lists every place a T3 tool's input takes keys it does not name, each reviewed (#785)", () => {
    const found = [...tools.values()].flatMap(openKeysIn).toSorted();
    expect(found).toEqual(Object.keys(OPEN_KEYS_REVIEWED).toSorted());
  });

  it("catches a record, an open object or a value of any shape in a tool's input", () => {
    const tool = Tool.make("fake_open", {
      parameters: Schema.Struct({
        threadId: Schema.String,
        patch: Schema.Record(Schema.String, Schema.String),
        options: Schema.Struct({ extra: Schema.Unknown, list: Schema.Array(Schema.Unknown) }),
        either: Schema.Union([Schema.String, Schema.Record(Schema.String, Schema.Number)]),
      }),
    });
    expect(openKeysIn(tool).toSorted()).toEqual(
      [
        "fake_open $.patch",
        "fake_open $.options.extra",
        "fake_open $.options.list[]",
        "fake_open $.either",
      ].toSorted(),
    );
    const closed = Tool.make("fake_closed", {
      parameters: Schema.Struct({ threadId: Schema.String, n: Schema.optionalKey(Schema.Number) }),
    });
    expect(openKeysIn(closed)).toEqual([]);
  });

  it("catches a person-only key at the end of a settings patch path", () => {
    // `settings.placement.mode` and `settings.projectSettingsOverrides.<id>.placementNeeds`: the key
    // is found under a record of projects too, not only as a top-level name.
    const tool = Tool.make("fake_patch", {
      parameters: Schema.Struct({
        settings: Schema.Struct({
          placement: Schema.optionalKey(Schema.Struct({ mode: Schema.String })),
          projectSettingsOverrides: Schema.optionalKey(
            Schema.Record(
              Schema.String,
              Schema.Struct({ placementNeeds: Schema.optionalKey(Schema.Array(Schema.String)) }),
            ),
          ),
        }),
      }),
    });
    expect(personOnlyKeysIn(tool).toSorted()).toEqual(["placement", "placementNeeds"]);
  });

  it("drops a person-only key a settings tool's call sends unnamed, at any depth", () => {
    // The tools that write settings, each with a call they take. The decode a call goes through
    // (McpHttpServer) drops a key the input does not name, so `placement` sent anyway, at the top
    // or inside one of the tool's own objects, never reaches the handler.
    const calls: ReadonlyArray<readonly [string, Record<string, unknown>]> = [
      [
        "t3_environment_preferences_update",
        { backgroundActivity: { profile: "balanced" }, newWorktreesStartFromOrigin: true },
      ],
      ["t3_project_update", { projectId: "project-1", title: "Renamed" }],
      ["t3_thread_configure", { threadId: "thread-1" }],
    ];
    const sneaked = {
      placement: { mode: "enforce", labels: ["owner-logins"] },
      placementNeeds: ["gpu"],
      projectSettingsOverrides: { "project-1": { placementNeeds: ["gpu"] } },
    };
    /** The call with `sneaked` added to it and to each object inside it. */
    const withSneaked = (value: unknown): unknown =>
      value !== null && typeof value === "object" && !Array.isArray(value)
        ? {
            ...Object.fromEntries(
              Object.entries(value).map(([key, inner]) => [key, withSneaked(inner)]),
            ),
            ...sneaked,
          }
        : value;
    const keysIn = (value: unknown): ReadonlyArray<string> =>
      value !== null && typeof value === "object"
        ? Object.entries(value).flatMap(([key, inner]) => [key, ...keysIn(inner)])
        : [];
    for (const [name, call] of calls) {
      const tool = tools.get(name);
      expect(tool, name).toBeDefined();
      if (tool === undefined) continue;
      // As McpHttpServer decodes a call.
      const decode = Schema.decodeUnknownSync(
        Schema.toCodecJson(tool.parametersSchema as Schema.Codec<unknown, unknown>),
      );
      // The call itself is one the tool takes.
      expect(decode(call), name).toMatchObject(call);
      const decoded = decode(withSneaked(call));
      expect(
        keysIn(decoded).filter((key) => PERSON_ONLY.has(normalize(key))),
        name,
      ).toEqual([]);
    }
  });

  it("skips only the carried profile a linked machine sends, not one beside it", () => {
    const tool = Tool.make("fake_launch", {
      parameters: Schema.Struct({
        mcpProfile: Schema.optionalKey(Schema.String),
        delegatedFrom: Schema.optional(
          Schema.NullOr(
            Schema.Struct({
              threadId: Schema.String,
              mcpProfile: Schema.optionalKey(Schema.String),
              reviewRules: Schema.optionalKey(Schema.String),
            }),
          ),
        ),
      }),
    });
    expect(personOnlyKeysIn(tool).toSorted()).toEqual(["mcpProfile", "reviewRules"]);
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
        [...names].flatMap((name) => [
          mcpT3ToolGroup(folder, name),
          ...mcpT3ToolNarrowGroups(folder, name),
        ]),
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

  it("puts every T3 tool in exactly one known group, and playtest alone in its own", () => {
    const known: ReadonlyArray<string> = [...MCP_T3_TOOL_GROUPS, "ungrouped"];
    const members = new Map<string, Array<string>>();
    for (const [folder, names] of toolsByFolder) {
      for (const name of names) {
        const group = mcpT3ToolGroup(folder, name);
        expect(known, `${folder}/${name} -> ${group}`).toContain(group);
        members.set(group, [...(members.get(group) ?? []), `${folder}/${name}`]);
      }
    }
    // A profile that keeps waits (to wait and list tasks) does not get the playtest tool.
    expect(members.get("playtest")).toEqual(["waits/playtest"]);
    expect(members.get("waits")).not.toContain("waits/playtest");
  });

  it("pins each own-group tool to the toolkit that has it", () => {
    for (const [tool, { toolkit, group }] of Object.entries(MCP_T3_TOOL_OWN_GROUPS)) {
      expect(toolsByFolder.get(toolkit)?.has(tool), `${toolkit} has ${tool}`).toBe(true);
      expect(MCP_T3_UNGROUPED_TOOLS).not.toContain(tool);
      expect(mcpT3ToolGroup(toolkit, tool)).toBe(group);
    }
    // The toolkit's other tools keep its group; the same name in another toolkit is that one's.
    expect(mcpT3ToolGroup("waits", "wait_for")).toBe("waits");
    expect(mcpT3ToolGroup("thread", "playtest")).toBe("threads");
    expect(mcpT3ToolGroup("message", "playtest")).toBe(MCP_T3_TOOL_NOT_ALLOWED);
  });

  it("does not take an ungrouped tool's name in another toolkit as ungrouped", () => {
    // Same name, other toolkit: that toolkit's group, or not allowed in a toolkit with no group.
    expect(mcpT3ToolGroup("waits", "send_to_user")).toBe("waits");
    expect(mcpT3ToolGroup("orchestrator", "tool_search")).toBe("orchestration");
    expect(mcpT3ToolGroup("message", "request_secret")).toBe(MCP_T3_TOOL_NOT_ALLOWED);
    expect(mcpT3ToolGroup("toolSearch", "send_to_user")).toBe(MCP_T3_TOOL_NOT_ALLOWED);
  });

  it("gives the T3 tool filter's table exactly the toolkit folders' tools, each in its group", () => {
    // The filter (mcpT3ToolRule.ts) lists its toolkits by hand. A toolkit it leaves out would have
    // every tool refused by any profile but `all`, and no profile setting could fix that.
    const fromFolders = new Map<string, string>();
    for (const [folder, names] of toolsByFolder) {
      for (const name of names) {
        const group = mcpT3ToolGroup(folder, name);
        const known = fromFolders.get(name);
        fromFolders.set(name, known === undefined || known === group ? group : "two groups");
      }
    }
    const sorted = (table: ReadonlyMap<string, string>) =>
      [...table].toSorted(([a], [b]) => a.localeCompare(b));
    expect(sorted(T3_TOOL_GROUPS)).toEqual(sorted(fromFolders));
  });

  it("gives every group of MCP_T3_TOOL_GROUPS a tool in the T3 tool filter's table", () => {
    // A group added to the contracts (a new toolkit, or tools moved) with no toolkit behind it in the
    // filter would be offered in Settings and keep nothing.
    const used = new Set<string>([
      ...T3_TOOL_GROUPS.values(),
      ...[...T3_TOOL_NARROW_GROUPS.values()].flat(),
    ]);
    expect(MCP_T3_TOOL_GROUPS.filter((group) => !used.has(group))).toEqual([]);
  });

  it("names each narrow group's tools in their toolkits, within its whole groups (#980)", () => {
    for (const [narrow, kits] of Object.entries(MCP_T3_NARROW_GROUP_TOOLS)) {
      const wholes = MCP_T3_NARROW_GROUPS[narrow as keyof typeof MCP_T3_NARROW_GROUPS] ?? [];
      expect(wholes, narrow).not.toEqual([]);
      const homes = new Set<string>();
      for (const [toolkit, names] of Object.entries(kits)) {
        for (const name of names) {
          expect(toolsByFolder.get(toolkit)?.has(name), `${toolkit} has ${name}`).toBe(true);
          const home = mcpT3ToolGroup(toolkit, name);
          // Within its whole groups, so a profile with all of them keeps the narrow group's tools.
          expect(wholes, `${toolkit}/${name} -> ${home}`).toContain(home);
          homes.add(home);
        }
      }
      // Each whole group listed has one of its tools, so the containment is not wider than real.
      expect([...wholes].toSorted(), narrow).toEqual([...homes].toSorted());
    }
    // No other group is narrow, and no tool is in a narrow group of a toolkit that lacks it.
    expect(Object.keys(MCP_T3_NARROW_GROUPS).toSorted()).toEqual(
      Object.keys(MCP_T3_NARROW_GROUP_TOOLS).toSorted(),
    );
    expect(mcpT3ToolNarrowGroups("project", "t3_thread_send")).toEqual([]);
    expect(mcpT3ToolNarrowGroups("orchestrator", "t3_thread_send")).toEqual(["message"]);
  });

  it("does not allow a tool this build does not know", () => {
    expect(mcpT3ToolGroup("someNewToolkit", "anything")).toBe(MCP_T3_TOOL_NOT_ALLOWED);
    expect(mcpT3ToolGroup("someNewToolkit", "send_to_user")).toBe(MCP_T3_TOOL_NOT_ALLOWED);
    expect(mcpT3ToolGroup("message", "new_message_tool")).toBe(MCP_T3_TOOL_NOT_ALLOWED);
  });
});
