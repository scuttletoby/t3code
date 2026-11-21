/// <reference types="vite-plus/client" />

import * as Effect from "effect/Effect";
import * as Logger from "effect/Logger";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "@effect/vitest";

import {
  checkModelPolicyPick,
  DEFAULT_MODEL_POLICY,
  MODEL_POLICY_FAIL_CLOSED,
  MODEL_POLICY_START_ROWS,
  modelPolicyPickEfforts,
  modelPolicyStance,
  ModelPolicyPatch,
  ModelPolicyFromSettingsFile,
} from "./modelPolicy.ts";
import { ServerSettings, ServerSettingsPatch } from "./settings.ts";

const decodeStrict = Schema.decodeUnknownSync(ModelPolicyPatch);
const decodeFile = Schema.decodeUnknownSync(ModelPolicyFromSettingsFile);
const decodeFileEffect = Schema.decodeUnknownEffect(ModelPolicyFromSettingsFile);
const decodeServer = Schema.decodeUnknownSync(ServerSettings);
const encodeServer = Schema.encodeSync(ServerSettings);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);

const ROW = {
  driver: "claudeAgent",
  model: "claude-sonnet-5-5",
  efforts: ["medium", "high", "xhigh"],
  defaultEffort: "high",
  useFor: "everyday coding",
};
const POLICY = { mode: "enforce", allow: [ROW, { driver: "codex", model: "gpt-mini" }] };
/** Misspelt keys, at the top and in a row: each must fail closed, never read as a wider policy. */
const TYPOS = [
  { mode: "enforce", allow: [{ driver: "claudeAgent", model: "m", effort: ["low"] }] },
  { mode: "enforce", allow: [{ ...ROW, default_effort: "high" }] },
  { Mode: "enforce", allow: [ROW] },
  { mode: "enforce", allow: [ROW], allowed: [] },
];
/** The rules a policy holds, without the kept hand edit. */
const rules = (policy: { readonly mode: unknown; readonly allow: unknown }) => ({
  mode: policy.mode,
  allow: policy.allow,
});

describe("model policy in the settings file", () => {
  it("reads a file without the key as off with nothing allowed", () => {
    expect(DEFAULT_MODEL_POLICY).toEqual({ mode: "off", allow: [] });
    expect(decodeServer({}).modelPolicy).toEqual(DEFAULT_MODEL_POLICY);
  });

  it("keeps a valid policy through a save and a read", () => {
    const settings = decodeServer({ modelPolicy: POLICY });
    expect(settings.modelPolicy).toEqual(POLICY);
    expect(decodeServer(encodeServer(settings)).modelPolicy).toEqual(POLICY);
    // The writer drops fields equal to their defaults, so a sparse value reads the same.
    expect(decodeServer({ modelPolicy: { mode: "enforce" } }).modelPolicy).toEqual(
      MODEL_POLICY_FAIL_CLOSED,
    );
    expect(decodeServer({ modelPolicy: { allow: [ROW] } }).modelPolicy).toEqual({
      mode: "off",
      allow: [ROW],
    });
  });

  it("reads a bad value as enforce with nothing allowed, and keeps every other setting", () => {
    for (const bad of [
      { mode: "on", allow: [] },
      { mode: "advise", allow: [{ driver: "claudeAgent" }] },
      { mode: "advise", allow: [{ ...ROW, defaultEffort: "low" }] },
      { mode: "advise", allow: [{ ...ROW, efforts: [] }] },
      // No defaultEffort, so only the non-empty check can refuse it.
      { mode: "advise", allow: [{ driver: "claudeAgent", model: "m", efforts: [] }] },
      { mode: "advise", allow: ROW },
      ...TYPOS,
      "off",
      null,
      [],
    ]) {
      expect(rules(decodeFile(bad)), JSON.stringify(bad)).toEqual(MODEL_POLICY_FAIL_CLOSED);
      const settings = decodeServer({ enableAgentBrowserAccess: false, modelPolicy: bad });
      expect(rules(settings.modelPolicy), JSON.stringify(bad)).toEqual(MODEL_POLICY_FAIL_CLOSED);
      expect(settings.enableAgentBrowserAccess).toBe(false);
    }
  });

  it("keeps a bad value as written when another setting is saved", () => {
    const bad = { mode: "Enforce", allow: [ROW, { driver: "codex", model: "m", effort: ["low"] }] };
    const settings = decodeServer({ modelPolicy: bad });
    expect(rules(settings.modelPolicy)).toEqual(MODEL_POLICY_FAIL_CLOSED);
    // A save encodes the settings and the writer reads them back: twice in a row, still as written.
    const saved = encodeServer(settings);
    expect(saved.modelPolicy).toEqual(bad);
    expect(encodeServer(decodeServer(saved)).modelPolicy).toEqual(bad);
    expect(rules(decodeServer(saved).modelPolicy)).toEqual(MODEL_POLICY_FAIL_CLOSED);
  });

  it.effect("warns when it reads a bad value", () =>
    Effect.gen(function* () {
      const logged: Array<unknown> = [];
      const logger = Logger.make(({ message }) => logged.push(message));
      yield* decodeFileEffect({ mode: "on" }).pipe(
        Effect.provide(Logger.layer([logger], { mergeWithExisting: false })),
      );
      expect(JSON.stringify(logged)).toContain("invalid modelPolicy read as enforce");
    }),
  );
});

