const assert = require("node:assert/strict");
const test = require("node:test");

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  parseJobs,
  compare,
  record,
  current,
  drift,
  stepsOf,
  GROUPS,
} = require("./fork-tests-drift.cjs");

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

// A temporary repository root with the given files; returns its path.
function tree(files) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-drift-"));
  for (const [file, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
    fs.writeFileSync(path.join(dir, file), text);
  }
  return dir;
}

const release =
  "jobs:\n" +
  ["quality", "test", "test_web", "test_server"]
    .map((job) => `  ${job}:\n    steps:\n      - name: Run\n        run: echo ${job}\n`)
    .join("");

const windows = `on:
  workflow_dispatch:
    inputs:
      package:
        default: ""
permissions:
  contents: read
jobs:
  test:
    runs-on: blacksmith-8vcpu-windows-2025
    steps:
      - name: Checkout
        uses: actions/checkout@abc # v6.1.0
        with:
          name: not-a-step-key
      - name: "Test"
        run: vp test
`;

const copy = `jobs:
  drift:
    steps:
      - run: node .github/scripts/fork-tests-drift.cjs --windows
  test:
    runs-on: windows-2025
    steps:
      - name: Checkout
        uses: actions/checkout@abc
      - name: Test
        shell: pwsh
        run: vp test
`;

// Records each group of a tree, as `--record` would.
function recordAll(dir) {
  for (const group of Object.values(GROUPS))
    fs.writeFileSync(path.join(dir, group.record), JSON.stringify(current(group, dir).now));
}

test("Fork tests' drift check (the merge path) never reads upstream's windows-tests.yml", () => {
  assert.ok(!(GROUPS.windows.sameSteps.file in GROUPS.tests.tracked));
  assert.ok(GROUPS.windows.sameSteps.file in GROUPS.windows.tracked);
  const dir = tree({
    ".github/workflows/ci.yml": base,
    ".github/workflows/release.yml": release,
    ".github/workflows/windows-tests.yml": windows,
    ".github/workflows/fork-windows-tests.yml": copy,
  });
  try {
    recordAll(dir);
    assert.deepEqual(drift(GROUPS.tests, dir), []);
    assert.deepEqual(drift(GROUPS.windows, dir), []);
    // Upstream changes its Windows lane: only the Windows report goes red.
    const upstream = path.join(dir, ".github/workflows/windows-tests.yml");
    fs.writeFileSync(upstream, windows.replace("vp test", "vp test --windows"));
    assert.deepEqual(drift(GROUPS.tests, dir), []);
    assert.deepEqual(drift(GROUPS.windows, dir), [
      '.github/workflows/windows-tests.yml: job `test`: step "Test" changed',
    ]);
    // Upstream removes it: a clear line, not a crash, and still nothing in Fork tests.
    fs.rmSync(upstream);
    assert.deepEqual(drift(GROUPS.tests, dir), []);
    assert.deepEqual(drift(GROUPS.windows, dir), [
      ".github/workflows/windows-tests.yml: removed upstream: port what replaced it into " +
        "fork-windows-tests.yml or drop the copy, then update GROUPS in fork-tests-drift.cjs",
    ]);
    assert.deepEqual(current(GROUPS.windows, dir).removed, [".github/workflows/windows-tests.yml"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("the Windows group tracks upstream's \`on:\` (its inputs say what the Test step runs)", () => {
  const top = (text) => ({ "w.yml": parseJobs(text, GROUPS.windows.topLevel) });
  assert.deepEqual(
    compare(top(windows), top(windows.replace('default: ""', "default: apps/web"))),
    ["w.yml: top-level `on:` changed"],
  );
  assert.ok(!GROUPS.tests.topLevel.includes("on"));
});

test("the Windows copy keeps upstream's step names and pinned actions, in order", () => {
  assert.deepEqual(stepsOf(windows, "test"), [
    ["Checkout", "actions/checkout@abc"],
    ["Test", null],
  ]);
  const dir = tree({
    ".github/workflows/ci.yml": base,
    ".github/workflows/release.yml": release,
    ".github/workflows/windows-tests.yml": windows,
    ".github/workflows/fork-windows-tests.yml": copy.replace("checkout@abc", "checkout@v6"),
  });
  try {
    recordAll(dir);
    const [line, ...rest] = drift(GROUPS.windows, dir);
    assert.deepEqual(rest, []);
    assert.match(
      line,
      /^fork-windows-tests\.yml: job `test`: steps \(names, pinned actions\) differ/,
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
