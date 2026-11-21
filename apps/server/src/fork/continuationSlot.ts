/**
 * Fork: a provider continuation into an idle thread takes an agent slot. Asked at the queue, not
 * around the dispatch: an adapter's `dispatchIfCurrent` holds its own lock while it dispatches.
 * A refused request goes back on the queue; the adapter's latch stays set, so it offers no second.
 */
import type { ProviderInstanceId, ThreadId } from "@t3tools/contracts";
import * as ProviderContinuationRequests from "@t3tools/provider-core/server/ProviderContinuationRequests";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import type * as Scope from "effect/Scope";

import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import { admitSend, reasonHere } from "./agentSlotRoutes.ts";

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
 * `take` hands out a request into an idle thread only with a slot. Upstream's at once: a delegated
 * result (docs/fork/README.md) and a gone thread (upstream drops it and clears the adapter's latch).
 * An unreadable thread waits, as a refused one does.
 */
export const gateContinuations = <E>(
  requests: Requests,
  gate: Option.Option<SlotGate.SlotGate["Service"]>,
  threadOf: (threadId: ThreadId) => Effect.Effect<ContinuationThread, E>,
  scope: Scope.Scope,
): Requests => {
  if (Option.isNone(gate)) return requests;
  /** Logged once until handed out. */
  const waiting = new Set<Request>();

  /** `null` when the request may go to upstream now, else why it waits. */
  const waitsFor = Effect.fn("ContinuationSlot.waitsFor")(function* (request: Request) {
    if (request.delegatedCompletion !== undefined) return null;
    const thread = yield* threadOf(request.threadId).pipe(
      Effect.orElseSucceed(() => "unreadable" as const),
    );
    if (thread === "unreadable") return "the thread could not be read to check for a slot";
    if (thread === null) return null;
    const slot = yield* admitSend(gate, {
      callerThreadId: request.threadId,
      threadId: request.threadId,
      instanceId: thread.instanceId,
      keepsHeld: true,
    });
    return slot.type === "refuse" ? reasonHere(slot.refusal) : null;
  });

  const putBack = Effect.fn("ContinuationSlot.putBack")(function* (
    request: Request,
    detail: string,
  ) {
    if (!waiting.has(request)) {
      waiting.add(request);
      yield* Effect.logInfo("provider continuation waits for an agent slot", {
        threadId: request.threadId,
        providerThreadId: request.providerThreadId,
        detail,
      });
    }
    yield* Effect.sleep(CONTINUATION_SLOT_RETRY).pipe(
      Effect.andThen(requests.offer(request)),
      Effect.forkIn(scope),
    );
  });

  const take: Effect.Effect<Request> = Effect.gen(function* () {
    while (true) {
      const request = yield* requests.take;
      const detail = yield* waitsFor(request);
      if (detail === null) {
        waiting.delete(request);
        return request;
      }
      yield* putBack(request, detail);
    }
  });

  return { offer: requests.offer, take };
};

/** The queue the adapters offer to (memoized by reference), gated for upstream's worker only. */
export const layerOver = Layer.effect(
  ProviderContinuationRequests.ProviderContinuationRequests,
  Effect.gen(function* () {
    const requests = yield* ProviderContinuationRequests.ProviderContinuationRequests;
    const threads = yield* ThreadManagement.ThreadManagementService;
    const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
    return gateContinuations(
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
