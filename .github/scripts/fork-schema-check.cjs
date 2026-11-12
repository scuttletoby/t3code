// Refuses fork changes to the database schema: T3 has no down-migrations, so a fork migration
// strands installs that return to upstream. Usage: fork-schema-check.cjs <base> <head>.
// Refused: migrations, schema setup and what they import (with the package.json files that map
// those imports, and the workspace package list); an import of theirs that should resolve inside
// the repository but does not; shipped source files holding a schema statement; a PR copy of this
// check or of .gitattributes that differs from the base's copy; and a branch holding commits
// already on the base (built on an old, since rebased base, or copying its commits). Files are
// read as git blobs.

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
// Files whose imports the closure follows; other files it reaches (JSON, SQL) are guarded whole.
const SCRIPT_SOURCE = /\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/;
const MANIFEST = /(^|\/)package\.json$/;
const WORKSPACE_FILE = "pnpm-workspace.yaml";
const IMPORT_SPECIFIER = /\b(?:from|import|require)\s*\(?\s*["']([^"'\n]+)["']/g;
// An import whose target is computed (`import(name)`, a template, `import.meta.glob`): not followed.
const COMPUTED_IMPORT = /\b(?:import|require)\s*\(\s*(?!["'\s])|\bimport\.meta\.glob\b/g;
// `import { A, type B as C } from "x"` and `export { A } from "x"`: the names a static clause takes.
const IMPORT_CLAUSE =
  /\b(?:import|export)\s+(?:type\s+)?([\w$*{}\s,]*?)\s*from\s*["']([^"'\n]+)["']/g;

const isTest = (file) => TEST_SOURCE.test(file);
const isShippedSource = (file) => SHIPPED_SOURCE.test(file) && !isTest(file);

/**
 * Files the schema roots import, transitively: relative imports, and for an import of a workspace
 * package its package.json (which maps the import to files) and every file any export condition
 * of that entry can pick. For a package root (`@t3tools/contracts`) only the files that declare
 * the imported names are followed. The barrels on the way to a declaring file are guarded too (a
 * barrel edit could re-point a name), but their other re-exports are not followed, so the rest of
 * the package stays free to change. A name that cannot be traced guards the whole barrel and
 * everything it re-exports.
 *
 * Nothing that could be schema code is skipped silently: an import inside the repository that
 * resolves to no file is listed in `unresolved` (the check refuses it), and only packages outside
 * the workspace resolve to nothing; their names are listed in `outside`.
 * @param {{ files: string[]; has(file: string): boolean; read(file: string): string }} tree
 * @returns {{ closure: Set<string>; unresolved: string[]; outside: Set<string> }}
 */
function traceSchema(tree) {
  const queue = tree.files.filter(
    (file) => SCHEMA_ROOTS.some((pattern) => pattern.test(file)) && !isTest(file),
  );
  const closure = new Set(queue);
  const unresolved = new Map();
  const ctx = {
    tree,
    ...workspacePackages(tree),
    outside: new Set(),
    guard: (file) => closure.add(file),
    unresolved: (from, specifier, why) =>
      unresolved.set(`${from}\0${specifier}`, `${from}: imports "${specifier}", but ${why}.`),
  };
  const walked = new Set(queue);
  while (queue.length > 0) {
    const file = queue.pop();
    if (!SCRIPT_SOURCE.test(file)) continue;
    const text = tree.read(file);
    const names = importedNames(text);
    for (const match of text.matchAll(COMPUTED_IMPORT)) {
      ctx.unresolved(
        file,
        `${match[0].trim()}...`,
        "its target is computed, so it cannot be followed",
      );
    }
    for (const match of text.matchAll(IMPORT_SPECIFIER)) {
      const reached = resolveImport(ctx, file, match[1], names.get(match[1]));
      // A test-path file that schema code imports ships with it, so it is guarded too.
      for (const target of reached.barrels) closure.add(target);
      for (const target of reached.files) {
        if (!walked.has(target)) {
          closure.add(target);
          walked.add(target);
          queue.push(target);
        }
      }
    }
  }
  const why =
    "the check cannot resolve it to a file in this repository, so it cannot tell which files " +
    "the schema depends on. Fix the import (or teach the check this import shape)";
  return {
    closure,
    unresolved: [...unresolved.values()].map((reason) => reason.replace(/\.$/, `: ${why}.`)),
    outside: ctx.outside,
  };
}

/** The files the schema roots import, transitively (see traceSchema). */
const schemaClosure = (tree) => traceSchema(tree).closure;

/**
 * The workspace's member directories as written in pnpm-workspace.yaml `packages:`, or else the
 * root package.json `workspaces`; undefined when neither says (then every package.json counts).
 * @returns {{ source?: string; patterns?: string[] }}
 */
function workspacePatterns(tree) {
  if (tree.has(WORKSPACE_FILE)) {
    const lines = tree.read(WORKSPACE_FILE).split("\n");
    const start = lines.findIndex((line) => /^packages\s*:/.test(line));
    if (start < 0) return { source: WORKSPACE_FILE };
    const inline = lines[start].match(/^packages\s*:\s*\[(.*)\]/);
    const items = inline
      ? inline[1].split(",")
      : lines
          .slice(start + 1)
          .filter((line, index, rest) => rest.slice(0, index + 1).every((l) => /^(\s|#|$)/.test(l)))
          .map((line) => line.match(/^\s+-\s*(.*)$/)?.[1])
          .filter((item) => item !== undefined);
    return {
      source: WORKSPACE_FILE,
      patterns: items
        .map((item) =>
          item
            .replace(/\s+#.*$/, "")
            .trim()
            .replace(/^["']|["']$/g, ""),
        )
        .filter(Boolean),
    };
  }
  try {
    const { workspaces } = JSON.parse(tree.read("package.json"));
    const patterns = Array.isArray(workspaces) ? workspaces : workspaces?.packages;
    if (Array.isArray(patterns)) return { source: "package.json", patterns };
  } catch {
    // No root manifest, or not JSON: no workspace list, so every package.json counts.
  }
  return {};
}

/**
 * Workspace packages by name. A name may have several directories (all are consulted). Manifests
 * that are not JSON are listed in `broken`: such a package's name is unknown, so an import that
 * matches no package is then not trusted to be from outside the repository.
 */
function workspacePackages(tree) {
  const { patterns } = workspacePatterns(tree);
  const glob = (pattern) =>
    new RegExp(
      `^${pattern
        .replace(/^\.\//, "")
        .replace(/\/$/, "")
        .replace(/[.+^${}()|[\]\\]/g, "\\$&")
        .replace(
          /\*\*\/|\*\*|\*/g,
          (star) => ({ "**/": "(?:.*/)?", "**": ".*" })[star] ?? "[^/]*",
        )}$`,
    );
  const include = patterns?.filter((pattern) => !pattern.startsWith("!")).map(glob);
  const exclude = patterns
    ?.filter((pattern) => pattern.startsWith("!"))
    .map((p) => glob(p.slice(1)));
  const member = (dir) =>
    include
      ? dir === "" || (include.some((re) => re.test(dir)) && !exclude.some((re) => re.test(dir)))
      : !/(^|\/)node_modules(\/|$)/.test(dir);
  const packages = new Map();
  const broken = [];
  for (const file of tree.files) {
    if (!MANIFEST.test(file)) continue;
    const dir = file === "package.json" ? "" : path.posix.dirname(file);
    if (!member(dir)) continue;
    let manifest;
    try {
      manifest = JSON.parse(tree.read(file));
    } catch {
      broken.push(file);
      continue;
    }
    if (typeof manifest?.name !== "string") continue;
    const list = packages.get(manifest.name) ?? [];
    list.push({ dir, file, manifest });
    packages.set(manifest.name, list);
  }
  return { packages, broken };
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
    const outside = clause
      .replace(/\{[^}]*\}/, "")
      .replace(/,/g, " ")
      .trim();
    if (/\*/.test(outside)) set.add("*");
    else if (outside) set.add("default");
    for (const part of braces ? braces[1].split(",") : []) {
      const name = part
        .trim()
        .replace(/^type\s+/, "")
        .split(/\s+as\s+/)[0]
        .trim();
      if (name) set.add(name);
    }
    if (!braces && !outside) set.add("*");
  }
  return names;
}

/**
 * The files an import reaches: the files it maps to, or for a package root the files declaring
 * `names` (`files`, whose own imports matter) and the barrels re-exporting them (`barrels`, guarded
 * only). A workspace package's package.json is guarded through `ctx.guard`; an import that should
 * resolve inside the repository but does not is reported through `ctx.unresolved`.
 * @returns {{ files: string[]; barrels: string[] }}
 */
function resolveImport(ctx, from, specifier, names) {
  const { tree } = ctx;
  const none = { files: [], barrels: [] };
  if (/^\.\.?(\/|$)/.test(specifier)) {
    const target = resolveFile(tree, path.posix.join(path.posix.dirname(from), specifier));
    if (target) return { files: [target], barrels: [] };
    ctx.unresolved(from, specifier, "no file in the repository matches it");
    return none;
  }
  if (/^[a-z][\w+.-]*:/i.test(specifier)) {
    // `node:fs` and other scheme imports come from the runtime, not from this repository.
    ctx.outside.add(specifier);
    return none;
  }
  // `#name` (package.json "imports") is not traced, so it is reported like any unresolved import.
  const parts = specifier.match(/^((?:@[^/@]+\/)?[^/@.#][^/]*)(?:\/(.+))?$/);
  if (!parts) {
    ctx.unresolved(from, specifier, "it is neither a relative import nor a package name");
    return none;
  }
  const [, name, subpath] = parts;
  const candidates = ctx.packages.get(name);
  if (!candidates) {
    if (ctx.broken.length > 0) {
      ctx.unresolved(
        from,
        specifier,
        `${ctx.broken.join(", ")} is not JSON, so its name is unknown`,
      );
    } else {
      ctx.outside.add(name);
    }
    return none;
  }
  const entries = [];
  for (const pkg of candidates) {
    ctx.guard(pkg.file);
    for (const target of packageTargets(pkg.manifest, subpath)) {
      const file = resolveFile(tree, path.posix.join(pkg.dir, target));
      if (file && !entries.includes(file)) entries.push(file);
    }
  }
  if (entries.length === 0) {
    ctx.unresolved(
      from,
      specifier,
      `${name} is a package of this repository (${candidates.map((pkg) => pkg.file).join(", ")}) ` +
        "and none of the entry points it lists for that import is a file here",
    );
    return none;
  }
  if (subpath || !names || names.has("*") || names.has("default"))
    return { files: entries, barrels: [] };
  const files = new Set();
  const barrels = new Set();
  for (const entry of entries) {
    const found = new Set();
    // A name no file is seen to declare fails closed: that entry file is walked whole.
    const traced = [...names].every((each) =>
      declaringFiles(ctx, entry, each, found, barrels, new Map()),
    );
    for (const file of traced ? found : [entry]) files.add(file);
  }
  return { files: [...files], barrels: [...barrels] };
}

/**
 * Every path a package.json can map `name[/subpath]` to, for any export condition (`node`,
 * `import`, `types`, `default`, custom ones, nested or in fallback arrays): the check does not
 * guess which condition a tool picks, so it guards them all. Without `exports`: `main`, `module`,
 * `types`, a string `browser` and `index`, or the subpath itself.
 * @returns {string[]}
 */
function packageTargets(manifest, subpath) {
  const key = subpath ? `./${subpath}` : ".";
  const { exports } = manifest;
  if (exports === undefined || exports === null) {
    if (subpath) return [key];
    return [
      manifest.main,
      manifest.module,
      manifest.types,
      manifest.typings,
      manifest.browser,
      "./index",
    ].filter((target) => typeof target === "string");
  }
  const map =
    typeof exports === "object" &&
    !Array.isArray(exports) &&
    Object.keys(exports).some((each) => each.startsWith("."))
      ? exports
      : { ".": exports };
  const targets = [];
  const leaves = (value, star) => {
    if (typeof value === "string")
      targets.push(star === undefined ? value : value.replaceAll("*", star));
    else if (Array.isArray(value)) for (const each of value) leaves(each, star);
    else if (value && typeof value === "object")
      for (const each of Object.values(value)) leaves(each, star);
  };
  if (Object.hasOwn(map, key)) leaves(map[key]);
  else {
    for (const [pattern, value] of Object.entries(map)) {
      const star = pattern.indexOf("*");
      if (star < 0) continue;
      const [prefix, suffix] = [pattern.slice(0, star), pattern.slice(star + 1)];
      if (key.length >= pattern.length - 1 && key.startsWith(prefix) && key.endsWith(suffix)) {
        leaves(value, key.slice(prefix.length, key.length - suffix.length));
      }
    }
  }
  return targets;
}

/**
 * Follows `export * from` and `export { name } from` until a file declares `name`. Adds declaring
 * files to `found` and the re-exporting files on the way to `barrels`.
 * @returns {boolean} whether `name` was traced to a declaring file from `file`
 */
function declaringFiles(ctx, file, name, found, barrels, seen) {
  const key = `${file}\0${name}`;
  if (seen.has(key)) return seen.get(key);
  seen.set(key, false);
  const text = ctx.tree.read(file);
  if (declaresName(text, name)) {
    found.add(file);
    seen.set(key, true);
    return true;
  }
  let traced = false;
  for (const match of text.matchAll(
    /\bexport\s+(?:type\s+)?(\*(?:\s+as\s+([\w$]+))?|\{[^}]*\})\s*from\s*["']([^"'\n]+)["']/g,
  )) {
    const [, clause, namespace, specifier] = match;
    const targets = resolveImport(ctx, file, specifier, new Set(["*"])).files;
    if (clause.startsWith("*") && !namespace) {
      for (const target of targets) {
        if (declaringFiles(ctx, target, name, found, barrels, seen)) traced = true;
      }
      continue;
    }
    const exported = namespace
      ? namespace === name && "*"
      : clause
          .slice(1, -1)
          .split(",")
          .map((part) =>
            part
              .trim()
              .replace(/^type\s+/, "")
              .split(/\s+as\s+/),
          )
          .find((pair) => (pair[1] ?? pair[0]).trim() === name)?.[0]
          .trim();
    if (!exported) continue;
    for (const target of targets) {
      if (exported === "*") found.add(target);
      if (exported === "*" || declaringFiles(ctx, target, exported, found, barrels, seen)) {
        traced = true;
      }
    }
  }
  if (traced) barrels.add(file);
  seen.set(key, traced);
  return traced;
}

/**
 * Whether `text` declares and exports `name` itself: a declaration (`export async function`,
 * `export const { name } = ...` and the like), a local `export { [type] x as name }`, or for
 * `default` an `export default`.
 */
function declaresName(text, name) {
  const word = name.replace(/\$/g, "\\$");
  const bound = String.raw`(?<![\w$])${word}(?![\w$])`;
  const declaration = String.raw`\bexport\s+(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?(?:const|let|var|function\s*\*?|class|type|interface|enum|namespace|module)\s*${bound}`;
  if (new RegExp(declaration).test(text)) return true;
  if (name === "default" && /\bexport\s+default\b/.test(text)) return true;
  if (destructuresName(text, word)) return true;
  return [...text.matchAll(/\bexport\s+(?:type\s+)?\{([^}]*)\}(?!\s*from)/g)].some((match) =>
    match[1].split(",").some(
      (part) =>
        part
          .trim()
          .replace(/^type\s+/, "")
          .split(/\s+as\s+/)
          .pop()
          .trim() === name,
    ),
  );
}

/**
 * Whether an `export const { ... } = ` or `export const [ ... ] = ` pattern binds `word`. A binding
 * stands after `{`, `[`, `,`, `:` or `...` and is not a key: in `{ S: renamed }` only `renamed` is
 * bound.
 */
function destructuresName(text, word) {
  const binding = new RegExp(String.raw`(?:^|[{[,:]|\.\.\.)\s*${word}(?![\w$])(?!\s*:)`);
  for (const match of text.matchAll(/\bexport\s+(?:const|let|var)\s*(?=[{[])/g)) {
    let depth = 0;
    let end = match.index + match[0].length;
    for (; end < text.length; end += 1) {
      if (text[end] === "{" || text[end] === "[") depth += 1;
      else if ((text[end] === "}" || text[end] === "]") && --depth === 0) break;
    }
    if (binding.test(text.slice(match.index + match[0].length + 1, end))) return true;
  }
  return false;
}

function resolveFile(tree, file) {
  const candidates = SOURCE_EXTENSIONS.map((extension) => file + extension);
  if (file.endsWith(".js")) candidates.unshift(file.replace(/\.js$/, ".ts"));
  return candidates.find((candidate) => tree.has(candidate));
}

/**
 * @param {ReadonlyArray<{ path: string; before?: string; after?: string; sameAsBase?: boolean; fromOldMain?: boolean }>} changes
 *   one entry per path the branch changes since the merge-base; `before`/`after` are the file
 *   texts (undefined when absent); `sameAsBase` says the branch's copy equals the base's copy;
 *   `fromOldMain` says the branch's copy is the one a commit already on main left there
 * @param {{ closure: ReadonlySet<string>; baseHasCheck: boolean; staleCommits?: number }} context
 *   `staleCommits`: the branch's commits that the base already holds as rebased copies
 * @returns {string[]} one reason per refused file
 */
function findSchemaChanges(changes, { closure, baseHasCheck, staleCommits = 0 }) {
  const reasons = [];
  if (staleCommits > 0) {
    reasons.push(
      `${staleCommits} commit(s) of this branch are already on main (it is based on an old main, ` +
        "or it copies main's commits), so rebase it onto main.",
    );
  }
  const also = staleCommits > 0 ? " (also rebase onto main)" : "";
  for (const change of changes) {
    const file = change.path;
    const guarded = CHECK_FILES.includes(file) || GITATTRIBUTES.test(file);
    // The branch carries main's copy (e.g. it branched before main was rebased): nothing to judge.
    if (guarded && change.sameAsBase) continue;
    // Main's own older copy, carried by commits already on main: only the rebase is needed.
    if (guarded && staleCommits > 0 && change.fromOldMain) {
      reasons.push(
        `${file}: differs from main's copy, because this branch needs a rebase onto main.`,
      );
      continue;
    }
    if (CHECK_FILES.includes(file)) {
      if (baseHasCheck) {
        reasons.push(
          `${file}: changes the schema check itself. Only the owner may do that: the check stays ` +
            `red, and the owner merges past it${also}.`,
        );
      }
      continue;
    }
    if (GITATTRIBUTES.test(file)) {
      reasons.push(
        `${file}: changes how git treats files, which can hide changes from review. Only the ` +
          `owner may do that${also}.`,
      );
      continue;
    }
    if (SCHEMA_ROOTS.some((pattern) => pattern.test(file)) || closure.has(file)) {
      reasons.push(`${file}: changes the database schema, its migrations or code they import`);
      continue;
    }
    if (!isShippedSource(file)) continue;
    const statement = [change.after, change.before].map(schemaStatement).find(Boolean);
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

/** The files of a commit (any import may name any file); texts are read only when the walk asks. */
function readTree(rev) {
  const blobs = new Map();
  for (const record of git(["ls-tree", "-r", "-z", "--full-tree", rev]).split("\0")) {
    const match = record.match(/^\d+ blob ([0-9a-f]+)\t(.+)$/s);
    if (match) blobs.set(match[2], match[1]);
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
 * The branch's commits (merge-base..head) that the base already holds as copies (rebased or
 * cherry-picked), matched by author, author time and subject: both keep all three. Needs no file
 * contents. Returns the branch-side commit ids.
 */
function staleCommits(mergeBase, base, head) {
  const ids = (range) =>
    git(["log", "--no-merges", "--format=%H%x00%an%x00%ae%x00%at%x00%s", range])
      .split("\n")
      .filter(Boolean)
      .map((line) => ({
        sha: line.slice(0, line.indexOf("\0")),
        id: line.slice(line.indexOf("\0")),
      }));
  const onBase = new Set(ids(`${mergeBase}..${base}`).map((commit) => commit.id));
  return ids(`${mergeBase}..${head}`)
    .filter((commit) => onBase.has(commit.id))
    .map((commit) => commit.sha);
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
  const stale = staleCommits(mergeBase, base, head);
  // A guarded file's copy that one of those commits left: main's older copy, not a branch edit.
  const fromOldMain = (entry) => stale.some((sha) => blobAt(sha, entry.path) === entry.newBlob);
  const changes = entries.map((entry) => ({
    path: entry.path,
    before: text(entry, entry.oldBlob),
    after: text(entry, entry.newBlob),
    sameAsBase: guarded(entry.path) ? blobAt(base, entry.path) === entry.newBlob : undefined,
    fromOldMain: guarded(entry.path) && stale.length > 0 ? fromOldMain(entry) : undefined,
  }));
  const trees = [readTree(mergeBase), readTree(head)];
  const traces = trees.map(traceSchema);
  const closure = new Set(traces.flatMap((trace) => [...trace.closure]));
  const reasons = [...new Set(traces.flatMap((trace) => trace.unresolved))];
  // Which directories are workspace packages decides what a package import resolves to.
  const [before, after] = trees.map(workspacePatterns);
  if (JSON.stringify(before) !== JSON.stringify(after)) {
    reasons.push(
      `${after.source ?? before.source}: changes which directories are workspace packages, which ` +
        "can re-point a package the schema code imports",
    );
  }
  reasons.push(
    ...findSchemaChanges(changes, {
      closure,
      baseHasCheck: treeHas(base, CHECK_SCRIPT),
      staleCommits: stale.length,
    }),
  );
  const outside = [...new Set(traces.flatMap((trace) => [...trace.outside]))].sort();
  console.log(
    `Schema code: ${closure.size} guarded files. Imports from outside this repository, not ` +
      `guarded: ${outside.join(", ") || "none"}.`,
  );
  if (reasons.length === 0) {
    console.log(`No database schema changes (${changes.length} changed files checked).`);
    return 0;
  }
  console.error("Fork changes must not change the database schema (no down-migrations exist):");
  for (const reason of reasons) console.error(`- ${reason}`);
  return 1;
}

module.exports = {
  findSchemaChanges,
  parseRawDiff,
  schemaClosure,
  traceSchema,
  CHECK_FILES,
  schemaStatement,
};

if (require.main === module) {
  process.exitCode = main(process.argv.slice(2));
}
