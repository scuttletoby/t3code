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
  modelPolicyAliasTargets,
  modelPolicyPickEfforts,
  modelPolicyRowsText,
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

  it("reads Claude's default effort choice (sends no effort, Claude Code picks) as no effort", () => {
    const rows = [
      { driver: "claudeAgent", model: "gw", efforts: ["low", "high"] },
      { driver: "claudeAgent", model: "gw-plain" },
    ] as never;
    const claudePick = (model: string, value: string, driver?: string) => ({
      ...(driver === undefined ? {} : { driver }),
      model,
      options: [{ id: "effort", value }],
    });
    expect(modelPolicyPickEfforts(claudePick("gw", "default", "claudeAgent"))).toEqual([]);
    // A row with efforts refuses it; a row without (no effort control) allows it as left out.
    expect(checkModelPolicyPick(rows, claudePick("gw", "default", "claudeAgent"))).toMatchObject({
      inside: false,
      why: "gw needs an effort from its row, and the pick names none",
    });
    expect(
      checkModelPolicyPick(rows, claudePick("gw-plain", "default", "claudeAgent")),
    ).toMatchObject({ inside: true });
    // Not Claude's no-effort choice under another driver, or with the driver not known.
    expect(modelPolicyPickEfforts(claudePick("gw", "default", "fx"))).toEqual(["default"]);
    expect(checkModelPolicyPick(rows, claudePick("gw", "default"))).toMatchObject({
      inside: false,
    });
  });

  it("leaves a row's no-effort choice out of the allowed list, as a pick naming it is refused", () => {
    // A hand-written Claude row holding `default` (no merged build offered it) still saves.
    const rows = [
      { driver: "claudeAgent", model: "gw", efforts: ["default", "low"], defaultEffort: "default" },
      { driver: "claudeAgent", model: "gw-only", efforts: ["default"] },
      { driver: "opencode", model: "oc-x", efforts: ["t3:default", "high"], defaultEffort: "high" },
      { driver: "fx", model: "fx-x", efforts: ["default", "low"], defaultEffort: "default" },
    ] as never;
    expect(
      checkModelPolicyPick(rows, {
        driver: "claudeAgent",
        model: "gw",
        options: [{ id: "effort", value: "default" }],
      }),
    ).toMatchObject({ inside: false });
    expect(modelPolicyRowsText(rows)).toBe(
      [
        "claudeAgent gw (efforts low; default left out: on claudeAgent it sends no effort, so a pick naming it is refused)",
        "claudeAgent gw-only (efforts none usable; default left out: on claudeAgent it sends no effort, so a pick naming it is refused)",
        "opencode oc-x (efforts high; default high; t3:default left out: on opencode it sends no effort, so a pick naming it is refused)",
        // Another driver's `default` is a level like any other.
        "fx fx-x (efforts default, low; default default)",
      ].join("; "),
    );
  });

  it("reads OpenCode's Default variant (t3:default, sends no variant) as no effort, as if left out", () => {
    const rows = [
      { driver: "opencode", model: "oc-x", efforts: ["low", "high"] },
      { driver: "opencode", model: "oc-plain" },
    ] as never;
    const pick = (model: string, value: string, driver?: string) => ({
      driver,
      model,
      options: [{ id: "variant", value }],
    });
    const left = (model: string) => ({ driver: "opencode", model, options: [] });
    expect(modelPolicyPickEfforts(pick("oc-x", "t3:default", "opencode"))).toEqual([]);
    for (const model of ["oc-x", "oc-plain"]) {
      expect(checkModelPolicyPick(rows, pick(model, "t3:default", "opencode"))).toEqual(
        checkModelPolicyPick(rows, left(model)),
      );
    }
    // A row with efforts still needs one named; a row without efforts takes it.
    expect(checkModelPolicyPick(rows, pick("oc-x", "t3:default", "opencode"))).toMatchObject({
      inside: false,
      why: "oc-x needs an effort from its row, and the pick names none",
    });
    expect(checkModelPolicyPick(rows, pick("oc-plain", "t3:default", "opencode"))).toMatchObject({
      inside: true,
    });
    // It never hides another effort option, and only OpenCode's own id with a known driver counts.
    expect(
      modelPolicyPickEfforts({
        driver: "opencode",
        model: "oc-x",
        options: [
          { id: "variant", value: "t3:default" },
          { id: "effort", value: "max" },
        ],
      }),
    ).toEqual(["max"]);
    expect(modelPolicyPickEfforts(pick("oc-x", "t3:default"))).toEqual(["t3:default"]);
    expect(
      modelPolicyPickEfforts({
        driver: "opencode",
        model: "oc-x",
        options: [{ id: "effort", value: "t3:default" }],
      }),
    ).toEqual(["t3:default"]);
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

  it("a pick read as a full id from the name it gave (namedAs) matches a row naming either", () => {
    const high = [{ id: "effort", value: "high" }];
    const aliasRow = [{ ...rows[0]!, model: "opus" }];
    const asOpus = { ...pick("claude-opus-5-5", high), namedAs: "opus" };
    // A row saved with the alias matches the alias the call gave; a full-id row the id it is read as.
    expect(checkModelPolicyPick(aliasRow, asOpus)).toMatchObject({
      inside: true,
      row: { model: "opus" },
    });
    expect(checkModelPolicyPick(rows, asOpus)).toMatchObject({
      inside: true,
      row: { model: "claude-opus-5-5" },
    });
    // Still at the row's efforts, and only the names given: no other alias or id matches.
    expect(
      checkModelPolicyPick(aliasRow, { ...asOpus, options: [{ id: "effort", value: "max" }] })
        .inside,
    ).toBe(false);
    expect(checkModelPolicyPick(aliasRow, pick("claude-opus-5-5", high)).inside).toBe(false);
    expect(
      checkModelPolicyPick(aliasRow, { ...pick("claude-sonnet-5-5", high), namedAs: "sonnet" })
        .inside,
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

describe("an effort under the id the row's driver reads (#780)", () => {
  const rows = MODEL_POLICY_START_ROWS;
  const OPUS = "claude-opus-5-5";
  const acpRows = [{ driver: "acpRegistry", model: "acp-model", efforts: ["high"] }] as never;
  const options = (...pairs: ReadonlyArray<readonly [string, unknown]>) =>
    pairs.map(([id, value]) => ({ id, value }));

  it("a Claude row needs the effort under effort: another id only is the driver's default level", () => {
    // Claude reads only `effort`; `reasoning: high` alone runs its catalog default, unchecked.
    for (const driver of ["claudeAgent", undefined]) {
      expect(
        checkModelPolicyPick(rows, {
          driver,
          model: OPUS,
          options: options(["reasoning", "high"]),
        }),
      ).toEqual({
        inside: false,
        why: `${OPUS} names no effort under effort, the option claudeAgent reads it from (left out, the driver runs its default level)`,
      });
    }
    // An ACP model's own id for Opus on this machine does not name Claude's effort either.
    expect(
      checkModelPolicyPick(rows, {
        driver: undefined,
        model: OPUS,
        options: options(["thought_level", "high"]),
        effortOptionIds: ["thought_level"],
        driverEffortIds: { acpRegistry: ["thought_level"] },
      }).inside,
    ).toBe(false);
    // Under `effort`: inside, on either route.
    for (const driver of ["claudeAgent", undefined]) {
      expect(
        checkModelPolicyPick(rows, { driver, model: OPUS, options: options(["effort", "high"]) }),
      ).toMatchObject({ inside: true });
    }
  });

  it("a driver with no fixed id reads the id its catalog marks; none known refuses (fail closed)", () => {
    const acp = (
      pairs: ReadonlyArray<readonly [string, unknown]>,
      driverEffortIds?: Readonly<Record<string, ReadonlyArray<string>>>,
      driver: string | undefined = "acpRegistry",
    ) =>
      checkModelPolicyPick(acpRows, {
        driver,
        model: "acp-model",
        options: options(...pairs),
        ...(driverEffortIds === undefined ? {} : { driverEffortIds }),
      });
    const known = { acpRegistry: ["thought_level"] };
    expect(acp([["thought_level", "high"]], known)).toMatchObject({ inside: true });
    expect(acp([["thought_level", "high"]], known, undefined)).toMatchObject({ inside: true });
    // The general id only: the agent never sees it and runs its own default.
    expect(acp([["effort", "high"]], known)).toEqual({
      inside: false,
      why: "acp-model names no effort under thought_level, the option acpRegistry reads it from (left out, the driver runs its default level)",
    });
    // Missing or empty catalog (not probed yet, or the model not listed there): not known, refused,
    // whatever the pick names, also the agent's own id beside an allowed general one.
    for (const missing of [undefined, {}, { acpRegistry: [] }, { cursor: ["effort"] }]) {
      for (const driver of ["acpRegistry", undefined]) {
        expect(
          acp(
            [
              ["effort", "high"],
              ["thought_level", "max"],
            ],
            missing,
            driver,
          ),
        ).toEqual({
          inside: false,
          why: "the option id acpRegistry reads acp-model's effort from is not known here (no acpRegistry model list here marks an effort control for acp-model), so the level it would run cannot be told",
          fix: "effort-id",
        });
      }
    }
    // A row without efforts asks for no id: a pick that names none is inside.
    expect(
      checkModelPolicyPick([{ driver: "acpRegistry", model: "plain" }] as never, {
        driver: "acpRegistry",
        model: "plain",
      }),
    ).toMatchObject({ inside: true });
  });

  it("a catalog id holding no effort name is refused, and a decoy beside it still counts", () => {
    const known = { acpRegistry: ["thought_level"] };
    expect(
      checkModelPolicyPick(acpRows, {
        driver: undefined,
        model: "acp-model",
        options: options(["thought_level", true]),
        driverEffortIds: known,
      }).inside,
    ).toBe(false);
    expect(
      checkModelPolicyPick(acpRows, {
        driver: undefined,
        model: "acp-model",
        options: options(["thought_level", "high"], ["reasoning", "max"]),
        driverEffortIds: known,
      }),
    ).toEqual({ inside: false, why: "acp-model does not allow effort high + max" });
  });

  it("an ultracode default a catalog may turn on counts both ways for a pick with no known driver", () => {
    const pick = (value: string) => ({
      driver: undefined,
      model: OPUS,
      options: options(["effort", value]),
      ultracodeMaybeDefault: true,
    });
    expect(modelPolicyPickEfforts(pick("xhigh"))).toEqual(["ultracode", "xhigh"]);
    expect(modelPolicyPickEfforts(pick("high"))).toEqual(["ultracode", "high"]);
    // The pick's own option wins over a default.
    expect(
      modelPolicyPickEfforts({
        ...pick("high"),
        options: [...options(["effort", "high"], ["ultracode", false])],
      }),
    ).toEqual(["high"]);
    const ultracodeRow = [{ driver: "claudeAgent", model: OPUS, efforts: ["ultracode"] }] as never;
    expect(checkModelPolicyPick(ultracodeRow, pick("xhigh")).inside).toBe(false);
    expect(checkModelPolicyPick(rows, pick("high")).inside).toBe(false);
  });
});

/** The refusal's note on a control only a linked machine's agent has. */
const LINKED_CONTROL =
  " (the pick runs on a linked machine: a control only the agent there has is refused here too, as this machine cannot tell whether it sets the effort; start that agent on this machine once so its model list describes it)";

describe("an option the policy cannot read, while a policy is on (#793 review)", () => {
  const kiroRows = [{ driver: "kiro", model: "k", efforts: ["high"] }] as never;
  const acpRows = [{ driver: "acpRegistry", model: "acp-model", efforts: ["high"] }] as never;
  const options = (...pairs: ReadonlyArray<readonly [string, unknown]>) =>
    pairs.map(([id, value]) => ({ id, value }));
  /** Each case: the rows, the pick (its lists' described ids given), and what refuses it. */
  const cases = [
    {
      name: "Kiro: a second effort id the driver applies after its own (effortLevel)",
      rows: kiroRows,
      pick: {
        model: "k",
        options: options(["reasoningEffort", "high"], ["effortLevel", "max"]),
        describedOptionIds: ["reasoningEffort"],
      },
      unreadable: "effortLevel",
    },
    {
      name: "ACP: the hidden session mode that mirrors the thought levels",
      rows: acpRows,
      pick: {
        model: "acp-model",
        options: options(["thought_level", "high"], ["_t3/session-mode", "max"]),
        effortOptionIds: ["thought_level"],
        driverEffortIds: { acpRegistry: ["thought_level"] },
        describedOptionIds: ["thought_level"],
      },
      unreadable: "_t3/session-mode",
    },
    {
      name: "Grok: the agent's native thought_level beside reasoningEffort",
      rows: [{ driver: "grok", model: "g", efforts: ["high"] }] as never,
      pick: {
        model: "g",
        options: options(["reasoningEffort", "high"], ["thought_level", "max"]),
        describedOptionIds: ["reasoningEffort"],
      },
      unreadable: "thought_level",
    },
  ];

  it("refuses it, on a known driver and on a pick for a linked machine; leaving it out is inside", () => {
    for (const { name, rows, pick, unreadable } of cases) {
      const driver = (rows as ReadonlyArray<{ driver: string }>)[0]!.driver;
      for (const known of [driver, undefined]) {
        // Runs again: the same verdict.
        for (const _ of [1, 2]) {
          expect(checkModelPolicyPick(rows, { ...pick, driver: known }), name).toEqual(
            unreadable === "_t3/session-mode"
              ? {
                  inside: false,
                  why: "the option _t3/session-mode picks an agent or a mode, which brings its own prompt, tools and settings (an agent can carry its own model) that the policy cannot see",
                  fix: "agent-option",
                }
              : {
                  inside: false,
                  why: `the option ${unreadable} is not described for ${pick.model} in this machine's model list and is not an effort id the policy reads, so the driver could apply it unchecked${known === undefined ? LINKED_CONTROL : ""}`,
                  fix: "option",
                },
          );
        }
        const without = pick.options.filter((option) => option.id !== unreadable);
        expect(
          checkModelPolicyPick(rows, { ...pick, driver: known, options: without }),
          name,
        ).toMatchObject({ inside: true });
      }
    }
  });

  it("a model option beside the allowed model is refused with the model field to use", () => {
    for (const driver of ["kiro", undefined]) {
      expect(
        checkModelPolicyPick(kiroRows, {
          driver,
          model: "k",
          options: options(["reasoningEffort", "high"], ["model", "model-in-no-row"]),
          describedOptionIds: ["reasoningEffort"],
        }),
      ).toEqual({
        inside: false,
        why: "the option model (beside k) would change the model after this check",
        fix: "model-option",
      });
    }
  });

  it("reads described ids, effort ids and ultracode; empty lists allow only those the policy reads", () => {
    const claudeRows = MODEL_POLICY_START_ROWS;
    const OPUS = "claude-opus-5-5";
    // Described for the model here: inside (fastMode changes neither the model nor its effort).
    expect(
      checkModelPolicyPick(claudeRows, {
        driver: "claudeAgent",
        model: OPUS,
        options: options(["effort", "high"], ["fastMode", true]),
        describedOptionIds: ["effort", "fastMode"],
      }),
    ).toMatchObject({ inside: true });
    // Old, empty or missing lists (no model list read yet, or the model not listed here): the
    // effort ids the policy reads and ultracode still pass, as their values are checked.
    for (const describedOptionIds of [[], undefined]) {
      expect(
        checkModelPolicyPick(claudeRows, {
          driver: "claudeAgent",
          model: OPUS,
          options: options(["effort", "high"], ["reasoning", "high"], ["ultracode", false]),
          describedOptionIds,
        }),
      ).toMatchObject({ inside: true });
    }
    expect(
      checkModelPolicyPick(claudeRows, {
        driver: "claudeAgent",
        model: OPUS,
        options: options(["effort", "high"], ["fastMode", true]),
        describedOptionIds: [],
      }),
    ).toMatchObject({ inside: false, fix: "option" });
    // Unset (Settings' marks of a person's pick): not checked.
    expect(
      checkModelPolicyPick(claudeRows, {
        driver: "claudeAgent",
        model: OPUS,
        options: options(["effort", "high"], ["effortLevel", "max"]),
      }),
    ).toMatchObject({ inside: true });
    // A model in no row is still told as such, before its options.
    expect(
      checkModelPolicyPick(claudeRows, {
        driver: "claudeAgent",
        model: "other",
        options: options(["effortLevel", "max"]),
        describedOptionIds: [],
      }),
    ).toEqual({ inside: false, why: "other (claudeAgent) is in no row" });
  });
});

describe("modelPolicyAliasTargets", () => {
  const OPUS = "claude-opus-5-5";
  it("names the model an alias names, case aside", () => {
    expect(modelPolicyAliasTargets([[{ slug: OPUS, aliases: ["opus"] }]], "Opus")).toEqual([OPUS]);
  });

  it("an id is never an alias: a name that is one model's id and another's alias names none", () => {
    // A custom model `opus` in one list, the catalog's `opus` alias in another.
    const lists = [[{ slug: "opus" }], [{ slug: OPUS, aliases: ["opus"] }]];
    expect(modelPolicyAliasTargets(lists, "opus")).toEqual([]);
    expect(modelPolicyAliasTargets([lists[1]!, lists[0]!], "opus")).toEqual([]);
    expect(modelPolicyAliasTargets(lists, OPUS)).toEqual([]);
  });
});

describe("#802: what the sending machine cannot know, and options that pick an agent", () => {
  const OPUS = "claude-opus-5-5";
  const rows = MODEL_POLICY_START_ROWS;
  const options = (...pairs: ReadonlyArray<readonly [string, unknown]>) =>
    pairs.map(([id, value]) => ({ id, value }));

  it("a pick for a linked machine that leaves ultracode out reads both ways on a Claude row", () => {
    const remote = (...pairs: ReadonlyArray<readonly [string, unknown]>) => ({
      driver: undefined,
      model: OPUS,
      options: options(...pairs),
      ultracodeDefaultUnknown: true,
    });
    // The machine there may turn ultracode on by default; its row does not allow ultracode.
    for (const _ of [1, 2]) {
      expect(checkModelPolicyPick(rows, remote(["effort", "high"]))).toEqual({
        inside: false,
        why: `the pick leaves the option ultracode out, and the machine that runs it may turn ultracode on by default for ${OPUS} (its model list is not read here), which its claudeAgent row does not allow`,
        fix: "ultracode",
      });
    }
    // Naming the option settles it, either way the row allows.
    expect(
      checkModelPolicyPick(rows, remote(["effort", "high"], ["ultracode", false])),
    ).toMatchObject({ inside: true });
    expect(checkModelPolicyPick(rows, remote(["effort", "high"], ["ultracode", true])).inside).toBe(
      false,
    );
    const ultracodeToo = [
      { driver: "claudeAgent", model: OPUS, efforts: ["high", "ultracode"] },
    ] as never;
    expect(checkModelPolicyPick(ultracodeToo, remote(["effort", "high"]))).toMatchObject({
      inside: true,
    });
    // A known driver reads its own catalog's default (pickOfSelection), and another driver's row
    // has no ultracode: the field changes neither.
    expect(
      checkModelPolicyPick(rows, { ...remote(["effort", "high"]), driver: "claudeAgent" }),
    ).toMatchObject({ inside: true });
    const codexRows = [{ driver: "codex", model: "gpt", efforts: ["high"] }] as never;
    expect(
      checkModelPolicyPick(codexRows, {
        ...remote(["reasoningEffort", "high"]),
        model: "gpt",
      }),
    ).toMatchObject({ inside: true });
  });

  it("several effort ids for one driver: the refusal names each and why (fail closed)", () => {
    const acpRows = [{ driver: "acpRegistry", model: "acp-model", efforts: ["high"] }] as never;
    const pick = (...pairs: ReadonlyArray<readonly [string, unknown]>) => ({
      driver: undefined,
      model: "acp-model",
      options: options(...pairs),
      driverEffortIds: { acpRegistry: ["thought_level", "effort_level"] },
    });
    expect(checkModelPolicyPick(acpRows, pick(["thought_level", "high"]))).toEqual({
      inside: false,
      why: "acp-model names no effort under effort_level, the option acpRegistry reads it from (left out, the driver runs its default level); acpRegistry reads acp-model's effort from each of thought_level, effort_level here (its instances' model lists name the control differently), and which instance runs the pick is not known, so the pick must name every one (an instance whose list does not describe an id refuses it in turn: pick a model whose driver has a fixed effort id, or start the task where one instance runs it)",
    });
    expect(
      checkModelPolicyPick(acpRows, pick(["thought_level", "high"], ["effort_level", "high"])),
    ).toMatchObject({ inside: true });
  });

  it("refuses an option that picks an agent or a mode while the policy is on, even when described", () => {
    const agentText = (id: string) => ({
      inside: false,
      why: `the option ${id} picks an agent or a mode, which brings its own prompt, tools and settings (an agent can carry its own model) that the policy cannot see`,
      fix: "agent-option",
    });
    const opencodeRows = [{ driver: "opencode", model: "oc", efforts: ["high"] }] as never;
    const opencode = (driver: string | undefined, ...extra: ReadonlyArray<[string, unknown]>) =>
      checkModelPolicyPick(opencodeRows, {
        driver,
        model: "oc",
        options: options(["variant", "high"], ...extra),
        describedOptionIds: ["variant", "agent"],
      });
    for (const driver of ["opencode", undefined]) {
      expect(opencode(driver, ["agent", "build"])).toEqual(agentText("agent"));
      expect(opencode(driver)).toMatchObject({ inside: true });
    }
    // A described ACP session mode (its modes are not a copy of the thought levels), any driver.
    const acpRows = [{ driver: "acpRegistry", model: "acp-model", efforts: ["high"] }] as never;
    for (const driver of ["acpRegistry", undefined]) {
      expect(
        checkModelPolicyPick(acpRows, {
          driver,
          model: "acp-model",
          options: options(["thought_level", "high"], ["_t3/session-mode", "plan"]),
          driverEffortIds: { acpRegistry: ["thought_level"] },
          describedOptionIds: ["thought_level", "_t3/session-mode"],
        }),
      ).toEqual(agentText("_t3/session-mode"));
    }
    // Kiro mode: on Kiro, and on a pick whose driver is not known; another driver's own `mode`
    // stays an ordinary option.
    const kiroRows = [{ driver: "kiro", model: "k", efforts: ["high"] }] as never;
    for (const driver of ["kiro", undefined]) {
      expect(
        checkModelPolicyPick(kiroRows, {
          driver,
          model: "k",
          options: options(["reasoningEffort", "high"], ["mode", "x"]),
          describedOptionIds: ["reasoningEffort", "mode"],
        }),
      ).toEqual(agentText("mode"));
    }
    expect(
      checkModelPolicyPick([{ driver: "codex", model: "gpt", efforts: ["high"] }] as never, {
        driver: "codex",
        model: "gpt",
        options: options(["reasoningEffort", "high"], ["mode", "x"]),
        describedOptionIds: ["reasoningEffort", "mode"],
      }),
    ).toMatchObject({ inside: true });
    // Settings' marks (no described ids): not checked, as before.
    expect(
      checkModelPolicyPick(opencodeRows, {
        driver: "opencode",
        model: "oc",
        options: options(["variant", "high"], ["agent", "build"]),
      }),
    ).toMatchObject({ inside: true });
  });
});
