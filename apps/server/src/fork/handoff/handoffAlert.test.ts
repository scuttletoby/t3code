/**
 * Fork: what the phone gets around a handoff. The real handoff service and upstream's real relay
 * share one test clock and one thread; the relay also sees T3 children, PR watches and monitor
 * wakes. The person's turn alerts once; a handoff sent while the finish was not shown alerts.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import { describe, expect, it } from "@effect/vitest";
import {
  DEFAULT_HANDOFF_SETTINGS,
  DEFAULT_SERVER_SETTINGS,
  EnvironmentId,
  EventId,
  MessageId,
  type OrchestrationV2DomainEvent,
  type OrchestrationV2Run,
  type OrchestrationV2ServerCommand,
  type OrchestrationV2ThreadProjection,
  RunId,
  RuntimeRequestId,
  type ThreadId,
} from "@t3tools/contracts";
import { RelayAgentActivityState } from "@t3tools/contracts/relay";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Queue from "effect/Queue";
import * as Schema from "effect/Schema";
import * as Stream from "effect/Stream";
import * as TestClock from "effect/testing/TestClock";
import * as FetchHttpClient from "effect/http/FetchHttpClient";

import * as ServerSecretStore from "../../auth/ServerSecretStore.ts";
import {
  PUBLISH_AGENT_ACTIVITY_SECRET,
  RELAY_ENVIRONMENT_CREDENTIAL_SECRET,
  RELAY_ISSUER_SECRET,
  RELAY_URL_SECRET,
} from "../../cloud/config.ts";
import * as EnvironmentKeys from "../../cloud/environmentKeys.ts";
import * as ServerEnvironment from "../../environment/ServerEnvironment.ts";
import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import type { AgentAwarenessThread } from "../../orchestration-v2/ProjectionStore.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as ProjectService from "../../project/ProjectService.ts";
import * as AgentAwarenessRelay from "../../relay/AgentAwarenessRelay.ts";
import * as ServerSettings from "../../serverSettings.ts";
import * as HandoffService from "./HandoffService.ts";
import * as HandoffCommand from "./handoffCommand.ts";
import {
  adapterRegistry,
  at,
  awarenessShell,
  handoffMessageId,
  MINUTE_MS,
  NOW_MS,
  projection,
  run,
  threadId,
} from "./handoff.testkit.ts";
import { HANDOFF_LOUD_SUFFIX, HANDOFF_RETRY_SUFFIX } from "./handoffFacts.ts";
import { HANDOFF_SETTLE_MS } from "./handoffQuiet.ts";

const PublishPayload = Schema.Struct({ state: Schema.NullOr(RelayAgentActivityState) });
const decodePublishPayload = Schema.decodeUnknownSync(Schema.fromJsonString(PublishPayload));
const unused = () => Effect.die("Unexpected test dependency call");
const handoffRunId = RunId.make("run-2");
const retryRunId = RunId.make("run-3");

/**
 * One thread: its records (what the service and the quiet check read) and the awareness row the
 * relay reads, derived from them as upstream's projection does for the fields that matter here.
 */
