const assert = require("node:assert/strict");
const { execFileSync, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  CHECK_FILES,
  findSchemaChanges,
  parseRawDiff,
  schemaClosure,
} = require("./fork-schema-check.cjs");

const SCRIPT = path.join(__dirname, "fork-schema-check.cjs");
const WORKFLOW = path.join(__dirname, "..", "workflows", "fork-checks.yml");
const MIGRATIONS = "apps/server/src/persistence/Migrations";
const SEARCH_INDEX = "apps/server/src/orchestration-v2/messageSearchIndex.ts";

const TRIGGER_FILE = [
  'import * as SqlClient from "effect/sql/SqlClient";',
  "export const createMessageSearchTriggers = (sql) => sql`",
  "  CREATE TRIGGER message_search_update",
  "  AFTER UPDATE ON messages",
  "  WHEN (old.streaming = 0 OR new.streaming = 0)",
  "  BEGIN SELECT 1; END",
  "`;",
  "",
].join("\n");

const treeOf = (files) => ({
  files: Object.keys(files),
  has: (file) => Object.hasOwn(files, file),
  read: (file) => files[file] ?? "",
});
const changed = (file, before, after) => ({ path: file, before, after });
const noClosure = { closure: new Set(), baseHasCheck: true };

test("refuses any change to a migration or the schema setup", () => {
  const reasons = findSchemaChanges(
    [
      changed(`${MIGRATIONS}/082_New.ts`, undefined, "export default 1;"),
      changed("apps/server/src/persistence/Migrations.ts", "a", "b"),
      changed("apps/server/src/persistence/databaseMigrationHead.ts", "a", "b"),
      changed(`${MIGRATIONS}/001_A.ts`, "export default 1;", undefined),
    ],
    noClosure,
  );
  assert.equal(reasons.length, 4);
});

test("refuses a schema statement split over lines, before or after the change", () => {
  const added = "export const x = sql`\n  CREATE\n  TABLE IF NOT EXISTS fork_state (id TEXT)`;";
  const reasons = findSchemaChanges(
    [
      changed("apps/server/src/feature/Store.ts", "export {};", added),
      changed(SEARCH_INDEX, TRIGGER_FILE, TRIGGER_FILE.replace(" OR new.streaming = 0", "")),
    ],
    noClosure,
  );
  assert.equal(reasons.length, 2);
  assert.match(reasons[0], /Store\.ts: holds a schema statement \(CREATE TABLE\)/);
  assert.match(reasons[1], /messageSearchIndex\.ts: holds a schema statement \(CREATE TRIGGER\)/);
});

test("refuses statements built with interpolation, string joins or SQL comments", () => {
  const shapes = [
    "export const make = (sql, kind) => sql`CREATE ${kind} fork_state (id TEXT)`;",
    'export const ddl = "CREATE " +\n  "TABLE fork_state (id TEXT)";',
    "export const ddl = sql`CREATE /* fork */ TABLE fork_state (id TEXT)`;",
    "export const ddl = sql`ALTER -- fork\n TABLE threads ADD COLUMN x`;",
  ];
  const reasons = findSchemaChanges(
    shapes.map((text, index) => changed(`apps/server/src/fork/state${index}.ts`, "", text)),
    noClosure,
  );
  assert.equal(reasons.length, shapes.length, reasons.join("\n"));
  for (const reason of reasons) assert.match(reason, /holds a schema statement/);
  // UI text with the same words in title case stays free.
  assert.deepEqual(
    findSchemaChanges(
      [changed("apps/web/src/Git.tsx", "", "const label = `Create ${terms.shortLabel}`;")],
      noClosure,
    ),
    [],
  );
});

test("a check file or .gitattributes the branch carries unchanged from the base passes", () => {
  const changes = [...CHECK_FILES, ".gitattributes"].map((file) => ({
    ...changed(file, undefined, "same as main"),
    sameAsBase: true,
  }));
  assert.deepEqual(findSchemaChanges(changes, noClosure), []);
});

