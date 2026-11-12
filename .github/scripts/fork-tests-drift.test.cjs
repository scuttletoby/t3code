const assert = require("node:assert/strict");
const test = require("node:test");

const { parseJobs, compare, record } = require("./fork-tests-drift.cjs");

const base = `name: CI
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      # comment
      - name: Checkout
        uses: actions/checkout@v6
        with:
          sparse-checkout: |
            /*
      - uses: ./.github/actions/setup
      - name: Check
        run: |
          vp check
  test:
    needs: [lint]
    strategy:
      matrix:
        shard: [1, 2]
    steps:
      - name: Test
        run: vp test
`;

const wrap = (text) => ({ "ci.yml": parseJobs(text) });

test("parses jobs and steps, ignoring comments and blank lines", () => {
  const jobs = parseJobs(base);
  assert.deepEqual(Object.keys(jobs), ["lint", "test"]);
  assert.deepEqual(
    jobs.lint.steps.map((s) => s.name),
    ["Checkout", "./.github/actions/setup", "Check"],
  );
  const noisy = base.replace("      - name: Check", "\n      # new comment\n      - name: Check");
  assert.deepEqual(compare(wrap(base), wrap(noisy)), []);
});

test("reports added, removed, changed and reordered steps and jobs", () => {
  const changed = base
    .replace("vp check", "vp check --fix")
    .replace("      - uses: ./.github/actions/setup\n", "")
    .replace("shard: [1, 2]", "shard: [1, 2, 3]")
    .concat("  rust:\n    steps:\n      - name: Cargo\n        run: cargo test\n");
  assert.deepEqual(compare(wrap(base), wrap(changed)), [
    'ci.yml: job `lint`: step "Check" changed',
    'ci.yml: job `lint`: step "./.github/actions/setup" removed',
    "ci.yml: job `test`: settings changed (runs-on, if, needs, matrix, env...)",
    "ci.yml: job `rust` added upstream",
  ]);
  const reordered = base.replace(
    "      - uses: ./.github/actions/setup\n      - name: Check\n        run: |\n          vp check\n",
    "      - name: Check\n        run: |\n          vp check\n      - uses: ./.github/actions/setup\n",
  );
  assert.deepEqual(compare(wrap(base), wrap(reordered)), ["ci.yml: job `lint`: steps reordered"]);
});

test("repeated step names stay distinct", () => {
  const twice = base.replace("  test:", "      - name: Check\n        run: echo again\n  test:");
  const names = parseJobs(twice).lint.steps.map((s) => s.name);
  assert.deepEqual(names.slice(-2), ["Check", "Check #2"]);
});

test("top-level env, defaults, concurrency and permissions count, wherever they are", () => {
  const changed = `env:\n  FOO: bar\n${base}defaults:\n  run:\n    shell: bash\n`;
  assert.deepEqual(compare(wrap(base), wrap(changed)), [
    "ci.yml: top-level `env:` added",
    "ci.yml: top-level `defaults:` added",
  ]);
  const withPerms = base.replace("jobs:", "permissions:\n  contents: read\njobs:");
  assert.deepEqual(compare(wrap(withPerms), wrap(withPerms.replace(": read", ": write"))), [
    "ci.yml: top-level `permissions:` changed",
  ]);
  assert.deepEqual(compare(wrap(withPerms), wrap(base)), [
    "ci.yml: top-level `permissions:` removed",
  ]);
  // `on:` and `name:` are not digested.
  assert.deepEqual(compare(wrap(base), wrap(base.replace("on: push", "on: pull_request"))), []);
});

test("a tracked job missing on both sides is reported, not a crash", () => {
  const recorded = { "release.yml": { test_web: null } };
  assert.deepEqual(compare(recorded, { "release.yml": {} }), [
    "release.yml: job `test_web` missing upstream (renamed or removed?)",
  ]);
});

test("--record refuses while a tracked job is missing and writes nothing", () => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-drift-"));
  const file = path.join(dir, "record.json");
  const error = console.error;
  const log = console.log;
  const said = [];
  console.error = console.log = (line) => said.push(line);
  try {
    record(wrap(base), ["release.yml: job `test_web`"], file);
    assert.equal(process.exitCode, 1);
    assert.equal(fs.existsSync(file), false);
    assert.match(said.join("\n"), /Not recorded: tracked jobs missing upstream/);
    process.exitCode = undefined;
    said.length = 0;
    record(wrap(base), [], file);
    assert.equal(process.exitCode, undefined);
    assert.deepEqual(JSON.parse(fs.readFileSync(file, "utf8")), wrap(base));
    assert.match(said.join("\n"), /ci.yml: job `lint` added upstream[\s\S]*what was ported/);
  } finally {
    console.error = error;
    console.log = log;
    process.exitCode = undefined;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
