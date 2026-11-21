/**
 * Fork: the MCP profile a scheduled task was saved with, through upstream's real service over a
 * real task table (SQLite in memory) and state folder: saves, scheduled runs, and missing, old,
 * half-written, corrupt and pruned records.
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
  ProjectId,
  ProviderInstanceId,
  type ScheduledTask,
  ScheduledTaskId,
  type ScheduledTaskUpsertInput,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as SqlClient from "effect/sql/SqlClient";
import * as Stream from "effect/Stream";
import * as TestClock from "effect/testing/TestClock";

import * as ServerConfig from "../config.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import * as OrchestratorMcpService from "../mcp/OrchestratorMcpService.ts";
import * as ThreadMetadataMcpService from "../mcp/ThreadMetadataMcpService.ts";
import * as OrchestratorHandlers from "../mcp/toolkits/orchestrator/handlers.ts";
import { OrchestratorToolkit } from "../mcp/toolkits/orchestrator/tools.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ThreadLaunchService from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ThreadSearch from "../orchestration-v2/ThreadSearch.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinkRequests from "../peer/PeerLinkRequests.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ManagedProjectFolders from "../project/ManagedProjectFolders.ts";
import * as ScheduledTasks from "../scheduledTasks/ScheduledTaskService.ts";
import * as Scheduler from "../scheduling/Scheduler.ts";
import * as SecretRequests from "../secrets/SecretRequests.ts";
import * as ServerSettings from "../serverSettings.ts";

/** Written out, not imported, so these tests also run (and fail) on code without the record. */
const FILE = "scheduled-task-mcp-profiles.json";

const PROJECT = ProjectId.make("project:wide");
const NARROW_PROJECT = ProjectId.make("project:narrow");
const CALLER = ThreadId.make("thread:caller");
const WIDE = ThreadId.make("thread:wide");
const INSTANCE = ProviderInstanceId.make("claudeAgent");
const NOW = DateTime.makeUnsafe("2026-11-20T00:00:00.000Z");

type Settings = Parameters<typeof ServerSettings.layerTest>[0];

/** CALLER is kept to `docs`; WIDE, beside it in PROJECT, gets all; NARROW_PROJECT's threads get `docs`. */
const NARROWED = {
  mcpProfiles: {
    docs: { servers: ["docs"], t3Tools: "all" },
    wider: { servers: ["docs", "github"], t3Tools: "all" },
  },
  mcpThreadProfiles: { [CALLER]: "docs" },
  projectSettingsFolded: true,
  projectSettingsOverrides: { [NARROW_PROJECT]: { mcpProfile: "docs" } },
};
/** The owner later gives NARROW_PROJECT's threads more than `docs`. */
const WIDENED = {
  ...NARROWED,
  projectSettingsOverrides: { [NARROW_PROJECT]: { mcpProfile: "wider" } },
};

const appThread = (id: ThreadId, projectId: ProjectId): OrchestrationV2AppThread =>
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
    lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: id },
    forkedFrom: null,
    createdAt: NOW,
    updatedAt: NOW,
    archivedAt: null,
    settledOverride: null,
    settledAt: null,
    lastVisitedAt: null,
    deletedAt: null,
  }) as unknown as OrchestrationV2AppThread;

const THREADS = [appThread(CALLER, PROJECT), appThread(WIDE, PROJECT)];
const shellOf = (thread: OrchestrationV2AppThread): OrchestrationV2ThreadShell =>
  ({
    ...thread,
    activeRunId: thread.id === CALLER ? "run:1" : null,
  }) as unknown as OrchestrationV2ThreadShell;
const findShell = (threadId: ThreadId) => {
  const thread = THREADS.find((candidate) => candidate.id === threadId);
  return Effect.succeed(thread === undefined ? null : shellOf(thread));
};

/** The thread an MCP call comes from; `null` is the owner's own client (no thread). */
const scopeOf = (threadId: ThreadId | null): McpInvocationContext.McpInvocationScope =>
  ({
    environmentId: EnvironmentId.make("environment-here"),
    requestNamespace: "thread:caller:session",
    thread:
      threadId === null
        ? undefined
        : { threadId, providerSessionId: "session", providerInstanceId: INSTANCE },
    client:
      threadId === null
        ? { sessionId: "session-1", label: "Owner", access: "full-access" }
        : undefined,
    issuedAt: 0,
    capabilities: new Set(["orchestration" as const]),
  }) as McpInvocationContext.McpInvocationScope;
