const assert = require("node:assert/strict");
const test = require("node:test");

const { plan, readList } = require("./fork-ci-tests.cjs");

const never = () => false;

test("runs every present test, listed or not", () => {
  assert.deepEqual(plan(["a"], ["a", "b"], never).run, ["a", "b"]);
});

test("a present test missing from the list fails", () => {
  assert.deepEqual(plan(["a"], ["a", "b"], never).unlisted, ["b"]);
});

test("a listed test never in history is pending, not a failure", () => {
  const result = plan(["a", "later"], ["a"], never);
  assert.deepEqual(result.pending, ["later"]);
  assert.deepEqual(result.deleted, []);
});

test("a listed test deleted from history fails", () => {
  const result = plan(["a", "gone"], ["a"], (file) => file === "gone");
  assert.deepEqual(result.deleted, ["gone"]);
  assert.deepEqual(result.pending, []);
});

test("the list ignores comments and blank lines", () => {
  assert.deepEqual(readList("# head\n\na.sh  # from #2\n  b.py\n"), ["a.sh", "b.py"]);
});
