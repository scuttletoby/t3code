/** Fork (issue 1156, phase 3): the lane tools as the server registers them, over a real lane store. */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, expect, it } from "@effect/vitest";
import {
  EnvironmentId,
  type OrchestrationV2ThreadShell,
  ProjectId,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import { McpSchema, McpServer } from "effect/ai";

import * as McpHttpServer from "../../mcp/McpHttpServer.ts";
import * as McpInvocationContext from "../../mcp/McpInvocationContext.ts";
import * as McpToolAccessTestkit from "../../mcp/McpToolAccess.testkit.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../../peer/PeerForwarding.ts";
import * as RemoteDelegation from "../../peer/RemoteDelegation.ts";
import { Playtest } from "../../playtest/PlaytestLayer.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import { Waits } from "../../waits/WaitWorker.ts";
import * as LaneStore from "./LaneStore.ts";
import { BOARD_NOTES_PER_LANE } from "./laneTools.ts";

const PROJECT = ProjectId.make("project:mcp-test");
const OTHER_PROJECT = ProjectId.make("project:elsewhere");
const MAIN = ThreadId.make("thread-main");
const PR_LANE = ThreadId.make("thread-pr-lane");
const CI_LANE = ThreadId.make("thread-ci-lane");
const ELSEWHERE = ThreadId.make("thread-elsewhere");
const PR = "pr:owner/repo#12";
const ISSUE = "issue:owner/repo#3";

const CI = { description: "Keeps CI green.", owns: ["pr" as const], driver: "agent" as const };
const ON = { lanes: { enabled: true, definitions: { ci: CI } } };

const shells = new Map<ThreadId, OrchestrationV2ThreadShell>([
  [ELSEWHERE, { ...McpToolAccessTestkit.liveThreadShell(ELSEWHERE), projectId: OTHER_PROJECT }],
]);

const scopeOf = (threadId: ThreadId): McpInvocationContext.McpInvocationScope => ({
  environmentId: EnvironmentId.make("lanes-environment"),
  requestNamespace: `lanes-${threadId}`,
  thread: {
    threadId,
    providerSessionId: `session-${threadId}`,
    providerInstanceId: ProviderInstanceId.make("codex"),
  },
  client: undefined,
  issuedAt: 0,
  capabilities: new Set(["orchestration"]),
});

const client = McpSchema.McpServerClient.of({
  clientId: 1,
  protocolVersion: "2025-06-18",
  clientCapabilities: {},
  clientInfo: { name: "lanes", version: "1" },
  initializePayload: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "lanes", version: "1" },
  },
  getClient: Effect.die("unused"),
});

/** The fork's tools over a lane store in `directory` (one server start). */
const toolsLayer = (directory: string, settings: object) => {
  const base = Layer.mergeAll(
    ServerSettingsService.layerTest(settings as never),
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadShell: (threadId) =>
        Effect.succeed(shells.get(threadId) ?? McpToolAccessTestkit.liveThreadShell(threadId)),
    }),
    // The other fork tools' services; these tests never call them.
    Layer.mock(Waits)({}),
    Layer.mock(RemoteDelegation.RemoteDelegation)({}),
    Layer.mock(PeerForwarding.PeerForwarding)({}),
    Layer.mock(Playtest)({ filePath: "unused" }),
  );
  const store = Layer.effect(LaneStore.LaneStore, LaneStore.make({ directory }));
  return McpHttpServer.layerForkTools.pipe(
    Layer.provideMerge(McpHttpServer.layerInProcessMcpServer),
    Layer.provideMerge(store),
    Layer.provideMerge(base),
  );
};

const call = (caller: ThreadId, name: string, args: Record<string, unknown>) =>
  Effect.gen(function* () {
    const server = yield* McpServer.McpServer;
    return yield* server
      .callTool({ name, arguments: args })
      .pipe(
        Effect.provideService(McpInvocationContext.McpInvocationContext, scopeOf(caller)),
        Effect.provideService(McpSchema.McpServerClient, client),
      );
  });

const succeeded = (result: McpSchema.CallToolResult) => {
  assert.notStrictEqual(result.isError, true, JSON.stringify(result.content));
  return result.structuredContent as Record<string, any>;
};

const failed = (result: McpSchema.CallToolResult) => {
  const text = result.content[0];
  assert.isTrue(result.isError === true && text?.type === "text", "expected a declared failure");
  return JSON.parse(text?.type === "text" ? text.text : "{}") as {
    readonly code: string;
    readonly message: string;
    readonly holder?: Record<string, unknown>;
  };
};

const workspace = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const directory = path.join(
    yield* fs.makeTempDirectoryScoped({ prefix: "lane-tools-" }),
    "lanes",
  );
  return { fs, directory, file: path.join(directory, `${encodeURIComponent(PROJECT)}.json`) };
});

