/**
 * Fork: the model policy, which models (and efforts) agents may pick, per provider driver: the
 * setting, the fork's start rows, and the one check of a pick that the server (agents' picks,
 * `apps/server/src/fork/modelPolicyCheck.ts`) and Settings (the "outside policy" mark) share.
 * Wired into the server settings only through forkSettings.ts.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { TrimmedNonEmptyString, TrimmedString } from "./baseSchemas.ts";
import { ProviderDriverKind } from "./providerInstance.ts";

/** `off` changes nothing; `advise` only tells the agent; `enforce` refuses a pick off the list. */
export const ModelPolicyMode = Schema.Literals(["off", "advise", "enforce"]);
export type ModelPolicyMode = typeof ModelPolicyMode.Type;

/** One allowed model. Without `efforts` only the model is checked (it has no effort control). */
export const ModelPolicyRow = Schema.Struct({
  driver: ProviderDriverKind,
  model: TrimmedNonEmptyString,
  efforts: Schema.optionalKey(Schema.NonEmptyArray(TrimmedNonEmptyString)),
  defaultEffort: Schema.optionalKey(TrimmedNonEmptyString),
  /** Shown to agents when they pick a model. */
  useFor: Schema.optionalKey(TrimmedString),
}).check(
  Schema.makeFilter(
    (row) =>
      row.efforts === undefined ||
      row.defaultEffort === undefined ||
      row.efforts.includes(row.defaultEffort),
    { expected: "a defaultEffort that is one of the row's efforts" },
  ),
);
export type ModelPolicyRow = typeof ModelPolicyRow.Type;

const ModelPolicyRules = {
  mode: ModelPolicyMode,
  allow: Schema.Array(ModelPolicyRow),
};

/**
 * The value as the settings file holds it. Both fields default, as the writer saves sparse
 * (`{mode: "enforce", allow: []}` is stored as `{mode: "enforce"}`).
 */
const ModelPolicyStored = Schema.Struct({
  mode: ModelPolicyRules.mode.pipe(Schema.withDecodingDefault(Effect.succeed("off"))),
  allow: ModelPolicyRules.allow.pipe(Schema.withDecodingDefault(Effect.succeed([]))),
});

/**
 * The setting as the server holds it. `unreadable` is set only when the file's value is bad: it is
 * the person's text, kept so a save of another setting writes it back unchanged (the rules then
 * stay {@link MODEL_POLICY_FAIL_CLOSED}). Saving the key itself replaces it.
 */
export const ModelPolicy = Schema.Struct({
  ...ModelPolicyStored.fields,
  unreadable: Schema.optionalKey(Schema.Unknown),
});
export type ModelPolicy = typeof ModelPolicy.Type;

export const DEFAULT_MODEL_POLICY: ModelPolicy = { mode: "off", allow: [] };
/** What a bad value in the settings file reads as: every agent pick refused. */
export const MODEL_POLICY_FAIL_CLOSED: ModelPolicy = { mode: "enforce", allow: [] };

/** A key nobody defined (a typo such as `effort`) is an error, never dropped. */
const REFUSE_UNKNOWN_KEYS = { onExcessProperty: "error" } as const;

const decodeStoredOption = Schema.decodeUnknownOption(ModelPolicyStored);

/**
 * The settings file's value. A bad one (a hand edit, an unknown key included) reads as enforce with
 * nothing allowed, with a warning (fail closed; the rest of the file still loads). The person's text
 * is kept in `unreadable` and written back as it was until they save the key.
 */
export const ModelPolicyFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    ModelPolicy,
    SchemaTransformation.transformEffect({
      decode: Effect.fn("ModelPolicyFromSettingsFile.decode")(function* (raw: unknown) {
        if (decodeStoredOption(raw, REFUSE_UNKNOWN_KEYS)._tag === "Some") {
          return raw as typeof ModelPolicy.Encoded;
        }
        yield* Effect.logWarning(
          "settings: invalid modelPolicy read as enforce with nothing allowed; kept as written until you fix it in settings",
        );
        return { ...MODEL_POLICY_FAIL_CLOSED, unreadable: raw } as typeof ModelPolicy.Encoded;
      }),
      encode: (policy) => Effect.succeed("unreadable" in policy ? policy.unreadable : policy),
    }),
  ),
);

