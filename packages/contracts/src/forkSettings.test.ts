/// <reference types="vite-plus/client" />

import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { ProjectId } from "./baseSchemas.ts";
import {
  FORK_AGENT_WRITABLE_KEYS,
  FORK_INLINE_SERVER_KEYS,
  FORK_PATCH_RULES,
  FORK_PROJECT_SCOPED_KEYS,
  ForkClientSettingsFields,
  ForkProjectOverrideFields,
  ForkServerSettingsFields,
  ForkServerSettingsPatchFields,
} from "./forkSettings.ts";
// The source texts, to pin the fork's lines in upstream's file and keep forkSettings.ts its own.
import FORK_SOURCE from "./forkSettings.ts?raw";
import {
  ClientSettingsSchema,
  PROJECT_SCOPED_SERVER_SETTING_KEYS,
  ProjectSettingsOverrides,
  ServerSettings,
  ServerSettingsPatch,
} from "./settings.ts";
import SETTINGS_SOURCE from "./settings.ts?raw";

const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const PROJECT = ProjectId.make("project-1");

const sameFields = (
  struct: Readonly<Record<string, unknown>>,
  fork: Readonly<Record<string, unknown>>,
) => {
  for (const [key, field] of Object.entries(fork)) {
    // The same object: upstream's struct has no key of its own that hides the fork's.
    expect(struct[key], key).toBe(field);
  }
};

describe("fork settings seam", () => {
  it("puts every fork field into upstream's structs unchanged", () => {
    sameFields(ServerSettings.fields, ForkServerSettingsFields);
    sameFields(ServerSettingsPatch.fields, ForkServerSettingsPatchFields);
    sameFields(ProjectSettingsOverrides.fields, ForkProjectOverrideFields);
    sameFields(ClientSettingsSchema.fields, ForkClientSettingsFields);
    for (const key of FORK_PROJECT_SCOPED_KEYS) {
      expect(PROJECT_SCOPED_SERVER_SETTING_KEYS).toContain(key);
    }
    for (const key of FORK_INLINE_SERVER_KEYS) {
      expect(Object.keys(ServerSettings.fields)).toContain(key);
      expect(Object.keys(ServerSettingsPatch.fields)).toContain(key);
      expect(Object.keys(ForkServerSettingsFields)).not.toContain(key);
    }
  });

  it("takes a project's fork overrides in a patch, refusing a bad check", () => {
    const patch = (waitChecks: unknown) =>
      decodePatch({
        projectSettingsOverrides: { [PROJECT]: { waitChecks } },
      });
    const good = { "lint-ok": { argv: ["/bin/true"] } };
    expect(patch(good).projectSettingsOverrides?.[PROJECT]).toMatchObject({ waitChecks: good });
    expect(() => patch({ "Bad Name": { argv: ["/bin/true"] } })).toThrow();
  });

  it("gives every fork setting a patch field and a patch rule", () => {
    const keys = Object.keys(ForkServerSettingsFields).toSorted();
    expect(Object.keys(ForkServerSettingsPatchFields).toSorted()).toEqual(keys);
    expect(Object.keys(FORK_PATCH_RULES).toSorted()).toEqual(keys);
    expect(Object.keys(ForkProjectOverrideFields).toSorted()).toEqual(
      [...FORK_PROJECT_SCOPED_KEYS].toSorted(),
    );
    // No agent tool may write a fork setting until a pull request gives the reason.
    expect(FORK_AGENT_WRITABLE_KEYS).toEqual([]);
  });

  it("pins the fork's lines in settings.ts", () => {
    const lines = SETTINGS_SOURCE.split("\n");
    // The import, `export *` and six spreads.
    expect(lines.filter((line) => /forkSettings|ForkSettings/.test(line))).toHaveLength(8);
    // The inline `automaticUpdates`: its comment, its field and its patch field.
    expect(lines.filter((line) => line.includes("Fork:"))).toHaveLength(1);
    expect(lines.filter((line) => /^ {2}automaticUpdates:/.test(line))).toHaveLength(2);
    // forkSettings.ts never imports settings.ts, so upstream's file stays the only one with seams.
    expect(FORK_SOURCE).not.toMatch(/from "\.\/settings(\.ts)?"/);
  });
});
