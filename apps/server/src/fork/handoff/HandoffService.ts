/**
 * Fork: hands a long thread off to a fresh session in the same thread (design 4.4). At each turn end
 * it asks `handoffDecision`, sleeps to the idle window or waits for a busy thread to change, settles
 * (`HANDOFF_SETTLE_MS`, watching the relay's view), then sends one fresh-context message through
 * `dispatchHandoff`, which drops or cancels a late one.
 */
import {
  CommandId,
  MessageId,
  type OrchestrationV2Run,
  type RunId,
  type ThreadId,
} from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FiberMap from "effect/FiberMap";
import * as FiberSet from "effect/FiberSet";
import * as Layer from "effect/Layer";
import * as Stream from "effect/Stream";

import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ProviderAdapterRegistry from "../../orchestration-v2/ProviderAdapterRegistry.ts";
import { forkParked } from "../../serverActivation.ts";
import * as ServerSettings from "../../serverSettings.ts";
import { type HandoffTrigger, handoffDecision } from "./handoffDecision.ts";
import { dispatchHandoff, readHandoffRecords } from "./handoffDispatch.ts";
import {
  HANDOFF_LOUD_SUFFIX,
  HANDOFF_RETRY_SUFFIX,
  handoffThreadFacts,
  parseHandoffMessageId,
} from "./handoffFacts.ts";
import { handoffMessageText } from "./handoffMessage.ts";
import { HANDOFF_SETTLE_CHECK_MS, HANDOFF_SETTLE_MS } from "./handoffQuiet.ts";
import { relayShowsFinish } from "./handoffRelayView.ts";
import { handoffInstanceBlocked, threadHandoff } from "./handoffOwner.ts";

/** Part 3 reads the turn items of this many latest runs; the budget keeps the newest of them. */
const TRANSCRIPT_RUNS = 20;

