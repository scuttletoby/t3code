/// <reference types="vite-plus/client" />

import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  FORK_SETTING_DEFAULTS,
  type ForkSettingScope,
  forkSettingAsSaved,
  pinnedPart,
} from "./forkDefaults.ts";
import { ClientSettingsSchema, ProjectSettingsOverrides, ServerSettings } from "./settings.ts";
// The source text of the same file, to read the `Fork:` comments.
import SOURCE from "./settings.ts?raw";

// The fork's settings are found in the code, not listed by hand: each top-level key of these
// structs whose comment says `Fork:` is one. So a new fork setting without a row fails here.
const STRUCTS: ReadonlyArray<{
  readonly scope: ForkSettingScope;
  readonly name: string;
  readonly fields: Readonly<Record<string, unknown>>;
}> = [
  { scope: "server", name: "ServerSettings", fields: ServerSettings.fields },
  {
    scope: "projectOverride",
    name: "ProjectSettingsOverrides",
    fields: ProjectSettingsOverrides.fields,
  },
  { scope: "client", name: "ClientSettingsSchema", fields: ClientSettingsSchema.fields },
];

/** Each top-level key of `export const <name> = Schema.Struct({`, with the comment right above it. */
function structKeys(
  name: string,
): ReadonlyArray<{ readonly key: string; readonly comment: string }> {
  const lines = SOURCE.split("\n");
  const start = lines.indexOf(`export const ${name} = Schema.Struct({`);
  if (start === -1) throw new Error(`settings.ts has no struct ${name}`);
  const keys: Array<{ key: string; comment: string }> = [];
  let comment: Array<string> = [];
  for (const line of lines.slice(start + 1)) {
    // The struct ends at the first line back at column 0: `});` or `} satisfies …);`.
    if (line.startsWith("}")) return keys;
    if (/^ {2}(\/\/|\/\*)|^ {3}\*/.test(line)) {
      comment.push(line);
      continue;
    }
    const key = /^ {2}(\w+):/.exec(line)?.[1];
    if (key !== undefined) keys.push({ key, comment: comment.join("\n") });
    // A deeper line continues the key above; anything else at this depth ends a comment.
    if (!line.startsWith("   ")) comment = [];
  }
  throw new Error(`settings.ts struct ${name} has no end`);
}

const discovered = STRUCTS.flatMap(({ scope, name }) =>
  structKeys(name)
    .filter(({ comment }) => comment.includes("Fork:"))
    .map(({ key }) => `${scope}.${key}`),
);
const rowId = (row: { readonly scope: string; readonly key: string }) => `${row.scope}.${row.key}`;

const decoded: Readonly<Record<ForkSettingScope, Readonly<Record<string, unknown>>>> = {
  server: Schema.decodeSync(ServerSettings)({}),
  projectOverride: Schema.decodeSync(ProjectSettingsOverrides)({}),
  client: Schema.decodeSync(ClientSettingsSchema)({}),
};

describe("fork setting defaults", () => {
  it("reads every key of each settings struct from the source", () => {
    // Guards the reader itself: a formatting change it cannot follow fails here, not silently.
    for (const { name, fields } of STRUCTS) {
      expect(structKeys(name).map(({ key }) => key)).toEqual(Object.keys(fields));
    }
  });

  it("finds the fork's settings by their comment", () => {
    expect(discovered).toContain("server.automaticUpdates");
    expect(discovered).toContain("projectOverride.waitChecks");
  });

  it("has exactly one row for each fork setting", () => {
    const rows = FORK_SETTING_DEFAULTS.map(rowId);
    expect(new Set(rows).size).toBe(rows.length);
    // A setting missing here needs a row in forkDefaults.ts; a row missing in the code is stale.
    expect([...rows].toSorted()).toEqual([...discovered].toSorted());
  });

  it.each(FORK_SETTING_DEFAULTS.map((row) => [rowId(row), row] as const))(
    "pins the default of %s",
    (_id, row) => {
      const saved = forkSettingAsSaved(row, decoded[row.scope][row.key]);
      expect(pinnedPart(row, saved)).toEqual(row.default);
      expect(row.reason.trim()).not.toBe("");
      expect(row.decidedIn).toMatch(/^#\d+$/);
    },
  );

  it("keeps every fork switch off by default but the read-only usage refresh", () => {
    expect(FORK_SETTING_DEFAULTS.filter((row) => !row.off).map(rowId)).toEqual([
      "server.headlessUsageRefreshInterval",
    ]);
  });
});
