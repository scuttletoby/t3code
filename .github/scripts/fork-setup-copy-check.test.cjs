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
const ACTION_DIR = path.join(root, ".github/actions/fork-setup-ci-deps");
const KEY_SCRIPT = path.join(ACTION_DIR, "node-modules-key.cjs");
const keys = require(KEY_SCRIPT);

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

// A step's text without comments and blank lines; the store steps' one fork clause (skip after a
// node_modules hit) taken out, so everything else must be upstream's text.
const FORK_CLAUSE = {
  "Restore pnpm store": [" && steps.node_modules_cache.outputs.cache-hit != 'true'", ""],
  "Save pnpm store": ["\n        steps.node_modules_cache.outputs.cache-hit != 'true' &&", ""],
};
const body = (step, fork = false) => {
  let text = step.text
    .split("\n")
    .filter((line) => !/^\s*(#.*)?$/.test(line))
    .join("\n");
  if (fork && FORK_CLAUSE[step.name]) {
    const [clause, none] = FORK_CLAUSE[step.name];
    assert.ok(text.includes(clause), `${step.name} lost its fork clause`);
    text = text.replace(clause, none);
  }
  return text;
};

test("the copy keeps every upstream step, in order, word for word (inputs, id, if, env, with, uses, run)", () => {
  let at = -1;
  for (const up of upstreamSteps) {
    const i = copySteps.findIndex((s, j) => j > at && s.name === up.name);
    assert.ok(i > at, `upstream step "${up.name}" is missing or out of order in the copy`);
    assert.equal(body(copySteps[i], true), body(up), up.name);
    at = i;
  }
  // Upstream's inputs, name and description are the copy's too.
  const upstream = fs.readFileSync(path.join(root, UPSTREAM), "utf8");
  const copy = fs.readFileSync(path.join(root, COPY), "utf8");
  const head = (text) => text.split(/^runs:\n/m)[0].split(/^name: /m)[1];
  for (const block of `name: ${head(upstream)}`.split(/^(?=  [a-z-]+:$)/m)) {
    assert.ok(copy.includes(block), `upstream's header or input is not in the copy:\n${block}`);
  }
});

test("the step-text check catches a changed env or with value", () => {
  const setup = copySteps.find((s) => s.name === "Setup Vite+");
  const changed = {
    ...setup,
    text: setup.text.replace('VP_DOWNLOAD_TIMEOUT: "120"', 'VP_DOWNLOAD_TIMEOUT: "600"'),
  };
  const up = upstreamSteps.find((s) => s.name === "Setup Vite+");
  assert.equal(body(setup, true), body(up));
  assert.notEqual(body(changed, true), body(up));
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

test("node_modules is saved right after the install, by the store's saver only, only on a push to main, never over a hit", () => {
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
  // Only a push to the default branch that tests the pushed commit: the key script's decision and
  // the event and ref again here. No pull request, dispatch, call or schedule saves.
  assert.ok(save.if.includes("steps.node_modules.outputs.save == 'true'"));
  assert.ok(save.if.includes("github.event_name == 'push'"));
  assert.ok(
    save.if.includes(
      "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
    ),
  );
  assert.doesNotMatch(save.if, /\|\|/, "no other way in");
  assert.doesNotMatch(
    save.if,
    /always\(\)|failure\(\)|cancelled\(\)/,
    "a failed install saves nothing",
  );
  const restore = byName("Restore node_modules");
  assert.equal(save.text.match(/path: (.*)/)[1], restore.text.match(/path: (.*)/)[1]);
  assert.equal(save.text.match(/key: (.*)/)[1], restore.text.match(/key: (.*)/)[1]);
  const resolve = byName("Resolve node_modules cache");
  assert.match(
    resolve.text,
    /DEFAULT_BRANCH: \$\{\{ github\.event\.repository\.default_branch \}\}/,
  );
  assert.match(resolve.run, /node "\$GITHUB_ACTION_PATH\/node-modules-key\.cjs"/);
});

test("the key script saves only for a push to the default branch that tests the pushed commit", () => {
  const sha = "a".repeat(40);
  const push = {
    GITHUB_EVENT_NAME: "push",
    GITHUB_REF: "refs/heads/main",
    DEFAULT_BRANCH: "main",
    GITHUB_SHA: sha,
  };
  assert.equal(keys.saveDecision(push, sha)[0], true);
  for (const [label, env, head] of [
    [
      "a pull request",
      { ...push, GITHUB_EVENT_NAME: "pull_request", GITHUB_REF: "refs/pull/1/merge" },
      sha,
    ],
    [
      "a dispatch on main testing another ref",
      { ...push, GITHUB_EVENT_NAME: "workflow_dispatch" },
      "b".repeat(40),
    ],
    ["a dispatch on main testing main", { ...push, GITHUB_EVENT_NAME: "workflow_dispatch" }, sha],
    ["a schedule (fork-follow's candidate)", { ...push, GITHUB_EVENT_NAME: "schedule" }, sha],
    ["a workflow_call from a release", { ...push, GITHUB_EVENT_NAME: "workflow_call" }, sha],
    ["a push to another branch", { ...push, GITHUB_REF: "refs/heads/feature" }, sha],
    ["a push testing another commit", push, "b".repeat(40)],
    ["a push with no checkout", push, ""],
    ["no default branch known", { ...push, DEFAULT_BRANCH: "" }, sha],
  ]) {
    assert.equal(keys.saveDecision(env, head)[0], false, label);
  }
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

  packages/empty: {}

packages: {}
`;

const WORKSPACE = [
  "packages:",
  "  - apps/*",
  "  - packages/*",
  "patchedDependencies:",
  "  # why",
  '  "a@1.0.0": patches/a@1.0.0.patch',
  "  b@2.0.0: patches/b.patch # a note",
  "  'c@3.0.0': 'patches/c with space.patch'",
  "",
].join("\n");

const FILES = {
  "pnpm-lock.yaml": LOCK,
  "pnpm-workspace.yaml": WORKSPACE,
  "package.json": '{"pnpm":{"patchedDependencies":{"d@4.0.0":"patches/d.patch"}}}\n',
  "apps/web/package.json": '{"name":"web"}\n',
  "packages/odd name/package.json": '{"name":"odd"}\n',
  "packages/empty/package.json": '{"name":"empty"}\n',
  "patches/a@1.0.0.patch": "--- a\n",
  "patches/b.patch": "--- b\n",
  "patches/c with space.patch": "--- c\n",
  "patches/d.patch": "--- d\n",
};

const ENV = {
  NAMESPACE: "full",
  VP_VERSION: "1.8.0",
  NODE_VERSION: "26.13.1",
  PNPM: "pnpm@12.14.0",
  STORE_PATH: "/home/runner/.local/share/pnpm/store/v11",
  RUNNER_OS: "Linux",
  RUNNER_ARCH: "X64",
  RUNNER_ENVIRONMENT: "github-hosted",
};

// compute() on an in-memory workspace: files maps a path to its text (null: absent).
function keyOf(
  files = {},
  { env = {}, cwd = "/home/runner/work/t3code/t3code", ownDir = ACTION_DIR } = {},
) {
  const all = { ...FILES, ...files };
  return keys.compute({
    env: { ...ENV, ...env },
    read: (file) => (all[file] == null ? undefined : Buffer.from(all[file])),
    cwd,
    ownDir,
    head: "",
    osRelease: "ubuntu-24.04",
  });
}

test("the key script lists every importer's node_modules once (empty and quoted importers too)", () => {
  const { key, paths, skip } = keyOf();
  assert.equal(skip, undefined);
  assert.match(
    key,
    new RegExp(
      `^fork-node-modules-${keys.VERSION}-full-Linux-X64-node26\\.13\\.1-pnpm@12\\.14\\.0-[0-9a-f]{64}$`,
    ),
  );
  assert.deepEqual(paths, [
    "node_modules",
    "apps/web/node_modules",
    "packages/empty/node_modules",
    "packages/odd name/node_modules",
  ]);
  assert.deepEqual(keys.patches(WORKSPACE, FILES["package.json"]), [
    "patches/a@1.0.0.patch",
    "patches/b.patch",
    "patches/c with space.patch",
    "patches/d.patch",
  ]);
});

test("the key is the same for the same inputs (a re-run, another job)", () => {
  assert.equal(keyOf().key, keyOf().key);
});

test("the key changes with every input of the install (an old or other install is never restored)", () => {
  const base = keyOf().key;
  const edits = {
    "pnpm-lock.yaml": `${LOCK}\n`,
    "pnpm-workspace.yaml": `${WORKSPACE}catalog: {}\n`,
    "package.json": '{"pnpm":{"patchedDependencies":{"d@4.0.0":"patches/d.patch"}},"x":1}\n',
    "apps/web/package.json": '{"name":"web","bin":"x"}\n',
    "packages/odd name/package.json": "{}\n",
    "packages/empty/package.json": "{}\n",
    "patches/a@1.0.0.patch": "--- a2\n",
    "patches/b.patch": "--- b2\n",
    "patches/c with space.patch": "--- c2\n",
    "patches/d.patch": "--- d2\n",
    ".npmrc": "x=1\n",
    ".pnpmfile.cjs": "module.exports = {}\n",
  };
  for (const [file, text] of Object.entries(edits)) {
    assert.notEqual(keyOf({ [file]: text }).key, base, file);
  }
  for (const [name, value] of Object.entries({
    NAMESPACE: "filtered-123",
    VP_VERSION: "1.9.0",
    NODE_VERSION: "26.14.0",
    PNPM: "pnpm@12.15.0",
    STORE_PATH: "/elsewhere",
    RUNNER_OS: "macOS",
    RUNNER_ARCH: "ARM64",
    NODE_ENV: "production",
    pnpm_config_node_linker: "hoisted",
    npm_config_registry: "https://example.invalid/",
  })) {
    assert.notEqual(keyOf({}, { env: { [name]: value } }).key, base, name);
  }
  assert.notEqual(keyOf({}, { cwd: "/elsewhere" }).key, base, "the workspace path");
  // The action and the key script are in the key: a change to how the tree is built is a new key.
  for (const file of ["action.yml", "node-modules-key.cjs"]) {
    const dir = tmp();
    for (const own of ["action.yml", "node-modules-key.cjs"]) {
      fs.copyFileSync(path.join(ACTION_DIR, own), path.join(dir, own));
    }
    assert.equal(keyOf({}, { ownDir: dir }).key, base, "a same copy");
    fs.appendFileSync(path.join(dir, file), "\n# changed\n");
    assert.notEqual(keyOf({}, { ownDir: dir }).key, base, file);
  }
});

test("anything the key script cannot read for sure, or a runner that is not GitHub-hosted, turns the cache off", () => {
  for (const [label, files, env = {}] of [
    ["no lockfile", { "pnpm-lock.yaml": null }],
    ["no importers", { "pnpm-lock.yaml": "lockfileVersion: '9.0'\npackages: {}\n" }],
    [
      "an importer outside the workspace",
      { "pnpm-lock.yaml": "importers:\n\n  ../outside:\n    dependencies: {}\n" },
    ],
    [
      "an importer with a .. segment",
      { "pnpm-lock.yaml": "importers:\n\n  a/../../b:\n    dependencies: {}\n" },
    ],
    ["an absolute importer", { "pnpm-lock.yaml": "importers:\n\n  /abs:\n    dependencies: {}\n" }],
    ["flow-style importers", { "pnpm-lock.yaml": "importers: {a: {}}\n" }],
    [
      "an importer line it cannot read",
      { "pnpm-lock.yaml": "importers:\n\n  a: {dependencies: {}}\n" },
    ],
    ["an odd indent", { "pnpm-lock.yaml": "importers:\n   a:\n" }],
    ["a tab indent", { "pnpm-lock.yaml": "importers:\n\t.:\n" }],
    ["flow-style patches", { "pnpm-workspace.yaml": "patchedDependencies: {a@1: p.patch}\n" }],
    [
      "a patch line it cannot read",
      { "pnpm-workspace.yaml": "patchedDependencies:\n  a@1: [p.patch]\n" },
    ],
    [
      "a nested patch entry",
      { "pnpm-workspace.yaml": "patchedDependencies:\n  a@1:\n    path: p.patch\n" },
    ],
    ["package.json that is not JSON", { "package.json": "{" }],
    ["a pnpmfile set by path", { "pnpm-workspace.yaml": "pnpmfile: tools/pnpmfile.cjs\n" }],
    ["a fleet runner", {}, { RUNNER_ENVIRONMENT: "self-hosted" }],
    ["an unknown runner", {}, { RUNNER_ENVIRONMENT: "" }],
  ]) {
    const result = keyOf(files, { env });
    assert.equal(result.key, undefined, label);
    assert.equal(typeof result.skip, "string", label);
  }
  assert.equal(
    keyOf({ "pnpm-workspace.yaml": "patchedDependencies: {}\n" }).skip,
    undefined,
    "an empty map",
  );
});

test("the key script's command writes key, paths and save, or only save=false", () => {
  const dir = tmp();
  for (const [file, text] of Object.entries(FILES)) {
    fs.mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
    fs.writeFileSync(path.join(dir, file), text);
  }
  execFileSync("git", ["init", "-q"], { cwd: dir });
  execFileSync(
    "git",
    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"],
    { cwd: dir },
  );
  const sha = execFileSync("git", ["rev-parse", "HEAD"], { cwd: dir, encoding: "utf8" }).trim();
  const run = (env) => {
    const out = path.join(tmp(), "output");
    fs.writeFileSync(out, "");
    const result = spawnSync(process.execPath, [KEY_SCRIPT], {
      cwd: dir,
      env: { PATH: process.env.PATH, ...ENV, GITHUB_OUTPUT: out, ...env },
      encoding: "utf8",
    });
    assert.equal(result.status, 0, result.stderr);
    return fs.readFileSync(out, "utf8");
  };
  const push = {
    GITHUB_EVENT_NAME: "push",
    GITHUB_REF: "refs/heads/main",
    DEFAULT_BRANCH: "main",
    GITHUB_SHA: sha,
  };
  const saved = run(push);
  assert.match(saved, /^key=fork-node-modules-/m);
  assert.match(saved, /^save=true$/m);
  assert.match(saved, /^paths<<FORK_NODE_MODULES_PATHS\nnode_modules\napps\/web\/node_modules\n/m);
  assert.match(run({ ...push, GITHUB_SHA: "b".repeat(40) }), /^save=false$/m);
  assert.match(run({ ...push, GITHUB_EVENT_NAME: "workflow_dispatch" }), /^save=false$/m);
  assert.equal(run({ ...push, RUNNER_ENVIRONMENT: "self-hosted" }), "save=false\n");
});

test("a signing or attesting workflow must turn the copy's node_modules cache off", () => {
  const ok = { [UPSTREAM]: UP, [COPY]: copyOf(blobId(Buffer.from(UP))) };
  const step = (extra) =>
    `permissions:\n  id-token: write\njobs:\n  a:\n    steps:\n      - name: Setup\n        uses: ./.github/actions/fork-setup-ci-deps\n        with:\n          install-args: --filter=x\n${extra}      - name: Next\n        run: echo\n`;
  const cached = check(tree({ ...ok, ".github/workflows/fork-x.yml": step("") }));
  assert.equal(cached.length, 1);
  assert.match(cached[0], /fork-x\.yml:7 .*signs or attests.*cache-node-modules: "false"/);
  for (const off of [
    '          cache-node-modules: "false"\n',
    "          cache-node-modules: false\n",
  ]) {
    assert.deepEqual(check(tree({ ...ok, ".github/workflows/fork-x.yml": step(off) })), [], off);
  }
  // A cache-off in the next step does not count for this one.
  const next = step("").replace(
    "        run: echo\n",
    '        with:\n          cache-node-modules: "false"\n',
  );
  assert.equal(check(tree({ ...ok, ".github/workflows/fork-x.yml": next })).length, 1);
  // A workflow that signs nothing may cache.
  const plain = step("").replace("permissions:\n  id-token: write\n", "");
  assert.deepEqual(check(tree({ ...ok, ".github/workflows/fork-x.yml": plain })), []);
  // The release workflow turns it off in every job that uses the copy.
  const release = fs.readFileSync(path.join(root, ".github/workflows/fork-release.yml"), "utf8");
  assert.match(release, /id-token: write/);
  assert.equal(
    (release.match(/uses: \.\/\.github\/actions\/fork-setup-ci-deps$/gm) ?? []).length,
    2,
  );
  assert.equal((release.match(/^ +cache-node-modules: "false"$/gm) ?? []).length, 2);
});
