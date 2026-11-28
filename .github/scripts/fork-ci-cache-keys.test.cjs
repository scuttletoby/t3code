const assert = require("node:assert/strict");
const test = require("node:test");

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  FAMILIES,
  parseYaml,
  cacheSteps,
  check,
  forkWorkflows,
  main,
} = require("./fork-ci-cache-keys.cjs");

const LOCK = "${{ hashFiles('pnpm-lock.yaml') }}";
const SHA = "${{ github.sha }}";

// One job with a restore, the test, and a push-only save of Vitest's module cache, as upstream's test_server.
function workflow({
  restoreUses = "actions/cache/restore@abc # v6",
  key = `vitest-modules-web-\${{ matrix.shard }}-\${{ runner.os }}-${LOCK}-${SHA}`,
  restoreKeys = `vitest-modules-web-\${{ matrix.shard }}-\${{ runner.os }}-${LOCK}-`,
  saveIf = "github.event_name == 'push'",
  saveKey = key,
  savePath = "node_modules/.vitest-cache",
} = {}) {
  return `name: Fork tests
on: push
jobs:
  test_web:
    needs:
      [
        changes,
        build,
      ]
    strategy:
      matrix:
        shard: [1, 2, 3]
    steps:
      # comment
      - name: Checkout
        uses: actions/checkout@abc # v6
        with:
          sparse-checkout: |
            /*
            !/.repos/
      - name: Restore Vitest module cache
        id: vitest
        uses: ${restoreUses}
        with:
          path: node_modules/.vitest-cache
          key: ${key}
          restore-keys: |
            ${restoreKeys}
      - name: Test
        run: >-
          vp test run
          --shard \${{ matrix.shard }}
      - name: Save Vitest module cache
        if: ${saveIf}
        uses: actions/cache/save@abc # v6
        with:
          path: ${savePath}
          key: ${saveKey}
`;
}

test("parseYaml reads mappings, step lists, block scalars, flow lists over lines and comments", () => {
  const parsed = parseYaml(workflow());
  const job = parsed.jobs.test_web;
  assert.equal(job.needs, "[changes,build,]");
  assert.equal(job.strategy.matrix.shard, "[1, 2, 3]");
  assert.equal(job.steps.length, 4);
  assert.equal(job.steps[0].uses, "actions/checkout@abc");
  assert.equal(job.steps[0].with["sparse-checkout"], "/*\n!/.repos/");
  assert.equal(job.steps[2].run, "vp test run --shard ${{ matrix.shard }}");
  assert.equal(job.steps[3].if, "github.event_name == 'push'");
  assert.deepEqual(parseYaml("a: 'it''s'\nb: \"x # y\"\nc: plain # note\n"), {
    a: "it's",
    b: "x # y",
    c: "plain",
  });
});

test("cacheSteps finds each cache step with its kind, path, key and restore keys", () => {
  const steps = cacheSteps(workflow());
  assert.deepEqual(
    steps.map((step) => [step.job, step.index, step.kind, step.path, step.restoreKeys.length]),
    [
      ["test_web", 1, "restore", "node_modules/.vitest-cache", 1],
      ["test_web", 3, "save", "node_modules/.vitest-cache", 0],
    ],
  );
});

test("a module cache keyed by lockfile and commit, restored by lockfile, saved on push, passes", () => {
  assert.deepEqual(check(workflow()), []);
});

test("a key without the lockfile's hash fails, and so does a restore key that drops it", () => {
  const noLock = check(
    workflow({
      key: `vitest-modules-web-\${{ runner.os }}-${SHA}`,
      restoreKeys: "vitest-modules-web-${{ runner.os }}-",
      saveKey: `vitest-modules-web-\${{ runner.os }}-${SHA}`,
    }),
  );
  assert.ok(
    noLock.some((problem) => /does not hash pnpm-lock\.yaml/.test(problem)),
    noLock.join("\n"),
  );

  const widened = check(
    workflow({ restoreKeys: "vitest-modules-web-${{ matrix.shard }}-${{ runner.os }}-" }),
  );
  assert.ok(
    widened.some((problem) => /drops hashFiles\('pnpm-lock\.yaml'\)/.test(problem)),
    widened.join("\n"),
  );
});

test("a restore key that is not a prefix of the key fails", () => {
  const problems = check(workflow({ restoreKeys: `vitest-modules-other-${LOCK}-` }));
  assert.ok(
    problems.some((problem) => /is not a prefix of the key/.test(problem)),
    problems.join("\n"),
  );
});

