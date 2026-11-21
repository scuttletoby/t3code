/**
 * Fork: a thread with a narrower MCP profile than `all` cannot get a wider thread started
 * through `schedule_task` (a task whose runs start new threads) or `t3_thread_transfer`'s fork,
 * through upstream's real handlers and access rules. The forks resolve over a real thread store
 * (SQLite in memory), as the server reads a fork's source thread.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { expect, it } from "@effect/vitest";
import {
  EnvironmentId,
  EventId,
  type OrchestrationV2AppThread,
  type OrchestrationV2ThreadShell,
  OrchestratorMcpFailure,
  type OrchestratorMcpScheduledTask,
  ProjectId,
  ProviderInstanceId,
  type ScheduledTask,
  ScheduledTaskId,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Stream from "effect/Stream";

import * as ServerConfig from "../config.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import * as OrchestratorMcpService from "../mcp/OrchestratorMcpService.ts";
import * as ThreadMetadataMcpService from "../mcp/ThreadMetadataMcpService.ts";
import * as OrchestratorHandlers from "../mcp/toolkits/orchestrator/handlers.ts";
import { OrchestratorToolkit } from "../mcp/toolkits/orchestrator/tools.ts";
import * as ThreadHandlers from "../mcp/toolkits/thread/handlers.ts";
import { ThreadToolkit } from "../mcp/toolkits/thread/tools.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ThreadSearch from "../orchestration-v2/ThreadSearch.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinkRequests from "../peer/PeerLinkRequests.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ScheduledTasks from "../scheduledTasks/ScheduledTaskService.ts";
import * as ServerSettings from "../serverSettings.ts";

const PROJECT = ProjectId.make("project:wide");
const NARROW_PROJECT = ProjectId.make("project:narrow");
const BARE_PROJECT = ProjectId.make("project:bare");
const CALLER = ThreadId.make("thread:caller");
const WIDE = ThreadId.make("thread:wide");
const ORPHAN = ThreadId.make("thread:orphan");
const INSTANCE = ProviderInstanceId.make("claudeAgent");
const HERE = EnvironmentId.make("environment-here");
const THERE = EnvironmentId.make("environment-there");
const NOW = DateTime.makeUnsafe("2026-11-20T00:00:00.000Z");

type Settings = Parameters<typeof ServerSettings.layerTest>[0];

/** A profile that keeps one outside server and every T3 tool. */
const DOCS_ONLY = { mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } } };
const pickFor = (profile: string) => ({ mcpThreadProfiles: { [CALLER]: profile } });
const projects = (overrides: Record<string, { readonly mcpProfile?: string }>) => ({
  projectSettingsFolded: true,
  projectSettingsOverrides: overrides,
});

const appThread = (
  id: ThreadId,
  projectId: ProjectId,
  parent?: ThreadId,
): OrchestrationV2AppThread =>
  ({
    createdBy: "user",
    creationSource: "web",
    id,
    projectId,
    title: `Thread ${id}`,
    providerInstanceId: INSTANCE,
    modelSelection: { instanceId: INSTANCE, model: "claude-opus" },
    runtimeMode: "full-access",
    interactionMode: "default",
    branch: null,
    worktreePath: null,
    activeProviderThreadId: null,
    lineage: {
      parentThreadId: parent ?? null,
      relationshipToParent: parent === undefined ? null : "subagent",
      rootThreadId: parent ?? id,
    },
    forkedFrom: null,
    createdAt: NOW,
    updatedAt: NOW,
    archivedAt: null,
    settledOverride: null,
    settledAt: null,
    lastVisitedAt: null,
    deletedAt: null,
  }) as unknown as OrchestrationV2AppThread;

/** The caller, a thread in PROJECT; a wide thread beside it; one whose parent is gone (old state). */
const THREADS = [
  appThread(CALLER, PROJECT),
  appThread(WIDE, PROJECT),
  appThread(ORPHAN, PROJECT, ThreadId.make("thread:deleted-parent")),
];

const shellOf = (thread: OrchestrationV2AppThread): OrchestrationV2ThreadShell =>
  ({
    ...thread,
    activeRunId: thread.id === CALLER ? "run:1" : null,
  }) as unknown as OrchestrationV2ThreadShell;

