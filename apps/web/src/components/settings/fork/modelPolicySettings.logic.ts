/** Fork: the pure parts of the model policy panel (`modelPolicy`, server only). */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
import {
  checkModelPolicyPick,
  MODEL_POLICY_DRIVER_ALIASES,
  MODEL_POLICY_START_ROWS,
  type ModelPolicy,
  modelPolicyAliasTargets,
  modelPolicyCatalogEffortIds,
  type ModelPolicyListedModel,
  modelPolicyListedEfforts,
  type ModelPolicyMode,
  ModelPolicyPatch,
  type ModelPolicyRow,
  modelPolicyPickEfforts,
  modelPolicyStance,
  type ModelSelection,
  isProviderDriverKind,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";

const decodePatch = Schema.decodeUnknownExit(ModelPolicyPatch);

export const MODEL_POLICY_MODE_OPTIONS: ReadonlyArray<{
  readonly value: ModelPolicyMode;
  readonly label: string;
  readonly description: string;
}> = [
  {
    value: "off",
    label: "Off",
    description: "Off: agents pick any model, as in upstream T3 Code.",
  },
  {
    value: "advise",
    label: "Advise",
    description: "Advise: an agent is told when its pick is not on the list; the pick still runs.",
  },
  {
    value: "enforce",
    label: "Enforce",
    description: "Enforce: a pick not on the list, or one that names no model, is refused.",
  },
];

/** The settings file's own text when it could not be read (the rules then read as enforce, nothing allowed). */
export function unreadableModelPolicyText(policy: ModelPolicy): string | null {
  return "unreadable" in policy
    ? (JSON.stringify(policy.unreadable) ?? String(policy.unreadable))
    : null;
}

/** The allowed rows as the editor shows them: JSON, as in the settings file. */
export const modelPolicyAllowText = (policy: ModelPolicy) => JSON.stringify(policy.allow, null, 2);

/** The unreadable value's own fields, when it is a JSON object. */
function unreadableFields(policy: ModelPolicy): Readonly<Record<string, unknown>> | null {
  if (!("unreadable" in policy)) return null;
  const raw = policy.unreadable;
  return typeof raw === "object" && raw !== null && !Array.isArray(raw)
    ? (raw as Record<string, unknown>)
    : null;
}

/**
 * The text the rows editor starts from. For an unreadable value, its own rows when it has a list
 * of them (a bare list counts too), so the person fixes the typo instead of retyping every row;
 * the fail-closed `[]` would throw the text away without saying so.
 */
export function modelPolicyEditorText(policy: ModelPolicy): string {
  if (modelPolicyStartsFromStartRows(policy))
    return JSON.stringify(MODEL_POLICY_START_ROWS, null, 2);
  if (!("unreadable" in policy)) return modelPolicyAllowText(policy);
  const raw = Array.isArray(policy.unreadable)
    ? policy.unreadable
    : unreadableFields(policy)?.allow;
  return JSON.stringify(Array.isArray(raw) ? raw : [], null, 2);
}

/**
 * The editor offers the fork's start rows while a readable policy has no rows saved: the
 * owner saves them as they are or changes them first. Nothing is saved until they do.
 */
export function modelPolicyStartsFromStartRows(policy: ModelPolicy): boolean {
  return !("unreadable" in policy) && policy.allow.length === 0;
}

/** The mode an unreadable value names, when it names one of the three; never the fail-closed one. */
export function unreadableModelPolicyMode(policy: ModelPolicy): ModelPolicyMode | null {
  const mode = unreadableFields(policy)?.mode;
  return MODEL_POLICY_MODE_OPTIONS.find((option) => option.value === mode)?.value ?? null;
}

/**
 * The drivers the rows name that no provider on the selected environments has, in the rows' order
 * (a typo such as `claudAgent`: with Enforce, every pick of that provider is refused). Empty when
 * no provider is known, so a disconnected environment flags nothing.
 */
export function unknownModelPolicyDrivers(
  rows: ReadonlyArray<ModelPolicyRow>,
  knownDrivers: ReadonlySet<string>,
): ReadonlyArray<string> {
  if (knownDrivers.size === 0) return [];
  return [...new Set(rows.map((row) => row.driver))].filter((driver) => !knownDrivers.has(driver));
}

/**
 * A provider as the alias check reads it: its driver, the model lists it has, and the machine it is
 * on (`environmentId`; providers without one read as one machine).
 */
export interface ModelPolicyAliasProvider {
  readonly driver: string;
  readonly environmentId?: string | undefined;
  readonly models?: ReadonlyArray<ModelPolicyListedModel> | undefined;
  readonly workspaceSnapshots?:
    | ReadonlyArray<{ readonly models?: ReadonlyArray<ModelPolicyListedModel> | undefined }>
    | undefined;
}

/** The instance's model list and every folder's list it scanned. */
const providerLists = (provider: ModelPolicyAliasProvider) => [
  provider.models ?? [],
  ...(provider.workspaceSnapshots ?? []).flatMap((snapshot) =>
    snapshot.models === undefined ? [] : [snapshot.models],
  ),
];

/** The providers per machine (`environmentId`; providers without one read as one machine). */
const byMachine = (providers: ReadonlyArray<ModelPolicyAliasProvider>) => {
  const machines = new Map<string | undefined, Array<ModelPolicyAliasProvider>>();
  for (const provider of providers) {
    machines.set(provider.environmentId, [
      ...(machines.get(provider.environmentId) ?? []),
      provider,
    ]);
  }
  return machines;
};

/**
 * Rows naming an alias on some selected machine, with its models there (none for a name the driver
 * resolves by itself): such a row follows the alias to a new model and misses the full id. Each
 * machine is read on its own lists, as its server is. Empty with no provider of the driver known.
 */
export function aliasModelPolicyRows(
  rows: ReadonlyArray<ModelPolicyRow>,
  providers: ReadonlyArray<ModelPolicyAliasProvider>,
): ReadonlyArray<{ readonly row: ModelPolicyRow; readonly aliasOf: ReadonlyArray<string> }> {
  const machines = byMachine(providers);
  return rows.flatMap((row) => {
    let flagged = false;
    const aliasOf = new Set<string>();
    for (const machineProviders of machines.values()) {
      const own = machineProviders.filter((provider) => provider.driver === row.driver);
      if (own.length === 0) continue;
      const lists = own.flatMap(providerLists);
      if (lists.some((list) => list.some((candidate) => candidate.slug === row.model))) continue;
      const targets = modelPolicyAliasTargets(lists, row.model);
      // A name the driver resolves by itself, once a model list of the driver is known there.
      const driverOwn =
        own.some((provider) => provider.models !== undefined) &&
        (MODEL_POLICY_DRIVER_ALIASES[row.driver] ?? []).some(
          (alias) => alias.toLowerCase() === row.model.toLowerCase(),
        );
      if (targets.length === 0 && !driverOwn) continue;
      flagged = true;
      for (const target of targets) aliasOf.add(target);
    }
    return flagged ? [{ row, aliasOf: [...aliasOf] }] : [];
  });
}

/** A machine's lists of a driver hold the model by id or alias (or the driver resolves the name). */
const machineHoldsModel = (
  own: ReadonlyArray<ModelPolicyAliasProvider>,
  lists: ReadonlyArray<ReadonlyArray<ModelPolicyListedModel>>,
  row: ModelPolicyRow,
) =>
  lists.some((list) => list.some((candidate) => candidate.slug === row.model)) ||
  modelPolicyAliasTargets(lists, row.model).length > 0 ||
  (own.some((provider) => provider.models !== undefined) &&
    (MODEL_POLICY_DRIVER_ALIASES[row.driver] ?? []).some(
      (alias) => alias.toLowerCase() === row.model.toLowerCase(),
    ));

/**
 * Rows whose model no list of their driver holds on some selected machine, by id or alias: a
 * retired or misspelled model, so the row may allow nothing there. Each machine is read on its own
 * lists; a machine with no model of the driver listed yet (or none at all) says nothing, and an
 * alias row is left to {@link aliasModelPolicyRows}.
 */
export function staleModelPolicyRows(
  rows: ReadonlyArray<ModelPolicyRow>,
  providers: ReadonlyArray<ModelPolicyAliasProvider>,
): ReadonlyArray<ModelPolicyRow> {
  const machines = byMachine(providers);
  return rows.filter((row) =>
    [...machines.values()].some((machineProviders) => {
      const own = machineProviders.filter((provider) => provider.driver === row.driver);
      const lists = own.flatMap(providerLists);
      if (!lists.some((list) => list.length > 0)) return false;
      return !machineHoldsModel(own, lists, row);
    }),
  );
}

/** A provider as the add-a-model list reads it: its lists' models may carry a display name. */
export interface ModelPolicyListProvider extends ModelPolicyAliasProvider {
  readonly models?: ReadonlyArray<ModelPolicyListedModel & { readonly name?: string }> | undefined;
  readonly workspaceSnapshots?:
    | ReadonlyArray<{
        readonly models?:
          | ReadonlyArray<ModelPolicyListedModel & { readonly name?: string }>
          | undefined;
      }>
    | undefined;
}

/** A model the selected machines list, as the row Settings adds for it. */
export interface ModelPolicyListedRow {
  readonly key: string;
  readonly label: string;
  readonly row: ModelPolicyRow;
}

/**
 * The models the selected machines list that no row names yet, each as the row Settings adds for
 * it: its driver and id, and the efforts its own control offers ({@link modelPolicyListedEfforts}).
 * A model listed on several machines or folders is offered once, as its first list holds it.
 */
export function modelPolicyListedRows(
  existing: ReadonlyArray<{ readonly driver?: unknown; readonly model?: unknown }>,
  providers: ReadonlyArray<ModelPolicyListProvider>,
): ReadonlyArray<ModelPolicyListedRow> {
  const keyOf = (driver: unknown, model: unknown) => JSON.stringify([driver, model]);
  const seen = new Set(existing.map((row) => keyOf(row.driver, row.model)));
  const listed: Array<ModelPolicyListedRow> = [];
  for (const provider of providers) {
    const driver = provider.driver;
    // A driver that is no valid id could not be saved in a row.
    if (!isProviderDriverKind(driver)) continue;
    const models = [
      ...(provider.models ?? []),
      ...(provider.workspaceSnapshots ?? []).flatMap((snapshot) => snapshot.models ?? []),
    ];
    for (const model of models) {
      const key = keyOf(driver, model.slug);
      if (seen.has(key)) continue;
      seen.add(key);
      const { efforts, defaultEffort } = modelPolicyListedEfforts(driver, model);
      const [first, ...rest] = efforts;
      const row: ModelPolicyRow =
        first === undefined
          ? { driver, model: model.slug }
          : {
              driver,
              model: model.slug,
              efforts: [first, ...rest],
              ...(defaultEffort === undefined ? {} : { defaultEffort }),
            };
      const name = model.name !== undefined && model.name !== model.slug ? model.name : undefined;
      listed.push({
        key,
        label: `${driver}: ${name === undefined ? model.slug : `${name} (${model.slug})`}`,
        row,
      });
    }
  }
  return listed;
}

/** The editor's text as a list, or `undefined` when it is not one (empty text is no rows). */
const editorList = (text: string): ReadonlyArray<unknown> | undefined => {
  try {
    const parsed: unknown = JSON.parse(text.trim() === "" ? "[]" : text);
    return Array.isArray(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
};

/** The editor's rows read loosely (the objects in its list), none for text that is not a list. */
export function modelPolicyTextRows(
  text: string,
): ReadonlyArray<{ readonly driver?: unknown; readonly model?: unknown }> {
  return (editorList(text) ?? []).filter(
    (row): row is { readonly driver?: unknown; readonly model?: unknown } =>
      typeof row === "object" && row !== null && !Array.isArray(row),
  );
}

/** The editor's text with the row added at the end, or why it cannot be added (the text is kept). */
export function withAddedModelPolicyRow(
  text: string,
  row: ModelPolicyRow,
): { readonly text: string } | { readonly error: string } {
  const list = editorList(text);
  if (list === undefined) {
    return { error: "Fix the rows' JSON first; a listed model is added to a valid list." };
  }
  return { text: JSON.stringify([...list, row], null, 2) };
}

/** One allowed row as one line. */
export function modelPolicyRowLine(row: ModelPolicyRow): string {
  const efforts =
    row.efforts === undefined
      ? ""
      : ` (efforts ${row.efforts.join(", ")}${
          row.defaultEffort === undefined ? "" : `; default ${row.defaultEffort}`
        })`;
  return `${row.model}${efforts}${row.useFor ? `: ${row.useFor}` : ""}`;
}

/**
 * The whole value a save sends, with `mode` and the edited rows, or why it cannot be saved. Checked
 * as the server's save checks it: strict, an unknown key refused.
 */
export function withModelPolicyAllow(
  mode: ModelPolicyMode,
  text: string,
): { readonly value: ModelPolicyPatch } | { readonly error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text.trim() === "" ? "[]" : text);
  } catch {
    return { error: "The rows are not valid JSON." };
  }
  if (!Array.isArray(parsed)) return { error: "The rows are a JSON list, [ ... ]." };
  const exit = decodePatch({ mode, allow: parsed });
  if (exit._tag === "Success") return { value: exit.value };
  return {
    error:
      "The rows cannot be saved: each needs driver and model; efforts is a non-empty list, defaultEffort one of them, useFor a short note. Any other key is refused.",
  };
}

/** The person's own model picks in the server settings that Settings checks against the rows. */
const PERSON_MODEL_PICKS = [
  { key: "defaultModelSelection", label: "Default model for new threads" },
  { key: "fallbackModelSelection", label: "Fallback model" },
  { key: "textGenerationModelSelection", label: "Text generation model" },
  { key: "sourceControlWriterModelSelection", label: "Commit and pull request writer" },
  { key: "reviewGuideModelSelection", label: "Review guide model" },
  { key: "speechPostProcessingModelSelection", label: "Transcript cleanup model" },
] as const;

export type PersonModelPickKey = (typeof PERSON_MODEL_PICKS)[number]["key"];

export interface PersonModelPickMark {
  readonly key: PersonModelPickKey;
  readonly label: string;
  /** The pick as one line: model and effort. */
  readonly pick: string;
  /** Why it is outside the rows; `null` when it is inside. */
  readonly outside: string | null;
}

/**
 * The person's own picks, marked inside or outside the rows (a mark only; never refused). Empty
 * while the policy is off or unreadable. Only `environmentId`'s instances are read (a provider
 * without one counts); a pick on an instance it lacks reads without a driver, so every row naming
 * its model must allow it.
 */
export function personModelPickMarks(
  policy: ModelPolicy,
  picks: Partial<Record<PersonModelPickKey, ModelSelection | null | undefined>>,
  providers: ReadonlyArray<
    ModelPolicyAliasProvider & {
      readonly instanceId: string;
      readonly environmentId?: string | undefined;
    }
  >,
  environmentId?: string,
): ReadonlyArray<PersonModelPickMark> {
  const stance = modelPolicyStance(policy);
  if (stance.mode === "off" || stance.unreadable) return [];
  return PERSON_MODEL_PICKS.flatMap(({ key, label }) => {
    const selection = picks[key];
    if (selection === null || selection === undefined) return [];
    // The machine whose settings hold the picks runs them: its instance, not another machine's
    // instance with the same id (the default `claudeAgent` is on every machine).
    const provider = providers.find(
      (candidate) =>
        candidate.instanceId === selection.instanceId &&
        (environmentId === undefined ||
          candidate.environmentId === undefined ||
          candidate.environmentId === environmentId),
    );
    // The effort control the instance's catalog marks, as the server reads an agent's pick.
    const effortOptionIds =
      provider === undefined
        ? []
        : modelPolicyCatalogEffortIds(providerLists(provider), selection.model);
    const pick = {
      driver: provider?.driver,
      model: selection.model,
      options: selection.options,
      effortOptionIds,
      driverEffortIds: provider === undefined ? {} : { [provider.driver]: effortOptionIds },
    };
    const efforts = modelPolicyPickEfforts(pick);
    const verdict = checkModelPolicyPick(stance.allow, pick);
    return [
      {
        key,
        label,
        pick: `${selection.model}${efforts.length === 0 ? "" : `, effort ${efforts.join(" + ")}`}`,
        outside: verdict.inside ? null : verdict.why,
      },
    ];
  });
}