const asCaller = <A, E, R>(effect: Effect.Effect<A, E, R>) =>
  Effect.provideService(effect, McpInvocationContext.McpInvocationContext, scopeOf(CALLER));

const task = (
  id: string,
  patch: Partial<ScheduledTaskUpsertInput> = {},
): ScheduledTaskUpsertInput => ({
  id: ScheduledTaskId.make(id),
  title: "Check in",
  prompt: "Do the work",
  enabled: true,
  schedule: { type: "interval", everyMs: 60_000 },
  projectId: NARROW_PROJECT,
  threadId: null,
  workspaceStrategy: { type: "root" },
  modelSelection: { instanceId: ProviderInstanceId.make("codex"), model: "gpt-5.4" },
  runtimeMode: "full-access",
  interactionMode: "default",
  ...patch,
});

/** A row an older server wrote (no record of who saved it), as a raw insert. */
const insertOldRow = (id: string, projectId: ProjectId, threadId: ThreadId | null = null) =>
  Effect.gen(function* () {
    const sql = yield* SqlClient.SqlClient;
    const now = "1970-01-01T00:00:00.000Z";
    yield* sql`INSERT INTO scheduled_tasks ${sql.insert({
      task_id: id,
      title: "Old task",
      prompt: "Old words",
      enabled: 1,
      schedule_json: '{"type":"interval","everyMs":60000}',
      project_id: projectId,
      thread_id: threadId,
      workspace_strategy_json: '{"type":"root"}',
      model_selection_json: '{"instanceId":"codex","model":"gpt-5.4"}',
      runtime_mode: "full-access",
      interaction_mode: "default",
      created_by: "agent",
      creation_source: "mcp",
      created_at: now,
      updated_at: now,
      next_run_at: "1970-01-01T00:01:00.000Z",
      last_run_at: null,
      last_run_status: "never",
      last_run_error: null,
      run_count: 0,
    })}`;
  });

interface World {
  /** Threads a run started. */
  readonly launched: Array<unknown>;
  /** Bound threads a run sent to. */
  readonly sent: Array<ThreadId>;
  /** The service's scheduled pass, as the scheduler would run it. */
  job: Effect.Effect<void> | undefined;
}

const newWorld = (): World => ({ launched: [], sent: [], job: undefined });

/** The service's own dependencies, with threads, launches and the scheduler recorded in `world`. */
const serviceDependencies = (settings: object, world: World) =>
  Layer.mergeAll(
    ServerSettings.layerTest({
      fallbackModelSelection: { instanceId: INSTANCE, model: "claude-opus" },
      ...settings,
    } as Settings),
    ManagedPolicy.layerTest(),
    Layer.mock(ThreadLaunchService.ThreadLaunchService)({
      launch: (input) =>
        Effect.sync(() => {
          world.launched.push(input);
          return {
            threadId: ThreadId.make(`thread:launched-${world.launched.length}`),
            resumed: false,
          } as never;
        }),
    }),
    Layer.mock(SecretRequests.SecretRequests)({}),
    Layer.mock(ManagedProjectFolders.ManagedProjectFolders)({
      namedProjectsRoot: "/projects",
      isScratchProject: () => Effect.succeed(false),
    }),
    NodeCrypto.layer,
    Layer.succeed(Scheduler.Scheduler, {
      register: <E, R>(name: string, effect: Effect.Effect<void, E, R>) =>
        Effect.sync(() => {
          // The service registers its pass with every service it needs already provided.
          // @effect-diagnostics-next-line unsafeEffectTypeAssertion:off -- R is never here.
          if (name === "scheduled-tasks") world.job = Effect.orDie(effect) as Effect.Effect<void>;
        }),
    }),
  );

const threadManagement = (world: World) =>
  Layer.mock(ThreadManagement.ThreadManagementService)({
    getThreadShell: findShell,
    getProjectThreadRecords: ({ threadId }) =>
      Effect.succeed({ thread: THREADS.find((candidate) => candidate.id === threadId)! } as never),
    getThreadRecords: () => Effect.succeed({ runs: [] } as never),
    sendToThread: (input) =>
      Effect.sync(() => {
        world.sent.push(input.threadId);
        return {} as never;
      }),
  });

/**
 * Starts the service (a server start) over the task table and state folder the test holds, runs
 * `body` with it, and stops it. Called again on the same table and folder, it is a restart.
 */
