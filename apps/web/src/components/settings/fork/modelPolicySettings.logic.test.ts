import {
  checkModelPolicyPick,
  MODEL_POLICY_START_ROWS,
  ProviderDriverKind,
  ServerSettings,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  aliasModelPolicyRows,
  modelPolicyAllowText,
  modelPolicyEditorText,
  modelPolicyListedRows,
  modelPolicyStartsFromStartRows,
  modelPolicyTextRows,
  personModelPickMarks,
  staleModelPolicyRows,
  unknownModelPolicyDrivers,
  unreadableModelPolicyMode,
  unreadableModelPolicyText,
  withAddedModelPolicyRow,
  withModelPolicyAllow,
} from "./modelPolicySettings.logic";

const fromFile = Schema.decodeUnknownSync(ServerSettings);
const ROW = { driver: "codex", model: "gpt-mini", efforts: ["low", "high"], defaultEffort: "high" };

describe("withModelPolicyAllow", () => {
  it("reads the stored rows back unchanged, and empty text as no rows", () => {
    const stored = fromFile({ modelPolicy: { mode: "advise", allow: [ROW] } }).modelPolicy;
    expect(withModelPolicyAllow("advise", modelPolicyAllowText(stored))).toEqual({
      value: { mode: "advise", allow: [ROW] },
    });
    expect(withModelPolicyAllow("enforce", " ")).toEqual({ value: { mode: "enforce", allow: [] } });
  });

  it("refuses what the server's save refuses: an unknown key, a bad default, not a list", () => {
    expect(withModelPolicyAllow("off", JSON.stringify([{ ...ROW, effort: "low" }]))).toHaveProperty(
      "error",
    );
    expect(
      withModelPolicyAllow("off", JSON.stringify([{ ...ROW, defaultEffort: "medium" }])),
    ).toHaveProperty("error");
    expect(withModelPolicyAllow("off", JSON.stringify([{ driver: "codex" }]))).toHaveProperty(
      "error",
    );
    expect(withModelPolicyAllow("off", JSON.stringify(ROW))).toEqual({
      error: "The rows are a JSON list, [ ... ].",
    });
    expect(withModelPolicyAllow("off", "[")).toEqual({ error: "The rows are not valid JSON." });
  });
});

describe("unreadableModelPolicyText", () => {
  it("is the file's own text only when the value could not be read", () => {
    expect(unreadableModelPolicyText(fromFile({}).modelPolicy)).toBeNull();
    const bad = fromFile({ modelPolicy: { mode: "on" } }).modelPolicy;
    expect(bad).toMatchObject({ mode: "enforce", allow: [] });
    expect(unreadableModelPolicyText(bad)).toBe('{"mode":"on"}');
  });
});

describe("modelPolicyEditorText and unreadableModelPolicyMode", () => {
  it("start the editor from an unreadable value's own rows and name its own mode", () => {
    const typo = { ...ROW, effort: "low" };
    const bad = fromFile({ modelPolicy: { mode: "advise", allow: [typo] } }).modelPolicy;
    expect(bad).toMatchObject({ mode: "enforce", allow: [] });
    expect(JSON.parse(modelPolicyEditorText(bad))).toEqual([typo]);
    expect(unreadableModelPolicyMode(bad)).toBe("advise");
  });

  it("start from no rows and no mode when the unreadable value has neither", () => {
    for (const value of [{ mode: "always" }, "enforce", 3, null]) {
      const bad = fromFile({ modelPolicy: value }).modelPolicy;
      expect(modelPolicyEditorText(bad)).toBe("[]");
      expect(unreadableModelPolicyMode(bad)).toBeNull();
    }
    const list = fromFile({ modelPolicy: [ROW] }).modelPolicy;
    expect(JSON.parse(modelPolicyEditorText(list))).toEqual([ROW]);
    expect(unreadableModelPolicyMode(list)).toBeNull();
  });

  it("start from the stored rows when the value was read", () => {
    const stored = fromFile({ modelPolicy: { mode: "off", allow: [ROW] } }).modelPolicy;
    expect(modelPolicyEditorText(stored)).toBe(modelPolicyAllowText(stored));
    expect(unreadableModelPolicyMode(stored)).toBeNull();
  });
});

