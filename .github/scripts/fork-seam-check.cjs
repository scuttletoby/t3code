// Usage: [--config-ref <rev>] <base> <head> | --from-upstream [--after-rebase] <upstream> <head>.
// Refuses <head>'s upstream-file changes beyond .github/fork-seams.json. A pull request's list is
// <base>'s (or --config-ref's), never <head>'s; --from-upstream (fork-follow) reads <head>'s, even if
// upstream has its own. --after-rebase: a ceiling above the count is a note. Reads git objects only.

const { execFileSync } = require("node:child_process");

const CONFIG = ".github/fork-seams.json";
// Upstream workflow files the fork deletes (fork-follow.sh keeps them deleted): with upstream's
// fork-*.yml, which fork-follow makes the fork delete (that list cannot name them), the only upstream
// files a change may delete.
const REMOVED = ".github/fork-removed-workflows.txt";
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
 * The non-blank lines in order, with their indentation (in YAML or Python it is meaning) but no
 * trailing blanks or CR (as `git diff --ignore-space-at-eol --ignore-blank-lines` compares them).
 */
const orderedLines = (text) =>
  text
    .split("\n")
    .map((line) => line.trimEnd())
    .filter(Boolean);

/** Whether `head` keeps `base`'s lines, in their order and indentation (blank lines aside). */
function sameLines(base, head) {
  const [a, b] = [orderedLines(base), orderedLines(head)];
  return a.length === b.length && a.every((line, i) => line === b[i]);
}

/**
 * How many of the lines `base` lists `head` does not keep in their order: deleted, moved or changed
 * (`base`'s length less the longest common subsequence). Common ends are matched first, so the work
 * is the changed middle squared (a few ms for a 10,000-line file with fork lines at both ends).
 */
function lostLines(base, head) {
  let start = 0;
  while (start < base.length && start < head.length && base[start] === head[start]) start++;
  let end = 0;
  while (
    end < base.length - start &&
    end < head.length - start &&
    base[base.length - 1 - end] === head[head.length - 1 - end]
  ) {
    end++;
  }
  const ids = new Map();
  const id = (line) => ids.get(line) ?? ids.set(line, ids.size).get(line);
  const a = base.slice(start, base.length - end).map(id);
  const b = head.slice(start, head.length - end).map(id);
  let previous = new Uint32Array(b.length + 1);
  let row = new Uint32Array(b.length + 1);
  for (const line of a) {
    for (let j = 1; j <= b.length; j++) {
      row[j] = line === b[j - 1] ? previous[j - 1] + 1 : Math.max(previous[j], row[j - 1]);
    }
    [previous, row] = [row, previous];
  }
  return a.length - previous[b.length];
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
    const head = atHead(file);
    // Neither has it (upstream renamed it): nothing to guard, a note (findListedGone).
    if (base === undefined && head === undefined) continue;
    // A listed file upstream does not have guards nothing: fail closed, so the list is fixed.
    if (base === undefined) {
      reasons.push(
        `${file}: listed in ${CONFIG}, but the base does not have it (upstream moved or removed it?).`,
      );
      continue;
    }
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
 * names it). A "pending" one holds exactly its ceiling from upstream (one too high goes to
 * `ceilings`; after a rebase to `notes`, as the old base forbade lowering it first); on a PR
 * (`isForkFile` names fork files) it may change but not grow.
 */
