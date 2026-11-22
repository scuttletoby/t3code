/**
 * Fork: a thread's MCP profile on Codex (MCP profile design, part 5). The wrapped client runs over
 * a fake app-server that keeps a config per thread as Codex does: a resume of a loaded thread keeps
 * its old config, config/read lists config.toml, launch-argument and project servers, and
 * mcpServerStatus/list reports each thread's own servers.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  CodexSettings,
  EventId,
  type OrchestrationV2AppThread,
  ProjectId,
  ProviderInstanceId,
  ProviderSessionId,
  ProviderThreadId,
  ThreadId,
} from "@t3tools/contracts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import * as McpProviderSessions from "@t3tools/provider-core/server/McpProviderSessions";
import { makeProviderFailure } from "@t3tools/provider-core/server/failure";
import * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import type * as CodexClient from "effect-codex-app-server/client";
import * as CodexError from "effect-codex-app-server/errors";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Exit from "effect/Exit";
import * as Schema from "effect/Schema";
import * as Scope from "effect/Scope";
import * as TestClock from "effect/testing/TestClock";

import { makeTurnInput } from "../orchestration-v2/Adapters/AcpAdapterV2.testkit.ts";
import * as CodexAdapterV2 from "../orchestration-v2/Adapters/CodexAdapterV2.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import { makeReplayServerConfig } from "../orchestration-v2/testkit/ProviderReplayHarness.ts";
import * as ServerConfig from "../config.ts";
import * as PortScanner from "../preview/PortScanner.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import { ClaudeMcpProfileError, threadNotInStore } from "./claudeMcpProfile.ts";
import {
  CODEX_ADOPT_WAIT_MS,
  afterStoreLag,
  codexCarriedT3McpServers,
  codexConfiguredMcpServers,
  codexMcpProfileConfig,
  codexMcpServersOutsideProfile,
  profiledClientFactory,
  withCodexMcpProfiles,
  type ProfileById,
} from "./codexMcpProfile.ts";
import type { McpProfileResolved, McpProfileSet } from "./mcpProfile.ts";
import * as T3ToolSets from "./mcpT3ToolSets.ts";
import { CurrentTurnMcpProfile } from "./mcpTurnProfile.ts";

/** The groups the thread's set holds now, or its tag when it is not a list. */
const groupsOf = (threadId: string) => {
  const set = T3ToolSets.t3ToolSetFor(threadId);
  return set?._tag === "Groups" && set.groups !== "all" ? [...set.groups] : set?._tag;
};

type Client = CodexClient.CodexAppServerClient["Service"];
type Json = Record<string, unknown>;

const decodeCodexSettings = Schema.decodeSync(CodexSettings);
const POLICY = ProviderAdapter.ProviderAdapterV2RuntimePolicy.make({
  runtimeMode: "full-access",
  interactionMode: "default",
  cwd: "/workspace",
});
const THREAD_KEY = "shell_environment_policy.set.T3CODE_THREAD_ID";
const T3_SERVER = { url: "http://127.0.0.1:1/mcp", http_headers: { Authorization: "Bearer x" } };
const FIX = 'pick "all" for this thread, or fix the profile in Settings';

/** A thread/start answer as Codex gives it, enough for the adapter to read. */
const STARTED = {
  thread: {
    id: "",
    sessionId: "session",
    projectId: null,
    forkedFromId: null,
    preview: "",
    ephemeral: false,
    modelProvider: "openai",
    createdAt: 1782622440,
    updatedAt: 1782622440,
    status: { type: "idle" },
    path: "/tmp/thread.jsonl",
    cwd: "/workspace",
    cliVersion: "0.144.0",
    source: "vscode",
    threadSource: null,
    agentNickname: null,
    agentRole: null,
    gitInfo: null,
    name: null,
    turns: [],
  },
  model: "gpt-5.4",
  modelProvider: "openai",
  serviceTier: null,
  cwd: "/workspace",
  instructionSources: [],
  approvalPolicy: "on-request",
  approvalsReviewer: "user",
  sandbox: { type: "workspaceWrite", writableRoots: [], networkAccess: false },
  reasoningEffort: "medium",
};

interface FakeThread {
  readonly id: string;
  readonly cwd: string;
  config: Json;
  loaded: boolean;
  /** The thread that spawned it, for a Codex sub-agent. */
  readonly parent?: string;
}

/**
 * A Codex app-server as far as profiles see it: `servers` config.toml, `launchArgs` `-c` servers,
 * `project` each workspace's `.codex/config.toml`, `plugins` servers no config entry switches off,
 * `apps` ChatGPT sign-in (adds `codex_apps`, unlisted by config/read, while `features.apps` is on).
 * Statuses use Codex's own words.
 */
const makeFakeCodex = (
  setup: {
    readonly servers?: ReadonlyArray<string>;
    readonly launchArgs?: ReadonlyArray<string>;
    readonly project?: Readonly<Record<string, ReadonlyArray<string>>>;
    readonly plugins?: ReadonlyArray<string>;
    readonly apps?: boolean;
  } = {},
) => {
  const threads = new Map<string, FakeThread>();
  const calls: Array<{ readonly method: string; readonly params: Json }> = [];
  /** Methods that fail, or return what `answers` gives instead of the fake's answer. */
  const failing = new Set<string>();
  const answers = new Map<string, unknown>();
  /** Requests that say they arrived, then wait on a gate before answering (to stop one half-way). */
  const gates = new Map<
    string,
    { readonly reached: Deferred.Deferred<void>; readonly open: Deferred.Deferred<void> }
  >();
  let next = 0;
  let pageSize = 2;
  let endlessPages = false;

  const configured = (cwd: string) => [
    ...(setup.servers ?? []),
    ...(setup.launchArgs ?? []),
    ...(setup.project?.[cwd] ?? []),
  ];
  const fail = (method: string) =>
    new CodexError.CodexAppServerRequestError({ code: -32603, errorMessage: "boom", method });
  const reply = (
    method: string,
    params: Json,
  ): Effect.Effect<unknown, CodexError.CodexAppServerError> => {
    if (failing.has(method)) return Effect.fail(fail(method));
    if (answers.has(method)) return Effect.succeed(answers.get(method));
    const thread = (id: unknown) => threads.get(String(id));
    switch (method) {
      case "thread/start": {
        const id = `native-${++next}`;
        const cwd = typeof params.cwd === "string" ? params.cwd : "/workspace";
        threads.set(id, { id, cwd, config: { ...(params.config as Json) }, loaded: true });
        return Effect.succeed({ ...STARTED, cwd, thread: { ...STARTED.thread, id, cwd } });
      }
      case "thread/resume": {
        const found = thread(params.threadId);
        if (found === undefined) return Effect.fail(fail(method));
        // Codex keeps a loaded thread's config: a resume then changes nothing.
        if (!found.loaded) {
          found.config = { ...(params.config as Json) };
          found.loaded = true;
        }
        return Effect.succeed({ thread: { id: found.id, cwd: found.cwd } });
      }
      case "thread/fork": {
        const source = thread(params.threadId);
        if (source === undefined) return Effect.fail(fail(method));
        const id = `native-${++next}`;
        const cwd = typeof params.cwd === "string" ? params.cwd : source.cwd;
        threads.set(id, { id, cwd, config: { ...(params.config as Json) }, loaded: true });
        return Effect.succeed({ thread: { id, cwd } });
      }
      case "thread/read": {
        const found = thread(params.threadId);
        return found === undefined
          ? Effect.fail(fail(method))
          : Effect.succeed({
              thread: {
                id: found.id,
                cwd: found.cwd,
                ...(found.parent === undefined
                  ? {}
                  : {
                      source: {
                        subAgent: { thread_spawn: { parent_thread_id: found.parent, depth: 1 } },
                      },
                    }),
              },
            });
      }
      case "config/read": {
        const cwd = typeof params.cwd === "string" ? params.cwd : "";
        return Effect.succeed({
          config: { mcp_servers: Object.fromEntries(configured(cwd).map((name) => [name, {}])) },
        });
      }
      case "mcpServerStatus/list": {
        const found = thread(params.threadId);
        if (found === undefined || !found.loaded) return Effect.fail(fail(method));
        const names = [
          ...configured(found.cwd),
          ...(setup.plugins ?? []),
          ...(setup.apps === true && found.config["features.apps"] !== false ? ["codex_apps"] : []),
          ...Object.keys(found.config)
            .filter((key) => /^mcp_servers\.[^.]+$/.test(key))
            .map((key) => key.slice("mcp_servers.".length)),
        ];
        const data = [...new Set(names)].map((name) => ({
          name,
          runtimeStatus:
            found.config[`mcp_servers.${name}.enabled`] === false && !setup.plugins?.includes(name)
              ? "disabled"
              : "connected",
          tools: {},
        }));
        const start = typeof params.cursor === "string" ? Number(params.cursor) : 0;
        const end = start + pageSize;
        return Effect.succeed({
          data: data.slice(start, end),
          nextCursor: endlessPages || end < data.length ? String(end) : null,
        });
      }
      default:
        return Effect.succeed({ turn: { id: `turn-${++next}` } });
    }
  };
  /** Waiters for the `count`-th request of a method (a forked check reaching a milestone). */
  const waiters: Array<{
    readonly method: string;
    readonly count: number;
    readonly done: Deferred.Deferred<void>;
  }> = [];
  const wake = Effect.forEach(
    waiters,
    (waiter) =>
      calls.filter((call) => call.method === waiter.method).length >= waiter.count
        ? Deferred.succeed(waiter.done, undefined)
        : Effect.void,
    { discard: true },
  );
  const request = (method: string, params: unknown) =>
    Effect.gen(function* () {
      calls.push({ method, params: (params ?? {}) as Json });
      yield* wake;
      const gate = gates.get(method);
      if (gate !== undefined) {
        yield* Deferred.succeed(gate.reached, undefined);
        yield* Deferred.await(gate.open);
      }
      return yield* reply(method, (params ?? {}) as Json);
    });
  /** Notification handlers, in the order they were added, as the real client keeps them. */
  const handlers = new Map<
    string,
    Array<(payload: unknown) => Effect.Effect<void, CodexError.CodexAppServerError>>
  >();
  /** Server-request handlers (the last one added for a method, as the real client keeps them). */
  const serverRequests = new Map<
    string,
    (payload: unknown) => Effect.Effect<unknown, CodexError.CodexAppServerError>
  >();
  const client = {
    request,
    raw: { request, requests: undefined as never },
    notify: () => Effect.void,
    awaitTermination: Effect.never,
    handleServerRequest: (
      method: string,
      handler: (payload: unknown) => Effect.Effect<unknown, CodexError.CodexAppServerError>,
    ) => Effect.sync(() => void serverRequests.set(method, handler)),
    handleServerNotification: (
      method: string,
      handler: (payload: unknown) => Effect.Effect<void, CodexError.CodexAppServerError>,
    ) => Effect.sync(() => void handlers.set(method, [...(handlers.get(method) ?? []), handler])),
    handleUnknownServerRequest: () => Effect.void,
    handleUnknownServerNotification: () => Effect.void,
  } as unknown as Client;
  return {
    client,
    threads,
    calls,
    failing,
    answers,
    gates,
    /** Waits until `method` has been requested `count` times in all (it may already have been). */
    reached: (method: string, count: number) =>
      Effect.gen(function* () {
        const done = yield* Deferred.make<void>();
        waiters.push({ method, count, done });
        yield* wake;
        yield* Deferred.await(done);
      }),
    /** Codex sends a notification: every handler for it runs, in order. */
    emit: (method: string, payload: unknown) =>
      Effect.forEach(handlers.get(method) ?? [], (handler) => handler(payload), {
        discard: true,
      }).pipe(Effect.orDie),
    /** Codex asks the client (a server request): the handler's answer. */
    ask: (method: string, payload: unknown) => serverRequests.get(method)!(payload),
    setPageSize: (size: number) => void (pageSize = size),
    setEndlessPages: () => void (endlessPages = true),
    /**
     * Codex spawns a sub-agent of a loaded thread: the child copies the parent's loaded config,
     * servers switched off included (codex-rs core/src/agent/child_config.rs).
     */
    spawnChild: (parent: string) => {
      const source = threads.get(parent)!;
      const id = `native-${++next}`;
      threads.set(id, { id, cwd: source.cwd, config: { ...source.config }, loaded: true, parent });
      return id;
    },
    /** Codex lets the thread go (idle): its next resume loads it with the config sent then. */
    unload: (id: string) => void (threads.get(id)!.loaded = false),
    sent: (method: string) => calls.filter((call) => call.method === method),
  };
};