describe("unknownModelPolicyDrivers", () => {
  const rows = fromFile({
    modelPolicy: {
      mode: "enforce",
      allow: [ROW, { driver: "claudAgent", model: "x" }, { driver: "claudAgent", model: "y" }],
    },
  }).modelPolicy.allow;

  it("names each driver no provider here has, once", () => {
    expect(unknownModelPolicyDrivers(rows, new Set(["codex", "claudeAgent"]))).toEqual([
      "claudAgent",
    ]);
    expect(unknownModelPolicyDrivers(rows, new Set(["codex", "claudAgent"]))).toEqual([]);
  });

  it("flags nothing when no provider is known (a disconnected environment)", () => {
    expect(unknownModelPolicyDrivers(rows, new Set())).toEqual([]);
  });
});

describe("aliasModelPolicyRows (#127)", () => {
  const rows = fromFile({
    modelPolicy: {
      mode: "enforce",
      allow: [
        { driver: "claudeAgent", model: "opus", efforts: ["high"] },
        { driver: "claudeAgent", model: "claude-opus-5-5", efforts: ["high"] },
        { driver: "codex", model: "opus" },
        { driver: "claudeAgent", model: "Fable" },
      ],
    },
  }).modelPolicy.allow;
  const claude = {
    driver: "claudeAgent",
    models: [{ slug: "claude-opus-5-5", aliases: ["opus"] }],
    // A folder's own list counts too.
    workspaceSnapshots: [{ models: [{ slug: "claude-fable-5-2", aliases: ["fable"] }] }],
  };

  it("names each row whose model is an alias in its own driver's lists, with the model", () => {
    expect(aliasModelPolicyRows(rows, [claude, { driver: "codex", models: [] }])).toEqual([
      { row: rows[0], aliasOf: ["claude-opus-5-5"] },
      { row: rows[3], aliasOf: ["claude-fable-5-2"] },
    ]);
    // Runs again: the same answer.
    expect(aliasModelPolicyRows(rows, [claude])).toEqual(aliasModelPolicyRows(rows, [claude]));
  });

  it("flags nothing for a full id, or with no provider of the driver known", () => {
    expect(aliasModelPolicyRows([rows[1]!], [claude])).toEqual([]);
    expect(aliasModelPolicyRows(rows, [])).toEqual([]);
    expect(aliasModelPolicyRows(rows, [{ driver: "claudeAgent" }])).toEqual([]);
  });

  it("an id is never an alias: a name that is one model's id and another's alias is not flagged", () => {
    const row = rows[0]!; // opus
    // A custom model `opus` beside the catalog's `opus` alias, on the same machine.
    const custom = { driver: "claudeAgent", models: [{ slug: "opus" }] };
    expect(aliasModelPolicyRows([row], [custom, claude])).toEqual([]);
    expect(aliasModelPolicyRows([row], [claude, custom])).toEqual([]);
  });

  it("reads each machine on its own lists, as its server does (#780)", () => {
    const row = rows[0]!; // opus
    const here = { driver: "claudeAgent", environmentId: "here", models: [{ slug: "opus" }] };
    const there = { ...claude, environmentId: "there" };
    // `opus` is a model's id here, an alias there: the server there reads a pick of it as named.
    expect(aliasModelPolicyRows([row], [here, there])).toEqual([
      { row, aliasOf: ["claude-opus-5-5"] },
    ]);
    // Only the machine where it is an id: not flagged.
    expect(aliasModelPolicyRows([row], [here])).toEqual([]);
    // Runs again: the same answer.
    expect(aliasModelPolicyRows([row], [here, there])).toEqual(
      aliasModelPolicyRows([row], [here, there]),
    );
  });

  it("flags a name Claude Code resolves by itself, also when no list holds it as an alias (#780)", () => {
    const sonnet = fromFile({
      modelPolicy: { mode: "enforce", allow: [{ driver: "claudeAgent", model: "sonnet" }] },
    }).modelPolicy.allow;
    const plain = { driver: "claudeAgent", models: [{ slug: "claude-opus-5-5" }] };
    expect(aliasModelPolicyRows(sonnet, [plain])).toEqual([{ row: sonnet[0], aliasOf: [] }]);
    // Not for another driver, nor a machine whose model list is not known yet, nor a custom id.
    expect(aliasModelPolicyRows(sonnet, [{ driver: "claudeAgent" }])).toEqual([]);
    expect(
      aliasModelPolicyRows(sonnet, [{ driver: "claudeAgent", models: [{ slug: "sonnet" }] }]),
    ).toEqual([]);
    expect(aliasModelPolicyRows([rows[2]!], [{ driver: "codex", models: [] }])).toEqual([]);
  });
});

