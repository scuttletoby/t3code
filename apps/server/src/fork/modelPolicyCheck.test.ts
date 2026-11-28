// @effect-diagnostics nodeBuiltinImport:off - reads the usage pacing gate's source to keep a list in step.
/**
 * Fork: the server's check of an agent's model pick (`modelPolicyCheck.ts`): the answer for each
 * policy state, how a selection reads as a pick, and `t3_thread_configure` through upstream's real
 * handler. `delegateOwnOptions.test.ts` covers delegate_task, create_threads and the remote target
 * through the service; `launchMcpProfile.test.ts` covers t3_thread_launch through its handler.
 */
import * as NodeFS from "node:fs";

import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  DEFAULT_SERVER_SETTINGS,
  EnvironmentId,
  MODEL_POLICY_DRIVER_ALIASES,
  MODEL_POLICY_EFFORT_OPTION_IDS,
  MODEL_POLICY_SESSION_MODE_OPTION_ID,
  MODEL_POLICY_START_ROWS,
  modelPolicyPickEfforts,
  type ModelPolicy,
  type ModelPolicyPick,
  type ModelPolicyRow,
  type ModelSelection,
  type OrchestrationV2ThreadShell,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  type ServerProvider,
  OrchestratorMcpFailure,
  ThreadId,
} from "@t3tools/contracts";
import { ACP_SESSION_MODE_OPTION_ID } from "@t3tools/provider-acp/server/sessionConfig";
import { DEFAULT_VARIANT_CHOICE_ID } from "@t3tools/shared/model";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Stream from "effect/Stream";

import * as ServerConfig from "../config.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import * as ThreadHandlers from "../mcp/toolkits/thread/handlers.ts";
import { ThreadToolkit } from "../mcp/toolkits/thread/tools.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ThreadSearch from "../orchestration-v2/ThreadSearch.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import {
  BUNDLED_CLAUDE_MODEL_CATALOG,
  claudeReportedModels,
  withUltracodeToggle,
} from "../provider/ClaudeModelCatalog.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ScheduledTasks from "../scheduledTasks/ScheduledTaskService.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as ModelPolicyCheck from "./modelPolicyCheck.ts";
import {
  OPTION_CASE_PROVIDERS,
  OPTION_CASE_ROWS,
  OPTION_CASES,
  type OptionCase,
} from "./modelPolicyOptions.testkit.ts";

const OPUS = "claude-opus-5-5";
const FABLE = "claude-fable-5-2";
const ROWS = MODEL_POLICY_START_ROWS;
/** An ACP model whose reasoning control has an id of its own (no fixed effort id). */
const ACP_MODEL = "acp-model";
const ACP_REASONING_ID = "thought_level";
const ACP_PROVIDER = {
  instanceId: ProviderInstanceId.make("acp"),
  driver: ProviderDriverKind.make("acpRegistry"),
  models: [
    {
      slug: ACP_MODEL,
      name: "ACP model",
      isCustom: false,
      capabilities: {
        optionDescriptors: [
          {
            id: ACP_REASONING_ID,
            label: "Thought level",
            type: "select",
            role: "reasoning",
            options: [],
          },
        ],
      },
    },
  ],
} as unknown as ServerProvider;
const policy = (mode: "off" | "advise" | "enforce", allow = ROWS): ModelPolicy => ({
  mode,
  allow,
});
const claude = (model: string | undefined, effort?: string | undefined): ModelPolicyPick => ({
  driver: "claudeAgent",
  model,
  options: effort === undefined ? [] : [{ id: "effort", value: effort }],
});
const answer = (value: ModelPolicy | undefined, pick: ModelPolicyPick) =>
  ModelPolicyCheck.answerPick(value, "delegate_task", pick);
const refusal = (value: ModelPolicy | undefined, pick: ModelPolicyPick) => {
  const result = answer(value, pick);
  return "refuse" in result ? result.refuse : undefined;
};

