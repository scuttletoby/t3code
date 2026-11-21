/**
 * Fork: a pull request watch wake-up into an idle thread starts a run, so it takes an agent slot.
 * Refused, its dispatch fails: upstream's reactor reads the pull request again next sweep (2 min)
 * and finds the same news against the stored watch, so it is sent once when a slot frees.
 */
import type {
  OrchestrationV2ServerCommand,
  ProviderInstanceId,
  ThreadId,
  ThreadPullRequestKey,
} from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";

import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import { admitSend, leaveLine, reasonHere } from "./agentSlotRoutes.ts";

type WatchSync = Extract<
  OrchestrationV2ServerCommand,
  { readonly type: "thread.pull-request-watch.sync" }
>;

/** The cause a refused wake's dispatch fails with: the reactor logs it and tries again. */
export class PullRequestWatchWakeWaitsForSlot extends Schema.TaggedError<PullRequestWatchWakeWaitsForSlot>()(
  "PullRequestWatchWakeWaitsForSlot",
  { detail: Schema.String },
) {
  override get message(): string {
    return `The pull request watch wake-up waits for an agent slot: ${this.detail}.`;
  }
}

/** A key no refusal renewed this long is a watch that ended or whose thread went (sweeps: 2 min). */
export const FORGET_WATCH_KEY_MS = 10 * 60_000;

/** A refused wake-up keeps its place in the gate's line over 2 missed sweeps (2 min each). */
const WATCH_PLACE_STALE = Duration.minutes(5);

const watchKey = (
  threadId: ThreadId,
  watch: ThreadPullRequestKey & { readonly startedAt: string },
): string =>
  JSON.stringify([threadId, watch.host, watch.repository, watch.number, watch.startedAt]);

/** Every watch the command records: its own and the ones recorded together with it. */
const watchesOf = (command: WatchSync) => [command, ...(command.others ?? [])];

/**
 * Upstream's orchestrator, except that a pull request watch wake-up asks `gate` for a slot first
 * (see the file's doc). With no gate (a host that runs none) it is upstream's own orchestrator.
 * `instanceOf` reads the thread's provider instance; `null` (no such thread) leaves the command
 * to upstream, which refuses it. A thread that cannot be read waits, as a refused wake does.
 */
export const gatePullRequestWatchWakes = <E>(
  orchestrator: Orchestrator.OrchestratorV2["Service"],
  gate: Option.Option<SlotGate.SlotGate["Service"]>,
  instanceOf: (threadId: ThreadId) => Effect.Effect<ProviderInstanceId | null, E>,
): Orchestrator.OrchestratorV2["Service"] => {
  if (Option.isNone(gate)) return orchestrator;
  /**
   * Watches whose ending wake was refused. The reactor's give-up path falls back to ending the
   * watch with no wake, which would lose it; that next silent end is refused once, so the reactor
   * keeps the watch and gives up again, with its wake, on its next failed read.
   */
  const endingRefused = new Map<string, number>();
  /** Watches whose wake waits, logged once until one lands. */
  const waiting = new Map<string, number>();
  const forgetOld = Effect.fn("PullRequestWatchSlot.forgetOld")(function* () {
    const nowMs = yield* Clock.currentTimeMillis;
    for (const keys of [endingRefused, waiting])
      for (const [key, atMs] of keys) if (nowMs - atMs > FORGET_WATCH_KEY_MS) keys.delete(key);
    return nowMs;
  });

  const waitsForSlot = (command: WatchSync, detail: string) =>
    Effect.fail(
      new Orchestrator.OrchestratorDispatchError({
        commandId: command.commandId,
        commandType: command.type,
        cause: new PullRequestWatchWakeWaitsForSlot({ detail }),
      }),
    );

  const dispatchWake = Effect.fn("PullRequestWatchSlot.dispatchWake")(function* (
    command: WatchSync,
  ) {
    const instanceId = yield* instanceOf(command.threadId).pipe(
      Effect.orElseSucceed(() => "unreadable" as const),
    );
    const place = { key: `pull-request-watch:${command.threadId}`, staleAfter: WATCH_PLACE_STALE };
    // No such thread: its wake is not owed, so its place goes (upstream refuses the command).
    if (instanceId === null)
      return yield* leaveLine(gate, [place.key]).pipe(
        Effect.andThen(orchestrator.dispatch(command)),
      );
    if (instanceId === "unreadable")
      return yield* waitsForSlot(command, "the thread could not be read to check for a slot");
    const slot = yield* admitSend(gate, {
      callerThreadId: command.threadId,
      threadId: command.threadId,
      instanceId,
      keepsHeld: true,
      // Asked again at upstream's next sweep (2 min) while refused: it keeps its place in line,
      // but holds a free slot only `SlotGate.LINE_HOLD` (15 s) without asking.
      place,
    });
    const keys = watchesOf(command).map((watch) => watchKey(command.threadId, watch));
    if (slot.type === "refuse") {
      const detail = reasonHere(slot.refusal);
      const nowMs = yield* forgetOld();
      for (const watch of watchesOf(command))
        if (watch.watch === null) endingRefused.set(watchKey(command.threadId, watch), nowMs);
      if (keys.some((key) => !waiting.has(key)))
        yield* Effect.logInfo("pull request watch wake-up waits for an agent slot", {
          threadId: command.threadId,
          detail,
        });
      for (const key of keys) waiting.set(key, nowMs);
      return yield* waitsForSlot(command, detail);
    }
    for (const key of keys) {
      waiting.delete(key);
      endingRefused.delete(key);
    }
    return yield* orchestrator.dispatch(command);
  });

  return {
    ...orchestrator,
    dispatch: Effect.fn("PullRequestWatchSlot.dispatch")(function* (command) {
      if (command.type !== "thread.pull-request-watch.sync")
        return yield* orchestrator.dispatch(command);
      if (command.wake !== undefined) return yield* dispatchWake(command);
      yield* forgetOld();
      const silentEnds = watchesOf(command)
        .filter((watch) => watch.watch === null)
        .map((watch) => watchKey(command.threadId, watch))
        .filter((key) => endingRefused.has(key));
      if (silentEnds.length === 0) return yield* orchestrator.dispatch(command);
      for (const key of silentEnds) endingRefused.delete(key);
      return yield* waitsForSlot(command, "its ending wake-up waits for an agent slot");
    }),
  };
};

/** Upstream's orchestrator with pull request watch wake-ups gated by the gate it is given. */
export const layerOver = Layer.effect(
  Orchestrator.OrchestratorV2,
  Effect.gen(function* () {
    const orchestrator = yield* Orchestrator.OrchestratorV2;
    const projections = yield* ProjectionStore.ProjectionStoreV2;
    const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
    return gatePullRequestWatchWakes(orchestrator, gate, (threadId) =>
      projections
        .findThread(threadId)
        .pipe(Effect.map((thread) => thread?.modelSelection.instanceId ?? null)),
    );
  }),
);

/**
 * {@link layerOver} for the runtime's workers (`ForkLayers.handoffThreadsSkipIdleCompaction`,
 * where upstream's watch reactor reads it), over the server's one gate (`SlotGate.layer`: layers
 * memoize by reference).
 */
export const layer = layerOver.pipe(
  Layer.provide(Layer.merge(SlotGate.layer, ProjectionStore.layer)),
);
