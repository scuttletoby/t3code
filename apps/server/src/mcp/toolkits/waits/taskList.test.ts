/**
 * Fork (issues 386, 625): `task_list` through the registered tool. The state question: a linked
 * environment that went away (its tasks read as unknown, never as live slots, and it is asked
 * once), a stop still owed there, settled results (left out unless asked), old rows with no
 * delivery record, an environment with more threads than one page, and an empty thread.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, expect, it } from "@effect/vitest";
import {
  EnvironmentId,
  type OrchestrationV2Subagent,
  type OrchestrationV2ThreadShell,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import { McpSchema, McpServer } from "effect/ai";
import { TestClock } from "effect/testing";

import { delegatedTask, PARENT } from "../../../fork/delegatedResults/delegatedResults.testkit.ts";
import * as ThreadManagement from "../../../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../../../peer/PeerForwarding.ts";
import * as RemoteDelegation from "../../../peer/RemoteDelegation.ts";
import { Playtest } from "../../../playtest/PlaytestLayer.ts";
import { LaneStore } from "../../../fork/lanes/LaneStore.ts";
import { ServerSettingsService } from "../../../serverSettings.ts";
import { Waits } from "../../../waits/WaitWorker.ts";
import * as McpHttpServer from "../../McpHttpServer.ts";
import * as McpInvocationContext from "../../McpInvocationContext.ts";
import * as McpToolAccessTestkit from "../../McpToolAccess.testkit.ts";
import { ORPHAN_PAGES, REMOTE_READ_TIMEOUT } from "./taskList.ts";

const HERE = EnvironmentId.make("env-here");
const ENV_UP = EnvironmentId.make("env-up");
const ENV_GONE = EnvironmentId.make("env-gone");
/** Answers every page with more to come: more threads than `task_list` reads. */
const ENV_BIG = EnvironmentId.make("env-big");
/** Answers its first page, never its second. */
const ENV_CUT = EnvironmentId.make("env-cut");

const remoteTask = (id: string, environmentId: EnvironmentId, status = "running") =>
  delegatedTask({
    id,
    childThreadId: null,
    remoteChild: {
      environmentId,
      threadId: ThreadId.make(`remote-${id}`),
      label: environmentId,
      runId: `run-${id}` as never,
    },
    status: status as OrchestrationV2Subagent["status"],
    ...(status === "running" ? { completionDelivery: undefined, completedAt: null } : {}),
  });

const shell = (
  id: ThreadId,
  overrides: Partial<OrchestrationV2ThreadShell> = {},
): OrchestrationV2ThreadShell => ({
  ...McpToolAccessTestkit.liveThreadShell(id, { activeRunId: null }),
  status: "idle",
  ...overrides,
});

const scope: McpInvocationContext.McpInvocationScope = {
  environmentId: HERE,
  requestNamespace: "task-list",
  thread: {
    threadId: PARENT,
    providerSessionId: "task-list",
    providerInstanceId: ProviderInstanceId.make("codex"),
  },
  client: undefined,
  issuedAt: 0,
  capabilities: new Set(["orchestration"]),
};

const client = McpSchema.McpServerClient.of({
  clientId: 1,
  protocolVersion: "2025-06-18",
  clientCapabilities: {},
  clientInfo: { name: "task-list", version: "1" },
  initializePayload: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "task-list", version: "1" },
  },
  getClient: Effect.die("unused"),
});

interface World {
  readonly tasks: ReadonlyArray<OrchestrationV2Subagent>;
  readonly shells: ReadonlyMap<ThreadId, OrchestrationV2ThreadShell>;
  /** Local children of this thread, as upstream lists them (also those of settled tasks). */
  readonly children?: ReadonlyArray<ThreadId>;
}

/** Reads of a run there, per environment; tests read it. */
const runStatusReads = new Map<EnvironmentId, number>();