it.layer(NodeServices.layer)("lane tools", (it) => {
  it.effect("lane_start switches a defined lane on with a thread of the project", () =>
    Effect.gen(function* () {
      const { directory } = yield* workspace;
      yield* Effect.gen(function* () {
        const started = succeeded(
          yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: PR_LANE }),
        );
        expect(started).toMatchObject({ lane: "pr-queue", generation: 1, threadId: PR_LANE });
        // A repeat (a retried call) changes nothing.
        expect(
          succeeded(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: PR_LANE })),
        ).toMatchObject({ generation: 1, startedAt: started.startedAt });
        expect(
          failed(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: CI_LANE })),
        ).toMatchObject({ code: "already-running" });
        // One lane per thread.
        expect(
          failed(yield* call(MAIN, "lane_start", { lane: "ci", threadId: PR_LANE })),
        ).toMatchObject({ code: "already-running", message: expect.stringContaining("pr-queue") });
        expect(
          failed(yield* call(MAIN, "lane_start", { lane: "nightly", threadId: CI_LANE })),
        ).toMatchObject({ code: "unknown-lane" });
        // A lane's thread is in the lane's project.
        expect(
          failed(yield* call(MAIN, "lane_start", { lane: "issues", threadId: ELSEWHERE })),
        ).toMatchObject({ code: "thread-elsewhere" });
      }).pipe(Effect.provide(toolsLayer(directory, ON)));
    }),
  );

  it.effect("lane_claim: one lane per item, only from the lane's own thread", () =>
    Effect.gen(function* () {
      const { directory } = yield* workspace;
      yield* Effect.gen(function* () {
        succeeded(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: PR_LANE }));
        succeeded(yield* call(MAIN, "lane_start", { lane: "ci", threadId: CI_LANE }));
        const claimed = succeeded(
          yield* call(PR_LANE, "lane_claim", { lane: "pr-queue", generation: 1, item: PR }),
        );
        expect(claimed.claim).toMatchObject({ item: PR, lane: "pr-queue", threadId: PR_LANE });
        expect(claimed.takenFrom).toBeNull();
        // The same claim again is the same claim.
        expect(
          succeeded(
            yield* call(PR_LANE, "lane_claim", { lane: "pr-queue", generation: 1, item: PR }),
          ).claim,
        ).toEqual(claimed.claim);
        // Another lane is refused and told who holds it.
        const refused = failed(
          yield* call(CI_LANE, "lane_claim", { lane: "ci", generation: 1, item: PR }),
        );
        expect(refused).toMatchObject({ code: "claimed", holder: { lane: "pr-queue" } });
        // The caller is its own thread, never one named in input: the main thread cannot act as a lane.
        expect(
          failed(yield* call(MAIN, "lane_claim", { lane: "pr-queue", generation: 1, item: PR })),
        ).toMatchObject({ code: "stale-owner" });
        expect(
          failed(
            yield* call(PR_LANE, "lane_claim", { lane: "pr-queue", generation: 1, item: ISSUE }),
          ),
        ).toMatchObject({ code: "kind-not-owned" });
      }).pipe(Effect.provide(toolsLayer(directory, ON)));
    }),
  );

  it.effect("lane_release frees an item for another lane; a non-holder is refused", () =>
    Effect.gen(function* () {
      const { directory } = yield* workspace;
      yield* Effect.gen(function* () {
        succeeded(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: PR_LANE }));
        succeeded(yield* call(MAIN, "lane_start", { lane: "ci", threadId: CI_LANE }));
        succeeded(
          yield* call(PR_LANE, "lane_claim", { lane: "pr-queue", generation: 1, item: PR }),
        );
        expect(
          failed(yield* call(CI_LANE, "lane_release", { lane: "ci", generation: 1, item: PR })),
        ).toMatchObject({ code: "not-holder", holder: { lane: "pr-queue" } });
        expect(
          succeeded(
            yield* call(PR_LANE, "lane_release", { lane: "pr-queue", generation: 1, item: PR }),
          ).released,
        ).toMatchObject({ item: PR, lane: "pr-queue" });
        // Released twice: nothing to release.
        expect(
          succeeded(
            yield* call(PR_LANE, "lane_release", { lane: "pr-queue", generation: 1, item: PR }),
          ).released,
        ).toBeNull();
        expect(
          succeeded(yield* call(CI_LANE, "lane_claim", { lane: "ci", generation: 1, item: PR }))
            .claim,
        ).toMatchObject({ lane: "ci" });
      }).pipe(Effect.provide(toolsLayer(directory, ON)));
    }),
  );

  it.effect("lane_stop releases the lane's claims; the old thread can no longer claim", () =>
    Effect.gen(function* () {
      const { directory } = yield* workspace;
      yield* Effect.gen(function* () {
        succeeded(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: PR_LANE }));
        succeeded(
          yield* call(PR_LANE, "lane_claim", { lane: "pr-queue", generation: 1, item: PR }),
        );
        expect(succeeded(yield* call(MAIN, "lane_stop", { lane: "pr-queue" }))).toEqual({
          lane: "pr-queue",
          released: [PR],
        });
        expect(succeeded(yield* call(MAIN, "lane_stop", { lane: "pr-queue" }))).toEqual({
          lane: "pr-queue",
          released: [],
        });
        expect(
          failed(yield* call(PR_LANE, "lane_claim", { lane: "pr-queue", generation: 1, item: PR })),
        ).toMatchObject({ code: "not-running" });
        // Started again: the next generation, and the old one is refused.
        expect(
          succeeded(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: PR_LANE })),
        ).toMatchObject({ generation: 2 });
        expect(
          failed(yield* call(PR_LANE, "lane_claim", { lane: "pr-queue", generation: 1, item: PR })),
        ).toMatchObject({ code: "stale-owner" });
      }).pipe(Effect.provide(toolsLayer(directory, ON)));
    }),
  );

  it.effect("lane_board: every defined lane, claims, escalations and recent notes", () =>
    Effect.gen(function* () {
      const { directory } = yield* workspace;
      yield* Effect.gen(function* () {
        succeeded(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: PR_LANE }));
        succeeded(yield* call(MAIN, "lane_start", { lane: "ci", threadId: CI_LANE }));
        succeeded(
          yield* call(PR_LANE, "lane_claim", { lane: "pr-queue", generation: 1, item: PR }),
        );
        succeeded(yield* call(MAIN, "lane_stop", { lane: "ci" }));
        // Notes and escalations have no tool yet (phase 5): written through the same store.
        const store = yield* LaneStore.LaneStore;
        const actor = { lane: "pr-queue", generation: 1, threadId: PR_LANE };
        for (const n of [1, 2, 3, 4, 5]) yield* store.note(PROJECT, actor, `note ${n}`, []);
        yield* store.escalate(PROJECT, actor, "esc-1", "Merge #12?", [PR]);

        const board = succeeded(yield* call(MAIN, "lane_board", {}));
        expect(board.lanes).toEqual([
          {
            lane: "ci",
            owns: ["pr"],
            status: "stopped",
            generation: 1,
            threadId: CI_LANE,
            startedAt: expect.any(String),
            stoppedAt: expect.any(String),
          },
          {
            lane: "issues",
            owns: ["issue"],
            status: "off",
            generation: null,
            threadId: null,
            startedAt: null,
            stoppedAt: null,
          },
          {
            lane: "pr-queue",
            owns: ["pr"],
            status: "running",
            generation: 1,
            threadId: PR_LANE,
            startedAt: expect.any(String),
            stoppedAt: null,
          },
        ]);
        expect(board.claims).toEqual([
          expect.objectContaining({ item: PR, lane: "pr-queue", live: true }),
        ]);
        expect(board.escalations).toEqual([expect.objectContaining({ id: "esc-1" })]);
        expect(board.notes.map((note: { text: string }) => note.text)).toEqual(
          ["note 3", "note 4", "note 5"].slice(-BOARD_NOTES_PER_LANE),
        );
      }).pipe(Effect.provide(toolsLayer(directory, ON)));
    }),
  );

  it.effect("a lane removed from settings shows as undefined; its claim is stale", () =>
    Effect.gen(function* () {
      const { directory } = yield* workspace;
      yield* Effect.gen(function* () {
        succeeded(yield* call(MAIN, "lane_start", { lane: "ci", threadId: CI_LANE }));
        succeeded(yield* call(CI_LANE, "lane_claim", { lane: "ci", generation: 1, item: PR }));
      }).pipe(Effect.provide(toolsLayer(directory, ON)));
      yield* Effect.gen(function* () {
        const board = succeeded(yield* call(MAIN, "lane_board", {}));
        expect(board.lanes.find((lane: { lane: string }) => lane.lane === "ci")).toMatchObject({
          status: "undefined",
          owns: [],
        });
        expect(board.claims).toEqual([expect.objectContaining({ item: PR, live: false })]);
      }).pipe(
        Effect.provide(
          toolsLayer(directory, {
            lanes: { enabled: true },
          }),
        ),
      );
    }),
  );

  it.effect("lanes off: every lane tool is refused and no lane file is written", () =>
    Effect.gen(function* () {
      const { fs, directory, file } = yield* workspace;
      yield* Effect.gen(function* () {
        for (const [name, args] of [
          ["lane_start", { lane: "pr-queue", threadId: PR_LANE }],
          ["lane_stop", { lane: "pr-queue" }],
          ["lane_board", {}],
          ["lane_claim", { lane: "pr-queue", generation: 1, item: PR }],
          ["lane_release", { lane: "pr-queue", generation: 1, item: PR }],
        ] as const) {
          expect(failed(yield* call(PR_LANE, name, args)), name).toMatchObject({
            code: "lanes-off",
          });
        }
      }).pipe(Effect.provide(toolsLayer(directory, {})));
      assert.isFalse(yield* fs.exists(file));
    }),
  );
});