const resolved = (
  name: string,
  servers: McpProfileSet["servers"],
  t3Tools: McpProfileSet["t3Tools"] = "all",
): McpProfileResolved => ({
  _tag: "Resolved",
  name,
  source: "thread-pick",
  profile: { servers, t3Tools },
  narrowedByParent: false,
  unknownToolGroups: [],
});
const ALL = resolved("all", "all");
const isRefusal = Schema.is(ClaudeMcpProfileError);

/** Each T3 thread's profile, or its refusal; a thread not listed gets `all`. */
const makeProfiles = (entries: Record<string, McpProfileResolved | ClaudeMcpProfileError> = {}) => {
  const profiles = new Map<string, McpProfileResolved | ClaudeMcpProfileError>(
    Object.entries(entries),
  );
  const profileFor: ProfileById = (threadId) => {
    const entry = profiles.get(threadId) ?? ALL;
    return entry._tag === "ClaudeMcpProfileError" ? Effect.fail(entry) : Effect.succeed(entry);
  };
  return { profiles, profileFor };
};

/** The config CodexAdapterV2 sends for a T3 thread: its id and T3's own servers. */
const threadConfig = (threadId: string) => ({
  approvals_reviewer: "user",
  [THREAD_KEY]: threadId,
  "mcp_servers.t3-code": { ...T3_SERVER, disabled_tools: ["t3_thread_wait"] },
  "mcp_servers.t3-code-direct": { ...T3_SERVER, enabled_tools: ["t3_thread_wait"] },
});

/** `ok`, or the message the user reads for the failure (as a turn's error shows it). */
const outcome = <A, E>(effect: Effect.Effect<A, E>) =>
  effect.pipe(
    Effect.exit,
    Effect.map((exit) =>
      exit._tag === "Success"
        ? "ok"
        : makeProviderFailure({ cause: exit.cause, class: "provider_error" }).message,
    ),
  );

const start = (client: Client, threadId: string, cwd = "/workspace") =>
  client.request("thread/start", { cwd, config: threadConfig(threadId) } as never) as Effect.Effect<
    { readonly thread: { readonly id: string } },
    CodexError.CodexAppServerError
  >;
const resume = (client: Client, nativeId: string, threadId?: string) =>
  client.raw.request("thread/resume", {
    threadId: nativeId,
    excludeTurns: true,
    ...(threadId === undefined ? {} : { config: threadConfig(threadId) }),
  } as never);
const turn = (client: Client, nativeId: string) =>
  client.request("turn/start", { threadId: nativeId, input: [] } as never);
const offKeys = (config: Json) =>
  Object.keys(config)
    .filter((key) => key.startsWith("mcp_servers.") && key.endsWith(".enabled"))
    .sort();

/** What the adapter's own turn/completed handler is given, through the wrapped client. */
const completions = (client: Client) =>
  Effect.gen(function* () {
    const seen: Array<{ readonly id: string; readonly status: string; readonly error?: unknown }> =
      [];
    yield* client.handleServerNotification("turn/completed", (payload) =>
      Effect.sync(() => void seen.push(payload.turn as never)),
    );
    return seen;
  });
const turnStarted = (threadId: string, turnId: string) => ({
  threadId,
  turn: { id: turnId, items: [], status: "inProgress", error: null },
});
const turnCompleted = (threadId: string, turnId: string, status = "interrupted") => ({
  threadId,
  turn: { id: turnId, items: [], status, error: null },
});
const STOPPED = "T3 stopped this turn, which Codex started on its own for the thread's goal";
/** The item Codex reports when a thread spawns sub-agents (collab tools, multi-agent v1). */
const spawned = (threadId: string, children: ReadonlyArray<string>) => ({
  threadId,
  turnId: "parent-turn",
  item: {
    type: "collabAgentToolCall",
    id: "call-1",
    tool: "spawnAgent",
    status: "completed",
    senderThreadId: threadId,
    receiverThreadIds: children,
    agentsStates: {},
  },
});
/** The item Codex reports for a sub-agent's life (multi-agent v2). */
const subAgentActivity = (threadId: string, child: string) => ({
  threadId,
  turnId: "parent-turn",
  item: {
    type: "subAgentActivity",
    id: "act-1",
    kind: "started",
    agentPath: "/a",
    agentThreadId: child,
  },
});

describe("codexMcpProfile helpers", () => {
  it("turns off every configured server the profile leaves out, never T3's own", () => {
    assert.deepStrictEqual(
      codexMcpProfileConfig("mine", { servers: ["alpha"], t3Tools: "all" }, [
        "alpha",
        "beta",
        "t3-code",
        "t3-code-direct",
      ]),
      { "mcp_servers.beta.enabled": false, "features.apps": false },
    );
    assert.deepStrictEqual(
      codexMcpProfileConfig("all", { servers: "all", t3Tools: "all" }, ["alpha"]),
      {},
    );
    // A kept name that is not set up keeps nothing and is not an error (design §9).
    assert.deepStrictEqual(
      codexMcpProfileConfig("mine", { servers: ["nowhere"], t3Tools: "all" }, []),
      { "features.apps": false },
    );
    // Codex's connectors server is never listed by config/read: kept only by name.
    assert.deepStrictEqual(
      codexMcpProfileConfig("apps", { servers: ["codex_apps"], t3Tools: "all" }, ["beta"]),
      { "mcp_servers.beta.enabled": false },
    );
  });

  it("a profile that limits T3's tools adds no Codex config: T3's own server cuts them", () => {
    assert.deepStrictEqual(
      codexMcpProfileConfig("lean", { servers: "all", t3Tools: ["threads"] }, ["alpha"]),
      {},
    );
    assert.deepStrictEqual(
      codexMcpProfileConfig("lean", { servers: ["alpha"], t3Tools: [] }, ["alpha", "beta"]),
      codexMcpProfileConfig("lean", { servers: ["alpha"], t3Tools: "all" }, ["alpha", "beta"]),
    );
  });

  it("refuses a profile it cannot apply, with the fix", () => {
    const named = codexMcpProfileConfig("mine", { servers: [], t3Tools: "all" }, ["a.b"]);
    assert.isTrue(isRefusal(named));
    assert.include((named as ClaudeMcpProfileError).reason, '"a.b"');
    assert.include((named as ClaudeMcpProfileError).reason, FIX);
  });

  it("reads server names from config/read, and nothing else as a list", () => {
    assert.deepStrictEqual(codexConfiguredMcpServers({ mcp_servers: { a: {}, b: {} } }), [
      "a",
      "b",
    ]);
    assert.deepStrictEqual(codexConfiguredMcpServers({}), []);
    assert.deepStrictEqual(codexConfiguredMcpServers({ mcp_servers: null }), []);
    assert.isUndefined(codexConfiguredMcpServers({ mcp_servers: ["a"] }));
    assert.isUndefined(codexConfiguredMcpServers(undefined));
  });

  it("finds live servers outside the profile that Codex did not disable", () => {
    const statuses = [
      { name: "alpha", runtimeStatus: "connected" },
      { name: "beta", runtimeStatus: "disabled" },
      { name: "gamma", runtimeStatus: "starting" },
      { name: "t3-code", runtimeStatus: "connected" },
    ];
    assert.deepStrictEqual(codexMcpServersOutsideProfile(["alpha"], statuses), ["gamma"]);
    assert.deepStrictEqual(codexMcpServersOutsideProfile("all", statuses), []);
  });

  it("only Codex's own word for a switched-off server counts as off", () => {
    // codex-rs 0.171.0 McpServerRuntimeStatus, as the app-server sends it (camelCase): a server
    // with enabled = false reports "disabled"; every other word, none, or an unknown one is on.
    const words = [
      "notStarted",
      "starting",
      "connected",
      "authenticationRequired",
      "failed",
      "cancelled",
      "unknown",
      "Disabled",
    ];
    const statuses = [
      ...words.map((word) => ({ name: word, runtimeStatus: word })),
      { name: "none", runtimeStatus: null },
      { name: "missing" },
      { name: "off", runtimeStatus: "disabled" },
    ];
    assert.deepStrictEqual(codexMcpServersOutsideProfile([], statuses), [
      ...words,
      "none",
      "missing",
    ]);
  });
});

