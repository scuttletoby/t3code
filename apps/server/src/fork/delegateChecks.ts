/**
 * Fork: every fork check on the delegation paths, behind three seats, so the upstream
 * `OrchestratorMcpService.ts` keeps only the seat calls (`delegateChecks.test.ts` counts them).
 * A retry of an accepted request passes every seat untouched: it starts no new work.
 */
import {
  isProviderWorkspaceSnapshotCurrent,
  isProviderWorkspaceSnapshotPending,
  type ModelSelection,
  type OrchestratorMcpDelegateTaskInput,
  OrchestratorMcpFailure,
  type OrchestratorMcpTarget,
  PROVIDER_WORKSPACE_SNAPSHOT_TTL_MS,
  type OrchestrationV2ThreadProjection,
  type RuntimeMode,
  type ServerProvider,
  type ThreadId,
} from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import type * as Scope from "effect/Scope";

import { resolveRuntimeMode } from "../mcp/delegatedTaskModes.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
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
  /**
   * The service's folder scan for one instance (the project's own provider config), returning the
   * registry after it. The target lookup scans only the requested instance.
   */
  readonly scanWorkspace: (
    instanceId: ServerProvider["instanceId"],
    cwd: string,
  ) => Effect.Effect<ReadonlyArray<ServerProvider>>;
}

