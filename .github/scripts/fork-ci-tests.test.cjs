const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
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
      "          cd x && node --test commented.test.cjs\n",
    ".github/workflows/ci.yml": "run: node --test x/upstream.test.cjs",
    "scripts/smoke.ts": '// x/commented-ts.test.cjs\nrun(["--test", "x/named-ts.test.cjs"]);\n',
  };
  const entries = [
    { file: "x/named.test.cjs", runBy: ".github/workflows/fork-tests.yml" },
    { file: "x/unnamed.test.cjs", runBy: ".github/workflows/fork-tests.yml" },
    { file: "x/upstream.test.cjs", runBy: ".github/workflows/ci.yml" },
    { file: "x/nowhere.test.cjs", runBy: "scripts/missing.ts" },
    // Named only in a YAML or shell comment (the run goes there by `cd`): not run (#646).
    { file: "x/commented.test.cjs", runBy: ".github/workflows/fork-tests.yml" },
    { file: "x/commented-ts.test.cjs", runBy: "scripts/smoke.ts" },
    { file: "x/named-ts.test.cjs", runBy: "scripts/smoke.ts" },
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
  ]);
  assert.deepEqual(
    result.notRun.map((n) => n.file),
    [
      "x/unnamed.test.cjs",
      "x/upstream.test.cjs",
      "x/nowhere.test.cjs",
      "x/commented.test.cjs",
      "x/commented-ts.test.cjs",
    ],
  );
  assert.match(result.notRun[1].why, /upstream workflow/);
  assert.match(
    result.notRun[3].why,
    /does not name x\/commented\.test\.cjs \(a comment does not count\)/,
  );
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

// The arguments of each `node .github/scripts/fork-ci-tests.cjs ...` call in a workflow's text.
const runnerCalls = (text) =>
  text
    .split("\n")
    .filter((line) => !/^\s*#/.test(line))
    .flatMap((line) => [
      ...line.matchAll(/\bnode\s+\.github\/scripts\/fork-ci-tests\.cjs((?:[ \t]+[^\s#;&|]+)*)/g),
    ])
    .map((match) =>
      [...match[1].matchAll(/'([^']*)'|"([^"]*)"|(\S+)/g)].map((a) => a[1] ?? a[2] ?? a[3]),
    );

test("reads a workflow's runner calls, quotes removed, comment lines skipped", () => {
  const text = [
    "      # node .github/scripts/fork-ci-tests.cjs commented/",
    "        run: node .github/scripts/fork-ci-tests.cjs fork-kit/",
    "          node .github/scripts/fork-ci-tests.cjs '!fork-kit/' \"!x/\" # a note",
    "          node .github/scripts/fork-ci-tests.cjs",
  ].join("\n");
  assert.deepEqual(runnerCalls(text), [["fork-kit/"], ["!fork-kit/", "!x/"], []]);
});

test("Fork tests' runner calls split every listed and present test into exactly one part", () => {
  const root = path.resolve(__dirname, "../..");
  const calls = runnerCalls(
    fs.readFileSync(path.join(root, ".github/workflows/fork-tests.yml"), "utf8"),
  );
  assert.ok(calls.length > 0, "fork-tests.yml calls fork-ci-tests.cjs");
  const listed = readList(fs.readFileSync(path.join(root, ".github/fork-ci-tests.txt"), "utf8"));
  const files = [...new Set([...listed.map(({ file }) => file), ...presentTests(root)])];
  const wrong = files
    .map((file) => [file, calls.filter((parts) => inPart(file, parts))])
    .filter(([, parts]) => parts.length !== 1)
    .map(
      ([file, parts]) =>
        `${file}: in ${parts.length} parts (${parts.map((p) => p.join(" ") || "all").join("; ")})`,
    );
  assert.deepEqual(wrong, []);
});

// The runner calls of each job in a workflow's text: a job starts at a two-space `name:` line under `jobs:`.
const callsByJob = (text) => {
  const jobs = {};
  let job = null;
  let inJobs = false;
  for (const line of text.split("\n")) {
    if (/^\S/.test(line)) inJobs = line.startsWith("jobs:");
    const head = inJobs && line.match(/^ {2}([A-Za-z_][\w-]*):\s*$/);
    if (head) job = jobs[head[1]] = [];
    else if (job) job.push(line);
  }
  return Object.fromEntries(
    Object.entries(jobs)
      .map(([name, lines]) => [name, runnerCalls(lines.join("\n"))])
      .filter(([, calls]) => calls.length > 0),
  );
};

// The composite actions' and fleet walls' tests are about two thirds of the non-kit script tests' time: in a job
// of their own, each of the three jobs stays well under its time limit. One call per job: two calls in one job run
// one after the other.
test("Fork tests run the script tests in three jobs: the kits (sharded), the actions with the walls, the rest", () => {
  const root = path.resolve(__dirname, "../..");
  const calls = callsByJob(
    fs.readFileSync(path.join(root, ".github/workflows/fork-tests.yml"), "utf8"),
  );
  assert.deepEqual(calls, {
    workflows: [["!fork-kit/", "!fork-ci/", "!.github/scripts/fork-fleet-walls"]],
    action_tests: [["fork-ci/", ".github/scripts/fork-fleet-walls"]],
    kit_tests: [["fork-kit/"]],
  });
});

test("reads each job's runner calls", () => {
  const text = [
    "on: push",
    "  a:",
    "jobs:",
    "  one:",
    "    steps:",
    "      - run: node .github/scripts/fork-ci-tests.cjs x/",
    "  two:",
    "    steps:",
    "      - run: echo",
  ].join("\n");
  assert.deepEqual(callsByJob(text), { one: [["x/"]] });
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
  assert.equal(slowNote(["e"], weights, 4, 70), null); // a minute more exactly is not over a minute (#646)
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
  delete env.FORK_CI_TEST_SHARD;
  if (extra.FORK_CI_TEST_SHARD !== undefined) env.FORK_CI_TEST_SHARD = extra.FORK_CI_TEST_SHARD;
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

// #646: main() itself prints the warning and the summary's Slow line, and neither fails the run.
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
