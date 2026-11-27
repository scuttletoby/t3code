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
  type OrchestrationV2TurnItem,
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
import * as Context from "effect/Context";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Stream from "effect/Stream";

import * as EffectOutbox from "../../orchestration-v2/EffectOutbox.ts";
import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ProviderAdapterRegistry from "../../orchestration-v2/ProviderAdapterRegistry.ts";
import { continuationStartRetryDelayMs } from "../../orchestration-v2/RestartBackgroundNote.ts";
import { forkParked } from "../../serverActivation.ts";
import * as ServerSettings from "../../serverSettings.ts";
import * as SlotGate from "../../slots/SlotGate.ts";
import {
  HANDOFF_AGENT_MARKER,
  HANDOFF_WRITE_STOP_REASON,
  type HandoffAgentResult,
  handoffAgentReply,
  handoffWriteJoined,
  handoffWriteText,
  handoffWriteTimedOut,
} from "./handoffAgent.ts";
import * as HandoffCommand from "./handoffCommand.ts";
import {
  HANDOFF_IDLE_LEAD_MS,
  type HandoffBlocker,
  type HandoffSkipReason,
  type HandoffTrigger,
  handoffDecision,
} from "./handoffDecision.ts";
import { leaveLine } from "../agentSlotRoutes.ts";
import {
  dispatchHandoff,
  handoffPlace,
  type HandoffWaitsForSlot,
  readHandoffRecords,
} from "./handoffDispatch.ts";
import {
  HANDOFF_LOUD_SUFFIX,
  HANDOFF_MESSAGE_PREFIX,
  HANDOFF_RETRY_SUFFIX,
  handoffThreadFacts,
  handoffWriteChain,
  handoffWriteIds,
  latestStartedRun,
  parseHandoffMessageId,
  parseHandoffWriteMessageId,
} from "./handoffFacts.ts";
import { handoffMessageText } from "./handoffMessage.ts";
import { HANDOFF_SETTLE_CHECK_MS, HANDOFF_SETTLE_MS } from "./handoffQuiet.ts";
import { relayShowsFinish } from "./handoffRelayView.ts";
import { handoffInstanceBlocked, threadHandoff } from "./handoffOwner.ts";

/** Part 3 reads the turn items of this many latest runs; the budget keeps the newest of them. */
const TRANSCRIPT_RUNS = 20;

/** A handoff refused a slot asks again after this (or at the thread's next event). */
export const HANDOFF_SLOT_RETRY = Duration.seconds(5);

/**
 * A write turn not ended this long after it was requested (the idle window the agent source starts
 * in) is stopped, and does not count. From its request, so a restart does not give it more time.
 */
export const HANDOFF_WRITE_TIMEOUT = Duration.millis(HANDOFF_IDLE_LEAD_MS.agent);
/** After T3 asked it to stop, how long the wait goes on for the write turn to end. */
export const HANDOFF_WRITE_STOP_WAIT = Duration.minutes(1);
const WRITE_ENDED = new Set(["completed", "interrupted", "failed", "cancelled", "rolled_back"]);
/**
 * A write turn that ended this way may have a restart continuation: upstream cancels a run
 * a restart cut short, and continues again a continuation that failed before its prompt.
 */
const CUT_SHORT = new Set(["cancelled", "failed"]);
/**
 * Upstream's outbox row that starts a run's restart continuation (`Orchestrator.ts`
 * `retryContinuationStart`, `ProviderRuntimeRecoveryService.ts`). Upstream does not export the id:
 * `handoffContinuationRow.test.ts` checks it against upstream's restart writer.
 */
export const continuationEffectId = (runId: string) => `effect:restart-continuation:${runId}`;
/**
 * A continuation that failed before its prompt gets upstream's retry row just after its failure
 * (no thread event marks it): read again this often until it is there.
 */
const CONTINUATION_RECHECK = Duration.seconds(5);

/**
 * A write turn's chain (`handoffWriteChain`) as one turn's items, in order: the continuations'
 * prompts are upstream's, not a message that joined the write turn, so they are left out.
 */
function chainItems(
  chain: ReadonlyArray<OrchestrationV2Run>,
  items: ReadonlyArray<OrchestrationV2TurnItem>,
): ReadonlyArray<OrchestrationV2TurnItem> {
  const position = new Map(chain.map((member, index) => [member.id as string, index]));
  const prompts = new Set<string>(chain.slice(1).map((member) => member.userMessageId));
  return items
    .flatMap((item) => {
      const at = item.runId === null ? undefined : position.get(item.runId);
      if (at === undefined) return [];
      return item.type === "user_message" && prompts.has(item.messageId) ? [] : [{ at, item }];
    })
    .toSorted((left, right) => left.at - right.at || left.item.ordinal - right.item.ordinal)
    .map(({ item }, ordinal) => ({ ...item, ordinal }));
}

/**
 * The write turns' own items left out of part 3: the prompt and the notes are not conversation.
 * A write turn a person steered into keeps that exchange: only its prompt and a marked reply go.
 */
