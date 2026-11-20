// Fork only: FORK_CHANGES.md names every fork setting of the pin table (packages/contracts/src/forkDefaults.ts,
// read as text) and no setting that does not exist. Run by fork-changes-list.test.cjs in Fork tests' `workflows`
// job, which runs on every pull request, so FORK_CHANGES.md can be fork-owned (.github/fork-owned-paths.txt) and a
// fork feature pull request that only adds its line there still skips the app jobs.
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const CHANGES = "FORK_CHANGES.md";
const DEFAULTS = "packages/contracts/src/forkDefaults.ts";
const SCOPES = ["server", "projectOverride", "client"];

/** A setting as the docs write it: `server.<key>`, `projectOverride.<key>` or `client.<key>`. */
const SETTING_ID = /`((?:server|projectOverride|client)\.[A-Za-z0-9]+)`/gu;

/** A row of the table as the formatter writes it: `{` on its own line, or the whole row on one line. */
const ROW_OPEN = /^ {2}\{$/u;
const ROW_CLOSE = /^ {2}\},?$/u;
const ROW_ONE_LINE = /^ {2}\{.*\},?$/u;
/** A line at the rows' own indent: inside a row, only its closing `},` may be there. */
const ROW_INDENT = /^ {2}\S/u;
const ROW_SCOPE_KEY = /\bscope:\s*(["'])(\w+)\1,\s*key:\s*(["'])([A-Za-z0-9]+)\3/gu;

/**
 * A row's text without its comments (`code`), and also without the contents of its strings (`bare`), so neither a
 * comment nor a string can stand in for the row's own `scope:`, spread or braces.
 */
function stripRow(text) {
  let code = "";
  let bare = "";
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (char === "/" && (text[index + 1] === "/" || text[index + 1] === "*")) {
      const close = text[index + 1] === "/" ? "\n" : "*/";
      const end = text.indexOf(close, index + 2);
      index = end === -1 ? text.length : close === "\n" ? end - 1 : end + 1;
      code += " ";
      bare += " ";
    } else if (char === '"' || char === "'" || char === "`") {
      let end = index + 1;
      while (end < text.length && text[end] !== char) end += text[end] === "\\" ? 2 : 1;
      code += text.slice(index, end + 1);
      bare += char + char;
      index = end;
    } else {
      code += char;
      bare += char;
    }
  }
  return { code, bare };
}

/**
 * `<scope>.<key>` of each row of FORK_SETTING_DEFAULTS in forkDefaults.ts's text. Fails closed, so no row goes
 * unchecked: throws on a missing or empty table, an entry between rows that is not a `{ ... }` row (spread, helper
 * call), a row whose closing `},` line is not found, a row still open at the `];`, a spread or a second object on a
 * row's line, or a row that is not exactly one `scope: "<scope>", key: "<key>"` (comments not counted).
 */
function pinnedIds(text) {
  const head = /^export const FORK_SETTING_DEFAULTS\b[^=]*=\s*\[/mu.exec(text);
  if (head === null)
    throw new Error(`${DEFAULTS}: no "export const FORK_SETTING_DEFAULTS ... = [" table`);
  const end = text.indexOf("\n];", head.index);
  if (end === -1)
    throw new Error(`${DEFAULTS}: the FORK_SETTING_DEFAULTS table has no closing "];" line`);
  const firstLine = text.slice(0, head.index).split("\n").length;
  const lines = text.slice(head.index, end).split("\n");
  const where = (index) => `${DEFAULTS}: FORK_SETTING_DEFAULTS line ${firstLine + index}: `;
  const rest = text.slice(head.index + head[0].length, text.indexOf("\n", head.index)).trim();
  if (rest !== "")
    throw new Error(
      `${where(0)}\`${rest}\` after the table's "[" (start the rows on the next line)`,
    );
  const rows = [];
  let open = -1;
  for (let index = 1; index < lines.length; index++) {
    const line = lines[index];
    if (open !== -1) {
      if (ROW_CLOSE.test(line)) {
        rows.push({ at: open, text: lines.slice(open, index + 1).join("\n") });
        open = -1;
      } else if (ROW_INDENT.test(line)) {
        throw new Error(
          `${where(index)}\`${line.trim()}\` at the rows' indent, but the row opened on line ` +
            `${firstLine + open} has not closed (end each row with "},", alone on its line)`,
        );
      }
    } else if (ROW_OPEN.test(line)) open = index;
    else if (ROW_ONE_LINE.test(line)) rows.push({ at: index, text: line });
    else if (line.trim() !== "" && !line.trim().startsWith("//")) {
      throw new Error(
        `${where(index)}\`${line.trim()}\` is not a \`{ ... }\` row (write each setting as its own object literal)`,
      );
    }
  }
  if (open !== -1)
    throw new Error(
      `${where(open)}a row is still open at the table's "];" (a "];" at column 0 inside a row?)`,
    );
  if (rows.length === 0) throw new Error(`${DEFAULTS}: no row read from FORK_SETTING_DEFAULTS`);
  const ids = rows.map((row) => {
    const { code, bare } = stripRow(row.text);
    if (bare.includes("...")) {
      throw new Error(
        `${where(row.at)}a spread in a row (write each row's scope and key in the row itself)`,
      );
    }
    const objects = bare.trim().replace(/,$/u, "");
    let depth = 0;
    for (let index = 0; index < objects.length; index++) {
      if (objects[index] === "{") depth++;
      else if (objects[index] === "}" && --depth === 0 && index !== objects.length - 1) {
        throw new Error(
          `${where(row.at)}more than one \`{ ... }\` on a row's line (one row per line)`,
        );
      }
    }
    const read = [...code.matchAll(ROW_SCOPE_KEY)];
    const scopes = [...code.matchAll(/\bscope:/gu)].length;
    if (read.length !== 1 || scopes !== 1) {
      throw new Error(
        `${where(row.at)}${scopes} "scope:" but ${read.length} read as ` +
          '`scope: "<scope>", key: "<key>"` (write each row that way)',
      );
    }
    return { scope: read[0][2], key: read[0][4] };
  });
  const unknown = ids.filter((id) => !SCOPES.includes(id.scope));
  if (unknown.length > 0) {
    throw new Error(`${DEFAULTS}: unknown scope ${unknown.map((id) => id.scope).join(", ")}`);
  }
  return ids.map((id) => `${id.scope}.${id.key}`).sort();
}

/** The settings FORK_CHANGES.md names, sorted and without repeats. */
function namedIds(text) {
  return [...new Set([...text.matchAll(SETTING_ID)].map((match) => match[1]))].sort();
}

/** { missing: pinned settings the list does not name, unknown: named settings that are not pinned }. */
function compare(pinned, named) {
  return {
    missing: pinned.filter((id) => !named.includes(id)),
    unknown: named.filter((id) => !pinned.includes(id)),
  };
}

function git(args, root) {
  return execFileSync("git", args, {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
}

/**
 * A repository file's text. The `workflows` job's sparse checkout may leave it out of the working tree; then (and
 * only in a sparse checkout) it is read from the checked-out commit, whose objects git fetches on demand. A file
 * missing from a full checkout is an error, as is one missing from the commit.
 */
function readRepoFile(file, root = ROOT) {
  const full = path.join(root, file);
  if (fs.existsSync(full)) return fs.readFileSync(full, "utf8");
  let sparse = "";
  try {
    sparse = git(["config", "--bool", "core.sparseCheckout"], root).trim();
  } catch {
    // not set: a full checkout
  }
  if (sparse !== "true") throw new Error(`${file} is missing from the checkout`);
  return git(["show", `HEAD:${file}`], root);
}

module.exports = { CHANGES, DEFAULTS, SETTING_ID, pinnedIds, namedIds, compare, readRepoFile };
