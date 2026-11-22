/**
 * Fork (issue 925): a finished delegated child's own provider session is stopped after the grace
 * time, through upstream's idle release. Never while it works, waits or asks; never a shared
 * session or a provider's own subagent; a second stop finds nothing; a later run end arms again.
 */
import { describe, expect, it } from "@effect/vitest";
import {
  type OrchestrationV2DomainEvent,
  type OrchestrationV2ProviderSession,
  type OrchestrationV2ProviderThread,
  type OrchestrationV2Run,
  type OrchestrationV2Subagent,
  type OrchestrationV2ThreadShell,
  ProviderSessionId,
  ThreadId,
} from "@t3tools/contracts";
import * as Deferred from "effect/Deferred";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as PubSub from "effect/PubSub";
import * as Stream from "effect/Stream";
import { TestClock } from "effect/testing";

import * as McpToolAccessTestkit from "../mcp/McpToolAccess.testkit.ts";
import * as ProviderSessionManager from "../orchestration-v2/ProviderSessionManager.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import { Waits } from "../waits/WaitWorker.ts";
import { delegatedTask, PARENT } from "./delegatedResults/delegatedResults.testkit.ts";
import * as StopFinishedChildren from "./stopFinishedChildren.ts";

const CHILD = ThreadId.make("child-task-1");
const SESSION = ProviderSessionId.make("session:child");
const GRACE = 60;

const idleShell = (
  id: ThreadId,
  overrides: Partial<OrchestrationV2ThreadShell> = {},
): OrchestrationV2ThreadShell => ({
  ...McpToolAccessTestkit.liveThreadShell(id, { activeRunId: null }),
  status: "idle",
  lineage: { rootThreadId: PARENT, parentThreadId: PARENT, relationshipToParent: "subagent" },
  ...overrides,
});

const session = (id: ProviderSessionId, shared = false): OrchestrationV2ProviderSession =>
  ({
    id,
    status: "ready",
    capabilities: { sessions: { supportsMultipleProviderThreadsPerSession: shared } },
  }) as unknown as OrchestrationV2ProviderSession;

const providerThread = (
  appThreadId: ThreadId,
  providerSessionId: ProviderSessionId,
): OrchestrationV2ProviderThread =>
  ({
    id: `pt:${appThreadId}`,
    appThreadId,
    providerSessionId,
  }) as unknown as OrchestrationV2ProviderThread;

interface Release {
  readonly providerSessionId: ProviderSessionId;
  readonly reason: string;
  readonly kept: boolean;
}

