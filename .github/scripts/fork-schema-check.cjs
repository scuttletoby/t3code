// Refuses fork changes to the database schema: T3 has no down-migrations, so a fork migration
// strands installs that return to upstream. Usage: fork-schema-check.cjs <base> <head>.
// Refused: migrations, schema setup and what they import; shipped source files holding a schema
// statement; a PR copy of this check or of .gitattributes that differs from the base's copy; and a
// branch built on an old (since rebased) base. Files are read as git blobs.

const { execFileSync } = require("node:child_process");
const path = require("node:path");

// Files that define or apply the schema. Any change to them, or to what they import, is refused.
const SCHEMA_ROOTS = [
  /^apps\/server\/src\/persistence\/Migrations(\/|\.ts$)/,
  /^apps\/server\/src\/persistence\/databaseMigrationHead\.ts$/,
  /^apps\/server\/src\/persistence\/initializeV2Database\.ts$/,
  /^apps\/server\/src\/persistence\/reconcileV2PreviewMigration\.ts$/,
];

// The check's own files. Changing them would let a PR judge itself, so it is never allowed.
const CHECK_SCRIPT = ".github/scripts/fork-schema-check.cjs";
const CHECK_FILES = [
  CHECK_SCRIPT,
  ".github/scripts/fork-schema-check.test.cjs",
  ".github/workflows/fork-checks.yml",
];
const GITATTRIBUTES = /(^|\/)\.gitattributes$/;

// Schema statements in shipped source (tests may build their own tables). Between the keywords
// may stand line breaks, SQL comments and string joins ("CREATE " + "TABLE"). An interpolation
// where the object kind belongs (`CREATE ${kind}`) counts too, but only with upper-case keywords,
// so UI text such as `Create ${label}` stays free.
const GAP = String.raw`(?:\s|["'\x60+]|/\*[\s\S]*?\*/|--[^\n]*\n)+`;
const HOLE = String.raw`\$\{[^}]*\}`;
const SCHEMA_STATEMENTS = [
  new RegExp(
    String.raw`\b(?:CREATE${GAP}(?:UNIQUE${GAP})?(?:TABLE|INDEX|VIEW|TRIGGER|VIRTUAL${GAP}TABLE)|ALTER${GAP}TABLE|DROP${GAP}(?:TABLE|INDEX|VIEW|TRIGGER))\b`,
    "i",
  ),
  new RegExp(String.raw`\b(?:CREATE${GAP}(?:UNIQUE${GAP})?|ALTER${GAP}|DROP${GAP})${HOLE}`),
];
/** The first schema statement in `text`, or undefined. */
const schemaStatement = (text) =>
  text === undefined
    ? undefined
    : SCHEMA_STATEMENTS.map((pattern) => text.match(pattern)?.[0]).find(Boolean);
