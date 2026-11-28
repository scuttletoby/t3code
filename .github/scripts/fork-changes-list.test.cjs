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
  CHANGES_DIR,
  DEFAULTS,
  pinnedIds,
  namedIds,
  compare,
  readRepoFile,
  readChanges,
  changesNamedIds,
  checkEntryPage,
  parseChange,
  formatChange,
  relink,
  tableRows,
  fileName,
  adopt,
  printList,
} = require("./fork-changes-list.cjs");

// Read once: a file that cannot be read fails both checks below.
const read = () => {
  const pinned = pinnedIds(readRepoFile(DEFAULTS));
  return { pinned, ...compare(pinned, changesNamedIds(readChanges())) };
};

test(`${CHANGES_DIR}/ names every fork setting (${DEFAULTS})`, () => {
  const { pinned, missing } = read();
  assert.ok(pinned.length > 0);
  assert.deepEqual(
    missing,
    [],
    `fork settings no file of ${CHANGES_DIR}/ names: add each to its change's file`,
  );
});

test(`${CHANGES_DIR}/ names no setting that does not exist`, () => {
  const { unknown } = read();
  assert.deepEqual(unknown, [], `settings ${CHANGES_DIR}/ names that ${DEFAULTS} does not pin`);
});

test(`${CHANGES} is the entry page: no table row, the list is in ${CHANGES_DIR}/`, () => {
  checkEntryPage(readRepoFile(CHANGES));
  assert.ok(readChanges().length > 0);
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
    /line \d+: 1 "scope:" but 0 read/u,
  );
  assert.throws(() => pinnedIds(TABLE(ROW("machine", "x"))), /unknown scope machine/u);
  assert.throws(() => pinnedIds("export const FORK_SETTING_DEFAULTS = [\n"), /no closing/u);
});

test("pinnedIds fails closed on a table entry that is not a `{ ... }` object literal (#396)", () => {
  const first = ROW("server", "waitFor");
  // Each shape is skipped by a parse that only looks for `scope:` text, so its setting would go unchecked.
  // TABLE puts the first row on line 6, so the entry after it starts on line 12.
  const shapes = {
    spread: ["  ...EXTRA_ROWS,\n", /line 12: `\.\.\.EXTRA_ROWS,` is not a `\{ \.\.\. \}` row/u],
    "helper call": ['  row("server", "x"),\n', /line 12: `row\("server", "x"\),` is not a/u],
    "shorthand on one line": ['  { scope, key: "x" },\n', /line 12: 0 "scope:" but 0 read/u],
    "shorthand over several lines": [
      '  {\n    scope,\n    key: "x",\n    off: true,\n  },\n',
      /line 12: 0 "scope:" but 0 read/u,
    ],
  };
  for (const [shape, [entry, error]] of Object.entries(shapes)) {
    assert.throws(() => pinnedIds(TABLE(first + entry + ROW("client", "after"))), error, shape);
  }
  // An entry on the table's own first line, and a one-line empty table followed by another array.
  assert.throws(
    () => pinnedIds(`export const FORK_SETTING_DEFAULTS = [...EXTRA_ROWS,\n${first}];\n`),
    /line 1: `\.\.\.EXTRA_ROWS,` after the table's "\["/u,
  );
  assert.throws(
    () => pinnedIds(`export const FORK_SETTING_DEFAULTS = [];\nconst after = [\n${first}];\n`),
    /line 1: `\];` after the table's "\["/u,
  );
  // A `];` at column 0 inside a row ends the text table early: the rows after it would go unread.
  const cut =
    '  {\n    scope: "server",\n    key: "cut",\n    default: [\n];\n    off: true,\n  },\n';
  assert.throws(
    () => pinnedIds(TABLE(first + cut + ROW("client", "after"))),
    /FORK_SETTING_DEFAULTS line \d+: a row is still open at the table's "\];"/u,
  );
});

test("pinnedIds fails closed on a row whose scope and key could be read from the wrong place (#417)", () => {
  const first = ROW("server", "waitFor");
  const after = ROW("client", "after");
  // TABLE puts the first row on line 6, so the entry after it starts on line 12.
  const shapes = {
    // Without the count of "scope:", the nested note's scope and key would read as the row's.
    "nested scope and key": [
      '  { scope: SCOPE, key: "x", note: { scope: "server", key: "a" } },\n',
      /line 12: 2 "scope:" but 1 read/u,
    ],
    // The comment's "scope:" no longer counts, so the row's real (spread) scope is refused.
    "spread with a comment": [
      '  {\n    ...BASE, // scope: "server", key: "a"\n    off: true,\n  },\n',
      /line 12: a spread in a row/u,
    ],
    "spread on one line": [
      '  { ...BASE, scope: "server", key: "a" },\n',
      /line 12: a spread in a row/u,
    ],
    "a comment only": [
      '  {\n    /* scope: "server", key: "a" */\n    key: "x",\n  },\n',
      /line 12: 0 "scope:" but 0 read/u,
    ],
    // A string's "scope:" does not count, so a row whose own scope is not written as `scope:` is refused.
    "shorthand scope, a string with a scope and key": [
      `  { scope, key, note: "scope: 'server', key: 'fake'" },\n`,
      /line 12: 0 "scope:" but 1 read/u,
    ],
    "quoted scope, a string with a scope and key": [
      `  { "scope": "client", "key": "real", note: "scope: 'server', key: 'fake'" },\n`,
      /line 12: 0 "scope:" but 1 read/u,
    ],
    "real scope and key, a string with another pair": [
      `  { scope: "client", key: "real", note: "scope: 'server', key: 'fake'" },\n`,
      /line 12: 1 "scope:" but 2 read/u,
    ],
    // Two entries on one line read as one row, so the second would go unread.
    "two objects on one line": [
      '  { scope: "client", key: "b" }, { scope: "server", key: "c" },\n',
      /line 12: more than one `\{ \.\.\. \}` on a row's line/u,
    ],
    // A closing line the reader does not know: name it, not the two rows it then reads as one.
    "unrecognised close": [
      '  {\n    scope: "server",\n    key: "a",\n  }, // end\n',
      /line 15: `\}, \/\/ end` at the rows' indent, but the row opened on line 12 has not closed/u,
    ],
  };
  for (const [shape, [entry, error]] of Object.entries(shapes)) {
    assert.throws(() => pinnedIds(TABLE(first + entry + after)), error, shape);
  }
});

