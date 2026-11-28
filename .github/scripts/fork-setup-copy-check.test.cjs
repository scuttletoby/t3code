const assert = require("node:assert/strict");
const test = require("node:test");
const { execFileSync, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { COPIES, blobId, check, record, fromTree } = require("./fork-setup-copy-check.cjs");

const root = path.resolve(__dirname, "../..");
const UPSTREAM = ".github/actions/setup-ci-deps/action.yml";
const COPY = ".github/actions/fork-setup-ci-deps/action.yml";
const KEY_SCRIPT = path.join(root, ".github/actions/fork-setup-ci-deps/node-modules-key.sh");

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "fork-setup-copy-"));

// An in-memory tree for check() and record().
function tree(files) {
  const store = { ...files };
  return {
    store,
    read: (file) => (file in store ? Buffer.from(store[file]) : undefined),
    write: (file, text) => {
      store[file] = text;
    },
    workflows: Object.keys(store).filter((file) =>
      /^\.github\/workflows\/fork-.*\.ya?ml$/.test(file),
    ),
  };
}

const UP = "name: Setup\nruns:\n  using: composite\n  steps: []\n";
const copyOf = (id, extra = "") => `# Fork copy\n# upstream-blob: ${id}\n${extra}${UP}`;

test("the blob id is git's own", () => {
  const dir = tmp();
  const file = path.join(dir, "a.yml");
  fs.writeFileSync(file, "x: 1\nüñí: ✓\n");
  const git = execFileSync("git", ["hash-object", file], { encoding: "utf8" }).trim();
  assert.equal(blobId(fs.readFileSync(file)), git);
});

test("a copy that records upstream's current file passes", () => {
  const t = tree({ [UPSTREAM]: UP, [COPY]: copyOf(blobId(Buffer.from(UP))) });
  assert.deepEqual(check(t), []);
});

test("an upstream change fails until the copy records it, and says to port it first", () => {
  const old = blobId(Buffer.from(UP));
  const changed = `${UP}# upstream added a step\n`;
  const t = tree({ [UPSTREAM]: changed, [COPY]: copyOf(old) });
  const [problem, ...rest] = check(t);
  assert.deepEqual(rest, []);
  assert.match(problem, /changed upstream/);
  assert.match(
    problem,
    /Port the change into \.github\/actions\/fork-setup-ci-deps\/action\.yml first/,
  );
  assert.ok(problem.includes(`git diff ${old} ${blobId(Buffer.from(changed))}`), problem);
  // Recording (after the port) makes it pass; a second record changes nothing.
  assert.deepEqual(record(t), [`${COPY}: upstream-blob ${blobId(Buffer.from(changed))}`]);
  assert.deepEqual(check(t), []);
  assert.deepEqual(record(t), []);
});

