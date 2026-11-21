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
  ProviderDriverKind,
  RunId,
  RuntimeRequestId,
  type ServerSettings as ServerSettingsValue,
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
import * as SlotGate from "../../slots/SlotGate.ts";
import * as HandoffService from "./HandoffService.ts";
import {
  adapterRegistry,
  at,
  awarenessShell,
  handoffMessageId,
  MINUTE_MS,
  modelSelection,
  NOW_MS,
  otherInstanceConfig,
  otherInstanceId,
  projection,
  run,
  runId,
  runnableCases,
  runnableThread,
  runnableSettings,
  threadId,
} from "./handoff.testkit.ts";
import { HANDOFF_RULES } from "./handoffMessage.ts";
import { HANDOFF_SETTLE_MS } from "./handoffQuiet.ts";
import { RELAY_COMPLETION_HOLD_MS } from "./handoffShell.ts";
import { HANDOFF_LOUD_SUFFIX } from "./handoffFacts.ts";

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
  /** The thread's project override (over the defaults), or none. */
  project?: Partial<HandoffSettings> | undefined;
  /** Runs inside the mocked dispatch, after the command is recorded. */
  onDispatch?: (command: OrchestrationV2ServerCommand) => Effect.Effect<void>;
  /** The relay's view: read as the records show it (default), missing, or failing. */
  view?: "records" | "missing" | "fails";
  /** The server's provider instances (none: the built-in ones, on). */
  providerInstances?: ServerSettingsValue["providerInstances"];
  /** Provider instances the thread's project turns on or off, by id. */
  projectProviderInstances?: Record<string, { readonly enabled: boolean }> | undefined;
  /** Instance ids upstream's registry does not have (removed); read at each lookup. */
  missing?: Set<string>;
  /** The server's agent slot gate (none: a server without one). */
  gate?: Layer.Layer<SlotGate.SlotGate>;
}) =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW_MS);
    const events = yield* Queue.unbounded<OrchestrationV2DomainEvent>();
    const sent: Array<OrchestrationV2ServerCommand> = [];
    /** `text`: the reads that build a handoff's text (its transcript part). */
    const reads = { count: 0, text: 0 };
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
        // The relay's view: the thread as its records show it, no live children.
        getAgentAwarenessSnapshot: () =>
          state.view === "fails"
            ? Effect.die(new Error("awareness read failed"))
            : Effect.sync(() => ({
                snapshotSequence: 1,
                threads:
                  state.view === "missing"
                    ? []
                    : [awarenessShell(state.data, { updatedAt: state.data.updatedAt })],
              })),
        listActiveShellIndex: () => Effect.succeed([]),
        dispatch: (command) =>
          Effect.sync(() => sent.push(command)).pipe(
            Effect.andThen(state.onDispatch?.(command) ?? Effect.void),
            Effect.as({ sequence: sent.length, storedEvents: [] }),
          ),
      }),
      Layer.mock(ProjectionStore.ProjectionStoreV2)({
        getThread: () => Effect.succeed(state.data.thread),
        getThreadRecords: (_threadId, fields) =>
          Effect.sync(() => {
            reads.count++;
            if ((fields as ReadonlyArray<string>).includes("attempts")) reads.text++;
            return state.data;
          }),
      }),
      Layer.mock(ServerSettings.ServerSettingsService)({
        getSettings: Effect.sync(() => ({
          ...DEFAULT_SERVER_SETTINGS,
          providerInstances: state.providerInstances ?? {},
          handoff: { ...DEFAULT_HANDOFF_SETTINGS, ...state.settings },
          projectSettingsOverrides:
            state.project === undefined && state.projectProviderInstances === undefined
              ? {}
              : ({
                  [state.data.thread.projectId]: {
                    ...(state.project === undefined
                      ? {}
                      : { handoff: { ...DEFAULT_HANDOFF_SETTINGS, ...state.project } }),
                    ...(state.projectProviderInstances === undefined
                      ? {}
                      : { providerInstances: state.projectProviderInstances }),
                  },
                } as unknown as ServerSettingsValue["projectSettingsOverrides"]),
        })),
      }),
      adapterRegistry(() => state.missing ?? new Set()),
      state.gate ?? Layer.empty,
    );
    yield* HandoffService.make.pipe(
      Effect.flatMap((service) => service.start()),
      Effect.provide(dependencies),
      Effect.withTracer(tracer),
    );
    /** Offers an event, then waits until the span `name` has ended `count` times in all. */
    const emit = (event: OrchestrationV2DomainEvent | null, name: string, count: number) =>
      Effect.gen(function* () {
        const span = name.includes(".") ? name : `HandoffService.${name}`;
        const done = yield* Deferred.make<void>();
        milestones.push({ name: span, count, done });
        if ((ended.get(span) ?? 0) >= count) yield* Deferred.succeed(done, undefined);
        if (event !== null) yield* Queue.offer(events, event);
        yield* Deferred.await(done);
      });
    return { sent, emit, reads, events };
  });

