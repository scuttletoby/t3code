/// <reference types="vite-plus/client" />

import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { ProjectId } from "./baseSchemas.ts";
import {
  FORK_AGENT_WRITABLE_KEYS,
  FORK_INLINE_SERVER_KEYS,
  FORK_PATCH_RULES,
  FORK_PERSON_ONLY_KEYS,
  FORK_PROJECT_SCOPED_KEYS,
  ForkClientSettingsFields,
  ForkClientSettingsPatchFields,
  ForkProjectOverrideFields,
  ForkServerSettingsFields,
  ForkServerSettingsPatchFields,
} from "./forkSettings.ts";
// The source texts, to pin the fork's lines in upstream's file and keep forkSettings.ts its own.
import FORK_SOURCE from "./forkSettings.ts?raw";
import {
  ClientSettingsPatch,
  ClientSettingsSchema,
  PROJECT_SCOPED_SERVER_SETTING_KEYS,
  ProjectSettingsOverrides,
  ServerSettings,
  ServerSettingsPatch,
} from "./settings.ts";
import SETTINGS_SOURCE from "./settings.ts?raw";
import { PROJECT_FILE_BACKED_SETTINGS, T3ProjectFile } from "./t3ProjectFile.ts";

const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const decodeProjectOverrides = Schema.decodeUnknownSync(ProjectSettingsOverrides);
const decodeServerSettings = Schema.decodeUnknownSync(ServerSettings);
const encodeServerSettings = Schema.encodeSync(ServerSettings);
const PROJECT = ProjectId.make("project-1");

/**
 * Person-only keys that a repository's t3.json could set. That file is in the repo, so an agent
 * edits it like any other file: a key read from it is agent-writable, whatever the tools allow.
 */
const personOnlyKeysFromRepoFile = (
  fileFields: Readonly<Record<string, unknown>>,
  backed: Readonly<Record<string, { readonly field: string }>>,
) =>
  FORK_PERSON_ONLY_KEYS.filter(
    (key) =>
      key in fileFields ||
      key in backed ||
      Object.values(backed).some((setting) => setting.field === key),
  );

const sameFields = (
  struct: Readonly<Record<string, unknown>>,
  fork: Readonly<Record<string, unknown>>,
) => {
  for (const [key, field] of Object.entries(fork)) {
    // The same object: upstream's struct has no key of its own that hides the fork's.
    expect(struct[key], key).toBe(field);
  }
};

// Does a device's client settings file, old or new, open the owner-questions page, and can a save
// turn it off (fork issue 828)?
describe("ownerQuestionsPage", () => {
  const decodeClient = Schema.decodeUnknownSync(ClientSettingsSchema);
  const decodeClientPatch = Schema.decodeUnknownSync(ClientSettingsPatch);

  it("is on for a device whose file never named it", () => {
    expect(decodeClient({}).ownerQuestionsPage).toBe(true);
  });

  it("keeps a device's saved off", () => {
    expect(decodeClient({ ownerQuestionsPage: false }).ownerQuestionsPage).toBe(false);
  });

  it("is a field a settings save may name", () => {
    expect(decodeClientPatch({ ownerQuestionsPage: false })).toEqual({ ownerQuestionsPage: false });
    expect(() => decodeClientPatch({ ownerQuestionsPage: "no" })).toThrow();
  });
});

