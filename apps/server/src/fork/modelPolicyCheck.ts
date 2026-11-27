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
  type ModelPolicyFix,
  modelPolicyCatalogEffortIds,
  type ModelPolicyPick,
  modelPolicyPickEfforts,
  type ModelPolicyRow,
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
  | "t3_thread_configure"
  /** Claude's own Agent tool (claudeAgentModelPolicy.ts). */
  | "Agent"
  /** Claude's Skill tool, for a skill that runs in a sub-agent or names a model (same file). */
  | "Skill"
  /**
   * Any other thread command an agent's MCP call dispatches with a model, or a fork onto its
   * source's model (the backstop, modelPolicyBackstop.ts).
   */
  | "agent command";

/** Where the call names its model, for the refusal's hint. */
const FIELDS: Record<PolicyRoute, string> = {
  delegate_task: "target.model, with one of its efforts in target.options",
  create_threads: "target.model, with one of its efforts in target.options",
  t3_thread_launch: "modelSelection.model, with one of its efforts in modelSelection.options",
  t3_thread_configure: "modelSelection.model, with one of its efforts in modelSelection.options",
  Agent: "the Agent call's model, with one of its efforts in effort",
  Skill:
    "the skill's frontmatter: model, with one of its efforts in effort (or use a skill that runs inline and names neither)",
  "agent command":
    "the call's model (a thread fork runs on its source thread's model: start the thread with t3_thread_launch or delegate_task and a model from the rows instead)",
};

/** Where the call names its model alone, and its options. */
const MODEL_FIELD: Record<PolicyRoute, string> = {
  delegate_task: "target.model",
  create_threads: "target.model",
  t3_thread_launch: "modelSelection.model",
  t3_thread_configure: "modelSelection.model",
  Agent: "model",
  Skill: "the skill's frontmatter model",
  "agent command": "the call's model",
};
const OPTIONS_FIELD: Record<PolicyRoute, string> = {
  delegate_task: "target.options",
  create_threads: "target.options",
  t3_thread_launch: "modelSelection.options",
  t3_thread_configure: "modelSelection.options",
  Agent: "the call",
  Skill: "the skill's frontmatter",
  "agent command": "the call's options",
};

/** What the agent does to fix a refused pick, per the verdict's `fix`. */
const fixText = (fix: ModelPolicyFix | undefined, route: PolicyRoute): string => {
  switch (fix) {
    case "model-option":
      return `Pick the model in the model field (${MODEL_FIELD[route]}) and leave the model option out of ${OPTIONS_FIELD[route]}: this option is not allowed while the model policy is on.`;
    case "option":
      return `Leave that option out of ${OPTIONS_FIELD[route]}: this option is not allowed while the model policy is on. Pick the model in ${MODEL_FIELD[route]} and its effort under the id its driver reads.`;
    case "effort-id":
      return "Start that agent on this machine once so its model list shows the effort control, or pick a model whose driver has a fixed effort id.";
    case "agent-option":
      return `Leave that option out of ${OPTIONS_FIELD[route]}: an option that picks an agent or a mode is not allowed while the model policy is on. Pick the model in ${MODEL_FIELD[route]} and its effort under the id its driver reads.`;
    case "ultracode":
      return `Name the option ultracode in ${OPTIONS_FIELD[route]}: { "id": "ultracode", "value": false } runs the effort named, true runs ultracode (when its row allows it).`;
    case undefined:
      return `Name one of these models in ${FIELDS[route]}.`;
  }
};

const SETTINGS_PLACE = "Settings → Fork → Model policy";

/**
 * A pick as the server reads it: `aliasOf` is the model an alias names here. An alias is not read
 * as its model (the rows name full ids, and a provider may move an alias to a new model, which must
 * not widen the policy); the refusal names the full id to use instead.
 */
