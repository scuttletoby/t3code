// Refuses fork changes to the database schema: T3 has no down-migrations, so a fork migration
// strands installs that return to upstream. Fork state goes in existing JSON fields instead.
// Usage: node .github/scripts/fork-schema-check.cjs <base> <head> (checks <base>...<head>).

const { execFileSync } = require("node:child_process");

// Files that define or apply the schema. Any change to them is refused.
const SCHEMA_PATHS = [
  /^apps\/server\/src\/persistence\/Migrations(\/|\.ts$)/,
  /^apps\/server\/src\/persistence\/databaseMigrationHead\.ts$/,
  /^apps\/server\/src\/persistence\/initializeV2Database\.ts$/,
  /^apps\/server\/src\/persistence\/reconcileV2PreviewMigration\.ts$/,
];

// Schema statements added anywhere else in shipped source (tests may build their own tables).
const SCHEMA_STATEMENT =
  /\b(CREATE\s+(UNIQUE\s+)?(TABLE|INDEX|VIEW|TRIGGER|VIRTUAL\s+TABLE)|ALTER\s+TABLE|DROP\s+(TABLE|INDEX|VIEW|TRIGGER))\b/i;
const SHIPPED_SOURCE = /^(apps|packages)\/.+\.(ts|tsx|js|mjs|cjs|sql)$/;
const TEST_SOURCE = /(\.test\.|\.spec\.|\/testkit\/|\/__tests__\/|\/test\/)/;

/**
 * @param {ReadonlyArray<{ path: string; addedLines: ReadonlyArray<string> }>} files
 * @returns {string[]} one reason per refused file
 */
function findSchemaChanges(files) {
  const reasons = [];
  for (const file of files) {
    if (SCHEMA_PATHS.some((pattern) => pattern.test(file.path))) {
      reasons.push(`${file.path}: changes the database schema or its migrations`);
      continue;
    }
    if (!SHIPPED_SOURCE.test(file.path) || TEST_SOURCE.test(file.path)) continue;
    const statement = file.addedLines.find((line) => SCHEMA_STATEMENT.test(line));
    if (statement) {
      reasons.push(`${file.path}: adds a schema statement: ${statement.trim().slice(0, 120)}`);
    }
  }
  return reasons;
}

/** Parses `git diff --unified=0` output into changed files and their added lines. */
function parseDiff(diff) {
  const files = [];
  let oldPath = "";
  let current;
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git ")) {
      current = undefined;
      oldPath = "";
    } else if (current === undefined && line.startsWith("--- ")) {
      oldPath = line.slice(4).replace(/^a\//, "");
    } else if (current === undefined && line.startsWith("+++ ")) {
      // A deleted file's new side is /dev/null; it keeps its old path, so deleting is refused too.
      const target = line.slice(4);
      current = {
        path: target === "/dev/null" ? oldPath : target.replace(/^b\//, ""),
        addedLines: [],
      };
      files.push(current);
    } else if (current && line.startsWith("+")) {
      current.addedLines.push(line.slice(1));
    }
  }
  return files;
}

function main(argv) {
  const [base, head] = argv;
  if (!base || !head) {
    console.error("Usage: fork-schema-check.cjs <base> <head>");
    return 2;
  }
  const diff = execFileSync(
    "git",
    ["diff", "--unified=0", "--no-renames", "--no-color", `${base}...${head}`],
    { encoding: "utf8", maxBuffer: 256 * 1024 * 1024 },
  );
  const reasons = findSchemaChanges(parseDiff(diff));
  if (reasons.length === 0) {
    console.log("No database schema changes.");
    return 0;
  }
  console.error("Fork changes must not change the database schema (no down-migrations exist):");
  for (const reason of reasons) console.error(`- ${reason}`);
  return 1;
}

module.exports = { findSchemaChanges, parseDiff };

if (require.main === module) {
  process.exitCode = main(process.argv.slice(2));
}
