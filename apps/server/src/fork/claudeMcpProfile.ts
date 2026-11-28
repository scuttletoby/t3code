/**
 * Fork: a thread's MCP profile, applied to its Claude session. This applies the outside servers;
 * T3's own tools are filtered on the T3 MCP server for every provider (mcpT3ToolRule.ts), with
 * the groups fixed as each query opens. A profile that cannot be applied refuses the turn instead
 * of starting it with every tool: the owner chose refusing over warning.
 */
import {
  BUILTIN_TOOL_NAMES,
  type HookCallback,
  type HookCallbackMatcher,
  type HookEvent,
  type McpServerStatus,
  type PreToolUseHookInput,
  type Settings as ClaudeSettings,
} from "@anthropic-ai/claude-agent-sdk";
import {
  DEFAULT_SERVER_SETTINGS,
  MCP_BUILTIN_TOOLS_NONE,
  MCP_CARRIED_PROFILES_REFUSE,
  MCP_PROFILE_ALL,
  type OrchestrationV2AppThread,
  ProjectId,
  type ServerSettings,
  type ThreadId,
  isDelegatedAppThread,
} from "@t3tools/contracts";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";

import * as ServerConfig from "../config.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import {
  MCP_PROFILE_MAX_LINEAGE,
  type McpProfileResolved,
  type McpProfileSet,
  type McpProfileThread,
  resolveMcpProfile,
} from "./mcpProfile.ts";
import { makeClaudeAgentModelPolicyHook } from "./claudeAgentModelPolicy.ts";
import { withBackgroundAgentGuidance } from "./claudeBackgroundAgents.ts";
import { decodeSettingsFileSilently } from "./settingsFileRead.ts";
import {
  MAX_T3_TOOL_SETS,
  type T3ToolSet,
  fixT3ToolSet,
  nextT3ToolSetTurn,
  refusedT3ToolSet,
  t3ToolSetOf,
} from "./mcpT3ToolSets.ts";
import {
  type McpProfileFileKeys,
  mcpProfileFileKeysOf,
  readMcpProfileKeysFromText,
  withMcpProfileFileKeys,
} from "./mcpProfileSettingsText.ts";

export class ClaudeMcpProfileError extends Schema.TaggedError<ClaudeMcpProfileError>()(
  "ClaudeMcpProfileError",
  { reason: Schema.String },
) {
  override get message(): string {
    return this.reason;
  }
}

const isRefusal = Schema.is(ClaudeMcpProfileError);

/**
 * The SDK options a profile adds, and the outside servers the session may then show. `hooks` holds
 * the call-time guard; ClaudeAdapterV2 merges it into its own hooks.
 */
export interface ClaudeMcpProfilePlan {
  readonly name: string;
  readonly options: {
    readonly strictMcpConfig?: true;
    readonly managedSettings?: ClaudeSettings;
    readonly hooks?: Partial<Record<HookEvent, Array<HookCallbackMatcher>>>;
    /** `builtinTools: "none"`: `[]`, after (so over) the adapter's own tool set. */
    readonly tools?: Array<string>;
    readonly disallowedTools?: Array<string>;
  };
  readonly servers: McpProfileSet["servers"];
  /** T3's own tool groups; the T3 MCP server applies them (mcpT3ToolRule.ts). */
  readonly t3Tools: McpProfileSet["t3Tools"];
  readonly builtinTools?: McpProfileSet["builtinTools"];
}

export type LineageThread = Pick<
  OrchestrationV2AppThread,
  "id" | "projectId" | "lineage" | "delegatedFrom"
>;

const ALL_PLAN: ClaudeMcpProfilePlan = {
  name: MCP_PROFILE_ALL,
  options: {},
  servers: "all",
  t3Tools: "all",
};
const FIX = `pick "${MCP_PROFILE_ALL}" for this thread, or fix the profile in Settings`;
/** What can make Claude load a server T3's options leave out, at start. */
const START_LEAKS =
  "A managed Claude policy on this machine or --mcp-config in the Claude launch arguments can add servers T3's list does not drop";

/**
 * `builtinTools: "none"`: Claude starts with no built-in tool (`tools: []`), every one the SDK
 * names is also disallowed (so an `allowedTools` entry cannot bring one back), and a call to any
 * tool but an MCP server's is refused as it is made (what a launch argument adds). With no
 * `ToolSearch`, Claude lists MCP tools directly.
 */