export interface ServerPick extends ModelPolicyPick {
  readonly aliasOf?: string | undefined;
  /**
   * What passes on this route, in place of the rows (Claude's Agent tool names only aliases, so it
   * says which alias and effort pass, `claudeAgentModelPolicy.ts`).
   */
  readonly allowedText?: ((allow: ReadonlyArray<ModelPolicyRow>) => string) | undefined;
  /**
   * Why the pick cannot be read at all (a skill whose file T3 cannot read): `enforce` refuses it,
   * `advise` lets it run with a note.
   */
  readonly unreadable?: string | undefined;
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
  if (pick.unreadable !== undefined) {
    return stance.mode === "enforce"
      ? {
          refuse: `Model policy refused this call (fail closed): ${pick.unreadable}. Allowed: ${modelPolicyRowsText(stance.allow)}.`,
        }
      : {
          note: `Model policy (advise): ${pick.unreadable}, so this call was not checked; it runs, and enforce would refuse it.`,
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
  const allowed =
    pick.allowedText?.(stance.allow) ?? `Allowed: ${modelPolicyRowsText(stance.allow)}.`;
  const why =
    pick.aliasOf === undefined || pick.model === undefined
      ? verdict.why
      : `${verdict.why} (${pick.model} is an alias of ${pick.aliasOf}; an alias is not read as its model, so name the full id)`;
  if (stance.mode === "enforce") {
    return {
      refuse: `Model policy refused this pick: ${why}. ${allowed} ${fixText(verdict.fix, route)}`,
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
 * Every option id these lists describe for the model: what the policy lets a pick name beside the
 * effort ids it reads (`describedOptionIds`). A control the catalog hides (an ACP session mode that
 * repeats the thought levels, an agent's `model` option) is not described, so it is refused.
 */
const describedIds = (lists: ReadonlyArray<ServerProvider["models"]>, model: string) => [
  ...new Set(
    lists
      .flatMap((list) => list.filter((candidate) => candidate.slug === model))
      .flatMap((candidate) => candidate.capabilities?.optionDescriptors ?? [])
      .map((descriptor) => descriptor.id),
  ),
];

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
    // Only what this instance's lists describe for the model (none for an instance not listed).
    describedOptionIds: describedIds(lists, model),
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
    // What any list here describes for the model: the driver there is not known.
    describedOptionIds: describedIds(lists, listed),
    ...(ultracodeByDefault(lists, listed, options) ? { ultracodeMaybeDefault: true } : {}),
    // The machine there reads its own list for the ultracode default: a Claude row reads both ways.
    ultracodeDefaultUnknown: true,
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
  /**
   * False for the backstop's own check: it reads one command of the call, so it must not mark the
   * call as checked by its route (the call's next command is read too).
   */
  readonly marksCall?: boolean;
}

/**
 * The MCP calls whose route checked its own picks (by the request's scope, a new object for each
 * request): the backstop (modelPolicyBackstop.ts) leaves their commands alone, so a pick is
 * checked, and logged, once.
 */
const routeCheckedCalls = new WeakSet<McpInvocationContext.McpInvocationScope>();

/** Whether this call's route checked its picks itself. */
export const routeCheckedCall = (scope: McpInvocationContext.McpInvocationScope): boolean =>
  routeCheckedCalls.has(scope);

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
    /**
     * The pick, read only while a policy is on (it may read the providers); `undefined`: the call
     * picks no model (a skill that runs inline and names none), so nothing is checked or logged.
     */
    readPick: Effect.Effect<ServerPick | undefined>,
    options: PickCheckOptions = {},
  ) {
    if (options.marksCall !== false) {
      const scope = yield* Effect.serviceOption(McpInvocationContext.McpInvocationContext);
      if (Option.isSome(scope)) routeCheckedCalls.add(scope.value);
    }
    if (Option.isNone(settings)) return undefined;
    const policy = Option.getOrUndefined(
      yield* settings.value.getSettings.pipe(
        Effect.map((value) => value.modelPolicy),
        Effect.option,
      ),
    );
    if (policy !== undefined && modelPolicyStance(policy).mode === "off") return undefined;
    const pick = yield* readPick;
    if (pick === undefined) return undefined;
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
export const currentProviders = Effect.serviceOption(ProviderRegistry.ProviderRegistry).pipe(
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
