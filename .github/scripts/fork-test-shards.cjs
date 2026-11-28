// Fork only: prints one Test shard's work (`fork-test-shards.cjs 1/2`, or `--plan 2` for all):
// `split <dir>` for a large package every shard runs a vitest slice of, `whole <name>` for the
// rest, longest first onto the shard with the fewest seconds (.github/fork-test-seconds.md).
// TEST_PACKAGES from `changes` limits the set; empty means every package.
const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { readWorkspace } = require("./ci-changes.cjs");

const TABLE = path.join(__dirname, "../fork-test-seconds.json");
// Wall seconds per file and per second of its test time: whole packages run 4 at once, a
// slice alone (fitted to 10 Test jobs of 2026-11-27/28; a refreshed table brings its own).
const DEFAULT_MODEL = {
  whole: { perFile: 0.363, perSecond: 0.135 },
  slice: { perFile: 0.269, perSecond: 0.818 },
};

// Tested by their own jobs (Test Server, Test Web) or not a package to test.
const OWN_JOB = new Set(["t3", "@t3tools/web", "@t3tools/monorepo"]);
const TEST_FILE = /\.(?:test|spec)\.[cm]?[jt]sx?$/;
// One vitest run of this many files outlasts the rest of a shard.
const SPLIT_AT = 100;

const round = (value) => Math.round(value * 100) / 100;

const validMode = (mode) =>
  mode && [mode.perFile, mode.perSecond].every((value) => typeof value === "number" && value >= 0);

/**
 * The seconds table from its text: { files: Map, median, model }, or { reason } when it is
 * missing, not JSON, empty, or not a table in seconds (an old one); callers then count files.
 */
function readTable(text) {
  if (text === undefined) return { reason: "no .github/fork-test-seconds.json" };
  let table;
  try {
    table = JSON.parse(text);
  } catch {
    return { reason: ".github/fork-test-seconds.json is not JSON" };
  }
  if (table?.unit !== "seconds" || typeof table.files !== "object" || table.files === null)
    return { reason: ".github/fork-test-seconds.json is not a table in seconds" };
  const entries = Object.entries(table.files);
  if (entries.length === 0) return { reason: ".github/fork-test-seconds.json is empty" };
  if (!entries.every(([, value]) => typeof value === "number" && value >= 0))
    return { reason: ".github/fork-test-seconds.json has a time that is not seconds" };
  const values = entries.map(([, value]) => value).toSorted((a, b) => a - b);
  const mid = Math.floor(values.length / 2);
  const median = values.length % 2 ? values[mid] : (values[mid - 1] + values[mid]) / 2;
  const model =
    validMode(table.model?.whole) && validMode(table.model?.slice) ? table.model : DEFAULT_MODEL;
  return { files: new Map(entries), median, model, runs: table.runs?.length ?? 0 };
}

/** The files of each vitest `--shard i/count`, as vitest's BaseSequencer cuts them. */
function vitestSlices(files, count) {
  const sorted = files
    .map((file) => ({ file, hash: crypto.createHash("sha1").update(`/${file}`).digest("hex") }))
    .toSorted((a, b) => (a.hash < b.hash ? -1 : a.hash > b.hash ? 1 : 0))
    .map((entry) => entry.file);
  const base = Math.floor(sorted.length / count);
  const extra = sorted.length % count;
  return Array.from({ length: count }, (_, i) => {
    const start = i * base + Math.min(i, extra);
    return sorted.slice(start, start + base + (i < extra ? 1 : 0));
  });
}

/**
 * A package's weights: `weight`, its seconds run whole; `slices`, the seconds of each vitest
 * slice (with a table only); `files`, its test file count. `pkg.testFiles`: repo paths.
 */
function weigh(pkg, table, count) {
  const files = pkg.testFiles.length;
  const base = { name: pkg.name, dir: pkg.dir, splittable: pkg.splittable, files };
  if (!table.files) return { ...base, weight: Math.max(1, files) };
  const seconds = (file) => table.files.get(file) ?? table.median;
  const cost = (mode, list) =>
    round(list.reduce((total, file) => total + mode.perFile + mode.perSecond * seconds(file), 0));
  const relative = pkg.testFiles.map((file) => file.slice(pkg.dir.length + 1));
  return {
    ...base,
    weight: cost(table.model.whole, pkg.testFiles),
    slices: vitestSlices(relative, count).map((slice) =>
      cost(
        table.model.slice,
        slice.map((file) => `${pkg.dir}/${file}`),
      ),
    ),
  };
}