test("pinnedIds reads past comments, strings and nested objects in a row (#417)", () => {
  // A nested object's `},` sits deeper than the rows' two-space indent, so it does not end the row.
  const nested =
    '  {\n    scope: "server",\n    key: "nested",\n    default: {\n      enabled: false,\n    },\n' +
    "    off: true,\n  },\n";
  const commented =
    '  {\n    // the scope: is "server"\n    scope: "server", // see https://example.invalid/a\n' +
    '    key: "commented",\n    reason: "waits... { not a brace }",\n  },\n';
  assert.deepEqual(
    pinnedIds(
      TABLE(
        nested +
          commented +
          '  { scope: "client", key: "theme", default: { mode: "dark" }, off: "light" },\n',
      ),
    ),
    ["client.theme", "server.commented", "server.nested"],
  );
});

test("pinnedIds reads a one-line row and passes a comment between rows", () => {
  assert.deepEqual(
    pinnedIds(
      TABLE(
        ROW("server", "waitFor") +
          "  // a note between rows\n\n" +
          '  { scope: "client", key: "theme", default: "dark", off: "light" },\n',
      ),
    ),
    ["client.theme", "server.waitFor"],
  );
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

const PR = (n) => `[#${n}](https://github.com/scuttletoby/t3code/pull/${n})`;
const ENTRY = (n, change, extra = {}) => ({
  Change: change,
  Why: `Reason ${n}.`,
  Setting: "none",
  Default: "always",
  PR: PR(n),
  ...extra,
});
/** The old FORK_CHANGES.md: one table per section, padded as the formatter pads it. */
function oldPage(sections) {
  const out = ["# Fork changes", "", "Intro.", ""];
  for (const [title, entries] of sections) {
    const rows = [
      ["Change", "Why", "Setting", "Default", "PR"],
      ...entries.map((e) => [e.Change, e.Why, e.Setting, e.Default, e.PR]),
    ];
    const width = rows[0].map((_, i) => Math.max(...rows.map((row) => row[i].length)));
    const line = (row) => `| ${row.map((cell, i) => cell.padEnd(width[i])).join(" | ")} |`;
    out.push(
      `## ${title}`,
      "",
      line(rows[0]),
      `| ${width.map((w) => "-".repeat(w)).join(" | ")} |`,
    );
    out.push(...rows.slice(1).map(line), "");
  }
  return out.join("\n");
}
const APP = "In T3 Code itself";
const REPO = "Repository, CI and releases";
const NEW_PAGE = "# Fork changes\n\nEvery change has its own file in docs/fork/changes/.\n";

function tempRepo(t, name) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), name));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const git = (...args) =>
    execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@example.invalid", ...args], {
      cwd: root,
      stdio: "pipe",
      encoding: "utf8",
    });
  git("init", "-q", "-b", "main");
  const write = (file, text) => {
    fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    fs.writeFileSync(path.join(root, file), text);
  };
  const commit = (message) => {
    git("add", "-A");
    git("commit", "-qm", message);
  };
  /** `git merge <branch>`: the files left in conflict, none when it merged. */
  const merge = (branch) => {
    try {
      git("merge", "-q", "--no-edit", branch);
      return [];
    } catch {
      return git("diff", "--name-only", "--diff-filter=U").split("\n").filter(Boolean);
    }
  };
  return { root, git, write, commit, merge };
}

/** Writes adopt's result into a folder, as the `adopt` command does. */
function apply(root, result) {
  for (const file of result.write) {
    fs.mkdirSync(path.dirname(path.join(root, file.path)), { recursive: true });
    fs.writeFileSync(path.join(root, file.path), file.text);
  }
  for (const file of result.remove) fs.rmSync(path.join(root, file));
}

