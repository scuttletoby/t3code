// Usage: fork-seam-check.cjs [--config-ref <rev>] <base> <head>. Refuses lines <head> adds to any
// upstream file since the merge-base beyond .github/fork-seams.json. A <base> without that list is
// upstream; else it is a pull request's base, and the list comes from it (or --config-ref), never
// from <head> or the working tree. Files are read as git blobs; nothing runs.

const { execFileSync } = require("node:child_process");

const CONFIG = ".github/fork-seams.json";

function git(args) {
  return execFileSync("git", ["-c", "core.quotePath=false", ...args], {
    encoding: "utf8",
    maxBuffer: 512 * 1024 * 1024,
  });
}

/** The file's text in `rev`, or undefined when `rev` has no such file. */
function textAt(rev, file) {
  const record = git(["ls-tree", "-z", "--full-tree", rev, "--", file]).split("\0")[0];
  const match = record.match(/^\d+ (\w+) ([0-9a-f]+)\t(.+)$/s);
  if (!match || match[3] !== file || match[1] !== "blob") return undefined;
  return git(["cat-file", "blob", match[2]]);
}

/** Counts of each non-blank line, trimmed. */
function lineCounts(text) {
  const counts = new Map();
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (line !== "") counts.set(line, (counts.get(line) ?? 0) + 1);
  }
  return counts;
}

/** Lines `head` has more often than `base`, as [line, extra copies]. */
function addedLines(base, head) {
  const baseCounts = lineCounts(base);
  return [...lineCounts(head)]
    .map(([line, count]) => [line, count - (baseCounts.get(line) ?? 0)])
    .filter(([, extra]) => extra > 0);
}

const total = (lines) => lines.reduce((sum, [, extra]) => sum + extra, 0);

/**
 * Reasons for the seam files' lines. A line is the fork's when `head` has it more often than `base`;
 * each listed line allows one. Not from upstream, `base` is main, whose copies spent the allowance.
 */
function findSeamBreaches(files, { atBase, atHead, upstream = false }) {
  const reasons = [];
  for (const [file, seam] of Object.entries(files)) {
    const base = atBase(file);
    // A listed file upstream does not have guards nothing: fail closed, so the list is fixed.
    if (base === undefined) {
      reasons.push(
        `${file}: listed in ${CONFIG}, but the base does not have it (upstream moved or removed it?).`,
      );
      continue;
    }
    const head = atHead(file);
    if (head === undefined) {
      reasons.push(`${file}: this change deletes or renames it.`);
      continue;
    }
    const allowed = lineCounts((seam.added ?? []).join("\n"));
    const baseCounts = lineCounts(base);
    for (const [line, added] of addedLines(base, head)) {
      const allowance = allowed.get(line) ?? 0;
      const left = upstream ? allowance : Math.max(0, allowance - (baseCounts.get(line) ?? 0));
      const extra = added - left;
      if (extra > 0) {
        reasons.push(`${file}: adds ${extra === 1 ? "" : `${extra}× `}\`${line}\``);
      }
    }
  }
  return reasons;
}

const FORK_SEGMENT = /^(fork|fork[-_.A-Z].*|FORK_.*)$/;

/** A path named as the fork's: a folder or file `fork`, `fork-*`, `fork_*`, `forkName*` or `FORK_*`. */
const forkNamed = (file) => file.split("/").some((segment) => FORK_SEGMENT.test(segment));

const escape = (text) => text.replace(/[.+?^${}()|[\]\\]/g, "\\$&");

// One "forkFiles" pattern as a test: `dir/` is everything under it, `**` followed by `/` any
// folders, `*` any characters but `/`.
function compileForkPattern(pattern) {
  const body = pattern
    .split("**/")
    .map((part) => part.split("*").map(escape).join("[^/]*"))
    .join("(?:.*/)?");
  const re = new RegExp(`^${body}${pattern.endsWith("/") ? "" : "$"}`);
  return (file) => re.test(file);
}

/**
 * Is `file` the fork's own when judged from the fork's main? A fork name or a "forkFiles" pattern
 * makes it so, a `!` pattern never. Only used where upstream is not at hand (a pull request's run).
 */
