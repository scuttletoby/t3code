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

/**
 * `<scope>.<key>` of each row of FORK_SETTING_DEFAULTS in forkDefaults.ts's text. Throws when the table is not
 * found, is empty, or has a `scope:` the parse cannot pair with its `key:` (fail closed: a row it cannot read
 * would otherwise go unchecked).
 */
function pinnedIds(text) {
  const start = text.search(/^export const FORK_SETTING_DEFAULTS\b[^=]*=\s*\[/mu);
  if (start === -1)
    throw new Error(`${DEFAULTS}: no "export const FORK_SETTING_DEFAULTS ... = [" table`);
  const end = text.indexOf("\n];", start);
  if (end === -1)
    throw new Error(`${DEFAULTS}: the FORK_SETTING_DEFAULTS table has no closing "];" line`);
  const table = text.slice(start, end);
  const scopes = [...table.matchAll(/\bscope:/gu)].length;
  const rows = [...table.matchAll(/\bscope:\s*(["'])(\w+)\1,\s*key:\s*(["'])([A-Za-z0-9]+)\3/gu)];
  if (rows.length === 0) throw new Error(`${DEFAULTS}: no row read from FORK_SETTING_DEFAULTS`);
  if (rows.length !== scopes) {
    throw new Error(
      `${DEFAULTS}: ${scopes} "scope:" in FORK_SETTING_DEFAULTS but ${rows.length} read as ` +
        '`scope: "<scope>", key: "<key>"` (write each row that way)',
    );
  }
  const unknown = rows.filter((row) => !SCOPES.includes(row[2]));
  if (unknown.length > 0) {
    throw new Error(`${DEFAULTS}: unknown scope ${unknown.map((row) => row[2]).join(", ")}`);
  }
  return rows.map((row) => `${row[2]}.${row[4]}`).sort();
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
