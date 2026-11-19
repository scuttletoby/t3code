/**
 * Fork: a thread's MCP profile, applied to its Claude session. This applies the outside servers
 * only; T3's own tools are filtered in a later PR. A profile that cannot be applied refuses the
 * turn instead of starting it with every tool: the owner chose refusing over warning.
 */
import type { McpServerStatus, Settings as ClaudeSettings } from "@anthropic-ai/claude-agent-sdk";
import {
  MCP_PROFILE_ALL,
  MCP_SUBAGENT_INHERIT,
  type OrchestrationV2AppThread,
  type ServerSettings,
  type ThreadId,
  isDelegatedAppThread,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";

import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import {
  MCP_PROFILE_MAX_LINEAGE,
  type McpProfileSet,
  type McpProfileThread,
  resolveMcpProfile,
} from "./mcpProfile.ts";

export class ClaudeMcpProfileError extends Schema.TaggedError<ClaudeMcpProfileError>()(
  "ClaudeMcpProfileError",
  { reason: Schema.String },
) {
  override get message(): string {
    return this.reason;
  }
}

const isRefusal = Schema.is(ClaudeMcpProfileError);

/** The SDK options a profile adds, and the outside servers the session may then show. */
export interface ClaudeMcpProfilePlan {
  readonly name: string;
  readonly options: {
    readonly strictMcpConfig?: true;
    readonly managedSettings?: ClaudeSettings;
  };
  readonly servers: McpProfileSet["servers"];
}

export type LineageThread = Pick<
  OrchestrationV2AppThread,
  "id" | "projectId" | "lineage" | "delegatedFrom"
>;

const ALL_PLAN: ClaudeMcpProfilePlan = { name: MCP_PROFILE_ALL, options: {}, servers: "all" };
const FIX = `pick "${MCP_PROFILE_ALL}" for this thread, or fix the profile in Settings`;
const blank = (value: string | undefined) => value === undefined || value.trim() === "";

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
  if (profile.servers.length === 0) {
    return { name, options: { strictMcpConfig: true }, servers: [] };
  }
  // T3's own servers are listed too, so the allowlist cannot drop them.
  const allowed = [...new Set([...t3ServerNames, ...profile.servers])];
  return {
    name,
    options: {
      managedSettings: { allowedMcpServers: allowed.map((serverName) => ({ serverName })) },
    },
    servers: profile.servers,
  };
}

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

/** With no thread picks, project values or sub-agent rule, every thread in a lineage gets `mcpProfile`. */
const needsLineage = (settings: ServerSettings) =>
  Object.keys(settings.mcpThreadProfiles).length > 0 ||
  !(
    blank(settings.mcpSubagentProfile) ||
    settings.mcpSubagentProfile.trim() === MCP_SUBAGENT_INHERIT
  ) ||
  Object.values(settings.projectSettingsOverrides).some(
    (project) => !blank(project.mcpProfile) || !blank(project.mcpSubagentProfile),
  );

const profileThread = (settings: ServerSettings, thread: LineageThread): McpProfileThread => {
  const project = settings.projectSettingsOverrides[thread.projectId];
  return {
    threadId: thread.id,
    delegated: isDelegatedAppThread(thread),
    project: { mcpProfile: project?.mcpProfile, mcpSubagentProfile: project?.mcpSubagentProfile },
  };
};

