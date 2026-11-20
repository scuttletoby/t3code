const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  plan,
  jsCode,
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
  declarations,
  ownRuns,
  runKey,
  expectedKeys,
  countTests,
  recordProblems,
  record,
  recordsTo,
  writeRecords,
  command,
} = require("./fork-ci-tests.cjs");

const never = () => false;
const here = (...files) => files.map((file) => ({ file, runBy: null }));

test("runs every listed present test with a runner", () => {
  assert.deepEqual(plan(here("a.test.sh", "b.test.mjs"), ["a.test.sh", "b.test.mjs"], never).run, [
    "a.test.sh",
    "b.test.mjs",
  ]);
});

test("a present test missing from the list fails", () => {
  assert.deepEqual(plan(here("a.test.sh"), ["a.test.sh", "b.test.js"], never).unlisted, [
    "b.test.js",
  ]);
});

test("a listed test with no runner for its extension fails and is not run", () => {
  const result = plan(here("a.test.ts"), ["a.test.ts"], never);
  assert.deepEqual(result.noRunner, ["a.test.ts"]);
  assert.deepEqual(result.run, []);
});

test("a listed test never in history is pending, not a failure", () => {
  const result = plan(here("a.test.sh", "later.test.sh"), ["a.test.sh"], never);
  assert.deepEqual(result.pending, ["later.test.sh"]);
  assert.deepEqual(result.deleted, []);
});

test("a listed test deleted from history fails", () => {
  const result = plan(
    here("a.test.sh", "gone.test.sh"),
    ["a.test.sh"],
    (f) => f === "gone.test.sh",
  );
  assert.deepEqual(result.deleted, ["gone.test.sh"]);
  assert.deepEqual(result.pending, []);
});

test("a test run elsewhere must be named by its runner, which is not an upstream workflow", () => {
  const files = {
    ".github/workflows/fork-tests.yml":
      "run: node --test x/named.test.cjs\n" +
      "      # x/commented.test.cjs\n" +
      "      - run: |\n" +
      "          # x/commented.test.cjs\n" +
      "          cd x && node --test commented.test.cjs\n" +
      "      - run: true # x/trailing.test.cjs\n",
    ".github/workflows/ci.yml": "run: node --test x/upstream.test.cjs",
    "scripts/smoke.ts":
      '// x/commented-ts.test.cjs\nrun(["--test", "x/named-ts.test.cjs"]);\n' +
      "run([]); // x/trailing-ts.test.cjs\n" +
      "/* x/block-ts.test.cjs\n   x/block-ts.test.cjs */ run([]);\n" +
      'fetch("https://example.invalid/x/url-ts.test.cjs");\n',
  };
  const entries = [
    { file: "x/named.test.cjs", runBy: ".github/workflows/fork-tests.yml" },
    { file: "x/unnamed.test.cjs", runBy: ".github/workflows/fork-tests.yml" },
    { file: "x/upstream.test.cjs", runBy: ".github/workflows/ci.yml" },
    { file: "x/nowhere.test.cjs", runBy: "scripts/missing.ts" },
    // Named only in a YAML or shell comment (the run goes there by `cd`): not run.
    { file: "x/commented.test.cjs", runBy: ".github/workflows/fork-tests.yml" },
    { file: "x/commented-ts.test.cjs", runBy: "scripts/smoke.ts" },
    { file: "x/named-ts.test.cjs", runBy: "scripts/smoke.ts" },
    // A comment after code, or a /* */ block over lines, names nothing either; a URL's // is not a comment.
    { file: "x/trailing.test.cjs", runBy: ".github/workflows/fork-tests.yml" },
    { file: "x/trailing-ts.test.cjs", runBy: "scripts/smoke.ts" },
    { file: "x/block-ts.test.cjs", runBy: "scripts/smoke.ts" },
    { file: "x/url-ts.test.cjs", runBy: "scripts/smoke.ts" },
  ];
  const result = plan(
    entries,
    entries.map((e) => e.file),
    never,
    (file) => files[file] ?? null,
  );
  assert.deepEqual(result.run, []);
  assert.deepEqual(result.elsewhere, [
    { file: "x/named.test.cjs", runBy: ".github/workflows/fork-tests.yml" },
    { file: "x/named-ts.test.cjs", runBy: "scripts/smoke.ts" },
    { file: "x/url-ts.test.cjs", runBy: "scripts/smoke.ts" },
  ]);
  assert.deepEqual(
    result.notRun.map((n) => n.file),
    [
      "x/unnamed.test.cjs",
      "x/upstream.test.cjs",
      "x/nowhere.test.cjs",
      "x/commented.test.cjs",
      "x/commented-ts.test.cjs",
      "x/trailing.test.cjs",
      "x/trailing-ts.test.cjs",
      "x/block-ts.test.cjs",
    ],
  );
  assert.match(result.notRun[1].why, /upstream workflow/);
  assert.match(
    result.notRun[3].why,
    /does not name x\/commented\.test\.cjs \(a comment does not count\)/,
  );
});

// Upstream's scripts/release-smoke.ts has `'**/*'` in a template literal: a comment marker inside a string, a
// template literal or a regular expression starts no comment, so a later `/** ... */` removes nothing before it.
test("a JS or TS runner's comment markers inside strings, templates and regular expressions are not comments", () => {
  const files = {
    "scripts/smoke.ts": [
      "const yaml = `",
      "  paths:",
      "    - '**/*'",
      "`;",
      'const url = "//x"; run(["--test", "x/after-string.test.cjs"]);',
      "const glob = `${dir}/**/*${ext ? `/*${ext}` : ''}`; run(['x/after-template.test.cjs']);",
      'const re = /\\/\\*|[/*]/g; run(["x/after-regex.test.cjs"]); // x/comment.test.cjs',
      "const half = total / 2; run(['x/after-division.test.cjs']); // x/after-division-comment.test.cjs",
      "/** A doc block after them all. */",
      "export const later = 1;",
    ].join("\n"),
  };
  const entries = [
    "x/after-string.test.cjs",
    "x/after-template.test.cjs",
    "x/after-regex.test.cjs",
    "x/after-division.test.cjs",
    "x/comment.test.cjs",
    "x/after-division-comment.test.cjs",
  ].map((file) => ({ file, runBy: "scripts/smoke.ts" }));
  const result = plan(
    entries,
    entries.map((e) => e.file),
    never,
    (file) => files[file] ?? null,
  );
  assert.deepEqual(
    result.elsewhere.map((e) => e.file),
    entries.slice(0, 4).map((e) => e.file),
  );
  assert.deepEqual(
    result.notRun.map((n) => n.file),
    ["x/comment.test.cjs", "x/after-division-comment.test.cjs"],
  );
  // Comments are blanked, not cut: the lines and their numbers stay.
  const text = files["scripts/smoke.ts"];
  assert.equal(jsCode(text).split("\n").length, text.split("\n").length);
  assert.match(jsCode(text).split("\n")[8], /^ +$/);
});

test("the real release-smoke.ts names its listed test outside a comment", () => {
  const root = path.resolve(__dirname, "../..");
  const text = fs.readFileSync(path.join(root, "scripts/release-smoke.ts"), "utf8");
  assert.ok(text.includes("'**/*'"), "the shape this guards against is still there");
  // A doc block after the whole file, as upstream could add: the test is still named.
  const later = `${text}\n/** a later doc block */\n`;
  const result = plan(
    [{ file: ".github/scripts/relay-state-output.test.cjs", runBy: "scripts/release-smoke.ts" }],
    [".github/scripts/relay-state-output.test.cjs"],
    never,
    () => later,
  );
  assert.deepEqual(result.notRun, []);
});

