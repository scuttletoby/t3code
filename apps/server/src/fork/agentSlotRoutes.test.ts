/**
 * Fork: scheduled runs through the agent slot gate, on upstream's real scheduled task
 * service and the server's real gate (`slotWorld.testkit.ts`). `create_threads` is covered in
 * `mcp/OrchestratorMcpService.slotGate.test.ts`, `t3_thread_launch` in `launchMcpProfile.test.ts`.
 *
 * The owner's rule: a refused run waits for a slot; it is never lost and never runs twice.
 */
import * as NodePlatformCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  type OrchestrationV2ThreadShell,
  OrchestratorMcpFailure,
  ProviderInstanceId,
  RunId,
  ScheduledTaskUpsertInput,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";
import * as TestClock from "effect/testing/TestClock";

import * as ThreadLaunchService from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ManagedProjectFolders from "../project/ManagedProjectFolders.ts";
import * as ScheduledTaskService from "../scheduledTasks/ScheduledTaskService.ts";
import * as Scheduler from "../scheduling/Scheduler.ts";
import * as SecretRequests from "../secrets/SecretRequests.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as AgentSlotRoutes from "./agentSlotRoutes.ts";
import { type SlotWorld, slotGateOver, slotWorld } from "./slotWorld.testkit.ts";

const INSTANCE = ProviderInstanceId.make("codex");
const OTHER = ProviderInstanceId.make("claude");
const BUSY = ThreadId.make("thread:busy");
const BOUND = ThreadId.make("thread:bound");
const STARTED = ThreadId.make("thread:scheduled-run");
const START = Date.parse("2026-11-21T10:00:00.000Z");
const EVERY_MS = 3_600_000;
const ONE_SLOT = { agentSlots: { enabled: true, maxPerMachine: 1 } };

const decodeInput = Schema.decodeUnknownEffect(ScheduledTaskUpsertInput);
const taskInput = (overrides: Record<string, unknown> = {}) =>
  decodeInput({
    title: "Nightly review",
    prompt: "Review the open pull requests.",
    enabled: true,
    schedule: { type: "interval", everyMs: EVERY_MS },
    projectId: "project-slots",
    workspaceStrategy: { type: "root" },
    modelSelection: { instanceId: INSTANCE, model: "gpt-5.4" },
    runtimeMode: "full-access",
    interactionMode: "default",
    ...overrides,
  });

/** Every schedule a run can be due on (there is no cron type; webhooks are tested below). */
const SCHEDULES = [
  { name: "interval", schedule: { type: "interval", everyMs: EVERY_MS } },
  { name: "once", schedule: { type: "once", runAt: "2026-11-21T11:00:00.000Z" } },
  { name: "fixed_time", schedule: { type: "fixed_time", timeOfDay: "23:30" } },
] as const;

/** The bound thread as the thread list shows it: its provider, and whether a run is active. */
interface BoundThread {
  instanceId: ProviderInstanceId;
  running: boolean;
}

interface Harness {
  readonly service: ScheduledTaskService.ScheduledTaskService["Service"];
  readonly sql: SqlClient.SqlClient;
  /** Every thread launched and every message sent to a bound thread. */
  readonly started: Array<string>;
  /** One pass of the due-task poll, as the scheduler runs it. */
  readonly poll: Effect.Effect<void>;
  readonly webhookWorker: Effect.Effect<void>;
  /** One pass of the fork's waiting-note cleanup. */
  readonly notesWorker: Effect.Effect<void>;
  /** The server's gate the runs ask (with \`slots\`). */
  readonly gate: Option.Option<SlotGate.SlotGate["Service"]>;
}

interface Options {
  readonly slots?: SlotWorld;
  readonly settings?: object;
  readonly bound?: BoundThread;
  /** Shared across boots, so a restart test sees what the first boot started. */
  readonly started?: Array<string>;
}

/**
 * Upstream's scheduled task service, its scheduler's work run by hand, on the store the caller
 * provides (one store, booted twice, is a restart). With `slots`, the server's gate runs over that
 * world on `settings`; without, the host runs none.
 */
