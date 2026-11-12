const assert = require("node:assert/strict");
const test = require("node:test");

const { assign } = require("./fork-test-shards.cjs");

const names = (shards) => shards.map((shard) => shard.packages.map((pkg) => pkg.name));

test("places the heaviest package first on the lightest shard", () => {
  const packages = [
    { name: "small", weight: 1 },
    { name: "big", weight: 10 },
    { name: "mid", weight: 6 },
    { name: "mid2", weight: 5 },
  ];
  assert.deepEqual(names(assign(packages, 2)), [
    ["big", "small"],
    ["mid", "mid2"],
  ]);
});

test("ties break by name, so every shard computes the same split", () => {
  const packages = [
    { name: "b", weight: 2 },
    { name: "a", weight: 2 },
    { name: "c", weight: 2 },
  ];
  assert.deepEqual(names(assign(packages, 2)), [["a", "c"], ["b"]]);
  assert.deepEqual(names(assign(packages.toReversed(), 2)), [["a", "c"], ["b"]]);
});

test("every package lands on exactly one shard, and spare shards stay empty", () => {
  const packages = [{ name: "only", weight: 3 }];
  assert.deepEqual(names(assign(packages, 3)), [["only"], [], []]);
});