describe("withCodexMcpProfiles", () => {
  it.effect(
    "a left-out server gets enabled=false on that thread only; kept and T3's own stay as sent",
    () =>
      Effect.gen(function* () {
        const fake = makeFakeCodex({ servers: ["alpha", "beta"], launchArgs: ["gamma"] });
        const { profileFor } = makeProfiles({ narrow: resolved("alpha-only", ["alpha"]) });
        const client = yield* withCodexMcpProfiles(fake.client, profileFor);
        const narrow = (yield* start(client, "narrow")).thread.id;
        const wide = (yield* start(client, "wide")).thread.id;
        const [narrowSent, wideSent] = fake
          .sent("thread/start")
          .map((call) => call.params.config as Json);
        // beta from config.toml and gamma from a launch argument; alpha is kept.
        assert.deepStrictEqual(offKeys(narrowSent!), [
          "mcp_servers.beta.enabled",
          "mcp_servers.gamma.enabled",
        ]);
        assert.isFalse(narrowSent!["mcp_servers.beta.enabled"]);
        // Everything the adapter sent is still there, unchanged.
        for (const [key, value] of Object.entries(threadConfig("narrow"))) {
          assert.deepStrictEqual(narrowSent![key], value);
        }
        // The other thread on the same app-server is sent exactly what the adapter sent.
        assert.deepStrictEqual(wideSent, threadConfig("wide"));
        assert.deepStrictEqual(fake.threads.get(wide)!.config, threadConfig("wide"));
        assert.strictEqual(yield* outcome(turn(client, narrow)), "ok");
        assert.strictEqual(yield* outcome(turn(client, wide)), "ok");
        assert.strictEqual(fake.sent("turn/start").length, 2);
      }),
  );

  it.effect.each([
    { order: "narrow first", first: "a", second: "b" },
    { order: "narrow second", first: "b", second: "a" },
  ] as const)("two threads on one app-server keep their own sets ($order)", ({ first, second }) =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta", "gamma"] });
      const { profileFor } = makeProfiles({
        a: resolved("alpha-only", ["alpha"]),
        b: resolved("beta-gamma", ["beta", "gamma"]),
      });
      const client = yield* withCodexMcpProfiles(fake.client, profileFor);
      const ids: Record<string, string> = {};
      ids[first] = (yield* start(client, first)).thread.id;
      ids[second] = (yield* start(client, second)).thread.id;
      assert.deepStrictEqual(offKeys(fake.threads.get(ids.a!)!.config), [
        "mcp_servers.beta.enabled",
        "mcp_servers.gamma.enabled",
      ]);
      assert.deepStrictEqual(offKeys(fake.threads.get(ids.b!)!.config), [
        "mcp_servers.alpha.enabled",
      ]);
      // Each turn is checked against its own thread's servers and profile.
      assert.strictEqual(yield* outcome(turn(client, ids.a!)), "ok");
      assert.strictEqual(yield* outcome(turn(client, ids.b!)), "ok");
      // A resume of one names that thread, whichever opened the session.
      fake.unload(ids.a!);
      yield* resume(client, ids.a!, "a");
      assert.deepStrictEqual(offKeys(fake.threads.get(ids.a!)!.config), [
        "mcp_servers.beta.enabled",
        "mcp_servers.gamma.enabled",
      ]);
    }),
  );

  it.effect("project servers come from config/read in the thread's workspace, forks included", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha"], project: { "/proj": ["delta"] } });
      const { profileFor } = makeProfiles({
        p: resolved("none", []),
        forked: resolved("none", []),
      });
      const client = yield* withCodexMcpProfiles(fake.client, profileFor);
      const p = (yield* start(client, "p", "/proj")).thread.id;
      assert.deepStrictEqual(offKeys(fake.threads.get(p)!.config), [
        "mcp_servers.alpha.enabled",
        "mcp_servers.delta.enabled",
      ]);
      assert.deepStrictEqual(fake.sent("config/read")[0]!.params, {
        cwd: "/proj",
        includeLayers: false,
      });
      // A fork without a cwd is loaded where its source is: read from Codex.
      const fork = (yield* client.request("thread/fork", {
        threadId: p,
        config: threadConfig("forked"),
      } as never)) as { readonly thread: { readonly id: string } };
      assert.strictEqual(fake.sent("thread/read").length, 1);
      assert.deepStrictEqual(offKeys(fake.threads.get(fork.thread.id)!.config), [
        "mcp_servers.alpha.enabled",
        "mcp_servers.delta.enabled",
      ]);
      assert.strictEqual(yield* outcome(turn(client, fork.thread.id)), "ok");
    }),
  );

  it.effect("a profile that cannot be worked out refuses with its reason; nothing loads", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha"] });
      const { profileFor } = makeProfiles({
        missing: new ClaudeMcpProfileError({
          reason: `MCP profile "gone" is not in Settings, so this thread cannot start: ${FIX}.`,
        }),
      });
      const client = yield* withCodexMcpProfiles(fake.client, profileFor);
      const message = yield* outcome(start(client, "missing"));
      assert.include(message, 'MCP profile "gone" is not in Settings');
      assert.include(message, FIX);
      assert.deepStrictEqual(fake.sent("thread/start"), []);
    }),
  );

  it.effect(
    "a server name Codex cannot take refuses; a profile that limits only T3's tools loads",
    () =>
      Effect.gen(function* () {
        const fake = makeFakeCodex({ servers: ["alpha", "my server"] });
        const { profileFor } = makeProfiles({
          tools: resolved("lean", "all", ["threads"]),
          named: resolved("none", []),
        });
        const client = yield* withCodexMcpProfiles(fake.client, profileFor);
        const named = yield* outcome(start(client, "named"));
        assert.include(named, '"my server"');
        assert.include(named, FIX);
        assert.deepStrictEqual(fake.sent("thread/start"), []);
        // Every outside server kept: nothing extra is asked of Codex or sent to it.
        const reads = fake.sent("config/read").length;
        assert.strictEqual(yield* outcome(start(client, "tools")), "ok");
        assert.strictEqual(fake.sent("thread/start").length, 1);
        assert.strictEqual(fake.sent("config/read").length, reads);
        const sent = fake.sent("thread/start")[0]!.params as { readonly config: Json };
        assert.deepStrictEqual(offKeys(sent.config), []);
      }),
  );

  it.effect.each([
    {
      case: "config/read fails",
      set: (fake: ReturnType<typeof makeFakeCodex>) => void fake.failing.add("config/read"),
    },
    {
      case: "config/read gives no server table",
      set: (fake: ReturnType<typeof makeFakeCodex>) =>
        void fake.answers.set("config/read", { config: { mcp_servers: "x" } }),
    },
  ])("refuses, never loads with every server, when $case", ({ set }) =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha"] });
      set(fake);
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("none", []) }).profileFor,
      );
      const message = yield* outcome(start(client, "t"));
      assert.include(message, "could not read which MCP servers Codex has set up");
      assert.include(message, FIX);
      assert.deepStrictEqual(fake.sent("thread/start"), []);
    }),
  );

  it.effect("a thread with no profile (all) costs no extra request, as before profiles", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha"] });
      const client = yield* withCodexMcpProfiles(fake.client, makeProfiles().profileFor);
      const id = (yield* start(client, "old")).thread.id;
      yield* turn(client, id);
      // Requests without a T3 thread (no profile anywhere on this app-server) pass as before.
      yield* resume(client, id);
      yield* client.request("thread/goal/set", { threadId: id, status: "active" } as never);
      assert.deepStrictEqual(
        fake.calls.map((call) => call.method),
        ["thread/start", "turn/start", "thread/resume", "thread/goal/set"],
      );
      assert.deepStrictEqual(fake.calls[0]!.params.config, threadConfig("old"));
    }),
  );

  it.effect("an empty server list turns off only Codex's connectors, and the thread runs", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex();
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("none", []) }).profileFor,
      );
      const id = (yield* start(client, "t")).thread.id;
      assert.deepStrictEqual(fake.threads.get(id)!.config, {
        ...threadConfig("t"),
        "features.apps": false,
      });
      assert.strictEqual(yield* outcome(turn(client, id)), "ok");
    }),
  );

  it.effect("runs again: starting or resuming twice sends the same set", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("a", ["alpha"]) }).profileFor,
      );
      const first = (yield* start(client, "t")).thread.id;
      const second = (yield* start(client, "t")).thread.id;
      assert.deepStrictEqual(fake.threads.get(first)!.config, fake.threads.get(second)!.config);
      fake.unload(first);
      yield* resume(client, first, "t");
      yield* resume(client, first, "t");
      const resumes = fake.sent("thread/resume").map((call) => call.params.config);
      assert.deepStrictEqual(resumes[0], resumes[1]);
      assert.deepStrictEqual(offKeys(fake.threads.get(first)!.config), [
        "mcp_servers.beta.enabled",
      ]);
      assert.strictEqual(yield* outcome(turn(client, first)), "ok");
    }),
  );

  it.effect(
    "a profile narrowed while Codex has the thread loaded refuses its turns until Codex reloads it",
    () =>
      Effect.gen(function* () {
        const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
        const { profiles, profileFor } = makeProfiles();
        const client = yield* withCodexMcpProfiles(fake.client, profileFor);
        const id = (yield* start(client, "t")).thread.id;
        profiles.set("t", resolved("a", ["alpha"]));
        // Codex keeps the loaded thread's servers, so this resume changes nothing there.
        yield* resume(client, id, "t");
        const message = yield* outcome(turn(client, id));
        assert.include(message, "Codex still has beta switched on for this thread");
        assert.include(message, "once Codex loads it again");
        assert.include(message, FIX);
        assert.deepStrictEqual(fake.sent("turn/start"), []);
        // A goal starts work too; a paused goal does not.
        assert.include(
          yield* outcome(
            client.request("thread/goal/set", { threadId: id, status: "active" } as never),
          ),
          "beta switched on",
        );
        assert.strictEqual(
          yield* outcome(
            client.request("thread/goal/set", { threadId: id, status: "paused" } as never),
          ),
          "ok",
        );
        fake.unload(id);
        yield* resume(client, id, "t");
        assert.strictEqual(yield* outcome(turn(client, id)), "ok");
      }),
  );

  it.effect(
    "a profile narrowed during a running goal: the next turn Codex starts is stopped, with why",
    () =>
      Effect.gen(function* () {
        const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
        const { profiles, profileFor } = makeProfiles();
        const client = yield* withCodexMcpProfiles(fake.client, profileFor);
        const seen = yield* completions(client);
        const id = (yield* start(client, "t")).thread.id;
        assert.strictEqual(
          yield* outcome(
            client.request("thread/goal/set", { threadId: id, status: "active" } as never),
          ),
          "ok",
        );
        // The goal runs; the profile is narrowed while Codex has the thread loaded.
        profiles.set("t", resolved("a", ["alpha"]));
        yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
        yield* fake.reached("turn/interrupt", 1);
        // The goal is paused before the turn is interrupted, so Codex starts no next one.
        const stops = fake.calls.filter(
          (call) => call.method === "thread/goal/set" || call.method === "turn/interrupt",
        );
        assert.deepStrictEqual(stops.slice(-2), [
          { method: "thread/goal/set", params: { threadId: id, status: "paused" } },
          { method: "turn/interrupt", params: { threadId: id, turnId: "goal-2" } },
        ]);
        yield* fake.emit("turn/completed", turnCompleted(id, "goal-2"));
        assert.strictEqual(seen.length, 1);
        assert.strictEqual(seen[0]!.status, "failed");
        const message = (seen[0]!.error as { readonly message: string }).message;
        assert.include(message, STOPPED);
        assert.include(message, "paused the goal");
        assert.include(message, "Codex still has beta switched on for this thread");
        assert.include(message, FIX);
        // Another turn's end is passed on as Codex sent it.
        yield* fake.emit("turn/completed", turnCompleted(id, "other", "completed"));
        assert.deepStrictEqual(seen[1], turnCompleted(id, "other", "completed").turn as never);
      }),
  );

  it.effect('builtinTools "none" refuses its turns, also one Codex starts itself for a goal', () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha"] });
      const { profiles, profileFor } = makeProfiles();
      const client = yield* withCodexMcpProfiles(fake.client, profileFor);
      const seen = yield* completions(client);
      const id = (yield* start(client, "t")).thread.id;
      profiles.set("t", {
        ...resolved("bare", "all"),
        profile: { servers: "all", t3Tools: "all", builtinTools: "none" },
      });
      const message = yield* outcome(turn(client, id));
      assert.include(message, 'MCP profile "bare" has builtinTools "none"');
      assert.include(message, "codex cannot run a thread without its built-in tools");
      assert.deepStrictEqual(fake.sent("turn/start"), []);
      yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
      yield* fake.reached("turn/interrupt", 1);
      yield* fake.emit("turn/completed", turnCompleted(id, "goal-2"));
      assert.include((seen[0]!.error as { readonly message: string }).message, "builtinTools");
    }),
  );

  it.effect("an unchanged profile: a turn Codex starts for the goal runs untouched", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("a", ["alpha"]) }).profileFor,
      );
      const seen = yield* completions(client);
      const id = (yield* start(client, "t")).thread.id;
      const reads = fake.sent("mcpServerStatus/list").length;
      yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
      yield* fake.reached("mcpServerStatus/list", reads + 1);
      yield* fake.emit("turn/completed", turnCompleted(id, "goal-2", "completed"));
      assert.deepStrictEqual(fake.sent("turn/interrupt"), []);
      assert.deepStrictEqual(fake.sent("thread/goal/set"), []);
      assert.deepStrictEqual(seen, [turnCompleted(id, "goal-2", "completed").turn as never]);
    }),
  );

  it.effect(
    "a profile that breaks during a goal stops its next turn with the profile's reason",
    () =>
      Effect.gen(function* () {
        const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
        const { profiles, profileFor } = makeProfiles({ t: resolved("a", ["alpha"]) });
        const client = yield* withCodexMcpProfiles(fake.client, profileFor);
        const seen = yield* completions(client);
        const id = (yield* start(client, "t")).thread.id;
        profiles.set(
          "t",
          new ClaudeMcpProfileError({ reason: `MCP profile "a" is not in Settings: ${FIX}.` }),
        );
        yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
        yield* fake.reached("turn/interrupt", 1);
        yield* fake.emit("turn/completed", turnCompleted(id, "goal-2", "completed"));
        assert.strictEqual(seen[0]!.status, "failed");
        assert.include(
          (seen[0]!.error as { readonly message: string }).message,
          "is not in Settings",
        );
      }),
  );

  it.effect(
    "runs again and restarts mid-goal: each Codex-started turn is checked; a reload applies the profile",
    () =>
      Effect.gen(function* () {
        const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
        const { profiles, profileFor } = makeProfiles();
        const before = yield* withCodexMcpProfiles(fake.client, profileFor);
        const id = (yield* start(before, "t")).thread.id;
        profiles.set("t", resolved("a", ["alpha"]));
        // Codex starts goal turns again and again: each one is stopped, none slips through.
        yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
        yield* fake.reached("turn/interrupt", 1);
        yield* fake.emit("turn/started", turnStarted(id, "goal-3"));
        yield* fake.reached("turn/interrupt", 2);
        // The app-server restarts mid-goal: T3 loads the thread again, now with beta off, and
        // the goal's next turn runs.
        fake.unload(id);
        const after = yield* withCodexMcpProfiles(fake.client, profileFor);
        yield* resume(after, id, "t");
        assert.deepStrictEqual(offKeys(fake.threads.get(id)!.config), ["mcp_servers.beta.enabled"]);
        const reads = fake.sent("mcpServerStatus/list").length;
        yield* fake.emit("turn/started", turnStarted(id, "goal-4"));
        // Both wrappers listen on this fake; the old one's check sees the new config too.
        yield* fake.reached("mcpServerStatus/list", reads + 2);
        assert.strictEqual(fake.sent("turn/interrupt").length, 2);
      }),
  );

  it.effect(
    "T3's tools narrowed after the thread loaded on all: its next turn runs with exactly that set",
    () =>
      Effect.gen(function* () {
        T3ToolSets.__testing.clear();
        const fake = makeFakeCodex({ servers: ["alpha"] });
        const { profiles, profileFor } = makeProfiles();
        const client = yield* withCodexMcpProfiles(fake.client, profileFor);
        const id = (yield* start(client, "t")).thread.id;
        // Every server kept, only T3's tools limited: no server is outside the profile.
        profiles.set("t", resolved("lean", "all", ["threads"]));
        assert.strictEqual(yield* outcome(turn(client, id)), "ok");
        assert.strictEqual(fake.sent("turn/start").length, 1);
        assert.deepStrictEqual(groupsOf("t"), ["threads"]);
        // A turn Codex starts on its own (a goal's next turn) fixes the set as it starts too.
        profiles.set("t", resolved("lean", ["alpha"], ["threads", "waits"]));
        const reads = fake.sent("mcpServerStatus/list").length;
        yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
        yield* fake.reached("mcpServerStatus/list", reads + 1);
        assert.deepStrictEqual(groupsOf("t"), ["threads", "waits"]);
        assert.deepStrictEqual(fake.sent("turn/interrupt"), []);
        // A profile that cannot be worked out keeps only the tools every profile keeps.
        profiles.set("t", new ClaudeMcpProfileError({ reason: "gone" }));
        assert.include(yield* outcome(turn(client, id)), "gone");
        assert.strictEqual(T3ToolSets.t3ToolSetFor("t")?._tag, "Refused");
      }),
  );

  it.effect(
    "a refused resume is not swapped for a fresh session: the next start of that thread refuses once",
    () =>
      Effect.gen(function* () {
        const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
        const client = yield* withCodexMcpProfiles(
          fake.client,
          makeProfiles({ t: resolved("a", ["alpha"]) }).profileFor,
        );
        const id = (yield* start(client, "t")).thread.id;
        fake.unload(id);
        // One failed read refuses the resume...
        fake.failing.add("config/read");
        assert.include(yield* outcome(resume(client, id, "t")), "could not read which MCP servers");
        fake.failing.delete("config/read");
        // ...and T3's fallback, a new Codex session for the same thread, is refused with it.
        const fresh = yield* outcome(start(client, "t"));
        assert.include(fresh, "could not read which MCP servers");
        assert.include(fresh, "did not move the thread to a new Codex session");
        assert.strictEqual(fake.sent("thread/start").length, 1);
        // Retrying the turn resumes the same Codex thread; later starts are not held.
        yield* resume(client, id, "t");
        assert.deepStrictEqual(offKeys(fake.threads.get(id)!.config), ["mcp_servers.beta.enabled"]);
        assert.strictEqual(yield* outcome(start(client, "t")), "ok");
        // Stopped half-way: a refused resume followed by no start leaves one refusal, not more.
        fake.unload(id);
        fake.failing.add("config/read");
        yield* outcome(resume(client, id, "t"));
        fake.failing.delete("config/read");
        assert.include(yield* outcome(start(client, "t")), "new Codex session");
        assert.strictEqual(yield* outcome(start(client, "t")), "ok");
      }),
  );

  it.effect("Codex's connectors server is switched off unless the profile keeps it by name", () =>
    Effect.gen(function* () {
      // With ChatGPT sign-in Codex adds codex_apps, which config/read never lists.
      const fake = makeFakeCodex({ servers: ["alpha"], apps: true });
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ a: resolved("a", ["alpha"]), apps: resolved("apps", ["codex_apps"]) })
          .profileFor,
      );
      const narrow = (yield* start(client, "a")).thread.id;
      assert.isFalse(fake.threads.get(narrow)!.config["features.apps"]);
      assert.strictEqual(yield* outcome(turn(client, narrow)), "ok");
      const kept = (yield* start(client, "apps")).thread.id;
      assert.notProperty(fake.threads.get(kept)!.config, "features.apps");
      assert.strictEqual(yield* outcome(turn(client, kept)), "ok");
      // A thread on all keeps it too.
      const all = (yield* start(client, "wide")).thread.id;
      assert.notProperty(fake.threads.get(all)!.config, "features.apps");
    }),
  );

  it.effect(
    "a Codex sub-agent copies the servers T3 switched off in its parent's own thread config",
    () =>
      Effect.gen(function* () {
        const fake = makeFakeCodex({ servers: ["alpha", "beta"], apps: true });
        const client = yield* withCodexMcpProfiles(
          fake.client,
          makeProfiles({ t: resolved("a", ["alpha"]) }).profileFor,
        );
        const seen = yield* completions(client);
        const id = (yield* start(client, "t")).thread.id;
        // What T3 sends at load is what a child copies: the off keys sit in the thread's config.
        assert.deepStrictEqual(fake.sent("thread/start")[0]!.params.config, {
          ...threadConfig("t"),
          "mcp_servers.beta.enabled": false,
          "features.apps": false,
        });
        const child = fake.spawnChild(id);
        // Seen twice (started and completed): runs again, adopted once.
        yield* fake.emit("item/started", spawned(id, []));
        yield* fake.emit("item/completed", spawned(id, [child]));
        yield* fake.emit("item/completed", spawned(id, [child]));
        const reads = fake.sent("mcpServerStatus/list").length;
        yield* fake.emit("turn/started", turnStarted(child, "child-1"));
        yield* fake.reached("mcpServerStatus/list", reads + 1);
        assert.strictEqual(fake.sent("mcpServerStatus/list")[reads]!.params.threadId, child);
        yield* fake.emit("turn/completed", turnCompleted(child, "child-1", "completed"));
        assert.deepStrictEqual(fake.sent("turn/interrupt"), []);
        assert.deepStrictEqual(seen, [turnCompleted(child, "child-1", "completed").turn as never]);
      }),
  );

  type Spawn = (parent: string, child: string) => readonly [string, unknown];
  const spawns: ReadonlyArray<readonly [string, Spawn]> = [
    ["spawnAgent", (parent, child) => ["item/completed", spawned(parent, [child])]],
    ["subAgentActivity", (parent, child) => ["item/started", subAgentActivity(parent, child)]],
  ];
  it.effect.each(spawns)(
    "a profile narrowed while a Codex sub-agent runs (%s): its next turn is stopped, with why",
    ([, spawn]) =>
      Effect.gen(function* () {
        const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
        const { profiles, profileFor } = makeProfiles();
        const client = yield* withCodexMcpProfiles(fake.client, profileFor);
        const seen = yield* completions(client);
        const id = (yield* start(client, "t")).thread.id;
        const child = fake.spawnChild(id);
        const [method, payload] = spawn(id, child);
        yield* fake.emit(method, payload);
        profiles.set("t", resolved("a", ["alpha"]));
        yield* fake.emit("turn/started", turnStarted(child, "child-2"));
        yield* fake.reached("turn/interrupt", 1);
        assert.deepStrictEqual(fake.sent("turn/interrupt")[0]!.params, {
          threadId: child,
          turnId: "child-2",
        });
        yield* fake.emit("turn/completed", turnCompleted(child, "child-2"));
        assert.strictEqual(seen[0]!.status, "failed");
        assert.include(
          (seen[0]!.error as { readonly message: string }).message,
          "Codex still has beta switched on for this thread",
        );
        // A sub-agent T3 later resumes (to read its model) loads under its parent's profile.
        fake.unload(child);
        yield* resume(client, child);
        assert.deepStrictEqual(offKeys(fake.threads.get(child)!.config), [
          "mcp_servers.beta.enabled",
        ]);
      }),
  );

  it.effect("a widened profile runs at once: the thread keeps fewer servers until reloaded", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const { profiles, profileFor } = makeProfiles({ t: resolved("a", ["alpha"]) });
      const client = yield* withCodexMcpProfiles(fake.client, profileFor);
      const id = (yield* start(client, "t")).thread.id;
      profiles.set("t", ALL);
      assert.strictEqual(yield* outcome(turn(client, id)), "ok");
      // The status list is read only for a profile narrower than all.
      assert.deepStrictEqual(fake.sent("mcpServerStatus/list").length, 0);
    }),
  );

  it.effect("a plugin server Codex will not switch off refuses the turn", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha"], plugins: ["plug"] });
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("a", ["alpha"]) }).profileFor,
      );
      const id = (yield* start(client, "t")).thread.id;
      const message = yield* outcome(turn(client, id));
      assert.include(message, "Codex still has plug switched on");
      assert.include(message, "a Codex plugin can also add a server");
    }),
  );

  it.effect("the server list is read across pages, and refuses when it cannot be read", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["a1", "a2", "a3", "a4", "a5"] });
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("a", ["a1"]) }).profileFor,
      );
      const id = (yield* start(client, "t")).thread.id;
      assert.strictEqual(yield* outcome(turn(client, id)), "ok");
      assert.isAbove(fake.sent("mcpServerStatus/list").length, 2);
      fake.setEndlessPages();
      assert.include(yield* outcome(turn(client, id)), "could not read which MCP servers");
      fake.failing.add("mcpServerStatus/list");
      assert.include(yield* outcome(turn(client, id)), "could not read which MCP servers");
      assert.strictEqual(fake.sent("turn/start").length, 1);
    }),
  );

  it.effect("app-server restarted: a fresh client knows no thread until T3 loads it again", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const { profileFor } = makeProfiles({ t: resolved("a", ["alpha"]) });
      const before = yield* withCodexMcpProfiles(fake.client, profileFor);
      const id = (yield* start(before, "t")).thread.id;
      // The new process: same threads on disk, none loaded, and a new wrapped client.
      fake.unload(id);
      const after = yield* withCodexMcpProfiles(fake.client, profileFor);
      // Nothing narrowed on it yet, so a turn on a thread it never loaded is left to Codex.
      yield* resume(after, id, "t");
      assert.deepStrictEqual(offKeys(fake.threads.get(id)!.config), ["mcp_servers.beta.enabled"]);
      assert.strictEqual(yield* outcome(turn(after, id)), "ok");
    }),
  );

  it.effect("with a profile in use, a thread T3 did not load itself is refused", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("a", ["alpha"]) }).profileFor,
      );
      yield* start(client, "t");
      // Codex's own sub-agent thread, resumed with no T3 thread id (a model lookup).
      const child = (yield* fake.client.request("thread/start", {} as never)) as {
        readonly thread: { readonly id: string };
      };
      fake.unload(child.thread.id);
      assert.include(
        yield* outcome(resume(client, child.thread.id)),
        "cannot tell which T3 thread this Codex thread belongs to",
      );
      assert.strictEqual(fake.sent("thread/resume").length, 0);
      assert.include(
        yield* outcome(turn(client, child.thread.id)),
        "did not load this Codex thread",
      );
      assert.strictEqual(fake.sent("turn/start").length, 0);
    }),
  );

  it.effect("stopped half-way: a turn interrupted in its check never starts; a retry runs", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("a", ["alpha"]) }).profileFor,
      );
      const id = (yield* start(client, "t")).thread.id;
      const gate = { reached: yield* Deferred.make<void>(), open: yield* Deferred.make<void>() };
      fake.gates.set("mcpServerStatus/list", gate);
      const fiber = yield* Effect.forkChild(turn(client, id));
      yield* Deferred.await(gate.reached);
      yield* Fiber.interrupt(fiber);
      assert.strictEqual(fake.sent("turn/start").length, 0);
      fake.gates.delete("mcpServerStatus/list");
      assert.strictEqual(yield* outcome(turn(client, id)), "ok");
      assert.strictEqual(fake.sent("turn/start").length, 1);
    }),
  );

  it.effect("stopped half-way: a load interrupted while servers are read sends nothing", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("a", ["alpha"]) }).profileFor,
      );
      const gate = { reached: yield* Deferred.make<void>(), open: yield* Deferred.make<void>() };
      fake.gates.set("config/read", gate);
      const fiber = yield* Effect.forkChild(start(client, "t"));
      yield* Deferred.await(gate.reached);
      yield* Fiber.interrupt(fiber);
      assert.strictEqual(fake.sent("thread/start").length, 0);
      fake.gates.delete("config/read");
      const id = (yield* start(client, "t")).thread.id;
      assert.deepStrictEqual(offKeys(fake.threads.get(id)!.config), ["mcp_servers.beta.enabled"]);
    }),
  );
});