const SHIPPED_SOURCE = /^(apps|packages)\/.+\.(ts|tsx|mts|cts|js|mjs|cjs|sql)$/;
const TEST_SOURCE = /(\.test\.|\.spec\.|\.testkit\.|\/testkit\/|\/__tests__\/|\/test\/)/;
const SOURCE_EXTENSIONS = ["", ".ts", ".tsx", ".mts", ".js", "/index.ts", "/index.tsx"];
const PACKAGE_MANIFEST = /^(apps|packages)\/[^/]+\/package\.json$/;
const IMPORT_SPECIFIER = /\b(?:from|import|require)\s*\(?\s*["']([^"'\n]+)["']/g;
// `import { A, type B as C } from "x"` and `export { A } from "x"`: the names a static clause takes.
const IMPORT_CLAUSE =
  /\b(?:import|export)\s+(?:type\s+)?([\w$*{}\s,]*?)\s*from\s*["']([^"'\n]+)["']/g;

const isTest = (file) => TEST_SOURCE.test(file);
const isShippedSource = (file) => SHIPPED_SOURCE.test(file) && !isTest(file);

/**
 * Files the schema roots import, transitively: relative and package subpath imports, and for a
 * package root (`@t3tools/contracts`) the files that declare the names imported from it. The
 * root's barrel itself is not added, so the rest of the package stays free to change.
 * @param {{ files: string[]; has(file: string): boolean; read(file: string): string }} tree
 * @returns {Set<string>}
 */
function schemaClosure(tree) {
  const packages = packageDirectories(tree);
  const queue = tree.files.filter(
    (file) => SCHEMA_ROOTS.some((pattern) => pattern.test(file)) && !isTest(file),
  );
  const closure = new Set(queue);
  while (queue.length > 0) {
    const file = queue.pop();
    const text = tree.read(file);
    const names = importedNames(text);
    for (const match of text.matchAll(IMPORT_SPECIFIER)) {
      for (const target of resolveImport(tree, packages, file, match[1], names.get(match[1]))) {
        if (!isTest(target) && !closure.has(target)) {
          closure.add(target);
          queue.push(target);
        }
      }
    }
  }
  return closure;
}

function packageDirectories(tree) {
  const packages = new Map();
  for (const file of tree.files) {
    if (!PACKAGE_MANIFEST.test(file)) continue;
    try {
      const manifest = JSON.parse(tree.read(file));
      if (typeof manifest.name === "string") {
        packages.set(manifest.name, { dir: path.posix.dirname(file), exports: manifest.exports });
      }
    } catch {
      // A broken package.json fails the build elsewhere; it defines no schema imports here.
    }
  }
  return packages;
}

/**
 * Per specifier, the names static clauses take from it; `*` for a namespace or a clause it cannot
 * read. A specifier with no entry (dynamic import, require, side-effect import) takes everything.
 * @returns {Map<string, Set<string>>}
 */
function importedNames(text) {
  const names = new Map();
  for (const [, clause, specifier] of text.matchAll(IMPORT_CLAUSE)) {
    const set = names.get(specifier) ?? new Set();
    names.set(specifier, set);
    const braces = clause.match(/\{([^}]*)\}/);
    const outside = clause.replace(/\{[^}]*\}/, "").replace(/,/g, " ").trim();
    if (/\*/.test(outside)) set.add("*");
    else if (outside) set.add("default");
    for (const part of braces ? braces[1].split(",") : []) {
      const name = part.trim().replace(/^type\s+/, "").split(/\s+as\s+/)[0].trim();
      if (name) set.add(name);
    }
    if (!braces && !outside) set.add("*");
  }
  return names;
}

/** The files an import reaches: one file, or for a package root the files declaring `names`. */
function resolveImport(tree, packages, from, specifier, names) {
  if (specifier.startsWith(".")) {
    const target = resolveFile(tree, path.posix.join(path.posix.dirname(from), specifier));
    return target ? [target] : [];
  }
  const parts = specifier.match(/^(@[^/]+\/[^/]+)(?:\/(.+))?$/);
  const pkg = parts && packages.get(parts[1]);
  if (!pkg || !pkg.exports) return [];
  const exports =
    typeof pkg.exports === "string" || "import" in pkg.exports || "default" in pkg.exports
      ? { ".": pkg.exports }
      : pkg.exports;
  const entry = exports[parts[2] ? `./${parts[2]}` : "."];
  const target =
    typeof entry === "string" ? entry : entry && (entry.import ?? entry.default ?? entry.types);
  const file =
    typeof target === "string" ? resolveFile(tree, path.posix.join(pkg.dir, target)) : undefined;
  if (!file) return [];
  if (parts[2] || !names || names.has("*") || names.has("default")) return [file];
  const found = new Set();
  for (const name of names) declaringFiles(tree, packages, file, name, found, new Set());
  return [...found];
}

/** Follows `export * from` and `export { name } from` until a file declares `name`. */
function declaringFiles(tree, packages, file, name, found, seen) {
  if (seen.has(`${file}\0${name}`)) return;
  seen.add(`${file}\0${name}`);
  const text = tree.read(file);
  const word = name.replace(/\$/g, "\\$");
  const declares = new RegExp(
    String.raw`\bexport\s+(?:declare\s+)?(?:abstract\s+)?(?:const|let|var|function\*?|class|type|interface|enum|namespace)\s+${word}(?![\w$])`,
  );
  const local = [...text.matchAll(/\bexport\s+(?:type\s+)?\{([^}]*)\}(?!\s*from)/g)].some(
    (match) => match[1].split(",").some((part) => part.trim().split(/\s+as\s+/).pop() === name),
  );
  if (declares.test(text) || local) {
    found.add(file);
    return;
  }
  for (const match of text.matchAll(
    /\bexport\s+(?:type\s+)?(\*(?:\s+as\s+([\w$]+))?|\{[^}]*\})\s*from\s*["']([^"'\n]+)["']/g,
  )) {
    const [, clause, namespace, specifier] = match;
    const targets = resolveImport(tree, packages, file, specifier, new Set(["*"]));
    if (clause.startsWith("*") && !namespace) {
      for (const target of targets) declaringFiles(tree, packages, target, name, found, seen);
      continue;
    }
    const exported = namespace
      ? namespace === name && "*"
      : clause
          .slice(1, -1)
          .split(",")
          .map((part) => part.trim().replace(/^type\s+/, "").split(/\s+as\s+/))
          .find((pair) => (pair[1] ?? pair[0]).trim() === name)?.[0]
          .trim();
    if (!exported) continue;
    for (const target of targets) {
      if (exported === "*") found.add(target);
      else declaringFiles(tree, packages, target, exported, found, seen);
    }
  }
}