describe("the editor's start rows (#126)", () => {
  it("offers the fork's start rows while a readable policy has none saved", () => {
    for (const value of [{}, { mode: "advise" }, { mode: "enforce", allow: [] }]) {
      const policy = fromFile({ modelPolicy: value }).modelPolicy;
      expect(modelPolicyStartsFromStartRows(policy)).toBe(true);
      expect(JSON.parse(modelPolicyEditorText(policy))).toEqual(MODEL_POLICY_START_ROWS);
      // Saved as offered, they read back the same.
      expect(withModelPolicyAllow("advise", modelPolicyEditorText(policy))).toEqual({
        value: { mode: "advise", allow: MODEL_POLICY_START_ROWS },
      });
    }
  });

  it("never replaces saved rows or an unreadable value's own text", () => {
    const stored = fromFile({ modelPolicy: { mode: "off", allow: [ROW] } }).modelPolicy;
    expect(modelPolicyStartsFromStartRows(stored)).toBe(false);
    const bad = fromFile({ modelPolicy: { mode: "on" } }).modelPolicy;
    expect(modelPolicyStartsFromStartRows(bad)).toBe(false);
    expect(modelPolicyEditorText(bad)).toBe("[]");
  });
});

describe("personModelPickMarks: the outside policy mark (#125)", () => {
  const providers = [
    { instanceId: "claudeAgent", driver: "claudeAgent" },
    { instanceId: "codex", driver: "codex" },
  ];
  const policy = (mode: string, allow: unknown = MODEL_POLICY_START_ROWS) =>
    fromFile({ modelPolicy: { mode, allow } }).modelPolicy;
  const picks = {
    defaultModelSelection: {
      instanceId: "claudeAgent",
      model: "claude-opus-5-5",
      options: [{ id: "effort", value: "high" }],
    },
    textGenerationModelSelection: { instanceId: "codex", model: "gpt-mini" },
    reviewGuideModelSelection: {
      instanceId: "claudeAgent",
      model: "claude-fable-5-2",
      options: [{ id: "effort", value: "max" }],
    },
    sourceControlWriterModelSelection: null,
  } as never;

  it("marks each set pick inside or outside the rows, in the settings' order", () => {
    expect(personModelPickMarks(policy("enforce"), picks, providers)).toEqual([
      {
        key: "defaultModelSelection",
        label: "Default model for new threads",
        pick: "claude-opus-5-5, effort high",
        outside: null,
      },
      {
        key: "textGenerationModelSelection",
        label: "Text generation model",
        pick: "gpt-mini",
        outside: "gpt-mini (codex) is in no row",
      },
      {
        key: "reviewGuideModelSelection",
        label: "Review guide model",
        pick: "claude-fable-5-2, effort max",
        outside: "claude-fable-5-2 does not allow effort max",
      },
    ]);
    // Advise marks the same.
    expect(personModelPickMarks(policy("advise"), picks, providers)).toEqual(
      personModelPickMarks(policy("enforce"), picks, providers),
    );
  });

  it("reads an instance's effort as the server reads an agent's pick: its catalog's own id (#780)", () => {
    const acp = {
      instanceId: "acp",
      driver: "acpRegistry",
      models: [
        {
          slug: "acp-model",
          capabilities: {
            optionDescriptors: [{ id: "thought_level", type: "select", role: "reasoning" }],
          },
        },
      ],
    };
    const rows = [{ driver: "acpRegistry", model: "acp-model", efforts: ["high"] }];
    const mark = (options: ReadonlyArray<{ id: string; value: string }>) =>
      personModelPickMarks(
        policy("enforce", rows),
        { defaultModelSelection: { instanceId: "acp", model: "acp-model", options } } as never,
        [acp],
      )[0]?.outside;
    expect(mark([{ id: "thought_level", value: "high" }])).toBeNull();
    expect(mark([{ id: "effort", value: "high" }])).toBe(
      "acp-model names no effort under thought_level, the option acpRegistry reads it from (left out, the driver runs its default level)",
    );
    // Claude reads only `effort`: another id alone is marked.
    expect(
      personModelPickMarks(
        policy("enforce"),
        {
          defaultModelSelection: {
            instanceId: "claudeAgent",
            model: "claude-opus-5-5",
            options: [{ id: "reasoning", value: "high" }],
          },
        } as never,
        providers,
      )[0]?.outside,
    ).toContain("names no effort under effort");
  });

  it("reads the instance of the machine whose settings hold the picks, not another machine's with the same id (#802)", () => {
    const acpOn = (environmentId: string, control: string) => ({
      environmentId,
      instanceId: "acp",
      driver: "acpRegistry",
      models: [
        {
          slug: "acp-model",
          capabilities: {
            optionDescriptors: [{ id: control, type: "select", role: "reasoning" }],
          },
        },
      ],
    });
    // The primary is listed first; the picks are the second machine's.
    const both = [acpOn("primary", "effort_level"), acpOn("second", "thought_level")];
    const rows = [{ driver: "acpRegistry", model: "acp-model", efforts: ["high"] }];
    const mark = (environmentId: string) =>
      personModelPickMarks(
        policy("enforce", rows),
        {
          defaultModelSelection: {
            instanceId: "acp",
            model: "acp-model",
            options: [{ id: "thought_level", value: "high" }],
          },
        } as never,
        both,
        environmentId,
      )[0]?.outside;
    expect(mark("second")).toBeNull();
    // The primary's list reads only effort_level: thought_level there names no effort.
    expect(mark("primary")).toBe("acp-model needs an effort from its row, and the pick names none");
  });

  it("marks nothing while the policy is off or cannot be read; empty rows mark every pick", () => {
    expect(personModelPickMarks(policy("off"), picks, providers)).toEqual([]);
    expect(
      personModelPickMarks(fromFile({ modelPolicy: { mode: "x" } }).modelPolicy, picks, providers),
    ).toEqual([]);
    expect(
      personModelPickMarks(policy("enforce", []), picks, providers).every(
        (mark) => mark.outside !== null,
      ),
    ).toBe(true);
  });
});

