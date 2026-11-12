// Refuses fork changes to the database schema: T3 has no down-migrations, so a fork migration
// strands installs that return to upstream. Usage: fork-schema-check.cjs <base> <head>.
// Refused: migrations, schema setup and what they import; shipped files holding a schema statement
// (matched across lines); this check itself and .gitattributes. Files are read as git blobs.

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

// Schema statements in shipped source (tests may build their own tables). \s spans line breaks.
const SCHEMA_STATEMENT =
  /\b(CREATE\s+(UNIQUE\s+)?(TABLE|INDEX|VIEW|TRIGGER|VIRTUAL\s+TABLE)|ALTER\s+TABLE|DROP\s+(TABLE|INDEX|VIEW|TRIGGER))\b/i;
const SHIPPED_SOURCE = /^(apps|packages)\/.+\.(ts|tsx|mts|cts|js|mjs|cjs|sql)$/;
const TEST_SOURCE = /(\.test\.|\.spec\.|\.testkit\.|\/testkit\/|\/__tests__\/|\/test\/)/;
const SOURCE_EXTENSIONS = ["", ".ts", ".tsx", ".mts", ".js", "/index.ts", "/index.tsx"];
const PACKAGE_MANIFEST = /^(apps|packages)\/[^/]+\/package\.json$/;
const IMPORT_SPECIFIER = /\b(?:from|import|require)\s*\(?\s*["']([^"'\n]+)["']/g;

const isTest = (file) => TEST_SOURCE.test(file);
const isShippedSource = (file) => SHIPPED_SOURCE.test(file) && !isTest(file);

/**
 * Files the schema roots import, transitively: relative and package subpath imports. Package
 * roots (`@t3tools/contracts`) re-export the whole package, so they are not followed.
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
    for (const match of text.matchAll(IMPORT_SPECIFIER)) {
      const target = resolveImport(tree, packages, file, match[1]);
      if (target && !isTest(target) && !closure.has(target)) {
        closure.add(target);
        queue.push(target);
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

function resolveImport(tree, packages, from, specifier) {
  if (specifier.startsWith(".")) {
    return resolveFile(tree, path.posix.join(path.posix.dirname(from), specifier));
  }
  const scoped = specifier.match(/^(@[^/]+\/[^/]+)\/(.+)$/);
  const pkg = scoped && packages.get(scoped[1]);
  if (!pkg || !pkg.exports || typeof pkg.exports !== "object") return undefined;
  const entry = pkg.exports[`./${scoped[2]}`];
  const target =
    typeof entry === "string" ? entry : entry && (entry.import ?? entry.default ?? entry.types);
  return typeof target === "string"
    ? resolveFile(tree, path.posix.join(pkg.dir, target))
    : undefined;
}

function resolveFile(tree, file) {
  const candidates = SOURCE_EXTENSIONS.map((extension) => file + extension);
  if (file.endsWith(".js")) candidates.unshift(file.replace(/\.js$/, ".ts"));
  return candidates.find((candidate) => tree.has(candidate));
}

/**
 * @param {ReadonlyArray<{ path: string; before?: string; after?: string }>} changes
 *   one entry per changed path; `before`/`after` are the file texts (undefined when absent)
 * @param {{ closure: ReadonlySet<string>; baseHasCheck: boolean }} context
 * @returns {string[]} one reason per refused file
 */
function findSchemaChanges(changes, { closure, baseHasCheck }) {
  const reasons = [];
  for (const change of changes) {
    const file = change.path;
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
      .map((text) => text?.match(SCHEMA_STATEMENT)?.[0])
      .find(Boolean);
    if (statement) {
      reasons.push(
        `${file}: holds a schema statement (${statement.replace(/\s+/g, " ")}), so any change ` +
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
  for (let index = 0; index + 1 < fields.length; index += 2) {
    const header = fields[index];
    if (!header.startsWith(":")) break;
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
  return git(["ls-tree", "--full-tree", "--name-only", rev, "--", file]).trim() === file;
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
  const changes = entries.map((entry) => ({
    path: entry.path,
    before: text(entry, entry.oldBlob),
    after: text(entry, entry.newBlob),
  }));
  const closure = new Set([
    ...schemaClosure(readTree(mergeBase)),
    ...schemaClosure(readTree(head)),
  ]);
  const reasons = findSchemaChanges(changes, {
    closure,
    baseHasCheck: treeHas(base, CHECK_SCRIPT),
  });
  if (reasons.length === 0) {
    console.log(`No database schema changes (${changes.length} changed files checked).`);
    return 0;
  }
  console.error("Fork changes must not change the database schema (no down-migrations exist):");
  for (const reason of reasons) console.error(`- ${reason}`);
  return 1;
}

module.exports = { findSchemaChanges, parseRawDiff, schemaClosure, CHECK_FILES };

if (require.main === module) {
  process.exitCode = main(process.argv.slice(2));
}