const boot = <A, E>(body: (harness: Harness) => Effect.Effect<A, E>, options: Options = {}) =>
  Effect.gen(function* () {
    const started: Array<string> = options.started ?? [];
    const bound = options.bound ?? { instanceId: INSTANCE, running: false };
    const workers = new Map<string, Effect.Effect<void>>();
    const base = Layer.mergeAll(
      NodePlatformCrypto.layer,
      NodePlatformCrypto.layerDigest,
      NodePlatformCrypto.layerMac,
      Layer.succeed(Scheduler.Scheduler, {
        register: <E2, R>(name: string, work: Effect.Effect<void, E2, R>) =>
          Effect.gen(function* () {
            const context = yield* Effect.context<R>();
            workers.set(name, work.pipe(Effect.provideContext(context), Effect.orDie));
          }),
      }),
      ManagedPolicy.layerTest(),
      Layer.mock(ThreadLaunchService.ThreadLaunchService)({
        launch: (input) =>
          Effect.sync(() => {
            started.push(`launch:${input.commandId}`);
            return { threadId: STARTED, resumed: false } as never;
          }),
        isNonGitProject: () => Effect.succeed(false),
      }),
      Layer.mock(ThreadManagementService.ThreadManagementService)({
        getThreadShell: (threadId) =>
          Effect.sync(
            () =>
              ({
                id: threadId,
                projectId: "project-slots",
                modelSelection: { instanceId: bound.instanceId, model: "gpt-5.4" },
                activeRunId: bound.running ? RunId.make(`run:${threadId}`) : null,
                archivedAt: null,
                deletedAt: null,
              }) as unknown as OrchestrationV2ThreadShell,
          ),
        getThreadRecords: () => Effect.succeed({ runs: [], messages: [] } as never),
        sendToThread: (input) =>
          Effect.sync(() => {
            started.push(`send:${input.threadId}`);
            return {} as never;
          }),
      }),
      Layer.mock(ManagedProjectFolders.ManagedProjectFolders)({
        namedProjectsRoot: "/projects",
        isScratchProject: () => Effect.succeed(false),
      }),
      Layer.mock(SecretRequests.SecretRequests)({}),
      Layer.succeed(
        ScheduledTaskService.ScheduledTaskWebhookOrigin,
        Effect.succeed({ relayHookBaseUrl: null }),
      ),
    );
    const settings = ServerSettings.layerTest((options.settings ?? ONE_SLOT) as never);
    const dependencies =
      options.slots === undefined
        ? base
        : Layer.mergeAll(
            base,
            slotGateOver(options.slots, INSTANCE).pipe(
              Layer.provide(Layer.mergeAll(settings, NodeServices.layer)),
            ),
          );
    return yield* Effect.gen(function* () {
      const service = yield* ScheduledTaskService.ScheduledTaskService;
      const sql = yield* SqlClient.SqlClient;
      const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
      return yield* body({
        gate,
        service,
        sql,
        started,
        poll: Effect.suspend(() => workers.get("scheduled-tasks")!),
        webhookWorker: Effect.suspend(() => workers.get("webhook-coalescing")!),
        notesWorker: Effect.suspend(() => workers.get("agent-slot-notes")!),
      });
    }).pipe(
      Effect.provide(
        Layer.fresh(ScheduledTaskService.layer.pipe(Layer.provideMerge(dependencies))),
      ),
    );
  });

/** {@link boot} once, on a fresh store. */
const withService = <A, E>(
  body: (harness: Harness) => Effect.Effect<A, E>,
  options: Options = {},
) => boot(body, options).pipe(Effect.provide(SqlitePersistence.layerMemory));

/** The stored row: what the task's runs list shows. */
const rowOf = (sql: SqlClient.SqlClient, id: string) =>
  sql<{
    last_run_status: string;
    last_run_error: string | null;
    last_run_at: string | null;
    next_run_at: string | null;
    run_count: number;
  }>`SELECT last_run_status, last_run_error, last_run_at, next_run_at, run_count
     FROM scheduled_tasks WHERE task_id = ${id}`.pipe(Effect.map((rows) => rows[0]!));

/** Creates the task at START and moves the clock to its first run time, plus a second. */
const dueTask = (service: Harness["service"], overrides: Record<string, unknown> = {}) =>
  Effect.gen(function* () {
    yield* TestClock.setTime(START);
    const { task } = yield* service.upsert(yield* taskInput(overrides));
    yield* TestClock.setTime(Date.parse(task.nextRunAt!) + 1_000);
    return task;
  });

/** Lets forked work (a webhook delivery) run until it waits on the clock again. */
const settle = Effect.forEach(
  Array.from({ length: 20 }),
  () => Effect.yieldNow.pipe(Effect.andThen(Effect.promise(() => new Promise(setImmediate)))),
  { discard: true },
);

/** Moves the clock one slot retry at a time until `done`, settling between. */
const until = (done: () => boolean, label: string) =>
  Effect.gen(function* () {
    for (let step = 0; step < 50; step++) {
      yield* settle;
      if (done()) return;
      yield* TestClock.adjust(AgentSlotRoutes.WEBHOOK_SLOT_RETRY);
    }
    assert.fail(`never: ${label}`);
  });

/** One pass of the webhook worker, then one slot retry, `count` times. */
const workerRounds = (webhookWorker: Effect.Effect<void>, count: number) =>
  Effect.forEach(
    Array.from({ length: count }),
    () =>
      webhookWorker.pipe(
        Effect.andThen(settle),
        Effect.andThen(TestClock.adjust(AgentSlotRoutes.WEBHOOK_SLOT_RETRY)),
      ),
    { discard: true },
  );

const pendingStates = (sql: SqlClient.SqlClient, taskId: string) =>
  sql<{ delivery_id: string; state: string }>`
    SELECT delivery_id, state FROM scheduled_task_webhook_pending WHERE task_id = ${taskId}`;

const launches = (started: ReadonlyArray<string>) => started.filter((s) => s.startsWith("launch:"));

const webhookRequest = (
  task: { readonly id: string; readonly webhook?: { readonly path: string } | undefined },
  body = "{}",
) => ({
  hookId: task.id,
  token: task.webhook?.path.split("/").at(-1) ?? "",
  method: "POST" as const,
  path: `/api/hooks/${task.id}`,
  query: "",
  headers: { "content-type": "application/json" },
  body: new TextEncoder().encode(body),
  bodyText: body,
});