const ModelPolicyPatchRules = Schema.Struct(ModelPolicyRules);
const decodePatchRules = Schema.decodeUnknownEffect(ModelPolicyPatchRules);

/**
 * A save's value, replaced whole: both `mode` and `allow` are required (a save that leaves one out
 * is refused, never filled in with off or no rows), and an unknown key is refused, not dropped.
 */
export const ModelPolicyPatch = Schema.Unknown.pipe(
  Schema.decodeTo(
    ModelPolicyPatchRules,
    SchemaTransformation.transformEffect({
      decode: (raw: unknown) =>
        decodePatchRules(raw, REFUSE_UNKNOWN_KEYS).pipe(
          Effect.mapError((error) => error.issue),
          Effect.as(raw as typeof ModelPolicyPatchRules.Encoded),
        ),
      encode: (policy) => Effect.succeed(policy as unknown),
    }),
  ),
);
export type ModelPolicyPatch = typeof ModelPolicyPatch.Type;

/**
 * The fork's start rows: the rows Settings offers while none are saved, for the owner to
 * change. Not the setting's default: a file without rows keeps meaning no rows, as the writer saves
 * `allow: []` by leaving it out (a default here would widen such a file on update).
 */
export const MODEL_POLICY_START_ROWS: ReadonlyArray<ModelPolicyRow> = [
  {
    driver: ProviderDriverKind.make("claudeAgent"),
    model: "claude-opus-5-5",
    efforts: ["medium", "high", "xhigh"],
    defaultEffort: "high",
    useFor: "most work: building, routine work, review lenses",
  },
  {
    driver: ProviderDriverKind.make("claudeAgent"),
    model: "claude-fable-5-2",
    efforts: ["medium", "high", "xhigh"],
    defaultEffort: "high",
    useFor:
      "creative solutions and direction checks: design options, stuck audits, root causes others missed; it costs more, so not routine work",
  },
];

/**
 * The option ids a model's effort control goes by: the same list as the usage pacing gate
 * (`apps/server/src/usage/UsagePacingGate.ts`; a server test keeps the two in step).
 */
export const MODEL_POLICY_EFFORT_OPTION_IDS: ReadonlyArray<string> = [
  "effort",
  "reasoningEffort",
  "reasoning_effort",
  "reasoning",
  "thinking_level",
];
/**
 * The option id each built-in driver reads its effort from (an effort under another id runs its
 * default): the model guard's `DRIVER_EFFORT_IDS`, kept in step by the guard's test. A driver not
 * listed reads the control its catalog marks ({@link modelPolicyCatalogEffortIds}).
 */
const MODEL_POLICY_DRIVER_EFFORT_IDS: Readonly<Record<string, string>> = {
  claudeAgent: "effort",
  codex: "reasoningEffort",
  fx: "effort",
  grok: "reasoningEffort",
  kiro: "reasoningEffort",
  muse: "reasoningEffort",
  opencode: "variant",
  pi: "thinking",
};
/**
 * OpenCode's "Default" variant choice, which sends no variant (upstream's `DEFAULT_VARIANT_CHOICE_ID`
 * in packages/shared/src/model.ts; a server test keeps the two in step). For an OpenCode pick it is
 * no effort, exactly as if the option were left out.
 */
const MODEL_POLICY_OPENCODE_NO_VARIANT = "t3:default";
/**
 * Per driver, the effort choice that sends no effort (the driver picks): OpenCode's no-variant and
 * Claude's `default` for a model outside its catalog (`ClaudeModelCatalog.ts` `claudeReportedModels`;
 * a server test keeps them in step). A pick reads it as no effort; a new row never offers it.
 */
const MODEL_POLICY_NO_EFFORT_CHOICES: Readonly<Record<string, string>> = {
  claudeAgent: "default",
  opencode: MODEL_POLICY_OPENCODE_NO_VARIANT,
};
/** Whether a driver's effort option holding this value sends no effort ({@link MODEL_POLICY_NO_EFFORT_CHOICES}). */
const sendsNoEffort = (driver: string | undefined, optionId: string, value: unknown): boolean =>
  driver !== undefined &&
  Object.hasOwn(MODEL_POLICY_NO_EFFORT_CHOICES, driver) &&
  optionId === MODEL_POLICY_DRIVER_EFFORT_IDS[driver] &&
  value === MODEL_POLICY_NO_EFFORT_CHOICES[driver];