function resolveFile(tree, file) {
  const candidates = SOURCE_EXTENSIONS.map((extension) => file + extension);
  if (file.endsWith(".js")) candidates.unshift(file.replace(/\.js$/, ".ts"));
  return candidates.find((candidate) => tree.has(candidate));
}

/**
 * @param {ReadonlyArray<{ path: string; before?: string; after?: string; sameAsBase?: boolean }>} changes
 *   one entry per path the branch changes since the merge-base; `before`/`after` are the file
 *   texts (undefined when absent); `sameAsBase` says the branch's copy equals the base's copy
 * @param {{ closure: ReadonlySet<string>; baseHasCheck: boolean; staleCommits?: number }} context
 *   `staleCommits`: the branch's commits that the base already holds as rebased copies
 * @returns {string[]} one reason per refused file
 */
function findSchemaChanges(changes, { closure, baseHasCheck, staleCommits = 0 }) {
  const reasons = [];
  const rebase =
    "this branch is based on an old main that has since been rebased; rebase it onto main";
  if (staleCommits > 0) {
    reasons.push(
      `${staleCommits} commit(s) of this branch are already on main as rebased copies, so ${rebase}.`,
    );
  }
  for (const change of changes) {
    const file = change.path;
    const guarded = CHECK_FILES.includes(file) || GITATTRIBUTES.test(file);
    // The branch carries main's copy (e.g. it branched before main was rebased): nothing to judge.
    if (guarded && change.sameAsBase) continue;
    if (guarded && staleCommits > 0) {
      reasons.push(`${file}: differs from main's copy because ${rebase}.`);
      continue;
    }
    if (CHECK_FILES.includes(file)) {
      if (baseHasCheck) {
        reasons.push(
          `${file}: changes the schema check itself. Only the owner may do that: the check stays ` +
            "red, and the owner merges past it.",
        );
      }
      continue;
    }
    if (GITATTRIBUTES.test(file)) {
      reasons.push(
        `${file}: changes how git treats files, which can hide changes from review. Only the ` +
          "owner may do that.",
      );
      continue;
    }
    if (SCHEMA_ROOTS.some((pattern) => pattern.test(file)) || closure.has(file)) {
      reasons.push(`${file}: changes the database schema, its migrations or code they import`);
      continue;
    }
    if (!isShippedSource(file)) continue;
    const statement = [change.after, change.before]
      .map(schemaStatement)
      .find(Boolean);
    if (statement) {
      reasons.push(
        `${file}: holds a schema statement (${statement.replace(/[^\w${}]+/g, " ")}), so any change ` +
          "to it may change the schema",
      );
    }
  }
  return reasons;
}

/**
 * Parses `git diff --raw -z --no-abbrev` output into { path, oldBlob, newBlob } entries. A side
 * that is absent or a submodule (no blob) is undefined; the path is still listed.
 */
function parseRawDiff(raw) {
  const fields = raw.split("\0");
  const entries = [];
  if (fields.at(-1) === "") fields.pop();
  if (fields.length % 2 !== 0) throw new Error("cannot parse git diff --raw output (odd fields)");
  for (let index = 0; index < fields.length; index += 2) {
    const header = fields[index];
    if (!/^:\d+ \d+ [0-9a-f]+ [0-9a-f]+ [A-Z]\d*$/.test(header)) {
      throw new Error(`cannot parse git diff --raw record: ${JSON.stringify(header)}`);
    }
    const [oldMode, newMode, oldSha, newSha, status] = header.slice(1).split(" ");
    if (/^[RC]/.test(status)) throw new Error(`unexpected rename in diff: ${header}`);
    const blob = (mode, sha) => (/^10|^12/.test(mode) && !/^0+$/.test(sha) ? sha : undefined);
    entries.push({
      path: fields[index + 1],
      oldBlob: blob(oldMode, oldSha),
      newBlob: blob(newMode, newSha),
    });
  }
  return entries;
}

