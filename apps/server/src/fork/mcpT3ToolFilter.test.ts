/**
 * Fork: the MCP profile's T3 tool groups over the real `/mcp` transport, a real thread store and
 * upstream's settings test service: `tools/list`, `tools/call` (by any name, or in a batch) and
 * `tool_search`, the ways an agent reaches a T3 tool.
 */
import { NodeHttpServer } from "@effect/platform-node";
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { expect, it } from "@effect/vitest";
import {
  EnvironmentId,
  EventId,
  type OrchestrationV2AppThread,
  ProjectId,
  ProviderInstanceId,
  ProviderThreadId,
  ThreadId,
} from "@t3tools/contracts";
import * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as TestClock from "effect/testing/TestClock";
import { HttpBody, HttpClient, HttpRouter } from "effect/http";
import { describe } from "vite-plus/test";

import * as GitManager from "../git/GitManager.ts";
import * as McpHttpServer from "../mcp/McpHttpServer.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpSessionRegistry from "../mcp/McpSessionRegistry.ts";
import * as McpToolAccessTestkit from "../mcp/McpToolAccess.testkit.ts";
import { makeTurnInput } from "../orchestration-v2/Adapters/AcpAdapterV2.testkit.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import { Playtest } from "../playtest/PlaytestLayer.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as PullRequestReviewGuideService from "../pullRequest/PullRequestReviewGuideService.ts";
import * as PullRequestService from "../pullRequest/PullRequestService.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import { Waits } from "../waits/WaitWorker.ts";
import { makeClaudeMcpProfiles } from "./claudeMcpProfile.ts";
import * as McpT3ToolFilter from "./mcpT3ToolFilter.ts";
import * as McpT3ToolRule from "./mcpT3ToolRule.ts";
import * as T3ToolSets from "./mcpT3ToolSets.ts";

const ENVIRONMENT = EnvironmentId.make("environment-t3-tool-groups");
const PROJECT = ProjectId.make("project-t3-tool-groups");
const NOW = DateTime.makeUnsafe("2026-11-21T00:00:00.000Z");
const CODEX = ProviderInstanceId.make("codex");

const WAIT_TOOLS = ["wait_for", "wait_list", "wait_cancel", "task_list"];
const PR_TOOL = "list_thread_pull_requests";

/** The wait tools list only while waits are on. */
const WAITS_ON = { waitFor: { enabled: true } };

const SETTINGS = {
  ...WAITS_ON,
  mcpProfiles: {
    prs: { servers: "all", t3Tools: ["pull-requests"] },
    waits: { servers: "all", t3Tools: ["waits"] },
    none: { servers: "all", t3Tools: [] },
  },
  mcpSubagentProfile: "waits",
  mcpThreadProfiles: {
    "thread-prs": "prs",
    "thread-waits": "waits",
    "thread-none": "none",
    "thread-gone": "gone",
  },
};

const appThread = (id: string, parent?: string): OrchestrationV2AppThread => {
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

/** Threads in the store. `thread-deleted` has a credential but no row. */
const STORED = [
  appThread("thread-all"),
  appThread("thread-prs"),
  appThread("thread-waits"),
  appThread("thread-none"),
  appThread("thread-gone"),
  // Its own role default is `waits` (mcpSubagentProfile), cut down to its parent's `prs`.
  appThread("thread-child", "thread-prs"),
];

const scopeFor = (token: string): McpInvocationContext.McpThreadInvocationScope => ({
  environmentId: ENVIRONMENT,
  requestNamespace: token,
  thread: {
    threadId: ThreadId.make(`thread-${token}`),
    providerSessionId: `session-${token}`,
    providerInstanceId: CODEX,
    subagent: token === "child",
  },
  client: undefined,
  capabilities: new Set(["orchestration", "pull-requests", "issues", "worktree"]),
  issuedAt: 1,
});

const TOKENS = ["all", "prs", "waits", "none", "gone", "child", "deleted"];

/** Every `waits.list` the server ran: a call that reached the handler. */
let listed = 0;

const serverLayer = <S>(shared: Layer.Layer<S>) => {
  const scopes = new Map<string, McpInvocationContext.McpInvocationScope>(
    TOKENS.map((token) => [token, scopeFor(token)]),
  );
  // An outside MCP client of this machine: no thread, so no profile.
  scopes.set("outside", { ...scopeFor("outside"), thread: undefined });
  return McpHttpServer.layerMcpTransport(
    Layer.mergeAll(
      McpHttpServer.layerPullRequestsToolkit,
      McpHttpServer.layerForkTools,
      McpHttpServer.layerToolSearch,
      McpT3ToolRule.layer,
    ),
  ).pipe(
    Layer.provide(
      Layer.mock(McpSessionRegistry.McpSessionRegistry)({
        resolve: (token) => Effect.succeed(scopes.get(token) as never),
      }),
    ),
    Layer.provide(
      Layer.mergeAll(
        Layer.mock(ThreadManagement.ThreadManagementService)({
          getThreadShell: (threadId) =>
            Effect.succeed({
              ...McpToolAccessTestkit.liveThreadShell(threadId),
              projectId: PROJECT,
            }),
        }),
        Layer.mock(Waits)({
          list: () =>
            Effect.sync(() => {
              listed += 1;
              return { enabled: false, waits: [], checks: [] };
            }),
        }),
        Layer.mock(Playtest)({ filePath: "unused" }),
        Layer.mock(RemoteDelegation.RemoteDelegation)({}),
        Layer.mock(PeerForwarding.PeerForwarding)({}),
        Layer.mock(ProjectService.ProjectService)({}),
        Layer.mock(PullRequestReviewGuideService.PullRequestReviewGuideService)({}),
        Layer.mock(PullRequestService.PullRequestService)({
          servesProject: () => Effect.succeed(true),
        }),
        Layer.mock(GitManager.GitManager)({}),
        Layer.mock(Orchestrator.OrchestratorV2)({}),
      ),
    ),
    Layer.provide(shared),
    Layer.provide(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer)),
  );
};