describe("follow-ups: holds, sub-agents, unknown threads, per-call checks (fork issues #814, #918)", () => {
  it.effect("a turn Codex starts holds T3's requests until its set is fixed", () =>
    Effect.gen(function* () {
      T3ToolSets.__testing.clear();
      const fake = makeFakeCodex({ servers: ["alpha"] });
      const gate = yield* Deferred.make<void>();
      let gated = false;
      const profileFor: ProfileById = () =>
        gated
          ? Deferred.await(gate).pipe(Effect.as(resolved("lean", "all", ["waits"])))
          : Effect.succeed(ALL);
      const client = yield* withCodexMcpProfiles(fake.client, profileFor);
      const id = (yield* start(client, "t")).thread.id;
      gated = true;
      yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
      // Held from the notification on, before the profile is read: no request sees the old set.
      assert.isDefined(T3ToolSets.__testing.asked("t"));
      yield* Deferred.succeed(gate, undefined);
      yield* TestClock.withLive(Effect.sleep("20 millis"));
      assert.isUndefined(T3ToolSets.__testing.asked("t"));
      assert.deepStrictEqual(groupsOf("t"), ["waits"]);
    }),
  );

  it.effect("stopped half-way: a turn Codex starts as the client closes leaves no hold", () =>
    Effect.gen(function* () {
      T3ToolSets.__testing.clear();
      const fake = makeFakeCodex({ servers: ["alpha"] });
      const clientScope = yield* Scope.make();
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("lean", "all", ["waits"]) }).profileFor,
      ).pipe(Scope.provide(clientScope));
      const id = (yield* start(client, "t")).thread.id;
      // The session shuts down, then Codex's turn/started for the thread still arrives: the check
      // never runs, and it must not leave the thread's T3 requests held.
      yield* Scope.close(clientScope, Exit.void);
      yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
      assert.isUndefined(T3ToolSets.__testing.asked("t"));
    }),
  );

  it.effect("stopped half-way: a turn's check interrupted mid-way releases its hold", () =>
    Effect.gen(function* () {
      T3ToolSets.__testing.clear();
      const fake = makeFakeCodex({ servers: ["alpha"] });
      const reading = yield* Deferred.make<void>();
      let gated = false;
      const profileFor: ProfileById = () =>
        gated
          ? Deferred.succeed(reading, undefined).pipe(Effect.andThen(Effect.never))
          : Effect.succeed(ALL);
      const clientScope = yield* Scope.make();
      const client = yield* withCodexMcpProfiles(fake.client, profileFor).pipe(
        Scope.provide(clientScope),
      );
      const id = (yield* start(client, "t")).thread.id;
      gated = true;
      yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
      yield* Deferred.await(reading);
      assert.isDefined(T3ToolSets.__testing.asked("t"));
      // The session closes while the check reads the profile: the check is interrupted.
      yield* Scope.close(clientScope, Exit.void);
      assert.isUndefined(T3ToolSets.__testing.asked("t"));
    }),
  );

  it.effect("a sub-agent's turn, or a goal set active, checks but leaves the parent's set", () =>
    Effect.gen(function* () {
      T3ToolSets.__testing.clear();
      const fake = makeFakeCodex({ servers: ["alpha"] });
      const { profiles, profileFor } = makeProfiles({ t: resolved("a", "all", ["threads"]) });
      const client = yield* withCodexMcpProfiles(fake.client, profileFor);
      const id = (yield* start(client, "t")).thread.id;
      yield* turn(client, id);
      assert.deepStrictEqual(groupsOf("t"), ["threads"]);
      const child = fake.spawnChild(id);
      yield* fake.emit("item/completed", spawned(id, [child]));
      // The parent's turn still runs with its set; a change reaches its next turn only.
      profiles.set("t", resolved("a", "all", ["waits"]));
      yield* fake.emit("turn/started", turnStarted(child, "child-1"));
      yield* TestClock.withLive(Effect.sleep("20 millis"));
      assert.deepStrictEqual(groupsOf("t"), ["threads"]);
      assert.strictEqual(
        yield* outcome(
          client.request("thread/goal/set", { threadId: id, status: "active" } as never),
        ),
        "ok",
      );
      assert.deepStrictEqual(groupsOf("t"), ["threads"]);
      // The goal's own turn, which Codex starts, fixes it.
      yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
      yield* TestClock.withLive(Effect.sleep("20 millis"));
      assert.deepStrictEqual(groupsOf("t"), ["waits"]);
    }),
  );

  it.effect(
    "a turn on a Codex thread T3 has not seen: adopted when Codex names its parent, else stopped",
    () =>
      Effect.gen(function* () {
        const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
        const { profiles, profileFor } = makeProfiles({ t: resolved("a", ["alpha"]) });
        const client = yield* withCodexMcpProfiles(fake.client, profileFor);
        const seen = yield* completions(client);
        const id = (yield* start(client, "t")).thread.id;
        // A sub-agent whose spawn item T3 has not seen yet: thread/read names its parent.
        const child = fake.spawnChild(id);
        profiles.set("t", resolved("a", ["alpha", "beta"]));
        yield* fake.emit("turn/started", turnStarted(child, "child-1"));
        yield* fake.reached("mcpServerStatus/list", 1);
        assert.strictEqual(fake.sent("mcpServerStatus/list")[0]!.params.threadId, child);
        assert.deepStrictEqual(fake.sent("turn/interrupt"), []);
        // A thread Codex names no parent for, and T3 never sees spawned, is stopped after a wait.
        const stray = (
          (yield* fake.client.request("thread/start", {} as never)) as {
            readonly thread: { readonly id: string };
          }
        ).thread.id;
        const readsBefore = fake.sent("thread/read").length;
        const waited = yield* Effect.forkChild(
          Effect.gen(function* () {
            yield* fake.emit("turn/started", turnStarted(stray, "stray-1"));
            yield* fake.reached("turn/interrupt", 1);
          }),
        );
        // Codex names no parent for it; then T3 waits for the spawn to show.
        yield* fake.reached("thread/read", readsBefore + 1);
        yield* drive(waited, CODEX_ADOPT_WAIT_MS);
        assert.deepStrictEqual(fake.sent("turn/interrupt")[0]!.params, {
          threadId: stray,
          turnId: "stray-1",
        });
        yield* fake.emit("turn/completed", turnCompleted(stray, "stray-1"));
        assert.include(
          (seen.at(-1)!.error as { readonly message: string }).message,
          "did not load this Codex thread",
        );
      }),
  );

  it.effect("T3's own turn reads the profile the turn check worked out, once per turn", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      let reads = 0;
      const profileFor: ProfileById = () =>
        Effect.sync(() => void (reads += 1)).pipe(Effect.as(resolved("a", ["alpha"])));
      const client = yield* withCodexMcpProfiles(fake.client, profileFor);
      const id = (yield* start(client, "t")).thread.id;
      reads = 0;
      const response = (yield* turn(client, id).pipe(
        Effect.provideService(CurrentTurnMcpProfile, {
          threadId: ThreadId.make("t"),
          resolved: resolved("a", ["alpha"]),
        }),
      )) as { readonly turn: { readonly id: string } };
      const listed = fake.sent("mcpServerStatus/list").length;
      assert.isAbove(listed, 0);
      // Codex reports T3's own turn started: it was checked before it was sent.
      yield* fake.emit("turn/started", turnStarted(id, response.turn.id));
      yield* TestClock.withLive(Effect.sleep("20 millis"));
      assert.strictEqual(reads, 0);
      assert.strictEqual(fake.sent("mcpServerStatus/list").length, listed);
    }),
  );

  it.effect("MCP Apps calls and elicitations to a server outside the profile are refused", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("a", ["alpha"]) }).profileFor,
      );
      let asked = 0;
      yield* client.handleServerRequest("mcpServer/elicitation/request", () =>
        Effect.sync(() => void (asked += 1)).pipe(Effect.as({ action: "accept" as const })),
      );
      const id = (yield* start(client, "t")).thread.id;
      const call = (server: string) =>
        outcome(
          client.request("mcpServer/tool/call", { threadId: id, server, tool: "x" } as never),
        );
      assert.include(yield* call("beta"), 'MCP server "beta" is not in this thread\'s MCP profile');
      assert.include(
        yield* outcome(
          client.request("mcpServer/resource/read", {
            threadId: id,
            server: "beta",
            uri: "ui://x",
          } as never),
        ),
        "not in this thread's MCP profile",
      );
      assert.strictEqual(yield* call("alpha"), "ok");
      assert.strictEqual(yield* call("t3-code"), "ok");
      assert.deepStrictEqual(
        fake.sent("mcpServer/tool/call").map((sent) => sent.params.server),
        ["alpha", "t3-code"],
      );
      const elicit = (serverName: string) =>
        fake.ask("mcpServer/elicitation/request", {
          threadId: id,
          serverName,
          message: "ok?",
          mode: "form",
          requestedSchema: { type: "object", properties: {} },
        });
      assert.deepStrictEqual(yield* elicit("beta"), { action: "decline" });
      assert.strictEqual(asked, 0);
      assert.deepStrictEqual(yield* elicit("alpha"), { action: "accept" });
      assert.strictEqual(asked, 1);
    }),
  );

  it.effect("a server of the user's named like T3's is switched off unless T3 carries it", () =>
    Effect.gen(function* () {
      assert.deepStrictEqual([...codexCarriedT3McpServers(threadConfig("t"))].sort(), [
        "t3-code",
        "t3-code-direct",
      ]);
      const fake = makeFakeCodex({ servers: ["alpha", "t3-code"] });
      const client = yield* withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("a", ["alpha"]) }).profileFor,
      );
      // A load with no T3 server of its own (T3's MCP session was not set up).
      const {
        "mcp_servers.t3-code": _,
        "mcp_servers.t3-code-direct": __,
        ...bare
      } = threadConfig("t");
      const id = (
        (yield* client.request("thread/start", { cwd: "/workspace", config: bare } as never)) as {
          readonly thread: { readonly id: string };
        }
      ).thread.id;
      assert.deepStrictEqual(offKeys(fake.threads.get(id)!.config), [
        "mcp_servers.t3-code.enabled",
      ]);
      assert.strictEqual(yield* outcome(turn(client, id)), "ok");
      // One T3 does carry stays on.
      const own = (yield* start(client, "t")).thread.id;
      assert.deepStrictEqual(offKeys(fake.threads.get(own)!.config), []);
    }),
  );

  it.effect("a thread the store does not have yet is read again before it is refused", () =>
    Effect.gen(function* () {
      let reads = 0;
      const lagging = afterStoreLag(() =>
        Effect.suspend(() =>
          ++reads < 3 ? Effect.fail(threadNotInStore()) : Effect.succeed(resolved("a", "all")),
        ),
      );
      const found = yield* drive(yield* Effect.forkChild(lagging(ThreadId.make("t"))), 1_750);
      assert.strictEqual(found.name, "a");
      assert.strictEqual(reads, 3);
      // Any other refusal stands at once.
      reads = 0;
      const refused = afterStoreLag(() =>
        Effect.sync(() => void (reads += 1)).pipe(
          Effect.andThen(Effect.fail(new ClaudeMcpProfileError({ reason: "gone" }))),
        ),
      );
      assert.include(yield* outcome(refused(ThreadId.make("t"))), "gone");
      assert.strictEqual(reads, 1);
    }),
  );
});