const storedTask = (
  patch: Omit<Partial<ScheduledTask>, "id"> & { readonly id: string },
): ScheduledTask =>
  ({
    title: "Old task",
    prompt: "Do the work",
    enabled: true,
    schedule: { type: "interval", everyMs: 3_600_000 },
    projectId: PROJECT,
    threadId: null,
    workspaceStrategy: { type: "local" },
    modelSelection: { instanceId: INSTANCE, model: "claude-opus" },
    runtimeMode: "full-access",
    interactionMode: "default",
    // Made by an agent through MCP before this check existed.
    createdBy: "agent",
    creationSource: "mcp",
    createdAt: "2026-11-01T00:00:00.000Z",
    updatedAt: "2026-11-01T00:00:00.000Z",
    nextRunAt: null,
    lastRunAt: null,
    lastRunStatus: "never",
    lastRunError: null,
    runCount: 0,
    ...patch,
    id: ScheduledTaskId.make(patch.id),
  }) as ScheduledTask;

const TASKS = {
  /** Runs start a new thread in PROJECT, which is left on all. */
  newThread: storedTask({ id: "scheduled-task:new-thread" }),
  /** Runs go into the caller itself. */
  bound: storedTask({ id: "scheduled-task:bound", threadId: CALLER, threadMode: "continue" }),
  /** Runs start a new thread in a project kept to `docs`. */
  narrow: storedTask({ id: "scheduled-task:narrow", projectId: NARROW_PROJECT }),
};

/** What the service answers for a stored task; only that the call went matters here. */
const summary = (id: string): OrchestratorMcpScheduledTask => ({
  scheduledTaskId: ScheduledTaskId.make(id),
  title: "Task",
  prompt: "Do the work",
  enabled: true,
  projectId: PROJECT,
  boundThreadId: null,
  freshSession: false,
  schedule: { type: "interval", everyMs: 3_600_000 },
  nextRunAt: null,
  lastRunStatus: "never",
});

interface Recorded {
  readonly scheduled: Array<unknown>;
  readonly updated: Array<unknown>;
  readonly dispatched: Array<{ readonly type: string }>;
  readonly ran: Array<unknown>;
  /** Calls sent to another machine, by tool. */
  readonly forwarded: Array<string>;
}

/**
 * Runs `body` with upstream's orchestrator and thread toolkits over the settings given, a thread
 * caller (or the owner's own client with `caller: false`), a thread store holding THREADS, and
 * every scheduled task write, run, thread command and call sent to another machine recorded. `scheduledTasks: false` leaves the
 * scheduled task service out (it cannot be read).
 */