const withService = <A, E, R>(
  settings: object,
  world: World,
  body: (service: ScheduledTasks.ScheduledTaskService["Service"]) => Effect.Effect<A, E, R>,
) =>
  Effect.scoped(
    Effect.gen(function* () {
      const context = yield* Layer.build(
        ScheduledTasks.layer.pipe(
          Layer.provide(
            Layer.mergeAll(serviceDependencies(settings, world), threadManagement(world)),
          ),
        ),
      );
      return yield* body(yield* ScheduledTasks.ScheduledTaskService.pipe(Effect.provide(context)));
    }),
  );

/** One task table and one state folder, kept across the restarts a test makes. */
const held = <A, E, R>(body: Effect.Effect<A, E, R>) =>
  body.pipe(
    Effect.provide(
      Layer.mergeAll(
        SqlitePersistence.layerMemory,
        ServerConfig.layerTest(process.cwd(), { prefix: "t3-scheduled-task-mcp-profile-" }),
      ).pipe(Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))),
    ),
  );

const recordPath = Effect.gen(function* () {
  const config = yield* ServerConfig.ServerConfig;
  const path = yield* Path.Path;
  return path.join(config.stateDir, FILE);
});
const readRecord = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const file = yield* recordPath;
  if (!(yield* fs.exists(file))) return undefined;
  return JSON.parse(yield* fs.readFileString(file)) as {
    readonly version: number;
    readonly tasks: Record<
      string,
      { readonly threadId: string | null; readonly profile: { readonly name: string } | null }
    >;
  };
});
const writeRecord = (text: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    yield* fs.writeFileString(yield* recordPath, text);
  });

const row = (id: string) =>
  Effect.gen(function* () {
    const sql = yield* SqlClient.SqlClient;
    const [found] = yield* sql<{
      prompt: string;
      thread_id: string | null;
      last_run_status: string;
      last_run_error: string | null;
      next_run_at: string | null;
    }>`SELECT prompt, thread_id, last_run_status, last_run_error, next_run_at FROM scheduled_tasks WHERE task_id = ${id}`;
    return found;
  });

/** Moves the clock past every task's next run and runs the scheduler's pass once. */
const scheduledPass = (world: World) =>
  Effect.gen(function* () {
    yield* TestClock.adjust("2 minutes");
    yield* world.job!;
  });

const messageOf = (error: unknown) => (error as { readonly message: string }).message;

it.effect("a narrowed thread's save of a task whose runs start a wider thread is refused", () =>
  held(
    Effect.gen(function* () {
      const world = newWorld();
      yield* withService(NARROWED, world, (service) =>
        Effect.gen(function* () {
          // Its runs start a thread in PROJECT, on all. Runs again: each retry is refused too.
          for (let attempt = 0; attempt < 3; attempt++) {
            const refused = yield* asCaller(
              service.upsert(task("task:wide", { projectId: PROJECT })),
            ).pipe(Effect.flip);
            expect(messageOf(refused)).toContain(
              'This thread\'s MCP profile is "docs", and a thread this task\'s runs start in its project would get "all"',
            );
          }
          expect((yield* service.list()).tasks).toHaveLength(0);
          expect((yield* readRecord)?.tasks["task:wide"]).toBeUndefined();
          // Within its own profile: saved, and who saved it recorded.
          yield* asCaller(service.upsert(task("task:docs")));
          expect((yield* readRecord)?.tasks["task:docs"]).toMatchObject({
            threadId: CALLER,
            profile: { name: "docs" },
          });
          // The owner saves anything.
          yield* service.upsert(task("task:owner", { projectId: PROJECT }));
          expect((yield* readRecord)?.tasks["task:owner"]).toMatchObject({
            threadId: null,
            profile: null,
          });
        }),
      );
    }),
  ),
);

