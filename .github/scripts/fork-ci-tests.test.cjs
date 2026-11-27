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
  readCounts,
  elsewhereAllowed,
  RUN_ELSEWHERE,
  recordProblems,
  record,
  recordsTo,
  writeRecords,
  command,
  NODE_COUNTS,
  fileLimit,
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
  // Each `run by` but fork-tests.yml as RUN_ELSEWHERE would name it (else refused: the test below).
  const elsewhere = Object.fromEntries(entries.map((e) => [e.file, e.runBy]));
  const result = plan(
    entries,
    entries.map((e) => e.file),
    never,
    (file) => files[file] ?? null,
    elsewhere,
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
    Object.fromEntries(entries.map((e) => [e.file, "scripts/smoke.ts"])),
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
    readList(
      "# head\n\na.test.sh  # from #2\n  b.test.py run by c.yml unittest\nd.test.cjs ~120s\n",
    ),
    [
      { file: "a.test.sh", runBy: null, weight: null, format: null },
      { file: "b.test.py", runBy: "c.yml", weight: null, format: "unittest" },
      { file: "d.test.cjs", runBy: null, weight: 120, format: null },
    ],
  );
  // A Python test says its format, "unittest" or "ok lines", after any other word.
  assert.deepEqual(
    readList(
      "e.test.py ok lines\nf.test.py ~40s ok lines\ng.test.py run by c.yml ok lines\nh.test.py ~40s unittest\n" +
        "i.test.py unittest\n",
    ).map((e) => [e.file, e.weight, e.runBy, e.format]),
    [
      ["e.test.py", null, null, "ok lines"],
      ["f.test.py", 40, null, "ok lines"],
      ["g.test.py", null, "c.yml", "ok lines"],
      ["h.test.py", 40, null, "unittest"],
      ["i.test.py", null, null, "unittest"],
    ],
  );
  for (const [other, word] of [
    ["a.test.sh ok lines", "ok lines"],
    ["a.test.cjs ~20s ok lines", "ok lines"],
    ["a.test.sh unittest", "unittest"],
    ["a.test.mjs run by c.yml unittest", "unittest"],
  ])
    assert.throws(
      () => readList(`${other}\n`),
      new RegExp(`only a Python test says "${word}"`),
      other,
    );
  // A Python test without its format is refused, whatever else its line says: none is read as unittest.
  for (const bare of ["a.test.py", "a.test.py ~40s", "a.test.py run by c.yml"])
    assert.throws(
      () => readList(`b.test.py unittest\n${bare}\n`),
      /a Python test says its format at the end of its line: "unittest" .* or "ok lines"/,
      bare,
    );
  // One spelling of a runner's path, so `./a/b` and `a//b` compare equal to `a/b` wherever the list is read.
  assert.deepEqual(
    readList(
      "e.test.py run by ./.github/workflows/x.yml unittest\nf.test.py run by .github//workflows/x.yml ok lines\n",
    ).map((e) => e.runBy),
    [".github/workflows/x.yml", ".github/workflows/x.yml"],
  );
  for (const bad of [
    "a.test.sh run c.yml",
    "a.test.sh ~0s",
    "a.test.sh ~12",
    "a.test.sh ~1.5s",
    "a.test.sh ~12s run by c.yml",
    "a.test.py ok lines ~12s",
    "a.test.py oklines",
    "a.test.py ok lines ok lines",
    "a.test.py unittest ~12s",
    "a.test.py unittest ok lines",
    "a.test.py ok lines unittest",
    "a.test.py unittests",
    "a.test.py Unittest",
  ])
    assert.throws(() => readList(`${bad}\n`), /cannot read/, bad);
  assert.throws(
    () => readList("a.test.py unittest ~12s\n"),
    /"; a Python test's line then ends in its format: "unittest" or "ok lines"\)$/,
  );
  // One line per test: a second line for a file is refused, whatever it says.
  for (const twice of [
    "a.test.sh\nb.test.sh\na.test.sh\n",
    "a.test.py unittest\na.test.py ok lines\n",
    "a.test.py ~40s unittest\na.test.py run by c.yml unittest\n",
  ])
    assert.throws(
      () => readList(twice),
      /"a\.test\.(sh|py)" is listed twice \(one line per test\)/,
      twice,
    );
});