describe("the answer for a pick, per policy state", () => {
  it("off (the default, or a file without the key): every pick runs, nothing is said", () => {
    for (const pick of [claude(OPUS, "high"), claude("gpt-5.4"), claude(undefined)]) {
      assert.deepStrictEqual(answer(policy("off"), pick), {});
      assert.deepStrictEqual(answer(policy("off", []), pick), {});
    }
  });

  it("enforce: an allowed pick runs with its row's note; others are refused with the rows", () => {
    const fable = answer(policy("enforce"), claude(FABLE, "xhigh"));
    assert.deepStrictEqual(Object.keys(fable), ["note"]);
    assert.include("note" in fable ? fable.note : "", `${FABLE} is for creative solutions`);
    assert.include(
      "note" in answer(policy("enforce"), claude(OPUS, "medium"))
        ? (answer(policy("enforce"), claude(OPUS, "medium")) as { note: string }).note
        : "",
      "most work",
    );
    const cases: ReadonlyArray<readonly [ModelPolicyPick, string]> = [
      // a call that names no model.
      [claude(undefined, "high"), "the call names no model"],
      // a model in no row, also a new or renamed one.
      [claude("claude-opus-5-6", "high"), "claude-opus-5-6 (claudeAgent) is in no row"],
      [claude("claude-haiku-5-5", "high"), "claude-haiku-5-5 (claudeAgent) is in no row"],
      // The right model on another driver is in no row of that driver.
      [{ ...claude(OPUS, "high"), driver: "codex" }, `${OPUS} (codex) is in no row`],
      // Efforts outside the row, none named, ultracode the row does not list.
      [claude(OPUS, "low"), `${OPUS} does not allow effort low`],
      [claude(FABLE, "max"), `${FABLE} does not allow effort max`],
      [claude(OPUS), `${OPUS} needs an effort from its row, and the pick names none`],
      [claude(OPUS, "ultracode"), `${OPUS} does not allow effort ultracode`],
    ];
    for (const [pick, why] of cases) {
      const text = refusal(policy("enforce"), pick);
      assert.isDefined(text, why);
      assert.include(text, `Model policy refused this pick: ${why}.`);
      assert.include(
        text,
        `Allowed: claudeAgent ${OPUS} (efforts medium, high, xhigh; default high); claudeAgent ${FABLE} (efforts medium, high, xhigh; default high).`,
      );
      assert.include(text, "Name one of these models in target.model");
    }
  });

  it("advise: every pick runs; one outside the rows gets the note enforce would refuse", () => {
    assert.deepStrictEqual(Object.keys(answer(policy("advise"), claude(OPUS, "high"))), ["note"]);
    for (const pick of [claude(undefined), claude("gpt-5.4", "high"), claude(OPUS, "max")]) {
      const result = answer(policy("advise"), pick);
      assert.isFalse("refuse" in result);
      assert.isTrue("warn" in result && result.warn === true);
      assert.include("note" in result ? result.note : "", "Model policy (advise): this pick is");
      assert.include("note" in result ? result.note : "", "enforce would refuse it");
    }
  });

  it("empty state: enforce with no rows refuses every pick and says the policy has none", () => {
    const text = refusal(policy("enforce", []), claude(OPUS, "high"));
    assert.include(text, "is in no row");
    assert.include(text, "Allowed: none: the policy has no rows");
  });

  it("a row without efforts allows only a pick that names none; with no driver, all rows count", () => {
    const plain = [
      { driver: ProviderDriverKind.make("exampleDriver"), model: "plain-1" },
      ...ROWS,
    ] as const;
    assert.isUndefined(
      refusal(policy("enforce", plain), { driver: "exampleDriver", model: "plain-1" }),
    );
    assert.include(
      refusal(policy("enforce", plain), {
        driver: "exampleDriver",
        model: "plain-1",
        options: [{ id: "reasoningEffort", value: "high" }],
      }),
      "does not allow effort high",
    );
    // A pick sent to a linked machine has no driver here: every row naming its model must allow
    // it, so a second row of the same model on another driver with other efforts refuses.
    const twoDrivers: ReadonlyArray<ModelPolicyRow> = [
      ...ROWS,
      { driver: ProviderDriverKind.make("other"), model: OPUS, efforts: ["low"] },
    ];
    assert.isUndefined(
      refusal(policy("enforce"), { model: OPUS, options: [{ id: "effort", value: "high" }] }),
    );
    assert.include(
      refusal(policy("enforce", twoDrivers), {
        model: OPUS,
        options: [{ id: "effort", value: "high" }],
      }),
      "does not allow effort high",
    );
  });

  it("corrupt state: a policy the file holds but cannot be read refuses, unless it says advise", () => {
    // As `ModelPolicyFromSettingsFile` reads a bad value: enforce, nothing allowed, the text kept.
    const unreadable = (raw: unknown): ModelPolicy => ({
      mode: "enforce",
      allow: [],
      unreadable: raw,
    });
    for (const raw of [
      { mode: "enforce", allow: "oops" },
      { mode: "off", allow: [{ model: OPUS }] },
      { mode: "Advise", allow: [] },
      "advise",
      ["advise"],
      null,
    ]) {
      const text = refusal(unreadable(raw), claude(OPUS, "high"));
      assert.include(
        text,
        "the model policy in the settings file cannot be read (",
        JSON.stringify(raw),
      );
      assert.include(text, "so every agent pick is refused", JSON.stringify(raw));
      assert.include(text, "Settings → Fork → Model policy");
    }
    const advise = answer(unreadable({ mode: "advise", allow: [{ model: OPUS }] }), claude("x"));
    assert.isFalse("refuse" in advise);
    assert.isTrue("warn" in advise && advise.warn === true);
    assert.include(
      "note" in advise ? advise.note : "",
      "cannot be read, so this pick was not checked; it runs",
    );
  });

  it("missing state: settings that cannot be read at all refuse every pick (fail closed)", () => {
    assert.include(
      refusal(undefined, claude(OPUS, "high")),
      "the server settings cannot be read, so this pick cannot be checked and is refused",
    );
  });

  it("runs again: the same policy and pick give the same answer", () => {
    for (const pick of [claude(OPUS, "high"), claude("gpt-5.4", "high"), claude(undefined)]) {
      for (const value of [policy("advise"), policy("enforce"), undefined]) {
        assert.deepStrictEqual(answer(value, pick), answer(value, pick));
      }
    }
  });

  it("each route names the field its call picks the model in", () => {
    const pick = claude("gpt-5.4", "high");
    const text = (route: ModelPolicyCheck.PolicyRoute) => {
      const result = ModelPolicyCheck.answerPick(policy("enforce"), route, pick);
      return "refuse" in result ? result.refuse : "";
    };
    assert.include(text("create_threads"), "target.model");
    assert.include(text("t3_thread_launch"), "modelSelection.model");
    assert.include(text("t3_thread_configure"), "modelSelection.model");
  });
});

