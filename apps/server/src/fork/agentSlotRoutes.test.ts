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
  PROVIDER_SEND_TURN_MAX_INPUT_CHARS,
  ProviderInstanceId,
  RunId,
  ScheduledTask,
  ScheduledTaskUpsertInput,
  ThreadId,
} from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Metric from "effect/Metric";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";
import * as SqlError from "effect/sql/SqlError";
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
  /** What each of those was started with, in the same order (a send has no title). */
  readonly prompts: Array<Prompt>;
  /** One pass of the due-task poll, as the scheduler runs it. */
  readonly poll: Effect.Effect<void>;
  readonly webhookWorker: Effect.Effect<void>;
  /** One pass of the fork's waiting-note cleanup. */
  readonly notesWorker: Effect.Effect<void>;
  /** The server's gate the runs ask (with \`slots\`). */
  readonly gate: Option.Option<SlotGate.SlotGate["Service"]>;
}

interface Prompt {
  readonly text: string;
  readonly title?: string | undefined;
  readonly projectId?: string | undefined;
}

interface Options {
  readonly slots?: SlotWorld;
  readonly settings?: object;
  readonly bound?: BoundThread;
  /** Shared across boots, so a restart test sees what the first boot started. */
  readonly started?: Array<string>;
  readonly prompts?: Array<Prompt>;
  /** With `slots`: each list of line keys the scheduled runs ask the gate to leave, in order. */
  readonly leaves?: Array<ReadonlyArray<string>>;
}

/** The gate `layer` builds, each list of keys it is asked to leave noted in `leaves` first. */
const spyLeaves = <E, R>(
  layer: Layer.Layer<SlotGate.SlotGate, E, R>,
  leaves: Array<ReadonlyArray<string>>,
) =>
  Layer.effect(
    SlotGate.SlotGate,
    Effect.map(SlotGate.SlotGate, (gate) => ({
      ...gate,
      leave: (keys: ReadonlyArray<string>) =>
        Effect.sync(() => leaves.push(keys)).pipe(Effect.andThen(gate.leave(keys))),
    })),
  ).pipe(Layer.provide(layer));

/**
 * Upstream's scheduled task service, its scheduler's work run by hand, on the store the caller
 * provides (one store, booted twice, is a restart). With `slots`, the server's gate runs over that
 * world on `settings`; without, the host runs none.
 */
