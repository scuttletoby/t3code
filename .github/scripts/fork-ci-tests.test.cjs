const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  plan,
  readList,
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
    ".github/workflows/fork-tests.yml": "run: node --test x/named.test.cjs",
    ".github/workflows/ci.yml": "run: node --test x/upstream.test.cjs",
  };
  const entries = [
    { file: "x/named.test.cjs", runBy: ".github/workflows/fork-tests.yml" },
    { file: "x/unnamed.test.cjs", runBy: ".github/workflows/fork-tests.yml" },
    { file: "x/upstream.test.cjs", runBy: ".github/workflows/ci.yml" },
    { file: "x/nowhere.test.cjs", runBy: "scripts/missing.ts" },
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
  ]);
  assert.deepEqual(
    result.notRun.map((n) => n.file),
    ["x/unnamed.test.cjs", "x/upstream.test.cjs", "x/nowhere.test.cjs"],
  );
  assert.match(result.notRun[1].why, /upstream workflow/);
});

test("the list ignores comments and blank lines and reads `run by`", () => {
  assert.deepEqual(readList("# head\n\na.test.sh  # from #2\n  b.test.py run by c.yml\n"), [
    { file: "a.test.sh", runBy: null },
    { file: "b.test.py", runBy: "c.yml" },
  ]);
  assert.throws(() => readList("a.test.sh run c.yml\n"), /cannot read/);
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
  let running = 0;
  let most = 0;
  const started = [];
  const results = await runAll([30, 5, 5, 5, 5], 2, async (ms) => {
    started.push(ms);
    running += 1;
    most = Math.max(most, running);
    await new Promise((resolve) => setTimeout(resolve, ms));
    running -= 1;
    return ms * 2;
  });
  assert.equal(most, 2);
  assert.deepEqual(results, [60, 10, 10, 10, 10]);
  assert.equal(started.length, 5);
  assert.deepEqual(await runAll([], 4, async () => 1), []);
});

test("jobCount reads FORK_CI_TEST_JOBS, else the CPU count", () => {
  assert.equal(jobCount({ FORK_CI_TEST_JOBS: "3" }), 3);
  for (const bad of [undefined, "", "0", "-2", "1.5", "many"])
    assert.equal(jobCount({ FORK_CI_TEST_JOBS: bad }), os.availableParallelism(), String(bad));
});

// Runs the real script in a scratch tree whose list names `tests` ({ file: shell text }).
function runScript(tests, jobs) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-tests-"));
  const write = (file, text) => {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.writeFileSync(path.join(root, file), text);
  };
  write(
    ".github/scripts/fork-ci-tests.cjs",
    fs.readFileSync(require.resolve("./fork-ci-tests.cjs")),
  );
  write(".github/fork-ci-tests.txt", Object.keys(tests).join("\n") + "\n");
  for (const [file, text] of Object.entries(tests)) write(file, text);
  const env = {
    ...process.env,
    FORK_CI_TEST_JOBS: String(jobs),
    LEFT: path.join(root, "left.pid"),
  };
  delete env.GITHUB_STEP_SUMMARY;
  const started = Date.now();
  const run = childProcess.spawnSync("node", [".github/scripts/fork-ci-tests.cjs"], {
    cwd: root,
    env,
    encoding: "utf8",
    timeout: 60000,
  });
  return { root, run, seconds: (Date.now() - started) / 1000 };
}

test("test files run in parallel, each one's output in its own group, and a failure fails the run", () => {
  const { root, run, seconds } = runScript(
    {
      "fork-ci/a.test.sh": "echo a-start; sleep 2; echo a-end\n",
      "fork-ci/b.test.sh": "echo b-start; sleep 2; echo b-end\n",
      "fork-ci/c.test.sh": "echo c-start; sleep 2; echo c-end; exit 3\n",
    },
    3,
  );
  try {
    assert.equal(run.status, 1, run.stdout + run.stderr);
    // One after another they take 6 s.
    assert.ok(seconds < 5, `took ${seconds} s`);
    for (const name of ["a", "b", "c"])
      assert.match(
        run.stdout,
        new RegExp(
          `::group::fork-ci/${name}\\.test\\.sh \\([0-9.]+ s\\)\\n${name}-start\\n${name}-end\\n`,
        ),
      );
    assert.match(
      run.stdout,
      /::error file=fork-ci\/c\.test\.sh::fork-ci\/c\.test\.sh failed \(exit 3\)/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a process a test leaves behind holding its output does not hang the run", () => {
  const { root, run, seconds } = runScript(
    { "fork-ci/left.test.sh": 'sleep 60 & echo $! > "$LEFT"; echo done\n' },
    1,
  );
  try {
    assert.equal(run.error, undefined, String(run.error));
    assert.equal(run.status, 0, run.stdout + run.stderr);
    assert.ok(seconds < 20, `took ${seconds} s`);
    assert.match(run.stdout, /::group::fork-ci\/left\.test\.sh[^\n]*\ndone\n/);
  } finally {
    try {
      process.kill(Number(fs.readFileSync(path.join(root, "left.pid"), "utf8")));
    } catch {}
    fs.rmSync(root, { recursive: true, force: true });
  }
});