describe("staleModelPolicyRows (#876 M6)", () => {
  const rows = fromFile({
    modelPolicy: {
      mode: "enforce",
      allow: [
        { driver: "claudeAgent", model: "claude-opus-5-5", efforts: ["high"] },
        { driver: "claudeAgent", model: "claude-opus-4-1" },
        { driver: "claudeAgent", model: "opus" },
        { driver: "codex", model: "gpt-mini" },
      ],
    },
  }).modelPolicy.allow;
  const claude = {
    driver: "claudeAgent",
    models: [{ slug: "claude-opus-5-5", aliases: ["opus"] }],
  };

  it("names each row whose model no list of its driver holds, by id or alias", () => {
    // A retired id is stale; the full id and the alias (marked as an alias instead) are not.
    expect(staleModelPolicyRows(rows, [claude])).toEqual([rows[1]]);
    // Runs again: the same answer.
    expect(staleModelPolicyRows(rows, [claude])).toEqual(staleModelPolicyRows(rows, [claude]));
  });

  it("counts a folder's own list, and a name the driver resolves by itself", () => {
    const folder = {
      driver: "claudeAgent",
      models: [{ slug: "claude-fable-5-2" }],
      workspaceSnapshots: [{ models: [{ slug: "claude-opus-4-1" }] }],
    };
    // `opus` is Claude Code's own alias: not stale even where no list names it.
    expect(staleModelPolicyRows(rows.slice(1, 3), [folder])).toEqual([]);
    // A provider with only a folder's list still resolves its own alias.
    const folderOnly = { driver: "claudeAgent", workspaceSnapshots: folder.workspaceSnapshots };
    expect(staleModelPolicyRows([rows[2]!], [folderOnly])).toEqual([]);
  });

  it("flags nothing for a driver whose model list is not known here", () => {
    // No codex provider (the unknown driver note covers it), no list yet, or an empty list.
    expect(staleModelPolicyRows([rows[3]!], [claude])).toEqual([]);
    expect(staleModelPolicyRows([rows[1]!], [{ driver: "claudeAgent" }])).toEqual([]);
    expect(staleModelPolicyRows([rows[1]!], [{ driver: "claudeAgent", models: [] }])).toEqual([]);
  });

  it("reads each machine on its own lists: listed here but not there is stale there", () => {
    const here = { ...claude, environmentId: "here" };
    const there = {
      driver: "claudeAgent",
      environmentId: "there",
      models: [{ slug: "claude-opus-4-1" }],
    };
    expect(staleModelPolicyRows([rows[0]!, rows[1]!], [here, there])).toEqual([rows[0], rows[1]]);
    // A machine without the driver's list says nothing.
    expect(
      staleModelPolicyRows([rows[1]!], [there, { driver: "claudeAgent", environmentId: "x" }]),
    ).toEqual([]);
  });
});