export function claudeMcpProfilePlan(
  name: string,
  profile: McpProfileSet,
  t3ServerNames: ReadonlyArray<string>,
): ClaudeMcpProfilePlan | ClaudeMcpProfileError {
  const plan = claudeMcpServersPlan(name, profile, t3ServerNames);
  if (isRefusal(plan) || profile.builtinTools !== MCP_BUILTIN_TOOLS_NONE) return plan;
  const preToolUse = [
    ...(plan.options.hooks?.PreToolUse ?? []),
    { hooks: [claudeBuiltinToolsGuard(name)] },
  ];
  return {
    ...plan,
    builtinTools: MCP_BUILTIN_TOOLS_NONE,
    options: {
      ...plan.options,
      tools: [],
      disallowedTools: [...BUILTIN_TOOL_NAMES],
      hooks: { ...plan.options.hooks, PreToolUse: preToolUse },
    },
  };
}

/**
 * Refuses a call to any tool but an MCP server's; hooks run in every mode and in sub-agents. Fails
 * closed: a tool name that is not a string, or any error while checking, refuses the call (a hook
 * that throws would count as a non-blocking error and let the call through). It never trusts
 * `hook_event_name`: whatever it says (missing, empty, another event), only an `mcp__` tool passes.
 */
const claudeBuiltinToolsGuard =
  (name: string): HookCallback =>
  async (input) => {
    try {
      const toolName: unknown = (input as { tool_name?: unknown }).tool_name;
      if (typeof toolName === "string" && toolName.startsWith("mcp__")) return {};
      const shown = typeof toolName === "string" ? `the built-in tool "${toolName}"` : "a tool";
      return builtinToolDenial(
        `MCP profile "${name}" has builtinTools "${MCP_BUILTIN_TOOLS_NONE}", so T3 refused ${shown}: this thread has only MCP tools. To use it, ${FIX}.`,
      );
    } catch {
      return builtinToolDenial(
        `MCP profile "${name}" has builtinTools "${MCP_BUILTIN_TOOLS_NONE}", and T3 could not check this tool call, so it refused it.`,
      );
    }
  };

const builtinToolDenial = (reason: string) => ({
  hookSpecificOutput: {
    hookEventName: "PreToolUse" as const,
    permissionDecision: "deny" as const,
    permissionDecisionReason: reason,
  },
});

/**
 * `t3-only` and an empty list ignore every server Claude would load itself; a list keeps those named.
 * `t3Tools` adds no option: T3's own server hides and refuses the tools it leaves out.
 */
function claudeMcpServersPlan(
  name: string,
  profile: McpProfileSet,
  t3ServerNames: ReadonlyArray<string>,
): ClaudeMcpProfilePlan | ClaudeMcpProfileError {
  const t3Tools = profile.t3Tools;
  if (profile.servers === "all") return { ...ALL_PLAN, name, t3Tools };
  const servers = profile.servers;
  const hooks = { PreToolUse: [{ hooks: [claudeMcpProfileGuard(name, servers, t3ServerNames)] }] };
  if (servers.length === 0) {
    return { name, options: { strictMcpConfig: true, hooks }, servers, t3Tools };
  }
  // T3's own servers are listed too, so the allowlist cannot drop them.
  const allowed = [...new Set([...t3ServerNames, ...servers])];
  return {
    name,
    options: {
      managedSettings: { allowedMcpServers: allowed.map((serverName) => ({ serverName })) },
      hooks,
    },
    servers,
    t3Tools,
  };
}

/** A server name as Claude writes it in a tool name (`mcp__<name>__<tool>`), per its CLI. */
const toolNameServer = (name: string) => {
  const normalized = name.replace(/[^a-zA-Z0-9_-]/g, "_");
  return name.startsWith("claude.ai ")
    ? normalized.replace(/_+/g, "_").replace(/^_|_$/g, "")
    : normalized;
};

/** Claude's own tools that reach an MCP server named in their input. */
const MCP_RESOURCE_TOOLS: ReadonlySet<string> = new Set([
  "ListMcpResourcesTool",
  "ReadMcpResourceTool",
]);

/** What `claudeMcpCallOutsideProfile` returns for a resource listing of every connected server. */
export const EVERY_MCP_SERVER = "*";

/**
 * Claude's tools that run a command by name, and the input holding it. Claude names an MCP
 * server's prompt `mcp__<server>__<prompt>`; Claude Code 2.1.326 refuses a plain MCP prompt in
 * `Skill`, but runs a skill an MCP server serves, so a name of that form is checked too.
 * `SlashCommand` is the older name of `Skill`, whose input holds the whole command line.
 */
const COMMAND_TOOLS: Readonly<Record<string, string>> = { Skill: "skill", SlashCommand: "command" };

const mcpToolServer = (allowed: ReadonlyArray<string>, toolName: string): string | undefined => {
  const kept = allowed.some((name) => toolName.startsWith(`mcp__${toolNameServer(name)}__`));
  return kept ? undefined : toolName.slice("mcp__".length).split("__")[0];
};

/**
 * The MCP server a tool call reaches that the profile leaves out, or `undefined`. Claude names the
 * server it calls (`mcp_server`); an older one does not, so the tool name's prefix is matched.
 */
