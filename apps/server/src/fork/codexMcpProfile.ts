/**
 * Fork: MCP profiles on Codex. Each thread on the shared app-server gets `enabled = false` for
 * each server its profile leaves out (T3's own only when the load carries them); each turn is
 * checked and fixes its T3 tool groups (mcpT3ToolRule.ts). MCP Apps calls are checked too.
 */
import { MCP_BUILTIN_TOOLS_NONE, MCP_PROFILE_ALL, ThreadId } from "@t3tools/contracts";
import type * as CodexClient from "effect-codex-app-server/client";
import * as CodexError from "effect-codex-app-server/errors";
import type * as CodexSchema from "effect-codex-app-server/schema";
import * as Clock from "effect/Clock";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import type { CodexAppServerClientFactoryShape } from "../orchestration-v2/Adapters/CodexAdapterV2.ts";
import { AGENT_THREAD_ENV } from "../preview/PortScanner.ts";
import {
  ClaudeMcpProfileError,
  isNotInStore,
  makeThreadMcpProfilesById,
} from "./claudeMcpProfile.ts";
import {
  type McpProfileResolved,
  type McpProfileSet,
  builtinToolsNoneRefusal,
} from "./mcpProfile.ts";
import {
  fixT3ToolSet,
  holdT3ToolSet,
  nextT3ToolSetTurn,
  refusedT3ToolSet,
  t3ToolSetOf,
} from "./mcpT3ToolSets.ts";
import { CurrentTurnMcpProfile } from "./mcpTurnProfile.ts";

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
 * T3's own servers a load request carries (CodexAdapterV2 sends each as `mcp_servers.<name>`).
 * Only those are T3's: a server of the user's that has one of their names is not.
 */
export function codexCarriedT3McpServers(config: unknown): ReadonlySet<string> {
  if (!isRecord(config)) return new Set();
  return new Set(
    [...CODEX_T3_MCP_SERVERS].filter((name) => isRecord(config[`mcp_servers.${name}`])),
  );
}

/**
 * The per-thread config that turns off every configured server the profile does not keep (and
 * Codex's connectors server, see {@link CODEX_APPS_MCP_SERVER}), or the refusal. T3's own servers
 * the thread carries stay; a kept name that is not configured keeps nothing (design §9). `t3Tools`
 * adds no config: T3's own server hides and refuses the tools it leaves out.
 */