describe("scheduled runs through the agent slot gate (#706)", () => {
  it.effect.each(SCHEDULES)(
    "$name: refused, the run stays due with one note; the slot frees and it runs once",
    ({ schedule }) => {
      const slots = slotWorld([BUSY]);
      return withService(
        ({ service, sql, poll, started }) =>
          Effect.gen(function* () {
            const task = yield* dueTask(service, { schedule });
            const before = yield* rowOf(sql, task.id);
            yield* poll;
            assert.deepEqual(started, []);
            const waiting = yield* rowOf(sql, task.id);
            // Still due, nothing counted as a run, and the history says why it waits.
            assert.equal(waiting.next_run_at, before.next_run_at);
            assert.equal(waiting.last_run_status, before.last_run_status);
            assert.equal(waiting.run_count, 0);
            assert.equal(waiting.last_run_at, null);
            assert.equal(
              waiting.last_run_error,
              "Waiting for an agent slot: 1 agent is running on this machine, and the limit is 1. The run starts when a slot is free.",
            );
            const listed = (yield* service.list()).tasks.find((entry) => entry.id === task.id)!;
            assert.equal(listed.lastRunError, waiting.last_run_error);
            // Runs again: each poll asks again, and the one note stays one note.
            for (const _ of [1, 2, 3]) {
              yield* TestClock.adjust("5 seconds");
              yield* poll;
            }
            assert.deepEqual(started, []);
            const still = yield* rowOf(sql, task.id);
            assert.equal(still.last_run_error, waiting.last_run_error);
            assert.equal(still.next_run_at, before.next_run_at);
            // The slot frees: one run, the note gone.
            slots.running = [];
            yield* TestClock.adjust("5 seconds");
            yield* poll;
            assert.equal(started.length, 1);
            const ran = yield* rowOf(sql, task.id);
            assert.equal(ran.last_run_status, "succeeded");
            assert.equal(ran.last_run_error, null);
            assert.equal(ran.run_count, 1);
            // No catch-up: the next polls start nothing more.
            for (const _ of [1, 2]) {
              yield* TestClock.adjust("5 seconds");
              yield* poll;
            }
            assert.equal(started.length, 1);
          }),
        { slots },
      );
    },
  );

  it.effect("a count that cannot be read: the run waits the same way, then runs once", () => {
    const slots = { ...slotWorld(), fails: true };
    return withService(
      ({ service, sql, poll, started }) =>
        Effect.gen(function* () {
          const task = yield* dueTask(service);
          yield* poll;
          yield* poll;
          assert.deepEqual(started, []);
          const row = yield* rowOf(sql, task.id);
          assert.equal(
            row.last_run_error,
            "Waiting for an agent slot: the running agents could not be counted (the read failed with an error). The run starts when a slot is free.",
          );
          assert.equal(row.run_count, 0);
          slots.fails = false;
          yield* poll;
          yield* poll;
          assert.equal(started.length, 1);
          assert.equal((yield* rowOf(sql, task.id)).run_count, 1);
        }),
      { slots },
    );
  });

  it.effect("a restart while a run waits: the new server runs it, once", () =>
    Effect.gen(function* () {
      const started: Array<string> = [];
      const id = yield* boot(
        ({ service, poll }) =>
          Effect.gen(function* () {
            const task = yield* dueTask(service);
            yield* poll;
            return task.id;
          }),
        { slots: slotWorld([BUSY]), started },
      );
      assert.deepEqual(started, []);
      // The server stops and starts again; the busy agent is gone by then.
      yield* boot(
        ({ sql, poll }) =>
          Effect.gen(function* () {
            yield* TestClock.adjust("30 seconds");
            yield* poll;
            yield* poll;
            assert.equal(started.length, 1);
            const row = yield* rowOf(sql, id);
            assert.equal(row.run_count, 1);
            assert.equal(row.last_run_error, null);
          }),
        { slots: slotWorld(), started },
      );
    }).pipe(Effect.provide(SqlitePersistence.layerMemory)),
  );

  it.effect("an old store: a waiting note and a failed run left by an older server", () =>
    withService(
      ({ service, sql, poll, started }) =>
        Effect.gen(function* () {
          const task = yield* dueTask(service);
          // As an older server left it: a failed run on record (the old "Skipped:" note).
          yield* sql`UPDATE scheduled_tasks SET last_run_status = 'failed',
            last_run_error = 'Skipped: no slot.', run_count = 3 WHERE task_id = ${task.id}`;
          yield* poll;
          assert.deepEqual(started, []);
          const row = yield* rowOf(sql, task.id);
          assert.match(row.last_run_error ?? "", /^Waiting for an agent slot: /);
          assert.equal(row.last_run_status, "failed");
          assert.equal(row.run_count, 3);
        }),
      { slots: slotWorld([BUSY]) },
    ),
  );

  it.effect("a count unreadable for many polls logs its warning once, not every poll", () => {
    const slots = { ...slotWorld(), fails: true };
    const logs: Array<Logger.Options<unknown>> = [];
    const lines = (pattern: RegExp) =>
      logs.filter((entry) => pattern.test(Logger.formatLogFmt.log(entry))).length;
    return withService(
      ({ service, poll, started }) =>
        Effect.gen(function* () {
          yield* dueTask(service);
          for (let round = 0; round < 6; round++) {
            yield* poll;
            yield* TestClock.adjust("5 seconds");
          }
          assert.deepEqual(started, []);
          // The fake read crashes: its warning, once.
          assert.equal(lines(/counting running agents crashed/), 1);
          // One info line per due time for the waiting run, too.
          assert.equal(lines(/Scheduled run waits for an agent slot/), 1);
          slots.fails = false;
          yield* poll;
          assert.equal(started.length, 1);
          assert.equal(lines(/running agents can be counted again/), 1);
        }),
      { slots },
    ).pipe(
      Effect.provide(
        Logger.layer([Logger.make((entry) => logs.push(entry))], { mergeWithExisting: false }),
      ),
    );
  });

  it.effect(
    "the waiting note follows the reason, and a poll that changes nothing writes nothing",
    () => {
      const slots = { ...slotWorld([BUSY]), fails: true };
      const updatedAt = (sql: SqlClient.SqlClient, id: string) =>
        sql<{
          updated_at: string;
        }>`SELECT updated_at FROM scheduled_tasks WHERE task_id = ${id}`.pipe(
          Effect.map((rows) => rows[0]!.updated_at),
        );
      return withService(
        ({ service, sql, poll, notesWorker, started }) =>
          Effect.gen(function* () {
            const task = yield* dueTask(service);
            yield* poll;
            assert.match((yield* rowOf(sql, task.id)).last_run_error ?? "", /could not be counted/);
            // The count reads again, the machine still full: the note says so.
            slots.fails = false;
            yield* TestClock.adjust("5 seconds");
            yield* poll;
            const full =
              "Waiting for an agent slot: 1 agent is running on this machine, and the limit is 1. The run starts when a slot is free.";
            assert.equal((yield* rowOf(sql, task.id)).last_run_error, full);
            const written = yield* updatedAt(sql, task.id);
            for (const _ of [1, 2]) {
              yield* TestClock.adjust("5 seconds");
              yield* poll;
              // Still due: the cleanup keeps the note.
              yield* notesWorker;
            }
            assert.equal(yield* updatedAt(sql, task.id), written);
            assert.equal((yield* rowOf(sql, task.id)).last_run_error, full);
            assert.deepEqual(started, []);
          }),
        { slots },
      );
    },
  );

  const NOTE_ENDS = [
    {
      name: "paused",
      end: (service: Harness["service"], id: string) =>
        service.setEnabled({ id, enabled: false } as never),
    },
    {
      name: "moved to a later time",
      end: (service: Harness["service"], id: string) =>
        Effect.flatMap(
          taskInput({ id, schedule: { type: "once", runAt: "2026-11-23T11:00:00.000Z" } }),
          (input) => service.upsert(input),
        ),
    },
  ] as const;
  it.effect.each(NOTE_ENDS)(
    "a waiting note goes when the task is $name; a later wait writes a fresh one",
    ({ name, end }) =>
      withService(
        ({ service, sql, poll, notesWorker, started }) =>
          Effect.gen(function* () {
            const task = yield* dueTask(service, { schedule: SCHEDULES[1].schedule });
            yield* poll;
            assert.match(
              (yield* rowOf(sql, task.id)).last_run_error ?? "",
              /^Waiting for an agent slot: /,
            );
            yield* end(service, task.id);
            yield* notesWorker;
            const ended = yield* rowOf(sql, task.id);
            assert.equal(ended.last_run_error, null);
            assert.equal(ended.run_count, 0);
            if (name === "moved to a later time") {
              // Due again and still refused: a fresh note for the new due time.
              yield* TestClock.setTime(Date.parse("2026-11-23T11:00:01.000Z"));
              yield* poll;
              assert.match(
                (yield* rowOf(sql, task.id)).last_run_error ?? "",
                /^Waiting for an agent slot: /,
              );
            }
            assert.deepEqual(started, []);
          }),
        { slots: slotWorld([BUSY]) },
      ),
  );

  it.effect("fixed time: a run that waits past the missed-run grace still runs, once", () => {
    const slots = slotWorld([BUSY]);
    return withService(
      ({ service, sql, poll, started }) =>
        Effect.gen(function* () {
          const task = yield* dueTask(service, { schedule: SCHEDULES[2].schedule });
          // Polled every 5 s for 25 minutes (2.5 times the 10-minute grace).
          for (let poll_ = 0; poll_ < 300; poll_++) {
            yield* poll;
            yield* TestClock.adjust("5 seconds");
          }
          assert.deepEqual(started, []);
          const waiting = yield* rowOf(sql, task.id);
          assert.match(waiting.last_run_error ?? "", /^Waiting for an agent slot: /);
          // Still due today, not moved to tomorrow.
          assert.isTrue(
            Date.parse(waiting.next_run_at!) <= Date.parse(task.nextRunAt!) + 25 * 60_000,
          );
          slots.running = [];
          yield* poll;
          yield* poll;
          assert.equal(started.length, 1);
          assert.equal((yield* rowOf(sql, task.id)).run_count, 1);
        }),
      { slots },
    );
  });

  it.effect("free: the run starts, and the thread it started fills the slot", () =>
    withService(
      ({ service, sql, poll, started }) =>
        Effect.gen(function* () {
          const task = yield* dueTask(service);
          yield* poll;
          assert.deepEqual(started.length, 1);
          const row = yield* rowOf(sql, task.id);
          assert.equal(row.last_run_status, "succeeded");
          assert.equal(row.run_count, 1);
          // Its thread counts until its run shows (the start grace), so the next start waits.
          const refused = yield* service.runNow({ id: task.id }).pipe(Effect.flip);
          assert.include(refused.message, "agent");
          assert.deepEqual(started.length, 1);
        }),
      { slots: slotWorld() },
    ),
  );

  it.effect("a run that fails before its dispatch frees its slot at once", () =>
    withService(
      ({ service, sql, started }) =>
        Effect.gen(function* () {
          const task = yield* dueTask(service);
          // The write that marks the run running fails (a full disk, say).
          yield* sql`CREATE TRIGGER fail_mark BEFORE UPDATE OF last_run_status ON scheduled_tasks
            WHEN NEW.last_run_status = 'running' BEGIN SELECT RAISE(ABORT, 'disk full'); END`;
          const failed = yield* service.runNow({ id: task.id }).pipe(Effect.flip);
          assert.notInclude(failed.message, AgentSlotRoutes.RUN_NOW_REFUSED_PREFIX);
          assert.deepEqual(started, []);
          yield* sql`DROP TRIGGER fail_mark`;
          // Runs again at once: the failed run held no slot.
          const ran = yield* service.runNow({ id: task.id });
          assert.equal(ran.task.lastRunStatus, "succeeded");
          assert.equal(started.length, 1);
        }),
      { slots: slotWorld() },
    ),
  );

  it.effect("full: run now fails with a plain reason and writes nothing; free: it runs", () =>
    Effect.gen(function* () {
      const slots = slotWorld([BUSY]);
      yield* withService(
        ({ service, sql, started }) =>
          Effect.gen(function* () {
            const task = yield* dueTask(service);
            const before = yield* rowOf(sql, task.id);
            // Runs again with the same task: refused each time, the row untouched.
            for (const _ of [1, 2]) {
              const refused = yield* service.runNow({ id: task.id }).pipe(Effect.flip);
              assert.equal(refused._tag, "ScheduledTaskError");
              assert.equal(
                refused.message,
                "Not run, no agent slot: 1 agent is running on this machine, and the limit is 1.",
              );
              // The MCP tool answers it `slots_full`; any other failure keeps its code.
              const mapped = AgentSlotRoutes.runNowSlotRefusal(
                new OrchestratorMcpFailure({
                  code: "orchestration_error",
                  message: refused.message,
                }),
              );
              assert.equal(mapped.code, "slots_full");
              assert.equal(mapped.message, refused.message);
              assert.deepEqual(yield* rowOf(sql, task.id), before);
            }
            assert.deepEqual(started, []);
            slots.running = [];
            const ran = yield* service.runNow({ id: task.id });
            assert.equal(ran.task.lastRunStatus, "succeeded");
            assert.deepEqual(started.length, 1);
          }),
        { slots },
      );
      const other = new OrchestratorMcpFailure({
        code: "orchestration_error",
        message: "Schedule task is already running.",
      });
      assert.strictEqual(AgentSlotRoutes.runNowSlotRefusal(other), other);
    }),
  );

  it.effect("a bound task whose thread runs only queues its prompt: no slot, never waits", () =>
    withService(
      ({ service, sql, poll, started }) =>
        Effect.gen(function* () {
          const task = yield* dueTask(service, { threadId: BOUND });
          yield* poll;
          assert.deepEqual(started, [`send:${BOUND}`]);
          const row = yield* rowOf(sql, task.id);
          assert.equal(row.run_count, 1);
          assert.equal(row.last_run_error, null);
        }),
      { slots: slotWorld([BUSY, BOUND]), bound: { instanceId: INSTANCE, running: true } },
    ),
  );

  it.effect(
    "a bound task whose thread's run ends just after the check holds the slot it starts (#711)",
    () => {
      const slots = slotWorld([BOUND]);
      return withService(
        ({ service, poll, started }) =>
          Effect.gen(function* () {
            yield* dueTask(service, { threadId: BOUND });
            yield* poll;
            assert.deepEqual(started, [`send:${BOUND}`]);
            // The run ended before the prompt landed: the prompt starts the next run, in the slot.
            slots.running = [];
            const { task } = yield* service.upsert(yield* taskInput());
            const refused = yield* service.runNow({ id: task.id }).pipe(Effect.flip);
            assert.include(refused.message, "no agent slot");
            assert.deepEqual(launches(started), []);
            // That run never showed within the start grace: the slot frees, once.
            yield* TestClock.adjust("30 seconds");
            yield* service.runNow({ id: task.id });
            assert.equal(launches(started).length, 1);
          }),
        { slots, bound: { instanceId: INSTANCE, running: true } },
      );
    },
  );

  it.effect(
    "a bound task whose thread is idle starts a run: full, it waits; free, it sends",
    () => {
      const slots = slotWorld([BUSY]);
      return withService(
        ({ service, sql, poll, started }) =>
          Effect.gen(function* () {
            const task = yield* dueTask(service, { threadId: BOUND });
            yield* poll;
            assert.deepEqual(started, []);
            assert.match(
              (yield* rowOf(sql, task.id)).last_run_error ?? "",
              /^Waiting for an agent/,
            );
            slots.running = [];
            yield* poll;
            assert.deepEqual(started, [`send:${BOUND}`]);
          }),
        { slots, bound: { instanceId: INSTANCE, running: false } },
      );
    },
  );

  it.effect("a bound task is counted on its thread's current provider, not the stored one", () =>
    Effect.gen(function* () {
      // One agent may run on the stored provider's account; it is busy.
      const settings = { agentSlots: { enabled: true, maxPerAccount: { [INSTANCE]: 1 } } };
      yield* withService(
        ({ service, started }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(yield* taskInput({ threadId: BOUND }));
            // The thread moved to another provider: its account is free.
            yield* service.runNow({ id: task.id });
            assert.deepEqual(started, [`send:${BOUND}`]);
          }),
        { slots: slotWorld([BUSY]), settings, bound: { instanceId: OTHER, running: false } },
      );
      yield* withService(
        ({ service, started }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(
              yield* taskInput({
                threadId: BOUND,
                modelSelection: { instanceId: OTHER, model: "gpt-5.4" },
              }),
            );
            // Stored on the free account, but the thread now runs on the busy one.
            const refused = yield* service.runNow({ id: task.id }).pipe(Effect.flip);
            assert.include(refused.message, `account ${INSTANCE}`);
            assert.deepEqual(started, []);
          }),
        { slots: slotWorld([BUSY]), settings, bound: { instanceId: INSTANCE, running: false } },
      );
    }),
  );

  it.effect("full: a direct webhook delivery is stored, then runs once when a slot frees", () => {
    const slots = slotWorld([BUSY]);
    return withService(
      ({ service, sql, started, webhookWorker }) =>
        Effect.gen(function* () {
          yield* TestClock.setTime(START);
          const { task } = yield* service.upsert(
            yield* taskInput({ schedule: { type: "webhook" } }),
          );
          const accepted = yield* service.triggerWebhook(webhookRequest(task));
          if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
          yield* settle;
          // Refused: stored for the stored-delivery worker; the in-memory copy ended unrun.
          assert.deepEqual(yield* pendingStates(sql, task.id), [
            { delivery_id: accepted.deliveryId, state: "dispatching" },
          ]);
          // A minute of worker rounds and retries: still waiting, not dropped.
          yield* workerRounds(webhookWorker, 12);
          assert.deepEqual(started, []);
          const waiting = yield* service.getWebhookDelivery({
            id: task.id,
            deliveryId: accepted.deliveryId,
          });
          assert.notEqual(waiting.delivery.outcome, "dispatch_failed");
          slots.running = [];
          yield* until(() => started.length === 1, "the delivery started");
          // Runs again: more rounds start nothing more (one owner, never twice).
          yield* workerRounds(webhookWorker, 4);
          assert.equal(started.length, 1);
          assert.equal((yield* rowOf(sql, task.id)).run_count, 1);
          assert.deepEqual(yield* pendingStates(sql, task.id), []);
        }),
      { slots },
    );
  });

  it.effect("a direct webhook delivery waiting at a restart runs on the new server, once", () =>
    Effect.gen(function* () {
      const started: Array<string> = [];
      const ids = yield* boot(
        ({ service, sql, webhookWorker }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(
              yield* taskInput({ schedule: { type: "webhook" } }),
            );
            const accepted = yield* service.triggerWebhook(webhookRequest(task, '{"n":1}'));
            if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
            // 30 s of waiting, the worker holding it, before the server stops.
            yield* workerRounds(webhookWorker, 6);
            assert.deepEqual(
              (yield* pendingStates(sql, task.id)).map((row) => row.state),
              ["dispatching"],
            );
            return { taskId: task.id, deliveryId: accepted.deliveryId };
          }),
        { slots: slotWorld([BUSY]), started },
      );
      assert.deepEqual(started, []);
      yield* boot(
        ({ service, sql, poll, webhookWorker }) =>
          Effect.gen(function* () {
            yield* webhookWorker;
            yield* until(() => started.length === 1, "the delivery started after the restart");
            for (let round = 0; round < 10; round++) {
              yield* poll;
              yield* workerRounds(webhookWorker, 1);
            }
            assert.equal(started.length, 1);
            assert.equal((yield* rowOf(sql, ids.taskId)).run_count, 1);
            assert.deepEqual(yield* pendingStates(sql, ids.taskId), []);
            const { delivery } = yield* service.getWebhookDelivery({
              id: ids.taskId,
              deliveryId: ids.deliveryId,
            });
            assert.equal(delivery.outcome, "accepted");
          }),
        { slots: slotWorld(), started },
      );
    }).pipe(Effect.provide(SqlitePersistence.layerMemory)),
  );

  const WEBHOOK_STOPS = [
    {
      name: "paused",
      stop: (service: Harness["service"], id: string) =>
        service.setEnabled({ id, enabled: false } as never),
    },
    {
      name: "deleted",
      stop: (service: Harness["service"], id: string) => service.delete({ id } as never),
    },
    {
      // A run let through after the delete would start on the new task (same id, same trigger).
      name: "deleted and made again under its id",
      stop: (service: Harness["service"], id: string) =>
        service.delete({ id } as never).pipe(
          Effect.andThen(taskInput({ id, schedule: { type: "webhook" } })),
          Effect.flatMap((input) => service.upsert(input)),
        ),
    },
    {
      name: "given another trigger",
      stop: (service: Harness["service"], id: string) =>
        Effect.flatMap(
          taskInput({ id, schedule: { type: "interval", everyMs: EVERY_MS } }),
          (input) => service.upsert(input),
        ),
    },
  ] as const;
  it.effect.each(WEBHOOK_STOPS)(
    "a webhook delivery that waits ends when its task is $name: the slot frees, nothing starts",
    ({ name, stop }) => {
      const slots = slotWorld([BUSY]);
      return withService(
        ({ service, started, webhookWorker, gate }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(
              yield* taskInput({ schedule: { type: "webhook" } }),
            );
            const accepted = yield* service.triggerWebhook(webhookRequest(task));
            if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
            // The worker holds it, waiting for a slot.
            yield* workerRounds(webhookWorker, 2);
            yield* stop(service, task.id);
            // The slot frees: had the wait not seen the stop, the delivery would start now.
            slots.running = [];
            yield* workerRounds(webhookWorker, 4);
            assert.deepEqual(started, []);
            // Given up: its place in the gate's line is gone too.
            assert.deepEqual(yield* Option.getOrThrow(gate).line, []);
            if (name === "paused") {
              const { delivery } = yield* service.getWebhookDelivery({
                id: task.id,
                deliveryId: accepted.deliveryId,
              });
              assert.equal(delivery.outcome, "dispatch_failed");
            }
          }),
        { slots },
      );
    },
  );

  it.effect("a coalesced webhook delivery waiting at a restart runs on the new server, once", () =>
    Effect.gen(function* () {
      const started: Array<string> = [];
      const dedupe = { coalesceKey: "{{body.group}}", coalesceWindowSeconds: 10 };
      const id = yield* boot(
        ({ service, sql, webhookWorker }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(
              yield* taskInput({ schedule: { type: "webhook", dedupe } }),
            );
            const accepted = yield* service.triggerWebhook(webhookRequest(task, '{"group":"A"}'));
            assert.equal(accepted._tag === "accepted" && accepted.outcome, "accepted");
            yield* TestClock.adjust("11 seconds");
            yield* webhookWorker;
            for (let step = 0; step < 6; step++) {
              yield* settle;
              yield* TestClock.adjust(AgentSlotRoutes.WEBHOOK_SLOT_RETRY);
            }
            const pending = yield* sql<{ state: string }>`
              SELECT state FROM scheduled_task_webhook_pending WHERE task_id = ${task.id}`;
            assert.deepEqual(
              pending.map((row) => row.state),
              ["dispatching"],
            );
            return task.id;
          }),
        { slots: slotWorld([BUSY]), started },
      );
      assert.deepEqual(started, []);
      yield* boot(
        ({ sql, webhookWorker }) =>
          Effect.gen(function* () {
            yield* webhookWorker;
            yield* until(() => started.length === 1, "the delivery started after the restart");
            for (let step = 0; step < 3; step++) {
              yield* webhookWorker;
              yield* settle;
              yield* TestClock.adjust(AgentSlotRoutes.WEBHOOK_SLOT_RETRY);
            }
            assert.equal(started.length, 1);
            assert.equal((yield* rowOf(sql, id)).run_count, 1);
          }),
        { slots: slotWorld(), started },
      );
    }).pipe(Effect.provide(SqlitePersistence.layerMemory)),
  );

  it.effect("off, or a host that runs no gate: runs as before, nothing read", () =>
    Effect.gen(function* () {
      const slots = slotWorld([BUSY]);
      yield* withService(
        ({ service, poll, started }) =>
          Effect.gen(function* () {
            yield* dueTask(service);
            yield* poll;
            assert.deepEqual(launches(started).length, 1);
          }),
        { slots, settings: { agentSlots: { mode: "fixed", maxPerMachine: 1 } } },
      );
      assert.equal(slots.reads, 0);
      yield* withService(({ service, poll, started }) =>
        Effect.gen(function* () {
          yield* dueTask(service);
          yield* poll;
          assert.deepEqual(started.length, 1);
        }),
      );
    }),
  );
});