it.effect("a narrowed thread cannot change what a task bound to a wider thread runs", () =>
  held(
    Effect.gen(function* () {
      const world = newWorld();
      yield* withService(NARROWED, world, (service) =>
        Effect.gen(function* () {
          const bound = task("task:bound-wide", {
            projectId: PROJECT,
            threadId: WIDE,
            threadMode: "continue",
          });
          yield* service.upsert(bound);
          for (const change of [
            { prompt: "Other words" },
            { threadId: CALLER },
            { threadId: null, threadMode: "new_thread" as const },
          ]) {
            const refused = yield* asCaller(service.upsert({ ...bound, ...change })).pipe(
              Effect.flip,
            );
            expect(messageOf(refused)).toContain(
              `this task runs in thread ${WIDE}, whose MCP profile "all" keeps MCP servers or T3 tools "docs" does not`,
            );
          }
          // Reading it, renaming it, pausing it or changing its schedule stays allowed.
          const listed = yield* asCaller(service.list());
          expect(listed.tasks.map((stored) => stored.prompt)).toEqual(["Do the work"]);
          yield* asCaller(
            service.upsert({
              ...bound,
              title: "Renamed",
              enabled: false,
              schedule: { type: "interval", everyMs: 120_000 },
            }),
          );
          expect(yield* row("task:bound-wide")).toMatchObject({
            prompt: "Do the work",
            thread_id: WIDE,
          });
          // Its own thread: it may change what it runs itself.
          const own = task("task:bound-caller", {
            projectId: PROJECT,
            threadId: CALLER,
            threadMode: "continue",
          });
          yield* asCaller(service.upsert(own));
          yield* asCaller(service.upsert({ ...own, prompt: "My own words" }));
          expect((yield* row("task:bound-caller"))?.prompt).toBe("My own words");
        }),
      );
    }),
  ),
);

it.effect("a scheduled run is refused when its new thread would get more than the saver had", () =>
  held(
    Effect.gen(function* () {
      const world = newWorld();
      // Saved by the narrowed thread while NARROW_PROJECT's threads get `docs`: allowed.
      yield* withService(NARROWED, world, (service) =>
        asCaller(service.upsert(task("task:narrow"))),
      );
      // The owner then gives that project's threads more, and the server starts again.
      yield* withService(WIDENED, world, (service) =>
        Effect.gen(function* () {
          // Runs again: each scheduled pass is refused and recorded like a refused run.
          for (let pass = 0; pass < 2; pass++) {
            const before = yield* row("task:narrow");
            yield* scheduledPass(world);
            const after = yield* row("task:narrow");
            expect(after?.last_run_status).toBe("failed");
            expect(after?.last_run_error).toContain(
              'Not run: this task was saved by a thread whose MCP profile is "docs", and the thread this run would start in its project gets "wider"',
            );
            // Moved on to its next time, not left due.
            expect(after!.next_run_at! > before!.next_run_at!).toBe(true);
          }
          const now = yield* service
            .runNow({ id: ScheduledTaskId.make("task:narrow") })
            .pipe(Effect.flip);
          expect(messageOf(now)).toContain("Not run: this task was saved by a thread");
          expect(world.launched).toHaveLength(0);
          // The owner saves it again: it is the owner's now, and runs.
          yield* service.upsert(task("task:narrow"));
          yield* service.runNow({ id: ScheduledTaskId.make("task:narrow") });
          expect(world.launched).toHaveLength(1);
        }),
      );
    }),
  ),
);

it.effect(
  "a task with no record (old, or no file) runs as the owner's until a thread saves it",
  () =>
    held(
      Effect.gen(function* () {
        const world = newWorld();
        yield* insertOldRow("task:old", NARROW_PROJECT);
        yield* insertOldRow("task:old-wide", PROJECT);
        yield* withService(WIDENED, world, (service) =>
          Effect.gen(function* () {
            // No file at all: both run.
            expect(yield* readRecord).toBeUndefined();
            yield* scheduledPass(world);
            expect(world.launched).toHaveLength(2);
            // A file with other tasks in it: still no entry for these, still run.
            yield* service.upsert(task("task:other"));
            expect(Object.keys((yield* readRecord)!.tasks)).toEqual(["task:other"]);
            yield* scheduledPass(world);
            expect(world.launched).toHaveLength(5);
          }),
        );
        // Its first change by a narrowed thread records that thread's profile.
        yield* withService(NARROWED, world, (service) =>
          Effect.gen(function* () {
            const [old] = (yield* service.list()).tasks.filter(
              (stored) => stored.id === "task:old",
            );
            yield* asCaller(service.upsert({ ...old!, title: "Renamed" } as ScheduledTask));
            expect((yield* readRecord)?.tasks["task:old"]).toMatchObject({
              threadId: CALLER,
              profile: { name: "docs" },
            });
            // An old task in a wider project cannot be changed by it, and nothing is recorded.
            const [oldWide] = (yield* service.list()).tasks.filter(
              (stored) => stored.id === "task:old-wide",
            );
            yield* asCaller(
              service.upsert({ ...oldWide!, title: "Renamed" } as ScheduledTask),
            ).pipe(Effect.flip);
            expect((yield* readRecord)?.tasks["task:old-wide"]).toBeUndefined();
          }),
        );
        yield* withService(WIDENED, world, () =>
          Effect.gen(function* () {
            const launched = world.launched.length;
            yield* scheduledPass(world);
            // `task:old` is now the narrowed thread's and refused; the rest still run.
            expect((yield* row("task:old"))?.last_run_error).toContain('MCP profile is "docs"');
            expect(world.launched).toHaveLength(launched + 2);
          }),
        );
      }),
    ),
);