export function claudeMcpCallOutsideProfile(
  servers: ReadonlyArray<string>,
  t3ServerNames: ReadonlyArray<string>,
  call: {
    readonly tool_name: string;
    readonly tool_input: unknown;
    readonly mcp_server?: { readonly name: string } | undefined;
  },
): string | undefined {
  const allowed = [...t3ServerNames, ...servers];
  if (call.mcp_server !== undefined) {
    return allowed.includes(call.mcp_server.name) ? undefined : call.mcp_server.name;
  }
  if (call.tool_name.startsWith("mcp__")) return mcpToolServer(allowed, call.tool_name);
  const input = (call.tool_input ?? {}) as Readonly<Record<string, unknown>>;
  if (MCP_RESOURCE_TOOLS.has(call.tool_name)) {
    const server = input.server;
    // A listing with no server lists every connected server's, those left out too.
    if (call.tool_name === "ListMcpResourcesTool" && (server === undefined || server === "")) {
      return EVERY_MCP_SERVER;
    }
    return typeof server === "string" && !allowed.includes(server) ? server : undefined;
  }
  const commandKey = Object.hasOwn(COMMAND_TOOLS, call.tool_name)
    ? COMMAND_TOOLS[call.tool_name]
    : undefined;
  const command = commandKey === undefined ? undefined : input[commandKey];
  if (typeof command === "string") {
    const name = command.trim().replace(/^\//, "").split(/\s/)[0] ?? "";
    return name.startsWith("mcp__") ? mcpToolServer(allowed, name) : undefined;
  }
  return undefined;
}

/**
 * Refuses, at the call, a tool of an MCP server the profile leaves out but that loaded anyway (a
 * managed policy that drops T3's allowlist, an agent file's servers, one added mid-session). Hooks
 * run in every permission mode and in sub-agents. It never trusts `hook_event_name`: whatever it
 * says (missing, empty, another event), a left-out server's tool is refused.
 */
const claudeMcpProfileGuard =
  (
    name: string,
    servers: ReadonlyArray<string>,
    t3ServerNames: ReadonlyArray<string>,
  ): HookCallback =>
  async (input) => {
    // Registered only under PreToolUse, so the input is read as one whatever its event name.
    const call = input as PreToolUseHookInput;
    const server = claudeMcpCallOutsideProfile(servers, t3ServerNames, call);
    if (server === undefined) return {};
    return {
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason:
          server === EVERY_MCP_SERVER
            ? `MCP profile "${name}" leaves out some MCP servers, so T3 refused listing every server's resources: name a server in "server", one the profile keeps.`
            : `MCP profile "${name}" leaves out the MCP server "${server}", so T3 refused this call. Claude loaded it anyway: a managed Claude policy on this machine, --mcp-config in the Claude launch arguments, or an agent file's servers can add one. To use it, ${FIX} so the profile names it.`,
      },
    };
  };

/**
 * Servers the started session shows that the plan leaves out. An IT-managed policy on the machine
 * drops T3's allowlist, and an older Claude may not know strict mode, so the session is checked.
 */
export function claudeMcpServersOutsidePlan(
  plan: ClaudeMcpProfilePlan,
  t3ServerNames: ReadonlyArray<string>,
  statuses: ReadonlyArray<Pick<McpServerStatus, "name" | "status">>,
): ReadonlyArray<string> {
  const kept = plan.servers;
  if (kept === "all") return [];
  return statuses
    .filter(
      (server) =>
        server.status !== "disabled" &&
        !t3ServerNames.includes(server.name) &&
        !kept.includes(server.name),
    )
    .map((server) => server.name);
}

const profileThread = (settings: ServerSettings, thread: LineageThread): McpProfileThread => {
  const project = settings.projectSettingsOverrides[thread.projectId];
  return {
    threadId: thread.id,
    delegated: isDelegatedAppThread(thread),
    project: { mcpProfile: project?.mcpProfile, mcpSubagentProfile: project?.mcpSubagentProfile },
  };
};

/** Reads a parent thread; `null` when the store has no such row or cannot read or decode it. */
export type FindThread = (threadId: ThreadId) => Effect.Effect<LineageThread | null>;

/**
 * The thread's MCP profile, from its lineage here and the profile carried with its top thread. An
 * unreadable parent refuses; `findThread` is `undefined` only with no store and no settings
 * service (upstream's adapter tests), where the defaults give `all` and nothing can be carried.
 */
/** The schema requires a lineage; a hand-made record without one is read as a root thread. */
const withLineage = (thread: LineageThread): LineageThread =>
  (thread.lineage as LineageThread["lineage"] | undefined) === undefined
    ? {
        ...thread,
        lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: thread.id },
      }
    : thread;