describe("modelPolicyListedRows: rows picked from the live model list (#876 M6)", () => {
  const effort = (choices: ReadonlyArray<{ id: string; isDefault?: boolean }>, id = "effort") => ({
    capabilities: {
      optionDescriptors: [
        {
          id,
          label: "Effort",
          type: "select",
          options: choices.map((c) => ({ ...c, label: c.id })),
        },
      ],
    },
  });
  const claude = {
    driver: "claudeAgent",
    environmentId: "here",
    models: [
      {
        slug: "claude-opus-5-5",
        name: "Opus 5.5",
        ...effort([{ id: "medium" }, { id: "high", isDefault: true }, { id: "xhigh" }]),
      },
      { slug: "claude-haiku-5-5", name: "Haiku 5.5" },
    ],
  };

  it("offers each listed model as a row the server's save accepts, efforts from its own control", () => {
    const listed = modelPolicyListedRows([], [claude]);
    expect(listed.map((entry) => entry.row)).toEqual([
      {
        driver: "claudeAgent",
        model: "claude-opus-5-5",
        efforts: ["medium", "high", "xhigh"],
        defaultEffort: "high",
      },
      // No effort control: the row checks only the model.
      { driver: "claudeAgent", model: "claude-haiku-5-5" },
    ]);
    expect(listed.map((entry) => entry.label)).toEqual([
      "claudeAgent: Opus 5.5 (claude-opus-5-5)",
      "claudeAgent: Haiku 5.5 (claude-haiku-5-5)",
    ]);
    const rows = listed.map((entry) => entry.row);
    expect(withModelPolicyAllow("enforce", JSON.stringify(rows))).toEqual({
      value: { mode: "enforce", allow: rows },
    });
  });

  it("reads the effort under the id the driver reads, not a decoy, and never OpenCode's no-variant", () => {
    const codex = {
      driver: "codex",
      models: [
        {
          slug: "gpt-mini",
          name: "GPT mini",
          capabilities: {
            optionDescriptors: [
              {
                id: "effort",
                label: "Decoy",
                type: "select",
                options: [{ id: "max", label: "x" }],
              },
              {
                id: "reasoningEffort",
                label: "Effort",
                type: "select",
                options: [{ id: "low", label: "Low" }],
              },
            ],
          },
        },
      ],
    };
    const opencode = {
      driver: "opencode",
      models: [
        {
          slug: "big",
          name: "Big",
          ...effort([{ id: "t3:default", isDefault: true }, { id: "high" }], "variant"),
        },
      ],
    };
    expect(modelPolicyListedRows([], [codex, opencode]).map((entry) => entry.row)).toEqual([
      { driver: "codex", model: "gpt-mini", efforts: ["low"] },
      { driver: "opencode", model: "big", efforts: ["high"] },
    ]);
  });

  it("never offers a choice that sends no effort: Claude's default for a gateway model (lens major)", () => {
    // As the Claude driver lists a model outside its catalog: `default` sends none, Claude Code picks.
    const gateway = {
      driver: "claudeAgent",
      models: [
        {
          slug: "my-gateway-model",
          name: "Gateway",
          ...effort([{ id: "default", isDefault: true }, { id: "low" }, { id: "high" }]),
        },
      ],
    };
    const [row] = modelPolicyListedRows([], [gateway]).map((entry) => entry.row);
    expect(row).toEqual({
      driver: "claudeAgent",
      model: "my-gateway-model",
      efforts: ["low", "high"],
    });
    // The server check refuses the pick of `default` under that row: it names no effort.
    const pick = (value: string) => ({
      driver: "claudeAgent",
      model: "my-gateway-model",
      options: [{ id: "effort", value }],
    });
    expect(checkModelPolicyPick([row!], pick("default"))).toMatchObject({ inside: false });
    expect(checkModelPolicyPick([row!], pick("high"))).toMatchObject({ inside: true });
    // Another driver's `default` is a choice like any other.
    const other = {
      driver: "fx",
      models: [{ slug: "m", name: "M", ...effort([{ id: "default" }]) }],
    };
    expect(modelPolicyListedRows([], [other])[0]!.row.efforts).toEqual(["default"]);
  });

  it("never offers ultracode as an effort choice (a machine on an older build lists it so)", () => {
    const old = {
      driver: "claudeAgent",
      models: [
        {
          slug: "claude-opus-5-5",
          name: "Opus 5.5",
          ...effort([{ id: "high", isDefault: true }, { id: "xhigh" }, { id: "ultracode" }]),
        },
      ],
    };
    expect(modelPolicyListedRows([], [old])[0]!.row).toEqual({
      driver: "claudeAgent",
      model: "claude-opus-5-5",
      efforts: ["high", "xhigh"],
      defaultEffort: "high",
    });
  });

  it("a model that turns ultracode on by default gets a row that allows the pick leaving it out", () => {
    const model = effort([{ id: "high" }, { id: "xhigh", isDefault: true }]);
    const ultracodeOn = {
      driver: "claudeAgent",
      models: [
        {
          slug: "claude-opus-5-5",
          name: "Opus 5.5",
          capabilities: {
            optionDescriptors: [
              ...model.capabilities.optionDescriptors,
              { id: "ultracode", label: "Ultracode", type: "boolean", currentValue: true },
            ],
          },
        },
      ],
    };
    const [row] = modelPolicyListedRows([], [ultracodeOn]).map((entry) => entry.row);
    expect(row).toEqual({
      driver: "claudeAgent",
      model: "claude-opus-5-5",
      efforts: ["high", "xhigh", "ultracode"],
      defaultEffort: "ultracode",
    });
    // As the server reads a pick that leaves the toggle out here (the default on), and for a
    // linked machine (read both ways): both inside, so a Save of this row refuses neither.
    const here = {
      driver: "claudeAgent",
      model: "claude-opus-5-5",
      options: [
        { id: "effort", value: "high" },
        { id: "ultracode", value: true },
      ],
    };
    expect(checkModelPolicyPick([row!], here)).toMatchObject({ inside: true });
    const remote = {
      model: "claude-opus-5-5",
      options: [{ id: "effort", value: "xhigh" }],
      ultracodeMaybeDefault: true,
      ultracodeDefaultUnknown: true,
    };
    expect(checkModelPolicyPick([row!], remote)).toMatchObject({ inside: true });
  });

  it("leaves out models a row already names, and lists a model once across machines", () => {
    const there = { ...claude, environmentId: "there" };
    const listed = modelPolicyListedRows(
      [{ driver: "claudeAgent", model: "claude-haiku-5-5" }],
      [claude, there],
    );
    expect(listed.map((entry) => entry.row.model)).toEqual(["claude-opus-5-5"]);
    expect(new Set(listed.map((entry) => entry.key)).size).toBe(listed.length);
  });
});