describe("model policy rows", () => {
  it("checks only the model when a row has no efforts", () => {
    expect(decodeStrict({ mode: "advise", allow: [{ driver: "codex", model: "m" }] })).toEqual({
      mode: "advise",
      allow: [{ driver: "codex", model: "m" }],
    });
  });

  it("refuses a default effort that is not one of the row's efforts", () => {
    expect(() =>
      decodeStrict({ mode: "enforce", allow: [{ ...ROW, defaultEffort: "low" }] }),
    ).toThrow();
    expect(() =>
      decodePatch({ modelPolicy: { mode: "enforce", allow: [{ ...ROW, defaultEffort: "low" }] } }),
    ).toThrow();
    expect(decodePatch({ modelPolicy: POLICY }).modelPolicy).toEqual(POLICY);
  });

  it("refuses an empty efforts list", () => {
    const row = { driver: "claudeAgent", model: "m", efforts: [] };
    expect(() => decodePatch({ modelPolicy: { mode: "advise", allow: [row] } })).toThrow();
  });
});

describe("model policy saves", () => {
  it("refuses a misspelt key instead of dropping it", () => {
    for (const typo of TYPOS) {
      expect(() => decodePatch({ modelPolicy: typo }), JSON.stringify(typo)).toThrow();
    }
  });

  it("refuses a save without both mode and allow, so it never drops rows or turns the policy off", () => {
    for (const partial of [{}, { mode: "advise" }, { allow: [ROW] }]) {
      expect(() => decodePatch({ modelPolicy: partial }), JSON.stringify(partial)).toThrow();
    }
    expect(decodePatch({ modelPolicy: { mode: "off", allow: [] } }).modelPolicy).toEqual(
      DEFAULT_MODEL_POLICY,
    );
  });
});

describe("the fork's start rows (#126)", () => {
  it("are Opus 5.5 and Fable 5.2 at medium, high or xhigh (default high), and save as they are", () => {
    expect(
      MODEL_POLICY_START_ROWS.map((row) => [row.driver, row.model, row.efforts, row.defaultEffort]),
    ).toEqual([
      ["claudeAgent", "claude-opus-5-5", ["medium", "high", "xhigh"], "high"],
      ["claudeAgent", "claude-fable-5-2", ["medium", "high", "xhigh"], "high"],
    ]);
    const saved = { mode: "advise", allow: MODEL_POLICY_START_ROWS };
    expect(decodePatch({ modelPolicy: saved }).modelPolicy).toEqual(saved);
    expect(decodeServer(encodeServer(decodeServer({ modelPolicy: saved }))).modelPolicy).toEqual(
      saved,
    );
  });

  it("are not the setting's default: a file without rows keeps meaning none", () => {
    expect(decodeServer({}).modelPolicy.allow).toEqual([]);
    expect(decodeServer({ modelPolicy: { mode: "enforce" } }).modelPolicy.allow).toEqual([]);
  });
});