test("two pull requests that each add or edit a different change merge without a conflict", (t) => {
  const { root, git, write, commit, merge } = tempRepo(t, "fork-changes-merge-");
  const file = (name) => `${CHANGES_DIR}/app/${name}`;
  write(CHANGES, NEW_PAGE);
  for (const n of [1, 2, 3]) write(file(`${n}-change.md`), formatChange(ENTRY(n, `Change ${n}.`)));
  commit("base");
  git("checkout", "-qb", "a");
  // A long edit: in a padded table this one row re-pads every other line.
  write(
    file("1-change.md"),
    formatChange(ENTRY(1, `Change 1, much longer now: ${"x".repeat(300)}.`)),
  );
  write(file("10-added-by-a.md"), formatChange(ENTRY(10, "Added by a.")));
  commit("a");
  git("checkout", "-q", "main");
  git("checkout", "-qb", "b");
  write(file("2-change.md"), formatChange(ENTRY(2, "Change 2, edited by b.")));
  write(file("11-added-by-b.md"), formatChange(ENTRY(11, "Added by b.")));
  commit("b");
  assert.deepEqual(merge("a"), []);
  const changes = Object.fromEntries(readChanges(root).map((c) => [c.name, c.entry.Change]));
  assert.deepEqual(Object.keys(changes).sort(), [
    "1-change.md",
    "10-added-by-a.md",
    "11-added-by-b.md",
    "2-change.md",
    "3-change.md",
  ]);
  assert.match(changes["1-change.md"], /much longer now/u);
  assert.equal(changes["2-change.md"], "Change 2, edited by b.");

  // The same two pull requests on the old padded table conflict: the reason for the files.
  const old = tempRepo(t, "fork-changes-table-");
  const base = [1, 2, 3].map((n) => ENTRY(n, `Change ${n}.`));
  old.write(CHANGES, oldPage([[APP, base]]));
  old.commit("base");
  old.git("checkout", "-qb", "a");
  old.write(
    CHANGES,
    oldPage([[APP, [ENTRY(1, `Change 1, ${"x".repeat(300)}.`), ...base.slice(1)]]]),
  );
  old.commit("a");
  old.git("checkout", "-q", "main");
  old.git("checkout", "-qb", "b");
  old.write(CHANGES, oldPage([[APP, [base[0], ENTRY(2, "Change 2, edited by b."), base[2]]]]));
  old.commit("b");
  assert.deepEqual(old.merge("a"), [CHANGES]);
});

test("readChanges fails closed: no folder, no change file, a stray file, a bad name, a file of another shape", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fork-changes-read-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const write = (file, text) => {
    fs.mkdirSync(path.dirname(path.join(root, CHANGES_DIR, file)), { recursive: true });
    fs.writeFileSync(path.join(root, CHANGES_DIR, file), text);
  };
  const drop = (file) => fs.rmSync(path.join(root, CHANGES_DIR, file), { recursive: true });
  assert.throws(() => readChanges(root), /docs\/fork\/changes\/ is missing from the checkout/u);
  write("README.md", "# The folder's own page, not a change\n");
  assert.throws(() => readChanges(root), /no change file/u);
  write("app/1-one.md", formatChange(ENTRY(1, "One.")));
  assert.deepEqual(
    readChanges(root).map((c) => [c.section, c.name]),
    [["app", "1-one.md"]],
  );
  const refused = {
    "notes.md": [formatChange(ENTRY(2, "Two.")), /notes\.md: not a change file/u],
    "other/2-two.md": [formatChange(ENTRY(2, "Two.")), /other\/2-two\.md: not a change file/u],
    "app/deeper/2-two.md": [
      formatChange(ENTRY(2, "Two.")),
      /deeper\/2-two\.md: not a change file/u,
    ],
    "app/2_Two.md": [formatChange(ENTRY(2, "Two.")), /a change file's name is lowercase/u],
    "app/2-four-lines.md": [
      "- Change: a\n- Why: b\n- Setting: c\n- Default: d\n",
      /4 lines, not 5/u,
    ],
    "app/2-extra-line.md": [`${formatChange(ENTRY(2, "Two."))}\nMore text.\n`, /7 lines, not 5/u],
    "app/2-order.md": [
      "- Why: b\n- Change: a\n- Setting: c\n- Default: d\n- PR: e\n",
      /line 1 does not start with "- Change: "/u,
    ],
    "app/2-empty.md": [formatChange(ENTRY(2, "Two.", { Why: " " })), /line 2: an empty Why/u],
  };
  for (const [file, [text, error]] of Object.entries(refused)) {
    write(file, text);
    assert.throws(() => readChanges(root), error, file);
    drop(file.split("/").length === 3 ? file.split("/").slice(0, 2).join("/") : file);
  }
  assert.equal(readChanges(root).length, 1);
});

test("readChanges reads the folder from the commit only in a sparse checkout that left it out", (t) => {
  const { root, git, write, commit } = tempRepo(t, "fork-changes-sparse-");
  write("kept/a.txt", "kept\n");
  write(`${CHANGES_DIR}/repo/2-two.md`, formatChange(ENTRY(2, "Two, see [x](../../x.md#a).")));
  commit("files");
  git("sparse-checkout", "set", "--no-cone", "/kept/");
  assert.equal(fs.existsSync(path.join(root, CHANGES_DIR)), false);
  const [change] = readChanges(root);
  assert.equal(change.path, `${CHANGES_DIR}/repo/2-two.md`);
  // Links read repository-relative, whatever folder the file is in.
  assert.equal(change.entry.Change, "Two, see [x](docs/fork/x.md#a).");
});

test("checkEntryPage refuses a table row on the entry page and names the adopt command", () => {
  checkEntryPage(NEW_PAGE);
  checkEntryPage("");
  assert.throws(
    () => checkEntryPage(oldPage([[APP, [ENTRY(1, "One.")]]])),
    /FORK_CHANGES\.md line \d+: a table row.*fork-changes-list\.cjs adopt/su,
  );
});