function findUnlistedChanges(
  config,
  { changed, atBase, atHead, upstream, isForkFile, removed = new Set(), afterRebase = false },
) {
  const reasons = [];
  const ceilings = [];
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
        `${file}: upstream file whose lines this change deletes, moves or re-indents, but no entry in ${CONFIG}${entry}`,
      );
    }
  }
  for (const [file, ceiling] of Object.entries(pending)) {
    const base = atBase(file);
    const head = atHead(file);
    if (base === undefined && head === undefined) continue;
    if (base === undefined) {
      reasons.push(
        `${file}: "pending" in ${CONFIG}, but the base does not have it (upstream moved or removed it?).`,
      );
      continue;
    }
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
        (afterRebase ? notes : ceilings).push(
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
  return { reasons, ceilings, notes };
}

/**
 * Upstream lines a seam or pending file lost: `lines` not kept in order and indentation, `gone`
 * whose text is gone (so a line already moved or re-indented cannot be deleted later for free).
 * From upstream both are held exactly to its "lost" entry (0 when unlisted), like a ceiling.
 */
function findLostLines(config, { atBase, atHead, upstream, afterRebase = false }) {
  const reasons = [];
  const ceilings = [];
  const notes = [];
  const lost = config.lost ?? {};
  const files = Object.entries(config.files).map(([file, seam]) => [
    file,
    lineCounts(seam.added.join("\n")),
  ]);
  if (upstream) for (const file of Object.keys(config.pending ?? {})) files.push([file, new Map()]);
  const say = (count, what) => `${count} upstream line${count === 1 ? "" : "s"} ${what}`;
  const what = {
    lines: "deleted, moved or re-indented",
    gone: "deleted or changed (its text is gone)",
  };
  for (const [file, listed] of files) {
    const base = atBase(file);
    const head = atHead(file);
    if (base === undefined || head === undefined) continue; // Refused (or noted) elsewhere.
    const lines = lostLines(orderedLines(base), orderedLines(head));
    const gone = addedLines(head, base); // [line, copies the head has fewer of]
    if (!upstream) {
      // Main's pending lines mix with upstream's, so only seam files: their listed lines may go,
      // not move (a moved `}` looks like upstream's).
      const dropped = gone.reduce(
        (sum, [line, copies]) => sum + Math.min(copies, listed.get(line) ?? 0),
        0,
      );
      const count = lines - dropped;
      if (count > 0) {
        reasons.push(
          `${file}: ${say(count, what.lines)} by this change (only its listed seam lines may go, and none may move: a moved copy looks like upstream's line).`,
        );
      }
      continue;
    }
    const counts = { lines, gone: total(gone) };
    const held = lost[file] ?? { lines: 0, gone: 0 };
    const below = [];
    for (const key of ["lines", "gone"]) {
      const text = say(counts[key], what[key]);
      if (counts[key] > held[key]) {
        reasons.push(
          `${file}: ${text}, more than its "lost" ${key} count of ${held[key]} (keep upstream's lines; a fork change goes in a file of the fork's own).`,
        );
      } else if (counts[key] < held[key]) {
        below.push(
          `${file}: ${text}, below its "lost" ${key} count of ${held[key]}; lower it to ${counts[key]}.`,
        );
      }
    }
    if (below.length > 0) {
      (afterRebase ? notes : ceilings).push(
        ...(lines === 0
          ? [
              `${file}: no upstream lines ${what.lines}, below its "lost" entry; remove it from "lost".`,
            ]
          : below),
      );
    }
  }
  return { reasons, ceilings, notes };
}

/**
 * Listed seam and pending paths that neither the base nor the head has: nothing there to guard.
 * Upstream renamed (or removed) the file, so the list names both paths until the base moves on, and
 * the checks before and after fork-follow's rebase each see one. A note, not a refusal.
 */
function findListedGone(config, { atBase, atHead }) {
  return [...Object.keys(config.files), ...Object.keys(config.pending ?? {})]
    .filter((file) => atBase(file) === undefined && atHead(file) === undefined)
    .map(
      (file) =>
        `${file}: listed in ${CONFIG}, but neither the base nor the head has it (renamed or removed upstream?); remove the entry once the fork's base no longer has it.`,
    );
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

const KINDS = ["files", "pending", "forkFiles", "lost"];
const SEAM_KEYS = ["why", "added"];

/** Refuses a key the check does not read (a misspelt block guards nothing); "//…" is a comment. */
function refuseUnknownKeys(object, known, where) {
  for (const key of Object.keys(object)) {
    if (!key.startsWith("//") && !known.includes(key)) {
      throw new Error(`${CONFIG}: ${where}unknown key "${key}" (known: ${known.join(", ")})`);
    }
  }
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
  refuseUnknownKeys(config, KINDS, "");
  for (const [file, seam] of Object.entries(files)) {
    if (typeof seam?.why !== "string" || seam.why.trim() === "") {
      throw new Error(`${CONFIG}: ${file} needs a "why"`);
    }
    if (!Array.isArray(seam.added) || seam.added.some((line) => typeof line !== "string")) {
      throw new Error(`${CONFIG}: ${file}: "added" must be a list of lines`);
    }
    refuseUnknownKeys(seam, SEAM_KEYS, `${file}: `);
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
  const lost = config.lost ?? {};
  if (typeof lost !== "object" || lost === null || Array.isArray(lost)) {
    throw new Error(`${CONFIG}: "lost" must map each path to its count of upstream lines lost`);
  }
  for (const [file, entry] of Object.entries(lost)) {
    if (!Object.hasOwn(files, file) && !Object.hasOwn(pending, file)) {
      throw new Error(`${CONFIG}: "lost": ${file} is not in "files" or "pending"`);
    }
    // `gone` (text gone) is at most `lines` (not kept in order) for any file, so more never matches.
    const { lines, gone, ...other } = entry ?? {};
    if (
      Object.keys(other).length > 0 ||
      !Number.isSafeInteger(lines) ||
      lines < 1 ||
      !Number.isSafeInteger(gone) ||
      gone < 0 ||
      gone > lines
    ) {
      throw new Error(
        `${CONFIG}: "lost": ${file} needs {"lines": N, "gone": M}, with N at least 1 and M from 0 to N`,
      );
    }
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
  return { files, pending, forkFiles: config.forkFiles ?? [], lost };
}

/**
 * Why a pull request's own list cannot become main's (its format only: its rules are not in force
 * here), or undefined. Merged, a malformed list crashes every later run, the fix's run included.
 */
function ownListProblem(text) {
  const own = `this change's own ${CONFIG}`;
  if (text === undefined) return `${own}: this change deletes it.`;
  try {
    parseConfig(text);
    return undefined;
  } catch (error) {
    return `${own} is malformed: ${error instanceof Error ? error.message : error}`;
  }
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
  const flag = (name) => {
    const at = args.indexOf(name);
    if (at !== -1) args.splice(at, 1);
    return at !== -1;
  };
  const fromUpstream = flag("--from-upstream");
  const afterRebase = flag("--after-rebase");
  let configRef;
  const at = args.indexOf("--config-ref");
  if (at !== -1) {
    configRef = args[at + 1];
    if (!configRef || configRef.startsWith("--")) return undefined;
    args.splice(at, 2);
  }
  if (args.length !== 2 || args.some((arg) => arg.startsWith("--"))) return undefined;
  // A pull request's run never runs from upstream; only a run from upstream has a rebase behind it.
  if (fromUpstream ? configRef !== undefined : afterRebase) return undefined;
  return { configRef, fromUpstream, afterRebase, base: args[0], head: args[1] };
}

function main(argv) {
  const parsed = parseArgs(argv);
  if (!parsed) {
    console.error(
      "Usage: fork-seam-check.cjs [--config-ref <rev>] <base> <head>\n" +
        "       fork-seam-check.cjs --from-upstream [--after-rebase] <upstream> <head>",
    );
    return 2;
  }
  const { base, head } = parsed;
  // Only the caller knows that `base` is upstream (fork-follow says so): a base without the list
  // used to be taken for upstream, and an upstream that has one (a fork of this fork) for main.
  const upstream = parsed.fromUpstream;
  const mergeBase = git(["merge-base", base, head]).trim();
  if (!upstream) {
    // A pull request's base and merge-base are the fork's: without the list, the whole old series
    // (or a base branch that dropped the list) would count as this change's lines.
    const stale = !hasConfig(base)
      ? `The base ${base} has no ${CONFIG}: open the pull request against main or a branch of it (a run from an upstream commit passes --from-upstream).`
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
  const lost = findLostLines(config, { atBase, atHead, upstream, afterRebase: parsed.afterRebase });
  // A ceiling above the count is fixed by lowering it, not by moving lines: its own hint.
  const ceilings = [...unlisted.ceilings, ...lost.ceilings];
  const reasons = [
    ...findSeamBreaches(config.files, { atBase, atHead, upstream }),
    ...lost.reasons,
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
  const notes = [...findListedGone(config, { atBase, atHead }), ...unlisted.notes, ...lost.notes];
  for (const note of notes) console.log(`Note: ${note}`);
  const ownList = textAt(head, CONFIG);
  const editsList = !upstream && ownList !== textAt(mergeBase, CONFIG);
  const listProblem = editsList ? ownListProblem(ownList) : undefined;
  if (editsList) {
    console.log(
      `Note: this change edits ${CONFIG}; it is judged by the list in ${from}, not its own. ` +
        "A new seam or list entry takes effect once the owner merges it.",
    );
  }
  const counted = upstream ? "every upstream file" : "every upstream file it changes";
  if (listProblem !== undefined) {
    console.error(listProblem);
    console.error(
      `Fix the list in this change: merged, it is main's list, and a malformed one stops every later pull request and fork-follow run.`,
    );
  }
  if (reasons.length === 0 && ceilings.length === 0) {
    if (listProblem !== undefined) return 1;
    console.log(
      `Fork lines in upstream files: only the pinned seams (${counted}; list from ${from}).`,
    );
    return 0;
  }
  console.error(
    `Fork changes to upstream files must stay within the seam lines in ${CONFIG} (list from ${from}):`,
  );
  for (const reason of [...reasons, ...ceilings]) console.error(`- ${reason}`);
  if (reasons.length > 0) {
    console.error(
      "Put the change in a file of the fork's own (a fork setting goes in " +
        "packages/contracts/src/forkSettings.ts; see docs/fork/updates.md). A new seam line needs " +
        `its own entry in ${CONFIG}, which the owner approves.`,
    );
  }
  if (ceilings.length > 0) {
    console.error(
      `Lower each "pending" ceiling and "lost" count named above (or remove the entry) in ${CONFIG} with a pull request to main.`,
    );
  }
  return 1;
}

module.exports = {
  CONFIG,
  compileForkPattern,
  findHiddenUpstreamFiles,
  findListedGone,
  findLostLines,
  findSeamBreaches,
  findUnlistedChanges,
  forkFileTest,
  lineCounts,
  lostLines,
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