test("a key without the commit at its end fails: one entry per commit", () => {
  const key = `vitest-modules-web-\${{ matrix.shard }}-\${{ runner.os }}-${LOCK}`;
  const problems = check(workflow({ key, saveKey: key }));
  assert.ok(
    problems.some((problem) => /must end with \$\{\{ github\.sha \}\}/.test(problem)),
    problems.join("\n"),
  );
});

test("a push-only cache saved by pull requests, after a failure, or by actions/cache fails", () => {
  for (const saveIf of ["always()", "github.event_name == 'pull_request'", "${{ !cancelled() }}"]) {
    const problems = check(workflow({ saveIf }));
    assert.ok(
      problems.some((problem) => /only on a push/.test(problem)),
      `${saveIf}: ${problems.join("\n")}`,
    );
  }
  for (const saveIf of [
    "always() && github.event_name == 'push'",
    "failure() || github.event_name == 'push'",
  ]) {
    const problems = check(workflow({ saveIf }));
    assert.ok(
      problems.some((problem) => /every step before it passed/.test(problem)),
      `${saveIf}: ${problems.join("\n")}`,
    );
  }
  const both = check(workflow({ restoreUses: "actions/cache@abc # v6" }));
  assert.ok(
    both.some((problem) => /restores with actions\/cache\/restore/.test(problem)),
    both.join("\n"),
  );
});

test("a save of a key or path no earlier step restores fails; one from the restore's cache-primary-key passes", () => {
  const otherKey = check(workflow({ saveKey: `vitest-modules-web-x-${LOCK}-${SHA}` }));
  assert.ok(
    otherKey.some((problem) => /no earlier step of this job restores/.test(problem)),
    otherKey.join("\n"),
  );
  const otherPath = check(workflow({ savePath: "node_modules/.vite" }));
  assert.ok(
    otherPath.some((problem) => /no earlier step of this job restores/.test(problem)),
    otherPath.join("\n"),
  );

  assert.deepEqual(
    check(workflow({ saveKey: "${{ steps.vitest.outputs.cache-primary-key }}" })),
    [],
  );
  const unknown = check(workflow({ saveKey: "${{ steps.nothing.outputs.cache-primary-key }}" }));
  assert.ok(
    unknown.some((problem) => /no earlier restore in this job/.test(problem)),
    unknown.join("\n"),
  );
});

test("cargo's cache is exact: its inputs only, no restore keys, saved on a push when the restore missed", () => {
  const key =
    "cargo-v1-${{ runner.os }}-rustc${{ steps.rustc.outputs.version }}-${{ hashFiles('native/*/Cargo.lock') }}";
  const pushMiss = "github.event_name == 'push' && steps.cargo.outputs.cache-hit != 'true'";
  const cargo = ({ restoreKey = "", saveIf = pushMiss, keyOf = key } = {}) =>
    [
      "jobs:",
      "  rust:",
      "    steps:",
      "      - name: Restore Cargo cache",
      "        id: cargo",
      "        uses: actions/cache/restore@abc",
      "        with:",
      "          path: |",
      "            ~/.cargo/registry/cache/",
      "            native/resource-monitor/target",
      `          key: ${keyOf}`,
      ...(restoreKey ? [`          restore-keys: ${restoreKey}`] : []),
      "      - name: Test",
      "        run: cargo test --locked",
      "      - name: Save Cargo cache",
      `        if: ${saveIf}`,
      "        uses: actions/cache/save@abc",
      "        with:",
      "          path: |",
      "            ~/.cargo/registry/cache/",
      "            native/resource-monitor/target",
      "          key: ${{ steps.cargo.outputs.cache-primary-key }}",
      "",
    ].join("\n");
  const expectProblem = (text, pattern) => {
    const problems = check(text);
    assert.ok(
      problems.some((problem) => pattern.test(problem)),
      problems.join("\n"),
    );
  };
  assert.deepEqual(check(cargo()), []);
  expectProblem(cargo({ restoreKey: "cargo-v1-${{ runner.os }}-" }), /has no restore keys/);
  expectProblem(cargo({ saveIf: "github.event_name == 'push'" }), /only when its restore missed/);
  expectProblem(cargo({ saveIf: "steps.cargo.outputs.cache-hit != 'true'" }), /only on a push/);
  expectProblem(cargo({ keyOf: `${key}-${SHA}` }), /names only its inputs/);
  expectProblem(cargo({ keyOf: `cargo-v1-${LOCK}` }), /does not hash Cargo\.lock/);
  // Any path to the lockfile counts.
  assert.deepEqual(check(cargo({ keyOf: "cargo-v1-${{ hashFiles('Cargo.lock') }}" })), []);
});