test("the list ignores comments and blank lines and reads `run by` and weights", () => {
  assert.deepEqual(
    readList("# head\n\na.test.sh  # from #2\n  b.test.py run by c.yml\nd.test.cjs ~120s\n"),
    [
      { file: "a.test.sh", runBy: null, weight: null },
      { file: "b.test.py", runBy: "c.yml", weight: null },
      { file: "d.test.cjs", runBy: null, weight: 120 },
    ],
  );
  // One spelling of a runner's path, so `./a/b` and `a//b` compare equal to `a/b` wherever the list is read.
  assert.deepEqual(
    readList(
      "e.test.py run by ./.github/workflows/x.yml\nf.test.py run by .github//workflows/x.yml\n",
    ).map((e) => e.runBy),
    [".github/workflows/x.yml", ".github/workflows/x.yml"],
  );
  for (const bad of [
    "a.test.sh run c.yml",
    "a.test.sh ~0s",
    "a.test.sh ~12",
    "a.test.sh ~1.5s",
    "a.test.sh ~12s run by c.yml",
  ])
    assert.throws(() => readList(`${bad}\n`), /cannot read/, bad);
});

test("the heaviest tests start first, tests without a weight before them, ties in list order", () => {
  const entries = readList(
    "a.test.sh ~10s\nb.test.sh\nc.test.sh ~90s\nd.test.sh ~10s\ne.test.sh\n",
  );
  assert.deepEqual(
    order(["a.test.sh", "b.test.sh", "c.test.sh", "d.test.sh", "e.test.sh"], entries),
    ["b.test.sh", "e.test.sh", "c.test.sh", "a.test.sh", "d.test.sh"],
  );
  assert.deepEqual(order(["d.test.sh", "a.test.sh"], entries), ["a.test.sh", "d.test.sh"]);
});

test("shards split the files by weight, heaviest first onto the lightest shard, each file in exactly one", () => {
  const entries = readList(
    "a.test.sh ~100s\nb.test.sh ~60s\nc.test.sh ~50s\nd.test.sh ~40s\ne.test.sh\nf.test.sh ~5s\n",
  );
  const files = entries.map((entry) => entry.file);
  const shards = [1, 2, 3].map((index) => shard(files, entries, index, 3));
  // a 100 | b 60 | c 50, then d 40 onto c (90), e (no weight: counts 10) onto b (70), f 5 onto b (75).
  assert.deepEqual(shards, [
    ["a.test.sh"],
    ["b.test.sh", "e.test.sh", "f.test.sh"],
    ["c.test.sh", "d.test.sh"],
  ]);
  assert.deepEqual(shards.flat().toSorted(), files.toSorted());
  assert.deepEqual(shard(files, entries, 1, 1).toSorted(), files.toSorted());
  // The same split from any order of the files: every shard of a run computes it on its own.
  assert.deepEqual(
    [1, 2, 3].map((index) => shard(files.toReversed(), entries, index, 3)),
    shards,
  );
  // More shards than files: the extra ones get none (the run then fails: an empty part).
  assert.deepEqual(shard(["a.test.sh"], entries, 2, 2), []);
});

// The kit shards in .github/workflows/fork-tests.yml: the matrix's shard numbers, and the env that hands this script
// its shard. One job of the kit's tests reached 8 to 9 of its 10 minutes: sharded, each stays under half.
test("the kit shards: a matrix of shards, each handed to this script, each well under the job's time limit", () => {
  const root = path.resolve(__dirname, "../..");
  const workflow = fs.readFileSync(path.join(root, ".github/workflows/fork-tests.yml"), "utf8");
  const start = workflow.indexOf("\n  kit_tests:\n");
  assert.notEqual(start, -1);
  const rest = workflow.slice(start + 1);
  const job = rest.slice(0, rest.slice(1).search(/\n {2}[a-z_]+:\n/) + 1);
  const numbers = job.match(/\n {8}shard: \[([\d, ]+)\]\n/);
  assert.ok(numbers, "kit_tests has no `shard: [1, 2, ...]` matrix");
  const count = numbers[1].split(",").length;
  assert.deepEqual(
    numbers[1].split(",").map(Number),
    Array.from({ length: count }, (_, i) => i + 1),
  );
  assert.ok(count >= 2, numbers[0]);
  assert.match(
    job,
    /\n {10}FORK_CI_TEST_SHARD: \$\{\{ matrix\.shard \}\}\/\$\{\{ strategy\.job-total \}\}\n/,
  );
  const limit = Number(job.match(/\n {4}timeout-minutes: (\d+)\n/)[1]) * 60;

  const entries = readList(fs.readFileSync(path.join(root, ".github/fork-ci-tests.txt"), "utf8"));
  const kit = entries.filter((entry) => !entry.runBy && entry.file.startsWith("fork-kit/"));
  const files = kit.map((entry) => entry.file);
  const shards = Array.from({ length: count }, (_, i) => shard(files, kit, i + 1, count));
  assert.deepEqual(shards.flat().toSorted(), files.toSorted());
  const weight = (file) => kit.find((e) => e.file === file).weight ?? 10;
  const sum = (list) => list.reduce((total, file) => total + weight(file), 0);
  const loads = shards.map(sum);
  // Balanced: no shard over the average by more than the heaviest test.
  const heaviest = Math.max(...files.map(weight));
  assert.ok(Math.max(...loads) <= sum(files) / count + heaviest, loads.join(" "));
  // A weight is a file's time with the others running beside it on a usual runner's 4 cores, so a shard takes about
  // its load over 4, and never less than its heaviest file. Half the limit leaves room for a slow runner.
  const wall = shards.map((list) => Math.max(sum(list) / 4, ...list.map(weight)));
  assert.ok(
    Math.max(...wall) <= limit / 2,
    `shards' expected seconds ${wall.map(Math.round).join(" ")}, limit ${limit}`,
  );
});

test("FORK_CI_TEST_SHARD is <index>/<count>; unset or empty is the whole part; anything else throws", () => {
  assert.deepEqual(readShard({}), { index: 1, count: 1 });
  assert.deepEqual(readShard({ FORK_CI_TEST_SHARD: "" }), { index: 1, count: 1 });
  assert.deepEqual(readShard({ FORK_CI_TEST_SHARD: "2/3" }), { index: 2, count: 3 });
  for (const bad of ["0/3", "4/3", "1", "1/0", "a/b", "1/3 ", "-1/3", "/3"])
    assert.throws(() => readShard({ FORK_CI_TEST_SHARD: bad }), /FORK_CI_TEST_SHARD/, bad);
});

test("a part is the files under its prefixes and under none of its `!` prefixes", () => {
  const files = ["fork-kit/a.test.py", "fork-kit/x/b.test.sh", ".github/scripts/c.test.cjs"];
  const pick = (...parts) => files.filter((file) => inPart(file, parts));
  assert.deepEqual(pick(), files);
  assert.deepEqual(pick("fork-kit/"), files.slice(0, 2));
  assert.deepEqual(pick("!fork-kit/"), files.slice(2));
  assert.deepEqual(pick("fork-kit/", "!fork-kit/x/"), files.slice(0, 1));
});

test("a long test without a weight, or one far from its weight, gets a note with the weight to write", () => {
  assert.equal(weightNote("a.test.sh", null, 20), null);
  assert.match(weightNote("a.test.sh", null, 45), /no weight: add "~45s"/);
  assert.equal(weightNote("a.test.sh", 100, 180), null); // load moves times this much
  assert.equal(weightNote("a.test.sh", 100, 60), null);
  assert.match(weightNote("a.test.sh", 100, 230), /weight is ~100s: make it "~230s"/);
  assert.equal(weightNote("a.test.sh", 10, 35), null);
  assert.match(weightNote("a.test.sh", 10, 45), /make it "~45s"/);
  assert.match(weightNote("a.test.sh", 200, 20), /make it "~20s"/);
  // The edges: over 30 s (no weight), over 30 s off and over double or under half.
  assert.equal(weightNote("a.test.sh", null, 30), null);
  assert.match(weightNote("a.test.sh", null, 31), /add "~31s"/);
  assert.equal(weightNote("a.test.sh", 10, 40), null);
  assert.match(weightNote("a.test.sh", 10, 41), /make it "~41s"/);
  assert.equal(weightNote("a.test.sh", 100, 200), null);
  assert.match(weightNote("a.test.sh", 100, 201), /make it "~201s"/);
  assert.equal(weightNote("a.test.sh", 200, 100), null);
  assert.match(weightNote("a.test.sh", 200, 99), /make it "~99s"/);
  // On a runner twice as slow as usual, times are read at half: a test that only took twice its weight is fine,
  // and the weight to write is the usual time.
  assert.equal(weightNote("a.test.sh", 100, 230, 2), null);
  assert.equal(weightNote("a.test.sh", null, 50, 2), null);
  assert.match(
    weightNote("a.test.sh", 20, 200, 2),
    /took 200 s \(~100s on a usual runner\), .* make it "~100s"/,
  );
});

