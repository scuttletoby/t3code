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
  type OrchestratorMcpDelegateTaskResult,
  OrchestratorMcpFailure,
  type OrchestratorMcpCreateThreadRequest,
  type OrchestratorMcpTarget,
  PROVIDER_WORKSPACE_SNAPSHOT_TTL_MS,
  type OrchestrationV2Subagent,
  type OrchestrationV2ThreadProjection,
  type ProviderOptionSelection,
  type RuntimeMode,
  type ServerProvider,
  type ServerSettings as ServerSettingsValue,
  type ThreadId,
} from "@t3tools/contracts";
import { kindOfRole } from "@t3tools/shared/slotObserve";
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
import * as AccountOrder from "../usage/accountOrder.ts";
import * as UsagePacer from "../usage/UsagePacer.ts";
import * as UsagePacingGate from "../usage/UsagePacingGate.ts";
import * as AgentSlotRoutes from "./agentSlotRoutes.ts";
import * as LaunchMcpProfile from "./launchMcpProfile.ts";
import * as ModelPolicyCheck from "./modelPolicyCheck.ts";
import * as PlacementAdvice from "./placementAdvice.ts";
import { readPlacementForThisOs } from "./placementRead.ts";
import * as RemoteTaskPrompts from "./remoteTaskPrompts.ts";

/** A checked delegation target (the same shape as the service's resolved target). */
export interface DelegateTarget {
  readonly modelSelection: ModelSelection;
  readonly supportedRuntimeModes: ReadonlyArray<RuntimeMode> | undefined;
  /** The slot gate's capacity kind, from the task's `role` (`kindOfRole`); absent: a writer. */
  readonly agentKind?: string;
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
  /** The task's target, with its `role` (the slot gate's kind; the caller may lie about it). */
  readonly requested:
    | (OrchestratorMcpTarget & { readonly role?: OrchestratorMcpDelegateTaskInput["role"] })
    | undefined;
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
  /**
   * The thread to be created: its place in the call's batch. A thread with a prompt starts a run
   * and takes an agent slot; the first one checks the whole batch fits. The caller is the parent.
   */
  readonly child: {
    readonly threadId: ThreadId;
    readonly index: number;
    readonly batch: ReadonlyArray<OrchestratorMcpCreateThreadRequest>;
  };
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
  /**
   * `delegate_task`, on the final target, last before the dispatch. Its hold lasts until the
   * dispatch and the child's run preparation end, or the call's scope closes.
   */
  readonly afterWorkspace: (
    input: AfterWorkspaceInput,
  ) => Effect.Effect<DelegateHold, OrchestratorMcpFailure, Scope.Scope>;
  /**
   * `create_threads`, per thread before its dispatch. A caller with a narrower MCP profile than
   * `all` is refused a thread that would get more (`launchMcpProfile.ts`): it has no parent. A
   * thread that starts a run takes an agent slot last, on the final target, or is refused
   * `slots_full` (`agentSlotRoutes.ts`).
   */
  readonly beforeCreateThread: (
    input: BeforeCreateThreadInput,
  ) => Effect.Effect<BeforeCreateThreadResult, OrchestratorMcpFailure>;
  /**
   * A remote `delegate_task`'s target, before it is sent: {@link remoteTarget} under the policy,
   * then the model policy's check of the pick (`modelPolicyCheck.ts`; a refusal sends nothing).
   */
  readonly remoteTarget: <Target extends OrchestratorMcpTarget>(
    target: Target,
  ) => Effect.Effect<Target, OrchestratorMcpFailure>;
  /**
   * A remote task's status: what its thread on the linked machine waits on, read from that
   * machine (fork/remoteTaskPrompts.ts). Spread over upstream's fixed `false`/`null`.
   */
  readonly remotePrompts: (
    task: OrchestrationV2Subagent,
    status: OrchestratorMcpDelegateTaskResult["status"],
  ) => Effect.Effect<Partial<RemoteTaskPrompts.StatusReport>>;
}

/**
 * Upstream's `endsTaskWait` seat: a pending secret request of a task on a linked machine
 * ends a blocking wait, as a question or approval does. Pure: it reads only the result.
 */
export const promptEndsWait = (
  result: Pick<OrchestratorMcpDelegateTaskResult, "pendingSecretRequests">,
): boolean => (result.pendingSecretRequests?.length ?? 0) > 0;

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
 * The option ids a call names: its own, and `ultracode` when it names the old effort `ultracode`,
 * which T3 reads as `xhigh` plus the ultracode option (`withLegacyUltracodeEffort`).
 */
