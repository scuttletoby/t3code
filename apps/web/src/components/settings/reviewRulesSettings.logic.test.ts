import {
  REVIEW_RULES_RECOMMENDED,
  ServerSettings,
  type ReviewRules,
  effectiveReviewRules,
} from "@t3tools/contracts";
import { applyServerSettingsPatch } from "@t3tools/shared/serverSettings";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  REVIEW_RULES_BUTTON_VALUE,
  effectiveReviewRulesRows,
  handSetFields,
  planReviewRulesOverrideClear,
  planReviewRulesWrite,
  reviewRulesDescription,
  reviewRulesSaveError,
  reviewRulesState,
  reviewRulesSummary,
  unreadableReviewRulesText,
} from "./reviewRulesSettings.logic";

const decodeSettings = Schema.decodeUnknownSync(ServerSettings);
const fromFile = (value: unknown): ReviewRules =>
  decodeSettings(value === undefined ? {} : { reviewRules: value }).reviewRules;
const environment = { isProjectScope: false, projectOverrides: false };

describe("old, empty and bad settings files", () => {
  it("shows off for a settings file without the key", () => {
    const value = fromFile(undefined);
    expect(reviewRulesState(value)).toBe("off");
    expect(reviewRulesSummary(value)).toBe("Off");
    expect(reviewRulesDescription(value, environment)).toBe(
      "Off: agents get no way to start review lenses here and nothing is checked. On: they can start the lenses a pull request still needs (lens_start), with the Recommended values. Default: off.",
    );
    expect(effectiveReviewRulesRows(value)).toEqual([]);
    // Undefined (a server too old to send the key) reads the same.
    expect(reviewRulesState(undefined)).toBe("off");
  });

  it("shows a bad stored value as off, with its text, never as on", () => {
    const value = fromFile({ preset: "recommended", laterField: true });
    expect(reviewRulesState(value)).toBe("unreadable");
    expect(reviewRulesSummary(value)).toBe("Off: the stored value cannot be read");
    expect(effectiveReviewRulesRows(value)).toEqual([]);
    expect(unreadableReviewRulesText(value)).toBe('{"preset":"recommended","laterField":true}');
    expect(unreadableReviewRulesText(fromFile({ preset: "off" }))).toBeNull();
  });

  it("shortens a long unreadable value", () => {
    const text = unreadableReviewRulesText(fromFile({ preset: "x".repeat(2000) }));
    expect(text?.length).toBe(601);
    expect(text?.endsWith("…")).toBe(true);
  });
});

describe("the effective values", () => {
  it("shows every preset value, marked as the preset's", () => {
    const rows = effectiveReviewRulesRows(fromFile({ preset: "recommended" }));
    expect(rows.map((row) => row.key)).toEqual([
      "runner",
      "lensRunners.approach",
      "stateQuestion",
      "minors",
      "configPath",
      "reportMaxChars",
    ]);
    expect(rows.every((row) => row.fromPreset)).toBe(true);
    const value = (key: string) => rows.find((row) => row.key === key)?.value;
    const { runner, lensRunners } = REVIEW_RULES_RECOMMENDED;
    expect(value("runner")).toBe(`${runner.driver} · ${runner.model} · high effort`);
    expect(value("lensRunners.approach")).toBe(
      `${lensRunners.approach!.driver} · ${lensRunners.approach!.model} · high effort`,
    );
    expect(value("stateQuestion")).toBe(REVIEW_RULES_RECOMMENDED.stateQuestion);
    expect(value("minors")).toBe("Fixed in the pull request or dropped (same-round)");
    expect(value("configPath")).toBe(REVIEW_RULES_RECOMMENDED.configPath);
    expect(value("reportMaxChars")).toBe("60000 characters");
  });

  it("marks hand-set fields and shows their values, hand-set lenses after approach", () => {
    const stored = fromFile({
      preset: "recommended",
      minors: "free",
      reportMaxChars: 2000,
      lensRunners: {
        security: { driver: "codex", model: "gpt-x", effort: "high" },
        // Sorts before "approach" by name: approach still comes first.
        accessibility: { driver: "codex", model: "gpt-x", effort: "high" },
      },
    });
    const rows = effectiveReviewRulesRows(stored);
    const row = (key: string) => rows.find((candidate) => candidate.key === key)!;
    expect(rows.map((candidate) => candidate.key).slice(1, 4)).toEqual([
      "lensRunners.approach",
      "lensRunners.accessibility",
      "lensRunners.security",
    ]);
    expect(row("minors")).toMatchObject({ value: "No rule (free)", fromPreset: false });
    expect(row("reportMaxChars")).toMatchObject({ value: "2000 characters", fromPreset: false });
    expect(row("lensRunners.security").fromPreset).toBe(false);
    expect(row("lensRunners.approach").fromPreset).toBe(true);
    expect(row("runner").fromPreset).toBe(true);
    // The rows are the contract's effective values, not a second copy of the rule.
    expect(row("configPath").value).toBe(
      (effectiveReviewRules(stored) as { configPath: string }).configPath,
    );
  });

  it("says hand-set fields do nothing while the preset is off", () => {
    const value = fromFile({ preset: "off", minors: "free" });
    expect(handSetFields(value)).toEqual(["minors"]);
    expect(effectiveReviewRulesRows(value)).toEqual([]);
    expect(reviewRulesDescription(value, environment)).toContain("do nothing while the preset");
  });

  it("names the project's override, or that it uses the environment's", () => {
    const on = fromFile({ preset: "recommended" });
    expect(reviewRulesDescription(on, { isProjectScope: true, projectOverrides: true })).toMatch(
      /^This project overrides the environment's review rules\. On:/u,
    );
    expect(reviewRulesDescription(on, { isProjectScope: true, projectOverrides: false })).toMatch(
      /^This project uses the environment's review rules\. On:/u,
    );
  });
});

