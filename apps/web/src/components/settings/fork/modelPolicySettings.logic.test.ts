import { ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  modelPolicyAllowText,
  modelPolicyEditorText,
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
