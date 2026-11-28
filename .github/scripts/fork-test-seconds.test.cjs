const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  parseTestLog,
  buildTable,
  fitWalls,
  render,
  tableFromLogs,
} = require("./fork-test-seconds.cjs");

const SCRIPT = path.join(__dirname, "fork-test-seconds.cjs");
const packages = [
  { name: "@t/a", dir: "packages/a" },
  { name: "@t/b", dir: "packages/b" },
  { name: "@t/c", dir: "packages/c" },
  { name: "mobile", dir: "apps/mobile" },
];
const testFiles = [
  "packages/a/src/one.test.ts",
  "packages/a/src/same.test.ts",
  "packages/b/src/two.test.tsx",
  "packages/b/src/same.test.ts",
  "packages/c/src/same.test.ts",
  "apps/mobile/src/screen.test.tsx",
  "apps/mobile/src/slow.test.ts",
  "native/kde/src/outside.test.ts",
];

// A `Test N` job log as GitHub serves it: timestamps, the step's env, whole packages, then slices.
const log = (lines) =>
  lines
    .map(([second, text]) => `2026-11-28T00:00:${String(second).padStart(2, "0")}.0000000Z ${text}`)
    .join("\n");
const JOB = log([
  [0, "##[group]Run set -o pipefail"],
  [0, "  WHOLE: @t/a @t/b"],
  [0, "##[endgroup]"],
  [2, " \\u2713 src/one.test.ts (3 tests) 1500ms"],
  [3, " \x1b[32m✓\x1b[39m src/two.test.tsx \x1b[2m(1 test)\x1b[22m 2.5s"],
  [3, " \\u2713 src/same.test.ts (2 tests) 1000ms"],
  [4, " \\u2713 src/same.test.ts (2 tests) 3000ms"],
  [4, " \\u2193 src/gone.test.ts (1 test | 1 skipped)"],
  [5, "    \\u2713 a test inside a file 400ms"],
  [10, "##[group]apps/mobile slice 1/2"],
  [11, "  \\u2713  react  src/screen.test.tsx (23 tests) 89ms"],
  [12, "  \\u2713  react  src/slow.test.ts (2 tests) 6000ms"],
  [13, "  \\u2713  unit  ../../native/kde/src/outside.test.ts (2 tests) 700ms"],
  [30, "##[endgroup]"],
  [31, "##[group]Run timeout -k 5 40 python3 -I .github/scripts/fork-ci-pressure.py end"],
]);

// Q: does the parser find each file line, of a whole package or a slice, and its package?
test("parseTestLog reads whole and sliced file lines, in ms or s, escaped or not", () => {
  const { seconds, skipped, runs } = parseTestLog(JOB, packages, testFiles);
  assert.equal(seconds.get("packages/a/src/one.test.ts"), 1.5);
  assert.equal(seconds.get("packages/b/src/two.test.tsx"), 2.5);
  // A project label before the path; a slice line counts for its group's package.
  assert.equal(seconds.get("apps/mobile/src/screen.test.tsx"), 0.089);
  assert.equal(seconds.get("apps/mobile/src/slow.test.ts"), 6);
  // A file the package's config takes from outside its folder: its own repo path.
  assert.equal(seconds.get("native/kde/src/outside.test.ts"), 0.7);
  // src/same.test.ts: a and b (WHOLE) both hold it, c is not in WHOLE: 4 s shared by a and b.
  assert.equal(seconds.get("packages/a/src/same.test.ts"), 2);
  assert.equal(seconds.get("packages/b/src/same.test.ts"), 2);
  assert.equal(seconds.has("packages/c/src/same.test.ts"), false);
  // An untracked file is skipped; a test line inside a file is no file line.
  assert.equal(skipped, 1);
  // Walls: the whole part until the first slice (10 s), the slice its group (20 s).
  assert.deepEqual(
    runs.map((run) => [run.mode, run.wall, run.files.length]),
    [
      ["whole", 10, 4],
      ["slice", 20, 3],
    ],
  );
});

test("a job without slices ends its whole part at the next step", () => {
  const { runs } = parseTestLog(
    log([
      [0, "##[group]Run set -o pipefail"],
      [0, "  WHOLE: @t/a"],
      [1, " \\u2713 src/one.test.ts (3 tests) 1500ms"],
      [8, "##[group]Run next step"],
    ]),
    packages,
    testFiles,
  );
  assert.deepEqual(
    runs.map((run) => [run.mode, run.wall]),
    [["whole", 8]],
  );
});