/** The ultracode option, and the effort name the policy gives it: it runs at xhigh in ultracode. */
const MODEL_POLICY_ULTRACODE = "ultracode";
/** The only driver with the ultracode option (the Claude driver reads the catalog's default for it). */
const MODEL_POLICY_ULTRACODE_DRIVER = "claudeAgent";
/**
 * Options that pick an agent or a mode (own prompt, tools, settings, maybe model): refused while a
 * policy is on, even when described. The model guard's `DRIVER_MODEL_OPTION_IDS` and ACP's
 * `ACP_SESSION_MODE_OPTION_ID`; the model guard's test and a server test keep them in step.
 */
const MODEL_POLICY_AGENT_OPTION_IDS: Readonly<Record<string, ReadonlyArray<string>>> = {
  kiro: ["mode"],
  opencode: ["agent"],
};
export const MODEL_POLICY_SESSION_MODE_OPTION_ID = "_t3/session-mode";

/** One pick of a model, as a call or a setting names it. */
export interface ModelPolicyPick {
  /** The provider driver; `undefined` when it is not known (a task sent to a linked machine). */
  readonly driver?: string | undefined;
  /** The model id; `undefined` when the call names none. */
  readonly model: string | undefined;
  readonly options?: ReadonlyArray<{ readonly id: string; readonly value: unknown }> | undefined;
  /** More option ids read as the effort (a select the model marks as its reasoning control). */
  readonly effortOptionIds?: ReadonlyArray<string> | undefined;
  /**
   * Per driver, the option ids its instances on the checking machine read the model's effort from,
   * as that machine's own catalog marks them ({@link modelPolicyCatalogEffortIds}). Used for a driver
   * with no fixed id; a driver missing here reads none, so its row with efforts refuses.
   */
  readonly driverEffortIds?: Readonly<Record<string, ReadonlyArray<string>>> | undefined;
  /**
   * The pick leaves the ultracode option out, and a catalog here turns it on by default for the
   * model, but the driver that runs it is not known (a pick for a linked machine): it may run either
   * way, so both readings count, the one with ultracode and the one without.
   */
  readonly ultracodeMaybeDefault?: boolean | undefined;
  /**
   * The pick leaves the ultracode option out and runs on a machine whose model list is not read here
   * (a pick for a linked machine): that list may turn ultracode on by default for the model, so a
   * Claude row reads it both ways, with ultracode and without. Naming the option settles it.
   */
  readonly ultracodeDefaultUnknown?: boolean | undefined;
  /**
   * The option ids the checking machine's model lists describe for the model. When set, any other
   * option the policy does not read as an effort is refused: the driver may still apply it (Kiro's
   * `effortLevel`, a hidden `_t3/session-mode`, a `model` option). Unset (Settings): not checked.
   */
  readonly describedOptionIds?: ReadonlyArray<string> | undefined;
  /**
   * The name the call gave, when the check reads it as another id in `model`: Claude's Agent tool
   * names only aliases (`opus`), read as the full id this machine maps it to. A row naming either
   * matches, so a row naming the alias matches it as on the MCP routes, which read a name as given.
   */
  readonly namedAs?: string | undefined;
}

/**
 * The efforts a pick runs at: the value of every option with an effort id (a decoy id must not hide
 * the one the driver reads; without a known driver every driver's id counts), plus `ultracode` while
 * that option is on. `xhigh` with ultracode reads as `ultracode` alone; the old effort value
 * `ultracode` means xhigh with the option on, unless the call gives the option.
 */
export function modelPolicyPickEfforts(pick: ModelPolicyPick): ReadonlyArray<string> {
  const options = pick.options ?? [];
  const effortIds = pickEffortIds(pick, true);
  const named = [
    ...new Set(
      options.flatMap((option) =>
        effortIds.has(option.id) &&
        typeof option.value === "string" &&
        !sendsNoEffort(pick.driver, option.id, option.value)
          ? [option.value]
          : [],
      ),
    ),
  ];
  const ultracodeOption = options.find((option) => option.id === MODEL_POLICY_ULTRACODE);
  const legacy = named.includes(MODEL_POLICY_ULTRACODE);
  const ultracode = ultracodeOption === undefined ? legacy : ultracodeOption.value !== false;
  const efforts = [
    ...new Set(named.map((effort) => (effort === MODEL_POLICY_ULTRACODE ? "xhigh" : effort))),
  ];
  if (!ultracode && ultracodeOption === undefined && pick.ultracodeMaybeDefault === true) {
    // Either reading may run: the row must allow ultracode and every effort named, xhigh included.
    return [MODEL_POLICY_ULTRACODE, ...efforts];
  }
  if (!ultracode) return efforts;
  return [MODEL_POLICY_ULTRACODE, ...efforts.filter((effort) => effort !== "xhigh")];
}