function git(args) {
  return execFileSync("git", ["-c", "core.quotePath=false", ...args], {
    encoding: "utf8",
    maxBuffer: 512 * 1024 * 1024,
  });
}

/** Source files and package manifests of a commit; texts are read only when the walk asks. */
function readTree(rev) {
  const blobs = new Map();
  for (const record of git(["ls-tree", "-r", "-z", "--full-tree", rev]).split("\0")) {
    const match = record.match(/^\d+ blob ([0-9a-f]+)\t(.+)$/s);
    if (match && (SHIPPED_SOURCE.test(match[2]) || PACKAGE_MANIFEST.test(match[2]))) {
      blobs.set(match[2], match[1]);
    }
  }
  return {
    files: [...blobs.keys()],
    has: (file) => blobs.has(file),
    read: (file) => (blobs.has(file) ? readBlob(blobs.get(file)) : ""),
  };
}

const blobCache = new Map();
function readBlob(sha) {
  if (!blobCache.has(sha)) blobCache.set(sha, git(["cat-file", "blob", sha]));
  return blobCache.get(sha);
}

function treeHas(rev, file) {
  return blobAt(rev, file) !== undefined;
}

/** The blob id of `file` in `rev`, or undefined when absent. */
function blobAt(rev, file) {
  const record = git(["ls-tree", "-z", "--full-tree", rev, "--", file]).split("\0")[0];
  const match = record.match(/^\d+ \w+ ([0-9a-f]+)\t(.+)$/s);
  return match && match[2] === file ? match[1] : undefined;
}

/**
 * The branch's commits (merge-base..head) that the base already holds as rebased copies, matched
 * by author, author time and subject: a rebase keeps all three. Needs no file contents.
 */
function staleCommitCount(mergeBase, base, head) {
  const ids = (range) =>
    git(["log", "--no-merges", "--format=%an%x00%ae%x00%at%x00%s", range])
      .split("\n")
      .filter(Boolean);
  const onBase = new Set(ids(`${mergeBase}..${base}`));
  return ids(`${mergeBase}..${head}`).filter((id) => onBase.has(id)).length;
}

function main(argv) {
  const [base, head] = argv;
  if (!base || !head) {
    console.error("Usage: fork-schema-check.cjs <base> <head>");
    return 2;
  }
  const mergeBase = git(["merge-base", base, head]).trim();
  const entries = parseRawDiff(
    git(["diff", "--raw", "-z", "--no-abbrev", "--no-renames", "--no-ext-diff", mergeBase, head]),
  );
  // Only shipped source is searched for statements, so only its texts are read.
  const text = (entry, sha) => (sha && isShippedSource(entry.path) ? readBlob(sha) : undefined);
  const guarded = (file) => CHECK_FILES.includes(file) || GITATTRIBUTES.test(file);
  const changes = entries.map((entry) => ({
    path: entry.path,
    before: text(entry, entry.oldBlob),
    after: text(entry, entry.newBlob),
    sameAsBase: guarded(entry.path) ? blobAt(base, entry.path) === entry.newBlob : undefined,
  }));
  const closure = new Set([
    ...schemaClosure(readTree(mergeBase)),
    ...schemaClosure(readTree(head)),
  ]);
  const reasons = findSchemaChanges(changes, {
    closure,
    baseHasCheck: treeHas(base, CHECK_SCRIPT),
    staleCommits: staleCommitCount(mergeBase, base, head),
  });
  if (reasons.length === 0) {
    console.log(`No database schema changes (${changes.length} changed files checked).`);
    return 0;
  }
  console.error("Fork changes must not change the database schema (no down-migrations exist):");
  for (const reason of reasons) console.error(`- ${reason}`);
  return 1;
}

module.exports = { findSchemaChanges, parseRawDiff, schemaClosure, CHECK_FILES, schemaStatement };

if (require.main === module) {
  process.exitCode = main(process.argv.slice(2));
}
