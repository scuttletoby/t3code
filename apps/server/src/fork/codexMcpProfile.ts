/**
 * Fork: MCP profiles on Codex (design part 5). Each thread on the shared app-server gets
 * `mcp_servers.<name>.enabled = false` for every listed server its profile leaves out; each turn
 * checks the thread's live servers, refuses on a mismatch, and fixes its T3 tool groups (cut on
 * T3's MCP server, mcpT3ToolFilter.ts), also on a turn Codex starts itself.
 */
import { MCP_BUILTIN_TOOLS_NONE, MCP_PROFILE_ALL, ThreadId } from "@t3tools/contracts";
import type * as CodexClient from "effect-codex-app-server/client";
import * as CodexError from "effect-codex-app-server/errors";
import type * as CodexSchema from "effect-codex-app-server/schema";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import type { CodexAppServerClientFactoryShape } from "../orchestration-v2/Adapters/CodexAdapterV2.ts";
import { AGENT_THREAD_ENV } from "../preview/PortScanner.ts";
import { ClaudeMcpProfileError, makeThreadMcpProfilesById } from "./claudeMcpProfile.ts";
import {
  type McpProfileResolved,
  type McpProfileSet,
  builtinToolsNoneRefusal,
} from "./mcpProfile.ts";
import { fixT3ToolSet, refusedT3ToolSet, t3ToolSetOf } from "./mcpT3ToolSets.ts";

type Client = CodexClient.CodexAppServerClient["Service"];
type Send = (
  method: string,
  params: unknown,
) => Effect.Effect<unknown, CodexError.CodexAppServerError>;
/** A thread's resolved profile, or a refusal, by its T3 thread id. */
export type ProfileById = (
  threadId: ThreadId,
) => Effect.Effect<McpProfileResolved, ClaudeMcpProfileError>;

/** T3's own servers (CodexAdapterV2 sends them per thread); a profile never turns them off. */
const CODEX_T3_MCP_SERVERS: ReadonlySet<string> = new Set(["t3-code", "t3-code-direct"]);
/**
 * Codex's built-in connectors server. Codex adds it (with ChatGPT sign-in) only while its `apps`
 * feature is on, and config/read never lists it; `mcp_servers.codex_apps.enabled = false` is
 * refused as an invalid transport, so a profile that leaves it out switches the feature off.
 */
const CODEX_APPS_MCP_SERVER = "codex_apps";
const CODEX_APPS_OFF = "features.apps";
/** The per-thread config key CodexAdapterV2 puts the T3 thread id in. */
const THREAD_ID_KEY = `shell_environment_policy.set.${AGENT_THREAD_ENV}`;
/** Names Codex accepts for an MCP server; any other could not be written as a dotted key. */
const SERVER_NAME = /^[A-Za-z0-9_-]+$/;
/** How long config/read, thread/read or mcpServerStatus/list may take before the turn refuses. */
const CODEX_READ_TIMEOUT = "30 seconds";
/** Status pages read before giving up; a page holds every server on Codex today. */
const MAX_STATUS_PAGES = 20;
/** Not -32600: the adapter reads that code with "thread not found:" as an unloaded thread. */
const REFUSAL_CODE = -32001;
const FIX = `pick "${MCP_PROFILE_ALL}" for this thread, or fix the profile in Settings`;
/** What a refusal of a turn Codex started on its own says happened. */
const STOPPED =
  "T3 stopped this turn, which Codex started on its own for the thread's goal, and paused the goal";

const isRefusal = Schema.is(ClaudeMcpProfileError);
const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const stringField = (value: unknown, key: string): string | undefined =>
  isRecord(value) && typeof value[key] === "string" ? value[key] : undefined;

/** Server names in a config/read result's `mcp_servers`; `undefined` when it is not a table. */
export function codexConfiguredMcpServers(config: unknown): ReadonlyArray<string> | undefined {
  if (!isRecord(config)) return undefined;
  const servers = config.mcp_servers;
  if (servers === undefined || servers === null) return [];
  return isRecord(servers) ? Object.keys(servers) : undefined;
}