const withToolkits = <A, E>(
  settings: object,
  body: (input: {
    readonly recorded: Recorded;
    readonly schedule: (params: object) => Effect.Effect<string | undefined>;
    readonly fork: (threadId: ThreadId, params?: object) => Effect.Effect<string | undefined>;
    readonly runNow: (taskId: string, params?: object) => Effect.Effect<string | undefined>;
  }) => Effect.Effect<A, E>,
  options: { readonly caller?: boolean; readonly scheduledTasks?: boolean } = {},
) => {
  const recorded: Recorded = { scheduled: [], updated: [], dispatched: [], ran: [], forwarded: [] };
  const threadCaller = options.caller !== false;
  const store = ProjectionStore.layer.pipe(Layer.provideMerge(SqlitePersistence.layerMemory));
  const dependencies = Layer.mergeAll(
    store,
    ServerSettings.layerTest({
      fallbackModelSelection: { instanceId: INSTANCE, model: "claude-opus" },
      ...settings,
    } as Settings),
    Layer.succeed(McpInvocationContext.McpInvocationContext, {
      environmentId: HERE,
      requestNamespace: "thread:caller:session",
      thread: threadCaller
        ? { threadId: CALLER, providerSessionId: "session", providerInstanceId: INSTANCE }
        : undefined,
      client: threadCaller
        ? undefined
        : { sessionId: "session-1", label: "Owner", access: "full-access" },
      issuedAt: 0,
      capabilities: new Set(["orchestration" as const]),
    }),
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadShell: (threadId) => {
        const thread = THREADS.find((candidate) => candidate.id === threadId);
        return Effect.succeed(thread === undefined ? null : shellOf(thread));
      },
      getProjectThreadRecords: ({ threadId }) => {
        const thread = THREADS.find((candidate) => candidate.id === threadId)!;
        return Effect.succeed({ thread, runs: [] } as never);
      },
      dispatch: (command) => {
        recorded.dispatched.push(command);
        return Effect.succeed({ sequence: recorded.dispatched.length } as never);
      },
    }),
    Layer.mock(OrchestratorMcpService.OrchestratorMcpService)({
      scheduleTask: (_scope, input) => {
        recorded.scheduled.push(input);
        return Effect.succeed(summary("scheduled-task:made"));
      },
      updateScheduledTask: (_scope, input) => {
        recorded.updated.push(input);
        return Effect.succeed(summary(input.scheduledTaskId));
      },
    }),
    // Left out, the toolkits still build: upstream's handlers read the service per call.
    (options.scheduledTasks === false
      ? Layer.empty
      : Layer.mock(ScheduledTasks.ScheduledTaskService)({
          list: () => Effect.succeed({ tasks: Object.values(TASKS) }),
          runNow: (input) => {
            recorded.ran.push(input);
            const task = Object.values(TASKS).find((candidate) => candidate.id === input.id)!;
            return Effect.succeed({ task, threadId: null });
          },
        })) as Layer.Layer<ScheduledTasks.ScheduledTaskService>,
    Layer.mock(ThreadMetadataMcpService.ThreadMetadataMcpService)({}),
    Layer.mock(PeerForwarding.PeerForwarding)({
      call: (_scope, tool) => {
        recorded.forwarded.push(tool.name);
        // What the machine there answers does not matter here, only that the call went.
        return Effect.fail(
          new OrchestratorMcpFailure({ code: "orchestration_error", message: "sent there" }),
        ) as never;
      },
    }),
    Layer.mock(PeerLinkRequests.PeerLinkRequests)({}),
    Layer.mock(PeerLinks.PeerLinks)({}),
    Layer.mock(RemoteDelegation.RemoteDelegation)({}),
    Layer.mock(ThreadSearch.ThreadSearch)({}),
  ).pipe(
    Layer.provideMerge(
      ServerConfig.layerTest(process.cwd(), { prefix: "t3-schedule-fork-mcp-profile-" }).pipe(
        Layer.provide(NodeServices.layer),
      ),
    ),
    Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer)),
  );
  return Effect.scoped(
    Effect.gen(function* () {
      const context = yield* Layer.build(dependencies);
      const provided = Layer.succeedContext(context);
      const projection = yield* ProjectionStore.ProjectionStoreV2.pipe(Effect.provide(context));
      for (const thread of THREADS) {
        yield* projection.apply({
          id: EventId.make(`seed:${thread.id}`),
          type: "thread.created",
          threadId: thread.id,
          occurredAt: NOW,
          payload: thread,
        } as never);
      }
      const orchestrator = yield* OrchestratorToolkit.pipe(
        Effect.provide(
          McpToolAccess.HandlersLayer.layer(OrchestratorHandlers.layer).pipe(
            Layer.provide(provided),
          ),
        ),
      );
      const threads = yield* ThreadToolkit.pipe(
        Effect.provide(
          McpToolAccess.HandlersLayer.layer(ThreadHandlers.layer).pipe(Layer.provide(provided)),
        ),
      );
      const schedule = (params: object) =>
        orchestrator
          .handle("schedule_task", {
            prompt: "Do the work",
            schedule: { type: "interval", everyMs: 3_600_000 },
            ...params,
          } as never)
          .pipe(
            Stream.unwrap,
            Stream.runCollect,
            Effect.map(refusal),
            Effect.provide(context),
            Effect.orDie,
          );
      const fork = (threadId: ThreadId, params: object = {}) =>
        threads
          .handle("t3_thread_transfer", { action: "fork", threadId, ...params } as never)
          .pipe(
            Stream.unwrap,
            Stream.runCollect,
            Effect.map(refusal),
            Effect.provide(context),
            Effect.orDie,
          );
      const runNow = (taskId: string, params: object = {}) =>
        threads
          .handle("run_scheduled_task_now", { taskId, ...params } as never)
          .pipe(
            Stream.unwrap,
            Stream.runCollect,
            Effect.map(refusal),
            Effect.provide(context),
            Effect.orDie,
          );
      return yield* body({ recorded, schedule, fork, runNow });
    }),
  ).pipe(Effect.orDie);
};

/** `code: message` of a refused call, or `undefined` for one that went through. */
const refusal = (chunks: Iterable<unknown>): string | undefined => {
  const last = [...chunks].at(-1) as
    | { readonly isFailure: boolean; readonly result: { code?: string; message?: string } }
    | undefined;
  return last?.isFailure === true ? `${last.result.code}: ${last.result.message}` : undefined;
};

