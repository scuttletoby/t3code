// Usage: [--config-ref <rev>] <base> <head> | --from-upstream [--after-rebase [--lowered-list <path>]] <upstream> <head>.
// Refuses <head>'s upstream-file changes beyond .github/fork-seams.json: a PR uses <base>'s list (its own only for
// "pending" ceilings it must lower); --from-upstream reads <head>'s. --after-rebase notes a too-high ceiling and
// --lowered-list writes the lowered list. Reads git objects only (and writes only that file).

const { execFileSync } = require("node:child_process");
const fs = require("node:fs");

const CONFIG = ".github/fork-seams.json";
// Upstream workflow files the fork deletes (fork-follow.sh keeps them deleted): with upstream's
// fork-*.yml, which fork-follow makes the fork delete (that list cannot name them), the only upstream
// files a change may delete.
const REMOVED = ".github/fork-removed-workflows.txt";
const REMOVED_PATH = /^\.github\/workflows\/[^/,\s]+\.ya?ml$/;
const FORK_WORKFLOW = /^\.github\/workflows\/fork-[^/]*\.ya?ml$/;
// Generated upstream files: the folder each is generated from, and the fork test that proves the
// file is the generator's output (a hand edit fails it). A fork route repeats upstream's lines
// (`} as any)`), so the file is not judged line by line when its folder changes too.
const GENERATED = new Map([
  [
    "apps/web/src/routeTree.gen.ts",
    { source: "apps/web/src/routes/", proof: "apps/web/src/forkRouteTree.test.ts" },
  ],
]);

function git(args) {
  return execFileSync("git", ["-c", "core.quotePath=false", ...args], {
    encoding: "utf8",
    maxBuffer: 512 * 1024 * 1024,
  });
}

/**
 * The committer date (YYYY-MM-DD) of the commit `rev` names. A tag is peeled (`git show <tag>`
 * prints the tag before the date); anything but a day throws, as no day compares as "not expired".
 */