const mcpProfileForThread = Effect.fn("ClaudeMcpProfile.profileForThread")(function* (
  settings: ServerSettings,
  thread: LineageThread,
  findThread: FindThread | undefined,
) {
  const lineage: Array<LineageThread> = [withLineage(thread)];
  let parentMissing = false;
  // Parents are read even with no profile set here: one of them may be a task another machine
  // delegated, which carries the profile of its thread there.
  let parentId = lineage[0]!.lineage.parentThreadId;
  // One past the limit, or a repeat, is kept so the resolver refuses it.
  while (parentId !== null && lineage.length <= MCP_PROFILE_MAX_LINEAGE) {
    const repeated = lineage.find((entry) => entry.id === parentId);
    const found = repeated ?? (findThread === undefined ? null : yield* findThread(parentId));
    const parent = found === null ? null : withLineage(found);
    parentMissing = parent === null;
    if (parent === null) break;
    lineage.push(parent);
    parentId = repeated === undefined ? parent.lineage.parentThreadId : null;
  }
  const top = lineage[lineage.length - 1]!;
  const delegatedFrom = parentMissing ? null : (top.delegatedFrom ?? null);
  const carried = delegatedFrom?.mcpProfile;
  if (delegatedFrom !== null && carried === undefined) {
    // An older sender, or a task stored before carrying: its parent's profile there is unknown.
    yield* Effect.logWarning(
      "MCP profiles: the machine that delegated this thread's task sent no MCP profile (it runs an older T3), so the task gets this machine's profile for its role",
      { threadId: thread.id, delegatedFrom: delegatedFrom.environmentId },
    );
  }
  const resolved = resolveMcpProfile(
    settings,
    lineage.map((entry) => profileThread(settings, entry)),
    carried,
  );
  if (resolved._tag === "CannotApply") {
    return yield* new ClaudeMcpProfileError({ reason: resolved.message });
  }
  // A parent the store cannot give may be a task another machine delegated with a narrower
  // profile, so the thread refuses whatever the settings; only the owner's pick for it stands.
  if (parentMissing && findThread !== undefined && resolved.source !== "thread-pick") {
    return yield* new ClaudeMcpProfileError({
      reason: `This thread's parent thread cannot be read, so its MCP profile cannot be worked out: ${FIX}.`,
    });
  }
  return resolved;
});

/** The thread's profile as a Claude plan; a parent `findThread` cannot give refuses it. */
export const claudeMcpProfileForThread = Effect.fn("ClaudeMcpProfile.forThread")(function* (
  settings: ServerSettings,
  thread: LineageThread,
  t3ServerNames: ReadonlyArray<string>,
  findThread: FindThread,
) {
  const resolved = yield* mcpProfileForThread(settings, thread, findThread);
  const plan = claudeMcpProfilePlan(resolved.name, resolved.profile, t3ServerNames);
  return isRefusal(plan) ? yield* plan : plan;
});

const PLAN_KEYS = new WeakMap<object, { readonly key: string | undefined }>();

/**
 * The plan's part of the live-query key (`undefined` for `all`), found by the turn's MCP overrides,
 * the only input of upstream's key line. Overrides with no plan get a key no planned turn has, so
 * such a turn never reuses a process a profile started (a pure function, as upstream's test asks).
 */
export const planKeyFor = (mcpOverrides: object): string | undefined =>
  (PLAN_KEYS.get(mcpOverrides) ?? { key: "unplanned" }).key;

/**
 * The plan's key part, or `undefined` for `all`. NUL keeps the joined names apart. A change of T3
 * tool groups or built-in tools changes it too, so the next turn's fresh process lists them again.
 */
const planKey = (plan: ClaudeMcpProfilePlan): string | undefined =>
  plan.servers === "all" && plan.t3Tools === "all" && plan.builtinTools === undefined
    ? undefined
    : [
        "mcp",
        ...(plan.servers === "all" ? ["*"] : plan.servers),
        ...(plan.t3Tools === "all" ? [] : ["\u0001t3", ...plan.t3Tools]),
        ...(plan.builtinTools === undefined ? [] : ["\u0002builtin", plan.builtinTools]),
      ].join("\u0000");

/** settings.json as this read found it. */
type SettingsFileRead =
  /** No file: the policy's defaults, its locks laid over. */
  | { readonly kind: "missing"; readonly settings: ServerSettings }
  /** A file that decodes: its settings, the policy's locks laid over. */
  | { readonly kind: "decoded"; readonly settings: ServerSettings }
  /** A file that does not decode (or whose legacy keys do not): the MCP profile keys its text sets. */
  | { readonly kind: "text"; readonly keys: McpProfileFileKeys }
  /** Not a JSON object and naming no key (empty, cut off before its keys): it cannot say. */
  | { readonly kind: "unknown"; readonly issue: string }
  /** Names a key but cannot be read. */
  | { readonly kind: "unreadable"; readonly issue: string };