/** The settings and the thread store, shared by the server and the test. */
const sharedLayer = (settings: object) =>
  Layer.mergeAll(
    ServerSettingsService.ServerSettingsService.layerTest(settings as never),
    ProjectionStore.layer.pipe(Layer.provideMerge(SqlitePersistence.layerMemory)),
  ).pipe(Layer.provideMerge(NodeServices.layer));

interface RpcReply {
  readonly id?: number;
  readonly result?: {
    readonly tools?: ReadonlyArray<{ readonly name: string }>;
    readonly isError?: boolean;
    readonly structuredContent?: unknown;
  };
  readonly error?: { readonly code: number; readonly message: string };
}

/** Starts the server with `settings` and the threads stored, then runs `body` against it. */
const withServer = <A, E, R>(
  body: (mcp: {
    readonly rpc: (token: string, payload: unknown) => Effect.Effect<unknown>;
    readonly list: (token: string) => Effect.Effect<ReadonlyArray<string>>;
    readonly call: (token: string, name: string, args?: object) => Effect.Effect<RpcReply>;
  }) => Effect.Effect<A, E, R>,
  settings: object = SETTINGS,
) =>
  Effect.scoped(
    Effect.gen(function* () {
      T3ToolSets.__testing.clear();
      McpT3ToolRule.__testing.clearRefused();
      listed = 0;
      const shared = yield* Layer.build(sharedLayer(settings));
      const store = yield* ProjectionStore.ProjectionStoreV2.pipe(Effect.provide(shared));
      for (const thread of STORED) {
        yield* store.apply({
          id: EventId.make(`seed:${thread.id}`),
          type: "thread.created",
          threadId: thread.id,
          occurredAt: NOW,
          payload: thread,
        } as never);
      }
      yield* HttpRouter.serve(serverLayer(Layer.succeedContext(shared)), {
        disableListenLog: true,
        disableLogger: true,
      }).pipe(Layer.build);
      const http = yield* HttpClient.HttpClient;
      const sessions = new Map<string, string>();
      const rpc = (token: string, payload: unknown) =>
        Effect.gen(function* () {
          const headers = {
            accept: "application/json, text/event-stream",
            authorization: `Bearer ${token}`,
          };
          let session = sessions.get(token);
          if (session === undefined) {
            const initialize = yield* http.post("/mcp", {
              headers,
              body: HttpBody.text(
                `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1.0.0"}}}`,
                "application/json",
              ),
            });
            session = initialize.headers["mcp-session-id"]!;
            sessions.set(token, session);
          }
          const reply = yield* http.post("/mcp", {
            headers: {
              ...headers,
              "mcp-session-id": session,
              "mcp-protocol-version": "2025-06-18",
            },
            body: HttpBody.text(JSON.stringify(payload), "application/json"),
          });
          const text = yield* reply.text;
          if (text === "") return { status: reply.status };
          // A reply that also sends a notification (tool_search's list change) comes as events.
          if (!text.startsWith("data:")) return JSON.parse(text) as unknown;
          const messages = text
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => JSON.parse(line.slice("data:".length)) as { readonly id?: unknown });
          return messages.find((message) => message.id !== undefined) ?? messages;
        }).pipe(Effect.orDie);
      const list = (token: string) =>
        rpc(token, { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }).pipe(
          Effect.map((reply) => ((reply as RpcReply).result?.tools ?? []).map((tool) => tool.name)),
        );
      const call = (token: string, name: string, args: object = {}) =>
        rpc(token, {
          jsonrpc: "2.0",
          id: 3,
          method: "tools/call",
          params: { name, arguments: args },
        }) as Effect.Effect<RpcReply>;
      return yield* body({ rpc, list, call }).pipe(Effect.provide(shared));
    }),
  ).pipe(Effect.provide(NodeHttpServer.layerTest));