const NARROWED = { ...DOCS_ONLY, ...pickFor("docs") };

it.effect("a narrowed thread cannot schedule a task whose runs start a wider thread", () =>
  withToolkits(NARROWED, ({ recorded, schedule }) =>
    Effect.gen(function* () {
      const message = yield* schedule({ bindToCurrentThread: false });
      expect(message).toContain('capability_denied: This thread\'s MCP profile is "docs"');
      expect(message).toContain("a thread this task's runs start in that project");
      expect(message).toContain("bindToCurrentThread: true");
      // Runs again: an agent's retry with the same key, again, then a new call. Each is
      // refused before anything is stored.
      for (const clientRequestId of ["retry-1", "retry-1", undefined]) {
        expect(
          yield* schedule({
            bindToCurrentThread: false,
            ...(clientRequestId && { clientRequestId }),
          }),
        ).toContain("capability_denied");
      }
      // Another project left on all, which a task there gets by default (not bound).
      expect(yield* schedule({ projectId: BARE_PROJECT })).toContain("capability_denied");
      expect(recorded.scheduled).toHaveLength(0);
    }),
  ),
);

it.effect("a task bound to the caller, or in a project within its profile, is scheduled", () =>
  withToolkits(
    { ...NARROWED, ...projects({ [NARROW_PROJECT]: { mcpProfile: "docs" } }) },
    ({ recorded, schedule }) =>
      Effect.gen(function* () {
        // Its own project: bound to the caller by default, so runs keep the caller's profile.
        expect(yield* schedule({})).toBeUndefined();
        expect(yield* schedule({ freshSession: true })).toBeUndefined();
        expect(yield* schedule({ projectId: NARROW_PROJECT })).toBeUndefined();
        expect(recorded.scheduled).toHaveLength(3);
      }),
  ),
);

it.effect("a project with no profile of its own gives the server's to the task's runs", () =>
  Effect.gen(function* () {
    // The server keeps every thread to `docs`, and BARE_PROJECT sets nothing.
    const server = { ...DOCS_ONLY, mcpProfile: "docs" };
    yield* withToolkits({ ...server, ...pickFor("docs") }, ({ recorded, schedule }) =>
      Effect.gen(function* () {
        expect(yield* schedule({ projectId: BARE_PROJECT })).toBeUndefined();
        expect(recorded.scheduled).toHaveLength(1);
      }),
    );
    // `t3-only` keeps no outside server, so a `docs` run is wider.
    yield* withToolkits({ ...server, ...pickFor("t3-only") }, ({ recorded, schedule }) =>
      Effect.gen(function* () {
        expect(yield* schedule({ projectId: BARE_PROJECT })).toContain(
          'would get "docs", which keeps MCP servers or T3 tools "t3-only" does not',
        );
        expect(recorded.scheduled).toHaveLength(0);
      }),
    );
  }),
);

it.effect("a caller whose profile cannot be worked out is refused, never taken as all", () =>
  // A pick of a profile no setting defines (an old or empty state): it cannot apply.
  withToolkits(pickFor("deleted-profile"), ({ recorded, schedule, fork }) =>
    Effect.gen(function* () {
      const scheduled = yield* schedule({ bindToCurrentThread: false });
      expect(scheduled).toContain("invalid_request");
      expect(scheduled).toContain("cannot be worked out");
      expect(yield* schedule({ scheduledTaskId: TASKS.newThread.id })).toContain(
        "cannot be worked out",
      );
      expect(yield* fork(WIDE)).toContain("cannot be worked out");
      expect(recorded.scheduled).toHaveLength(0);
      expect(recorded.updated).toHaveLength(0);
      expect(recorded.dispatched).toHaveLength(0);
    }),
  ),
);

