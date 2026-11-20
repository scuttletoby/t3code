/**
 * Fork: a thread's MCP profile, applied to its Claude session. This applies the outside servers
 * only; T3's own tools are filtered in a later PR. A profile that cannot be applied refuses the
 * turn instead of starting it with every tool: the owner chose refusing over warning.
 */
import type {
  HookCallback,
  HookCallbackMatcher,
  HookEvent,
  McpServerStatus,
  Settings as ClaudeSettings,
} from "@anthropic-ai/claude-agent-sdk";
import {
  DEFAULT_SERVER_SETTINGS,
  MCP_CARRIED_PROFILES_REFUSE,
  MCP_PROFILE_ALL,
  type OrchestrationV2AppThread,
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
import { readMcpProfileKeysFromText, withMcpProfileFileKeys } from "./mcpProfileSettingsText.ts";

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
  };
  readonly servers: McpProfileSet["servers"];
}

export type LineageThread = Pick<
  OrchestrationV2AppThread,
  "id" | "projectId" | "lineage" | "delegatedFrom"
>;

const ALL_PLAN: ClaudeMcpProfilePlan = { name: MCP_PROFILE_ALL, options: {}, servers: "all" };
const FIX = `pick "${MCP_PROFILE_ALL}" for this thread, or fix the profile in Settings`;
/** What can make Claude load a server T3's options leave out, at start. */
const START_LEAKS =
  "A managed Claude policy on this machine or --mcp-config in the Claude launch arguments can add servers T3's list does not drop";

