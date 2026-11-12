const assert = require("node:assert/strict");
const test = require("node:test");
const { findSchemaChanges, parseDiff } = require("./fork-schema-check.cjs");

const file = (path, ...addedLines) => ({ path, addedLines });

test("refuses any change to a migration or the schema setup", () => {
  const reasons = findSchemaChanges([
    file("apps/server/src/persistence/Migrations/082_New.ts", "export default 1;"),
    file("apps/server/src/persistence/Migrations.ts", "import x from './x';"),
    file("apps/server/src/persistence/databaseMigrationHead.ts", "export const HEAD = 82;"),
  ]);
  assert.equal(reasons.length, 3);
});

test("refuses a schema statement added in shipped source", () => {
  const [reason] = findSchemaChanges([
    file(
      "apps/server/src/feature/Store.ts",
      "  yield* sql`ALTER TABLE threads ADD COLUMN x TEXT`;",
    ),
  ]);
  assert.match(reason, /Store\.ts: adds a schema statement: .*ALTER TABLE/);
});

test("allows schema statements in tests and ordinary changes elsewhere", () => {
  assert.deepEqual(
    findSchemaChanges([
      file("apps/server/src/feature/Store.test.ts", "CREATE TABLE scratch (id TEXT)"),
      file("apps/server/src/persistence/testkit/Fake.ts", "CREATE INDEX i ON t (x)"),
      file("apps/server/src/feature/Store.ts", "const label = 'create a table view';"),
      file("docs/notes.md", "ALTER TABLE in prose"),
      file("packages/contracts/src/settings.ts", "  mcpProfile: Schema.optional(Schema.String),"),
    ]),
    [],
  );
});

test("parseDiff keeps added lines and the old path of a deleted file", () => {
  const diff = [
    "diff --git a/apps/x.ts b/apps/x.ts",
    "--- a/apps/x.ts",
    "+++ b/apps/x.ts",
    "@@ -1,0 +2 @@",
    "+const added = 1;",
    "-const removed = 1;",
    "diff --git a/apps/server/src/persistence/Migrations/001_A.ts b/apps/server/src/persistence/Migrations/001_A.ts",
    "--- a/apps/server/src/persistence/Migrations/001_A.ts",
    "+++ /dev/null",
    "@@ -1 +0,0 @@",
    "-export default 1;",
  ].join("\n");
  assert.deepEqual(parseDiff(diff), [
    { path: "apps/x.ts", addedLines: ["const added = 1;"] },
    { path: "apps/server/src/persistence/Migrations/001_A.ts", addedLines: [] },
  ]);
  assert.equal(findSchemaChanges(parseDiff(diff)).length, 1);
});
