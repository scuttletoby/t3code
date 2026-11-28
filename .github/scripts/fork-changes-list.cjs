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
    if (name.endsWith(".md.tmp")) {
      throw new Error(
        `${where}: left by an adopt run that stopped; run adopt again (it removes it) or delete the file`,
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
  const seen = new Map();
  for (const change of changes) {
    for (const key of [`change\0${change.entry.Change}`, `name\0${fileName(change.entry)}`]) {
      const other = seen.get(key);
      if (other !== undefined) {
        throw new Error(
          `${change.path}: the same change as ${other} (${key.startsWith("change") ? "the same Change line" : "the same first pull request and first words"}); keep one file`,
        );
      }
      seen.set(key, change.path);
    }
  }
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
  const result = { write: [], remove: [], conflicts: [] };
  if (head.length === 0) {
    // A page with no table row is the new entry page, not "the pull request removed every row".
    if (base.length > 0) {
      result.conflicts.push(
        `the pull request's ${CHANGES} has no table row but its base has ${base.length}: nothing removed. ` +
          "If the branch has the new entry page already, there is nothing to adopt; otherwise the base is wrong " +
          "(a stale origin/main: git fetch; a stacked pull request: adopt <its base branch>)",
      );
    }
    return result;
  }
  const held = (row) =>
    changes.some((change) => change.section === row.section && sameEntry(change.entry, row.entry));
  if (base.length === 0 && head.some(held)) {
    // The pull request's page holds the whole old list, but its base has none (a merge base from before the list,
    // or after it moved): every row would look added, and an edited one would become a second file.
    result.conflicts.push(
      `the base's ${CHANGES} has no table row, but the pull request's side has the old list (${head.length} rows): ` +
        "nothing written. Pass the pull request's base branch (adopt <base branch>), or replay its commits with " +
        "git cherry-pick and run adopt at each conflict",
    );
    return result;
  }
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
      // A new row that names a change a file already holds (same Change line, or same first pull request and
      // first words) is that change in older or newer words: a second file would list it twice.
      const same = files.find(
        (other) =>
          other.entry.Change === row.entry.Change ||
          fileName(other.entry) === fileName(row.entry) ||
          path.posix.basename(other.path) === fileName(row.entry),
      );
      const candidate = `${CHANGES_DIR}/${row.section}/${fileName(row.entry)}`;
      if (same !== undefined || taken.has(candidate)) {
        result.conflicts.push(
          `${label(row)}: a change file already holds this change (${same?.path ?? candidate}); make the edit there by hand`,
        );
        continue;
      }
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

/**
 * The two pages `adopt` compares and the side it lands on, from the operation in progress: a cherry-pick or rebase
 * replays CHERRY_PICK_HEAD / REBASE_HEAD (compared with its parent) onto HEAD; a merge brings MERGE_HEAD into HEAD
 * (the pull request); with none, HEAD against its merge base with `baseRef`.
 */
function adoptSides(root, baseRef) {
  const rev = (ref) => git(["rev-parse", "--verify", "-q", `${ref}^{commit}`], root).trim();
  const pending = (name) =>
    fs.existsSync(path.resolve(root, git(["rev-parse", "--git-path", name], root).trim()));
  for (const [name, how] of [
    ["CHERRY_PICK_HEAD", "cherry-pick"],
    ["REBASE_HEAD", "rebase"],
  ]) {
    if (pending(name)) {
      return {
        how,
        pr: rev(name),
        base: rev(`${name}^`),
        onto: rev("HEAD"),
        next: `git ${how} --continue`,
      };
    }
  }
  if (pending("MERGE_HEAD")) {
    const onto = rev("MERGE_HEAD");
    return {
      how: "merge",
      pr: rev("HEAD"),
      base: git(["merge-base", "HEAD", onto], root).trim(),
      onto,
      next: "git commit --no-edit",
    };
  }
  return {
    how: "no merge in progress",
    pr: rev("HEAD"),
    base: git(["merge-base", "HEAD", baseRef], root).trim(),
    onto: rev(baseRef),
    next: "git commit",
  };
}

/**
 * `adopt [<base>]` in the checkout of the current folder: writes and removes change files, then (when every row of
 * the page in the working tree is held by a file or was on the base already) puts the landing side's entry page in
 * FORK_CHANGES.md. Exits 1 with the reason whenever it leaves something to do by hand.
 */
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
  for (const section of Object.keys(SECTIONS)) {
    const dir = path.join(root, CHANGES_DIR, section);
    if (!fs.existsSync(dir)) continue;
    for (const name of fs.readdirSync(dir).filter((file) => file.endsWith(".md.tmp"))) {
      fs.rmSync(path.join(dir, name), { force: true });
      console.log(`removed ${CHANGES_DIR}/${section}/${name} (left by a run that stopped)`);
    }
  }
  const sides = adoptSides(root, baseRef);
  const short = (sha) => sha.slice(0, 9);
  console.log(
    `reading ${CHANGES} of ${short(sides.pr)} against ${short(sides.base)} (${sides.how}), onto ${short(sides.onto)}`,
  );
  const ontoPage = show(sides.onto);
  if (tableRows(ontoPage).length > 0) {
    console.error(
      `by hand: ${short(sides.onto)}'s ${CHANGES} is the old table still: nothing adopted (git fetch, or name the right base)`,
    );
    return 1;
  }
  const basePage = show(sides.base);
  const prPage = show(sides.pr);
  const changes = readChanges(root);
  const result = adopt(basePage, prPage, changes);
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
  const conflicts = [...result.conflicts];
  const pagePath = path.join(root, CHANGES);
  const working = fs.existsSync(pagePath) ? fs.readFileSync(pagePath, "utf8") : "";
  const rowLines = (text) =>
    text
      .split("\n")
      .filter((line) => line.startsWith("|"))
      .map((line) => line.trim());
  if (rowLines(working).length > 0) {
    // Only rows that came from the two commits may go: the pull request's (adopted above) and the base's.
    const known = new Set([...rowLines(prPage), ...rowLines(basePage)]);
    const unknown = rowLines(working).filter((line) => !known.has(line));
    if (unknown.length > 0) {
      conflicts.push(
        `${CHANGES} has ${unknown.length} table row(s) from neither side, e.g. "${unknown[0].slice(0, 60)}...": ` +
          `move them to change files by hand, then take ${short(sides.onto)}'s page`,
      );
    }
    if (conflicts.length === 0) {
      fs.writeFileSync(`${pagePath}.tmp`, ontoPage);
      fs.renameSync(`${pagePath}.tmp`, pagePath);
      console.log(`wrote   ${CHANGES} (the entry page of ${short(sides.onto)})`);
    }
  }
  for (const conflict of conflicts) console.error(`by hand: ${conflict}`);
  if (result.write.length + result.remove.length + conflicts.length === 0) {
    console.log(
      `nothing to adopt: ${short(sides.pr)}'s ${CHANGES} edits no table row, or every row is a file already`,
    );
  }
  if (conflicts.length > 0) {
    console.log(
      `then: git show ${short(sides.onto)}:${CHANGES} > ${CHANGES} once no row there is left unmoved`,
    );
  }
  console.log(`next: git add ${CHANGES} ${CHANGES_DIR} && ${sides.next}`);
  return conflicts.length === 0 ? 0 : 1;
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
      console.error(
        "usage: fork-changes-list.cjs print (every change, by first pull request) | adopt [<base branch>]",
      );
      process.exitCode = 2;
    }
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