test("a weight note names the usual time only when the runner's speed is not 1", () => {
  assert.equal(
    weightNote("a.test.sh", 100, 230),
    'a.test.sh took 230 s, its weight is ~100s: make it "~230s"',
  );
  assert.equal(
    weightNote("a.test.sh", null, 45),
    'a.test.sh took 45 s and has no weight: add "~45s"',
  );
  assert.equal(
    weightNote("a.test.sh", 20, 200, 2),
    'a.test.sh took 200 s (~100s on a usual runner), its weight is ~20s: make it "~100s"',
  );
});

test("a shard's results: its failed tests, and a weight note only for a passed test far from its weight", () => {
  const weights = new Map([
    ["far", 100],
    ["near", 100],
    ["broke", 100],
    ["new", null],
  ]);
  const run = (file, seconds, status = 0) => ({ file, seconds: String(seconds), status });
  // `broke` failed after 300 s, far from its ~100s: a failure, but its time says nothing about its weight.
  const { failed, stale, speed } = judge(
    [run("far", 300), run("near", 100), run("broke", 300, 1), run("new", 5, null)],
    weights,
  );
  assert.deepEqual(failed, ["broke", "new"]);
  assert.deepEqual(stale, [{ file: "far", note: weightNote("far", 100, 300) }]);
  assert.equal(speed, 1); // two passed weighted tests are too few to tell
  // At a speed read from enough tests, notes use it; nothing passed, nothing to judge.
  const many = new Map([...weights, ["b", 50], ["c", 50]]);
  const slow = judge([run("near", 200), run("b", 100), run("c", 100), run("far", 900)], many);
  assert.equal(slow.speed, 2);
  assert.deepEqual(slow.stale, [{ file: "far", note: weightNote("far", 100, 900, 2) }]);
  assert.deepEqual(judge([], weights), { failed: [], stale: [], speed: 1 });
});

// A slowdown all of a shard's tests share moves the speed, not any one test against it: no weight note shows it, so
// the shard's own time against its weights does, as a warning.
test("a shard over twice its expected time, and by over a minute, gets a warning; the weights alone do not", () => {
  const weights = new Map([
    ["a", 100],
    ["b", 60],
    ["c", 60],
    ["d", 20],
    ["e", null],
  ]);
  const files = ["a", "b", "c", "d", "e"];
  // 250 s of weight on 4 slots is 62.5 s, but never less than the heaviest test.
  assert.equal(expectedSeconds(files, weights, 4), 100);
  assert.equal(expectedSeconds(files, weights, 1), 250);
  assert.equal(expectedSeconds(["e"], weights, 4), 10);
  assert.equal(expectedSeconds([], weights, 4), 0);
  assert.equal(slowNote(files, weights, 4, 200), null); // twice, not more
  assert.equal(slowNote(files, weights, 4, 120), null);
  assert.equal(
    slowNote(files, weights, 4, 201),
    "the tests here took 201 s in all, ~100s expected from their weights (4 at once): a slowdown all of them " +
      "share, which no weight shows; the job's time limit is next",
  );
  assert.equal(slowNote(["e"], weights, 4, 65), null); // over twice, but under a minute more
  assert.equal(slowNote(["e"], weights, 4, 70), null); // a minute more exactly is not over a minute
  assert.notEqual(slowNote(["e"], weights, 4, 70.5), null);
  assert.match(slowNote(["e"], weights, 4, 71), /took 71 s in all, ~10s expected/);
  assert.match(slowNote(files, weights, 1, 600), /~250s expected .*\(1 at once\)/);
  // Every test 2.5 times its weight: the speed takes it all, no weight is stale, only the shard's time shows it.
  const runs = files.map((file) => ({
    file,
    seconds: String(2.5 * (weights.get(file) ?? 10)),
    status: 0,
  }));
  assert.deepEqual(judge(runs, weights).stale, []);
  assert.notEqual(slowNote(files, weights, 4, 250), null);
});

test("the runner's speed is the median of time over weight of the passed tests weighing 10 s or more", () => {
  const weights = new Map([
    ["a", 100],
    ["b", 50],
    ["c", 20],
    ["d", 5],
    ["e", null],
    ["f", 40],
  ]);
  const run = (file, seconds, status = 0) => ({ file, seconds: String(seconds), status });
  // a 2x, b 3x, c 1x; d (under 10 s), e (none) and the failed f do not count
  assert.equal(
    runSpeed(
      [run("a", 200), run("b", 150), run("c", 20), run("d", 50), run("e", 9), run("f", 1, 1)],
      weights,
    ),
    2,
  );
  assert.equal(runSpeed([run("a", 200), run("b", 150), run("c", 20), run("f", 160)], weights), 2.5);
  assert.equal(runSpeed([run("a", 200), run("b", 150)], weights), 1); // too few to tell
});