/**
 * The option ids read as a pick's effort. `guess`: without a known driver, every driver's own id
 * counts too (another driver's id may hold a toggle there, such as Claude's `thinking: true`).
 */
function pickEffortIds(pick: ModelPolicyPick, guess: boolean): ReadonlySet<string> {
  const driverIds =
    pick.driver === undefined
      ? guess
        ? Object.values(MODEL_POLICY_DRIVER_EFFORT_IDS)
        : []
      : Object.hasOwn(MODEL_POLICY_DRIVER_EFFORT_IDS, pick.driver)
        ? [MODEL_POLICY_DRIVER_EFFORT_IDS[pick.driver]!]
        : [];
  return new Set([
    ...MODEL_POLICY_EFFORT_OPTION_IDS,
    ...driverIds,
    ...(pick.effortOptionIds ?? []),
    ...Object.values(pick.driverEffortIds ?? {}).flat(),
  ]);
}

/**
 * The option ids a driver reads a model's effort from: its fixed id for a built-in driver, else the
 * effort control the checking machine's catalog marks for the model on that driver's instances.
 * Empty when neither is known: the level it would run cannot be told.
 */
function driverReadIds(pick: ModelPolicyPick, driver: string): ReadonlyArray<string> {
  return Object.hasOwn(MODEL_POLICY_DRIVER_EFFORT_IDS, driver)
    ? [MODEL_POLICY_DRIVER_EFFORT_IDS[driver]!]
    : [
        ...new Set(
          pick.driverEffortIds !== undefined && Object.hasOwn(pick.driverEffortIds, driver)
            ? pick.driverEffortIds[driver]!
            : [],
        ),
      ];
}

/** A model as a provider's model list holds it: its id, the aliases that name it, its options. */
export interface ModelPolicyListedModel {
  readonly slug: string;
  readonly aliases?: ReadonlyArray<string> | undefined;
  readonly capabilities?:
    | {
        readonly optionDescriptors?:
          | ReadonlyArray<{
              readonly id: string;
              readonly type: string;
              readonly role?: string | undefined;
              /** A toggle's value when the pick leaves it out (the ultracode default). */
              readonly currentValue?: unknown;
              /** A select's choices, as Settings offers them for a new row. */
              readonly options?:
                | ReadonlyArray<{ readonly id: string; readonly isDefault?: boolean | undefined }>
                | undefined;
            }>
          | undefined;
      }
    | null
    | undefined;
}

/**
 * The option ids these lists mark as the model's effort control: a select its agent marks
 * `role: "reasoning"` (an ACP `thought_level`), or a select under a general effort id (a Cursor
 * model's `effort`), as the usage pacing gate finds it. From every list that holds the model.
 */
export function modelPolicyCatalogEffortIds(
  lists: ReadonlyArray<ReadonlyArray<ModelPolicyListedModel>>,
  model: string,
): ReadonlyArray<string> {
  return [
    ...new Set(
      lists
        .flatMap((list) => list.filter((candidate) => candidate.slug === model))
        .flatMap((candidate) => candidate.capabilities?.optionDescriptors ?? [])
        .filter(
          (descriptor) =>
            descriptor.role === "reasoning" ||
            (descriptor.type === "select" &&
              MODEL_POLICY_EFFORT_OPTION_IDS.includes(descriptor.id)),
        )
        .map((descriptor) => descriptor.id),
    ),
  ];
}

/**
 * The efforts a new row for a listed model names: the choices of the select its driver reads (else
 * the marked control) and its default, never a no-effort or old `ultracode` choice. With ultracode
 * on by default, a pick leaving it out runs ultracode, so the row allows it as its default.
 */
