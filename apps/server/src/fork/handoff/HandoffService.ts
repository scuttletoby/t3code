/**
 * Fork: hands a long thread off to a fresh session in the same thread (design 4.4). At each turn end
 * it asks `handoffDecision`, sleeps to the idle window or waits for a busy thread to change, then
 * sends one fresh-context message through `dispatchHandoff`, which drops or cancels a late one.
 */
import {
  CommandId,
  MessageId,
  type OrchestrationV2Run,
  resolveHandoffSettings,
  type RunId,
  type ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as FiberMap from "effect/FiberMap";
import * as FiberSet from "effect/FiberSet";
import * as Layer from "effect/Layer";
import * as Stream from "effect/Stream";

import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import { forkParked } from "../../serverActivation.ts";
import * as ServerSettings from "../../serverSettings.ts";
import { type HandoffTrigger, handoffDecision } from "./handoffDecision.ts";
import { dispatchHandoff, readHandoffRecords } from "./handoffDispatch.ts";
import { HANDOFF_RETRY_SUFFIX, handoffThreadFacts, parseHandoffMessageId } from "./handoffFacts.ts";
import { handoffMessageText } from "./handoffMessage.ts";

/** Part 3 reads the turn items of this many latest runs; the budget keeps the newest of them. */
const TRANSCRIPT_RUNS = 20;

export const make = Effect.gen(function* () {
  const orchestrator = yield* Orchestrator.OrchestratorV2;
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  const serverSettings = yield* ServerSettings.ServerSettingsService;
  // One per waiting thread; any event of that thread completes it, so the wait reads again.
  const wakes = new Map<ThreadId, Deferred.Deferred<void>>();
  const withStores = <A, E, R>(effect: Effect.Effect<A, E, R>) =>
    effect.pipe(
      Effect.provideService(Orchestrator.OrchestratorV2, orchestrator),
      Effect.provideService(ProjectionStore.ProjectionStoreV2, projections),
    );

  const readThread = Effect.fn("HandoffService.readThread")(function* (threadId: ThreadId) {
    const current = yield* serverSettings.getSettings;
    const thread = yield* projections.getThread(threadId);
    const resolved = resolveHandoffSettings(
      current.handoff,
      current.projectSettingsOverrides[thread.projectId]?.handoff,
      threadId,
    );
    // Off (the default) and `compact` read nothing more.
    if (resolved.mode !== "handoff") return null;
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
      while (true) {
        // Set before the read, so a change between the read and the wait is not missed.
        const wake = yield* Deferred.make<void>();
        wakes.set(threadId, wake);
        const read = yield* readThread(threadId);
        if (read === null) return;
        const latest = handoffThreadFacts(read.records).lastRun;
        // A late update of an earlier run replaced the latest run's timer or wait: take it over, as
        // upstream's idle compaction does. That run's turn end decides again, with the same ids. A
        // timer that wakes to a newer run leaves it to that run's own turn end (`newer-run`).
        if (trigger === "turn-end" && latest?.state === "completed" && latest.id !== runId) {
          runId = latest.id as RunId;
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
        const run = read.records.runs.find((candidate) => candidate.id === runId);
        if (run !== undefined) yield* send(threadId, run, decision, read);
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
).pipe(Layer.provide(ProjectionStore.layer));