/**
 * The per-thread config that turns off every configured server the profile does not keep (and
 * Codex's connectors server, see {@link CODEX_APPS_MCP_SERVER}), or the refusal. T3's own servers
 * stay; a kept name that is not configured keeps nothing (design §9). `t3Tools` adds no config:
 * T3's own server hides and refuses the tools it leaves out.
 */
export function codexMcpProfileConfig(
  name: string,
  profile: McpProfileSet,
  configured: ReadonlyArray<string>,
): Readonly<Record<string, false>> | ClaudeMcpProfileError {
  const kept = profile.servers;
  if (kept === "all") return {};
  const off = configured.filter(
    (server) => !CODEX_T3_MCP_SERVERS.has(server) && !kept.includes(server),
  );
  const unnamed = off.filter((server) => !SERVER_NAME.test(server));
  if (unnamed.length > 0) {
    return new ClaudeMcpProfileError({
      reason: `MCP profile "${name}" could not be applied: Codex has the MCP server ${unnamed.map((server) => JSON.stringify(server)).join(", ")} set up, and T3 cannot switch off a server with that name, so this thread cannot start. Rename it in Codex's config, or ${FIX}.`,
    });
  }
  return Object.fromEntries([
    ...off.map((server) => [`mcp_servers.${server}.enabled`, false] as const),
    ...(kept.includes(CODEX_APPS_MCP_SERVER) ? [] : [[CODEX_APPS_OFF, false] as const]),
  ]);
}

/** Servers the thread has that the profile leaves out and Codex did not disable. */
export function codexMcpServersOutsideProfile(
  kept: McpProfileSet["servers"],
  statuses: ReadonlyArray<{ readonly name: string; readonly runtimeStatus?: string | null }>,
): ReadonlyArray<string> {
  if (kept === "all") return [];
  return statuses
    .filter(
      (server) =>
        server.runtimeStatus !== "disabled" &&
        !CODEX_T3_MCP_SERVERS.has(server.name) &&
        !kept.includes(server.name),
    )
    .map((server) => server.name);
}

/** The text a refusal gives the user. */
const reasonOf = (error: CodexError.CodexAppServerError): string =>
  error._tag === "CodexAppServerRequestError" && isRefusal(error.cause)
    ? error.cause.reason
    : `T3 could not check this thread's MCP servers (${error.message}): retry, or ${FIX}.`;

/** A failed request whose reason the user reads (failure.ts shows a ClaudeMcpProfileError cause). */
const refusal = (method: string, cause: ClaudeMcpProfileError) =>
  new CodexError.CodexAppServerRequestError({
    code: REFUSAL_CODE,
    errorMessage: cause.reason,
    method,
    cause,
  });

const LOADS: ReadonlySet<string> = new Set(["thread/start", "thread/resume", "thread/fork"]);

/**
 * The client with the thread's profile applied to each thread it loads and checked before each
 * turn. One per app-server: what it learns (which Codex thread is which T3 thread) ends with it, so
 * a restarted app-server starts empty and every thread is loaded, and checked, again.
 */