test("parseChange and formatChange round-trip; relink moves relative links only", () => {
  const entry = ENTRY(5, "Five with `server.waitFor` and a | pipe.");
  assert.deepEqual(parseChange(formatChange(entry), "x"), entry);
  const text = "[a](docs/fork/x.md#h), [b](https://e.invalid/a), [c](#top), [d](/abs)";
  const moved = relink(text, "", "docs/fork/changes/app");
  assert.equal(moved, "[a](../../x.md#h), [b](https://e.invalid/a), [c](#top), [d](/abs)");
  assert.equal(relink(moved, "docs/fork/changes/app", ""), text);
  assert.equal(
    fileName(ENTRY(384, "The fork's service layers in the server are wired")),
    "384-forks-service-layers-in-server-are.md",
  );
  assert.equal(fileName({ ...ENTRY(1, "A thing."), PR: "none yet" }), "change-thing.md");
});

test("printList prints every change as the old table, which reads back to the same rows", () => {
  const changes = [
    { section: "repo", name: "4-four.md", entry: ENTRY(4, "Four.") },
    { section: "app", name: "9-nine.md", entry: ENTRY(9, "Nine | with a pipe.") },
    { section: "app", name: "3-three.md", entry: ENTRY(3, "Three.") },
  ];
  const printed = printList(changes);
  assert.deepEqual(tableRows(printed), [
    { section: "app", entry: ENTRY(3, "Three.") },
    { section: "app", entry: ENTRY(9, "Nine | with a pipe.") },
    { section: "repo", entry: ENTRY(4, "Four.") },
  ]);
});

/** The change files adopt would make from a whole old page, as readChanges returns them. */
function converted(page) {
  return adopt("", page, []).write.map((file) => {
    const [section, name] = file.path.slice(CHANGES_DIR.length + 1).split("/");
    const entry = parseChange(file.text, file.path);
    return {
      section,
      name,
      path: file.path,
      text: file.text,
      entry: relinkEntry(entry, file.path),
    };
  });
}
const relinkEntry = (entry, file) =>
  Object.fromEntries(
    Object.entries(entry).map(([k, v]) => [k, relink(v, path.posix.dirname(file), "")]),
  );

test("adopt: an open pull request's added, edited and removed rows become file writes and removals", () => {
  const r1 = ENTRY(10, "Ten, see [docs](docs/fork/README.md).");
  const [r2, r3, r4] = [ENTRY(11, "Eleven."), ENTRY(12, "Twelve."), ENTRY(20, "Twenty.")];
  const basePage = oldPage([
    [APP, [r1, r2, r3]],
    [REPO, [r4]],
  ]);
  const main = converted(basePage);
  assert.equal(main.length, 4);
  assert.equal(main[0].path, `${CHANGES_DIR}/app/10-ten-see-docs.md`);
  assert.match(main[0].text, /\[docs\]\(\.\.\/\.\.\/README\.md\)/u);
  const fileOf = (entry) => main.find((c) => c.entry.Change === entry.Change).path;

  // The pull request edits r1 (same first PR), adds r5, removes r3; the long row re-pads the whole table.
  const r1edit = { ...r1, Change: `Ten, edited: ${"y".repeat(200)}.` };
  const r5 = ENTRY(30, "Thirty, `server.waitFor`.", { Setting: "`server.waitFor`" });
  const headPage = oldPage([
    [APP, [r1edit, r2, r5]],
    [REPO, [r4]],
  ]);
  const result = adopt(basePage, headPage, main);
  assert.deepEqual(result.conflicts, []);
  assert.deepEqual(result.remove, [fileOf(r3)]);
  assert.deepEqual(
    result.write.map((w) => w.path),
    [fileOf(r1), `${CHANGES_DIR}/app/30-thirty-server-waitfor.md`],
  );
  assert.match(result.write[0].text, /^- Change: Ten, edited: y+\.$/mu);

  // Runs again after it was applied (or after a run that stopped half-way): only what is still missing.
  const after = [
    ...main.filter((c) => c.path !== fileOf(r1) && c.path !== fileOf(r3)),
    ...converted(oldPage([[APP, [r1edit, r5]]])).map((c, i) => ({
      ...c,
      path: result.write[i].path,
    })),
  ];
  assert.deepEqual(adopt(basePage, headPage, after), { write: [], remove: [], conflicts: [] });
  const half = main.filter((c) => c.path !== fileOf(r3)); // r3 removed, nothing written yet
  assert.deepEqual(
    adopt(basePage, headPage, half).write.map((w) => w.path),
    result.write.map((w) => w.path),
  );
  assert.deepEqual(adopt(basePage, headPage, half).remove, []);
});