/** For a test that hangs, not fails, when a check is gone: it ends in milliseconds when right. */
const SHORT_TIMEOUT_MS = 5_000;
const on = { mode: "handoff" } as const;
/** The wait at a quiet thread before a send, so the relay's alert for the person's turn goes first. */
const settle = TestClock.adjust(HANDOFF_SETTLE_MS);
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
      yield* emit(runUpdated(run()), "readThread", 1);
      // Not inside the relay's hold of the person's "finished" alert: only after the settle.
      expect(HANDOFF_SETTLE_MS).toBeGreaterThan(RELAY_COMPLETION_HOLD_MS);
      yield* TestClock.adjust(HANDOFF_SETTLE_MS - 1);
      expect(sent).toEqual([]);
      yield* TestClock.adjust(1);
      yield* emit(null, "handOffWhenDue", 1);
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

  it.effect.each(["fails", "missing"] as const)(
    "a relay view that %s sends the handoff as one that alerts, with the same command id",
    (view) =>
      Effect.gen(function* () {
        const state = { data: projection({ usedTokens: 160_000 }), settings: on, view };
        const { sent, emit } = yield* harness(state);
        yield* emit(runUpdated(run()), "readThread", 1);
        yield* settle;
        yield* emit(null, "handOffWhenDue", 1);
        const [message, ...rest] = messages(sent);
        expect(rest).toEqual([]);
        // Unsure whether the person's finish was shown: alert. The command id still dedupes.
        expect(message).toMatchObject({
          commandId: `server:handoff:${threadId}:${runId}`,
          messageId: `${handoffMessageId}${HANDOFF_LOUD_SUFFIX}`,
        });
      }),
  );

  it.effect("without a model setting uses the thread's model and warns there is no effort", () =>
    Effect.gen(function* () {
      const state = { data: projection({ usedTokens: 160_000 }), settings: on };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(run()), "readThread", 1);
      yield* settle;
      yield* emit(null, "handOffWhenDue", 1);
      expect(messages(sent)[0]?.modelSelection).toEqual(modelSelection);
      expect(messages(sent)[0]?.text).toContain("no effort set (warning)");
    }),
  );

  it.effect("two triggers for one run send the same ids, so only one handoff runs", () =>
    Effect.gen(function* () {
      const state = { data: projection({ usedTokens: 160_000 }), settings: on };
      const { sent, emit } = yield* harness(state);
      // Each trigger reads, settles and reads again before it sends.
      yield* emit(runUpdated(run()), "readThread", 1);
      yield* settle;
      yield* emit(null, "handOffWhenDue", 1);
      yield* emit(runUpdated(run()), "readThread", 3);
      yield* settle;
      yield* emit(null, "handOffWhenDue", 2);
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
      expect(sent).toEqual([]);
      yield* settle;
      expect(messages(sent).map((message) => message.messageId)).toEqual([handoffMessageId]);

      // An older run's timer: a newer run finished meanwhile (its own turn end decides).
      sent.length = 0;
      state.data = projection({ usedTokens: 100_000, cacheExpiresInMs: 20 * MINUTE_MS });
      yield* emit(runUpdated(run()), "readThread", 4);
      state.data = {
        ...state.data,
        runs: [...state.data.runs, run({ id: RunId.make("run-2"), ordinal: 2 })],
      };
      yield* TestClock.adjust(20 * MINUTE_MS);
      expect(sent).toEqual([]);
    }),
  );

  it.effect("waits while the thread is busy and sends a settle after the work ends", () =>
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
      yield* TestClock.adjust(MINUTE_MS);
      state.data = quiet;
      // The relay's "finished" alert for the turn starts its hold only now: the settle starts here.
      yield* emit(threadChanged(state.data), "readThread", 3);
      yield* TestClock.adjust(HANDOFF_SETTLE_MS - 1);
      expect(sent).toEqual([]);
      yield* TestClock.adjust(1);
      yield* emit(null, "handOffWhenDue", 1);
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
    { server: on, project: { mode: "off" }, sends: 0 },
    { server: on, project: { mode: "compact" }, sends: 0 },
    { server: {}, project: on, sends: 1 },
    // The project's own thread entry wins over its mode, as for the server's.
    { server: on, project: { mode: "off", threads: { [threadId]: "handoff" } }, sends: 1 },
    { server: on, project: { mode: "handoff", threads: { [threadId]: "off" } }, sends: 0 },
  ] as ReadonlyArray<{
    server: Partial<HandoffSettings>;
    project: Partial<HandoffSettings>;
    sends: number;
  }>)(
    "a project's own setting decides for its threads, past the size limit (sends $sends)",
    ({ server, project, sends }) =>
      Effect.gen(function* () {
        const state = { data: projection({ usedTokens: 400_000 }), settings: server, project };
        const { sent, emit, reads } = yield* harness(state);
        yield* emit(runUpdated(run()), "readThread", 1);
        yield* settle;
        yield* emit(null, "handOffWhenDue", 1);
        expect(messages(sent)).toHaveLength(sends);
        // Off for the thread reads nothing more than its settings.
        if (sends === 0) expect(reads.count).toBe(0);
      }),
  );

  it.effect(
    "its project switched off while the handoff waits on a busy thread: sends nothing",
    () =>
      Effect.gen(function* () {
        const quiet = projection({ usedTokens: 160_000 });
        const state = {
          data: {
            ...quiet,
            runtimeRequests: [
              { id: RuntimeRequestId.make("request-1"), status: "pending" },
            ] as unknown as OrchestrationV2ThreadProjection["runtimeRequests"],
          },
          settings: on,
          project: undefined as Partial<HandoffSettings> | undefined,
        };
        const { sent, emit } = yield* harness(state);
        yield* emit(runUpdated(run()), "readThread", 1);
        state.project = { mode: "off" };
        state.data = quiet;
        yield* emit(threadChanged(state.data), "handOffWhenDue", 1);
        expect(sent).toEqual([]);
      }),
  );

  it.effect("its project switched off while the idle timer sleeps: sends nothing", () =>
    Effect.gen(function* () {
      const state = {
        data: projection({ usedTokens: 100_000, cacheExpiresInMs: 10 * MINUTE_MS }),
        settings: on,
        project: undefined as Partial<HandoffSettings> | undefined,
      };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(run()), "readThread", 1);
      yield* TestClock.adjust(MINUTE_MS);
      state.project = { mode: "off" };
      yield* TestClock.adjust(10 * MINUTE_MS);
      yield* emit(null, "handOffWhenDue", 1);
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
      yield* emit(runUpdated({ ...failed, id: RunId.make("run-4") }), "handoffRunEnded", 3);
      expect(messages(sent).length).toBe(1);
    }),
  );

  /** A gate with `free` slots: each allowed start takes one; the test frees them. */
  const countingGate = (free: number) => {
    const slots = {
      free,
      asks: [] as Array<SlotGate.SlotAcquireInput>,
      left: [] as Array<string>,
    };
    const layer = Layer.mock(SlotGate.SlotGate)({
      acquire: (input) =>
        Effect.sync((): SlotGate.SlotAcquire => {
          slots.asks.push(input);
          if (slots.free === 0)
            return {
              type: "refuse",
              message: "full",
              refusal: { type: "unread", problem: "timeout" },
            };
          slots.free -= 1;
          return { type: "allow", token: slots.asks.length };
        }),
      started: () => Effect.void,
      release: () => Effect.void,
      leave: (keys) => Effect.sync(() => slots.left.push(...keys)),
    });
    return { slots, layer };
  };

  it.effect("no free agent slot: the handoff waits without a new settle, then is sent once", () =>
    Effect.gen(function* () {
      const gate = countingGate(0);
      const state = { data: projection({ usedTokens: 160_000 }), settings: on, gate: gate.layer };
      const { sent, emit, reads } = yield* harness(state);
      yield* emit(runUpdated(run()), "readThread", 1);
      yield* settle;
      yield* TestClock.adjust(MINUTE_MS);
      expect(sent).toEqual([]);
      // Asked again every 5 s, each time after the busy check, as the thread's own start, in
      // its one place in the gate's line.
      expect(gate.slots.asks.length).toBeGreaterThanOrEqual(12);
      const place = { key: `handoff:server:handoff:${threadId}:${runId}` };
      expect(gate.slots.asks[0]).toEqual({
        callerThreadId: threadId,
        targetInstanceId: modelSelection.instanceId,
        intoThreadId: threadId,
        keepsHeld: true,
        place,
      });
      expect(gate.slots.asks.every((asked) => asked.place?.key === place.key)).toBe(true);
      // Refused: its text (the transcript read) is not built at all.
      expect(reads.text).toBe(0);
      gate.slots.free = 1;
      yield* TestClock.adjust(HandoffService.HANDOFF_SLOT_RETRY);
      yield* emit(null, "handOffWhenDue", 1);
      expect(messages(sent).map((message) => message.commandId)).toEqual([
        `server:handoff:${threadId}:${runId}`,
      ]);
      // Built once, with the slot.
      expect(reads.text).toBe(1);
    }),
  );

  it.effect("a waiting handoff whose thread gets busy, or a newer run, leaves its place", () =>
    Effect.gen(function* () {
      const gate = countingGate(0);
      const data = projection({ usedTokens: 160_000 });
      const state = { data, settings: on, gate: gate.layer };
      const { sent, emit, events } = yield* harness(state);
      yield* emit(runUpdated(run()), "readThread", 1);
      yield* settle;
      yield* TestClock.adjust(MINUTE_MS);
      expect(gate.slots.left).toEqual([]);
      // A newer run completes: its own timer replaces the waiting one, which leaves the line.
      const newer = run({ id: RunId.make("run-2"), ordinal: 2 });
      state.data = { ...data, runs: [...data.runs, newer] };
      yield* Queue.offer(events, runUpdated(newer));
      yield* settle;
      expect(gate.slots.left).toContain(`handoff:server:handoff:${threadId}:${runId}`);
      expect(sent).toEqual([]);
    }),
  );

  it.effect("no free agent slot: a failed handoff's retry waits too, then is sent once", () =>
    Effect.gen(function* () {
      const data = projection({ usedTokens: 160_000 });
      const failed = run({
        id: RunId.make("run-2"),
        ordinal: 2,
        userMessageId: handoffMessageId,
        status: "failed",
      });
      const gate = countingGate(0);
      const state = {
        data: {
          ...data,
          runs: [...data.runs, failed],
          messages: [{ ...data.messages[0]!, id: handoffMessageId, text: "the first text" }],
        },
        settings: on as Partial<HandoffSettings>,
        gate: gate.layer,
      };
      const { sent, emit, events } = yield* harness(state);
      yield* Queue.offer(events, runUpdated(failed));
      yield* TestClock.adjust(MINUTE_MS);
      expect(sent).toEqual([]);
      gate.slots.free = 1;
      yield* TestClock.adjust(HandoffService.HANDOFF_SLOT_RETRY);
      yield* emit(null, "handoffRunEnded", 1);
      expect(messages(sent).map((message) => message.messageId)).toEqual([
        `${handoffMessageId}:retry1`,
      ]);
    }),
  );

  const run2 = run({ id: RunId.make("run-2"), ordinal: 2 });
  const run2HandoffId = `handoff:${threadId}:run-2`;
  /** A late `completed` update of the older run-1, as a child's settlement or a wake re-emits. */
  const lateRun1 = () => ({
    ...runUpdated(run()),
    id: EventId.make(`event:late:${(changes += 1)}`),
  });

  it.effect(
    "a late update of an older run keeps the latest run's idle timer (twice in a row)",
    () =>
      Effect.gen(function* () {
        const data = projection({ usedTokens: 100_000, cacheExpiresInMs: 10 * MINUTE_MS });
        const state = { data: { ...data, runs: [run(), run2] }, settings: on };
        const { sent, emit } = yield* harness(state);
        yield* emit(runUpdated(run2), "readThread", 1);
        // Each late update replaces the timer; it reads, takes over run-2 and reads again.
        yield* emit(lateRun1(), "readThread", 3);
        yield* emit(lateRun1(), "readThread", 5);
        yield* TestClock.adjust(5 * MINUTE_MS);
        expect(sent).toEqual([]);
        yield* TestClock.adjust(2 * MINUTE_MS);
        yield* settle;
        expect(messages(sent).map((message) => message.messageId)).toEqual([run2HandoffId]);
      }),
  );

  it.effect("a late update of an older run keeps the latest run's wait at the size backstop", () =>
    Effect.gen(function* () {
      const quiet = { ...projection({ usedTokens: 160_000 }), runs: [run(), run2] };
      const busy = {
        ...quiet,
        runtimeRequests: [
          { id: RuntimeRequestId.make("request-1"), status: "pending" },
        ] as unknown as OrchestrationV2ThreadProjection["runtimeRequests"],
      };
      const state = { data: busy, settings: on };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(run2), "readThread", 1);
      yield* emit(lateRun1(), "readThread", 3);
      expect(sent).toEqual([]);
      state.data = quiet;
      yield* emit(threadChanged(state.data), "readThread", 4);
      yield* settle;
      yield* emit(null, "send", 1);
      expect(messages(sent).map((message) => message.messageId)).toEqual([run2HandoffId]);
    }),
  );

  it.effect("a handoff run a person stopped is not sent again", () =>
    Effect.gen(function* () {
      const data = projection({ usedTokens: 160_000 });
      const stopped = run({
        id: RunId.make("run-2"),
        ordinal: 2,
        userMessageId: handoffMessageId,
        status: "interrupted",
      });
      const state = {
        data: {
          ...data,
          runs: [...data.runs, stopped],
          messages: [{ ...data.messages[0]!, id: handoffMessageId, text: "[T3 handoff 1]" }],
        },
        settings: on,
      };
      const { sent, emit, reads } = yield* harness(state);
      yield* emit(runUpdated(stopped), "handoffRunEnded", 1);
      expect(sent).toEqual([]);
      expect(reads.count).toBe(0);
    }),
  );

  it.effect("a failed retry reads nothing and sends nothing, though it is the latest run", () =>
    Effect.gen(function* () {
      const data = projection({ usedTokens: 160_000 });
      const retryId = MessageId.make(`${handoffMessageId}:retry1`);
      const first = run({
        id: RunId.make("run-2"),
        ordinal: 2,
        userMessageId: handoffMessageId,
        status: "failed",
      });
      const retry = run({
        id: RunId.make("run-3"),
        ordinal: 3,
        userMessageId: retryId,
        status: "failed",
      });
      const state = {
        data: {
          ...data,
          runs: [...data.runs, first, retry],
          messages: [{ ...data.messages[0]!, id: retryId, text: "[T3 handoff 1]" }],
        },
        settings: on,
      };
      const { sent, emit, reads } = yield* harness(state);
      yield* emit(runUpdated(retry), "handoffRunEnded", 1);
      expect(sent).toEqual([]);
      expect(reads.count).toBe(0);
    }),
  );

  it.effect("a late update during the send does not stop the cancel of a queued handoff", () =>
    Effect.gen(function* () {
      const quiet = projection({ usedTokens: 160_000 });
      const gate = yield* Deferred.make<void>();
      const inSend = yield* Deferred.make<void>();
      const state: Parameters<typeof harness>[0] = { data: quiet, settings: on };
      // A user message got in first: upstream queues the handoff behind it.
      state.onDispatch = (command) =>
        command.type === "message.dispatch"
          ? Effect.gen(function* () {
              state.data = {
                ...state.data,
                runs: [
                  ...state.data.runs,
                  run({ id: RunId.make("run-2"), ordinal: 2, status: "running" }),
                  run({
                    id: RunId.make("run-3"),
                    ordinal: 3,
                    userMessageId: command.messageId,
                    status: "queued",
                    startedAt: null,
                    completedAt: null,
                  }),
                ],
              };
              yield* Deferred.succeed(inSend, undefined);
              yield* Deferred.await(gate);
            })
          : Effect.void;
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(run()), "readThread", 1);
      yield* settle;
      yield* Deferred.await(inSend);
      // A late update for the source run replaces the thread's fiber while it is inside the send.
      // The second one is handled only after the first replaced (interrupted) the sending fiber.
      yield* emit(lateRun1(), "readThread", 3);
      yield* emit(lateRun1(), "readThread", 4);
      yield* Deferred.succeed(gate, undefined);
      yield* emit(null, "HandoffDispatch.dispatch", 1);
      expect(sent.filter((command) => command.type === "queued-run.cancel")).toMatchObject([
        { runId: "run-3" },
      ]);
    }),
  );
});