/** The child and its parent as the server keeps them, and one live Claude-like session. */
const makeWorld = (stopFinishedChildren: { enabled: boolean; graceSeconds?: number }) => {
  const shells = new Map<ThreadId, OrchestrationV2ThreadShell>([
    [
      PARENT,
      idleShell(PARENT, {
        lineage: { rootThreadId: PARENT, parentThreadId: null, relationshipToParent: null },
      }),
    ],
    [CHILD, idleShell(CHILD)],
  ]);
  const tasks: Array<OrchestrationV2Subagent> = [delegatedTask()];
  /** Tasks the child itself delegated. */
  const childTasks: Array<OrchestrationV2Subagent> = [];
  const runs: Array<Pick<OrchestrationV2Run, "status">> = [];
  const state = {
    sessions: [session(SESSION)],
    providerThreads: [providerThread(CHILD, SESSION)],
    openWaits: 0,
    /** The wait store could not be read. */
    storeError: false,
    backgroundWork: false,
    /** Run while upstream decides, as a turn start in that same step would. */
    beforeKeepIf: null as null | (() => void),
  };
  const live = new Set<ProviderSessionId>([SESSION]);
  const releases: Array<Release> = [];
  const released = Deferred.makeUnsafe<void>();
  const layer = (events: Stream.Stream<OrchestrationV2DomainEvent>) =>
    Layer.mergeAll(
      ServerSettingsService.layerTest({
        stopFinishedChildren: { graceSeconds: GRACE, ...stopFinishedChildren },
      }),
      Layer.mock(ThreadManagement.ThreadManagementService)({
        streamDomainEvents: events,
        getThreadShell: (threadId) => Effect.succeed(shells.get(threadId) ?? null),
        getThreadRecords: ((threadId: ThreadId, fields: ReadonlyArray<string>) =>
          Effect.succeed({
            thread: {},
            ...(fields.includes("runs") ? { runs: threadId === CHILD ? runs : [] } : {}),
            ...(fields.includes("subagents")
              ? { subagents: threadId === PARENT ? tasks : threadId === CHILD ? childTasks : [] }
              : {}),
            ...(fields.includes("providerSessions")
              ? { providerSessions: threadId === CHILD ? state.sessions : [] }
              : {}),
            ...(fields.includes("providerThreads")
              ? { providerThreads: threadId === CHILD ? state.providerThreads : [] }
              : {}),
          })) as never,
      }),
      Layer.mock(ProviderSessionManager.ProviderSessionManagerV2)({
        isLive: (id) => Effect.succeed(live.has(id)),
        get: (id) =>
          Effect.succeed(
            live.has(id)
              ? Option.some({
                  hasPendingBackgroundWork: Effect.sync(() => state.backgroundWork),
                } as never)
              : Option.none(),
          ),
        releaseUnlessBusy: (input) =>
          Effect.gen(function* () {
            if (!live.has(input.providerSessionId)) return false;
            state.beforeKeepIf?.();
            const kept = input.keepIf === undefined ? false : yield* input.keepIf;
            releases.push({
              providerSessionId: input.providerSessionId,
              reason: input.reason,
              kept,
            });
            if (kept) return false;
            live.delete(input.providerSessionId);
            yield* Deferred.succeed(released, undefined);
            return true;
          }),
      }),
      Layer.mock(Waits)({
        list: () =>
          Effect.succeed({
            enabled: true,
            checks: [],
            waits: Array.from({ length: state.openWaits }, () => ({}) as never),
            ...(state.storeError ? { storeError: "unreadable" } : {}),
          }),
      }),
    );
  return { shells, tasks, childTasks, runs, state, live, releases, released, layer };
};

type World = ReturnType<typeof makeWorld>;

const service = (world: World, events: Stream.Stream<OrchestrationV2DomainEvent> = Stream.empty) =>
  StopFinishedChildren.make().pipe(Effect.provide(world.layer(events)));

const settle = Effect.repeat(Effect.yieldNow, { times: 20 });

const event = (type: string, threadId: ThreadId, payload: unknown) =>
  ({ type, threadId, payload }) as unknown as OrchestrationV2DomainEvent;

const stoppedOnce = { type: "stopped", providerSessionIds: [SESSION] };