test("adopt leaves a row whose file changed on the base since, and names it", () => {
  const [r1, r2, r3] = [ENTRY(10, "Ten."), ENTRY(11, "Eleven."), ENTRY(12, "Twelve.")];
  const basePage = oldPage([[APP, [r1, r2, r3]]]);
  // Since the merge base, main changed r1's and r3's files.
  const main = converted(basePage).map((c) =>
    c.entry.Change === "Eleven." ? c : { ...c, entry: { ...c.entry, Why: "Changed on main." } },
  );
  const r4 = ENTRY(13, "Thirteen.");
  const headPage = oldPage([[APP, [{ ...r1, Why: "Edited by the pull request." }, r2, r4]]]);
  const result = adopt(basePage, headPage, main);
  assert.deepEqual(
    result.write.map((w) => w.path),
    [`${CHANGES_DIR}/app/13-thirteen.md`],
  );
  assert.deepEqual(result.remove, []);
  assert.equal(result.conflicts.length, 2);
  assert.match(
    result.conflicts[0],
    /"Ten\.\.\.\.": the row this pull request edits changed on the base since \(now docs\/fork\/changes\/app\/10-ten\.md\)/u,
  );
  assert.match(
    result.conflicts[1],
    /"Twelve\.\.\.\.": this pull request removes the row, but it changed/u,
  );
});

test("adopt does nothing on empty, unchanged or new-format pages, or a row main already has", () => {
  const basePage = oldPage([[APP, [ENTRY(10, "Ten.")]]]);
  const main = converted(basePage);
  const none = { write: [], remove: [], conflicts: [] };
  assert.deepEqual(adopt("", "", main), none);
  assert.deepEqual(adopt(basePage, basePage, main), none);
  assert.deepEqual(adopt(NEW_PAGE, NEW_PAGE, main), none);
  const both = oldPage([[APP, [ENTRY(10, "Ten."), ENTRY(11, "Eleven.")]]]);
  assert.deepEqual(adopt(basePage, both, converted(both)), none);
});

test("adopt removes nothing when the pull request's page has no table row but its base has (adopted, or a stale base)", () => {
  const basePage = oldPage([[APP, [ENTRY(10, "Ten."), ENTRY(11, "Eleven.")]]]);
  const result = adopt(basePage, NEW_PAGE, converted(basePage));
  assert.deepEqual(result.remove, []);
  assert.deepEqual(result.write, []);
  assert.equal(result.conflicts.length, 1);
  assert.match(result.conflicts[0], /no table row but its base has 2: nothing removed/u);
});

test("adopt writes no second file when the base has no table but the pull request's side has the old list", () => {
  // A branch from before the list's history: its merge base has no FORK_CHANGES.md, and its table has
  // the old wording of a row main edited since.
  const [r1, r2] = [ENTRY(10, "Ten."), ENTRY(11, "Eleven, edited on main.")];
  const main = converted(oldPage([[APP, [r1, r2]]]));
  const headPage = oldPage([
    [APP, [r1, { ...r2, Change: "Eleven, edited on main, in old words." }]],
  ]);
  for (const basePage of ["", NEW_PAGE]) {
    const result = adopt(basePage, headPage, main);
    assert.deepEqual([result.write, result.remove], [[], []]);
    assert.match(
      result.conflicts.join("\n"),
      /base's FORK_CHANGES\.md has no table row.*old list \(2 rows\)/u,
    );
  }
  // One row added to the new page by hand (no old list) is adopted; one that names a held change is not.
  const one = `${NEW_PAGE}\n## ${APP}\n\n| Change | Why | Setting | Default | PR |\n| --- | --- | --- | --- | --- |\n`;
  const fresh = adopt(
    NEW_PAGE,
    `${one}| ${Object.values(ENTRY(40, "Forty.")).join(" | ")} |\n`,
    main,
  );
  assert.deepEqual(
    fresh.write.map((w) => w.path),
    [`${CHANGES_DIR}/app/40-forty.md`],
  );
  const again = { ...r2, Why: "Other words." };
  const dup = adopt(NEW_PAGE, `${one}| ${Object.values(again).join(" | ")} |\n`, main);
  assert.deepEqual(dup.write, []);
  assert.match(
    dup.conflicts[0],
    /a change file already holds this change \(docs\/fork\/changes\/app\/11-eleven-edited-on-main\.md\)/u,
  );
});

test("readChanges refuses one change in two files, and names a leftover .md.tmp with its fix", (t) => {
  const { root, write } = tempRepo(t, "fork-changes-dup-");
  const text = formatChange(ENTRY(10, "Ten."));
  write(`${CHANGES_DIR}/app/10-ten.md`, text);
  write(`${CHANGES_DIR}/app/10-ten-2.md`, text.replace("Reason 10.", "Older words."));
  assert.throws(() => readChanges(root), /10-ten\.md: the same change as .*10-ten-2\.md/u);
  fs.rmSync(path.join(root, CHANGES_DIR, "app/10-ten-2.md"));
  write(`${CHANGES_DIR}/app/other.md`, text.replace("Ten.", "Ten.").replace("[#10]", "[#11]"));
  assert.throws(() => readChanges(root), /the same Change line/u);
  fs.rmSync(path.join(root, CHANGES_DIR, "app/other.md"));
  write(`${CHANGES_DIR}/app/11-eleven.md.tmp`, text);
  assert.throws(
    () => readChanges(root),
    /11-eleven\.md\.tmp: left by an adopt run that stopped; run adopt again/u,
  );
});