test("a branch built on an old, rebased main is told to rebase, not to merge past red", () => {
  const changes = [{ ...changed(CHECK_FILES[0], undefined, "old copy"), fromOldMain: true }];
  const reasons = findSchemaChanges(changes, { ...noClosure, staleCommits: 2 });
  assert.equal(reasons.length, 2);
  for (const reason of reasons) {
    assert.match(reason, /rebase/);
    assert.doesNotMatch(reason, /merges past/);
  }
});

test("on a stale branch, a real edit to the check is still reported as one", () => {
  const changes = [{ ...changed(CHECK_FILES[0], "a", "weakened"), fromOldMain: false }];
  const reasons = findSchemaChanges(changes, { ...noClosure, staleCommits: 1 });
  assert.equal(reasons.length, 2);
  assert.match(reasons[0], /already on main/);
  assert.match(reasons[1], /changes the schema check itself.*\(also rebase onto main\)/);
});

test("parseRawDiff fails loudly on output it cannot read", () => {
  const sha = "a".repeat(40);
  const zero = "0".repeat(40);
  assert.deepEqual(parseRawDiff(`:000000 100644 ${zero} ${sha} A\0new.ts\0`), [
    { path: "new.ts", oldBlob: undefined, newBlob: sha },
  ]);
  assert.deepEqual(parseRawDiff(""), []);
  assert.throws(() => parseRawDiff("warning: something\0x\0"), /cannot parse/);
  assert.throws(() => parseRawDiff(`:000000 100644 ${zero} ${sha} A\0`), /cannot parse/);
});

test("allows schema statements in tests and ordinary changes elsewhere", () => {
  assert.deepEqual(
    findSchemaChanges(
      [
        changed("apps/server/src/feature/Store.test.ts", "", "CREATE TABLE scratch (id TEXT)"),
        changed("apps/server/src/persistence/testkit/Fake.ts", "", "CREATE INDEX i ON t (x)"),
        changed("apps/server/src/provider/Child.testkit.ts", "", "CREATE TABLE t (x)"),
        changed("apps/server/src/feature/Store.ts", "", "const label = 'create a table view';"),
        changed("docs/notes.md", "", "ALTER TABLE in prose"),
        changed(
          "packages/contracts/src/settings.ts",
          "",
          "  mcpProfile: Schema.optional(Schema.String),",
        ),
      ],
      noClosure,
    ),
    [],
  );
});

test("refuses changes to files the migrations import", () => {
  const reasons = findSchemaChanges([changed("packages/shared/src/helper.ts", "a", "b")], {
    closure: new Set(["packages/shared/src/helper.ts"]),
    baseHasCheck: true,
  });
  assert.match(reasons[0], /helper\.ts: changes the database schema, its migrations or code/);
});

test("refuses changes to the check itself and to .gitattributes once the base has the check", () => {
  const changes = [...CHECK_FILES, ".gitattributes", "apps/.gitattributes"].map((file) =>
    changed(file, "a", "b"),
  );
  const reasons = findSchemaChanges(changes, noClosure);
  assert.equal(reasons.length, 5);
  for (const reason of reasons.slice(0, 3)) assert.match(reason, /Only the owner may do that/);
  // The series that first adds the check (base is an upstream tag) may add its files.
  assert.equal(findSchemaChanges(changes, { closure: new Set(), baseHasCheck: false }).length, 2);
});