/**
 * settings.json as a load decodes it (no provider secrets: MCP keys hold none); `undefined` with no
 * file to read here. A file that does not decode gives no settings of its own: its MCP profile
 * keys are read from its text, as upstream's load would give every setting its default, a thread's
 * profile `all` too. A read error fails.
 */
const settingsFileReader = Effect.gen(function* () {
  const config = yield* Effect.serviceOption(ServerConfig.ServerConfig);
  const fileSystem = yield* Effect.serviceOption(FileSystem.FileSystem);
  const policy = yield* Effect.serviceOption(ManagedPolicy.ManagedPolicy);
  if (Option.isNone(config) || Option.isNone(fileSystem) || Option.isNone(policy)) {
    return undefined;
  }
  const { settingsPath } = config.value;
  const fs = fileSystem.value;
  const managed = policy.value.settings;
  const read = Effect.gen(function* () {
    if (!(yield* fs.exists(settingsPath))) {
      return { kind: "missing", settings: managed.applyLocks(managed.defaults) };
    }
    const raw = yield* fs.readFileString(settingsPath);
    const decoded = decodeSettingsFileSilently(
      raw,
      managed.withDefaults,
      ServerSettingsService.decodePersistedSettings,
    );
    if (Exit.isSuccess(decoded) && !ServerSettingsService.settingsFileLegacyKeysUnreadable(raw)) {
      return { kind: "decoded", settings: managed.applyLocks(decoded.value) };
    }
    const text = readMcpProfileKeysFromText(raw, managed.withDefaults);
    return text._tag === "keys"
      ? { kind: "text", keys: text.keys }
      : { kind: text._tag, issue: text.issue };
  }) satisfies Effect.Effect<SettingsFileRead, unknown>;
  return {
    read,
    /**
     * `base` with MCP profile keys a file that does not decode stands for, and
     * `mcpCarriedProfiles` `refuse` (the file may say so), the policy's locks laid over.
     */
    layOver: (base: ServerSettings, keys: McpProfileFileKeys): ServerSettings =>
      managed.applyLocks({
        ...withMcpProfileFileKeys(base, keys),
        mcpCarriedProfiles: MCP_CARRIED_PROFILES_REFUSE,
      }),
    /** The policy's defaults, a base when the settings service cannot read. */
    defaults: managed.defaults,
  };
});

/**
 * The MCP profile keys settings.json last gave (a file that decodes, text that reads, or no file)
 * per settings service, so per server process, with the read's number and whether a file gave
 * them. Memory only: a server that starts with a file that cannot say refuses until it is fixed.
 */
const LAST_READ_KEYS = new WeakMap<
  object,
  { readonly read: number; readonly fromFile: boolean; readonly keys: McpProfileFileKeys }
>();
/** Numbers reads in the order they start, so a read that ends late never stores older keys last. */
let readsStarted = 0;
const rememberKeys = (
  service: object,
  read: number,
  fromFile: boolean,
  keys: McpProfileFileKeys,
) => {
  const last = LAST_READ_KEYS.get(service);
  if (last === undefined || last.read < read) {
    LAST_READ_KEYS.set(service, { read, fromFile, keys });
  }
};

const cannotReadSettings = () =>
  new ClaudeMcpProfileError({
    reason: `T3 could not read this thread's MCP profile settings, so this thread cannot start: retry, or check that T3's settings.json can be read.`,
  });

/** The refusal for a settings.json whose text cannot say which MCP profile a thread has. */
const unreadableSettingsFile = (issue: string) =>
  new ClaudeMcpProfileError({
    reason: `T3's settings.json sets MCP profiles but cannot be read (${issue}), so this thread's MCP profile cannot be worked out and it cannot start: fix settings.json (the server log says what is wrong); the next turn reads it again.`,
  });

/** The refusal for a settings.json that cannot say, with no keys read since the server started. */
const noReadSettingsFile = (issue: string) =>
  new ClaudeMcpProfileError({
    reason: `T3's settings.json cannot be read (${issue}), and this server has not read its MCP profiles since it started, so this thread's MCP profile cannot be worked out and it cannot start: fix settings.json (the server log says what is wrong); the next turn reads it again.`,
  });

/**
 * The settings MCP profiles read, or a refusal. settings.json's own MCP keys win on every read (the
 * service may still hold a broken load's defaults). Undecodable text gives its keys and carried
 * `refuse`; text that cannot say, or no file once keys were read, keeps the keys last read,
 * refusing with none since start.
 */