const grouped = (names: ReadonlyArray<string>) => ({
  waits: WAIT_TOOLS.filter((name) => names.includes(name)),
  prs: names.includes(PR_TOOL),
});

describe("the T3 tool filter on /mcp", () => {
  it.effect("tools/list: a tool outside the thread's groups is absent, one inside unchanged", () =>
    withServer(({ list }) =>
      Effect.gen(function* () {
        const all = yield* list("all");
        expect(grouped(all)).toEqual({ waits: WAIT_TOOLS, prs: true });
        // Inside the groups: the same entries as with `all`.
        expect(grouped(yield* list("prs"))).toEqual({ waits: [], prs: true });
        const waits = yield* list("waits");
        expect(grouped(waits)).toEqual({ waits: WAIT_TOOLS, prs: false });
        const inWaits = (name: string) =>
          ["waits", "ungrouped"].includes(McpT3ToolRule.T3_TOOL_GROUPS.get(name) ?? "");
        expect(waits).toEqual(all.filter(inWaits));
        // `playtest` lives in the waits toolkit but has its own group: keeping `waits` hides it.
        expect(McpT3ToolRule.T3_TOOL_GROUPS.get("playtest")).toBe("playtest");
        expect(all).toContain("playtest");
        expect(waits).not.toContain("playtest");
        expect(grouped(yield* list("none"))).toEqual({ waits: [], prs: false });
        // An outside client of this machine has no profile: only upstream's own rules apply (the
        // wait tools that act as the calling thread are hidden from a client with none).
        expect(grouped(yield* list("outside"))).toEqual({ waits: ["wait_cancel"], prs: true });
      }),
    ),
  );

  it.effect("tools/call: refused by exact name, with a plain reason and the fix", () =>
    withServer(({ call }) =>
      Effect.gen(function* () {
        const refused = yield* call("prs", "wait_list");
        expect(refused.result).toBeUndefined();
        expect(refused.error?.code).toBe(-32001);
        expect(refused.error?.message).toBe(
          'T3 refused "wait_list": it is in the "waits" T3 tool group, which this thread\'s MCP profile "prs" leaves out. Ask the user to add "waits" to that profile\'s t3Tools in Settings, or to pick a profile that has it for this thread; it applies from the thread\'s next turn.',
        );
        expect(listed).toBe(0);
        // Inside the groups the call runs as before.
        const ran = yield* call("waits", "wait_list");
        expect(ran.error).toBeUndefined();
        expect(ran.result?.isError).not.toBe(true);
        expect(listed).toBe(1);
      }),
    ),
  );

  it.effect("tools/call: no other name, and no batch, reaches a tool outside the groups", () =>
    withServer(({ call, rpc }) =>
      Effect.gen(function* () {
        // The names a provider shows its model, and case changes: the server knows none of them.
        for (const alias of [
          "mcp__t3-code__wait_list",
          "t3-code__wait_list",
          "WAIT_LIST",
          " wait_list",
        ]) {
          const reply = yield* call("prs", alias);
          expect([alias, reply.result?.isError === true || reply.error !== undefined]).toEqual([
            alias,
            true,
          ]);
        }
        // A name the server does not have: upstream's lookup refuses it, before the group rule.
        expect((yield* call("prs", "no_such_tool")).error?.code).toBe(-32602);
        // A JSON-RPC batch: the transport (MCP 2025-06-18) refuses it whole, so no call in it runs.
        const batch = yield* rpc("prs", [
          {
            jsonrpc: "2.0",
            id: 10,
            method: "tools/call",
            params: { name: "wait_list", arguments: {} },
          },
          {
            jsonrpc: "2.0",
            id: 11,
            method: "tools/call",
            params: { name: "task_list", arguments: {} },
          },
        ]);
        expect(batch).toEqual({ status: 400 });
        expect(listed).toBe(0);
      }),
    ),
  );

  it.effect("tool_search: a tool outside the groups is not found, nor loaded", () =>
    withServer(({ call }) =>
      Effect.gen(function* () {
        const found = (reply: RpcReply) =>
          (
            (reply.result?.structuredContent as { tools?: ReadonlyArray<{ name: string }> })
              ?.tools ?? []
          ).map((tool) => tool.name);
        expect(found(yield* call("all", "tool_search", { query: "wait_list waits" }))).toContain(
          "wait_list",
        );
        const prs = found(yield* call("prs", "tool_search", { query: "wait_list waits" }));
        expect(prs.filter((name) => WAIT_TOOLS.includes(name))).toEqual([]);
        expect(found(yield* call("prs", "tool_search", { query: PR_TOOL }))).toContain(PR_TOOL);
        // A profile that cannot apply finds no grouped tool.
        expect(found(yield* call("gone", "tool_search", { query: "wait_list pull" }))).toEqual([]);
      }),
    ),
  );

  it.effect("a child is never wider than its parent: its own default is cut to the parent's", () =>
    withServer(({ list, call }) =>
      Effect.gen(function* () {
        // Its own role default (`waits`) and its parent's (`prs`) share no group: it gets none.
        expect(grouped(yield* list("child"))).toEqual({ waits: [], prs: false });
        expect((yield* call("child", "wait_list")).error?.code).toBe(-32001);
        expect((yield* call("child", PR_TOOL)).error?.code).toBe(-32001);
      }),
    ),
  );

  it.effect("fail closed: a profile that is gone, or a thread T3 cannot read, keeps no group", () =>
    withServer(({ list, call }) =>
      Effect.gen(function* () {
        for (const token of ["gone", "deleted"]) {
          expect(grouped(yield* list(token))).toEqual({ waits: [], prs: false });
          const refused = yield* call(token, "wait_list");
          expect(refused.error?.code).toBe(-32001);
          expect(refused.error?.message).toContain("this thread's MCP profile cannot be applied");
        }
        expect((yield* call("gone", "wait_list")).error?.message).toContain(
          'MCP profile "gone" is not set up',
        );
        expect((yield* call("deleted", "wait_list")).error?.message).toContain(
          "T3 cannot read this thread",
        );
        // The tools every profile keeps stay: tool_search answers (with nothing grouped).
        expect((yield* call("gone", "tool_search", { query: "x" })).error).toBeUndefined();
        expect(listed).toBe(0);
      }),
    ),
  );

  it.effect("old or empty state: no profile set anywhere, every thread sees every tool", () =>
    withServer(
      ({ list }) =>
        Effect.gen(function* () {
          for (const token of ["all", "prs", "child"]) {
            expect(grouped(yield* list(token))).toEqual({ waits: WAIT_TOOLS, prs: true });
          }
        }),
      WAITS_ON,
    ),
  );

  it.effect(
    "a change mid-thread applies from the next turn; a restart works the set out again",
    () =>
      withServer(({ list }) =>
        Effect.gen(function* () {
          expect(grouped(yield* list("prs"))).toEqual({ waits: [], prs: true });
          const settings = yield* ServerSettingsService.ServerSettingsService;
          yield* settings.updateSettings({
            mcpThreadProfiles: { "thread-prs": "waits" },
          } as never);
          // The running turn keeps the set it started with.
          expect(grouped(yield* list("prs"))).toEqual({ waits: [], prs: true });
          // The next turn starts (Claude's query opens): the new set from then on.
          const plan = yield* makeClaudeMcpProfiles;
          void (yield* plan(STORED[1]!, {})).options;
          expect(grouped(yield* list("prs"))).toEqual({ waits: WAIT_TOOLS, prs: false });
          // Runs again: a second turn start with the same settings gives the same set.
          void (yield* plan(STORED[1]!, {})).options;
          expect(grouped(yield* list("prs"))).toEqual({ waits: WAIT_TOOLS, prs: false });
          // A restart loses the memory: the set is worked out again from the settings as they are.
          yield* settings.updateSettings({ mcpThreadProfiles: { "thread-prs": "prs" } } as never);
          T3ToolSets.__testing.clear();
          expect(grouped(yield* list("prs"))).toEqual({ waits: [], prs: true });
        }),
      ),
  );
});

