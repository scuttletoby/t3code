// Fork only: prints one Test shard's work (`fork-test-shards.cjs 1/2`): `split
// <dir>` for a large package every shard runs a vitest slice of, `whole <name>`
// for the rest, heaviest first (by test files) onto the lightest shard.
// TEST_PACKAGES from `changes` limits the set; empty means every package.
const childProcess = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const { readWorkspace } = require("./ci-changes.cjs");

// Tested by their own jobs (Test Server, Test Web) or not a package to test.
const OWN_JOB = new Set(["t3", "@t3tools/web", "@t3tools/monorepo"]);
const TEST_FILE = /\.(?:test|spec)\.[cm]?[jt]sx?$/;
// One vitest run of this many files outlasts the rest of a shard.
const SPLIT_AT = 100;

function assign(packages, count) {
  const split = packages.filter((pkg) => pkg.splittable && pkg.weight >= SPLIT_AT);
  const share = split.reduce((total, pkg) => total + pkg.weight, 0) / count;
  const shards = Array.from({ length: count }, () => ({ weight: share, whole: [], split }));
  const ordered = packages
    .filter((pkg) => !split.includes(pkg))
    .toSorted((a, b) => b.weight - a.weight || a.name.localeCompare(b.name));
  for (const pkg of ordered) {
    const lightest = shards.reduce((best, shard) => (shard.weight < best.weight ? shard : best));
    lightest.weight += pkg.weight;
    lightest.whole.push(pkg);
  }
  return shards;
}

function testFiles(dir) {
  return childProcess
    .execFileSync("git", ["ls-files", "-z", "--", dir], { encoding: "utf8", maxBuffer: 1 << 26 })
    .split("\0")
    .filter((file) => TEST_FILE.test(file)).length;
}

function main() {
  const match = (process.argv[2] ?? "").match(/^(\d+)\/(\d+)$/);
  if (!match || Number(match[1]) < 1 || Number(match[1]) > Number(match[2]))
    throw new Error("usage: fork-test-shards.cjs <index>/<count>");
  const [index, count] = [Number(match[1]), Number(match[2])];
  const only = (process.env.TEST_PACKAGES ?? "").split(/\s+/).filter(Boolean);
  const root = path.resolve(__dirname, "../..");
  const packages = readWorkspace()
    .filter((pkg) => pkg.hasTest && !OWN_JOB.has(pkg.name))
    .filter((pkg) => only.length === 0 || only.includes(pkg.name))
    .map((pkg) => {
      const manifest = JSON.parse(
        fs.readFileSync(path.join(root, pkg.dir, "package.json"), "utf8"),
      );
      return {
        name: pkg.name,
        dir: pkg.dir,
        weight: Math.max(1, testFiles(pkg.dir)),
        // Run in its directory with --shard, so the script must be vitest itself.
        splittable: /^vp test run(\s|$)/.test(manifest.scripts.test),
      };
    });
  const unknown = only.filter((name) => !packages.some((pkg) => pkg.name === name));
  if (unknown.length)
    throw new Error(`TEST_PACKAGES names no tested package: ${unknown.join(" ")}`);

  const shard = assign(packages, count)[index - 1];
  for (const pkg of shard.split) console.log(`split ${pkg.dir}`);
  for (const pkg of shard.whole) console.log(`whole ${pkg.name}`);
  const summary = process.env.GITHUB_STEP_SUMMARY;
  if (summary) {
    const list = (pkgs) => pkgs.map((pkg) => `\`${pkg.name}\` (${pkg.weight})`).join(", ");
    const lines = [`### Test shard ${index}/${count}`, ""];
    if (shard.split.length) lines.push(`Slice ${index}/${count} of ${list(shard.split)}.`, "");
    if (shard.whole.length) lines.push(`Whole: ${list(shard.whole)}.`, "");
    if (!shard.split.length && !shard.whole.length)
      lines.push(
        `Nothing to run: the ${packages.length} affected package(s) are on other shards.`,
        "",
      );
    fs.appendFileSync(summary, `${lines.join("\n")}\n`);
  }
}

module.exports = { assign, SPLIT_AT };

if (require.main === module) main();