it.effect(
  "a row changed after its record (a save stopped half-way) is refused until saved again",
  () =>
    held(
      Effect.gen(function* () {
        const world = newWorld();
        yield* withService(NARROWED, world, (service) =>
          Effect.gen(function* () {
            // Saved by the owner, so recorded as the owner's; then the row changes with no save
            // recording it (a save that wrote its record and stopped, or a write the record misses).
            yield* service.upsert(task("task:changed"));
            const sql = yield* SqlClient.SqlClient;
            yield* sql`UPDATE scheduled_tasks SET prompt = 'Words nobody recorded' WHERE task_id = 'task:changed'`;
            const refused = yield* service
              .runNow({ id: ScheduledTaskId.make("task:changed") })
              .pipe(Effect.flip);
            expect(messageOf(refused)).toContain(
              "Not run: this task changed after the MCP profile it was saved with was recorded",
            );
            yield* scheduledPass(world);
            expect((yield* row("task:changed"))?.last_run_error).toContain(
              "this task changed after the MCP profile",
            );
            expect(world.launched).toHaveLength(0);
            // Saved again (by the owner): runs.
            yield* service.upsert(task("task:changed", { prompt: "Words nobody recorded" }));
            yield* service.runNow({ id: ScheduledTaskId.make("task:changed") });
            expect(world.launched).toHaveLength(1);
          }),
        );
      }),
    ),
);

it.effect("a record that cannot be read refuses saves and new-thread runs, never the owner's", () =>
  held(
    Effect.gen(function* () {
      const world = newWorld();
      yield* insertOldRow("task:unbound", PROJECT);
      yield* insertOldRow("task:bound", PROJECT, WIDE);
      for (const text of [
        "{ not json",
        '{"version":2,"tasks":{}}',
        '{"version":1,"tasks":{},"extra":true}',
        '{"version":1,"tasks":{"task:unbound":{"digest":"x","threadId":null,"profile":{"name":"docs","servers":"docs"},"recordedAt":"now"}}}',
      ]) {
        yield* writeRecord(text);
        yield* withService(NARROWED, world, (service) =>
          Effect.gen(function* () {
            for (const save of [
              service.upsert(task("task:new")),
              asCaller(service.upsert(task("task:new"))),
            ]) {
              const refused = yield* save.pipe(Effect.flip);
              expect(messageOf(refused)).toContain(
                `(${FILE} in the server's state folder) cannot be read`,
              );
            }
            const sent = world.sent.length;
            const launched = world.launched.length;
            yield* scheduledPass(world);
            expect((yield* row("task:unbound"))?.last_run_error).toContain(
              "Not run: The MCP profiles",
            );
            // A bound run goes into its thread, which has its own profile: not held up.
            expect(world.sent).toHaveLength(sent + 1);
            expect(world.launched).toHaveLength(launched);
            expect((yield* service.list()).tasks).toHaveLength(2);
          }),
        );
      }
    }),
  ),
);

it.effect("records of deleted tasks are dropped at every start, and an empty table drops all", () =>
  held(
    Effect.gen(function* () {
      const world = newWorld();
      // An empty table and no file: a start writes nothing.
      yield* withService(NARROWED, world, () => Effect.void);
      expect(yield* readRecord).toBeUndefined();
      yield* withService(NARROWED, world, (service) =>
        Effect.gen(function* () {
          yield* service.upsert(task("task:kept"));
          yield* asCaller(service.upsert(task("task:deleted")));
          yield* service.delete({ id: ScheduledTaskId.make("task:deleted") });
        }),
      );
      // A new task's save stopped after its record and before its row leaves an entry too.
      const record = (yield* readRecord)!;
      yield* writeRecord(
        JSON.stringify({
          ...record,
          tasks: { ...record.tasks, "task:never-saved": record.tasks["task:kept"] },
        }),
      );
      // Started twice: the first drops both, the second finds nothing to drop.
      for (let start = 0; start < 2; start++) {
        yield* withService(NARROWED, world, () => Effect.void);
        expect(Object.keys((yield* readRecord)!.tasks)).toEqual(["task:kept"]);
      }
      // Every task deleted: the record empties.
      yield* withService(NARROWED, world, (service) =>
        service.delete({ id: ScheduledTaskId.make("task:kept") }),
      );
      yield* withService(NARROWED, world, () => Effect.void);
      expect(yield* readRecord).toEqual({ version: 1, tasks: {} });
    }),
  ),
);

