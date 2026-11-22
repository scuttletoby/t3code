import { MODEL_POLICY_START_ROWS, ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  aliasModelPolicyRows,
  modelPolicyAllowText,
  modelPolicyEditorText,
  modelPolicyStartsFromStartRows,
  personModelPickMarks,
  unknownModelPolicyDrivers,
  unreadableModelPolicyMode,
  unreadableModelPolicyText,
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
