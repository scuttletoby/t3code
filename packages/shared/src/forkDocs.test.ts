// @effect-diagnostics nodeBuiltinImport:off - reads the fork's docs from the repository.
/// <reference types="vite-plus/client" />

// Fork: the fork's settings docs follow the one pin table (forkDefaults.ts), so a new fork setting
// without a docs row, or a docs row for a setting that does not exist, fails here.
import { FORK_SETTING_DEFAULTS, type ForkSettingDefault } from "@t3tools/contracts/forkDefaults";
import * as NodeFS from "node:fs";
import { describe, expect, it } from "vite-plus/test";

const repoFile = (path: string) =>
  NodeFS.readFileSync(new URL(`../../../${path}`, import.meta.url), "utf8");

const README = repoFile("docs/fork/README.md");
const CHANGES = repoFile("FORK_CHANGES.md");

const rowId = (row: ForkSettingDefault) => `${row.scope}.${row.key}`;
const pinned = FORK_SETTING_DEFAULTS.map(rowId).toSorted();
const byId = new Map(FORK_SETTING_DEFAULTS.map((row) => [rowId(row), row] as const));

/** A setting as the docs write it: `server.<key>`, `projectOverride.<key>` or `client.<key>`. */
const SETTING_ID = /`((?:server|projectOverride|client)\.[A-Za-z0-9]+)`/gu;

/** The lines of one `## ` section, without its heading. */
function section(markdown: string, heading: string): ReadonlyArray<string> {
  const lines = markdown.split("\n");
  const start = lines.indexOf(`## ${heading}`);
  if (start === -1) throw new Error(`docs/fork/README.md has no "## ${heading}" section`);
  const end = lines.findIndex((line, index) => index > start && line.startsWith("## "));
  return lines.slice(start + 1, end === -1 ? undefined : end);
}

interface TableRow {
  readonly line: string;
  readonly cells: ReadonlyArray<string>;
}

/** Body rows of the first table in `lines` (no header, no separator). */
function tableRows(lines: ReadonlyArray<string>): ReadonlyArray<TableRow> {
  const first = lines.findIndex((line) => line.startsWith("|"));
  if (first === -1) return [];
  const table = lines.slice(first);
  const last = table.findIndex((line) => !line.startsWith("|"));
  return table.slice(2, last === -1 ? undefined : last).map((line) => ({
    line,
    cells: line
      .split("|")
      .slice(1, -1)
      .map((cell) => cell.trim()),
  }));
}

/** The default cell as the README writes it: the settings file's JSON, or "not set". */
const defaultCell = (row: ForkSettingDefault) =>
  row.default === undefined ? "not set" : `\`${JSON.stringify(row.default)}\``;

const settingRows = tableRows(section(README, "Settings"));

describe("fork settings docs (docs/fork/README.md)", () => {
  it("has one table row for each fork setting and none for anything else", () => {
    // A row whose first cell names no setting counts as "(no setting)" and fails the comparison.
    const ids = settingRows.map(
      (row) => [...row.cells[0]!.matchAll(SETTING_ID)][0]?.[1] ?? row.line,
    );
    expect(ids.toSorted()).toEqual(pinned);
  });

  it.each(settingRows.map((row) => [row.cells[0]!, row] as const))(
    "row %s matches its pin",
    (_label, { cells }) => {
      const id = [...cells[0]!.matchAll(SETTING_ID)][0]?.[1] ?? "";
      const row = byId.get(id);
      expect(row, `no fork setting ${id}`).toBeDefined();
      if (row === undefined) return;
      expect(cells[0]).toBe(`[\`${id}\`](#${id.toLowerCase().replace(".", "")})`);
      expect(cells[1]).toBe(defaultCell(row));
      expect(cells[2]).toBe(row.off ? "yes" : "no");
      expect(cells[3]).toBe(
        `[${row.decidedIn}](https://github.com/scuttletoby/t3code/issues/${row.decidedIn.slice(1)})`,
      );
    },
  );

  it("has one section for each fork setting", () => {
    const headings = section(README, "Each setting")
      .map((line) => /^### `([^`]+)`$/u.exec(line)?.[1])
      .filter((id) => id !== undefined);
    expect(headings.toSorted()).toEqual(pinned);
  });
});

describe("fork changes list (FORK_CHANGES.md)", () => {
  const named = new Set([...CHANGES.matchAll(SETTING_ID)].map((match) => match[1]!));

  it("names every fork setting", () => {
    expect(pinned.filter((id) => !named.has(id))).toEqual([]);
  });

  it("names no setting that does not exist", () => {
    expect([...named].filter((id) => !byId.has(id))).toEqual([]);
  });
});
