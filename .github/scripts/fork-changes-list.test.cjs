// FORK_CHANGES.md against the fork's pin table (fork-changes-list.cjs), and the guards that keep the check honest.
//   node --test .github/scripts/fork-changes-list.test.cjs
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  CHANGES,
  DEFAULTS,
  pinnedIds,
  namedIds,
  compare,
  readRepoFile,
} = require("./fork-changes-list.cjs");

// Read once: a file that cannot be read fails both checks below.
const read = () => {
  const pinned = pinnedIds(readRepoFile(DEFAULTS));
  return { pinned, ...compare(pinned, namedIds(readRepoFile(CHANGES))) };
};

test(`${CHANGES} names every fork setting (${DEFAULTS})`, () => {
  const { pinned, missing } = read();
  assert.ok(pinned.length > 0);
  assert.deepEqual(
    missing,
    [],
    `fork settings ${CHANGES} does not name: add each to its change's line`,
  );
});

test(`${CHANGES} names no setting that does not exist`, () => {
  const { unknown } = read();
  assert.deepEqual(unknown, [], `settings ${CHANGES} names that ${DEFAULTS} does not pin`);
});

const TABLE = (rows) =>
  `export interface ForkSettingDefault {\n  readonly scope: ForkSettingScope;\n}\n\n` +
  `export const FORK_SETTING_DEFAULTS: ReadonlyArray<ForkSettingDefault> = [\n${rows}];\n\n` +
  `const after = [{ scope: "server", key: "notARow" }];\n`;
const ROW = (scope, key, extra = "") =>
  `  {\n    scope: "${scope}",\n    key: "${key}",\n    default: { enabled: false },${extra}\n    off: true,\n  },\n`;

test("pinnedIds reads each row's scope and key, and nothing outside the table", () => {
  assert.deepEqual(
    pinnedIds(TABLE(ROW("server", "waitFor") + ROW("projectOverride", "waitChecks"))),
    ["projectOverride.waitChecks", "server.waitFor"],
  );
});

test("pinnedIds fails closed: no table, an empty one, a row it cannot read, an unknown scope", () => {
  assert.throws(
    () => pinnedIds("export const OTHER = [\n];\n"),
    /no "export const FORK_SETTING_DEFAULTS/u,
  );
  assert.throws(() => pinnedIds(TABLE("")), /no row read/u);
  assert.throws(
    () =>
      pinnedIds(TABLE(ROW("server", "waitFor") + `  {\n    scope: SCOPE,\n    key: "x",\n  },\n`)),
    /2 "scope:" .* but 1 read/u,
  );
  assert.throws(() => pinnedIds(TABLE(ROW("machine", "x"))), /unknown scope machine/u);
  assert.throws(() => pinnedIds("export const FORK_SETTING_DEFAULTS = [\n"), /no closing/u);
});

test("namedIds takes only `scope.key` in backticks; compare finds missing and unknown settings", () => {
  const named = namedIds(
    "`server.waitFor`, `server.waitFor`, server.bare, `other.key`, `client.theme`\n",
  );
  assert.deepEqual(named, ["client.theme", "server.waitFor"]);
  assert.deepEqual(compare(["server.agentSlots", "server.waitFor"], named), {
    missing: ["server.agentSlots"],
    unknown: ["client.theme"],
  });
  // An empty list names nothing: every setting is missing.
  assert.deepEqual(compare(["server.waitFor"], namedIds("")), {
    missing: ["server.waitFor"],
    unknown: [],
  });
});

function repo(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-changes-list-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const git = (...args) =>
    execFileSync("git", args, { cwd: root, stdio: "pipe", encoding: "utf8" });
  git("init", "-q");
  fs.mkdirSync(path.join(root, "kept"));
  fs.writeFileSync(path.join(root, "kept/a.txt"), "kept\n");
  fs.writeFileSync(path.join(root, "left.txt"), "from the commit\n");
  git("add", ".");
  git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "files");
  return { root, git };
}

test("readRepoFile reads the working tree, and the commit only for a file a sparse checkout left out", (t) => {
  const { root, git } = repo(t);
  fs.writeFileSync(path.join(root, "left.txt"), "edited\n");
  assert.equal(readRepoFile("left.txt", root), "edited\n");
  fs.rmSync(path.join(root, "left.txt"));
  // A full checkout with the file gone (deleted, not left out): an error, not the committed copy.
  assert.throws(() => readRepoFile("left.txt", root), /missing from the checkout/u);
  git("checkout", "-q", "--", "left.txt");
  git("sparse-checkout", "set", "--no-cone", "/kept/");
  assert.equal(fs.existsSync(path.join(root, "left.txt")), false);
  assert.equal(readRepoFile("left.txt", root), "from the commit\n");
  assert.throws(() => readRepoFile("not-in-the-commit.txt", root));
});
