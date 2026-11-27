// @effect-diagnostics nodeBuiltinImport:off - the service's record reads are made synchronous here.
/**
 * Fork: the MCP profile a scheduled task was saved with, through upstream's real service over a
 * real task table (SQLite in memory) and state folder: saves, scheduled runs, and missing, old,
 * half-written, corrupt and pruned records.
 */
import * as NodeFS from "node:fs";

import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, expect, it } from "@effect/vitest";
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
import * as Logger from "effect/Logger";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";
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
import * as AgentSlotRoutes from "./agentSlotRoutes.ts";
import * as ScheduledTaskMcpProfile from "./scheduledTaskMcpProfile.ts";
import { type SlotWorld, slotGateOver, slotWorld } from "./slotWorld.testkit.ts";

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

/** A row an older server wrote (no record of who saved it), as a raw insert; an agent's by default. */
const insertOldRow = (
  id: string,
  projectId: ProjectId,
  threadId: ThreadId | null = null,
  createdBy: "agent" | "user" = "agent",
) =>
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
      created_by: createdBy,
      creation_source: createdBy === "agent" ? "mcp" : "web",
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
  /** Every pass the service registered (the webhook worker, the slot notes' cleanup), by name. */
  readonly workers: Map<string, Effect.Effect<void>>;
  /** With this, the server's agent slot gate runs (one slot here), counting this world's threads. */
  readonly slots?: SlotWorld | undefined;
}

const newWorld = (slots?: SlotWorld): World => ({
  launched: [],
  sent: [],
  job: undefined,
  workers: new Map(),
  slots,
});

/** The instance scheduled runs start on (`task`), which the slot gate counts. */
const RUN_INSTANCE = ProviderInstanceId.make("codex");

/** The service's own dependencies, with threads, launches and the scheduler recorded in `world`. */
const serviceDependencies = (settings: object, world: World) => {
  const serverSettings = ServerSettings.layerTest({
    fallbackModelSelection: { instanceId: INSTANCE, model: "claude-opus" },
    ...(world.slots === undefined ? {} : { agentSlots: { enabled: true, maxPerMachine: 1 } }),
    ...settings,
  } as Settings);
  const gate =
    world.slots === undefined
      ? Layer.empty
      : slotGateOver(world.slots, RUN_INSTANCE).pipe(
          Layer.provide(Layer.mergeAll(serverSettings, NodeServices.layer)),
        );
  return Layer.mergeAll(
    serverSettings,
    gate,
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
        Effect.gen(function* () {
          const context = yield* Effect.context<R>();
          const work = effect.pipe(Effect.provideContext(context), Effect.orDie);
          world.workers.set(name, work);
          if (name === "scheduled-tasks") world.job = work;
        }),
    }),
  );
};

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
 * Whole-file reads at once, not on Node's thread pool: a forked run reads the record file, and the
 * tests give forked work a fixed number of turns (`settle`), which a thread-pool read on a loaded
 * machine can outlast. Read at once, a run takes the same number of turns on any machine.
 */
const readsAtOnce = Layer.effect(
  FileSystem.FileSystem,
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    return {
      ...fs,
      readFileString: (path: string, encoding?: string) =>
        Effect.suspend(() => {
          try {
            return Effect.succeed(
              NodeFS.readFileSync(path, { encoding: (encoding ?? "utf8") as BufferEncoding }),
            );
          } catch (cause) {
            // A missing file, as the real one reports it; any other failure from the real one.
            return (cause as NodeJS.ErrnoException).code === "ENOENT"
              ? Effect.fail(
                  PlatformError.systemError({
                    _tag: "NotFound",
                    module: "FileSystem",
                    method: "readFileString",
                    syscall: "open",
                    pathOrDescriptor: path,
                    cause,
                  }),
                )
              : fs.readFileString(path, encoding);
          }
        }),
    };
  }),
);

/**
 * Starts the service (a server start) over the task table and state folder the test holds, runs
 * `body` with it (and the settings service, which the owner changes with), and stops it. Called
 * again on the same table and folder, it is a restart.
 */