export const withCodexMcpProfiles = Effect.fn("CodexMcpProfile.wrap")(function* (
  client: Client,
  profileFor: ProfileById,
) {
  const scope = yield* Effect.scope;
  /**
   * Codex thread id to T3 thread id, for each thread loaded through this client with one and for
   * each Codex sub-agent of such a thread (its parent's T3 thread).
   */
  const known = new Map<string, ThreadId>();
  /** Turns stopped here because their profile was narrower, by Codex turn id: the reason. */
  const stoppedTurns = new Map<string, string>();
  /**
   * T3 threads whose resume a profile check refused, with the reason. T3 answers a failed resume
   * by starting the thread in a fresh Codex session (with a summary, not its history); the next
   * start of such a thread is refused too, once, so a passing hiccup cannot cost the history.
   */
  const refusedResumes = new Map<ThreadId, string>();
  /** Set once a thread here got a profile that leaves out an outside server. */
  let narrowed = false;
  const read = (method: string, params: unknown) =>
    (client.raw.request as unknown as Send)(method, params).pipe(
      Effect.timeout(CODEX_READ_TIMEOUT),
    );

  const profileOf = (method: string, threadId: ThreadId) =>
    profileFor(threadId).pipe(Effect.mapError((cause) => refusal(method, cause)));

  /** The workspace Codex loads the thread in: the request's, else the stored thread's. */
  const workspaceOf = (method: string, params: Readonly<Record<string, unknown>>) => {
    const cwd = stringField(params, "cwd");
    const source = stringField(params, "threadId");
    if (cwd !== undefined || method === "thread/start" || source === undefined) {
      return Effect.succeed(cwd);
    }
    return read("thread/read", { threadId: source, includeTurns: false }).pipe(
      Effect.map((response) =>
        stringField(isRecord(response) ? response.thread : undefined, "cwd"),
      ),
      Effect.orElseSucceed(() => undefined),
    );
  };

  const cannotList = (method: string, name: string) =>
    refusal(
      method,
      new ClaudeMcpProfileError({
        reason: `T3 could not read which MCP servers Codex has set up for profile "${name}", so this thread cannot start: retry, or ${FIX}.`,
      }),
    );

  const load = Effect.fn("CodexMcpProfile.load")(function* (
    send: Send,
    method: string,
    params: unknown,
  ) {
    const request = isRecord(params) ? params : {};
    const config = isRecord(request.config) ? request.config : {};
    const nativeId = method === "thread/resume" ? stringField(request, "threadId") : undefined;
    const fromConfig = stringField(config, THREAD_ID_KEY);
    const threadId =
      fromConfig === undefined
        ? nativeId === undefined
          ? undefined
          : known.get(nativeId)
        : ThreadId.make(fromConfig);
    if (threadId === undefined) {
      // A Codex thread T3 did not load and saw no parent spawn (e.g. a sub-agent from before an
      // app-server restart): its T3 thread is not known, so with a profile in use it is not loaded.
      if (narrowed) {
        return yield* refusal(
          method,
          new ClaudeMcpProfileError({
            reason: `T3 cannot tell which T3 thread this Codex thread belongs to, so its MCP profile cannot be applied and it was not loaded: ${FIX}.`,
          }),
        );
      }
      return yield* send(method, params);
    }
    const resumeRefused = method === "thread/start" ? refusedResumes.get(threadId) : undefined;
    if (resumeRefused !== undefined) {
      refusedResumes.delete(threadId);
      return yield* refusal(
        method,
        new ClaudeMcpProfileError({
          reason: `${resumeRefused} T3 did not move the thread to a new Codex session instead, which would lose its history; retrying the turn resumes it.`,
        }),
      );
    }
    const off = yield* offFor(method, threadId, request).pipe(
      Effect.tapError((error) =>
        method === "thread/resume"
          ? Effect.sync(() => void refusedResumes.set(threadId, reasonOf(error)))
          : Effect.void,
      ),
    );
    const response = yield* send(
      method,
      Object.keys(off).length === 0 ? params : { ...request, config: { ...config, ...off } },
    );
    refusedResumes.delete(threadId);
    const loaded = stringField(isRecord(response) ? response.thread : undefined, "id");
    if (loaded !== undefined) known.set(loaded, threadId);
    return response;
  });

  /** The per-thread config that applies the thread's profile as it loads, or the refusal. */
  const offFor = Effect.fn("CodexMcpProfile.offFor")(function* (
    method: string,
    threadId: ThreadId,
    request: Readonly<Record<string, unknown>>,
  ) {
    const resolved = yield* profileOf(method, threadId);
    let off: Readonly<Record<string, false>> = {};
    if (resolved.profile.servers !== "all") {
      const cwd = yield* workspaceOf(method, request);
      const configured = yield* read("config/read", {
        ...(cwd === undefined ? {} : { cwd }),
        includeLayers: false,
      }).pipe(
        Effect.map((response) =>
          codexConfiguredMcpServers(isRecord(response) ? response.config : undefined),
        ),
        Effect.orElseSucceed(() => undefined),
      );
      if (configured === undefined) return yield* cannotList(method, resolved.name);
      const planned = codexMcpProfileConfig(resolved.name, resolved.profile, configured);
      if (isRefusal(planned)) return yield* refusal(method, planned);
      off = planned;
      narrowed = true;
    }
    return off;
  });

  /** Refuses a turn on a thread whose live servers include one its profile leaves out. */
  const check = Effect.fn("CodexMcpProfile.check")(function* (method: string, params: unknown) {
    const nativeId = stringField(params, "threadId");
    const threadId = nativeId === undefined ? undefined : known.get(nativeId);
    if (threadId === undefined) {
      if (!narrowed) return;
      return yield* refusal(
        method,
        new ClaudeMcpProfileError({
          reason: `T3 did not load this Codex thread itself, so it cannot tell which MCP servers the thread has, and runs no turn on it: ${FIX}.`,
        }),
      );
    }
    // The turn's T3 tool groups, also for a turn Codex started itself (`turn/started`): fixed
    // until the next turn, and only the tools every profile keeps when the profile cannot apply.
    const resolved = yield* profileFor(threadId).pipe(
      Effect.tapError((cause) =>
        Effect.sync(() => fixT3ToolSet(threadId, refusedT3ToolSet(cause.reason))),
      ),
      Effect.mapError((cause) => refusal(method, cause)),
    );
    fixT3ToolSet(threadId, t3ToolSetOf(resolved));
    // Codex cannot run a thread without its shell and file tools; also a turn it starts itself.
    if (resolved.profile.builtinTools === MCP_BUILTIN_TOOLS_NONE) {
      const reason = builtinToolsNoneRefusal(resolved.name, "codex");
      return yield* refusal(method, new ClaudeMcpProfileError({ reason }));
    }
    // A thread loaded narrower than its profile is now stays narrower until Codex reloads it.
    if (resolved.profile.servers === "all") return;
    const statuses: Array<{ readonly name: string; readonly runtimeStatus?: string | null }> = [];
    let cursor: string | undefined;
    for (let page = 0; ; page++) {
      if (page === MAX_STATUS_PAGES) return yield* cannotList(method, resolved.name);
      const response = yield* read("mcpServerStatus/list", {
        threadId: nativeId,
        detail: "toolsAndAuthOnly",
        ...(cursor === undefined ? {} : { cursor }),
      }).pipe(Effect.orElseSucceed(() => undefined));
      const data = isRecord(response) ? response.data : undefined;
      if (!Array.isArray(data)) return yield* cannotList(method, resolved.name);
      for (const entry of data) {
        const name = stringField(entry, "name");
        if (name === undefined) return yield* cannotList(method, resolved.name);
        statuses.push({ name, runtimeStatus: stringField(entry, "runtimeStatus") ?? null });
      }
      cursor = isRecord(response) ? stringField(response, "nextCursor") : undefined;
      if (cursor === undefined) break;
    }
    const outside = codexMcpServersOutsideProfile(resolved.profile.servers, statuses);
    if (outside.length === 0) return;
    return yield* refusal(
      method,
      new ClaudeMcpProfileError({
        reason: `MCP profile "${resolved.name}" could not be applied: Codex still has ${outside.join(", ")} switched on for this thread, so T3 runs no turn on it. Codex sets a thread's MCP servers only when it loads the thread, so a profile changed while the thread was loaded applies once Codex loads it again (after it has been idle for a while, or after T3 restarts); a Codex plugin can also add a server T3 cannot switch off. Until then, ${FIX}.`,
      }),
    );
  });

  /** Model work Codex starts on a thread at this request; a paused goal starts none. */
  const startsWork = (method: string, params: unknown) =>
    method === "turn/start" ||
    (method === "thread/goal/set" && stringField(params, "status") !== "paused");

  const intercept =
    (send: Send): Send =>
    (method, params) =>
      LOADS.has(method)
        ? load(send, method, params)
        : startsWork(method, params)
          ? check(method, params).pipe(Effect.andThen(send(method, params)))
          : send(method, params);

  /**
   * A turn Codex started on its own (a goal's next turn) never passed `turn/start`, so every turn
   * that starts is checked again; one the check refuses is stopped: its goal is paused (so Codex
   * starts no next one) and the turn interrupted, and it ends failed with the reason.
   */
  const watchTurn = (threadId: string, turnId: string) =>
    (known.has(threadId)
      ? check("turn/started", { threadId }).pipe(Effect.asVoid)
      : Effect.void
    ).pipe(
      Effect.catch((error) =>
        Effect.gen(function* () {
          stoppedTurns.set(turnId, `${STOPPED}. ${reasonOf(error)}`);
          const raw = client.raw.request as unknown as Send;
          yield* raw("thread/goal/set", { threadId, status: "paused" }).pipe(Effect.ignore);
          yield* raw("turn/interrupt", { threadId, turnId }).pipe(Effect.ignore);
        }),
      ),
      Effect.forkIn(scope, { startImmediately: true }),
      Effect.asVoid,
    );
  yield* client.handleServerNotification("turn/started", (payload) =>
    watchTurn(payload.threadId, payload.turn.id),
  );

  /**
   * Codex's own sub-agents copy their parent's loaded config, servers switched off included
   * (codex-rs core/src/agent/child_config.rs), and run their own turns; each one is counted as its
   * parent's T3 thread, so those turns are checked against the same profile as the parent's.
   */
  const adoptChildren = (
    payload:
      | CodexSchema.V2ItemStartedNotification["item"]
      | CodexSchema.V2ItemCompletedNotification["item"],
    parent: string,
  ) => {
    const threadId = known.get(parent);
    if (threadId === undefined) return;
    const children =
      payload.type === "collabAgentToolCall"
        ? payload.receiverThreadIds
        : payload.type === "subAgentActivity"
          ? [payload.agentThreadId]
          : [];
    for (const child of children) if (!known.has(child)) known.set(child, threadId);
  };
  yield* client.handleServerNotification("item/started", (payload) =>
    Effect.sync(() => adoptChildren(payload.item, payload.threadId)),
  );
  yield* client.handleServerNotification("item/completed", (payload) =>
    Effect.sync(() => adoptChildren(payload.item, payload.threadId)),
  );

  /** A stopped turn ends failed with the reason, whatever Codex reports, so the user reads why. */
  const completedWithReason = (
    payload: CodexSchema.V2TurnCompletedNotification,
  ): CodexSchema.V2TurnCompletedNotification => {
    const reason = stoppedTurns.get(payload.turn.id);
    if (reason === undefined) return payload;
    stoppedTurns.delete(payload.turn.id);
    return { ...payload, turn: { ...payload.turn, status: "failed", error: { message: reason } } };
  };

  const wrapped: Client = {
    ...client,
    request: intercept(client.request as unknown as Send) as unknown as Client["request"],
    raw: {
      ...client.raw,
      request: intercept(
        client.raw.request as unknown as Send,
      ) as unknown as Client["raw"]["request"],
    },
    handleServerNotification: (method, handler) =>
      method === "turn/completed"
        ? client.handleServerNotification("turn/completed", (payload) =>
            (handler as (p: typeof payload) => ReturnType<typeof handler>)(
              completedWithReason(payload),
            ),
          )
        : client.handleServerNotification(method, handler),
  };
  return wrapped;
});

/**
 * CodexAdapterV2's client factory with each app-server's client wrapped in
 * {@link withCodexMcpProfiles}; spread over the adapter's options, so its key replaces theirs.
 * Typed optional only so the spread may follow upstream's own `clientFactory` key (TS2783); it is
 * always set.
 */
export const profiledClientFactory = Effect.fn("CodexMcpProfile.clientFactory")(function* (
  factory: CodexAppServerClientFactoryShape,
) {
  const profileFor = yield* makeThreadMcpProfilesById;
  const clientFactory: CodexAppServerClientFactoryShape = {
    open: (input) =>
      factory
        .open(input)
        .pipe(Effect.flatMap((client) => withCodexMcpProfiles(client, profileFor))),
  };
  const options: { readonly clientFactory?: CodexAppServerClientFactoryShape } = { clientFactory };
  return options;
});
