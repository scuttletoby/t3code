/**
 * Fork: one owner per thread. Upstream's idle compaction skips a thread whose handoff mode is
 * `handoff`, decided at the send, so a compaction and a handoff never both act on it. Every other
 * command passes through; `IdleCompactionService.ts` stays upstream's.
 */
import {
  type OrchestrationV2ServerCommand,
  type ProjectId,
  resolveHandoffSettings,
  type ServerSettings as ServerSettingsValue,
  type ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ServerSettings from "../../serverSettings.ts";

/** A thread's handoff settings and mode: its project's override, else the server's (one source). */
export const threadHandoffSettings = (
  current: Pick<ServerSettingsValue, "handoff" | "projectSettingsOverrides">,
  projectId: ProjectId,
  threadId: ThreadId,
): ReturnType<typeof resolveHandoffSettings> =>
  resolveHandoffSettings(
    current.handoff,
    Object.hasOwn(current.projectSettingsOverrides, projectId)
      ? current.projectSettingsOverrides[projectId]?.handoff
      : undefined,
    threadId,
  );

const isIdleCompaction = (
  command: OrchestrationV2ServerCommand,
): command is Extract<OrchestrationV2ServerCommand, { readonly type: "message.dispatch" }> =>
  command.type === "message.dispatch" && command.idleCompaction !== undefined;

/**
 * Upstream's orchestrator, except that an idle compaction send to a thread `ownsThread` claims is
 * skipped (it reports nothing stored). Any other command is upstream's own call, unchanged.
 */
export const skipIdleCompactionOfOwnedThreads = (
  orchestrator: Orchestrator.OrchestratorV2["Service"],
  ownsThread: (threadId: ThreadId) => Effect.Effect<boolean>,
): Orchestrator.OrchestratorV2["Service"] => ({
  ...orchestrator,
  dispatch: (command) =>
    isIdleCompaction(command)
      ? ownsThread(command.threadId).pipe(
          Effect.flatMap((owned) =>
            owned
              ? Effect.logInfo("idle compaction skipped: handoff owns this thread", {
                  threadId: command.threadId,
                }).pipe(Effect.as({ sequence: 0, storedEvents: [] }))
              : orchestrator.dispatch(command),
          ),
        )
      : orchestrator.dispatch(command),
});

/**
 * Upstream's orchestrator, with idle compaction skipped on a thread whose handoff mode is `handoff`.
 * Read at each send, so a setting changed while compaction waited counts. When the settings or the
 * thread cannot be read, upstream decides (compaction goes ahead).
 */
export const make = Effect.gen(function* () {
  const orchestrator = yield* Orchestrator.OrchestratorV2;
  const serverSettings = yield* ServerSettings.ServerSettingsService;
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  const handoffOwns = Effect.fn("HandoffOwner.handoffOwns")(
    function* (threadId: ThreadId) {
      const thread = yield* projections.findThread(threadId);
      if (thread === null) return false;
      const current = yield* serverSettings.getSettings;
      return threadHandoffSettings(current, thread.projectId, threadId).mode === "handoff";
    },
    (effect, threadId) =>
      effect.pipe(
        Effect.catch((cause) =>
          Effect.logWarning("handoff owner unknown; idle compaction decides", {
            threadId,
            cause,
          }).pipe(Effect.as(false)),
        ),
      ),
  );
  return skipIdleCompactionOfOwnedThreads(orchestrator, handoffOwns);
});

/** `make` for the runtime's workers (`ForkLayers.handoffThreadsSkipIdleCompaction`). */
export const layer = Layer.effect(Orchestrator.OrchestratorV2, make).pipe(
  Layer.provide(ProjectionStore.layer),
);
