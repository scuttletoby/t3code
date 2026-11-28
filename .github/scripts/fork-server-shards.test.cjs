const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  readTable,
  weigh,
  split,
  parseLog,
  buildTable,
  UPSTREAM_UNRECORDED,
} = require("./fork-server-shards.cjs");

const root = path.resolve(__dirname, "../..");
const table = (files, extra = {}) =>
  JSON.stringify({ unit: "seconds", runs: ["1"], files, ...extra });
const names = (shards) => shards.map((shard) => shard.files);

test("upstream's cut: longest first, ties by name, onto the lightest (first of equals) shard", () => {
  const shards = split({ e: 2, c: 3, a: 5, d: 3, b: 4 }, 2);
  assert.deepEqual(names(shards), [
    ["a", "d"],
    ["b", "c", "e"],
  ]);
  assert.deepEqual(
    shards.map((shard) => shard.seconds),
    [8, 9],
  );
});

test("every file lands on exactly one shard, the same whatever order the files come in", () => {
  const seconds = Object.fromEntries(
    Array.from({ length: 40 }, (_, i) => [`src/f${i}.test.ts`, (i * 7) % 11]),
  );
  const keys = Object.keys(seconds);
  const orders = [keys, [...keys].reverse(), [...keys].sort()];
  const splits = orders.map((order) =>
    names(split(Object.fromEntries(order.map((key) => [key, seconds[key]])), 6)),
  );
  assert.deepEqual(splits[1], splits[0]);
  assert.deepEqual(splits[2], splits[0]);
  const all = splits[0].flat();
  assert.equal(all.length, keys.length);
  assert.deepEqual([...new Set(all)].sort(), [...keys].sort());
});

test("with a stale upstream table, the measured split's slowest shard is lighter", () => {
  // Upstream's table lacks the fork file, so it counts 0.6 s and joins a shard that is already full.
  const truth = { "src/old.test.ts": 10, "src/x.test.ts": 9, "src/fork/slow.test.ts": 55 };
  const keys = Object.keys(truth);
  const upstream = JSON.stringify({ "src/old.test.ts": 10, "src/x.test.ts": 9 });
  const slowest = (shards) =>
    Math.max(...shards.map((shard) => shard.files.reduce((sum, file) => sum + truth[file], 0)));
  const before = split(weigh(keys, undefined, upstream).seconds, 2);
  const after = split(weigh(keys, table(truth), upstream).seconds, 2);
  assert.equal(slowest(before), 64);
  assert.equal(slowest(after), 55);
});

test("a new test file the table has never seen counts as the table's median", () => {
  const odd = weigh(["new.test.ts"], table({ a: 1, b: 4, c: 10 }));
  assert.equal(odd.source, "measured");
  assert.equal(odd.seconds["new.test.ts"], 4);
  const even = weigh(["new.test.ts", "a"], table({ a: 1, b: 3, c: 5, d: 100 }));
  assert.equal(even.seconds["new.test.ts"], 4);
  assert.equal(even.seconds.a, 1);
});

test("a table entry for a deleted file changes nothing", () => {
  const keys = ["a", "b", "c"];
  const files = { a: 3, b: 2, c: 1 };
  // The median of a 4-entry table differs from a 3-entry one, so only the files' own times may count.
  const withGone = weigh(keys, table({ ...files, gone: 2 }));
  assert.deepEqual(withGone.seconds, files);
  assert.deepEqual(names(split(withGone.seconds, 2)), names(split(files, 2)));
});

test("missing, broken, empty, not-seconds or negative timing data falls back to upstream's split exactly", () => {
  const upstream = JSON.stringify({ a: 2, b: 1 });
  const keys = ["a", "b", "new"];
  const want = { a: 2, b: 1, new: UPSTREAM_UNRECORDED };
  const cases = [
    [undefined, /missing/],
    ["{not json", /not JSON/],
    [table({}), /no files/],
    [JSON.stringify({ files: { a: 1 } }), /not in seconds/],
    [table({ a: -1 }), /no usable time/],
    [table({ a: "3" }), /no usable time/],
    ["null", /not in seconds/],
  ];
  for (const [text, why] of cases) {
    const got = weigh(keys, text, upstream);
    assert.equal(got.source, "upstream", String(text));
    assert.match(got.why, why);
    assert.deepEqual(got.seconds, want, String(text));
  }
  // Without upstream's table too, every file is upstream's default.
  assert.deepEqual(weigh(["x"], undefined, "{broken").seconds, { x: UPSTREAM_UNRECORDED });
  assert.equal(readTable(table({ a: 0 })).files.a, 0);
});

