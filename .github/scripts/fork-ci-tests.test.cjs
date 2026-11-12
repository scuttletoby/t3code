const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { plan, readList, findTests } = require("./fork-ci-tests.cjs");

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
  const result = plan(here("a.test.sh", "gone.test.sh"), ["a.test.sh"], (f) => f === "gone.test.sh");
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
    assert.deepEqual(findTests("s", root), ["s/a.test.cjs", "s/sub/c.test.mjs", "s/sub/deep/d.test.ts"]);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