export const makeMcpProfileSettings = Effect.gen(function* () {
  const settingsService = yield* Effect.serviceOption(ServerSettingsService.ServerSettingsService);
  if (Option.isNone(settingsService)) {
    yield* Effect.logWarning(
      "MCP profiles: no settings service here, so every thread gets the all profile, cut only to one another machine sent",
    );
    return Effect.succeed(DEFAULT_SERVER_SETTINGS);
  }
  const service = settingsService.value;
  const reader = yield* settingsFileReader;
  return Effect.gen(function* () {
    const read = yield* service.getSettings.pipe(Effect.result);
    if (reader === undefined) {
      if (read._tag === "Success") return read.success;
      yield* Effect.logWarning("MCP profiles: the settings could not be read", {
        cause: read.failure,
      });
      return yield* cannotReadSettings();
    }
    readsStarted += 1;
    const readNumber = readsStarted;
    const fileRead = yield* reader.read.pipe(Effect.result);
    if (fileRead._tag === "Failure") {
      // The service's settings may be a broken file's defaults from an earlier load, so they do
      // not count while the file cannot be read: never `all` on a guess.
      yield* Effect.logWarning("MCP profiles: settings.json could not be read", {
        cause: fileRead.failure,
      });
      return yield* cannotReadSettings();
    }
    const file = fileRead.success;
    const base = read._tag === "Success" ? read.success : reader.defaults;
    const last = LAST_READ_KEYS.get(service);
    const kept = last?.keys;
    if (file.kind === "missing" && last?.fromFile === true && kept !== undefined) {
      // No file after this run read a file's keys: an editor's rename-then-write gap, or a
      // delete. Keep them, never the defaults' all; with no file before, a fresh install.
      yield* Effect.logWarning(
        "MCP profiles: settings.json is missing, so the MCP profile settings last read stand, and carried MCP profiles read as refuse",
      );
      return reader.layOver(base, kept);
    }
    switch (file.kind) {
      case "missing":
      case "decoded": {
        rememberKeys(
          service,
          readNumber,
          file.kind === "decoded",
          mcpProfileFileKeysOf(file.settings),
        );
        if (read._tag === "Failure") {
          yield* Effect.logWarning(
            "MCP profiles: settings could not be read in full, so the MCP profile settings come from settings.json alone",
            { cause: read.failure },
          );
          return file.settings;
        }
        // No file: the service's settings stand. A file that decodes: its MCP profile keys count
        // over the service's, which may still be an earlier broken load's defaults.
        if (file.kind === "missing") return read.success;
        return {
          ...withMcpProfileFileKeys(read.success, mcpProfileFileKeysOf(file.settings)),
          mcpCarriedProfiles: file.settings.mcpCarriedProfiles,
        };
      }
      case "text":
        rememberKeys(service, readNumber, true, file.keys);
        yield* Effect.logWarning(
          "MCP profiles: settings.json does not decode, so the MCP profile settings come from its text, and carried MCP profiles read as refuse",
        );
        return reader.layOver(base, file.keys);
      case "unreadable":
        yield* Effect.logWarning(
          "MCP profiles: settings.json does not decode and its text cannot say which MCP profile a thread has, so threads are refused until it is fixed",
          { issue: file.issue },
        );
        return yield* unreadableSettingsFile(file.issue);
      case "unknown": {
        if (kept === undefined) {
          yield* Effect.logWarning(
            "MCP profiles: settings.json cannot be read and no MCP profile settings were read since the server started, so threads are refused until it is fixed",
            { issue: file.issue },
          );
          return yield* noReadSettingsFile(file.issue);
        }
        yield* Effect.logWarning(
          "MCP profiles: settings.json cannot be read, so the MCP profile settings last read stand, and carried MCP profiles read as refuse",
          { issue: file.issue },
        );
        return reader.layOver(base, kept);
      }
    }
  }).pipe(Effect.withSpan("ClaudeMcpProfile.readSettings"));
});

type McpOverrides = { readonly mcpServers?: Readonly<Record<string, unknown>> | undefined };

/** {@link makeThreadMcpProfileReads}, and the store's thread reads it uses. */
const makeLineageReads = Effect.gen(function* () {
  const settingsService = yield* Effect.serviceOption(ServerSettingsService.ServerSettingsService);
  const sql = yield* Effect.serviceOption(SqlClient.SqlClient);
  // The adapter built alone (upstream's adapter tests): default settings and no store, so nothing
  // here can narrow a thread. The server wires both.
  const alone = Option.isNone(sql) && Option.isNone(settingsService);
  if (Option.isNone(sql)) {
    yield* Effect.logWarning(
      alone
        ? "MCP profiles: no thread store and no settings here, so parent threads are not read; every thread gets all, cut only to one another machine sent"
        : "MCP profiles: no thread store here, so a thread with a parent is refused: its parent cannot be read",
    );
  }
  const store = Option.isSome(sql)
    ? yield* ProjectionStore.ProjectionStoreV2.pipe(
        Effect.provide(ProjectionStore.layer),
        Effect.provideService(SqlClient.SqlClient, sql.value),
      )
    : undefined;
  // A parent that cannot be read refuses the thread, as a missing one does.
  const findThread: FindThread | undefined = store
    ? (id) => store.findThread(id).pipe(Effect.orElseSucceed(() => null))
    : alone
      ? undefined
      : () => Effect.succeed(null);
  const readSettings = yield* makeMcpProfileSettings;
  const reads = Effect.map(
    readSettings,
    (settings) => (thread: LineageThread) => mcpProfileForThread(settings, thread, findThread),
  );
  return { reads, findThread };
});