test("the log parser takes each file's Duration and skips interleaved, ambiguous ones", () => {
  const stamp = "2026-11-28T00:56:55.8591062Z ";
  const log = [
    "~/apps/server$ vp test run 'src/a.test.ts' ○ cache miss: executing",
    " RUN  v5.1.1 /home/runner/work/t3code/t3code/apps/server",
    " ✓ src/a.test.ts (8 tests) 88ms",
    "",
    " Test Files  1 passed (1)",
    "   Duration  4.85s (transform 71%, import 22%, setup 5%, tests 2%)",
    " ✓ integration/b.integration.test.ts (1 test) 175ms",
    "   ✓ Grok replay fixtures (15)",
    "     ✓ runs grok_monitor/grok through OrchestratorV2 using deterministic replay 1882ms",
    "   Duration  593ms (setup 60%, import 17%)",
    // Two file lines before one Duration: whose it is cannot be told, so neither counts.
    " ✓ src/c.test.ts (1 test) 1ms",
    " ✓ src/d.test.ts (1 test) 1ms",
    "   Duration  9.00s (tests 1%)",
    "   Duration  1.00s (tests 1%)",
    " ✓ src/e.test.ts (2 tests) 3ms\r",
    "   Duration  2.50s (tests 1%)\r",
  ]
    .map((line) => stamp + line)
    .join("\n");
  assert.deepEqual(parseLog(log), {
    "src/a.test.ts": 4.85,
    "integration/b.integration.test.ts": 0.593,
    "src/e.test.ts": 2.5,
  });
});

test("the table is each file's median over the logs, rounded, sorted, and the same from the same logs", () => {
  const log = (a, b) =>
    ` ✓ src/z.test.ts (1 test) 1ms\n   Duration  ${a}s\n ✓ src/a.test.ts (1 test) 1ms\n   Duration  ${b}s\n`;
  const logs = [log(1, 10.04), log(3, 10.06), log(2, 99)];
  const built = buildTable(logs, ["9", "10"]);
  assert.equal(built.unit, "seconds");
  assert.deepEqual(built.runs, ["10", "9"]);
  assert.deepEqual(Object.keys(built.files), ["src/a.test.ts", "src/z.test.ts"]);
  assert.deepEqual(built.files, { "src/a.test.ts": 10.1, "src/z.test.ts": 2 });
  assert.deepEqual(buildTable([...logs].reverse(), ["10", "9"]), built);
  assert.ok(readTable(JSON.stringify(built)).files);
});

test("the committed table is usable", () => {
  const got = readTable(
    fs.readFileSync(path.join(root, ".github/fork-server-test-seconds.json"), "utf8"),
  );
  assert.equal(got.why, undefined);
  assert.ok(Object.keys(got.files).length > 100);
});

test("Test Server runs this script's files, not upstream's --shard cut", () => {
  const workflow = fs.readFileSync(path.join(root, ".github/workflows/fork-tests.yml"), "utf8");
  const job = workflow.slice(workflow.indexOf("\n  test_server:"), workflow.indexOf("\n  rust:"));
  assert.match(
    job,
    /node \.github\/scripts\/fork-server-shards\.cjs --shard \$\{\{ matrix\.shard \}\}\/\$\{\{ strategy\.job-total \}\}/,
  );
  assert.doesNotMatch(job, /test:cached -- --shard/);
  // An empty list would run every server test on that shard.
  assert.match(
    job,
    /if \[ "\$\{#files\[@\]\}" -eq 0 \] \|\| \[ -z "\$\{files\[0\]\}" \]; then .*exit 1; fi/,
  );
});
