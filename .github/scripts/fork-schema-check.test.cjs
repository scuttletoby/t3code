const assert = require("node:assert/strict");
const { execFileSync, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { CHECK_FILES, findSchemaChanges, schemaClosure } = require("./fork-schema-check.cjs");

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

test("the closure follows relative and package subpath imports, not package roots or tests", () => {
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
    "packages/shared/src/assistantCitations.ts",
    "packages/shared/src/threadLinks.ts",
  ]);
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
  const commit = (files) => {
    write(files);
    git("add", "-A");
    git("commit", "-q", "--allow-empty", "-m", "change");
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