describe("a scheduled run or webhook delivery that waits keeps its place in the gate's line (#738)", () => {
  const LATER = ThreadId.make("thread:later-waiter");
  /** A wake-up of another idle thread that joins the line after the run (asks every 5 s). */
  const laterWaiter = (gate: Harness["gate"]) =>
    Option.getOrThrow(gate).acquire({
      callerThreadId: LATER,
      targetInstanceId: INSTANCE,
      intoThreadId: LATER,
      keepsHeld: true,
      place: { key: "later-waiter" },
    });
  const lineOf = (gate: Harness["gate"]) => Option.getOrThrow(gate).line;

  it.effect(
    "full: a waiter that joins later does not pass a scheduled run; the run starts first",
    () => {
      const slots = slotWorld([BUSY]);
      return withService(
        ({ service, poll, started, gate }) =>
          Effect.gen(function* () {
            const task = yield* dueTask(service);
            yield* poll;
            assert.deepEqual(started, []);
            assert.deepEqual(yield* lineOf(gate), [`scheduled-run:${task.id}`]);
            yield* TestClock.adjust("5 seconds");
            assert.equal((yield* laterWaiter(gate)).type, "refuse");
            // Runs again: each poll keeps its place (no second place).
            yield* poll;
            assert.deepEqual(yield* lineOf(gate), [`scheduled-run:${task.id}`, "later-waiter"]);
            // A slot frees and the later waiter asks first: the run is ahead of it.
            slots.running = [];
            yield* TestClock.adjust("1 second");
            assert.equal((yield* laterWaiter(gate)).type, "refuse");
            yield* TestClock.adjust("4 seconds");
            yield* poll;
            assert.deepEqual(launches(started).length, 1);
            // Let through: its place is gone; the waiter is next.
            assert.deepEqual(yield* lineOf(gate), ["later-waiter"]);
          }),
        { slots },
      );
    },
  );

  it.effect(
    "a run due before a waiter joined is ahead of it, though the poll asks after it",
    () => {
      const slots = slotWorld([BUSY]);
      return withService(
        ({ service, poll, started, gate }) =>
          Effect.gen(function* () {
            const task = yield* dueTask(service);
            // The waiter asks a second after the due time, before the poll does.
            assert.equal((yield* laterWaiter(gate)).type, "refuse");
            yield* poll;
            assert.deepEqual(yield* lineOf(gate), [`scheduled-run:${task.id}`, "later-waiter"]);
            slots.running = [];
            assert.equal((yield* laterWaiter(gate)).type, "refuse");
            yield* poll;
            assert.equal(launches(started).length, 1);
          }),
        { slots },
      );
    },
  );

  it.effect("full: a waiter that joins later does not pass a stored webhook delivery", () => {
    const slots = slotWorld([BUSY]);
    return withService(
      ({ service, started, webhookWorker, gate }) =>
        Effect.gen(function* () {
          yield* TestClock.setTime(START);
          const { task } = yield* service.upsert(
            yield* taskInput({ schedule: { type: "webhook" } }),
          );
          const accepted = yield* service.triggerWebhook(webhookRequest(task));
          if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
          // Refused and stored; the worker holds it, asking every 5 s in its place.
          yield* workerRounds(webhookWorker, 2);
          assert.deepEqual(yield* lineOf(gate), [`webhook-delivery:${accepted.deliveryId}`]);
          assert.equal((yield* laterWaiter(gate)).type, "refuse");
          slots.running = [];
          assert.equal((yield* laterWaiter(gate)).type, "refuse");
          yield* until(() => started.length === 1, "the delivery started");
          assert.deepEqual(yield* lineOf(gate), ["later-waiter"]);
        }),
      { slots },
    );
  });

  it.effect(
    "a stored delivery's place goes when its task is deleted before the worker runs it",
    () => {
      const slots = slotWorld([BUSY]);
      return withService(
        ({ service, webhookWorker, notesWorker, gate }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(
              yield* taskInput({ schedule: { type: "webhook" } }),
            );
            const accepted = yield* service.triggerWebhook(webhookRequest(task));
            if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
            yield* settle;
            // Refused and stored: its place waits for the worker.
            assert.deepEqual(yield* lineOf(gate), [`webhook-delivery:${accepted.deliveryId}`]);
            yield* service.delete({ id: task.id } as never);
            // The worker drops it without asking the gate; the sweep sees it is no longer pending.
            yield* webhookWorker;
            yield* settle;
            yield* notesWorker;
            assert.deepEqual(yield* lineOf(gate), []);
          }),
        { slots },
      );
    },
  );

  it.effect(
    "a waiting run's place goes when its task is deleted or paused (no slot kept for it)",
    () => {
      const slots = slotWorld([BUSY]);
      return withService(
        ({ service, poll, notesWorker, gate }) =>
          Effect.gen(function* () {
            const deleted = yield* dueTask(service);
            const paused = yield* service.upsert(
              yield* taskInput({
                title: "Second",
                schedule: { type: "interval", everyMs: 60_000 },
              }),
            );
            yield* TestClock.adjust("2 minutes");
            yield* poll;
            assert.sameMembers(
              [...(yield* lineOf(gate))],
              [`scheduled-run:${deleted.id}`, `scheduled-run:${paused.task.id}`],
            );
            // Still due: the sweep keeps both.
            yield* notesWorker;
            assert.lengthOf(yield* lineOf(gate), 2);
            yield* service.delete({ id: deleted.id } as never);
            yield* service.setEnabled({ id: paused.task.id, enabled: false } as never);
            yield* notesWorker;
            assert.deepEqual(yield* lineOf(gate), []);
            // A later start is not held back by them.
            slots.running = [];
            assert.equal((yield* laterWaiter(gate)).type, "allow");
          }),
        { slots },
      );
    },
  );
});