test("the closure follows relative and package subpath imports, not root barrels or tests", () => {
  const closure = schemaClosure(
    treeOf({
      "apps/server/src/persistence/Migrations.ts": 'import m from "./Migrations/001_A.ts";',
      [`${MIGRATIONS}/001_A.ts`]: `import { t } from "../../orchestration-v2/messageSearchIndex.ts";`,
      [`${MIGRATIONS}/001_A.test.ts`]: 'import x from "../../feature/OnlyTests.ts";',
      [SEARCH_INDEX]: [
        'import { a } from "@t3tools/shared/assistantCitations";',
        'import { Thread } from "@t3tools/contracts";',
        'import type { B } from "./types.js";',
      ].join("\n"),
      "apps/server/src/orchestration-v2/types.ts": "export type B = 1;",
      "apps/server/src/feature/OnlyTests.ts": "",
      "packages/shared/package.json": JSON.stringify({
        name: "@t3tools/shared",
        exports: { "./assistantCitations": { import: "./src/assistantCitations.ts" } },
      }),
      "packages/shared/src/assistantCitations.ts": 'import { c } from "./threadLinks.ts";',
      "packages/shared/src/threadLinks.ts": "",
      "packages/contracts/package.json": JSON.stringify({
        name: "@t3tools/contracts",
        exports: { ".": "./src/index.ts" },
      }),
      "packages/contracts/src/index.ts": "",
    }),
  );
  assert.deepEqual([...closure].sort(), [
    "apps/server/src/orchestration-v2/messageSearchIndex.ts",
    "apps/server/src/orchestration-v2/types.ts",
    "apps/server/src/persistence/Migrations.ts",
    `${MIGRATIONS}/001_A.ts`,
    // `Thread` is declared nowhere, so the barrel is guarded whole (fail closed).
    "packages/contracts/src/index.ts",
    "packages/shared/src/assistantCitations.ts",
    "packages/shared/src/threadLinks.ts",
  ]);
});

test("the closure follows a package root only to the files that declare the imported names", () => {
  const closure = schemaClosure(
    treeOf({
      [`${MIGRATIONS}/064_Links.ts`]: [
        "import {",
        "  ThreadLinkedPullRequest,",
        "  type ThreadPullRequestKey,",
        '} from "@t3tools/contracts";',
        'import { Renamed } from "@t3tools/contracts";',
      ].join("\n"),
      "packages/contracts/package.json": JSON.stringify({
        name: "@t3tools/contracts",
        exports: { ".": { types: "./src/index.ts", import: "./src/index.ts" } },
      }),
      "packages/contracts/src/index.ts": [
        'export * from "./settings.ts";',
        'export * from "./threadPullRequest.ts";',
        'export { Original as Renamed } from "./aliases.ts";',
      ].join("\n"),
      "packages/contracts/src/settings.ts": "export const Settings = 1;",
      "packages/contracts/src/threadPullRequest.ts": [
        'import { Url } from "./baseSchemas.ts";',
        "export const ThreadLinkedPullRequest = Url;",
        "export type ThreadPullRequestKey = string;",
      ].join("\n"),
      "packages/contracts/src/baseSchemas.ts": "export const Url = 1;",
      "packages/contracts/src/aliases.ts": "const Original = 1;\nexport { Original };",
    }),
  );
  assert.deepEqual([...closure].sort(), [
    `${MIGRATIONS}/064_Links.ts`,
    "packages/contracts/src/aliases.ts",
    "packages/contracts/src/baseSchemas.ts",
    // The barrel on the way is guarded (an edit to it could re-point a name), not followed:
    // settings.ts stays free.
    "packages/contracts/src/index.ts",
    "packages/contracts/src/threadPullRequest.ts",
  ]);
});

// A migration importing `S` from a package root whose barrel re-exports `./a.ts` with `reexport`.
const rootImportTree = (reexport, declaring, extra = {}) =>
  treeOf({
    [`${MIGRATIONS}/001_A.ts`]: 'import { S } from "@t3tools/c";',
    "packages/c/package.json": JSON.stringify({ name: "@t3tools/c", exports: "./src/index.ts" }),
    "packages/c/src/index.ts": `${reexport}\nexport * from "./other.ts";`,
    "packages/c/src/a.ts": declaring,
    "packages/c/src/other.ts": "export const Other = 1;",
    ...extra,
  });

test("the closure traces the declaration shapes a name can have", () => {
  const shapes = [
    ['export * from "./a.ts";', "export async function S() {}"],
    ['export * from "./a.ts";', "const S = 1;\ntype T = 2;\nexport { type T, S };"],
    ['export * from "./a.ts";', "type X = 1;\nexport { type X as S };"],
    ['export { default as S } from "./a.ts";', "export default function () {}"],
    ['export * from "./a.ts";', "const o = { S: 1 };\nexport const { S } = o;"],
    ['export * from "./a.ts";', "export const [first, S] = [1, 2];"],
    ['export * from "./a.ts";', "export function* S() {}"],
  ];
  for (const [reexport, declaring] of shapes) {
    assert.deepEqual(
      [...schemaClosure(rootImportTree(reexport, declaring))].sort(),
      [`${MIGRATIONS}/001_A.ts`, "packages/c/src/a.ts", "packages/c/src/index.ts"],
      declaring,
    );
  }
});

