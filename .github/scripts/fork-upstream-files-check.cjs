// Refuses fork changes to listed upstream files: the fork's add-ons live in files and layers of their
// own, so these files stay exactly upstream's and every upstream sync applies to them cleanly.
// Usage: fork-upstream-files-check.cjs <base> <head> <files>. <files> is the list, separated by
// whitespace (fork-checks.yml passes its one config line). Judged like fork-schema-check.cjs: the
// diff from the merge-base of <base> and <head> to <head>, so upstream's own changes, which reach
// <base> through fork-follow's rebase, are never the branch's. A listed file is refused when the
// branch changes it in any way (edit, delete, rename away, mode) and its copy at <head> is not the
// one <base> has. Files are read as git tree entries; nothing is checked out or run.

const { execFileSync } = require("node:child_process");
const { parseRawDiff } = require("./fork-schema-check.cjs");

function git(args) {
  return execFileSync("git", ["-c", "core.quotePath=false", ...args], {
    encoding: "utf8",
    maxBuffer: 512 * 1024 * 1024,
  });
}

/** The tree entry of `file` in `rev` ("<mode> <type> <sha>"), or undefined when absent. */
function entryAt(rev, file) {
  const record = git(["ls-tree", "-z", "--full-tree", rev, "--", file]).split("\0")[0];
  const match = record.match(/^(\d+ \w+ [0-9a-f]+)\t(.+)$/s);
  return match && match[2] === file ? match[1] : undefined;
}

/** The list from the config line(s): paths separated by whitespace, each once. */
const parseList = (args) => [...new Set(args.join(" ").split(/\s+/).filter(Boolean))];

/**
 * One reason per refused file. `listed`: the guarded paths. `changed`: paths the branch changes
 * (merge-base to head). `atBase`/`atHead`: a path's tree entry there (undefined when absent).
 */
function findUpstreamFileChanges(listed, changed, { atBase, atHead }) {
  const reasons = [];
  const touched = new Set(changed);
  for (const file of listed) {
    const base = atBase(file);
    // A listed file main does not have guards nothing: the list is out of date (upstream moved or
    // removed it). Fail closed, so the guard is never silently empty.
    if (base === undefined) {
      reasons.push(
        `${file}: listed, but main does not have it (upstream moved or removed it?). Update the ` +
          "list in .github/workflows/fork-checks.yml; only the owner may do that.",
      );
      continue;
    }
    if (!touched.has(file)) continue;
    const head = atHead(file);
    // The branch carries main's copy (e.g. it sits on the same upstream as main, reached another
    // way): it changes nothing main does not already have.
    if (head === base) continue;
    const what =
      head === undefined
        ? "deletes or renames it"
        : head.split(" ")[2] === base.split(" ")[2]
          ? "changes its file mode"
          : "changes it";
    reasons.push(`${file}: this branch ${what}`);
  }
  return reasons;
}

function main(argv) {
  const [base, head, ...rest] = argv;
  const listed = parseList(rest);
  if (!base || !head || listed.length === 0) {
    console.error(
      "Usage: fork-upstream-files-check.cjs <base> <head> <files (whitespace-separated)>",
    );
    return 2;
  }
  const mergeBase = git(["merge-base", base, head]).trim();
  const changed = parseRawDiff(
    git(["diff", "--raw", "-z", "--no-abbrev", "--no-renames", "--no-ext-diff", mergeBase, head]),
  ).map((entry) => entry.path);
  const reasons = findUpstreamFileChanges(listed, changed, {
    atBase: (file) => entryAt(base, file),
    atHead: (file) => entryAt(head, file),
  });
  if (reasons.length === 0) {
    console.log(
      `No upstream file changes (${listed.length} listed files, ${changed.length} changed files checked).`,
    );
    return 0;
  }
  console.error("Fork changes must leave these upstream files exactly as upstream has them:");
  for (const reason of reasons) console.error(`- ${reason}`);
  console.error(
    "Put the change in a file of the fork's own (its own layer, toolkit or budget file). If this " +
      "branch sits on an older main, rebase it onto main first. Only the owner may change these " +
      "files: the check stays red, and the owner merges past it.",
  );
  return 1;
}

module.exports = { findUpstreamFileChanges, parseList };

if (require.main === module) {
  process.exitCode = main(process.argv.slice(2));
}
