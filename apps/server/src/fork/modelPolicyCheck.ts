/**
 * Fork: the server's check of an agent's pick against `modelPolicy`, on the MCP routes where an agent
 * picks a model, never on a person's pick. `enforce` refuses a pick outside the rows (or naming no
 * model); `advise` lets it run with a note. A policy that cannot be read refuses, unless its text says
 * `advise`. Each checked pick is logged as "model policy pick", so one model's picks can be counted.
 */
import {
  checkModelPolicyPick,
  type ModelPolicy,
  modelPolicyAliasTargets,
  modelPolicyCatalogEffortIds,
  type ModelPolicyPick,
  modelPolicyPickEfforts,
  modelPolicyRowsText,
  modelPolicyStance,
  type ModelSelection,
  OrchestratorMcpFailure,
  type OrchestratorMcpTarget,
  type ServerProvider,
} from "@t3tools/contracts";
import type { Tool } from "effect/ai";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import { loadCaller } from "../mcp/threadAccess.ts";
import type { ProjectToolkit } from "../mcp/toolkits/project/tools.ts";
import type { ThreadToolkit } from "../mcp/toolkits/thread/tools.ts";
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

/**
 * A pick as the server reads it: `aliasOf` is the model an alias names here. An alias is not read
 * as its model (the rows name full ids, and a provider may move an alias to a new model, which must
 * not widen the policy); the refusal names the full id to use instead.
 */
export interface ServerPick extends ModelPolicyPick {
  readonly aliasOf?: string | undefined;
}

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
  pick: ServerPick,
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
          refuse: `Model policy: the model policy in the settings file cannot be read (a bad modelPolicy value, or a settings file that does not parse), so every agent pick is refused until a person fixes the file or the policy in ${SETTINGS_PLACE}.`,
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
  const why =
    pick.aliasOf === undefined || pick.model === undefined
      ? verdict.why
      : `${verdict.why} (${pick.model} is an alias of ${pick.aliasOf}; an alias is not read as its model, so name the full id)`;
  if (stance.mode === "enforce") {
    return {
      refuse: `Model policy refused this pick: ${why}. ${allowed} Name one of these models in ${FIELDS[route]}.`,
    };
  }
  return {
    note: `Model policy (advise): this pick is outside the policy (${why}); it runs, and enforce would refuse it. ${allowed}`,
    warn: true,
  };
}

/** The instance's model list and every folder's list it scanned. */
const modelLists = (provider: ServerProvider | undefined) =>
  provider === undefined
    ? []
    : [
        provider.models,
        ...(provider.workspaceSnapshots ?? []).flatMap((snapshot) =>
          snapshot.models === undefined ? [] : [snapshot.models],
        ),
      ];

/**
 * The model an alias names in these lists, when it names one model everywhere; `undefined` for a
 * model's own id or a name that is no alias. Only for the refusal's hint: the check reads the name
 * as given.
 */
const aliasTarget = (
  lists: ReadonlyArray<ServerProvider["models"]>,
  model: string,
): string | undefined => {
  const targets = modelPolicyAliasTargets(lists, model);
  return targets.length === 1 ? targets[0]! : undefined;
};

/**
 * The option ids these lists mark as the model's effort control, read as its effort: from every
 * list that holds the model, a folder's own list included (a folder-only model has its control
 * there). The rule is shared with Settings (`modelPolicyCatalogEffortIds`).
 */
const reasoningIds = (lists: ReadonlyArray<ServerProvider["models"]>, model: string) =>
  modelPolicyCatalogEffortIds(lists, model);

/** Whether a pick leaves the ultracode option out while these lists turn it on for the model. */
const ultracodeByDefault = (
  lists: ReadonlyArray<ServerProvider["models"]>,
  model: string,
  options: ModelPolicyPick["options"],
): boolean =>
  options?.some((option) => option.id === "ultracode") !== true &&
  lists.some((list) =>
    list.some(
      (candidate) =>
        candidate.slug === model &&
        candidate.capabilities?.optionDescriptors?.some(
          (descriptor) => descriptor.id === "ultracode" && descriptor.currentValue === true,
        ),
    ),
  );