describe("StopFinishedChildren.check", () => {
  it.effect("stops an idle finished child's own session through upstream's idle release", () =>
    Effect.gen(function* () {
      const world = makeWorld({ enabled: true });
      const reaper = yield* service(world);
      expect(yield* reaper.check(CHILD)).toEqual(stoppedOnce);
      expect(world.releases).toEqual([
        { providerSessionId: SESSION, reason: "idle_timeout", kept: false },
      ]);
    }).pipe(Effect.scoped),
  );

  it.effect("does nothing while the setting is off (a machine that never names it)", () =>
    Effect.gen(function* () {
      const world = makeWorld({ enabled: false });
      const reaper = yield* service(world);
      expect(yield* reaper.check(CHILD)).toEqual({ type: "off" });
      expect(world.releases).toEqual([]);
    }).pipe(Effect.scoped),
  );

  it.effect(
    "never stops a child that works, asks, waits, runs background work or has an open task",
    () =>
      Effect.gen(function* () {
        const cases: ReadonlyArray<readonly [string, (world: World) => void, string]> = [
          [
            "a run",
            (world) =>
              world.shells.set(
                CHILD,
                idleShell(CHILD, { activityRunStatus: "running", status: "running" }),
              ),
            "busy",
          ],
          ["a queued run", (world) => world.runs.push({ status: "queued" }), "busy"],
          [
            "a question",
            (world) =>
              world.shells.set(CHILD, idleShell(CHILD, { pendingRuntimeRequest: {} as never })),
            "busy",
          ],
          [
            "an open follow-up task",
            (world) =>
              world.tasks.push(
                delegatedTask({ id: "task-2", childThreadId: CHILD, status: "running" }),
              ),
            "busy",
          ],
          [
            "a task it delegated itself, still open",
            (world) =>
              world.childTasks.push(
                delegatedTask({
                  id: "grandchild-task",
                  threadId: CHILD,
                  childThreadId: ThreadId.make("grandchild"),
                  status: "running",
                }),
              ),
            "busy",
          ],
          ["a wait", (world) => (world.state.openWaits = 1), "waiting"],
          [
            "a wait store that cannot be read",
            (world) => (world.state.storeError = true),
            "waiting",
          ],
          ["background work", (world) => (world.state.backgroundWork = true), "busy"],
        ];
        for (const [name, set, expected] of cases) {
          const world = makeWorld({ enabled: true });
          set(world);
          const reaper = yield* service(world);
          expect({ name, outcome: yield* reaper.check(CHILD) }).toEqual({
            name,
            outcome: { type: expected },
          });
          expect(world.releases).toEqual([]);
          expect(reaper.watched.has(CHILD)).toBe(true);
        }
      }).pipe(Effect.scoped),
  );

  it.effect("keeps the session when the child is woken in the same step as the release", () =>
    Effect.gen(function* () {
      const world = makeWorld({ enabled: true });
      world.state.beforeKeepIf = () => world.runs.push({ status: "starting" });
      const reaper = yield* service(world);
      expect(yield* reaper.check(CHILD)).toEqual({ type: "none" });
      expect(world.releases).toEqual([
        { providerSessionId: SESSION, reason: "idle_timeout", kept: true },
      ]);
      expect(world.live.has(SESSION)).toBe(true);
    }).pipe(Effect.scoped),
  );

  it.effect("leaves a shared session, and a provider's own subagent, to upstream", () =>
    Effect.gen(function* () {
      const shared = makeWorld({ enabled: true });
      shared.state.sessions = [session(SESSION, true)];
      expect(yield* (yield* service(shared)).check(CHILD)).toEqual({ type: "none" });
      expect(shared.releases).toEqual([]);

      const native = makeWorld({ enabled: true });
      native.tasks.splice(0, 1, delegatedTask({ origin: "provider_native" }));
      expect(yield* (yield* service(native)).check(CHILD)).toEqual({ type: "notChild" });
      expect(native.releases).toEqual([]);
    }).pipe(Effect.scoped),
  );

  it.effect("stops only a session the child's own provider thread names", () =>
    Effect.gen(function* () {
      const world = makeWorld({ enabled: true });
      // The session is in the child's records, but only another thread's provider thread names it.
      world.state.providerThreads = [providerThread(PARENT, SESSION)];
      expect(yield* (yield* service(world)).check(CHILD)).toEqual({ type: "none" });
      expect(world.releases).toEqual([]);
      expect(world.live.has(SESSION)).toBe(true);
    }).pipe(Effect.scoped),
  );

  it.effect("stops a child whose own delegated tasks have all ended", () =>
    Effect.gen(function* () {
      const world = makeWorld({ enabled: true });
      world.childTasks.push(
        delegatedTask({
          id: "grandchild-task",
          threadId: CHILD,
          childThreadId: ThreadId.make("grandchild"),
        }),
      );
      expect(yield* (yield* service(world)).check(CHILD)).toEqual(stoppedOnce);
    }).pipe(Effect.scoped),
  );

  it.effect("a second stop finds nothing live and releases nothing more", () =>
    Effect.gen(function* () {
      const world = makeWorld({ enabled: true });
      const reaper = yield* service(world);
      expect(yield* reaper.check(CHILD)).toEqual(stoppedOnce);
      expect(yield* reaper.check(CHILD)).toEqual({ type: "none" });
      expect(world.releases).toHaveLength(1);
    }).pipe(Effect.scoped),
  );

  it.effect("stops a child a linked machine delegated here once its run is over", () =>
    Effect.gen(function* () {
      const world = makeWorld({ enabled: true });
      world.shells.set(
        CHILD,
        idleShell(CHILD, {
          lineage: { rootThreadId: CHILD, parentThreadId: null, relationshipToParent: null },
          delegatedFrom: { environmentId: "env-parent" as never, threadId: PARENT, title: "p" },
        }),
      );
      world.tasks.length = 0;
      expect(yield* (yield* service(world)).check(CHILD)).toEqual(stoppedOnce);

      const plain = makeWorld({ enabled: true });
      plain.shells.set(
        CHILD,
        idleShell(CHILD, {
          lineage: { rootThreadId: CHILD, parentThreadId: null, relationshipToParent: null },
        }),
      );
      expect(yield* (yield* service(plain)).check(CHILD)).toEqual({ type: "notChild" });
    }).pipe(Effect.scoped),
  );
});