/** Moves test time on by `millis` (each sleep in turn wakes), then joins the fiber. */
const drive = <A>(fiber: Fiber.Fiber<A>, millis: number) =>
  TestClock.adjust(millis).pipe(Effect.andThen(Fiber.join(fiber)));

describe("a set worked out by a request, not a turn (fork issues #812, #918)", () => {
  it.effect("a refused set is not remembered: it is reused briefly, then worked out again", () =>
    withServer(({ list }) =>
      Effect.gen(function* () {
        expect(grouped(yield* list("gone"))).toEqual({ waits: [], prs: false });
        // Not the thread's set: the next turn, or the next request after a moment, works it out.
        expect(T3ToolSets.t3ToolSetFor("thread-gone")).toBeUndefined();
        const settings = yield* ServerSettingsService.ServerSettingsService;
        yield* settings.updateSettings({
          mcpProfiles: { ...SETTINGS.mcpProfiles, gone: { servers: "all", t3Tools: ["waits"] } },
        } as never);
        // Reused for a moment, so a list request does not read the settings once per tool.
        expect(grouped(yield* list("gone"))).toEqual({ waits: [], prs: false });
        yield* TestClock.adjust(McpT3ToolRule.REFUSED_SET_REUSE_MS + 1);
        expect(grouped(yield* list("gone"))).toEqual({ waits: WAIT_TOOLS, prs: false });
      }),
    ),
  );

  it.effect("while a turn's set is being fixed, a request waits for it", () =>
    withServer(({ list }) =>
      Effect.gen(function* () {
        const release = T3ToolSets.holdT3ToolSet(
          "thread-prs",
          yield* Clock.currentTimeMillis,
          T3ToolSets.nextT3ToolSetTurn(),
        );
        const listing = yield* Effect.forkChild(list("prs"));
        yield* Deferred.await(T3ToolSets.__testing.asked("thread-prs")!);
        expect(listing.pollUnsafe()).toBeUndefined();
        // The turn's check fixes the thread's new set, then releases: the request gets that one.
        T3ToolSets.fixT3ToolSet(
          "thread-prs",
          {
            _tag: "Groups",
            name: "waits",
            groups: new Set(["waits"]),
          },
          T3ToolSets.nextT3ToolSetTurn(),
        );
        release();
        release();
        expect(grouped(yield* Fiber.join(listing))).toEqual({ waits: WAIT_TOOLS, prs: false });
        expect(T3ToolSets.__testing.asked("thread-prs")).toBeUndefined();
      }),
    ),
  );

  it.effect("a hold never released: the request is refused, not given the set before", () =>
    withServer(({ call }) =>
      Effect.gen(function* () {
        T3ToolSets.fixT3ToolSet(
          "thread-all",
          { _tag: "Groups", name: "all", groups: "all" },
          T3ToolSets.nextT3ToolSetTurn(),
        );
        T3ToolSets.holdT3ToolSet(
          "thread-all",
          yield* Clock.currentTimeMillis,
          T3ToolSets.nextT3ToolSetTurn(),
        );
        const calling = yield* Effect.forkChild(call("all", "wait_list"));
        yield* Deferred.await(T3ToolSets.__testing.asked("thread-all")!);
        const refused = yield* drive(calling, McpT3ToolRule.T3_TOOL_SET_HOLD_WAIT_MS);
        expect(refused.error?.code).toBe(-32001);
        expect(refused.error?.message).toContain("still working out this thread's MCP profile");
        expect(listed).toBe(0);
      }),
    ),
  );
});