export function modelPolicyListedEfforts(
  driver: string,
  model: ModelPolicyListedModel,
): { readonly efforts: ReadonlyArray<string>; readonly defaultEffort: string | undefined } {
  const ids = Object.hasOwn(MODEL_POLICY_DRIVER_EFFORT_IDS, driver)
    ? [MODEL_POLICY_DRIVER_EFFORT_IDS[driver]!]
    : modelPolicyCatalogEffortIds([[model]], model.slug);
  const descriptors = model.capabilities?.optionDescriptors ?? [];
  const choices = descriptors
    .filter((descriptor) => descriptor.type === "select" && ids.includes(descriptor.id))
    .flatMap((descriptor) =>
      (descriptor.options ?? []).filter(
        (choice) =>
          choice.id !== MODEL_POLICY_OPENCODE_NO_VARIANT &&
          choice.id !== MODEL_POLICY_ULTRACODE &&
          !sendsNoEffort(driver, descriptor.id, choice.id),
      ),
    );
  const efforts = [...new Set(choices.map((choice) => choice.id))];
  const ultracodeByDefault = descriptors.some(
    (descriptor) => descriptor.id === MODEL_POLICY_ULTRACODE && descriptor.currentValue === true,
  );
  if (ultracodeByDefault && efforts.length > 0) {
    return { efforts: [...efforts, MODEL_POLICY_ULTRACODE], defaultEffort: MODEL_POLICY_ULTRACODE };
  }
  const defaultEffort = choices.find((choice) => choice.isDefault === true)?.id;
  return { efforts, defaultEffort };
}

/**
 * Model names a driver resolves by itself, without its catalog: Claude Code's own aliases
 * (`apps/server/src/claudeModelOptions.ts` `CLAUDE_CODE_MODEL_ALIASES`; a server test keeps the two
 * in step). Settings flags a row naming one even when no catalog lists it as an alias.
 */
export const MODEL_POLICY_DRIVER_ALIASES: Readonly<Record<string, ReadonlyArray<string>>> = {
  claudeAgent: ["opus", "sonnet", "haiku", "fable"],
};

/**
 * The models an alias names in these lists (case does not matter), or none for a model's own id or
 * a name that is no alias. The server reads an alias as given, never as its model; Settings flags a
 * row that names one (a provider may move an alias to a new model, so the row would follow it).
 */
export function modelPolicyAliasTargets(
  lists: ReadonlyArray<ReadonlyArray<ModelPolicyListedModel>>,
  model: string,
): ReadonlyArray<string> {
  if (lists.some((list) => list.some((candidate) => candidate.slug === model))) return [];
  return [
    ...new Set(
      lists.flatMap((list) =>
        list
          .filter((candidate) =>
            candidate.aliases?.some((alias) => alias.toLowerCase() === model.toLowerCase()),
          )
          .map((candidate) => candidate.slug),
      ),
    ),
  ];
}

const rowAllows = (row: ModelPolicyRow, efforts: ReadonlyArray<string>) =>
  row.efforts === undefined
    ? efforts.length === 0
    : efforts.length > 0 && efforts.every((effort) => row.efforts!.includes(effort));

/** What fixes a refused pick, when it is not "name a model in the rows" ({@link ModelPolicyVerdict}). */
export type ModelPolicyFix = "option" | "model-option" | "effort-id" | "agent-option" | "ultracode";

export type ModelPolicyVerdict =
  | { readonly inside: true; readonly row: ModelPolicyRow }
  | {
      readonly inside: false;
      readonly why: string;
      /**
       * What fixes it, when it is not "name a model in the rows": `option`, leave out an option the
       * policy cannot read; `model-option`, pick the model in the model field, not in the options;
       * `effort-id`, the driver's effort id is not known on this machine; `agent-option`, leave out
       * an option that picks an agent or a mode; `ultracode`, name the ultracode option.
       */
      readonly fix?: ModelPolicyFix;
    };

/**
 * The first option of a pick that the policy cannot read: not described for the model by the
 * checking machine's lists, and not an id it reads as an effort (or ultracode). `undefined` when
 * the pick has no such option, or when no lists were given ({@link ModelPolicyPick.describedOptionIds}).
 */
function unreadableOption(pick: ModelPolicyPick): string | undefined {
  if (pick.describedOptionIds === undefined) return undefined;
  const agentOption = agentPickingOption(pick);
  if (agentOption !== undefined) return agentOption;
  const readable = new Set([
    ...pick.describedOptionIds,
    ...pickEffortIds(pick, true),
    MODEL_POLICY_ULTRACODE,
  ]);
  return pick.options?.find((option) => !readable.has(option.id))?.id;
}