/**
 * Upstream's real `schedule_task` handler over the real service, with `OrchestratorMcpService`'s
 * change cut to what it does (read the task, then save it), and the owner unbinding the task in
 * between: the handler's own check saw it bound to the caller.
 */
it.effect("a change is checked on the service's own read, so a change in between is seen", () =>
  held(
    Effect.gen(function* () {
      const world = newWorld();
      const owner = scopeOf(null);
      const dependencies = Layer.mergeAll(
        ProjectionStore.layer,
        serviceDependencies(NARROWED, world),
        threadManagement(world),
        Layer.succeed(McpInvocationContext.McpInvocationContext, scopeOf(CALLER)),
        Layer.mock(ThreadMetadataMcpService.ThreadMetadataMcpService)({}),
        Layer.mock(PeerForwarding.PeerForwarding)({}),
        Layer.mock(PeerLinkRequests.PeerLinkRequests)({}),
        Layer.mock(PeerLinks.PeerLinks)({}),
        Layer.mock(RemoteDelegation.RemoteDelegation)({}),
        Layer.mock(ThreadSearch.ThreadSearch)({}),
      );
      const withTasks = Layer.provideMerge(ScheduledTasks.layer, dependencies);
      yield* Effect.scoped(
        Effect.gen(function* () {
          const base = yield* Layer.build(withTasks);
          const service = yield* ScheduledTasks.ScheduledTaskService.pipe(Effect.provide(base));
          const changes = Layer.mock(OrchestratorMcpService.OrchestratorMcpService)({
            updateScheduledTask: (_scope, input) =>
              Effect.gen(function* () {
                // The owner unbinds the task after the handler's check read it.
                const [before] = (yield* service.list()).tasks;
                yield* service
                  .upsert({ ...before!, threadId: null, threadMode: "new_thread" })
                  .pipe(Effect.provideService(McpInvocationContext.McpInvocationContext, owner));
                // What the service then does: read the task, and save the change on it.
                const [existing] = (yield* service.list()).tasks;
                const { task: saved } = yield* service.upsert({
                  ...existing!,
                  prompt: input.prompt ?? existing!.prompt,
                });
                return {
                  scheduledTaskId: saved.id,
                  title: saved.title,
                  prompt: saved.prompt,
                  enabled: saved.enabled,
                  projectId: saved.projectId,
                  boundThreadId: saved.threadId,
                  freshSession: false,
                  schedule: saved.schedule,
                  nextRunAt: saved.nextRunAt,
                  lastRunStatus: saved.lastRunStatus,
                };
              }).pipe(
                Effect.mapError(
                  (error) =>
                    new OrchestratorMcpFailure({
                      code: "orchestration_error",
                      message: `Could not update scheduled task: ${messageOf(error)}`,
                    }),
                ),
              ),
          });
          const context = yield* Layer.build(changes);
          const provided = Layer.succeedContext(base).pipe(
            Layer.merge(Layer.succeedContext(context)),
          );
          const projection = yield* ProjectionStore.ProjectionStoreV2.pipe(Effect.provide(base));
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
          // Bound to the caller: its own words may go in, so the handler's check lets it by.
          yield* asCaller(
            service.upsert(
              task("task:raced", { projectId: PROJECT, threadId: CALLER, threadMode: "continue" }),
            ),
          );
          const chunks = yield* orchestrator
            .handle("schedule_task", {
              scheduledTaskId: "task:raced",
              prompt: "Words for a wider thread",
            } as never)
            .pipe(Stream.unwrap, Stream.runCollect, Effect.provide(provided), Effect.orDie);
          const last = [...chunks].at(-1) as {
            readonly isFailure: boolean;
            readonly result: { readonly message?: string };
          };
          expect(last.isFailure).toBe(true);
          expect(last.result.message).toContain(
            'a thread this task\'s runs start in its project would get "all"',
          );
          expect(yield* row("task:raced")).toMatchObject({
            prompt: "Do the work",
            thread_id: null,
          });
        }),
      );
    }),
  ),
);