// Q: is a file's time its median over the runs, and is the table sorted and rounded?
test("buildTable takes each file's median over runs, sorted by path", () => {
  const run = (id, entries) => ({ id, seconds: new Map(entries) });
  const table = buildTable([
    run(3, [
      ["b.test.ts", 1],
      ["a.test.ts", 2],
    ]),
    run(1, [["a.test.ts", 4]]),
    run(2, [
      ["a.test.ts", 10],
      ["b.test.ts", 1.23456],
    ]),
  ]);
  assert.deepEqual(table, {
    unit: "seconds",
    runs: ["1", "2", "3"],
    model: {},
    files: { "a.test.ts": 4, "b.test.ts": 1.12 },
  });
  assert.deepEqual(Object.keys(table.files), ["a.test.ts", "b.test.ts"]);
});

test("fitWalls finds the per-file and per-second wall, and refuses what it cannot fix", () => {
  // wall = 0.5 per file + 2 per test second.
  const rows = [
    { files: 10, tests: 1, wall: 7 },
    { files: 4, tests: 3, wall: 8 },
    { files: 1, tests: 0, wall: 0.5 },
  ];
  assert.deepEqual(fitWalls(rows), { perFile: 0.5, perSecond: 2 });
  assert.equal(fitWalls(rows.slice(0, 1)), undefined);
  assert.equal(
    fitWalls([
      { files: 1, tests: 1, wall: 1 },
      { files: 2, tests: 2, wall: 2 },
    ]),
    undefined,
  );
  // A negative coefficient (more files, less wall) is no model.
  assert.equal(
    fitWalls([
      { files: 10, tests: 1, wall: 1 },
      { files: 1, tests: 1, wall: 5 },
    ]),
    undefined,
  );
});

// Q: does refreshing from the same logs write the same bytes, whatever order the files are read in?
test("the refresh gives the same file for the same logs", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-test-seconds-"));
  try {
    fs.writeFileSync(path.join(dir, "200-2.log"), JOB);
    fs.writeFileSync(path.join(dir, "100-1.log"), JOB.replaceAll("1500ms", "500ms"));
    fs.writeFileSync(path.join(dir, "notes.txt"), "not a log");
    const first = render(tableFromLogs(dir, packages, testFiles).table);
    assert.equal(render(tableFromLogs(dir, packages, testFiles).table), first);
    const table = JSON.parse(first);
    assert.deepEqual(table.runs, ["100", "200"]);
    assert.equal(table.files["packages/a/src/one.test.ts"], 1);
    assert.ok(first.endsWith("}\n"));
    // vp fmt's layout: the runs on one line, so a refresh leaves no format diff.
    assert.ok(first.includes('  "runs": ["100", "200"],\n'), first.slice(0, 80));

    // The command line, twice, into the same file: byte for byte the same. It reads this
    // repository's workspace, so its log names one of this repository's sliced test files.
    const real = childProcess
      .execFileSync("git", ["ls-files", "--", "apps/mobile"], {
        cwd: path.resolve(__dirname, "../.."),
        encoding: "utf8",
      })
      .split("\n")
      .find((file) => /\.test\.tsx?$/.test(file));
    const logs = fs.mkdtempSync(path.join(dir, "logs-"));
    fs.writeFileSync(
      path.join(logs, "300-3.log"),
      log([
        [0, "##[group]apps/mobile slice 1/2"],
        [1, ` \\u2713 ${real.slice("apps/mobile/".length)} (1 test) 1234ms`],
        [9, "##[endgroup]"],
      ]),
    );
    const out = path.join(dir, "out.json");
    const run = () => {
      childProcess.execFileSync(process.execPath, [SCRIPT, "--logs", logs, "--out", out], {
        stdio: "pipe",
      });
      return fs.readFileSync(out, "utf8");
    };
    const once = run();
    assert.equal(run(), once);
    assert.equal(JSON.parse(once).files[real], 1.23);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("logs without a file line write nothing and fail", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-test-seconds-"));
  try {
    fs.writeFileSync(path.join(dir, "1-1.log"), log([[0, "nothing here"]]));
    const out = path.join(dir, "out.json");
    assert.throws(() =>
      childProcess.execFileSync(process.execPath, [SCRIPT, "--logs", dir, "--out", out], {
        stdio: "pipe",
      }),
    );
    assert.equal(fs.existsSync(out), false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