export function codexMcpProfileConfig(
  name: string,
  profile: McpProfileSet,
  configured: ReadonlyArray<string>,
  carried: ReadonlySet<string> = CODEX_T3_MCP_SERVERS,
): Readonly<Record<string, false>> | ClaudeMcpProfileError {
  const kept = profile.servers;
  if (kept === "all") return {};
  const off = configured.filter((server) => !carried.has(server) && !kept.includes(server));
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
  carried: ReadonlySet<string> = CODEX_T3_MCP_SERVERS,
): ReadonlyArray<string> {
  if (kept === "all") return [];
  return statuses
    .filter(
      (server) =>
        server.runtimeStatus !== "disabled" &&
        !carried.has(server.name) &&
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
/** Requests the UI sends to one MCP server of a thread (MCP Apps); checked like a tool call. */
const SERVER_CALLS: ReadonlySet<string> = new Set([
  "mcpServer/tool/call",
  "mcpServer/resource/read",
]);
const ELICITATION = "mcpServer/elicitation/request";
/** How long a turn on a Codex thread T3 has not seen waits to be counted as a sub-agent's. */
export const CODEX_ADOPT_WAIT_MS = 5_000;
const ADOPT_POLL_MS = 250;
/** T3-started turns remembered as checked, until Codex reports them started. */
const MAX_OWN_TURNS = 256;

/** The Codex thread's parent, from a thread/read answer (a sub-agent's spawn source). */
const parentOf = (response: unknown): string | undefined => {
  const thread = isRecord(response) ? response.thread : undefined;
  const direct = stringField(thread, "parentThreadId");
  if (direct !== undefined) return direct;
  const source = isRecord(thread) ? thread.source : undefined;
  const subAgent = isRecord(source) ? source.subAgent : undefined;
  const spawn = isRecord(subAgent) ? subAgent.thread_spawn : undefined;
  return stringField(spawn, "parent_thread_id");
};

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
  /** Codex sub-agents in `known`: their turns are checked but never fix their parent's T3 set. */
  const children = new Set<string>();
  /** T3's own servers each Codex thread was loaded with; a sub-agent's are its parent's. */
  const carried = new Map<string, ReadonlySet<string>>();
  const carriedBy = (nativeId: string | undefined) =>
    (nativeId === undefined ? undefined : carried.get(nativeId)) ?? CODEX_T3_MCP_SERVERS;
  /** Codex threads with a T3 `turn/start` sent and not yet answered (its turn is checked). */
  const starting = new Set<string>();
  /** Turns T3 started itself and checked before sending, until Codex reports them started. */
  const ownTurns = new Set<string>();
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

  /** The thread's profile: the one the turn check worked out for this turn, else worked out now. */
  const workedOut = Effect.fn("CodexMcpProfile.workedOut")(function* (threadId: ThreadId) {
    const current = yield* CurrentTurnMcpProfile;
    if (current?.threadId === threadId) return current.resolved;
    return yield* profileFor(threadId);
  });
  const profileOf = (method: string, threadId: ThreadId) =>
    workedOut(threadId).pipe(Effect.mapError((cause) => refusal(method, cause)));

  /** Counts a Codex sub-agent as its parent's T3 thread, with the parent's servers. */
  const adopt = (child: string, parent: string) => {
    const threadId = known.get(parent);
    if (threadId === undefined || known.has(child)) return;
    known.set(child, threadId);
    children.add(child);
    carried.set(child, carriedBy(parent));
  };
  /** Adopts the Codex thread when Codex says it is a sub-agent of a thread T3 knows. */
  const adoptByRead = (nativeId: string) =>
    read("thread/read", { threadId: nativeId, includeTurns: false }).pipe(
      Effect.map((response) => {
        const parent = parentOf(response);
        if (parent !== undefined) adopt(nativeId, parent);
      }),
      Effect.ignore,
    );

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
    // A sub-agent T3 did not see spawned (one from before an app-server restart) is its parent's.
    if (fromConfig === undefined && nativeId !== undefined && !known.has(nativeId) && narrowed) {
      yield* adoptByRead(nativeId);
    }
    const threadId =
      fromConfig === undefined
        ? nativeId === undefined
          ? undefined
          : known.get(nativeId)
        : ThreadId.make(fromConfig);
    if (threadId === undefined) {
      // A Codex thread T3 did not load and whose parent it does not know: its T3 thread is not
      // known, so with a profile in use it is not loaded.
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
    // A resume without T3's servers (an adopted sub-agent's) keeps the ones it was loaded with.
    const carriedHere =
      fromConfig === undefined && nativeId !== undefined
        ? carriedBy(nativeId)
        : codexCarriedT3McpServers(config);
    const off = yield* offFor(method, threadId, request, carriedHere).pipe(
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
    if (loaded !== undefined) {
      known.set(loaded, threadId);
      carried.set(loaded, carriedHere);
    }
    return response;
  });

  /** The per-thread config that applies the thread's profile as it loads, or the refusal. */
  const offFor = Effect.fn("CodexMcpProfile.offFor")(function* (
    method: string,
    threadId: ThreadId,
    request: Readonly<Record<string, unknown>>,
    carriedHere: ReadonlySet<string>,
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
      const planned = codexMcpProfileConfig(
        resolved.name,
        resolved.profile,
        configured,
        carriedHere,
      );
      if (isRefusal(planned)) return yield* refusal(method, planned);
      off = planned;
      narrowed = true;
    }
    return off;
  });

  /**
   * The thread's profile, with its T3 tool groups fixed when `turn` is given (a turn of the thread
   * itself, not of a sub-agent, and not a goal set while a turn may run): fixed until the next
   * turn, and only the tools every profile keeps when the profile cannot apply. `turn` is drawn
   * before the profile is read, so a check that finishes after a later turn's fixes nothing.
   */
  const profileStep = (method: string, threadId: ThreadId, turn: number | undefined) =>
    workedOut(threadId).pipe(
      Effect.tap((resolved) =>
        turn === undefined
          ? Effect.void
          : Effect.sync(() => fixT3ToolSet(threadId, t3ToolSetOf(resolved), turn)),
      ),
      Effect.tapError((cause) =>
        turn === undefined
          ? Effect.void
          : Effect.sync(() => fixT3ToolSet(threadId, refusedT3ToolSet(cause.reason), turn)),
      ),
      Effect.mapError((cause) => refusal(method, cause)),
    );

  /** Refuses a turn on a thread whose live servers include one its profile leaves out. */
  const serverStep = Effect.fn("CodexMcpProfile.servers")(function* (
    method: string,
    nativeId: string,
    resolved: McpProfileResolved,
  ) {
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
    const outside = codexMcpServersOutsideProfile(
      resolved.profile.servers,
      statuses,
      carriedBy(nativeId),
    );
    if (outside.length === 0) return;
    return yield* refusal(
      method,
      new ClaudeMcpProfileError({
        reason: `MCP profile "${resolved.name}" could not be applied: Codex still has ${outside.join(", ")} switched on for this thread, so T3 runs no turn on it. Codex sets a thread's MCP servers only when it loads the thread, so a profile changed while the thread was loaded applies once Codex loads it again (after it has been idle for a while, or after T3 restarts); a Codex plugin can also add a server T3 cannot switch off. Until then, ${FIX}.`,
      }),
    );
  });

  const notLoadedHere = (method: string) =>
    refusal(
      method,
      new ClaudeMcpProfileError({
        reason: `T3 did not load this Codex thread itself, so it cannot tell which MCP servers the thread has, and runs no turn on it: ${FIX}.`,
      }),
    );

  /** The turn's checks: its profile (`fix`: see {@link profileStep}), then its live servers. */
  const check = Effect.fn("CodexMcpProfile.check")(function* (
    method: string,
    nativeId: string | undefined,
    fix: boolean,
  ) {
    const threadId = nativeId === undefined ? undefined : known.get(nativeId);
    if (nativeId === undefined || threadId === undefined) {
      if (!narrowed) return;
      return yield* notLoadedHere(method);
    }
    const turn = fix && !children.has(nativeId) ? nextT3ToolSetTurn() : undefined;
    const resolved = yield* profileStep(method, threadId, turn);
    yield* serverStep(method, nativeId, resolved);
  });

  /** T3's own turn: checked, then remembered so its `turn/started` is not checked again. */
  const turnStart = Effect.fn("CodexMcpProfile.turnStart")(function* (send: Send, params: unknown) {
    const nativeId = stringField(params, "threadId");
    yield* check("turn/start", nativeId, true);
    if (nativeId === undefined) return yield* send("turn/start", params);
    starting.add(nativeId);
    const response = yield* send("turn/start", params).pipe(
      Effect.ensuring(Effect.sync(() => starting.delete(nativeId))),
    );
    const turnId = stringField(isRecord(response) ? response.turn : undefined, "id");
    if (turnId !== undefined) {
      ownTurns.add(turnId);
      for (const oldest of ownTurns) {
        if (ownTurns.size <= MAX_OWN_TURNS) break;
        ownTurns.delete(oldest);
      }
    }
    return response;
  });

  /**
   * A UI call to one of the thread's MCP servers (MCP Apps), or an elicitation one asks for: refused
   * when the thread's profile leaves the server out, or when T3 cannot tell the thread's profile
   * while a narrower one is in use. Read now, not at the turn: no turn runs it.
   */
  const guardServer = Effect.fn("CodexMcpProfile.guardServer")(function* (
    method: string,
    nativeId: string | undefined,
    server: string | undefined,
  ) {
    const threadId = nativeId === undefined ? undefined : known.get(nativeId);
    const named = server === undefined ? "an MCP server" : `MCP server "${server}"`;
    if (threadId === undefined) {
      if (!narrowed) return;
      return yield* refusal(
        method,
        new ClaudeMcpProfileError({
          reason: `T3 did not load this Codex thread itself, so it cannot tell which MCP servers the thread may use, and refused this request to ${named}: ${FIX}.`,
        }),
      );
    }
    const resolved = yield* profileOf(method, threadId);
    const kept = resolved.profile.servers;
    if (kept === "all") return;
    if (server !== undefined && (carriedBy(nativeId).has(server) || kept.includes(server))) return;
    return yield* refusal(
      method,
      new ClaudeMcpProfileError({
        reason: `${named[0]!.toUpperCase()}${named.slice(1)} is not in this thread's MCP profile "${resolved.name}", so T3 refused this request to it: ${FIX}.`,
      }),
    );
  });

  /** Model work Codex starts on a thread at this request; a paused goal starts none. */
  const setsGoal = (method: string, params: unknown) =>
    method === "thread/goal/set" && stringField(params, "status") !== "paused";

  const intercept =
    (send: Send): Send =>
    (method, params) =>
      LOADS.has(method)
        ? load(send, method, params)
        : method === "turn/start"
          ? turnStart(send, params)
          : setsGoal(method, params)
            ? // The goal's turn fixes the set as it starts; a turn may still be running now.
              check(method, stringField(params, "threadId"), false).pipe(
                Effect.andThen(send(method, params)),
              )
            : SERVER_CALLS.has(method)
              ? guardServer(
                  method,
                  stringField(params, "threadId") ?? undefined,
                  stringField(params, "server"),
                ).pipe(Effect.andThen(send(method, params)))
              : send(method, params);

  /** A refused turn Codex started is stopped: its goal paused, then the turn interrupted. */
  const stop = (nativeId: string, turnId: string) => (error: CodexError.CodexAppServerError) =>
    Effect.gen(function* () {
      stoppedTurns.set(turnId, `${STOPPED}. ${reasonOf(error)}`);
      const raw = client.raw.request as unknown as Send;
      yield* raw("thread/goal/set", { threadId: nativeId, status: "paused" }).pipe(Effect.ignore);
      yield* raw("turn/interrupt", { threadId: nativeId, turnId }).pipe(Effect.ignore);
    });

  /**
   * A turn on a Codex thread T3 has not seen, while a narrower profile is in use: counted as a
   * sub-agent's when Codex names a parent T3 knows, or T3 sees the spawn within a short wait, and
   * checked as one; otherwise stopped.
   */
  const adoptOrStop = Effect.fn("CodexMcpProfile.adoptOrStop")(function* (nativeId: string) {
    yield* adoptByRead(nativeId);
    for (let waited = 0; !known.has(nativeId) && waited < CODEX_ADOPT_WAIT_MS;) {
      yield* Effect.sleep(ADOPT_POLL_MS);
      waited += ADOPT_POLL_MS;
    }
    if (!known.has(nativeId)) return yield* notLoadedHere("turn/started");
    yield* check("turn/started", nativeId, false);
  });

  /**
   * A turn Codex started itself (a goal's, a sub-agent's) is checked as it starts; a refused one is
   * stopped (goal paused, turn interrupted). While a thread's own such turn fixes its set, T3's MCP
   * server holds the thread's requests (mcpT3ToolRule.ts), so its first T3 tool call has that set.
   */
  const watchTurn = (nativeId: string, turnId: string) =>
    Effect.suspend(() => {
      if (ownTurns.delete(turnId) || starting.has(nativeId)) return Effect.void;
      const threadId = known.get(nativeId);
      if (threadId === undefined) {
        if (!narrowed) return Effect.void;
        return adoptOrStop(nativeId).pipe(
          Effect.catch(stop(nativeId, turnId)),
          Effect.forkIn(scope, { startImmediately: true }),
          Effect.asVoid,
        );
      }
      // The turn's place among the thread's turns, in the order Codex reports them started.
      const turn = children.has(nativeId) ? undefined : nextT3ToolSetTurn();
      // Held inside the check, which starts before this handler returns, and released however it
      // ends: a closed scope never starts it, so no hold is left. Only the profile step is held;
      // T3's tools carry the new set while the server step runs.
      const held =
        turn === undefined
          ? profileStep("turn/started", threadId, undefined)
          : Effect.acquireUseRelease(
              Effect.flatMap(Clock.currentTimeMillis, (now) =>
                Effect.sync(() => holdT3ToolSet(threadId, now, turn)),
              ),
              () => profileStep("turn/started", threadId, turn),
              (release) => Effect.sync(release),
            );
      return held.pipe(
        Effect.flatMap((resolved) => serverStep("turn/started", nativeId, resolved)),
        Effect.catch(stop(nativeId, turnId)),
        Effect.forkIn(scope, { startImmediately: true }),
        Effect.asVoid,
      );
    });
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
    const found =
      payload.type === "collabAgentToolCall"
        ? payload.receiverThreadIds
        : payload.type === "subAgentActivity"
          ? [payload.agentThreadId]
          : [];
    for (const child of found) adopt(child, parent);
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

  /**
   * Codex asks before an MCP server's elicitation (also its approval of an MCP tool call, when the
   * approval policy asks): one from a server outside the thread's profile is declined.
   */
  const declineOutside =
    (
      handler: (
        payload: CodexSchema.McpServerElicitationRequestParams,
      ) => Effect.Effect<
        CodexSchema.McpServerElicitationRequestResponse,
        CodexError.CodexAppServerError
      >,
    ) =>
    (payload: CodexSchema.McpServerElicitationRequestParams) =>
      guardServer(ELICITATION, payload.threadId, payload.serverName).pipe(
        Effect.matchEffect({
          onFailure: (error) =>
            Effect.logWarning("MCP profiles: declined an MCP elicitation", {
              serverName: payload.serverName,
              reason: reasonOf(error),
            }).pipe(
              Effect.as({
                action: "decline",
              } satisfies CodexSchema.McpServerElicitationRequestResponse),
            ),
          onSuccess: () => handler(payload),
        }),
      );

  const wrapped: Client = {
    ...client,
    request: intercept(client.request as unknown as Send) as unknown as Client["request"],
    raw: {
      ...client.raw,
      request: intercept(
        client.raw.request as unknown as Send,
      ) as unknown as Client["raw"]["request"],
    },
    handleServerRequest: ((method: string, handler: never) =>
      method === ELICITATION
        ? client.handleServerRequest(ELICITATION, declineOutside(handler))
        : client.handleServerRequest(method as never, handler)) as Client["handleServerRequest"],
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

/** Waits between reads of a thread the store did not give yet (a projection still catching up). */
const STORE_LAG_WAITS = ["250 millis", "500 millis", "1 second"] as const;

/**
 * `profileFor` that reads a thread the store does not have again a few times before it refuses:
 * Codex asks for a thread's profile as T3 creates it, which can be before the store has the row.
 * Any other refusal, and the last, stands.
 */
export const afterStoreLag =
  (profileFor: ProfileById): ProfileById =>
  (threadId) => {
    const attempt = (
      waits: ReadonlyArray<(typeof STORE_LAG_WAITS)[number]>,
    ): ReturnType<ProfileById> =>
      profileFor(threadId).pipe(
        Effect.catchIf(
          (error) => waits.length > 0 && isNotInStore(error),
          () => Effect.sleep(waits[0]!).pipe(Effect.andThen(attempt(waits.slice(1)))),
        ),
      );
    return attempt(STORE_LAG_WAITS);
  };

/**
 * CodexAdapterV2's client factory with each app-server's client wrapped in
 * {@link withCodexMcpProfiles}; spread over the adapter's options, so its key replaces theirs.
 * Typed optional only so the spread may follow upstream's own `clientFactory` key (TS2783); it is
 * always set.
 */
export const profiledClientFactory = Effect.fn("CodexMcpProfile.clientFactory")(function* (
  factory: CodexAppServerClientFactoryShape,
) {
  const profileFor = afterStoreLag(yield* makeThreadMcpProfilesById);
  const clientFactory: CodexAppServerClientFactoryShape = {
    open: (input) =>
      factory
        .open(input)
        .pipe(Effect.flatMap((client) => withCodexMcpProfiles(client, profileFor))),
  };
  const options: { readonly clientFactory?: CodexAppServerClientFactoryShape } = { clientFactory };
  return options;
});