describe("HandoffService: a handoff that cannot run (#608)", () => {
  const big = () => projection({ usedTokens: 160_000 });
  const chosen = { instanceId: otherInstanceId, model: "claude-opus-5-5" };
  const otherOn = otherInstanceConfig(true);

  // The same table as the compaction wrapper's test (handoffOwner.test.ts): a handoff is sent
  // exactly when the wrapper lets handoff own the thread, so the two never disagree.
  it.effect.each(runnableCases)("$name: sends a handoff: $runs", (entry) =>
    Effect.gen(function* () {
      const data = runnableThread(entry, big());
      const parts = runnableSettings(entry, data.thread.projectId);
      const state = {
        data,
        settings: { ...on, ...parts.handoff } as Partial<HandoffSettings>,
        providerInstances: parts.providerInstances as ServerSettingsValue["providerInstances"],
        projectProviderInstances: parts.projectProviderInstances[data.thread.projectId],
        missing: parts.missing,
      };
      const { sent, emit, reads } = yield* harness(state);
      yield* emit(runUpdated(data.runs[0]!), "readThread", 1);
      yield* settle;
      yield* emit(null, "handOffWhenDue", 1);
      expect(messages(sent)).toHaveLength(entry.runs ? 1 : 0);
      // It sends on the instance it checked: the setting's, else the thread's own.
      if (entry.runs)
        expect(messages(sent)[0]?.modelSelection?.instanceId).toBe(
          entry.target ?? data.thread.modelSelection.instanceId,
        );
      // One that cannot run reads no records, as with handoff off.
      if (!entry.runs) expect(reads.count).toBe(0);
    }),
  );

  it.effect(
    "its instance removed while the idle timer sleeps (changed half-way): sends nothing",
    () =>
      Effect.gen(function* () {
        const state = {
          data: projection({ usedTokens: 100_000, cacheExpiresInMs: 10 * MINUTE_MS }),
          settings: { ...on, modelSelection: chosen } as Partial<HandoffSettings>,
          providerInstances: otherOn as ServerSettingsValue["providerInstances"],
          missing: new Set<string>(),
        };
        const { sent, emit } = yield* harness(state);
        yield* emit(runUpdated(run()), "readThread", 1);
        yield* TestClock.adjust(MINUTE_MS);
        state.missing.add(otherInstanceId);
        yield* TestClock.adjust(10 * MINUTE_MS);
        yield* emit(null, "handOffWhenDue", 1);
        expect(sent).toEqual([]);
      }),
  );

  it.effect("its instance turned off inside the settle: the read after it sends nothing", () =>
    Effect.gen(function* () {
      const state = {
        data: big(),
        settings: { ...on, modelSelection: chosen } as Partial<HandoffSettings>,
        providerInstances: otherOn as ServerSettingsValue["providerInstances"],
      };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(run()), "readThread", 1);
      yield* TestClock.adjust(HANDOFF_SETTLE_MS / 2);
      state.providerInstances = otherInstanceConfig(false);
      yield* TestClock.adjust(HANDOFF_SETTLE_MS / 2);
      yield* emit(null, "handOffWhenDue", 1);
      expect(sent).toEqual([]);
    }),
  );

  it.effect(
    "removed, then added back: the same run reported again (runs again) hands off once",
    () =>
      Effect.gen(function* () {
        const state = {
          data: big(),
          settings: { ...on, modelSelection: chosen } as Partial<HandoffSettings>,
          providerInstances: otherOn as ServerSettingsValue["providerInstances"],
          missing: new Set<string>([otherInstanceId]),
        };
        const { sent, emit } = yield* harness(state);
        yield* emit(runUpdated(run()), "handOffWhenDue", 1);
        expect(sent).toEqual([]);
        // Nothing was remembered: the next report reads afresh.
        state.missing.clear();
        yield* emit(runUpdated(run()), "readThread", 2);
        yield* settle;
        yield* emit(null, "handOffWhenDue", 2);
        expect(messages(sent)).toMatchObject([
          { messageId: handoffMessageId, modelSelection: chosen },
        ]);
      }),
    // Without the registry check the first report settles and waits on the test clock forever.
    SHORT_TIMEOUT_MS,
  );

  // The retry sends on the failed run's own model. The setting now names B (on), but the run went
  // to A, which was turned off or removed before the run failed: the retry must not start on A.
  it.effect.each([
    { name: "turned off", builtIn: false, missing: [] },
    { name: "removed", builtIn: true, missing: [modelSelection.instanceId] },
  ])("the setting switched to B and the run's instance A $name: no retry to A", (entry) =>
    Effect.gen(function* () {
      const data = big();
      const failed = run({
        id: RunId.make("run-2"),
        ordinal: 2,
        userMessageId: handoffMessageId,
        status: "failed",
        modelSelection,
      });
      const state = {
        data: {
          ...data,
          runs: [...data.runs, failed],
          messages: [{ ...data.messages[0]!, id: handoffMessageId, text: "[T3 handoff 1]" }],
        },
        settings: { ...on, modelSelection: chosen } as Partial<HandoffSettings>,
        providerInstances: {
          ...otherOn,
          [modelSelection.instanceId]: {
            driver: ProviderDriverKind.make("claudeAgent"),
            enabled: entry.builtIn,
          },
        } as ServerSettingsValue["providerInstances"],
        missing: new Set<string>(entry.missing),
      };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(failed), "handoffRunEnded", 1);
      expect(sent).toEqual([]);
      // A on and registered again: the same failed run reported again is retried, on A.
      state.providerInstances = {
        ...otherOn,
        [modelSelection.instanceId]: {
          driver: ProviderDriverKind.make("claudeAgent"),
          enabled: true,
        },
      } as ServerSettingsValue["providerInstances"];
      state.missing.clear();
      yield* emit(runUpdated(failed), "handoffRunEnded", 2);
      expect(messages(sent)).toMatchObject([
        { messageId: `${handoffMessageId}:retry1`, modelSelection },
      ]);
    }),
  );

  it.effect("a failed handoff run whose instance is gone now is not sent again", () =>
    Effect.gen(function* () {
      const data = big();
      const failed = run({
        id: RunId.make("run-2"),
        ordinal: 2,
        userMessageId: handoffMessageId,
        status: "failed",
      });
      const state = {
        data: {
          ...data,
          runs: [...data.runs, failed],
          messages: [{ ...data.messages[0]!, id: handoffMessageId, text: "[T3 handoff 1]" }],
        },
        settings: on as Partial<HandoffSettings>,
        // No modelSelection: the thread's own instance, which is no longer registered.
        missing: new Set<string>([modelSelection.instanceId]),
      };
      const { sent, emit } = yield* harness(state);
      yield* emit(runUpdated(failed), "handoffRunEnded", 1);
      expect(sent).toEqual([]);
    }),
  );
});