test("a key of no known family fails closed; a download must name its version", () => {
  const unknown = check(
    workflow({
      key: `turbo-${LOCK}-${SHA}`,
      saveKey: `turbo-${LOCK}-${SHA}`,
      restoreKeys: `turbo-${LOCK}-`,
    }),
  );
  assert.ok(
    unknown.some((problem) => /no cache family/.test(problem)),
    unknown.join("\n"),
  );

  const download = (key) =>
    `jobs:\n  lint:\n    steps:\n      - name: Cache Electron download\n        uses: actions/cache@abc\n        with:\n          path: ~/.cache/electron\n          key: ${key}\n`;
  assert.deepEqual(
    check(download("electron-${{ runner.os }}-${{ hashFiles('electron/package.json') }}")),
    [],
  );
  const unversioned = check(download("electron-Linux"));
  assert.ok(
    unversioned.some((problem) => /must name the version/.test(problem)),
    unversioned.join("\n"),
  );
});

test("every family has a known kind, and output families name their lockfiles", () => {
  for (const family of FAMILIES) {
    assert.ok(["download", "output"].includes(family.kind), family.prefix);
    if (family.kind === "output") assert.ok(family.lockfiles?.length, family.prefix);
  }
});

test("the fork's own workflows keep the rules, and the reader still sees their cache steps", () => {
  const files = forkWorkflows();
  assert.ok(files.some((file) => file.endsWith("fork-tests.yml")));
  const steps = files.flatMap((file) => cacheSteps(fs.readFileSync(file, "utf8")));
  // Upstream's push-only Vitest module cache of the Test Server shards, at least.
  assert.ok(
    steps.some(
      (step) =>
        step.job === "test_server" &&
        step.kind === "save" &&
        step.key.startsWith("vitest-modules-"),
    ),
  );
  const log = [];
  const original = console.log;
  console.log = (line) => log.push(line);
  try {
    assert.equal(main([]), 0, log.join("\n"));
  } finally {
    console.log = original;
  }
});

test("main prints one ::error:: line per problem and returns 1", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-ci-cache-keys-"));
  try {
    const file = path.join(dir, "fork-bad.yml");
    fs.writeFileSync(file, workflow({ saveIf: "always()" }));
    const log = [];
    const original = console.log;
    console.log = (line) => log.push(line);
    try {
      assert.equal(main([file]), 1);
    } finally {
      console.log = original;
    }
    assert.ok(log.length >= 1 && log.every((line) => line.startsWith("::error::")), log.join("\n"));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("Test, Test Web, Test Server and Rust restore their build output caches and save them only on a push", () => {
  const steps = cacheSteps(
    fs.readFileSync(path.join(__dirname, "../workflows/fork-tests.yml"), "utf8"),
  );
  const want = {
    test: { prefix: "vitest-modules-test-", path: "node_modules/.vitest-cache" },
    test_web: { prefix: "vitest-modules-web-", path: "node_modules/.vitest-cache" },
    test_server: { prefix: "vitest-modules-server-", path: "node_modules/.vitest-cache" },
    rust: { prefix: "cargo-", path: "native/resource-monitor/target" },
  };
  for (const [job, { prefix, path: cachePath }] of Object.entries(want)) {
    const restore = steps.find(
      (step) => step.job === job && step.kind === "restore" && step.key.startsWith(prefix),
    );
    const save = steps.find(
      (step) =>
        step.job === job &&
        step.kind === "save" &&
        (step.key.startsWith(prefix) ||
          step.key.includes(`steps.${restore?.id}.outputs.cache-primary-key`)),
    );
    assert.ok(
      restore && save && restore.index < save.index,
      `${job}: a restore of ${prefix} and a later save`,
    );
    assert.ok(restore.path.split("\n").includes(cachePath), `${job}: restores ${cachePath}`);
    assert.match(save.if, /github\.event_name == 'push'/, `${job}: saves only on a push`);
  }
});
