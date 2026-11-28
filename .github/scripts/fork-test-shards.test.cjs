const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const test = require("node:test");

const {
  assign,
  weigh,
  readTable,
  vitestSlices,
  specPath,
  outsideTests,
  DEFAULT_MODEL,
  SPLIT_AT,
} = require("./fork-test-shards.cjs");

const pkg = (name, weight, splittable = true, files = weight) => ({
  name,
  dir: `packages/${name}`,
  files,
  weight,
  splittable,
});
const whole = (shards) => shards.map((shard) => shard.whole.map((p) => p.name));
const split = (shards) => shards.map((shard) => shard.split.map((p) => p.name));
const source = (name, count) => ({
  name,
  dir: `packages/${name}`,
  splittable: true,
  testFiles: Array.from({ length: count }, (_, i) => `packages/${name}/src/f${i}.test.ts`),
});
const table = (files, model) => JSON.stringify({ unit: "seconds", runs: ["1"], model, files });
const UNIT = {
  whole: { perFile: 0, perSecond: 1 },
  slice: { perFile: 0, perSecond: 1 },
};

test("places the heaviest package first on the lightest shard", () => {
  const packages = [pkg("small", 1), pkg("big", 10), pkg("mid", 6), pkg("mid2", 5)];
  assert.deepEqual(whole(assign(packages, 2)), [
    ["big", "small"],
    ["mid", "mid2"],
  ]);
});

test("ties break by name, so every shard computes the same split", () => {
  const packages = [pkg("b", 2), pkg("a", 2), pkg("c", 2)];
  assert.deepEqual(whole(assign(packages, 2)), [["a", "c"], ["b"]]);
  assert.deepEqual(whole(assign(packages.toReversed(), 2)), [["a", "c"], ["b"]]);
});

test("a large vitest package is sliced on every shard, the rest stay whole", () => {
  const packages = [pkg("large", SPLIT_AT + 50), pkg("script", SPLIT_AT, false), pkg("a", 3)];
  const shards = assign(packages, 2);
  assert.deepEqual(split(shards), [["large"], ["large"]]);
  assert.deepEqual(whole(shards), [["script"], ["a"]]);
});

test("every package lands on exactly one shard, and spare shards stay empty", () => {
  assert.deepEqual(whole(assign([pkg("only", 3)], 3)), [["only"], [], []]);
});

// Q: is the split by seconds the same on every shard, whatever order the packages come in?
test("balancing by seconds is deterministic: any input order, the same shards and loads", () => {
  const seconds = table(
    {
      "packages/a/src/f0.test.ts": 9,
      "packages/b/src/f0.test.ts": 4,
      "packages/b/src/f1.test.ts": 4,
      "packages/c/src/f0.test.ts": 3,
      "packages/d/src/f0.test.ts": 3,
    },
    UNIT,
  );
  const parsed = readTable(seconds);
  const sources = [source("a", 1), source("b", 2), source("c", 1), source("d", 1)];
  const run = (list) =>
    assign(
      list.map((p) => weigh(p, parsed, 2)),
      2,
    );
  const first = run(sources);
  assert.deepEqual(whole(first), [
    ["a", "d"],
    ["b", "c"],
  ]);
  assert.deepEqual(
    first.map((shard) => shard.weight),
    [12, 11],
  );
  assert.deepEqual(run(sources.toReversed()), first);
  assert.deepEqual(run([sources[2], sources[0], sources[3], sources[1]]), first);
});

// Q: does a test file the table has never seen count as the table's median, not 0 or 1?
test("a file missing from the table counts as the table's median", () => {
  const parsed = readTable(
    table(
      { "packages/x/src/f0.test.ts": 1, "packages/x/src/f1.test.ts": 5, "packages/y/a.test.ts": 9 },
      UNIT,
    ),
  );
  assert.equal(parsed.median, 5);
  const weighed = weigh(source("x", 3), parsed, 2);
  // f0 1 s, f1 5 s, f2 unknown: the median, 5 s.
  assert.equal(weighed.weight, 11);
  assert.equal(weighed.files, 3);
});

// Q: does the model turn test seconds into wall seconds per mode (whole vs vitest slice)?
test("whole and sliced packages use their own per-file and per-second costs", () => {
  const model = { whole: { perFile: 1, perSecond: 0.5 }, slice: { perFile: 2, perSecond: 1 } };
  const parsed = readTable(table({ "packages/x/src/f0.test.ts": 4 }, model));
  const weighed = weigh(source("x", 2), parsed, 2);
  // Whole: (1 + 0.5 * 4) + (1 + 0.5 * 4), the second file at the median 4 s.
  assert.equal(weighed.weight, 6);
  // Each slice holds one file: 2 + 1 * 4.
  assert.deepEqual(weighed.slices, [6, 6]);
});