/** A temp repo where `pr` edits the old table (r2) and adds r5, and main since moved the list to files. */
function adoptRepo(t, name) {
  const repoT = tempRepo(t, name);
  const { root, git, write, commit } = repoT;
  const script = path.join(__dirname, "fork-changes-list.cjs");
  const run = (...args) =>
    execFileSync("node", [script, ...args], { cwd: root, encoding: "utf8", stdio: "pipe" });
  /** The command's exit status and output, for a run that may fail. */
  const status = (...args) => {
    try {
      return { code: 0, out: run(...args) };
    } catch (error) {
      return { code: error.status, out: `${error.stdout}${error.stderr}` };
    }
  };
  const [r1, r2] = [ENTRY(10, "Ten."), ENTRY(11, "Eleven.")];
  const basePage = oldPage([[APP, [r1, r2]]]);
  write(CHANGES, basePage);
  commit("old table");
  git("tag", "old");
  git("checkout", "-qb", "pr");
  const r5 = ENTRY(30, "Thirty, from the pull request.");
  write(CHANGES, oldPage([[APP, [r1, { ...r2, Why: "Edited by the pull request." }, r5]]]));
  commit("pr edits the table");
  git("checkout", "-q", "main");
  apply(root, adopt("", basePage, []));
  write(CHANGES, NEW_PAGE);
  commit("one file per change");
  const why = () => Object.fromEntries(readChanges(root).map((c) => [c.name, c.entry.Why]));
  const adopted = {
    "10-ten.md": "Reason 10.",
    "11-eleven.md": "Edited by the pull request.",
    "30-thirty-from-pull-request.md": "Reason 30.",
  };
  return { ...repoT, run, status, why, adopted };
}

test("the adopt command moves an open pull request's table edits to files during its merge of main", (t) => {
  const { root, git, merge, run, why, adopted } = adoptRepo(t, "fork-changes-adopt-");
  git("checkout", "-q", "pr");
  assert.throws(
    () => run("adopt", "main"),
    /docs\/fork\/changes\/ is not in this checkout: merge main first/u,
  );
  assert.deepEqual(merge("main"), [CHANGES]);
  const out = run("adopt", "main");
  assert.match(out, /wrote {3}docs\/fork\/changes\/app\/11-eleven\.md/u);
  assert.match(out, /wrote {3}docs\/fork\/changes\/app\/30-thirty-from-pull-request\.md/u);
  // adopt puts main's entry page in place itself, once every row is a file: no checkout that could drop one.
  assert.equal(fs.readFileSync(path.join(root, CHANGES), "utf8"), NEW_PAGE);
  git("add", CHANGES, CHANGES_DIR);
  git("commit", "-q", "--no-edit");
  checkEntryPage(fs.readFileSync(path.join(root, CHANGES), "utf8"));
  assert.deepEqual(why(), adopted);
  // Again, after the merge: HEAD's page has no table row, so nothing is left to adopt.
  assert.match(run("adopt", "main"), /nothing to adopt/u);
});

test("the adopt command reads the replayed commit during a cherry-pick or rebase, not HEAD", (t) => {
  for (const how of ["cherry-pick", "rebase"]) {
    const { root, git, run, why, adopted } = adoptRepo(t, `fork-changes-${how}-`);
    if (how === "cherry-pick") {
      git("checkout", "-qb", "replay", "main");
      assert.throws(() => git("cherry-pick", "pr"));
    } else {
      git("checkout", "-q", "pr");
      assert.throws(() => git("rebase", "-q", "main"));
    }
    const out = run("adopt", "main");
    assert.match(out, new RegExp(`\\(${how}\\)`, "u"));
    assert.match(out, /wrote {3}docs\/fork\/changes\/app\/30-thirty-from-pull-request\.md/u);
    assert.match(out, new RegExp(`next: .*git ${how} --continue`, "u"));
    assert.equal(fs.readFileSync(path.join(root, CHANGES), "utf8"), NEW_PAGE);
    git("add", CHANGES, CHANGES_DIR);
    execFileSync(
      "git",
      ["-c", "user.name=t", "-c", "user.email=t@example.invalid", how, "--continue"],
      {
        cwd: root,
        stdio: "pipe",
        env: { ...process.env, GIT_EDITOR: "true" },
      },
    );
    assert.deepEqual(why(), adopted);
    assert.match(run("adopt", "main"), /nothing to adopt/u);
  }
});

test("the adopt command removes nothing on a branch with the new page when its base still has the old table", (t) => {
  const { root, git, status } = adoptRepo(t, "fork-changes-stale-");
  git("checkout", "-qb", "stacked", "main");
  // `old` stands for a stale origin/main, or the base of a branch stacked on this change before it lands.
  const files = () => readChanges(root).map((c) => c.path);
  const before = files();
  const stale = status("adopt", "old");
  assert.equal(stale.code, 1);
  assert.match(stale.out, /old table still: nothing to adopt onto.*\nnothing changed/su);
  assert.doesNotMatch(stale.out, /removed/u);
  assert.deepEqual(files(), before);
  // The pure check under it: the pull request's side has no table row, its base has.
  const sideNoTable = adopt(git("show", `old:${CHANGES}`), NEW_PAGE, readChanges(root));
  assert.deepEqual([sideNoTable.remove, sideNoTable.conflicts.length], [[], 1]);
});

