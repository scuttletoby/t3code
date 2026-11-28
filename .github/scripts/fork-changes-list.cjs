// Fork only: the changes list (one file per change in docs/fork/changes/, so pull requests that add or edit different
// changes merge cleanly; FORK_CHANGES.md is its entry page) names every fork setting of forkDefaults.ts and no other.
// Run by fork-changes-list.test.cjs in Fork tests' `workflows` job. CLI: `print` (the list as one table) and
// `adopt [<base>]` (an open pull request's rows of the old FORK_CHANGES.md table, written as change files).
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const CHANGES = "FORK_CHANGES.md";
const CHANGES_DIR = "docs/fork/changes";
/** Each folder of docs/fork/changes/ and the heading its changes print under (the old table's sections). */
const SECTIONS = { app: "In T3 Code itself", repo: "Repository, CI and releases" };
/** The lines of a change file, in this order, each `- <field>: <text>`. */
const FIELDS = ["Change", "Why", "Setting", "Default", "PR"];
const FILE_NAME = /^[a-z0-9][a-z0-9-]*\.md$/u;
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
/** The same shape once strings are emptied, so the one read from `code` is the row's own and not a string's. */
const ROW_SCOPE_KEY_BARE = /\bscope:\s*(""|''),\s*key:\s*(""|'')/gu;

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
        `${where(row.at)}a spread in a row (no spreads anywhere in a row: write it all out)`,
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
    const scopes = [...bare.matchAll(/\bscope:/gu)].length;
    const shaped = [...bare.matchAll(ROW_SCOPE_KEY_BARE)].length;
    if (read.length !== 1 || scopes !== 1 || shaped !== 1) {
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
  if (!isSparse(root)) throw new Error(`${file} is missing from the checkout`);
  return git(["show", `HEAD:${file}`], root);
}

function isSparse(root) {
  try {
    return git(["config", "--bool", "core.sparseCheckout"], root).trim() === "true";
  } catch {
    return false; // not set: a full checkout
  }
}

/**
 * Every file under docs/fork/changes/, as { rel: its path in that folder, read() }. Read like readRepoFile: a sparse
 * checkout that left the folder out reads it from the checked-out commit; a full checkout without it is an error.
 */
function changeFiles(root) {
  const dir = path.join(root, CHANGES_DIR);
  if (fs.existsSync(dir)) {
    const rels = [];
    const walk = (rel) => {
      for (const item of fs.readdirSync(path.join(dir, rel), { withFileTypes: true })) {
        const child = rel === "" ? item.name : `${rel}/${item.name}`;
        if (item.isDirectory()) walk(child);
        else rels.push(child);
      }
    };
    walk("");
    return rels.sort().map((rel) => ({
      rel,
      read: () => fs.readFileSync(path.join(dir, rel), "utf8"),
    }));
  }
  if (!isSparse(root)) throw new Error(`${CHANGES_DIR}/ is missing from the checkout`);
  return git(["ls-tree", "-r", "--name-only", "HEAD", "--", CHANGES_DIR], root)
    .split("\n")
    .filter(Boolean)
    .map((file) => ({
      rel: file.slice(CHANGES_DIR.length + 1),
      read: () => git(["show", `HEAD:${file}`], root),
    }));
}

/** A change file's text as { Change, Why, Setting, Default, PR }. Throws on any other shape, so no line goes unread. */
function parseChange(text, where) {
  const fail = (why) => {
    throw new Error(
      `${where}: ${why} (a change file is exactly ${FIELDS.length} lines: ` +
        `${FIELDS.map((field) => `"- ${field}: ..."`).join(", ")}, in that order)`,
    );
  };
  const lines = text.replace(/\n$/u, "").split("\n");
  if (lines.length !== FIELDS.length) fail(`${lines.length} lines, not ${FIELDS.length}`);
  const entry = {};
  FIELDS.forEach((field, index) => {
    const prefix = `- ${field}: `;
    if (!lines[index].startsWith(prefix)) fail(`line ${index + 1} does not start with "${prefix}"`);
    const value = lines[index].slice(prefix.length).trim();
    if (value === "") fail(`line ${index + 1}: an empty ${field}`);
    entry[field] = value;
  });
  return entry;
}

