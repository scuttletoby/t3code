/**
 * Fork: one owner per thread. Upstream's idle compaction skips a thread whose handoff mode is
 * `handoff` and whose handoff can run, decided at the send, so a compaction and a handoff never both
 * act on it. Every other command passes through; `IdleCompactionService.ts` stays upstream's.
 */
import {
  type ModelSelection,
  type OrchestrationV2ServerCommand,
  type ProjectId,
  holdsThreadRole,
  type ProviderInstanceId,
  resolveHandoffSettings,
  resolveProjectProviderInstanceEnabled,
  type ServerSettings as ServerSettingsValue,
  type ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ProviderAdapterRegistry from "../../orchestration-v2/ProviderAdapterRegistry.ts";
import * as ServerSettings from "../../serverSettings.ts";

/**
 * A thread's handoff settings and mode: its project's override, else the server's; a thread that
 * holds a role (HO6) takes the settings' `roles` mode unless it has its own.
 */
const threadHandoffSettings = (
  current: Pick<ServerSettingsValue, "handoff" | "projectSettingsOverrides" | "threadRoles">,
  projectId: ProjectId,
  threadId: ThreadId,
): ReturnType<typeof resolveHandoffSettings> =>
  resolveHandoffSettings(
    current.handoff,
    Object.hasOwn(current.projectSettingsOverrides, projectId)
      ? current.projectSettingsOverrides[projectId]?.handoff
      : undefined,
    threadId,
    holdsThreadRole(current.threadRoles, { id: threadId, projectId }),
  );

/** Why a handoff cannot run: its provider instance is turned off for the project, or not there. */
export type HandoffBlocked = {
  readonly instanceId: ProviderInstanceId;
  readonly reason: "instance-off" | "instance-missing";
};

/**
 * One source for the wrapper and the service: a thread's handoff settings and mode, and whether the
 * handoff can run. Its instance (`modelSelection`, else the thread's, as `send` sends) must be on for
 * the project (upstream's rule) and registered (upstream's lookup, where a send to it fails).
 */
export const threadHandoff = Effect.fn("HandoffOwner.threadHandoff")(function* (
  current: Pick<
    ServerSettingsValue,
    "handoff" | "projectSettingsOverrides" | "providerInstances" | "threadRoles"
  >,
  thread: {
    readonly id: ThreadId;
    readonly projectId: ProjectId;
    readonly modelSelection: ModelSelection;
  },
) {
  const resolved = threadHandoffSettings(current, thread.projectId, thread.id);
  if (resolved.mode !== "handoff") return { ...resolved, blocked: null };
  const instanceId = (resolved.settings.modelSelection ?? thread.modelSelection).instanceId;
  return {
    ...resolved,
    blocked: yield* handoffInstanceBlocked(current, thread.projectId, instanceId),
  };
});

/**
 * Whether a handoff send to `instanceId` would be blocked: turned off for the project (upstream's
 * rule), or not registered (upstream's lookup, where a send to it fails). `threadHandoff` asks it for
 * the instance the setting names; the retry of a failed handoff run also asks it for the run's own.
 */
export const handoffInstanceBlocked = Effect.fn("HandoffOwner.handoffInstanceBlocked")(function* (
  current: Pick<ServerSettingsValue, "projectSettingsOverrides" | "providerInstances">,
  projectId: ProjectId,
  instanceId: ProviderInstanceId,
) {
  if (!resolveProjectProviderInstanceEnabled(current, projectId, instanceId))
    return { instanceId, reason: "instance-off" } as HandoffBlocked;
  const adapters = yield* ProviderAdapterRegistry.ProviderAdapterRegistryV2;
  // Any lookup failure means the send would fail the same way: count it as missing.
  const registered = yield* adapters.get(instanceId).pipe(
    Effect.as(true),
    Effect.orElseSucceed(() => false),
  );
  return registered ? null : ({ instanceId, reason: "instance-missing" } as HandoffBlocked);
});

/** Handoff owns a thread when its mode is `handoff` and the handoff can run. */
const handoffOwnsThread = (handoff: Effect.Success<ReturnType<typeof threadHandoff>>) =>
  handoff.mode === "handoff" && handoff.blocked === null;

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
 * Upstream's orchestrator, with idle compaction skipped on a thread that handoff owns (mode
 * `handoff`, and the handoff can run). Read at each send, so a setting or provider instance changed
 * while compaction waited counts. When the settings or the thread cannot be read, upstream decides
 * (compaction goes ahead), as it does when the handoff cannot run.
 */
export const make = Effect.gen(function* () {
  const orchestrator = yield* Orchestrator.OrchestratorV2;
  const serverSettings = yield* ServerSettings.ServerSettingsService;
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  const adapters = yield* ProviderAdapterRegistry.ProviderAdapterRegistryV2;
  const handoffOwns = Effect.fn("HandoffOwner.handoffOwns")(
    function* (threadId: ThreadId) {
      const thread = yield* projections.findThread(threadId);
      if (thread === null) return false;
      const current = yield* serverSettings.getSettings;
      const handoff = yield* threadHandoff(current, thread).pipe(
        Effect.provideService(ProviderAdapterRegistry.ProviderAdapterRegistryV2, adapters),
      );
      if (handoff.mode === "handoff" && handoff.blocked !== null)
        yield* Effect.logWarning("handoff cannot run; idle compaction covers this thread", {
          threadId,
          ...handoff.blocked,
        });
      return handoffOwnsThread(handoff);
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
  // Upstream's own lookup over the server's instance registry, as server.ts gives /mcp.
  Layer.provide(ProviderAdapterRegistry.layerFromProviderInstanceRegistry),
);