it.effect("a narrowed thread cannot change a task that starts wider threads, old ones too", () =>
  withToolkits(
    { ...NARROWED, ...projects({ [NARROW_PROJECT]: { mcpProfile: "docs" } }) },
    ({ recorded, schedule }) =>
      Effect.gen(function* () {
        // Old state: a task an agent made before this check, whose runs start threads on all.
        // Rewriting, re-enabling or rescheduling it would start them with this thread's words.
        for (const change of [
          { prompt: "Something else" },
          { enabled: true },
          { schedule: { type: "interval", everyMs: 60_000 } },
          { enabled: false },
        ]) {
          const message = yield* schedule({ scheduledTaskId: TASKS.newThread.id, ...change });
          expect(message).toContain("capability_denied");
          expect(message).toContain("The task was not changed.");
          expect(message).toContain("delete_scheduled_task");
        }
        // Unbinding a task from the caller makes its runs start new threads: refused, twice.
        for (let attempt = 0; attempt < 2; attempt++) {
          expect(
            yield* schedule({ scheduledTaskId: TASKS.bound.id, bindToCurrentThread: false }),
          ).toContain("capability_denied");
        }
        expect(recorded.updated).toHaveLength(0);
        // Changes whose runs keep within the caller's profile go through: a bound task, binding
        // a new-thread task to itself, and a task in a project kept to `docs`.
        expect(yield* schedule({ scheduledTaskId: TASKS.bound.id, prompt: "New" })).toBeUndefined();
        expect(
          yield* schedule({ scheduledTaskId: TASKS.newThread.id, bindToCurrentThread: true }),
        ).toBeUndefined();
        expect(
          yield* schedule({ scheduledTaskId: TASKS.narrow.id, prompt: "New" }),
        ).toBeUndefined();
        // A task that is not there is left to the service, which says it was not found.
        expect(
          yield* schedule({ scheduledTaskId: ScheduledTaskId.make("scheduled-task:missing") }),
        ).toBeUndefined();
        expect(recorded.updated).toHaveLength(4);
      }),
  ),
);

it.effect("a change is refused when the scheduled task service cannot be read", () =>
  withToolkits(
    NARROWED,
    ({ recorded, schedule }) =>
      Effect.gen(function* () {
        expect(yield* schedule({ scheduledTaskId: TASKS.newThread.id, prompt: "New" })).toContain(
          "cannot be read here, so the task was not changed",
        );
        expect(recorded.updated).toHaveLength(0);
      }),
    { scheduledTasks: false },
  ),
);

it.effect("a narrowed thread cannot fork a wider thread; it can fork itself", () =>
  withToolkits(NARROWED, ({ recorded, fork }) =>
    Effect.gen(function* () {
      // WIDE gets PROJECT's all, so its fork does too: the fork is cut to its source, not to
      // the thread that asked for it.
      const message = yield* fork(WIDE);
      expect(message).toContain('capability_denied: This thread\'s MCP profile is "docs"');
      expect(message).toContain('a fork of that thread would get "all"');
      // Runs again: each call is new and refused the same way, with nothing forked.
      expect(yield* fork(WIDE)).toContain("capability_denied");
      expect(yield* fork(WIDE)).toContain("capability_denied");
      expect(recorded.dispatched).toHaveLength(0);
      // Its own fork is cut to its own profile.
      expect(yield* fork(CALLER)).toBeUndefined();
      expect(recorded.dispatched.map((command) => command.type)).toEqual(["thread.fork"]);
    }),
  ),
);

it.effect("a fork whose source's parent is gone is refused, never taken as all", () =>
  // Old or missing state: ORPHAN's parent is not in the store, so the fork's profile is unknown.
  withToolkits(NARROWED, ({ recorded, fork }) =>
    Effect.gen(function* () {
      const message = yield* fork(ORPHAN);
      expect(message).toContain("invalid_request");
      expect(message).toContain("The new thread's MCP profile cannot be worked out");
      expect(recorded.dispatched).toHaveLength(0);
    }),
  ),
);

it.effect("a fork into a project with no profile gets the server's", () =>
  Effect.gen(function* () {
    const server = { ...DOCS_ONLY, mcpProfile: "docs" };
    yield* withToolkits({ ...server, ...pickFor("docs") }, ({ recorded, fork }) =>
      Effect.gen(function* () {
        expect(yield* fork(WIDE)).toBeUndefined();
        expect(recorded.dispatched).toHaveLength(1);
      }),
    );
    yield* withToolkits({ ...server, ...pickFor("t3-only") }, ({ recorded, fork }) =>
      Effect.gen(function* () {
        expect(yield* fork(WIDE)).toContain('a fork of that thread would get "docs"');
        expect(recorded.dispatched).toHaveLength(0);
      }),
    );
  }),
);