const toolsLayer = (world: World) => {
  let omitSettled: boolean | undefined;
  runStatusReads.clear();
  const services = Layer.mergeAll(
    ServerSettingsService.layerTest({}),
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadShell: (threadId) =>
        Effect.succeed(
          threadId === PARENT
            ? McpToolAccessTestkit.liveThreadShell(PARENT)
            : (world.shells.get(threadId) ?? null),
        ),
      getThreadRecords: ((
        _threadId: ThreadId,
        _fields: unknown,
        filter?: { omitSettledSubagents?: true },
      ) => {
        omitSettled = filter?.omitSettledSubagents === true;
        return Effect.succeed({
          subagents: omitSettled
            ? world.tasks.filter(
                (task) =>
                  !["delivered", "acknowledged", "disposed"].includes(
                    task.completionDelivery?.state ?? "",
                  ),
              )
            : world.tasks,
        });
      }) as never,
      listAppOwnedChildThreadIds: () =>
        Effect.succeed(
          world.children ??
            world.tasks.flatMap((task) =>
              task.origin === "app_owned" && task.childThreadId !== null
                ? [task.childThreadId]
                : [],
            ),
        ),
    }),
    Layer.mock(Waits)({}),
    Layer.mock(LaneStore)({}),
    Layer.mock(Playtest)({ filePath: "unused" }),
    Layer.mock(RemoteDelegation.RemoteDelegation)({
      // The environment that went away never answers; the other one says the run is going.
      runStatus: (_scope, task) => {
        const environmentId = task.remoteChild!.environmentId;
        runStatusReads.set(environmentId, (runStatusReads.get(environmentId) ?? 0) + 1);
        return environmentId === ENV_GONE ? Effect.never : Effect.succeed("running");
      },
    }),
    Layer.mock(PeerForwarding.PeerForwarding)({
      links: () =>
        Effect.succeed({
          environmentId: HERE,
          links: [ENV_UP, ENV_GONE, ENV_BIG, ENV_CUT].map((environmentId) => ({
            environmentId,
            label: environmentId,
            status: "reachable" as const,
            access: {} as never,
            expiresAt: "2027-01-01T00:00:00.000Z",
            lastError: null,
          })),
          notListed: "",
        }),
      call: ((
        _scope: unknown,
        _tool: unknown,
        environmentId: EnvironmentId,
        params: { readonly cursor: number },
      ) =>
        environmentId === ENV_GONE || (environmentId === ENV_CUT && params.cursor > 0)
          ? Effect.never
          : environmentId === ENV_CUT
            ? Effect.succeed({
                projectId: null,
                currentThreadId: null,
                nextCursor: 100,
                total: 200,
                threads: [],
              })
            : environmentId === ENV_BIG
              ? Effect.succeed({
                  projectId: null,
                  currentThreadId: null,
                  nextCursor: params.cursor + 100,
                  total: 10_000,
                  threads: [{ threadId: ThreadId.make(`big-${params.cursor}`), title: "busy" }],
                })
              : params.cursor > 0
                ? // The second page: an older thread this one launched, also lost.
                  Effect.succeed({
                    projectId: null,
                    currentThreadId: null,
                    nextCursor: null,
                    total: 101,
                    threads: [
                      {
                        threadId: ThreadId.make("remote-lost-older"),
                        title: "older lost one",
                        delegatedFrom: { environmentId: HERE, threadId: PARENT, title: "p" },
                      },
                    ],
                  })
                : Effect.succeed({
                    projectId: null,
                    currentThreadId: null,
                    nextCursor: 100,
                    total: 101,
                    threads: [
                      // Followed by a task here.
                      {
                        threadId: ThreadId.make("remote-r-up"),
                        title: "known",
                        delegatedFrom: { environmentId: HERE, threadId: PARENT, title: "p" },
                      },
                      // Launched by this thread, but its record never ran.
                      {
                        threadId: ThreadId.make("remote-lost"),
                        title: "lost one",
                        delegatedFrom: { environmentId: HERE, threadId: PARENT, title: "p" },
                      },
                      // Another thread's child.
                      {
                        threadId: ThreadId.make("remote-other"),
                        title: "other",
                        delegatedFrom: {
                          environmentId: HERE,
                          threadId: ThreadId.make("thread-other"),
                          title: "o",
                        },
                      },
                      // Launched by a thread of the same id in another environment.
                      {
                        threadId: ThreadId.make("remote-foreign"),
                        title: "foreign",
                        delegatedFrom: {
                          environmentId: EnvironmentId.make("env-other"),
                          threadId: PARENT,
                          title: "p",
                        },
                      },
                      { threadId: ThreadId.make("remote-own"), title: "its own thread" },
                    ],
                  })) as never,
    }),
  ).pipe(Layer.provideMerge(NodeServices.layer));
  return McpHttpServer.layerForkTools.pipe(
    Layer.provideMerge(McpHttpServer.layerInProcessMcpServer),
    Layer.provideMerge(services),
  );
};

