/**
 * Fork: the server's check of an agent's pick against `modelPolicy`, on the MCP routes where an agent
 * picks a model, never on a person's pick. `enforce` refuses a pick outside the rows (or naming no
 * model); `advise` lets it run with a note. A policy that cannot be read refuses, unless its text says
 * `advise`. Each checked pick is logged as "model policy pick", so one model's picks can be counted.
 */
import {
  checkModelPolicyPick,
  type ModelPolicy,
  type ModelPolicyPick,
  modelPolicyPickEfforts,
  modelPolicyRowsText,
  modelPolicyStance,
  type ModelSelection,
  OrchestratorMcpFailure,
  type OrchestratorMcpTarget,
  type ServerProvider,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import { loadCaller } from "../mcp/threadAccess.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ServerSettings from "../serverSettings.ts";

export type PolicyRoute =
  | "delegate_task"
  | "create_threads"
  | "t3_thread_launch"
  | "t3_thread_configure";

/** Where the call names its model, for the refusal's hint. */
const FIELDS: Record<PolicyRoute, string> = {
  delegate_task: "target.model, with one of its efforts in target.options",
  create_threads: "target.model, with one of its efforts in target.options",
  t3_thread_launch: "modelSelection.model, with one of its efforts in modelSelection.options",
  t3_thread_configure: "modelSelection.model, with one of its efforts in modelSelection.options",
};

const SETTINGS_PLACE = "Settings → Fork → Model policy";

/** What the check answers: refuse with a text, or let the pick run (with a note for the agent). */
export type PolicyAnswer =
  | { readonly refuse: string }
  | { readonly note?: string; readonly warn?: boolean };

/**
 * The answer for one pick, from the policy as the settings hold it (`undefined`: the settings could
 * not be read). Pure: the same policy and pick give the same answer on every call.
 */
export function answerPick(
  policy: ModelPolicy | undefined,
  route: PolicyRoute,
  pick: ModelPolicyPick,
): PolicyAnswer {
  if (policy === undefined) {
    return {
      refuse:
        "Model policy: the server settings cannot be read, so this pick cannot be checked and is refused. Try again; if it stays, a person checks the settings file.",
    };
  }
  const stance = modelPolicyStance(policy);
  if (stance.mode === "off") return {};
  if (stance.unreadable) {
    return stance.mode === "enforce"
      ? {
          refuse: `Model policy: the model policy in the settings file cannot be read, so every agent pick is refused until a person fixes it in ${SETTINGS_PLACE}.`,
        }
      : {
          note: `Model policy (advise): the model policy in the settings file cannot be read, so this pick was not checked; it runs. A person fixes it in ${SETTINGS_PLACE}.`,
          warn: true,
        };
  }
  const verdict = checkModelPolicyPick(stance.allow, pick);
  if (verdict.inside) {
    const useFor = verdict.row.useFor;
    return useFor === undefined || useFor === ""
      ? {}
      : { note: `Model policy: ${verdict.row.model} is for ${useFor}.` };
  }
  const allowed = `Allowed: ${modelPolicyRowsText(stance.allow)}.`;
  if (stance.mode === "enforce") {
    return {
      refuse: `Model policy refused this pick: ${verdict.why}. ${allowed} Name one of these models in ${FIELDS[route]}.`,
    };
  }
  return {
    note: `Model policy (advise): this pick is outside the policy (${verdict.why}); it runs, and enforce would refuse it. ${allowed}`,
    warn: true,
  };
}

/** A model by its id or an alias, in the instance's list or in any folder's list it scanned. */
const canonicalModel = (provider: ServerProvider | undefined, model: string): string => {
  if (provider === undefined) return model;
  const lists = [
    provider.models,
    ...(provider.workspaceSnapshots ?? []).flatMap((snapshot) =>
      snapshot.models === undefined ? [] : [snapshot.models],
    ),
  ];
  if (lists.some((list) => list.some((candidate) => candidate.slug === model))) return model;
  const slugs = new Set(
    lists.flatMap((list) =>
      list
        .filter((candidate) =>
          candidate.aliases?.some((alias) => alias.toLowerCase() === model.toLowerCase()),
        )
        .map((candidate) => candidate.slug),
    ),
  );
  // An alias that means one model everywhere is that model; anything else stays as given (and so
  // is in no row unless a row names it as it is).
  return slugs.size === 1 ? [...slugs][0]! : model;
};

/** The option ids a model marks as its reasoning control, read as its effort. */
const reasoningIds = (provider: ServerProvider | undefined, model: string) =>
  (
    provider?.models.find((candidate) => candidate.slug === model)?.capabilities
      ?.optionDescriptors ?? []
  )
    .filter((descriptor) => "role" in descriptor && descriptor.role === "reasoning")
    .map((descriptor) => descriptor.id);

/**
 * A resolved selection as the policy reads it: its driver from the instance, the model's id (an
 * alias read as its model), its options. `named: false` (the call named no model, it inherits one)
 * reads as no model.
 */
export function pickOfSelection(
  providers: ReadonlyArray<ServerProvider>,
  selection: Pick<ModelSelection, "instanceId" | "model" | "options">,
  named: boolean,
): ModelPolicyPick {
  const provider = providers.find((candidate) => candidate.instanceId === selection.instanceId);
  const model = canonicalModel(provider, selection.model);
  return {
    driver: provider?.driver,
    model: named ? model : undefined,
    options: selection.options,
    effortOptionIds: reasoningIds(provider, model),
  };
}

/** A target sent to a linked machine: no driver known here unless the call names it. */
export const pickOfRemoteTarget = (target: OrchestratorMcpTarget): ModelPolicyPick => ({
  driver: target.driverKind,
  model: target.model,
  options: target.options,
});

const describePick = (pick: ModelPolicyPick) => ({
  driver: pick.driver ?? "(not known)",
  model: pick.model ?? "(none named)",
  efforts: modelPolicyPickEfforts(pick).join(" + ") || "(none)",
});

export interface PickCheckOptions {
  /**
   * Not logged: a second check of the same call (the target the usage pacer moved it to), so the
   * log keeps one line per agent pick.
   */
  readonly quiet?: boolean;
}

/**
 * The check of one pick, on the server settings of the service that builds it: it reads the
 * settings on every call (a changed policy counts from the next call), logs the pick while a policy
 * is on, and fails with the refusal or returns the note for the agent.
 */
export const makePickCheck = Effect.gen(function* () {
  // A host built without settings (upstream's handler tests) has no policy: nothing is checked.
  // The server always wires them; there a settings read that fails refuses.
  const settings = yield* Effect.serviceOption(ServerSettings.ServerSettingsService);
  return Effect.fn("ModelPolicyCheck.checkPick")(function* (
    route: PolicyRoute,
    /** The pick, read only while a policy is on (it may read the providers). */
    readPick: Effect.Effect<ModelPolicyPick>,
    options: PickCheckOptions = {},
  ) {
    if (Option.isNone(settings)) return undefined;
    const policy = Option.getOrUndefined(
      yield* settings.value.getSettings.pipe(
        Effect.map((value) => value.modelPolicy),
        Effect.option,
      ),
    );
    if (policy !== undefined && modelPolicyStance(policy).mode === "off") return undefined;
    const pick = yield* readPick;
    const answer = answerPick(policy, route, pick);
    const annotations = {
      route,
      ...describePick(pick),
      mode: policy === undefined ? "(settings unreadable)" : modelPolicyStance(policy).mode,
    };
    if ("refuse" in answer) {
      yield* Effect.logWarning("model policy pick refused").pipe(
        Effect.annotateLogs({ ...annotations, verdict: "refused", reason: answer.refuse }),
      );
      return yield* new OrchestratorMcpFailure({
        code: "model_unavailable",
        message: answer.refuse,
      });
    }
    if (options.quiet !== true) {
      yield* (answer.warn === true ? Effect.logWarning : Effect.logInfo)("model policy pick").pipe(
        Effect.annotateLogs({
          ...annotations,
          verdict: answer.warn === true ? "outside or unchecked (advise)" : "inside",
        }),
      );
    }
    return answer.note;
  });
});

/**
 * The registry's providers, or none in a host built without it (a pick then has no driver, so
 * every row naming its model must allow it).
 */
const currentProviders = Effect.serviceOption(ProviderRegistry.ProviderRegistry).pipe(
  Effect.flatMap(
    Option.match({
      onNone: () => Effect.succeed([] as ReadonlyArray<ServerProvider>),
      onSome: (registry) => registry.getProviders,
    }),
  ),
);

/**
 * `t3_thread_launch`, once the launch's model is known: an agent's launch (a calling thread) is
 * checked; the owner's client and a task from a linked machine have no calling thread and are not.
 * A replay of an accepted launch starts nothing new and is not checked. Advice goes to the log only
 * (the launch's reply has no place for a note).
 */
export const checkLaunch = Effect.fn("ModelPolicyCheck.checkLaunch")(function* (
  caller: { readonly id: unknown } | undefined,
  modelSelection: ModelSelection,
  input: { readonly modelSelection?: ModelSelection | undefined },
  replaying: boolean,
) {
  if (replaying || caller === undefined) return;
  const check = yield* makePickCheck;
  yield* check(
    "t3_thread_launch",
    Effect.map(currentProviders, (providers) =>
      pickOfSelection(providers, modelSelection, input.modelSelection !== undefined),
    ),
  );
});

/**
 * `t3_thread_configure` with a model: an agent's change of a thread's model is checked like a
 * launch (the thread's next run uses it). A caller with no thread (the owner's client) is not.
 */
export const checkConfigure = Effect.fn("ModelPolicyCheck.checkConfigure")(function* (
  modelSelection: ModelSelection,
) {
  const { caller } = yield* loadCaller();
  if (caller === undefined) return;
  const check = yield* makePickCheck;
  yield* check(
    "t3_thread_configure",
    Effect.map(currentProviders, (providers) => pickOfSelection(providers, modelSelection, true)),
  );
});