test("a copy without exactly one record line fails", () => {
  const id = blobId(Buffer.from(UP));
  for (const copy of [UP, copyOf(id, `# upstream-blob: ${id}\n`), copyOf("abc")]) {
    const problems = check(tree({ [UPSTREAM]: UP, [COPY]: copy }));
    assert.equal(problems.length, 1, copy);
    assert.match(problems[0], /exactly one "# upstream-blob/);
  }
});

test("a missing upstream file or copy fails (closed, never silently passes)", () => {
  assert.match(check(tree({ [COPY]: copyOf("0".repeat(40)) }))[0], /is gone/);
  assert.match(check(tree({ [UPSTREAM]: UP }))[0], /is missing/);
});

test("a fork workflow may not use upstream's action; upstream's workflows may", () => {
  const ok = { [UPSTREAM]: UP, [COPY]: copyOf(blobId(Buffer.from(UP))) };
  for (const line of [
    "      - uses: ./.github/actions/setup-ci-deps",
    "        uses: ./.github/actions/setup-ci-deps",
    '        uses: "./.github/actions/setup-ci-deps"',
    "        uses: ./.github/actions/setup-ci-deps/ # pinned",
  ]) {
    const problems = check(
      tree({ ...ok, ".github/workflows/fork-x.yml": `jobs:\n  a:\n    steps:\n${line}\n` }),
    );
    assert.equal(problems.length, 1, line);
    assert.match(problems[0], /fork-x\.yml uses \.\/\.github\/actions\/setup-ci-deps/);
  }
  const fine = tree({
    ...ok,
    ".github/workflows/fork-x.yml": "        uses: ./.github/actions/fork-setup-ci-deps\n",
    ".github/workflows/ci.yml": "        uses: ./.github/actions/setup-ci-deps\n",
  });
  assert.deepEqual(check(fine), []);
});

test("the repository: the copy follows upstream's file and every fork workflow uses the copy", () => {
  const t = fromTree(root);
  assert.deepEqual(check(t), []);
  assert.deepEqual(
    COPIES.map((c) => c.copy),
    [COPY],
  );
  for (const workflow of [
    ".github/workflows/fork-tests.yml",
    ".github/workflows/fork-release.yml",
  ]) {
    assert.match(
      t.read(workflow).toString(),
      /uses: \.\/\.github\/actions\/fork-setup-ci-deps$/m,
      workflow,
    );
  }
});

// Each step of a composite action: { name, id, uses, if, run, with }, read by indentation.
function steps(text) {
  const body = text.split(/^  steps:\n/m)[1];
  return body
    .split(/^ {4}- /m)
    .slice(1)
    .map((block) => {
      const lines = `      ${block}`.split("\n");
      const step = { text: block };
      for (let i = 0; i < lines.length; i++) {
        const m = lines[i].match(/^ {6}([a-z-]+):\s?(.*)$/);
        if (!m) continue;
        let value = m[2];
        if (value === ">-" || value === "|") {
          const more = [];
          while (i + 1 < lines.length && (/^ {8}/.test(lines[i + 1]) || lines[i + 1] === ""))
            more.push(lines[++i].trim());
          value = more.join(value === ">-" ? " " : "\n").trim();
        }
        step[m[1]] = value;
      }
      return step;
    });
}

const upstreamSteps = steps(fs.readFileSync(path.join(root, UPSTREAM), "utf8"));
const copySteps = steps(fs.readFileSync(path.join(root, COPY), "utf8"));
const byName = (name) => {
  const found = copySteps.filter((s) => s.name === name);
  assert.equal(found.length, 1, name);
  return found[0];
};
const index = (name) => copySteps.findIndex((s) => s.name === name);
const HIT = "steps.node_modules_cache.outputs.cache-hit != 'true'";

test("the copy keeps every upstream step, in order, with the same action and run", () => {
  let at = -1;
  for (const up of upstreamSteps) {
    const i = copySteps.findIndex((s, j) => j > at && s.name === up.name);
    assert.ok(i > at, `upstream step "${up.name}" is missing or out of order in the copy`);
    assert.equal(copySteps[i].uses, up.uses, up.name);
    assert.equal(copySteps[i].run, up.run, up.name);
    at = i;
  }
});

test("the copy, like upstream's, caches Vite+ before it installs or runs project code", () => {
  assert.match(byName("Restore Vite+").uses, /^actions\/cache\/restore@/);
  assert.ok(index("Save Vite+") < index("Resolve node_modules cache"));
  assert.ok(index("Save Vite+") < index("Install dependencies"));
  assert.ok(
    copySteps.every((s) => !(s.uses ?? "").startsWith("actions/cache@")),
    "restore and save are separate",
  );
});

test("node_modules is restored on its exact key only (no entry from another lockfile)", () => {
  const restore = byName("Restore node_modules");
  assert.match(restore.uses, /^actions\/cache\/restore@[0-9a-f]{40} /);
  assert.doesNotMatch(restore.text, /restore-keys/);
  assert.ok(index("Restore node_modules") < index("Install dependencies"));
});

test("a restore without a hit leaves no partial node_modules behind", () => {
  const drop = byName("Drop a partial node_modules");
  assert.match(drop.if, new RegExp(HIT.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.ok(index("Restore node_modules") < index("Drop a partial node_modules"));
  assert.ok(index("Drop a partial node_modules") < index("Restore pnpm store"));
  assert.ok(index("Drop a partial node_modules") < index("Install dependencies"));
  // Run the step's script on a half-extracted tree.
  const dir = tmp();
  for (const file of [
    "node_modules/.modules.yaml",
    "apps/web/node_modules/x/index.js",
    "apps/web/src/keep.ts",
    "keep.txt",
  ]) {
    fs.mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
    fs.writeFileSync(path.join(dir, file), "x");
  }
  const result = spawnSync("bash", ["-euo", "pipefail", "-c", drop.run], {
    cwd: dir,
    env: {
      ...process.env,
      NODE_MODULES_PATHS: "node_modules\napps/web/node_modules\napps/none/node_modules\n",
    },
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.ok(!fs.existsSync(path.join(dir, "node_modules")));
  assert.ok(!fs.existsSync(path.join(dir, "apps/web/node_modules")));
  assert.ok(fs.existsSync(path.join(dir, "apps/web/src/keep.ts")));
  assert.ok(fs.existsSync(path.join(dir, "keep.txt")));
});

test("after a node_modules hit the store is neither restored nor saved (no near-empty store under the full key)", () => {
  assert.ok(byName("Restore pnpm store").if.includes(HIT));
  assert.ok(byName("Save pnpm store").if.includes(HIT));
  // vp install still runs on a hit: pnpm checks node_modules against the lockfile, re-copies
  // directory dependencies and runs the root lifecycle scripts, as on every install.
  assert.equal(byName("Install dependencies").if, "inputs.install == 'true'");
  assert.equal(byName("Restore lockfile verification").if, "inputs.install == 'true'");
});

test("node_modules is saved right after the install, by the store's saver only, never over a hit", () => {
  const save = byName("Save node_modules");
  assert.match(save.uses, /^actions\/cache\/save@[0-9a-f]{40} /);
  assert.ok(index("Install dependencies") < index("Save node_modules"));
  assert.equal(
    index("Save node_modules"),
    copySteps.length - 2,
    "the last step before the task cache",
  );
  assert.ok(save.if.includes(HIT));
  assert.ok(save.if.includes("inputs.save-pnpm-store == 'true'"));
  assert.ok(
    save.if.includes("github.base_ref == github.event.repository.default_branch"),
    "a stacked PR does not save",
  );
  assert.doesNotMatch(
    save.if,
    /always\(\)|failure\(\)|cancelled\(\)/,
    "a failed install saves nothing",
  );
  const restore = byName("Restore node_modules");
  assert.equal(save.text.match(/path: (.*)/)[1], restore.text.match(/path: (.*)/)[1]);
  assert.equal(save.text.match(/key: (.*)/)[1], restore.text.match(/key: (.*)/)[1]);
});

test("cache-node-modules: false is upstream's action (nothing restored or saved)", () => {
  assert.equal(
    byName("Resolve node_modules cache").if,
    "inputs.install == 'true' && inputs.cache-node-modules == 'true'",
  );
  for (const name of ["Restore node_modules", "Drop a partial node_modules", "Save node_modules"]) {
    assert.ok(byName(name).if.includes("steps.node_modules.outputs.key != ''"), name);
  }
});

const LOCK = `---
lockfileVersion: '9.0'

importers:

  .:
    packageManagerDependencies: {}

---
lockfileVersion: '9.0'

importers:

  .:
    devDependencies: {}

  apps/web:
    dependencies: {}

  'packages/odd name':
    dependencies: {}

packages: {}
`;

function workspace(files = {}) {
  const dir = tmp();
  const all = {
    "pnpm-lock.yaml": LOCK,
    "pnpm-workspace.yaml":
      'packages:\n  - apps/*\n  - packages/*\npatchedDependencies:\n  # why\n  "a@1.0.0": patches/a@1.0.0.patch\n',
    "package.json": "{}\n",
    "apps/web/package.json": '{"name":"web"}\n',
    "packages/odd name/package.json": '{"name":"odd"}\n',
    "patches/a@1.0.0.patch": "--- a\n",
    ...files,
  };
  for (const [file, text] of Object.entries(all)) {
    if (text === null) continue;
    fs.mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
    fs.writeFileSync(path.join(dir, file), text);
  }
  return dir;
}

const ENV = {
  NAMESPACE: "full",
  VP_VERSION: "1.8.0",
  NODE_VERSION: "26.13.1",
  PNPM: "pnpm@12.14.0",
  STORE_PATH: "/home/runner/.local/share/pnpm/store/v11",
  RUNNER_OS: "Linux",
  RUNNER_ARCH: "X64",
};

function keyOf(dir, env = {}) {
  const out = path.join(tmp(), "output");
  fs.writeFileSync(out, "");
  const result = spawnSync("bash", [KEY_SCRIPT], {
    cwd: dir,
    env: { PATH: process.env.PATH, ...ENV, ...env, GITHUB_OUTPUT: out },
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  const text = fs.readFileSync(out, "utf8");
  const key = text.match(/^key=(.*)$/m)?.[1];
  const paths = text
    .match(/^paths<<FORK_NODE_MODULES_PATHS\n([\s\S]*?)\nFORK_NODE_MODULES_PATHS$/m)?.[1]
    .split("\n");
  return { key, paths, stdout: result.stdout };
}

test("the key script lists every importer's node_modules once, across lockfile documents", () => {
  const { key, paths } = keyOf(workspace());
  assert.match(
    key,
    /^fork-node-modules-v1-full-Linux-X64-node26\.13\.1-pnpm@12\.14\.0-[0-9a-f]{64}$/,
  );
  assert.deepEqual(paths, [
    "node_modules",
    "apps/web/node_modules",
    "packages/odd name/node_modules",
  ]);
});

test("the key is the same for the same inputs (a re-run, another job)", () => {
  const dir = workspace();
  assert.equal(keyOf(dir).key, keyOf(dir).key);
});

test("the key changes with every input of the install (an old or other install is never restored)", () => {
  const dir = workspace();
  const base = keyOf(dir).key;
  const edits = {
    "pnpm-lock.yaml": `${LOCK}\n`,
    "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
    "package.json": '{"x":1}\n',
    "apps/web/package.json": '{"name":"web","bin":"x"}\n',
    "packages/odd name/package.json": "{}\n",
    "patches/a@1.0.0.patch": "--- b\n",
    ".npmrc": "x=1\n",
    ".pnpmfile.cjs": "module.exports = {}\n",
  };
  for (const [file, text] of Object.entries(edits)) {
    const other = workspace({ [file]: text });
    // Same workspace path, so only the file differs.
    fs.rmSync(dir, { recursive: true });
    fs.renameSync(other, dir);
    assert.notEqual(keyOf(dir).key, base, file);
    fs.rmSync(dir, { recursive: true });
    fs.renameSync(workspace(), dir);
    assert.equal(keyOf(dir).key, base, `restored ${file}`);
  }
  for (const [name, value] of Object.entries({
    NAMESPACE: "filtered-123",
    VP_VERSION: "1.9.0",
    NODE_VERSION: "26.14.0",
    PNPM: "pnpm@12.15.0",
    STORE_PATH: "/elsewhere",
    RUNNER_OS: "macOS",
    RUNNER_ARCH: "ARM64",
  })) {
    assert.notEqual(keyOf(dir, { [name]: value }).key, base, name);
  }
  // The workspace path is in .bin shims: another checkout path is another key.
  const elsewhere = workspace();
  assert.notEqual(keyOf(elsewhere).key, base);
});

test("without a usable lockfile nothing is cached (the action installs from the store)", () => {
  for (const [label, files] of [
    ["no lockfile", { "pnpm-lock.yaml": null }],
    ["no importers", { "pnpm-lock.yaml": "lockfileVersion: '9.0'\npackages: {}\n" }],
    [
      "an importer outside the workspace",
      { "pnpm-lock.yaml": "importers:\n\n  ../outside:\n    dependencies: {}\n" },
    ],
    ["an absolute importer", { "pnpm-lock.yaml": "importers:\n\n  /abs:\n    dependencies: {}\n" }],
  ]) {
    const { key, paths, stdout } = keyOf(workspace(files));
    assert.equal(key, undefined, label);
    assert.equal(paths, undefined, label);
    assert.match(stdout, /node_modules is not cached/, label);
  }
});