export const make = Effect.gen(function* () {
  const orchestrator = yield* Orchestrator.OrchestratorV2;
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  const serverSettings = yield* ServerSettings.ServerSettingsService;
  const adapters = yield* ProviderAdapterRegistry.ProviderAdapterRegistryV2;
  // One per waiting thread; any event of that thread completes it, so the wait reads again.
  const wakes = new Map<ThreadId, Deferred.Deferred<void>>();
  // Completed by a request or run change of any thread (a child's request shows on its parent), so
  // a settle checks the relay's view again at once; replaced each time.
  let anyRequestOrRun = Deferred.makeUnsafe<void>();
  const withStores = <A, E, R>(effect: Effect.Effect<A, E, R>) =>
    effect.pipe(
      Effect.provideService(Orchestrator.OrchestratorV2, orchestrator),
      Effect.provideService(ProjectionStore.ProjectionStoreV2, projections),
    );

  const readThread = Effect.fn("HandoffService.readThread")(function* (threadId: ThreadId) {
    const current = yield* serverSettings.getSettings;
    const thread = yield* projections.getThread(threadId);
    const { blocked, ...resolved } = yield* threadHandoff(current, thread).pipe(
      Effect.provideService(ProviderAdapterRegistry.ProviderAdapterRegistryV2, adapters),
    );
    // Off (the default) and `compact` read nothing more.
    if (resolved.mode !== "handoff") return null;
    // Same rule as the compaction wrapper (handoffOwner.ts): a handoff that cannot run sends
    // nothing, and idle compaction covers the thread instead. Read again at every step.
    if (blocked !== null) {
      yield* Effect.logWarning("handoff cannot run; idle compaction covers this thread", {
        threadId,
        ...blocked,
      });
      return null;
    }
    return { ...resolved, records: yield* withStores(readHandoffRecords(threadId)) };
  });

  const send = Effect.fn("HandoffService.send")(function* (
    threadId: ThreadId,
    run: OrchestrationV2Run,
    ids: { readonly commandId: string; readonly messageId: string },
    read: NonNullable<Effect.Success<ReturnType<typeof readThread>>>,
  ) {
    const recent = read.records.runs
      .toSorted((left, right) => right.ordinal - left.ordinal)
      .slice(0, TRANSCRIPT_RUNS)
      .map((recentRun) => recentRun.id);
    const more = yield* projections.getThreadRecords(
      threadId,
      ["attempts", "subagents", "turnItems"],
      { turnItemRunIds: recent, omitSettledSubagents: true },
    );
    const firstSends = new Set(
      read.records.runs.flatMap((other) => {
        const parsed = parseHandoffMessageId(threadId, other.userMessageId);
        return parsed === null ? [] : [parsed.runId];
      }),
    );
    // The setting, else the thread's current model, always written out in full.
    const modelSelection = read.settings.modelSelection ?? read.records.thread.modelSelection;
    yield* withStores(
      dispatchHandoff({
        type: "message.dispatch",
        commandId: CommandId.make(ids.commandId),
        messageId: MessageId.make(ids.messageId),
        threadId,
        text: handoffMessageText({
          records: { ...read.records, ...more },
          sourceRunId: run.id,
          number: firstSends.size + 1,
          nowIso: DateTime.formatIso(yield* DateTime.now),
          modelSelection,
        }),
        attachments: [],
        createdBy: "agent",
        creationSource: "server",
        freshContext: true,
        modelSelection,
        dispatchMode: { type: "start_immediately" },
      }),
    );
  });

  const handOffWhenDue = Effect.fn("HandoffService.handOffWhenDue")(
    function* (threadId: ThreadId, sourceRunId: RunId) {
      let runId = sourceRunId;
      let trigger: HandoffTrigger = "turn-end";
      // The settle under way: its fixed-length timer (a monotonic sleep, not wall-clock times) and
      // whether any check found the relay not showing the finish. Busy work or a sleep ends it.
      let settle: { readonly timer: Fiber.Fiber<void>; loud: boolean } | undefined;
      const endSettle = Effect.suspend(() => {
        const timer = settle?.timer;
        settle = undefined;
        return timer === undefined ? Effect.void : Fiber.interrupt(timer);
      });
      /** Unsure (a failed read) counts as not shown: the handoff then alerts. */
      const checkRelayView = Effect.suspend(() =>
        relayShowsFinish(threadId).pipe(
          Effect.provideService(Orchestrator.OrchestratorV2, orchestrator),
          Effect.catchCauseIf(
            (cause) => !Cause.hasInterruptsOnly(cause),
            (cause) =>
              Effect.logWarning("handoff relay view unread; the handoff run alerts", {
                threadId,
                cause: Cause.pretty(cause),
              }).pipe(Effect.as(false)),
          ),
          Effect.map((shown) => {
            if (!shown && settle !== undefined) settle.loud = true;
          }),
        ),
      );
      while (true) {
        // Set before the read, so a change between the read and the wait is not missed.
        const wake = yield* Deferred.make<void>();
        wakes.set(threadId, wake);
        const read = yield* readThread(threadId);
        if (read === null) return;
        const latest = handoffThreadFacts(read.records).lastRun;
        // A late update of an earlier run replaced the latest run's timer or wait: take it over, as
        // upstream's idle compaction does. That run's turn end decides again, with the same ids, and
        // settles afresh. A timer that wakes to a newer run leaves it to that run's own turn end.
        if (trigger === "turn-end" && latest?.state === "completed" && latest.id !== runId) {
          runId = latest.id as RunId;
          yield* endSettle;
          continue;
        }
        const nowMs = DateTime.toEpochMillis(yield* DateTime.now);
        const decision = handoffDecision({
          ...handoffThreadFacts(read.records),
          trigger,
          runId,
          threadId,
          mode: read.mode,
          source: read.settings.source,
          sizeTokens: read.settings.sizeTokens,
          idleTokens: read.settings.idleTokens,
          nowMs,
        });
        if (decision.kind !== "handoff") yield* endSettle;
        if (decision.kind === "skip") return;
        if (decision.kind === "sleep") {
          yield* Effect.sleep(Duration.millis(decision.untilMs - nowMs));
          trigger = "idle-timer";
          continue;
        }
        if (decision.kind === "wait") {
          yield* Effect.logInfo("handoff waiting", { threadId, blockers: decision.blockers });
          // Read again when the thread changes: the work ending decides, not only the next turn.
          yield* Deferred.await(wake);
          continue;
        }
        // Settle first, on upstream's own view: the relay holds a "finished" alert while a run may
        // continue, longer while a child, a PR watch or a request shows, and a handoff run that
        // starts in that hold drops the person's alert for their own turn. Only a settle that saw
        // the finish shown at every check sends a quiet handoff (handoffQuiet.ts); else it alerts.
        settle ??= {
          timer: yield* Effect.forkChild(Effect.sleep(Duration.millis(HANDOFF_SETTLE_MS))),
          loud: false,
        };
        yield* checkRelayView;
        if (settle.timer.pollUnsafe() === undefined) {
          // Until the settle ends: an event of this thread reads it all again (busy work ends the
          // settle); any thread's request or run, or a second passing, checks the relay's view.
          while (true) {
            const next = yield* Effect.raceFirst(
              Effect.raceFirst(
                Fiber.join(settle.timer).pipe(Effect.as("read" as const)),
                Deferred.await(wake).pipe(Effect.as("read" as const)),
              ),
              Effect.raceFirst(
                Deferred.await(anyRequestOrRun),
                Effect.sleep(Duration.millis(HANDOFF_SETTLE_CHECK_MS)),
              ).pipe(Effect.as("check" as const)),
            );
            if (next === "read") break;
            yield* checkRelayView;
          }
          continue;
        }
        const run = read.records.runs.find((candidate) => candidate.id === runId);
        const loud = settle.loud;
        settle = undefined;
        if (run !== undefined) {
          yield* send(
            threadId,
            run,
            {
              commandId: decision.commandId,
              messageId: loud ? `${decision.messageId}${HANDOFF_LOUD_SUFFIX}` : decision.messageId,
            },
            read,
          );
        }
        return;
      }
    },
    (effect, threadId) =>
      effect.pipe(
        Effect.catch((cause) => Effect.logWarning("handoff skipped", { threadId, cause })),
      ),
  );

  /** A handoff run that ended: keep the thread read, or send the same text once more if it failed. */
  const handoffRunEnded = Effect.fn("HandoffService.handoffRunEnded")(
    function* (
      run: OrchestrationV2Run,
      source: { readonly runId: string; readonly retry: boolean },
    ) {
      const threadId = run.threadId;
      if (run.status === "completed") {
        const thread = yield* projections.getThread(threadId);
        const { runs } = yield* projections.getThreadRecords(threadId, ["runs"], {
          runIds: [source.runId as RunId],
        });
        const sourceDone = runs[0]?.completedAt;
        // Read before the handoff stays read: the handoff's own completion is nothing new.
        if (
          run.completedAt !== null &&
          sourceDone != null &&
          thread.lastVisitedAt !== null &&
          DateTime.toEpochMillis(thread.lastVisitedAt) >= DateTime.toEpochMillis(sourceDone)
        ) {
          yield* orchestrator.dispatch({
            type: "thread.visit",
            commandId: CommandId.make(`server:handoff-read:${threadId}:${run.id}`),
            threadId,
            visitedAt: DateTime.formatIso(run.completedAt),
          });
        }
        return;
      }
      // A stopped handoff stays stopped: the person's Stop is not overridden by a retry.
      if (source.retry || run.status !== "failed") {
        return yield* Effect.logWarning("handoff not delivered; its text stays in the thread", {
          threadId,
          runId: run.id,
          status: run.status,
        });
      }
      const read = yield* readThread(threadId);
      const { messages } = yield* projections.getThreadRecords(threadId, ["messages"], {
        messageIds: [run.userMessageId],
      });
      const message = messages[0];
      if (read === null || message === undefined) return;
      // The retry sends on the run's own model, which may not be the one the setting names now:
      // check that instance too, by the same rule. Off or gone: no retry; its text stays.
      const blocked = yield* handoffInstanceBlocked(
        yield* serverSettings.getSettings,
        read.records.thread.projectId,
        run.modelSelection.instanceId,
      ).pipe(Effect.provideService(ProviderAdapterRegistry.ProviderAdapterRegistryV2, adapters));
      if (blocked !== null) {
        return yield* Effect.logWarning("handoff not sent again: its run's instance cannot run", {
          threadId,
          runId: run.id,
          ...blocked,
        });
      }
      yield* withStores(
        dispatchHandoff({
          type: "message.dispatch",
          commandId: CommandId.make(`server:${run.userMessageId}${HANDOFF_RETRY_SUFFIX}`),
          messageId: MessageId.make(`${run.userMessageId}${HANDOFF_RETRY_SUFFIX}`),
          threadId,
          text: message.text,
          attachments: [],
          createdBy: "agent",
          creationSource: "server",
          freshContext: true,
          modelSelection: run.modelSelection,
          dispatchMode: { type: "start_immediately" },
        }),
      );
    },
    (effect, run) =>
      effect.pipe(
        Effect.catch((cause) =>
          Effect.logWarning("handoff follow-up failed", { threadId: run.threadId, cause }),
        ),
      ),
  );

  const start = Effect.fn("HandoffService.start")(function* () {
    // Keyed by thread: a newer finished run replaces the waiting one. In memory: a restart drops it.
    const timers = yield* FiberMap.make<ThreadId>();
    // Off the event stream: a dispatch from inside its handler would wait on the stream itself.
    const followUps = yield* FiberSet.make();
    yield* forkParked(
      Stream.runForEach(orchestrator.streamDomainEvents, (event) =>
        Effect.gen(function* () {
          const wake = wakes.get(event.threadId);
          if (wake !== undefined) {
            wakes.delete(event.threadId);
            yield* Deferred.succeed(wake, undefined);
          }
          if (event.type === "runtime-request.updated" || event.type === "run.updated") {
            const changed = anyRequestOrRun;
            anyRequestOrRun = Deferred.makeUnsafe<void>();
            yield* Deferred.succeed(changed, undefined);
          }
          if (event.type !== "run.updated") return;
          const run = event.payload;
          const handoff = parseHandoffMessageId(run.threadId, run.userMessageId);
          if (handoff !== null) {
            if (
              run.status === "completed" ||
              run.status === "failed" ||
              run.status === "interrupted"
            )
              yield* FiberSet.run(followUps, handoffRunEnded(run, handoff));
            return;
          }
          if (run.status === "completed") {
            yield* FiberMap.run(timers, event.threadId, handOffWhenDue(event.threadId, run.id));
          }
        }),
      ),
    );
  });
  return { start };
});

/** Started with the server's other workers, from `ForkLayers.layerRuntimeServices` (fork/forkLayers.ts). */
export const layer = Layer.effectDiscard(
  make.pipe(Effect.flatMap((service) => service.start())),
).pipe(
  Layer.provide(ProjectionStore.layer),
  // Upstream's own lookup over the server's instance registry, as server.ts gives /mcp.
  Layer.provide(ProviderAdapterRegistry.layerFromProviderInstanceRegistry),
);