describe("a selection as the policy reads it", () => {
  const INSTANCE = ProviderInstanceId.make("claudeAgent");
  const provider = {
    instanceId: INSTANCE,
    driver: ProviderDriverKind.make("claudeAgent"),
    models: [
      {
        slug: OPUS,
        name: "Opus",
        isCustom: false,
        aliases: ["opus"],
        capabilities: {
          optionDescriptors: [
            { id: "level", label: "Level", type: "select", role: "reasoning", options: [] },
          ],
        },
      },
    ],
  } as unknown as ServerProvider;

  it("reads the driver from the instance and a reasoning select", () => {
    const options = [
      { id: "effort", value: "high" },
      { id: "level", value: "high" },
    ];
    const pick = ModelPolicyCheck.pickOfSelection(
      [provider],
      { instanceId: INSTANCE, model: OPUS, options },
      true,
    );
    assert.deepStrictEqual(pick, {
      driver: "claudeAgent",
      model: OPUS,
      options,
      effortOptionIds: ["level"],
      driverEffortIds: { claudeAgent: ["level"] },
      describedOptionIds: ["level"],
    });
    assert.isUndefined(refusal(policy("enforce"), pick));
    // The select alone: Claude reads its effort under `effort` only.
    const alone = ModelPolicyCheck.pickOfSelection(
      [provider],
      { instanceId: INSTANCE, model: OPUS, options: [{ id: "level", value: "high" }] },
      true,
    );
    assert.include(refusal(policy("enforce"), alone), `${OPUS} names no effort under effort`);
  });

  it("an alias is read as given, not as its model: refused with the full id to name", () => {
    // Local and remote agree: an alias is not widened to its model (a provider may move an alias
    // to a new model). A remote pick looks it up in this machine's lists too, for the hint only.
    for (const alias of ["Opus", "opus"]) {
      const pick = ModelPolicyCheck.pickOfSelection(
        [provider],
        { instanceId: INSTANCE, model: alias, options: [{ id: "level", value: "high" }] },
        true,
      );
      assert.strictEqual(pick.model, alias);
      const text = refusal(policy("enforce"), pick);
      assert.include(text, `${alias} is an alias of ${OPUS}`);
      assert.include(text, "name the full id");
      // Its effort control is still the model's, for a row that names the alias itself.
      assert.deepStrictEqual(pick.effortOptionIds, ["level"]);
      const remote = ModelPolicyCheck.pickOfRemoteTarget([provider], {
        model: alias,
        options: [{ id: "effort", value: "high" }],
      } as never);
      assert.strictEqual(remote.model, alias);
      const remoteText = refusal(policy("enforce"), remote);
      assert.include(remoteText, `${alias} is in no row`);
      // The same full-id hint as a pick here.
      assert.include(remoteText, `(${alias} is an alias of ${OPUS}; an alias is not read`);
      assert.include(remoteText, "name the full id");
    }
  });

  it("a folder-only model's reasoning control is read from the folder's list", () => {
    const folderOnly = {
      ...provider,
      workspaceSnapshots: [
        {
          cwd: "/projects/a",
          checkedAt: "2026-11-21T00:00:00.000Z",
          slashCommands: [],
          skills: [],
          models: [
            {
              slug: FABLE,
              name: "Fable",
              isCustom: false,
              capabilities: {
                optionDescriptors: [
                  { id: "depth", label: "Depth", type: "select", role: "reasoning", options: [] },
                ],
              },
            },
          ],
        },
      ],
    } as unknown as ServerProvider;
    const pick = ModelPolicyCheck.pickOfSelection(
      [folderOnly],
      {
        instanceId: INSTANCE,
        model: FABLE,
        options: [
          { id: "effort", value: "high" },
          { id: "depth", value: "max" },
        ],
      },
      true,
    );
    assert.deepStrictEqual(pick.effortOptionIds, ["depth"]);
    assert.include(refusal(policy("enforce"), pick), "does not allow effort high + max");
  });

  it("a pick that leaves ultracode out reads the catalog's default for it: on is ultracode", () => {
    // The catalog as the Claude driver builds it from a manifest whose effort default is ultracode.
    const ultracodeByDefault = {
      ...provider,
      models: [
        {
          slug: OPUS,
          name: "Opus",
          isCustom: false,
          capabilities: withUltracodeToggle({
            optionDescriptors: [
              {
                id: "effort",
                label: "Effort",
                type: "select",
                currentValue: "ultracode",
                options: ["medium", "high", "xhigh", "ultracode"].map((id) => ({ id, label: id })),
              },
            ],
          }),
        },
      ],
    } as unknown as ServerProvider;
    const read = (providers: ReadonlyArray<ServerProvider>, ultracode?: boolean) =>
      ModelPolicyCheck.pickOfSelection(
        providers,
        {
          instanceId: INSTANCE,
          model: OPUS,
          options: [
            { id: "effort", value: "high" },
            ...(ultracode === undefined ? [] : [{ id: "ultracode", value: ultracode }]),
          ],
        },
        true,
      );
    assert.include(
      refusal(policy("enforce"), read([ultracodeByDefault])),
      "does not allow effort ultracode + high",
    );
    // The pick turns it off: its own value stands, inside. A catalog without the default: inside.
    assert.deepStrictEqual(read([ultracodeByDefault], false).options, [
      { id: "effort", value: "high" },
      { id: "ultracode", value: false },
    ]);
    assert.isUndefined(refusal(policy("enforce"), read([ultracodeByDefault], false)));
    assert.isUndefined(refusal(policy("enforce"), read([provider])));
  });

  it("a call that named no model reads as none (#128), whatever it inherits", () => {
    const pick = ModelPolicyCheck.pickOfSelection(
      [provider],
      { instanceId: INSTANCE, model: OPUS, options: [{ id: "effort", value: "high" }] },
      false,
    );
    assert.isUndefined(pick.model);
    assert.include(refusal(policy("enforce"), pick), "the call names no model");
  });

  it("an instance not listed here gives no driver", () => {
    const pick = ModelPolicyCheck.pickOfSelection(
      [],
      { instanceId: ProviderInstanceId.make("gone"), model: OPUS },
      true,
    );
    assert.isUndefined(pick.driver);
  });

  it("a driver's own effort id is read for that driver (its sync with the model guard is the guard's own test)", () => {
    assert.include(MODEL_POLICY_EFFORT_OPTION_IDS, "effort");
    assert.include(MODEL_POLICY_EFFORT_OPTION_IDS, "reasoningEffort");
    assert.deepStrictEqual(
      modelPolicyPickEfforts({
        driver: "opencode",
        model: "m",
        options: [{ id: "variant", value: "low" }],
      }),
      ["low"],
    );
  });

  it("a remote pick's driverKind is the agent's claim: not read, every row and effort id counts", () => {
    const rows: ReadonlyArray<ModelPolicyRow> = [
      { driver: ProviderDriverKind.make("claudeAgent"), model: "shared", efforts: ["high"] },
      { driver: ProviderDriverKind.make("codex"), model: "shared", efforts: ["high", "max"] },
    ];
    // Claims codex (whose row allows max), but sends to a machine where the model is Claude's.
    const claim = ModelPolicyCheck.pickOfRemoteTarget([], {
      driverKind: "codex",
      model: "shared",
      options: [{ id: "reasoningEffort", value: "max" }],
    } as never);
    assert.isUndefined(claim.driver);
    assert.include(refusal(policy("enforce", rows), claim), "effort max");
    // A driver's own effort id beside a general one: a decoy is read too.
    const decoy = ModelPolicyCheck.pickOfRemoteTarget([], {
      driverKind: "claudeAgent",
      model: "shared",
      options: [
        { id: "effort", value: "high" },
        { id: "variant", value: "max" },
      ],
    } as never);
    assert.include(refusal(policy("enforce", rows), decoy), "does not allow effort high + max");
    // Each row's driver reads its own id, so the pick names the effort under both (and ultracode,
    // which the machine there may turn on by default for a Claude model).
    const inside = ModelPolicyCheck.pickOfRemoteTarget([], {
      driverKind: "codex",
      model: "shared",
      options: [
        { id: "reasoningEffort", value: "high" },
        { id: "effort", value: "high" },
        { id: "ultracode", value: false },
      ],
    } as never);
    assert.isUndefined(refusal(policy("enforce", rows), inside));
  });

  it("a remote pick reads the model's own reasoning id from this machine's catalog (an ACP model)", () => {
    const rows: ReadonlyArray<ModelPolicyRow> = [
      { driver: ProviderDriverKind.make("acpRegistry"), model: ACP_MODEL, efforts: ["high"] },
    ];
    const remote = (
      providers: ReadonlyArray<ServerProvider>,
      options: ReadonlyArray<{ readonly id: string; readonly value: unknown }>,
    ) => ModelPolicyCheck.pickOfRemoteTarget(providers, { model: ACP_MODEL, options } as never);
    // A decoy general id beside the model's own control: both are read, so the pick is refused.
    const decoy = [
      { id: "effort", value: "high" },
      { id: ACP_REASONING_ID, value: "max" },
    ];
    assert.include(
      refusal(policy("enforce", rows), remote([ACP_PROVIDER], decoy)),
      `${ACP_MODEL} does not allow effort high + max`,
    );
    // Its own id alone names an effort the row allows.
    assert.isUndefined(
      refusal(
        policy("enforce", rows),
        remote([ACP_PROVIDER], [{ id: ACP_REASONING_ID, value: "high" }]),
      ),
    );
    // Runs again: the same answer.
    assert.deepStrictEqual(
      remote([ACP_PROVIDER], decoy).effortOptionIds,
      remote([ACP_PROVIDER], decoy).effortOptionIds,
    );
    // Fallback: a machine without the model (or with no providers read) has only the fixed ids.
    assert.deepStrictEqual(modelPolicyPickEfforts(remote([], decoy)), ["high"]);
    const otherModel = { ...ACP_PROVIDER, models: [{ ...ACP_PROVIDER.models[0]!, slug: "other" }] };
    assert.deepStrictEqual(modelPolicyPickEfforts(remote([otherModel], decoy)), ["high"]);
    // A pick that names no model reads no catalog id.
    assert.deepStrictEqual(
      ModelPolicyCheck.pickOfRemoteTarget([ACP_PROVIDER], {} as never).effortOptionIds,
      [],
    );
  });

  it("the effort option ids are the usage pacing gate's", () => {
    const source = NodeFS.readFileSync(
      new URL("../usage/UsagePacingGate.ts", import.meta.url),
      "utf8",
    );
    const list = /const EFFORT_OPTION_IDS[^=]*= new Set\(\[([^\]]*)\]\)/.exec(source)?.[1];
    assert.isDefined(list);
    assert.deepStrictEqual(
      [...(list ?? "").matchAll(/"([^"]+)"/g)].map((match) => match[1]),
      [...MODEL_POLICY_EFFORT_OPTION_IDS],
    );
  });

  it("OpenCode's Default variant, which sends no variant, is no effort (upstream's choice id)", () => {
    const pick = {
      driver: "opencode",
      model: "oc-x",
      options: [{ id: "variant", value: DEFAULT_VARIANT_CHOICE_ID }],
    };
    assert.deepStrictEqual(modelPolicyPickEfforts(pick), []);
  });

  it("Claude's default effort choice for a gateway model, which sends no effort, is no effort", () => {
    // The model as the Claude driver lists one outside its catalog: its effort select's default.
    const { models } = claudeReportedModels(
      BUNDLED_CLAUDE_MODEL_CATALOG,
      [],
      [
        {
          value: "my-gateway-model",
          displayName: "Gateway",
          description: "",
          supportsEffort: true,
          supportedEffortLevels: ["low", "high"],
        },
      ],
    );
    const descriptor = models[0]?.capabilities?.optionDescriptors?.find(
      (candidate) => candidate.id === "effort",
    );
    const choice =
      descriptor?.type === "select"
        ? descriptor.options.find((option) => option.isDefault === true)?.id
        : undefined;
    assert.isDefined(choice);
    const INSTANCE = ProviderInstanceId.make("claudeAgent");
    const gateway = {
      instanceId: INSTANCE,
      driver: ProviderDriverKind.make("claudeAgent"),
      models,
    } as unknown as ServerProvider;
    const rows: ReadonlyArray<ModelPolicyRow> = [
      {
        driver: ProviderDriverKind.make("claudeAgent"),
        model: "my-gateway-model",
        efforts: ["low", "high"],
      },
    ];
    const read = (value: string) =>
      ModelPolicyCheck.pickOfSelection(
        [gateway],
        { instanceId: INSTANCE, model: "my-gateway-model", options: [{ id: "effort", value }] },
        true,
      );
    assert.deepStrictEqual(modelPolicyPickEfforts(read(choice!)), []);
    assert.include(
      refusal(policy("enforce", rows), read(choice!)),
      "needs an effort from its row, and the pick names none",
    );
    assert.isUndefined(refusal(policy("enforce", rows), read("high")));
  });
});