// A Python test the runner never runs (upstream's, which a plain step runs) leaves no record, so the runner never reads
// its format word: its word is checked here against its source instead.
test("a listed Python test the runner does not judge says the format its source has", () => {
  const root = path.resolve(__dirname, "../..");
  const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
  const entries = readList(read(".github/fork-ci-tests.txt"));
  const present = entries.map((e) => e.file).filter((file) => fs.existsSync(path.join(root, file)));
  const keys = expectedKeys(entries, present, read);
  const unjudged = entries.filter(
    (e) =>
      e.format &&
      present.includes(e.file) &&
      !keys.some((key) => key === e.file || key.startsWith(`${e.file} --run`)),
  );
  assert.ok(unjudged.some((e) => e.file === ".github/scripts/stage-preview-bundle.test.py"));
  for (const { file, format } of unjudged) {
    const text = read(file);
    const unittest = /\bunittest\.main\(|\bunittest\.TestCase\b|\bTextTestRunner\b/.test(text);
    const okLines = /all passed: /.test(text);
    assert.equal(unittest !== okLines, true, `${file}: its source shows no single format`);
    assert.equal(
      format,
      unittest ? "unittest" : "ok lines",
      `${file}: its source is ${unittest ? "unittest" : "ok lines"}`,
    );
  }
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
  write(
    ".github/scripts/fork-ci-unittest.py",
    fs.readFileSync(path.join(__dirname, "fork-ci-unittest.py")),
  );
  // require.resolve with a literal also lets knip trace the reporter, which node --test loads by path.
  write(NODE_COUNTS, fs.readFileSync(require.resolve("./fork-ci-node-counts.cjs")));
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
    "FORK_CI_TEST_COUNTS",
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

test("a test file's time limit: FORK_CI_TEST_LIMIT, else 3 times its weight and at least 240 s (#1070)", () => {
  assert.equal(fileLimit(null, {}), 240);
  assert.equal(fileLimit(30, {}), 240);
  assert.equal(fileLimit(120, {}), 360);
  assert.equal(fileLimit(120, { FORK_CI_TEST_LIMIT: "" }), 360);
  assert.equal(fileLimit(120, { FORK_CI_TEST_LIMIT: "2" }), 2);
  assert.equal(fileLimit(null, { FORK_CI_TEST_LIMIT: "0.5" }), 0.5);
  for (const bad of ["0", "-1", "x", "1e3", " 5", "Infinity"])
    assert.throws(() => fileLimit(10, { FORK_CI_TEST_LIMIT: bad }), /FORK_CI_TEST_LIMIT/, bad);
  // A longer setTimeout fires at once (every file would fail as timed out): refused, saying the maximum.
  assert.equal(fileLimit(null, { FORK_CI_TEST_LIMIT: "2147483" }), 2147483);
  assert.throws(() => fileLimit(10, { FORK_CI_TEST_LIMIT: "2147484" }), /at most 2147483 s/);
  assert.throws(() => fileLimit(10, { FORK_CI_TEST_LIMIT: "3000000" }), /at most 2147483 s/);
  assert.equal(fileLimit(99999, {}), 299997); // the largest weight the list takes fits
});

// A test that never ends fails at its time limit, quickly, with its file named and its last output lines shown
// outside the group, and the run goes on to the next file. The 60 s limit of runScript() is the hang this replaces.
test("a test file that never ends is stopped at its time limit and fails, naming the file (#1070)", () => {
  const { root, run } = runScript(
    {
      // Ignores SIGTERM too: SIGKILL follows.
      "fork-ci/hang.test.sh": "trap '' TERM; echo hang-started; sleep 30 & wait; sleep 30\n",
      "fork-ci/after.test.sh": "echo after-ran\n",
    },
    1,
    [],
    "fork-ci/hang.test.sh\nfork-ci/after.test.sh",
    { FORK_CI_TEST_LIMIT: "1" },
  );
  try {
    assert.equal(run.error, undefined, String(run.error)); // not killed at the 60 s limit
    assert.equal(run.status, 1, run.stdout + run.stderr);
    assert.match(
      run.stdout,
      /::error file=fork-ci\/hang\.test\.sh::fork-ci\/hang\.test\.sh timed out after 1 s/,
    );
    assert.match(
      run.stdout,
      /Last \d+ line\(s\) of fork-ci\/hang\.test\.sh:\n::stop-commands::[\w-]+\nhang-started\n/,
    );
    assert.match(run.stdout, /did not end within 1 s: stopping it/);
    assert.match(run.stdout, /::group::fork-ci\/after\.test\.sh[^\n]*\nafter-ran\n/);
    assert.doesNotMatch(run.stdout, /::error file=fork-ci\/after\.test\.sh/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// The case that hung the runner: a Python test file whose failing test leaves a thread that never ends (its cleanup never ran), so
// the file does not end after unittest's summary. It fails at its limit with what it printed (not lost in a buffer)
// and where each thread was (fork-ci-unittest.py's stack dump on SIGTERM).
test("a failing Python test file that never ends shows its output and its threads' stacks (#1070)", () => {
  const py = [
    "import threading, unittest",
    "stop = threading.Event()",
    "def waits_for_ever():",
    "    stop.wait()",
    "class T(unittest.TestCase):",
    "    def test_leaves_a_thread(self):",
    "        threading.Thread(target=waits_for_ever).start()  # not a daemon: the file cannot end",
    "        print('printed-before-the-hang')",
    "        self.fail('this test fails before its cleanup would stop the thread')",
    "unittest.main()",
    "",
  ].join("\n");
  const { root, run } = runScript(
    { "fork-ci/hang.test.py": py },
    1,
    [],
    "fork-ci/hang.test.py unittest",
    { FORK_CI_TEST_LIMIT: "2" },
  );
  try {
    assert.equal(run.error, undefined, String(run.error));
    assert.equal(run.status, 1, run.stdout + run.stderr);
    assert.match(run.stdout, /fork-ci\/hang\.test\.py timed out after 2 s/);
    assert.match(run.stdout, /printed-before-the-hang/);
    assert.match(run.stdout, /FAILED \(failures=1\)/);
    assert.match(run.stdout, /in waits_for_ever/); // the stack of the thread that kept it running
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// The common hang: the test's main thread waits (a read, communicate()) after a print. The line is in the output
// when the test is killed, not lost in stdout's buffer.
test("a Python test hung in its main thread keeps the lines it printed (#1070)", () => {
  const py = [
    "import threading, unittest",
    "class T(unittest.TestCase):",
    "    def test_waits(self):",
    "        print('printed-before-main-hang')",
    "        threading.Event().wait()",
    "unittest.main()",
    "",
  ].join("\n");
  const { root, run } = runScript(
    { "fork-ci/main-hang.test.py": py },
    1,
    [],
    "fork-ci/main-hang.test.py unittest",
    { FORK_CI_TEST_LIMIT: "2" },
  );
  try {
    assert.equal(run.status, 1, run.stdout + run.stderr);
    assert.match(run.stdout, /fork-ci\/main-hang\.test\.py timed out after 2 s/);
    // In the file's group, before the stop (the stack dump after it fills the 20-line tail).
    assert.match(
      run.stdout,
      /::group::fork-ci\/main-hang\.test\.py[^\n]*\nprinted-before-main-hang\n[\s\S]*did not end within 2 s/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// A process the test started (a background job, a server) is in the test's process group and stops with it.
test("a test stopped at its limit takes the processes it started with it (#1070)", () => {
  const { root, run } = runScript(
    {
      "fork-ci/bg.test.sh": 'sleep 300 & echo $! > "$MARKS/bg.pid"; echo bg-started; wait\n',
    },
    1,
    [],
    "fork-ci/bg.test.sh",
    { FORK_CI_TEST_LIMIT: "1" },
  );
  const pid = Number(fs.readFileSync(path.join(root, "bg.pid"), "utf8"));
  const alive = () => {
    try {
      return !/^\d+ \(.*\) Z /.test(fs.readFileSync(`/proc/${pid}/stat`, "utf8"));
    } catch {
      return false;
    }
  };
  try {
    assert.equal(run.status, 1, run.stdout + run.stderr);
    assert.match(run.stdout, /fork-ci\/bg\.test\.sh timed out after 1 s/);
    assert.equal(alive(), false, "the background sleep outlived its test");
  } finally {
    try {
      process.kill(pid);
    } catch {}
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// The failure tail repeats the test's last lines as text: a workflow command the test printed (an `::error` line)
// is an annotation once, from the group, not again from the tail.
test("a failed test's tail repeats its own ::error line as text, not as a second annotation (#1070)", () => {
  const { root, run } = runScript(
    {
      "fork-ci/err.test.sh":
        'echo "::error::own-error-line"; echo "$(printf "x%.0s" $(seq 600))"; exit 3\n',
    },
    1,
  );
  try {
    assert.equal(run.status, 1, run.stdout + run.stderr);
    assert.match(run.stdout, /fork-ci\/err\.test\.sh failed \(exit 3\)/);
    const tail = run.stdout.slice(run.stdout.indexOf("Last "));
    assert.match(
      tail,
      /^Last \d+ line\(s\) of fork-ci\/err\.test\.sh:\n::stop-commands::(fork-ci-tail-[\w-]+)\n::error::own-error-line\nx{500}\.\.\.\n::\1::\n/,
    );
  } finally {
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

// The records of what each test ran, which fork-ci-result checks.

test("countTests reads fork-ci-unittest.py's plan and count lines, node's reporter line, else ok lines", () => {
  const py = "fork-ci/a.test.py";
  // The list's format for a Python test (readList() refuses one without).
  const ut = { format: "unittest" };
  const line = (tests, skipped = [], failed = 0) => ({ tests, skipped, failed });
  const plan = (n) => ({ plan: n });
  // The shim's last line, when the file ended: the tests its source defines that no run planned.
  const end = (...unloaded) => ({ unloaded });
  assert.deepEqual(countTests(py, "", "", [plan(3), line(3, ["T.test_b"]), end()], ut), {
    format: "unittest",
    tests: 3,
    skipped: 1,
    names: ["T.test_b"],
    unloaded: [],
    failed: 0,
    note: null,
  });
  // Two unittest runs in one test (two TextTestRunner runs, one after the other or one inside the other): summed.
  for (const lines of [
    [plan(2), line(2, [], 1), plan(4), line(4, ["U.test_x", "U.test_y"], 2), end()],
    [plan(4), plan(2), line(2, [], 1), line(4, ["U.test_x", "U.test_y"], 2), end()],
  ])
    assert.deepEqual(countTests(py, "", "", lines, ut), {
      format: "unittest",
      tests: 6,
      skipped: 2,
      names: ["U.test_x", "U.test_y"],
      unloaded: [],
      failed: 3,
      note: null,
    });
  // A test its source defines that no run planned (a class only under a condition, a load_tests that drops it) is a
  // skip by its name; an own run of one class counts only that class's.
  assert.deepEqual(
    countTests(py, "", "", [plan(2), line(2), end("Tool.test_a", "Tool.test_b")], ut),
    {
      format: "unittest",
      tests: 4,
      skipped: 2,
      names: ["Tool.test_a", "Tool.test_b"],
      unloaded: ["Tool.test_a", "Tool.test_b"],
      failed: 0,
      note: null,
    },
  );
  const only = countTests(py, "", "", [plan(1), line(1), end("Tool.test_a", "L.test_b")], {
    ...ut,
    only: "L",
  });
  assert.deepEqual([only.tests, only.names, only.unloaded], [2, ["L.test_b"], ["L.test_b"]]);
  // A hard stop before the first plan (at import, while it loads): no line at all, whatever its stdout says (a
  // child's ok lines and end line). Not an ok-lines test unless the list says so.
  const hard = countTests(py, "ok child\nall passed: 1 ok, 0 skipped\n", "", [], ut);
  assert.deepEqual([hard.format, hard.tests], ["unittest", null]);
  assert.match(hard.note, /wrote no line: it stopped hard before its first unittest run/);
  // A Python test with no format from the list is never read as either: nothing counts, whatever its lines.
  for (const lines of [[], [plan(1), line(1), end()], [end()]])
    for (const how of [{}, { format: null }, { format: "ok" }, { only: "T" }]) {
      const counted = countTests(py, "ok child\nall passed: 1 ok, 0 skipped\n", "", lines, how);
      assert.deepEqual([counted.format, counted.tests], [null, null], JSON.stringify([lines, how]));
      assert.match(counted.note, /declares no format for it \("unittest" or "ok lines"\)/);
    }
  // Its runs ended, then a hard stop before the file's end; or the file ended without a run (sys.exit at import).
  for (const [lines, note] of [
    [[plan(2), line(2)], /runs ended, but the file never reached its end/],
    [[end()], /started no unittest run/],
    [[end("T.test_a")], /started no unittest run/],
  ]) {
    const counted = countTests(py, "ok child\nall passed: 1 ok, 0 skipped\n", "", lines, ut);
    assert.deepEqual([counted.format, counted.tests], ["unittest", null], JSON.stringify(lines));
    assert.match(counted.note, note);
  }
  // A hard exit (os._exit, a signal) after the plan: nothing counts, whatever ok lines its stdout has.
  for (const lines of [
    [plan(4)],
    [plan(2), line(2), plan(3)],
    [plan(4), plan(2), line(2)],
    [plan(4), end()],
  ]) {
    const counted = countTests(py, "ok child\nall passed: 1 ok, 0 skipped\n", "", lines, ut);
    assert.equal(counted.format, "unittest");
    assert.equal(counted.tests, null);
    assert.match(counted.note, /started but never ended/);
  }
  // Stopped half-way through writing a line, counts without their plan, a count not its plan's, or another shape.
  assert.equal(countTests(py, "", "", [plan(2), line(2), null], ut), null);
  assert.equal(countTests(py, "", "", [line(2)], ut), null);
  assert.equal(countTests(py, "", "", [plan(3), line(2)], ut), null);
  assert.equal(countTests(py, "", "", [plan(2), { tests: 2, skipped: 1, failed: 0 }], ut), null);
  assert.equal(countTests(py, "", "", [{ plan: 2, tests: 2 }, line(2)], ut), null);
  assert.equal(countTests(py, "", "", [plan(2), line(2), end(), end()], ut), null);
  assert.equal(countTests(py, "", "", [end(), plan(2), line(2)], ut), null);
  assert.equal(countTests(py, "", "", [plan(2), line(2), { unloaded: [1] }], ut), null);
  assert.equal(countTests(py, "", "", [plan(2), line(2), { unloaded: [], more: 1 }], ut), null);
  // unittest's own summary is never read (a class skip counts once there; stderr may go on after it).
  assert.equal(countTests(py, "", "Ran 3 tests in 0.01s\n\nOK (skipped=1)\n", [], ut).tests, null);
  const js = "fork-ci/a.test.cjs";
  const node = (tests, more = {}) => ({
    tests,
    skipped: 0,
    failed: 0,
    started: tests,
    stopped: 0,
    file_test: false,
    ...more,
  });
  assert.deepEqual(countTests(js, "", "", [node(4, { skipped: 2, failed: 1 })]), {
    format: "node",
    tests: 4,
    skipped: 2,
    names: [],
    unloaded: [],
    failed: 1,
    note: null,
  });
  // node's totals on stdout are never read: an empty file, a top-level return or an early exit says "tests 1, pass 1".
  assert.equal(countTests(js, "# tests 1\n# pass 1\nℹ tests 2\nℹ pass 2\n", "", []), null);
  for (const [n, note] of [
    [node(1, { file_test: true }), /only the file itself/],
    [node(0, { file_test: true, started: 0 }), /only the file itself/],
    [node(40, { started: 60, stopped: 20 }), /60 tests started and 40 ended/],
    [node(2, { started: 3 }), /3 tests started and 2 ended/],
    [node(2, { stopped: 1 }), /2 tests started and 2 ended/],
  ]) {
    const counted = countTests(js, "", "", [n]);
    assert.equal(counted.tests, null, JSON.stringify(n));
    assert.match(counted.note, note);
  }
  // Two lines (two reporters in one file?), a line cut off, or a line of another shape: no count at all.
  assert.equal(countTests(js, "", "", [node(1), node(1)]), null);
  assert.equal(countTests(js, "", "", [null]), null);
  assert.equal(countTests(js, "", "", [{ ...node(1), file_test: 0 }]), null);
  assert.equal(countTests(js, "", "", [{ ...node(1), started: undefined }]), null);
  const sh = "fork-ci/a.test.sh";
  // A failure counts without the end line (the test failed: red either way); stderr's lines never count.
  assert.deepEqual(countTests(sh, "ok one\nFAIL two\n", "ok three\n"), {
    format: "ok lines",
    tests: 2,
    skipped: 0,
    names: [],
    unloaded: [],
    failed: 1,
    note: null,
  });
  assert.deepEqual(
    countTests(sh, "ok one\nskip net no network here\nall passed: 1 ok, 1 skipped\n", ""),
    {
      format: "ok lines",
      tests: 2,
      skipped: 1,
      names: ["net"],
      unloaded: [],
      failed: 0,
      note: null,
    },
  );
  // A Python test the list says prints ok lines is read by them: its shim's only line is its end, naming no test.
  const okLines = { format: "ok lines" };
  assert.deepEqual(countTests(py, "ok one\nall passed: 1 ok, 0 skipped\n", "", [end()], okLines), {
    format: "ok lines",
    tests: 1,
    skipped: 0,
    names: [],
    unloaded: [],
    failed: 0,
    note: null,
  });
  for (const [lines, note] of [
    [[], /stopped before its end \(a hard exit/],
    [[plan(1), line(1), end()], /says it prints ok lines, but it ran unittest/],
    [[plan(1), end()], /says it prints ok lines, but it ran unittest/],
    [[end("T.test_a")], /defines unittest tests no run loaded \(T\.test_a\)/],
  ]) {
    const counted = countTests(py, "ok one\nall passed: 1 ok, 0 skipped\n", "", lines, okLines);
    assert.deepEqual([counted.format, counted.tests], ["ok lines", null], JSON.stringify(lines));
    assert.match(counted.note, note);
  }
  // Stopped before its end (an `exit 0` half-way), its end line not last, or a count that is not its lines'.
  for (const [out, note] of [
    ["ok one\n", /does not end in "all passed: <n> ok, <n> skipped", so it may have stopped/],
    ["ok one\nall passed: 1 ok, 0 skipped\nok late\n", /does not end in/],
    [
      "ok one\nok child\nall passed: 1 ok, 0 skipped\n",
      /says 1 ok, 0 skipped, but its stdout has 2 ok and 0 skip/,
    ],
    ["ok one\nskip a\nall passed: 1 ok, 0 skipped\n", /but its stdout has 1 ok and 1 skip/],
  ]) {
    const counted = countTests(sh, out, "");
    assert.equal(counted.tests, null, out);
    assert.match(counted.note, note, out);
  }
  assert.equal(countTests(sh, "all fine\n", ""), null);
  assert.equal(countTests(js, "ok one\nall passed: 1 ok, 0 skipped\n", ""), null);
});

test("readCounts: one object a line; a missing or empty file none; a line cut off is null", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-counts-test-"));
  try {
    const file = path.join(dir, "counts.jsonl");
    assert.deepEqual(readCounts(file), []);
    fs.writeFileSync(file, "");
    assert.deepEqual(readCounts(file), []);
    fs.writeFileSync(file, '{"tests": 2, "skipped": [], "failed": 0}\n{"tests": 3, "ski');
    assert.deepEqual(readCounts(file), [{ tests: 2, skipped: [], failed: 0 }, null]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("declarations read run and may-skip lines and refuse every other fork-ci: line", () => {
  const good = declarations(
    [
      "# fork-ci: run as root with A_TESTS=1 B_TESTS=x/y only Launcher",
      "#fork-ci: run",
      "  // fork-ci: may skip T.test_b",
      "# fork-ci: may skip net",
      "# fork-ci: may skip helper_mod.T.test_b",
      "# fork-ci: run only Other.test_one",
      'x = "# fork-ci: run as root"  # not a whole comment line',
    ].join("\n"),
    "fork-ci/a.test.py",
  );
  assert.deepEqual(good, {
    runs: [
      { root: true, vars: ["A_TESTS=1", "B_TESTS=x/y"], only: "Launcher" },
      { root: false, vars: [], only: null },
      { root: false, vars: [], only: "Other.test_one" },
    ],
    skips: ["T.test_b", "net", "helper_mod.T.test_b"],
    problems: [],
  });
  const bad = {
    // The old budget form: a number names no test, so it is refused (its line must name each test).
    "# fork-ci: skips at most 1": /expected "fork-ci: may skip <test>"/,
    "# fork-ci: may skip": /expected "fork-ci: may skip <test>"/,
    "# fork-ci: may skip T.a T.b": /expected "fork-ci: may skip <test>"/,
    "# fork-ci: may skip m.T.": /expected "fork-ci: may skip <test>"/,
    "# fork-ci: may skip m..T.a": /expected "fork-ci: may skip <test>"/,
    "# fork-ci: may skip 9": /expected "fork-ci: may skip <test>"/,
    "# fork-ci: may skip T.a\n# fork-ci: may skip T.a": /T\.a is named twice/,
    "# fork-ci: runs": /expected "fork-ci: run/,
    "# fork-ci: run with": /takes <NAME>_TESTS=value/,
    "# fork-ci: run with PATH=/tmp": /takes <NAME>_TESTS=value/,
    "# fork-ci: run with NODE_OPTIONS=--x": /takes <NAME>_TESTS=value/,
    "# fork-ci: run with A_TESTS=1 PATH=/tmp": /`PATH=\/tmp` is not understood/,
    "# fork-ci: run with A_TESTS=$(id)": /takes <NAME>_TESTS=value/,
    "# fork-ci: run with A_TESTS=1 A_TESTS=2": /a switch comes twice/,
    "# fork-ci: run only lower": /one test class/,
    "# fork-ci: run only T.test_a.more": /one test class or one of its tests/,
    "# fork-ci: run only T\n# fork-ci: run with A_TESTS=1 only T.test_a":
      /two own runs of the same tests/,
    "# fork-ci: run only T.test_a\n# fork-ci: run as root only T": /two own runs of the same tests/,
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
  assert.deepEqual(declarations("", "x"), { runs: [], skips: [], problems: [] });
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
    // A test another runner runs: only as RUN_ELSEWHERE says; any other `run by` (a fork test or not) owes its record.
    { file: "fork-ci/other.test.py", runBy: ".github/workflows/fork-other.yml" },
    {
      file: ".github/scripts/fork-secret-scan.test.sh",
      runBy: ".github/workflows/fork-secret-scan.yml",
    },
    { file: "scripts/upstream.test.ts", runBy: "scripts/smoke.ts" },
    { file: ".github/scripts/relay-state-output.test.cjs", runBy: "scripts/release-smoke.ts" },
    { file: "fork-ci/pending.test.py", runBy: null },
  ];
  const present = [
    ...Object.keys(texts),
    "fork-ci/other.test.py",
    ".github/scripts/fork-secret-scan.test.sh",
    "scripts/upstream.test.ts",
    ".github/scripts/relay-state-output.test.cjs",
  ];
  assert.deepEqual(
    expectedKeys(entries, present, (file) => texts[file] ?? ""),
    [
      "fork-ci/heavy.test.py --run",
      "fork-ci/other.test.py",
      "fork-ci/own.test.py --run",
      "fork-ci/part.test.py",
      "fork-ci/part.test.py --run only L",
      "fork-ci/plain.test.sh",
      "scripts/upstream.test.ts",
    ],
  );
});

test("recordProblems: a failure, no count, nothing run, a skip not declared or declared and run, a failed test", () => {
  const row = (fields) => ({
    key: "k",
    status: 0,
    tests: 3,
    skipped: 0,
    skipped_tests: [],
    failed: 0,
    note: null,
    allowed: [],
    ...fields,
  });
  assert.deepEqual(recordProblems(row({})), []);
  assert.deepEqual(recordProblems(row({ status: null, timed_out: 240 })), [
    "k timed out after 240 s",
  ]);
  assert.deepEqual(recordProblems(row({ status: null })), ["k failed (exit null)"]);
  assert.deepEqual(
    recordProblems(row({ skipped: 1, skipped_tests: ["T.a"], allowed: ["T.a"] })),
    [],
  );
  const cases = [
    [{ status: 1 }, /^k failed \(exit 1\)$/],
    [{ status: null }, /^k failed \(exit null\)$/],
    [{ tests: null, skipped: null, failed: null }, /^k: its output shows no test count, so/],
    [{ tests: null, note: "it stopped" }, /^k: its output shows no test count \(it stopped\), so/],
    [{ tests: 0 }, /^k ran no test \(0 found, 0 skipped\)$/],
    [
      { tests: 2, skipped: 2, skipped_tests: ["T.a", "T.b"], allowed: ["T.a", "T.b"] },
      /^k ran no test \(2 found, 2 skipped\)$/,
    ],
    // Tight both ways: a skip it does not declare, and a declared one that ran (a stale line).
    [{ skipped: 1, skipped_tests: ["T.a"] }, /^k skipped T\.a, which it does not declare/],
    [
      { skipped: 1, skipped_tests: ["T.b"], allowed: ["T.a", "T.b"] },
      /for T\.a, which did not skip: remove that line$/,
    ],
    [{ allowed: ["T.a"] }, /^k declares "fork-ci: may skip <test>" for T\.a, which did not skip/],
    // A skip without a name (node's), or one named twice: a declaration could cover it, so red.
    [{ skipped: 1, skipped_tests: [] }, /^k skipped 1 tests, 0 of them by a name of their own/],
    [
      { skipped: 2, skipped_tests: ["T.a", "T.a"], allowed: ["T.a"] },
      /every skip must name its test, once/,
    ],
    [{ failed: 1 }, /^k: 1 failed$/],
  ];
  for (const [fields, reason] of cases) {
    const problems = recordProblems(row(fields));
    assert.equal(problems.length, 1, JSON.stringify(fields) + JSON.stringify(problems));
    assert.match(problems[0], reason);
  }
});

test("record keeps what a run was, what its output counted and the skips its test declares", () => {
  const run = { root: true, vars: ["A_TESTS=1"], only: "L" };
  assert.deepEqual(
    record(
      "f --run only L",
      "fork-ci/f.test.py",
      run,
      {
        status: 0,
        seconds: "1.5",
        stdout: "",
        stderr: "Ran 2 tests in 0.1s\n\nOK\n",
        unittest: [
          { plan: 2 },
          { tests: 2, skipped: ["L.test_x"], failed: 0 },
          { unloaded: ["M.test_z", "L.test_y"] },
        ],
      },
      ["L.test_y", "L.test_x"],
      "unittest",
    ),
    {
      key: "f --run only L",
      test: "fork-ci/f.test.py",
      own: true,
      root: true,
      vars: ["A_TESTS=1"],
      only: "L",
      status: 0,
      timed_out: null,
      seconds: 1.5,
      format: "unittest",
      tests: 3,
      skipped: 2,
      skipped_tests: ["L.test_x", "L.test_y"],
      unloaded: ["L.test_y"],
      failed: 0,
      note: null,
      allowed: ["L.test_x", "L.test_y"],
    },
  );
  // A Python test's format is the list's (none given: no count), so a hard stop as it loads is never read as ok lines.
  const printed = {
    status: 0,
    seconds: "1",
    stdout: "ok a\nall passed: 1 ok, 0 skipped\n",
    unittest: [],
  };
  assert.deepEqual(
    [
      record("p", "fork-ci/p.test.py", null, printed, []).tests,
      record("p", "fork-ci/p.test.py", null, printed, [], "unittest").tests,
      record(
        "p",
        "fork-ci/p.test.py",
        null,
        { ...printed, unittest: [{ unloaded: [] }] },
        [],
        "ok lines",
      ).tests,
    ],
    [null, null, 1],
  );
  assert.equal(record("p", "fork-ci/p.test.py", null, printed, []).format, null);
  const none = record("f", "fork-ci/f.test.sh", null, { status: 0, seconds: "0.1" }, ["b", "a"]);
  assert.deepEqual(
    [none.own, none.vars, none.tests, none.skipped, none.skipped_tests, none.allowed],
    [false, [], null, null, [], ["a", "b"]],
  );
  const early = record(
    "f",
    "fork-ci/f.test.sh",
    null,
    { status: 0, seconds: "0.1", stdout: "ok one\n" },
    [],
  );
  assert.deepEqual([early.tests, early.format], [null, "ok lines"]);
  assert.match(early.note, /may have stopped before its end/);
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

test("command: Python through the counting shim; an own run as root through sudo -n env; no records or counts reach a test", () => {
  const env = {
    FORK_CI_TEST_RECORDS: "/r",
    FORK_CI_TEST_COUNTS: "/outer/counts.jsonl", // a runner's test that runs this runner: not handed on
    GITHUB_ACTIONS: "true",
    KEEP: "1",
  };
  const part = command("fork-ci/a.test.sh", null, env, "/c/counts.jsonl");
  assert.deepEqual(part.argv, ["bash", "fork-ci/a.test.sh"]);
  assert.equal(part.env.FORK_CI_TEST_RECORDS, undefined);
  assert.equal(part.env.FORK_CI_TEST_COUNTS, undefined);
  assert.equal(part.env.KEEP, "1");
  const py = command("fork-ci/a.test.py", null, env, "/c/counts.jsonl");
  assert.deepEqual(py.argv, [
    "python3",
    ".github/scripts/fork-ci-unittest.py",
    "fork-ci/a.test.py",
  ]);
  assert.equal(py.env.FORK_CI_TEST_COUNTS, "/c/counts.jsonl");
  assert.equal(command("fork-ci/a.test.py", null, env, null).env.FORK_CI_TEST_COUNTS, undefined);
  const root = command(
    "fork-ci/a.test.py",
    { root: true, vars: ["A_TESTS=1"], only: "L" },
    env,
    "/c/n.jsonl",
  );
  assert.deepEqual(root.argv, [
    "sudo",
    "-n",
    "env",
    "A_TESTS=1",
    "GITHUB_ACTIONS=true",
    "FORK_CI_TEST_COUNTS=/c/n.jsonl",
    "python3",
    "-I",
    "-B",
    ".github/scripts/fork-ci-unittest.py",
    "fork-ci/a.test.py",
    "-v",
    "L",
  ]);
  assert.equal(root.env.FORK_CI_TEST_RECORDS, undefined);
  const user = command("fork-ci/a.test.cjs", { root: false, vars: ["A_TESTS=x"], only: null }, env);
  assert.deepEqual(user.argv, ["node", "--test", "fork-ci/a.test.cjs"]);
  assert.equal(user.env.A_TESTS, "x");
  assert.equal(user.env.FORK_CI_TEST_RECORDS, undefined);
  assert.equal(user.env.FORK_CI_TEST_COUNTS, undefined);
  assert.equal(env.A_TESTS, undefined); // the caller's environment is left alone
  // A node test also reports to the counting reporter (spec still on stdout for the log); node's own context is not
  // handed on (this file runs under node --test), or node would report to that and not to the reporters.
  const node = command(
    "fork-ci/a.test.cjs",
    null,
    { ...env, NODE_TEST_CONTEXT: "child-v8" },
    "/c/n.jsonl",
  );
  assert.deepEqual(node.argv, [
    "node",
    "--test",
    "--test-reporter=spec",
    "--test-reporter-destination=stdout",
    "--test-reporter=./.github/scripts/fork-ci-node-counts.cjs",
    "--test-reporter-destination=/c/n.jsonl",
    "fork-ci/a.test.cjs",
  ]);
  assert.equal(node.env.NODE_TEST_CONTEXT, undefined);
  assert.equal(node.env.FORK_CI_TEST_COUNTS, undefined);
});

// An ok-lines test's end line: its own count of its ok lines (countTests()).
const END = (n) => `echo "all passed: ${n} ok, 0 skipped"\n`;

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
      'echo "ok a"\n[ -z "$FORK_CI_TEST_RECORDS" ] || { echo "FAIL records folder reached the test"; exit 1; }\n' +
      END(1),
    "fork-ci/b.test.sh": 'echo "ok b"\n' + END(1),
    "fork-ci/own.test.py": "# fork-ci: run with OWN_TESTS=1\n",
    ".github/workflows/fork-tests.yml":
      "      - run: node .github/scripts/fork-ci-tests.cjs --run fork-ci/own.test.py\n",
  };
  const list =
    "fork-ci/a.test.sh\nfork-ci/b.test.sh\nfork-ci/own.test.py run by .github/workflows/fork-tests.yml unittest\n";
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

test("a part fails on a skip its test does not declare or a declared one that ran, passes on exactly those", () => {
  for (const [head, status, reason] of [
    ["", 1, /fork-ci\/s\.test\.py skipped T\.test_b, which it does not declare/],
    ["# fork-ci: may skip T.test_b\n", 0, null],
    [
      "# fork-ci: may skip T.test_b\n# fork-ci: may skip T.test_a\n",
      1,
      /fork-ci\/s\.test\.py declares "fork-ci: may skip <test>" for T\.test_a, which did not skip: remove that line/,
    ],
    ["# fork-ci: may skip T.test_c\n", 1, /skipped T\.test_b, which it does not declare/],
    // The old budget line: refused, not read as a budget.
    [
      "# fork-ci: skips at most 1\n",
      1,
      /fork-ci\/s\.test\.py: "fork-ci: skips at most 1": expected "fork-ci: may skip/,
    ],
  ]) {
    const { run, records, cleanup } = runRecording(
      { "fork-ci/s.test.py": head + SKIPPING_PY },
      [],
      "fork-ci/s.test.py unittest",
    );
    try {
      assert.equal(run.status, status, head + run.stdout + run.stderr);
      if (reason) assert.match(run.stdout, reason, head);
      const [only] = Object.values(readRecords(records));
      const r = only.results[0];
      assert.deepEqual(
        [r.format, r.tests, r.skipped, r.skipped_tests],
        ["unittest", 2, 1, ["T.test_b"]],
        head,
      );
    } finally {
      cleanup();
    }
  }
});

// A class whose setUpClass raises SkipTest: unittest's summary counts it as one skip and leaves its tests out of
// `Ran N`, so 3 unrun tests looked like 1. Each test counts here, by name.
const CLASS_SKIP_PY =
  "import unittest\n" +
  "class Off(unittest.TestCase):\n" +
  "    @classmethod\n" +
  "    def setUpClass(cls): raise unittest.SkipTest('no sandbox')\n" +
  "    def test_x(self): pass\n" +
  "    def test_y(self): pass\n" +
  "    def test_z(self): pass\n" +
  "class On(unittest.TestCase):\n" +
  "    def test_a(self): pass\n" +
  "    @unittest.expectedFailure\n" +
  "    def test_known(self): raise AssertionError\n" +
  "    def test_sub(self):\n" +
  "        for i in range(3):\n" +
  "            with self.subTest(i=i):\n" +
  "                pass\n" +
  "if __name__ == '__main__':\n" +
  "    unittest.main()\n";

test("a class or module skip counts every test it skips, by name, so a skipped class never looks like a pass", () => {
  const cases = [
    // A skip of one test declared: the class skip's other tests are still red.
    [
      "# fork-ci: may skip Off.test_x\n" + CLASS_SKIP_PY,
      1,
      6,
      ["Off.test_x", "Off.test_y", "Off.test_z", "On.test_known"],
      /skipped Off\.test_y, Off\.test_z, On\.test_known, which it does not declare/,
    ],
    // Every one of them declared: passes, and the record names each.
    [
      ["Off.test_x", "Off.test_y", "Off.test_z", "On.test_known"]
        .map((t) => `# fork-ci: may skip ${t}\n`)
        .join("") + CLASS_SKIP_PY,
      0,
      6,
      ["Off.test_x", "Off.test_y", "Off.test_z", "On.test_known"],
      null,
    ],
    // A module skip: all of them, so none ran.
    [
      "# fork-ci: may skip T.test_a\n" +
        "import unittest\n" +
        "def setUpModule(): raise unittest.SkipTest('off')\n" +
        "class T(unittest.TestCase):\n" +
        "    def test_a(self): pass\n" +
        "    def test_b(self): pass\n" +
        "unittest.main()\n",
      1,
      2,
      ["T.test_a", "T.test_b"],
      /ran no test \(2 found, 2 skipped\)/,
    ],
    // A setUpClass that fails: its tests never ran (skipped, by name) and the error fails the run.
    [
      "import unittest\n" +
        "class T(unittest.TestCase):\n" +
        "    @classmethod\n" +
        "    def setUpClass(cls): raise RuntimeError('broken')\n" +
        "    def test_a(self): pass\n" +
        "class U(unittest.TestCase):\n" +
        "    def test_b(self): pass\n" +
        "unittest.main()\n",
      1,
      2,
      ["T.test_a"],
      /fork-ci\/c\.test\.py failed \(exit 1\)/,
    ],
    // Stopped half-way (an exit inside a test): no counts line, so no count, so red.
    [
      "import os, unittest\n" +
        "class T(unittest.TestCase):\n" +
        "    def test_a(self): os._exit(0)\n" +
        "    def test_b(self): pass\n" +
        "unittest.main()\n",
      1,
      null,
      [],
      /fork-ci\/c\.test\.py: its output shows no test count/,
    ],
    // Two TextTestRunner runs (two suites in one test): both counted, and the counts file is not this test's to see.
    [
      "import os, unittest\n" +
        "class T(unittest.TestCase):\n" +
        "    def test_a(self): self.assertNotIn('FORK_CI_TEST_COUNTS', os.environ)\n" +
        "class U(unittest.TestCase):\n" +
        "    def test_b(self): pass\n" +
        "    def test_c(self): pass\n" +
        "load = unittest.defaultTestLoader.loadTestsFromTestCase\n" +
        "ok = all(unittest.TextTestRunner().run(load(c)).wasSuccessful() for c in (T, U))\n" +
        "raise SystemExit(0 if ok else 1)\n",
      0,
      3,
      [],
      null,
    ],
  ];
  for (const [text, status, tests, skipped, reason] of cases) {
    const { run, records, cleanup } = runRecording(
      { "fork-ci/c.test.py": text },
      [],
      "fork-ci/c.test.py unittest",
    );
    try {
      assert.equal(run.status, status, text + run.stdout + run.stderr);
      if (reason) assert.match(run.stdout, reason, text);
      const r = Object.values(readRecords(records))[0].results[0];
      assert.deepEqual([r.tests, r.skipped_tests], [tests, skipped], text);
    } finally {
      cleanup();
    }
  }
});

test("a Python test run through the shim imports its own folder's modules and sees its own path, as run plainly", () => {
  const tests = {
    "fork-ci/near.py": "VALUE = 7\n",
    "fork-ci/i.test.py":
      "import os, sys, unittest\n" +
      "import near\n" +
      "class T(unittest.TestCase):\n" +
      "    def test_near(self): self.assertEqual(near.VALUE, 7)\n" +
      "    def test_file(self): self.assertTrue(__file__.endswith('i.test.py'))\n" +
      "    def test_argv(self): self.assertTrue(sys.argv[0].endswith('i.test.py'))\n" +
      "if __name__ == '__main__':\n" +
      "    unittest.main()\n",
  };
  const { run, records, cleanup } = runRecording(tests, [], "fork-ci/i.test.py unittest");
  try {
    assert.equal(run.status, 0, run.stdout + run.stderr);
    assert.equal(Object.values(readRecords(records))[0].results[0].tests, 3);
  } finally {
    cleanup();
  }
});

test("an ok-lines test that stops before its end line, or prints a child's ok lines, counts nothing and fails", () => {
  for (const [text, reason] of [
    ['echo "ok one"\nexit 0\necho "ok two"\n' + END(2), /may have stopped before its end/],
    [
      'echo "ok one"\necho "ok two from a child"\n' + END(1),
      /says 1 ok, 0 skipped, but its stdout has 2 ok/,
    ],
    [
      'echo "ok one"\necho "skip net no network"\n' + END(1),
      /says 1 ok, 0 skipped, but its stdout has 1 ok and 1 skip/,
    ],
  ]) {
    const { run, cleanup } = runRecording({ "fork-ci/e.test.sh": text }, [], "fork-ci/e.test.sh");
    try {
      assert.equal(run.status, 1, text + run.stdout + run.stderr);
      assert.match(run.stdout, reason, text);
    } finally {
      cleanup();
    }
  }
  // Its named skips, declared: passes.
  const { run, records, cleanup } = runRecording(
    {
      "fork-ci/e.test.sh":
        '# fork-ci: may skip net\necho "ok one"\necho "skip net no network"\n' +
        'echo "all passed: 1 ok, 1 skipped"\n',
    },
    [],
    "fork-ci/e.test.sh",
  );
  try {
    assert.equal(run.status, 0, run.stdout + run.stderr);
    assert.deepEqual(Object.values(readRecords(records))[0].results[0].skipped_tests, ["net"]);
  } finally {
    cleanup();
  }
});

test("a test `run by` a workflow other than fork-tests.yml (and not RUN_ELSEWHERE's) fails and still owes its record", () => {
  assert.deepEqual(RUN_ELSEWHERE, {
    ".github/scripts/fork-secret-scan.test.sh": ".github/workflows/fork-secret-scan.yml",
    ".github/scripts/relay-state-output.test.cjs": "scripts/release-smoke.ts",
  });
  assert.equal(
    elsewhereAllowed({ file: "fork-ci/a.test.sh", runBy: ".github/workflows/fork-other.yml" }),
    false,
  );
  assert.equal(
    elsewhereAllowed({ file: "fork-ci/a.test.sh", runBy: ".github/workflows/fork-tests.yml" }),
    true,
  );
  // A test named like upstream's is decided by the table too: a fork test without the prefix cannot leave the records.
  assert.equal(
    elsewhereAllowed({ file: ".github/scripts/a.test.py", runBy: "scripts/smoke.ts" }),
    false,
  );
  assert.equal(
    elsewhereAllowed(
      { file: ".github/scripts/a.test.py", runBy: "scripts/smoke.ts" },
      {
        ".github/scripts/a.test.py": "scripts/smoke.ts",
      },
    ),
    true,
  );
  assert.equal(
    elsewhereAllowed({
      file: ".github/scripts/relay-state-output.test.cjs",
      runBy: "scripts/release-smoke.ts",
    }),
    true,
  );
  assert.equal(
    elsewhereAllowed({
      file: ".github/scripts/fork-secret-scan.test.sh",
      runBy: ".github/workflows/fork-other.yml",
    }),
    false,
  );
  const tests = {
    "fork-ci/a.test.sh": 'echo "ok a"\n' + END(1),
    "fork-ci/b.test.sh": 'echo "ok b"\n' + END(1),
    ".github/workflows/fork-other.yml": "      - run: bash fork-ci/b.test.sh\n",
  };
  const list = "fork-ci/a.test.sh\nfork-ci/b.test.sh run by .github/workflows/fork-other.yml\n";
  const { run, records, cleanup } = runRecording(tests, ["fork-ci/"], list);
  try {
    assert.equal(run.status, 1, run.stdout + run.stderr);
    assert.match(
      run.stdout,
      /fork-ci\/b\.test\.sh.*a listed test is run by \.github\/workflows\/fork-tests\.yml/,
    );
    const part = Object.values(readRecords(records))[0];
    assert.deepEqual(part.all, ["fork-ci/a.test.sh", "fork-ci/b.test.sh"]); // fork-ci-result: b not recorded
    assert.deepEqual(part.owed, ["fork-ci/a.test.sh"]);
  } finally {
    cleanup();
  }
  // The same for a fork test without the fork- prefix, which its runner names.
  const plain = runRecording(
    {
      "fork-ci/a.test.sh": 'echo "ok a"\n' + END(1),
      ".github/scripts/b.test.sh": 'echo "ok b"\n' + END(1),
      ".github/workflows/fork-other.yml": "      - run: bash .github/scripts/b.test.sh\n",
    },
    ["fork-ci/", ".github/scripts/"],
    "fork-ci/a.test.sh\n.github/scripts/b.test.sh run by .github/workflows/fork-other.yml\n",
  );
  try {
    assert.equal(plain.run.status, 1, plain.run.stdout + plain.run.stderr);
    assert.match(plain.run.stdout, /\.github\/scripts\/b\.test\.sh.*only where RUN_ELSEWHERE/);
    const part = Object.values(readRecords(plain.records))[0];
    assert.deepEqual(part.all, [".github/scripts/b.test.sh", "fork-ci/a.test.sh"]);
    assert.deepEqual(part.owed, ["fork-ci/a.test.sh"]);
  } finally {
    plain.cleanup();
  }
});

// A git repository of one commit in `root`, its HEAD returned.
function commitAll(root) {
  const git = (...args) =>
    childProcess
      .execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@example.invalid", ...args], {
        cwd: root,
        encoding: "utf8",
        env: { ...process.env, GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_NOSYSTEM: "1" },
      })
      .trim();
  git("init", "-q");
  git("add", "-A");
  git("commit", "-q", "-m", "x");
  return git("rev-parse", "HEAD");
}

test("a part's and an own run's record name the commit the checkout tests; outside a repository, none", () => {
  const tests = {
    "fork-ci/a.test.sh": 'echo "ok a"\n' + END(1),
    "fork-ci/own.test.sh": "# fork-ci: run\n" + 'echo "ok own"\n' + END(1),
  };
  const list = "fork-ci/a.test.sh\nfork-ci/own.test.sh run by .github/workflows/fork-tests.yml\n";
  // Outside a repository first (the same tree, before its commit): no commit, which fork-ci-result refuses.
  for (const repo of [false, true]) {
    const records = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-rec-"));
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-tests-"));
    try {
      const write = (file, text) => {
        fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
        fs.writeFileSync(path.join(root, file), text);
      };
      write(
        ".github/scripts/fork-ci-tests.cjs",
        fs.readFileSync(require.resolve("./fork-ci-tests.cjs")),
      );
      write(
        ".github/scripts/fork-ci-unittest.py",
        fs.readFileSync(path.join(__dirname, "fork-ci-unittest.py")),
      );
      write(".github/fork-ci-tests.txt", list);
      write(
        ".github/workflows/fork-tests.yml",
        "      - run: node .github/scripts/fork-ci-tests.cjs --run fork-ci/own.test.sh\n",
      );
      for (const [file, text] of Object.entries(tests)) write(file, text);
      const head = repo ? commitAll(root) : null;
      const env = {
        ...process.env,
        ...recording(records),
        FORK_CI_TEST_JOBS: "1",
        GIT_CEILING_DIRECTORIES: os.tmpdir(),
      };
      for (const name of ["FORK_CI_TEST_SHARD", "GITHUB_STEP_SUMMARY", "FORK_CI_TEST_COUNTS"])
        delete env[name];
      for (const args of [["fork-ci/"], ["--run", "fork-ci/own.test.sh"]]) {
        const run = childProcess.spawnSync("node", [".github/scripts/fork-ci-tests.cjs", ...args], {
          cwd: root,
          env,
          encoding: "utf8",
        });
        assert.equal(run.status, 0, run.stdout + run.stderr);
      }
      const all = readRecords(records);
      assert.deepEqual(Object.keys(all).toSorted(), [
        "part-1of1-fork-ci_.json",
        "run-fork-ci_own.test.sh.json",
      ]);
      for (const body of Object.values(all)) assert.equal(body.sha, head, JSON.stringify(body));
    } finally {
      for (const dir of [root, records]) fs.rmSync(dir, { recursive: true, force: true });
    }
  }
});

// main() in a scratch tree with a fake runTest, recording; returns the part's record.
async function mainRecording(list, runTest) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-main-"));
  const records = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-rec-"));
  const script = path.join(root, ".github/scripts/fork-ci-tests.cjs");
  fs.mkdirSync(path.dirname(script), { recursive: true });
  fs.copyFileSync(require.resolve("./fork-ci-tests.cjs"), script);
  fs.writeFileSync(path.join(root, ".github/fork-ci-tests.txt"), list.join("\n") + "\n");
  for (const line of list) {
    fs.mkdirSync(path.join(root, path.dirname(line.split(" ")[0])), { recursive: true });
    fs.writeFileSync(path.join(root, line.split(" ")[0]), "exit 0\n");
  }
  const saved = { ...process.env };
  const exitCode = process.exitCode;
  const log = console.log;
  const out = [];
  Object.assign(process.env, { FORK_CI_TEST_JOBS: "1", ...recording(records) });
  delete process.env.FORK_CI_TEST_SHARD;
  delete process.env.GITHUB_STEP_SUMMARY;
  console.log = (...args) => out.push(args.join(" "));
  try {
    await require(script).main(["fork-ci/"], { runTest });
    const part = Object.values(readRecords(records))[0];
    return { part, out: out.join("\n"), exitCode: process.exitCode };
  } finally {
    console.log = log;
    process.exitCode = exitCode;
    for (const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key];
    Object.assign(process.env, saved);
    for (const dir of [root, records]) fs.rmSync(dir, { recursive: true, force: true });
  }
}

test("a part's owed list is its plan (its shard's tests), not what came back from running them", async () => {
  const ok = {
    status: 0,
    seconds: "1.0",
    stdout: "ok x\nall passed: 1 ok, 0 skipped\n",
    stderr: "",
  };
  // A run that comes back as another test's (a runner bug): owed still names the planned ones, so the
  // results differ from it and fork-ci-result fails the part ("results do not match what it owed").
  const { part } = await mainRecording(
    ["fork-ci/a.test.sh", "fork-ci/b.test.sh"],
    async (file) => ({
      ...ok,
      file: file === "fork-ci/b.test.sh" ? "fork-ci/z.test.sh" : file,
    }),
  );
  assert.deepEqual(part.owed, ["fork-ci/a.test.sh", "fork-ci/b.test.sh"]);
  assert.deepEqual(part.results.map((r) => r.key).toSorted(), [
    "fork-ci/a.test.sh",
    "fork-ci/z.test.sh",
  ]);
});

// One test file run as a listed part test, recording: [exit status, its record, the part's stdout]. `line`: its line
// in the list (a Python test's says "unittest" unless given).
function recordOf(file, text, more = {}, line = file.endsWith(".py") ? `${file} unittest` : file) {
  const { run, records, cleanup } = runRecording({ [file]: text, ...more }, [], line);
  try {
    return [run.status, Object.values(readRecords(records))[0].results[0], run.stdout + run.stderr];
  } finally {
    cleanup();
  }
}

test("a node test counts the tests node's reporter saw end; the file alone, or tests that never ended, count nothing", () => {
  const file = "fork-ci/n.test.cjs";
  const t = 'const test = require("node:test");\n';
  const exits = 'test("b", () => process.exit(0));\ntest("c", () => {});\ntest("d", () => {});\n';
  const sub =
    'test("a", async (t) => { await t.test("x", () => {}); await t.test("y", () => process.exit(0)); ' +
    'await t.test("z", () => {}); });\n';
  // node itself says "tests 1, pass 1" (the file as its one test) for each of these.
  for (const [text, why] of [
    ["", /only the file itself/],
    ["return;\n" + t + 'test("a", () => {});\n', /only the file itself/],
    [t + 'test("a", () => {});\n' + exits, /only the file itself|tests started and \d+ ended/],
    [t + sub, /only the file itself|tests started and \d+ ended/],
    [
      t +
        Array.from(
          { length: 60 },
          (_, i) => `test("t${i}", () => { if (${i} === 40) process.exit(0); });\n`,
        ).join(""),
      /only the file itself|tests started and \d+ ended/,
    ],
  ]) {
    const [status, r, out] = recordOf(file, text);
    assert.equal(status, 1, text + out);
    assert.deepEqual([r.format, r.tests], ["node", null], text);
    assert.match(r.note, why, text);
    assert.match(out, /fork-ci\/n\.test\.cjs: its output shows no test count/, text);
  }
  const good =
    t +
    'const { describe, it } = test;\ntest("a", () => {});\n' +
    'describe("s", () => { it("b", () => {}); it("c", () => {}); });\n';
  const [status, r, out] = recordOf(file, good);
  assert.equal(status, 0, out);
  assert.deepEqual([r.format, r.tests, r.skipped, r.failed, r.note], ["node", 3, 0, 0, null]);
  // A run again in a new part writes its own record from its own reporter file: the same count, nothing carried over.
  assert.deepEqual(recordOf(file, good)[1].tests, 3);
});

test("a Python test that exits hard after its plan counts nothing, even with a child's ok and end lines on stdout", () => {
  const [status, r, out] = recordOf(
    "fork-ci/h.test.py",
    "import os, unittest\n" +
      "class T(unittest.TestCase):\n" +
      "    def test_a(self):\n" +
      "        print('ok child'); print('all passed: 1 ok, 0 skipped', flush=True); os._exit(0)\n" +
      "    def test_b(self): pass\n" +
      "unittest.main()\n",
  );
  assert.equal(status, 1, out);
  assert.deepEqual([r.format, r.tests], ["unittest", null]);
  assert.match(r.note, /a unittest run of 2 tests started but never ended/);
});

// A unittest file that stops before unittest.main: a child prints ok lines and a matching end line on the shared stdout.
const STOPS_AS_IT_LOADS = (exit) =>
  "import os, subprocess, sys, unittest\n" +
  "subprocess.run([sys.executable, '-c', \"print('ok child'); print('all passed: 1 ok, 0 skipped')\"])\n" +
  `${exit}\n` +
  "class T(unittest.TestCase):\n" +
  "    def test_a(self): pass\n" +
  "unittest.main()\n";
// An ok-lines Python test: its own lines and end line, no unittest.
const OK_LINES_PY =
  "print('ok one')\nprint('skip net no network here')\nprint('all passed: 1 ok, 1 skipped')\n";

test("a Python test that stops before its first unittest run counts nothing, whatever ok lines its stdout has", () => {
  const file = "fork-ci/h.test.py";
  // A hard stop as it loads leaves no shim line at all; a soft one (sys.exit) its end line, with no run before it.
  for (const [exit, note] of [
    ["os._exit(0)", /wrote no line: it stopped hard before its first unittest run/],
    ["sys.exit(0)", /started no unittest run/],
  ]) {
    const [status, r, out] = recordOf(file, STOPS_AS_IT_LOADS(exit), {}, `${file} unittest`);
    assert.equal(status, 1, exit + out);
    assert.deepEqual([r.format, r.tests], ["unittest", null], exit);
    assert.match(r.note, note, exit);
    assert.match(out, /ok child/, exit); // the child's lines were there to be misread
  }
  // Said to print ok lines, a hard stop leaves no end line from the shim either: nothing counts.
  const [status, r] = recordOf(file, STOPS_AS_IT_LOADS("os._exit(0)"), {}, `${file} ok lines`);
  assert.equal(status, 1);
  assert.deepEqual([r.format, r.tests], ["ok lines", null]);
  assert.match(r.note, /stopped before its end \(a hard exit/);
});

test("a Python test that prints ok lines is read by them only where the list says so", () => {
  const file = "fork-ci/o.test.py";
  const [status, r, out] = recordOf(
    file,
    "# fork-ci: may skip net\n" + OK_LINES_PY,
    {},
    `${file} ok lines`,
  );
  assert.equal(status, 0, out);
  assert.deepEqual([r.format, r.tests, r.skipped_tests, r.note], ["ok lines", 2, ["net"], null]);
  const [status2, r2] = recordOf(
    file,
    "# fork-ci: may skip net\n" + OK_LINES_PY,
    {},
    `${file} unittest`,
  );
  assert.equal(status2, 1);
  assert.deepEqual([r2.format, r2.tests], ["unittest", null]);
  assert.match(
    r2.note,
    /started no unittest run .* says "ok lines" in \.github\/fork-ci-tests\.txt/,
  );
  // A unittest test said to print ok lines is refused too, not read by its ok lines.
  const [status3, r3] = recordOf(file, SKIPPING_PY, {}, `${file} ok lines`);
  assert.equal(status3, 1);
  assert.match(r3.note, /says it prints ok lines, but it ran unittest/);
  // An own run (`--run`) reads the same list line: an ok-lines Python test run whole by fork-tests.yml.
  const plain = "print('ok one')\nprint('all passed: 1 ok, 0 skipped')\n";
  for (const [line, status4, format] of [
    [`${file} run by .github/workflows/fork-tests.yml ok lines`, 0, "ok lines"],
    [`${file} run by .github/workflows/fork-tests.yml unittest`, 1, "unittest"],
  ]) {
    const { run, records, cleanup } = runRecording({ [file]: plain }, ["--run", file], line);
    try {
      assert.equal(run.status, status4, line + run.stdout + run.stderr);
      const r = Object.values(readRecords(records))[0].results[0];
      assert.deepEqual([r.key, r.format, r.tests], [`${file} --run`, format, status4 ? null : 1]);
    } finally {
      cleanup();
    }
  }
});

// A unittest file that stops hard as it loads, before any unittest run: no shim line, nothing on stdout.
const EXITS_AT_IMPORT =
  "import os\n" +
  "os._exit(0)\n" +
  "import unittest\n" +
  "class T(unittest.TestCase):\n" +
  "    def test_a(self): pass\n" +
  "unittest.main()\n";

test("each listed Python test declares its format and is judged by it alone; none declared is refused (#702)", () => {
  const file = "fork-ci/d.test.py";
  // What a record says, without its time: a run again must give the same.
  const verdict = ([status, r]) => [status, r.format, r.tests, r.skipped_tests, r.note];
  // Declared unittest, it stops as it loads: no plan line, red; the same again on a second run.
  const first = verdict(recordOf(file, EXITS_AT_IMPORT, {}, `${file} unittest`));
  assert.deepEqual(first.slice(0, 4), [1, "unittest", null, []]);
  assert.match(first[4], /wrote no line: it stopped hard before its first unittest run/);
  assert.deepEqual(verdict(recordOf(file, EXITS_AT_IMPORT, {}, `${file} unittest`)), first);
  // Declared ok lines, an ok-lines file passes, by its own count, again on a second run.
  const ok = verdict(
    recordOf(file, "# fork-ci: may skip net\n" + OK_LINES_PY, {}, `${file} ok lines`),
  );
  assert.deepEqual(ok, [0, "ok lines", 2, ["net"], null]);
  assert.deepEqual(
    verdict(recordOf(file, "# fork-ci: may skip net\n" + OK_LINES_PY, {}, `${file} ok lines`)),
    ok,
  );
  // A declared format the file does not keep is red, either way round.
  for (const [text, line, format, note] of [
    [OK_LINES_PY, `${file} unittest`, "unittest", /started no unittest run/],
    [SKIPPING_PY, `${file} ok lines`, "ok lines", /says it prints ok lines, but it ran unittest/],
    [EXITS_AT_IMPORT, `${file} ok lines`, "ok lines", /stopped before its end \(a hard exit/],
  ]) {
    const [status, r, out] = recordOf(file, text, {}, line);
    assert.equal(status, 1, line + out);
    assert.deepEqual([r.format, r.tests], [format, null], line);
    assert.match(r.note, note, line);
  }
  // No format declared: the list is refused, with what to write, before any test runs (MARKS/ran stays absent).
  for (const line of [file, `${file} ~20s`, `${file} run by .github/workflows/fork-tests.yml`]) {
    const tests = {
      [file]:
        "import os\nopen(os.path.join(os.environ['MARKS'], 'ran'), 'w').close()\n" + OK_LINES_PY,
      "fork-ci/a.test.sh": 'echo "ok a"\n' + END(1),
    };
    const { root, run, records, cleanup } = runRecording(
      tests,
      line.includes("run by") ? ["--run", file] : [],
      `fork-ci/a.test.sh\n${line}`,
    );
    try {
      assert.equal(run.status, 1, line + run.stdout + run.stderr);
      assert.match(
        run.stderr,
        /fork-ci-tests\.txt: ".*d\.test\.py.*": a Python test says its format at the end of its line: "unittest"/,
        line,
      );
      assert.equal(fs.existsSync(path.join(root, "ran")), false, line);
      assert.equal(fs.existsSync(path.join(records, "records")), false, line);
    } finally {
      cleanup();
    }
  }
});

test("a test the file defines that no unittest run loaded is a skip by name: a class under a condition, load_tests", () => {
  const file = "fork-ci/u.test.py";
  const conditional =
    "import shutil, unittest\n" +
    "class Basic(unittest.TestCase):\n" +
    "    def test_a(self): pass\n" +
    "if shutil.which('no-such-tool-here'):\n" +
    "    class Tool(unittest.TestCase):\n" +
    "        def test_b(self): pass\n" +
    "        def test_c(self): pass\n" +
    // A mixin's and a base's tests run under their subclasses, a function outside a class is no test, and a class
    // made in a function is not seen: none of these is unloaded.
    "class Mixin:\n" +
    "    def test_m(self): pass\n" +
    "class WithMixin(Mixin, unittest.TestCase): pass\n" +
    "class Base(unittest.TestCase):\n" +
    "    def test_base(self): pass\n" +
    "class Sub(Base): pass\n" +
    "del Base\n" +
    "def test_helper(): pass\n" +
    "def make():\n" +
    "    class Made(unittest.TestCase):\n" +
    "        def test_never(self): pass\n" +
    "unittest.main()\n";
  const [status, r, out] = recordOf(file, conditional);
  assert.equal(status, 1, out);
  assert.deepEqual(
    [r.format, r.tests, r.skipped, r.skipped_tests, r.unloaded],
    ["unittest", 5, 2, ["Tool.test_b", "Tool.test_c"], ["Tool.test_b", "Tool.test_c"]],
  );
  assert.match(
    out,
    /skipped Tool\.test_b, Tool\.test_c, which it does not declare \(Tool\.test_b, Tool\.test_c: defined in its source, but no unittest run loaded them/,
  );
  // Declared, as any test that cannot run here: then it must not load, exactly as a declared skip must skip.
  const declared =
    "# fork-ci: may skip Tool.test_b\n# fork-ci: may skip Tool.test_c\n" + conditional;
  assert.equal(recordOf(file, declared)[0], 0);
  assert.equal(
    recordOf(file, declared.replace("no-such-tool-here", "python3"))[0],
    1,
    "the tool there: the declared tests ran",
  );
  // A load_tests that leaves a test out.
  const [status2, r2] = recordOf(
    file,
    "import unittest\n" +
      "class T(unittest.TestCase):\n" +
      "    def test_a(self): pass\n" +
      "    def test_b(self): pass\n" +
      "def load_tests(loader, tests, pattern):\n" +
      "    return unittest.TestSuite([T('test_a')])\n" +
      "unittest.main()\n",
  );
  assert.equal(status2, 1);
  assert.deepEqual([r2.tests, r2.skipped_tests, r2.unloaded], [2, ["T.test_b"], ["T.test_b"]]);
});

test("an own run of one class counts that class's unloaded tests only", () => {
  const file = "fork-ci/c.test.py";
  const text = (cond) =>
    "# fork-ci: run only L\n" +
    "import unittest\n" +
    "class L(unittest.TestCase):\n" +
    "    def test_a(self): pass\n" +
    `    if ${cond}:\n` +
    "        def test_b(self): pass\n" +
    "class M(unittest.TestCase):\n" +
    "    def test_m(self): pass\n" +
    "unittest.main()\n";
  const list = `${file} run by .github/workflows/fork-tests.yml unittest`;
  for (const [cond, status, skipped] of [
    ["True", 0, []],
    ["False", 1, ["L.test_b"]],
  ]) {
    const { run, records, cleanup } = runRecording({ [file]: text(cond) }, ["--run", file], list);
    try {
      assert.equal(run.status, status, run.stdout + run.stderr);
      const r = Object.values(readRecords(records))[0].results[0];
      assert.deepEqual([r.key, r.skipped_tests], [`${file} --run only L`, skipped]);
    } finally {
      cleanup();
    }
  }
  // An own run of one test counts that test when no run loaded it, and no other.
  for (const [only, cond, status, skipped] of [
    ["L.test_b", "True", 0, []],
    ["L.test_b", "False", 1, ["L.test_b"]],
    ["L.test_a", "False", 0, []],
  ]) {
    const one = text(cond).replace("run only L\n", `run only ${only}\n`);
    const { run, records, cleanup } = runRecording({ [file]: one }, ["--run", file], list);
    try {
      assert.equal(run.status, status, only + cond + run.stdout + run.stderr);
      const r = Object.values(readRecords(records))[0].results[0];
      assert.deepEqual([r.key, r.skipped_tests], [`${file} --run only ${only}`, skipped]);
    } finally {
      cleanup();
    }
  }
});

test("the shim names no test unittest would not load, and names tests made by assignment or setattr", () => {
  const file = "fork-ci/n.test.py";
  // None of these is a test unittest loads, so none is unloaded: a helper class's test* method, a property, a name
  // set to None (to leave a base's test out), a base's method every loaded subclass replaces (the base deleted), and
  // an alias's setattr of a test that ran under its class's own name.
  const [status, r, out] = recordOf(
    file,
    "import unittest\n" +
      "class FakeServer:\n" +
      "    def test_connection(self): pass\n" +
      "class Base(unittest.TestCase):\n" +
      "    def test_a(self): pass\n" +
      "    def test_off(self): pass\n" +
      "class A(Base):\n" +
      "    def test_a(self): pass\n" +
      "    test_off = None\n" +
      "    @property\n" +
      "    def test_data(self): return 1\n" +
      "del Base\n" +
      "X = A\n" +
      "setattr(X, 'test_set', lambda self: None)\n" +
      "unittest.main()\n",
  );
  assert.equal(status, 0, out);
  // A.test_a and A.test_set, twice: unittest loads the alias X as a class of its own.
  assert.deepEqual([r.tests, r.skipped, r.unloaded], [4, 0, []]);
  // Each of these is a test that did not run, so it is named: a test made by assignment or by setattr in a class a
  // condition left out, a mixin's test whose only class a condition left out, and a base's test one loaded subclass
  // does not replace, which a load_tests left out.
  const [status2, r2, out2] = recordOf(
    file,
    "import unittest\n" +
      "def make(): return lambda self: None\n" +
      "class Mixin:\n" +
      "    def test_m(self): pass\n" +
      "if False:\n" +
      "    class Gone(unittest.TestCase):\n" +
      "        test_made = make()\n" +
      "    setattr(Gone, 'test_set', make())\n" +
      "    class WithMixin(Mixin, unittest.TestCase): pass\n" +
      "class Base(unittest.TestCase):\n" +
      "    def test_kept(self): pass\n" +
      "class A(Base):\n" +
      "    def test_a(self): pass\n" +
      "class B(Base):\n" +
      "    def test_kept(self): pass\n" +
      "del Base\n" +
      "def load_tests(loader, tests, pattern):\n" +
      "    return unittest.TestSuite([A('test_a'), B('test_kept')])\n" +
      "unittest.main()\n",
  );
  assert.equal(status2, 1, out2);
  assert.deepEqual(r2.unloaded, [
    "Base.test_kept",
    "Gone.test_made",
    "Gone.test_set",
    "Mixin.test_m",
  ]);
  assert.match(out2, /give it a name that does not start with "test"/);
});

test("a child made by os.fork writes no shim line, so its end line never stands in for its parent's", () => {
  // After the parent's run, a child leaves through the shim's end (sys.exit); the parent then stops hard: no end line.
  const [status, r, out] = recordOf(
    "fork-ci/f.test.py",
    "import os, sys, unittest\n" +
      "class T(unittest.TestCase):\n" +
      "    def test_a(self): pass\n" +
      "unittest.main(exit=False)\n" +
      "pid = os.fork()\n" +
      "if pid == 0:\n" +
      "    sys.exit(0)\n" +
      "os.waitpid(pid, 0)\n" +
      "sys.stdout.flush()\n" +
      "os._exit(0)\n",
  );
  assert.equal(status, 1, out);
  assert.deepEqual([r.format, r.tests], ["unittest", null]);
  assert.match(r.note, /the file never reached its end/);
});

test("the shim counts an expected failure that passed as a failure, and names a loaded module's tests by module", () => {
  const [status, r] = recordOf(
    "fork-ci/x.test.py",
    "import unittest\n" +
      "class T(unittest.TestCase):\n" +
      "    @unittest.expectedFailure\n" +
      "    def test_a(self): pass\n" +
      "unittest.main()\n",
  );
  assert.equal(status, 1);
  assert.deepEqual([r.tests, r.failed], [1, 1]);
  // Two modules with the same class and test, one skipped: the record says which.
  const mod = (skip) =>
    "import unittest\nclass T(unittest.TestCase):\n" +
    (skip ? "    @unittest.skip('off')\n" : "") +
    "    def test_x(self): pass\n";
  const [status2, r2, out2] = recordOf(
    "fork-ci/m.test.py",
    "# fork-ci: may skip mod_b.T.test_x\n" +
      "import sys, unittest\nsys.path.insert(0, __import__('os').path.dirname(__file__))\n" +
      "import mod_a, mod_b\n" +
      "load = unittest.defaultTestLoader.loadTestsFromModule\n" +
      "ok = unittest.TextTestRunner().run(unittest.TestSuite([load(mod_a), load(mod_b)])).wasSuccessful()\n" +
      "raise SystemExit(0 if ok else 1)\n",
    { "fork-ci/mod_a.py": mod(false), "fork-ci/mod_b.py": mod(true) },
  );
  assert.equal(status2, 0, out2);
  assert.deepEqual([r2.tests, r2.skipped_tests], [2, ["mod_b.T.test_x"]]);
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
    { "fork-ci/q.test.sh": 'echo "ok q"\n' + END(1) },
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
  const tests = { "fork-ci/a.test.sh": 'echo "ok a"\n' + END(1) };
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
  'echo "ok switched"\n' +
  END(1);

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
      [["fork-ci/own.test.sh --run", true, ["OWN_TESTS=1"], 0, 1, 0, []]],
    );
  } finally {
    cleanup();
  }
});

test("--run of a listed fork test fork-tests.yml runs, with no run line, is one plain run", () => {
  const tests = { "fork-ci/plain.test.sh": 'echo "ok plain"\n' + END(1) };
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
  // A may-skip line is for its run in a part; an own run may skip none whatever its lines say.
  const skipping = "# fork-ci: run with OWN_TESTS=1\n# fork-ci: may skip T.test_b\n" + SKIPPING_PY;
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
    "fork-ci/skip.test.py unittest",
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
    assert.match(
      run.stdout,
      /fork-ci\/skip\.test\.py --run skipped T\.test_b, which it does not declare/,
    );
    const r = Object.values(readRecords(records))[0].results[0]; // recorded all the same
    assert.deepEqual([r.skipped, r.skipped_tests, r.allowed], [1, ["T.test_b"], []]);
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