function withoutWriteTurns(
  writes: ReadonlyArray<OrchestrationV2Run>,
  items: ReadonlyArray<OrchestrationV2TurnItem>,
): ReadonlyArray<OrchestrationV2TurnItem> {
  const drop = new Set<string>();
  for (const write of writes) {
    const own = items.filter((item) => item.runId === write.id);
    const joined = handoffWriteJoined(write, own);
    for (const item of own) {
      if (
        !joined ||
        (item.type === "user_message" && item.messageId === write.userMessageId) ||
        (item.type === "assistant_message" && item.text.trim().startsWith(HANDOFF_AGENT_MARKER))
      )
        drop.add(item.id);
    }
  }
  return drop.size === 0 ? items : items.filter((item) => !drop.has(item.id));
}

const waitsForSlot = (outcome: unknown): outcome is HandoffWaitsForSlot =>
  typeof outcome === "object" && outcome !== null && "type" in outcome;

/**
 * The answer to a person's "Hand off now" (design 4.5): `started`, or why not now. `wait` names
 * what the thread waits on; nothing is queued, so a busy thread is asked again once it is quiet.
 */
export type HandoffAskAnswer =
  | { readonly kind: "off" }
  | { readonly kind: "started" }
  | { readonly kind: "skip"; readonly reason: HandoffSkipReason }
  | { readonly kind: "wait"; readonly blockers: ReadonlyArray<HandoffBlocker> };

/** A thread's handoff for the thread menu and the "handoff waiting on" chip. */
export interface HandoffThreadState {
  /** The thread's resolved mode is `handoff` and it can run. */
  readonly on: boolean;
  /** The last provider turn's size; null when unknown. */
  readonly usedTokens: number | null;
  /** What "Hand off now" would answer now, without starting anything. */
  readonly ask: HandoffAskAnswer;
  /** What a due handoff waits on now; empty when none waits. In memory: a restart clears it. */
  readonly waitingOn: ReadonlyArray<HandoffBlocker>;
}