describe("an effort named only under a driver's own option id (#780)", () => {
  const acpRows: ReadonlyArray<ModelPolicyRow> = [
    { driver: ProviderDriverKind.make("acpRegistry"), model: ACP_MODEL, efforts: ["high"] },
  ];
  const remote = (
    providers: ReadonlyArray<ServerProvider>,
    model: string,
    options: ReadonlyArray<{ readonly id: string; readonly value: unknown }>,
  ) => ModelPolicyCheck.pickOfRemoteTarget(providers, { model, options } as never);
  /** An ACP provider here that lists Opus with its own `thought_level` control (lens A's probe). */
  const acpOpus = {
    ...ACP_PROVIDER,
    models: [{ ...ACP_PROVIDER.models[0]!, slug: OPUS }],
  } as unknown as ServerProvider;
  /** The ACP provider with the model listed but no option read yet (an old or empty catalog). */
  const acpUnprobed = {
    ...ACP_PROVIDER,
    models: [{ ...ACP_PROVIDER.models[0]!, capabilities: { optionDescriptors: [] } }],
  } as unknown as ServerProvider;

  it("sent to a linked machine: an id the row's driver does not read names no effort there", () => {
    // Lens A's probes: both were inside before. Claude (the Opus row's driver) reads only `effort`.
    // Each names ultracode, which a Claude pick sent there must.
    const NO_ULTRA = { id: "ultracode", value: false };
    const probes = [
      remote([acpOpus], OPUS, [{ id: ACP_REASONING_ID, value: "high" }, NO_ULTRA]),
      remote([], OPUS, [{ id: "reasoning", value: "high" }, NO_ULTRA]),
    ];
    for (const pick of probes) {
      assert.include(
        refusal(policy("enforce"), pick),
        `${OPUS} names no effort under effort, the option claudeAgent reads it from`,
      );
    }
    assert.isUndefined(
      refusal(
        policy("enforce"),
        remote([acpOpus], OPUS, [{ id: "effort", value: "high" }, NO_ULTRA]),
      ),
    );
  });

  it("sent to a linked machine: a driver whose id this machine does not know refuses (old, empty or missing catalog)", () => {
    // The agent's own id at max beside an allowed general one: before, read as `high` alone.
    const decoy = [
      { id: "effort", value: "high" },
      { id: ACP_REASONING_ID, value: "max" },
    ];
    for (const providers of [[], [acpUnprobed]]) {
      // Runs again: the same refusal.
      for (const _ of [1, 2]) {
        assert.include(
          refusal(policy("enforce", acpRows), remote(providers, ACP_MODEL, decoy)),
          `the option id acpRegistry reads ${ACP_MODEL}'s effort from is not known here`,
        );
      }
    }
    // With the catalog here: the agent's own id at an allowed level is inside.
    assert.isUndefined(
      refusal(
        policy("enforce", acpRows),
        remote([ACP_PROVIDER], ACP_MODEL, [{ id: ACP_REASONING_ID, value: "high" }]),
      ),
    );
  });

  it("checked here (the receiver): read with this machine's own catalog for the instance", () => {
    const ACP_INSTANCE = ACP_PROVIDER.instanceId;
    const read = (
      provider: ServerProvider,
      options: ReadonlyArray<{ id: string; value: string }>,
    ) =>
      ModelPolicyCheck.pickOfSelection(
        [provider],
        { instanceId: ACP_INSTANCE, model: ACP_MODEL, options },
        true,
      );
    // The general id only: the agent never reads it and runs its own default.
    assert.include(
      refusal(policy("enforce", acpRows), read(ACP_PROVIDER, [{ id: "effort", value: "high" }])),
      `${ACP_MODEL} names no effort under ${ACP_REASONING_ID}, the option acpRegistry reads it from`,
    );
    // The model listed with no option read yet: its id is not known, refused.
    assert.include(
      refusal(
        policy("enforce", acpRows),
        read(acpUnprobed, [
          { id: "effort", value: "high" },
          { id: ACP_REASONING_ID, value: "max" },
        ]),
      ),
      "is not known here",
    );
    assert.isUndefined(
      refusal(
        policy("enforce", acpRows),
        read(ACP_PROVIDER, [{ id: ACP_REASONING_ID, value: "high" }]),
      ),
    );
  });

  it("sent to a linked machine: the model's ultracode default counts both ways", () => {
    const ultracodeByDefault = {
      instanceId: ProviderInstanceId.make("claudeAgent"),
      driver: ProviderDriverKind.make("claudeAgent"),
      models: [
        {
          slug: OPUS,
          name: "Opus",
          isCustom: false,
          capabilities: withUltracodeToggle({
            optionDescriptors: [
              {
                id: "effort",
                label: "Effort",
                type: "select",
                currentValue: "ultracode",
                options: ["medium", "high", "xhigh", "ultracode"].map((id) => ({ id, label: id })),
              },
            ],
          }),
        },
      ],
    } as unknown as ServerProvider;
    const pick = remote([ultracodeByDefault], OPUS, [{ id: "effort", value: "high" }]);
    assert.isTrue(pick.ultracodeMaybeDefault);
    // Before: read as `high` and sent, though the model there may run ultracode by default.
    assert.include(refusal(policy("enforce"), pick), "does not allow effort ultracode + high");
    // A row allowing ultracode but not xhigh: the reading without the default still refuses.
    const ultracodeOnly: ReadonlyArray<ModelPolicyRow> = [
      { driver: ProviderDriverKind.make("claudeAgent"), model: OPUS, efforts: ["ultracode"] },
    ];
    assert.include(
      refusal(
        policy("enforce", ultracodeOnly),
        remote([ultracodeByDefault], OPUS, [{ id: "effort", value: "xhigh" }]),
      ),
      "does not allow effort ultracode + xhigh",
    );
    // The pick names the option: its own value stands.
    const off = remote([ultracodeByDefault], OPUS, [
      { id: "effort", value: "high" },
      { id: "ultracode", value: false },
    ]);
    assert.isUndefined(off.ultracodeMaybeDefault);
    assert.isUndefined(refusal(policy("enforce"), off));
    // No catalog here that turns it on: the machine there reads its own, which may, so a
    // Claude row reads it both ways too; the refusal says to name the option.
    const unknown = refusal(policy("enforce"), remote([], OPUS, [{ id: "effort", value: "high" }]));
    assert.include(
      unknown,
      `the machine that runs it may turn ultracode on by default for ${OPUS} (its model list is not read here)`,
    );
    assert.include(
      unknown,
      'Name the option ultracode in target.options: { "id": "ultracode", "value": false }',
    );
    assert.isUndefined(
      refusal(
        policy("enforce"),
        remote([], OPUS, [
          { id: "effort", value: "high" },
          { id: "ultracode", value: false },
        ]),
      ),
    );
  });

  it("an option that picks an agent or a mode is refused with what to leave out (#802)", () => {
    const opencode = {
      driver: "opencode",
      model: "oc",
      options: [
        { id: "variant", value: "high" },
        { id: "agent", value: "build" },
      ],
      describedOptionIds: ["variant", "agent"],
    };
    const rows: ReadonlyArray<ModelPolicyRow> = [
      { driver: ProviderDriverKind.make("opencode"), model: "oc", efforts: ["high"] },
    ];
    const text = refusal(policy("enforce", rows), opencode);
    assert.include(text, "the option agent picks an agent or a mode");
    assert.include(
      text,
      "Leave that option out of target.options: an option that picks an agent or a mode is not allowed while the model policy is on.",
    );
    const advised = answer(policy("advise", rows), opencode);
    assert.include("note" in advised ? (advised.note ?? "") : "", "the option agent picks");
  });

  it("the session mode the policy refuses is ACP's own", () => {
    // The agent-picking ids are the model guard's: fork-kit/model-guard/tests/guard.test.py keeps
    // the two lists in step (app code reads no fork-owned file).
    assert.strictEqual(MODEL_POLICY_SESSION_MODE_OPTION_ID, ACP_SESSION_MODE_OPTION_ID);
  });

  it("the aliases a driver resolves by itself are Claude Code's own", () => {
    const source = NodeFS.readFileSync(
      new URL("../claudeModelOptions.ts", import.meta.url),
      "utf8",
    );
    const list = /const CLAUDE_CODE_MODEL_ALIASES = new Set\(\[([^\]]*)\]\)/.exec(source)?.[1];
    assert.isDefined(list);
    assert.deepStrictEqual(
      [...(list ?? "").matchAll(/"([^"]+)"/g)].map((match) => match[1]).toSorted(),
      [...(MODEL_POLICY_DRIVER_ALIASES.claudeAgent ?? [])].toSorted(),
    );
  });
});