const withService = <A, E, R>(
  settings: object,
  world: World,
  body: (
    service: ScheduledTasks.ScheduledTaskService["Service"],
    serverSettings: ServerSettings.ServerSettingsService["Service"],
  ) => Effect.Effect<A, E, R>,
) =>
  Effect.scoped(
    Effect.gen(function* () {
      const context = yield* Layer.build(
        ScheduledTasks.layer.pipe(
          Layer.provideMerge(
            Layer.mergeAll(serviceDependencies(settings, world), threadManagement(world)),
          ),
          Layer.provide(readsAtOnce),
        ),
      );
      return yield* body(
        yield* ScheduledTasks.ScheduledTaskService.pipe(Effect.provide(context)),
        yield* ServerSettings.ServerSettingsService.pipe(Effect.provide(context)),
      );
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
      {
        readonly by: string;
        readonly threadId: string | null;
        readonly profile: { readonly name: string } | null;
      }
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
      project_id: string;
      thread_id: string | null;
      last_run_status: string;
      last_run_error: string | null;
      next_run_at: string | null;
      run_count: number;
    }>`SELECT prompt, project_id, thread_id, last_run_status, last_run_error, next_run_at, run_count FROM scheduled_tasks WHERE task_id = ${id}`;
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
              `this task runs in thread ${WIDE}, whose MCP profile "all" keeps MCP servers "docs" does not`,
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

/** What a save by the assistant recorded before the split: its three whole groups. */
const OLD_ASSISTANT = {
  name: "assistant",
  servers: [],
  t3Tools: ["orchestration", "threads", "projects"],
  builtinTools: "none",
};
/** NARROW_PROJECT's threads get the whole groups (the old assistant's), or the built-in assistant. */
const whole = (projectProfile: string) => ({
  ...NARROWED,
  mcpProfiles: {
    ...NARROWED.mcpProfiles,
    whole: { servers: [], t3Tools: ["orchestration", "threads", "projects"], builtinTools: "none" },
  },
  projectSettingsOverrides: { [NARROW_PROJECT]: { mcpProfile: projectProfile } },
});

it.effect("a task the assistant saved before the split reads as today's assistant (#980)", () =>
  held(
    Effect.gen(function* () {
      const world = newWorld();
      yield* withService(NARROWED, world, (service) =>
        asCaller(service.upsert(task("task:old-assistant"))),
      );
      // The record as a save by the assistant wrote it before the split.
      const record = yield* readRecord;
      const entry = record!.tasks["task:old-assistant"]!;
      yield* writeRecord(
        JSON.stringify({
          ...record,
          tasks: { ...record!.tasks, "task:old-assistant": { ...entry, profile: OLD_ASSISTANT } },
        }),
      );
      // Its runs would start threads with the whole groups: wider than the assistant now.
      yield* withService(whole("whole"), world, () =>
        Effect.gen(function* () {
          yield* scheduledPass(world);
          const after = yield* row("task:old-assistant");
          expect(after?.last_run_status).toBe("failed");
          expect(after?.last_run_error).toContain(
            'Not run: this task was saved by a thread whose MCP profile is "assistant", and the thread this run would start in its project gets "whole"',
          );
          expect(world.launched).toHaveLength(0);
        }),
      );
      // Threads there within today's assistant: the same record runs.
      yield* withService(whole("assistant"), world, () =>
        Effect.gen(function* () {
          yield* scheduledPass(world);
          expect(world.launched).toHaveLength(1);
        }),
      );
    }),
  ),
);

const AGENT_NO_RECORD = "Not run: an agent made this task, and no MCP profile is recorded for it";
const OWNER_SAVES = "The owner saves it once in the T3 app to allow it.";

const listed = (service: ScheduledTasks.ScheduledTaskService["Service"], id: string) =>
  service
    .list()
    .pipe(Effect.map(({ tasks }) => tasks.find((stored) => stored.id === id)! as ScheduledTask));

it.effect(
  "a task with no record (old, or the file lost) runs if the owner made it; an agent's waits for the owner's save",
  () =>
    held(
      Effect.gen(function* () {
        const world = newWorld();
        yield* insertOldRow("task:old", NARROW_PROJECT);
        yield* insertOldRow("task:old-wide", PROJECT);
        yield* insertOldRow("task:old-owner", PROJECT, null, "user");
        yield* withService(WIDENED, world, (service) =>
          Effect.gen(function* () {
            // No file at all. Runs again: each pass refuses the agent's two and runs the owner's.
            expect(yield* readRecord).toBeUndefined();
            for (let pass = 1; pass <= 2; pass++) {
              yield* scheduledPass(world);
              expect(world.launched).toHaveLength(pass);
              for (const id of ["task:old", "task:old-wide"]) {
                const refused = yield* row(id);
                expect(refused).toMatchObject({ last_run_status: "failed", run_count: 0 });
                expect(refused?.last_run_error).toContain(AGENT_NO_RECORD);
                expect(refused?.last_run_error).toContain(OWNER_SAVES);
              }
            }
            expect((yield* row("task:old-owner"))?.run_count).toBe(2);
            const now = yield* service
              .runNow({ id: ScheduledTaskId.make("task:old") })
              .pipe(Effect.flip);
            expect(messageOf(now)).toContain(AGENT_NO_RECORD);
            expect(world.launched).toHaveLength(2);
            // The owner saves it once (unchanged): recorded as the owner's, and it runs.
            yield* service.upsert(yield* listed(service, "task:old"));
            expect((yield* readRecord)?.tasks["task:old"]).toMatchObject({
              by: "owner",
              threadId: null,
              profile: null,
            });
            yield* scheduledPass(world);
            expect(yield* row("task:old")).toMatchObject({
              last_run_status: "succeeded",
              last_run_error: null,
              run_count: 1,
            });
            expect(world.launched).toHaveLength(4);
          }),
        );
        // A thread here saves a new task (an agent's, as every MCP save is), within its profile.
        yield* withService(NARROWED, world, (service) =>
          Effect.gen(function* () {
            yield* asCaller(service.upsert(task("task:agent", { createdBy: "agent" })));
            yield* service.upsert(task("task:owner-new"));
            expect((yield* readRecord)?.tasks["task:agent"]).toMatchObject({
              by: "thread",
              threadId: CALLER,
            });
          }),
        );
        // The file is lost; at the next start, an older copy of it is put back.
        const backup = (yield* readRecord)!;
        const fs = yield* FileSystem.FileSystem;
        yield* fs.remove(yield* recordPath);
        for (const restored of [false, true]) {
          if (restored) {
            yield* writeRecord(
              JSON.stringify({
                version: 1,
                tasks: { "task:owner-new": backup.tasks["task:owner-new"] },
              }),
            );
          }
          yield* withService(WIDENED, world, () =>
            Effect.gen(function* () {
              const launched = world.launched.length;
              yield* scheduledPass(world);
              // The agents' tasks wait for the owner again, the owner's save of `task:old`
              // included (it is lost with the file); the owner's two run.
              for (const id of ["task:old", "task:old-wide", "task:agent"]) {
                expect((yield* row(id))?.last_run_error).toContain(AGENT_NO_RECORD);
              }
              expect(world.launched).toHaveLength(launched + 2);
            }),
          );
        }
        // A narrowed thread's change of an old task records that thread's profile.
        yield* withService(NARROWED, world, (service) =>
          Effect.gen(function* () {
            const old = yield* listed(service, "task:old");
            yield* asCaller(service.upsert({ ...old, title: "Renamed" }));
            expect((yield* readRecord)?.tasks["task:old"]).toMatchObject({
              by: "thread",
              threadId: CALLER,
              profile: { name: "docs" },
            });
            // An old task in a wider project cannot be changed by it, and nothing is recorded.
            const oldWide = yield* listed(service, "task:old-wide");
            yield* asCaller(service.upsert({ ...oldWide, title: "Renamed" })).pipe(Effect.flip);
            expect((yield* readRecord)?.tasks["task:old-wide"]).toBeUndefined();
          }),
        );
        yield* withService(WIDENED, world, () =>
          Effect.gen(function* () {
            const launched = world.launched.length;
            yield* scheduledPass(world);
            // `task:old` is now the narrowed thread's and refused for that; the owner's two run.
            expect((yield* row("task:old"))?.last_run_error).toContain('MCP profile is "docs"');
            expect((yield* row("task:old-wide"))?.last_run_error).toContain(AGENT_NO_RECORD);
            expect(world.launched).toHaveLength(launched + 2);
          }),
        );
      }),
    ),
);

/** Every schedule a run can be due on (webhooks are tested below). */
const SCHEDULES = [
  { name: "interval", schedule: { type: "interval", everyMs: 60_000 } },
  { name: "once", schedule: { type: "once", runAt: "1970-01-01T00:01:00.000Z" } },
  { name: "fixed_time", schedule: { type: "fixed_time", timeOfDay: "00:01" } },
  // Some weekdays only (Monday and Wednesday): its next time skips days.
  {
    name: "fixed_time_weekdays",
    schedule: { type: "fixed_time", timeOfDay: "00:01", weekdays: [1, 3] },
  },
] as const;

/** Moves the clock to a second after the task's next run time and runs the scheduler's pass. */
const passAtNextRun = (world: World, id: string) =>
  Effect.gen(function* () {
    const next = (yield* row(id))?.next_run_at ?? null;
    if (next !== null) yield* TestClock.setTime(Date.parse(next) + 1_000);
    yield* world.job!;
  });

it.effect.each(SCHEDULES)(
  "$name: a refused run is a failed run moved to its next time, with one note; the owner's save runs it",
  ({ name, schedule }) =>
    held(
      Effect.gen(function* () {
        const world = newWorld();
        const id = `task:${name}`;
        yield* withService(NARROWED, world, (service) =>
          asCaller(service.upsert(task(id, { schedule, createdBy: "agent" }))),
        );
        yield* withService(WIDENED, world, (service) =>
          Effect.gen(function* () {
            const before = yield* row(id);
            yield* passAtNextRun(world, id);
            const refused = yield* row(id);
            expect(refused).toMatchObject({ last_run_status: "failed", run_count: 0 });
            expect(refused?.last_run_error).toContain(
              'Not run: this task was saved by a thread whose MCP profile is "docs"',
            );
            if (name === "once") {
              // No next time; the note says what the save that allows it needs.
              expect(refused?.next_run_at).toBeNull();
              expect(refused?.last_run_error).toContain(
                "If its time has passed, run it now or give it a new time after that save",
              );
            } else {
              expect(refused!.next_run_at! > before!.next_run_at!).toBe(true);
              expect(refused?.last_run_error).not.toContain("new time");
            }
            // Runs again: the next pass refuses the same way, one note, nothing counted.
            yield* TestClock.adjust("1 second");
            yield* passAtNextRun(world, id);
            const again = yield* row(id);
            expect(again).toMatchObject({
              last_run_status: "failed",
              last_run_error: refused?.last_run_error,
              run_count: 0,
            });
            expect(world.launched).toHaveLength(0);
            // The owner saves it in the app. A one-time task then has no time left, as its note
            // says, so the owner runs it now (or gives it a new time); the others come due.
            yield* service.upsert(yield* listed(service, id));
            if (name === "once") {
              expect((yield* row(id))?.next_run_at).toBeNull();
              yield* passAtNextRun(world, id);
              expect(world.launched).toHaveLength(0);
              yield* service.runNow({ id: ScheduledTaskId.make(id) });
            } else {
              yield* passAtNextRun(world, id);
            }
            expect(world.launched).toHaveLength(1);
            expect(yield* row(id)).toMatchObject({
              last_run_status: "succeeded",
              last_run_error: null,
              run_count: 1,
            });
          }),
        );
      }),
    ),
);

const ONCE_CASES = [
  {
    name: "the record file cannot be read",
    reason: "Not run: The MCP profiles",
    hint: "run it now or give it a new time once it is fixed",
    settings: NARROWED,
    breaks: writeRecord("{ not json"),
  },
  {
    name: "the new thread's MCP profile cannot be worked out",
    reason: "Not run: the MCP profile of the thread this run would start cannot be worked out",
    hint: "run it now or give it a new time once that is fixed",
    // The project names a profile the settings do not have.
    settings: {
      ...NARROWED,
      projectSettingsOverrides: { [NARROW_PROJECT]: { mcpProfile: "gone" } },
    },
    breaks: Effect.void,
  },
];

it.effect.each(ONCE_CASES)(
  "a one-time task refused because $name: the note says to run it now once that is fixed",
  ({ reason, hint, settings, breaks }) =>
    held(
      Effect.gen(function* () {
        const world = newWorld();
        const id = "task:once";
        const schedule = { type: "once", runAt: "1970-01-01T00:01:00.000Z" } as const;
        yield* withService(NARROWED, world, (service) =>
          asCaller(service.upsert(task(id, { schedule, createdBy: "agent" }))),
        );
        const saved = JSON.stringify(yield* readRecord);
        yield* breaks;
        yield* withService(settings, world, (service) =>
          Effect.gen(function* () {
            yield* passAtNextRun(world, id);
            const refused = yield* row(id);
            expect(refused).toMatchObject({ last_run_status: "failed", run_count: 0 });
            expect(refused?.next_run_at).toBeNull();
            expect(refused?.last_run_error).toContain(reason);
            expect(refused?.last_run_error).toContain(hint);
            // Runs again: it has no next time, so a later pass leaves it as it is.
            yield* TestClock.adjust("1 second");
            yield* passAtNextRun(world, id);
            expect(yield* row(id)).toMatchObject({
              last_run_error: refused?.last_run_error,
              run_count: 0,
            });
            // Run now is refused the same way while it is not fixed.
            const runNow = yield* service
              .runNow({ id: ScheduledTaskId.make(id) })
              .pipe(Effect.flip);
            expect(messageOf(runNow)).toContain(reason);
            expect(world.launched).toHaveLength(0);
          }),
        );
        // Fixed (the file restored, the setting put back): it still does not come due, as the
        // note says, and run now runs it.
        yield* writeRecord(saved);
        yield* withService(NARROWED, world, (service) =>
          Effect.gen(function* () {
            yield* passAtNextRun(world, id);
            expect(world.launched).toHaveLength(0);
            yield* service.runNow({ id: ScheduledTaskId.make(id) });
            expect(world.launched).toHaveLength(1);
            expect(yield* row(id)).toMatchObject({ last_run_status: "succeeded", run_count: 1 });
          }),
        );
      }),
    ),
);

it.effect("a run refused by the slot gate, then by its MCP profile: one note, the profile's", () =>
  held(
    Effect.gen(function* () {
      const slots = slotWorld([ThreadId.make("thread:busy")]);
      const world = newWorld(slots);
      yield* withService(NARROWED, world, (service, serverSettings) =>
        Effect.gen(function* () {
          yield* asCaller(service.upsert(task("task:gated", { createdBy: "agent" })));
          const due = (yield* row("task:gated"))!.next_run_at;
          yield* passAtNextRun(world, "task:gated");
          // The slot is full: it waits, still due, with the gate's note.
          expect(yield* row("task:gated")).toMatchObject({
            last_run_status: "never",
            next_run_at: due,
            run_count: 0,
          });
          expect((yield* row("task:gated"))?.last_run_error).toContain(
            AgentSlotRoutes.WAITING_NOTE_PREFIX,
          );
          // The owner widens the project while it waits: the next pass refuses it for that.
          yield* serverSettings.updateSettings({
            projectSettingsOverrides: { [NARROW_PROJECT]: { mcpProfile: "wider" } },
          } as never);
          yield* TestClock.adjust("5 seconds");
          yield* world.job!;
          const refused = yield* row("task:gated");
          expect(refused).toMatchObject({ last_run_status: "failed", run_count: 0 });
          expect(refused?.last_run_error).toContain(
            'Not run: this task was saved by a thread whose MCP profile is "docs"',
          );
          expect(refused?.last_run_error).not.toContain(AgentSlotRoutes.WAITING_NOTE_PREFIX);
          expect(refused!.next_run_at! > due!).toBe(true);
          // Runs again: the slot notes' cleanup keeps it, and with the slot free it is still
          // refused, the one note as it was.
          yield* world.workers.get("agent-slot-notes")!;
          slots.running = [];
          yield* passAtNextRun(world, "task:gated");
          expect(yield* row("task:gated")).toMatchObject({
            last_run_status: "failed",
            last_run_error: refused?.last_run_error,
            run_count: 0,
          });
          expect(world.launched).toHaveLength(0);
          // Refused for its profile while the slot is full: the profile's note, not the gate's.
          slots.running = [ThreadId.make("thread:busy")];
          yield* passAtNextRun(world, "task:gated");
          expect((yield* row("task:gated"))?.last_run_error).toBe(refused?.last_run_error);
          // The owner saves it; the slot frees: one run, the note gone.
          yield* service.upsert(yield* listed(service, "task:gated"));
          slots.running = [];
          yield* passAtNextRun(world, "task:gated");
          expect(world.launched).toHaveLength(1);
          expect(yield* row("task:gated")).toMatchObject({
            last_run_status: "succeeded",
            last_run_error: null,
          });
        }),
      );
    }),
  ),
);

const webhookRequest = (stored: ScheduledTask, body = "{}") => ({
  hookId: stored.id,
  token: stored.webhook?.path.split("/").at(-1) ?? "",
  method: "POST" as const,
  path: `/api/hooks/${stored.id}`,
  query: "",
  headers: { "content-type": "application/json" },
  body: new TextEncoder().encode(body),
  bodyText: body,
});

/** Lets forked work (a webhook delivery) run until it waits on the clock again. */
const settle = Effect.forEach(
  Array.from({ length: 20 }),
  () => Effect.yieldNow.pipe(Effect.andThen(Effect.promise(() => new Promise(setImmediate)))),
  { discard: true },
);

/**
 * Runs `step` (a settle by default) until `done` holds: forked work on a loaded machine can take
 * more than one settle, so a test waits for the state it needs instead of a fixed number of steps.
 */
const settleUntil = (
  done: Effect.Effect<boolean>,
  label: string,
  step: Effect.Effect<void> = settle,
) =>
  Effect.gen(function* () {
    for (let round = 0; round < 50; round++) {
      yield* step;
      if (yield* done) return;
    }
    assert.fail(`never: ${label}`);
  });

/** One pass of the stored-delivery worker, then one slot retry, `count` times. */
const workerRounds = (world: World, count: number) =>
  Effect.forEach(
    Array.from({ length: count }),
    () =>
      world.workers
        .get("webhook-coalescing")!
        .pipe(
          Effect.andThen(settle),
          Effect.andThen(TestClock.adjust(AgentSlotRoutes.WEBHOOK_SLOT_RETRY)),
          Effect.andThen(settle),
        ),
    { discard: true },
  );

const deliveryOf = (
  service: ScheduledTasks.ScheduledTaskService["Service"],
  id: string,
  deliveryId: string,
) =>
  service
    .getWebhookDelivery({ id: ScheduledTaskId.make(id), deliveryId } as never)
    .pipe(Effect.map(({ delivery }) => delivery));

/** The delivery once its run ended (refused, here). */
const endedDelivery = (
  service: ScheduledTasks.ScheduledTaskService["Service"],
  id: string,
  deliveryId: string,
) =>
  settleUntil(
    deliveryOf(service, id, deliveryId).pipe(
      Effect.map((delivery) => delivery.outcome !== "accepted"),
      Effect.orDie,
    ),
    `delivery ${deliveryId} ended`,
  ).pipe(Effect.andThen(deliveryOf(service, id, deliveryId)));

/** A stored delivery's worker run of `taskId` is in its slot wait (it logged so). */
const inSlotWait = (logs: ReadonlyArray<Logger.Options<unknown>>, taskId: string) =>
  Effect.sync(() =>
    logs.some((entry) => {
      const [text, fields] = entry.message as ReadonlyArray<unknown>;
      return (
        text === "Webhook delivery waits for an agent slot" &&
        (fields as { readonly taskId?: unknown } | undefined)?.taskId === taskId
      );
    }),
  );

/** Every log line, for `inSlotWait`. */
const logInto = (logs: Array<Logger.Options<unknown>>) =>
  Effect.provide(
    Logger.layer([Logger.make((entry) => logs.push(entry))], { mergeWithExisting: false }),
  );

it.effect("a webhook run is refused when it starts, and again after it waited for a slot", () => {
  const logs: Array<Logger.Options<unknown>> = [];
  return held(
    Effect.gen(function* () {
      const slots = slotWorld();
      const world = newWorld(slots);
      yield* withService(NARROWED, world, (service, serverSettings) =>
        Effect.gen(function* () {
          const hook = { schedule: { type: "webhook" } as const, createdBy: "agent" as const };
          const { task: direct } = yield* asCaller(service.upsert(task("task:hook", hook)));
          const { task: waiting } = yield* asCaller(service.upsert(task("task:hook-wait", hook)));
          // An agent's task with no record: its delivery is refused, nothing starts.
          const { task: unrecorded } = yield* service.upsert(task("task:hook-old", hook));
          const sql = yield* SqlClient.SqlClient;
          const record = (yield* readRecord)!;
          const { "task:hook-old": _dropped, ...kept } = record.tasks;
          yield* writeRecord(JSON.stringify({ version: 1, tasks: kept }));
          const old = yield* service.triggerWebhook(webhookRequest(unrecorded));
          if (old._tag !== "accepted") return assert.fail(`not accepted: ${old._tag}`);
          const oldDelivery = yield* endedDelivery(service, "task:hook-old", old.deliveryId);
          expect(oldDelivery.outcome).toBe("dispatch_failed");
          expect(oldDelivery.error).toContain(AGENT_NO_RECORD);
          // The slot is full: the other delivery is stored and waits.
          slots.running = [ThreadId.make("thread:busy")];
          const stored = yield* service.triggerWebhook(webhookRequest(waiting));
          if (stored._tag !== "accepted") return assert.fail(`not accepted: ${stored._tag}`);
          // The widening must land while the worker's run waits for a slot (the check this test
          // is about), so wait for that state, not for a number of rounds.
          yield* settleUntil(
            inSlotWait(logs, "task:hook-wait"),
            "the delivery waits in its slot wait",
            workerRounds(world, 1),
          );
          expect(world.launched).toHaveLength(0);
          expect((yield* deliveryOf(service, "task:hook-wait", stored.deliveryId)).outcome).toBe(
            "accepted",
          );
          // The owner widens the project while it waits, the slot still full. Its next retry
          // checks it again before it asks for the slot, and ends it refused.
          yield* serverSettings.updateSettings({
            projectSettingsOverrides: { [NARROW_PROJECT]: { mcpProfile: "wider" } },
          } as never);
          yield* settleUntil(
            deliveryOf(service, "task:hook-wait", stored.deliveryId).pipe(
              Effect.map((delivery) => delivery.outcome !== "accepted"),
              Effect.orDie,
            ),
            "the waiting delivery ended",
            workerRounds(world, 1),
          );
          // Then the slot frees. Runs again: more rounds start nothing, and the delivery is not
          // stored any more.
          slots.running = [];
          yield* workerRounds(world, 4);
          expect(world.launched).toHaveLength(0);
          const waited = yield* deliveryOf(service, "task:hook-wait", stored.deliveryId);
          expect(waited.outcome).toBe("dispatch_failed");
          expect(waited.error).toContain(
            'Not run: this task was saved by a thread whose MCP profile is "docs"',
          );
          expect(
            yield* sql`SELECT 1 FROM scheduled_task_webhook_pending WHERE task_id = 'task:hook-wait'`,
          ).toHaveLength(0);
          // A direct delivery after the widening is refused as it starts.
          const late = yield* service.triggerWebhook(webhookRequest(direct, '{"n":2}'));
          if (late._tag !== "accepted") return assert.fail(`not accepted: ${late._tag}`);
          const lateDelivery = yield* endedDelivery(service, "task:hook", late.deliveryId);
          expect(lateDelivery.outcome).toBe("dispatch_failed");
          expect(lateDelivery.error).toContain('MCP profile is "docs"');
          expect(world.launched).toHaveLength(0);
          // The owner saves it: the next delivery runs.
          yield* service.upsert(yield* listed(service, "task:hook"));
          const ran = yield* service.triggerWebhook(webhookRequest(direct, '{"n":3}'));
          if (ran._tag !== "accepted") return assert.fail(`not accepted: ${ran._tag}`);
          yield* settleUntil(
            Effect.sync(() => world.launched.length === 1),
            "the owner's task ran",
          );
        }),
      );
    }),
  ).pipe(logInto(logs));
});

it.effect(
  "a stored delivery refused for its profile as its worker starts it leaves its place in line (#911)",
  () =>
    held(
      Effect.gen(function* () {
        const slots = slotWorld();
        const world = newWorld(slots);
        yield* withService(NARROWED, world, (service, serverSettings) =>
          Effect.gen(function* () {
            const hook = { schedule: { type: "webhook" } as const, createdBy: "agent" as const };
            const { task: waiting } = yield* asCaller(service.upsert(task("task:hook-wait", hook)));
            const { task: owners } = yield* service.upsert(
              task("task:hook-owner", { schedule: { type: "webhook" } }),
            );
            const sql = yield* SqlClient.SqlClient;
            // The slot is full: the agent's delivery asks for it, is refused, and is stored with
            // its place in line from that ask.
            slots.running = [ThreadId.make("thread:busy")];
            const stored = yield* service.triggerWebhook(webhookRequest(waiting));
            if (stored._tag !== "accepted") return assert.fail(`not accepted: ${stored._tag}`);
            yield* settleUntil(
              sql`SELECT 1 FROM scheduled_task_webhook_pending WHERE task_id = 'task:hook-wait'`.pipe(
                Effect.map((rows) => rows.length === 1),
                Effect.orDie,
              ),
              "the delivery is stored",
            );
            // Before the worker starts it, the owner widens the project and the slot frees. The
            // worker's run is refused for its profile as it starts, never asking for the slot.
            yield* serverSettings.updateSettings({
              projectSettingsOverrides: { [NARROW_PROJECT]: { mcpProfile: "wider" } },
            } as never);
            slots.running = [];
            yield* settleUntil(
              deliveryOf(service, "task:hook-wait", stored.deliveryId).pipe(
                Effect.map((delivery) => delivery.outcome !== "accepted"),
                Effect.orDie,
              ),
              "the stored delivery ended",
              workerRounds(world, 1),
            );
            const ended = yield* deliveryOf(service, "task:hook-wait", stored.deliveryId);
            expect(ended.outcome).toBe("dispatch_failed");
            expect(ended.error).toContain('MCP profile is "docs"');
            // Its place left with it: the owner's delivery takes the free slot as it comes in,
            // not stored behind a delivery that already ended.
            const ran = yield* service.triggerWebhook(webhookRequest(owners));
            if (ran._tag !== "accepted") return assert.fail(`not accepted: ${ran._tag}`);
            yield* settleUntil(
              Effect.sync(() => world.launched.length === 1),
              "the owner's task ran",
            );
            expect(
              yield* sql`SELECT 1 FROM scheduled_task_webhook_pending WHERE task_id = 'task:hook-owner'`,
            ).toHaveLength(0);
          }),
        );
      }),
    ),
);

/** The waiting task's row, for a case that makes it unreadable and then puts it back. */
const setScheduleJson = (json: string) =>
  SqlClient.SqlClient.pipe(
    Effect.flatMap(
      (sql) =>
        sql`UPDATE scheduled_tasks SET schedule_json = ${json} WHERE task_id = 'task:hook-wait'`,
    ),
    Effect.asVoid,
  );

/**
 * How a stored delivery's worker run ends before it asks for a slot, the worker then removing its
 * row: skipped (paused, trigger changed) or failed (its task cannot be read as it starts: a row
 * already marked running skips the worker's own read, so the run's read is the one that fails).
 */
const STORED_EARLY_ENDS = [
  {
    name: "paused",
    end: (service: ScheduledTasks.ScheduledTaskService["Service"], stored: ScheduledTask) =>
      service.setEnabled({ id: stored.id, enabled: false } as never).pipe(Effect.asVoid),
    repair: Effect.void,
    error: "The task was paused before this delivery ran.",
  },
  {
    name: "trigger changed",
    end: (service: ScheduledTasks.ScheduledTaskService["Service"], stored: ScheduledTask) =>
      service
        .upsert({ ...stored, schedule: { type: "interval", everyMs: 3_600_000 } } as never)
        .pipe(Effect.asVoid),
    repair: Effect.void,
    error: "The task's trigger changed before this delivery ran.",
  },
  {
    name: "failed before its ask",
    end: () =>
      SqlClient.SqlClient.pipe(
        Effect.flatMap(
          (sql) =>
            sql`UPDATE scheduled_task_webhook_pending SET state = 'running' WHERE task_id = 'task:hook-wait'`,
        ),
        Effect.andThen(setScheduleJson("not json")),
      ),
    repair: setScheduleJson(JSON.stringify({ type: "webhook" })),
    error: "The run failed to start.",
  },
] as const;

it.effect.each(STORED_EARLY_ENDS)(
  "$name: a stored delivery its worker ends without a run leaves its place in line (#915)",
  ({ end, repair, error }) =>
    held(
      Effect.gen(function* () {
        const slots = slotWorld();
        const world = newWorld(slots);
        yield* withService(NARROWED, world, (service) =>
          Effect.gen(function* () {
            const hook = { schedule: { type: "webhook" } as const };
            const { task: waiting } = yield* service.upsert(task("task:hook-wait", hook));
            const { task: other } = yield* service.upsert(task("task:hook-other", hook));
            const sql = yield* SqlClient.SqlClient;
            // The slot is full: the delivery asks for it, is refused, and is stored with its place
            // in line from that ask.
            slots.running = [ThreadId.make("thread:busy")];
            const stored = yield* service.triggerWebhook(webhookRequest(waiting));
            if (stored._tag !== "accepted") return assert.fail(`not accepted: ${stored._tag}`);
            yield* settleUntil(
              sql`SELECT 1 FROM scheduled_task_webhook_pending WHERE task_id = 'task:hook-wait'`.pipe(
                Effect.map((rows) => rows.length === 1),
                Effect.orDie,
              ),
              "the delivery is stored",
            );
            // Before its worker takes it, it comes to an end there, and the slot frees.
            yield* end(service, waiting);
            slots.running = [];
            yield* settleUntil(
              sql`SELECT 1 FROM scheduled_task_webhook_pending WHERE task_id = 'task:hook-wait'`.pipe(
                Effect.map((rows) => rows.length === 0),
                Effect.orDie,
              ),
              "the worker removed the stored delivery",
              world.workers.get("webhook-coalescing")!.pipe(Effect.andThen(settle)),
            );
            yield* repair;
            const [ended] = yield* sql<{ outcome: string; error: string | null }>`
              SELECT outcome, error FROM scheduled_task_webhook_deliveries
              WHERE delivery_id = ${stored.deliveryId}
            `;
            expect(ended?.outcome).toBe("dispatch_failed");
            expect(ended?.error).toContain(error);
            expect(world.launched).toHaveLength(0);
            // Its place left with its row: another task's delivery takes the free slot as it
            // comes in, not stored behind a delivery that already ended.
            const ran = yield* service.triggerWebhook(webhookRequest(other));
            if (ran._tag !== "accepted") return assert.fail(`not accepted: ${ran._tag}`);
            yield* settleUntil(
              Effect.sync(() => world.launched.length === 1),
              "the other task's delivery ran",
            );
            expect(
              yield* sql`SELECT 1 FROM scheduled_task_webhook_pending WHERE task_id = 'task:hook-other'`,
            ).toHaveLength(0);
          }),
        );
      }),
    ),
);

it.effect(
  "a webhook delivery saved while it waited runs once on the task as saved, checked on it (#719)",
  () => {
    const logs: Array<Logger.Options<unknown>> = [];
    return held(
      Effect.gen(function* () {
        const slots = slotWorld();
        const world = newWorld(slots);
        yield* withService(NARROWED, world, (service) =>
          Effect.gen(function* () {
            // The owner's webhook task; the slot is full, so its delivery is stored and waits.
            const { task: hook } = yield* service.upsert(
              task("task:hook-saved", { schedule: { type: "webhook" } }),
            );
            slots.running = [ThreadId.make("thread:busy")];
            const stored = yield* service.triggerWebhook(webhookRequest(hook));
            if (stored._tag !== "accepted") return assert.fail(`not accepted: ${stored._tag}`);
            // The save must land after the worker took its copy (it takes the prompt saved at
            // that moment), so wait for that state, not for a number of rounds.
            yield* settleUntil(
              inSlotWait(logs, "task:hook-saved"),
              "the delivery waits in its slot wait",
              workerRounds(world, 1),
            );
            expect(world.launched).toHaveLength(0);
            expect(
              (yield* deliveryOf(service, "task:hook-saved", stored.deliveryId)).renderedPrompt,
            ).toContain("Do the work");
            // The owner saves a new prompt while it waits: recorded, so the task is fine to run.
            yield* service.upsert({
              ...(yield* listed(service, "task:hook-saved")),
              prompt: "New words",
            });
            // The run it held was rendered from the old prompt: not started, and not dropped. The
            // delivery stays stored and is read again, so the profile check is on the task as
            // saved (its record matches), not on the old copy.
            yield* workerRounds(world, 3);
            expect(world.launched).toHaveLength(0);
            const waiting = yield* deliveryOf(service, "task:hook-saved", stored.deliveryId);
            expect(waiting.outcome).toBe("accepted");
            expect(waiting.renderedPrompt).toContain("New words");
            const sql = yield* SqlClient.SqlClient;
            expect(
              yield* sql`SELECT state FROM scheduled_task_webhook_pending WHERE task_id = 'task:hook-saved'`,
            ).toEqual([{ state: "dispatching" }]);
            // The slot frees: it runs once, with the new prompt.
            slots.running = [];
            yield* settleUntil(
              Effect.sync(() => world.launched.length === 1),
              "the waiting delivery ran",
              workerRounds(world, 1),
            );
            expect(JSON.stringify(world.launched[0])).toContain("New words");
            expect(JSON.stringify(world.launched[0])).not.toContain("Do the work");
            yield* workerRounds(world, 3);
            expect(world.launched).toHaveLength(1);
            const ran = yield* deliveryOf(service, "task:hook-saved", stored.deliveryId);
            expect(ran.outcome).toBe("accepted");
            expect(
              yield* sql`SELECT 1 FROM scheduled_task_webhook_pending WHERE task_id = 'task:hook-saved'`,
            ).toHaveLength(0);
          }),
        );
      }),
    ).pipe(logInto(logs));
  },
);

const STALE_COPY =
  "Not run: this task was saved again after this run read it, and the run would have used the task as it was before, so it was not started.";

it.effect(
  "a delivery handed back after a save while it waits is refused on the task as saved (#750)",
  () =>
    held(
      Effect.gen(function* () {
        const slots = slotWorld();
        const world = newWorld(slots);
        yield* withService(NARROWED, world, (service, serverSettings) =>
          Effect.gen(function* () {
            const sql = yield* SqlClient.SqlClient;
            // The narrowed thread's webhook task; the slot is full, so its delivery waits.
            const { task: hook } = yield* asCaller(
              service.upsert(
                task("task:hook-back", { schedule: { type: "webhook" }, createdBy: "agent" }),
              ),
            );
            slots.running = [ThreadId.make("thread:busy")];
            const stored = yield* service.triggerWebhook(webhookRequest(hook));
            if (stored._tag !== "accepted") return assert.fail(`not accepted: ${stored._tag}`);
            yield* workerRounds(world, 3);
            expect(world.launched).toHaveLength(0);
            // The thread saves a new prompt while it waits (within its profile, so recorded):
            // the delivery is handed back and read again, still waiting.
            yield* asCaller(
              service.upsert({
                ...(yield* listed(service, "task:hook-back")),
                prompt: "New words",
              }),
            );
            yield* workerRounds(world, 3);
            expect(world.launched).toHaveLength(0);
            const waiting = yield* deliveryOf(service, "task:hook-back", stored.deliveryId);
            expect(waiting.outcome).toBe("accepted");
            expect(waiting.renderedPrompt).toContain("New words");
            // Then the owner widens the project before it runs: the next retry checks the task as
            // saved against the thread's record and refuses it, as a profile refusal (not a stale
            // copy), and nothing starts once the slot frees.
            yield* serverSettings.updateSettings({
              projectSettingsOverrides: { [NARROW_PROJECT]: { mcpProfile: "wider" } },
            } as never);
            yield* settleUntil(
              deliveryOf(service, "task:hook-back", stored.deliveryId).pipe(
                Effect.map((delivery) => delivery.outcome !== "accepted"),
                Effect.orDie,
              ),
              "the waiting delivery ended",
              workerRounds(world, 1),
            );
            slots.running = [];
            yield* workerRounds(world, 4);
            expect(world.launched).toHaveLength(0);
            const refused = yield* deliveryOf(service, "task:hook-back", stored.deliveryId);
            expect(refused.outcome).toBe("dispatch_failed");
            expect(refused.error).toContain(
              'Not run: this task was saved by a thread whose MCP profile is "docs"',
            );
            expect(refused.error).not.toContain(STALE_COPY);
            expect(
              yield* sql`SELECT 1 FROM scheduled_task_webhook_pending WHERE task_id = 'task:hook-back'`,
            ).toHaveLength(0);
          }),
        );
      }),
    ),
);

/**
 * A run's copy of the task that a save overtook (a scheduled run or "run now" whose read a save
 * beat, the way a waiting webhook delivery's is): the record's check, on that copy, as it starts.
 */
it.effect("a run's copy that a save overtook says so, with the one-time task's way on", () =>
  held(
    Effect.gen(function* () {
      const world = newWorld();
      const profiles = yield* withService(NARROWED, world, (service) =>
        Effect.gen(function* () {
          const once = { schedule: { type: "once", runAt: "1970-01-01T00:01:00.000Z" } } as const;
          const { task: oneTime } = yield* service.upsert(task("task:once", once));
          const { task: repeating } = yield* service.upsert(task("task:every"));
          return [oneTime, repeating] as const;
        }),
      );
      const [oneTime, repeating] = profiles;
      const check = yield* ScheduledTaskMcpProfile.makeScheduledTaskMcpProfiles(
        ThreadManagement.ThreadManagementService.of({ getThreadShell: findShell } as never),
      ).pipe(Effect.provide(ServerSettings.layerTest(NARROWED as Settings)));
      // Each copy as the run read it, before the owner's save wrote the row and its record.
      const before = (stored: ScheduledTask) => ({ ...stored, prompt: "Words from before" });
      const onceReason = yield* check.runRefusal(before(oneTime));
      expect(onceReason).toContain(STALE_COPY);
      expect(onceReason).toContain("run it now or give it a new time");
      expect(onceReason).not.toContain("The next run");
      const everyReason = yield* check.runRefusal(before(repeating));
      expect(everyReason).toContain(STALE_COPY);
      expect(everyReason).toContain("The next run, or the next webhook delivery");
      expect(everyReason).not.toContain("waited");
      // A copy of a task that was deleted and made again under its id is not "saved again": the
      // writer of this row is not known.
      const remade = yield* check.runRefusal({
        ...before(repeating),
        createdAt: "2026-01-01T00:00:00.000Z",
      });
      expect(remade).toContain("Save the task again");
      expect(remade).not.toContain(STALE_COPY);
    }),
  ),
);

/** A call from another machine's thread, which says no MCP profile for it. */
const linkedScope = {
  environmentId: EnvironmentId.make("environment-here"),
  requestNamespace: "linked:session",
  client: { sessionId: "linked-1", label: "Laptop", access: "full-access", linked: true },
  issuedAt: 0,
  capabilities: new Set(["orchestration" as const]),
} as unknown as McpInvocationContext.McpInvocationScope;

it.effect(
  "a save from another machine is not the owner's: its runs wait for the owner's save",
  () =>
    held(
      Effect.gen(function* () {
        const world = newWorld();
        yield* withService(NARROWED, world, (service) =>
          Effect.gen(function* () {
            const fromLinked = (input: ScheduledTaskUpsertInput) =>
              service
                .upsert(input)
                .pipe(
                  Effect.provideService(McpInvocationContext.McpInvocationContext, linkedScope),
                );
            yield* fromLinked(task("task:linked", { projectId: PROJECT, createdBy: "agent" }));
            expect((yield* readRecord)?.tasks["task:linked"]).toEqual(
              expect.objectContaining({ by: "linked", threadId: null, profile: null }),
            );
            // Runs again: run now and each pass are refused, and say what lets it run.
            for (let attempt = 0; attempt < 2; attempt++) {
              const now = yield* service
                .runNow({ id: ScheduledTaskId.make("task:linked") })
                .pipe(Effect.flip);
              expect(messageOf(now)).toContain("Not run: this task was saved from another machine");
              expect(messageOf(now)).toContain(OWNER_SAVES);
              yield* scheduledPass(world);
            }
            expect((yield* row("task:linked"))?.last_run_error).toContain(
              "saved from another machine",
            );
            expect(world.launched).toHaveLength(0);
            // A bound task it saves runs in its thread, which has its own profile: not held up.
            yield* fromLinked(
              task("task:linked-bound", {
                projectId: PROJECT,
                threadId: WIDE,
                threadMode: "continue",
              }),
            );
            yield* service.runNow({ id: ScheduledTaskId.make("task:linked-bound") });
            expect(world.sent).toEqual([WIDE]);
            // The owner saves it: it runs.
            yield* service.upsert(yield* listed(service, "task:linked"));
            yield* service.runNow({ id: ScheduledTaskId.make("task:linked") });
            expect(world.launched).toHaveLength(1);
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
        '{"version":1,"tasks":{"task:unbound":{"digest":"x","by":"thread","threadId":"thread:caller","profile":{"name":"docs","servers":"docs"},"recordedAt":"now"}}}',
        // Who saved it: missing (an entry from before `by`), unknown, or not matching its fields.
        '{"version":1,"tasks":{"task:unbound":{"digest":"x","threadId":null,"profile":null,"recordedAt":"now"}}}',
        '{"version":1,"tasks":{"task:unbound":{"digest":"x","by":"someone","threadId":null,"profile":null,"recordedAt":"now"}}}',
        '{"version":1,"tasks":{"task:unbound":{"digest":"x","by":"thread","threadId":null,"profile":null,"recordedAt":"now"}}}',
        '{"version":1,"tasks":{"task:unbound":{"digest":"x","by":"owner","threadId":"thread:caller","profile":null,"recordedAt":"now"}}}',
        '{"version":1,"tasks":{"task:unbound":{"digest":"x","by":"linked","threadId":null,"profile":{"name":"docs","servers":["docs"],"t3Tools":"all"},"recordedAt":"now"}}}',
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

it.effect("a record that cannot be read is logged once while it stays so, not at every run", () => {
  const logs: Array<Logger.Options<unknown>> = [];
  const warnings = () =>
    logs.filter(
      (entry) =>
        entry.logLevel === "Warn" &&
        /scheduled-task-mcp-profiles\.json in the server's state folder\) cannot be read/.test(
          Logger.formatLogFmt.log(entry),
        ),
    ).length;
  return held(
    Effect.gen(function* () {
      const world = newWorld();
      yield* insertOldRow("task:owner-old", PROJECT, null, "user");
      yield* withService(NARROWED, world, (service) =>
        Effect.gen(function* () {
          // Readable (no file): the owner's task runs, nothing is logged.
          yield* scheduledPass(world);
          expect(world.launched).toHaveLength(1);
          // Broken while the server runs: each pass refuses the run, the file is logged once.
          yield* writeRecord("{ not json");
          for (let pass = 0; pass < 3; pass++) yield* scheduledPass(world);
          expect(world.launched).toHaveLength(1);
          expect((yield* row("task:owner-old"))?.last_run_error).toContain(
            "Not run: The MCP profiles",
          );
          expect(warnings()).toBe(1);
          // Removed, the way out the warning names: the task runs again.
          const fs = yield* FileSystem.FileSystem;
          yield* fs.remove(yield* recordPath);
          yield* scheduledPass(world);
          expect(world.launched).toHaveLength(2);
          // Broken again: logged again, once.
          yield* writeRecord("{ not json");
          for (let pass = 0; pass < 2; pass++) yield* scheduledPass(world);
          expect(warnings()).toBe(2);
          // Fixed, and only a save reads it: that good read counts too, so the same breakage
          // after it is logged again.
          yield* fs.remove(yield* recordPath);
          yield* service.upsert(task("task:saved-meanwhile"));
          yield* writeRecord("{ not json");
          yield* scheduledPass(world);
          expect(warnings()).toBe(3);
          expect(world.launched).toHaveLength(2);
        }),
      );
      // A start on the broken file: its clean-up and its runs log it once between them.
      yield* withService(NARROWED, world, () =>
        Effect.gen(function* () {
          for (let pass = 0; pass < 2; pass++) yield* scheduledPass(world);
          expect(warnings()).toBe(4);
          expect(world.launched).toHaveLength(2);
        }),
      );
    }),
  ).pipe(
    Effect.provide(
      Logger.layer([Logger.make((entry) => logs.push(entry))], { mergeWithExisting: false }),
    ),
  );
});

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

/** The last chunk of an MCP tool call's result. */
interface ToolResult {
  readonly isFailure: boolean;
  readonly result: { readonly message?: string };
}

/**
 * Upstream's real `schedule_task` handler over the real service, called by the narrowed thread,
 * with `OrchestratorMcpService`'s change made by `change` (given the service).
 */
const withScheduleTaskHandler = <A, E, R>(
  world: World,
  change: (
    service: ScheduledTasks.ScheduledTaskService["Service"],
  ) => OrchestratorMcpService.OrchestratorMcpService["Service"]["updateScheduledTask"],
  body: (
    service: ScheduledTasks.ScheduledTaskService["Service"],
    scheduleTask: (input: object) => Effect.Effect<ToolResult>,
  ) => Effect.Effect<A, E, R>,
) =>
  Effect.scoped(
    Effect.gen(function* () {
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
      const base = yield* Layer.build(Layer.provideMerge(ScheduledTasks.layer, dependencies));
      const service = yield* ScheduledTasks.ScheduledTaskService.pipe(Effect.provide(base));
      const context = yield* Layer.build(
        Layer.mock(OrchestratorMcpService.OrchestratorMcpService)({
          updateScheduledTask: change(service),
        }),
      );
      const provided = Layer.succeedContext(base).pipe(Layer.merge(Layer.succeedContext(context)));
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
      const scheduleTask = (input: object) =>
        orchestrator.handle("schedule_task", input as never).pipe(
          Stream.unwrap,
          Stream.runCollect,
          Effect.provide(provided),
          Effect.orDie,
          Effect.map((chunks) => [...chunks].at(-1) as ToolResult),
        );
      return yield* body(service, scheduleTask);
    }),
  );

/**
 * The service's change cut to what it does (read the task, then save it), with the owner
 * unbinding the task in between: the handler's own check saw it bound to the caller.
 */
it.effect("a change is checked on the service's own read, so a change in between is seen", () =>
  held(
    Effect.gen(function* () {
      const world = newWorld();
      const owner = scopeOf(null);
      yield* withScheduleTaskHandler(
        world,
        (service) => (_scope, input) =>
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
        (service, scheduleTask) =>
          Effect.gen(function* () {
            // Bound to the caller: its own words may go in, so the handler's check lets it by.
            yield* asCaller(
              service.upsert(
                task("task:raced", {
                  projectId: PROJECT,
                  threadId: CALLER,
                  threadMode: "continue",
                }),
              ),
            );
            const last = yield* scheduleTask({
              scheduledTaskId: "task:raced",
              prompt: "Words for a wider thread",
            });
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

/**
 * The service's change consumes a webhook secretRef before it saves; a change it would refuse
 * for the task's wider thread stops in the handler, so the service (and the secret) is not reached.
 */
it.effect(
  "a change to a task bound to a wider thread is refused before the service is called",
  () =>
    held(
      Effect.gen(function* () {
        const world = newWorld();
        const reached: Array<unknown> = [];
        yield* withScheduleTaskHandler(
          world,
          (service) => (_scope, input) =>
            Effect.gen(function* () {
              reached.push(input);
              const stored = yield* listed(service, input.scheduledTaskId).pipe(Effect.orDie);
              return {
                scheduledTaskId: stored.id,
                title: stored.title,
                prompt: stored.prompt,
                enabled: stored.enabled,
                projectId: stored.projectId,
                boundThreadId: stored.threadId,
                freshSession: false,
                schedule: stored.schedule,
                nextRunAt: stored.nextRunAt,
                lastRunStatus: stored.lastRunStatus,
              };
            }),
          (service, scheduleTask) =>
            Effect.gen(function* () {
              yield* service.upsert(
                task("task:bound-wide", {
                  projectId: PROJECT,
                  threadId: WIDE,
                  threadMode: "continue",
                }),
              );
              // Runs again: each try is refused the same way, before the service.
              for (const change of [
                { prompt: "Other words" },
                { prompt: "Other words" },
                { bindToCurrentThread: false },
                { bindToCurrentThread: true },
                { bindToCurrentThread: true, prompt: "Other words" },
              ]) {
                const last = yield* scheduleTask({ scheduledTaskId: "task:bound-wide", ...change });
                expect(last.isFailure).toBe(true);
                expect(last.result.message).toContain(
                  `this task runs in thread ${WIDE}, whose MCP profile "all" keeps MCP servers "docs" does not`,
                );
              }
              expect(reached).toHaveLength(0);
              // A new title, a pause, or the prompt it has: the service's to do, as before.
              for (const change of [
                { title: "Renamed" },
                { enabled: false },
                { prompt: "Do the work" },
              ]) {
                yield* scheduleTask({ scheduledTaskId: "task:bound-wide", ...change });
              }
              expect(reached).toHaveLength(3);
              // A task bound to the caller itself: its own words go through.
              yield* asCaller(
                service.upsert(
                  task("task:bound-own", {
                    projectId: PROJECT,
                    threadId: CALLER,
                    threadMode: "continue",
                  }),
                ),
              );
              yield* scheduleTask({ scheduledTaskId: "task:bound-own", prompt: "My words" });
              expect(reached).toHaveLength(4);
            }),
        );
      }),
    ),
);

/**
 * What `schedule_task`'s change does (read the task, then save the change on it), with the owner
 * moving the task's thread to another project in between (a scratch promotion moves its tasks).
 * Saved on the project it read, the task would move back and lose its thread.
 */
it.effect("a change that races the owner's move of the task's thread is refused, not unbound", () =>
  held(
    Effect.gen(function* () {
      const world = newWorld();
      yield* withService(NARROWED, world, (service) =>
        Effect.gen(function* () {
          yield* service.upsert(
            task("task:moved", { projectId: PROJECT, threadId: WIDE, threadMode: "continue" }),
          );
          const read = yield* listed(service, "task:moved");
          yield* service.moveThreadTasks({ threadIds: [WIDE], projectId: NARROW_PROJECT });
          // From a thread on all, and from the owner's own MCP client. Runs again: refused each
          // time, the task as the move left it.
          for (const scope of [scopeOf(WIDE), scopeOf(null), scopeOf(WIDE)]) {
            const refused = yield* service
              .upsert({ ...read, prompt: "New words" })
              .pipe(
                Effect.provideService(McpInvocationContext.McpInvocationContext, scope),
                Effect.flip,
              );
            expect(messageOf(refused)).toContain(
              "This task was moved to another project (with its thread) while this save was being made",
            );
            expect(yield* row("task:moved")).toMatchObject({
              prompt: "Do the work",
              project_id: NARROW_PROJECT,
              thread_id: WIDE,
            });
          }
          // Read again, the same change is saved where the task is now, still bound.
          const again = yield* listed(service, "task:moved");
          yield* service
            .upsert({ ...again, prompt: "New words" })
            .pipe(Effect.provideService(McpInvocationContext.McpInvocationContext, scopeOf(WIDE)));
          expect(yield* row("task:moved")).toMatchObject({
            prompt: "New words",
            project_id: NARROW_PROJECT,
            thread_id: WIDE,
          });
          // The app's save (no MCP call) keeps upstream's behaviour: the project the owner saves
          // is the one they picked, so the stale read moves the task back, off its thread.
          yield* service.upsert({ ...read, prompt: "Owner words" });
          expect(yield* row("task:moved")).toMatchObject({
            prompt: "Owner words",
            project_id: PROJECT,
            thread_id: null,
          });
        }),
      );
    }),
  ),
);
