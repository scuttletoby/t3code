// Fork only: cuts Test Server's file list as upstream's sequencer does, but by .github/fork-server-test-seconds.json
// (upstream's shardWeights.json lacks most fork files), and prints one shard's files for `vp run test:cached`.
// Usage and the table: .github/fork-server-test-seconds.md (`--shard i/n`, `--plan n`, `--refresh --logs <dir>`).
const fs = require("node:fs");
const path = require("node:path");
const url = require("node:url");

const root = path.resolve(__dirname, "../..");
const SERVER = "apps/server";
const TABLE = ".github/fork-server-test-seconds.json";
const UPSTREAM_TABLE = "apps/server/src/testUtils/shardWeights.json";
// Upstream's weight for a file its table lacks (weightedShardSequencer.ts).
const UPSTREAM_UNRECORDED = 0.6;

const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
};

// The table's `files` (keys relative to apps/server, as upstream's) and why it cannot be used, if it cannot.
function readTable(text) {
  if (text === undefined) return { why: `${TABLE} is missing` };
  let table;
  try {
    table = JSON.parse(text);
  } catch {
    return { why: `${TABLE} is not JSON` };
  }
  if (!table || table.unit !== "seconds") return { why: `${TABLE} is not in seconds` };
  const files = table.files;
  if (!files || typeof files !== "object" || Object.keys(files).length === 0)
    return { why: `${TABLE} has no files` };
  for (const [file, seconds] of Object.entries(files))
    if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0)
      return { why: `${TABLE}: ${file} has no usable time` };
  return { files };
}

// Each file's weight. With a usable fork table: its time, or the table's median for a file it lacks (a new
// test). Without one: upstream's table and default, so the split is exactly upstream's.
function weigh(keys, forkText, upstreamText) {
  const fork = readTable(forkText);
  if (fork.files) {
    const fallback = median(Object.values(fork.files));
    return {
      source: "measured",
      seconds: Object.fromEntries(keys.map((key) => [key, fork.files[key] ?? fallback])),
    };
  }
  let upstream = {};
  try {
    upstream = JSON.parse(upstreamText ?? "{}") ?? {};
  } catch {}
  return {
    source: "upstream",
    why: fork.why,
    seconds: Object.fromEntries(keys.map((key) => [key, upstream[key] ?? UPSTREAM_UNRECORDED])),
  };
}

// Upstream's cut (weightedShardSequencer.ts): longest first, ties by name, each onto the lightest shard
// (the first of equals). The same on every shard whatever order the files come in.
function split(seconds, count) {
  const shards = Array.from({ length: count }, () => ({ files: [], seconds: 0 }));
  const order = Object.keys(seconds).sort(
    (a, b) => seconds[b] - seconds[a] || (a < b ? -1 : a > b ? 1 : 0),
  );
  for (const key of order) {
    const least = Math.min(...shards.map((shard) => shard.seconds));
    const lightest = shards.find((shard) => shard.seconds === least);
    lightest.seconds = least + seconds[key];
    lightest.files.push(key);
  }
  return shards;
}

const readOptional = (file) => {
  try {
    return fs.readFileSync(path.join(root, file), "utf8");
  } catch {
    return undefined;
  }
};

// The server's test files as upstream's vitest config finds them (its own discovery, shard 1/1: every file).
async function discover() {
  const lib = url.pathToFileURL(path.join(root, "scripts/lib/sharded-tests.ts")).href;
  const { shardedTestFiles } = await import(lib);
  const files = await shardedTestFiles(path.join(root, SERVER), "1/1");
  return files.map((file) => path.relative(path.join(root, SERVER), file).replaceAll("\\", "/"));
}