const CALLER = ThreadId.make("thread:policy-caller");
const TARGET = ThreadId.make("thread:policy-target");
const PROJECT = ProjectId.make("project:policy");
const INSTANCE = ProviderInstanceId.make("claudeAgent");

/**
 * Upstream's real `t3_thread_configure` handler with a thread caller (or the owner's client with
 * `caller: false`, a linked machine's link with `"linked agent"` (its agent's call, naming the
 * thread) or `"linked owner"`), the settings given, and every model change it dispatches recorded.
 */
const configureHarness = (
  settings: Parameters<typeof ServerSettings.layerTest>[0],
  options: {
    readonly caller?: boolean | "linked agent" | "linked owner";
    /** The settings as the server holds them, for a state a file decodes to (a kept bad value). */
    readonly held?: ModelPolicy;
    /** More providers this machine has, beside its Claude instance. */
    readonly providers?: ReadonlyArray<ServerProvider>;
  } = {},
) => {
  const dispatched: Array<unknown> = [];
  const forwarded: Array<unknown> = [];
  const threadCaller = options.caller === undefined || options.caller === true;
  const linkSender = { environmentId: "environment-there", threadId: "thread:s", title: "Sender" };
  const shell = (id: ThreadId) =>
    ({
      id,
      projectId: PROJECT,
      providerInstanceId: INSTANCE,
      modelSelection: { instanceId: INSTANCE, model: OPUS },
      runtimeMode: "full-access",
      interactionMode: "default",
      lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: id },
      deletedAt: null,
      archivedAt: null,
    }) as unknown as OrchestrationV2ThreadShell;
  const dependencies = Layer.mergeAll(
    options.held === undefined
      ? ServerSettings.layerTest(settings)
      : Layer.mock(ServerSettings.ServerSettingsService)({
          getSettings: Effect.succeed({ ...DEFAULT_SERVER_SETTINGS, modelPolicy: options.held }),
        }),
    Layer.mock(ProviderRegistry.ProviderRegistry)({
      getProviders: Effect.succeed([
        {
          instanceId: INSTANCE,
          driver: ProviderDriverKind.make("claudeAgent"),
          models: [{ slug: OPUS }, { slug: FABLE }, { slug: "claude-haiku-5-5" }],
        } as unknown as ServerProvider,
        ...(options.providers ?? []),
      ]),
    }),
    Layer.succeed(McpInvocationContext.McpInvocationContext, {
      environmentId: EnvironmentId.make("environment-here"),
      requestNamespace: "thread:policy-caller:session",
      thread: threadCaller
        ? { threadId: CALLER, providerSessionId: "session", providerInstanceId: INSTANCE }
        : undefined,
      client: threadCaller
        ? undefined
        : options.caller === false
          ? { sessionId: "session-1", label: "Owner", access: "full-access" }
          : {
              sessionId: "link-1",
              label: "Laptop",
              access: "full-access",
              linked: true,
              ...(options.caller === "linked agent" ? { senderThread: linkSender as never } : {}),
            },
      issuedAt: 0,
      capabilities: new Set(["orchestration" as const]),
    }),
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadShell: (threadId) => Effect.succeed(shell(threadId)),
      getProjectThreadRecords: ({ threadId }) =>
        Effect.succeed({ thread: shell(threadId) } as never),
      dispatch: (command) =>
        Effect.sync(() => {
          dispatched.push((command as { readonly modelSelection?: unknown }).modelSelection);
          return { sequence: 1 } as never;
        }),
    }),
    Layer.mock(ScheduledTasks.ScheduledTaskService)({}),
    Layer.mock(ThreadSearch.ThreadSearch)({}),
    Layer.mock(PeerForwarding.PeerForwarding)({
      call: (_scope, _tool, environmentId, params) => {
        forwarded.push({ environmentId, params });
        return Effect.fail(
          new OrchestratorMcpFailure({ code: "orchestration_error", message: "sent there" }),
        ) as never;
      },
    }),
  ).pipe(
    Layer.provideMerge(
      ServerConfig.layerTest(process.cwd(), { prefix: "t3-model-policy-configure-" }).pipe(
        Layer.provide(NodeServices.layer),
      ),
    ),
    Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer)),
  );
  /** With `environmentId`, the change is for a thread on that linked machine (sent there). */
  const configure = (
    modelSelection: Omit<ModelSelection, "instanceId"> | undefined,
    environmentId?: EnvironmentId,
  ) =>
    Effect.gen(function* () {
      const toolkit = yield* ThreadToolkit.pipe(
        Effect.provide(
          McpToolAccess.HandlersLayer.layer(ThreadHandlers.layer).pipe(Layer.provide(dependencies)),
        ),
      );
      const chunks = yield* toolkit
        .handle("t3_thread_configure", {
          threadId: TARGET,
          ...(modelSelection === undefined
            ? { title: "Renamed" }
            : { modelSelection: { instanceId: INSTANCE, ...modelSelection } }),
          ...(environmentId === undefined ? {} : { environmentId }),
        } as never)
        .pipe(Stream.unwrap, Stream.runCollect, Effect.provide(dependencies));
      const last = [...chunks].at(-1) as
        | { readonly isFailure: boolean; readonly result: { code?: string; message?: string } }
        | undefined;
      return last?.isFailure === true ? `${last.result.code}: ${last.result.message}` : undefined;
    });
  return { dispatched, forwarded, configure };
};