describe("the module upstream's tool_search imports (fork issue #812)", () => {
  it.effect("imports no toolkit, profile resolver or store", () =>
    Effect.promise(async () => {
      const { readFile } = await import("node:fs/promises");
      const text = await readFile(new URL("./mcpT3ToolFilter.ts", import.meta.url), "utf8");
      const specifiers = [...text.matchAll(/^import[^;]*?from "([^"]+)";/gms)].map(
        (match) => match[1],
      );
      expect(specifiers).toEqual([
        "effect/Context",
        "effect/Effect",
        "effect/Fiber",
        "../mcp/McpInvocationContext.ts",
      ]);
    }),
  );
});

describe("tool_search's filter", () => {
  const searchable = (name: string) =>
    Effect.sync(() => McpT3ToolFilter.searchableT3Tool({ name })).pipe(
      Effect.provideService(McpInvocationContext.McpInvocationContext, scopeFor("prs")),
    );

  it.effect("with the rule on the server, a thread with no set finds only the ungrouped", () =>
    withServer(() =>
      Effect.gen(function* () {
        T3ToolSets.__testing.clear();
        expect(yield* searchable("wait_list")).toBe(false);
        expect(yield* searchable("request_secret")).toBe(true);
      }),
    ),
  );

  it.effect("on a server without the rule (upstream's tests), it searches as upstream does", () =>
    Effect.gen(function* () {
      T3ToolSets.__testing.clear();
      expect(yield* searchable("wait_list")).toBe(true);
    }),
  );
});

describe("the T3 tool table", () => {
  it.effect("names every tool the server registers, each in one group", () =>
    Effect.sync(() => {
      const unknown = [...McpT3ToolRule.T3_TOOL_GROUPS].filter(
        ([, group]) => group === "not-allowed",
      );
      expect(unknown).toEqual([]);
      expect(McpT3ToolRule.T3_TOOL_GROUPS.get("wait_list")).toBe("waits");
      expect(McpT3ToolRule.T3_TOOL_GROUPS.get("request_secret")).toBe("ungrouped");
      expect(McpT3ToolRule.T3_TOOL_GROUPS.get("send_to_user")).toBe("ungrouped");
      expect(McpT3ToolRule.T3_TOOL_GROUPS.get("tool_search")).toBe("ungrouped");
    }),
  );
});

describe("the refusal of a tool in no group", () => {
  it.effect("names the real cause (a T3 bug), not a setting that cannot fix it", () =>
    Effect.sync(() => {
      const set: T3ToolSets.T3ToolSet = { _tag: "Groups", name: "prs", groups: new Set(["waits"]) };
      for (const name of ["orphan_tool", "send_to_user_in_a_new_toolkit"]) {
        const message = McpT3ToolRule.__testing.t3ToolRefusal(set, name);
        expect(message).toBe(
          `T3 refused "${name}": this tool has no T3 tool group, so MCP profile "prs" cannot keep it. This is a T3 bug, not a setting to change: report it.`,
        );
      }
      // A tool in a group still gets the group and its fix.
      expect(McpT3ToolRule.__testing.t3ToolRefusal(set, "list_thread_pull_requests")).toContain(
        'Ask the user to add "pull-requests" to that profile\'s t3Tools',
      );
    }),
  );
});