describe("what a button saves", () => {
  it("writes exactly the preset, which the patch replaces whole", () => {
    expect(REVIEW_RULES_BUTTON_VALUE.recommended).toEqual({ preset: "recommended" });
    expect(REVIEW_RULES_BUTTON_VALUE.off).toEqual({ preset: "off" });
    const handSet = applyServerSettingsPatch(decodeSettings({}), {
      reviewRules: { preset: "recommended", minors: "free" },
    });
    const saved = applyServerSettingsPatch(handSet, {
      reviewRules: { ...REVIEW_RULES_BUTTON_VALUE.recommended },
    });
    expect(saved.reviewRules).toEqual({ preset: "recommended" });
  });

  it("saves nothing on a second click: the value is already there", () => {
    expect(planReviewRulesWrite([fromFile({ preset: "recommended" })], "recommended")).toEqual({
      kind: "same",
    });
    // A file without the key reads as off, so Turn off has nothing to do.
    expect(planReviewRulesWrite([fromFile(undefined)], "off")).toEqual({ kind: "same" });
    expect(planReviewRulesWrite([fromFile(undefined)], "recommended")).toEqual({ kind: "write" });
  });

  it("writes a project's first override, even when the environment's value is the same", () => {
    expect(planReviewRulesWrite([undefined], "off")).toEqual({ kind: "write" });
    expect(planReviewRulesWrite([], "off")).toEqual({ kind: "write" });
  });

  it("asks first before it drops hand-set fields", () => {
    const plan = planReviewRulesWrite(
      [fromFile({ preset: "recommended", minors: "free", configPath: "ci/q.json" })],
      "recommended",
    );
    expect(plan).toEqual({
      kind: "confirm",
      reasons: ["Saving removes the fields set by hand: small findings, repository config."],
    });
  });

  it("asks first before it replaces an unreadable value, on any selected environment", () => {
    const plan = planReviewRulesWrite(
      [fromFile({ preset: "off" }), fromFile({ preset: "strict" })],
      "off",
    );
    expect(plan.kind).toBe("confirm");
    expect(plan.kind === "confirm" && plan.reasons[0]).toContain("cannot be read");
  });

  it("asks first before clearing an unreadable project override, not a readable one", () => {
    expect(planReviewRulesOverrideClear([fromFile({ preset: "recommended" })])).toEqual({
      kind: "write",
    });
    expect(planReviewRulesOverrideClear([fromFile({ preset: 3 })]).kind).toBe("confirm");
  });
});

describe("a failed save", () => {
  it("says nothing changed only when no environment saved", () => {
    const nothing = "Not saved. The review rules stay as they were.";
    expect(reviewRulesSaveError(undefined)).toBe(nothing);
    expect(reviewRulesSaveError({ failed: ["Laptop"], saved: [] })).toBe(nothing);
    expect(reviewRulesSaveError({ failed: ["Laptop", "Box"], saved: ["Server"] })).toBe(
      "Not saved on Laptop, Box. Saved on Server.",
    );
  });
});