describe("an option the policy cannot read (#793 review, lens A majors)", () => {
  const optionPolicy = (mode: "advise" | "enforce") => policy(mode, OPTION_CASE_ROWS);
  const picks = (testCase: OptionCase, options: ReadonlyArray<{ id: string; value: string }>) => ({
    // Checked here: an agent's launch or configure here, or one a linked agent sent here.
    here: ModelPolicyCheck.pickOfSelection(
      OPTION_CASE_PROVIDERS,
      { instanceId: testCase.instanceId, model: testCase.model, options },
      true,
    ),
    // Sent to a linked machine: the driver there is not known, every list here is read.
    sent: ModelPolicyCheck.pickOfRemoteTarget(OPTION_CASE_PROVIDERS, {
      model: testCase.model,
      options,
    } as never),
  });

  it("enforce refuses it with the fix, here and before it is sent; advise runs it and says so", () => {
    for (const testCase of OPTION_CASES) {
      for (const [side, pick] of Object.entries(
        picks(testCase, [testCase.allowed, testCase.extra]),
      )) {
        const name = `${testCase.name} (${side})`;
        // Runs again: the same refusal.
        for (const _ of [1, 2]) {
          const refused = refusal(optionPolicy("enforce"), pick);
          assert.include(refused, `Model policy refused this pick: ${testCase.why}`, name);
          assert.include(refused, testCase.fix, name);
          assert.notInclude(refused, "Name one of these models", name);
        }
        const advised = answer(optionPolicy("advise"), pick);
        assert.deepStrictEqual("refuse" in advised, false, name);
        assert.include("note" in advised ? advised.note : "", testCase.why, name);
      }
      // The allowed effort alone: inside on both sides.
      for (const [side, pick] of Object.entries(picks(testCase, [testCase.allowed]))) {
        assert.isUndefined(refusal(optionPolicy("enforce"), pick), `${testCase.name} (${side})`);
      }
    }
  });

  it("old, empty or missing catalog: only the ids the policy reads pass, nothing else", () => {
    const [kiro] = OPTION_CASES;
    const options = [kiro!.allowed, kiro!.extra];
    const emptied = OPTION_CASE_PROVIDERS.map(
      (provider) =>
        ({
          ...provider,
          models: provider.models.map((model) => ({ ...model, capabilities: null })),
        }) as unknown as ServerProvider,
    );
    for (const providers of [[], emptied]) {
      for (const pick of [
        ModelPolicyCheck.pickOfSelection(
          providers,
          { instanceId: kiro!.instanceId, model: kiro!.model, options },
          true,
        ),
        ModelPolicyCheck.pickOfRemoteTarget(providers, { model: kiro!.model, options } as never),
      ]) {
        assert.include(refusal(policy("enforce", OPTION_CASE_ROWS), pick), kiro!.why);
        // Kiro's fixed id alone is still read without a catalog.
        assert.isUndefined(
          refusal(policy("enforce", OPTION_CASE_ROWS), { ...pick, options: [kiro!.allowed] }),
        );
      }
    }
  });

  it("the unknown effort id refusal says how to fix it", () => {
    const acpRows = [{ driver: "acpRegistry", model: ACP_MODEL, efforts: ["high"] }] as never;
    const refused = refusal(
      policy("enforce", acpRows),
      // No ACP agent here: its own id is not known, and the general one names an allowed effort.
      ModelPolicyCheck.pickOfRemoteTarget([], {
        model: ACP_MODEL,
        options: [{ id: "effort", value: "high" }],
      } as never),
    );
    assert.include(refused, "no acpRegistry model list here marks an effort control");
    assert.include(
      refused,
      "Start that agent on this machine once so its model list shows the effort control, or pick a model whose driver has a fixed effort id.",
    );
    assert.notInclude(refused, "Name one of these models");
  });
});