/**
 * The first option of a pick that picks an agent or a mode ({@link MODEL_POLICY_AGENT_OPTION_IDS}):
 * its driver's own, or every driver's when the driver is not known (a pick for a linked machine).
 */
function agentPickingOption(pick: ModelPolicyPick): string | undefined {
  const ids = new Set([
    MODEL_POLICY_SESSION_MODE_OPTION_ID,
    ...(pick.driver === undefined
      ? Object.values(MODEL_POLICY_AGENT_OPTION_IDS).flat()
      : Object.hasOwn(MODEL_POLICY_AGENT_OPTION_IDS, pick.driver)
        ? MODEL_POLICY_AGENT_OPTION_IDS[pick.driver]!
        : []),
  ]);
  return pick.options?.find((option) => ids.has(option.id))?.id;
}

/**
 * Whether one pick is inside the rows: a named model in a row; for a row with efforts, one of them
 * under the id its driver reads (an id not known here refuses), and none for a row without. With no
 * driver, every row naming the model must allow it. An effort option with no effort name refuses,
 * as does an option the policy cannot read ({@link ModelPolicyPick.describedOptionIds}).
 */
export function checkModelPolicyPick(
  allow: ReadonlyArray<ModelPolicyRow>,
  pick: ModelPolicyPick,
): ModelPolicyVerdict {
  if (pick.model === undefined) {
    return { inside: false, why: "the call names no model" };
  }
  const model = pick.model;
  // `{effort: true}`: the driver runs its own default effort, which the pick does not show.
  const unnamed = pick.options?.find(
    (option) => pickEffortIds(pick, false).has(option.id) && typeof option.value !== "string",
  );
  if (unnamed !== undefined) {
    return { inside: false, why: `${model}'s effort option ${unnamed.id} holds no effort name` };
  }
  const rows = allow.filter(
    (row) =>
      (row.model === model || (pick.namedAs !== undefined && row.model === pick.namedAs)) &&
      (pick.driver === undefined || row.driver === pick.driver),
  );
  if (rows.length === 0) {
    return {
      inside: false,
      why: `${model}${pick.driver === undefined ? "" : ` (${pick.driver})`} is in no row`,
    };
  }
  const efforts = modelPolicyPickEfforts(pick);
  const options = pick.options ?? [];
  // The machine that runs it may turn ultracode on by default: a Claude row reads it both ways.
  const ultracodeUnknown =
    pick.driver === undefined &&
    pick.ultracodeDefaultUnknown === true &&
    !options.some((option) => option.id === MODEL_POLICY_ULTRACODE);
  const bothWays = ultracodeUnknown
    ? modelPolicyPickEfforts({ ...pick, ultracodeMaybeDefault: true })
    : efforts;
  /** Why one row refuses the pick, or `undefined` when it allows it. */
  const refuses = (
    row: ModelPolicyRow,
  ): { readonly why: string; readonly fix?: "effort-id" | "ultracode" } | undefined => {
    if (!rowAllows(row, efforts)) {
      return {
        why:
          efforts.length === 0
            ? `${model} needs an effort from its row, and the pick names none`
            : `${model} does not allow effort ${efforts.join(" + ")}`,
      };
    }
    if (row.driver === MODEL_POLICY_ULTRACODE_DRIVER && !rowAllows(row, bothWays)) {
      return {
        why: `the pick leaves the option ultracode out, and the machine that runs it may turn ultracode on by default for ${model} (its model list is not read here), which its ${row.driver} row does not allow`,
        fix: "ultracode",
      };
    }
    if (row.efforts === undefined) return undefined;
    // The effort must be under the id the row's driver reads: under another, it runs its default.
    const own = driverReadIds(pick, row.driver);
    if (own.length === 0) {
      return {
        why: `the option id ${row.driver} reads ${model}'s effort from is not known here (no ${row.driver} model list here marks an effort control for ${model}), so the level it would run cannot be told`,
        fix: "effort-id",
      };
    }
    const missing = own.find(
      (id) => !options.some((option) => option.id === id && typeof option.value === "string"),
    );
    if (missing === undefined) return undefined;
    // Several ids: the model lists of several instances of the driver here name the control
    // differently, and which one runs the pick is not known, so each must be named.
    const several =
      own.length > 1
        ? `; ${row.driver} reads ${model}'s effort from each of ${own.join(", ")} here (its instances' model lists name the control differently), and which instance runs the pick is not known, so the pick must name every one (an instance whose list does not describe an id refuses it in turn: pick a model whose driver has a fixed effort id, or start the task where one instance runs it)`
        : "";
    return {
      why: `${model} names no effort under ${missing}, the option ${row.driver} reads it from (left out, the driver runs its default level)${several}`,
    };
  };
  const verdicts = rows.map((row) => ({ row, refusal: refuses(row) }));
  const allowed = verdicts.filter((verdict) => verdict.refusal === undefined);
  const row = allowed[0]?.row;
  if (row !== undefined && (pick.driver !== undefined || allowed.length === rows.length)) {
    // Inside by the rows; an option the policy cannot read may still be applied by the driver,
    // after the checked ones (a second effort id, a hidden session mode, a `model` option).
    const unreadable = unreadableOption(pick);
    if (unreadable === undefined) return { inside: true, row };
    if (unreadable === agentPickingOption(pick)) {
      return {
        inside: false,
        why: `the option ${unreadable} picks an agent or a mode, which brings its own prompt, tools and settings (an agent can carry its own model) that the policy cannot see`,
        fix: "agent-option",
      };
    }
    return unreadable === "model"
      ? {
          inside: false,
          why: `the option model (beside ${model}) would change the model after this check`,
          fix: "model-option",
        }
      : {
          inside: false,
          why: `the option ${unreadable} is not described for ${model} in this machine's model list and is not an effort id the policy reads, so the driver could apply it unchecked${
            pick.driver === undefined
              ? " (the pick runs on a linked machine: a control only the agent there has is refused here too, as this machine cannot tell whether it sets the effort; start that agent on this machine once so its model list describes it)"
              : ""
          }`,
          fix: "option",
        };
  }
  const refusing = verdicts.filter((verdict) => verdict.refusal !== undefined);
  const { why, fix } = refusing[0]!.refusal!;
  const withFix = fix === undefined ? {} : { fix };
  if (row === undefined) return { inside: false, why, ...withFix };
  // Some rows allow it: the driver there is not known, and these rows refuse it.
  return {
    inside: false,
    why: `${why} (its ${refusing.map((verdict) => verdict.row.driver).join(", ")} row refuses it: the driver there is not known here, so every row naming the model must allow it)`,
    ...withFix,
  };
}

