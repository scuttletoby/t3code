// Fork only: runs every `.github/scripts/fork-*.test.{sh,py,cjs}` and checks
// them against `.github/fork-ci-tests.txt`. A present test missing from the list
// fails; a listed test that is missing fails once it has been in the tested
// commit's history (deleted while still listed) and is a note before that.
const childProcess = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const LIST = ".github/fork-ci-tests.txt";
const TEST_FILE = /^fork-.+\.test\.(sh|py|cjs)$/;
const RUNNER = { sh: ["bash"], py: ["python3"], cjs: ["node", "--test"] };

// `ever(file)` says whether the file is in the tested commit's history.
function plan(listed, present, ever) {
  const unlisted = present.filter((file) => !listed.includes(file));
  const missing = listed.filter((file) => !present.includes(file));
  const deleted = missing.filter((file) => ever(file));
  const pending = missing.filter((file) => !deleted.includes(file));
  return { run: present, unlisted, deleted, pending };
}

function readList(text) {
  return text
    .split("\n")
    .map((line) => line.replace(/#.*/, "").trim())
    .filter(Boolean);
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

function main() {
  const listed = readList(fs.readFileSync(path.join(ROOT, LIST), "utf8"));
  const present = fs
    .readdirSync(path.join(ROOT, ".github/scripts"))
    .filter((name) => TEST_FILE.test(name))
    .map((name) => `.github/scripts/${name}`)
    .toSorted();
  const result = plan(listed, present, everInHistory);
  const rows = [];
  let failed = result.unlisted.length + result.deleted.length;
  for (const file of result.run) {
    const [command, ...args] = RUNNER[path.basename(file).match(TEST_FILE)[1]];
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
  for (const file of result.pending)
    rows.push(`| \`${file}\` | not present: its PR has not merged | |`);
  for (const file of result.unlisted) rows.push(`| \`${file}\` | **not in ${LIST}** | |`);
  for (const file of result.deleted) rows.push(`| \`${file}\` | **deleted while listed** | |`);

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

module.exports = { plan, readList };

if (require.main === module) main();
