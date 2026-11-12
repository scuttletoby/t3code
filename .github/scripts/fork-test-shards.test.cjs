const assert = require("node:assert/strict");
const test = require("node:test");

const { assign, SPLIT_AT } = require("./fork-test-shards.cjs");

const pkg = (name, weight, splittable = true) => ({
  name,
  dir: `packages/${name}`,
  weight,
  splittable,
});
const whole = (shards) => shards.map((shard) => shard.whole.map((p) => p.name));
const split = (shards) => shards.map((shard) => shard.split.map((p) => p.name));

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