const namedOptionIds = (requested: OrchestratorMcpTarget): ReadonlySet<string> => {
  const options = requested.options ?? [];
  const ids = new Set(options.map((option) => option.id));
  if (options.some((option) => option.id === "effort" && option.value === "ultracode")) {
    ids.add("ultracode");
  }
  return ids;
};

/**
 * The one rule for a child's options on every launch path, applied while a model policy is on: a
 * call that names its model keeps only the options it names, as a policy and the model guard check
 * only the call. A call that names no model keeps the parent's options, as upstream does.
 */
export const ownOptions = (
  options: ReadonlyArray<ProviderOptionSelection> | undefined,
  requested: OrchestratorMcpTarget | undefined,
): ReadonlyArray<ProviderOptionSelection> | undefined => {
  if (requested?.model === undefined || options === undefined) return options;
  const named = namedOptionIds(requested);
  return options.filter((option) => named.has(option.id));
};

/** A model policy is on (`advise` or `enforce`): {@link ownOptions} applies. */
const policyOn = (settings: { readonly modelPolicy: { readonly mode: string } }) =>
  settings.modelPolicy.mode !== "off";

/** `ownOptions` on a resolved target: the same object back when nothing is dropped. */
export const withOwnOptions = (
  target: DelegateTarget,
  requested: OrchestratorMcpTarget | undefined,
): DelegateTarget => {
  const options = ownOptions(target.modelSelection.options, requested);
  return options === undefined || options.length === target.modelSelection.options?.length
    ? target
    : { ...target, modelSelection: { ...target.modelSelection, options } };
};

/**
 * {@link ownOptions} for a remote `delegate_task`, whose `RemoteDelegation.ts` sends a call's own
 * options as given and lays the parent's under none: a call that names its model and no option
 * sends `[]`. Its retry sends the same. The checks' `remoteTarget` applies it under a policy.
 */
export const remoteTarget = <Target extends OrchestratorMcpTarget>(target: Target): Target =>
  target.model === undefined || target.options !== undefined ? target : { ...target, options: [] };

