/// <reference types="vite-plus/client" />

import * as Effect from "effect/Effect";
import * as Logger from "effect/Logger";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "@effect/vitest";

import {
  DEFAULT_MODEL_POLICY,
  MODEL_POLICY_FAIL_CLOSED,
  ModelPolicy,
  ModelPolicyFromSettingsFile,
} from "./modelPolicy.ts";
import { ServerSettings, ServerSettingsPatch } from "./settings.ts";

const decodeStrict = Schema.decodeUnknownSync(ModelPolicy);
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
      { mode: "advise", allow: ROW },
      "off",
      null,
      [],
    ]) {
      expect(decodeFile(bad), JSON.stringify(bad)).toEqual(MODEL_POLICY_FAIL_CLOSED);
      const settings = decodeServer({ enableAgentBrowserAccess: false, modelPolicy: bad });
      expect(settings.modelPolicy, JSON.stringify(bad)).toEqual(MODEL_POLICY_FAIL_CLOSED);
      expect(settings.enableAgentBrowserAccess).toBe(false);
    }
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
});
