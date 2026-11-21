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
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
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
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as PullRequestReviewGuideService from "../pullRequest/PullRequestReviewGuideService.ts";
import * as PullRequestService from "../pullRequest/PullRequestService.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import { Waits } from "../waits/WaitWorker.ts";
import { makeClaudeMcpProfiles } from "./claudeMcpProfile.ts";
import * as McpT3ToolFilter from "./mcpT3ToolFilter.ts";
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
      McpT3ToolFilter.layer,
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
        const outsideWaits = (name: string) =>
          McpT3ToolFilter.T3_TOOL_GROUPS.get(name) !== "pull-requests";
        expect(waits).toEqual(all.filter(outsideWaits));
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
          yield* plan(STORED[1]!, {});
          expect(grouped(yield* list("prs"))).toEqual({ waits: WAIT_TOOLS, prs: false });
          // Runs again: a second turn start with the same settings gives the same set.
          yield* plan(STORED[1]!, {});
          expect(grouped(yield* list("prs"))).toEqual({ waits: WAIT_TOOLS, prs: false });
          // A restart loses the memory: the set is worked out again from the settings as they are.
          yield* settings.updateSettings({ mcpThreadProfiles: { "thread-prs": "prs" } } as never);
          T3ToolSets.__testing.clear();
          expect(grouped(yield* list("prs"))).toEqual({ waits: [], prs: true });
        }),
      ),
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
      const unknown = [...McpT3ToolFilter.T3_TOOL_GROUPS].filter(
        ([, group]) => group === "not-allowed",
      );
      expect(unknown).toEqual([]);
      expect(McpT3ToolFilter.T3_TOOL_GROUPS.get("wait_list")).toBe("waits");
      expect(McpT3ToolFilter.T3_TOOL_GROUPS.get("request_secret")).toBe("ungrouped");
      expect(McpT3ToolFilter.T3_TOOL_GROUPS.get("send_to_user")).toBe("ungrouped");
      expect(McpT3ToolFilter.T3_TOOL_GROUPS.get("tool_search")).toBe("ungrouped");
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
      T3ToolSets.fixT3ToolSet("t", groups("turn"));
      expect(T3ToolSets.rememberT3ToolSet("t", groups("request"))).toEqual(groups("turn"));
      expect(T3ToolSets.rememberT3ToolSet("u", groups("request"))).toEqual(groups("request"));
      // A later turn start does replace it.
      T3ToolSets.fixT3ToolSet("u", groups("turn"));
      expect(T3ToolSets.t3ToolSetFor("u")).toEqual(groups("turn"));
    }),
  );

  it.effect("keeps at most MAX_T3_TOOL_SETS threads, dropping the oldest", () =>
    Effect.sync(() => {
      T3ToolSets.__testing.clear();
      for (let index = 0; index <= T3ToolSets.MAX_T3_TOOL_SETS; index += 1) {
        T3ToolSets.fixT3ToolSet(`thread-${index}`, groups("p"));
      }
      expect(T3ToolSets.__testing.size()).toBe(T3ToolSets.MAX_T3_TOOL_SETS);
      // The dropped thread has no set: its next request works it out again, never `all`.
      expect(T3ToolSets.t3ToolSetFor("thread-0")).toBeUndefined();
      expect(T3ToolSets.t3ToolSetFor(`thread-${T3ToolSets.MAX_T3_TOOL_SETS}`)).toBeDefined();
    }),
  );
});
