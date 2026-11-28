/**
 * Fork (model policy): the warning for a thread whose next turn names no effort while the
 * model policy is on. The thread then runs the driver's default level, which no row may allow.
 * The server checks only picks that agents send; a pick made in the composer never reaches that
 * check, so the composer says it instead.
 */
import {
  modelPolicyCatalogEffortIds,
  modelPolicyPickEfforts,
  type ModelPolicyMode,
  type ModelPolicyPick,
  type ModelSelection,
  type ProjectId,
  type ServerProvider,
} from "@t3tools/contracts";
import { type UnifiedSettings } from "@t3tools/contracts/settings";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import { resolveNewThreadModelSelection } from "@t3tools/shared/usageLimits";

import {
  NO_PROVIDER_MODEL_SELECTION,
  resolveProjectDefaultProviderModelSelection,
} from "../../../providerInstances";

type Selection = Pick<ModelSelection, "instanceId" | "model" | "options">;
type CatalogProvider = Pick<ServerProvider, "instanceId" | "driver" | "models">;

export interface NoEffortWarning {
  /** The model the turn runs, by the name the catalog gives it. */
  readonly modelName: string;
}

/**
 * The selection read as the policy reads it, under the same ids as the server's check
 * (`pickOfSelection` in apps/server/src/fork/modelPolicyCheck.ts): the driver from the instance,
 * the effort control from this machine's catalog, and the model's ultracode default.
 */
function pickOf(provider: CatalogProvider, selection: Selection): ModelPolicyPick {
  const effortOptionIds = modelPolicyCatalogEffortIds([provider.models], selection.model);
  const descriptors = optionDescriptors(provider, selection.model);
  const ultracodeByDefault =
    selection.options?.some((option) => option.id === "ultracode") !== true &&
    descriptors.some(
      (descriptor) => descriptor.id === "ultracode" && descriptor.currentValue === true,
    );
  return {
    driver: provider.driver,
    model: selection.model,
    options: ultracodeByDefault
      ? [...(selection.options ?? []), { id: "ultracode", value: true }]
      : selection.options,
    effortOptionIds,
    driverEffortIds: { [provider.driver]: effortOptionIds },
  };
}

const optionDescriptors = (provider: CatalogProvider, model: string) =>
  provider.models.find((candidate) => candidate.slug === model)?.capabilities?.optionDescriptors ??
  [];

/**
 * Whether the model has an effort control: a select the policy reads as an effort when a value
 * is set under its id. A model without one never warns: there is no effort to pick.
 */
function hasEffortControl(provider: CatalogProvider, selection: Selection): boolean {
  const pick = pickOf(provider, { ...selection, options: [] });
  return optionDescriptors(provider, selection.model).some(
    (descriptor) =>
      descriptor.type === "select" &&
      modelPolicyPickEfforts({ ...pick, options: [{ id: descriptor.id, value: "set" }] }).length >
        0,
  );
}

/**
 * The warning, or `null`: only while the policy's mode is `advise` or `enforce`, for the first
 * selection the next turn sends (one, or each model of a fan-out) whose model has an effort
 * control in the catalog and that names no effort, read with `modelPolicyPickEfforts` as the
 * server reads a pick.
 */
export function noEffortWarning(input: {
  readonly mode: ModelPolicyMode;
  readonly selections: ReadonlyArray<Selection>;
  readonly providers: ReadonlyArray<CatalogProvider>;
}): NoEffortWarning | null {
  if (input.mode === "off") return null;
  for (const selection of input.selections) {
    const provider = input.providers.find(
      (candidate) => candidate.instanceId === selection.instanceId,
    );
    if (provider === undefined || !hasEffortControl(provider, selection)) continue;
    if (modelPolicyPickEfforts(pickOf(provider, selection)).length > 0) continue;
    const modelName =
      provider.models.find((candidate) => candidate.slug === selection.model)?.name ??
      selection.model;
    return { modelName };
  }
  return null;
}

/**
 * What a new draft thread sends while the user picked nothing for it: the project's default
 * model, or the fallback model while the default's usage limit is reached. The same functions,
 * composed as ChatView composes them for the draft (`draftModelResolution`,
 * `localDraftThread.modelSelection` and `activeProjectDefaultModelSelection`).
 */
export function draftThreadModelSelections(input: {
  readonly settings: UnifiedSettings;
  /** The draft's project, once loaded. */
  readonly project:
    | (NonNullable<Parameters<typeof resolveProjectSettings>[2]> & { readonly id: ProjectId })
    | null;
  readonly projectId: ProjectId;
  readonly providers: ReadonlyArray<ServerProvider>;
  readonly now: number;
}): { readonly thread: ModelSelection; readonly project: ModelSelection | null } {
  const { settings, project, projectId, providers } = input;
  const defaultSelection = resolveProjectSettings(
    settings,
    project?.id ?? null,
    project ?? undefined,
  ).settings.defaultModelSelection;
  const resolution = resolveNewThreadModelSelection({
    defaultSelection:
      defaultSelection ??
      resolveProjectDefaultProviderModelSelection(providers, settings, projectId),
    fallbackSelection: settings.fallbackModelSelection,
    providers,
    settings,
    projectId,
    now: input.now,
  });
  const fallback = resolution.fallback ? resolution.selection : null;
  return {
    thread: fallback ?? defaultSelection ?? NO_PROVIDER_MODEL_SELECTION,
    project: fallback ?? defaultSelection,
  };
}