describe("one pick against the rows", () => {
  const rows = MODEL_POLICY_START_ROWS;
  const pick = (
    model: string | undefined,
    options: ReadonlyArray<{ id: string; value: unknown }> = [],
  ) => ({
    driver: "claudeAgent",
    model,
    options,
  });

  it("reads the efforts a pick runs at, ultracode as its own effort", () => {
    const efforts = (options: ReadonlyArray<{ id: string; value: unknown }>) =>
      modelPolicyPickEfforts(pick("m", options));
    expect(efforts([])).toEqual([]);
    expect(efforts([{ id: "effort", value: "high" }])).toEqual(["high"]);
    expect(efforts([{ id: "reasoningEffort", value: "low" }])).toEqual(["low"]);
    expect(
      efforts([
        { id: "effort", value: "xhigh" },
        { id: "ultracode", value: true },
      ]),
    ).toEqual(["ultracode"]);
    expect(
      efforts([
        { id: "effort", value: "high" },
        { id: "ultracode", value: true },
      ]),
    ).toEqual(["ultracode", "high"]);
    expect(
      efforts([
        { id: "effort", value: "xhigh" },
        { id: "ultracode", value: false },
      ]),
    ).toEqual(["xhigh"]);
    // The old effort `ultracode`: xhigh with the option on, unless the option is given.
    expect(efforts([{ id: "effort", value: "ultracode" }])).toEqual(["ultracode"]);
    expect(
      efforts([
        { id: "effort", value: "ultracode" },
        { id: "ultracode", value: false },
      ]),
    ).toEqual(["xhigh"]);
    // A boolean under an effort id is not an effort.
    expect(efforts([{ id: "thinking_level", value: true }])).toEqual([]);
  });

  it("reads every effort option, so a second effort-like option cannot hide a higher effort", () => {
    // [reasoning: high, effort: max]: Claude runs at max (it reads `effort`); the pick is refused.
    // `null`: a pick whose driver is not known (a default parameter would swallow `undefined`).
    const decoy = (first: string, second: string, driver: string | null = "claudeAgent") => ({
      driver: driver ?? undefined,
      model: "claude-opus-5-5",
      options: [
        { id: first, value: "high" },
        { id: second, value: "max" },
      ],
    });
    for (const [first, second] of [
      ["reasoning", "effort"],
      ["effort", "reasoning"],
      ["effort", "reasoningEffort"],
      ["reasoning_effort", "effort"],
      ["thinking_level", "effort"],
    ]) {
      expect(modelPolicyPickEfforts(decoy(first!, second!))).toEqual(["high", "max"]);
      expect(checkModelPolicyPick(rows, decoy(first!, second!))).toMatchObject({
        inside: false,
        why: "claude-opus-5-5 does not allow effort high + max",
      });
    }
    // A driver's own id is read for that driver, and for a pick whose driver is not known.
    expect(modelPolicyPickEfforts(decoy("effort", "variant", "opencode"))).toEqual(["high", "max"]);
    expect(modelPolicyPickEfforts(decoy("effort", "variant", null))).toEqual(["high", "max"]);
    expect(modelPolicyPickEfforts(decoy("effort", "thinking", null))).toEqual(["high", "max"]);
    // Another driver's id is not that driver's effort: Claude ignores `variant`.
    expect(modelPolicyPickEfforts(decoy("effort", "variant"))).toEqual(["high"]);
    // The same effort twice is one effort; inside its row.
    const twice = {
      ...decoy("effort", "reasoning"),
      options: [
        { id: "effort", value: "high" },
        { id: "reasoning", value: "high" },
      ],
    };
    expect(modelPolicyPickEfforts(twice)).toEqual(["high"]);
    expect(checkModelPolicyPick(rows, twice)).toMatchObject({ inside: true });
  });

  it("refuses an effort option that holds no effort name, which runs the driver's default", () => {
    const unnamed = [
      { id: "reasoning", value: "high" },
      { id: "effort", value: true },
    ];
    for (const driver of ["claudeAgent", undefined]) {
      expect(
        checkModelPolicyPick(rows, { driver, model: "claude-opus-5-5", options: unnamed }),
      ).toEqual({
        inside: false,
        why: "claude-opus-5-5's effort option effort holds no effort name",
      });
    }
    // Claude's `thinking: true` sent to a linked machine is no pi effort: still inside.
    expect(
      checkModelPolicyPick(rows, {
        driver: undefined,
        model: "claude-opus-5-5",
        options: [
          { id: "effort", value: "high" },
          { id: "thinking", value: true },
        ],
      }),
    ).toMatchObject({ inside: true });
  });

  it("names the drivers whose rows refuse a pick with no known driver that another row allows", () => {
    const disagree = [
      { driver: "codex", model: "gpt-x", efforts: ["high"] },
      { driver: "opencode", model: "gpt-x" },
    ] as never;
    const remote = (options: ReadonlyArray<{ id: string; value: unknown }>) =>
      checkModelPolicyPick(disagree, { driver: undefined, model: "gpt-x", options });
    expect(remote([{ id: "reasoningEffort", value: "high" }])).toEqual({
      inside: false,
      why: "gpt-x does not allow effort high (its opencode row refuses it: the driver there is not known here, so every row naming the model must allow it)",
    });
    expect(remote([])).toEqual({
      inside: false,
      why: "gpt-x needs an effort from its row, and the pick names none (its codex row refuses it: the driver there is not known here, so every row naming the model must allow it)",
    });
    // No row allows it: the plain reason.
    expect(remote([{ id: "reasoningEffort", value: "max" }])).toEqual({
      inside: false,
      why: "gpt-x does not allow effort max",
    });
  });

  it("is inside only with a named model in a row of its driver, at one of the row's efforts", () => {
    expect(
      checkModelPolicyPick(rows, pick("claude-opus-5-5", [{ id: "effort", value: "xhigh" }])),
    ).toMatchObject({
      inside: true,
      row: { model: "claude-opus-5-5" },
    });
    const outside = [
      pick(undefined, [{ id: "effort", value: "high" }]),
      pick("claude-sonnet-5-5", [{ id: "effort", value: "high" }]),
      pick("claude-opus-5-5"),
      pick("claude-opus-5-5", [{ id: "effort", value: "low" }]),
      pick("claude-opus-5-5", [{ id: "effort", value: "max" }]),
      pick("claude-fable-5-2", [
        { id: "effort", value: "xhigh" },
        { id: "ultracode", value: true },
      ]),
      { ...pick("claude-opus-5-5", [{ id: "effort", value: "high" }]), driver: "codex" },
    ];
    for (const candidate of outside) {
      expect(checkModelPolicyPick(rows, candidate).inside, JSON.stringify(candidate)).toBe(false);
    }
    expect(
      checkModelPolicyPick([], pick("claude-opus-5-5", [{ id: "effort", value: "high" }])).inside,
    ).toBe(false);
  });

  it("reads an unreadable value as enforce, or advise when its own text says advise", () => {
    const bad = (raw: unknown) => decodeFile(raw);
    expect(modelPolicyStance(bad({ mode: "advise", allow: "x" }))).toEqual({
      mode: "advise",
      unreadable: true,
    });
    for (const raw of [
      { mode: "off", allow: "x" },
      { mode: "enforce", typo: 1 },
      "advise",
      [],
      3,
    ]) {
      expect(modelPolicyStance(bad(raw)), JSON.stringify(raw)).toEqual({
        mode: "enforce",
        unreadable: true,
      });
    }
    expect(modelPolicyStance(DEFAULT_MODEL_POLICY)).toEqual({ mode: "off" });
    expect(modelPolicyStance({ mode: "advise", allow: rows })).toEqual({
      mode: "advise",
      allow: rows,
      unreadable: false,
    });
  });
});
