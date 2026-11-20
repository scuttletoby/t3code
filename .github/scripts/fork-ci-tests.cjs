// Fork only: every `*.test.*` under .github/scripts, fork-ci and fork-kit is listed in .github/fork-ci-tests.txt and
// runs here by extension (RUNNER), FORK_CI_TEST_JOBS at once (default: CPU count), or `run by <file>` (not an upstream
// workflow; it names the test). Unlisted or runner-less tests fail; a listed one that is gone fails once history had it.
// Arguments pick a part by path prefix (`fork-kit/`, `!fork-kit/`; none: all), FORK_CI_TEST_SHARD one shard of it.
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const LIST = ".github/fork-ci-tests.txt";
const TEST_DIRS = [".github/scripts", "fork-ci", "fork-kit"];
const TEST_FILE = /\.test\.([^./]+)$/;
const RUNNER = {
  sh: ["bash"],
  py: ["python3"],
  cjs: ["node", "--test"],
  mjs: ["node", "--test"],
  js: ["node", "--test"],
};
const UPSTREAM_WORKFLOW = /^\.github\/workflows\/(?!fork-)[^/]+\.ya?ml$/;

// `entries`: [{ file, runBy, weight }]; `present`: test files in the tree;
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
      const match = line.match(/^(\S+)(?:\s+~([1-9]\d{0,4})s|\s+run by\s+(\S+))?$/);
      if (!match)
        throw new Error(
          `${LIST}: cannot read "${line}" (expected "<test>", "<test> ~<seconds>s" or "<test> run by <file>")`,
        );
      return {
        file: match[1],
        runBy: match[3] ?? null,
        weight: match[2] ? Number(match[2]) : null,
      };
    });
}

// A test without a weight counts this much when shards are split: one over 30 s fails until it has one.
const UNWEIGHTED = 10;

// The files to run in start order: those without a weight first (a new test may be long), in list order, then the
// heaviest first (equal weights in list order). With every slot busy from the start, the longest test sets the time.
function order(files, entries) {
  const weight = new Map(entries.map((entry) => [entry.file, entry.weight]));
  const position = new Map(entries.map((entry, index) => [entry.file, index]));
  const rank = (file) => weight.get(file) ?? Infinity;
  return files.toSorted((a, b) => rank(b) - rank(a) || position.get(a) - position.get(b));
}

// Shard `index` (1-based) of `count`: the files heaviest first (as order() starts them), each onto the shard with the
// least weight so far (the lowest number on a tie). Only the list and the files decide, so every shard of a run makes
// the same split: each file is in exactly one shard.
function shard(files, entries, index, count) {
  const weight = new Map(entries.map((entry) => [entry.file, entry.weight ?? UNWEIGHTED]));
  const load = new Array(count).fill(0);
  const mine = [];
  for (const file of order(
    files,
    entries.map((entry) => ({ ...entry, weight: weight.get(entry.file) })),
  )) {
    const lightest = load.indexOf(Math.min(...load));
    load[lightest] += weight.get(file) ?? UNWEIGHTED;
    if (lightest === index - 1) mine.push(file);
  }
  return mine;
}

// FORK_CI_TEST_SHARD as { index, count }: unset or empty is the whole part (1/1); anything else but `<i>/<n>` with
// 1 <= i <= n throws, so a broken matrix cannot quietly run one shard's tests or none.
function readShard(env = process.env) {
  const value = env.FORK_CI_TEST_SHARD ?? "";
  if (value === "") return { index: 1, count: 1 };
  const match = value.match(/^([1-9]\d*)\/([1-9]\d*)$/);
  if (!match || Number(match[1]) > Number(match[2]))
    throw new Error(`FORK_CI_TEST_SHARD="${value}": expected <index>/<count>, 1 <= index <= count`);
  return { index: Number(match[1]), count: Number(match[2]) };
}

// Whether `file` is in the part the arguments name: under one of the `<prefix>` ones (any, when there are none),
// and under none of the `!<prefix>` ones.
function inPart(file, parts) {
  const want = parts.filter((part) => !part.startsWith("!"));
  const skip = parts.filter((part) => part.startsWith("!")).map((part) => part.slice(1));
  return (
    (want.length === 0 || want.some((prefix) => file.startsWith(prefix))) &&
    !skip.some((prefix) => file.startsWith(prefix))
  );
}

