/**
 * Fork: upstream's usage-limit resume starts a run in each failed thread at a reset, so each takes
 * an agent slot. Refused, nothing is sent: the thread stays failed and armed (no time limit), the
 * worker asks again next tick (5 s), a restart finds it again, and its fixed id sends it once.
 */
import type {
  CommandId,
  OrchestrationV2ServerCommand,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";

import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import { admitSend, reasonHere } from "./agentSlotRoutes.ts";

/** Upstream's resume of a run that failed on a usage limit. */
const isLimitResume = (
  command: OrchestrationV2ServerCommand,
): command is Extract<OrchestrationV2ServerCommand, { readonly type: "message.dispatch" }> =>
  command.type === "message.dispatch" && command.usageLimitContinuationOfRunId !== undefined;

/**
 * Upstream's thread management, except that a usage-limit resume asks `gate` for a slot first
 * and is skipped (nothing stored) while it is refused. `instanceOf` reads the thread's provider
 * instance; `null` (no such thread) leaves the resume to upstream, which refuses it. A thread that
 * cannot be read waits, as a refused resume does. With no gate it is upstream's own service.
 */
export const gateLimitResumes = <E>(
  threads: ThreadManagement.ThreadManagementService["Service"],
  gate: Option.Option<SlotGate.SlotGate["Service"]>,
  instanceOf: (threadId: ThreadId) => Effect.Effect<ProviderInstanceId | null, E>,
): ThreadManagement.ThreadManagementService["Service"] => {
  if (Option.isNone(gate)) return threads;
  /** Resumes that wait, logged once until they land. */
  const waiting = new Set<CommandId>();
  const skipped = Effect.fn("LimitResumeSlot.skipped")(function* (
    commandId: CommandId,
    threadId: ThreadId,
    detail: string,
  ) {
    if (!waiting.has(commandId))
      yield* Effect.logInfo("usage-limit resume waits for an agent slot", { threadId, detail });
    waiting.add(commandId);
    return { sequence: 0, storedEvents: [] };
  });
  return {
    ...threads,
    dispatch: (command) => {
      if (!isLimitResume(command)) return threads.dispatch(command);
      return Effect.gen(function* () {
        const instanceId = yield* instanceOf(command.threadId).pipe(
          Effect.orElseSucceed(() => "unreadable" as const),
        );
        if (instanceId === null) return yield* threads.dispatch(command);
        if (instanceId === "unreadable")
          return yield* skipped(
            command.commandId,
            command.threadId,
            "the thread could not be read to check for a slot",
          );
        const slot = yield* admitSend(gate, {
          callerThreadId: command.threadId,
          threadId: command.threadId,
          instanceId,
          keepsHeld: true,
        });
        if (slot.type === "refuse")
          return yield* skipped(command.commandId, command.threadId, reasonHere(slot.refusal));
        waiting.delete(command.commandId);
        return yield* threads.dispatch(command);
      });
    },
  };
};

/** Upstream's thread management with usage-limit resumes gated by the gate it is given. */
export const layerOver = Layer.effect(
  ThreadManagement.ThreadManagementService,
  Effect.gen(function* () {
    const threads = yield* ThreadManagement.ThreadManagementService;
    const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
    return gateLimitResumes(threads, gate, (threadId) =>
      threads
        .getThreadShell(threadId)
        .pipe(Effect.map((shell) => shell?.modelSelection.instanceId ?? null)),
    );
  }),
);

/**
 * {@link layerOver} for upstream's usage-limit recovery worker (runtimeLayer.ts, first in its
 * pipe, as `WaitWorker.limitResumeSlot`), over the server's one gate (`SlotGate.layer`: layers
 * memoize by reference).
 */
export const layer = layerOver.pipe(Layer.provide(SlotGate.layer));
