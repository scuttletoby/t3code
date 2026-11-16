// Tests for fork-vitest-gc-check.cjs, on fake pnpm-style installs (no real vitest needed).
//   node --test .github/scripts/fork-vitest-gc-check.test.cjs
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { VARIABLE, findVitest, readers } = require("./fork-vitest-gc-check.cjs");

const SCRIPT = path.join(__dirname, "fork-vitest-gc-check.cjs");

function write(file, text) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, text);
}

// A workspace laid out like pnpm's: apps/web/node_modules/vite-plus links into .pnpm, and
// vite-plus's vitest is a sibling link there. `chunk` is the text of vitest's dist chunk.
function install(t, chunk, { strayVitest = false } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-vitest-gc-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const store = path.join(root, "node_modules/.pnpm");
  const vitest = path.join(store, "vitest@5.0.5/node_modules/vitest");
  write(path.join(vitest, "package.json"), '{"name":"vitest","version":"5.0.5"}');
  write(path.join(vitest, "dist/chunks/index.abc.js"), chunk);
  write(path.join(vitest, "dist/index.js"), "export {};\n");
  const plusDeps = path.join(store, "vite-plus@1.4.0/node_modules");
  write(path.join(plusDeps, "vite-plus/package.json"), '{"name":"vite-plus","version":"1.4.0"}');
  fs.symlinkSync(vitest, path.join(plusDeps, "vitest"));
  const web = path.join(root, "apps/web");
  fs.mkdirSync(path.join(web, "node_modules"), { recursive: true });
  fs.symlinkSync(path.join(plusDeps, "vite-plus"), path.join(web, "node_modules/vite-plus"));
  if (strayVitest) {
    // Another vitest the app could see, which `vp test` does not run.
    const other = path.join(root, "node_modules/vitest");
    write(path.join(other, "package.json"), '{"name":"vitest","version":"4.0.0"}');
    write(path.join(other, "dist/index.js"), `process.env.${VARIABLE};\n`);
  }
  return { root, web, vitest };
}

const run = (dir) => spawnSync(process.execPath, [SCRIPT, dir], { encoding: "utf8" });
const reads = `const t = Number(process.env.${VARIABLE} ?? 4) * 1024 ** 3;\n`;

test("finds vite-plus's own vitest through pnpm's links", (t) => {
  const { web, vitest } = install(t, reads, { strayVitest: true });
  assert.equal(findVitest(web), fs.realpathSync(vitest));
});

test("passes when the installed vitest reads the setting", (t) => {
  const { web, vitest } = install(t, reads);
  assert.deepEqual(readers(vitest), [
    path.join(fs.realpathSync(vitest), "dist/chunks/index.abc.js"),
  ]);
  const result = run(web);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, new RegExp(`vitest 5\\.0\\.5 reads ${VARIABLE}`));
});

test("fails, saying why, when vitest dropped or renamed the setting", (t) => {
  const { web } = install(t, "const t = Number(process.env.VITEST_CHROMIUM_GC_THRESHOLD ?? 4);\n");
  const result = run(web);
  assert.equal(result.status, 1);
  assert.match(result.stderr, new RegExp(`::error::vitest 5\\.0\\.5 no longer reads ${VARIABLE}`));
  assert.match(result.stderr, /#245/);
});

test("vite-plus's vitest decides, not another one that still reads it", (t) => {
  const { web } = install(t, "export {};\n", { strayVitest: true });
  assert.equal(run(web).status, 1);
});

test("fails when no vitest is installed at all", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-vitest-gc-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const result = run(root);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /no vitest installed/);
});