function forkFileTest(patterns = []) {
  const include = [];
  const exclude = [];
  for (const pattern of patterns) {
    (pattern.startsWith("!") ? exclude : include).push(
      compileForkPattern(pattern.replace(/^!/, "")),
    );
  }
  return (file) =>
    !exclude.some((test) => test(file)) && (forkNamed(file) || include.some((test) => test(file)));
}

/**
 * Changed files outside the seams. An upstream file in no list refuses every line it gains. A
 * "pending" one is only noted from upstream; on a pull request it may change but not grow. Not from
 * upstream, the base's files that `isForkFile` names are the fork's own.
 */
function findUnlistedChanges(config, { changed, atBase, atHead, upstream, isForkFile }) {
  const reasons = [];
  const notes = [];
  const pending = new Set(config.pending ?? []);
  for (const file of changed) {
    if (Object.hasOwn(config.files, file) || pending.has(file)) continue;
    const base = atBase(file);
    if (base === undefined || (!upstream && isForkFile(file))) continue;
    const added = total(addedLines(base, atHead(file) ?? ""));
    if (added > 0) {
      reasons.push(
        `${file}: upstream file with ${added} fork line${added === 1 ? "" : "s"} but no entry in ${CONFIG}` +
          (upstream
            ? "."
            : ' (if it is a fork file upstream does not have, add it to "forkFiles").'),
      );
    }
  }
  for (const file of pending) {
    const base = atBase(file);
    if (base === undefined) {
      reasons.push(
        `${file}: "pending" in ${CONFIG}, but the base does not have it (upstream moved or removed it?).`,
      );
      continue;
    }
    const head = atHead(file) ?? "";
    const added = total(addedLines(base, head));
    if (upstream) {
      if (added === 0)
        notes.push(`${file}: "pending" with no fork lines left; remove it from the list.`);
      continue;
    }
    const grows = added - total(addedLines(head, base));
    if (grows > 0) {
      reasons.push(
        `${file}: "pending" (fork lines to move out), and this change adds ${grows} more line${grows === 1 ? "" : "s"} than it removes.`,
      );
    }
  }
  return { reasons, notes };
}

/**
 * Upstream's files the head still has that a pull request's run would take for the fork's own (a
 * hole in that run). `upstreamFiles`: every path upstream has; `atHead`: a path's text in head.
 */
function findHiddenUpstreamFiles(upstreamFiles, isForkFile, atHead) {
  return upstreamFiles
    .filter((file) => isForkFile(file) && atHead(file) !== undefined)
    .map(
      (file) =>
        `${file}: upstream has it, but a pull request's run takes it for a fork file (its name or "forkFiles"); exclude it with "!${file}".`,
    );
}

const isStringList = (value) =>
  Array.isArray(value) && value.every((item) => typeof item === "string" && item.trim() !== "");

/** The config, checked; throws on a malformed config so the check never runs empty. */
function parseConfig(text) {
  const config = JSON.parse(text);
  const files = config?.files;
  if (typeof files !== "object" || files === null || Array.isArray(files)) {
    throw new Error(`${CONFIG}: "files" must be an object`);
  }
  for (const [file, seam] of Object.entries(files)) {
    if (typeof seam?.why !== "string" || seam.why.trim() === "") {
      throw new Error(`${CONFIG}: ${file} needs a "why"`);
    }
    if (!Array.isArray(seam.added) || seam.added.some((line) => typeof line !== "string")) {
      throw new Error(`${CONFIG}: ${file}: "added" must be a list of lines`);
    }
  }
  for (const key of ["pending", "forkFiles"]) {
    if (config[key] !== undefined && !isStringList(config[key])) {
      throw new Error(`${CONFIG}: "${key}" must be a list of paths`);
    }
  }
  for (const file of config.pending ?? []) {
    if (Object.hasOwn(files, file))
      throw new Error(`${CONFIG}: ${file} is in "files" and "pending"`);
  }
  for (const pattern of config.forkFiles ?? []) {
    const path = pattern.replace(/^!/, "");
    if (
      path === "" ||
      path.startsWith("/") ||
      path.split("/").includes("..") ||
      /^\**\/?$/.test(path)
    ) {
      throw new Error(`${CONFIG}: "forkFiles": not a repository path pattern: ${pattern}`);
    }
  }
  return { files, pending: config.pending ?? [], forkFiles: config.forkFiles ?? [] };
}

