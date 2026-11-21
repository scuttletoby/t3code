/**
 * Fork: a provider continuation into an idle thread takes an agent slot, asked off upstream's queue
 * (never inside `dispatchIfCurrent`, which holds the adapter's lock), each request in its own fiber
 * and place in the gate's line. The slot is held until upstream's dispatch of it ends.
 */
import type { ProviderInstanceId, ThreadId } from "@t3tools/contracts";
import * as ProviderContinuationRequests from "@t3tools/provider-core/server/ProviderContinuationRequests";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Queue from "effect/Queue";
import type * as Scope from "effect/Scope";

import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import { admitSend, leaveLine, reasonHere } from "./agentSlotRoutes.ts";

type Request = ProviderContinuationRequests.ProviderContinuationRequest;
interface Requests {
  readonly offer: (request: Request) => Effect.Effect<void>;
  readonly take: Effect.Effect<Request>;
}

/** As a wait's wake-up. */
export const CONTINUATION_SLOT_RETRY = Duration.seconds(5);

/** `null`: no such thread, or it is gone. */
export type ContinuationThread = { readonly instanceId: ProviderInstanceId } | null;

/**
 * The request holding its slot (`end`) until upstream's dispatch of it ends, whichever way, so a
 * run that waits behind its thread's earlier dispatch still has it. One never dispatched frees it
 * at the gate's dispatch limit.
 */
const holdingSlot = (
  request: Request,
  end: (childThreadId: ThreadId | null) => Effect.Effect<void>,
): Request => {
  const dispatchIfCurrent = request.dispatchIfCurrent;
  const clearIfCurrent = request.clearIfCurrent;
  const ended = end(request.threadId);
  return {
    ...request,
    dispatchIfCurrent: <A, E, R>(effect: Effect.Effect<A, E, R>) =>
      (dispatchIfCurrent === undefined ? Effect.asSome(effect) : dispatchIfCurrent(effect)).pipe(
        Effect.ensuring(ended),
      ),
    ...(clearIfCurrent === undefined
      ? {}
      : { clearIfCurrent: () => clearIfCurrent().pipe(Effect.ensuring(ended)) }),
  };
};

/**
 * `take` hands out a request into an idle thread only with a slot. Upstream's at once: a delegated
 * result (docs/fork/README.md) and a gone thread (upstream drops it and clears the adapter's latch).
 * An unreadable thread waits, as a refused one does. No ask ever holds up upstream's queue: each
 * request asks in its own fiber, so a slow count delays only that request.
 */
export const gateContinuations = <E>(
  requests: Requests,
  gate: Option.Option<SlotGate.SlotGate["Service"]>,
  threadOf: (threadId: ThreadId) => Effect.Effect<ContinuationThread, E>,
  scope: Scope.Scope,
): Effect.Effect<Requests> => {
  if (Option.isNone(gate)) return Effect.succeed(requests);
  let places = 0;

  /** The request for upstream when it may go now (holding its slot), else why it waits. */
  const ask = Effect.fn("ContinuationSlot.ask")(function* (
    request: Request,
    place: SlotGate.SlotPlace,
  ) {
    const thread = yield* threadOf(request.threadId).pipe(
      Effect.orElseSucceed(() => "unreadable" as const),
    );
    if (thread === "unreadable") return "the thread could not be read to check for a slot";
    if (thread === null) return request;
    const slot = yield* admitSend(gate, {
      callerThreadId: request.threadId,
      threadId: request.threadId,
      instanceId: thread.instanceId,
      keepsHeld: true,
      place,
      endsItself: true,
    });
    return slot.type === "refuse" ? reasonHere(slot.refusal) : holdingSlot(request, slot.end);
  });

  /** One request, until it may go: asked again every `CONTINUATION_SLOT_RETRY`, logged once. */
  const untilSlot = Effect.fn("ContinuationSlot.untilSlot")(
    function* (request: Request, place: SlotGate.SlotPlace, ready: Queue.Queue<Request>) {
      let logged = false;
      while (true) {
        const answer = yield* ask(request, place);
        if (typeof answer !== "string") {
          yield* Queue.offer(ready, answer);
          return;
        }
        if (!logged) {
          logged = true;
          yield* Effect.logInfo("provider continuation waits for an agent slot", {
            threadId: request.threadId,
            providerThreadId: request.providerThreadId,
            detail: answer,
          });
        }
        yield* Effect.sleep(CONTINUATION_SLOT_RETRY);
      }
    },
    // Stopped while it waits (the server stops): nothing is sent later, and its place goes.
    (effect, _request: Request, place: SlotGate.SlotPlace) =>
      Effect.ensuring(effect, leaveLine(gate, [place.key])),
  );

  return Effect.gen(function* () {
    const ready = yield* Queue.unbounded<Request>();
    // Upstream's queue is only read here, and never waits on an ask.
    const handOut = (request: Request): Effect.Effect<void> =>
      request.delegatedCompletion !== undefined
        ? Effect.asVoid(Queue.offer(ready, request))
        : Effect.asVoid(
            Effect.forkIn(untilSlot(request, { key: `continuation:${places++}` }, ready), scope),
          );
    yield* requests.take.pipe(Effect.flatMap(handOut), Effect.forever, Effect.forkIn(scope));
    return { offer: requests.offer, take: Queue.take(ready) };
  });
};

/** The queue the adapters offer to (memoized by reference), gated for upstream's worker only. */
export const layerOver = Layer.effect(
  ProviderContinuationRequests.ProviderContinuationRequests,
  Effect.gen(function* () {
    const requests = yield* ProviderContinuationRequests.ProviderContinuationRequests;
    const threads = yield* ThreadManagement.ThreadManagementService;
    const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
    return yield* gateContinuations(
      requests,
      gate,
      (threadId) =>
        threads
          .getThreadShell(threadId)
          .pipe(
            Effect.map((shell) =>
              shell === null || shell.deletedAt !== null || shell.archivedAt !== null
                ? null
                : { instanceId: shell.modelSelection.instanceId },
            ),
          ),
      yield* Effect.scope,
    );
  }),
);

/** As `WaitWorker.continuationSlot` in runtimeLayer.ts, over the server's one gate. */
export const layer = layerOver.pipe(Layer.provide(SlotGate.layer));