/** A change file's text: one `- <field>: <text>` line per field. */
function formatChange(entry) {
  return `${FIELDS.map((field) => `- ${field}: ${entry[field]}`).join("\n")}\n`;
}

const LINK = /\]\(([^)\s]+)\)/gu;

/** Relative Markdown links of a text written in folder `from` (repository-relative), rewritten for folder `to`. */
function relink(text, from, to) {
  return text.replace(LINK, (all, target) => {
    if (/^(?:[a-z][a-z0-9+.-]*:|#|\/)/iu.test(target)) return all;
    const hash = target.indexOf("#");
    const file = hash === -1 ? target : target.slice(0, hash);
    const anchor = hash === -1 ? "" : target.slice(hash);
    return `](${path.posix.relative(to || ".", path.posix.join(from || ".", file))}${anchor})`;
  });
}

const relinkEntry = (entry, from, to) =>
  Object.fromEntries(FIELDS.map((field) => [field, relink(entry[field], from, to)]));

/**
 * The changes list: { section, name, path, text, entry } per file of docs/fork/changes/<section>/, its entry's links
 * repository-relative. Fails closed: no change file, a file outside a section folder (README.md at the top is the
 * folder's own page), a name that is not lowercase-and-dashes .md, or a file that is not exactly the five lines.
 */
function readChanges(root = ROOT) {
  const changes = [];
  for (const { rel, read } of changeFiles(root)) {
    if (rel === "README.md") continue;
    const where = `${CHANGES_DIR}/${rel}`;
    const [section, name, ...deeper] = rel.split("/");
    if (name === undefined || deeper.length > 0 || !Object.hasOwn(SECTIONS, section)) {
      throw new Error(
        `${where}: not a change file (a change is ${CHANGES_DIR}/<${Object.keys(SECTIONS).join("|")}>/<name>.md)`,
      );
    }
    if (!FILE_NAME.test(name)) {
      throw new Error(
        `${where}: a change file's name is lowercase letters, digits and "-", ending in .md`,
      );
    }
    const text = read();
    const entry = relinkEntry(parseChange(text, where), path.posix.dirname(where), "");
    changes.push({ section, name, path: where, text, entry });
  }
  if (changes.length === 0) throw new Error(`${CHANGES_DIR}/: no change file`);
  return changes;
}

/** The settings the changes list names (namedIds over every change file). */
const changesNamedIds = (changes) => namedIds(changes.map((change) => change.text).join("\n"));

/**
 * FORK_CHANGES.md is the entry page. A table row there is the old list (an open pull request that kept its own copy
 * in a merge), which this check no longer reads: refused, with the command that moves the rows to files.
 */
function checkEntryPage(text) {
  const row = text.split("\n").findIndex((line) => line.trimStart().startsWith("|"));
  if (row !== -1) {
    throw new Error(
      `${CHANGES} line ${row + 1}: a table row, but the list is one file per change in ${CHANGES_DIR}/ now. ` +
        "Move an open pull request's rows there with: node .github/scripts/fork-changes-list.cjs adopt",
    );
  }
}

function splitRow(line) {
  return line
    .trim()
    .replace(/^\|/u, "")
    .replace(/(?<!\\)\|$/u, "")
    .split(/(?<!\\)\|/u)
    .map((cell) => cell.trim().replace(/\\\|/gu, "|"));
}

/** The rows of the old FORK_CHANGES.md table, as { section, entry }: none in the new entry page. */
function tableRows(text) {
  const rows = [];
  let section = null;
  for (const line of text.split("\n")) {
    const heading = /^## (.+)$/u.exec(line);
    if (heading !== null) {
      section = Object.keys(SECTIONS).find((key) => SECTIONS[key] === heading[1].trim()) ?? null;
      continue;
    }
    if (!line.startsWith("|")) continue;
    const cells = splitRow(line);
    if (cells.every((cell) => /^:?-+:?$/u.test(cell))) continue;
    if (cells[0] === FIELDS[0] && cells[1] === FIELDS[1]) continue;
    const where = `${CHANGES}: the row "${line.slice(0, 60).trim()}..."`;
    if (section === null) throw new Error(`${where} is under no known "## " section`);
    if (cells.length !== FIELDS.length || cells.some((cell) => cell === "")) {
      throw new Error(`${where} has ${cells.length} cells or an empty one, not ${FIELDS.length}`);
    }
    rows.push({ section, entry: Object.fromEntries(FIELDS.map((field, i) => [field, cells[i]])) });
  }
  return rows;
}

/** The first `#<number>` a change's PR field links, or null. */
function firstRef(entry) {
  const match = /#(\d+)/u.exec(entry.PR);
  return match === null ? null : Number(match[1]);
}

/** A new change file's name: its first pull request or issue number and the change's first words. */
function fileName(entry) {
  const stop = new Set(["a", "an", "the"]);
  const words = entry.Change.toLowerCase()
    .replace(/\]\([^)]*\)/gu, "]")
    .replace(/['’]/gu, "")
    .replace(/[^a-z0-9]+/gu, " ")
    .trim()
    .split(" ")
    .filter((word) => word !== "" && !stop.has(word))
    .slice(0, 6);
  const ref = firstRef(entry);
  return `${[ref ?? "change", ...words].join("-").slice(0, 60).replace(/-+$/u, "")}.md`;
}

const sameEntry = (a, b) => FIELDS.every((field) => a[field] === b[field]);
const sameChange = (a, b) =>
  a.Change === b.Change || (firstRef(a) !== null && firstRef(a) === firstRef(b));

/**
 * An open pull request's edits of the old table (rows of `headText` not in its merge base's `baseText`) as change
 * files: { write, remove, conflicts }. An edit writes its row's file only while that file still holds the base row.
 * Idempotent: a row a file already holds is skipped, so a rerun writes only what is missing.
 */
function adopt(baseText, headText, changes) {
  const base = tableRows(baseText);
  const head = tableRows(headText);
  const key = (row) => [row.section, ...FIELDS.map((field) => row.entry[field])].join("\0");
  const baseKeys = new Set(base.map(key));
  const headKeys = new Set(head.map(key));
  const added = head.filter((row) => !baseKeys.has(key(row)));
  const removed = base.filter((row) => !headKeys.has(key(row)));
  const files = changes.map((change) => ({
    section: change.section,
    path: change.path,
    entry: change.entry,
  }));
  const holding = (row) =>
    files.find((file) => file.section === row.section && sameEntry(file.entry, row.entry));
  const taken = new Set(files.map((file) => file.path));
  const result = { write: [], remove: [], conflicts: [] };
  const paired = new Set();
  const label = (row) => `${row.section}: "${row.entry.Change.slice(0, 60)}..."`;
  for (const row of added) {
    const open = removed.filter((was) => !paired.has(was) && was.section === row.section);
    const was =
      open.find((candidate) => candidate.entry.Change === row.entry.Change) ??
      open.find((candidate) => sameChange(candidate.entry, row.entry));
    if (was !== undefined) paired.add(was);
    if (holding(row) !== undefined) continue;
    let file = was === undefined ? undefined : holding(was);
    if (was !== undefined && file === undefined) {
      const now = files.find(
        (other) => other.section === row.section && sameChange(other.entry, was.entry),
      );
      result.conflicts.push(
        `${label(row)}: the row this pull request edits changed on the base since ` +
          `(${now === undefined ? "it is gone there" : `now ${now.path}`}); make the edit there by hand`,
      );
      continue;
    }
    if (file === undefined) {
      const dir = `${CHANGES_DIR}/${row.section}`;
      const name = fileName(row.entry).replace(/\.md$/u, "");
      let candidate = `${dir}/${name}.md`;
      for (let n = 2; taken.has(candidate); n++) candidate = `${dir}/${name}-${n}.md`;
      taken.add(candidate);
      file = { section: row.section, path: candidate };
      files.push(file);
    }
    file.entry = row.entry;
    result.write.push({
      path: file.path,
      text: formatChange(relinkEntry(row.entry, "", path.posix.dirname(file.path))),
    });
  }
  for (const was of removed) {
    if (paired.has(was)) continue;
    const file = holding(was);
    if (file !== undefined) result.remove.push(file.path);
    else if (
      files.some(
        (other) => other.section === was.section && other.entry.Change === was.entry.Change,
      )
    ) {
      result.conflicts.push(
        `${label(was)}: this pull request removes the row, but it changed on the base since; remove it by hand`,
      );
    }
  }
  return result;
}

/** The whole list as Markdown tables, one per section, each ordered by first pull request (none last). */
function printList(changes) {
  const order = (change) => firstRef(change.entry) ?? Number.MAX_SAFE_INTEGER;
  const out = [];
  for (const [section, title] of Object.entries(SECTIONS)) {
    const rows = changes
      .filter((change) => change.section === section)
      .sort((a, b) => order(a) - order(b) || a.name.localeCompare(b.name));
    if (rows.length === 0) continue;
    out.push(
      `## ${title}`,
      "",
      `| ${FIELDS.join(" | ")} |`,
      `|${FIELDS.map(() => " --- |").join("")}`,
    );
    for (const change of rows) {
      out.push(
        `| ${FIELDS.map((field) => change.entry[field].replace(/\|/gu, "\\|")).join(" | ")} |`,
      );
    }
    out.push("");
  }
  return out.join("\n");
}

/** `adopt [<base>]` in the checkout of the current folder: writes and removes files, says what is left to do. */
function adoptCommand(baseRef = "origin/main") {
  const root = git(["rev-parse", "--show-toplevel"], process.cwd()).trim();
  if (!fs.existsSync(path.join(root, CHANGES_DIR))) {
    throw new Error(
      `${CHANGES_DIR}/ is not in this checkout: merge ${baseRef} first, then run this again`,
    );
  }
  const show = (rev) => {
    try {
      return git(["show", `${rev}:${CHANGES}`], root);
    } catch {
      return "";
    }
  };
  const mergeBase = git(["merge-base", "HEAD", baseRef], root).trim();
  const result = adopt(show(mergeBase), show("HEAD"), readChanges(root));
  for (const file of result.write) {
    const full = path.join(root, file.path);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(`${full}.tmp`, file.text);
    fs.renameSync(`${full}.tmp`, full);
    console.log(`wrote   ${file.path}`);
  }
  for (const file of result.remove) {
    fs.rmSync(path.join(root, file), { force: true });
    console.log(`removed ${file}`);
  }
  for (const conflict of result.conflicts) console.error(`by hand: ${conflict}`);
  if (result.write.length + result.remove.length + result.conflicts.length === 0) {
    console.log(
      `nothing to adopt: HEAD's ${CHANGES} edits no table row, or every row is a file already`,
    );
  }
  console.log(
    `next: git checkout ${baseRef} -- ${CHANGES} && git add ${CHANGES} ${CHANGES_DIR} (then commit or finish the merge)`,
  );
  return result.conflicts.length === 0 ? 0 : 1;
}

module.exports = {
  CHANGES,
  CHANGES_DIR,
  DEFAULTS,
  SECTIONS,
  SETTING_ID,
  pinnedIds,
  namedIds,
  compare,
  readRepoFile,
  readChanges,
  changesNamedIds,
  checkEntryPage,
  parseChange,
  formatChange,
  relink,
  tableRows,
  fileName,
  adopt,
  printList,
};

if (require.main === module) {
  const [command, ...rest] = process.argv.slice(2);
  try {
    if (command === "print") process.stdout.write(printList(readChanges()));
    else if (command === "adopt") process.exitCode = adoptCommand(...rest);
    else {
      console.error("usage: fork-changes-list.cjs print | adopt [<base>]");
      process.exitCode = 2;
    }
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
