/** Fork (issue 1156, phase 3): the lane tools as the server registers them, over a real lane store. */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, expect, it } from "@effect/vitest";
import {
  EnvironmentId,
  type OrchestrationV2ThreadShell,
  ProjectId,
  ProviderInstanceId,
  ServerSettingsError,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
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
const ISSUES_THREAD = ThreadId.make("thread-issues");
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

/**
 * Settings that read once more after `allow(n)`, then fail: a read the store did not make sees the
 * failure.
 */
const flakySettings = (settings: object) => {
  let left = Number.POSITIVE_INFINITY;
  const layer = Layer.effect(
    ServerSettingsService,
    Effect.gen(function* () {
      const real = yield* ServerSettingsService;
      return ServerSettingsService.of({
        ...real,
        getSettings: Effect.suspend(() =>
          left-- > 0
            ? real.getSettings
            : Effect.fail(
                new ServerSettingsError({ settingsPath: "test", operation: "read-file" }),
              ),
        ),
      });
    }),
  ).pipe(Layer.provide(ServerSettingsService.layerTest(settings as never)));
  return { layer, allow: (reads: number) => (left = reads) };
};

/** The fork's tools over a lane store in `directory` (one server start), with these settings. */
const toolsLayerWith = <E>(directory: string, settings: Layer.Layer<ServerSettingsService, E>) => {
  const base = Layer.mergeAll(
    settings,
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

const toolsLayer = (directory: string, settings: object) =>
  toolsLayerWith(directory, ServerSettingsService.layerTest(settings as never));

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

  it.effect(
    "an item is one item whatever its owner/repo case; a lane named like a built-in key is unknown",
    () =>
      Effect.gen(function* () {
        const { directory } = yield* workspace;
        yield* Effect.gen(function* () {
          succeeded(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: PR_LANE }));
          succeeded(yield* call(MAIN, "lane_start", { lane: "ci", threadId: CI_LANE }));
          const claimed = succeeded(
            yield* call(PR_LANE, "lane_claim", {
              lane: "pr-queue",
              generation: 1,
              item: " pr:Owner/Repo#12 ",
            }),
          );
          expect(claimed.claim).toMatchObject({ item: PR });
          // Another spelling of the same item is the same item.
          expect(
            failed(
              yield* call(CI_LANE, "lane_claim", {
                lane: "ci",
                generation: 1,
                item: "pr:OWNER/repo#12",
              }),
            ),
          ).toMatchObject({ code: "claimed", holder: { item: PR } });
          expect(
            succeeded(
              yield* call(PR_LANE, "lane_release", {
                lane: "pr-queue",
                generation: 1,
                item: "pr:owner/REPO#12",
              }),
            ).released,
          ).toMatchObject({ item: PR });
          expect(
            failed(
              yield* call(PR_LANE, "lane_release", {
                lane: "pr-queue",
                generation: 1,
                item: "#12",
              }),
            ),
          ).toMatchObject({ code: "bad-item" });
          // Inherited keys of a plain object are not lanes.
          for (const lane of ["constructor", "toString", "__proto__"]) {
            expect(
              failed(yield* call(MAIN, "lane_start", { lane, threadId: ISSUES_THREAD })),
              lane,
            ).toMatchObject({ code: "unknown-lane" });
          }
          const board = succeeded(yield* call(MAIN, "lane_board", {}));
          expect(board.lanes.map((lane: { lane: string }) => lane.lane)).toEqual([
            "ci",
            "issues",
            "pr-queue",
          ]);
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
            startedBy: MAIN,
            startedAt: expect.any(String),
            stoppedAt: expect.any(String),
          },
          {
            lane: "issues",
            owns: ["issue"],
            status: "off",
            generation: null,
            threadId: null,
            startedBy: null,
            startedAt: null,
            stoppedAt: null,
          },
          {
            lane: "pr-queue",
            owns: ["pr"],
            status: "running",
            generation: 1,
            threadId: PR_LANE,
            startedBy: MAIN,
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

  it.effect("lane_start refuses the calling thread as the lane's own thread", () =>
    Effect.gen(function* () {
      const { fs, directory, file } = yield* workspace;
      yield* Effect.gen(function* () {
        // The caller (an orchestrator) cannot make itself the lane and then claim as it.
        expect(
          failed(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: MAIN })),
        ).toMatchObject({ code: "self-lane" });
        expect(
          failed(yield* call(MAIN, "lane_claim", { lane: "pr-queue", generation: 1, item: PR })),
        ).toMatchObject({ code: "not-running" });
      }).pipe(Effect.provide(toolsLayer(directory, ON)));
      assert.isFalse(yield* fs.exists(file));
    }),
  );

  it.effect("lane_stop: only the thread that started the lane stops it", () =>
    Effect.gen(function* () {
      const { directory } = yield* workspace;
      yield* Effect.gen(function* () {
        succeeded(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: PR_LANE }));
        succeeded(
          yield* call(PR_LANE, "lane_claim", { lane: "pr-queue", generation: 1, item: PR }),
        );
        // Not the lane's own thread, nor another thread of the project (another lane's).
        for (const thread of [PR_LANE, CI_LANE, ISSUES_THREAD]) {
          expect(
            failed(yield* call(thread, "lane_stop", { lane: "pr-queue" })),
            thread,
          ).toMatchObject({ code: "not-starter", message: expect.stringContaining(MAIN) });
        }
        expect(succeeded(yield* call(MAIN, "lane_board", {})).claims).toEqual([
          expect.objectContaining({ item: PR, live: true }),
        ]);
        expect(succeeded(yield* call(MAIN, "lane_stop", { lane: "pr-queue" }))).toEqual({
          lane: "pr-queue",
          released: [PR],
        });
        // Started again by another thread: that one stops it now, the first no longer.
        succeeded(yield* call(CI_LANE, "lane_start", { lane: "pr-queue", threadId: PR_LANE }));
        expect(failed(yield* call(MAIN, "lane_stop", { lane: "pr-queue" }))).toMatchObject({
          code: "not-starter",
        });
        succeeded(yield* call(CI_LANE, "lane_stop", { lane: "pr-queue" }));
      }).pipe(Effect.provide(toolsLayer(directory, ON)));
    }),
  );

  it.effect(
    "lane_stop: once the starter is no longer a live thread of the project, any thread stops",
    () =>
      Effect.gen(function* () {
        const { directory } = yield* workspace;
        const OLD_ORCHESTRATOR = ThreadId.make("thread-old-orchestrator");
        const NEW_ORCHESTRATOR = ThreadId.make("thread-new-orchestrator");
        const MOVED = ThreadId.make("thread-moved-orchestrator");
        const live = McpToolAccessTestkit.liveThreadShell;
        yield* Effect.gen(function* () {
          succeeded(
            yield* call(OLD_ORCHESTRATOR, "lane_start", { lane: "pr-queue", threadId: PR_LANE }),
          );
          succeeded(
            yield* call(PR_LANE, "lane_claim", { lane: "pr-queue", generation: 1, item: PR }),
          );
          succeeded(yield* call(MOVED, "lane_start", { lane: "ci", threadId: CI_LANE }));
          // While the starter lives, another live thread is still refused.
          expect(
            failed(yield* call(NEW_ORCHESTRATOR, "lane_stop", { lane: "pr-queue" })),
          ).toMatchObject({ code: "not-starter" });
          // The starter is deleted; the other moved to another project.
          shells.set(OLD_ORCHESTRATOR, {
            ...live(OLD_ORCHESTRATOR),
            deletedAt: DateTime.makeUnsafe("2026-01-01T00:00:00.000Z"),
          });
          shells.set(MOVED, { ...live(MOVED), projectId: OTHER_PROJECT });
          expect(
            failed(
              yield* call(NEW_ORCHESTRATOR, "lane_start", {
                lane: "pr-queue",
                threadId: ISSUES_THREAD,
              }),
            ),
          ).toMatchObject({ code: "already-running" });
          expect(
            succeeded(yield* call(NEW_ORCHESTRATOR, "lane_stop", { lane: "pr-queue" })),
          ).toEqual({
            lane: "pr-queue",
            released: [PR],
          });
          succeeded(yield* call(NEW_ORCHESTRATOR, "lane_stop", { lane: "ci" }));
          // Restarted on the same thread by the new orchestrator: it is the starter now.
          expect(
            succeeded(
              yield* call(NEW_ORCHESTRATOR, "lane_start", { lane: "pr-queue", threadId: PR_LANE }),
            ),
          ).toMatchObject({ generation: 2 });
          expect(succeeded(yield* call(MAIN, "lane_board", {})).lanes).toContainEqual(
            expect.objectContaining({ lane: "pr-queue", startedBy: NEW_ORCHESTRATOR }),
          );
          expect(failed(yield* call(CI_LANE, "lane_stop", { lane: "pr-queue" }))).toMatchObject({
            code: "not-starter",
          });
        }).pipe(
          Effect.provide(toolsLayer(directory, ON)),
          Effect.ensuring(
            Effect.sync(() => {
              shells.delete(OLD_ORCHESTRATOR);
              shells.delete(MOVED);
            }),
          ),
        );
      }),
  );

  it.effect("a lane file that cannot be written: the store's refusal, not a crash", () =>
    Effect.gen(function* () {
      const { fs, directory, file } = yield* workspace;
      yield* fs.makeDirectory(directory, { recursive: true });
      yield* fs.chmod(directory, 0o555);
      yield* Effect.gen(function* () {
        expect(
          failed(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: PR_LANE })),
        ).toMatchObject({ code: "store-unwritable" });
      }).pipe(Effect.provide(toolsLayer(directory, ON)));
      yield* fs.chmod(directory, 0o755);
      assert.isFalse(yield* fs.exists(file));
    }),
  );

  it.effect("lane_board reads the settings once: no window where lanes show as undefined", () =>
    Effect.gen(function* () {
      const { directory } = yield* workspace;
      const settings = flakySettings(ON);
      yield* Effect.gen(function* () {
        succeeded(yield* call(MAIN, "lane_start", { lane: "pr-queue", threadId: PR_LANE }));
        // One read for the board; any second read fails.
        settings.allow(1);
        const board = succeeded(yield* call(MAIN, "lane_board", {}));
        expect(
          board.lanes.find((lane: { lane: string }) => lane.lane === "pr-queue"),
        ).toMatchObject({ status: "running", owns: ["pr"] });
        expect(
          board.lanes.filter((lane: { status: string }) => lane.status === "undefined"),
        ).toEqual([]);
        // Settings that cannot be read at all: a plain refusal, not a board of undefined lanes.
        settings.allow(0);
        expect(failed(yield* call(MAIN, "lane_board", {}))).toMatchObject({
          code: "settings-unreadable",
          message: expect.stringContaining("cannot be read"),
        });
      }).pipe(Effect.provide(toolsLayerWith(directory, settings.layer)));
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