test("the adopt command removes a leftover .md.tmp, and an abort is undone by checkout and clean", (t) => {
  const { root, git, merge, run, write } = adoptRepo(t, "fork-changes-abort-");
  git("checkout", "-q", "pr");
  assert.deepEqual(merge("main"), [CHANGES]);
  write(`${CHANGES_DIR}/app/30-thirty-from-pull-request.md.tmp`, "half");
  const out = run("adopt", "main");
  assert.match(
    out,
    /removed docs\/fork\/changes\/app\/30-thirty-from-pull-request\.md\.tmp \(left by a run that stopped\)/u,
  );
  assert.equal(
    fs.existsSync(path.join(root, CHANGES_DIR, "app/30-thirty-from-pull-request.md.tmp")),
    false,
  );
  // An abort refuses while adopt's edits are in tracked files; the documented undo: the folder back from the
  // index, then the abort, then adopt's new files gone.
  assert.throws(() => git("merge", "--abort"), /not uptodate/u);
  git("checkout", "--", CHANGES_DIR);
  git("merge", "--abort");
  git("clean", "-fdq", "--", CHANGES_DIR);
  assert.equal(git("status", "--porcelain"), "");
  assert.equal(fs.existsSync(path.join(root, CHANGES_DIR)), false);
});

/** The working tree's state: `git status` and every file's text under the list, to show a refusal changed nothing. */
function snapshot(root, git) {
  const files = {};
  const walk = (dir) => {
    if (!fs.existsSync(dir)) return;
    for (const item of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, item.name);
      if (item.isDirectory()) walk(full);
      else files[path.relative(root, full)] = fs.readFileSync(full, "utf8");
    }
  };
  walk(path.join(root, CHANGES_DIR));
  files[CHANGES] = fs.readFileSync(path.join(root, CHANGES), "utf8");
  return { status: git("status", "--porcelain"), files };
}

test("a REBASE_HEAD left by a finished rebase is not a rebase in progress: later merges read their own sides", (t) => {
  const { root, git, write, commit, merge, status, why, adopted } = adoptRepo(
    t,
    "fork-changes-stale-rebase-",
  );
  // P adds row Z to the old table; Y, stacked on P, adds row W. Y is replayed onto main (a conflicted rebase,
  // adopted and finished), and git leaves .git/REBASE_HEAD behind.
  const [r1, r2] = [ENTRY(10, "Ten."), ENTRY(11, "Eleven.")];
  const [z, w] = [ENTRY(50, "Fifty, from P."), ENTRY(60, "Sixty, from Y.")];
  git("checkout", "-qb", "P", "old");
  write(CHANGES, oldPage([[APP, [r1, r2, z]]]));
  commit("P adds Z");
  git("checkout", "-qb", "Y");
  write(CHANGES, oldPage([[APP, [r1, r2, z, w]]]));
  commit("Y adds W");
  assert.throws(() => git("rebase", "-q", "--onto", "main", "P"));
  const replayed = git("rev-parse", "REBASE_HEAD").trim();
  assert.match(
    status("adopt", "main").out,
    /wrote {3}docs\/fork\/changes\/app\/60-sixty-from-y\.md/u,
  );
  git("add", CHANGES, CHANGES_DIR);
  execFileSync(
    "git",
    ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "rebase", "--continue"],
    {
      cwd: root,
      stdio: "pipe",
      env: { ...process.env, GIT_EDITOR: "true" },
    },
  );
  const rebaseHead = path.join(root, ".git", "REBASE_HEAD");
  if (!fs.existsSync(rebaseHead)) fs.writeFileSync(rebaseHead, `${replayed}\n`); // as git 2.43 leaves it

  // The stacked step: Y merges P, which still has the old table. Refused, nothing changed (before: read as the
  // finished rebase, the page restored with exit 0, and Z in no file).
  assert.deepEqual(merge("P"), [CHANGES]);
  const before = snapshot(root, git);
  const stacked = status("adopt");
  assert.equal(stacked.code, 1, stacked.out);
  assert.match(stacked.out, /\(merge\)/u);
  assert.match(stacked.out, /old table still: nothing to adopt onto/u);
  assert.deepEqual(snapshot(root, git), before);
  git("merge", "--abort");

  // An old pull request merging main in the same clone adopts its own rows (before: refused, "git fetch").
  git("checkout", "-q", "pr");
  assert.deepEqual(merge("main"), [CHANGES]);
  const plain = status("adopt", "main");
  assert.equal(plain.code, 0, plain.out);
  assert.match(plain.out, /\(merge\)/u);
  assert.equal(fs.readFileSync(path.join(root, CHANGES), "utf8"), NEW_PAGE);
  git("add", CHANGES, CHANGES_DIR);
  git("commit", "-q", "--no-edit");
  assert.deepEqual(
    Object.fromEntries(Object.entries(why()).filter(([name]) => name in adopted)),
    adopted,
  );
});

test("conflict markers in the entry page's own text: adopt refuses and checkEntryPage rejects them", (t) => {
  const { root, git, write, commit, merge, status } = adoptRepo(t, "fork-changes-markers-");
  git("checkout", "-qb", "a", "main");
  write(CHANGES, NEW_PAGE.replace("Every change", "Each change"));
  commit("a edits the page");
  git("checkout", "-qb", "b", "main");
  write(CHANGES, NEW_PAGE.replace("Every change", "All changes"));
  commit("b edits the page");
  assert.deepEqual(merge("a"), [CHANGES]);
  const before = snapshot(root, git);
  const out = status("adopt", "main");
  assert.equal(out.code, 1, out.out);
  assert.match(out.out, /conflict markers in its own text/u);
  assert.deepEqual(snapshot(root, git), before);
  assert.throws(
    () => checkEntryPage(fs.readFileSync(path.join(root, CHANGES), "utf8")),
    /FORK_CHANGES\.md line \d+: a conflict marker/u,
  );
});