const callTaskList = (args: Record<string, unknown>) =>
  Effect.gen(function* () {
    const server = yield* McpServer.McpServer;
    const result = yield* server
      .callTool({ name: "task_list", arguments: args })
      .pipe(
        Effect.provideService(McpInvocationContext.McpInvocationContext, scope),
        Effect.provideService(McpSchema.McpServerClient, client),
      );
    assert.notStrictEqual(result.isError, true, JSON.stringify(result.content));
    return result.structuredContent as {
      readonly tasks: ReadonlyArray<Record<string, unknown>>;
      readonly owed: number;
      readonly unrecorded: number;
      readonly slots: ReadonlyArray<{ where: string; live: number; unknown: number }>;
      readonly orphans?: ReadonlyArray<{ environmentId: string; threadId: string }>;
      readonly unreachable?: ReadonlyArray<string>;
      readonly next: string;
    };
  });

/** Runs the call while the clock moves past each remote read timeout (activity, then links). */
const withTimeouts = <A, E, R>(effect: Effect.Effect<A, E, R>) =>
  Effect.gen(function* () {
    const fiber = yield* Effect.forkChild(effect);
    for (let step = 0; step < 4; step++) {
      yield* Effect.repeat(Effect.yieldNow, { times: 20 });
      yield* TestClock.adjust(REMOTE_READ_TIMEOUT);
    }
    return yield* Fiber.join(fiber);
  });

const mixedWorld: World = {
  tasks: [
    // Local, ended, result claimed but never delivered.
    delegatedTask({ id: "l-owed" }),
    // Local, ended, read already.
    delegatedTask({
      id: "l-read",
      completionDelivery: { state: "acknowledged", observedByRunId: null },
    }),
    // Local, ended before delivery records were kept.
    delegatedTask({ id: "l-old", completionDelivery: undefined }),
    // Local, still running.
    delegatedTask({ id: "l-live", status: "running", completionDelivery: undefined }),
    remoteTask("r-up", ENV_UP),
    remoteTask("r-gone", ENV_GONE),
    // Remote, ended with an owed result: its end is recorded, no read there.
    remoteTask("r-done", ENV_GONE, "completed"),
    // The provider's own subagent is never listed.
    delegatedTask({ id: "native", origin: "provider_native" }),
  ],
  shells: new Map([
    [ThreadId.make("child-l-owed"), shell(ThreadId.make("child-l-owed"))],
    [ThreadId.make("child-l-read"), shell(ThreadId.make("child-l-read"))],
    [ThreadId.make("child-l-old"), shell(ThreadId.make("child-l-old"))],
    [
      ThreadId.make("child-l-live"),
      shell(ThreadId.make("child-l-live"), { status: "running", activityRunStatus: "running" }),
    ],
  ]),
};

