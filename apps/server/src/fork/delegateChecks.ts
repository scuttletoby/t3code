/**
 * Fork: every fork check on the delegation paths, behind three seats, so the upstream
 * `OrchestratorMcpService.ts` keeps only the seat calls (`delegateChecks.test.ts` counts them).
 * A retry of an accepted request passes every seat untouched: it starts no new work.
 */
import {
  type ModelSelection,
  type OrchestratorMcpDelegateTaskInput,
  OrchestratorMcpFailure,
  type OrchestratorMcpTarget,
  type OrchestrationV2ThreadProjection,
  type RuntimeMode,
  type ServerProvider,
  type ThreadId,
} from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import type * as Scope from "effect/Scope";

import { resolveRuntimeMode } from "../mcp/delegatedTaskModes.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as UsagePacer from "../usage/UsagePacer.ts";
import * as UsagePacingGate from "../usage/UsagePacingGate.ts";

/** A checked delegation target (the same shape as the service's resolved target). */
export interface DelegateTarget {
  readonly modelSelection: ModelSelection;
  readonly supportedRuntimeModes: ReadonlyArray<RuntimeMode> | undefined;
}

type Parent = Pick<OrchestrationV2ThreadProjection, "thread">;

/** What the checks need from the service that hosts them. */
export interface DelegateChecksHost {
  /** The service's own target resolution, used to check a target the pacer may move to. */
  readonly resolveTarget: (input: {
    readonly parent: Parent;
    readonly target: OrchestratorMcpTarget | undefined;
    readonly providers: ReadonlyArray<ServerProvider>;
  }) => Effect.Effect<DelegateTarget, OrchestratorMcpFailure>;
}

export interface BeforeWorkspaceInput {
  /** A retry of a delegation that was accepted: it starts no work, so nothing is checked. */
  readonly isRetry: boolean;
  readonly parent: Parent;
  readonly requested: OrchestratorMcpTarget | undefined;
  readonly runtimeMode: OrchestratorMcpDelegateTaskInput["runtimeMode"];
  readonly providers: ReadonlyArray<ServerProvider>;
  readonly target: DelegateTarget;
}

export interface BeforeWorkspaceResult {
  readonly target: DelegateTarget;
  /** Text for the reply when a check changed or would change the task. */
  readonly note?: string;
}

export interface AfterWorkspaceInput {
  readonly isRetry: boolean;
  readonly callerThreadId: ThreadId;
  readonly target: DelegateTarget;
}

/** What `afterWorkspace` holds for the call (an agent slot), until `release` or the scope ends. */
export interface DelegateHold {
  /** The dispatch named the child thread; it keeps counting until its run shows. */
  readonly started: (childThreadId: ThreadId) => Effect.Effect<void>;
  /** The dispatch is done. Safe to run more than once; the call's scope runs it on every exit. */
  readonly release: Effect.Effect<void>;
}

export interface BeforeCreateThreadInput {
  readonly isRetry: boolean;
  readonly callerThreadId: ThreadId;
  readonly parent: Parent;
  readonly target: DelegateTarget;
}

/** The target the thread is created on: a later check (model policy, placement) may change it. */
export interface BeforeCreateThreadResult {
  readonly target: DelegateTarget;
}

export interface DelegateChecks {
  /** `delegate_task`, before the child workspace is planned. May move the task and add a note. */
  readonly beforeWorkspace: (
    input: BeforeWorkspaceInput,
  ) => Effect.Effect<BeforeWorkspaceResult, OrchestratorMcpFailure>;
  /** `delegate_task`, on the final target, last before the dispatch. */
  readonly afterWorkspace: (
    input: AfterWorkspaceInput,
  ) => Effect.Effect<DelegateHold, OrchestratorMcpFailure, Scope.Scope>;
  /** `create_threads`, per thread before its dispatch. */
  readonly beforeCreateThread: (
    input: BeforeCreateThreadInput,
  ) => Effect.Effect<BeforeCreateThreadResult, OrchestratorMcpFailure>;
}

const failure = (code: OrchestratorMcpFailure["code"], message: string) =>
  new OrchestratorMcpFailure({ code, message });

const advertisedModel = (provider: ServerProvider, model: string) =>
  provider.models.find((candidate) => candidate.slug === model) ??
  provider.models.find((candidate) =>
    candidate.aliases?.some((alias) => alias.toLowerCase() === model.toLowerCase()),
  );

const NO_HOLD: DelegateHold = { started: () => Effect.void, release: Effect.void };

/**
 * Builds the checks inside the hosting service. The usage pacer and the slot gate are read if the
 * server runs them; they are optional so no test of the host has to provide them (server.ts
 * refuses to compile without them).
 */