/** Moves test time on by `millis` (each sleep in turn wakes), then joins the fiber. */
const drive = <A, E>(fiber: Fiber.Fiber<A, E>, millis: number) =>
  TestClock.adjust(millis).pipe(Effect.andThen(Fiber.join(fiber)));

describe("a newer turn's T3 tool set is never overwritten by an older one (fork issue #986)", () => {
  /**
   * The thread's profile reads, in order: each is `all` once the queue is empty, or waits for its
   * gate and then gives its profile.
   */
  const scripted = () => {
    const queue: Array<Effect.Effect<McpProfileResolved>> = [];
    const profileFor: ProfileById = () => queue.shift() ?? Effect.succeed(ALL);
    const gated = (profile: McpProfileResolved) =>
      Effect.gen(function* () {
        const reached = yield* Deferred.make<void>();
        const open = yield* Deferred.make<void>();
        queue.push(
          Deferred.succeed(reached, undefined).pipe(
            Effect.andThen(Deferred.await(open)),
            Effect.as(profile),
          ),
        );
        return { reached, open };
      });
    const next = (profile: McpProfileResolved) => void queue.push(Effect.succeed(profile));
    return { profileFor, gated, next };
  };
  /**
   * The thread's hold, for its `done`: done once every check holding it has released, which each
   * does after fixing (or not) its set. A check whose profile read is not gated runs inside `emit`
   * (forked to start at once, nothing to wait on), so only a gated read's end needs awaiting.
   */
  const holdOf = (threadId: string) => T3ToolSets.t3ToolSetHold(threadId, 0)!.done;
  const TIMED_OUT = T3ToolSets.refusedT3ToolSet("timed out");
  /** What the T3 tool rule does as a request's wait on the hold reaches its bound. */
  const dropAtBound = (threadId: string) =>
    T3ToolSets.dropT3ToolSetHold(
      threadId,
      T3ToolSets.t3ToolSetHold(threadId, Number.MAX_SAFE_INTEGER)!,
      TIMED_OUT,
    );
  const LEAN = resolved("lean", "all", ["waits"]);

  it.effect("two checks finishing out of order: the later turn's set stands", () =>
    Effect.gen(function* () {
      T3ToolSets.__testing.clear();
      const fake = makeFakeCodex({ servers: ["alpha"] });
      const { profileFor, gated, next } = scripted();
      const client = yield* withCodexMcpProfiles(fake.client, profileFor);
      const id = (yield* start(client, "t")).thread.id;
      // goal-2's check reads `all` slowly; the profile is narrowed; goal-3's check reads lean.
      const slow = yield* gated(ALL);
      next(LEAN);
      yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
      yield* Deferred.await(slow.reached);
      yield* fake.emit("turn/started", turnStarted(id, "goal-3"));
      assert.deepStrictEqual(groupsOf("t"), ["waits"]);
      // goal-2's check finishes last: it must not widen the thread back to `all`.
      const done = holdOf("t");
      yield* Deferred.succeed(slow.open, undefined);
      yield* Deferred.await(done);
      assert.deepStrictEqual(groupsOf("t"), ["waits"]);
      assert.isUndefined(T3ToolSets.__testing.asked("t"));
      // Runs again: the thread's next turn fixes its set as usual.
      next(resolved("threads", "all", ["threads"]));
      yield* fake.emit("turn/started", turnStarted(id, "goal-4"));
      assert.deepStrictEqual(groupsOf("t"), ["threads"]);
    }),
  );

  it.effect("a joining turn fixed its set while the first is stuck: the drop keeps it", () =>
    Effect.gen(function* () {
      T3ToolSets.__testing.clear();
      const fake = makeFakeCodex({ servers: ["alpha"] });
      const { profileFor, gated, next } = scripted();
      const client = yield* withCodexMcpProfiles(fake.client, profileFor);
      const id = (yield* start(client, "t")).thread.id;
      const stuck = yield* gated(ALL);
      next(LEAN);
      yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
      yield* Deferred.await(stuck.reached);
      // goal-3 joins goal-2's hold (and its bound), and fixes its set; goal-2's check still runs.
      yield* fake.emit("turn/started", turnStarted(id, "goal-3"));
      assert.deepStrictEqual(groupsOf("t"), ["waits"]);
      assert.isDefined(T3ToolSets.__testing.asked("t"));
      // The bound passes: the hold goes, but goal-3's set stands (not Refused over it).
      const done = holdOf("t");
      assert.isFalse(dropAtBound("t"));
      assert.isUndefined(T3ToolSets.__testing.asked("t"));
      assert.deepStrictEqual(groupsOf("t"), ["waits"]);
      // goal-2's late check gives nothing either.
      yield* Deferred.succeed(stuck.open, undefined);
      yield* Deferred.await(done);
      assert.deepStrictEqual(groupsOf("t"), ["waits"]);
    }),
  );

  it.effect(
    "a turn T3 starts itself while a Codex turn's hold is stuck: the drop keeps its set",
    () =>
      Effect.gen(function* () {
        T3ToolSets.__testing.clear();
        const fake = makeFakeCodex({ servers: ["alpha"] });
        const { profileFor, gated, next } = scripted();
        const client = yield* withCodexMcpProfiles(fake.client, profileFor);
        const id = (yield* start(client, "t")).thread.id;
        const stuck = yield* gated(ALL);
        yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
        yield* Deferred.await(stuck.reached);
        // T3's own turn does not touch the hold, but it is later than goal-2: its set stands.
        next(LEAN);
        assert.strictEqual(yield* outcome(turn(client, id)), "ok");
        assert.deepStrictEqual(groupsOf("t"), ["waits"]);
        const done = holdOf("t");
        assert.isFalse(dropAtBound("t"));
        assert.deepStrictEqual(groupsOf("t"), ["waits"]);
        yield* Deferred.succeed(stuck.open, undefined);
        yield* Deferred.await(done);
        assert.deepStrictEqual(groupsOf("t"), ["waits"]);
      }),
  );

  it.effect("a hold whose check never fixes a set: the drop still refuses (fails closed)", () =>
    Effect.gen(function* () {
      T3ToolSets.__testing.clear();
      const fake = makeFakeCodex({ servers: ["alpha"] });
      const { profileFor, gated } = scripted();
      const client = yield* withCodexMcpProfiles(fake.client, profileFor);
      const id = (yield* start(client, "t")).thread.id;
      T3ToolSets.fixT3ToolSet(
        "t",
        { _tag: "Groups", name: "all", groups: "all" },
        T3ToolSets.nextT3ToolSetTurn(),
      );
      const stuck = yield* gated(LEAN);
      yield* fake.emit("turn/started", turnStarted(id, "goal-2"));
      yield* Deferred.await(stuck.reached);
      const done = holdOf("t");
      assert.isTrue(dropAtBound("t"));
      assert.strictEqual(groupsOf("t"), "Refused");
      // The stuck check is that turn's: when it finishes late, its set replaces the refusal.
      yield* Deferred.succeed(stuck.open, undefined);
      yield* Deferred.await(done);
      assert.deepStrictEqual(groupsOf("t"), ["waits"]);
    }),
  );
});