describe("the T3 tool sets in memory", () => {
  const groups = (name: string): T3ToolSets.T3ToolSet => ({
    _tag: "Groups",
    name,
    groups: new Set(["threads"]),
  });

  it.effect("a set worked out for a request never replaces one a turn fixed", () =>
    Effect.sync(() => {
      T3ToolSets.__testing.clear();
      T3ToolSets.fixT3ToolSet("t", groups("turn"), T3ToolSets.nextT3ToolSetTurn());
      expect(T3ToolSets.rememberT3ToolSet("t", groups("request"))).toEqual(groups("turn"));
      expect(T3ToolSets.rememberT3ToolSet("u", groups("request"))).toEqual(groups("request"));
      // A later turn start does replace it.
      T3ToolSets.fixT3ToolSet("u", groups("turn"), T3ToolSets.nextT3ToolSetTurn());
      expect(T3ToolSets.t3ToolSetFor("u")).toEqual(groups("turn"));
    }),
  );

  it.effect("keeps at most MAX_T3_TOOL_SETS threads, dropping the oldest", () =>
    Effect.sync(() => {
      T3ToolSets.__testing.clear();
      // thread-0's older turn is still checking (it read a wider profile) as a newer turn fixes.
      const older = T3ToolSets.nextT3ToolSetTurn();
      T3ToolSets.fixT3ToolSet("thread-0", groups("newer"), T3ToolSets.nextT3ToolSetTurn());
      for (let index = 1; index <= T3ToolSets.MAX_T3_TOOL_SETS; index += 1) {
        T3ToolSets.fixT3ToolSet(`thread-${index}`, groups("p"), T3ToolSets.nextT3ToolSetTurn());
      }
      expect(T3ToolSets.__testing.size()).toBe(T3ToolSets.MAX_T3_TOOL_SETS);
      // The dropped thread has no set: its next request works it out again, never `all`.
      expect(T3ToolSets.t3ToolSetFor("thread-0")).toBeUndefined();
      expect(T3ToolSets.t3ToolSetFor(`thread-${T3ToolSets.MAX_T3_TOOL_SETS}`)).toBeDefined();
      // The order outlives the drop: the older turn's late check writes nothing, nor replaces
      // the set a request works out from the settings; a later turn fixes as usual.
      T3ToolSets.fixT3ToolSet("thread-0", groups("older"), older);
      expect(T3ToolSets.t3ToolSetFor("thread-0")).toBeUndefined();
      expect(T3ToolSets.rememberT3ToolSet("thread-0", groups("request"))).toEqual(
        groups("request"),
      );
      T3ToolSets.fixT3ToolSet("thread-0", groups("older"), older);
      expect(T3ToolSets.t3ToolSetFor("thread-0")).toEqual(groups("request"));
      T3ToolSets.fixT3ToolSet("thread-0", groups("next"), T3ToolSets.nextT3ToolSetTurn());
      expect(T3ToolSets.t3ToolSetFor("thread-0")).toEqual(groups("next"));
    }),
  );

  it.effect("a check that finishes after a later turn's fixes nothing (fork issue #986)", () =>
    Effect.sync(() => {
      T3ToolSets.__testing.clear();
      const older = T3ToolSets.nextT3ToolSetTurn();
      const newer = T3ToolSets.nextT3ToolSetTurn();
      T3ToolSets.fixT3ToolSet("t", groups("newer"), newer);
      T3ToolSets.fixT3ToolSet("t", groups("older"), older);
      expect(T3ToolSets.t3ToolSetFor("t")).toEqual(groups("newer"));
      // The same turn fixing again (a refusal after its set, as Claude's plan does) still writes.
      T3ToolSets.fixT3ToolSet("t", groups("newer again"), newer);
      expect(T3ToolSets.t3ToolSetFor("t")).toEqual(groups("newer again"));
    }),
  );

  it.effect("stopped half-way: an older turn's stop leaves a newer set; its own clears it", () =>
    Effect.sync(() => {
      T3ToolSets.__testing.clear();
      const older = T3ToolSets.nextT3ToolSetTurn();
      const newer = T3ToolSets.nextT3ToolSetTurn();
      T3ToolSets.fixT3ToolSet("t", groups("newer"), newer);
      T3ToolSets.forgetT3ToolSet("t", older);
      expect(T3ToolSets.t3ToolSetFor("t")).toEqual(groups("newer"));
      const stopped = T3ToolSets.nextT3ToolSetTurn();
      T3ToolSets.forgetT3ToolSet("t", stopped);
      expect(T3ToolSets.t3ToolSetFor("t")).toBeUndefined();
      // No set stands, but the older turn's late check still may not write one.
      T3ToolSets.fixT3ToolSet("t", groups("older"), older);
      expect(T3ToolSets.t3ToolSetFor("t")).toBeUndefined();
      // A request may remember the one it works out; a later turn replaces it.
      expect(T3ToolSets.rememberT3ToolSet("t", groups("request"))).toEqual(groups("request"));
      T3ToolSets.fixT3ToolSet("t", groups("older"), older);
      expect(T3ToolSets.t3ToolSetFor("t")).toEqual(groups("request"));
      T3ToolSets.fixT3ToolSet("t", groups("next"), T3ToolSets.nextT3ToolSetTurn());
      expect(T3ToolSets.t3ToolSetFor("t")).toEqual(groups("next"));
    }),
  );

  it.effect("empty state (after a restart): any check writes", () =>
    Effect.sync(() => {
      T3ToolSets.__testing.clear();
      const older = T3ToolSets.nextT3ToolSetTurn();
      T3ToolSets.fixT3ToolSet("t", groups("older"), older);
      expect(T3ToolSets.t3ToolSetFor("t")).toEqual(groups("older"));
    }),
  );
});

