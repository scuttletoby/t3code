/// <reference types="vite-plus/client" />

import * as Effect from "effect/Effect";
import * as Logger from "effect/Logger";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "@effect/vitest";

import {
  DEFAULT_REVIEW_RULES,
  REVIEW_RULES_RECOMMENDED,
  ReviewRulesFromSettingsFile,
  ReviewRulesPatch,
  type ReviewRules,
  effectiveReviewRules,
} from "./reviewRules.ts";
import { ProjectSettingsOverrides, ServerSettings, ServerSettingsPatch } from "./settings.ts";

const decodeServer = Schema.decodeUnknownSync(ServerSettings);
const encodeServer = Schema.encodeSync(ServerSettings);
const decodeProject = Schema.decodeUnknownSync(ProjectSettingsOverrides);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const decodeFileEffect = Schema.decodeUnknownEffect(ReviewRulesFromSettingsFile);
const decodeStrict = Schema.decodeUnknownSync(ReviewRulesPatch);

const RUNNER = { driver: "codex", model: "gpt-review", effort: "medium" };
const ON = { preset: "recommended" } as const;
const QUESTION = "What breaks when the network drops in the middle of a save?";
const RECOMMENDED = { preset: "recommended", ...REVIEW_RULES_RECOMMENDED };

/** Bad values: each reads as off, never half on, and is kept as written. */
const BAD: ReadonlyArray<readonly [string, unknown]> = [
  ["an unknown preset from a newer build", { preset: "strict" }],
  ["a key from a newer build", { ...ON, lenses: ["review"] }],
  ["a misspelt key", { ...ON, statequestion: QUESTION }],
  ["a runner without a model", { ...ON, runner: { driver: "claudeAgent", effort: "high" } }],
  ["a runner with a misspelt key", { ...ON, runner: { ...RUNNER, efort: "high" } }],
  [
    "a lens runner without an effort",
    { ...ON, lensRunners: { approach: { driver: "codex", model: "m" } } },
  ],
  ["a bad lens name", { ...ON, lensRunners: { Approach: RUNNER } }],
  ["a short state question", { ...ON, stateQuestion: "  Does it rerun?        " }],
  ["a long state question", { ...ON, stateQuestion: "x".repeat(601) }],
  ["an unknown minors", { ...ON, minors: "later" }],
  ["a path out of the repo", { ...ON, configPath: "../other/pr-queue.json" }],
  ["an absolute path", { ...ON, configPath: "/etc/pr-queue.json" }],
  ["a drive path", { ...ON, configPath: "C:\\pr-queue.json" }],
  ["a report cap too large", { ...ON, reportMaxChars: 70000 }],
  ["a report cap that is not whole", { ...ON, reportMaxChars: 1500.5 }],
  ["a bare string", "recommended"],
  ["null", null],
  ["an array", []],
];

describe("reviewRules in the settings file", () => {
  it("reads an old file without the key, and an empty value, as off", () => {
    expect(decodeServer({}).reviewRules).toEqual(DEFAULT_REVIEW_RULES);
    expect(decodeServer({ reviewRules: {} }).reviewRules).toEqual({ preset: "off" });
    expect(effectiveReviewRules(decodeServer({}).reviewRules)).toEqual({ preset: "off" });
  });

  it("turns the preset on with one value, and keeps it through a save", () => {
    const settings = decodeServer({ reviewRules: ON });
    expect(effectiveReviewRules(settings.reviewRules)).toEqual(RECOMMENDED);
    expect(encodeServer(settings).reviewRules).toEqual(ON);
    // The writer saves sparse: an off value with a field written out loses only `preset`.
    expect(decodeServer({ reviewRules: { stateQuestion: QUESTION } }).reviewRules).toEqual({
      preset: "off",
      stateQuestion: QUESTION,
    });
  });

  it.each(BAD)("reads %s as off, keeps every other setting, and saves it back", (_name, bad) => {
    const settings = decodeServer({ enableAgentBrowserAccess: false, reviewRules: bad });
    expect(settings.reviewRules.preset).toBe("off");
    expect(effectiveReviewRules(settings.reviewRules)).toEqual({ preset: "off" });
    expect(settings.enableAgentBrowserAccess).toBe(false);
    // A save of another key encodes the settings; twice in a row, still as written.
    const saved = encodeServer(settings);
    expect(saved.reviewRules).toEqual(bad);
    expect(encodeServer(decodeServer(saved)).reviewRules).toEqual(bad);
  });

  it("reads a bad project value as off for that project, never as the server's", () => {
    const project = decodeProject({ reviewRules: { preset: "strict" } }).reviewRules;
    const server = decodeServer({ reviewRules: ON }).reviewRules;
    expect(effectiveReviewRules(server, project)).toEqual({ preset: "off" });
  });

  it.effect("warns when it reads a bad value", () =>
    Effect.gen(function* () {
      const logged: Array<unknown> = [];
      const logger = Logger.make(({ message }) => logged.push(message));
      yield* decodeFileEffect({ preset: "strict" }).pipe(
        Effect.provide(Logger.layer([logger], { mergeWithExisting: false })),
      );
      expect(JSON.stringify(logged)).toContain("invalid reviewRules read as off");
    }),
  );
});