describe("profiledClientFactory (settings and thread store)", () => {
  const NOW = DateTime.makeUnsafe("2026-11-21T00:00:00.000Z");
  const PROJECT = ProjectId.make("project");
  const CODEX = ProviderInstanceId.make("codex");
  const storedThread = (id: string, parent?: string): OrchestrationV2AppThread => {
    const threadId = ThreadId.make(id);
    const base = makeTurnInput({
      threadId,
      instanceId: CODEX,
      providerThread: {
        id: ProviderThreadId.make(`provider-thread:${id}`),
        threadId,
        providerInstanceId: CODEX,
      } as never,
      runtimePolicy: ProviderAdapter.ProviderAdapterV2RuntimePolicy.make({
        runtimeMode: "full-access",
        interactionMode: "default",
        cwd: "/workspace",
      }),
      now: NOW,
    }).appThread;
    return {
      ...base,
      projectId: PROJECT,
      lineage: {
        parentThreadId: parent === undefined ? null : ThreadId.make(parent),
        relationshipToParent: parent === undefined ? null : "subagent",
        rootThreadId: ThreadId.make(parent ?? id),
      },
    };
  };

  const withFactory = <A, E>(
    settings: Record<string, unknown>,
    stored: ReadonlyArray<OrchestrationV2AppThread>,
    body: (input: {
      readonly fake: ReturnType<typeof makeFakeCodex>;
      readonly client: Client;
    }) => Effect.Effect<A, E>,
  ) =>
    Effect.gen(function* () {
      const store = yield* ProjectionStore.ProjectionStoreV2;
      for (const thread of stored) {
        yield* store.apply({
          id: EventId.make(`seed:${thread.id}`),
          type: "thread.created",
          threadId: thread.id,
          occurredAt: NOW,
          payload: thread,
        } as never);
      }
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const opened: Array<unknown> = [];
      const factory: CodexAdapterV2.CodexAppServerClientFactoryShape = {
        open: (input) => Effect.sync(() => void opened.push(input)).pipe(Effect.as(fake.client)),
      };
      const { clientFactory } = yield* profiledClientFactory(factory);
      const client = yield* clientFactory!.open({ threadId: ThreadId.make("opener") } as never);
      assert.strictEqual(opened.length, 1);
      return yield* body({ fake, client });
    }).pipe(
      Effect.scoped,
      Effect.provide(
        Layer.mergeAll(
          ServerSettingsService.ServerSettingsService.layerTest(settings as never),
          ProjectionStore.layer.pipe(Layer.provideMerge(SqlitePersistence.layerMemory)),
        ).pipe(Layer.provideMerge(NodeServices.layer)),
      ),
    );

  it.effect(
    "a thread's pick from Settings applies on Codex, by the thread each request names",
    () =>
      withFactory(
        {
          mcpProfiles: { "alpha-only": { servers: ["alpha"], t3Tools: "all" } },
          mcpThreadProfiles: { picked: "alpha-only" },
        },
        [storedThread("picked"), storedThread("plain")],
        ({ fake, client }) =>
          Effect.gen(function* () {
            const picked = (yield* start(client, "picked")).thread.id;
            const plain = (yield* start(client, "plain")).thread.id;
            assert.deepStrictEqual(offKeys(fake.threads.get(picked)!.config), [
              "mcp_servers.beta.enabled",
            ]);
            assert.deepStrictEqual(fake.threads.get(plain)!.config, threadConfig("plain"));
            assert.strictEqual(yield* outcome(turn(client, picked)), "ok");
          }),
      ),
  );

  it.effect("a sub-agent is cut to its parent's profile", () =>
    withFactory(
      { mcpThreadProfiles: { parent: "t3-only" } },
      [storedThread("parent"), storedThread("child", "parent")],
      ({ fake, client }) =>
        Effect.gen(function* () {
          const child = (yield* start(client, "child")).thread.id;
          assert.deepStrictEqual(offKeys(fake.threads.get(child)!.config), [
            "mcp_servers.alpha.enabled",
            "mcp_servers.beta.enabled",
          ]);
        }),
    ),
  );

  it.effect("a missing profile, or a thread the store does not have, refuses", () =>
    withFactory(
      { mcpThreadProfiles: { lost: "gone" } },
      [storedThread("lost")],
      ({ fake, client }) =>
        Effect.gen(function* () {
          const lost = yield* outcome(start(client, "lost"));
          assert.include(lost, '"gone"');
          // Read again three times over 1.75 s (the store may lag a new thread), then refused.
          const unknown = yield* drive(
            yield* Effect.forkChild(outcome(start(client, "not-stored"))),
            1_750,
          );
          assert.include(unknown, "cannot read this thread from its store");
          assert.include(unknown, FIX);
          assert.deepStrictEqual(fake.sent("thread/start"), []);
        }),
    ),
  );
});