/** `t3-only` and an empty list ignore every server Claude would load itself; a list keeps those named. */
export function claudeMcpProfilePlan(
  name: string,
  profile: McpProfileSet,
  t3ServerNames: ReadonlyArray<string>,
): ClaudeMcpProfilePlan | ClaudeMcpProfileError {
  if (profile.t3Tools !== "all") {
    return new ClaudeMcpProfileError({
      reason: `MCP profile "${name}" limits T3's own tools, which this version cannot apply yet, so this thread cannot start: set its t3Tools to "all", or ${FIX}.`,
    });
  }
  if (profile.servers === "all") return { ...ALL_PLAN, name };
  const servers = profile.servers;
  const hooks = { PreToolUse: [{ hooks: [claudeMcpProfileGuard(name, servers, t3ServerNames)] }] };
  if (servers.length === 0) {
    return { name, options: { strictMcpConfig: true, hooks }, servers };
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
 * Refuses, at the moment of the call, a tool call to an MCP server the profile leaves out. The
 * options keep such servers from loading; this covers what loads anyway (a managed policy that
 * drops T3's allowlist, an agent file's servers, a server added mid-session). Hooks run in every
 * permission mode and inside sub-agents.
 */
const claudeMcpProfileGuard =
  (
    name: string,
    servers: ReadonlyArray<string>,
    t3ServerNames: ReadonlyArray<string>,
  ): HookCallback =>
  async (input) => {
    if (input.hook_event_name !== "PreToolUse") return {};
    const server = claudeMcpCallOutsideProfile(servers, t3ServerNames, input);
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
const mcpProfileForThread = Effect.fn("ClaudeMcpProfile.profileForThread")(function* (
  settings: ServerSettings,
  thread: LineageThread,
  findThread: FindThread | undefined,
) {
  const lineage: Array<LineageThread> = [thread];
  let parentMissing = false;
  // Parents are read even with no profile set here: one of them may be a task another machine
  // delegated, which carries the profile of its thread there.
  let parentId = thread.lineage.parentThreadId;
  // One past the limit, or a repeat, is kept so the resolver refuses it.
  while (parentId !== null && lineage.length <= MCP_PROFILE_MAX_LINEAGE) {
    const repeated = lineage.find((entry) => entry.id === parentId);
    const parent = repeated ?? (findThread === undefined ? null : yield* findThread(parentId));
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
let unplanned = 0;

/**
 * The plan's part of the live-query key (`undefined` for `all`), found by the turn's MCP overrides,
 * the only input of upstream's key line. Overrides with no plan get a key no other turn has, so
 * such a turn opens a new process instead of reusing one a wider profile started.
 */
export const planKeyFor = (mcpOverrides: object): string | undefined =>
  (PLAN_KEYS.get(mcpOverrides) ?? { key: `unplanned\u0000${++unplanned}` }).key;

/** The plan's key part, or `undefined` for `all`. NUL keeps the joined names apart. */
const planKey = (plan: ClaudeMcpProfilePlan): string | undefined =>
  plan.servers === "all" ? undefined : ["mcp", ...plan.servers].join("\u0000");

/** settings.json as a load decodes it, with the policy's locks laid over. */
type SettingsFileRead =
  | {
      readonly readable: true;
      /** The file's settings; no file gives the defaults. */
      readonly settings: ServerSettings;
    }
  | {
      /** A file that does not decode (also an empty one) or whose legacy keys do not. */
      readonly readable: false;
      /**
       * `base` with the MCP profile keys of the file's text (none when it names none: `base`'s
       * stand) and `mcpCarriedProfiles` `refuse` (the file may say so), the policy's locks laid
       * over; why not, when the text names a key but cannot be read.
       */
      readonly over: (base: ServerSettings) => ServerSettings | { readonly unreadable: string };
      /** The policy's defaults, a base for {@link over} when the settings service cannot read. */
      readonly defaults: ServerSettings;
    };

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
  return Effect.gen(function* () {
    if (!(yield* fs.exists(settingsPath))) {
      return { readable: true, settings: managed.applyLocks(managed.defaults) };
    }
    const raw = yield* fs.readFileString(settingsPath);
    const decoded = ServerSettingsService.decodeSettingsFileText(raw, managed.withDefaults);
    if (Exit.isSuccess(decoded) && !ServerSettingsService.settingsFileLegacyKeysUnreadable(raw)) {
      return { readable: true, settings: managed.applyLocks(decoded.value) };
    }
    const keys = readMcpProfileKeysFromText(raw, managed.withDefaults);
    return {
      readable: false,
      over: (base) =>
        keys.ok
          ? managed.applyLocks({
              ...(keys.keys === undefined ? base : withMcpProfileFileKeys(base, keys.keys)),
              mcpCarriedProfiles: MCP_CARRIED_PROFILES_REFUSE,
            })
          : { unreadable: keys.issue },
      defaults: managed.defaults,
    };
  }) satisfies Effect.Effect<SettingsFileRead, unknown>;
});

const cannotReadSettings = () =>
  new ClaudeMcpProfileError({
    reason: `T3 could not read this thread's MCP profile settings, so this thread cannot start: retry, or check that T3's settings.json can be read.`,
  });

/** The refusal for a settings.json whose text cannot say which MCP profile a thread has. */
const unreadableSettingsFile = (issue: string) =>
  new ClaudeMcpProfileError({
    reason: `T3's settings.json sets MCP profiles but cannot be read (${issue}), so this thread's MCP profile cannot be worked out and it cannot start: fix settings.json (the server log says what is wrong); the next turn reads it again.`,
  });

/**
 * The settings MCP profiles read (Claude turns, delegations, launches, the features route), or a
 * refusal. A failed read falls back to settings.json alone, else refuses. A settings.json that
 * does not decode, also one broken at load, gives the MCP profile keys its text holds, and
 * `mcpCarriedProfiles` reads as `refuse`, as it may say so; text that cannot say refuses.
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
  const readSettingsFile = yield* settingsFileReader;
  return Effect.gen(function* () {
    const read = yield* service.getSettings.pipe(Effect.result);
    if (readSettingsFile === undefined) {
      if (read._tag === "Success") return read.success;
      yield* Effect.logWarning("MCP profiles: the settings could not be read", {
        cause: read.failure,
      });
      return yield* cannotReadSettings();
    }
    const fileRead = yield* readSettingsFile.pipe(Effect.result);
    if (fileRead._tag === "Failure") {
      // The service's settings may be a broken file's defaults from an earlier load, so they do
      // not count while the file cannot be read: never `all` on a guess.
      yield* Effect.logWarning("MCP profiles: settings.json could not be read", {
        cause: fileRead.failure,
      });
      return yield* cannotReadSettings();
    }
    const file = fileRead.success;
    if (file.readable) {
      if (read._tag === "Success") return read.success;
      yield* Effect.logWarning(
        "MCP profiles: settings could not be read in full, so the MCP profile settings come from settings.json alone",
        { cause: read.failure },
      );
      return file.settings;
    }
    // The service gave every setting its default for this file (or still holds the last load's);
    // the MCP profile keys the text holds replace the service's, never widened to `all`.
    const settings = file.over(read._tag === "Success" ? read.success : file.defaults);
    if ("unreadable" in settings) {
      yield* Effect.logWarning(
        "MCP profiles: settings.json does not decode and its text cannot say which MCP profile a thread has, so threads are refused until it is fixed",
        { issue: settings.unreadable },
      );
      return yield* unreadableSettingsFile(settings.unreadable);
    }
    yield* Effect.logWarning(
      "MCP profiles: settings.json does not decode, so the MCP profile settings come from its text, and carried MCP profiles read as refuse",
    );
    return settings;
  }).pipe(Effect.withSpan("ClaudeMcpProfile.readSettings"));
});

type McpOverrides = { readonly mcpServers?: Readonly<Record<string, unknown>> | undefined };

/**
 * A thread's resolved profile, or a refusal, from the settings {@link makeMcpProfileSettings}
 * reads; without the settings service (tests), the default settings' (`all`, cut to a profile
 * another machine carried).
 */
export const makeThreadMcpProfiles = Effect.gen(function* () {
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
  return Effect.fn("ClaudeMcpProfile.profileFor")(function* (thread: LineageThread) {
    return yield* mcpProfileForThread(yield* readSettings, thread, findThread);
  });
});

/** The thread's Claude plan, or a refusal, from {@link makeThreadMcpProfiles}. */
export const makeClaudeMcpProfiles = Effect.gen(function* () {
  const profileFor = yield* makeThreadMcpProfiles;
  const planFor = Effect.fn("ClaudeMcpProfile.planFor")(function* (
    thread: LineageThread,
    overrides: McpOverrides,
  ) {
    const resolved: McpProfileResolved = yield* profileFor(thread);
    const t3ServerNames = Object.keys(overrides.mcpServers ?? {});
    const plan = claudeMcpProfilePlan(resolved.name, resolved.profile, t3ServerNames);
    return isRefusal(plan) ? yield* plan : plan;
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