const boot = <A, E>(body: (harness: Harness) => Effect.Effect<A, E>, options: Options = {}) =>
  Effect.gen(function* () {
    // The gate samples the machine every 30 s from when it is built. Built at the test clock's 0, a
    // move to START would run every sample in the 56 years between (the test clock runs each sleep
    // it passes): build it at START, never earlier. A clock already past START stays where it is.
    if ((yield* Clock.currentTimeMillis) < START) yield* TestClock.setTime(START);
    const started: Array<string> = options.started ?? [];
    const prompts: Array<Prompt> = options.prompts ?? [];
    const bound = options.bound ?? { instanceId: INSTANCE, running: false };
    const leaves = options.leaves;
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
            prompts.push({
              text: input.initialMessage?.text ?? "",
              title: input.title,
              projectId: input.projectId,
            });
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
            prompts.push({ text: input.text });
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
              (gateLayer) => (leaves === undefined ? gateLayer : spyLeaves(gateLayer, leaves)),
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
        prompts,
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

  /** The delivery as its log shows it. */
  const deliveryOf = (service: Harness["service"], id: string, deliveryId: string) =>
    service
      .getWebhookDelivery({ id, deliveryId } as never)
      .pipe(Effect.map(({ delivery }) => delivery));

  /** A webhook task, delivered once now with the slot full: the worker holds it in its wait. */
  const waitingDelivery = (harness: Harness, overrides: Record<string, unknown> = {}) =>
    Effect.gen(function* () {
      yield* TestClock.setTime(START);
      const input = yield* taskInput({ schedule: { type: "webhook" }, ...overrides });
      const { task } = yield* harness.service.upsert(input);
      const accepted = yield* harness.service.triggerWebhook(webhookRequest(task));
      if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
      // Stored; one worker pass with the clock still at START takes it into its slot wait.
      yield* settle;
      yield* harness.webhookWorker;
      yield* settle;
      assert.deepEqual(harness.started, []);
      const save = (patch: Record<string, unknown>) =>
        Effect.flatMap(taskInput({ ...input, id: task.id, ...patch }), (next) =>
          harness.service.upsert(next),
        );
      return { task, deliveryId: accepted.deliveryId, save };
    });

  const SAVED_WHILE_WAITING = [
    { name: "a task that starts a thread", overrides: {}, kind: "launch:" },
    { name: "a task bound to an idle thread", overrides: { threadId: BOUND }, kind: "send:" },
  ] as const;
  it.effect.each(SAVED_WHILE_WAITING)(
    "$name: a webhook delivery saved while it waits runs once, on the task as saved (#719)",
    ({ overrides, kind }) => {
      const slots = slotWorld([BUSY]);
      return withService(
        (harness) =>
          Effect.gen(function* () {
            const { service, sql, started, prompts, webhookWorker } = harness;
            const { task, deliveryId, save } = yield* waitingDelivery(harness, overrides);
            // Saved twice while it waits: first in the same millisecond as the copy it holds
            // (same `updated_at`, only the prompt differs), then in its next wait.
            for (const [step, prompt] of [
              "Read the new issues.",
              "Close the stale issues.",
            ].entries()) {
              if (step > 0) yield* TestClock.adjust("1 minute");
              yield* save({ prompt });
              yield* workerRounds(webhookWorker, 3);
              // Not run on the copy from before the save, not dropped: still stored, and its log
              // shows the prompt rendered again from the task as saved.
              assert.deepEqual(started, []);
              assert.deepEqual(yield* pendingStates(sql, task.id), [
                { delivery_id: deliveryId, state: "dispatching" },
              ]);
              const waiting = yield* deliveryOf(service, task.id, deliveryId);
              assert.equal(waiting.outcome, "accepted");
              assert.include(waiting.renderedPrompt ?? "", prompt);
            }
            slots.running = [];
            yield* until(() => started.length === 1, "the delivery started");
            assert.isTrue(started[0]!.startsWith(kind));
            assert.include(prompts[0]!.text, "Close the stale issues.");
            assert.notInclude(prompts[0]!.text, "Review the open pull requests.");
            assert.notInclude(prompts[0]!.text, "Read the new issues.");
            // Runs again: more rounds start nothing more, and the delivery is done, not failed.
            yield* workerRounds(webhookWorker, 4);
            assert.equal(started.length, 1);
            assert.equal((yield* rowOf(sql, task.id)).run_count, 1);
            assert.deepEqual(yield* pendingStates(sql, task.id), []);
            assert.equal((yield* deliveryOf(service, task.id, deliveryId)).outcome, "accepted");
          }),
        { slots },
      );
    },
  );

  it.effect("a save while it waits that keeps the prompt is seen too: it runs as saved", () => {
    const slots = slotWorld([BUSY]);
    return withService(
      (harness) =>
        Effect.gen(function* () {
          const { save } = yield* waitingDelivery(harness);
          yield* TestClock.adjust("1 minute");
          yield* save({ title: "Morning review" });
          yield* workerRounds(harness.webhookWorker, 2);
          slots.running = [];
          yield* until(() => harness.started.length === 1, "the delivery started");
          assert.equal(harness.prompts[0]!.title, "Morning review");
        }),
      { slots },
    );
  });

  it.effect.each([
    { name: "only the title", patch: { title: "Morning review" }, field: "title" },
    { name: "only the project", patch: { projectId: "project-other" }, field: "projectId" },
  ] as const)(
    "a save in the same millisecond as the copy it holds that changes $name is seen (#750)",
    ({ patch, field }) => {
      const slots = slotWorld([BUSY]);
      return withService(
        (harness) =>
          Effect.gen(function* () {
            // Same `updated_at` and same prompt: only the changed field tells the copies apart.
            const { save } = yield* waitingDelivery(harness);
            yield* save(patch);
            yield* workerRounds(harness.webhookWorker, 2);
            slots.running = [];
            yield* until(() => harness.started.length === 1, "the delivery started");
            assert.equal(harness.prompts[0]![field], Object.values(patch)[0]);
          }),
        { slots },
      );
    },
  );

  /**
   * The task's fields a waiting delivery does not compare (`RunCopy`), each with why: writing one
   * is not a save a run would see. A new task field must be added here or to `RunCopy`.
   */
  const NOT_COMPARED: Record<string, string> = {
    id: "the row is read by it",
    enabled: "`webhookGone` checks it (a pause ends the delivery)",
    createdAt: "`webhookGone` checks it (a replaced task ends the delivery)",
    updatedAt: "bookkeeping: a token rotation or a pause undone writes it",
    nextRunAt: "bookkeeping of runs on a schedule",
    lastRunAt: "bookkeeping of runs",
    lastRunStatus: "bookkeeping of runs",
    lastRunError: "bookkeeping of runs (a waiting note)",
    runCount: "bookkeeping of runs",
    webhook: "the endpoint and its token: a delivery already came in",
    blockedByPolicy: "derived from the managed policy, not stored on the row",
  };

  it("the wait compares every task field but a named list left out (#756)", () => {
    const compared = Object.keys(AgentSlotRoutes.RunCopy.fields).toSorted();
    const expected = Object.keys(ScheduledTask.fields)
      .filter((field) => !(field in NOT_COMPARED))
      .toSorted();
    assert.deepEqual(compared, expected);
    // Every name left out is a task field (none stale).
    for (const field of Object.keys(NOT_COMPARED)) assert.property(ScheduledTask.fields, field);
  });

  /** How many webhook runs ended as `outcome` (`webhookRunsTotal`), so far. */
  const webhookRuns = (outcome: string) =>
    Metric.snapshot.pipe(
      Effect.map((snapshots) =>
        snapshots
          .filter(
            (snapshot) =>
              snapshot.id === "t3_webhook_runs_total" && snapshot.attributes?.outcome === outcome,
          )
          .reduce(
            (sum, snapshot) =>
              sum + (snapshot.type === "Counter" ? Number(snapshot.state.count) : 0),
            0,
          ),
      ),
    );

  /** Counts each time the stored-delivery worker takes a delivery again (it writes its log). */
  const countTakes = (sql: SqlClient.SqlClient) =>
    Effect.gen(function* () {
      yield* sql`CREATE TABLE takes (delivery_id TEXT)`;
      yield* sql`CREATE TRIGGER count_takes AFTER UPDATE OF rendered_prompt
        ON scheduled_task_webhook_deliveries BEGIN INSERT INTO takes VALUES (NEW.delivery_id); END`;
      return sql<{ n: number }>`SELECT COUNT(*) AS n FROM takes`.pipe(
        Effect.map(([row]) => row!.n),
      );
    });

  it.effect.each([
    { name: "no save", write: () => Effect.void },
    {
      name: "a new webhook token",
      write: (harness: Harness, id: string) =>
        harness.service.rotateWebhookToken({ id } as never).pipe(Effect.asVoid),
    },
    {
      name: "a pause undone before its next retry",
      write: (harness: Harness, id: string) =>
        harness.service
          .setEnabled({ id, enabled: false } as never)
          .pipe(
            Effect.andThen(harness.service.setEnabled({ id, enabled: true } as never)),
            Effect.asVoid,
          ),
    },
    {
      name: "its prompt written outside T3 with spaces at either end",
      write: (harness: Harness, id: string) =>
        harness.sql`UPDATE scheduled_tasks SET prompt = '  ' || prompt || ' ' WHERE task_id = ${id}`.pipe(
          Effect.asVoid,
        ),
    },
  ])(
    "a stored delivery waiting with $name is left alone: not handed back, one start counted (#750)",
    ({ write }) => {
      const slots = slotWorld([BUSY]);
      return withService(
        (harness) =>
          Effect.gen(function* () {
            const { sql, started, prompts, webhookWorker } = harness;
            const { task, deliveryId } = yield* waitingDelivery(harness);
            // Counted once already: the direct delivery stored for the worker (upstream's count).
            const startedBefore = yield* webhookRuns("started");
            const takes = yield* countTakes(sql);
            yield* TestClock.adjust("1 second");
            yield* write(harness, task.id);
            // Runs again: retry after retry, the worker's run keeps its wait; nothing is handed
            // back to the worker, nothing starts and nothing counts as started.
            yield* workerRounds(webhookWorker, 4);
            assert.equal(yield* takes, 0);
            assert.deepEqual(started, []);
            assert.equal((yield* webhookRuns("started")) - startedBefore, 0);
            assert.deepEqual(yield* pendingStates(sql, task.id), [
              { delivery_id: deliveryId, state: "dispatching" },
            ]);
            slots.running = [];
            yield* until(() => started.length === 1, "the delivery started");
            yield* workerRounds(webhookWorker, 3);
            assert.equal(started.length, 1);
            assert.include(prompts[0]!.text, "Review the open pull requests.");
            assert.equal(yield* takes, 0);
            assert.equal((yield* webhookRuns("started")) - startedBefore, 1);
            assert.deepEqual(yield* pendingStates(sql, task.id), []);
          }),
        { slots },
      );
    },
  );

  it.effect(
    "a waiting delivery whose task cannot be read asks again at the next retry, not dropped (#750)",
    () => {
      const slots = slotWorld([BUSY]);
      return withService(
        (harness) =>
          Effect.gen(function* () {
            const { service, sql, started, prompts, webhookWorker } = harness;
            const { task, deliveryId } = yield* waitingDelivery(harness);
            // The task's row cannot be read (its prompt column is gone for a while), and a slot
            // is free: no slot is taken on a copy that cannot be checked, and it is not dropped.
            yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN prompt TO prompt_away`;
            slots.running = [];
            yield* workerRounds(webhookWorker, 3);
            assert.deepEqual(started, []);
            assert.deepEqual(yield* pendingStates(sql, task.id), [
              { delivery_id: deliveryId, state: "dispatching" },
            ]);
            assert.equal((yield* deliveryOf(service, task.id, deliveryId)).outcome, "accepted");
            // Readable again: the next retry runs it, once.
            yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN prompt_away TO prompt`;
            yield* until(() => started.length === 1, "the delivery started");
            yield* workerRounds(webhookWorker, 3);
            assert.equal(started.length, 1);
            assert.include(prompts[0]!.text, "Review the open pull requests.");
            assert.equal((yield* rowOf(sql, task.id)).run_count, 1);
            assert.deepEqual(yield* pendingStates(sql, task.id), []);
          }),
        { slots },
      );
    },
  );

  /** A delivery's log row, read without the task (whose row may not be readable). */
  const deliveryRow = (sql: SqlClient.SqlClient, deliveryId: string) =>
    sql<{ outcome: string; error: string | null }>`
      SELECT outcome, error FROM scheduled_task_webhook_deliveries WHERE delivery_id = ${deliveryId}
    `.pipe(Effect.map((rows) => rows[0]!));

  /** Moves the clock `count` slot retries, settling after each. */
  const retries = (count: number) =>
    Effect.forEach(
      Array.from({ length: count }),
      () => TestClock.adjust(AgentSlotRoutes.WEBHOOK_SLOT_RETRY).pipe(Effect.andThen(settle)),
      { discard: true },
    );

  /** Slot retries in the unreadable limit. */
  const LIMIT_RETRIES = AgentSlotRoutes.WEBHOOK_UNREADABLE_RETRIES;

  const UNREADABLE_WARNING = /waiting for a slot cannot read its task/;
  const SKIPPED_WARNING = /waiting for a slot is skipped: its task stayed unreadable/;

  /**
   * Runs `body` on a clock whose wall time `sleep` moves ahead with no timer woken, as when the
   * host sleeps between two retries (its timers count awake time only).
   */
  const withHostSleep = <A, E>(body: (sleep: (ms: number) => void) => Effect.Effect<A, E>) =>
    Clock.clockWith((clock) => {
      let asleepMs = 0;
      const wallMs = () => clock.currentTimeMillisUnsafe() + asleepMs;
      const host: Clock.Clock = Object.assign(Object.create(clock), {
        currentTimeMillisUnsafe: wallMs,
        currentTimeMillis: Effect.sync(wallMs),
        currentTimeNanosUnsafe: () => BigInt(wallMs()) * 1_000_000n,
        currentTimeNanos: Effect.sync(() => BigInt(wallMs()) * 1_000_000n),
      });
      return body((ms) => {
        asleepMs += ms;
      }).pipe(Effect.provideService(Clock.Clock, host));
    });

  it.effect(
    "a waiting delivery whose task stays unreadable is skipped after the limit, warned once, and the next delivery is not stuck (#756)",
    () => {
      const slots = slotWorld([BUSY]);
      const logs: Array<Logger.Options<unknown>> = [];
      const lines = (pattern: RegExp) =>
        logs.filter((entry) => pattern.test(Logger.formatLogFmt.log(entry))).length;
      return withService(
        (harness) =>
          Effect.gen(function* () {
            const { service, sql, started, prompts } = harness;
            const { task, deliveryId } = yield* waitingDelivery(harness);
            // A later delivery of the same task waits behind it (its task's webhook permit).
            const next = yield* service.triggerWebhook(webhookRequest(task));
            if (next._tag !== "accepted") return assert.fail(`not accepted: ${next._tag}`);
            yield* settle;
            // The row stays unreadable (a column the service reads is gone), and a slot is free.
            yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN prompt TO prompt_away`;
            slots.running = [];
            // Its cause changes within the stretch (another column gone instead): still one
            // stretch, warned once, with the first cause.
            yield* retries(10);
            yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN prompt_away TO prompt`;
            yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN title TO title_away`;
            // Just short of the limit: nothing starts, nothing is dropped, one warning so far.
            yield* retries(LIMIT_RETRIES - 11);
            assert.deepEqual(started, []);
            assert.deepEqual(yield* pendingStates(sql, task.id), [
              { delivery_id: deliveryId, state: "dispatching" },
            ]);
            assert.equal((yield* deliveryRow(sql, deliveryId)).outcome, "accepted");
            assert.equal((yield* deliveryRow(sql, next.deliveryId)).outcome, "accepted");
            assert.equal(lines(UNREADABLE_WARNING), 1);
            assert.equal(lines(SKIPPED_WARNING), 0);
            // At the limit: skipped with its reason, its row gone; nothing started on it. The
            // skip logs the cause it ended on (the last one), not only the first.
            yield* retries(1);
            assert.deepEqual(started, []);
            assert.deepEqual(yield* deliveryRow(sql, deliveryId), {
              outcome: "dispatch_failed",
              error: AgentSlotRoutes.UNREADABLE_WHILE_WAITING,
            });
            assert.deepEqual(yield* pendingStates(sql, task.id), []);
            // The delivery behind it is no longer held: it ends as upstream ends a delivery
            // whose task cannot be read, not left waiting.
            assert.equal((yield* deliveryRow(sql, next.deliveryId)).outcome, "dispatch_failed");
            assert.equal(lines(UNREADABLE_WARNING), 1);
            const skipped = logs
              .map((entry) => Logger.formatLogFmt.log(entry))
              .filter((line) => SKIPPED_WARNING.test(line));
            assert.equal(skipped.length, 1);
            assert.include(skipped[0], "title");
            assert.notInclude(skipped[0], "prompt");
            // Readable again: the next delivery runs, once.
            yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN title_away TO title`;
            const after = yield* service.triggerWebhook(webhookRequest(task));
            if (after._tag !== "accepted") return assert.fail(`not accepted: ${after._tag}`);
            yield* until(() => started.length === 1, "the next delivery started");
            yield* retries(3);
            assert.equal(started.length, 1);
            assert.include(prompts[0]!.text, "Review the open pull requests.");
            assert.equal((yield* rowOf(sql, task.id)).run_count, 1);
          }),
        { slots },
      ).pipe(
        Effect.provide(
          Logger.layer([Logger.make((entry) => logs.push(entry))], { mergeWithExisting: false }),
        ),
      );
    },
  );

  it.effect(
    "a waiting delivery whose task's row is gone while its pause check cannot read ends at the next retry as deleted (#763)",
    () => {
      const slots = slotWorld([BUSY]);
      return withService(
        (harness) =>
          Effect.gen(function* () {
            const { sql, started } = harness;
            const { task, deliveryId } = yield* waitingDelivery(harness);
            // `webhookGone` cannot read (a column it reads is gone), and the row is gone: the
            // compare's read finds no row, so it ends as deleted, not as unreadable.
            yield* sql`DELETE FROM scheduled_tasks WHERE task_id = ${task.id}`;
            yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN enabled TO enabled_away`;
            slots.running = [];
            yield* retries(1);
            assert.deepEqual(started, []);
            assert.deepEqual(yield* deliveryRow(sql, deliveryId), {
              outcome: "dispatch_failed",
              error: AgentSlotRoutes.DELETED_WHILE_WAITING,
            });
          }),
        { slots },
      );
    },
  );

  it.effect(
    "a host asleep between two unreadable retries does not use up the limit: it counts retries (#763)",
    () => {
      const slots = slotWorld([BUSY]);
      return withHostSleep((sleep) =>
        withService(
          (harness) =>
            Effect.gen(function* () {
              const { sql, started, webhookWorker } = harness;
              const { task, deliveryId } = yield* waitingDelivery(harness);
              yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN prompt TO prompt_away`;
              yield* retries(1);
              // The host sleeps longer than the limit's 5 minutes, then the next retry fails too.
              sleep(Duration.toMillis(Duration.minutes(10)));
              yield* retries(LIMIT_RETRIES - 2);
              assert.deepEqual(started, []);
              assert.equal((yield* deliveryRow(sql, deliveryId)).outcome, "accepted");
              // Readable with a slot free: it runs, once.
              yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN prompt_away TO prompt`;
              slots.running = [];
              yield* until(() => started.length === 1, "the delivery started");
              yield* workerRounds(webhookWorker, 3);
              assert.equal(started.length, 1);
              assert.equal((yield* rowOf(sql, task.id)).run_count, 1);
              assert.deepEqual(yield* pendingStates(sql, task.id), []);
            }),
          { slots },
        ),
      );
    },
  );

  it.effect(
    "a task unreadable twice, with a good read between, waits on: the limit counts one stretch, each warned (#756)",
    () => {
      const slots = slotWorld([BUSY]);
      const logs: Array<Logger.Options<unknown>> = [];
      const lines = (pattern: RegExp) =>
        logs.filter((entry) => pattern.test(Logger.formatLogFmt.log(entry))).length;
      return withService(
        (harness) =>
          Effect.gen(function* () {
            const { sql, started, webhookWorker } = harness;
            const { task, deliveryId } = yield* waitingDelivery(harness);
            const stretch = LIMIT_RETRIES - 4;
            // Unreadable most of the limit, readable for one retry (the slot still full), then
            // unreadable most of the limit again: more than the limit in all, never in one go.
            yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN prompt TO prompt_away`;
            yield* retries(stretch);
            yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN prompt_away TO prompt`;
            yield* retries(1);
            assert.equal(lines(/can read its task again/), 1);
            yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN prompt TO prompt_away`;
            yield* retries(stretch);
            assert.equal(lines(UNREADABLE_WARNING), 2);
            assert.deepEqual(started, []);
            assert.deepEqual(yield* pendingStates(sql, task.id), [
              { delivery_id: deliveryId, state: "dispatching" },
            ]);
            assert.equal((yield* deliveryRow(sql, deliveryId)).outcome, "accepted");
            // Readable and a slot free: it runs, once.
            yield* sql`ALTER TABLE scheduled_tasks RENAME COLUMN prompt_away TO prompt`;
            slots.running = [];
            yield* until(() => started.length === 1, "the delivery started");
            yield* workerRounds(webhookWorker, 3);
            assert.equal(started.length, 1);
            assert.equal((yield* rowOf(sql, task.id)).run_count, 1);
            assert.deepEqual(yield* pendingStates(sql, task.id), []);
          }),
        { slots },
      ).pipe(
        Effect.provide(
          Logger.layer([Logger.make((entry) => logs.push(entry))], { mergeWithExisting: false }),
        ),
      );
    },
  );

  it.effect.each([
    { name: "a slot free", full: false },
    { name: "a wait after it", full: true },
  ])(
    "a save between the stored-delivery worker's read and the run's is seen, with $name (#750)",
    ({ full }) => {
      const slots = slotWorld([BUSY]);
      return withService(
        ({ service, sql, started, prompts, webhookWorker }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(
              yield* taskInput({ schedule: { type: "webhook" } }),
            );
            const accepted = yield* service.triggerWebhook(webhookRequest(task));
            if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
            yield* settle;
            assert.deepEqual(yield* pendingStates(sql, task.id), [
              { delivery_id: accepted.deliveryId, state: "dispatching" },
            ]);
            // The worker writes the delivery log right after its read of the task, before the
            // run reads it: a save lands there (once), as an owner's save could.
            // (A trigger takes no bound values: the id is written into it.)
            yield* sql.unsafe(`CREATE TRIGGER save_between AFTER UPDATE OF rendered_prompt
              ON scheduled_task_webhook_deliveries BEGIN
                UPDATE scheduled_tasks SET prompt = 'Read the new issues.'
                WHERE task_id = '${task.id.replaceAll("'", "''")}'
                  AND prompt <> 'Read the new issues.';
              END`);
            if (!full) slots.running = [];
            yield* workerRounds(webhookWorker, 3);
            if (full) {
              assert.deepEqual(started, []);
              slots.running = [];
            }
            yield* until(() => started.length === 1, "the delivery started");
            yield* workerRounds(webhookWorker, 3);
            assert.equal(started.length, 1);
            assert.include(prompts[0]!.text, "Read the new issues.");
            assert.notInclude(prompts[0]!.text, "Review the open pull requests.");
            assert.equal((yield* rowOf(sql, task.id)).run_count, 1);
            assert.deepEqual(yield* pendingStates(sql, task.id), []);
            const { delivery } = yield* service.getWebhookDelivery({
              id: task.id,
              deliveryId: accepted.deliveryId,
            } as never);
            assert.equal(delivery.outcome, "accepted");
            assert.include(delivery.renderedPrompt ?? "", "Read the new issues.");
          }),
        { slots },
      );
    },
  );

  it.effect(
    "a delivery already sent (`running`) and then saved keeps the prompt it was sent with (#750)",
    () => {
      const slots = slotWorld([BUSY]);
      return withService(
        ({ service, sql, started, prompts, webhookWorker }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const input = yield* taskInput({ schedule: { type: "webhook" } });
            const { task } = yield* service.upsert(input);
            const accepted = yield* service.triggerWebhook(webhookRequest(task));
            if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
            yield* settle;
            // Stored, then sent before a stop (its row says `running`), then the task is saved:
            // the worker's recovery sends it again with the prompt it was sent with (the same
            // command id), which is not a copy a save overtook, so it is not handed back.
            yield* sql`UPDATE scheduled_task_webhook_pending SET state = 'running'
              WHERE delivery_id = ${accepted.deliveryId}`;
            yield* service.upsert(
              yield* taskInput({ ...input, id: task.id, prompt: "Read the new issues." }),
            );
            slots.running = [];
            yield* workerRounds(webhookWorker, 3);
            assert.equal(started.length, 1);
            assert.include(prompts[0]!.text, "Review the open pull requests.");
            assert.deepEqual(yield* pendingStates(sql, task.id), []);
          }),
        { slots },
      );
    },
  );

  it.effect(
    "a direct delivery is not handed to the worker for a stored one's prompt (#750)",
    () => {
      const slots = slotWorld([BUSY]);
      return withService(
        ({ service, sql, started, prompts, gate }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(
              yield* taskInput({ schedule: { type: "webhook" } }),
            );
            // A stored delivery the worker has not taken yet, rendered from an older prompt.
            const first = yield* service.triggerWebhook(webhookRequest(task));
            if (first._tag !== "accepted") return assert.fail(`not accepted: ${first._tag}`);
            yield* settle;
            yield* sql`UPDATE scheduled_task_webhook_pending
              SET task_json = json_set(task_json, '$.prompt', 'Older words.')
              WHERE delivery_id = ${first.deliveryId}`;
            // Out of the line, or it would go first (oldest): the worker no longer asks for it.
            yield* Option.getOrThrow(gate).leave([`webhook-delivery:${first.deliveryId}`]);
            // A direct delivery with a slot free runs at once; the stored row is not its own.
            slots.running = [];
            const direct = yield* service.triggerWebhook(webhookRequest(task, '{"n":2}'));
            if (direct._tag !== "accepted") return assert.fail(`not accepted: ${direct._tag}`);
            yield* settle;
            assert.equal(started.length, 1);
            assert.include(prompts[0]!.text, "Review the open pull requests.");
            // The stored one is still the worker's, untouched.
            assert.deepEqual(yield* pendingStates(sql, task.id), [
              { delivery_id: first.deliveryId, state: "dispatching" },
            ]);
          }),
        { slots },
      );
    },
  );

  it.effect.each([
    { name: "stored then", storable: true },
    { name: "still not storable", storable: false },
  ])(
    "a direct delivery waiting in memory (its store failed) whose task is saved: $name",
    ({ storable }) => {
      const slots = slotWorld([BUSY]);
      return withService(
        ({ service, sql, started, prompts, webhookWorker }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const input = yield* taskInput({ schedule: { type: "webhook" } });
            const { task } = yield* service.upsert(input);
            yield* sql`CREATE TRIGGER no_store BEFORE INSERT ON scheduled_task_webhook_pending
              BEGIN SELECT RAISE(ABORT, 'store refused'); END`;
            const accepted = yield* service.triggerWebhook(webhookRequest(task));
            if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
            yield* settle;
            // Not stored: it waits in memory.
            assert.deepEqual(yield* pendingStates(sql, task.id), []);
            yield* TestClock.adjust("1 minute");
            yield* service.upsert(yield* taskInput({ ...input, id: task.id, prompt: "Triage." }));
            if (storable) yield* sql`DROP TRIGGER no_store`;
            yield* TestClock.adjust(AgentSlotRoutes.WEBHOOK_SLOT_RETRY);
            yield* settle;
            if (storable) {
              // Stored now, for the worker, which runs it on the task as saved, once.
              assert.deepEqual(yield* pendingStates(sql, task.id), [
                { delivery_id: accepted.deliveryId, state: "dispatching" },
              ]);
              yield* workerRounds(webhookWorker, 2);
              assert.deepEqual(started, []);
              slots.running = [];
              yield* workerRounds(webhookWorker, 4);
              assert.equal(started.length, 1);
              assert.include(prompts[0]!.text, "Triage.");
              return;
            }
            // Its prompt is the old one and it cannot be handed on: not run, and it says why.
            const { delivery } = yield* service.getWebhookDelivery({
              id: task.id,
              deliveryId: accepted.deliveryId,
            });
            assert.equal(delivery.outcome, "dispatch_failed");
            assert.equal(delivery.error, AgentSlotRoutes.SAVED_WHILE_WAITING_NOT_STORED);
            slots.running = [];
            yield* TestClock.adjust(AgentSlotRoutes.WEBHOOK_SLOT_RETRY);
            yield* workerRounds(webhookWorker, 3);
            assert.deepEqual(started, []);
          }),
        { slots },
      );
    },
  );

  it.effect(
    "a delivery saved while it waits, then a restart: the new server runs it as saved",
    () =>
      Effect.gen(function* () {
        const started: Array<string> = [];
        const prompts: Array<Prompt> = [];
        const ids = yield* boot(
          (harness) =>
            Effect.gen(function* () {
              const { task, deliveryId, save } = yield* waitingDelivery(harness);
              yield* TestClock.adjust("1 minute");
              yield* save({ prompt: "Read the new issues." });
              // Stops before or after its wait saw the save: the row is still stored either way.
              yield* workerRounds(harness.webhookWorker, 1);
              return { taskId: task.id, deliveryId };
            }),
          { slots: slotWorld([BUSY]), started, prompts },
        );
        assert.deepEqual(started, []);
        yield* boot(
          ({ sql, service, webhookWorker }) =>
            Effect.gen(function* () {
              yield* webhookWorker;
              yield* until(() => started.length === 1, "the delivery started after the restart");
              yield* workerRounds(webhookWorker, 4);
              assert.equal(started.length, 1);
              assert.include(prompts[0]!.text, "Read the new issues.");
              assert.deepEqual(yield* pendingStates(sql, ids.taskId), []);
              const { delivery } = yield* service.getWebhookDelivery({
                id: ids.taskId,
                deliveryId: ids.deliveryId,
              });
              assert.equal(delivery.outcome, "accepted");
            }),
          { slots: slotWorld(), started, prompts },
        );
      }).pipe(Effect.provide(SqlitePersistence.layerMemory)),
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
    "a stored delivery's worker frees its own place, not one stored for a new task with its id (#941)",
    () => {
      const slots = slotWorld([BUSY]);
      const leaves: Array<ReadonlyArray<string>> = [];
      return withService(
        ({ service, sql, started, prompts, webhookWorker, gate }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(
              yield* taskInput({ schedule: { type: "webhook" } }),
            );
            const first = yield* service.triggerWebhook(webhookRequest(task));
            if (first._tag !== "accepted") return assert.fail(`not accepted: ${first._tag}`);
            // Refused and stored; its worker takes it and waits in its place.
            yield* settle;
            yield* workerRounds(webhookWorker, 1);
            assert.deepEqual(yield* lineOf(gate), [`webhook-delivery:${first.deliveryId}`]);
            // While it waits, its task is deleted and a new one saved with the same id. A delivery
            // for the new task is stored with its own place in line: put in by hand here, as one
            // that comes in as the first one's run ends (until then the id counts as running).
            yield* service.delete({ id: task.id } as never);
            yield* TestClock.adjust("1 second");
            const { task: again } = yield* service.upsert(
              yield* taskInput({
                id: task.id,
                prompt: "Review the new task's pull requests.",
                schedule: { type: "webhook" },
              }),
            );
            const second = "delivery:new-task";
            const request = JSON.stringify({
              method: "POST",
              path: `/api/hooks/${again.id}`,
              query: "",
              headers: { "content-type": "application/json" },
              bodyText: "{}",
            });
            yield* sql`INSERT INTO scheduled_task_webhook_pending
              (delivery_id, task_id, coalesce_key, due_at, state, task_json, request_json, delivery_ids_json)
              VALUES (${second}, ${again.id}, '', ${yield* Clock.currentTimeMillis}, 'dispatching',
                ${JSON.stringify(again)}, ${request}, json_array(${second}))`;
            const asked = yield* Option.getOrThrow(gate).acquire({
              callerThreadId: null,
              targetInstanceId: INSTANCE,
              intoThreadId: undefined,
              place: { key: `webhook-delivery:${second}` },
            });
            assert.equal(asked.type, "refuse");
            assert.deepEqual(yield* lineOf(gate), [
              `webhook-delivery:${first.deliveryId}`,
              `webhook-delivery:${second}`,
            ]);
            // The first delivery's wait sees its task gone and ends; its worker frees its own
            // place only, never the place of the delivery stored after it.
            leaves.length = 0;
            yield* TestClock.adjust(AgentSlotRoutes.WEBHOOK_SLOT_RETRY);
            yield* settle;
            assert.notInclude(leaves.flat(), `webhook-delivery:${second}`);
            assert.include(leaves.flat(), `webhook-delivery:${first.deliveryId}`);
            assert.deepEqual(yield* lineOf(gate), [`webhook-delivery:${second}`]);
            // The second delivery runs once, on the new task, when a slot frees.
            slots.running = [];
            yield* until(() => started.length === 1, "the new task's delivery started");
            assert.include(prompts[0]?.text ?? "", "Review the new task's pull requests.");
            yield* workerRounds(webhookWorker, 2);
            assert.lengthOf(started, 1);
          }),
        { slots, leaves },
      );
    },
  );

  /** How the worker removes a stored delivery before its run, set up after it was stored. */
  const REMOVED_BEFORE_RUN = [
    {
      name: "payload not prepared",
      error: "The pending webhook payload could not be prepared.",
      spoil: (harness: Harness, task: ScheduledTask, deliveryId: string) =>
        harness.sql`UPDATE scheduled_task_webhook_pending SET request_json = 'not json'
          WHERE delivery_id = ${deliveryId} AND task_id = ${task.id}`.pipe(Effect.asVoid),
    },
    {
      name: "prompt too long",
      error: "The filled-in prompt is too long.",
      // The owner saves a prompt too long to send while it waits: the worker renders that one.
      spoil: (harness: Harness, task: ScheduledTask) =>
        harness.service
          .upsert({ ...task, prompt: "x".repeat(PROVIDER_SEND_TURN_MAX_INPUT_CHARS + 1) } as never)
          .pipe(Effect.asVoid),
    },
  ] as const;

  it.effect.each(REMOVED_BEFORE_RUN)(
    "$name: a stored delivery its worker removes before its run leaves its place at once (#941)",
    ({ spoil, error }) => {
      const slots = slotWorld([BUSY]);
      return withService(
        (harness) =>
          Effect.gen(function* () {
            const { service, sql, started, webhookWorker, gate } = harness;
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(
              yield* taskInput({ schedule: { type: "webhook" } }),
            );
            const accepted = yield* service.triggerWebhook(webhookRequest(task));
            if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
            yield* settle;
            // Refused and stored: its place from that ask waits for the worker.
            assert.deepEqual(yield* lineOf(gate), [`webhook-delivery:${accepted.deliveryId}`]);
            yield* spoil(harness, task, accepted.deliveryId);
            // The worker removes it without a run; its place goes with it, not at the sweep
            // (no sweep runs here).
            yield* webhookWorker;
            yield* settle;
            assert.deepEqual(yield* pendingStates(sql, task.id), []);
            const [ended] = yield* sql<{ outcome: string; error: string | null }>`
              SELECT outcome, error FROM scheduled_task_webhook_deliveries
              WHERE delivery_id = ${accepted.deliveryId}`;
            assert.equal(ended?.outcome, "dispatch_failed");
            assert.include(ended?.error ?? "", error);
            assert.deepEqual(yield* lineOf(gate), []);
            // A start that asks next takes the slot as it frees: nothing ended holds it back.
            slots.running = [];
            assert.equal((yield* laterWaiter(gate)).type, "allow");
            assert.deepEqual(started, []);
          }),
        { slots },
      );
    },
  );

  it.effect(
    "a stored delivery is ahead of a waiter that joined after it was stored, though the worker asks after it",
    () => {
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
            yield* settle;
            // A restart: the line is memory only; the stored row keeps its due time.
            yield* Option.getOrThrow(gate).leave([`webhook-delivery:${accepted.deliveryId}`]);
            yield* TestClock.adjust("1 second");
            assert.equal((yield* laterWaiter(gate)).type, "refuse");
            yield* TestClock.adjust("1 second");
            yield* webhookWorker;
            yield* settle;
            assert.deepEqual(yield* lineOf(gate), [
              `webhook-delivery:${accepted.deliveryId}`,
              "later-waiter",
            ]);
            slots.running = [];
            assert.equal((yield* laterWaiter(gate)).type, "refuse");
            yield* until(() => started.length === 1, "the delivery started");
          }),
        { slots },
      );
    },
  );

  it.effect("a direct delivery waiting in memory keeps its place through the 5 s sweep", () => {
    const slots = slotWorld([BUSY]);
    return withService(
      ({ service, sql, started, notesWorker, gate }) =>
        Effect.gen(function* () {
          yield* TestClock.setTime(START);
          const { task } = yield* service.upsert(
            yield* taskInput({ schedule: { type: "webhook" } }),
          );
          // A row with its id is there already, not pending: it is not stored, it waits in memory.
          const deliveryId = "delivery:relay:held";
          yield* sql`INSERT INTO scheduled_task_webhook_pending
            (delivery_id, task_id, coalesce_key, due_at, state, task_json, request_json, delivery_ids_json)
            VALUES (${deliveryId}, ${task.id}, '', ${START}, 'running', '{}', '{}', '[]')`;
          const accepted = yield* service.triggerWebhook({
            ...webhookRequest(task),
            relayDeliveryId: "held",
          });
          if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
          assert.equal(accepted.deliveryId, deliveryId);
          yield* settle;
          yield* TestClock.adjust("1 second");
          assert.equal((yield* laterWaiter(gate)).type, "refuse");
          // The sweep finds no pending row for it, but it still waits: its place stays.
          yield* notesWorker;
          assert.deepEqual(yield* lineOf(gate), [`webhook-delivery:${deliveryId}`, "later-waiter"]);
          slots.running = [];
          assert.equal((yield* laterWaiter(gate)).type, "refuse");
          yield* until(() => started.length === 1, "the delivery started");
        }),
      { slots },
    );
  });

  it.effect("a stored delivery whose row cannot be read is logged and still keeps a place", () => {
    const slots = slotWorld([BUSY]);
    const readFails = { value: false };
    const warnings: Array<string> = [];
    const logger = Logger.make(({ message, logLevel }) => {
      if (logLevel === "Warn") warnings.push(JSON.stringify(message));
    });
    return boot(
      ({ service, started, webhookWorker, gate }) =>
        Effect.gen(function* () {
          yield* TestClock.setTime(START);
          const { task } = yield* service.upsert(
            yield* taskInput({ schedule: { type: "webhook" } }),
          );
          const accepted = yield* service.triggerWebhook(webhookRequest(task));
          if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
          yield* settle;
          // A restart, then the worker's read of the row's place fails: its task's place, now.
          yield* Option.getOrThrow(gate).leave([`webhook-delivery:${accepted.deliveryId}`]);
          readFails.value = true;
          yield* TestClock.adjust("1 second");
          yield* webhookWorker;
          yield* settle;
          assert.lengthOf(
            warnings.filter((warning) => warning.includes("place in line")),
            1,
          );
          assert.deepEqual(yield* lineOf(gate), [`webhook-task:${task.id}`]);
          assert.equal((yield* laterWaiter(gate)).type, "refuse");
          slots.running = [];
          assert.equal((yield* laterWaiter(gate)).type, "refuse");
          yield* until(() => started.length === 1, "the delivery started");
          assert.deepEqual(yield* lineOf(gate), ["later-waiter"]);
        }),
      { slots },
    ).pipe(
      Effect.updateService(
        SqlClient.SqlClient,
        (sql) =>
          new Proxy(sql, {
            apply: (target, self, args: Array<unknown>) =>
              readFails.value &&
              Array.isArray(args[0]) &&
              args[0].join("?").includes("SELECT delivery_id, due_at FROM")
                ? Effect.fail(
                    new SqlError.SqlError({
                      reason: new SqlError.LockTimeoutError({ cause: null }),
                    }),
                  )
                : Reflect.apply(target, self, args),
          }),
      ),
      Effect.provide(
        Layer.mergeAll(
          SqlitePersistence.layerMemory,
          Logger.layer([logger], { mergeWithExisting: true }),
        ),
      ),
    );
  });

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