test("a name the closure cannot trace guards the whole barrel and what it re-exports", () => {
  const closure = schemaClosure(rootImportTree('export * from "./a.ts";', "export const NotS = 1;"));
  assert.deepEqual([...closure].sort(), [
    `${MIGRATIONS}/001_A.ts`,
    "packages/c/src/a.ts",
    "packages/c/src/index.ts",
    "packages/c/src/other.ts",
  ]);
});

test("the workflow pins actions by commit SHA", () => {
  const uses = fs.readFileSync(WORKFLOW, "utf8").match(/^\s*(?:-\s*)?uses:\s*\S+/gm) ?? [];
  assert.ok(uses.length > 0);
  for (const line of uses) assert.match(line, /@[0-9a-f]{40}$/, line);
});

test("the workflow runs the default branch's copy and never runs pull request code", () => {
  const workflow = fs.readFileSync(WORKFLOW, "utf8");
  const code = workflow.replace(/^\s*#.*$/gm, "");
  assert.match(code, /^on:\n {2}pull_request_target:/m);
  assert.doesNotMatch(code, /^\s+pull_request:/m);
  // No checkout of the pull request: actions/checkout without `ref` takes the default branch.
  assert.doesNotMatch(code, /\bref:/);
  assert.doesNotMatch(code, /git (checkout|switch|worktree|reset)/);
  assert.match(code, /persist-credentials: false/);
  // Event values reach the shell only through env, never pasted into the script.
  for (const line of code.split("\n").filter((l) => /run:/.test(l))) {
    assert.doesNotMatch(line, /\$\{\{/, line);
  }
  assert.match(code, /node \.github\/scripts\/fork-schema-check\.cjs "\$BASE_SHA" "\$HEAD_SHA"/);
});

// End to end: the real git in a scratch repository, the way CI calls the check.

function scratchRepo() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-schema-check-"));
  const env = {
    ...process.env,
    GIT_CONFIG_GLOBAL: os.devNull,
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_AUTHOR_NAME: "test",
    GIT_AUTHOR_EMAIL: "test@example.invalid",
    GIT_COMMITTER_NAME: "test",
    GIT_COMMITTER_EMAIL: "test@example.invalid",
  };
  const git = (...args) => execFileSync("git", args, { cwd: dir, env, encoding: "utf8" }).trim();
  const write = (files) => {
    for (const [file, text] of Object.entries(files)) {
      const target = path.join(dir, file);
      if (text === null) {
        fs.rmSync(target);
        continue;
      }
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(target, text);
    }
  };
  const commit = (files, message = "change") => {
    write(files);
    git("add", "-A");
    git("commit", "-q", "--allow-empty", "-m", message);
    return git("rev-parse", "HEAD");
  };
  git("init", "-q", "-b", "main");
  const check = (base, head) => {
    const result = spawnSync(process.execPath, [SCRIPT, base, head], {
      cwd: dir,
      env,
      encoding: "utf8",
    });
    return { code: result.status, output: result.stdout + result.stderr };
  };
  return { dir, git, commit, check };
}

const BASE_TREE = {
  [CHECK_FILES[0]]: "// the check\n",
  [CHECK_FILES[1]]: "// its tests\n",
  [CHECK_FILES[2]]: "name: Fork checks\n",
  "apps/server/src/persistence/Migrations.ts": 'import m from "./Migrations/081_Search.ts";\n',
  [`${MIGRATIONS}/081_Search.ts`]:
    'import { createMessageSearchTriggers } from "../../orchestration-v2/messageSearchIndex.ts";\n',
  [SEARCH_INDEX]: TRIGGER_FILE,
  "apps/server/src/feature/Store.ts": "export const x = 1;\n",
};

function prAgainstBase(files, baseFiles = BASE_TREE) {
  const repo = scratchRepo();
  const base = repo.commit(baseFiles);
  const head = repo.commit(files);
  try {
    return repo.check(base, head);
  } finally {
    fs.rmSync(repo.dir, { recursive: true, force: true });
  }
}

test("e2e: an ordinary change passes and says how many files it checked", () => {
  const result = prAgainstBase({ "apps/server/src/feature/Store.ts": "export const x = 2;\n" });
  assert.equal(result.code, 0, result.output);
  assert.match(result.output, /No database schema changes \(1 changed files checked\)/);
});

test("e2e: a pull request that edits the check is refused, however small the edit", () => {
  const result = prAgainstBase({ [CHECK_FILES[0]]: "// the check, weakened\n" });
  assert.equal(result.code, 1, result.output);
  assert.match(result.output, /fork-schema-check\.cjs: changes the schema check itself/);
});

test("e2e: a pull request that deletes the workflow is refused", () => {
  const result = prAgainstBase({ [CHECK_FILES[2]]: null });
  assert.equal(result.code, 1, result.output);
  assert.match(result.output, /fork-checks\.yml: changes the schema check itself/);
});

test("e2e: the series that first adds the check passes (base has no check)", () => {
  const {
    [CHECK_FILES[0]]: _a,
    [CHECK_FILES[1]]: _b,
    [CHECK_FILES[2]]: _c,
    ...upstream
  } = BASE_TREE;
  const result = prAgainstBase(
    { [CHECK_FILES[0]]: "// the check\n", [CHECK_FILES[2]]: "name: Fork checks\n" },
    upstream,
  );
  assert.equal(result.code, 0, result.output);
});

test("e2e: an edit inside a trigger body outside the migrations folder is refused", () => {
  const result = prAgainstBase({
    [SEARCH_INDEX]: TRIGGER_FILE.replace(" OR new.streaming = 0", ""),
  });
  assert.equal(result.code, 1, result.output);
  assert.match(result.output, /messageSearchIndex\.ts: changes the database schema/);
});

test("e2e: a file the migrations import is refused even without a schema statement", () => {
  const files = {
    ...BASE_TREE,
    [SEARCH_INDEX]: 'export { keyFor } from "./searchKey.ts";\n',
    "apps/server/src/orchestration-v2/searchKey.ts": "export const keyFor = (x) => x;\n",
  };
  const repo = scratchRepo();
  const base = repo.commit(files);
  const head = repo.commit({
    "apps/server/src/orchestration-v2/searchKey.ts": "export const keyFor = (x) => `${x}!`;\n",
  });
  const result = repo.check(base, head);
  fs.rmSync(repo.dir, { recursive: true, force: true });
  assert.equal(result.code, 1, result.output);
  assert.match(result.output, /searchKey\.ts: changes the database schema/);
});

test("e2e: a new store with CREATE TABLE split over lines is refused", () => {
  const result = prAgainstBase({
    "apps/server/src/feature/ForkState.ts":
      "export const init = (sql) => sql`\n  CREATE\n  TABLE IF NOT EXISTS fork_state (id TEXT)\n`;\n",
  });
  assert.equal(result.code, 1, result.output);
  assert.match(result.output, /ForkState\.ts: holds a schema statement \(CREATE TABLE\)/);
});

test("e2e: a migration with a non-ASCII name is refused", () => {
  const result = prAgainstBase({ [`${MIGRATIONS}/082_Forké.ts`]: "export default 1;\n" });
  assert.equal(result.code, 1, result.output);
  assert.match(result.output, /Migrations\/082_Forké\.ts: changes the database schema/);
});

test("e2e: .gitattributes cannot hide a migration change", () => {
  const result = prAgainstBase({
    ".gitattributes": "apps/server/src/persistence/** -diff\n",
    [`${MIGRATIONS}/081_Search.ts`]: `${BASE_TREE[`${MIGRATIONS}/081_Search.ts`]}// more\n`,
  });
  assert.equal(result.code, 1, result.output);
  assert.match(result.output, /\.gitattributes: changes how git treats files/);
  assert.match(result.output, /081_Search\.ts: changes the database schema/);
});

test("e2e: renaming or deleting a migration is refused", () => {
  const renamed = prAgainstBase({
    [`${MIGRATIONS}/081_Search.ts`]: null,
    [`${MIGRATIONS}/081_Renamed.ts`]: BASE_TREE[`${MIGRATIONS}/081_Search.ts`],
  });
  assert.equal(renamed.code, 1, renamed.output);
  assert.match(renamed.output, /081_Search\.ts: changes/);
  assert.match(renamed.output, /081_Renamed\.ts: changes/);
});

test("e2e: a barrel-only edit that re-points a name a migration imports is refused", () => {
  const files = {
    ...BASE_TREE,
    [`${MIGRATIONS}/064_Links.ts`]: 'import { ThreadLinkedPullRequest } from "@t3tools/contracts";\n',
    "packages/contracts/package.json": JSON.stringify({
      name: "@t3tools/contracts",
      exports: { ".": "./src/index.ts" },
    }),
    "packages/contracts/src/index.ts":
      'export * from "./orchestration.ts";\nexport * from "./threadPullRequest.ts";\n',
    "packages/contracts/src/orchestration.ts": "export const ProviderKind = 1;\n",
    "packages/contracts/src/threadPullRequest.ts": "export const ThreadLinkedPullRequest = 2;\n",
  };
  const repo = scratchRepo();
  try {
    const base = repo.commit(files);
    const head = repo.commit({
      "packages/contracts/src/index.ts": `${files["packages/contracts/src/index.ts"]}export { ProviderKind as ThreadLinkedPullRequest } from "./orchestration.ts";\n`,
    });
    const result = repo.check(base, head);
    assert.equal(result.code, 1, result.output);
    assert.match(result.output, /contracts\/src\/index\.ts: changes the database schema/);
  } finally {
    fs.rmSync(repo.dir, { recursive: true, force: true });
  }
});

test("e2e: an edit to a declaring file the closure cannot trace is refused (fail closed)", () => {
  const files = {
    ...BASE_TREE,
    [`${MIGRATIONS}/001_A.ts`]: 'import { S } from "@t3tools/c";\n',
    "packages/c/package.json": JSON.stringify({ name: "@t3tools/c", exports: "./src/index.ts" }),
    "packages/c/src/index.ts": 'export * from "./a.ts";\n',
    // A shape the tracer does not know: `S` is declared, but not in a way it recognises.
    "packages/c/src/a.ts": "const o = { S: 1 };\nexport default o;\nexport const { S: renamed } = o;\nexports.S = 1;\n",
  };
  const repo = scratchRepo();
  try {
    const base = repo.commit(files);
    const head = repo.commit({ "packages/c/src/a.ts": `${files["packages/c/src/a.ts"]}// edited\n` });
    const result = repo.check(base, head);
    assert.equal(result.code, 1, result.output);
    assert.match(result.output, /packages\/c\/src\/a\.ts: changes the database schema/);
  } finally {
    fs.rmSync(repo.dir, { recursive: true, force: true });
  }
});

test("e2e: after main is rebased onto a new upstream release, an old branch is told to rebase", () => {
  const {
    [CHECK_FILES[0]]: script,
    [CHECK_FILES[1]]: tests,
    [CHECK_FILES[2]]: workflow,
    ...upstreamFiles
  } = BASE_TREE;
  const repo = scratchRepo();
  try {
    const upstreamOld = repo.commit(upstreamFiles, "upstream v1");
    const checkPatch = repo.commit(
      { [CHECK_FILES[0]]: script, [CHECK_FILES[1]]: tests, [CHECK_FILES[2]]: workflow },
      "fork: schema check",
    );
    repo.git("branch", "old-main", checkPatch);
    // A pull request opened from the old main.
    repo.git("checkout", "-q", "-b", "pr", checkPatch);
    const prHead = repo.commit({ "README.md": "fork notes\n" }, "docs: fork notes");
    // The follow workflow: a new upstream release, the patch series replayed on it, main moved.
    repo.git("checkout", "-q", "-b", "new-main", upstreamOld);
    repo.commit({ "apps/server/src/feature/Other.ts": "export {};\n" }, "upstream v2");
    repo.git("cherry-pick", checkPatch);
    const newMain = repo.git("rev-parse", "HEAD");

    const stale = repo.check(newMain, prHead);
    assert.equal(stale.code, 1, stale.output);
    assert.match(stale.output, /1 commit\(s\) of this branch are already on main/);
    assert.match(stale.output, /rebase it onto main/);
    assert.doesNotMatch(stale.output, /rebased copies|has since been rebased/);
    assert.doesNotMatch(stale.output, /merges past it|changes the schema check itself/);

    // The same branch rebased onto the new main passes.
    repo.git("checkout", "-q", "pr");
    repo.git("rebase", "-q", "--onto", "new-main", "old-main");
    const rebased = repo.check(newMain, repo.git("rev-parse", "HEAD"));
    assert.equal(rebased.code, 0, rebased.output);

    // A branch that really edits the check is still refused against the new main.
    const edit = repo.commit({ [CHECK_FILES[0]]: "// weakened\n" }, "weaken the check");
    const refused = repo.check(newMain, edit);
    assert.equal(refused.code, 1, refused.output);
    assert.match(refused.output, /fork-schema-check\.cjs: changes the schema check itself/);
  } finally {
    fs.rmSync(repo.dir, { recursive: true, force: true });
  }
});

test("e2e: a branch behind a main that moved forward normally is not called stale", () => {
  const repo = scratchRepo();
  try {
    repo.commit(BASE_TREE, "fork main");
    repo.git("checkout", "-q", "-b", "pr");
    const head = repo.commit({ "README.md": "notes\n" }, "docs: notes");
    repo.git("checkout", "-q", "main");
    const moved = repo.commit({ [CHECK_FILES[0]]: "// the check, v2\n" }, "fork: check v2");
    const result = repo.check(moved, head);
    assert.equal(result.code, 0, result.output);
  } finally {
    fs.rmSync(repo.dir, { recursive: true, force: true });
  }
});

test("e2e: a stale branch that also edits the check is told both", () => {
  const { [CHECK_FILES[0]]: script, ...rest } = BASE_TREE;
  const repo = scratchRepo();
  try {
    const old = repo.commit(rest, "upstream v1");
    const patch = repo.commit({ [CHECK_FILES[0]]: script }, "fork: schema check");
    repo.git("checkout", "-q", "-b", "pr", patch);
    const head = repo.commit({ [CHECK_FILES[0]]: "// weakened\n" }, "weaken the check");
    repo.git("checkout", "-q", "-b", "new-main", old);
    repo.commit({ "apps/server/src/feature/Other.ts": "export {};\n" }, "upstream v2");
    repo.git("cherry-pick", patch);
    const result = repo.check(repo.git("rev-parse", "HEAD"), head);
    assert.equal(result.code, 1, result.output);
    assert.match(result.output, /already on main/);
    assert.match(result.output, /fork-schema-check\.cjs: changes the schema check itself/);
  } finally {
    fs.rmSync(repo.dir, { recursive: true, force: true });
  }
});

test("e2e: a branch behind main that only cherry-picks a main commit is told to rebase", () => {
  const repo = scratchRepo();
  try {
    repo.commit(BASE_TREE, "fork main");
    repo.git("checkout", "-q", "-b", "pr");
    repo.commit({ "README.md": "notes\n" }, "docs: notes");
    repo.git("checkout", "-q", "main");
    const fix = repo.commit({ "apps/server/src/feature/Store.ts": "export const x = 3;\n" }, "fix");
    repo.git("checkout", "-q", "pr");
    repo.git("cherry-pick", fix);
    const result = repo.check(fix, repo.git("rev-parse", "HEAD"));
    assert.equal(result.code, 1, result.output);
    assert.match(result.output, /already on main \(it is based on an old main, or it copies/);
    assert.doesNotMatch(result.output, /has since been rebased/);
  } finally {
    fs.rmSync(repo.dir, { recursive: true, force: true });
  }
});
