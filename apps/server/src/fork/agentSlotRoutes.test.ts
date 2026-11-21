/**
 * Fork: scheduled runs through the agent slot gate, on upstream's real scheduled task
 * service and the server's real gate (`slotWorld.testkit.ts`). `create_threads` is covered in
 * `mcp/OrchestratorMcpService.slotGate.test.ts`, `t3_thread_launch` in `launchMcpProfile.test.ts`.
 */
import * as NodePlatformCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  type OrchestrationV2ThreadShell,
  ProviderInstanceId,
  ScheduledTaskUpsertInput,
  ThreadId,
} from "@t3tools/contracts";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";
import * as TestClock from "effect/testing/TestClock";
import * as Tracer from "effect/Tracer";

import * as ThreadLaunchService from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ManagedProjectFolders from "../project/ManagedProjectFolders.ts";
import * as ScheduledTaskService from "../scheduledTasks/ScheduledTaskService.ts";
import * as Scheduler from "../scheduling/Scheduler.ts";
import * as SecretRequests from "../secrets/SecretRequests.ts";
import * as ServerSettings from "../serverSettings.ts";
import { skippedRunError } from "./agentSlotRoutes.ts";
import { type SlotWorld, slotGateOver, slotWorld } from "./slotWorld.testkit.ts";

const INSTANCE = ProviderInstanceId.make("codex");
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

interface Harness {
  readonly service: ScheduledTaskService.ScheduledTaskService["Service"];
  readonly sql: SqlClient.SqlClient;
  /** Every thread launched and every message sent to a bound thread. */
  readonly started: Array<string>;
  /** One pass of the due-task poll, as the scheduler runs it. */
  readonly poll: Effect.Effect<void>;
  readonly webhookWorker: Effect.Effect<void>;
}

/**
 * Upstream's scheduled task service on a fresh store, its scheduler's work run by hand. With
 * `slots`, the server's gate runs over that world on `settings`; without, the host runs none.
 */
const withService = <A, E>(
  body: (harness: Harness) => Effect.Effect<A, E>,
  options: { readonly slots?: SlotWorld; readonly settings?: object } = {},
) =>
  Effect.gen(function* () {
    const started: Array<string> = [];
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
          Effect.succeed({
            id: threadId,
            projectId: "project-slots",
            modelSelection: { instanceId: INSTANCE, model: "gpt-5.4" },
            archivedAt: null,
            deletedAt: null,
          } as unknown as OrchestrationV2ThreadShell),
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
      return yield* body({
        service,
        sql,
        started,
        poll: Effect.suspend(() => workers.get("scheduled-tasks")!),
        webhookWorker: Effect.suspend(() => workers.get("webhook-coalescing")!),
      });
    }).pipe(
      Effect.provide(Layer.fresh(ScheduledTaskService.layer.pipe(Layer.provide(dependencies)))),
    );
  }).pipe(Effect.provide(SqlitePersistence.layerMemory));

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

const ranAt = (row: { readonly next_run_at: string | null }) => row.next_run_at;

