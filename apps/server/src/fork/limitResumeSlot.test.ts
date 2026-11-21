/**
 * Fork: agent slots (issue 711) for upstream's usage-limit resume (fork/limitResumeSlot.ts):
 * upstream's own recovery worker and scheduler, through the wrapper, over a gate with a set
 * number of free slots. At a reset the resumes over the limit wait; each is sent once, as slots
 * free, also after a restart. A resume long past its reset still waits and keeps its one id.
 */
import { assert, describe, expect, it } from "@effect/vitest";
import {
  type OrchestrationV2ServerCommand,
  ProviderInstanceId,
  RunId,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Stream from "effect/Stream";
import * as TestClock from "effect/testing/TestClock";
import * as NodeServices from "@effect/platform-node/NodeServices";

import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as UsageLimitRecoveryWorker from "../orchestration-v2/UsageLimitRecoveryWorker.ts";
import { limitRecoveryCommand } from "../orchestration-v2/UsageLimitRecoveryWorker.ts";
import * as Scheduler from "../scheduling/Scheduler.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as WaitWorker from "../waits/WaitWorker.ts";
import * as LimitResumeSlot from "./limitResumeSlot.ts";

const INSTANCE = ProviderInstanceId.make("codex");
const RESET_MS = Date.parse("2026-10-19T12:00:00.000Z");

/** A thread that failed on a usage limit, armed to resume at `RESET_MS`. */
const armed = (name: string): ProjectionStore.ProjectionLimitRecoveryCandidate => {
  const failedAt = DateTime.makeUnsafe(RESET_MS - 60 * 60 * 1000);
  const resetAt = DateTime.formatIso(DateTime.makeUnsafe(RESET_MS));
  const runId = RunId.make(`run:${name}`);
  return {
    id: ThreadId.make(`thread:${name}`),
    status: "failed",
    lastErrorClass: "usage_limit",
    usageLimitResetAt: resetAt,
    latestRunId: runId,
    latestRunCompletedAt: failedAt,
    updatedAt: failedAt,
    archivedAt: null,
    settledOverride: null,
    pendingRuntimeRequest: null,
    limitRecovery: { runId, resetAt, autoResume: true, snooze: false },
    snoozedUntil: null,
  } as ProjectionStore.ProjectionLimitRecoveryCandidate;
};

const threadOf = (command: OrchestrationV2ServerCommand) =>
  "threadId" in command ? command.threadId : undefined;

/** A gate with `free` slots: each allowed start takes one; the test frees them. */
const countingGate = (free: number) => {
  const state = { free, asks: [] as Array<SlotGate.SlotAcquireInput> };
  const layer = Layer.mock(SlotGate.SlotGate)({
    acquire: (input) =>
      Effect.sync((): SlotGate.SlotAcquire => {
        state.asks.push(input);
        if (state.free === 0)
          return {
            type: "refuse",
            message: "full",
            refusal: { type: "unread", problem: "timeout" },
          };
        state.free -= 1;
        return { type: "allow", token: state.asks.length };
      }),
    started: () => Effect.void,
    release: () => Effect.void,
  });
  return { state, layer };
};

const infoLogs = (into: Array<string>) =>
  Logger.layer(
    [
      Logger.make(({ message, logLevel }) => {
        if (logLevel === "Info") into.push(JSON.stringify(message));
      }),
    ],
    { mergeWithExisting: false },
  );

/**
 * Upstream's worker over `candidates` (a resumed thread leaves them, as its run then shows) and
 * a thread management that records each resume, through the wrapper over `gate`.
 */
const worker = (
  candidates: Array<ProjectionStore.ProjectionLimitRecoveryCandidate>,
  sent: Array<OrchestrationV2ServerCommand>,
  gate: Layer.Layer<SlotGate.SlotGate>,
) =>
  UsageLimitRecoveryWorker.layer.pipe(
    Layer.provide(LimitResumeSlot.layerOver),
    Layer.provide(
      Layer.mergeAll(
        Scheduler.layer,
        gate,
        Layer.mock(ProjectionStore.ProjectionStoreV2)({
          getLimitRecoveryCandidates: () => Effect.sync(() => [...candidates]),
          getLimitRecoveryWakeAt: () => Effect.succeed(null),
        }),
        Layer.mock(ThreadManagement.ThreadManagementService)({
          getThreadShell: (threadId) =>
            Effect.succeed({ id: threadId, modelSelection: { instanceId: INSTANCE } } as never),
          dispatch: (command) =>
            Effect.sync(() => {
              sent.push(command);
              const at = candidates.findIndex((thread) => thread.id === threadOf(command));
              if (at >= 0) candidates.splice(at, 1);
              return { sequence: sent.length, storedEvents: [] };
            }),
        }),
        Layer.mock(Orchestrator.OrchestratorV2)({ streamDomainEvents: Stream.never }),
        Layer.mock(ServerSettings.ServerSettingsService)({
          getSettings: Effect.succeed({
            autoResumeLimitedThreads: true,
            snoozeLimitedThreads: false,
          } as never),
          streamChanges: Stream.never,
        }),
      ),
    ),
  );

const resumedThreads = (sent: ReadonlyArray<OrchestrationV2ServerCommand>) => sent.map(threadOf);

describe("usage-limit resumes take an agent slot", () => {
  it.effect("at a reset only the free slots resume; the rest wait and resume once each", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(RESET_MS);
      const candidates = [armed("a"), armed("b"), armed("c")];
      const sent: Array<OrchestrationV2ServerCommand> = [];
      const gate = countingGate(1);
      const logs: Array<string> = [];
      yield* Effect.gen(function* () {
        // The first tick runs at once: one slot, one resume.
        yield* TestClock.adjust("0 seconds");
        expect(resumedThreads(sent)).toEqual([ThreadId.make("thread:a")]);
        // Ticks with no free slot: nothing more, logged once per waiting resume.
        yield* TestClock.adjust("1 minute");
        expect(resumedThreads(sent)).toEqual([ThreadId.make("thread:a")]);
        expect(logs.filter((log) => log.includes("resume waits for an agent slot"))).toHaveLength(
          2,
        );
        gate.state.free = 1;
        yield* TestClock.adjust("5 seconds");
        expect(resumedThreads(sent)).toEqual([
          ThreadId.make("thread:a"),
          ThreadId.make("thread:b"),
        ]);
        gate.state.free = 5;
        yield* TestClock.adjust("5 seconds");
        yield* TestClock.adjust("1 minute");
        expect(resumedThreads(sent)).toEqual(["thread:a", "thread:b", "thread:c"]);
        // Each with upstream's one id per failed run and reset.
        expect(sent.map((command) => command.commandId)).toEqual(
          [armed("a"), armed("b"), armed("c")].map(
            (thread) => limitRecoveryCommand(thread, true, RESET_MS)?.commandId,
          ),
        );
        // Each asked as the thread's own start.
        expect(gate.state.asks[0]).toEqual({
          callerThreadId: ThreadId.make("thread:a"),
          targetInstanceId: INSTANCE,
          intoThreadId: ThreadId.make("thread:a"),
          keepsHeld: true,
        });
      }).pipe(
        Effect.provide(
          worker(candidates, sent, gate.layer).pipe(Layer.provideMerge(infoLogs(logs))),
        ),
      );
    }),
  );

  it.effect("a restart while resumes wait loses none and sends none twice", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(RESET_MS);
      const candidates = [armed("d"), armed("e")];
      const sent: Array<OrchestrationV2ServerCommand> = [];
      const gate = countingGate(0);
      yield* TestClock.adjust("0 seconds").pipe(
        Effect.andThen(TestClock.adjust("30 seconds")),
        Effect.provide(worker(candidates, sent, gate.layer)),
      );
      expect(sent).toEqual([]);
      // A new server: the stored threads are still failed and armed.
      gate.state.free = 1;
      yield* Effect.gen(function* () {
        yield* TestClock.adjust("0 seconds");
        expect(resumedThreads(sent)).toEqual(["thread:d"]);
        gate.state.free = 1;
        yield* TestClock.adjust("5 seconds");
        yield* TestClock.adjust("1 minute");
        expect(resumedThreads(sent)).toEqual(["thread:d", "thread:e"]);
      }).pipe(Effect.provide(worker(candidates, sent, gate.layer)));
      yield* TestClock.adjust("0 seconds").pipe(
        Effect.andThen(TestClock.adjust("1 minute")),
        Effect.provide(worker(candidates, sent, countingGate(5).layer)),
      );
      expect(resumedThreads(sent)).toEqual(["thread:d", "thread:e"]);
    }),
  );

  it("an armed resume days past its reset is still sent, with the same id", () => {
    const thread = armed("late");
    const atReset = limitRecoveryCommand(thread, true, RESET_MS);
    const late = limitRecoveryCommand(thread, true, RESET_MS + 3 * 24 * 60 * 60 * 1000);
    assert.equal(late?.type, "message.dispatch");
    assert.equal(late?.commandId, atReset?.commandId);
  });

  it.effect(
    "other commands pass; a thread that cannot be read waits; with no gate it is upstream's",
    () =>
      Effect.gen(function* () {
        const sent: Array<OrchestrationV2ServerCommand> = [];
        const inner = {
          dispatch: (command: OrchestrationV2ServerCommand) =>
            Effect.sync(() => {
              sent.push(command);
              return { sequence: 1, storedEvents: [] };
            }),
        } as unknown as ThreadManagement.ThreadManagementService["Service"];
        const gate = countingGate(0);
        const slots = yield* Effect.serviceOption(SlotGate.SlotGate).pipe(
          Effect.provide(gate.layer),
        );
        const resume = limitRecoveryCommand(armed("f"), true, RESET_MS)!;
        const other = { type: "thread.stop", threadId: ThreadId.make("thread:f") } as never;
        const unreadable = LimitResumeSlot.gateLimitResumes(inner, slots, () =>
          Effect.fail("down"),
        );
        expect(yield* unreadable.dispatch(other)).toEqual({ sequence: 1, storedEvents: [] });
        expect(yield* unreadable.dispatch(resume as never)).toEqual({
          sequence: 0,
          storedEvents: [],
        });
        expect(sent).toEqual([other]);
        expect(gate.state.asks).toEqual([]);
        // A thread that is gone is upstream's to refuse.
        const gone = LimitResumeSlot.gateLimitResumes(inner, slots, () => Effect.succeed(null));
        yield* gone.dispatch(resume as never);
        expect(sent).toEqual([other, resume]);
        expect(
          LimitResumeSlot.gateLimitResumes(inner, Option.none(), () => Effect.succeed(null)),
        ).toBe(inner);
      }),
  );

  it.effect("runtime wiring: upstream's recovery worker gets the gated service first", () =>
    Effect.gen(function* () {
      expect(WaitWorker.limitResumeSlot).toBe(LimitResumeSlot.layer);
      const path = yield* Path.Path;
      const fileSystem = yield* FileSystem.FileSystem;
      const runtime = yield* fileSystem.readFileString(
        yield* path.fromFileUrl(new URL("../orchestration-v2/runtimeLayer.ts", import.meta.url)),
      );
      const start = runtime.indexOf("  UsageLimitRecoveryWorker.layer.pipe(\n");
      expect(start).toBeGreaterThan(-1);
      const firstInPipe = runtime.slice(
        start,
        runtime.indexOf("\n", runtime.indexOf("\n", start) + 1),
      );
      expect(firstInPipe).toContain("Layer.provide(WaitWorker.limitResumeSlot)");
      expect(runtime.split("UsageLimitRecoveryWorker.layer")).toHaveLength(2);
    }).pipe(Effect.provide(NodeServices.layer)),
  );
});