/**
 * A pick that leaves the ultracode option out runs at the model's default for it (the Claude
 * driver reads the catalog's `currentValue`), so a default that is on reads as ultracode.
 */
const withUltracodeDefault = (
  lists: ReadonlyArray<ServerProvider["models"]>,
  model: string,
  options: ModelSelection["options"],
): ModelSelection["options"] =>
  ultracodeByDefault(lists, model, options)
    ? [...(options ?? []), { id: "ultracode", value: true }]
    : options;

/**
 * A resolved selection as the policy reads it: its driver from the instance, the model as named
 * (an alias is not read as its model), its options with the model's ultracode default. `named:
 * false` (the call named no model, it inherits one) reads as no model.
 */
export function pickOfSelection(
  providers: ReadonlyArray<ServerProvider>,
  selection: Pick<ModelSelection, "instanceId" | "model" | "options">,
  named: boolean,
): ServerPick {
  const provider = providers.find((candidate) => candidate.instanceId === selection.instanceId);
  const lists = modelLists(provider);
  const aliasOf = aliasTarget(lists, selection.model);
  // A row may name the alias itself: its effort control and defaults are still the model's.
  const model = aliasOf ?? selection.model;
  const effortOptionIds = reasoningIds(lists, model);
  return {
    driver: provider?.driver,
    model: named ? selection.model : undefined,
    options: withUltracodeDefault(lists, model, selection.options),
    effortOptionIds,
    // The id this instance reads the effort from, from this machine's own catalog.
    driverEffortIds: provider === undefined ? {} : { [provider.driver]: effortOptionIds },
    ...(aliasOf === undefined ? {} : { aliasOf }),
  };
}

/**
 * A pick sent to a linked machine, its driver unknown (`driverKind` is the agent's claim, not
 * read): every row naming the model must allow it, each under its driver's id, read per driver from
 * this machine's catalog. Every catalog control here counts as a decoy; an ultracode default here
 * counts both ways; an alias gets the full-id hint.
 */
const pickOfRemote = (
  providers: ReadonlyArray<ServerProvider>,
  selection:
    | { readonly model?: string | undefined; readonly options?: ModelPolicyPick["options"] }
    | undefined,
): ServerPick => {
  const model = selection?.model;
  const options = selection?.options;
  if (model === undefined) return { driver: undefined, model, options, effortOptionIds: [] };
  const lists = providers.flatMap(modelLists);
  const aliasOf = aliasTarget(lists, model);
  const listed = aliasOf ?? model;
  const driverEffortIds: Record<string, ReadonlyArray<string>> = {};
  for (const provider of providers) {
    const ids = reasoningIds(modelLists(provider), listed);
    if (ids.length === 0) continue;
    driverEffortIds[provider.driver] = [
      ...new Set([...(driverEffortIds[provider.driver] ?? []), ...ids]),
    ];
  }
  return {
    driver: undefined,
    model,
    options,
    effortOptionIds: reasoningIds(lists, listed),
    driverEffortIds,
    ...(ultracodeByDefault(lists, listed, options) ? { ultracodeMaybeDefault: true } : {}),
    ...(aliasOf === undefined ? {} : { aliasOf }),
  };
};

/** A `delegate_task` target sent to a linked machine ({@link pickOfRemote}). */
export const pickOfRemoteTarget = (
  providers: ReadonlyArray<ServerProvider>,
  target: OrchestratorMcpTarget,
): ServerPick => pickOfRemote(providers, target);

const describePick = (pick: ModelPolicyPick) => ({
  driver: pick.driver ?? "(not known)",
  model: pick.model ?? "(none named)",
  efforts: modelPolicyPickEfforts(pick).join(" + ") || "(none)",
});

