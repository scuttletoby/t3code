// Fork only: every `*.test.*` under .github/scripts and fork-ci must be listed in .github/fork-ci-tests.txt.
// A folder missing from the checkout fails (readdir throws), so a narrower sparse checkout cannot hide tests.
// A plain entry runs here by its extension (RUNNER); `<test> run by <file>` is run by that file,
// which must name the test and cannot be a (disabled) upstream workflow. Unlisted or runner-less
// tests fail; a listed test that is gone fails once the tested history had it, a note before.
const childProcess = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const LIST = ".github/fork-ci-tests.txt";
const TEST_DIRS = [".github/scripts", "fork-ci"];
const TEST_FILE = /\.test\.([^./]+)$/;
const RUNNER = {
  sh: ["bash"],
  py: ["python3"],
  cjs: ["node", "--test"],
  mjs: ["node", "--test"],
  js: ["node", "--test"],
};
const UPSTREAM_WORKFLOW = /^\.github\/workflows\/(?!fork-)[^/]+\.ya?ml$/;

// `entries`: [{ file, runBy }]; `present`: test files in the tree;
// `ever(file)` says whether the file is in the tested commit's history;
// `read(file)` returns a runner file's text, or null when it does not exist.
function plan(entries, present, ever, read = () => null) {
  const listed = entries.map((entry) => entry.file);
  const unlisted = present.filter((file) => !listed.includes(file));
  const missing = listed.filter((file) => !present.includes(file));
  const deleted = missing.filter((file) => ever(file));
  const pending = missing.filter((file) => !deleted.includes(file));
  const here = entries.filter((entry) => !entry.runBy && present.includes(entry.file));
  const noRunner = here
    .filter((entry) => !RUNNER[entry.file.match(TEST_FILE)?.[1]])
    .map((entry) => entry.file);
  const elsewhere = [];
  const notRun = [];
  for (const { file, runBy } of entries) {
    if (!runBy || !present.includes(file)) continue;
    let why = null;
    if (UPSTREAM_WORKFLOW.test(runBy))
      why = `${runBy} is an upstream workflow, disabled in the fork`;
    else {
      const text = read(runBy);
      if (text === null) why = `${runBy} does not exist`;
      else if (!text.includes(file)) why = `${runBy} does not name ${file}`;
    }
    if (why) notRun.push({ file, why });
    else elsewhere.push({ file, runBy });
  }
  return {
    run: here.map((entry) => entry.file).filter((file) => !noRunner.includes(file)),
    unlisted,
    deleted,
    pending,
    noRunner,
    elsewhere,
    notRun,
  };
}

function readList(text) {
  return text
    .split("\n")
    .map((line) => line.replace(/#.*/, "").trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(/^(\S+)(?:\s+run by\s+(\S+))?$/);
      if (!match)
        throw new Error(
          `${LIST}: cannot read "${line}" (expected "<test>" or "<test> run by <file>")`,
        );
      return { file: match[1], runBy: match[2] ?? null };
    });
}

function findTests(dir, root = ROOT) {
  const out = [];
  for (const entry of fs.readdirSync(path.join(root, dir), { withFileTypes: true })) {
    const rel = `${dir}/${entry.name}`;
    if (entry.isDirectory()) out.push(...findTests(rel, root));
    else if (TEST_FILE.test(entry.name)) out.push(rel);
  }
  return out.toSorted();
}

function git(args) {
  return childProcess.execFileSync("git", args, { cwd: ROOT, encoding: "utf8" }).trim();
}

// A shallow checkout first fetches the full history of HEAD, trees only.
function everInHistory(file) {
  if (git(["rev-parse", "--is-shallow-repository"]) === "true")
    git([
      "fetch",
      "--quiet",
      "--no-tags",
      "--filter=blob:none",
      "--unshallow",
      "origin",
      git(["rev-parse", "HEAD"]),
    ]);
  return git(["log", "-1", "--format=%h", "HEAD", "--", file]) !== "";
}

function readFile(file) {
  try {
    return fs.readFileSync(path.join(ROOT, file), "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

function main() {
  const entries = readList(fs.readFileSync(path.join(ROOT, LIST), "utf8"));
  const present = TEST_DIRS.flatMap((dir) => findTests(dir));
  const result = plan(entries, present, everInHistory, readFile);
  const rows = [];
  let failed =
    result.unlisted.length + result.deleted.length + result.noRunner.length + result.notRun.length;
  for (const file of result.run) {
    const [command, ...args] = RUNNER[file.match(TEST_FILE)[1]];
    const started = Date.now();
    console.log(`::group::${file}`);
    const run = childProcess.spawnSync(command, [...args, file], { cwd: ROOT, stdio: "inherit" });
    console.log("::endgroup::");
    const seconds = ((Date.now() - started) / 1000).toFixed(1);
    if (run.status !== 0) {
      failed += 1;
      console.log(`::error file=${file}::${file} failed (exit ${run.status ?? run.signal})`);
    }
    rows.push(`| \`${file}\` | ${run.status === 0 ? "passed" : "**failed**"} | ${seconds} s |`);
  }
  for (const file of result.unlisted)
    console.log(`::error file=${file}::${file} is not in ${LIST}; add it there`);
  for (const file of result.deleted)
    console.log(`::error::${file} is listed in ${LIST} but was deleted; restore it or unlist it`);
  for (const file of result.noRunner)
    console.log(
      `::error file=${file}::${file}: no runner for this extension (${Object.keys(RUNNER).join(", ")}); ` +
        `rename it or list it as "run by <file>"`,
    );
  for (const { file, why } of result.notRun)
    console.log(`::error file=${file}::${file} is not run: ${why}`);
  for (const { file, runBy } of result.elsewhere)
    rows.push(`| \`${file}\` | run by \`${runBy}\` | |`);
  for (const file of result.pending)
    rows.push(`| \`${file}\` | not present: its PR has not merged | |`);
  for (const file of result.unlisted) rows.push(`| \`${file}\` | **not in ${LIST}** | |`);
  for (const file of result.deleted) rows.push(`| \`${file}\` | **deleted while listed** | |`);
  for (const file of result.noRunner)
    rows.push(`| \`${file}\` | **no runner for its extension** | |`);
  for (const { file, why } of result.notRun) rows.push(`| \`${file}\` | **not run: ${why}** | |`);

  const summary = process.env.GITHUB_STEP_SUMMARY;
  if (summary)
    fs.appendFileSync(
      summary,
      [
        "### Fork script tests",
        "",
        "| Test | Result | Time |",
        "| --- | --- | --- |",
        ...rows,
        "",
      ].join("\n") + "\n",
    );
  if (failed) process.exitCode = 1;
}

module.exports = { plan, readList, findTests };

if (require.main === module) main();