function commitDay(rev) {
  const day = git(["show", "-s", "--format=%cs", `${rev}^{commit}`]).trim();
  if (!isDay(day)) {
    throw new Error(
      `${rev}: no commit date (git show --format=%cs printed ${JSON.stringify(day.slice(0, 40))}); announcements are dated by it.`,
    );
  }
  return day;
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

/** A seam entry's lines: its "added" ones and those it announces (step 1 of a new line). */
const seamLines = (seam) => [...seam.added, ...(seam.announced?.added ?? [])];

// Control characters, then those that print as nothing or reorder text: soft hyphen, bidi marks,
// Hangul fillers, zero-width characters, variation selectors and tag characters.
const UNPRINTABLE =
  /[\u0000-\u0008\u000a-\u001f\u007f-\u009f\u00ad\u061c\u115f\u1160\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2069\u3164\ufe00-\ufe0f\ufeff\uffa0\u{e0000}-\u{e007f}\u{e0100}-\u{e01ef}]/gu;

/**
 * `text` with those characters escaped: a list's key with a newline cannot start a log line, and
 * a bidi or invisible character shows as itself, not as the text it reorders or hides.
 */
const printable = (text) =>
  String(text).replace(UNPRINTABLE, (char) => {
    const hex = char.codePointAt(0).toString(16);
    return hex.length > 4 ? `\\u{${hex}}` : `\\u${hex.padStart(4, "0")}`;
  });

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
 * Reasons for the seam files' lines: each listed line allows one copy more than `base` has (on a PR, main).
 * On `today` (a PR's run: the list's commit date) an expired or dated-ahead announcement allows nothing; its
 * in-date copies go to `late`. `atTip` (a PR's run: the base tip) spends the allowance too, so two branches
 * from one merge-base cannot each add the one listed copy once the first has merged.
 */
function findSeamBreaches(files, { atBase, atHead, atTip, upstream = false, today, late }) {
  const reasons = [];
  const lateReasons = late ?? reasons;
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
    const since = seam.announced?.since;
    const expired =
      today !== undefined &&
      since !== undefined &&
      (isExpired(since, today) || isAhead(since, today));
    const allowed = lineCounts((expired ? seam.added : seamLines(seam)).join("\n"));
    const inDate = expired ? lineCounts(seamLines(seam).join("\n")) : allowed;
    const baseCounts = lineCounts(base);
    const tip = upstream || atTip === undefined ? undefined : atTip(file);
    const tipCounts = tip === undefined ? new Map() : lineCounts(tip);
    const spent = (line) => Math.max(baseCounts.get(line) ?? 0, tipCounts.get(line) ?? 0);
    const leftOf = (counts, line) => {
      const allowance = counts.get(line) ?? 0;
      return upstream ? allowance : Math.max(0, allowance - spent(line));
    };
    // The hint only where the base branch's newer copies alone make these too many: judged from
    // the merge-base (the copies the announcement allows in date), the branch would pass.
    // `counts` is the allowance the copies are judged by: in date for `late`, else `allowed` (an
    // expired announcement allows nothing from the merge-base either, so updating would not help).
    const adds = (copies, line, added, counts) =>
      `${file}: adds ${copies === 1 ? "" : `${copies}× `}\`${line}\`${
        (tipCounts.get(line) ?? 0) > (baseCounts.get(line) ?? 0) &&
        added <= (counts.get(line) ?? 0) - (baseCounts.get(line) ?? 0)
          ? " (the base branch has more copies than this branch's merge-base: update the branch from its base)"
          : ""
      }`;
    for (const [line, added] of addedLines(base, head)) {
      const left = leftOf(allowed, line);
      const extra = added - left;
      if (extra <= 0) continue;
      // Only the copies the announcement would allow in date are its to fix; the rest are refused
      // either way.
      const lateCopies = Math.min(extra, leftOf(inDate, line) - left);
      if (lateCopies > 0) {
        const when = isAhead(since, today)
          ? "is dated after"
          : `is more than ${ANNOUNCED_DAYS} days before`;
        lateReasons.push(
          `${adds(lateCopies, line, added, inDate)} (its announcement of ${since} ${when} ${today}, the list's commit date: announce it again with a list-only pull request)`,
        );
      }
      if (extra > lateCopies) reasons.push(adds(extra - lateCopies, line, added, allowed));
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

// The old base held the count exactly, so the rise came with the rebase (upstream's own change):
// the operator's step, not a pull request author's (fork-follow cannot commit the raise itself).
const AFTER_REBASE_RISE =
  " after the rebase onto this base (upstream's own change did it, not a fork pull request): rebase by hand as docs/fork/updates.md says and raise the {count} in the rebased series before pushing it, or push it and then raise the {count} with a list-only pull request to main; fork-follow lowers a count after a rebase, but never raises one (raising one is the owner's review).";

/**
 * Changed files outside the seams. An unlisted upstream file may not change or go (unless `removed` names it).
 * A "pending" one holds its ceiling from upstream (too high: `ceilings`, after a rebase `notes`; exceeded: `rises`;
 * `lowered.pending` gets the lowered count). On a PR it may not grow, and a change sets its `ownPending` ceiling to
 * exactly what is left, counting upstream lines put back from `ownLost` (none left: it moves to `ownFiles`).
 */
function findUnlistedChanges(
  config,
  {
    changed,
    atBase,
    atHead,
    atMain = atBase,
    upstream,
    isForkFile,
    removed = new Set(),
    afterRebase = false,
    ownPending,
    ownFiles = {},
    ownLost = {},
    lowered,
  },
) {
  const reasons = [];
  const ceilings = [];
  const rises = [];
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
    const generated = GENERATED.get(file);
    let unproven = "";
    if (generated !== undefined && changed.some((other) => other.startsWith(generated.source))) {
      const proof = atHead(generated.proof);
      if (proof === undefined) {
        reasons.push(
          `${file}: generated from ${generated.source}, but this change has no ${generated.proof}, the test that proves it is the generator's output.`,
        );
        continue;
      }
      // On a pull request the proof must be main's: a change to it (it.skip, an always-green test)
      // could pass its own hand edit. From upstream the head's own script and tests judge.
      if (upstream || proof === atMain(generated.proof)) continue;
      unproven = ` Judged line by line: this change's ${generated.proof} is not main's, so it proves nothing (change that test in its own pull request, or update the branch from main).`;
    }
    const added = total(addedLines(base, head));
    const entry = upstream
      ? "."
      : ' (if it is a fork file upstream does not have, add it to "forkFiles").';
    if (added > 0) {
      reasons.push(
        `${file}: upstream file with ${added} fork line${added === 1 ? "" : "s"} but no entry in ${CONFIG}${entry}${unproven}`,
      );
    } else if (!sameLines(base, head)) {
      reasons.push(
        `${file}: upstream file whose lines this change deletes, moves or re-indents, but no entry in ${CONFIG}${entry}${unproven}`,
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
        (afterRebase ? rises : reasons).push(
          `${file}: "pending" with ${added} fork lines, more than its ceiling of ${ceiling}${afterRebase ? AFTER_REBASE_RISE.replaceAll("{count}", "ceiling") : " (fork lines to move out, not add)."}`,
        );
      } else if (added < ceiling) {
        // A ceiling left too high is room to swap upstream lines for fork lines later, and a note
        // in the nightly log is never seen: refuse, naming the fix (only noted right after a
        // rebase, where fork-follow commits the lowered list into the series). Only a
        // lower count goes to `lowered`: a rise is refused.
        lowered?.pending.set(file, added);
        (afterRebase ? notes : ceilings).push(
          added === 0
            ? `${file}: "pending" with no fork lines left; remove it from the list${Object.hasOwn(config.lost ?? {}, file) ? ' (to "files" with "added": [] while "lost" names it)' : ""}.`
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
      continue;
    }
    if (ownPending === undefined) continue;
    // A change sets the ceiling to exactly what is left (too high admits a fork line later; too low
    // fails the run from upstream). Fork lines change by lines gained less the change in "gone" (an
    // upstream line put back is a fork line fewer); that run holds both "gone" counts exact. The own
    // list only refuses, never widens; a list-only change (text unchanged) is not held: it may fix main.
    const goneOf = (lost) =>
      lost !== undefined && Object.hasOwn(lost, file) ? lost[file].gone : 0;
    const mainGone = goneOf(config.lost);
    const ownGone = goneOf(ownLost);
    const moved = Object.hasOwn(ownFiles, file) && !Object.hasOwn(ownPending, file);
    const edited = base !== head;
    if (!edited && !moved) continue;
    const want = Math.max(edited ? ceiling + grows + ownGone - mainGone : ceiling, 0);
    const parts = [];
    if (grows < 0) {
      parts.push(
        `removes ${-grows} more line${grows === -1 ? "" : "s"} than it adds (keep upstream's lines; if they are fork lines moved out)`,
      );
    }
    if (ownGone !== mainGone) {
      parts.push(
        `its own "lost" names ${ownGone} upstream line${ownGone === 1 ? "" : "s"} gone where main's names ${mainGone} (an upstream line put back is a fork line fewer)`,
      );
    }
    const does = parts.length > 0 ? parts.join(", and ") : "changes its lines";
    if (moved) {
      // In "files" with "added": [] it may hold no fork line: only an empty file may move there.
      if (want > 0) {
        ceilings.push(
          `${file}: "pending" with a ceiling of ${ceiling}, and this change ${does}, but moves it to "files" in this change's ${CONFIG} while ${want} fork line${want === 1 ? " is" : "s are"} left: keep it in "pending" with a ceiling of ${want} (only a file with no fork line left moves to "files").`,
        );
      }
      continue;
    }
    const listed = Object.hasOwn(ownPending, file);
    const own = listed ? ownPending[file] : 0;
    if (own !== want) {
      const has = listed ? `its list has ${own}` : "its list drops it";
      ceilings.push(
        want > 0
          ? `${file}: "pending" with a ceiling of ${ceiling}, and this change ${does}: set it to ${want} in this change's ${CONFIG} (${has}; main holds its ceiling exactly).`
          : `${file}: "pending" with a ceiling of ${ceiling}, and this change ${does}: remove it from "pending" in this change's ${CONFIG} (${has}).`,
      );
    }
  }
  return { reasons, ceilings, rises, notes };
}

/**
 * Upstream lines a seam or pending file lost: `lines` not kept in order, `gone` whose text is gone.
 * From upstream both are held to its "lost" entry (0 when unlisted); a rise after a rebase goes to
 * `rises`. On a PR only "added" lines may go: an announced line in main's file is upstream's.
 * `lowered.lost` gets each entry a run from upstream finds too high: its counts ({0, 0}: remove it).
 */
function findLostLines(config, { atBase, atHead, upstream, afterRebase = false, lowered }) {
  const reasons = [];
  const ceilings = [];
  const rises = [];
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
    // Own keys only: a file named `constructor` would read Object's and never be refused.
    const held = Object.hasOwn(lost, file) ? lost[file] : { lines: 0, gone: 0 };
    const below = [];
    for (const key of ["lines", "gone"]) {
      const text = say(counts[key], what[key]);
      if (counts[key] > held[key]) {
        (afterRebase ? rises : reasons).push(
          `${file}: ${text}, more than its "lost" ${key} count of ${held[key]}${afterRebase ? AFTER_REBASE_RISE.replaceAll("{count}", "count") : " (keep upstream's lines; a fork change goes in a file of the fork's own)."}`,
        );
      } else if (counts[key] < held[key]) {
        below.push(
          `${file}: ${text}, below its "lost" ${key} count of ${held[key]}; lower it to ${counts[key]}.`,
        );
      }
    }
    if (below.length > 0) {
      lowered?.lost.set(file, counts);
      (afterRebase ? notes : ceilings).push(
        ...(lines === 0
          ? [
              `${file}: no upstream lines ${what.lines}, below its "lost" entry; remove it from "lost".`,
            ]
          : below),
      );
    }
  }
  return { reasons, ceilings, rises, notes };
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

/**
 * Announced lines already in their file, as { file, line, count }: step 2 moved them to "added"
 * (a line is never in both), or upstream has them. `read(file)`: the file's text, or undefined.
 */
function findLandedAnnouncements(files, read) {
  const landed = [];
  for (const [file, seam] of Object.entries(files)) {
    const text = seam.announced === undefined ? undefined : read(file);
    if (text === undefined) continue;
    const there = lineCounts(text);
    for (const line of lineCounts(seam.announced.added.join("\n")).keys()) {
      const count = there.get(line) ?? 0;
      if (count > 0) landed.push({ file, line, count });
    }
  }
  return landed;
}

/**
 * Why the list no longer matches the tree: a listed seam line its file lacks, or an announced line
 * already there (step 2 moves it to "added"). `read(file)`: the file's text, or undefined. Run by
 * the tests on the tree they test (a pull request's included); from upstream the run refuses the
 * second kind before fork-follow pushes it, as main's tree then fails these tests for every PR.
 */
function findStaleSeamLines(files, read) {
  const problems = [];
  for (const [file, seam] of Object.entries(files)) {
    const text = read(file);
    if (text === undefined) {
      problems.push(`${file}: listed in ${CONFIG}, but the tree does not have it.`);
      continue;
    }
    const there = lineCounts(text);
    for (const line of lineCounts(seam.added.join("\n")).keys()) {
      if (!there.has(line)) {
        problems.push(
          `${file}: the listed seam line \`${line}\` is not in the file; remove it from the list.`,
        );
      }
    }
  }
  for (const { file, line, count } of findLandedAnnouncements(files, read)) {
    problems.push(
      `${file}: the announced line \`${line}\` is already in the file (${count}× there): move it to "added" in the change that adds it, or drop it (a line upstream has cannot be a seam line a pull request adds).`,
    );
  }
  return problems;
}

const ANNOUNCED_DAYS = 14;
const WARN_DAYS = 3;
const DAY_MS = 24 * 60 * 60 * 1000;
/** `day` as a count of days. Throws on anything but a day: NaN compares as "not expired". */
function dayNumber(day) {
  if (!isDay(day)) {
    throw new Error(`not a day (YYYY-MM-DD): ${JSON.stringify(String(day).slice(0, 40))}`);
  }
  return Date.parse(`${day}T00:00:00Z`) / DAY_MS;
}
// The day an announcement made again carries: "today" would lose the days its review takes.
const SINCE_AGAIN = 'set "since" to the day that pull request merges';
const dayAfter = (day, days) =>
  new Date((dayNumber(day) + days) * DAY_MS).toISOString().slice(0, 10);
/** Whether an announcement of `since` is more than its days old on `today`. */
const isExpired = (since, today) => dayNumber(today) - dayNumber(since) > ANNOUNCED_DAYS;
/** Whether `since` is after `today`, beyond the one day left for time zones. */
const isAhead = (since, today) => dayNumber(since) - dayNumber(today) > 1;

/**
 * Announcements past their days, or dated after `today` (the head's commit date: a fresh rebase is
 * dated when it ran). From upstream only; a PR's run refuses just a line an expired announcement
 * alone allows (findSeamBreaches), so the change that drops one is green.
 */
function findExpiredAnnouncements(files, today) {
  const reasons = [];
  for (const [file, seam] of Object.entries(files)) {
    if (seam.announced === undefined) continue;
    const { since, added } = seam.announced;
    const lines = `${added.length} line${added.length === 1 ? "" : "s"} "announced" on ${since}`;
    if (isExpired(since, today)) {
      reasons.push(
        `${file}: ${lines}, more than ${ANNOUNCED_DAYS} days before ${today} (the head's commit date): announce ${added.length === 1 ? "it" : "them"} again (${SINCE_AGAIN}) or remove the announcement, with a list-only pull request to main.`,
      );
    } else if (isAhead(since, today)) {
      reasons.push(
        `${file}: ${lines}, after ${today} (the head's commit date): date it the day it is announced.`,
      );
    }
  }
  return reasons;
}

/**
 * Warnings (never failures) for the announcements a pull request's own list adds or changes that
 * fork-follow refuses within WARN_DAYS of `today` (the clock's day) or that are dated after it:
 * a step-1 change that waited for its reviews would merge as good as expired.
 */
function findAnnouncementWarnings(ownFiles, baseFiles, today) {
  const warnings = [];
  for (const [file, seam] of Object.entries(ownFiles)) {
    if (seam.announced === undefined) continue;
    const before = Object.hasOwn(baseFiles, file) ? baseFiles[file].announced : undefined;
    if (JSON.stringify(before) === JSON.stringify(seam.announced)) continue;
    const { since } = seam.announced;
    const fix = 'set "since" to the day this pull request merges.';
    const refused = dayAfter(since, ANNOUNCED_DAYS + 1);
    if (isAhead(since, today)) {
      warnings.push(
        `${file}: "announced" on ${since}, after today (${today}), so fork-follow refuses it: ${fix}`,
      );
    } else if (dayNumber(refused) - dayNumber(today) <= WARN_DAYS) {
      warnings.push(
        `${file}: "announced" on ${since}, so fork-follow refuses it from ${refused} (more than ${ANNOUNCED_DAYS} days): ${fix}`,
      );
    }
  }
  return warnings;
}

const KINDS = ["files", "pending", "forkFiles", "lost"];
const SEAM_KEYS = ["why", "added", "announced"];
const ANNOUNCED_KEYS = ["since", "added"];

/** A path git reads as itself: relative, no `.`, `..` or empty part, no pathspec magic or `\\`. */
const isRepoPath = (file) =>
  !file.startsWith(":") &&
  !/[\\\u0000-\u001f\u007f]/.test(file) &&
  file.split("/").every((part) => part.trim() !== "" && part !== "." && part !== "..");

/** Whether `day` is a real calendar day written YYYY-MM-DD. */
function isDay(day) {
  if (typeof day !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
  const date = new Date(`${day}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === day;
}

/** Refuses a key the check does not read (a misspelt block guards nothing); "//…" is a comment. */
function refuseUnknownKeys(object, known, where) {
  for (const key of Object.keys(object)) {
    if (!key.startsWith("//") && !known.includes(key)) {
      throw new Error(
        `${CONFIG}: ${where}unknown key ${JSON.stringify(key)} (known: ${known.join(", ")})`,
      );
    }
  }
}

const isStringList = (value) =>
  Array.isArray(value) && value.every((item) => typeof item === "string" && item.trim() !== "");

/** Refuses a listed line with a line break: split, it would allow a line nobody listed as one. */
function refuseLineBreaks(file, lines) {
  const broken = lines.find((line) => /[\n\r]/.test(line));
  if (broken !== undefined) {
    throw new Error(
      `${CONFIG}: ${file}: a listed line holds a line break (one line per item): ${JSON.stringify(broken)}`,
    );
  }
}

/** The config, checked; throws on a malformed config so the check never runs empty. */
function parseConfig(text) {
  const config = JSON.parse(text);
  const files = config?.files;
  if (typeof files !== "object" || files === null || Array.isArray(files)) {
    throw new Error(`${CONFIG}: "files" must be an object`);
  }
  refuseUnknownKeys(config, KINDS, "");
  for (const [file, seam] of Object.entries(files)) {
    if (!isRepoPath(file)) {
      throw new Error(`${CONFIG}: "files": not a plain repository path: ${JSON.stringify(file)}`);
    }
    if (typeof seam?.why !== "string" || seam.why.trim() === "") {
      throw new Error(`${CONFIG}: ${file} needs a "why"`);
    }
    if (!Array.isArray(seam.added) || seam.added.some((line) => typeof line !== "string")) {
      throw new Error(`${CONFIG}: ${file}: "added" must be a list of lines`);
    }
    refuseUnknownKeys(seam, SEAM_KEYS, `${file}: `);
    refuseLineBreaks(file, seam.added);
    if (seam.announced !== undefined) {
      const announced = seam.announced;
      const shape = `${CONFIG}: ${file}: "announced" needs {"since": "YYYY-MM-DD", "added": [lines]}: the day it is announced and at least one line`;
      if (typeof announced !== "object" || announced === null || Array.isArray(announced)) {
        throw new Error(shape);
      }
      refuseUnknownKeys(announced, ANNOUNCED_KEYS, `${file}: "announced": `);
      if (!isDay(announced.since) || !isStringList(announced.added) || !announced.added.length) {
        throw new Error(shape);
      }
      refuseLineBreaks(file, announced.added);
      // Counted twice, a copied line would allow a second copy that nobody reviewed.
      const listed = new Set(seam.added.map((line) => line.trim()));
      const both = announced.added.find((line) => listed.has(line.trim()));
      if (both !== undefined) {
        throw new Error(
          `${CONFIG}: ${file}: ${JSON.stringify(both.trim())} is in "added" and "announced": step 2 moves a line from "announced" to "added", it does not copy it.`,
        );
      }
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
    if (!isRepoPath(file)) {
      throw new Error(`${CONFIG}: "pending": not a plain repository path: ${JSON.stringify(file)}`);
    }
    if (!Number.isSafeInteger(ceiling) || ceiling < 1) {
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
      throw new Error(`${CONFIG}: "lost": ${JSON.stringify(file)} is not in "files" or "pending"`);
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
 * The first key an object of `text` (valid JSON) has twice, decoded, or undefined. JSON.parse keeps
 * the last one, so the other is shown in the list but never applies.
 */
function findDuplicateKey(text) {
  const objects = []; // A Set of keys per open object, null per open array.
  let atKey = false;
  for (let at = 0; at < text.length; at++) {
    const char = text[at];
    if (char === '"') {
      let end = at + 1;
      while (text[end] !== '"') end += text[end] === "\\" ? 2 : 1;
      const keys = objects.at(-1);
      if (keys && atKey) {
        const key = JSON.parse(text.slice(at, end + 1));
        if (keys.has(key)) return key;
        keys.add(key);
        atKey = false;
      }
      at = end;
    } else if (char === "{" || char === "[") {
      objects.push(char === "{" ? new Set() : null);
      atKey = char === "{";
    } else if (char === "}" || char === "]") {
      objects.pop();
      atKey = false;
    } else if (char === ",") {
      atKey = objects.at(-1) instanceof Set;
    }
  }
  return undefined;
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
    const duplicate = findDuplicateKey(text);
    if (duplicate === undefined) return undefined;
    return printable(`${own} is malformed: duplicate key ${JSON.stringify(duplicate)}`);
  } catch (error) {
    return printable(`${own} is malformed: ${error instanceof Error ? error.message : error}`);
  }
}

// The "why" of a pending file fork-follow moves to "files" (see lowerList).
const MOVED_WHY =
  'Was "pending": fork-follow moved it here once it had no fork line left, as its "lost" entry still counts upstream lines it loses. No seam line: a fork line goes in a file of the fork\'s own.';

/**
 * `text` with the counts in `lowered` ({ pending, lost }: Maps of file to count) lowered, as { text, changes },
 * or undefined when nothing changes. Only lowers; a count at 0 removes the entry. A "pending" file with no fork
 * line left that "lost" still names moves to "files" with no seam line (no fork line may come back, no line may
 * be lost). Two-space JSON, parsed again before it is returned, so a lowered list is never one the check refuses.
 */
function lowerList(text, lowered) {
  const raw = JSON.parse(text);
  const changes = [];
  const lost = raw.lost ?? {};
  const counts = ({ lines, gone }) => `${lines}/${gone}`;
  const lostAfter = Object.entries(lost).flatMap(([file, entry]) => {
    const found = lowered.lost.get(file);
    if (found === undefined) return [[file, entry]];
    // Each count only lowers: one that rose is refused, and nothing is written then.
    const to = {
      lines: Math.min(found.lines, entry.lines),
      gone: Math.min(found.gone, entry.gone),
    };
    if (to.lines === entry.lines && to.gone === entry.gone) return [[file, entry]];
    if (to.lines === 0) {
      changes.push(`${file}: "lost" ${counts(entry)} removed (no upstream line lost)`);
      return [];
    }
    changes.push(`${file}: "lost" ${counts(entry)} -> ${counts(to)}`);
    return [[file, { lines: to.lines, gone: to.gone }]];
  });
  if (raw.lost !== undefined) raw.lost = Object.fromEntries(lostAfter);
  const stillLost = new Set(lostAfter.map(([file]) => file));
  const moved = [];
  if (raw.pending !== undefined) {
    raw.pending = Object.fromEntries(
      Object.entries(raw.pending).flatMap(([file, ceiling]) => {
        const to = lowered.pending.get(file);
        if (to === undefined || to >= ceiling) return [[file, ceiling]];
        if (to === 0 && stillLost.has(file)) {
          changes.push(
            `${file}: "pending" ${ceiling} moved to "files" with no seam line (no fork line left; "lost" still counts its upstream lines)`,
          );
          moved.push(file);
          return [];
        }
        if (to === 0) {
          changes.push(`${file}: "pending" ${ceiling} removed (no fork line left)`);
          return [];
        }
        changes.push(`${file}: "pending" ${ceiling} -> ${to}`);
        return [[file, to]];
      }),
    );
  }
  // Appended through defineProperty: a file named `__proto__` stays an own key.
  for (const file of moved) {
    Object.defineProperty(raw.files, file, {
      value: { why: MOVED_WHY, added: [] },
      enumerable: true,
      writable: true,
      configurable: true,
    });
  }
  if (changes.length === 0) return undefined;
  const lowerText = `${JSON.stringify(raw, null, 2)}\n`;
  parseConfig(lowerText);
  return { text: lowerText, changes };
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
  const option = (name) => {
    const at = args.indexOf(name);
    if (at === -1) return { value: undefined };
    const value = args[at + 1];
    if (!value || value.startsWith("--")) return undefined;
    args.splice(at, 2);
    return { value };
  };
  const configRef = option("--config-ref");
  const loweredList = option("--lowered-list");
  if (!configRef || !loweredList) return undefined;
  if (args.length !== 2 || args.some((arg) => arg.startsWith("--"))) return undefined;
  // A pull request's run never runs from upstream; only a run from upstream has a rebase behind it.
  if (fromUpstream ? configRef.value !== undefined : afterRebase) return undefined;
  // Only a run after a rebase lowers the list: fork-follow commits it into the rebased series.
  if (loweredList.value !== undefined && !afterRebase) return undefined;
  return {
    configRef: configRef.value,
    loweredList: loweredList.value,
    fromUpstream,
    afterRebase,
    base: args[0],
    head: args[1],
  };
}

function main(argv) {
  const parsed = parseArgs(argv);
  if (!parsed) {
    console.error(
      "Usage: fork-seam-check.cjs [--config-ref <rev>] <base> <head>\n" +
        "       fork-seam-check.cjs --from-upstream [--after-rebase [--lowered-list <path>]] <upstream> <head>",
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
  const ownList = textAt(head, CONFIG);
  const editsList = !upstream && ownList !== textAt(mergeBase, CONFIG);
  const listProblem = editsList ? ownListProblem(ownList) : undefined;
  // A pull request that moves fork lines out of a "pending" file lowers its ceiling in its own
  // list (read only for that: a lower ceiling never widens the gate).
  let ownPending;
  let ownFiles;
  let ownLost;
  if (!upstream && listProblem === undefined && ownList !== undefined) {
    try {
      ({ pending: ownPending, files: ownFiles, lost: ownLost } = parseConfig(ownList));
    } catch {
      ownPending = undefined; // The list judged it is main's; a malformed own one is said below.
    }
  }
  const lowered =
    parsed.loweredList === undefined ? undefined : { pending: new Map(), lost: new Map() };
  const unlisted = findUnlistedChanges(config, {
    changed,
    atBase,
    atHead,
    // The list's commit: main on a pull request (fork-checks), the head from upstream.
    atMain: (file) => textAt(configRef, file),
    upstream,
    isForkFile,
    removed,
    afterRebase: parsed.afterRebase,
    ownPending,
    ownFiles,
    ownLost,
    lowered,
  });
  const lost = findLostLines(config, {
    atBase,
    atHead,
    upstream,
    afterRebase: parsed.afterRebase,
    lowered,
  });
  // A ceiling above the count is fixed by lowering it, not by moving lines: its own hint. A count
  // that rose with a rebase names the operator's step in its own text.
  const ceilings = [...unlisted.ceilings, ...lost.ceilings];
  const rises = [...unlisted.rises, ...lost.rises];
  // A pull request's run dates an announcement by the commit whose list judges it (main's): its
  // own author sets its own date, and fork-checks' base can be older than the list's commit.
  const today = upstream ? undefined : commitDay(configRef);
  const late = [];
  const reasons = [
    // A pull request's base is its base branch's tip: a listed copy another change has merged
    // there since this branch's merge-base is spent.
    ...findSeamBreaches(config.files, {
      atBase,
      atHead,
      upstream,
      today,
      late,
      atTip: upstream ? undefined : (file) => textAt(base, file),
    }),
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
  const expired = upstream ? findExpiredAnnouncements(config.files, commitDay(head)) : [];
  // Upstream added an announced line: pushed, main's tree fails the stale-list test that
  // fork-checks runs for every pull request (the fix's too). Stop before the push.
  const landed = upstream
    ? findLandedAnnouncements(config.files, atHead).map(
        ({ file, line, count }) =>
          `${file}: the announced line \`${line}\` is already in the head's file (${count}× there): upstream added it, so no fork pull request can (a line upstream has is never a seam line).`,
      )
    : [];
  const from = parsed.configRef ? `--config-ref ${configRef}` : upstream ? "the head" : "the base";
  const notes = [...findListedGone(config, { atBase, atHead }), ...unlisted.notes, ...lost.notes];
  for (const note of notes) console.log(printable(`Note: ${note}`));
  if (editsList) {
    console.log(
      `Note: this change edits ${CONFIG}; it is judged by the list in ${from}, not its own. ` +
        "A new seam or list entry takes effect once the owner merges it.",
    );
    if (listProblem === undefined) {
      const own = parseConfig(ownList).files;
      const now = new Date().toISOString().slice(0, 10);
      for (const warning of findAnnouncementWarnings(own, config.files, now)) {
        console.log(printable(`Warning: ${warning}`));
      }
    }
  }
  const counted = upstream ? "every upstream file" : "every upstream file it changes";
  if (listProblem !== undefined) {
    console.error(listProblem);
    console.error(
      `Fix the list in this change: merged, it is main's list, and a malformed one stops every later pull request and fork-follow run.`,
    );
  }
  const refused = [...reasons, ...late, ...rises, ...ceilings, ...expired, ...landed];
  if (refused.length === 0) {
    if (listProblem !== undefined) return 1;
    if (lowered !== undefined) {
      // After a rebase only: fork-follow commits the lowered list into the rebased series.
      const lower = lowerList(textAt(configRef, CONFIG), lowered);
      if (lower !== undefined) {
        fs.writeFileSync(parsed.loweredList, lower.text);
        for (const change of lower.changes) console.log(printable(`Lowered: ${change}`));
      }
    }
    console.log(
      `Fork lines in upstream files: only the pinned seams (${counted}; list from ${from}).`,
    );
    return 0;
  }
  console.error(
    `Fork changes to upstream files must stay within the seam lines in ${CONFIG} (list from ${from}):`,
  );
  for (const reason of refused) console.error(printable(`- ${reason}`));
  if (reasons.length > 0) {
    console.error(
      "Put the change in a file of the fork's own (a fork setting goes in " +
        "packages/contracts/src/forkSettings.ts; see docs/fork/updates.md). A new seam line needs " +
        `its own entry in ${CONFIG}, which the owner approves.`,
    );
  }
  if (ceilings.length > 0) {
    console.error(
      `Lower each "pending" ceiling and "lost" count named above (or remove the entry, or set the value its line names) in ${CONFIG}: in this change's list where the line says so, otherwise with a pull request to main.`,
    );
  }
  if (late.length > 0) {
    console.error(
      `Announce each line named above again (${SINCE_AGAIN}) in ${CONFIG} with a list-only pull request to main, then update this change from main (docs/fork/updates.md, "Adding a seam line").`,
    );
  }
  if (expired.length > 0) {
    console.error(
      `Announce each line named above again (${SINCE_AGAIN}) or remove its announcement, with a list-only pull request to main; only then step 2. Step 2 alone is refused once main's commit date is past the announcement's days too (a pull request's run dates it by that; docs/fork/updates.md, "Adding a seam line").`,
    );
  }
  if (landed.length > 0) {
    console.error(
      `Remove each announcement named above from ${CONFIG} with a list-only pull request to main, then run fork-follow again. The series was not pushed: pushed, main's seam-check tests ("no announced line is in its file yet") would fail every pull request.`,
    );
  }
  return 1;
}

module.exports = {
  CONFIG,
  GENERATED,
  compileForkPattern,
  findAnnouncementWarnings,
  findDuplicateKey,
  findExpiredAnnouncements,
  findHiddenUpstreamFiles,
  findListedGone,
  findLostLines,
  findSeamBreaches,
  findStaleSeamLines,
  findUnlistedChanges,
  forkFileTest,
  lineCounts,
  lostLines,
  lowerList,
  ownListProblem,
  parseConfig,
  parseRemoved,
  sameLines,
};

if (require.main === module) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    console.error(
      printable(`fork-seam-check crashed: ${error instanceof Error ? error.message : error}`),
    );
    process.exitCode = 3;
  }
}