describe("t3_thread_configure through upstream's handler", () => {
  const effort = (value: string) => [{ id: "effort", value }];
  const enforce = { modelPolicy: { mode: "enforce", allow: ROWS } } as never;

  it.effect("enforce: an agent's change to a model off the list is refused; nothing changes", () =>
    Effect.gen(function* () {
      const { dispatched, configure } = configureHarness(enforce);
      const refused = yield* configure({ model: "claude-haiku-5-5", options: effort("high") });
      assert.include(refused, "model_unavailable: Model policy refused this pick");
      assert.include(refused, "claude-haiku-5-5 (claudeAgent) is in no row");
      assert.include(refused, "modelSelection.model");
      assert.include(yield* configure({ model: OPUS, options: effort("max") }), "effort max");
      // Runs again: refused again, still nothing dispatched.
      assert.include(yield* configure({ model: "claude-haiku-5-5" }), "is in no row");
      assert.deepStrictEqual(dispatched, []);
      assert.isUndefined(yield* configure({ model: FABLE, options: effort("high") }));
      assert.deepStrictEqual(dispatched, [
        { instanceId: INSTANCE, model: FABLE, options: effort("high") },
      ]);
    }),
  );

  it.effect("agents only: the owner's client changes the model freely (#125)", () =>
    Effect.gen(function* () {
      const { dispatched, configure } = configureHarness(enforce, { caller: false });
      assert.isUndefined(yield* configure({ model: "claude-haiku-5-5" }));
      assert.lengthOf(dispatched, 1);
    }),
  );

  it.effect("a change an agent on a linked machine sent here is checked against this policy", () =>
    Effect.gen(function* () {
      const { dispatched, configure } = configureHarness(enforce, { caller: "linked agent" });
      assert.include(yield* configure({ model: "claude-haiku-5-5" }), "is in no row");
      assert.deepStrictEqual(dispatched, []);
      assert.isUndefined(yield* configure({ model: FABLE, options: effort("high") }));
      assert.lengthOf(dispatched, 1);
      // A change the link sends for no thread (the owner's client there) stays unchecked.
      const owner = configureHarness(enforce, { caller: "linked owner" });
      assert.isUndefined(yield* owner.configure({ model: "claude-haiku-5-5" }));
      assert.lengthOf(owner.dispatched, 1);
    }),
  );

  it.effect(
    "a change a linked agent sent here: an effort under an id the driver here does not read is refused (#780)",
    () =>
      Effect.gen(function* () {
        const ACP_INSTANCE = ACP_PROVIDER.instanceId;
        const unprobed = {
          ...ACP_PROVIDER,
          instanceId: ProviderInstanceId.make("acp-unprobed"),
          models: [{ ...ACP_PROVIDER.models[0]!, capabilities: { optionDescriptors: [] } }],
        } as unknown as ServerProvider;
        const rows = [...ROWS, { driver: "acpRegistry", model: ACP_MODEL, efforts: ["high"] }];
        const { dispatched, configure } = configureHarness(
          { modelPolicy: { mode: "enforce", allow: rows } } as never,
          { caller: "linked agent", providers: [ACP_PROVIDER, unprobed] },
        );
        // Runs again: refused each time, nothing changed.
        for (const _ of [1, 2]) {
          // Claude here reads only `effort`: before, `reasoning: high` ran Claude's default unchecked.
          assert.include(
            yield* configure({ model: OPUS, options: [{ id: "reasoning", value: "high" }] }),
            `${OPUS} names no effort under effort, the option claudeAgent reads it from`,
          );
          // The ACP agent here reads `thought_level`: a general id only runs its own default.
          assert.include(
            yield* configure({
              instanceId: ACP_INSTANCE,
              model: ACP_MODEL,
              options: effort("high"),
            } as never),
            `${ACP_MODEL} names no effort under ${ACP_REASONING_ID}, the option acpRegistry reads it from`,
          );
          // An instance whose catalog has no option read yet: the agent's own id is not known.
          assert.include(
            yield* configure({
              instanceId: unprobed.instanceId,
              model: ACP_MODEL,
              options: [...effort("high"), { id: ACP_REASONING_ID, value: "max" }],
            } as never),
            "is not known here",
          );
        }
        assert.deepStrictEqual(dispatched, []);
        assert.isUndefined(
          yield* configure({
            instanceId: ACP_INSTANCE,
            model: ACP_MODEL,
            options: [{ id: ACP_REASONING_ID, value: "high" }],
          } as never),
        );
        assert.lengthOf(dispatched, 1);
      }),
  );

  it.effect("advise and off: the change goes through", () =>
    Effect.gen(function* () {
      for (const mode of ["advise", "off"]) {
        const { dispatched, configure } = configureHarness({
          modelPolicy: { mode, allow: ROWS },
        } as never);
        assert.isUndefined(yield* configure({ model: "claude-haiku-5-5" }));
        assert.lengthOf(dispatched, 1);
      }
    }),
  );

  it.effect("a corrupt policy in the file refuses the change (fail closed)", () =>
    Effect.gen(function* () {
      // As the server holds a bad value from the file (`ModelPolicyFromSettingsFile`).
      const { dispatched, configure } = configureHarness(
        {},
        { held: { mode: "enforce", allow: [], unreadable: { mode: "enforce", allow: "x" } } },
      );
      assert.include(
        yield* configure({ model: OPUS, options: effort("high") }),
        "does not parse), so every agent pick is refused",
      );
      assert.deepStrictEqual(dispatched, []);
    }),
  );

  it.effect("a change for a thread on a linked machine is checked before it is sent", () =>
    Effect.gen(function* () {
      const THERE = EnvironmentId.make("environment-there");
      const { dispatched, forwarded, configure } = configureHarness(enforce);
      assert.include(
        yield* configure({ model: "claude-haiku-5-5", options: effort("high") }, THERE),
        "Model policy refused this pick: claude-haiku-5-5 is in no row",
      );
      // A decoy effort id beside the real one: the driver there is not known, all are read.
      assert.include(
        yield* configure(
          { model: OPUS, options: [...effort("high"), { id: "reasoningEffort", value: "max" }] },
          THERE,
        ),
        "does not allow effort high + max",
      );
      // Runs again: refused again; nothing sent, nothing changed here.
      assert.include(yield* configure({ model: "claude-haiku-5-5" }, THERE), "is in no row");
      assert.lengthOf(forwarded, 0);
      // Inside the rows (naming ultracode, which a Claude pick sent there must), or a change
      // with no model (it only renames): sent.
      assert.include(
        yield* configure(
          { model: OPUS, options: [...effort("high"), { id: "ultracode", value: false }] },
          THERE,
        ),
        "sent there",
      );
      assert.include(yield* configure(undefined, THERE), "sent there");
      assert.lengthOf(forwarded, 2);
      assert.deepStrictEqual(dispatched, []);
      // The owner's client is not checked there either.
      const owner = configureHarness(enforce, { caller: false });
      assert.include(yield* owner.configure({ model: "claude-haiku-5-5" }, THERE), "sent there");
      assert.lengthOf(owner.forwarded, 1);
    }),
  );

  it.effect("a change sent there reads an ACP model's own reasoning id from this catalog", () =>
    Effect.gen(function* () {
      const THERE = EnvironmentId.make("environment-there");
      const rows = [{ driver: "acpRegistry", model: ACP_MODEL, efforts: ["high"] }];
      const { forwarded, configure } = configureHarness(
        { modelPolicy: { mode: "enforce", allow: rows } } as never,
        { providers: [ACP_PROVIDER] },
      );
      const decoy = [
        { id: "effort", value: "high" },
        { id: ACP_REASONING_ID, value: "max" },
      ];
      // Runs again: refused each time, nothing sent.
      for (const _ of [1, 2]) {
        assert.include(
          yield* configure({ model: ACP_MODEL, options: decoy }, THERE),
          `${ACP_MODEL} does not allow effort high + max`,
        );
      }
      assert.lengthOf(forwarded, 0);
      assert.include(
        yield* configure(
          { model: ACP_MODEL, options: [{ id: ACP_REASONING_ID, value: "high" }] },
          THERE,
        ),
        "sent there",
      );
      assert.lengthOf(forwarded, 1);
    }),
  );
});

