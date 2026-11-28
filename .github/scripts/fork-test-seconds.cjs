// Fork only: writes .github/fork-test-seconds.json (see .github/fork-test-seconds.md) from the
// `Test N` job logs of the last green pushes to main: each test file's median test seconds, and
// per mode (whole, slice) the wall a file and a test second add, fitted to the jobs' walls.
// Usage: fork-test-seconds.cjs [--runs 5] | --logs <dir of runId-jobId.log> [--out <file>]
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { readWorkspace } = require("./ci-changes.cjs");

const ROOT = path.resolve(__dirname, "../..");
const TABLE = ".github/fork-test-seconds.json";
const REPO = "scuttletoby/t3code";
const TEST_FILE = /\.(?:test|spec)\.[cm]?[jt]sx?$/;
const STAMP = /^\d{4}-\d\d-\d\dT[\d:.]+Z ?/;
const SLICE = /^##\[group\](\S+) slice \d+\/\d+\s*$/;
// The Test step runs output through a filter that writes ✓ and ↓ as ✓ and ↓.
const FILE_LINE =
  /^\s*(?:✓|↓|\\u2713|\\u2193)\s+(?:\S+\s+)??(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\s+\(\d+ tests?[^)]*\)(?:\s+(\d+(?:\.\d+)?)(ms|s))?/;

/** Repo paths of every tracked test file under `dirs`, and each relative path's possible owners. */
function indexTestFiles(testFiles, dirs) {
  const byRelative = new Map();
  for (const file of testFiles)
    for (const dir of dirs)
      if (file.startsWith(`${dir}/`)) {
        const rel = file.slice(dir.length + 1);
        byRelative.set(rel, [...(byRelative.get(rel) ?? []), { dir, file }]);
      }
  return byRelative;
}

/**
 * Seconds per test file (repo path) in one `Test N` job log. `packages`: [{ name, dir }] of the
 * workspace; `testFiles`: tracked test files. A file line among the whole packages counts when
 * one package (of the job's WHOLE list, when the log has it) holds that path; others are skipped.
 */
function parseTestLog(text, packages, testFiles) {
  const known = new Set(testFiles);
  const byRelative = indexTestFiles(
    testFiles,
    packages.map((pkg) => pkg.dir),
  );
  const seconds = new Map();
  const add = (file, value) => seconds.set(file, (seconds.get(file) ?? 0) + value);
  // A path several whole packages hold (src/server/adapter.test.ts): their lines' time, shared.
  const shared = new Map();
  let whole = null;
  let slice = null;
  let skipped = 0;
  // Walls: the Test step (the one whose env names WHOLE) from its start to its first slice, or
  // to the next step; each slice from its group to its end.
  const walls = [];
  let stepStart = null;
  let wholeWall = null;
  let current = null;
  for (const raw of text.split(/\r?\n/)) {
    const stamp = raw.match(STAMP);
    const at = stamp ? Date.parse(stamp[0].trim()) : NaN;
    const line = raw.replace(STAMP, "");
    if (line.startsWith("##[group]Run ")) {
      if (wholeWall && wholeWall.end === null) wholeWall.end = at;
      stepStart = at;
    }
    const env = line.match(/^\s+WHOLE: (.*)$/);
    if (env) {
      const names = new Set(env[1].split(/\s+/).filter(Boolean));
      whole = new Set(packages.filter((pkg) => names.has(pkg.name)).map((pkg) => pkg.dir));
      wholeWall = { mode: "whole", start: stepStart, end: null };
      walls.push(wholeWall);
      continue;
    }
    const group = line.match(SLICE);
    if (group) {
      slice = group[1];
      if (wholeWall && wholeWall.end === null) wholeWall.end = at;
      current = { mode: "slice", dir: slice, start: at, end: null };
      walls.push(current);
      continue;
    }
    if (line.startsWith("##[endgroup]")) {
      if (slice && current) current.end = at;
      slice = null;
      current = null;
      continue;
    }
    const match = line.match(FILE_LINE);
    if (!match) continue;
    const [, rel, amount, unit] = match;
    const value = amount === undefined ? 0 : Number(amount) / (unit === "ms" ? 1000 : 1);
    if (slice) {
      if (known.has(`${slice}/${rel}`)) add(`${slice}/${rel}`, value);
      else skipped += 1;
      continue;
    }
    let owners = byRelative.get(rel) ?? [];
    if (owners.length > 1 && whole) owners = owners.filter((owner) => whole.has(owner.dir));
    if (owners.length === 0) skipped += 1;
    else if (owners.length === 1) add(owners[0].file, value);
    else {
      const entry = shared.get(rel) ?? { owners, total: 0 };
      entry.total += value;
      shared.set(rel, entry);
    }
  }
  for (const { owners, total } of shared.values())
    for (const owner of owners) add(owner.file, total / owners.length);
  // Each wall with the files it ran: a slice its package's, the whole part every other one.
  const sliced = walls.filter((wall) => wall.mode === "slice").map((wall) => `${wall.dir}/`);
  const runs = walls
    .filter(
      (wall) => Number.isFinite(wall.start) && Number.isFinite(wall.end) && wall.end > wall.start,
    )
    .map((wall) => {
      const files = [...seconds.keys()].filter((file) =>
        wall.mode === "slice"
          ? file.startsWith(`${wall.dir}/`)
          : !sliced.some((prefix) => file.startsWith(prefix)),
      );
      return { mode: wall.mode, wall: (wall.end - wall.start) / 1000, files };
    })
    .filter((run) => run.files.length > 0);
  return { seconds, skipped, runs };
}