describe("refusal texts fit who reads them (#706)", () => {
  const full = {
    type: "full" as const,
    decision: {
      type: "refuse" as const,
      rule: "machine" as const,
      used: 3,
      max: 3,
      instanceId: INSTANCE,
    },
    waitsOn: true,
  };

  it("a start no agent here asked for: what happened there, no advice to wait or retry", () => {
    const text = AgentSlotRoutes.noCallerRefusalText(full);
    assert.equal(
      text,
      "No agent was started: the machine asked to start it has no free agent slot (3 agents are running there, and the limit is 3).",
    );
    const unread = AgentSlotRoutes.noCallerRefusalText({ type: "unread", problem: "timeout" });
    for (const said of [text, unread]) {
      assert.notInclude(said, "wait_for");
      assert.notInclude(said, "Try again");
      assert.notInclude(said, "end your turn");
    }
  });

  it("a waiting scheduled run's note gives no advice either", () => {
    for (const refusal of [full, { type: "unread" as const, problem: "error" as const }]) {
      const note = AgentSlotRoutes.waitingNote(refusal);
      assert.isTrue(note.startsWith(AgentSlotRoutes.WAITING_NOTE_PREFIX));
      assert.notInclude(note, "wait_for");
      assert.notInclude(note, "Try again");
    }
  });
});