describe("the hold bound, without a socket (fork issue #918)", () => {
  /** The rule's check of `targets`, one after another as upstream's list filter runs it. */
  const checkAll = (
    token: string,
    method: "tools/list" | "tools/call",
    targets: ReadonlyArray<string>,
  ) =>
    Effect.gen(function* () {
      const visible = yield* McpT3ToolRule.__testing.makeVisible;
      return yield* Effect.forEach(targets, (target) =>
        visible({ method, target } as never).pipe(
          Effect.orElseSucceed(() => false),
          Effect.map((shown) => [target, shown] as const),
        ),
      );
    }).pipe(Effect.provideService(McpInvocationContext.McpInvocationContext, scopeFor(token)));

  const prs: T3ToolSets.T3ToolSet = {
    _tag: "Groups",
    name: "prs",
    groups: new Set(["pull-requests"]),
  };

  it.effect("tools/list waits once for the hold, not once per tool", () =>
    Effect.gen(function* () {
      T3ToolSets.__testing.clear();
      McpT3ToolRule.__testing.clearRefused();
      T3ToolSets.fixT3ToolSet("thread-prs", prs, T3ToolSets.nextT3ToolSetTurn());
      expect(yield* checkAll("prs", "tools/list", [PR_TOOL])).toEqual([[PR_TOOL, true]]);
      T3ToolSets.holdT3ToolSet(
        "thread-prs",
        yield* Clock.currentTimeMillis,
        T3ToolSets.nextT3ToolSetTurn(),
      );
      const listing = yield* Effect.forkChild(
        checkAll("prs", "tools/list", [...WAIT_TOOLS, PR_TOOL]),
      );
      yield* Deferred.await(T3ToolSets.__testing.asked("thread-prs")!);
      yield* TestClock.adjust(McpT3ToolRule.T3_TOOL_SET_HOLD_WAIT_MS);
      for (let turn = 0; turn < 10; turn += 1) yield* Effect.yieldNow;
      // One wait for the whole list, then it is refused: never the set from before the hold.
      const done = listing.pollUnsafe();
      expect(done).toBeDefined();
      expect(yield* Fiber.join(listing)).toEqual(
        [...WAIT_TOOLS, PR_TOOL].map((name) => [name, false]),
      );
    }),
  );

  it.effect(
    "a hold past its bound is dropped: later requests do not wait, later turns start fresh",
    () =>
      Effect.gen(function* () {
        T3ToolSets.__testing.clear();
        McpT3ToolRule.__testing.clearRefused();
        T3ToolSets.fixT3ToolSet("thread-prs", prs, T3ToolSets.nextT3ToolSetTurn());
        T3ToolSets.holdT3ToolSet(
          "thread-prs",
          yield* Clock.currentTimeMillis,
          T3ToolSets.nextT3ToolSetTurn(),
        );
        const first = yield* Effect.forkChild(checkAll("prs", "tools/call", [PR_TOOL]));
        yield* Deferred.await(T3ToolSets.__testing.asked("thread-prs")!);
        expect(yield* drive(first, McpT3ToolRule.T3_TOOL_SET_HOLD_WAIT_MS)).toEqual([
          [PR_TOOL, false],
        ]);
        // Dropped: the next request does not wait 10 s again, and it keeps only the ungrouped tools
        // (never the set from before the hold) until a turn fixes the thread's set.
        expect(T3ToolSets.__testing.asked("thread-prs")).toBeUndefined();
        expect(yield* checkAll("prs", "tools/call", [PR_TOOL])).toEqual([[PR_TOOL, false]]);
        // The next turn's hold starts fresh, and its check's set applies once it releases.
        const release = T3ToolSets.holdT3ToolSet(
          "thread-prs",
          yield* Clock.currentTimeMillis,
          T3ToolSets.nextT3ToolSetTurn(),
        );
        const second = yield* Effect.forkChild(checkAll("prs", "tools/call", [PR_TOOL]));
        yield* Deferred.await(T3ToolSets.__testing.asked("thread-prs")!);
        T3ToolSets.fixT3ToolSet("thread-prs", prs, T3ToolSets.nextT3ToolSetTurn());
        release();
        expect(yield* Fiber.join(second)).toEqual([[PR_TOOL, true]]);
      }),
  );

  it.effect(
    "a check fixed its set but has not released yet as the bound passes: its set stands (#986)",
    () =>
      Effect.gen(function* () {
        T3ToolSets.__testing.clear();
        McpT3ToolRule.__testing.clearRefused();
        T3ToolSets.fixT3ToolSet(
          "thread-prs",
          { _tag: "Groups", name: "all", groups: "all" },
          T3ToolSets.nextT3ToolSetTurn(),
        );
        T3ToolSets.holdT3ToolSet(
          "thread-prs",
          yield* Clock.currentTimeMillis,
          T3ToolSets.nextT3ToolSetTurn(),
        );
        const waiting = yield* Effect.forkChild(checkAll("prs", "tools/call", [PR_TOOL]));
        yield* Deferred.await(T3ToolSets.__testing.asked("thread-prs")!);
        // The turn's check fixes its set; the wait ends before it releases.
        T3ToolSets.fixT3ToolSet("thread-prs", prs, T3ToolSets.nextT3ToolSetTurn());
        expect(yield* drive(waiting, McpT3ToolRule.T3_TOOL_SET_HOLD_WAIT_MS)).toEqual([
          [PR_TOOL, true],
        ]);
        expect(T3ToolSets.t3ToolSetFor("thread-prs")).toEqual(prs);
        expect(T3ToolSets.__testing.asked("thread-prs")).toBeUndefined();
      }),
  );

  it.effect("the hold's own turn fixes its set as the bound passes: the drop keeps it (#986)", () =>
    Effect.gen(function* () {
      T3ToolSets.__testing.clear();
      McpT3ToolRule.__testing.clearRefused();
      const turn = T3ToolSets.nextT3ToolSetTurn();
      const release = T3ToolSets.holdT3ToolSet("thread-prs", yield* Clock.currentTimeMillis, turn);
      const waiting = yield* Effect.forkChild(checkAll("prs", "tools/call", [PR_TOOL]));
      yield* Deferred.await(T3ToolSets.__testing.asked("thread-prs")!);
      T3ToolSets.fixT3ToolSet("thread-prs", prs, turn);
      expect(yield* drive(waiting, McpT3ToolRule.T3_TOOL_SET_HOLD_WAIT_MS)).toEqual([
        [PR_TOOL, true],
      ]);
      // The release that comes after the drop does nothing.
      release();
      expect(T3ToolSets.t3ToolSetFor("thread-prs")).toEqual(prs);
    }),
  );

  it.effect(
    "a joined turn still checking as the bound passes: the first's late check cannot widen (#986)",
    () =>
      Effect.gen(function* () {
        T3ToolSets.__testing.clear();
        McpT3ToolRule.__testing.clearRefused();
        // Turn A's check read the wide profile and is stuck; the profile is narrowed; turn B joins
        // A's hold, and B's check is still running too as the bound passes.
        const a = T3ToolSets.nextT3ToolSetTurn();
        const b = T3ToolSets.nextT3ToolSetTurn();
        const now = yield* Clock.currentTimeMillis;
        T3ToolSets.holdT3ToolSet("thread-prs", now, a);
        T3ToolSets.holdT3ToolSet("thread-prs", now, b);
        const waiting = yield* Effect.forkChild(checkAll("prs", "tools/call", [PR_TOOL]));
        yield* Deferred.await(T3ToolSets.__testing.asked("thread-prs")!);
        expect(yield* drive(waiting, McpT3ToolRule.T3_TOOL_SET_HOLD_WAIT_MS)).toEqual([
          [PR_TOOL, false],
        ]);
        expect(T3ToolSets.t3ToolSetFor("thread-prs")?._tag).toBe("Refused");
        // The refusal stands for B: A's late check, with its wide read, writes nothing.
        T3ToolSets.fixT3ToolSet("thread-prs", { _tag: "Groups", name: "all", groups: "all" }, a);
        expect(T3ToolSets.t3ToolSetFor("thread-prs")?._tag).toBe("Refused");
        // B's own late check replaces it.
        T3ToolSets.fixT3ToolSet("thread-prs", prs, b);
        expect(T3ToolSets.t3ToolSetFor("thread-prs")).toEqual(prs);
      }),
  );
});