// Q: with no table, an empty one or an old one (not in seconds), is the split exactly the old one?
test("a missing, empty, broken or old table falls back to test file counts", () => {
  for (const [label, text] of [
    ["missing", undefined],
    ["not json", "{"],
    ["empty", table({}, UNIT)],
    ["old (relative weights)", JSON.stringify({ "packages/x/src/f0.test.ts": 3 })],
    ["negative", table({ "packages/x/src/f0.test.ts": -1 }, UNIT)],
  ]) {
    const parsed = readTable(text);
    assert.ok(parsed.reason, `${label}: says why`);
    const weighed = weigh(source("x", 3), parsed, 2);
    assert.equal(weighed.weight, 3, label);
    assert.equal(weighed.slices, undefined, label);
    assert.equal(weighed.files, 3, label);
  }
  // As before: a split package's share is its file count over the shard count.
  const shards = assign([weigh(source("big", SPLIT_AT), readTable(undefined), 2)], 2);
  assert.deepEqual(
    shards.map((shard) => shard.weight),
    [SPLIT_AT / 2, SPLIT_AT / 2],
  );
});

test("a table without a usable model uses the default one", () => {
  for (const model of [undefined, { whole: { perFile: "x" } }]) {
    assert.deepEqual(readTable(table({ "a.test.ts": 1 }, model)).model, DEFAULT_MODEL);
  }
});

// Q: when a refresh could fit only one mode, is that mode kept (the other the default)?
test("a model with one usable mode keeps it and defaults the other", () => {
  const whole = { perFile: 1, perSecond: 2 };
  assert.deepEqual(readTable(table({ "a.test.ts": 1 }, { whole })).model, {
    whole,
    slice: DEFAULT_MODEL.slice,
  });
});

// Q: is drift visible: how many of a package's test files the table lacks?
test("weigh counts the package's files missing from the table", () => {
  const parsed = readTable(table({ "packages/x/src/f0.test.ts": 1 }, UNIT));
  assert.equal(weigh(source("x", 3), parsed, 2).missing, 2);
  assert.equal(weigh(source("x", 3), readTable(undefined), 2).missing, undefined);
});

// Q: are tests a package's vitest config includes from outside its folder weighed with it?
test("outsideTests finds the ../ test globs of a package's vitest config", () => {
  const config = `include: [...defaultInclude, "../../native/kde/src/*.test.ts"],
    setupFiles: ["../../packages/shared/src/testing/gitEnv.ts"],`;
  const tracked = [
    "native/kde/src/a.test.ts",
    "native/kde/src/deep/b.test.ts",
    "native/other/c.test.ts",
    "apps/desktop/src/d.test.ts",
  ];
  assert.deepEqual(outsideTests("apps/desktop", config, tracked), ["native/kde/src/a.test.ts"]);
  assert.deepEqual(outsideTests("apps/desktop", "include: []", tracked), []);
});

// Q: do our slices match vitest's --shard (sha1 of "/<path>", sorted, the remainder to the first)?
test("vitestSlices splits files as vitest's --shard does", () => {
  const files = ["a", "b", "c", "d", "e"].map((name) => `pkg/${name}.test.ts`);
  const slices = vitestSlices("pkg", files, 2);
  const bySha = files.toSorted((a, b) => {
    const [x, y] = [a, b].map((f) =>
      crypto.createHash("sha1").update(f.slice("pkg".length)).digest("hex"),
    );
    return x < y ? -1 : 1;
  });
  assert.deepEqual(slices, [bySha.slice(0, 3), bySha.slice(3)]);
  assert.deepEqual(
    vitestSlices("pkg", files, 3).map((slice) => slice.length),
    [2, 2, 1],
  );
});

// Q: does a file outside the package hash as vitest hashes it (absolute path cut at the root's length)?
test("specPath is the path vitest hashes, also for a file outside the package", () => {
  assert.equal(specPath("apps/desktop", "apps/desktop/src/a.test.ts"), "/src/a.test.ts");
  // /home/x/t3code/native/kde-snap-shot/... cut at the length of /home/x/t3code/apps/desktop.
  assert.equal(
    specPath("apps/desktop", "native/kde-snap-shot/src/activate.test.ts"),
    "nap-shot/src/activate.test.ts",
  );
});

// Q: when one vitest slice of a split package is heavier, do whole packages go to the other shard?
test("a heavy slice on one shard sends the whole packages to the other", () => {
  const big = source("big", SPLIT_AT);
  const [heavyFile] = vitestSlices(big.dir, big.testFiles, 2)[0];
  const files = Object.fromEntries(big.testFiles.map((file) => [file, 0]));
  files[heavyFile] = 50;
  files["packages/a/src/f0.test.ts"] = 1;
  files["packages/b/src/f0.test.ts"] = 1;
  const parsed = readTable(table(files, UNIT));
  const shards = assign(
    [big, source("a", 1), source("b", 1)].map((p) => weigh(p, parsed, 2)),
    2,
  );
  assert.deepEqual(split(shards), [["big"], ["big"]]);
  assert.deepEqual(whole(shards), [[], ["a", "b"]]);
});
