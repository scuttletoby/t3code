// Usage: fork-seam-check.cjs <base> <head>. Refuses lines <head> adds to an upstream file since the
// merge-base beyond that file's seam lines in .github/fork-seams.json (each listed once; blank lines
// and indentation ignored). <base>: the upstream commit for the fork's whole change, or a PR's base.
// Files are read as git blobs; nothing runs.

const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

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

/**
 * One reason per refused line or file. `files`: the config's `files` object. `atBase`/`atHead`: a
 * listed file's text there, undefined when absent. A line is the fork's when `head` has it more
 * often than `base`; each listed line allows one.
 */
function findSeamBreaches(files, { atBase, atHead }) {
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
    for (const [line, count] of lineCounts(head)) {
      const extra = count - (baseCounts.get(line) ?? 0) - (allowed.get(line) ?? 0);
      if (extra > 0) {
        reasons.push(`${file}: adds ${extra === 1 ? "" : `${extra}× `}\`${line}\``);
      }
    }
  }
  return reasons;
}

/** The config's `files`, checked; throws on a malformed config so the check never runs empty. */
function readConfig(root) {
  const config = JSON.parse(fs.readFileSync(path.join(root, CONFIG), "utf8"));
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
  return files;
}

function main(argv) {
  const [base, head] = argv;
  if (!base || !head) {
    console.error("Usage: fork-seam-check.cjs <base> <head>");
    return 2;
  }
  const files = readConfig(git(["rev-parse", "--show-toplevel"]).trim());
  const mergeBase = git(["merge-base", base, head]).trim();
  const reasons = findSeamBreaches(files, {
    atBase: (file) => textAt(mergeBase, file),
    atHead: (file) => textAt(head, file),
  });
  const listed = Object.keys(files).length;
  if (reasons.length === 0) {
    console.log(`Fork lines in upstream files: only the pinned seams (${listed} files checked).`);
    return 0;
  }
  console.error(
    "Fork changes to upstream files must stay within the seam lines in " + CONFIG + ":",
  );
  for (const reason of reasons) console.error(`- ${reason}`);
  console.error(
    "Put the change in a file of the fork's own (a fork setting goes in " +
      "packages/contracts/src/forkSettings.ts; see docs/fork/updates.md). A new seam line needs " +
      `its own entry in ${CONFIG}, which the owner approves.`,
  );
  return 1;
}

module.exports = { CONFIG, findSeamBreaches, lineCounts, readConfig };

if (require.main === module) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    console.error(`fork-seam-check crashed: ${error instanceof Error ? error.message : error}`);
    process.exitCode = 3;
  }
}