export const make = Effect.fnUntraced(function* (host: DelegateChecksHost) {
  const serverSettings = yield* ServerSettings.ServerSettingsService;
  const threadManagement = yield* ThreadManagementService.ThreadManagementService;
  const usagePacer = yield* Effect.serviceOption(UsagePacer.UsagePacer);
  const slotGate = yield* Effect.serviceOption(SlotGate.SlotGate);

  /**
   * The usage pacing gate (`usagePacingGate` setting, off by default). A setting or a pace that
   * cannot be read lets the task through as asked. The pacer moves a task only to a target with
   * the same runtime mode, so the mode the caller checked on the requested target still holds.
   */
  const paceTarget = Effect.fn("DelegateChecks.paceTarget")(function* (
    input: BeforeWorkspaceInput,
  ): Effect.fn.Return<BeforeWorkspaceResult, OrchestratorMcpFailure> {
    const settings = yield* serverSettings.getSettings.pipe(Effect.option);
    if (Option.isNone(settings) || settings.value.usagePacingGate === "off") {
      return { target: input.target };
    }
    const { instanceId, model, options } = input.target.modelSelection;
    const provider = input.providers.find((candidate) => candidate.instanceId === instanceId);
    const parentMode = input.parent.thread.runtimeMode;
    const modeOn = (target: DelegateTarget) =>
      resolveRuntimeMode(parentMode, input.runtimeMode, target).pipe(Effect.option);
    const outcome = yield* UsagePacingGate.decide({
      mode: settings.value.usagePacingGate,
      lowerEffort: settings.value.usagePacingLowerEffort,
      nowMs: yield* Clock.currentTimeMillis,
      instanceId,
      pinned: input.requested?.providerInstanceId !== undefined,
      providers: input.providers,
      descriptors:
        provider === undefined
          ? undefined
          : advertisedModel(provider, model)?.capabilities?.optionDescriptors,
      options,
      paceOf: (id) =>
        Option.match(usagePacer, {
          onNone: () => Effect.undefined,
          onSome: (pacer) => pacer.paceOf(id).pipe(Effect.orElseSucceed(() => undefined)),
        }),
      // The same model, options and runtime mode on an instance this project may use, or nothing.
      targetOn: Effect.fnUntraced(function* (candidate) {
        const enabled = yield* threadManagement.isProviderInstanceEnabledForProject({
          projectId: input.parent.thread.projectId,
          instanceId: candidate,
        });
        if (!enabled) return undefined;
        const resolved = yield* host
          .resolveTarget({
            parent: input.parent,
            target: {
              providerInstanceId: candidate,
              model,
              ...(options === undefined ? {} : { options }),
            },
            providers: input.providers,
          })
          .pipe(Effect.option);
        if (Option.isNone(resolved)) return undefined;
        const modes = yield* Effect.all([modeOn(input.target), modeOn(resolved.value)]);
        return Option.isSome(modes[1]) && Option.getOrNull(modes[0]) === modes[1].value
          ? resolved.value
          : undefined;
      }),
    });
    switch (outcome.action) {
      case "hold":
        return yield* failure("usage_pacing_hold", outcome.message);
      case "switch":
        return { target: outcome.target, note: outcome.note };
      case "lowerEffort":
        return {
          target: {
            ...input.target,
            modelSelection: {
              ...input.target.modelSelection,
              options: [
                ...(options ?? []).filter((option) => option.id !== outcome.step.optionId),
                { id: outcome.step.optionId, value: outcome.step.to },
              ],
            },
          },
          note: outcome.note,
        };
      case "pass":
        return {
          target: input.target,
          ...(outcome.note === undefined ? {} : { note: outcome.note }),
        };
    }
  });

  /**
   * The agent slot gate (`agentSlots`, off by default) on the final target. The slot is taken
   * uninterruptibly and released when the call's scope closes, on every exit (failure, defect, a
   * cancelled call); `release` ends it sooner, once the dispatch is done.
   */
  const acquireSlot = (gate: SlotGate.SlotGate["Service"], input: AfterWorkspaceInput) =>
    Effect.acquireRelease(
      gate
        .acquire({
          callerThreadId: input.callerThreadId,
          targetInstanceId: input.target.modelSelection.instanceId,
        })
        .pipe(
          Effect.flatMap((outcome) =>
            outcome.type === "refuse"
              ? Effect.fail(failure("slots_full", outcome.message))
              : Effect.succeed(outcome.token),
          ),
        ),
      (token) => gate.release(token),
    ).pipe(
      Effect.map((token): DelegateHold => ({
        started: (childThreadId) => gate.started(token, childThreadId),
        release: gate.release(token),
      })),
    );

  const checks: DelegateChecks = {
    beforeWorkspace: (input) =>
      input.isRetry ? Effect.succeed({ target: input.target }) : paceTarget(input),
    afterWorkspace: (input) =>
      input.isRetry || Option.isNone(slotGate)
        ? Effect.succeed(NO_HOLD)
        : acquireSlot(slotGate.value, input),
    // No fork check on created threads yet; model policy (M3) and placement (P1) add theirs here.
    beforeCreateThread: (input) => Effect.succeed({ target: input.target }),
  };
  return checks;
});