export interface BeforeWorkspaceInput {
  /** A retry of a delegation that was accepted: it starts no work, so nothing is checked. */
  readonly isRetry: boolean;
  readonly parent: Parent;
  readonly requested: OrchestratorMcpTarget | undefined;
  readonly runtimeMode: OrchestratorMcpDelegateTaskInput["runtimeMode"];
  /**
   * The parent's folder, as the target lookup reads it, or none. The checks read the providers
   * themselves, after the lookup, with the models the folder's own provider config adds.
   */
  readonly cwd: string | undefined;
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
  /** The target the caller asked for, before the parent's options were laid under it. */
  readonly requested: OrchestratorMcpTarget | undefined;
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

/**
 * The models a folder's own provider config adds, as the target lookup reads them: the same merge
 * as `withWorkspaceModels` in `mcp/OrchestratorMcpService.ts`, which this module cannot import (that
 * file imports this one). `delegateChecks.test.ts` checks that both give the same lists.
 */
export const withFolderModels = (
  providers: ReadonlyArray<ServerProvider>,
  cwd: string | undefined,
): ReadonlyArray<ServerProvider> =>
  cwd === undefined
    ? providers
    : providers.map((provider) => {
        const models = provider.workspaceSnapshots?.find(
          (snapshot) => snapshot.cwd === cwd,
        )?.models;
        return models === undefined ? provider : { ...provider, models };
      });

/**
 * How an instance's last scan of a folder stands. `models`: its model list is an answer (a current
 * snapshot whose models are not pending; only its slash commands may be). Without that, a missing
 * model may only mean the scan failed. `settled`: current and nothing pending, so the registry
 * does not scan it again before it expires (it rescans a pending snapshot on every call).
 */
const folderScan = (
  providers: ReadonlyArray<ServerProvider>,
  instanceId: ServerProvider["instanceId"],
  cwd: string,
  nowMs: number,
) => {
  const snapshot = providers
    .find((provider) => provider.instanceId === instanceId)
    ?.workspaceSnapshots?.find((entry) => entry.cwd === cwd);
  const current = snapshot !== undefined && isProviderWorkspaceSnapshotCurrent(snapshot, nowMs);
  return {
    models: current && snapshot.modelsPending !== true,
    settled: current && !isProviderWorkspaceSnapshotPending(snapshot),
  };
};

/**
 * A folder scan that gave no settled answer is not run again for the same instance and folder for
 * this long (the registry would run its probe again on every call).
 */
const FAILED_SCAN_BACKOFF_MS = PROVIDER_WORKSPACE_SNAPSHOT_TTL_MS;

/**
 * A back-off still runs: its end is ahead, and no further than one back-off ahead. An end further
 * away means the wall clock went back; it then ends now, not after the jump as well.
 */
const backingOff = (retryAtMs: number | undefined, nowMs: number): retryAtMs is number =>
  retryAtMs !== undefined && retryAtMs > nowMs && retryAtMs - nowMs <= FAILED_SCAN_BACKOFF_MS;

/**
 * Options that pick the agent a driver runs (OpenCode's `agent`, Kiro's `mode`), which bring their
 * own prompt, tools and settings. The model guard (`fork-kit/model-guard`) refuses these ids in a
 * call and pins this list in a contract test.
 */
const AGENT_OPTION_IDS: Readonly<Record<string, ReadonlyArray<string>>> = {
  kiro: ["mode"],
  opencode: ["agent"],
};

const ANY_AGENT_OPTION_ID = new Set(Object.values(AGENT_OPTION_IDS).flat());

/**
 * A call that names its model drops the parent's inherited agent option, which the model guard
 * cannot see; an agent option the call names itself stays, and a call without a model keeps all.
 * `driver` undefined (an unknown instance) drops every agent id.
 */
export const withoutInheritedAgent = (
  target: DelegateTarget,
  requested: OrchestratorMcpTarget | undefined,
  driver: string | undefined,
): DelegateTarget => {
  const options = target.modelSelection.options;
  if (requested?.model === undefined || options === undefined) return target;
  const ids = driver === undefined ? ANY_AGENT_OPTION_ID : new Set(AGENT_OPTION_IDS[driver] ?? []);
  const named = new Set((requested.options ?? []).map((option) => option.id));
  const kept = options.filter((option) => !ids.has(option.id) || named.has(option.id));
  return kept.length === options.length
    ? target
    : { ...target, modelSelection: { ...target.modelSelection, options: kept } };
};

const NO_HOLD: DelegateHold = { started: () => Effect.void, release: Effect.void };

/**
 * Builds the checks inside the hosting service. The usage pacer and the slot gate are read if the
 * server runs them; they are optional so no test of the host has to provide them (server.ts
 * refuses to compile without them).
 */
export const make = Effect.fnUntraced(function* (host: DelegateChecksHost) {
  const serverSettings = yield* ServerSettings.ServerSettingsService;
  const threadManagement = yield* ThreadManagementService.ThreadManagementService;
  const providerRegistry = yield* ProviderRegistry.ProviderRegistry;
  const usagePacer = yield* Effect.serviceOption(UsagePacer.UsagePacer);
  const slotGate = yield* Effect.serviceOption(SlotGate.SlotGate);
  /**
   * Folder scans for a switch target that gave no settled answer: instance and folder -> the time a
   * scan may run again. Memory only: a restart costs one more scan. Entries that no longer back off
   * (expired, or set before the clock went back) are dropped on each write.
   */
  const failedScans = yield* Ref.make<ReadonlyMap<string, number>>(new Map());

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
    const nowMs = yield* Clock.currentTimeMillis;
    // `advise` only says what `enforce` would do, so it runs no folder scan (a scan publishes a
    // snapshot and takes the driver's probe time). A named instance still scans in `enforce`: the
    // answer decides between holding the task and lowering its effort.
    const mayScan = settings.value.usagePacingGate === "enforce";
    // Read after the target lookup, so any refresh or folder scan it ran is in the list.
    const providers = withFolderModels(yield* providerRegistry.getProviders, input.cwd);
    const provider = providers.find((candidate) => candidate.instanceId === instanceId);
    const parentMode = input.parent.thread.runtimeMode;
    const modeOn = (target: DelegateTarget) =>
      resolveRuntimeMode(parentMode, input.runtimeMode, target).pipe(Effect.option);
    const sameTaskOn = (
      candidate: ServerProvider["instanceId"],
      list: ReadonlyArray<ServerProvider>,
    ) =>
      host
        .resolveTarget({
          parent: input.parent,
          target: {
            providerInstanceId: candidate,
            model,
            ...(options === undefined ? {} : { options }),
          },
          providers: withFolderModels(list, input.cwd),
        })
        .pipe(Effect.result);
    const outcome = yield* UsagePacingGate.decide({
      mode: settings.value.usagePacingGate,
      lowerEffort: settings.value.usagePacingLowerEffort,
      nowMs,
      instanceId,
      pinned: input.requested?.providerInstanceId !== undefined,
      providers,
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
      // The same model, options and runtime mode on an instance this project may use, or why not.
      targetOn: Effect.fnUntraced(function* (candidate) {
        const enabled = yield* threadManagement.isProviderInstanceEnabledForProject({
          projectId: input.parent.thread.projectId,
          instanceId: candidate,
        });
        if (!enabled) return { cannot: "this project turned it off" };
        let resolved = yield* sameTaskOn(candidate, providers);
        const missing = (result: typeof resolved) =>
          result._tag === "Failure" && result.failure.code === "model_unavailable";
        // The lookup scanned the folder for the requested instance only, and a fresh scan of one
        // instance drops the others' snapshots of it: scan it for this one too, then look again.
        // Only a missing model is worth a scan; any other refusal stands as it is.
        if (input.cwd !== undefined && missing(resolved)) {
          const cwd = input.cwd;
          const notListed = { cannot: `it does not list ${model} for this folder` };
          // A current model list is an answer, whoever published it (another path may have, during
          // a back-off): the reply gives it, not "no result".
          const answered = folderScan(providers, candidate, cwd, nowMs).models;
          const key = `${candidate}\u0000${cwd}`;
          const noAnswer = (retryAtMs: number) => ({
            cannot: `it does not list ${model} and its scan of this folder gave no result (scanned again after ${DateTime.formatIso(DateTime.makeUnsafe(retryAtMs))})`,
          });
          const retryAtMs = (yield* Ref.get(failedScans)).get(key);
          if (answered && (!mayScan || backingOff(retryAtMs, nowMs))) return notListed;
          // A back-off that `enforce` recorded holds in `advise` too: `enforce` would not scan now,
          // so the advice must not say it would.
          if (backingOff(retryAtMs, nowMs)) return noAnswer(retryAtMs);
          if (!mayScan) {
            return {
              cannot: `it does not list ${model} and has no current scan of this folder (advice only, so none was run)`,
              wouldScan: true,
            };
          }
          const scanned = yield* host.scanWorkspace(candidate, cwd);
          resolved = yield* sameTaskOn(candidate, scanned);
          // A failed scan leaves the list as it was, and a pending one is scanned again on every
          // call: do not scan again on every call. Say "no result" only without a model list.
          const after = folderScan(scanned, candidate, cwd, nowMs);
          const backOff = missing(resolved) && !after.settled;
          const nextRetryMs = nowMs + FAILED_SCAN_BACKOFF_MS;
          yield* Ref.update(failedScans, (current) => {
            const next = new Map([...current].filter(([, until]) => backingOff(until, nowMs)));
            if (backOff) next.set(key, nextRetryMs);
            else next.delete(key);
            return next;
          });
          if (missing(resolved) && !after.models) return noAnswer(nextRetryMs);
        }
        if (resolved._tag === "Failure") {
          return {
            cannot:
              resolved.failure.code === "model_unavailable"
                ? `it does not list ${model}${input.cwd === undefined ? "" : " for this folder"}`
                : `the task cannot run there (${resolved.failure.code})`,
          };
        }
        const modes = yield* Effect.all([modeOn(input.target), modeOn(resolved.success)]);
        return Option.isSome(modes[1]) && Option.getOrNull(modes[0]) === modes[1].value
          ? { target: resolved.success }
          : { cannot: "it would run the task in another runtime mode" };
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

  /**
   * `withoutInheritedAgent` on the final target. The providers are read only when the target holds
   * an agent option id, so a target without one reads nothing.
   */
  const dropInheritedAgent = Effect.fnUntraced(function* (
    target: DelegateTarget,
    requested: OrchestratorMcpTarget | undefined,
  ) {
    const options = target.modelSelection.options ?? [];
    if (requested?.model === undefined || !options.some((o) => ANY_AGENT_OPTION_ID.has(o.id))) {
      return target;
    }
    const providers = yield* providerRegistry.getProviders;
    const driver = providers.find(
      (provider) => provider.instanceId === target.modelSelection.instanceId,
    )?.driver;
    return withoutInheritedAgent(target, requested, driver);
  });

  const checks: DelegateChecks = {
    // After the pacer: a target it moves to is looked up with the options as resolved.
    beforeWorkspace: (input) =>
      input.isRetry
        ? Effect.succeed({ target: input.target })
        : paceTarget(input).pipe(
            Effect.flatMap((result) =>
              dropInheritedAgent(result.target, input.requested).pipe(
                Effect.map((target) => ({ ...result, target })),
              ),
            ),
          ),
    afterWorkspace: (input) =>
      input.isRetry || Option.isNone(slotGate)
        ? Effect.succeed(NO_HOLD)
        : acquireSlot(slotGate.value, input),
    // Placement (P1) adds its check here.
    beforeCreateThread: (input) =>
      input.isRetry
        ? Effect.succeed({ target: input.target })
        : dropInheritedAgent(input.target, input.requested).pipe(
            Effect.map((target) => ({ target })),
          ),
  };
  return checks;
});