describe("StopFinishedChildren.start", () => {
  it.effect("the task's end arms one check after the grace time; a run start cancels it", () =>
    Effect.gen(function* () {
      const world = makeWorld({ enabled: true });
      const pubsub = yield* PubSub.unbounded<OrchestrationV2DomainEvent>();
      const reaper = yield* service(world, Stream.fromPubSub(pubsub));
      const fiber = yield* reaper.start().pipe(Effect.andThen(Effect.never), Effect.forkScoped);
      yield* settle;

      yield* PubSub.publish(pubsub, event("subagent.updated", PARENT, delegatedTask()));
      yield* settle;
      yield* TestClock.adjust(Duration.seconds(GRACE - 1));
      expect(world.releases).toEqual([]);
      // Woken during the grace time: no stop, and the new run's end starts a new grace.
      yield* PubSub.publish(pubsub, event("run.created", CHILD, { status: "starting" }));
      yield* settle;
      yield* TestClock.adjust(Duration.seconds(GRACE));
      expect(world.releases).toEqual([]);

      yield* PubSub.publish(pubsub, event("run.updated", CHILD, { status: "completed" }));
      yield* settle;
      yield* TestClock.adjust(Duration.seconds(GRACE));
      yield* Deferred.await(world.released);
      expect(world.releases).toEqual([
        { providerSessionId: SESSION, reason: "idle_timeout", kept: false },
      ]);
      yield* Fiber.interrupt(fiber);
    }).pipe(Effect.scoped),
  );

  it.effect("a child found asking is checked again when its question is answered", () =>
    Effect.gen(function* () {
      const world = makeWorld({ enabled: true });
      world.shells.set(CHILD, idleShell(CHILD, { pendingRuntimeRequest: {} as never }));
      const pubsub = yield* PubSub.unbounded<OrchestrationV2DomainEvent>();
      const reaper = yield* service(world, Stream.fromPubSub(pubsub));
      const fiber = yield* reaper.start().pipe(Effect.andThen(Effect.never), Effect.forkScoped);
      yield* settle;

      yield* PubSub.publish(pubsub, event("subagent.updated", PARENT, delegatedTask()));
      yield* settle;
      yield* TestClock.adjust(Duration.seconds(GRACE));
      yield* settle;
      expect(world.releases).toEqual([]);
      expect(reaper.watched.has(CHILD)).toBe(true);

      world.shells.set(CHILD, idleShell(CHILD));
      yield* PubSub.publish(pubsub, event("runtime-request.updated", CHILD, {}));
      yield* settle;
      yield* TestClock.adjust(Duration.seconds(GRACE));
      yield* Deferred.await(world.released);
      expect(world.releases).toHaveLength(1);
      expect(reaper.watched.has(CHILD)).toBe(false);
      yield* Fiber.interrupt(fiber);
    }).pipe(Effect.scoped),
  );

  it.effect("arms nothing while the setting is off", () =>
    Effect.gen(function* () {
      const world = makeWorld({ enabled: false });
      const pubsub = yield* PubSub.unbounded<OrchestrationV2DomainEvent>();
      const reaper = yield* service(world, Stream.fromPubSub(pubsub));
      const fiber = yield* reaper.start().pipe(Effect.andThen(Effect.never), Effect.forkScoped);
      yield* settle;
      yield* PubSub.publish(pubsub, event("subagent.updated", PARENT, delegatedTask()));
      yield* settle;
      yield* TestClock.adjust(Duration.seconds(GRACE * 10));
      yield* settle;
      expect(world.releases).toEqual([]);
      expect(world.live.has(SESSION)).toBe(true);
      yield* Fiber.interrupt(fiber);
    }).pipe(Effect.scoped),
  );
});
