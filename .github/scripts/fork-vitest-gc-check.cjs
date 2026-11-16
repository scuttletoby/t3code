// Fails when the installed vitest no longer reads the Chromium garbage collection setting that the
// Test Web step of fork-tests.yml sets. It is not a documented vitest option, so an update could drop
// it silently and the browser crashes would come back. Recheck it by hand on every vitest bump too.
//   node .github/scripts/fork-vitest-gc-check.cjs [dir]   (dir: where `vp test` runs, default .)
const fs = require("node:fs");
const path = require("node:path");

const VARIABLE = "VITEST_CHROMIUM_GC_DISK_THRESHOLD_GB";

// Node's lookup by hand: <ancestor>/node_modules/<name>, nearest first, links followed (pnpm).
// Not require.resolve: vitest's exports need not include its package.json.
function findPackage(name, fromDir) {
  for (let dir = path.resolve(fromDir); ; dir = path.dirname(dir)) {
    const candidate = path.join(dir, "node_modules", name);
    if (fs.existsSync(path.join(candidate, "package.json"))) return fs.realpathSync(candidate);
    if (path.dirname(dir) === dir) return null;
  }
}

// The vitest that `vp test` runs: vite-plus's own dependency, else one found from dir.
function findVitest(dir) {
  const vitePlus = findPackage("vite-plus", dir);
  return (vitePlus && findPackage("vitest", vitePlus)) || findPackage("vitest", dir);
}

function filesUnder(dir) {
  return fs
    .readdirSync(dir, { withFileTypes: true, recursive: true })
    .filter((entry) => entry.isFile() && /\.[cm]?js$/.test(entry.name))
    .map((entry) => path.join(entry.parentPath, entry.name));
}

// The dist files that mention the variable; empty when vitest no longer reads it.
function readers(vitestDir) {
  const dist = path.join(vitestDir, "dist");
  if (!fs.existsSync(dist)) return [];
  return filesUnder(dist).filter((file) => fs.readFileSync(file, "utf8").includes(VARIABLE));
}

function main(argv) {
  const dir = argv[0] ?? ".";
  const vitest = findVitest(dir);
  if (!vitest) {
    console.error(`::error::fork-vitest-gc-check: no vitest installed under ${path.resolve(dir)}`);
    return 1;
  }
  const { version } = JSON.parse(fs.readFileSync(path.join(vitest, "package.json"), "utf8"));
  const found = readers(vitest);
  if (found.length === 0) {
    console.error(
      `::error::vitest ${version} no longer reads ${VARIABLE}, so Test Web's Chromium garbage ` +
        `collection after every file is off and the browser crashes of fork issue #245 can come back. ` +
        `Find vitest's new switch for it and set that in fork-tests.yml's test_web Test step.`,
    );
    return 1;
  }
  console.log(`vitest ${version} reads ${VARIABLE} (${path.relative(vitest, found[0])})`);
  return 0;
}

module.exports = { VARIABLE, findPackage, findVitest, readers, main };

if (require.main === module) process.exitCode = main(process.argv.slice(2));