describe("scheduled runs through the agent slot gate (#706)", () => {
  it.effect(
    "full: a run on its schedule is recorded as skipped, its next time as after a run",
    () =>
      Effect.gen(function* () {
        // The same task, run once with a slot free, gives the next time a run leaves.
        const afterRun = yield* withService(
          ({ service, sql, poll, started }) =>
            Effect.gen(function* () {
              const task = yield* dueTask(service);
              yield* poll;
              assert.deepEqual(started.length, 1);
              return ranAt(yield* rowOf(sql, task.id));
            }),
          { slots: slotWorld() },
        );
        const slots = slotWorld([BUSY]);
        yield* withService(
          ({ service, sql, poll, started }) =>
            Effect.gen(function* () {
              const task = yield* dueTask(service);
              yield* poll;
              assert.deepEqual(started, []);
              const row = yield* rowOf(sql, task.id);
              assert.equal(row.last_run_status, "failed");
              assert.match(row.last_run_error ?? "", /^Skipped: /);
              assert.equal(row.run_count, 0);
              assert.equal(row.last_run_at, null);
              assert.equal(row.next_run_at, afterRun);
              // Shown where the task's runs are: the list the settings page reads.
              const listed = (yield* service.list()).tasks.find((entry) => entry.id === task.id)!;
              assert.equal(listed.lastRunStatus, "failed");
              assert.equal(listed.lastRunError, row.last_run_error);
              assert.equal(listed.nextRunAt, afterRun);
              // Runs again (the next poll, same time): not due any more, so nothing more happens.
              yield* poll;
              assert.deepEqual(yield* rowOf(sql, task.id), row);
              assert.deepEqual(started, []);
            }),
          { slots },
        );
      }),
  );

  it.effect("the skip says why: the gate's own refusal text", () =>
    withService(
      ({ service, sql, poll }) =>
        Effect.gen(function* () {
          const task = yield* dueTask(service);
          yield* poll;
          const refused = yield* service.runNow({ id: task.id }).pipe(Effect.flip);
          const row = yield* rowOf(sql, task.id);
          assert.equal(row.last_run_error, skippedRunError(refused.message));
        }),
      { slots: slotWorld([BUSY]) },
    ),
  );

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

  it.effect("full: run now fails with the reason and writes nothing; free: it runs", () =>
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
              assert.isTrue(refused.message.length > 0);
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
    }),
  );

  it.effect("a task bound to a thread is counted on that thread: full refuses, free sends", () =>
    Effect.gen(function* () {
      const slots = slotWorld([BUSY]);
      yield* withService(
        ({ service, sql, started }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(yield* taskInput({ threadId: BOUND }));
            yield* service.runNow({ id: task.id }).pipe(Effect.flip);
            assert.deepEqual(started, []);
            assert.equal((yield* rowOf(sql, task.id)).run_count, 0);
            slots.running = [];
            yield* service.runNow({ id: task.id });
            assert.deepEqual(started, [`send:${BOUND}`]);
          }),
        { slots },
      );
    }),
  );

  it.effect("a count that cannot be read skips the run (fails closed)", () =>
    withService(
      ({ service, sql, poll, started }) =>
        Effect.gen(function* () {
          const task = yield* dueTask(service);
          yield* poll;
          assert.deepEqual(started, []);
          assert.match((yield* rowOf(sql, task.id)).last_run_error ?? "", /^Skipped: /);
        }),
      { slots: { ...slotWorld(), fails: true } },
    ),
  );

  it.effect("full: a webhook delivery is marked skipped with the reason, nothing started", () =>
    Effect.gen(function* () {
      const released = yield* Deferred.make<void>();
      const tracer = Tracer.make({
        span: (options) => {
          const span = new Tracer.NativeSpan(options);
          const end = span.end.bind(span);
          span.end = (...args) => {
            end(...args);
            if (span.name === "ScheduledTaskService.releaseFailedDelivery")
              Deferred.doneUnsafe(released, Effect.void);
          };
          return span;
        },
      });
      yield* withService(
        ({ service, sql, started }) =>
          Effect.gen(function* () {
            yield* TestClock.setTime(START);
            const { task } = yield* service.upsert(
              yield* taskInput({ schedule: { type: "webhook" } }),
            );
            const body = new TextEncoder().encode("{}");
            const accepted = yield* service.triggerWebhook({
              hookId: task.id,
              token: task.webhook?.path.split("/").at(-1) ?? "",
              method: "POST",
              path: `/api/hooks/${task.id}`,
              query: "",
              headers: { "content-type": "application/json" },
              body,
              bodyText: "{}",
            });
            if (accepted._tag !== "accepted") return assert.fail(`not accepted: ${accepted._tag}`);
            yield* Deferred.await(released);
            const { delivery } = yield* service.getWebhookDelivery({
              id: task.id,
              deliveryId: accepted.deliveryId,
            });
            assert.equal(delivery.outcome, "dispatch_failed");
            assert.isTrue((delivery.error ?? "").length > 0);
            assert.deepEqual(started, []);
            assert.equal((yield* rowOf(sql, task.id)).run_count, 0);
          }),
        { slots: slotWorld([BUSY]) },
      ).pipe(Effect.withTracer(tracer));
    }),
  );

  it.effect("off, or a host that runs no gate: runs as before, nothing read", () =>
    Effect.gen(function* () {
      const slots = slotWorld([BUSY]);
      yield* withService(
        ({ service, poll, started }) =>
          Effect.gen(function* () {
            yield* dueTask(service);
            yield* poll;
            assert.deepEqual(started.length, 1);
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

  it.effect("an old store: a task saved before the gate, already failed once, is skipped", () =>
    withService(
      ({ service, sql, poll, started }) =>
        Effect.gen(function* () {
          const task = yield* dueTask(service);
          // As an older server left it: a failed run on record, no reason the gate wrote.
          yield* sql`UPDATE scheduled_tasks SET last_run_status = 'failed',
            last_run_error = 'Launch failed.', run_count = 3 WHERE task_id = ${task.id}`;
          yield* poll;
          assert.deepEqual(started, []);
          const row = yield* rowOf(sql, task.id);
          assert.match(row.last_run_error ?? "", /^Skipped: /);
          assert.equal(row.run_count, 3);
        }),
      { slots: slotWorld([BUSY]) },
    ),
  );
});