/**
 * Least squares, no intercept: wall = perFile * files + perSecond * test seconds, over `rows`
 * ({ wall, files, tests }). Undefined when the rows cannot fix both (fewer than 2, collinear) or
 * a coefficient comes out negative; fork-test-shards.cjs then uses its own.
 */
function fitWalls(rows) {
  if (rows.length < 2) return undefined;
  let [nn, nt, tt, nw, tw] = [0, 0, 0, 0, 0];
  for (const { wall, files, tests } of rows) {
    nn += files * files;
    nt += files * tests;
    tt += tests * tests;
    nw += files * wall;
    tw += tests * wall;
  }
  const det = nn * tt - nt * nt;
  if (!(Math.abs(det) > 1e-9)) return undefined;
  const perFile = (nw * tt - tw * nt) / det;
  const perSecond = (tw * nn - nw * nt) / det;
  if (!(perFile >= 0 && perSecond >= 0)) return undefined;
  const round = (value) => Math.round(value * 1000) / 1000;
  return { perFile: round(perFile), perSecond: round(perSecond) };
}

const median = (values) => {
  const sorted = values.toSorted((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
};

/**
 * One table from several runs: runs[i] = { id, seconds: Map }, a job's maps merged per run.
 * `walls`: [{ mode, wall, files }] of every job, fitted per mode against the table's times.
 */
function buildTable(runs, walls = []) {
  const perFile = new Map();
  for (const run of runs)
    for (const [file, value] of run.seconds)
      perFile.set(file, [...(perFile.get(file) ?? []), value]);
  const files = Object.fromEntries(
    [...perFile]
      .map(([file, values]) => [file, Math.round(median(values) * 100) / 100])
      .toSorted(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
  );
  const ids = [...new Set(runs.map((run) => String(run.id)))].toSorted();
  const model = {};
  for (const mode of ["whole", "slice"]) {
    const rows = walls
      .filter((wall) => wall.mode === mode)
      .map((wall) => ({
        wall: wall.wall,
        files: wall.files.length,
        tests: wall.files.reduce((total, file) => total + (files[file] ?? 0), 0),
      }));
    const coefficients = fitWalls(rows);
    if (coefficients) model[mode] = coefficients;
  }
  return { unit: "seconds", runs: ids, model, files };
}

const render = (table) => `${JSON.stringify(table, null, 2)}\n`;

/** Reads <runId>-<jobId>.log files of `dir` into one table. */
function tableFromLogs(dir, packages, testFiles) {
  const byRun = new Map();
  const walls = [];
  let skipped = 0;
  for (const name of fs.readdirSync(dir).toSorted()) {
    const id = name.match(/^(\d+)-\d+\.log$/)?.[1];
    if (!id) continue;
    const log = parseTestLog(fs.readFileSync(path.join(dir, name), "utf8"), packages, testFiles);
    skipped += log.skipped;
    walls.push(...log.runs);
    const run = byRun.get(id) ?? new Map();
    for (const [file, value] of log.seconds) run.set(file, (run.get(file) ?? 0) + value);
    byRun.set(id, run);
  }
  const runs = [...byRun].map(([id, seconds]) => ({ id, seconds }));
  return { table: buildTable(runs, walls), skipped };
}

const gh = (args) =>
  childProcess.execFileSync("gh", args, { encoding: "utf8", maxBuffer: 1 << 28 });

/** Downloads the `Test N` job logs of the last `count` green pushes to main into `dir`. */
function fetchLogs(dir, count) {
  const runs = JSON.parse(
    gh(
      ["run", "list", "-R", REPO, "--workflow", "fork-tests.yml", "--branch", "main"].concat([
        "--event",
        "push",
        "--status",
        "success",
        "-L",
        String(count),
        "--json",
        "databaseId",
      ]),
    ),
  );
  for (const { databaseId: run } of runs) {
    const jobs = JSON.parse(
      gh(["api", `repos/${REPO}/actions/runs/${run}/jobs?per_page=100`, "--jq", ".jobs"]),
    ).filter((job) => /^Test \d+$/.test(job.name) && job.conclusion === "success");
    for (const job of jobs) {
      const log = gh([
        "api",
        "--allow-escape-sequences",
        `repos/${REPO}/actions/jobs/${job.id}/logs`,
      ]);
      fs.writeFileSync(path.join(dir, `${run}-${job.id}.log`), log.replace(/\x1b\[[0-9;]*m/g, ""));
    }
  }
}

function main(argv) {
  const option = (name) => {
    const at = argv.indexOf(name);
    return at === -1 ? undefined : argv[at + 1];
  };
  const out = path.resolve(ROOT, option("--out") ?? TABLE);
  const packages = readWorkspace().map((pkg) => ({ name: pkg.name, dir: pkg.dir }));
  const testFiles = childProcess
    .execFileSync("git", ["ls-files", "-z"], { cwd: ROOT, encoding: "utf8", maxBuffer: 1 << 26 })
    .split("\0")
    .filter((file) => TEST_FILE.test(file));
  let dir = option("--logs");
  if (!dir) {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-test-seconds-"));
    fetchLogs(dir, Number(option("--runs") ?? 5));
  }
  const { table, skipped } = tableFromLogs(dir, packages, testFiles);
  if (Object.keys(table.files).length === 0)
    throw new Error(`no test file times in the logs of ${dir}; ${TABLE} left as it was`);
  fs.writeFileSync(out, render(table));
  console.log(
    `${path.relative(ROOT, out)}: ${Object.keys(table.files).length} files from ${table.runs.length} run(s); ` +
      `model ${JSON.stringify(table.model)}; ${skipped} line(s) skipped (not a tracked test file). Logs: ${dir}`,
  );
}

module.exports = { parseTestLog, buildTable, fitWalls, render, tableFromLogs, TABLE };

if (require.main === module) main(process.argv.slice(2));