// Refresh: test:cached runs each file in its own vitest process, which prints ` ✓ <file> (N tests) <ms>ms` and then
// `   Duration  <t>s (...)`: the file's whole cost (start, import, tests). Four run at once, so a file line
// whose Duration another file line comes before is ambiguous and skipped.
const STAMP = /^\d{4}-\d\d-\d\dT[\d:.]+Z /;
const FILE_LINE = /^ [✓✔↓×✗❯] (\S+\.(?:test|spec)\.[cm]?[jt]sx?) \(/u;
const DURATION_LINE = /^ {3}Duration {2}([\d.]+)(ms|s)\b/;

function parseLog(text) {
  const seconds = {};
  let open = null;
  for (const raw of text.split("\n")) {
    const line = raw.replace(STAMP, "").replace(/\r$/, "");
    const file = FILE_LINE.exec(line);
    if (file) {
      open = open === null ? file[1] : undefined;
      continue;
    }
    const duration = DURATION_LINE.exec(line);
    if (duration) {
      if (open) seconds[open] = Number(duration[1]) / (duration[2] === "ms" ? 1000 : 1);
      open = null;
    }
  }
  return seconds;
}

// Each file's median over the jobs' logs, rounded to 0.1 s, sorted.
function buildTable(logs, runs) {
  const times = {};
  for (const text of logs)
    for (const [file, seconds] of Object.entries(parseLog(text)))
      (times[file] ??= []).push(seconds);
  const files = {};
  for (const file of Object.keys(times).sort())
    files[file] = Math.round(median(times[file]) * 10) / 10;
  return {
    "//": "Test Server's per-file seconds (vitest's Duration per file), the median over the runs; written by .github/scripts/fork-server-shards.cjs --refresh. See .github/fork-server-test-seconds.md.",
    unit: "seconds",
    runs: [...runs].map(String).sort(),
    files,
  };
}

function savedLogs(dir) {
  const names = fs
    .readdirSync(dir)
    .filter((name) => name.endsWith(".log"))
    .sort();
  return {
    runs: [...new Set(names.map((name) => name.split("-")[0]))],
    logs: names.map((name) => fs.readFileSync(path.join(dir, name), "utf8")),
  };
}

// As `vp fmt` writes it (runs on one line while it fits in 100 columns), so a refresh leaves no format diff.
function tableText(table) {
  const text = JSON.stringify(table, null, 2);
  const inline = `  "runs": [${table.runs.map((run) => JSON.stringify(run)).join(", ")}],`;
  return `${inline.length <= 100 ? text.replace(/ {2}"runs": \[[^\]]*\],/, inline) : text}\n`;
}

const stripColour = (text) => text.replace(/\x1b\[[0-9;]*m/g, "");

async function main(argv) {
  const arg = (name) => {
    const at = argv.indexOf(name);
    return at === -1 ? undefined : argv[at + 1];
  };
  if (argv.includes("--refresh")) {
    // Saved first (.github/fork-server-test-seconds.md): fork scripts reach gh only through fork-gh-guard.sh.
    if (!arg("--logs")) throw new Error("--refresh needs --logs <dir> of saved Test Server logs");
    const { runs, logs } = savedLogs(arg("--logs"));
    const table = buildTable(logs.map(stripColour), runs);
    if (Object.keys(table.files).length === 0) throw new Error("no file times in the logs");
    fs.writeFileSync(path.join(root, TABLE), tableText(table));
    console.log(
      `${TABLE}: ${Object.keys(table.files).length} files from runs ${table.runs.join(", ")}`,
    );
    return;
  }
  const shardArg = arg("--shard");
  const planArg = arg("--plan");
  const match = /^(\d+)\/(\d+)$/.exec(shardArg ?? `1/${planArg ?? ""}`);
  const index = Number(match?.[1]);
  const count = Number(match?.[2]);
  if (!match || index < 1 || index > count)
    throw new Error("usage: --shard i/n | --plan n | --refresh");
  const weights = weigh(await discover(), readOptional(TABLE), readOptional(UPSTREAM_TABLE));
  if (weights.why)
    console.error(`::warning::fork-server-shards: ${weights.why}; upstream's weights instead`);
  const shards = split(weights.seconds, count);
  if (planArg) {
    shards.forEach((shard, i) =>
      console.log(
        `Test Server ${i + 1}: ${shard.files.length} files, ~${Math.round(shard.seconds)} s (${weights.source})`,
      ),
    );
    return;
  }
  const mine = shards[index - 1].files;
  // An empty list would make test:cached run every server test.
  if (mine.length === 0) throw new Error(`shard ${index}/${count} has no test files`);
  for (const key of mine) console.log(`${SERVER}/${key}`);
}

if (require.main === module)
  main(process.argv.slice(2)).catch((error) => {
    console.error(`fork-server-shards: ${error.message}`);
    process.exit(1);
  });

module.exports = {
  readTable,
  weigh,
  split,
  parseLog,
  buildTable,
  tableText,
  median,
  UPSTREAM_UNRECORDED,
};