describe("task_list", () => {
  it.effect("lists unread results, local and remote, and live slots from live status", () =>
    Effect.gen(function* () {
      const listed = yield* withTimeouts(callTaskList({}));
      const byId = Object.fromEntries(listed.tasks.map((task) => [task.taskId, task]));
      expect(Object.keys(byId).toSorted()).toEqual(
        ["l-live", "l-old", "l-owed", "r-done", "r-gone", "r-up"].toSorted(),
      );
      expect(byId["l-owed"]).toMatchObject({ result: "owed", activity: "ended", where: "local" });
      expect(byId["l-old"]).toMatchObject({ result: "none" });
      expect(byId["l-live"]).toMatchObject({ result: null, activity: "live" });
      expect(byId["r-up"]).toMatchObject({ activity: "live", where: ENV_UP });
      // The environment that went away: unknown, never counted as a running child.
      expect(byId["r-gone"]).toMatchObject({ activity: "unknown", where: ENV_GONE });
      expect(byId["r-done"]).toMatchObject({ activity: "ended", result: "owed" });
      expect(listed.owed).toBe(2);
      expect(listed.unrecorded).toBe(1);
      expect(listed.slots).toEqual([
        { where: "local", live: 1, unknown: 0 },
        { where: ENV_GONE, live: 0, unknown: 1 },
        { where: ENV_UP, live: 1, unknown: 0 },
      ]);
      expect(listed.orphans).toBeUndefined();
      expect(listed.next).toContain("task_status");
    }).pipe(Effect.provide(toolsLayer(mixedWorld))),
  );

  it.effect("lists settled results too when asked", () =>
    Effect.gen(function* () {
      const listed = yield* withTimeouts(callTaskList({ includeSettled: true }));
      expect(listed.tasks.find((task) => task.taskId === "l-read")).toMatchObject({
        result: "settled",
      });
      expect(listed.owed).toBe(2);
    }).pipe(Effect.provide(toolsLayer(mixedWorld))),
  );

  it.effect("counts a live child of a settled task as a slot", () =>
    Effect.gen(function* () {
      const listed = yield* callTaskList({});
      expect(listed.tasks).toEqual([]);
      expect(listed.slots).toEqual([{ where: "local", live: 1, unknown: 0 }]);
    }).pipe(
      Effect.provide(
        toolsLayer({
          tasks: [
            delegatedTask({
              id: "sent-more",
              completionDelivery: { state: "acknowledged", observedByRunId: null },
            }),
          ],
          shells: new Map([
            [
              ThreadId.make("child-sent-more"),
              shell(ThreadId.make("child-sent-more"), {
                status: "running",
                activityRunStatus: "running",
              }),
            ],
          ]),
        }),
      ),
    ),
  );

  it.effect("finds a thread launched in a linked environment that no task follows", () =>
    Effect.gen(function* () {
      const listed = yield* withTimeouts(callTaskList({ checkLinked: true }));
      // Its second page too; never one launched by another environment's thread of that id.
      expect(listed.orphans).toEqual([
        { environmentId: ENV_UP, threadId: "remote-lost", title: "lost one" },
        { environmentId: ENV_UP, threadId: "remote-lost-older", title: "older lost one" },
      ]);
      expect(listed.unreachable).toEqual([ENV_GONE]);
      expect(listed.next).toContain("t3_thread_read");
      // The environment with more threads than it reads is named.
      expect(listed.next).toContain(`newest ${ORPHAN_PAGES * 100} threads of ${ENV_BIG}.`);
      // One whose later page did not answer is named for that, not for the page limit.
      expect(listed.next).toContain(`A later page of ${ENV_CUT} did not answer`);
      // Settled tasks are read to know what is followed, but listed only when asked.
      expect(listed.tasks.map((task) => task.taskId)).not.toContain("l-read");
    }).pipe(Effect.provide(toolsLayer(mixedWorld))),
  );

  it.effect("reads a remote task there while the stop it owes there is not sent", () =>
    Effect.gen(function* () {
      const listed = yield* callTaskList({});
      const byId = Object.fromEntries(listed.tasks.map((task) => [task.taskId, task]));
      expect(byId["r-stop"]).toMatchObject({ activity: "live", where: ENV_UP });
      expect(byId["r-stopped"]).toMatchObject({ activity: "ended", where: ENV_UP });
      expect(listed.slots).toEqual([{ where: ENV_UP, live: 1, unknown: 0 }]);
      // Only the one whose stop is owed was read there.
      expect(runStatusReads.get(ENV_UP)).toBe(1);
    }).pipe(
      Effect.provide(
        toolsLayer({
          tasks: [
            {
              ...remoteTask("r-stop", ENV_UP, "completed"),
              remoteChild: { ...remoteTask("r-stop", ENV_UP).remoteChild!, stoppedThere: false },
            },
            {
              ...remoteTask("r-stopped", ENV_UP, "completed"),
              remoteChild: { ...remoteTask("r-stopped", ENV_UP).remoteChild!, stoppedThere: true },
            },
          ],
          shells: new Map(),
        }),
      ),
    ),
  );

  it.effect("waits for an environment that does not answer once, not once per task", () =>
    Effect.gen(function* () {
      // Eight open tasks there, four read at a time: after the first timeouts the rest are
      // unknown without a read (it was two rounds of reads, eight in all).
      const listed = yield* withTimeouts(callTaskList({}));
      expect(listed.slots).toEqual([{ where: ENV_GONE, live: 0, unknown: 8 }]);
      expect(runStatusReads.get(ENV_GONE)).toBe(4);
    }).pipe(
      Effect.provide(
        toolsLayer({
          tasks: Array.from({ length: 8 }, (_, index) => remoteTask(`g-${index}`, ENV_GONE)),
          shells: new Map(),
        }),
      ),
    ),
  );

  it.effect("an empty thread lists nothing and owes nothing", () =>
    Effect.gen(function* () {
      const listed = yield* callTaskList({});
      expect(listed).toMatchObject({ tasks: [], owed: 0, unrecorded: 0, slots: [] });
      expect(listed.next).toContain("Nothing unread");
    }).pipe(Effect.provide(toolsLayer({ tasks: [], shells: new Map(), children: [] }))),
  );
});