/** The pacer's lowered effort is outside the policy: the task runs at the effort it checked. */
const KEEP_CHECKED = Symbol("keep the checked effort");

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
  const launchMcpProfile = yield* LaunchMcpProfile.makeLaunchMcpProfileCheck;
  const checkPick = yield* ModelPolicyCheck.makePickCheck;
  const remoteStatus = yield* RemoteTaskPrompts.makeStatusReport();
  const placementAdvice = yield* PlacementAdvice.make();
  /**
   * The model policy's check of the agent's pick on a resolved target (`modelPolicyCheck.ts`): a
   * call that names no model inherits one, which reads as no model.
   */
  const policyCheck = (
    route: ModelPolicyCheck.PolicyRoute,
    target: DelegateTarget,
    requested: OrchestratorMcpTarget | undefined,
    options?: ModelPolicyCheck.PickCheckOptions,
  ) =>
    checkPick(
      route,
      Effect.map(providerRegistry.getProviders, (providers) =>
        ModelPolicyCheck.pickOfSelection(
          providers,
          target.modelSelection,
          requested?.model !== undefined,
        ),
      ),
      options,
    );
  /**
   * Folder scans for a switch target that gave no settled answer: instance and folder -> the time a
   * scan may run again. Memory only: a restart costs one more scan. Entries that no longer back off
   * (expired, or set before the clock went back) are dropped on each write.
   */
  const failedScans = yield* Ref.make<ReadonlyMap<string, number>>(new Map());
  /**
   * Whether {@link ownOptions} applies: a model policy is on. A settings read that fails counts as
   * on, as a policy that cannot be read is enforced (`modelPolicy.ts`), and a child that starts on
   * its model's defaults is the safe side.
   */
  const ownOptionsOn = serverSettings.getSettings.pipe(
    Effect.map(policyOn),
    Effect.orElseSucceed(() => true),
  );
  /** {@link withOwnOptions} while the policy is on; settings are read only when it could drop one. */
  const ownTarget = (target: DelegateTarget, requested: OrchestratorMcpTarget | undefined) =>
    withOwnOptions(target, requested) === target
      ? Effect.succeed(target)
      : Effect.map(ownOptionsOn, (on) => (on ? withOwnOptions(target, requested) : target));

  /**
   * The same model, options and runtime mode as `input.target` on another instance this project may
   * use, or why not. `mayScan`: a model the instance does not list may be looked for with a folder
   * scan (`advise` runs none). Shared by the account order and the pacing gate.
   */
  const targetOnFor = (
    input: BeforeWorkspaceInput,
    settings: ServerSettingsValue,
    providers: ReadonlyArray<ServerProvider>,
    nowMs: number,
    mayScan: boolean,
  ) => {
    const { model, options } = input.target.modelSelection;
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
    return Effect.fnUntraced(function* (
      candidate: ServerProvider["instanceId"],
    ): Effect.fn.Return<UsagePacingGate.TargetOn<DelegateTarget>> {
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
        const before = folderScan(providers, candidate, cwd, nowMs);
        const key = `${candidate}\u0000${cwd}`;
        const noAnswer = (retryAtMs: number) => ({
          cannot: `it does not list ${model} and its scan of this folder gave no result (scanned again after ${DateTime.formatIso(DateTime.makeUnsafe(retryAtMs))})`,
        });
        const retryAtMs = (yield* Ref.get(failedScans)).get(key);
        // `enforce` scans unless a back-off holds, and the registry runs a probe for any snapshot
        // that is not settled (slash commands still loading too). So the advice answers from the
        // list only where `enforce` would: during a back-off, or from a settled list.
        if (before.models && (backingOff(retryAtMs, nowMs) || (!mayScan && before.settled))) {
          return notListed;
        }
        // A back-off that `enforce` recorded holds in `advise` too: `enforce` would not scan now,
        // so the advice must not say it would.
        if (backingOff(retryAtMs, nowMs)) return noAnswer(retryAtMs);
        if (!mayScan) {
          return {
            cannot: before.models
              ? `it does not list ${model} for this folder in a scan that is still loading its slash commands (advice only, so it was not scanned again)`
              : `it does not list ${model} and has no current scan of this folder (advice only, so none was run)`,
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
        ? {
            target: policyOn(settings)
              ? withOwnOptions(resolved.success, input.requested)
              : resolved.success,
          }
        : { cannot: "it would run the task in another runtime mode" };
    });
  };

  /**
   * The account order (`accountOrder`, empty by default): a task with no named instance, on a spent
   * account, moves to the first account in the order with room that can run it. Unknown usage takes
   * no work; with no room anywhere the task stays (holding is the pacing gate's call).
   */
  const orderTarget = Effect.fn("DelegateChecks.orderTarget")(function* (
    input: BeforeWorkspaceInput,
    settings: ServerSettingsValue,
  ) {
    const unchanged: BeforeWorkspaceResult = { target: input.target };
    if (settings.accountOrder.length === 0 || input.requested?.providerInstanceId !== undefined) {
      return unchanged;
    }
    const paceOf = (id: ServerProvider["instanceId"]) =>
      Option.match(usagePacer, {
        onNone: () => Effect.undefined,
        onSome: (pacer) => pacer.paceOf(id).pipe(Effect.orElseSucceed(() => undefined)),
      });
    const from = input.target.modelSelection.instanceId;
    const providers = withFolderModels(yield* providerRegistry.getProviders, input.cwd);
    const current = providers.find((provider) => provider.instanceId === from);
    if (current === undefined) return unchanged;
    const nowMs = yield* Clock.currentTimeMillis;
    const fromState = AccountOrder.accountStateOf(yield* paceOf(from));
    if (fromState.kind !== "spent") return unchanged;
    const ids = AccountOrder.orderCandidateIds({
      order: settings.accountOrder,
      current,
      providers,
    });
    if (ids.length === 0) return unchanged;
    const paces = yield* Effect.forEach(ids, paceOf);
    const candidates = ids.map((instanceId, index): AccountOrder.OrderCandidate => ({
      instanceId,
      state: AccountOrder.accountStateOf(paces[index]),
    }));
    const full = `${from} is at its limit (${AccountOrder.describeSpent(fromState.windows)})`;
    const targetOn = targetOnFor(input, settings, providers, nowMs, true);
    const skipped: Array<string> = [];
    for (const candidate of candidates) {
      if (candidate.state.kind !== "open") {
        skipped.push(AccountOrder.describeSkipped(candidate));
        continue;
      }
      const on = yield* targetOn(candidate.instanceId);
      if ("cannot" in on) {
        skipped.push(`${candidate.instanceId} has room, but ${on.cannot}`);
        continue;
      }
      const passed = skipped.length === 0 ? "" : ` (passed over: ${skipped.join("; ")})`;
      return {
        target: on.target,
        note: `Account order: ${full}, so this task runs on ${candidate.instanceId}, the first account in the order with room${passed}.`,
      };
    }
    return {
      target: input.target,
      note: `Account order: ${full} and no account in the order can take this task (${skipped.join("; ")}), so it runs on ${from} as asked.`,
    };
  });

  /**
   * The usage pacing gate (`usagePacingGate` setting, off by default). A setting or a pace that
   * cannot be read lets the task through as asked. The pacer moves a task only to a target with
   * the same runtime mode, so the mode the caller checked on the requested target still holds.
   */
  const gateTarget = Effect.fn("DelegateChecks.gateTarget")(function* (
    input: BeforeWorkspaceInput,
    settings: ServerSettingsValue,
  ): Effect.fn.Return<BeforeWorkspaceResult, OrchestratorMcpFailure> {
    const { instanceId, model, options } = input.target.modelSelection;
    const nowMs = yield* Clock.currentTimeMillis;
    // `advise` only says what `enforce` would do, so it runs no folder scan (a scan publishes a
    // snapshot and takes the driver's probe time). A named instance still scans in `enforce`: the
    // answer decides between holding the task and lowering its effort.
    const mayScan = settings.usagePacingGate === "enforce";
    // Read after the target lookup, so any refresh or folder scan it ran is in the list.
    const providers = withFolderModels(yield* providerRegistry.getProviders, input.cwd);
    const provider = providers.find((candidate) => candidate.instanceId === instanceId);
    const outcome = yield* UsagePacingGate.decide({
      mode: settings.usagePacingGate,
      lowerEffort: settings.usagePacingLowerEffort,
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
      targetOn: targetOnFor(input, settings, providers, nowMs, mayScan),
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

  /** The account order, then the pacing gate on the task where the order left it. */
  const paceTarget = Effect.fn("DelegateChecks.paceTarget")(function* (
    input: BeforeWorkspaceInput,
  ): Effect.fn.Return<BeforeWorkspaceResult, OrchestratorMcpFailure> {
    const settings = yield* serverSettings.getSettings.pipe(Effect.option);
    if (Option.isNone(settings)) return { target: input.target };
    const ordered = yield* orderTarget(input, settings.value);
    if (settings.value.usagePacingGate === "off") return ordered;
    const gated = yield* gateTarget({ ...input, target: ordered.target }, settings.value);
    const notes = [ordered.note, gated.note].filter((note) => note !== undefined);
    return { target: gated.target, ...(notes.length === 0 ? {} : { note: notes.join(" ") }) };
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
          kind: input.target.agentKind,
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
   * Placement advice (`placement.mode` advise, row P6) on the final target, last, after the note of
   * the checks that may move the task. It moves nothing; its text joins the note. A settings read,
   * a runtime mode or a provider list that cannot be read gives no advice; the task goes on.
   */
  const withPlacementAdvice = Effect.fn("DelegateChecks.withPlacementAdvice")(function* (
    input: BeforeWorkspaceInput,
    result: BeforeWorkspaceResult,
  ) {
    const settings = yield* serverSettings.getSettings.pipe(Effect.option);
    if (Option.isNone(settings) || (yield* readPlacementForThisOs(settings.value)).mode === "off") {
      return result;
    }
    const runtimeMode = yield* resolveRuntimeMode(
      input.parent.thread.runtimeMode,
      input.runtimeMode,
      result.target,
    ).pipe(Effect.option);
    if (Option.isNone(runtimeMode)) return result;
    const { instanceId, model } = result.target.modelSelection;
    const providers = yield* providerRegistry.getProviders;
    const advice = yield* placementAdvice.advise({
      settings: settings.value,
      projectId: input.parent.thread.projectId,
      runtimeMode: runtimeMode.value,
      driverKind: providers.find((provider) => provider.instanceId === instanceId)?.driver,
      model,
    });
    if (advice === undefined) return result;
    return { ...result, note: result.note === undefined ? advice : `${result.note} ${advice}` };
  });

  const checks: DelegateChecks = {
    // Before the pacer, so it paces the child's own options; a target it moves to keeps them too.
    // The model policy checks the agent's pick first (a refusal holds and paces nothing), and again
    // the target the pacer moved the task to or lowered its effort on: what runs is inside too. An
    // effort the pacer lowers off the row keeps the checked one (the agent cannot fix that pick).
    beforeWorkspace: (input) =>
      (input.isRetry
        ? Effect.succeed<BeforeWorkspaceResult>({ target: input.target })
        : Effect.gen(function* () {
            const target = yield* ownTarget(input.target, input.requested);
            const policyNote = yield* policyCheck("delegate_task", target, input.requested);
            const paced = yield* paceTarget({ ...input, target });
            const lowered =
              paced.target.modelSelection.instanceId === target.modelSelection.instanceId &&
              paced.target.modelSelection.model === target.modelSelection.model;
            const pacedNote =
              paced.target === target
                ? undefined
                : yield* policyCheck("delegate_task", paced.target, input.requested, {
                    logAs: "model policy paced pick",
                  }).pipe(
                    Effect.catchIf(
                      () => lowered,
                      (refusal) =>
                        Effect.logWarning(
                          "usagePacingLowerEffort lowered an effort that modelPolicy does not allow: the task keeps its checked effort",
                        ).pipe(
                          Effect.annotateLogs({ refusal: refusal.message }),
                          Effect.as(KEEP_CHECKED),
                        ),
                    ),
                  );
            if (pacedNote === KEEP_CHECKED) {
              return { target, ...(policyNote === undefined ? {} : { note: policyNote }) };
            }
            const notes = [
              policyNote,
              pacedNote === policyNote ? undefined : pacedNote,
              paced.note,
            ];
            const note = notes.filter((text) => text !== undefined).join(" ");
            return { target: paced.target, ...(note === "" ? {} : { note }) };
          }).pipe(Effect.flatMap((result) => withPlacementAdvice(input, result)))
      ).pipe(
        // The task's kind rides on the final target to the slot gate (`afterWorkspace`); a retry
        // holds no slot, so its target stays as it came.
        Effect.map((result) =>
          input.isRetry
            ? result
            : {
                ...result,
                target: { ...result.target, agentKind: kindOfRole(input.requested?.role) },
              },
        ),
      ),
    afterWorkspace: (input) =>
      input.isRetry || Option.isNone(slotGate)
        ? Effect.succeed(NO_HOLD)
        : acquireSlot(slotGate.value, input),
    // Placement (P1) adds its check here. The MCP profile check is first: a refusal starts nothing.
    // The slot is last, on the final target; a retry of an accepted creation starts nothing new.
    beforeCreateThread: (input) => {
      const slot = (target: DelegateTarget, retry: AgentSlotRoutes.CreateThreadSlot["retry"]) =>
        AgentSlotRoutes.admitCreateThread(slotGate, {
          callerThreadId: input.parent.thread.id,
          threadId: input.child.threadId,
          index: input.child.index,
          batch: input.child.batch,
          targetInstanceId: target.modelSelection.instanceId,
          defaultInstanceId: input.parent.thread.modelSelection.instanceId,
          retry,
        });
      // A retry starts the run its first try did not (the dispatch after the creation failed).
      const hasRun = Effect.suspend(() =>
        threadManagement.getThreadRecords(input.child.threadId, ["runs"]),
      ).pipe(
        Effect.map((records) => records.runs.length > 0),
        Effect.orElseSucceed(() => false),
      );
      return input.isRetry
        ? slot(input.target, { hasRun }).pipe(Effect.as({ target: input.target }))
        : launchMcpProfile(input.parent.thread, {
            projectId: input.parent.thread.projectId,
            route: "create_threads",
          }).pipe(
            Effect.andThen(ownTarget(input.target, input.requested)),
            // The model policy (advice goes to the log: the reply has no place for a note).
            Effect.tap((target) => policyCheck("create_threads", target, input.requested)),
            Effect.tap((target) => slot(target, null)),
            Effect.map((target) => ({ target })),
          );
    },
    remoteTarget: (target) =>
      (remoteTarget(target) === target
        ? Effect.succeed(target)
        : Effect.map(ownOptionsOn, (on) => (on ? remoteTarget(target) : target))
      ).pipe(
        // The linked machine's driver is not known here (a driverKind the call names is the agent's
        // claim, not read): every row naming the model must allow it. This machine's catalog names
        // the model's own reasoning control, when it has the model.
        Effect.tap((sent) =>
          checkPick(
            "delegate_task",
            Effect.map(providerRegistry.getProviders, (providers) =>
              ModelPolicyCheck.pickOfRemoteTarget(providers, sent),
            ),
          ),
        ),
      ),
    remotePrompts: (task, status) =>
      remoteStatus(
        task,
        status === "completed" ||
          status === "failed" ||
          status === "cancelled" ||
          status === "interrupted",
      ),
  };
  return checks;
});
