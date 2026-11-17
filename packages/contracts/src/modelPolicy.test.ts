/// <reference types="vite-plus/client" />

import * as Effect from "effect/Effect";
import * as Logger from "effect/Logger";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "@effect/vitest";

import {
  DEFAULT_MODEL_POLICY,
  MODEL_POLICY_FAIL_CLOSED,
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