export const make = Effect.gen(function* () {
  const orchestrator = yield* Orchestrator.OrchestratorV2;
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  const serverSettings = yield* ServerSettings.ServerSettingsService;
  const adapters = yield* ProviderAdapterRegistry.ProviderAdapterRegistryV2;
  const outbox = yield* EffectOutbox.EffectOutboxV2;
  const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
  const commandSource = yield* HandoffCommand.HandoffCommand;
  // One per waiting thread; any event of that thread completes it, so the wait reads again.
  const wakes = new Map<ThreadId, Deferred.Deferred<void>>();
  // Completed by a request or run change of any thread (a child's request shows on its parent), so
  // a settle checks the relay's view again at once; replaced each time.
  let anyRequestOrRun = Deferred.makeUnsafe<void>();
  const withStores = <A, E, R>(effect: Effect.Effect<A, E, R>) =>
    effect.pipe(
      Effect.provideService(Orchestrator.OrchestratorV2, orchestrator),
      Effect.provideService(ProjectionStore.ProjectionStoreV2, projections),
      (provided) =>
        Option.isSome(gate)
          ? Effect.provideService(provided, SlotGate.SlotGate, gate.value)
          : provided,
    );
  // Keyed by thread: a newer finished run (or a person's "Hand off now") replaces the waiting one.
  // In memory: a restart drops it.
  const timers = yield* FiberMap.make<ThreadId>();
  // Per thread, the loop that owns the entry (a replaced loop's late clean-up leaves a newer one):
  // what a due handoff waits on (the chip), and the run a loop is handing off now (settle onward).
  const waitingOn = new Map<
    ThreadId,
    { readonly loop: object; readonly blockers: ReadonlyArray<HandoffBlocker> }
  >();
  const handingOff = new Map<ThreadId, { readonly loop: object; readonly runId: string }>();
  // The run a person's press is handing off, from the press until its loop ends.
  const pressed = new Map<ThreadId, { readonly loop: object; readonly runId: string }>();
  const clearMarks = (threadId: ThreadId, loop: object) => {
    if (waitingOn.get(threadId)?.loop === loop) waitingOn.delete(threadId);
    if (handingOff.get(threadId)?.loop === loop) handingOff.delete(threadId);
  };
  const ownsRun = (threadId: ThreadId, runId: string) =>
    handingOff.get(threadId)?.runId === runId || pressed.get(threadId)?.runId === runId;
  const logSlotWait = (threadId: ThreadId, outcome: HandoffWaitsForSlot) =>
    Effect.logInfo("handoff waits for an agent slot", { threadId, detail: outcome.detail });

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
    command: HandoffCommand.HandoffCommandResult | undefined,
    agent?: HandoffAgentResult,
  ) {
    const firstSends = new Set(
      read.records.runs.flatMap((other) => {
        const parsed = parseHandoffMessageId(threadId, other.userMessageId);
        return parsed === null ? [] : [parsed.runId];
      }),
    );
    // The setting, else the thread's current model, always written out in full.
    const modelSelection = read.settings.modelSelection ?? read.records.thread.modelSelection;
    return yield* withStores(
      dispatchHandoff({
        type: "message.dispatch",
        commandId: CommandId.make(ids.commandId),
        messageId: MessageId.make(ids.messageId),
        threadId,
        // Built only with a slot: a refused handoff asks again every 5 s and reads nothing more.
        text: Effect.gen(function* () {
          // The latest runs, write turns (and their restart continuations) not counted; the write
          // turns among them are read too.
          const isWrite = (other: OrchestrationV2Run) =>
            handoffWriteChain(threadId, other, read.records.runs) !== null;
          const newest = read.records.runs.toSorted((left, right) => right.ordinal - left.ordinal);
          const oldest = newest.filter((other) => !isWrite(other)).at(TRANSCRIPT_RUNS - 1);
          const recent = newest.filter(
            (other) => oldest === undefined || other.ordinal >= oldest.ordinal,
          );
          const more = yield* projections.getThreadRecords(
            threadId,
            ["attempts", "subagents", "turnItems"],
            { turnItemRunIds: recent.map((recentRun) => recentRun.id), omitSettledSubagents: true },
          );
          return handoffMessageText({
            records: {
              ...read.records,
              ...more,
              turnItems: withoutWriteTurns(recent.filter(isWrite), more.turnItems),
            },
            sourceRunId: run.id,
            number: firstSends.size + 1,
            nowIso: DateTime.formatIso(yield* DateTime.now),
            modelSelection,
            command,
            agent,
          });
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

  /** A handoff's place in the gate's line while it waits for a slot; left when it stops waiting. */
  interface Waiting {
    key: string | undefined;
  }
  const leavePlace = (waiting: Waiting) =>
    Effect.suspend(() => {
      const key = waiting.key;
      waiting.key = undefined;
      return key === undefined ? Effect.void : leaveLine(gate, [key]);
    });

  /** Sends a handoff, asking again for a slot until one is free or the check drops it. */
  const sendWithSlot = Effect.fn("HandoffService.sendWithSlot")(function* (
    threadId: ThreadId,
    run: OrchestrationV2Run,
    ids: { readonly commandId: string; readonly messageId: string },
    first: NonNullable<Effect.Success<ReturnType<typeof readThread>>>,
    agent: HandoffAgentResult,
    waiting: Waiting,
  ) {
    let outcome = yield* send(threadId, run, ids, first, undefined, agent);
    if (waitsForSlot(outcome)) yield* logSlotWait(threadId, outcome);
    while (waitsForSlot(outcome)) {
      waiting.key = handoffPlace(ids.commandId).key;
      const wake = yield* Deferred.make<void>();
      wakes.set(threadId, wake);
      yield* Deferred.await(wake).pipe(
        Effect.timeoutOrElse({ duration: HANDOFF_SLOT_RETRY, orElse: () => Effect.void }),
      );
      // Read again at each try: part 1 is T3's state at the send, and a thread off now sends nothing.
      const again = yield* readThread(threadId);
      if (again === null) return "dropped" as const;
      outcome = yield* send(threadId, run, ids, again, undefined, agent);
    }
    return outcome;
  });

  /** Write turns this server started or took on, until their handoff is decided. */
  const carrying = new Set<string>();
  /**
   * Whether a loop that finds this write turn as the latest run carries it on: always one this
   * server has in hand; after a restart, one still running or that ended within its timeout. One
   * that ended long before (reported again after a restart) is left, and the next turn end decides.
   */
  const carriesOn = (write: OrchestrationV2Run, nowMs: number) =>
    carrying.has(write.id) ||
    write.completedAt === null ||
    nowMs - DateTime.toEpochMillis(write.completedAt) <= Duration.toMillis(HANDOFF_WRITE_TIMEOUT);

  /**
   * The source run of the write turn a restart continuation carries on, read from the thread's
   * runs; null for any other run. Unread counts as another run: its own turn end decides.
   */
  const continuedWrite = (threadId: ThreadId, run: OrchestrationV2Run) =>
    projections.getThreadRecords(threadId, ["runs"], {}).pipe(
      Effect.map(({ runs }) => handoffWriteChain(threadId, run, runs)?.write.runId ?? null),
      Effect.catchCauseIf(
        (cause) => !Cause.hasInterruptsOnly(cause),
        (cause) =>
          Effect.logWarning("handoff: a restart continuation's runs unread", {
            threadId,
            cause: Cause.pretty(cause),
          }).pipe(Effect.as(null)),
      ),
    );

  /**
   * What follows a write turn a restart cut short: its continuation, or one upstream owes (`row`
   * not settled; `retry` of a failed start, row not yet written, due at `dueMs`; `unread` row,
   * never "no row", read again shortly), or none. Row first: one settling meanwhile shows its run.
   */
  const continuationOf = Effect.fn("HandoffService.continuationOf")(function* (
    threadId: ThreadId,
    write: OrchestrationV2Run,
  ) {
    const effectId = continuationEffectId(write.id);
    const effect = yield* outbox.get(effectId).pipe(
      Effect.catch((cause) =>
        Effect.logWarning("handoff: a restart continuation's outbox row unread", {
          threadId,
          runId: write.id,
          cause,
        }).pipe(Effect.as("unread" as const)),
      ),
    );
    const projection = yield* projections.getThreadRecords(
      threadId,
      ["runs", "providerTurns", "turnItems"],
      { turnItemTypes: ["error"], turnItemRunIds: [write.id] },
    );
    const next = projection.runs.find(
      (candidate) => candidate.restartContinuationOfRunId === write.id,
    );
    if (next !== undefined) return { kind: "next", next } as const;
    if (effect === "unread") return { kind: "owed", wait: "unread" } as const;
    if (Option.isSome(effect)) {
      const { status, availableAt } = effect.value;
      return status === "pending" || status === "running"
        ? ({
            kind: "owed",
            wait: "row",
            effectId,
            dueMs: DateTime.toEpochMillis(DateTime.makeUnsafe(availableAt)),
          } as const)
        : ({ kind: "none" } as const);
    }
    const delayMs = continuationStartRetryDelayMs(write, projection);
    return delayMs === undefined || write.completedAt === null
      ? ({ kind: "none" } as const)
      : ({
          kind: "owed",
          wait: "retry",
          dueMs: DateTime.toEpochMillis(write.completedAt) + delayMs,
        } as const);
  });

  /**
   * A write turn, from wherever it is, to its handoff: waits for its end, stops it at its timeout,
   * judges the reply and sends (fixed ids: at most once). Any loop that finds a write turn as the
   * latest run comes here. `reason`: the trigger that started it; else the size decides.
   */
  const finishWrite = Effect.fn("HandoffService.finishWrite")(
    function* (
      threadId: ThreadId,
      found: OrchestrationV2Run,
      runs: ReadonlyArray<OrchestrationV2Run>,
      waiting: Waiting,
      reason: "size" | "idle" | "asked" | undefined,
      /** Every run of the chain this call took in hand (`carrying`), let go once it is decided. */
      held: Set<string>,
    ) {
      // `found` is the write turn or its restart continuation: the chain is one write turn.
      const origin = handoffWriteChain(threadId, found, runs);
      if (origin === null) return;
      const parsed = origin.write;
      let chain = origin.chain;
      for (const member of chain) held.add(member.id);
      carrying.add(found.id);
      yield* leavePlace(waiting);
      let write = found;
      // Each run of the chain gets the timeout: a restart is not the write turn's slowness, and
      // upstream stops continuing after 3 restarts in a row.
      let deadlineMs =
        DateTime.toEpochMillis(write.requestedAt) + Duration.toMillis(HANDOFF_WRITE_TIMEOUT);
      const stopWaitMs = Duration.toMillis(HANDOFF_WRITE_STOP_WAIT);
      let stopAsked = false;
      let owedLogged: string | undefined;
      while (true) {
        // Set before the read, so the run's end between the read and the wait is not missed.
        const wake = yield* Deferred.make<void>();
        wakes.set(threadId, wake);
        const { runs } = yield* projections.getThreadRecords(threadId, ["runs"], {
          runIds: [write.id],
        });
        const now = runs.find((candidate) => candidate.id === write.id);
        if (now === undefined) return;
        write = now;
        if (WRITE_ENDED.has(write.status)) {
          // A restart cut it short and upstream continued it: carry on with the continuation.
          const after = CUT_SHORT.has(write.status)
            ? yield* continuationOf(threadId, write)
            : ({ kind: "none" } as const);
          if (after.kind === "none") break;
          if (after.kind === "owed") {
            // Upstream still starts it: judging now would drop the continuation's notes. A
            // written row is waited for until it settles, however late a busy effect worker
            // runs it, and an unread one is read again until it can be; a retry whose row is not
            // written yet (it is, right after the failure) is given up a minute past its due time.
            const nowMs = DateTime.toEpochMillis(yield* DateTime.now);
            const giveUpMs =
              after.wait === "retry" ? after.dueMs + stopWaitMs : Number.POSITIVE_INFINITY;
            if (nowMs >= giveUpMs) {
              yield* Effect.logWarning(
                "handoff: upstream's restart continuation did not start; the write turn is judged as it is",
                { threadId, runId: write.id },
              );
              break;
            }
            if (owedLogged !== write.id) {
              owedLogged = write.id;
              yield* Effect.logInfo(
                "handoff write turn waits for upstream's restart continuation",
                {
                  threadId,
                  runId: write.id,
                  ...(after.wait === "unread"
                    ? {}
                    : { dueAt: DateTime.formatIso(DateTime.makeUnsafe(after.dueMs)) }),
                },
              );
            }
            yield* after.wait === "row"
              ? Effect.raceFirst(
                  Deferred.await(wake),
                  // A failed wait reads the row again shortly.
                  outbox
                    .awaitSettled(after.effectId)
                    .pipe(Effect.catch(() => Effect.sleep(CONTINUATION_RECHECK))),
                )
              : Deferred.await(wake).pipe(
                  Effect.timeoutOrElse({
                    duration: Duration.millis(
                      Math.min(Duration.toMillis(CONTINUATION_RECHECK), giveUpMs - nowMs),
                    ),
                    orElse: () => Effect.void,
                  }),
                );
            continue;
          }
          const next = after.next;
          yield* Effect.logInfo("handoff write turn continued after a restart", {
            threadId,
            runId: next.id,
          });
          held.add(next.id);
          carrying.add(next.id);
          chain = [...chain.slice(0, -1), write, next];
          write = next;
          deadlineMs =
            DateTime.toEpochMillis(write.requestedAt) + Duration.toMillis(HANDOFF_WRITE_TIMEOUT);
          stopAsked = false;
          continue;
        }
        const nowMs = DateTime.toEpochMillis(yield* DateTime.now);
        if (!stopAsked && nowMs >= deadlineMs) {
          stopAsked = true;
          yield* Effect.logWarning("handoff write turn did not end in time; T3 stops it", {
            threadId,
            runId: write.id,
          });
          yield* orchestrator
            .dispatch({
              type: "run.interrupt",
              commandId: CommandId.make(`server:handoff-write-stop:${write.userMessageId}`),
              threadId,
              runId: write.id,
              reason: HANDOFF_WRITE_STOP_REASON,
            })
            .pipe(
              Effect.catch((cause) =>
                Effect.logWarning("handoff write turn stop refused", { threadId, cause }),
              ),
            );
          continue;
        }
        if (stopAsked && nowMs >= deadlineMs + stopWaitMs) {
          // Kept in hand: its end, whenever it comes, carries on from here (the event handler).
          yield* Effect.logWarning("handoff write turn did not stop; nothing sent yet", {
            threadId,
            runId: write.id,
          });
          return "kept" as const;
        }
        const untilMs = stopAsked ? deadlineMs + stopWaitMs : deadlineMs;
        yield* Deferred.await(wake).pipe(
          Effect.timeoutOrElse({
            duration: Duration.millis(untilMs - nowMs),
            orElse: () => Effect.void,
          }),
        );
      }
      const { turnItems } = yield* projections.getThreadRecords(threadId, ["turnItems"], {
        turnItemRunIds: chain.map((member) => member.id),
        turnItemTypes: ["user_message", "assistant_message", "run_interrupt_request"],
      });
      // Judged as one turn: the write turn's prompt, its end, and the chain's items in order,
      // without upstream's "Continue where you left off" prompts (no person's message).
      const agent = handoffAgentReply(
        { status: write.status, userMessageId: chain[0]!.userMessageId },
        chainItems(chain, turnItems),
      );
      // Read now: part 1 is T3's state at the send, not before the write turn.
      const read = yield* readThread(threadId);
      if (read === null) return;
      const latest = latestStartedRun(read.records.runs);
      if (latest?.id !== write.id) {
        // Sent already (a loop replaced after its send), or the thread moved on.
        if (parseHandoffMessageId(threadId, latest?.userMessageId ?? "")?.runId === parsed.runId) {
          return;
        }
        return yield* Effect.logWarning(
          "handoff write turn spent, nothing sent: the thread moved on",
          {
            threadId,
            runId: write.id,
          },
        );
      }
      const size = handoffThreadFacts(read.records).usedTokens;
      // Found after a restart: a write turn marked `:fallback` was started where the fallback
      // follows its failure (and its failure is quiet for that), so it gets it; else the size
      // decides.
      const trigger =
        reason ??
        (parsed.fallback ||
        (size !== undefined && Number.isFinite(size) && size >= read.settings.sizeTokens)
          ? "size"
          : "idle");
      if (!agent.ok) {
        // At the idle trigger this log is all T3 adds: the write turn is not marked `:fallback`,
        // so a failed one alerts as upstream's failed run.
        yield* Effect.logWarning("handoff write turn did not count", {
          threadId,
          reason: agent.reason,
          trigger,
        });
        // Stopped by a person or joined: the old session carries on. Else, at the idle trigger
        // nothing is sent; at the size backstop parts 1, 3 and 4 are.
        if ("noHandoff" in agent || trigger === "idle") return;
      }
      const source = read.records.runs.find((candidate) => candidate.id === parsed.runId);
      if (source === undefined) return;
      const outcome = yield* sendWithSlot(
        threadId,
        source,
        {
          commandId: `server:${HANDOFF_MESSAGE_PREFIX}${threadId}:${source.id}`,
          messageId: `${HANDOFF_MESSAGE_PREFIX}${threadId}:${source.id}${parsed.loud ? HANDOFF_LOUD_SUFFIX : ""}`,
        },
        read,
        agent,
        waiting,
      );
      if (outcome !== "sent") {
        yield* Effect.logWarning("handoff write turn spent, nothing sent: the thread moved on", {
          threadId,
          runId: write.id,
        });
      }
    },
    (effect, _threadId, _found, _runs, _waiting, _reason, held) =>
      // Decided (sent, judged or left): no run of the chain is in hand any more. A loop replaced
      // mid-way keeps them, so the loop that replaces it carries the chain on.
      effect.pipe(
        Effect.tap((kept) =>
          Effect.sync(() => {
            if (kept !== "kept") for (const id of held) carrying.delete(id);
          }),
        ),
      ),
  );

  /**
   * At the size backstop after a write turn T3 had to stop (and no handoff since): the fallback at
   * once, so a slow provider cannot keep the thread growing behind write turn after write turn.
   */
  const lastWriteTimedOut = Effect.fn("HandoffService.lastWriteTimedOut")(function* (
    threadId: ThreadId,
    runs: ReadonlyArray<OrchestrationV2Run>,
  ) {
    const last = runs
      .filter(
        (candidate) =>
          handoffWriteChain(threadId, candidate, runs) !== null ||
          parseHandoffMessageId(threadId, candidate.userMessageId) !== null,
      )
      .reduce<OrchestrationV2Run | undefined>(
        (newest, candidate) =>
          newest === undefined || candidate.ordinal > newest.ordinal ? candidate : newest,
        undefined,
      );
    if (last === undefined || handoffWriteChain(threadId, last, runs) === null) return false;
    if (last.status !== "interrupted") return false;
    const { turnItems } = yield* projections.getThreadRecords(threadId, ["turnItems"], {
      turnItemRunIds: [last.id],
      turnItemTypes: ["run_interrupt_request"],
    });
    return handoffWriteTimedOut(turnItems.filter((item) => item.runId === last.id));
  });

  const handOffLoop = Effect.fn("HandoffService.handOffWhenDue")(
    function* (
      threadId: ThreadId,
      sourceRunId: RunId,
      waiting: Waiting,
      firstTrigger: "turn-end" | "asked",
    ) {
      let runId = sourceRunId;
      let trigger: HandoffTrigger = firstTrigger;
      let slotWaitLogged = false;
      // The `command` source's result for one handoff (its ids and command): a slot wait reuses it.
      let commandRun:
        | { readonly key: string; readonly result: HandoffCommand.HandoffCommandResult }
        | undefined;
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
        // A write turn is the latest run: the loop that started it was replaced (a late update of
        // its source run) or lost (a restart). Carry it on to its handoff, never skip it.
        const latestRun = latestStartedRun(read.records.runs);
        if (
          latestRun !== undefined &&
          handoffWriteChain(threadId, latestRun, read.records.runs) !== null
        ) {
          yield* endSettle;
          if (!carriesOn(latestRun, DateTime.toEpochMillis(yield* DateTime.now))) return;
          yield* finishWrite(threadId, latestRun, read.records.runs, waiting, undefined, new Set());
          return;
        }
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
        // Not due now: it needs no slot, so it holds no place from the waiters behind it.
        if (decision.kind !== "handoff") yield* leavePlace(waiting);
        clearMarks(threadId, waiting);
        if (decision.kind === "handoff") handingOff.set(threadId, { loop: waiting, runId });
        if (decision.kind === "skip") return;
        if (decision.kind === "sleep") {
          yield* Effect.sleep(Duration.millis(decision.untilMs - nowMs));
          trigger = "idle-timer";
          continue;
        }
        if (decision.kind === "wait") {
          // A person's press is not queued: busy again before it sent, it ends; the next turn
          // end decides, and the person may ask again.
          if (trigger === "asked") return;
          waitingOn.set(threadId, { loop: waiting, blockers: decision.blockers });
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
        if (run === undefined) return;
        const { source, command } = read.settings;
        let commandResult: HandoffCommand.HandoffCommandResult | undefined;
        if (source === "command" && command !== undefined) {
          const key = `${decision.commandId}\n${command.join("\n")}`;
          if (commandRun?.key !== key) {
            const result = yield* commandSource.run({
              command,
              threadId,
              projectId: read.records.thread.projectId,
            });
            commandRun = { key, result };
          }
          commandResult = commandRun.result;
          if (!commandResult.ok) {
            yield* Effect.logWarning("handoff command failed", {
              threadId,
              reason: commandResult.reason,
              trigger: decision.reason,
            });
            // At the idle trigger nothing is sent; at the size backstop parts 1, 3 and 4 are.
            if (decision.reason === "idle") return;
          }
        }
        const ids = {
          commandId: decision.commandId,
          messageId: settle.loud
            ? `${decision.messageId}${HANDOFF_LOUD_SUFFIX}`
            : decision.messageId,
        };
        // The command waiting for a slot: the handoff, or the write turn before it.
        let place = decision.commandId;
        let outcome: Effect.Success<ReturnType<typeof send>> | undefined;
        let agentResult: HandoffAgentResult | undefined;
        if (source === "agent") {
          if (
            decision.reason === "size" &&
            (yield* lastWriteTimedOut(threadId, read.records.runs))
          ) {
            agentResult = { ok: false, reason: "the last write turn did not end in time" };
            yield* Effect.logWarning("handoff sent without a write turn", {
              threadId,
              reason: agentResult.reason,
            });
          } else {
            // The `agent` source: one normal turn on the old session (its model, no fresh
            // context), with the same check before it as the handoff.
            // `:fallback` where the fallback follows a failed write turn (any trigger but idle),
            // so only that failure is quiet: at the idle trigger nothing follows it.
            const writeIds = handoffWriteIds(threadId, run.id, {
              loud: settle.loud,
              fallback: decision.reason !== "idle",
            });
            const written = yield* withStores(
              dispatchHandoff({
                type: "message.dispatch",
                commandId: CommandId.make(writeIds.commandId),
                messageId: MessageId.make(writeIds.messageId),
                threadId,
                text: handoffWriteText(read.settings.instructions),
                attachments: [],
                createdBy: "agent",
                creationSource: "server",
                dispatchMode: { type: "start_immediately" },
              }),
            );
            if (written === "sent") {
              const { runs } = yield* projections.getThreadRecords(threadId, ["runs"], {});
              const write = runs.find(
                (candidate) => candidate.userMessageId === writeIds.messageId,
              );
              if (write === undefined) return;
              yield* endSettle;
              yield* finishWrite(threadId, write, runs, waiting, decision.reason, new Set());
              return;
            }
            // Dropped or queued behind a newer run: that run's turn end decides.
            if (written === "dropped" || written === "cancelled") return;
            place = writeIds.commandId;
            outcome = written;
          }
        }
        outcome ??= yield* send(threadId, run, ids, read, commandResult, agentResult);
        while (waitsForSlot(outcome)) {
          if (waiting.key !== handoffPlace(place).key) yield* leavePlace(waiting);
          waiting.key = handoffPlace(place).key;
          // Settled already: it decides again and checks the relay's view, without a new settle.
          if (!slotWaitLogged) yield* logSlotWait(threadId, outcome);
          slotWaitLogged = true;
          // The loop's own wake decides again; a send again waits on a fresh one each time.
          const slotWake = agentResult === undefined ? wake : yield* Deferred.make<void>();
          wakes.set(threadId, slotWake);
          yield* Deferred.await(slotWake).pipe(
            Effect.timeoutOrElse({ duration: HANDOFF_SLOT_RETRY, orElse: () => Effect.void }),
          );
          // Decides again (the write turn too); only the fallback without a write turn sends again.
          if (agentResult === undefined) break;
          const again = yield* readThread(threadId);
          if (again === null) return;
          outcome = yield* send(threadId, run, ids, again, undefined, agentResult);
        }
        if (!waitsForSlot(outcome)) return;
      }
    },
    (effect, threadId) =>
      effect.pipe(
        Effect.catch((cause) => Effect.logWarning("handoff skipped", { threadId, cause })),
      ),
  );

  /** Replaced by a newer run's timer, or stopped: its place in the line and its marks go with it. */
  const handOffWhenDue = (
    threadId: ThreadId,
    sourceRunId: RunId,
    trigger: "turn-end" | "asked" = "turn-end",
  ) => {
    const waiting: Waiting = { key: undefined };
    const marked = Effect.sync(() => {
      if (trigger === "asked") pressed.set(threadId, { loop: waiting, runId: sourceRunId });
    });
    return Effect.andThen(marked, handOffLoop(threadId, sourceRunId, waiting, trigger)).pipe(
      Effect.ensuring(
        Effect.andThen(
          leavePlace(waiting),
          Effect.sync(() => {
            clearMarks(threadId, waiting);
            // Only here: the loop clears its other marks at each decision, and a late update
            // between two decisions must still find the press.
            if (pressed.get(threadId)?.loop === waiting) pressed.delete(threadId);
          }),
        ),
      ),
      // The whole timer, its clean-up too (the loop's own span ends before it).
      Effect.withSpan("HandoffService.handOffTimer"),
    );
  };

  /** The decision for the thread's latest run, as a person's press would ask it; null when off. */
  const askDecision = Effect.fn("HandoffService.askDecision")(function* (threadId: ThreadId) {
    const read = yield* readThread(threadId);
    if (read === null) return null;
    const facts = handoffThreadFacts(read.records);
    const runId = facts.lastRun?.id;
    const decision =
      runId === undefined
        ? ({ kind: "skip", reason: "no-runs" } as const)
        : handoffDecision({
            ...facts,
            trigger: "asked",
            runId,
            threadId,
            mode: read.mode,
            source: read.settings.source,
            sizeTokens: read.settings.sizeTokens,
            idleTokens: read.settings.idleTokens,
            nowMs: DateTime.toEpochMillis(yield* DateTime.now),
          });
    return { runId, decision, usedTokens: facts.usedTokens };
  });

  const toAnswer = (
    decision: NonNullable<Effect.Success<ReturnType<typeof askDecision>>>["decision"],
  ): HandoffAskAnswer =>
    decision.kind === "skip"
      ? { kind: "skip", reason: decision.reason }
      : decision.kind === "wait"
        ? { kind: "wait", blockers: decision.blockers }
        : { kind: "started" };

  /**
   * A person's "Hand off now": the same decision, check, settle and ids as a turn end's, so a
   * second press or a turn end at the same time sends one handoff. Not quiet now: answers what it
   * waits on, and starts nothing.
   */
  const ask = Effect.fn("HandoffService.ask")(function* (threadId: ThreadId) {
    const asked = yield* askDecision(threadId);
    if (asked === null) return { kind: "off" } as const satisfies HandoffAskAnswer;
    const answer = toAnswer(asked.decision);
    if (answer.kind !== "started" || asked.runId === undefined) return answer;
    // A loop already handing this run off (settling or sending), or an earlier press, is left to finish.
    if (!ownsRun(threadId, asked.runId)) {
      yield* FiberMap.run(
        timers,
        threadId,
        handOffWhenDue(threadId, asked.runId as RunId, "asked"),
      );
    }
    return answer;
  });

  /** The thread's handoff for the thread menu and the chip; off for a thread it cannot read. */
  const threadState = Effect.fn("HandoffService.threadState")(function* (threadId: ThreadId) {
    const asked = yield* askDecision(threadId);
    if (asked === null) {
      return { on: false, usedTokens: null, ask: { kind: "off" }, waitingOn: [] } as const;
    }
    const size = asked.usedTokens;
    return {
      on: true,
      usedTokens: size !== undefined && Number.isFinite(size) ? size : null,
      // A thread handing off now answers `started`: a press leaves it to finish.
      ask:
        asked.runId !== undefined && ownsRun(threadId, asked.runId)
          ? { kind: "started" }
          : toAnswer(asked.decision),
      waitingOn: waitingOn.get(threadId)?.blockers ?? [],
    } satisfies HandoffThreadState;
  });

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
      const resend = withStores(
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
      // The busy check runs again at each try: a thread that moved on drops it.
      let outcome = yield* resend;
      if (waitsForSlot(outcome)) yield* logSlotWait(threadId, outcome);
      // Its place in the line goes when it stops waiting, whichever way.
      yield* Effect.gen(function* () {
        while (waitsForSlot(outcome)) {
          yield* Effect.sleep(HANDOFF_SLOT_RETRY);
          if ((yield* readThread(threadId)) === null) return;
          outcome = yield* resend;
        }
      }).pipe(
        Effect.ensuring(
          leaveLine(gate, [handoffPlace(`server:${run.userMessageId}${HANDOFF_RETRY_SUFFIX}`).key]),
        ),
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
    // Off the event stream: a dispatch from inside its handler would wait on the stream itself.
    const followUps = yield* FiberSet.make();
    yield* forkParked(
      Stream.runForEach(orchestrator.streamDomainEvents, (event) =>
        Effect.gen(function* () {
          // A restart continuation's write turn, read before the wake below: a read after it would
          // let the woken loop send and end first, and this event would then start a second loop
          // for the same write turn.
          const continued =
            event.type === "run.updated" && event.payload.restartContinuationOfRunId !== undefined
              ? yield* continuedWrite(event.threadId, event.payload)
              : null;
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
          // The write turn's update wakes its wait (above); it is no turn end of its own. With no
          // loop on the thread (a restart mid write turn, or a loop that gave up waiting for its
          // stop) it starts one, which carries the write turn on (`carriesOn`). Upstream's
          // continuation of a write turn after a restart is that write turn.
          const write = parseHandoffWriteMessageId(run.threadId, run.userMessageId) ?? continued;
          if (write !== null) {
            yield* FiberMap.run(
              timers,
              event.threadId,
              handOffWhenDue(event.threadId, write as RunId),
              {
                onlyIfMissing: true,
              },
            );
            return;
          }
          // A late update of a run a person pressed for would replace the press's loop with a
          // turn end's, which skips a thread under the size backstop; a newer run still replaces
          // it. A turn end's own loop is replaced as before: the new one carries its handoff on.
          if (run.status === "completed" && pressed.get(event.threadId)?.runId !== run.id) {
            yield* FiberMap.run(timers, event.threadId, handOffWhenDue(event.threadId, run.id));
          }
        }),
      ),
    );
  });
  return { start, ask, threadState };
});

/** The running handoff service: a person's "Hand off now" and a thread's state (fork/handoff/handoffRoutes.ts). */
export class HandoffService extends Context.Service<
  HandoffService,
  {
    readonly ask: Effect.Success<typeof make>["ask"];
    readonly threadState: Effect.Success<typeof make>["threadState"];
  }
>()("t3/fork/handoff/HandoffService") {}

/** Started with the server's other workers, from `ForkLayers.layerRuntimeServices` (fork/forkLayers.ts). */
export const layer = Layer.effect(
  HandoffService,
  make.pipe(
    Effect.tap((service) => service.start()),
    Effect.map(({ ask, threadState }) => HandoffService.of({ ask, threadState })),
  ),
).pipe(
  Layer.provide(HandoffCommand.layer),
  Layer.provide(ProjectionStore.layer),
  // The runtime's own outbox (orchestration-v2/runtimeLayer.ts): layers memoize by reference, so
  // `awaitSettled` hears the effect worker settle a row.
  Layer.provide(EffectOutbox.layer),
  // Upstream's own lookup over the server's instance registry, as server.ts gives /mcp.
  Layer.provide(ProviderAdapterRegistry.layerFromProviderInstanceRegistry),
  // The server's one slot gate: layers memoize by reference.
  Layer.provide(SlotGate.layer),
);
