/**
 * Fork (issue 386): `task_list` through the registered tool. The state question: a linked
 * environment that went away (its tasks read as unknown, never as live slots), settled results
 * (left out unless asked), old rows with no delivery record, and an empty thread.
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
import { ServerSettingsService } from "../../../serverSettings.ts";
import { Waits } from "../../../waits/WaitWorker.ts";
import * as McpHttpServer from "../../McpHttpServer.ts";
import * as McpInvocationContext from "../../McpInvocationContext.ts";
import * as McpToolAccessTestkit from "../../McpToolAccess.testkit.ts";
import { REMOTE_READ_TIMEOUT } from "./taskList.ts";

const HERE = EnvironmentId.make("env-here");
const ENV_UP = EnvironmentId.make("env-up");
const ENV_GONE = EnvironmentId.make("env-gone");

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

const toolsLayer = (world: World) => {
  let omitSettled: boolean | undefined;
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
    Layer.mock(RemoteDelegation.RemoteDelegation)({
      // The environment that went away never answers; the other one says the run is going.
      runStatus: (_scope, task) =>
        task.remoteChild?.environmentId === ENV_GONE ? Effect.never : Effect.succeed("running"),
    }),
    Layer.mock(PeerForwarding.PeerForwarding)({
      links: () =>
        Effect.succeed({
          environmentId: HERE,
          links: [ENV_UP, ENV_GONE].map((environmentId) => ({
            environmentId,
            label: environmentId,
            status: "reachable" as const,
            access: {} as never,
            expiresAt: "2027-01-01T00:00:00.000Z",
            lastError: null,
          })),
          notListed: "",
        }),
      call: ((_scope: unknown, _tool: unknown, environmentId: EnvironmentId) =>
        environmentId === ENV_GONE
          ? Effect.never
          : Effect.succeed({
              projectId: null,
              currentThreadId: null,
              nextCursor: null,
              total: 3,
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
      expect(listed.orphans).toEqual([
        { environmentId: ENV_UP, threadId: "remote-lost", title: "lost one" },
      ]);
      expect(listed.unreachable).toEqual([ENV_GONE]);
      expect(listed.next).toContain("t3_thread_read");
    }).pipe(Effect.provide(toolsLayer(mixedWorld))),
  );

  it.effect("an empty thread lists nothing and owes nothing", () =>
    Effect.gen(function* () {
      const listed = yield* callTaskList({});
      expect(listed).toMatchObject({ tasks: [], owed: 0, unrecorded: 0, slots: [] });
      expect(listed.next).toContain("Nothing unread");
    }).pipe(Effect.provide(toolsLayer({ tasks: [], shells: new Map(), children: [] }))),
  );
});
