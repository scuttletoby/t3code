/**
 * Fork: the handoff service against mocked stores: when it sends, what it sends, and what it does
 * after a handoff run ends. The busy check (`dispatchHandoff`) is tested against the real
 * orchestrator in `handoffBusyCheck.test.ts`.
 */
import { describe, expect, it } from "@effect/vitest";
import {
  DEFAULT_HANDOFF_SETTINGS,
  DEFAULT_SERVER_SETTINGS,
  EventId,
  type HandoffSettings,
  MessageId,
  type OrchestrationV2ServerCommand,
  type OrchestrationV2DomainEvent,
  type OrchestrationV2Run,
  type OrchestrationV2ThreadProjection,
  RunId,
  RuntimeRequestId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Queue from "effect/Queue";
import * as Stream from "effect/Stream";
import * as Tracer from "effect/Tracer";
import * as TestClock from "effect/testing/TestClock";

import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ServerSettings from "../../serverSettings.ts";
import * as HandoffService from "./HandoffService.ts";
import {
  at,
  handoffMessageId,
  MINUTE_MS,
  modelSelection,
  NOW_MS,
  projection,
  run,
  runId,
  threadId,
} from "./handoff.testkit.ts";
import { HANDOFF_RULES } from "./handoffMessage.ts";

const runUpdated = (payload: OrchestrationV2Run): OrchestrationV2DomainEvent => ({
  id: EventId.make(`event:${payload.id}:${payload.status}`),
  type: "run.updated",
  threadId,
  runId: payload.id,
  occurredAt: at(),
  payload,
});

let changes = 0;
/** Any other event of the thread: it wakes a waiting handoff. */
const threadChanged = (data: OrchestrationV2ThreadProjection) =>
  ({
    id: EventId.make(`event:changed:${(changes += 1)}`),
    type: "thread.metadata-updated",
    threadId,
    occurredAt: at(),
    payload: data.thread,
  }) as OrchestrationV2DomainEvent;

/**
 * Runs the service with mocked stores. `data` and `settings` are read at every call, so a test may
 * change them between events. Returns the commands sent and a way to wait for a number of reads.
 */
const harness = (state: {
  data: OrchestrationV2ThreadProjection;
  settings: Partial<HandoffSettings>;
}) =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW_MS);
    const events = yield* Queue.unbounded<OrchestrationV2DomainEvent>();
    const sent: Array<OrchestrationV2ServerCommand> = [];
    const reads = { count: 0 };
    const ended = new Map<string, number>();
    const milestones: Array<{
      readonly name: string;
      readonly count: number;
      readonly done: Deferred.Deferred<void>;
    }> = [];
    const tracer = Tracer.make({
      span(options) {
        const span = new Tracer.NativeSpan(options);
        const end = span.end.bind(span);
        span.end = (...args) => {
          end(...args);
          ended.set(span.name, (ended.get(span.name) ?? 0) + 1);
          for (const milestone of milestones) {
            if ((ended.get(milestone.name) ?? 0) >= milestone.count)
              Deferred.doneUnsafe(milestone.done, Effect.void);
          }
        };
        return span;
      },
    });
    const dependencies = Layer.mergeAll(
      Layer.mock(Orchestrator.OrchestratorV2)({
        streamDomainEvents: Stream.fromQueue(events),
        dispatch: (command) =>
          Effect.sync(() => {
            sent.push(command);
            return { sequence: sent.length, storedEvents: [] };
          }),
      }),
      Layer.mock(ProjectionStore.ProjectionStoreV2)({
        getThread: () => Effect.succeed(state.data.thread),
        getThreadRecords: () =>
          Effect.sync(() => {
            reads.count++;
            return state.data;
          }),
      }),
      Layer.mock(ServerSettings.ServerSettingsService)({
        getSettings: Effect.sync(() => ({
          ...DEFAULT_SERVER_SETTINGS,
          handoff: { ...DEFAULT_HANDOFF_SETTINGS, ...state.settings },
        })),
      }),
    );
    yield* HandoffService.make.pipe(
      Effect.flatMap((service) => service.start()),
      Effect.provide(dependencies),
      Effect.withTracer(tracer),
    );
    /** Offers an event, then waits until the span `name` has ended `count` times in all. */
    const emit = (event: OrchestrationV2DomainEvent, name: string, count: number) =>
      Effect.gen(function* () {
        const done = yield* Deferred.make<void>();
        milestones.push({ name: `HandoffService.${name}`, count, done });
        if ((ended.get(`HandoffService.${name}`) ?? 0) >= count)
          yield* Deferred.succeed(done, undefined);
        yield* Queue.offer(events, event);
        yield* Deferred.await(done);
      });
    return { sent, emit, reads };
  });