const world = Effect.fnUntraced(function* (usage: {
  readonly usedTokens: number;
  readonly cacheExpiresInMs?: number;
}) {
  // The person's turn starts a minute before it ends at NOW_MS; the relay starts before it.
  yield* TestClock.setTime(NOW_MS - MINUTE_MS);
  const base = projection(usage);
  const state = {
    data: {
      ...base,
      runs: [run({ status: "running", completedAt: null })],
    } as OrchestrationV2ThreadProjection,
    updatedAt: at(-MINUTE_MS),
    /** Resolved when the service next reads its settings, at the start of handling an event. */
    serverRead: null as Deferred.Deferred<void> | null,
    /** Resolved when the service next reads the child's awareness row. */
    childViewRead: null as Deferred.Deferred<void> | null,
  };
  const latest = () => state.data.runs.reduce((a, b) => (b.ordinal > a.ordinal ? b : a));
  /** What only the relay sees. `open`: a T3 child below the thread (null none, true asking). */
  const kid = { open: null as boolean | null, watch: false, child: false, monitorWake: false };
  const shell = (): AgentAwarenessThread =>
    awarenessShell(state.data, {
      updatedAt: state.updatedAt,
      // As upstream's shell: an awaited PR watch, a running T3 child's row, a monitor.
      pendingBackgroundTasks: [
        ...(kid.watch
          ? [
              {
                taskId: "pull-request-watch:github.com/o/r#1",
                kind: "monitor" as const,
                awaited: true as const,
                description: "Watching pull request #1",
              },
            ]
          : []),
        ...(kid.child
          ? [{ taskId: "child-task", kind: "subagent" as const, description: "T3 child" }]
          : []),
        ...(kid.monitorWake
          ? [{ taskId: "monitor-1", kind: "monitor" as const, description: "tail the log" }]
          : []),
      ],
      // The person's run was a monitor's wake; the handoff run is not.
      latestRunBackgroundWake: kid.monitorWake && latest().id === runId1,
    });

  /** The thread's records; filters are ignored, every reader picks what it needs by id. */
  const projections = {
    getThread: () => Effect.sync(() => state.data.thread),
    getThreadRecords: () => Effect.sync(() => state.data),
  } as unknown as ProjectionStore.ProjectionStoreV2["Service"];

  const events = yield* Queue.unbounded<OrchestrationV2DomainEvent>();
  const sends = yield* Queue.unbounded<string>();
  /** What upstream's orchestrator does with the service's send: the handoff run starts. */
  const dispatch = (command: OrchestrationV2ServerCommand) =>
    Effect.gen(function* () {
      if (command.type !== "message.dispatch") return { sequence: 0, storedEvents: [] };
      const now = yield* DateTime.now;
      const retry = command.messageId.endsWith(HANDOFF_RETRY_SUFFIX);
      const id = retry ? retryRunId : handoffRunId;
      state.updatedAt = now;
      state.data = {
        ...state.data,
        runs: [
          ...state.data.runs,
          run({
            id,
            ordinal: state.data.runs.length + 1,
            userMessageId: command.messageId as MessageId,
            status: "running",
            requestedAt: now,
            startedAt: now,
            completedAt: null,
          }),
        ],
        messages: [
          ...state.data.messages,
          {
            ...state.data.messages[0]!,
            id: command.messageId,
            runId: id,
            text: command.text,
            createdBy: "agent",
            creationSource: "server",
            createdAt: now,
            updatedAt: now,
          },
        ],
      };
      yield* Queue.offer(sends, command.messageId);
      return { sequence: 1, storedEvents: [] };
    });
  yield* HandoffService.make.pipe(
    Effect.flatMap((service) => service.start()),
    Effect.provide(
      Layer.mergeAll(
        Layer.mock(Orchestrator.OrchestratorV2)({
          streamDomainEvents: Stream.fromQueue(events),
          dispatch,
          // Upstream's own reads, as the relay gets them.
          getAgentAwarenessSnapshot: (requested?: ThreadId) =>
            Effect.gen(function* () {
              const snapshot = yield* threads.getAgentAwarenessSnapshot(requested);
              if (requested === childId && state.childViewRead !== null) {
                yield* Deferred.succeed(state.childViewRead, undefined);
              }
              return snapshot;
            }),
          listActiveShellIndex: () => Effect.suspend(() => threads.listActiveShellIndex()),
        }),
        Layer.succeed(ProjectionStore.ProjectionStoreV2, projections),
        Layer.mock(ServerSettings.ServerSettingsService)({
          getSettings: Effect.gen(function* () {
            if (state.serverRead !== null) yield* Deferred.succeed(state.serverRead, undefined);
            return {
              ...DEFAULT_SERVER_SETTINGS,
              handoff: { ...DEFAULT_HANDOFF_SETTINGS, mode: "handoff" as const },
            };
          }),
        }),
        adapterRegistry(),
        // The agent source: no command runs.
        Layer.mock(HandoffCommand.HandoffCommand)({}),
      ),
    ),
  );

  // Upstream's relay, as its own tests build it, reading the thread's shell as upstream's.
  const secretValues = new Map<string, Uint8Array>([
    [PUBLISH_AGENT_ACTIVITY_SECRET, new TextEncoder().encode("true")],
    [RELAY_URL_SECRET, new TextEncoder().encode("https://relay.example.test")],
    [RELAY_ISSUER_SECRET, new TextEncoder().encode("https://relay.example.test")],
    [RELAY_ENVIRONMENT_CREDENTIAL_SECRET, new TextEncoder().encode("credential-1")],
  ]);
  const childShell = (): AgentAwarenessThread =>
    ({
      ...shell(),
      id: childId,
      lineage: childLineage,
      status: "running",
      activityRunStatus: "running",
      latestRunId: RunId.make("child-run"),
      activeRunId: RunId.make("child-run"),
      pendingRuntimeRequest: kid.open
        ? {
            id: RuntimeRequestId.make("child-request"),
            kind: "command",
            createdAt: state.updatedAt,
          }
        : null,
    }) as AgentAwarenessThread;
  const threads = {
    getAgentAwarenessSnapshot: (requested?: ThreadId) =>
      Effect.sync(() => ({
        snapshotSequence: 1,
        threads:
          requested === undefined || requested === threadId
            ? [shell()]
            : requested === childId && kid.open !== null
              ? [childShell()]
              : [],
      })),
    listActiveShellIndex: () =>
      Effect.sync(() =>
        kid.open === null
          ? []
          : [
              {
                id: childId,
                projectId: base.thread.projectId,
                lineage: childLineage,
                settledOverride: null,
                settledAt: null,
                pinnedAt: null,
                pinOrderKey: null,
                snoozedUntil: null,
                mayBeLive: true,
                hasRemoteTasks: false,
              },
            ],
      ),
  } as unknown as ThreadManagement.ThreadManagementService["Service"];
  const phases: Array<string | null> = [];
  const fetch: typeof globalThis.fetch = Object.assign(
    (_input: Parameters<typeof globalThis.fetch>[0], init?: RequestInit) => {
      const body = init?.body;
      const text = typeof body === "string" ? body : new TextDecoder().decode(body as Uint8Array);
      phases.push(decodePublishPayload(text).state?.phase ?? null);
      return Promise.resolve(Response.json({ ok: true, deliveries: [] }));
    },
    { preconnect: () => {} },
  );
  const project = {
    id: base.thread.projectId,
    title: "Project",
    workspaceRoot: "/workspace",
    defaultModelSelection: null,
    scripts: [],
    createdAt: DateTime.formatIso(at(-MINUTE_MS)),
    updatedAt: DateTime.formatIso(at(-MINUTE_MS)),
    deletedAt: null,
  };
  const relay = yield* AgentAwarenessRelay.make.pipe(
    Effect.provide(EnvironmentKeys.layer),
    Effect.provideService(
      ServerSecretStore.ServerSecretStore,
      ServerSecretStore.ServerSecretStore.of({
        get: (name) => Effect.succeed(Option.fromUndefinedOr(secretValues.get(name))),
        create: (name, value) => Effect.sync(() => void secretValues.set(name, value)),
        set: (name, value) => Effect.sync(() => void secretValues.set(name, value)),
        remove: (name) => Effect.sync(() => void secretValues.delete(name)),
        getOrCreateRandom: unused,
      }),
    ),
    Effect.provideService(ThreadManagement.ThreadManagementService, threads),
    Effect.provideService(ServerEnvironment.ServerEnvironment, {
      getEnvironmentId: Effect.succeed(EnvironmentId.make("relay-environment")),
      getDescriptor: unused(),
    }),
    Effect.provideService(ProjectService.ProjectService, {
      create: unused,
      bootstrap: unused,
      update: unused,
      delete: unused,
      getByWorkspaceRoot: unused,
      workspaceScanDirectory: unused,
      snapshot: Effect.succeed({ projects: [project], updatedAt: project.updatedAt }),
      listPage: unused,
      getShell: unused,
      listShells: unused,
      getById: () => Effect.succeedSome(project),
    } as unknown as ProjectService.ProjectService["Service"]),
    Effect.provideService(FetchHttpClient.Fetch, fetch),
    Effect.provide(NodeCrypto.layer),
  );

  let eventCount = 0;
  /** Changes the thread at the current time, tells the service (as the event stream does) and the relay. */
  const change = Effect.fnUntraced(function* (
    update: (data: OrchestrationV2ThreadProjection) => OrchestrationV2ThreadProjection,
    event: "run" | "thread" | null,
  ) {
    state.updatedAt = yield* DateTime.now;
    state.data = update(state.data);
    if (event !== null) {
      const read = yield* Deferred.make<void>();
      state.serverRead = read;
      const last = latest();
      yield* Queue.offer(
        events,
        (event === "run"
          ? {
              id: EventId.make(`event:${(eventCount += 1)}`),
              type: "run.updated",
              threadId,
              runId: last.id,
              occurredAt: state.updatedAt,
              payload: last,
            }
          : {
              id: EventId.make(`event:${(eventCount += 1)}`),
              type: "thread.metadata-updated",
              threadId,
              occurredAt: state.updatedAt,
              payload: state.data.thread,
            }) as OrchestrationV2DomainEvent,
      );
      // Until the service has taken the event (it then sleeps or waits on the clock).
      yield* Deferred.await(read);
      state.serverRead = null;
    }
    yield* relay.publishThread(threadId);
  });
  /** Moves the clock; the relay's held alerts and the service's timers run on it. */
  const wait = Effect.fnUntraced(function* (millis: number) {
    yield* TestClock.adjust(millis);
    yield* relay.drain;
    // A send the clock woke: the relay sees the new run as the event stream would tell it.
    if (latest().status === "running" && latest().id !== RunId.make("run-1")) {
      yield* relay.publishThread(threadId);
    }
  });
  const endRun = (id: RunId, status: OrchestrationV2Run["status"]) =>
    Effect.gen(function* () {
      const now = yield* DateTime.now;
      yield* change(
        (data) => ({
          ...data,
          runs: data.runs.map((one) =>
            one.id === id ? { ...one, status, completedAt: now } : one,
          ),
        }),
        id === RunId.make("run-1") ? "run" : null,
      );
    });
  const runIds = () => state.data.runs.map((one) => one.id);
  // The person's turn is running.
  yield* relay.publishThread(threadId);
  return { state, phases, change, wait, endRun, runIds, events, latest, sends, kid, relay };
});