/** One row as the refusal lists it. */
function modelPolicyRowText(row: ModelPolicyRow): string {
  const efforts =
    row.efforts === undefined
      ? "no effort"
      : `efforts ${row.efforts.join(", ")}${
          row.defaultEffort === undefined ? "" : `; default ${row.defaultEffort}`
        }`;
  return `${row.driver} ${row.model} (${efforts})`;
}

/** The allowed rows as one line, for a refusal or a note. */
export function modelPolicyRowsText(allow: ReadonlyArray<ModelPolicyRow>): string {
  return allow.length === 0
    ? "none: the policy has no rows, so every agent pick is outside it"
    : allow.map(modelPolicyRowText).join("; ");
}

/**
 * How the policy reads for a check: its mode, or, for a value the settings file holds that cannot
 * be read, `unreadable`. That one is Enforce (every agent pick refused), unless the file's own text
 * names the mode `advise`: then it is only a warning.
 */
export type ModelPolicyStance =
  | { readonly mode: "off" }
  | {
      readonly mode: "advise" | "enforce";
      readonly allow: ReadonlyArray<ModelPolicyRow>;
      readonly unreadable: false;
    }
  | { readonly mode: "advise" | "enforce"; readonly unreadable: true };

export function modelPolicyStance(policy: ModelPolicy): ModelPolicyStance {
  if ("unreadable" in policy) {
    const raw = policy.unreadable;
    const fileMode =
      typeof raw === "object" && raw !== null && !Array.isArray(raw)
        ? (raw as Record<string, unknown>).mode
        : undefined;
    return { mode: fileMode === "advise" ? "advise" : "enforce", unreadable: true };
  }
  if (policy.mode === "off") return { mode: "off" };
  return { mode: policy.mode, allow: policy.allow, unreadable: false };
}