const on = { mode: "handoff" } as const;
const messages = (sent: ReadonlyArray<OrchestrationV2ServerCommand>) =>
  sent.flatMap((command) => (command.type === "message.dispatch" ? [command] : []));

describe("HandoffService", () => {
  it.effect("default off reads no records and sends nothing", () =>
    Effect.gen(function* () {
      const state = { data: projection({ usedTokens: 400_000 }), settings: {} };
      const { sent, emit, reads } = yield* harness(state);
      yield* emit(runUpdated(run()), "handOffWhenDue", 1);
      expect(sent).toEqual([]);
      expect(reads.count).toBe(0);
    }),
  );

  it.effect("hands off at the size backstop with the model and effort written out", () =>
    Effect.gen(function* () {
      const chosen = { ...modelSelection, options: [{ id: "effort", value: "high" }] };
      const state = {
        data: projection({ usedTokens: 160_000 }),
        settings: { ...on, modelSelection: chosen },
      };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(run()), "handOffWhenDue", 1);
      const [message, ...rest] = messages(sent);
      expect(rest).toEqual([]);
      expect(message).toMatchObject({
        commandId: `server:handoff:${threadId}:${runId}`,
        messageId: handoffMessageId,
        createdBy: "agent",
        creationSource: "server",
        freshContext: true,
        modelSelection: chosen,
        dispatchMode: { type: "start_immediately" },
      });
      expect(message?.text).toContain(`[T3 handoff 1] Thread ${threadId}, made from run ${runId}`);
      expect(message?.text).toContain("effort high");
      expect(message?.text).toContain("No handoff source is set up yet");
      expect(message?.text).toContain("Recent conversation:");
      expect(message?.text.endsWith(HANDOFF_RULES)).toBe(true);
    }),
  );

  it.effect("without a model setting uses the thread's model and warns there is no effort", () =>
    Effect.gen(function* () {
      const state = { data: projection({ usedTokens: 160_000 }), settings: on };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(run()), "handOffWhenDue", 1);
      expect(messages(sent)[0]?.modelSelection).toEqual(modelSelection);
      expect(messages(sent)[0]?.text).toContain("no effort set (warning)");
    }),
  );

  it.effect("two triggers for one run send the same ids, so only one handoff runs", () =>
    Effect.gen(function* () {
      const state = { data: projection({ usedTokens: 160_000 }), settings: on };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(run()), "handOffWhenDue", 1);
      yield* emit(runUpdated(run()), "handOffWhenDue", 2);
      expect(new Set(messages(sent).map((message) => message.commandId))).toEqual(
        new Set([`server:handoff:${threadId}:${runId}`]),
      );
    }),
  );

  it.effect("the idle timer sends at the window, and not for an older run", () =>
    Effect.gen(function* () {
      const state = {
        data: projection({ usedTokens: 100_000, cacheExpiresInMs: 10 * MINUTE_MS }),
        settings: on,
      };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(run()), "readThread", 1);
      // The agent source starts 4 minutes before the cache expires: at 6 minutes.
      yield* TestClock.adjust(5 * MINUTE_MS);
      expect(sent).toEqual([]);
      yield* TestClock.adjust(MINUTE_MS);
      expect(messages(sent).map((message) => message.messageId)).toEqual([handoffMessageId]);

      // An older run's timer: a newer run finished meanwhile (its own turn end decides).
      sent.length = 0;
      state.data = projection({ usedTokens: 100_000, cacheExpiresInMs: 20 * MINUTE_MS });
      yield* emit(runUpdated(run()), "readThread", 3);
      state.data = {
        ...state.data,
        runs: [...state.data.runs, run({ id: RunId.make("run-2"), ordinal: 2 })],
      };
      yield* TestClock.adjust(20 * MINUTE_MS);
      expect(sent).toEqual([]);
    }),
  );

  it.effect("waits while the thread is busy and sends when the work ends", () =>
    Effect.gen(function* () {
      const quiet = projection({ usedTokens: 160_000 });
      const busy = {
        ...quiet,
        runtimeRequests: [
          { id: RuntimeRequestId.make("request-1"), status: "pending" },
        ] as unknown as OrchestrationV2ThreadProjection["runtimeRequests"],
      };
      const state = { data: busy, settings: on };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(run()), "readThread", 1);
      expect(sent).toEqual([]);
      // Something else changed, still busy: it reads again and keeps waiting.
      yield* emit(threadChanged(state.data), "readThread", 2);
      expect(sent).toEqual([]);
      state.data = quiet;
      yield* emit(threadChanged(state.data), "handOffWhenDue", 1);
      expect(messages(sent).map((message) => message.messageId)).toEqual([handoffMessageId]);
    }),
  );

  it.effect.each([
    [
      "settings switched off",
      (state: { settings: Partial<HandoffSettings> }) => (state.settings = {}),
    ],
    [
      "thread deleted",
      (state: { data: OrchestrationV2ThreadProjection }) =>
        (state.data = { ...state.data, thread: { ...state.data.thread, deletedAt: at() } }),
    ],
  ] as const)("sends nothing once %s while waiting", ([, change]) =>
    Effect.gen(function* () {
      const quiet = projection({ usedTokens: 160_000 });
      const state = {
        data: {
          ...quiet,
          contextTransfers: [
            { status: "pending" },
          ] as unknown as OrchestrationV2ThreadProjection["contextTransfers"],
        },
        settings: on as Partial<HandoffSettings>,
      };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(run()), "readThread", 1);
      change(state as never);
      state.data = { ...state.data, contextTransfers: [] };
      yield* emit(threadChanged(state.data), "handOffWhenDue", 1);
      expect(sent).toEqual([]);
    }),
  );

  it.effect.each([
    { read: true, visits: 1 },
    { read: false, visits: 0 },
  ])("a completed handoff run keeps a read thread read (read before: $read)", ({ read, visits }) =>
    Effect.gen(function* () {
      const data = projection({ usedTokens: 160_000 });
      const state = {
        data: {
          ...data,
          thread: { ...data.thread, lastVisitedAt: read ? at() : at(-MINUTE_MS) },
        },
        settings: on,
      };
      const { sent, emit } = yield* harness(state);
      const handoff = run({
        id: RunId.make("run-2"),
        ordinal: 2,
        userMessageId: handoffMessageId,
        completedAt: at(MINUTE_MS),
      });
      yield* emit(runUpdated(handoff), "handoffRunEnded", 1);
      const visitsSent = sent.filter((command) => command.type === "thread.visit");
      expect(visitsSent.length).toBe(visits);
      if (read) {
        expect(visitsSent[0]).toMatchObject({
          commandId: `server:handoff-read:${threadId}:run-2`,
          visitedAt: DateTime.formatIso(at(MINUTE_MS)),
        });
      }
    }),
  );

  it.effect("a failed handoff run is sent once more with the same text, and only once", () =>
    Effect.gen(function* () {
      const data = projection({ usedTokens: 160_000 });
      const text = "[T3 handoff 1] the first text";
      const failed = run({
        id: RunId.make("run-2"),
        ordinal: 2,
        userMessageId: handoffMessageId,
        status: "failed",
      });
      // The failed first send is the latest run, so the busy check lets its retry through.
      const state = {
        data: {
          ...data,
          runs: [...data.runs, failed],
          messages: [{ ...data.messages[0]!, id: handoffMessageId, text }],
        },
        settings: on as Partial<HandoffSettings>,
      };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(failed), "handoffRunEnded", 1);
      expect(messages(sent)).toMatchObject([
        {
          commandId: `server:${handoffMessageId}:retry1`,
          messageId: `${handoffMessageId}:retry1`,
          text,
          freshContext: true,
        },
      ]);
      // The retry fails too: nothing more is sent.
      const retry = run({
        id: RunId.make("run-3"),
        ordinal: 3,
        userMessageId: MessageId.make(`${handoffMessageId}:retry1`),
        status: "interrupted",
      });
      yield* emit(runUpdated(retry), "handoffRunEnded", 2);
      expect(messages(sent).length).toBe(1);
      // Switched off: a failed first send is not retried.
      state.settings = {};
      yield* emit(runUpdated({ ...failed, status: "interrupted" }), "handoffRunEnded", 3);
      expect(messages(sent).length).toBe(1);
    }),
  );
});