it.effect("a thread on all, and the owner's own client, schedule and fork as before", () =>
  Effect.gen(function* () {
    yield* withToolkits({}, ({ recorded, schedule, fork }) =>
      Effect.gen(function* () {
        expect(yield* schedule({ bindToCurrentThread: false })).toBeUndefined();
        expect(yield* schedule({ scheduledTaskId: TASKS.newThread.id, prompt: "New" })).toBe(
          undefined,
        );
        expect(yield* fork(WIDE)).toBeUndefined();
        expect(
          [recorded.scheduled, recorded.updated, recorded.dispatched].map((l) => l.length),
        ).toEqual([1, 1, 1]);
      }),
    );
    // The owner's client has no calling thread, so no profile to keep to.
    yield* withToolkits(
      { ...DOCS_ONLY, mcpProfile: "docs" },
      ({ recorded, schedule, fork }) =>
        Effect.gen(function* () {
          expect(yield* schedule({ projectId: PROJECT })).toBeUndefined();
          expect(yield* schedule({ scheduledTaskId: TASKS.newThread.id, prompt: "New" })).toBe(
            undefined,
          );
          expect(yield* fork(WIDE)).toBeUndefined();
          expect(recorded.scheduled).toHaveLength(1);
          expect(recorded.updated).toHaveLength(1);
          expect(recorded.dispatched).toHaveLength(1);
        }),
      { caller: false },
    );
  }),
);

it.effect("a narrowed thread cannot run a task now whose runs start a wider thread", () =>
  withToolkits(
    { ...NARROWED, ...projects({ [NARROW_PROJECT]: { mcpProfile: "docs" } }) },
    ({ recorded, schedule, runNow }) =>
      Effect.gen(function* () {
        // The route the reviews found: change an old task, then run it. Both are refused.
        expect(yield* schedule({ scheduledTaskId: TASKS.newThread.id, prompt: "New" })).toContain(
          "capability_denied",
        );
        const message = yield* runNow(TASKS.newThread.id);
        expect(message).toContain("capability_denied");
        expect(message).toContain("The task was not run.");
        // Runs again: refused each time, with nothing run.
        expect(yield* runNow(TASKS.newThread.id)).toContain("capability_denied");
        expect(recorded.ran).toHaveLength(0);
        // A task bound to the caller runs in it; one in a project kept to `docs` keeps within it.
        expect(yield* runNow(TASKS.bound.id)).toBeUndefined();
        expect(yield* runNow(TASKS.narrow.id)).toBeUndefined();
        expect(recorded.ran).toHaveLength(2);
      }),
  ),
);

it.effect("a run now by a caller whose profile cannot be worked out is refused", () =>
  withToolkits(pickFor("deleted-profile"), ({ recorded, runNow }) =>
    Effect.gen(function* () {
      expect(yield* runNow(TASKS.newThread.id)).toContain("cannot be worked out");
      expect(recorded.ran).toHaveLength(0);
    }),
  ),
);

it.effect("a narrowed thread cannot fork, change or run a task on another machine", () =>
  withToolkits(NARROWED, ({ recorded, schedule, fork, runNow }) =>
    Effect.gen(function* () {
      // A thread started there gets that machine's profile, which this one cannot check.
      const there = { environmentId: THERE };
      const forked = yield* fork(WIDE, there);
      expect(forked).toContain("capability_denied");
      expect(forked).toContain("it was not forked");
      // Its own fork too: there the fork gets that machine's profile, not this thread's.
      expect(yield* fork(CALLER, there)).toContain("capability_denied");
      expect(yield* schedule({ scheduledTaskId: TASKS.bound.id, ...there })).toContain(
        "the task was not changed",
      );
      expect(yield* runNow(TASKS.bound.id, there)).toContain("the task was not run");
      // Runs again: refused each time, and nothing is sent there.
      expect(yield* fork(WIDE, there)).toContain("capability_denied");
      expect(recorded.forwarded).toEqual([]);
    }),
  ),
);

it.effect("a thread on all forks, changes and runs tasks on another machine as before", () =>
  withToolkits({}, ({ recorded, schedule, fork, runNow }) =>
    Effect.gen(function* () {
      const there = { environmentId: THERE };
      expect(yield* fork(WIDE, there)).toContain("sent there");
      expect(yield* schedule({ scheduledTaskId: TASKS.bound.id, ...there })).toContain(
        "sent there",
      );
      expect(yield* runNow(TASKS.bound.id, there)).toContain("sent there");
      expect(recorded.forwarded).toEqual([
        "t3_thread_transfer",
        "schedule_task",
        "run_scheduled_task_now",
      ]);
    }),
  ),
);
