// Fork only: all changed paths fork-owned (.github/fork-owned-paths.txt) prints `fork_only=true` and every gate
// false; some prints `fork_only=false` and upstream's gates for the other paths alone (no app job reads a fork-owned
// one); none, or any error, prints just `fork_only=false`. Never exits non-zero. Run by fork-tests.yml's `changes` job
// from the merge commit: node .github/scripts/fork-owned-paths.cjs >> "$GITHUB_OUTPUT" (fork-owned-paths.test.py).
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const { decide, readWorkspace } = require("./ci-changes.cjs");

const LIST = ".github/fork-owned-paths.txt";

// Never fork-owned, whatever the list says: the switch itself, what runs or wires the app jobs, and
// files whose change reaches every package. Checked before the list.
const NEVER = [
  /^\.github\/fork-owned-paths\.txt$/,
  /^\.github\/scripts\/fork-owned-paths\.cjs$/,
  /^\.github\/scripts\/ci-changes\.cjs$/,
  /^\.github\/workflows\//,
  /^\.github\/actions\//,
  /(^|\/)(package\.json|pnpm-lock\.yaml|pnpm-workspace\.yaml|\.npmrc|\.nvmrc|\.node-version)$/,
  /(^|\/)(tsconfig[^/]*\.json|vite\.config\.[^/]+|vitest\.config\.[^/]+|knip\.jsonc?)$/,
];

const escape = (text) => text.replace(/[.+?^${}()|[\]\\]/g, "\\$&");

/** One list line as a matcher: `dir/` (everything under it) or a path whose `*` matches any characters but `/`. */
function compile(pattern) {
  if (pattern.endsWith("/")) return (file) => file.startsWith(pattern);
  const re = new RegExp(`^${pattern.split("*").map(escape).join("[^/]*")}$`);
  return (file) => re.test(file);
}

/** The list's rules, in order: [{ owned, match, line }]. Blank lines and `#` comments are skipped. */
function parseList(text) {
  const rules = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const owned = !line.startsWith("!");
    const pattern = owned ? line : line.slice(1);
    // An absolute, upward or empty pattern cannot name a repository path; refuse the whole list.
    const outside = pattern.startsWith("/") || pattern.split("/").includes("..");
    if (!pattern || outside || pattern === "*") {
      throw new Error(`${LIST}: not a repository path pattern: ${line}`);
    }
    rules.push({ owned, match: compile(pattern), line });
  }
  return rules;
}

/** Is `file` fork-owned? NEVER first, then the first `!` line that matches wins, else any owned line. */
function owned(file, rules) {
  if (NEVER.some((re) => re.test(file))) return false;
  if (rules.some((rule) => !rule.owned && rule.match(file))) return false;
  return rules.some((rule) => rule.owned && rule.match(file));
}

/** The decision for a list of changed paths: { forkOnly, notOwned: paths that keep every job }. */
function judge(files, rules) {
  const notOwned = files.filter((file) => !owned(file, rules));
  return { forkOnly: files.length > 0 && notOwned.length === 0, notOwned };
}

/** Upstream's gates for a change with no path left (all false), as `key=value` lines. */
function forkOnlyOutputs() {
  const { outputs } = decide([], [], false);
  return ["fork_only=true", ...Object.entries(outputs).map(([key, value]) => `${key}=${value}`)];
}

/** Upstream's gates for `paths` alone (the paths that are not fork-owned), as `key=value` lines. */
function narrowedOutputs(paths, packages = readWorkspace()) {
  if (paths.length === 0 || packages.length === 0) throw new Error("nothing to narrow to");
  const { outputs } = decide(paths, packages, false);
  return ["fork_only=false", ...Object.entries(outputs).map(([key, value]) => `${key}=${value}`)];
}

function git(args) {
  return execFileSync("git", ["-c", "core.quotePath=false", ...args], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    maxBuffer: 64 * 1024 * 1024,
  });
}

function main(env = process.env, log = console.error) {
  const no = (why) => {
    log(`Not fork-only: ${why} Every job the detector chose runs.`);
    return ["fork_only=false"];
  };
  try {
    const event = env.GITHUB_EVENT_NAME || "unknown";
    if (event !== "pull_request") return no(`a ${event} run.`);
    // GitHub's merge commit: its first parent is the base, so HEAD^1..HEAD is the pull request's change.
    git(["rev-parse", "--verify", "--quiet", "HEAD^2^{commit}"]);
    const files = git(["diff", "--name-only", "--no-renames", "-z", "HEAD^1", "HEAD"])
      .split("\0")
      .filter(Boolean);
    const rules = parseList(fs.readFileSync(LIST, "utf8"));
    const { forkOnly, notOwned } = judge(files, rules);
    if (files.length === 0) return no("the pull request changes no path.");
    if (!forkOnly) {
      const shown = notOwned.slice(0, 5).join(", ") + (notOwned.length > 5 ? ", ..." : "");
      if (notOwned.length === files.length) {
        return no(`${notOwned.length} of ${files.length} paths are not fork-owned: ${shown}.`);
      }
      // Some paths are fork-owned: upstream's detector judges the rest alone. Anything that fails here
      // (no workspace, a broken manifest) falls to the catch: the detector's answer for every path stands.
      const lines = narrowedOutputs(notOwned);
      log(
        `Partly fork-owned: ${files.length - notOwned.length} of ${files.length} changed paths are fork-owned ` +
          `(${LIST}) and left out; the gates are upstream's detector's for the other ${notOwned.length}: ${shown}.`,
      );
      return lines;
    }
    log(`Fork-only: all ${files.length} changed paths are fork-owned (${LIST}).`);
    return forkOnlyOutputs();
  } catch (error) {
    return no(`${String(error.message || error).split("\n")[0]}`);
  }
}

module.exports = { NEVER, parseList, owned, judge, forkOnlyOutputs, narrowedOutputs, main };

if (require.main === module) {
  for (const line of main()) console.log(line);
}
