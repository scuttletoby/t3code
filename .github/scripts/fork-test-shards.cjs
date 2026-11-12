// Fork only. Splits the Test job's packages across the shards of its matrix in
// .github/workflows/fork-tests.yml. Upstream runs the job on one large runner;
// a GitHub-hosted runner has 4 CPUs, so the packages go to several runners.
//
//   node .github/scripts/fork-test-shards.cjs 1/2    prints shard 1's packages
//
// TEST_PACKAGES (from the `changes` job) limits the set; empty means every
// package the Test job covers. Packages are weighted by their test file count
// and placed, heaviest first, on the least loaded shard, so the split follows
// new packages and tests without a hand-kept list.
const childProcess = require("node:child_process");
const fs = require("node:fs");

const { readWorkspace } = require("./ci-changes.cjs");

// Tested by their own jobs (Test Server, Test Web) or not a package to test.
const OWN_JOB = new Set(["t3", "@t3tools/web", "@t3tools/monorepo"]);
const TEST_FILE = /\.(?:test|spec)\.[cm]?[jt]sx?$/;

function assign(packages, count) {
  const shards = Array.from({ length: count }, () => ({ weight: 0, packages: [] }));
  const ordered = packages.toSorted((a, b) => b.weight - a.weight || a.name.localeCompare(b.name));
  for (const pkg of ordered) {
    const lightest = shards.reduce((best, shard) => (shard.weight < best.weight ? shard : best));
    lightest.weight += pkg.weight;
    lightest.packages.push(pkg);
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
  const packages = readWorkspace()
    .filter((pkg) => pkg.hasTest && !OWN_JOB.has(pkg.name))
    .filter((pkg) => only.length === 0 || only.includes(pkg.name))
    .map((pkg) => ({ name: pkg.name, weight: Math.max(1, testFiles(pkg.dir)) }));
  const unknown = only.filter((name) => !packages.some((pkg) => pkg.name === name));
  if (unknown.length)
    throw new Error(`TEST_PACKAGES names no tested package: ${unknown.join(" ")}`);

  const shard = assign(packages, count)[index - 1];
  for (const pkg of shard.packages) console.log(pkg.name);
  const summary = process.env.GITHUB_STEP_SUMMARY;
  if (summary) {
    const list = shard.packages.map((pkg) => `\`${pkg.name}\` (${pkg.weight})`).join(", ");
    fs.appendFileSync(
      summary,
      `### Test shard ${index}/${count}\n\n` +
        (shard.packages.length
          ? `${shard.packages.length} of ${packages.length} packages, ${shard.weight} test files: ${list}\n`
          : `No packages: the change affects ${packages.length} tested package(s), all on other shards.\n`),
    );
  }
}

module.exports = { assign };

if (require.main === module) main();