const hasConfig = (rev) => textAt(rev, CONFIG) !== undefined;

/** Reads `<rev>:.github/fork-seams.json`; throws when that commit has none. */
function readConfigAt(rev) {
  const text = textAt(rev, CONFIG);
  if (text === undefined) throw new Error(`${rev} has no ${CONFIG}`);
  return parseConfig(text);
}

function parseArgs(argv) {
  const args = [...argv];
  let configRef;
  const at = args.indexOf("--config-ref");
  if (at !== -1) {
    configRef = args[at + 1];
    if (!configRef || configRef.startsWith("--")) return undefined;
    args.splice(at, 2);
  }
  if (args.length !== 2 || args.some((arg) => arg.startsWith("--"))) return undefined;
  return { configRef, base: args[0], head: args[1] };
}

function main(argv) {
  const parsed = parseArgs(argv);
  if (!parsed) {
    console.error("Usage: fork-seam-check.cjs [--config-ref <rev>] <base> <head>");
    return 2;
  }
  const { base, head } = parsed;
  // `base` without the list is upstream; the fork's main always has it.
  const upstream = !hasConfig(base);
  const configRef = parsed.configRef ?? (upstream ? head : base);
  const config = readConfigAt(configRef);
  const mergeBase = git(["merge-base", base, head]).trim();
  const atBase = (file) => textAt(mergeBase, file);
  const atHead = (file) => textAt(head, file);
  const changed = git(["diff", "--no-renames", "--name-only", "-z", mergeBase, head])
    .split("\0")
    .filter(Boolean);
  const isForkFile = forkFileTest(config.forkFiles);
  const unlisted = findUnlistedChanges(config, { changed, atBase, atHead, upstream, isForkFile });
  const reasons = [
    ...findSeamBreaches(config.files, { atBase, atHead, upstream }),
    ...unlisted.reasons,
    ...(upstream
      ? findHiddenUpstreamFiles(
          git(["ls-tree", "-r", "-z", "--name-only", mergeBase]).split("\0").filter(Boolean),
          isForkFile,
          atHead,
        )
      : []),
  ];
  const from = parsed.configRef ? `--config-ref ${configRef}` : upstream ? "the head" : "the base";
  for (const note of unlisted.notes) console.log(`Note: ${note}`);
  if (!upstream && textAt(head, CONFIG) !== textAt(mergeBase, CONFIG)) {
    console.log(
      `Note: this change edits ${CONFIG}; it is judged by the list in ${from}, not its own. ` +
        "A new seam or list entry takes effect once the owner merges it.",
    );
  }
  const counted = upstream ? "every upstream file" : "every upstream file it changes";
  if (reasons.length === 0) {
    console.log(
      `Fork lines in upstream files: only the pinned seams (${counted}; list from ${from}).`,
    );
    return 0;
  }
  console.error(
    `Fork changes to upstream files must stay within the seam lines in ${CONFIG} (list from ${from}):`,
  );
  for (const reason of reasons) console.error(`- ${reason}`);
  console.error(
    "Put the change in a file of the fork's own (a fork setting goes in " +
      "packages/contracts/src/forkSettings.ts; see docs/fork/updates.md). A new seam line needs " +
      `its own entry in ${CONFIG}, which the owner approves.`,
  );
  return 1;
}

module.exports = {
  CONFIG,
  compileForkPattern,
  findHiddenUpstreamFiles,
  findSeamBreaches,
  findUnlistedChanges,
  forkFileTest,
  lineCounts,
  parseConfig,
};

if (require.main === module) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    console.error(`fork-seam-check crashed: ${error instanceof Error ? error.message : error}`);
    process.exitCode = 3;
  }
}