describe("CodexAdapterV2 with MCP profiles (the seam lines)", () => {
  it.effect("a thread the adapter starts gets its profile's servers turned off", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"], launchArgs: ["gamma"] });
      const store = yield* ProjectionStore.ProjectionStoreV2;
      const threadId = ThreadId.make("adapter-thread");
      const NOW = DateTime.makeUnsafe("2026-11-21T00:00:00.000Z");
      yield* store.apply({
        id: EventId.make("seed:adapter-thread"),
        type: "thread.created",
        threadId,
        occurredAt: NOW,
        payload: {
          ...makeTurnInput({
            threadId,
            instanceId: CodexAdapterV2.CODEX_DEFAULT_INSTANCE_ID,
            providerThread: { id: ProviderThreadId.make("p"), threadId } as never,
            runtimePolicy: POLICY,
            now: NOW,
          }).appThread,
          projectId: ProjectId.make("project"),
        },
      } as never);
      const adapter = yield* CodexAdapterV2.createCodexAdapterV2({
        instanceId: CodexAdapterV2.CODEX_DEFAULT_INSTANCE_ID,
        displayName: undefined,
        environment: [],
        enabled: true,
        config: decodeCodexSettings({}),
      }).pipe(
        Effect.provideService(CodexAdapterV2.CodexAppServerClientFactory, {
          open: () => Effect.succeed(fake.client),
        }),
      );
      const runtime = yield* adapter.openSession({
        threadId,
        providerSessionId: ProviderSessionId.make("session-mcp-profile"),
        modelSelection: { instanceId: CodexAdapterV2.CODEX_DEFAULT_INSTANCE_ID, model: "gpt-5.4" },
        runtimePolicy: POLICY,
      });
      yield* runtime.ensureThread({
        threadId,
        modelSelection: { instanceId: CodexAdapterV2.CODEX_DEFAULT_INSTANCE_ID, model: "gpt-5.4" },
        runtimePolicy: POLICY,
      });
      const [sent] = fake.sent("thread/start");
      assert.deepStrictEqual(offKeys(sent!.params.config as Json), [
        "mcp_servers.beta.enabled",
        "mcp_servers.gamma.enabled",
      ]);
    }).pipe(
      Effect.scoped,
      Effect.provide(
        Layer.mergeAll(
          Layer.effect(
            ServerConfig.ServerConfig,
            makeReplayServerConfig("codex-mcp-profile").pipe(Effect.orDie),
          ),
          IdAllocator.layer,
          PortScanner.layerTest,
          McpProviderSessions.layer,
          ServerSettingsService.ServerSettingsService.layerTest({
            mcpProfiles: { "alpha-only": { servers: ["alpha"], t3Tools: "all" } },
            mcpThreadProfiles: { "adapter-thread": "alpha-only" },
          } as never),
          ProjectionStore.layer.pipe(Layer.provideMerge(SqlitePersistence.layerMemory)),
        ).pipe(Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))),
      ),
    ),
  );
});
