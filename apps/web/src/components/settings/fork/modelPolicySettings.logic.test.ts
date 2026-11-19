import { ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  modelPolicyAllowText,
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
