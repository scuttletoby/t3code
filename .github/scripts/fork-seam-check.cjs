// Usage: fork-seam-check.cjs [--config-ref <rev>] [--after-rebase] <base> <head>. Refuses <head>'s
// upstream-file changes since the merge-base beyond .github/fork-seams.json, read from <base> (or
// --config-ref; never <head>); a <base> without it is upstream. --after-rebase (fork-follow): a
// "pending" file below its ceiling is a note (see findUnlistedChanges). Reads git objects only.

const { execFileSync } = require("node:child_process");

const CONFIG = ".github/fork-seams.json";
// Upstream workflow files the fork deletes (fork-follow.sh keeps them deleted): with upstream's
// fork-*.yml, which fork-follow makes the fork delete (that list cannot name them), the only upstream
// files a change may delete.
const REMOVED = ".github/fork-removed-workflows.txt";
// The refusals of a "pending" ceiling left above a file's fork lines (fixed by lowering it).
const CEILING_TOO_HIGH =
  /: "pending" with (no fork lines left|\d+ fork lines, fewer than its ceiling)/;
const REMOVED_PATH = /^\.github\/workflows\/[^/,\s]+\.ya?ml$/;
const FORK_WORKFLOW = /^\.github\/workflows\/fork-[^/]*\.ya?ml$/;

function git(args) {
  return execFileSync("git", ["-c", "core.quotePath=false", ...args], {
    encoding: "utf8",
    maxBuffer: 512 * 1024 * 1024,
  });
}

/** The tree entry for `file` in `rev` ({ type, id }), or undefined. Reads trees only. */
function entryAt(rev, file) {
  const record = git(["ls-tree", "-z", "--full-tree", rev, "--", file]).split("\0")[0];
  const match = record.match(/^\d+ (\w+) ([0-9a-f]+)\t(.+)$/s);
  return match && match[3] === file ? { type: match[1], id: match[2] } : undefined;
}

/**
 * The file's text in `rev`, or undefined when `rev` has no such file. A submodule (gitlink) reads
 * as the line `git diff` shows for it, so a moved pointer counts as a changed line.
 */
function textAt(rev, file) {
  const entry = entryAt(rev, file);
  if (entry?.type === "commit") return `Subproject commit ${entry.id}\n`;
  if (entry?.type !== "blob") return undefined;
  return git(["cat-file", "blob", entry.id]);
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
 * Whether `head` keeps `base`'s lines in their order, whitespace and blank lines aside (as
 * `git diff --ignore-all-space --ignore-blank-lines` sees no change).
 */
function sameLines(base, head) {
  const lines = (text) =>
    text
      .split("\n")
      .map((line) => line.replace(/\s+/g, ""))
      .filter(Boolean);
  const [a, b] = [lines(base), lines(head)];
  return a.length === b.length && a.every((line, i) => line === b[i]);
}

/** The upstream workflow paths a REMOVED list names; other lines are ignored. */
function parseRemoved(text = "") {
  return new Set(
    text
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => REMOVED_PATH.test(line)),
  );
}

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
 * Changed files outside the seams. An unlisted upstream file may not change or go (unless `removed`
 * names it). A "pending" one holds exactly its ceiling from upstream; on a PR (`isForkFile`
 * names fork files) it may change but not grow. `afterRebase`: below the ceiling (an adopted fork
 * line drops in the rebase; the old base forbids lowering first) is a note, refused on the next run.
 */