/**
 * One settings read, or a refusal, then threads' resolved profiles (or refusals) from it: two
 * threads compared against one file. Settings as {@link makeMcpProfileSettings} reads them; without
 * the settings service (tests), the default settings' (`all`, cut to a profile another machine
 * carried).
 */
export const makeThreadMcpProfileReads = Effect.map(makeLineageReads, ({ reads }) => reads);

/**
 * A thread's resolved profile, or a refusal, from the settings {@link makeMcpProfileSettings}
 * reads on each call; see {@link makeThreadMcpProfileReads}.
 */
export const makeThreadMcpProfiles = Effect.gen(function* () {
  const reads = yield* makeThreadMcpProfileReads;
  return Effect.fn("ClaudeMcpProfile.profileFor")(function* (thread: LineageThread) {
    return yield* (yield* reads)(thread);
  });
});

const NOT_IN_STORE = `T3 cannot read this thread from its store, so its MCP profile cannot be worked out and it cannot start: retry, or ${FIX}.`;

/** {@link makeThreadMcpProfilesById}'s refusal of a thread the store did not give. */
export const threadNotInStore = () => new ClaudeMcpProfileError({ reason: NOT_IN_STORE });
/** True for {@link threadNotInStore}'s refusal. */
export const isNotInStore = (error: ClaudeMcpProfileError): boolean =>
  error.reason === NOT_IN_STORE;

/**
 * A thread's resolved profile, or a refusal, found by its id alone (a Codex thread request carries
 * only the id). A thread the store cannot give refuses, as an unreadable parent does. With no store
 * and no settings service (upstream's adapter tests) it is read as a root thread: `all`.
 */
export const makeThreadMcpProfilesById = Effect.gen(function* () {
  const { reads, findThread } = yield* makeLineageReads;
  return Effect.fn("ClaudeMcpProfile.profileForId")(function* (threadId: ThreadId) {
    const read = yield* reads;
    if (findThread === undefined) {
      return yield* read({
        id: threadId,
        projectId: ProjectId.make("unknown-project"),
        lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: threadId },
      } as LineageThread);
    }
    const thread = yield* findThread(threadId);
    if (thread === null) return yield* threadNotInStore();
    return yield* read(thread);
  });
});

/** A plan with the model policy's Agent and Skill tool hook first among its PreToolUse hooks. */
const withAgentModelPolicy = (
  plan: ClaudeMcpProfilePlan,
  agentPolicy: HookCallbackMatcher,
): ClaudeMcpProfilePlan => ({
  ...plan,
  options: {
    ...plan.options,
    hooks: {
      ...plan.options.hooks,
      PreToolUse: [agentPolicy, ...(plan.options.hooks?.PreToolUse ?? [])],
    },
  },
});

/**
 * The plan key each thread's Claude process last opened with. Its T3 tool groups are part of the
 * key, so a later plan with the same key has the same set as that process. Memory only, bounded
 * like the sets: a thread dropped here just waits for its next process to fix its set.
 */
const openedPlanKeys = new Map<ThreadId, string | undefined>();

/** Keeps the thread's key as its most recently used; beyond the cap, the least recently used goes. */
const keepOpenedPlanKey = (threadId: ThreadId, key: string | undefined) => {
  openedPlanKeys.delete(threadId);
  openedPlanKeys.set(threadId, key);
  for (const oldest of openedPlanKeys.keys()) {
    if (openedPlanKeys.size <= MAX_T3_TOOL_SETS) break;
    openedPlanKeys.delete(oldest);
  }
};

export const __testing = {
  /** Forgets every process's plan key, as after a restart. */
  clearOpenedPlanKeys: () => openedPlanKeys.clear(),
  /** Records that the thread's process opened with `key`, as its options read does. */
  keepOpenedPlanKey,
  /** True while the thread's opened plan key is kept. */
  hasOpenedPlanKey: (threadId: ThreadId) => openedPlanKeys.has(threadId),
};

/**
 * The plan with its T3 tool set fixed when its `options` are first read: ClaudeAdapterV2 reads
 * them only to open a new process, after the old one closed. A turn that keeps the live process (a
 * continuation, the same plan) or is refused before replacing it (background work still runs)
 * leaves that process's set as its turn fixed it, so a running turn keeps its set.
 */