function assign(packages, count) {
  const split = packages.filter((pkg) => pkg.splittable && pkg.files >= SPLIT_AT);
  const shards = Array.from({ length: count }, (_, i) => ({
    weight: round(
      split.reduce((total, pkg) => total + (pkg.slices ? pkg.slices[i] : pkg.weight / count), 0),
    ),
    whole: [],
    split,
  }));
  const ordered = packages
    .filter((pkg) => !split.includes(pkg))
    .toSorted((a, b) => b.weight - a.weight || a.name.localeCompare(b.name));
  for (const pkg of ordered) {
    const lightest = shards.reduce((best, shard) => (shard.weight < best.weight ? shard : best));
    lightest.weight = round(lightest.weight + pkg.weight);
    lightest.whole.push(pkg);
  }
  return shards;
}

function testFiles(dir) {
  return childProcess
    .execFileSync("git", ["ls-files", "-z", "--", dir], { encoding: "utf8", maxBuffer: 1 << 26 })
    .split("\0")
    .filter((file) => TEST_FILE.test(file));
}

function main() {
  const plan = process.argv[2] === "--plan";
  const match = plan
    ? (process.argv[3] ?? "").match(/^()(\d+)$/)
    : (process.argv[2] ?? "").match(/^(\d+)\/(\d+)$/);
  if (!match || (!plan && (Number(match[1]) < 1 || Number(match[1]) > Number(match[2]))))
    throw new Error("usage: fork-test-shards.cjs <index>/<count> | --plan <count>");
  const [index, count] = [Number(match[1]), Number(match[2])];
  const table = readTable(fs.existsSync(TABLE) ? fs.readFileSync(TABLE, "utf8") : undefined);
  const only = (process.env.TEST_PACKAGES ?? "").split(/\s+/).filter(Boolean);
  const root = path.resolve(__dirname, "../..");
  const packages = readWorkspace()
    .filter((pkg) => pkg.hasTest && !OWN_JOB.has(pkg.name))
    .filter((pkg) => only.length === 0 || only.includes(pkg.name))
    .map((pkg) => {
      const manifest = JSON.parse(
        fs.readFileSync(path.join(root, pkg.dir, "package.json"), "utf8"),
      );
      return weigh(
        {
          name: pkg.name,
          dir: pkg.dir,
          testFiles: testFiles(pkg.dir),
          // Run in its directory with --shard, so the script must be vitest itself.
          splittable: /^vp test run(\s|$)/.test(manifest.scripts.test),
        },
        table,
        count,
      );
    });
  const unknown = only.filter((name) => !packages.some((pkg) => pkg.name === name));
  if (unknown.length)
    throw new Error(`TEST_PACKAGES names no tested package: ${unknown.join(" ")}`);

  const shards = assign(packages, count);
  const unit = table.files ? "s" : " files";
  const by = table.files
    ? `Balanced by measured seconds (.github/fork-test-seconds.json, ${table.runs} run(s); a file it lacks counts ${table.median} s).`
    : `Balanced by test file count: ${table.reason}.`;
  if (plan) {
    console.log(by);
    shards.forEach((shard, i) => {
      const names = shard.whole.map((pkg) => `${pkg.name} (${pkg.weight}${unit})`).join(", ");
      console.log(`${i + 1}/${count}: ~${shard.weight}${unit}; whole: ${names || "none"}`);
    });
    return;
  }
  const shard = shards[index - 1];
  for (const pkg of shard.split) console.log(`split ${pkg.dir}`);
  for (const pkg of shard.whole) console.log(`whole ${pkg.name}`);
  const summary = process.env.GITHUB_STEP_SUMMARY;
  if (summary) {
    const list = (pkgs, slice) =>
      pkgs
        .map(
          (pkg) =>
            `\`${pkg.name}\` (${slice && pkg.slices ? pkg.slices[index - 1] : pkg.weight}${unit})`,
        )
        .join(", ");
    const loads = shards.map((other) => `${other.weight}${unit}`).join(", ");
    const lines = [`### Test shard ${index}/${count}`, "", `${by} Shards: ${loads}.`, ""];
    if (shard.split.length)
      lines.push(`Slice ${index}/${count} of ${list(shard.split, true)}.`, "");
    if (shard.whole.length) lines.push(`Whole: ${list(shard.whole)}.`, "");
    if (!shard.split.length && !shard.whole.length)
      lines.push(
        `Nothing to run: the ${packages.length} affected package(s) are on other shards.`,
        "",
      );
    fs.appendFileSync(summary, `${lines.join("\n")}\n`);
  }
}

module.exports = { assign, weigh, readTable, vitestSlices, DEFAULT_MODEL, SPLIT_AT };

if (require.main === module) main();