function findUnlistedChanges(
  config,
  { changed, atBase, atHead, upstream, isForkFile, removed = new Set(), afterRebase = false },
) {
  const reasons = [];
  const notes = [];
  const pending = config.pending ?? {};
  const deleted = (file) =>
    `${file}: upstream file this change deletes (or renames); only the workflow files ${REMOVED} lists (and upstream fork-*.yml) may go.`;
  for (const file of changed) {
    if (Object.hasOwn(config.files, file) || Object.hasOwn(pending, file)) continue;
    const base = atBase(file);
    if (base === undefined || (!upstream && isForkFile(file))) continue;
    const head = atHead(file);
    if (head === undefined) {
      if (!removed.has(file) && !FORK_WORKFLOW.test(file)) reasons.push(deleted(file));
      continue;
    }
    const added = total(addedLines(base, head));
    const entry = upstream
      ? "."
      : ' (if it is a fork file upstream does not have, add it to "forkFiles").';
    if (added > 0) {
      reasons.push(
        `${file}: upstream file with ${added} fork line${added === 1 ? "" : "s"} but no entry in ${CONFIG}${entry}`,
      );
    } else if (!sameLines(base, head)) {
      reasons.push(
        `${file}: upstream file whose lines this change deletes or moves, but no entry in ${CONFIG}${entry}`,
      );
    }
  }
  for (const [file, ceiling] of Object.entries(pending)) {
    const base = atBase(file);
    if (base === undefined) {
      reasons.push(
        `${file}: "pending" in ${CONFIG}, but the base does not have it (upstream moved or removed it?).`,
      );
      continue;
    }
    const head = atHead(file);
    if (head === undefined) {
      reasons.push(deleted(file));
      continue;
    }
    const added = total(addedLines(base, head));
    if (upstream) {
      if (added > ceiling) {
        reasons.push(
          `${file}: "pending" with ${added} fork lines, more than its ceiling of ${ceiling} (fork lines to move out, not add).`,
        );
      } else if (added < ceiling) {
        // A ceiling left too high is room to swap upstream lines for fork lines later, and a note
        // in the nightly log is never seen: refuse, naming the fix (only noted right after a
        // rebase; the next run refuses it).
        (afterRebase ? notes : reasons).push(
          added === 0
            ? `${file}: "pending" with no fork lines left; remove it from the list.`
            : `${file}: "pending" with ${added} fork lines, fewer than its ceiling of ${ceiling}; lower it to ${added}.`,
        );
      }
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
  if (config.forkFiles !== undefined && !isStringList(config.forkFiles)) {
    throw new Error(`${CONFIG}: "forkFiles" must be a list of paths`);
  }
  const pending = config.pending ?? {};
  if (typeof pending !== "object" || pending === null || Array.isArray(pending)) {
    throw new Error(`${CONFIG}: "pending" must map each path to its ceiling of fork lines`);
  }
  for (const [file, ceiling] of Object.entries(pending)) {
    if (file.trim() === "" || !Number.isSafeInteger(ceiling) || ceiling < 1) {
      throw new Error(`${CONFIG}: "pending": ${file} needs a ceiling of 1 or more fork lines`);
    }
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
  return { files, pending, forkFiles: config.forkFiles ?? [] };
}

const hasConfig = (rev) => entryAt(rev, CONFIG)?.type === "blob";

/** Reads `<rev>:.github/fork-seams.json`; throws when that commit has none. */
function readConfigAt(rev) {
  const text = textAt(rev, CONFIG);
  if (text === undefined) throw new Error(`${rev} has no ${CONFIG}`);
  return parseConfig(text);
}

function parseArgs(argv) {
  const args = [...argv];
  const afterRebase = args.includes("--after-rebase");
  if (afterRebase) args.splice(args.indexOf("--after-rebase"), 1);
  let configRef;
  const at = args.indexOf("--config-ref");
  if (at !== -1) {
    configRef = args[at + 1];
    if (!configRef || configRef.startsWith("--")) return undefined;
    args.splice(at, 2);
  }
  if (args.length !== 2 || args.some((arg) => arg.startsWith("--"))) return undefined;
  return { configRef, afterRebase, base: args[0], head: args[1] };
}

function main(argv) {
  const parsed = parseArgs(argv);
  if (!parsed) {
    console.error("Usage: fork-seam-check.cjs [--config-ref <rev>] [--after-rebase] <base> <head>");
    return 2;
  }
  const { base, head } = parsed;
  // `base` without the list is upstream; the fork's main always has it. --config-ref is only for
  // a pull request's run, so it never runs from upstream.
  const upstream = !parsed.configRef && !hasConfig(base);
  const mergeBase = git(["merge-base", base, head]).trim();
  if (!upstream) {
    // A pull request's base and merge-base are the fork's: without the list, the whole old series
    // (or a base branch that dropped the list) would count as this change's lines.
    const stale = !hasConfig(base)
      ? `The base ${base} has no ${CONFIG}: open the pull request against main or a branch of it.`
      : !hasConfig(mergeBase)
        ? `This branch predates the last rebase of its base onto upstream (the merge-base ${mergeBase.slice(0, 12)} has no ${CONFIG}). Update the branch from its base; the seam check then judges only its own lines.`
        : undefined;
    if (stale) {
      console.error(stale);
      return 1;
    }
  }
  const configRef = parsed.configRef ?? (upstream ? head : base);
  const config = readConfigAt(configRef);
  const removed = parseRemoved(textAt(configRef, REMOVED));
  const atBase = (file) => textAt(mergeBase, file);
  const atHead = (file) => textAt(head, file);
  const changed = git(["diff", "--no-renames", "--name-only", "-z", mergeBase, head])
    .split("\0")
    .filter(Boolean);
  const isForkFile = forkFileTest(config.forkFiles);
  const unlisted = findUnlistedChanges(config, {
    changed,
    atBase,
    atHead,
    upstream,
    isForkFile,
    removed,
    afterRebase: parsed.afterRebase,
  });
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
  const ceilings = reasons.filter((reason) => CEILING_TOO_HIGH.test(reason)).length;
  if (ceilings < reasons.length) {
    console.error(
      "Put the change in a file of the fork's own (a fork setting goes in " +
        "packages/contracts/src/forkSettings.ts; see docs/fork/updates.md). A new seam line needs " +
        `its own entry in ${CONFIG}, which the owner approves.`,
    );
  }
  if (ceilings > 0) {
    console.error(
      `Lower each "pending" ceiling named above (or remove the entry) in ${CONFIG} with a pull request to main.`,
    );
  }
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
  parseRemoved,
  sameLines,
};

if (require.main === module) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    console.error(`fork-seam-check crashed: ${error instanceof Error ? error.message : error}`);
    process.exitCode = 3;
  }
}
