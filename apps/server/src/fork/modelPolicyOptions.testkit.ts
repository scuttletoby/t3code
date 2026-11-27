/**
 * Fork: options the model policy cannot read, which a driver still applies after the checked ones
 * (a second effort id, a hidden ACP session mode, a `model` option), for each agent route's tests.
 * Catalogs as the drivers build them: Kiro and Grok describe `reasoningEffort`, ACP its thought level.
 */
import {
  type ModelPolicyRow,
  ProviderDriverKind,
  ProviderInstanceId,
  type ServerProvider,
} from "@t3tools/contracts";

const KIRO_MODEL = "kiro-model";
const GROK_MODEL = "grok-model";
const ACP_OPTION_MODEL = "acp-option-model";

const select = (id: string, role?: "reasoning") => ({
  id,
  label: id,
  type: "select" as const,
  options: ["low", "high", "max"].map((value) => ({ id: value, label: value })),
  ...(role === undefined ? {} : { role }),
});

const provider = (
  driver: string,
  model: string,
  descriptors: ReadonlyArray<ReturnType<typeof select>>,
): ServerProvider =>
  ({
    instanceId: ProviderInstanceId.make(driver),
    driver: ProviderDriverKind.make(driver),
    enabled: true,
    installed: true,
    version: "test",
    status: "ready",
    auth: { status: "authenticated" },
    checkedAt: "2026-11-21T12:00:00.000Z",
    models: [
      {
        slug: model,
        name: model,
        isCustom: false,
        capabilities: { optionDescriptors: [...descriptors] },
      },
    ],
    slashCommands: [],
    skills: [],
  }) as unknown as ServerProvider;

/** This machine's providers for the cases. */
export const OPTION_CASE_PROVIDERS: ReadonlyArray<ServerProvider> = [
  provider("kiro", KIRO_MODEL, [select("reasoningEffort")]),
  provider("grok", GROK_MODEL, [select("reasoningEffort")]),
  provider("acpRegistry", ACP_OPTION_MODEL, [select("thought_level", "reasoning")]),
];

/** Each model at effort high only. */
export const OPTION_CASE_ROWS: ReadonlyArray<ModelPolicyRow> = [
  { driver: ProviderDriverKind.make("kiro"), model: KIRO_MODEL, efforts: ["high"] },
  { driver: ProviderDriverKind.make("grok"), model: GROK_MODEL, efforts: ["high"] },
  {
    driver: ProviderDriverKind.make("acpRegistry"),
    model: ACP_OPTION_MODEL,
    efforts: ["high"],
  },
];

export interface OptionCase {
  readonly name: string;
  readonly instanceId: ProviderInstanceId;
  readonly model: string;
  /** The allowed effort under the id the driver reads. */
  readonly allowed: { readonly id: string; readonly value: string };
  /** The option the policy cannot read, which the driver would still apply. */
  readonly extra: { readonly id: string; readonly value: string };
  /** A text the refusal holds. */
  readonly why: string;
  /** The fix the refusal gives (its field names follow the route). */
  readonly fix: string;
  /**
   * Upstream's own refusal of the option on a t3_thread_launch that runs on this machine: upstream
   * checks a launch's options against the model list first, in every policy mode,
   * so the policy is not asked there. A launch sent to a linked machine is still checked here.
   */
  readonly launchHere: string;
}

const OPTION_FIX = "this option is not allowed while the model policy is on";
const MODEL_FIX = "Pick the model in the model field";
/** An option that picks an agent or a mode: refused even where the model list describes it. */
const AGENT_WHY = (id: string) =>
  `the option ${id} picks an agent or a mode, which brings its own prompt, tools and settings (an agent can carry its own model) that the policy cannot see`;
const AGENT_FIX =
  "an option that picks an agent or a mode is not allowed while the model policy is on";
const unreadable = (id: string, model: string) =>
  `the option ${id} is not described for ${model} in this machine's model list and is not an effort id the policy reads`;

export const OPTION_CASES: ReadonlyArray<OptionCase> = [
  {
    name: "Kiro: a second effort id (effortLevel) written after reasoningEffort",
    instanceId: ProviderInstanceId.make("kiro"),
    model: KIRO_MODEL,
    allowed: { id: "reasoningEffort", value: "high" },
    extra: { id: "effortLevel", value: "max" },
    why: unreadable("effortLevel", KIRO_MODEL),
    fix: OPTION_FIX,
    launchHere: "Unknown option effortLevel; supported options: reasoningEffort.",
  },
  {
    name: "ACP: the session mode that mirrors the thought levels (hidden from the catalog)",
    instanceId: ProviderInstanceId.make("acpRegistry"),
    model: ACP_OPTION_MODEL,
    allowed: { id: "thought_level", value: "high" },
    extra: { id: "_t3/session-mode", value: "max" },
    why: AGENT_WHY("_t3/session-mode"),
    fix: AGENT_FIX,
    launchHere: "Unknown option _t3/session-mode; supported options: thought_level.",
  },
  {
    name: "Grok: the agent's native thought_level beside reasoningEffort",
    instanceId: ProviderInstanceId.make("grok"),
    model: GROK_MODEL,
    allowed: { id: "reasoningEffort", value: "high" },
    extra: { id: "thought_level", value: "max" },
    why: unreadable("thought_level", GROK_MODEL),
    fix: OPTION_FIX,
    launchHere: "Unknown option thought_level; supported options: reasoningEffort.",
  },
  {
    name: "Kiro: a model option applied after the allowed model",
    instanceId: ProviderInstanceId.make("kiro"),
    model: KIRO_MODEL,
    allowed: { id: "reasoningEffort", value: "high" },
    extra: { id: "model", value: "model-in-no-row" },
    why: `the option model (beside ${KIRO_MODEL}) would change the model after this check`,
    fix: MODEL_FIX,
    launchHere: "Unknown option model; supported options: reasoningEffort.",
  },
  {
    name: "ACP: a model option applied after the allowed model",
    instanceId: ProviderInstanceId.make("acpRegistry"),
    model: ACP_OPTION_MODEL,
    allowed: { id: "thought_level", value: "high" },
    extra: { id: "model", value: "model-in-no-row" },
    why: `the option model (beside ${ACP_OPTION_MODEL}) would change the model after this check`,
    fix: MODEL_FIX,
    launchHere: "Unknown option model; supported options: thought_level.",
  },
];