describe("fork settings seam", () => {
  it("every client fork setting has its patch field, so a device can save it", () => {
    expect(Object.keys(ForkClientSettingsPatchFields).toSorted()).toEqual(
      Object.keys(ForkClientSettingsFields).toSorted(),
    );
  });

  it("puts every fork field into upstream's structs unchanged", () => {
    sameFields(ServerSettings.fields, ForkServerSettingsFields);
    sameFields(ServerSettingsPatch.fields, ForkServerSettingsPatchFields);
    sameFields(ProjectSettingsOverrides.fields, ForkProjectOverrideFields);
    sameFields(ClientSettingsSchema.fields, ForkClientSettingsFields);
    // A device saves its fork settings with upstream's client patch.
    sameFields(ClientSettingsPatch.fields, ForkClientSettingsPatchFields);
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

  it("reads a project's hand-written null as left out (the server's), never as off or null", () => {
    // `null` is the save's "clear", so a file that holds it means the same thing. It reads as the
    // key left out: a `null` kept would reach readers as the project's own value.
    for (const key of ["playtest", "handoff", "reviewRules", "placementNeeds"] as const) {
      const project = decodeProjectOverrides({ [key]: null, defaultAutoPull: false });
      expect(Object.hasOwn(project, key), key).toBe(false);
      const settings = decodeServerSettings({
        projectSettingsOverrides: { [PROJECT]: { [key]: null, defaultAutoPull: false } },
      });
      expect(Object.hasOwn(settings.projectSettingsOverrides[PROJECT] ?? {}, key), key).toBe(false);
      // A save of another key writes the file without it (the same meaning), and so does the next.
      const written = encodeServerSettings(settings).projectSettingsOverrides?.[PROJECT];
      expect(written, key).toEqual({ defaultAutoPull: false });
      const again = encodeServerSettings(
        decodeServerSettings({ projectSettingsOverrides: { [PROJECT]: written } }),
      ).projectSettingsOverrides?.[PROJECT];
      expect(again, key).toEqual(written);
      // A project entry that held only the `null` reads as empty.
      expect(decodeProjectOverrides({ [key]: null }), key).toEqual({});
    }
  });

  it("never writes a project's null to the file, while a patch's null (the clear) goes over the wire", () => {
    const encodePatch = Schema.encodeSync(ServerSettingsPatch);
    for (const key of ["playtest", "handoff", "reviewRules", "placementNeeds"] as const) {
      // No path puts a `null` in memory today; if one ever did, the save drops it (the same
      // meaning: the server's value), so it never reaches the file to be read back.
      const inMemory = (entry: Record<string, unknown>) =>
        ({
          ...decodeServerSettings({}),
          projectSettingsOverrides: { [PROJECT]: entry },
        }) as unknown as ServerSettings;
      const written = encodeServerSettings(inMemory({ [key]: null, defaultAutoPull: false }))
        .projectSettingsOverrides?.[PROJECT];
      expect(written, key).toEqual({ defaultAutoPull: false });
      expect(
        encodeServerSettings(inMemory({ [key]: null })).projectSettingsOverrides?.[PROJECT],
        key,
      ).toEqual({});
      // The written file reads back the same, and writes the same again (runs again).
      const reread = decodeServerSettings(
        JSON.parse(JSON.stringify({ projectSettingsOverrides: { [PROJECT]: written } })),
      );
      expect(encodeServerSettings(reread).projectSettingsOverrides?.[PROJECT], key).toEqual(
        written,
      );
      // The patch has its own schema for these keys: a client's clear keeps its `null` both ways.
      const clear = { projectSettingsOverrides: { [PROJECT]: { [key]: null } } };
      const sent = JSON.parse(JSON.stringify(encodePatch(decodePatch(clear))));
      expect(sent, key).toEqual(clear);
      expect(decodePatch(sent).projectSettingsOverrides?.[PROJECT], key).toEqual({ [key]: null });
    }
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

  it("reads no person-only setting from the repository's t3.json", () => {
    expect(personOnlyKeysFromRepoFile(T3ProjectFile.fields, PROJECT_FILE_BACKED_SETTINGS)).toEqual(
      [],
    );
    // The check catches one: a t3.json field or a file-backed setting named like a fork key.
    expect(personOnlyKeysFromRepoFile({ ...T3ProjectFile.fields, playtest: {} }, {})).toEqual([
      "playtest",
    ]);
    expect(personOnlyKeysFromRepoFile({}, { waitChecks: { field: "scripts" } })).toEqual([
      "waitChecks",
    ]);
  });

  it("pins the fork's lines in settings.ts", () => {
    const lines = SETTINGS_SOURCE.split("\n");
    // The import, `export *` and seven spreads.
    expect(lines.filter((line) => /forkSettings|ForkSettings/.test(line))).toHaveLength(9);
    // The inline `automaticUpdates`: its comment, its field and its patch field.
    expect(lines.filter((line) => line.includes("Fork:"))).toHaveLength(1);
    expect(lines.filter((line) => /^ {2}automaticUpdates:/.test(line))).toHaveLength(2);
    // forkSettings.ts never imports settings.ts, so upstream's file stays the only one with seams.
    expect(FORK_SOURCE).not.toMatch(/from "\.\/settings(\.ts)?"/);
  });
});