export interface PickCheckOptions {
  /**
   * The log line's message, "model policy pick" unless named: a second check of the same call (the
   * target the usage pacer moved it to) logs as "model policy paced pick", so "model policy pick"
   * keeps one line per agent pick as asked, and the paced line says what ran instead.
   */
  readonly logAs?: string;
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
    readPick: Effect.Effect<ServerPick>,
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
      yield* Effect.logWarning(`${options.logAs ?? "model policy pick"} refused`).pipe(
        Effect.annotateLogs({ ...annotations, verdict: "refused", reason: answer.refuse }),
      );
      return yield* new OrchestratorMcpFailure({
        code: "model_unavailable",
        message: answer.refuse,
      });
    }
    yield* (answer.warn === true ? Effect.logWarning : Effect.logInfo)(
      options.logAs ?? "model policy pick",
    ).pipe(
      Effect.annotateLogs({
        ...annotations,
        verdict: answer.warn === true ? "outside or unchecked (advise)" : "inside",
      }),
    );
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
 * A call an agent on a linked machine sent here: upstream's forwarder names the calling thread
 * (`T3-Sender-Thread`) only for a thread's call, read only from a linked client. The sender checked
 * it against its own policy ({@link checkForwarded}); this machine checks it against its own too.
 */
const fromLinkedAgent = Effect.map(
  Effect.serviceOption(McpInvocationContext.McpInvocationContext),
  Option.exists(
    (scope) => scope.client?.linked === true && scope.client.senderThread !== undefined,
  ),
);

/**
 * `t3_thread_launch` once its model is known: an agent's launch (a calling thread here, or one on a
 * linked machine that sent it) is checked; the owner's client is not. A replay is checked again only
 * when it names a model (that model wins and can start a run). Advice goes to the log.
 */
export const checkLaunch = Effect.fn("ModelPolicyCheck.checkLaunch")(function* (
  caller: { readonly id: unknown } | undefined,
  modelSelection: ModelSelection,
  input: { readonly modelSelection?: ModelSelection | undefined },
  replaying: boolean,
) {
  if (replaying && input.modelSelection === undefined) return;
  if (caller === undefined && !(yield* fromLinkedAgent)) return;
  const check = yield* makePickCheck;
  yield* check(
    "t3_thread_launch",
    Effect.map(currentProviders, (providers) =>
      pickOfSelection(providers, modelSelection, input.modelSelection !== undefined),
    ),
  );
});

/**
 * `t3_thread_configure` with a model: an agent's change of a thread's model (a calling thread here,
 * or one on a linked machine that sent it) is checked like a launch (the thread's next run uses it).
 * The owner's client is not.
 */
export const checkConfigure = Effect.fn("ModelPolicyCheck.checkConfigure")(function* (
  modelSelection: ModelSelection,
) {
  const { caller } = yield* loadCaller();
  if (caller === undefined && !(yield* fromLinkedAgent)) return;
  const check = yield* makePickCheck;
  yield* check(
    "t3_thread_configure",
    Effect.map(currentProviders, (providers) => pickOfSelection(providers, modelSelection, true)),
  );
});

/**
 * The tools `checkForwarded` matches by name, typed against upstream's toolkits: a rename there
 * fails the typecheck here instead of dropping the check without a word.
 */
const LAUNCH_TOOL = "t3_thread_launch" satisfies keyof (typeof ProjectToolkit)["tools"];
const CONFIGURE_TOOL = "t3_thread_configure" satisfies keyof (typeof ThreadToolkit)["tools"];
const MODEL_SELECTION = "modelSelection" satisfies keyof Tool.Parameters<
  (typeof ProjectToolkit)["tools"][typeof LAUNCH_TOOL]
> &
  keyof Tool.Parameters<(typeof ThreadToolkit)["tools"][typeof CONFIGURE_TOOL]>;

/**
 * An agent's `t3_thread_launch` or `t3_thread_configure` sent to a linked machine, checked against
 * this machine's policy before it is sent (the machine there checks its own). The driver there is not known ({@link
 * pickOfRemote}); a launch naming no model is refused in enforce; a configure without one only reads.
 */
export const checkForwarded = Effect.fn("ModelPolicyCheck.checkForwarded")(function* (
  tool: string,
  params: object,
) {
  if (tool !== LAUNCH_TOOL && tool !== CONFIGURE_TOOL) return;
  const selection = (params as { readonly [MODEL_SELECTION]?: ModelSelection | undefined })[
    MODEL_SELECTION
  ];
  if (tool === CONFIGURE_TOOL && selection === undefined) return;
  const { caller } = yield* loadCaller();
  if (caller === undefined) return;
  const check = yield* makePickCheck;
  yield* check(
    tool,
    Effect.map(currentProviders, (providers) => pickOfRemote(providers, selection)),
  );
});