describe("modelPolicyTextRows and withAddedModelPolicyRow (#876 M6)", () => {
  const row = { driver: ProviderDriverKind.make("codex"), model: "gpt-mini" };

  it("reads the editor's rows loosely, none for text that is not a list", () => {
    expect(modelPolicyTextRows(JSON.stringify([ROW, 3, null]))).toEqual([ROW]);
    expect(modelPolicyTextRows("[ {")).toEqual([]);
    expect(modelPolicyTextRows(JSON.stringify(ROW))).toEqual([]);
  });

  it("adds the row at the end of the editor's list, empty text as no rows", () => {
    const added = withAddedModelPolicyRow(JSON.stringify([ROW]), row);
    expect(added).toEqual({ text: JSON.stringify([ROW, row], null, 2) });
    expect(withAddedModelPolicyRow("  ", row)).toEqual({ text: JSON.stringify([row], null, 2) });
  });

  it("keeps text it cannot read and says why, so nothing the person typed is lost", () => {
    expect(withAddedModelPolicyRow("[ {", row)).toEqual({
      error: "Fix the rows' JSON first; a listed model is added to a valid list.",
    });
    expect(withAddedModelPolicyRow(JSON.stringify(ROW), row)).toEqual({
      error: "Fix the rows' JSON first; a listed model is added to a valid list.",
    });
  });
});