// How fast this runner ran the tests against their weights: the median of time / weight over the passed tests with a
// weight of at least 10 s (1 with fewer than three). A slow runner takes about the same factor longer on each.
function runSpeed(runs, weights) {
  const ratios = runs
    .filter((run) => run.status === 0 && (weights.get(run.file) ?? 0) >= 10)
    .map((run) => Number(run.seconds) / weights.get(run.file))
    .toSorted((a, b) => a - b);
  if (ratios.length < 3) return 1;
  const middle = Math.floor(ratios.length / 2);
  return ratios.length % 2 ? ratios[middle] : (ratios[middle - 1] + ratios[middle]) / 2;
}

// Why a test's weight must change (null: it need not), from its time on a runner of `speed` (runSpeed()): a weight
// missing on a test over 30 s, or off by more than half or double and by more than 30 s (load moves times less than
// that). Shards are split by the weights, so a stale one can push a shard to its time limit: it fails the run.
function weightNote(file, weight, seconds, speed = 1) {
  const usual = Math.max(1, Math.round(seconds / speed));
  const took = `${file} took ${Math.round(seconds)} s${speed === 1 ? "" : ` (~${usual}s on a usual runner)`}`;
  if (weight === null) return usual > 30 ? `${took} and has no weight: add "~${usual}s"` : null;
  return Math.abs(usual - weight) > 30 && (usual > 2 * weight || usual < weight / 2)
    ? `${took}, its weight is ~${weight}s: make it "~${usual}s"`
    : null;
}

// One shard's results: its failed tests, the passed ones whose weight must change (weightNote(), at the runner's
// speed) and that speed. A failed test's time says nothing about its usual one: it gets no weight note.
function judge(runs, weights) {
  const speed = runSpeed(runs, weights);
  const stale = [];
  for (const run of runs) {
    const note =
      run.status === 0 &&
      weightNote(run.file, weights.get(run.file) ?? null, Number(run.seconds), speed);
    if (note) stale.push({ file: run.file, note });
  }
  return { failed: runs.filter((run) => run.status !== 0).map((run) => run.file), stale, speed };
}

// The seconds `files` should take on a usual runner, `jobs` at once: their weights (UNWEIGHTED for none) over the
// slots, never less than the heaviest (the kit shards' shape test plans each shard the same way).
function expectedSeconds(files, weights, jobs) {
  const each = files.map((file) => weights.get(file) ?? UNWEIGHTED);
  return Math.max(each.reduce((total, seconds) => total + seconds, 0) / jobs, ...each, 0);
}

// A warning (null: none) when a shard took more than twice its expected time and over a minute more. Each weight is
// judged at the shard's own speed (runSpeed()), so a slowdown all its tests share (a slow runner, or a change that
// slows them all) is no stale weight: without this, only the job's time limit would show it. A runner can be slow,
// so it warns and does not fail.
function slowNote(files, weights, jobs, seconds) {
  const expected = expectedSeconds(files, weights, jobs);
  return seconds > 2 * expected && seconds - expected > 60
    ? `the tests here took ${Math.round(seconds)} s in all, ~${Math.round(expected)}s expected from their weights ` +
        `(${jobs} at once): a slowdown all of them share, which no weight shows; the job's time limit is next`
    : null;
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

// Every test file under TEST_DIRS. A folder that is not there adds none: a listed test in it is then
// missing, which plan() reports (a failure once the tested history had it).
function presentTests(root = ROOT) {
  return TEST_DIRS.filter((dir) => fs.existsSync(path.join(root, dir))).flatMap((dir) =>
    findTests(dir, root),
  );
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

// Calls start(item) for every item, at most `jobs` at once, each as soon as a slot is free.
// start returns a promise; the results come back in the items' order.
async function runAll(items, jobs, start) {
  const results = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const index = next++;
      results[index] = await start(items[index]);
    }
  }
  await Promise.all(Array.from({ length: Math.max(1, Math.min(jobs, items.length)) }, worker));
  return results;
}

function jobCount(env = process.env) {
  const jobs = Number(env.FORK_CI_TEST_JOBS);
  return Number.isInteger(jobs) && jobs > 0 ? jobs : os.availableParallelism();
}