const fixedWhenOpened = (
  plan: ClaudeMcpProfilePlan,
  threadId: ThreadId,
  set: T3ToolSet,
  turn: number,
): ClaudeMcpProfilePlan => {
  const { options, ...rest } = plan;
  return Object.defineProperty({ ...rest }, "options", {
    enumerable: true,
    get: () => {
      fixT3ToolSet(threadId, set, turn);
      keepOpenedPlanKey(threadId, planKey(plan));
      return options;
    },
  }) as ClaudeMcpProfilePlan;
};

/** The thread's Claude plan, or a refusal, from {@link makeThreadMcpProfiles}. */
export const makeClaudeMcpProfiles = Effect.gen(function* () {
  const profileFor = yield* makeThreadMcpProfiles;
  // Every query also gets the model policy's check of Claude's own Agent and Skill tools.
  const agentPolicy = yield* makeClaudeAgentModelPolicyHook;
  const planFor = Effect.fn("ClaudeMcpProfile.planFor")(function* (
    thread: LineageThread,
    overrides: McpOverrides,
  ) {
    // Drawn before the read: a later turn's set stands over this one's, whichever finishes first.
    const turn = nextT3ToolSetTurn();
    // A profile that cannot apply refuses the turn and keeps only the tools every profile keeps,
    // at once: narrowing never waits. A profile that applies is fixed as its process opens.
    const resolved: McpProfileResolved = yield* profileFor(thread).pipe(
      Effect.tapError((refusal) =>
        Effect.sync(() => fixT3ToolSet(thread.id, refusedT3ToolSet(refusal.reason), turn)),
      ),
    );
    const t3ServerNames = Object.keys(overrides.mcpServers ?? {});
    const plan = claudeMcpProfilePlan(resolved.name, resolved.profile, t3ServerNames);
    if (isRefusal(plan)) {
      fixT3ToolSet(thread.id, refusedT3ToolSet(plan.reason), turn);
      return yield* plan;
    }
    const set = t3ToolSetOf(resolved);
    // The same plan as the thread's process opened with: the adapter keeps that process without
    // reading the options, so its set is fixed here, the one that process has (never wider). This
    // is how a good turn after a refused one gets its set back on the process the refusal kept.
    // The thread counts as just used, so the cap drops threads not used for longest first.
    const key = planKey(plan);
    if (openedPlanKeys.has(thread.id) && openedPlanKeys.get(thread.id) === key) {
      keepOpenedPlanKey(thread.id, key);
      fixT3ToolSet(thread.id, set, turn);
    }
    // The hooks go in before the options are made lazy, so adding them reads nothing early.
    const hooked = withBackgroundAgentGuidance(
      withAgentModelPolicy(plan, yield* agentPolicy),
      thread,
      overrides,
    );
    return fixedWhenOpened(hooked, thread.id, set, turn);
  });

  return Effect.fn("ClaudeMcpProfile.plan")(function* (
    thread: LineageThread,
    overrides: McpOverrides,
  ) {
    const plan = yield* planFor(thread, overrides);
    PLAN_KEYS.set(overrides, { key: planKey(plan) });
    return plan;
  });
});

/** How long the started session may take to list its servers; Claude's own boot is bounded alike. */
export const CHECK_SESSION_TIMEOUT = Duration.minutes(5);

/**
 * Refuses and closes a started session that shows a server the plan leaves out, or that does not
 * list its servers in time: with a profile set, an unchecked session never starts the turn.
 */
export const checkClaudeSession =
  (plan: ClaudeMcpProfilePlan, overrides: McpOverrides) =>
  <E>(session: {
    readonly mcpServerStatus: Effect.Effect<ReadonlyArray<McpServerStatus>, E>;
    readonly close: Effect.Effect<void, E>;
  }): Effect.Effect<void, ClaudeMcpProfileError> =>
    Effect.gen(function* () {
      if (plan.servers === "all") return;
      const t3ServerNames = Object.keys(overrides.mcpServers ?? {});
      const outside = yield* session.mcpServerStatus.pipe(
        Effect.map((statuses) => claudeMcpServersOutsidePlan(plan, t3ServerNames, statuses)),
        Effect.timeout(CHECK_SESSION_TIMEOUT),
        Effect.orElseSucceed(() => undefined),
      );
      if (outside?.length === 0) return;
      yield* session.close.pipe(Effect.ignore);
      return yield* new ClaudeMcpProfileError({
        reason:
          outside === undefined
            ? `T3 could not check which MCP servers Claude loaded for profile "${plan.name}", so this thread cannot start: retry, or ${FIX}.`
            : `MCP profile "${plan.name}" could not be applied: Claude still loaded ${outside.join(", ")}, so this thread cannot start. ${START_LEAKS}; check them, or ${FIX}.`,
      });
    }).pipe(Effect.withSpan("ClaudeMcpProfile.checkSession"));