test("adopt pairs an edited row by its Change line before its number: a new row above it with the same number", () => {
  const waits = ENTRY(84, "Waits: agents can wait.");
  const slots = ENTRY(84, "Agent slots: refused when full.");
  const basePage = oldPage([[APP, [ENTRY(10, "Ten."), waits, slots]]]);
  const main = converted(basePage);
  const fileOf = (entry) => main.find((c) => c.entry.Change === entry.Change).path;
  const added = ENTRY(84, "Waits, part 2: a new change.", { PR: `${PR(84)}, ${PR(99)}` });
  // The edit keeps the Change line: it goes to its own file, the new row to a new one.
  const kept = adopt(
    basePage,
    oldPage([[APP, [ENTRY(10, "Ten."), added, { ...waits, Why: "Edited." }, slots]]]),
    main,
  );
  assert.deepEqual(kept.conflicts, []);
  assert.deepEqual(kept.remove, []);
  assert.deepEqual(
    kept.write.map((w) => [w.path, /^- Change: (.*)$/mu.exec(w.text)[1]]),
    [
      [`${CHANGES_DIR}/app/84-waits-part-2-new-change.md`, added.Change],
      [fileOf(waits), waits.Change],
    ],
  );
  // The edit changes the Change line too: two added rows and one removed share #84, so a person pairs them.
  const reworded = adopt(
    basePage,
    oldPage([[APP, [ENTRY(10, "Ten."), added, { ...waits, Change: "Waits: reworded." }, slots]]]),
    main,
  );
  assert.deepEqual([reworded.write, reworded.remove], [[], []]);
  assert.equal(reworded.conflicts.length, 2);
  assert.match(reworded.conflicts[0], /more than one row with its first pull request \(#84\)/u);
});

test("every adopt refusal leaves the tree as it was: a row main changed since, next to an added one", (t) => {
  const { root, git, commit, merge, status } = adoptRepo(t, "fork-changes-refuse-");
  git("checkout", "-q", "main");
  const file = path.join(root, CHANGES_DIR, "app/11-eleven.md");
  fs.writeFileSync(file, fs.readFileSync(file, "utf8").replace("Reason 11.", "Changed on main."));
  commit("main edits eleven");
  git("checkout", "-q", "pr");
  assert.deepEqual(merge("main"), [CHANGES]);
  const before = snapshot(root, git);
  const out = status("adopt", "main");
  assert.equal(out.code, 1, out.out);
  assert.match(out.out, /the row this pull request edits changed on the base since/u);
  assert.match(out.out, /nothing changed/u);
  assert.doesNotMatch(out.out, /wrote/u); // before: the added row's file was written
  assert.deepEqual(snapshot(root, git), before);
});

test("a merge committed with the pull request's own table: adopt says to undo it, and that advice works", (t) => {
  const { root, git, merge, status, why, adopted } = adoptRepo(t, "fork-changes-kept-");
  git("checkout", "-q", "pr");
  assert.deepEqual(merge("main"), [CHANGES]);
  git("checkout", "--ours", CHANGES);
  git("add", CHANGES);
  git("commit", "-q", "--no-edit");
  const before = snapshot(root, git);
  const out = status("adopt", "main");
  assert.equal(out.code, 1, out.out);
  assert.match(out.out, /undo that merge \(git reset --merge HEAD\^\), merge again/u);
  assert.doesNotMatch(out.out, /git show .*> FORK_CHANGES\.md/u); // that would drop the pull request's row
  assert.deepEqual(snapshot(root, git), before);
  git("reset", "-q", "--merge", "HEAD^");
  assert.deepEqual(merge("main"), [CHANGES]);
  assert.equal(status("adopt", "main").code, 0);
  assert.deepEqual(why(), adopted);
});

test("during a merge whose base has no table, the advice is to replay, not a base argument adopt ignores", (t) => {
  const { root, git, write, commit, merge } = tempRepo(t, "fork-changes-ancient-");
  write("README.md", "readme\n");
  commit("before the list");
  git("tag", "pre");
  const basePage = oldPage([[APP, [ENTRY(10, "Ten."), ENTRY(11, "Eleven.")]]]);
  write(CHANGES, basePage);
  commit("old table");
  apply(root, adopt("", basePage, []));
  write(CHANGES, NEW_PAGE);
  commit("one file per change");
  git("checkout", "-qb", "ancient", "pre");
  write(CHANGES, oldPage([[APP, [ENTRY(10, "Ten."), ENTRY(11, "Eleven, old words.")]]]));
  commit("a branch from before the list's history");
  assert.deepEqual(merge("main"), [CHANGES]);
  const before = snapshot(root, git);
  const script = path.join(__dirname, "fork-changes-list.cjs");
  let out = "";
  assert.throws(
    () => execFileSync("node", [script, "adopt", "main"], { cwd: root, stdio: "pipe" }),
    (error) => {
      out = `${error.stdout}${error.stderr}`;
      return error.status === 1;
    },
  );
  assert.match(out, /abort this merge and replay the pull request's commits/u);
  assert.doesNotMatch(out, /adopt <base branch>/u);
  assert.deepEqual(snapshot(root, git), before);
});
