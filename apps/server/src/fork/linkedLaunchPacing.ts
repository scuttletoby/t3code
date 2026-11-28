/**
 * Fork (placement P1): a `delegate_task` child a linked agent sends here runs on this machine's
 * accounts, so this machine's `usagePacingGate` decides, as for a task started here: the owner's
 * lowered effort applies here unless `modelPolicy` refuses it, the account is never moved. A hold
 * is `usage_pacing_hold`.
 */
import type { ModelSelection, ServerProvider } from "@t3tools/contracts";
import { OrchestratorMcpFailure } from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as ModelPolicyCheck from "./modelPolicyCheck.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import * as UsagePacer from "../usage/UsagePacer.ts";
import * as UsagePacingGate from "../usage/UsagePacingGate.ts";

/** What the check reads from a launch. */
export interface PacedLaunch {
  readonly modelSelection: ModelSelection;
  /** Set on a `delegate_task` child that another machine sends; the owner's launches have none. */
  readonly delegatedFrom?: unknown;
  /** `false`: the call named no model (it inherits one), as the launch's own policy check reads it. */
  readonly namesModel?: boolean;
}

/**
 * A linked machine sent this call. Its sender thread is not needed: a missing or unreadable
 * header still paces a launch that carries `delegatedFrom`.
 */
const fromLinkedMachine = Effect.map(
  Effect.serviceOption(McpInvocationContext.McpInvocationContext),
  Option.exists((scope) => scope.client?.linked === true),
);

const listsModel = (provider: ServerProvider, model: string) =>
  provider.models.find((candidate) => candidate.slug === model) ??
  provider.models.find((candidate) =>
    candidate.aliases?.some((alias) => alias.toLowerCase() === model.toLowerCase()),
  );

/**
 * Asked before the slot gate; the model selection to start with when the owner's setting lowers
 * its effort (once, as `delegate_task` does here). A setting, a provider list or a pace that
 * cannot be read lets the task through, as the gate does for a task started here.
 */
export const checkLinkedLaunchPacing = Effect.fn("LinkedLaunchPacing.check")(function* (
  launch: PacedLaunch,
): Effect.fn.Return<ModelSelection | undefined, OrchestratorMcpFailure> {
  if (launch.delegatedFrom === undefined || !(yield* fromLinkedMachine)) return undefined;
  const settingsService = yield* Effect.serviceOption(ServerSettingsService.ServerSettingsService);
  if (Option.isNone(settingsService)) return undefined;
  const settings = yield* settingsService.value.getSettings.pipe(Effect.option);
  if (Option.isNone(settings) || settings.value.usagePacingGate === "off") return undefined;
  const registry = yield* Effect.serviceOption(ProviderRegistry.ProviderRegistry);
  const providers = Option.isNone(registry) ? [] : yield* registry.value.getProviders;
  const pacer = yield* Effect.serviceOption(UsagePacer.UsagePacer);
  const { instanceId, model, options } = launch.modelSelection;
  const provider = providers.find((candidate) => candidate.instanceId === instanceId);
  const outcome = yield* UsagePacingGate.decide({
    mode: settings.value.usagePacingGate,
    lowerEffort: settings.value.usagePacingLowerEffort,
    nowMs: yield* Clock.currentTimeMillis,
    instanceId,
    pinned: true,
    linked: true,
    providers,
    descriptors:
      provider === undefined
        ? undefined
        : listsModel(provider, model)?.capabilities?.optionDescriptors,
    options,
    paceOf: (id) =>
      Option.match(pacer, {
        onNone: () => Effect.undefined,
        onSome: (usage) => usage.paceOf(id).pipe(Effect.orElseSucceed(() => undefined)),
      }),
    // Not offered: whether the project there allows it, or its runtime mode fits, is not known here.
    targetOn: () =>
      Effect.succeed({ cannot: "a task from a linked machine keeps the account it names" }),
  });
  if (outcome.action === "hold") {
    return yield* new OrchestratorMcpFailure({
      code: "usage_pacing_hold",
      message: outcome.message,
    });
  }
  if ("note" in outcome && outcome.note !== undefined) {
    yield* Effect.logInfo("usage pacing, delegated task from a linked machine").pipe(
      Effect.annotateLogs({ note: outcome.note, instanceId }),
    );
  }
  if (outcome.action !== "lowerEffort") return undefined;
  const { optionId, to } = outcome.step;
  const lowered: ModelSelection = {
    ...launch.modelSelection,
    options: [
      ...(options ?? []).filter((option) => option.id !== optionId),
      { id: optionId, value: to },
    ],
  };
  // The policy checked the pick as asked; it checks what runs too. A refused lowered effort keeps
  // the checked one, as a task started here does (delegateChecks.ts).
  const checkPick = yield* ModelPolicyCheck.makePickCheck;
  const refused = yield* checkPick(
    "t3_thread_launch",
    Effect.succeed(
      ModelPolicyCheck.pickOfSelection(providers, lowered, launch.namesModel !== false),
    ),
    { logAs: "model policy paced pick" },
  ).pipe(
    Effect.as(false),
    Effect.catch((refusal) =>
      Effect.logWarning(
        "usagePacingLowerEffort lowered an effort that modelPolicy does not allow: the task keeps its checked effort",
      ).pipe(Effect.annotateLogs({ refusal: refusal.message }), Effect.as(true)),
    ),
  );
  return refused ? undefined : lowered;
});