const busy = (data: OrchestrationV2ThreadProjection): OrchestrationV2ThreadProjection => ({
  ...data,
  runtimeRequests: [
    { id: RuntimeRequestId.make("request-1"), status: "pending" },
  ] as unknown as OrchestrationV2ThreadProjection["runtimeRequests"],
});

describe("handoff alerts through upstream's relay", () => {
  it.effect("size limit: the person's turn alerts once, then the handoff run is quiet", () =>
    Effect.gen(function* () {
      const { phases, wait, endRun, runIds } = yield* world({ usedTokens: 160_000 });
      yield* wait(MINUTE_MS);
      yield* endRun(runId1, "completed");
      // The relay holds the alert 5 s; the service settles past that before it sends.
      yield* wait(HANDOFF_SETTLE_MS / 2);
      expect(runIds()).toEqual([runId1]);
      yield* wait(HANDOFF_SETTLE_MS / 2);
      expect(runIds()).toEqual([runId1, handoffRunId]);
      yield* wait(MINUTE_MS);
      yield* endRun(handoffRunId, "completed");
      yield* wait(5_000);
      yield* wait(5_000);
      // The handoff run's end clears the card, as upstream does for a stopped run.
      expect(phases).toEqual(["running", "completed", "running", null]);
    }),
  );

  it.effect("a handoff run that started inside the hold alerts as the turn's finish", () =>
    Effect.gen(function* () {
      // As the service sent before the settle: at the same turn end, inside the relay's hold.
      const { phases, wait, endRun, change, state } = yield* world({ usedTokens: 0 });
      yield* wait(MINUTE_MS);
      yield* endRun(runId1, "completed");
      const now = yield* DateTime.now;
      yield* change(
        (data) => ({
          ...data,
          runs: [
            ...data.runs,
            run({
              id: handoffRunId,
              ordinal: 2,
              userMessageId: handoffMessageId,
              status: "running",
              requestedAt: now,
              startedAt: now,
              completedAt: null,
            }),
          ],
          messages: [
            ...data.messages,
            {
              ...data.messages[0]!,
              id: handoffMessageId,
              runId: handoffRunId,
              createdBy: "agent",
              creationSource: "server",
            },
          ],
        }),
        null,
      );
      yield* wait(MINUTE_MS);
      yield* endRun(handoffRunId, "completed");
      yield* wait(5_000);
      yield* wait(5_000);
      // The person's "completed" was dropped in the hold, so the handoff run's end is the alert.
      expect(phases.filter((phase) => phase === "completed")).toHaveLength(1);
      expect(phases.at(-1)).toBe("completed");
      expect(state.data.runs).toHaveLength(2);
    }),
  );

  it.effect("a handoff that waited for busy work settles after the work ends", () =>
    Effect.gen(function* () {
      const { phases, wait, change, runIds, state } = yield* world({ usedTokens: 160_000 });
      yield* wait(MINUTE_MS);
      const now = yield* DateTime.now;
      // The turn ends with work still pending (here an open request): the service waits.
      yield* change(
        (data) =>
          busy({
            ...data,
            runs: data.runs.map((one) => ({ ...one, status: "completed", completedAt: now })),
          }),
        "run",
      );
      yield* wait(MINUTE_MS);
      expect(runIds()).toEqual([runId1]);
      // The work ends: the relay's "finished" hold starts only now, and so does the settle.
      yield* change((data) => ({ ...data, runtimeRequests: [] }), "thread");
      yield* wait(HANDOFF_SETTLE_MS / 2);
      expect(runIds()).toEqual([runId1]);
      yield* wait(HANDOFF_SETTLE_MS / 2);
      expect(runIds()).toEqual([runId1, handoffRunId]);
      yield* wait(MINUTE_MS);
      const end = yield* DateTime.now;
      yield* change(
        (data) => ({
          ...data,
          runs: data.runs.map((one) =>
            one.id === handoffRunId ? { ...one, status: "completed", completedAt: end } : one,
          ),
        }),
        null,
      );
      yield* wait(5_000);
      yield* wait(5_000);
      expect(phases).toEqual(["running", "waiting_for_approval", "completed", "running", null]);
      expect(state.data.runs).toHaveLength(2);
    }),
  );

  it.effect("idle handoff: the turn alerted long before, the handoff run is quiet", () =>
    Effect.gen(function* () {
      const { phases, wait, endRun, runIds } = yield* world({
        usedTokens: 100_000,
        cacheExpiresInMs: 10 * MINUTE_MS,
      });
      yield* wait(MINUTE_MS);
      yield* endRun(runId1, "completed");
      // The agent source opens the window 4 minutes before the cache expires: at 6 minutes.
      yield* wait(6 * MINUTE_MS);
      expect(runIds()).toEqual([runId1]);
      yield* wait(HANDOFF_SETTLE_MS);
      expect(runIds()).toEqual([runId1, handoffRunId]);
      yield* wait(MINUTE_MS);
      yield* endRun(handoffRunId, "completed");
      yield* wait(5_000);
      yield* wait(5_000);
      expect(phases).toEqual(["running", "completed", "running", null]);
    }),
  );

  it.effect("a failed handoff run alerts, and its retry that succeeds alerts too", () =>
    Effect.gen(function* () {
      const { phases, wait, endRun, runIds, events, latest, sends } = yield* world({
        usedTokens: 160_000,
      });
      yield* wait(MINUTE_MS);
      yield* endRun(runId1, "completed");
      yield* wait(HANDOFF_SETTLE_MS);
      expect(runIds()).toEqual([runId1, handoffRunId]);
      yield* wait(MINUTE_MS);
      yield* endRun(handoffRunId, "failed");
      // The service hears of the failure and sends the same text once more.
      yield* Queue.offer(events, {
        id: EventId.make("event:handoff-failed"),
        type: "run.updated",
        threadId,
        runId: handoffRunId,
        occurredAt: at(),
        payload: latest(),
      });
      expect(yield* Queue.take(sends)).toBe(handoffMessageId);
      expect(yield* Queue.take(sends)).toBe(`${handoffMessageId}${HANDOFF_RETRY_SUFFIX}`);
      expect(runIds()).toEqual([runId1, handoffRunId, retryRunId]);
      yield* wait(0);
      yield* wait(MINUTE_MS);
      yield* endRun(retryRunId, "completed");
      yield* wait(5_000);
      yield* wait(5_000);
      // The last alert said "failed"; the work went on, so the retry's finish alerts.
      expect(phases).toEqual(["running", "completed", "running", "failed", "running", "completed"]);
    }),
  );

  it.effect(
    "relay-visible work that comes and goes inside the settle: the handoff run alerts",
    () =>
      Effect.gen(function* () {
        const { phases, wait, endRun, change, runIds } = yield* world({ usedTokens: 160_000 });
        yield* wait(MINUTE_MS);
        yield* endRun(runId1, "completed");
        yield* wait(2_000);
        // e.g. a T3 child's approval shown on the parent; T3 children are no handoff blocker
        yield* change(busy, null);
        yield* wait(5_000);
        yield* change((data) => ({ ...data, runtimeRequests: [] }), null);
        yield* wait(3_000);
        expect(runIds()).toEqual([runId1, handoffRunId]);
        yield* wait(MINUTE_MS);
        yield* endRun(handoffRunId, "completed");
        yield* wait(5_000);
        yield* wait(5_000);
        expect(phases).toContain("completed");
      }),
  );

  it.effect.each([3_000, 7_000, 12_000])(
    `a T3 child's approval answered %d ms after the turn end: "finished" still alerts`,
    (answerAt) =>
      Effect.gen(function* () {
        const { phases, wait, endRun, runIds, kid, relay } = yield* world({ usedTokens: 160_000 });
        yield* wait(MINUTE_MS - 1_000);
        kid.open = true; // the T3 child (not a handoff blocker) asks for approval
        yield* relay.publishThread(childId);
        yield* wait(1_000);
        yield* endRun(runId1, "completed"); // the person's turn ends
        yield* wait(answerAt);
        kid.open = false; // the person answers the child
        yield* relay.publishThread(childId);
        yield* wait(0);
        yield* wait(15_000 - answerAt);
        expect(runIds()).toEqual([runId1, handoffRunId]);
        yield* wait(MINUTE_MS);
        yield* endRun(handoffRunId, "completed");
        yield* wait(5_000);
        yield* wait(5_000);
        expect(phases).toContain("completed");
      }),
  );

  it.effect(
    'an awaited PR watch across a size handoff: "finished" alerts when the watch ends',
    () =>
      Effect.gen(function* () {
        const { phases, wait, endRun, runIds, kid, relay } = yield* world({ usedTokens: 160_000 });
        yield* wait(MINUTE_MS - 1_000);
        kid.watch = true; // the agent watches its PR; checks pending
        yield* wait(1_000);
        yield* endRun(runId1, "completed");
        yield* wait(HANDOFF_SETTLE_MS);
        expect(runIds()).toEqual([runId1, handoffRunId]);
        yield* wait(MINUTE_MS);
        yield* endRun(handoffRunId, "completed");
        yield* wait(5 * MINUTE_MS);
        kid.watch = false; // checks done, watch ends
        yield* relay.publishThread(threadId);
        yield* wait(5_000);
        yield* wait(5_000);
        expect(phases).toContain("completed");
      }),
  );

  it.effect(
    "a T3 child running at the turn end: the handoff goes, loud, and alerts when the child ends",
    () =>
      Effect.gen(function* () {
        const { phases, wait, endRun, runIds, kid, relay, sends } = yield* world({
          usedTokens: 160_000,
        });
        yield* wait(MINUTE_MS - 1_000);
        kid.child = true; // a T3 child works on; children never block the handoff
        yield* wait(1_000);
        yield* endRun(runId1, "completed");
        yield* wait(HANDOFF_SETTLE_MS);
        expect(runIds()).toEqual([runId1, handoffRunId]);
        expect(yield* Queue.take(sends)).toBe(`${handoffMessageId}${HANDOFF_LOUD_SUFFIX}`);
        yield* wait(MINUTE_MS);
        yield* endRun(handoffRunId, "completed");
        yield* wait(5 * MINUTE_MS);
        kid.child = false;
        yield* relay.publishThread(threadId);
        yield* wait(5_000);
        yield* wait(5_000);
        // The relay never showed the person's finish; the loud handoff run's end is it.
        expect(phases).toEqual(["running", "completed"]);
      }),
  );

  it.effect("a monitor's wake run at the turn end: the handoff run alerts", () =>
    Effect.gen(function* () {
      const { phases, wait, endRun, runIds, kid, sends } = yield* world({ usedTokens: 160_000 });
      kid.monitorWake = true; // the person's run was a monitor's wake; the monitor runs on
      yield* wait(MINUTE_MS);
      yield* endRun(runId1, "completed");
      yield* wait(HANDOFF_SETTLE_MS);
      expect(runIds()).toEqual([runId1, handoffRunId]);
      expect(yield* Queue.take(sends)).toBe(`${handoffMessageId}${HANDOFF_LOUD_SUFFIX}`);
      yield* wait(MINUTE_MS);
      yield* endRun(handoffRunId, "completed");
      yield* wait(5_000);
      yield* wait(5_000);
      expect(phases).toEqual(["running", "completed"]);
    }),
  );

  it.effect("a T3 child's request opened and answered between two events still makes it loud", () =>
    Effect.gen(function* () {
      // Checked on the child's request events too, not only once a second.
      const { phases, wait, endRun, runIds, kid, relay, sends, events, state } = yield* world({
        usedTokens: 160_000,
      });
      yield* wait(MINUTE_MS);
      yield* endRun(runId1, "completed");
      yield* wait(6_500);
      kid.open = true;
      const read = yield* Deferred.make<void>();
      state.childViewRead = read;
      yield* relay.publishThread(childId);
      yield* Queue.offer(events, {
        id: EventId.make("event:child-request"),
        type: "runtime-request.updated",
        threadId: childId,
        occurredAt: yield* DateTime.now,
        payload: {},
      } as unknown as OrchestrationV2DomainEvent);
      yield* Deferred.await(read); // the service checked on the event, not on its next second
      state.childViewRead = null;
      kid.open = false;
      yield* relay.publishThread(childId);
      yield* wait(HANDOFF_SETTLE_MS);
      expect(runIds()).toEqual([runId1, handoffRunId]);
      expect(yield* Queue.take(sends)).toBe(`${handoffMessageId}${HANDOFF_LOUD_SUFFIX}`);
      yield* wait(MINUTE_MS);
      yield* endRun(handoffRunId, "completed");
      yield* wait(5_000);
      yield* wait(5_000);
      expect(phases.at(-1)).toBe("completed");
    }),
  );

  it.effect("busy work inside the settle starts it over once the work ends", () =>
    Effect.gen(function* () {
      const { phases, wait, endRun, change, runIds, sends } = yield* world({ usedTokens: 160_000 });
      yield* wait(MINUTE_MS);
      yield* endRun(runId1, "completed");
      yield* wait(4_000);
      yield* change(busy, "thread"); // the thread's own request: a blocker, the service waits
      yield* wait(4_000);
      yield* change((data) => ({ ...data, runtimeRequests: [] }), "thread");
      // A settle kept from before the work would send at 10 s, inside the relay's new hold.
      yield* wait(HANDOFF_SETTLE_MS - 1_000);
      expect(runIds()).toEqual([runId1]);
      yield* wait(1_000);
      expect(runIds()).toEqual([runId1, handoffRunId]);
      // The new settle saw the finish shown throughout: quiet.
      expect(yield* Queue.take(sends)).toBe(handoffMessageId);
      yield* wait(MINUTE_MS);
      yield* endRun(handoffRunId, "completed");
      yield* wait(5_000);
      yield* wait(5_000);
      // The request inside the relay's hold replaced the held "finished"; it alerts after the work.
      expect(phases).toEqual(["running", "waiting_for_approval", "completed", "running", null]);
    }),
  );

  it.effect("a later turn taken over inside the settle settles afresh", () =>
    Effect.gen(function* () {
      const { wait, endRun, change, runIds, sends } = yield* world({ usedTokens: 160_000 });
      yield* wait(MINUTE_MS);
      yield* endRun(runId1, "completed");
      yield* wait(6_000);
      // A newer turn of the person's, already finished, whose own event comes late: the waiting
      // service takes it over at its next read.
      const now = yield* DateTime.now;
      yield* change(
        (data) => ({
          ...data,
          runs: [
            ...data.runs,
            run({
              id: laterRunId,
              ordinal: data.runs.length + 1,
              userMessageId: MessageId.make("message-later"),
              requestedAt: now,
              startedAt: now,
              completedAt: now,
            }),
          ],
        }),
        "thread",
      );
      yield* wait(HANDOFF_SETTLE_MS - 1_000);
      expect(runIds()).toEqual([runId1, laterRunId]);
      yield* wait(1_000);
      expect(runIds()).toEqual([runId1, laterRunId, handoffRunId]);
      expect(yield* Queue.take(sends)).toBe(`handoff:${threadId}:${laterRunId}`);
    }),
  );
});

const runId1 = RunId.make("run-1");
const laterRunId = RunId.make("run-later");
const childId = "child-thread" as ThreadId;
const childLineage = {
  rootThreadId: threadId,
  parentThreadId: threadId,
  relationshipToParent: "subagent" as const,
};