// Runs one test file, its stdout and stderr kept together, and prints them as one group when it ends.
function runTest(file) {
  const [command, ...args] = RUNNER[file.match(TEST_FILE)[1]];
  const started = Date.now();
  return new Promise((resolve) => {
    const child = childProcess.spawn(command, [...args, file], {
      cwd: ROOT,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const output = [];
    child.stdout.on("data", (chunk) => output.push(chunk));
    child.stderr.on("data", (chunk) => output.push(chunk));
    let ended = false;
    const done = (status, signal) => {
      if (ended) return;
      ended = true;
      // Stop reading pipes a left-behind process still holds, so they do not keep this script alive.
      child.stdout.destroy();
      child.stderr.destroy();
      const seconds = ((Date.now() - started) / 1000).toFixed(1);
      process.stdout.write(`::group::${file} (${seconds} s)\n`);
      process.stdout.write(Buffer.concat(output));
      process.stdout.write("\n::endgroup::\n");
      if (status !== 0)
        console.log(`::error file=${file}::${file} failed (exit ${status ?? signal})`);
      resolve({ file, status, seconds });
    };
    child.on("error", (error) => {
      output.push(Buffer.from(`cannot start ${command}: ${error.message}\n`));
      done(null, "spawn error");
    });
    child.on("close", done);
    // A process the test left behind may hold the pipes open: the test is done when it exits.
    child.on("exit", (status, signal) => setTimeout(() => done(status, signal), 5000).unref());
  });
}

async function main(parts = process.argv.slice(2)) {
  const { index, count } = readShard();
  const entries = readList(fs.readFileSync(path.join(ROOT, LIST), "utf8")).filter((entry) =>
    inPart(entry.file, parts),
  );
  const present = presentTests().filter((file) => inPart(file, parts));
  const result = plan(entries, present, everInHistory, readFile);
  const weight = new Map(entries.map((entry) => [entry.file, entry.weight]));
  const rows = [];
  let failed =
    result.unlisted.length + result.deleted.length + result.noRunner.length + result.notRun.length;
  const mine = shard(result.run, entries, index, count);
  const jobs = jobCount();
  const started = Date.now();
  const runs = await runAll(order(mine, entries), jobs, runTest);
  const seconds = (Date.now() - started) / 1000;
  const judged = judge(runs, weight);
  const { speed } = judged;
  const stale = judged.stale.map(({ note }) => note);
  failed += judged.failed.length + stale.length;
  for (const { file, note } of judged.stale)
    console.log(`::error file=${file}::${note} in ${LIST}`);
  const slow = slowNote(mine, weight, jobs, seconds);
  if (slow) console.log(`::warning::${slow}`);
  for (const run of runs) {
    rows.push(
      `| \`${run.file}\` | ${run.status === 0 ? "passed" : "**failed**"} | ${run.seconds} s | ${weight.get(run.file) ? `~${weight.get(run.file)}s` : ""} |`,
    );
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
  // A part that runs nothing is a wrong argument (a typo, a renamed folder): its tests would never run.
  const empty = (parts.length > 0 || count > 1) && mine.length === 0;
  const named = [...parts, ...(count > 1 ? [`shard ${index}/${count}`] : [])].join(" ");
  if (empty) {
    failed += 1;
    console.log(
      `::error::the part ${named} runs no test here: fix the argument of this fork-ci-tests.cjs call ` +
        `(a typo or a renamed folder?) or its number of shards, or its tests never run.`,
    );
  }
  for (const { file, runBy } of result.elsewhere)
    rows.push(`| \`${file}\` | run by \`${runBy}\` | | |`);
  for (const file of result.pending)
    rows.push(`| \`${file}\` | not present: its PR has not merged | | |`);
  for (const file of result.unlisted) rows.push(`| \`${file}\` | **not in ${LIST}** | | |`);
  for (const file of result.deleted) rows.push(`| \`${file}\` | **deleted while listed** | | |`);
  for (const file of result.noRunner)
    rows.push(`| \`${file}\` | **no runner for its extension** | | |`);
  for (const { file, why } of result.notRun) rows.push(`| \`${file}\` | **not run: ${why}** | | |`);

  const summary = process.env.GITHUB_STEP_SUMMARY;
  if (summary)
    fs.appendFileSync(
      summary,
      [
        `### Fork script tests${named ? ` (${named})` : ""}`,
        "",
        ...(empty ? [`**No test runs in this part: check its argument.**`, ""] : []),
        ...(slow ? [`**Slow:** ${slow}.`, ""] : []),
        ...(stale.length
          ? [
              `**Weights to update in \`${LIST}\`** (this runner's speed: ${speed.toFixed(2)}x the weights):`,
              "",
              ...stale.map((note) => `- ${note}`),
              "",
            ]
          : []),
        "| Test | Result | Time | Weight |",
        "| --- | --- | --- | --- |",
        ...rows,
        "",
      ].join("\n") + "\n",
    );
  if (failed) process.exitCode = 1;
}

module.exports = {
  plan,
  readList,
  order,
  shard,
  readShard,
  inPart,
  runSpeed,
  weightNote,
  judge,
  expectedSeconds,
  slowNote,
  findTests,
  presentTests,
  runAll,
  jobCount,
};

if (require.main === module)
  main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