test("finds every *.test.* file, subdirectories included", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-tests-"));
  try {
    for (const file of ["s/a.test.cjs", "s/b.cjs", "s/sub/c.test.mjs", "s/sub/deep/d.test.ts"]) {
      fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
      fs.writeFileSync(path.join(root, file), "");
    }
    assert.deepEqual(findTests("s", root), [
      "s/a.test.cjs",
      "s/sub/c.test.mjs",
      "s/sub/deep/d.test.ts",
    ]);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("the composite actions' tests under fork-ci are found too, and a missing folder adds none", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-tests-"));
  const files = [".github/scripts/a.test.cjs", "fork-ci/actions/x/b.test.py", "other/c.test.py"];
  try {
    for (const file of files) {
      fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
      fs.writeFileSync(path.join(root, file), "");
    }
    assert.deepEqual(presentTests(root), files.slice(0, 2));
    fs.rmSync(path.join(root, "fork-ci"), { recursive: true });
    assert.deepEqual(presentTests(root), files.slice(0, 1));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("an unlisted test under .github/scripts or fork-ci fails the run", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-tests-"));
  try {
    const write = (file, text) => {
      fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
      fs.writeFileSync(path.join(root, file), text);
    };
    const script = fs.readFileSync(require.resolve("./fork-ci-tests.cjs"));
    write(".github/scripts/fork-ci-tests.cjs", script);
    write(".github/fork-ci-tests.txt", "# nothing listed\n");
    write(".github/scripts/s.test.sh", "");
    write("fork-ci/actions/x/a.test.py", "");
    const env = { ...process.env };
    delete env.GITHUB_STEP_SUMMARY;
    const run = childProcess.spawnSync("node", [".github/scripts/fork-ci-tests.cjs"], {
      cwd: root,
      env,
      encoding: "utf8",
    });
    assert.equal(run.status, 1, run.stdout + run.stderr);
    for (const file of [".github/scripts/s.test.sh", "fork-ci/actions/x/a.test.py"])
      assert.ok(
        run.stdout.includes(`${file} is not in .github/fork-ci-tests.txt`),
        `${file} not reported: ${run.stdout}`,
      );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("runAll runs at most `jobs` at once, starts the next as soon as a slot frees, keeps the order", async () => {
  const runs = new Map(); // item -> resolve of its run, in start order
  const waiting = []; // [count, resolve]: resolved once that many runs started
  const started = (count) =>
    new Promise((resolve) => (runs.size >= count ? resolve() : waiting.push([count, resolve])));
  let running = 0;
  let most = 0;
  const all = runAll(["a", "b", "c", "d"], 2, (item) => {
    running += 1;
    most = Math.max(most, running);
    const done = new Promise((resolve) => runs.set(item, resolve)).then(() => {
      running -= 1;
      return item.toUpperCase();
    });
    for (const [count, resolve] of waiting) if (runs.size >= count) resolve();
    return done;
  });
  await started(2);
  assert.deepEqual([...runs.keys()], ["a", "b"]); // two at once, c waits for a free slot
  runs.get("b")(); // the second ends first: its slot takes c
  await started(3);
  assert.deepEqual([...runs.keys()], ["a", "b", "c"]);
  runs.get("a")();
  await started(4);
  runs.get("d")();
  runs.get("c")();
  assert.deepEqual(await all, ["A", "B", "C", "D"]);
  assert.equal(most, 2);
  assert.deepEqual(await runAll([], 4, async () => 1), []);
});

test("jobCount reads FORK_CI_TEST_JOBS, else the CPU count", () => {
  assert.equal(jobCount({ FORK_CI_TEST_JOBS: "3" }), 3);
  for (const bad of [undefined, "", "0", "-2", "1.5", "many"])
    assert.equal(jobCount({ FORK_CI_TEST_JOBS: bad }), os.availableParallelism(), String(bad));
});

// Runs the real script in a scratch tree whose list names `tests` ({ file: shell text }).
function runScript(tests, jobs, args = [], list = Object.keys(tests).join("\n"), extra = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-tests-"));
  const write = (file, text) => {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.writeFileSync(path.join(root, file), text);
  };
  write(
    ".github/scripts/fork-ci-tests.cjs",
    fs.readFileSync(require.resolve("./fork-ci-tests.cjs")),
  );
  write(".github/fork-ci-tests.txt", list + "\n");
  for (const [file, text] of Object.entries(tests)) write(file, text);
  const env = {
    ...process.env,
    FORK_CI_TEST_JOBS: String(jobs),
    LEFT: path.join(root, "left.pid"),
    MARKS: root,
    ...extra,
  };
  for (const name of [
    "FORK_CI_TEST_SHARD",
    "FORK_CI_TEST_RECORDS",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_JOB",
  ]) {
    delete env[name];
    if (extra[name] !== undefined) env[name] = extra[name];
  }
  delete env.GITHUB_STEP_SUMMARY;
  const run = childProcess.spawnSync("node", [".github/scripts/fork-ci-tests.cjs", ...args], {
    cwd: root,
    env,
    encoding: "utf8",
    timeout: 60000,
  });
  return { root, run };
}

// A test file that passes only when `other` starts while it runs (it waits up to 5 s for the other's mark).
const meets = (name, other) =>
  `echo ${name}-start; : > "$MARKS/${name}"\n` +
  `for i in $(seq 50); do [ -e "$MARKS/${other}" ] && { echo ${name}-end; exit 0; }; sleep 0.1; done\n` +
  `echo "${other} did not run beside ${name}"; exit 1\n`;

test("test files run in parallel, each one's output in its own group, and a failure fails the run", () => {
  const { root, run } = runScript(
    {
      "fork-ci/a.test.sh": meets("a", "b"),
      "fork-ci/b.test.sh": meets("b", "a"),
      "fork-ci/c.test.sh": "echo c-start; echo c-end; exit 3\n",
    },
    3,
  );
  try {
    assert.equal(run.status, 1, run.stdout + run.stderr);
    for (const name of ["a", "b", "c"])
      assert.match(
        run.stdout,
        new RegExp(
          `::group::fork-ci/${name}\\.test\\.sh \\([0-9.]+ s\\)\\n${name}-start\\n${name}-end\\n`,
        ),
      );
    assert.doesNotMatch(run.stdout, /::error file=fork-ci\/[ab]\.test\.sh/);
    assert.match(
      run.stdout,
      /::error file=fork-ci\/c\.test\.sh::fork-ci\/c\.test\.sh failed \(exit 3\)/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("one part runs only its files, heaviest first; an unlisted file fails only its own part", () => {
  const tests = {
    "fork-kit/light.test.sh": "echo light\n",
    "fork-kit/heavy.test.sh": "echo heavy\n",
    "fork-ci/other.test.sh": "echo other\n",
    "fork-ci/unlisted.test.sh": "echo unlisted\n",
  };
  const list = "fork-kit/light.test.sh ~5s\nfork-kit/heavy.test.sh ~60s\nfork-ci/other.test.sh\n";
  const kit = runScript(tests, 1, ["fork-kit/"], list);
  const rest = runScript(tests, 1, ["!fork-kit/"], list);
  try {
    const groups = (out) => [...out.matchAll(/::group::(\S+)/g)].map((m) => m[1]);
    assert.deepEqual(groups(kit.run.stdout), ["fork-kit/heavy.test.sh", "fork-kit/light.test.sh"]);
    // heavy.test.sh ends at once, far from its ~60s: a stale weight fails the run and names the one to write.
    assert.equal(kit.run.status, 1, kit.run.stdout + kit.run.stderr);
    assert.match(
      kit.run.stdout,
      /::error file=fork-kit\/heavy\.test\.sh::fork-kit\/heavy\.test\.sh took \d+ s, its weight is ~60s: make it "~1s" in \.github\/fork-ci-tests\.txt/,
    );
    assert.doesNotMatch(kit.run.stdout, /::error file=fork-kit\/light/);
    assert.equal(rest.run.status, 1, rest.run.stdout + rest.run.stderr);
    assert.deepEqual(groups(rest.run.stdout), ["fork-ci/other.test.sh"]);
    assert.match(rest.run.stdout, /::error file=fork-ci\/unlisted\.test\.sh::.* is not in /);
  } finally {
    fs.rmSync(kit.root, { recursive: true, force: true });
    fs.rmSync(rest.root, { recursive: true, force: true });
  }
});

test("FORK_CI_TEST_SHARD runs one shard of the part; the shards together run each test once", () => {
  const tests = {
    "fork-kit/a.test.sh": "echo a\n",
    "fork-kit/b.test.sh": "echo b\n",
    "fork-kit/c.test.sh": "echo c\n",
    "fork-ci/d.test.sh": "echo d\n",
  };
  const list =
    "fork-kit/a.test.sh ~3s\nfork-kit/b.test.sh ~2s\nfork-kit/c.test.sh ~2s\nfork-ci/d.test.sh\n";
  const groups = (out) => [...out.matchAll(/::group::(\S+)/g)].map((m) => m[1]);
  const runs = ["1/2", "2/2"].map((value) =>
    runScript(tests, 1, ["fork-kit/"], list, { FORK_CI_TEST_SHARD: value }),
  );
  const bad = runScript(tests, 1, ["fork-kit/"], list, { FORK_CI_TEST_SHARD: "3/2" });
  const empty = runScript(tests, 1, ["fork-kit/"], list, { FORK_CI_TEST_SHARD: "4/4" });
  try {
    for (const { run } of runs) assert.equal(run.status, 0, run.stdout + run.stderr);
    assert.deepEqual(groups(runs[0].run.stdout), ["fork-kit/a.test.sh"]);
    assert.deepEqual(groups(runs[1].run.stdout), ["fork-kit/b.test.sh", "fork-kit/c.test.sh"]);
    assert.notEqual(bad.run.status, 0);
    assert.match(bad.run.stderr, /FORK_CI_TEST_SHARD="3\/2"/);
    assert.deepEqual(groups(bad.run.stdout), []);
    // More shards than tests: the empty one fails instead of passing with nothing run.
    assert.equal(empty.run.status, 1, empty.run.stdout + empty.run.stderr);
    assert.match(empty.run.stdout, /::error::the part fork-kit\/ shard 4\/4 runs no test here/);
  } finally {
    for (const { root } of [...runs, bad, empty]) fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a part that runs no test fails, so a wrong argument cannot skip its tests", () => {
  const tests = { "fork-kit/a.test.sh": "echo a\n", "fork-ci/b.test.sh": "echo b\n" };
  for (const args of [["fork-kits/"], ["fork-kit/", "!fork-kit/"]]) {
    const { root, run } = runScript(tests, 1, args);
    try {
      assert.equal(run.status, 1, run.stdout + run.stderr);
      assert.match(run.stdout, new RegExp(`::error::the part ${args.join(" ")} runs no test here`));
      assert.doesNotMatch(run.stdout, /::group::/);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
  const { root, run } = runScript(tests, 1, ["fork-kit/"]);
  try {
    assert.equal(run.status, 0, run.stdout + run.stderr);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// main() in this process, on a scratch copy (its ROOT), with a fake runTest and clock: the shard "took" `seconds`.
async function mainTaking(seconds, list) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-main-"));
  const script = path.join(root, ".github/scripts/fork-ci-tests.cjs");
  fs.mkdirSync(path.dirname(script), { recursive: true });
  fs.copyFileSync(require.resolve("./fork-ci-tests.cjs"), script);
  fs.writeFileSync(path.join(root, ".github/fork-ci-tests.txt"), list.join("\n") + "\n");
  for (const line of list) {
    fs.mkdirSync(path.join(root, path.dirname(line.split(" ")[0])), { recursive: true });
    fs.writeFileSync(path.join(root, line.split(" ")[0]), "exit 0\n");
  }
  const summary = path.join(root, "summary.md");
  const saved = { ...process.env };
  const exitCode = process.exitCode;
  const log = console.log;
  const out = [];
  Object.assign(process.env, { FORK_CI_TEST_JOBS: "1", GITHUB_STEP_SUMMARY: summary });
  delete process.env.FORK_CI_TEST_SHARD;
  delete process.env.FORK_CI_TEST_RECORDS;
  console.log = (...args) => out.push(args.join(" "));
  const clock = [0, seconds * 1000];
  try {
    await require(script).main(["fork-ci/"], {
      runTest: async (file) => ({ file, status: 0, seconds: "1.0" }),
      now: () => clock.shift(),
    });
    return {
      out: out.join("\n"),
      summary: fs.readFileSync(summary, "utf8"),
      exitCode: process.exitCode,
    };
  } finally {
    console.log = log;
    process.exitCode = exitCode;
    for (const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key];
    Object.assign(process.env, saved);
    fs.rmSync(root, { recursive: true, force: true });
  }
}

// main() itself prints the warning and the summary's Slow line, and neither fails the run.
test("a shard over twice its expected time and by over a minute warns in the log and the summary, and passes", async () => {
  const list = ["fork-ci/a.test.sh ~5s", "fork-ci/b.test.sh ~5s"]; // 10 s on one slot
  const slow = await mainTaking(71, list);
  assert.equal(slow.exitCode, undefined, slow.out);
  assert.match(
    slow.out,
    /^::warning::the tests here took 71 s in all, ~10s expected from their weights \(1 at once\)/m,
  );
  assert.match(slow.summary, /\*\*Slow:\*\* the tests here took 71 s in all/);
  for (const seconds of [70, 20]) {
    const usual = await mainTaking(seconds, list);
    assert.equal(usual.exitCode, undefined, usual.out);
    assert.doesNotMatch(usual.out, /::warning::/);
    assert.doesNotMatch(usual.summary, /Slow/);
  }
});

test("FORK_CI_TEST_JOBS=1 runs one file at a time", () => {
  const { root, run } = runScript(
    { "fork-ci/a.test.sh": meets("a", "b"), "fork-ci/b.test.sh": meets("b", "a") },
    1,
  );
  try {
    assert.equal(run.status, 1, run.stdout + run.stderr);
    assert.match(run.stdout, /b did not run beside a/); // b started only after a ended
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a process a test leaves behind holding its output does not hang the run", () => {
  const { root, run } = runScript(
    { "fork-ci/left.test.sh": 'sleep 60 & echo $! > "$LEFT"; echo done\n' },
    1,
  );
  try {
    assert.equal(run.error, undefined, String(run.error)); // not killed at the 60 s limit: the run ended
    assert.equal(run.status, 0, run.stdout + run.stderr);
    assert.match(run.stdout, /::group::fork-ci\/left\.test\.sh[^\n]*\ndone\n/);
  } finally {
    try {
      process.kill(Number(fs.readFileSync(path.join(root, "left.pid"), "utf8")));
    } catch {}
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// A pipe into a reader that stops early (`grep -q`, `grep -m`, `head`) kills the writer with SIGPIPE
// whenever it has more to write; under `pipefail` that reads as a failed check, more often under load.
const earlyExitPipe =
  /(?<!\|)\|(?!\|)\s*!?\s*(?:grep\s+(?:-[A-Za-z]*[qm]|--quiet|--max-count)|head\b)/;
const earlyExitPipes = (text) =>
  text
    .split("\n")
    .map((line, index) => ({ line, number: index + 1 }))
    .filter(({ line }) => !/^\s*#/.test(line) && earlyExitPipe.test(line));

test("a pipe into grep -q, grep -m or head is found; reading the whole output first is not", () => {
  const found = earlyExitPipes(
    [
      'sed -n "/a/,$p" f | grep -q x',
      "! calls | grep -vqE y",
      "git show c | grep -qx z",
      "seq 9 | grep -m1 3",
      'sed p <<< "$s" | head -n 1',
      'grep -q x <<< "$(sed -n p f)"',
      "a || grep -q x f",
      "# a comment: `x | grep -q` stops early",
      '[[ -z "$(grep -E a f | grep -vE b)" ]]',
    ].join("\n"),
  ).map(({ number }) => number);
  assert.deepEqual(found, [1, 2, 3, 4, 5]);
});

test("no listed shell test pipes into a reader that stops early", () => {
  const root = path.resolve(__dirname, "../..");
  const shellTests = readList(fs.readFileSync(path.join(root, ".github/fork-ci-tests.txt"), "utf8"))
    .map(({ file }) => file)
    .filter((file) => file.endsWith(".sh") && fs.existsSync(path.join(root, file)));
  assert.ok(shellTests.length > 0);
  const found = shellTests.flatMap((file) =>
    earlyExitPipes(fs.readFileSync(path.join(root, file), "utf8")).map(
      ({ line, number }) => `${file}:${number}: ${line.trim()}`,
    ),
  );
  assert.deepEqual(found, []);
});

// The records of what each test ran (#664).

test("countTests reads unittest's summary from stderr, node's totals from stdout, else ok/FAIL lines", () => {
  const py = "fork-ci/a.test.py";
  assert.deepEqual(countTests(py, "", "..s\n----\nRan 3 tests in 0.01s\n\nOK (skipped=1)\n"), {
    format: "unittest",
    tests: 3,
    skipped: 1,
    failed: 0,
  });
  assert.deepEqual(
    countTests(py, "", "Ran 5 tests in 1.2s\n\nFAILED (failures=1, errors=2, skipped=1)\n"),
    { format: "unittest", tests: 5, skipped: 1, failed: 3 },
  );
  assert.deepEqual(countTests(py, "", "\nRan 0 tests in 0.000s\n\nNO TESTS RAN\n"), {
    format: "unittest",
    tests: 0,
    skipped: 0,
    failed: 0,
  });
  // A summary a test prints on stdout, or one not at the end of stderr, is not this run's.
  assert.equal(countTests(py, "Ran 3 tests in 0.01s\n\nOK\n", ""), null);
  assert.equal(
    countTests(py, "", "Ran 3 tests in 0.01s\n\nOK\nTraceback (most recent call last):\n"),
    null,
  );
  const js = "fork-ci/a.test.cjs";
  assert.deepEqual(
    countTests(js, "# tests 1\n# pass 1\n# tests 4\n# skipped 1\n# todo 1\n# fail 1\n", ""),
    {
      format: "node",
      tests: 4,
      skipped: 2,
      failed: 1,
    },
  );
  assert.deepEqual(countTests(js, "ℹ tests 2\nℹ pass 2\nℹ fail 0\n", ""), {
    format: "node",
    tests: 2,
    skipped: 0,
    failed: 0,
  });
  assert.deepEqual(countTests("fork-ci/a.test.sh", "ok one\nFAIL two\n", "ok three\n"), {
    format: "ok lines",
    tests: 3,
    skipped: 0,
    failed: 1,
  });
  assert.equal(countTests("fork-ci/a.test.sh", "all fine\n", ""), null);
  assert.equal(countTests(js, "nothing\n", ""), null);
});

test("declarations read run and skips lines and refuse every other fork-ci: line", () => {
  const good = declarations(
    [
      "# fork-ci: run as root with A_TESTS=1 B_TESTS=x/y only Launcher",
      "#fork-ci: run",
      "  // fork-ci: skips at most 3",
      'x = "# fork-ci: run as root"  # not a whole comment line',
    ].join("\n"),
    "fork-ci/a.test.py",
  );
  assert.deepEqual(good, {
    runs: [
      { root: true, vars: ["A_TESTS=1", "B_TESTS=x/y"], only: "Launcher" },
      { root: false, vars: [], only: null },
    ],
    skips: 3,
    problems: [],
  });
  const bad = {
    "# fork-ci: skips at most 0": /n at least 1/,
    "# fork-ci: skips 2": /skips at most <n>/,
    "# fork-ci: skips at most 1\n# fork-ci: skips at most 2": /once/,
    "# fork-ci: runs": /expected "fork-ci: run/,
    "# fork-ci: run with": /takes <NAME>_TESTS=value/,
    "# fork-ci: run with PATH=/tmp": /takes <NAME>_TESTS=value/,
    "# fork-ci: run with NODE_OPTIONS=--x": /takes <NAME>_TESTS=value/,
    "# fork-ci: run with A_TESTS=1 PATH=/tmp": /`PATH=\/tmp` is not understood/,
    "# fork-ci: run with A_TESTS=$(id)": /takes <NAME>_TESTS=value/,
    "# fork-ci: run with A_TESTS=1 A_TESTS=2": /a switch comes twice/,
    "# fork-ci: run only lower": /one test class/,
    "# fork-ci: run as root\n# fork-ci: run with A_TESTS=1": /two own runs of the same tests/,
    "# fork-ci: run as admin": /`as admin` is not understood/,
    "# fork-ci: run only Launcher as root": /`as root` is not understood/,
  };
  for (const [text, reason] of Object.entries(bad)) {
    const read = declarations(text, "fork-ci/a.test.py");
    assert.equal(read.problems.length, 1, text);
    assert.match(read.problems[0], reason, text);
    assert.match(read.problems[0], /^fork-ci\/a\.test\.py: "fork-ci:/, text);
  }
  assert.match(
    declarations("// fork-ci: run only Launcher", "fork-ci/a.test.cjs").problems[0],
    /in a Python test/,
  );
  assert.deepEqual(declarations("", "x"), { runs: [], skips: 0, problems: [] });
});

test("ownRuns: what a test declares, or one plain run of a fork test the list says fork-tests.yml runs", () => {
  const yml = ".github/workflows/fork-tests.yml";
  const plain = [{ root: false, vars: [], only: null }];
  assert.deepEqual(ownRuns({ file: "fork-ci/a.test.py", runBy: yml }, "").runs, plain);
  assert.deepEqual(ownRuns({ file: "fork-ci/a.test.py", runBy: yml }, null).runs, plain);
  assert.deepEqual(ownRuns({ file: "fork-ci/a.test.py", runBy: null }, "").runs, []);
  // Not a fork test (fork-tests.yml runs upstream's in the `test` job): no implicit run.
  assert.deepEqual(ownRuns({ file: "scripts/a.test.ts", runBy: yml }, "").runs, []);
  assert.deepEqual(
    ownRuns({ file: "fork-ci/a.test.py", runBy: yml }, "# fork-ci: run with A_TESTS=1").runs,
    [{ root: false, vars: ["A_TESTS=1"], only: null }],
  );
});

test("runKey and expectedKeys name one record per part run and one per own run", () => {
  assert.equal(runKey("fork-ci/a.test.py"), "fork-ci/a.test.py");
  assert.equal(
    runKey("fork-ci/a.test.py", { root: true, vars: [], only: null }),
    "fork-ci/a.test.py --run",
  );
  assert.equal(
    runKey("fork-ci/a.test.py", { root: true, vars: [], only: "L" }),
    "fork-ci/a.test.py --run only L",
  );
  const yml = ".github/workflows/fork-tests.yml";
  const texts = {
    "fork-ci/part.test.py": "# fork-ci: run as root only L\n",
    "fork-ci/heavy.test.py": "# fork-ci: run with H_TESTS=1\n",
    "fork-ci/own.test.py": "",
    "fork-ci/plain.test.sh": "",
  };
  const entries = [
    { file: "fork-ci/plain.test.sh", runBy: null },
    { file: "fork-ci/part.test.py", runBy: null },
    { file: "fork-ci/heavy.test.py", runBy: yml },
    { file: "fork-ci/own.test.py", runBy: yml },
    { file: "fork-ci/other.test.py", runBy: ".github/workflows/fork-other.yml" },
    { file: "fork-ci/pending.test.py", runBy: null },
  ];
  const present = [...Object.keys(texts), "fork-ci/other.test.py"];
  assert.deepEqual(
    expectedKeys(entries, present, (file) => texts[file] ?? ""),
    [
      "fork-ci/heavy.test.py --run",
      "fork-ci/own.test.py --run",
      "fork-ci/part.test.py",
      "fork-ci/part.test.py --run only L",
      "fork-ci/plain.test.sh",
    ],
  );
});

test("recordProblems: a failure, no count, nothing run, too many skipped, a failed test", () => {
  const row = (fields) => ({
    key: "k",
    status: 0,
    tests: 3,
    skipped: 0,
    failed: 0,
    allowed: 0,
    ...fields,
  });
  assert.deepEqual(recordProblems(row({})), []);
  assert.deepEqual(recordProblems(row({ skipped: 1, allowed: 1 })), []);
  const cases = [
    [{ status: 1 }, /^k failed \(exit 1\)$/],
    [{ status: null }, /^k failed \(exit null\)$/],
    [{ tests: null, skipped: null, failed: null }, /no test count/],
    [{ tests: 0 }, /^k ran no test \(0 found, 0 skipped\)$/],
    [{ tests: 2, skipped: 2, allowed: 2 }, /^k ran no test \(2 found, 2 skipped\)$/],
    [{ skipped: 1 }, /^k skipped 1 tests, 0 allowed/],
    [{ skipped: 2, allowed: 1 }, /^k skipped 2 tests, 1 allowed/],
    [{ failed: 1 }, /^k: 1 failed$/],
  ];
  for (const [fields, reason] of cases) {
    const problems = recordProblems(row(fields));
    assert.equal(problems.length, 1, JSON.stringify(fields));
    assert.match(problems[0], reason);
  }
});

test("record keeps what a run was and what its output counted", () => {
  const run = { root: true, vars: ["A_TESTS=1"], only: "L" };
  assert.deepEqual(
    record(
      "f --run only L",
      "fork-ci/f.test.py",
      run,
      { status: 0, seconds: "1.5", stdout: "", stderr: "Ran 2 tests in 0.1s\n\nOK\n" },
      0,
    ),
    {
      key: "f --run only L",
      test: "fork-ci/f.test.py",
      own: true,
      root: true,
      vars: ["A_TESTS=1"],
      only: "L",
      status: 0,
      seconds: 1.5,
      format: "unittest",
      tests: 2,
      skipped: 0,
      failed: 0,
      allowed: 0,
    },
  );
  const none = record("f", "fork-ci/f.test.sh", null, { status: 0, seconds: "0.1" }, 2);
  assert.deepEqual(
    [none.own, none.vars, none.tests, none.skipped, none.allowed],
    [false, [], null, null, 2],
  );
});

test("recordsTo: no folder, no records; a folder without the run, attempt and job throws", () => {
  assert.equal(recordsTo({}), null);
  assert.equal(recordsTo({ FORK_CI_TEST_RECORDS: "" }), null);
  const full = {
    FORK_CI_TEST_RECORDS: "/r",
    GITHUB_RUN_ID: "77",
    GITHUB_RUN_ATTEMPT: "2",
    GITHUB_JOB: "kit_tests",
  };
  assert.deepEqual(recordsTo(full), { dir: "/r", run_id: "77", run_attempt: 2, job: "kit_tests" });
  for (const name of ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB"]) {
    assert.throws(() => recordsTo({ ...full, [name]: undefined }), new RegExp(`${name} is not`));
    assert.throws(() => recordsTo({ ...full, [name]: "a b" }), new RegExp(`${name} is not`));
  }
});

test("writeRecords writes the run, attempt and job into the file and never over an earlier one", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-records-"));
  try {
    const to = { dir: path.join(dir, "new"), run_id: "77", run_attempt: 2, job: "lint" };
    const file = writeRecords(to, "run-.github/scripts/x.test.py", { results: [] });
    assert.equal(path.basename(file), "run-.github_scripts_x.test.py.json");
    assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8")), {
      v: 1,
      run_id: "77",
      run_attempt: 2,
      job: "lint",
      results: [],
    });
    // Runs again in the same job (a second call of the same part): refused, the first record kept.
    assert.throws(() => writeRecords(to, "run-.github/scripts/x.test.py", { results: [1] }), {
      code: "EEXIST",
    });
    assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8")).results, []);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("command: an own run as root goes through sudo -n env with its switches; the records folder reaches no test", () => {
  const env = { FORK_CI_TEST_RECORDS: "/r", GITHUB_ACTIONS: "true", KEEP: "1" };
  const part = command("fork-ci/a.test.sh", null, env);
  assert.deepEqual(part.argv, ["bash", "fork-ci/a.test.sh"]);
  assert.equal(part.env.FORK_CI_TEST_RECORDS, undefined);
  assert.equal(part.env.KEEP, "1");
  const root = command("fork-ci/a.test.py", { root: true, vars: ["A_TESTS=1"], only: "L" }, env);
  assert.deepEqual(root.argv, [
    "sudo",
    "-n",
    "env",
    "A_TESTS=1",
    "GITHUB_ACTIONS=true",
    "python3",
    "-I",
    "-B",
    "fork-ci/a.test.py",
    "-v",
    "L",
  ]);
  assert.equal(root.env.FORK_CI_TEST_RECORDS, undefined);
  const user = command("fork-ci/a.test.cjs", { root: false, vars: ["A_TESTS=x"], only: null }, env);
  assert.deepEqual(user.argv, ["node", "--test", "fork-ci/a.test.cjs"]);
  assert.equal(user.env.A_TESTS, "x");
  assert.equal(user.env.FORK_CI_TEST_RECORDS, undefined);
  assert.equal(env.A_TESTS, undefined); // the caller's environment is left alone
});

// The CI variables a recording call has (runScript passes on only these).
const recording = (root, job = "kit_tests", attempt = "1") => ({
  FORK_CI_TEST_RECORDS: path.join(root, "records"),
  GITHUB_RUN_ID: "4242",
  GITHUB_RUN_ATTEMPT: attempt,
  GITHUB_JOB: job,
});
const readRecords = (root) =>
  Object.fromEntries(
    fs
      .readdirSync(path.join(root, "records"))
      .map((name) => [name, JSON.parse(fs.readFileSync(path.join(root, "records", name), "utf8"))]),
  );
// runScript with the records folder in its own scratch tree.
function runRecording(tests, args, list, extra = {}) {
  const records = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-rec-"));
  const out = runScript(tests, 2, args, list, { ...recording(records), ...extra });
  return {
    ...out,
    records,
    cleanup: () =>
      [out.root, records].forEach((d) => fs.rmSync(d, { recursive: true, force: true })),
  };
}

const SKIPPING_PY =
  "import unittest\n" +
  "class T(unittest.TestCase):\n" +
  "    def test_a(self): pass\n" +
  "    @unittest.skip('no switch')\n" +
  "    def test_b(self): pass\n" +
  "unittest.main()\n";

test("a part writes one record of every test it ran, keyed by run, attempt, job and shard", () => {
  const tests = {
    "fork-ci/a.test.sh":
      'echo "ok a"\n[ -z "$FORK_CI_TEST_RECORDS" ] || { echo "FAIL records folder reached the test"; exit 1; }\n',
    "fork-ci/b.test.sh": 'echo "ok b"\n',
    "fork-ci/own.test.py": "# fork-ci: run with OWN_TESTS=1\n",
    ".github/workflows/fork-tests.yml":
      "      - run: node .github/scripts/fork-ci-tests.cjs --run fork-ci/own.test.py\n",
  };
  const list =
    "fork-ci/a.test.sh\nfork-ci/b.test.sh\nfork-ci/own.test.py run by .github/workflows/fork-tests.yml\n";
  const { run, records, cleanup } = runRecording(tests, ["fork-ci/"], list, {
    FORK_CI_TEST_SHARD: "2/2",
  });
  try {
    assert.equal(run.status, 0, run.stdout + run.stderr);
    const all = readRecords(records);
    assert.deepEqual(Object.keys(all), ["part-2of2-fork-ci_.json"]);
    const part = all["part-2of2-fork-ci_.json"];
    assert.deepEqual(
      [part.v, part.run_id, part.run_attempt, part.job, part.call],
      [1, "4242", 1, "kit_tests", { parts: ["fork-ci/"], shard: "2/2" }],
    );
    assert.deepEqual(part.all, [
      "fork-ci/a.test.sh",
      "fork-ci/b.test.sh",
      "fork-ci/own.test.py --run",
    ]);
    assert.deepEqual(part.owed, part.results.map((r) => r.key).toSorted());
    assert.equal(part.owed.length, 1); // two tests, two shards: this one ran one
    assert.deepEqual(
      part.results.map((r) => [r.status, r.format, r.tests, r.skipped, r.failed, r.own]),
      [[0, "ok lines", 1, 0, 0, false]],
    );
  } finally {
    cleanup();
  }
});

test("a part fails on a skip its test does not declare, passes on one it does, and on a fork-ci: line it cannot read", () => {
  for (const [head, status, reason] of [
    ["", 1, /fork-ci\/s\.test\.py skipped 1 tests, 0 allowed/],
    ["# fork-ci: skips at most 1\n", 0, null],
    ["# fork-ci: skips at most one\n", 1, /fork-ci\/s\.test\.py: "fork-ci: skips at most one"/],
  ]) {
    const { run, records, cleanup } = runRecording(
      { "fork-ci/s.test.py": head + SKIPPING_PY },
      [],
      "fork-ci/s.test.py",
    );
    try {
      assert.equal(run.status, status, run.stdout + run.stderr);
      if (reason) assert.match(run.stdout, reason);
      const [only] = Object.values(readRecords(records));
      assert.deepEqual([only.results[0].tests, only.results[0].skipped], [2, 1]);
    } finally {
      cleanup();
    }
  }
});

test("a part that ran a test whose output counts nothing fails", () => {
  const { run, cleanup } = runRecording(
    { "fork-ci/q.test.sh": "echo fine\n" },
    [],
    "fork-ci/q.test.sh",
  );
  try {
    assert.equal(run.status, 1, run.stdout + run.stderr);
    assert.match(run.stdout, /fork-ci\/q\.test\.sh: its output shows no test count/);
  } finally {
    cleanup();
  }
});

test("without the records folder a part writes nothing and does not judge counts; with it but no run id it stops", () => {
  const quiet = runScript({ "fork-ci/q.test.sh": "echo fine\n" }, 1);
  try {
    assert.equal(quiet.run.status, 0, quiet.run.stdout + quiet.run.stderr);
  } finally {
    fs.rmSync(quiet.root, { recursive: true, force: true });
  }
  const { run, records, cleanup } = runRecording(
    { "fork-ci/q.test.sh": 'echo "ok q"\n' },
    [],
    "fork-ci/q.test.sh",
    {
      GITHUB_RUN_ID: undefined,
    },
  );
  try {
    assert.notEqual(run.status, 0);
    assert.match(run.stderr, /GITHUB_RUN_ID is not: the records cannot be keyed/);
    assert.equal(fs.existsSync(path.join(records, "records")), false);
  } finally {
    cleanup();
  }
});

test("the same part run again in the same job does not replace its record: the second call fails", () => {
  const tests = { "fork-ci/a.test.sh": 'echo "ok a"\n' };
  const { root, run, records, cleanup } = runRecording(tests, [], "fork-ci/a.test.sh");
  try {
    assert.equal(run.status, 0, run.stdout + run.stderr);
    const first = readRecords(records);
    const again = childProcess.spawnSync("node", [".github/scripts/fork-ci-tests.cjs"], {
      cwd: root,
      env: {
        ...process.env,
        ...recording(records),
        FORK_CI_TEST_SHARD: "",
        GITHUB_STEP_SUMMARY: "",
      },
      encoding: "utf8",
    });
    assert.notEqual(again.status, 0, again.stdout);
    assert.match(again.stderr, /EEXIST/);
    assert.deepEqual(readRecords(records), first);
  } finally {
    cleanup();
  }
});

const SWITCHED_SH =
  '[ "$OWN_TESTS" = 1 ] || { echo "FAIL OWN_TESTS not set"; exit 1; }\n' +
  '[ -z "$FORK_CI_TEST_RECORDS" ] || { echo "FAIL records folder reached the test"; exit 1; }\n' +
  'echo "ok switched"\n';

test("--run runs a test's declared own runs with their switches and writes their record", () => {
  const tests = { "fork-ci/own.test.sh": "# fork-ci: run with OWN_TESTS=1\n" + SWITCHED_SH };
  const list = "fork-ci/own.test.sh run by .github/workflows/fork-tests.yml\n";
  const { run, records, cleanup } = runRecording(tests, ["--run", "fork-ci/own.test.sh"], list, {
    GITHUB_JOB: "workflows",
  });
  try {
    assert.equal(run.status, 0, run.stdout + run.stderr);
    assert.match(run.stdout, /::group::fork-ci\/own\.test\.sh --run with OWN_TESTS=1 \(/);
    const all = readRecords(records);
    assert.deepEqual(Object.keys(all), ["run-fork-ci_own.test.sh.json"]);
    const own = all["run-fork-ci_own.test.sh.json"];
    assert.deepEqual(
      [own.job, own.all, own.call, own.owed],
      ["workflows", null, { run: "fork-ci/own.test.sh" }, ["fork-ci/own.test.sh --run"]],
    );
    assert.deepEqual(
      own.results.map((r) => [r.key, r.own, r.vars, r.status, r.tests, r.skipped, r.allowed]),
      [["fork-ci/own.test.sh --run", true, ["OWN_TESTS=1"], 0, 1, 0, 0]],
    );
  } finally {
    cleanup();
  }
});

test("--run of a listed fork test fork-tests.yml runs, with no run line, is one plain run", () => {
  const tests = { "fork-ci/plain.test.sh": 'echo "ok plain"\n' };
  const { run, records, cleanup } = runRecording(
    tests,
    ["--run", "fork-ci/plain.test.sh"],
    "fork-ci/plain.test.sh run by .github/workflows/fork-tests.yml",
  );
  try {
    assert.equal(run.status, 0, run.stdout + run.stderr);
    assert.deepEqual(Object.values(readRecords(records))[0].owed, ["fork-ci/plain.test.sh --run"]);
  } finally {
    cleanup();
  }
});

test("--run refuses what it may not run, and an own run may skip no test", () => {
  const skipping = "# fork-ci: run with OWN_TESTS=1\n# fork-ci: skips at most 1\n" + SKIPPING_PY;
  const tests = {
    "fork-ci/own.test.sh": "# fork-ci: run with OWN_TESTS=1\n" + SWITCHED_SH,
    "fork-ci/part.test.sh": 'echo "ok part"\n',
    "fork-ci/other.test.sh": '# fork-ci: run\necho "ok other"\n',
    "fork-ci/bad.test.sh": '# fork-ci: run with PATH=/x\necho "ok bad"\n',
    "fork-ci/skip.test.py": skipping,
    "fork-ci/unlisted.test.sh": "# fork-ci: run\n",
  };
  const list = [
    "fork-ci/own.test.sh run by .github/workflows/fork-tests.yml",
    "fork-ci/part.test.sh",
    "fork-ci/other.test.sh run by .github/workflows/fork-other.yml",
    "fork-ci/bad.test.sh run by .github/workflows/fork-tests.yml",
    "fork-ci/skip.test.py",
    "fork-ci/gone.test.sh",
  ].join("\n");
  const refused = [
    [[], /--run takes one listed test/],
    [["fork-ci/own.test.sh", "extra"], /--run takes one listed test/],
    [
      ["fork-ci/unlisted.test.sh"],
      /fork-ci\/unlisted\.test\.sh is not in \.github\/fork-ci-tests\.txt/,
    ],
    [["fork-ci/gone.test.sh"], /fork-ci\/gone\.test\.sh is not present/],
    [
      ["fork-ci/other.test.sh"],
      /says \.github\/workflows\/fork-other\.yml runs fork-ci\/other\.test\.sh/,
    ],
    [["fork-ci/part.test.sh"], /fork-ci\/part\.test\.sh declares no own run/],
    [["fork-ci/bad.test.sh"], /takes <NAME>_TESTS=value/],
  ];
  for (const [args, reason] of refused) {
    const { run, records, cleanup } = runRecording(tests, ["--run", ...args], list);
    try {
      assert.equal(run.status, 1, args.join(" ") + run.stdout + run.stderr);
      assert.match(run.stdout, reason, args.join(" "));
      assert.equal(fs.existsSync(path.join(records, "records")), false, args.join(" "));
    } finally {
      cleanup();
    }
  }
  const { run, records, cleanup } = runRecording(tests, ["--run", "fork-ci/skip.test.py"], list);
  try {
    assert.equal(run.status, 1, run.stdout + run.stderr);
    assert.match(run.stdout, /fork-ci\/skip\.test\.py --run skipped 1 tests, 0 allowed/);
    assert.equal(Object.values(readRecords(records))[0].results[0].skipped, 1); // recorded all the same
  } finally {
    cleanup();
  }
});

test("--run of a failing own run fails, and its record says so", () => {
  const tests = { "fork-ci/own.test.sh": '# fork-ci: run\necho "FAIL broken"; exit 4\n' };
  const { run, records, cleanup } = runRecording(
    tests,
    ["--run", "fork-ci/own.test.sh"],
    "fork-ci/own.test.sh run by .github/workflows/fork-tests.yml",
  );
  try {
    assert.equal(run.status, 1, run.stdout + run.stderr);
    assert.match(run.stdout, /fork-ci\/own\.test\.sh --run failed \(exit 4\)/);
    assert.equal(Object.values(readRecords(records))[0].results[0].status, 4);
  } finally {
    cleanup();
  }
});