describe("t3_thread_configure: an option the policy cannot read (#793 review)", () => {
  const settings = (mode: string) => ({ modelPolicy: { mode, allow: OPTION_CASE_ROWS } }) as never;
  const THERE = EnvironmentId.make("environment-there");
  const selection = (testCase: OptionCase, withExtra: boolean) =>
    ({
      instanceId: testCase.instanceId,
      model: testCase.model,
      options: withExtra ? [testCase.allowed, testCase.extra] : [testCase.allowed],
    }) as never;
  const routes = [
    { route: "an agent here", caller: true, environmentId: undefined },
    {
      route: "a linked agent's change received here",
      caller: "linked agent",
      environmentId: undefined,
    },
    { route: "an agent's change sent to a linked machine", caller: true, environmentId: THERE },
  ] as const;

  it.effect("enforce: refused on each route, nothing changed or sent; without it, it goes", () =>
    Effect.gen(function* () {
      for (const { route, caller, environmentId } of routes) {
        for (const testCase of OPTION_CASES) {
          const name = `${route}: ${testCase.name}`;
          const { dispatched, forwarded, configure } = configureHarness(settings("enforce"), {
            caller,
            providers: OPTION_CASE_PROVIDERS,
          });
          // Runs again: refused each time.
          for (const _ of [1, 2]) {
            const refused = yield* configure(selection(testCase, true), environmentId);
            assert.include(
              refused,
              `model_unavailable: Model policy refused this pick: ${testCase.why}`,
              name,
            );
            assert.include(refused, testCase.fix, name);
            assert.include(refused, "modelSelection.", name);
          }
          assert.deepStrictEqual([dispatched.length, forwarded.length], [0, 0], name);
          const allowed = yield* configure(selection(testCase, false), environmentId);
          if (environmentId === undefined) {
            assert.isUndefined(allowed, name);
            assert.lengthOf(dispatched, 1, name);
          } else {
            assert.include(allowed, "sent there", name);
            assert.lengthOf(forwarded, 1, name);
          }
        }
      }
    }),
  );

  it.effect("advise: the change goes through as today (the log says enforce would refuse it)", () =>
    Effect.gen(function* () {
      for (const { route, caller, environmentId } of routes) {
        const { dispatched, forwarded, configure } = configureHarness(settings("advise"), {
          caller,
          providers: OPTION_CASE_PROVIDERS,
        });
        for (const testCase of OPTION_CASES) {
          const result = yield* configure(selection(testCase, true), environmentId);
          if (environmentId === undefined) assert.isUndefined(result, route);
          else assert.include(result, "sent there", route);
        }
        assert.lengthOf(
          environmentId === undefined ? dispatched : forwarded,
          OPTION_CASES.length,
          route,
        );
      }
    }),
  );
});

describe("t3_thread_launch: a replay is checked again only when it names a model", () => {
  it.effect(
    "a replay that names no model (it keeps its checked one), and a launch with no calling thread, read nothing",
    () =>
      Effect.gen(function* () {
        const selection = { instanceId: INSTANCE, model: "claude-haiku-5-5" } as ModelSelection;
        // Settings and providers would fail the test if they were read.
        const untouched = Layer.mergeAll(
          Layer.mock(ServerSettings.ServerSettingsService)({
            getSettings: Effect.die("settings read"),
          }),
          Layer.mock(ProviderRegistry.ProviderRegistry)({
            getProviders: Effect.die("providers read"),
          }),
        );
        yield* ModelPolicyCheck.checkLaunch({ id: CALLER }, selection, {}, true).pipe(
          Effect.provide(untouched),
        );
        yield* ModelPolicyCheck.checkLaunch({ id: CALLER }, selection, {}, true).pipe(
          Effect.provide(untouched),
        );
        yield* ModelPolicyCheck.checkLaunch(undefined, selection, {}, false).pipe(
          Effect.provide(untouched),
        );
      }),
  );

  it.effect("a replay that names a model is checked like a new launch (its model wins)", () =>
    Effect.gen(function* () {
      const selection = { instanceId: INSTANCE, model: "claude-haiku-5-5" } as ModelSelection;
      const settings = Layer.mergeAll(
        ServerSettings.layerTest({ modelPolicy: { mode: "enforce", allow: ROWS } } as never),
        Layer.mock(ProviderRegistry.ProviderRegistry)({ getProviders: Effect.succeed([]) }),
      );
      const refused = yield* ModelPolicyCheck.checkLaunch(
        { id: CALLER },
        selection,
        { modelSelection: selection },
        true,
      ).pipe(Effect.provide(settings), Effect.flip);
      assert.include(refused.message, "claude-haiku-5-5 is in no row");
    }),
  );
});
