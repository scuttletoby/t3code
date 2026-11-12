const assert = require("node:assert/strict");
const test = require("node:test");

const { parseJobs, compare } = require("./fork-tests-drift.cjs");

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