/** The thread's profile as a Claude plan. `findThread` is `undefined` when no store is wired. */
export const claudeMcpProfileForThread = Effect.fn("ClaudeMcpProfile.forThread")(function* (
  settings: ServerSettings,
  thread: LineageThread,
  t3ServerNames: ReadonlyArray<string>,
  findThread: ((threadId: ThreadId) => Effect.Effect<LineageThread | null>) | undefined,
) {
  const lineage: Array<LineageThread> = [thread];
  let parentMissing = false;
  let parentId = needsLineage(settings) ? thread.lineage.parentThreadId : null;
  // One past the limit, or a repeat, is kept so the resolver refuses it.
  while (parentId !== null && lineage.length <= MCP_PROFILE_MAX_LINEAGE) {
    const repeated = lineage.find((entry) => entry.id === parentId);
    const parent = repeated ?? (findThread === undefined ? null : yield* findThread(parentId));
    parentMissing = parent === null;
    if (parent === null) break;
    lineage.push(parent);
    parentId = repeated === undefined ? parent.lineage.parentThreadId : null;
  }
  const resolved = resolveMcpProfile(
    settings,
    lineage.map((entry) => profileThread(settings, entry)),
  );
  if (resolved._tag === "CannotApply") {
    return yield* new ClaudeMcpProfileError({ reason: resolved.message });
  }
  // A parent that is gone (or on another machine) cannot cut the profile down.
  if (parentMissing && resolved.source !== "thread-pick") {
    return yield* new ClaudeMcpProfileError({
      reason: `This thread's parent thread cannot be read, so its MCP profile cannot be worked out: ${FIX}.`,
    });
  }
  const plan = claudeMcpProfilePlan(resolved.name, resolved.profile, t3ServerNames);
  return isRefusal(plan) ? yield* plan : plan;
});

const PLAN_KEYS = new WeakMap<object, string>();

/**
 * The plan's part of the live-query key, by the turn's MCP overrides object, so a profile change
 * opens a new Claude process. `undefined` for `all`, which leaves the key as it was.
 */
export const planKeyFor = (mcpOverrides: object): string | undefined => PLAN_KEYS.get(mcpOverrides);

type McpOverrides = { readonly mcpServers?: Readonly<Record<string, unknown>> | undefined };

/** The thread's plan, or a refusal. Without the settings service (tests), every thread gets `all`. */
export const makeClaudeMcpProfiles = Effect.gen(function* () {
  const settingsService = yield* Effect.serviceOption(ServerSettingsService.ServerSettingsService);
  const sql = yield* Effect.serviceOption(SqlClient.SqlClient);
  const store = Option.isSome(sql)
    ? yield* ProjectionStore.ProjectionStoreV2.pipe(
        Effect.provide(ProjectionStore.layer),
        Effect.provideService(SqlClient.SqlClient, sql.value),
      )
    : undefined;
  // A parent that cannot be read refuses the thread, as a missing one does.
  const findThread =
    store && ((id: ThreadId) => store.findThread(id).pipe(Effect.orElseSucceed(() => null)));
  return Effect.fn("ClaudeMcpProfile.plan")(function* (
    thread: LineageThread,
    overrides: McpOverrides,
  ) {
    if (Option.isNone(settingsService)) return ALL_PLAN;
    const settings = yield* settingsService.value.getSettings.pipe(
      Effect.mapError(
        () =>
          new ClaudeMcpProfileError({
            reason: `T3 could not read this thread's MCP profile settings, so this thread cannot start: retry, or ${FIX}.`,
          }),
      ),
    );
    const t3ServerNames = Object.keys(overrides.mcpServers ?? {});
    const plan = yield* claudeMcpProfileForThread(settings, thread, t3ServerNames, findThread);
    // NUL keeps the joined names apart.
    if (plan.servers !== "all") PLAN_KEYS.set(overrides, ["mcp", ...plan.servers].join("\u0000"));
    return plan;
  });
});

/** Refuses and closes a started session that shows a server the plan leaves out. */
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
        Effect.orElseSucceed(() => undefined),
      );
      if (outside?.length === 0) return;
      yield* session.close.pipe(Effect.ignore);
      return yield* new ClaudeMcpProfileError({
        reason:
          outside === undefined
            ? `T3 could not check which MCP servers Claude loaded for profile "${plan.name}", so this thread cannot start: retry, or ${FIX}.`
            : `MCP profile "${plan.name}" could not be applied: Claude still loaded ${outside.join(", ")}, so this thread cannot start. A managed Claude policy on this machine can override T3's server list; check it, or ${FIX}.`,
      });
    }).pipe(Effect.withSpan("ClaudeMcpProfile.checkSession"));