describe("effectiveReviewRules", () => {
  const read = (value: unknown): ReviewRules => decodeServer({ reviewRules: value }).reviewRules;
  const CASES: ReadonlyArray<{
    readonly name: string;
    readonly server?: unknown;
    readonly project?: unknown;
    readonly expected: unknown;
  }> = [
    { name: "no value", expected: { preset: "off" } },
    { name: "off", server: { preset: "off" }, expected: { preset: "off" } },
    {
      name: "off with fields written out",
      server: { preset: "off", runner: RUNNER, stateQuestion: QUESTION },
      expected: { preset: "off" },
    },
    { name: "the preset alone", server: ON, expected: RECOMMENDED },
    {
      name: "explicit fields over the preset",
      server: { ...ON, runner: RUNNER, minors: "same-round", reportMaxChars: 2000 },
      expected: { ...RECOMMENDED, runner: RUNNER, minors: "same-round", reportMaxChars: 2000 },
    },
    {
      name: "a lens runner beside the preset's",
      server: { ...ON, lensRunners: { security: RUNNER } },
      expected: {
        ...RECOMMENDED,
        lensRunners: { ...REVIEW_RULES_RECOMMENDED.lensRunners, security: RUNNER },
      },
    },
    {
      name: "a lens runner in place of the preset's",
      server: { ...ON, lensRunners: { approach: RUNNER } },
      expected: { ...RECOMMENDED, lensRunners: { approach: RUNNER } },
    },
    {
      name: "a project that turns it off",
      server: ON,
      project: { preset: "off" },
      expected: { preset: "off" },
    },
    {
      name: "a project that turns it on",
      server: { preset: "off" },
      project: ON,
      expected: RECOMMENDED,
    },
    {
      name: "a project value, which replaces the server's whole",
      server: { ...ON, runner: RUNNER, stateQuestion: QUESTION },
      project: { ...ON, minors: "free" },
      expected: { ...RECOMMENDED, minors: "free" },
    },
    {
      name: "an unreadable server value",
      server: { preset: "strict" },
      expected: { preset: "off" },
    },
  ];

  it.each(CASES)("resolves $name", ({ server, project, expected }) => {
    expect(
      effectiveReviewRules(
        server === undefined ? undefined : read(server),
        project === undefined ? undefined : decodeProject({ reviewRules: project }).reviewRules,
      ),
    ).toEqual(expected);
  });

  it("treats a project's null as no override", () => {
    expect(effectiveReviewRules(read(ON), null)).toEqual(RECOMMENDED);
  });
});

describe("reviewRules saves", () => {
  it("takes the recommended preset written out in full", () => {
    expect(decodeStrict(RECOMMENDED)).toEqual(RECOMMENDED);
    expect(decodePatch({ reviewRules: ON }).reviewRules).toEqual(ON);
  });

  it.each(BAD)("refuses %s", (_name, bad) => {
    expect(() => decodePatch({ reviewRules: bad })).toThrow();
  });

  it("refuses a save without a preset, so it never turns the rules off by leaving it out", () => {
    for (const partial of [{}, { stateQuestion: QUESTION }]) {
      expect(() => decodePatch({ reviewRules: partial }), JSON.stringify(partial)).toThrow();
    }
  });

  it("takes a project's value back as a client resends it, unreadable text included", () => {
    const project = (reviewRules: unknown) =>
      Object.values(
        decodePatch({ projectSettingsOverrides: { p: { reviewRules } } })
          .projectSettingsOverrides ?? {},
      )[0]?.reviewRules;
    expect(project(ON)).toEqual(ON);
    expect(project(null)).toBeNull();
    const kept = decodeProject({ reviewRules: { preset: "strict" } }).reviewRules;
    expect(project(kept)).toEqual({ preset: "off", unreadable: { preset: "strict" } });
    // Never a way to turn the rules on: kept text that would read as a good value is refused.
    expect(() => project({ preset: "off", unreadable: ON })).toThrow();
    expect(() => project({ preset: "recommended", unreadable: { preset: "strict" } })).toThrow();
    expect(() => project({ preset: "strict" })).toThrow();
  });
});
