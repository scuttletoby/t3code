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
import * as Schema from "effect/Schema";

import { makeTurnInput } from "../orchestration-v2/Adapters/AcpAdapterV2.testkit.ts";
import * as CodexAdapterV2 from "../orchestration-v2/Adapters/CodexAdapterV2.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import { makeReplayServerConfig } from "../orchestration-v2/testkit/ProviderReplayHarness.ts";
import * as ServerConfig from "../config.ts";
import * as PortScanner from "../preview/PortScanner.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import { ClaudeMcpProfileError } from "./claudeMcpProfile.ts";
import {
  codexConfiguredMcpServers,
  codexMcpProfileConfig,
  codexMcpServersOutsideProfile,
  profiledClientFactory,
  withCodexMcpProfiles,
  type ProfileById,
} from "./codexMcpProfile.ts";
import type { McpProfileResolved, McpProfileSet } from "./mcpProfile.ts";

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
}

/**
 * A Codex app-server as far as profiles see it. `servers` is config.toml, `launchArgs` the
 * servers `-c mcp_servers.<name>...` adds, `project` each workspace's `.codex/config.toml`, and
 * `plugins` servers a plugin adds, which no config entry switches off.
 */
const makeFakeCodex = (
  setup: {
    readonly servers?: ReadonlyArray<string>;
    readonly launchArgs?: ReadonlyArray<string>;
    readonly project?: Readonly<Record<string, ReadonlyArray<string>>>;
    readonly plugins?: ReadonlyArray<string>;
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
          : Effect.succeed({ thread: { id: found.id, cwd: found.cwd } });
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
          ...Object.keys(found.config)
            .filter((key) => /^mcp_servers\.[^.]+$/.test(key))
            .map((key) => key.slice("mcp_servers.".length)),
        ];
        const data = [...new Set(names)].map((name) => ({
          name,
          runtimeStatus:
            found.config[`mcp_servers.${name}.enabled`] === false && !setup.plugins?.includes(name)
              ? "disabled"
              : "ready",
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
  const request = (method: string, params: unknown) =>
    Effect.gen(function* () {
      calls.push({ method, params: (params ?? {}) as Json });
      const gate = gates.get(method);
      if (gate !== undefined) {
        yield* Deferred.succeed(gate.reached, undefined);
        yield* Deferred.await(gate.open);
      }
      return yield* reply(method, (params ?? {}) as Json);
    });
  const client = {
    request,
    raw: { request, requests: undefined as never },
    notify: () => Effect.void,
    awaitTermination: Effect.never,
    handleServerRequest: () => Effect.void,
    handleServerNotification: () => Effect.void,
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
    setPageSize: (size: number) => void (pageSize = size),
    setEndlessPages: () => void (endlessPages = true),
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

describe("codexMcpProfile helpers", () => {
  it("turns off every configured server the profile leaves out, never T3's own", () => {
    assert.deepStrictEqual(
      codexMcpProfileConfig("mine", { servers: ["alpha"], t3Tools: "all" }, [
        "alpha",
        "beta",
        "t3-code",
        "t3-code-direct",
      ]),
      { "mcp_servers.beta.enabled": false },
    );
    assert.deepStrictEqual(
      codexMcpProfileConfig("all", { servers: "all", t3Tools: "all" }, ["alpha"]),
      {},
    );
    // A kept name that is not set up keeps nothing and is not an error (design §9).
    assert.deepStrictEqual(
      codexMcpProfileConfig("mine", { servers: ["nowhere"], t3Tools: "all" }, []),
      {},
    );
  });

  it("refuses a profile it cannot apply, with the fix", () => {
    const tools = codexMcpProfileConfig("lean", { servers: "all", t3Tools: ["threads"] }, []);
    assert.isTrue(isRefusal(tools));
    assert.include((tools as ClaudeMcpProfileError).reason, 'set its t3Tools to "all"');
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
      { name: "alpha", runtimeStatus: "ready" },
      { name: "beta", runtimeStatus: "disabled" },
      { name: "gamma", runtimeStatus: "starting" },
      { name: "t3-code", runtimeStatus: "ready" },
    ];
    assert.deepStrictEqual(codexMcpServersOutsideProfile(["alpha"], statuses), ["gamma"]);
    assert.deepStrictEqual(codexMcpServersOutsideProfile("all", statuses), []);
  });
});

describe("withCodexMcpProfiles", () => {
  it.effect(
    "a left-out server gets enabled=false on that thread only; kept and T3's own stay as sent",
    () =>
      Effect.gen(function* () {
        const fake = makeFakeCodex({ servers: ["alpha", "beta"], launchArgs: ["gamma"] });
        const { profileFor } = makeProfiles({ narrow: resolved("alpha-only", ["alpha"]) });
        const client = withCodexMcpProfiles(fake.client, profileFor);
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
      const client = withCodexMcpProfiles(fake.client, profileFor);
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
      const client = withCodexMcpProfiles(fake.client, profileFor);
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
      const client = withCodexMcpProfiles(fake.client, profileFor);
      const message = yield* outcome(start(client, "missing"));
      assert.include(message, 'MCP profile "gone" is not in Settings');
      assert.include(message, FIX);
      assert.deepStrictEqual(fake.sent("thread/start"), []);
    }),
  );

  it.effect("a profile that limits T3's tools, or a server name Codex cannot take, refuses", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "my server"] });
      const { profileFor } = makeProfiles({
        tools: resolved("lean", "all", ["threads"]),
        named: resolved("none", []),
      });
      const client = withCodexMcpProfiles(fake.client, profileFor);
      assert.include(yield* outcome(start(client, "tools")), 'set its t3Tools to "all"');
      const named = yield* outcome(start(client, "named"));
      assert.include(named, '"my server"');
      assert.include(named, FIX);
      assert.deepStrictEqual(fake.sent("thread/start"), []);
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
      const client = withCodexMcpProfiles(
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
      const client = withCodexMcpProfiles(fake.client, makeProfiles().profileFor);
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

  it.effect("an empty server list turns nothing off and the thread runs", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex();
      const client = withCodexMcpProfiles(
        fake.client,
        makeProfiles({ t: resolved("none", []) }).profileFor,
      );
      const id = (yield* start(client, "t")).thread.id;
      assert.deepStrictEqual(fake.threads.get(id)!.config, threadConfig("t"));
      assert.strictEqual(yield* outcome(turn(client, id)), "ok");
    }),
  );

  it.effect("runs again: starting or resuming twice sends the same set", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const client = withCodexMcpProfiles(
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
        const client = withCodexMcpProfiles(fake.client, profileFor);
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

  it.effect("a widened profile runs at once: the thread keeps fewer servers until reloaded", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const { profiles, profileFor } = makeProfiles({ t: resolved("a", ["alpha"]) });
      const client = withCodexMcpProfiles(fake.client, profileFor);
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
      const client = withCodexMcpProfiles(
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
      const client = withCodexMcpProfiles(
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
      const before = withCodexMcpProfiles(fake.client, profileFor);
      const id = (yield* start(before, "t")).thread.id;
      // The new process: same threads on disk, none loaded, and a new wrapped client.
      fake.unload(id);
      const after = withCodexMcpProfiles(fake.client, profileFor);
      // Nothing narrowed on it yet, so a turn on a thread it never loaded is left to Codex.
      yield* resume(after, id, "t");
      assert.deepStrictEqual(offKeys(fake.threads.get(id)!.config), ["mcp_servers.beta.enabled"]);
      assert.strictEqual(yield* outcome(turn(after, id)), "ok");
    }),
  );

  it.effect("with a profile in use, a thread T3 did not load itself is refused", () =>
    Effect.gen(function* () {
      const fake = makeFakeCodex({ servers: ["alpha", "beta"] });
      const client = withCodexMcpProfiles(
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
      const client = withCodexMcpProfiles(
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
      const client = withCodexMcpProfiles(
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
          const unknown = yield* outcome(start(client, "not-stored"));
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
