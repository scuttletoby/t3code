const assert = require("node:assert/strict");
const { execFileSync, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  CONFIG,
  compileForkPattern,
  findAnnouncementWarnings,
  findExpiredAnnouncements,
  findHiddenUpstreamFiles,
  findSeamBreaches,
  findListedGone,
  findLostLines,
  findStaleSeamLines,
  findUnlistedChanges,
  forkFileTest,
  lostLines,
  ownListProblem,
  parseConfig,
  parseRemoved,
  sameLines,
} = require("./fork-seam-check.cjs");

const SCRIPT = path.join(__dirname, "fork-seam-check.cjs");
const REPO_ROOT = path.join(__dirname, "..", "..");
const SETTINGS = "packages/contracts/src/settings.ts";

const judge = (files, base, head, upstream = true) =>
  findSeamBreaches(files, {
    atBase: (file) => base[file],
    atHead: (file) => head[file],
    upstream,
  });
const SEAM = { "a.ts": { why: "test", added: ["import { fork } from './fork';", "...fork,"] } };
const BASE = { "a.ts": "import { x } from './x';\nconst s = {\n  a: 1,\n};\n" };

test("the seam lines pass, wherever they sit and however they are indented", () => {
  const head =
    "import { x } from './x';\n\nimport { fork } from './fork';\nconst s = {\n    ...fork,\n  a: 1,\n};\n";
  assert.deepEqual(judge(SEAM, BASE, { "a.ts": head }), []);
  assert.deepEqual(judge(SEAM, BASE, BASE), []);
});

test("a line beyond the seam is refused, and each listed line allows one copy", () => {
  const head = BASE["a.ts"].replace("  a: 1,", "  ...fork,\n  ...fork,\n  b: 2,\n  a: 1,");
  assert.deepEqual(judge(SEAM, BASE, { "a.ts": head }), [
    "a.ts: adds `...fork,`",
    "a.ts: adds `b: 2,`",
  ]);
  // A copy of a line upstream already has is still an added line.
  const twice = BASE["a.ts"].replace("  a: 1,", "  a: 1,\n  a: 1,");
  assert.deepEqual(judge(SEAM, BASE, { "a.ts": twice }), ["a.ts: adds `a: 1,`"]);
});

test("a listed file the base lacks, or the head deletes, fails closed", () => {
  assert.match(judge(SEAM, {}, BASE)[0], /^a\.ts: listed in .*, but the base does not have it/);
  assert.deepEqual(judge(SEAM, BASE, {}), ["a.ts: this change deletes or renames it."]);
});

// A pull request's base is the fork's main, which already carries its seam lines.
test("from main, a seam line main already has cannot be added a second time", () => {
  const main = { "a.ts": BASE["a.ts"].replace("  a: 1,", "  ...fork,\n  a: 1,") };
  const twice = { "a.ts": main["a.ts"].replace("  a: 1,", "  ...fork,\n  a: 1,") };
  assert.deepEqual(judge(SEAM, main, twice, false), ["a.ts: adds `...fork,`"]);
  assert.deepEqual(judge(SEAM, main, twice, true), []);
  // A seam line main does not have yet is still allowed once.
  assert.deepEqual(judge(SEAM, BASE, main, false), []);
});

// An upstream file with no entry used to pass with any number of fork lines.
const UP = "export const x = 1;\n";
const unlisted = (
  config,
  base,
  head,
  upstream,
  isForkFile = forkFileTest(config.forkFiles),
  removed = new Set(),
) =>
  findUnlistedChanges(
    { files: {}, pending: {}, forkFiles: [], ...config },
    {
      changed: [...new Set([...Object.keys(base), ...Object.keys(head)])],
      atBase: (file) => base[file],
      atHead: (file) => head[file],
      upstream,
      isForkFile,
      removed,
    },
  );

test("an upstream file with no entry refuses every fork line, from upstream and from main", () => {
  const head = { "u.ts": `${UP}export const forkLine = 2;\n` };
  for (const upstream of [true, false]) {
    const { reasons } = unlisted({}, { "u.ts": UP }, head, upstream);
    assert.equal(reasons.length, 1);
    assert.match(reasons[0], /^u\.ts: upstream file with 1 fork line but no entry/);
  }
  // Unchanged, blank lines, trailing blanks or CRLF line ends: no change to judge.
  for (const after of [UP, `${UP}\n\n`, UP.replace("\n", "  \r\n")]) {
    assert.deepEqual(unlisted({}, { "u.ts": UP }, { "u.ts": after }, false).reasons, []);
  }
});

// A guard line deleted, or moved below what it guards, adds no line.
test("an upstream file with no entry refuses lines it loses or moves, from upstream and from main", () => {
  const guarded = "if (!authorized) return;\ndoSensitive();\n";
  for (const after of ["doSensitive();\n", "doSensitive();\nif (!authorized) return;\n", ""]) {
    for (const upstream of [true, false]) {
      const { reasons } = unlisted({}, { "u.ts": guarded }, { "u.ts": after }, upstream);
      assert.equal(reasons.length, 1, JSON.stringify(after));
      assert.match(
        reasons[0],
        /^u\.ts: upstream file whose lines this change deletes, moves or re-indents/,
      );
    }
  }
  assert.equal(sameLines("a b\n\nc\n", "a b  \r\n\t\nc"), true);
  assert.equal(sameLines("a\nb\n", "b\na\n"), false);
});

// A rename shows as a deleted upstream file plus a new file, so a
// `git mv` (or a delete plus a rewrite under a fork name) hid any upstream file from the check.
test("an upstream file the change deletes is refused, unless it is a listed removed workflow", () => {
  const workflow = ".github/workflows/up.yml";
  const base = { "u.ts": UP, [workflow]: "on: push\n" };
  for (const upstream of [true, false]) {
    const { reasons } = unlisted({}, base, { "fork/u.ts": UP }, upstream);
    assert.deepEqual(
      reasons.map((reason) => reason.split(":")[0]),
      ["u.ts", workflow],
    );
    assert.match(reasons[0], /upstream file this change deletes \(or renames\)/);
    assert.deepEqual(
      unlisted({}, base, { "u.ts": UP }, upstream, undefined, new Set([workflow])).reasons,
      [],
    );
  }
  // Upstream's fork-*.yml, which fork-follow makes the fork delete and the list cannot name.
  const sneaky = ".github/workflows/fork-sneaky.yml";
  assert.deepEqual(unlisted({}, { [sneaky]: "on: push\n" }, {}, true).reasons, []);
  // A pull request may still delete the fork's own files.
  assert.deepEqual(unlisted({}, { "fork/x.ts": UP }, {}, false).reasons, []);
  // Only workflow paths count in the list of removed files.
  assert.deepEqual(
    [...parseRemoved(`# comment\n${workflow}\nsrc/auth.ts\n .github/workflows/b.yaml \n`)],
    [workflow, ".github/workflows/b.yaml"],
  );
});

test("fork files pass: new in the change, named as the fork's, or in forkFiles", () => {
  const grown = (text) => `${text}more();\n`;
  const files = [
    "apps/server/src/fork/thing.ts",
    "packages/contracts/src/forkSettings.ts",
    ".github/fork-seams.json",
    "fork-kit/pc/setup.sh",
    "FORK_CHANGES.md",
    "apps/server/src/waits/WaitService.ts",
  ];
  const base = Object.fromEntries(files.map((file) => [file, UP]));
  const head = Object.fromEntries(files.map((file) => [file, grown(UP)]));
  head["brand/new.ts"] = UP;
  const config = { forkFiles: ["apps/server/src/waits/"] };
  assert.deepEqual(unlisted(config, base, head, false).reasons, []);
  // From upstream, a file the base has is upstream's whatever its name: a name hides nothing there.
  assert.equal(unlisted(config, base, head, true).reasons.length, files.length);
  // Not fork names: a name that only starts like one.
  for (const file of ["docs/forking.md", "src/forklift.ts", "src/Fork.ts"]) {
    assert.equal(forkFileTest([])(file), false, file);
  }
});

test("forkFiles patterns: dir/, ** folders, * within a name, ! never", () => {
  const cases = [
    ["a/b/", "a/b/c/d.ts", true],
    ["a/b/", "a/bc.ts", false],
    ["a/x*", "a/xy.ts", true],
    ["a/x*", "a/x/y.ts", false],
    ["**/z.ts", "z.ts", true],
    ["**/z.ts", "a/b/z.ts", true],
    ["**/z.ts", "a/bz.ts", false],
    ["a.b", "aXb", false],
  ];
  for (const [pattern, file, want] of cases) {
    assert.equal(compileForkPattern(pattern)(file), want, `${pattern} ${file}`);
  }
  assert.equal(forkFileTest(["a/", "!a/up.ts"])("a/up.ts"), false);
  assert.equal(forkFileTest(["!fork/up.ts"])("fork/up.ts"), false);
});

test("from upstream, a fork pattern that matches an upstream file fails until excluded", () => {
  const upstreamFiles = ["src/forkJoin.ts", "src/usage/Up.ts", "src/x.ts"];
  const kept = () => UP;
  assert.deepEqual(
    findHiddenUpstreamFiles(upstreamFiles, forkFileTest(["src/usage/"]), kept).map(
      (reason) => reason.split(":")[0],
    ),
    ["src/forkJoin.ts", "src/usage/Up.ts"],
  );
  const fixed = forkFileTest(["src/usage/", "!src/usage/Up.ts", "!src/forkJoin.ts"]);
  assert.deepEqual(findHiddenUpstreamFiles(upstreamFiles, fixed, kept), []);
  // One the fork deletes leaves nothing to hide.
  assert.deepEqual(
    findHiddenUpstreamFiles(upstreamFiles, forkFileTest([]), () => undefined),
    [],
  );
});

test("a pending file: held to its ceiling from upstream; on a pull request it may change but not grow", () => {
  const config = { pending: { "p.ts": 2 } };
  const main = { "p.ts": `${UP}forkA();\nforkB();\n` };
  assert.deepEqual(unlisted(config, { "p.ts": UP }, main, true), {
    reasons: [],
    ceilings: [],
    rises: [],
    notes: [],
  });
  // From upstream, a pending file with no fork lines left, or fewer than its ceiling, is refused:
  // a ceiling left too high is room to swap upstream lines for fork lines later, and a note in
  // the nightly log is never seen. They are their own kind of refusal, not matched by text.
  assert.match(
    unlisted(config, { "p.ts": UP }, { "p.ts": UP }, true).ceilings[0],
    /^p\.ts: "pending" with no fork lines left/,
  );
  assert.deepEqual(unlisted(config, { "p.ts": UP }, { "p.ts": `${UP}forkA();\n` }, true), {
    reasons: [],
    ceilings: ['p.ts: "pending" with 1 fork lines, fewer than its ceiling of 2; lower it to 1.'],
    rises: [],
    notes: [],
  });
  const swap = { "p.ts": main["p.ts"].replace("forkB();", "forkC();") };
  const less = { "p.ts": `${UP}forkA();\n` };
  for (const head of [swap, less])
    assert.deepEqual(unlisted(config, main, head, false).reasons, []);
  const grown = { "p.ts": `${main["p.ts"]}forkC();\nforkD();\n` };
  assert.deepEqual(unlisted(config, main, grown, false).reasons, [
    'p.ts: "pending" (fork lines to move out), and this change adds 2 more lines than it removes.',
  ]);
  // A pending file the base lacks guards nothing: fail closed, like a seam file.
  assert.match(
    unlisted(config, {}, main, false).reasons[0],
    /^p\.ts: "pending" in .*, but the base does not have it/,
  );
  // Deleting it hides it, like any upstream file.
  for (const upstream of [true, false]) {
    assert.match(
      unlisted(config, main, {}, upstream).reasons[0],
      /^p\.ts: upstream file this change deletes/,
    );
  }
});

// A pull request is only measured against main, so swapping N
// upstream lines for N fork lines passed it, and from upstream pending files were never measured.
test("from upstream, a pending file that swapped upstream lines for fork lines is over its ceiling", () => {
  const upstreamText = `${UP}// one\n// two\nguard();\n`;
  const main = `${upstreamText}forkA();\n`;
  const swapped = `${UP}forkA();\nforkB();\nforkC();\nforkD();\n`;
  const config = { pending: { "p.ts": 1 } };
  // On the pull request: net zero against main, so it passes there ...
  assert.deepEqual(unlisted(config, { "p.ts": main }, { "p.ts": swapped }, false).reasons, []);
  // ... and the nightly run from upstream refuses it.
  assert.deepEqual(unlisted(config, { "p.ts": upstreamText }, { "p.ts": swapped }, true).reasons, [
    'p.ts: "pending" with 4 fork lines, more than its ceiling of 1 (fork lines to move out, not add).',
  ]);
});

// PR 1 moves a fork line out and the ceiling stays; PR 2 swaps an upstream guard for a fork line
// (net zero on the PR). From upstream that must be refused.
test("from upstream, a ceiling left too high after lines moved out is refused, so a later swap cannot use it", () => {
  const upstreamText = `${UP}if (!ok) return;\n`;
  const config = { pending: { "p.ts": 2 } };
  const afterMoveOut = `${upstreamText}forkA();\n`;
  assert.deepEqual(
    unlisted(config, { "p.ts": upstreamText }, { "p.ts": afterMoveOut }, true).ceilings,
    ['p.ts: "pending" with 1 fork lines, fewer than its ceiling of 2; lower it to 1.'],
  );
  // (So the nightly stops right after PR 1, before a PR 2 could use the spare line.)
  // Exactly at the ceiling passes; one over is refused.
  const one = { pending: { "p.ts": 1 } };
  assert.deepEqual(unlisted(one, { "p.ts": upstreamText }, { "p.ts": afterMoveOut }, true), {
    reasons: [],
    ceilings: [],
    rises: [],
    notes: [],
  });
  const zero = { "p.ts": upstreamText };
  assert.deepEqual(unlisted(one, zero, { "p.ts": `${afterMoveOut}forkB();\n` }, true).reasons, [
    'p.ts: "pending" with 2 fork lines, more than its ceiling of 1 (fork lines to move out, not add).',
  ]);
});

// Upstream adopts a pending fork line word for word, so the rebase drops it and the series falls
// below its ceiling. Lowering the ceiling first fails the check on
// the old base, so right after a rebase it is a note; the next run (from the new base) refuses it.
test("after a rebase, a pending file below its ceiling is a note; over it is still refused", () => {
  const config = { files: {}, pending: { "p.ts": 2 }, forkFiles: [] };
  const judge = (head, afterRebase) =>
    findUnlistedChanges(config, {
      changed: ["p.ts"],
      atBase: () => UP,
      atHead: () => head,
      upstream: true,
      isForkFile: () => false,
      afterRebase,
    });
  assert.deepEqual(judge(`${UP}forkA();\n`, true), {
    reasons: [],
    ceilings: [],
    rises: [],
    notes: ['p.ts: "pending" with 1 fork lines, fewer than its ceiling of 2; lower it to 1.'],
  });
  assert.deepEqual(judge(UP, true), {
    reasons: [],
    ceilings: [],
    rises: [],
    notes: ['p.ts: "pending" with no fork lines left; remove it from the list.'],
  });
  const [over, ...more] = judge(`${UP}forkA();\nforkB();\nforkC();\n`, true).rises;
  assert.deepEqual(more, []);
  assert.match(
    over,
    /^p\.ts: "pending" with 3 fork lines, more than its ceiling of 2 after the rebase/,
  );
  assert.equal(judge(UP, false).ceilings.length, 1);
});

const NOT_PATHS = [
  "",
  " ",
  "/abs",
  "../up.ts",
  "a/../b.ts",
  "./a.ts",
  "a//b.ts",
  "a/",
  ":(glob)a.ts",
  ":!a.ts",
  ":/a.ts",
  "a\\b.ts",
];

test("a malformed config throws instead of guarding nothing", () => {
  for (const bad of [
    {},
    { files: [] },
    { files: { "a.ts": { added: [] } } },
    { files: { "a.ts": { why: "x", added: "y" } } },
    { files: {}, pending: "a.ts" },
    { files: {}, pending: ["a.ts"] },
    { files: {}, pending: { "": 1 } },
    { files: {}, pending: { "a.ts": 0 } },
    { files: {}, pending: { "a.ts": 1.5 } },
    { files: {}, pending: { "a.ts": "3" } },
    { files: {}, forkFiles: [1] },
    { files: SEAM, pending: { "a.ts": 1 } },
    // "lost": a count of 1 or more for a listed seam or pending file.
    { files: SEAM, lost: [] },
    { files: SEAM, lost: "a.ts" },
    { files: SEAM, lost: { "a.ts": 2 } },
    { files: SEAM, lost: { "a.ts": null } },
    { files: SEAM, lost: { "a.ts": [2, 1] } },
    { files: SEAM, lost: { "a.ts": { lines: 0, gone: 0 } } },
    { files: SEAM, lost: { "a.ts": { lines: 1.5, gone: 0 } } },
    { files: SEAM, lost: { "a.ts": { lines: "2", gone: 0 } } },
    { files: SEAM, lost: { "a.ts": { lines: 2 } } },
    { files: SEAM, lost: { "a.ts": { lines: 2, gone: -1 } } },
    { files: SEAM, lost: { "a.ts": { lines: 2, gone: 0.5 } } },
    { files: SEAM, lost: { "a.ts": { lines: 2, gone: 3 } } },
    { files: SEAM, lost: { "a.ts": { lines: 2, gone: 1, why: "x" } } },
    { files: SEAM, lost: { "b.ts": { lines: 1, gone: 0 } } },
    // Unknown kinds: a misspelt block or seam key would silently guard nothing.
    { files: SEAM, lsot: {} },
    { files: SEAM, Pending: { "p.ts": 1 } },
    { files: { "a.ts": { ...SEAM["a.ts"], add: ["x"] } } },
    ...["/abs", "../up", "**", "*", "!", "a/../b"].map((pattern) => ({
      files: {},
      forkFiles: [pattern],
    })),
    // "files" and "pending" keys are plain repository paths: anything else made every later run
    // crash in `git ls-tree` once merged.
    ...NOT_PATHS.flatMap((file) => [
      { files: { [file]: SEAM["a.ts"] } },
      { files: {}, pending: { [file]: 1 } },
    ]),
    // "announced": since a real date, and at least one line.
    ...[
      true,
      "2026-11-18",
      { added: ["x"] },
      { since: "2026-11-18" },
      { since: "2026-11-18", added: [] },
      { since: "2026-11-18", added: [""] },
      { since: "2026-11-18", added: [1] },
      { since: "2026-11-18", added: "x" },
      { since: "2026-13-01", added: ["x"] },
      { since: "2026-02-30", added: ["x"] },
      { since: "18.11.2026", added: ["x"] },
      { since: "2026-11-18T00:00:00Z", added: ["x"] },
      { since: "2026-11-18", added: ["x"], why: "y" },
    ].map((announced) => ({ files: { "a.ts": { ...SEAM["a.ts"], announced } } })),
  ]) {
    assert.throws(() => parseConfig(JSON.stringify(bad)), JSON.stringify(bad));
  }
  // Keys that start with "//" are comments, at the top and in an entry.
  assert.doesNotThrow(() =>
    parseConfig(
      JSON.stringify({
        "//": "c",
        "//pending": "c",
        files: { "a.ts": { ...SEAM["a.ts"], "//": "c" } },
      }),
    ),
  );
  // A seam entry may announce lines; "added" may then be empty.
  const announced = {
    "a.ts": { ...SEAM["a.ts"], announced: { since: "2026-11-18", added: ["soon();"] } },
    "n.ts": { why: "new", added: [], announced: { since: "2024-02-29", added: ["x", "x"] } },
  };
  assert.deepEqual(parseConfig(JSON.stringify({ files: announced })).files, announced);
  // Plain paths with dots, dashes and nested folders are fine.
  assert.doesNotThrow(() =>
    parseConfig(JSON.stringify({ files: {}, pending: { "a/.b/c-d.e2e.ts": 1, "x..y.ts": 2 } })),
  );
  assert.deepEqual(parseConfig(JSON.stringify({ files: SEAM })), {
    files: SEAM,
    pending: {},
    forkFiles: [],
    lost: {},
  });
  const lost = { "a.ts": { lines: 2, gone: 0 }, "p.ts": { lines: 1, gone: 1 } };
  assert.deepEqual(
    parseConfig(JSON.stringify({ files: SEAM, pending: { "p.ts": 1 }, lost })).lost,
    lost,
  );
});

// The real files: read from the working tree, or from HEAD in a sparse checkout that leaves them out.
const readReal = (file) =>
  fs.existsSync(path.join(REPO_ROOT, file))
    ? fs.readFileSync(path.join(REPO_ROOT, file), "utf8")
    : execFileSync("git", ["-C", REPO_ROOT, "show", `HEAD:${file}`], { encoding: "utf8" });
// Read in each test, not at load: a parse error then fails the tests that pin it, not the file.
const realConfig = () => parseConfig(readReal(CONFIG));

test("every listed seam line is in its file and no announced one is yet, so the list is not stale", () => {
  const read = (file) => {
    try {
      return readReal(file);
    } catch {
      return undefined;
    }
  };
  assert.deepEqual(findStaleSeamLines(realConfig().files, read), []);
});

test("the stale-list check: a listed line must be in its file, an announced one not yet", () => {
  const files = {
    "a.ts": { why: "t", added: ["kept();", "gone();"] },
    "b.ts": { why: "t", added: [], announced: { since: "2026-11-18", added: ["soon();"] } },
    "c.ts": {
      why: "t",
      added: ["listed();"],
      announced: { since: "2026-11-18", added: ["later();", "}"] },
    },
  };
  const text = {
    "a.ts": "kept();\n",
    "b.ts": "upstream();\n",
    "c.ts": "  listed();\n}\n",
  };
  assert.deepEqual(
    findStaleSeamLines(files, (file) => text[file]),
    [
      "a.ts: the listed seam line `gone();` is not in the file; remove it from the list.",
      'c.ts: the announced line `}` is already in the file (1× there): move it to "added" in the change that adds it, or drop it (a line upstream has cannot be a seam line a pull request adds).',
    ],
  );
  // Step 2 adds the lines and keeps the announcement: refused, so it drops the flag.
  text["b.ts"] = "upstream();\nsoon();\n";
  assert.deepEqual(
    findStaleSeamLines({ "b.ts": files["b.ts"] }, (file) => text[file]),
    [
      'b.ts: the announced line `soon();` is already in the file (1× there): move it to "added" in the change that adds it, or drop it (a line upstream has cannot be a seam line a pull request adds).',
    ],
  );
  // A listed file the tree does not have: every line of it is stale.
  assert.equal(findStaleSeamLines({ "a.ts": files["a.ts"] }, () => undefined).length, 1);
});

// A step-2 change that copied a line into "added" instead of moving it passed every test, and the
// list then allowed two copies. A line is in one of them, never both.
test('a line in both "added" and "announced" is refused: step 2 moves it, never copies it', () => {
  const both = {
    files: {
      "a.ts": {
        why: "t",
        added: ["  twice();"],
        announced: { since: "2026-11-18", added: ["other();", "twice();  "] },
      },
    },
  };
  assert.throws(
    () => parseConfig(JSON.stringify(both)),
    /a\.ts: "twice\(\);" is in "added" and "announced": step 2 moves a line/,
  );
  assert.match(ownListProblem(JSON.stringify(both)), /is in "added" and "announced"/);
  // Moved, it is fine.
  both.files["a.ts"].announced.added = ["other();"];
  assert.equal(ownListProblem(JSON.stringify(both)), undefined);
});

// From the design review of the fork defaults table: a fork setting written straight into
// upstream's settings.ts, without the marker comment the old check looked for, went unseen.
const NEW_KEY =
  "  forkNewSwitch: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(true))),";
const STRUCT = "export const ServerSettings = Schema.Struct({";
for (const [how, added] of [
  ["no comment at all", NEW_KEY],
  ["a same-line comment", `${NEW_KEY} // Fork: on`],
  ["a lower-case marker", `  /** fork: new switch */\n${NEW_KEY}`],
  ["a `Fork:` comment above it", `  /** Fork: new switch */\n${NEW_KEY}`],
]) {
  test(`a fork setting added to settings.ts with ${how} is refused`, () => {
    const source = readReal(SETTINGS);
    assert.ok(source.includes(STRUCT));
    const head = source.replace(STRUCT, `${STRUCT}\n${added}`);
    const reasons = judge(realConfig().files, { [SETTINGS]: source }, { [SETTINGS]: head });
    assert.ok(
      reasons.some((reason) => reason.includes("forkNewSwitch")),
      reasons.join("\n"),
    );
  });
}

// A throwaway repository: commit({ path: text | null }) on the current branch, run(...args).
function withRepo(body) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-seams-git-"));
  const git = (...args) =>
    execFileSync("git", ["-C", dir, "-c", "user.name=t", "-c", "user.email=t@t", ...args], {
      encoding: "utf8",
    });
  // `date` (ISO) sets the commit's author and committer date; the check reads the committer's.
  const commit = (files, message, date) => {
    for (const [file, text] of Object.entries(files)) {
      const at = path.join(dir, file);
      if (text === null) fs.rmSync(at);
      else {
        fs.mkdirSync(path.dirname(at), { recursive: true });
        fs.writeFileSync(at, text);
      }
    }
    git("add", "-A");
    execFileSync(
      "git",
      [
        "-C",
        dir,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        message,
      ],
      {
        env: date
          ? { ...process.env, GIT_AUTHOR_DATE: date, GIT_COMMITTER_DATE: date }
          : process.env,
      },
    );
    return git("rev-parse", "HEAD").trim();
  };
  const run = (...args) => spawnSync("node", [SCRIPT, ...args], { cwd: dir, encoding: "utf8" });
  // As fork-follow runs it: from an upstream commit, the list read from the head.
  const fromUpstream = (...args) => run("--from-upstream", ...args);
  try {
    git("init", "-q");
    body({ dir, git, commit, run, fromUpstream });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}
const seams = (config) => ({ [CONFIG]: JSON.stringify(config) });

test("the command line judges the lines a branch adds since the merge-base", () => {
  withRepo(({ git, commit, run }) => {
    const base = commit({ ...seams({ files: SEAM }), ...BASE }, "upstream");
    const ok = commit({ "a.ts": BASE["a.ts"].replace("  a: 1,", "  ...fork,\n  a: 1,") }, "seam");
    const bad = commit(
      { "a.ts": BASE["a.ts"].replace("  a: 1,", "  ...fork,\n  inline: 1,\n  a: 1,") },
      "inline",
    );
    assert.equal(run(base, ok).status, 0);
    const refused = run(base, bad);
    assert.equal(refused.status, 1);
    assert.match(refused.stderr, /a\.ts: adds `inline: 1,`/);
    // From the seam commit, the branch adds only the inline line; upstream's own lines are never its.
    assert.equal(run(ok, bad).status, 1);
    git("checkout", "-q", "-b", "upstream-later", base);
    const later = commit({ "a.ts": `${BASE["a.ts"]}const upstreamLater = 1;\n` }, "upstream later");
    assert.equal(run(later, ok).status, 0);
    assert.equal(run("missing", ok).status, 3);
    for (const args of [[ok], [base, ok, ok], ["--config-ref", base, ok], ["--x", base, ok]]) {
      assert.equal(run(...args).status, 2, args.join(" "));
    }
  });
});

// The list that judges a pull request is its base's (or --config-ref's), never its own.
test("a pull request that widens or drops its own list is judged by main's list", () => {
  withRepo(({ dir, git, commit, run }) => {
    const main = commit({ ...seams({ files: SEAM }), ...BASE }, "main");
    const inline = { "a.ts": BASE["a.ts"].replace("  a: 1,", "  inline: 1,\n  a: 1,") };
    const widen = {
      files: { "a.ts": { why: "mine", added: [...SEAM["a.ts"].added, "inline: 1,"] } },
    };
    const widened = commit({ ...inline, ...seams(widen) }, "widen the list");
    git("checkout", "-q", "-b", "dropped", main);
    const dropped = commit({ ...inline, ...seams({ files: {} }) }, "drop the file");
    git("checkout", "-q", "-b", "deleted", main);
    const deleted = commit({ ...inline, [CONFIG]: null }, "delete the list");
    for (const head of [widened, dropped, deleted]) {
      // As in a pull request's run: the change itself is checked out.
      git("checkout", "-q", "-f", head);
      const result = run(main, head);
      assert.equal(result.status, 1, result.stdout + result.stderr);
      assert.match(result.stderr, /list from the base/);
      assert.match(result.stdout, /this change edits \.github\/fork-seams\.json/);
    }
    // The working tree is never read: a widened list on disk changes nothing.
    fs.mkdirSync(path.join(dir, ".github"), { recursive: true });
    fs.writeFileSync(path.join(dir, CONFIG), JSON.stringify(widen));
    assert.equal(run(main, deleted).status, 1);
    // A stacked pull request's base may carry the widened list; --config-ref names main's.
    git("checkout", "-q", "-f", "-b", "stacked", widened);
    const onTop = commit({ "b.txt": "fork file\n" }, "stacked");
    assert.equal(run(widened, onTop).status, 0);
    assert.equal(run("--config-ref", main, main, widened).status, 1);
    // An owner-merged list is main's list: the same change then passes.
    assert.equal(run(widened, widened).status, 0);
    // A --config-ref without the list is a crash, never an empty list.
    git("checkout", "-q", "--orphan", "bare");
    git("rm", "-rfq", ".");
    const bare = commit({ "x.txt": "x\n" }, "no list");
    assert.equal(run("--config-ref", bare, main, widened).status, 3);
  });
});

// A pull request is judged by main's list, so its own list was never parsed before merge: a
// malformed one (bad JSON, the old plain-number "lost", an unknown kind, or none) then made every
// later pull request and fork-follow run crash (exit 3), the pull request that fixes it included.
test("a pull request's own list is parsed for its format (never its rules) and refused when malformed", () => {
  withRepo(({ git, commit, run, fromUpstream }) => {
    const main = commit({ ...seams({ files: SEAM }), ...BASE }, "main");
    const heads = {
      'old plain-number "lost"': [
        seams({ files: SEAM, lost: { "a.ts": 3 } }),
        /"lost": a\.ts needs/,
      ],
      "bad JSON": [{ [CONFIG]: '{"files": {' }, /JSON/],
      "an unknown kind": [seams({ files: SEAM, lsot: {} }), /unknown key "lsot"/],
      "no list": [{ [CONFIG]: null }, /this change deletes it/],
    };
    for (const [what, [files, message]] of Object.entries(heads)) {
      git("checkout", "-q", "-f", "-B", "pr", main);
      const pr = commit(files, what);
      for (const args of [
        [main, pr],
        ["--config-ref", main, main, pr],
      ]) {
        const result = run(...args);
        assert.equal(result.status, 1, `${what}: ${result.stdout}${result.stderr}`);
        assert.match(result.stderr, /this change's own \.github\/fork-seams\.json/, what);
        assert.match(result.stderr, message, what);
        assert.match(result.stderr, /Fix the list in this change/, what);
        assert.doesNotMatch(result.stderr, /a file of the fork's own/, what);
      }
    }
    // A well-formed list passes, whatever its rules (main's list judges the change).
    git("checkout", "-q", "-f", "-B", "pr", main);
    const wide = {
      files: { "a.ts": { why: "mine", added: ["anything: 1,"] } },
      pending: { "z.ts": 9 },
    };
    const fine = commit(seams(wide), "a wider list");
    assert.equal(run(main, fine).status, 0);
    // From upstream the head's list is the one in force: malformed, it is still a crash.
    const broken = commit(seams({ files: SEAM, lost: { "a.ts": 3 } }), "broken");
    assert.equal(fromUpstream(main, broken).status, 3);
  });
});

// Every upstream file on the command line, from upstream (no list there: the series' own list judges it) and
// from main with a list that predates "pending" and "forkFiles" (the first run after this change).
test("every upstream file counts on the command line, from upstream and from main", () => {
  withRepo(({ git, commit, run, fromUpstream }) => {
    const upstream = commit({ "u.ts": UP, "p.ts": UP, ...BASE }, "upstream");
    const series = commit(
      {
        ...seams({ files: SEAM, pending: { "p.ts": 1 } }),
        "p.ts": `${UP}forkOld();\n`,
        "fork/own.ts": UP,
      },
      "fork series",
    );
    const unlistedLine = commit({ "u.ts": `${UP}forkNew();\n` }, "fork line in u.ts");
    const refused = fromUpstream(upstream, unlistedLine);
    assert.equal(refused.status, 1, refused.stdout);
    assert.match(refused.stderr, /- u\.ts: upstream file with 1 fork line but no entry/);
    const first = fromUpstream(upstream, series);
    assert.equal(first.status, 0, first.stderr);
    assert.match(first.stdout, /every upstream file; list from the head/);
    // Runs again: the same answer.
    const again = fromUpstream(upstream, series);
    assert.deepEqual([again.status, again.stdout, again.stderr], [0, first.stdout, first.stderr]);
    // From main whose list has no "pending" or "forkFiles" (older list): fork-named files pass,
    // the unlisted upstream file is refused.
    git("checkout", "-q", "-b", "pr", series);
    const forkOnly = commit({ "fork/own.ts": `${UP}more();\n`, "new.ts": UP }, "fork files");
    assert.equal(run(series, forkOnly).status, 0);
    assert.equal(run(series, unlistedLine).status, 1);
    git("checkout", "-q", "-b", "pr-old-list", upstream);
    const oldMain = commit(seams({ files: SEAM }), "main with an older list");
    const touch = commit({ "u.ts": `${UP}forkNew();\n`, "fork/x.ts": UP }, "pr");
    assert.match(run(oldMain, touch).stderr, /- u\.ts: upstream file with 1 fork line/);
  });
});

// Pins the hidden-file call in main(); turning it off used to pass every test.
test("from upstream, an upstream file named like a fork file fails on the command line until excluded", () => {
  withRepo(({ commit, fromUpstream }) => {
    const upstream = commit({ "src/forkJoin.ts": UP, ...BASE }, "upstream");
    const series = commit(seams({ files: SEAM }), "fork series");
    const refused = fromUpstream(upstream, series);
    assert.equal(refused.status, 1, refused.stdout);
    assert.match(
      refused.stderr,
      /- src\/forkJoin\.ts: upstream has it, but .* "!src\/forkJoin\.ts"/,
    );
    const fixed = commit(seams({ files: SEAM, forkFiles: ["!src/forkJoin.ts"] }), "exclude it");
    assert.equal(fromUpstream(upstream, fixed).status, 0);
  });
});

// Both cases passed in both modes before.
test("a rename or delete cannot hide an upstream file, from upstream or on a pull request", () => {
  withRepo(({ dir, git, commit, run, fromUpstream }) => {
    const workflow = ".github/workflows/up.yml";
    const upstream = commit(
      {
        "src/auth.ts": "export const auth = (user) => user.admin;\n",
        "src/unlisted.ts": UP,
        "src/uses.ts": 'import { auth } from "./auth";\n',
        [workflow]: "on: push\n",
        ...BASE,
      },
      "upstream",
    );
    const main = commit(
      { ...seams({ files: SEAM, forkFiles: ["src/forkstuff/"] }), "fork/own.ts": UP },
      "fork main",
    );
    git("checkout", "-q", "-b", "rewrite", main);
    git("rm", "-q", "src/auth.ts");
    const rewritten = commit(
      {
        "src/fork-auth.ts": "export const auth = () => true;\n",
        "fork/uses.ts": 'import { auth } from "../src/fork-auth";\n',
      },
      "delete and rewrite under a fork name",
    );
    git("checkout", "-q", "-b", "moved", main);
    fs.mkdirSync(path.join(dir, "src/forkstuff"));
    git("mv", "src/unlisted.ts", "src/forkstuff/unlisted.ts");
    const moved = commit(
      { "src/forkstuff/unlisted.ts": `${UP}evil();\n` },
      "git mv into forkFiles",
    );
    for (const [head, file] of [
      [rewritten, "src/auth.ts"],
      [moved, "src/unlisted.ts"],
    ]) {
      for (const base of [main, upstream]) {
        const result = base === main ? run(base, head) : fromUpstream(base, head);
        assert.equal(result.status, 1, `${file} from ${base === main ? "main" : "upstream"}`);
        assert.match(result.stderr, new RegExp(`- ${file}: upstream file this change deletes`));
      }
    }
    // A workflow the fork lists as removed may go.
    git("checkout", "-q", "-b", "drop-workflow", main);
    const dropped = commit(
      { [workflow]: null, ".github/fork-removed-workflows.txt": `${workflow}\n` },
      "delete a listed upstream workflow",
    );
    assert.equal(fromUpstream(upstream, dropped).status, 0);
  });
});

// A submodule pointer read as no file, so moving it passed.
test("a moved submodule pointer in an upstream path is a changed line", () => {
  withRepo(({ dir, git, commit, run, fromUpstream }) => {
    // Staged by hand: `git add -A` in commit() would drop a pointer with no folder on disk.
    const pointer = (sha, files = {}) => {
      for (const [file, text] of Object.entries(files)) {
        fs.mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
        fs.writeFileSync(path.join(dir, file), text);
        git("add", file);
      }
      git("update-index", "--add", "--cacheinfo", `160000,${sha},.repos/lib`);
      git("commit", "-q", "-m", `pointer ${sha.slice(0, 7)}`);
      return git("rev-parse", "HEAD").trim();
    };
    commit(BASE, "start");
    const upstream = pointer("1".repeat(40));
    const main = pointer("1".repeat(40), seams({ files: SEAM }));
    const moved = pointer("2".repeat(40));
    for (const base of [upstream, main]) {
      const result = base === main ? run(base, moved) : fromUpstream(base, moved);
      assert.equal(result.status, 1, result.stdout);
      assert.match(result.stderr, /- \.repos\/lib: upstream file with 1 fork line/);
    }
  });
});

// After fork-follow moves main onto a newer upstream commit, an
// older branch's merge-base is upstream's, and its run counted the whole old series as its own.
test("a branch from before the base's last rebase gets one 'update the branch' error", () => {
  withRepo(({ git, commit, run }) => {
    const upstream = commit({ "p.ts": UP, ...BASE }, "upstream");
    const series = { ...seams({ files: SEAM }), "p.ts": `${UP}forkOld();\nforkOlder();\n` };
    const oldMain = commit(series, "fork series");
    const pr = commit({ "fork/new.ts": UP }, "pull request");
    git("checkout", "-q", "-b", "upstream-later", upstream);
    commit({ "later.ts": UP }, "upstream later");
    const newMain = commit(series, "fork series, rebased");
    for (const args of [
      [newMain, pr],
      ["--config-ref", newMain, newMain, pr],
    ]) {
      const result = run(...args);
      assert.equal(result.status, 1, args.join(" "));
      assert.match(
        result.stderr,
        /^This branch predates the last rebase of its base onto upstream/,
      );
      assert.equal(result.stderr.trim().split("\n").length, 1, result.stderr);
    }
    assert.equal(run(oldMain, pr).status, 0);
  });
});

// With --config-ref, a stacked pull request whose base branch
// deleted the list was judged from upstream, where pending files are not measured.
test("with --config-ref, a base without the list is refused, not taken for upstream", () => {
  withRepo(({ commit, run }) => {
    commit({ "p.ts": UP, ...BASE }, "upstream");
    const main = commit(
      { ...seams({ files: SEAM, forkFiles: ["fork-x/"] }), "p.ts": `${UP}forkA();\n` },
      "fork main",
    );
    const noList = commit({ [CONFIG]: null }, "base branch drops the list");
    const grown = commit({ "p.ts": `${UP}forkA();\nforkB();\n` }, "grow it");
    const result = run("--config-ref", main, noList, grown);
    assert.equal(result.status, 1, result.stdout);
    assert.match(result.stderr, /^The base .* has no \.github\/fork-seams\.json/);
  });
});

test("--after-rebase notes a ceiling above the fork lines; without it the refusal names the fix", () => {
  withRepo(({ commit, fromUpstream }) => {
    const upstream = commit(
      { "p.ts": `${UP}forkA();\n`, "u.ts": UP },
      "upstream adopts the fork line",
    );
    const series = commit(seams({ files: {}, pending: { "p.ts": 1 } }), "fork series");
    const after = fromUpstream("--after-rebase", upstream, series);
    assert.equal(after.status, 0, after.stderr);
    assert.match(after.stdout, /^Note: p\.ts: "pending" with no fork lines left/m);
    const next = fromUpstream(upstream, series);
    assert.equal(next.status, 1);
    assert.match(next.stderr, /- p\.ts: "pending" with no fork lines left/);
    assert.match(next.stderr, /Lower each "pending" ceiling/);
    assert.doesNotMatch(next.stderr, /a file of the fork's own/);
    // A ceiling refusal next to another refusal keeps both hints.
    const line = commit({ "u.ts": `${UP}forkNew();\n` }, "fork line in u.ts");
    const mixed = fromUpstream(upstream, line);
    assert.equal(mixed.status, 1);
    assert.match(mixed.stderr, /- u\.ts: upstream file with 1 fork line/);
    assert.match(mixed.stderr, /a file of the fork's own/);
    assert.match(mixed.stderr, /Lower each "pending" ceiling/);
    // After a rebase, the other refusal still fails it.
    assert.equal(fromUpstream("--after-rebase", upstream, line).status, 1);
    assert.equal(fromUpstream("--after-rebase", "--after-rebase", upstream, series).status, 2);
  });
});

// In YAML and Python, indentation is meaning. Moving `if: false` from a job to one of
// its steps runs the job; the old compare stripped every space, so it passed.
test("re-indenting an upstream file's lines is a change; trailing blanks and blank lines are not", () => {
  const job = "jobs:\n  build:\n    if: false\n    steps:\n      - run: make\n";
  const step = "jobs:\n  build:\n    steps:\n      - run: make\n        if: false\n";
  const nested = "def main():\n    check()\n    run()\n";
  for (const [file, before, after] of [
    [".github/workflows/up.yml", job, step],
    ["tool.py", "def main():\n    run()\n", "def main():\nrun()\n"],
    ["tool.py", nested, nested.replace("    run()", "        run()")],
  ]) {
    for (const upstream of [true, false]) {
      const { reasons } = unlisted({}, { [file]: before }, { [file]: after }, upstream);
      assert.equal(reasons.length, 1, `${file} ${after}`);
      assert.match(reasons[0], /whose lines this change deletes, moves or re-indents/);
    }
  }
  assert.equal(sameLines("  a\n", "a\n"), false);
  assert.equal(sameLines("\ta\n", "  a\n"), false);
  assert.equal(sameLines("  a\nb\n", "  a \r\n\n   \nb"), true);
});

// A seam file's lines other than its listed seam lines are upstream's. A guard deleted,
// or moved below what it guards, added no line, so it passed both runs.
const GUARD_BASE = "import { x } from './x';\nif (!authorized) return;\ndoSensitive();\n";
const GUARD_SEAM = { "a.ts": { why: "test", added: ["...fork,", "forkHook();"] } };
const lostIn = (config, base, head, upstream, afterRebase = false) =>
  findLostLines(
    { files: {}, pending: {}, lost: {}, ...config },
    {
      atBase: (file) => base[file],
      atHead: (file) => head[file],
      upstream,
      afterRebase,
    },
  );
test("a seam file keeps upstream's lines in order: deleting, moving or re-indenting one is refused", () => {
  const config = { files: GUARD_SEAM };
  const main = { "a.ts": `${GUARD_BASE}forkHook();\n` };
  for (const head of [
    "import { x } from './x';\ndoSensitive();\nforkHook();\n",
    "import { x } from './x';\ndoSensitive();\nif (!authorized) return;\nforkHook();\n",
    "import { x } from './x';\n  if (!authorized) return;\ndoSensitive();\nforkHook();\n",
  ]) {
    // On a pull request (from main) and from upstream with no "lost" count.
    for (const [base, upstream] of [
      [main, false],
      [{ "a.ts": GUARD_BASE }, true],
    ]) {
      const { reasons } = lostIn(config, base, { "a.ts": head }, upstream);
      // From upstream a deleted line also counts as one whose text is gone.
      const deleted = upstream && !head.includes("authorized");
      assert.equal(reasons.length, deleted ? 2 : 1, `${upstream} ${head}`);
      assert.match(reasons[0], /^a\.ts: 1 upstream line deleted, moved or re-indented/);
    }
  }
  // Its own seam lines may go, and lines may be added around upstream's.
  for (const head of [
    GUARD_BASE,
    `import { x } from './x';\n\nif (!authorized) return;\n...fork,\ndoSensitive();\nforkHook();\n`,
  ]) {
    assert.deepEqual(lostIn(config, main, { "a.ts": head }, false), {
      reasons: [],
      ceilings: [],
      rises: [],
      notes: [],
    });
  }
  // A moved seam line is refused on a pull request: a moved copy of a listed line looks the same
  // as upstream's line with that text (`}`). Moving one takes two pull requests (drop, then add).
  const moved = "forkHook();\nimport { x } from './x';\nif (!authorized) return;\ndoSensitive();\n";
  assert.match(
    lostIn(config, main, { "a.ts": moved }, false).reasons[0],
    /^a\.ts: 1 upstream line deleted, moved or re-indented by this change/,
  );
});

test('from upstream, a seam or pending file\'s lost upstream lines are held to its "lost" counts', () => {
  const pendingConfig = (lost) => ({ pending: { "p.ts": 1 }, lost });
  const base = { "p.ts": GUARD_BASE };
  const dropGuard = { "p.ts": "import { x } from './x';\ndoSensitive();\nforkA();\n" };
  const moveGuard = {
    "p.ts": "import { x } from './x';\ndoSensitive();\nif (!authorized) return;\nforkA();\n",
  };
  for (const [head, gone] of [
    [dropGuard, 1],
    [moveGuard, 0],
  ]) {
    assert.deepEqual(lostIn(pendingConfig({}), base, head, true).reasons, [
      "p.ts: 1 upstream line deleted, moved or re-indented, more than its \"lost\" lines count of 0 (keep upstream's lines; a fork change goes in a file of the fork's own).",
      ...(gone
        ? [
            "p.ts: 1 upstream line deleted or changed (its text is gone), more than its \"lost\" gone count of 0 (keep upstream's lines; a fork change goes in a file of the fork's own).",
          ]
        : []),
    ]);
    // Exactly the counts pass.
    assert.deepEqual(lostIn(pendingConfig({ "p.ts": { lines: 1, gone } }), base, head, true), {
      reasons: [],
      ceilings: [],
      rises: [],
      notes: [],
    });
  }
  // One over a count is refused.
  const both = { "p.ts": "doSensitive();\nforkA();\n" };
  assert.equal(
    lostIn(pendingConfig({ "p.ts": { lines: 1, gone: 1 } }), base, both, true).reasons.length,
    2,
  );
  // A count above the lines lost is a ceiling to lower (a note right after a rebase).
  const kept = { "p.ts": `${GUARD_BASE}forkA();\n` };
  assert.deepEqual(lostIn(pendingConfig({ "p.ts": { lines: 1, gone: 1 } }), base, kept, true), {
    reasons: [],
    ceilings: [
      'p.ts: no upstream lines deleted, moved or re-indented, below its "lost" entry; remove it from "lost".',
    ],
    rises: [],
    notes: [],
  });
  assert.deepEqual(
    lostIn(pendingConfig({ "p.ts": { lines: 2, gone: 1 } }), base, dropGuard, true, true),
    {
      reasons: [],
      ceilings: [],
      rises: [],
      notes: [
        'p.ts: 1 upstream line deleted, moved or re-indented, below its "lost" lines count of 2; lower it to 1.',
      ],
    },
  );
  // On a pull request a pending file's own lines cannot be told from upstream's: the PR may move a
  // fork line out, so only the run from upstream counts them.
  assert.deepEqual(lostIn(pendingConfig({}), kept, dropGuard, false).reasons, []);
  // A seam file from upstream: the same counts.
  const seamConfig = (lost) => ({ files: GUARD_SEAM, lost });
  const seamHead = { "a.ts": "import { x } from './x';\ndoSensitive();\nforkHook();\n" };
  assert.equal(lostIn(seamConfig({}), { "a.ts": GUARD_BASE }, seamHead, true).reasons.length, 2);
  const held = { "a.ts": { lines: 1, gone: 1 } };
  assert.deepEqual(lostIn(seamConfig(held), { "a.ts": GUARD_BASE }, seamHead, true).reasons, []);
  // On a pull request the count does not widen a seam file: no upstream line may go.
  assert.equal(lostIn(seamConfig(held), { "a.ts": GUARD_BASE }, seamHead, false).reasons.length, 1);
});

// Right after a rebase only a count left too high is a note; one exceeded still stops the run.
test("after a rebase, a lost count exceeded is still refused", () => {
  const config = { pending: { "p.ts": 1 }, lost: { "p.ts": { lines: 1, gone: 1 } } };
  const base = { "p.ts": GUARD_BASE };
  const head = { "p.ts": "doSensitive();\nforkA();\n" };
  const { rises, notes } = lostIn(config, base, head, true, true);
  assert.equal(rises.length, 2, rises.join("\n"));
  assert.match(rises[0], /^p\.ts: 2 upstream lines deleted, moved or re-indented, more than/);
  assert.deepEqual(notes, []);
});

// Main's list pins generic seam lines (`}`, `/**`) in serverSettings.ts. Filtering every copy of
// them out let an upstream line move past an upstream `}` unseen, in both runs.
const BRACE_UP = [
  "for (const key of keys) {",
  "  if (!Equal.equals(currentRecord[key], defaultsRecord[key])) {",
  "    next[key] = currentRecord[key];",
  "  }",
  "}",
  "",
].join("\n");
const BRACE_MOVED = BRACE_UP.replace(
  "    next[key] = currentRecord[key];\n  }\n",
  "  }\n    next[key] = currentRecord[key];\n",
);
const BRACE_SEAM = { "s.ts": { why: "test", added: ["}", "/**", "forkHook();"] } };
test("a line moved past an upstream copy of a listed seam line is refused on a pull request", () => {
  // Main: upstream's text plus the fork's listed `}` and hook.
  const main = { "s.ts": `${BRACE_UP}forkHook();\n}\n` };
  const head = { "s.ts": `${BRACE_MOVED}forkHook();\n}\n` };
  assert.match(
    lostIn({ files: BRACE_SEAM }, main, head, false).reasons[0] ?? "",
    /^s\.ts: 1 upstream line deleted, moved or re-indented by this change/,
  );
  // Dropping the fork's own `}` (a copy main has beyond upstream's) still passes ...
  assert.deepEqual(
    lostIn({ files: BRACE_SEAM }, main, { "s.ts": `${BRACE_UP}forkHook();\n` }, false).reasons,
    [],
  );
  // ... but only as many copies as the list names: dropping upstream's `}` too is refused.
  const both = { "s.ts": `${BRACE_UP.replace(/\}\n$/, "")}forkHook();\n` };
  assert.match(
    lostIn({ files: BRACE_SEAM }, main, both, false).reasons[0] ?? "",
    /^s\.ts: 1 upstream line deleted, moved or re-indented by this change/,
  );
});

test("from upstream, a line moved past a listed seam line is counted", () => {
  const up = { "s.ts": BRACE_UP };
  assert.match(
    lostIn({ files: BRACE_SEAM }, up, { "s.ts": BRACE_MOVED }, true).reasons[0] ?? "",
    /^s\.ts: 1 upstream line deleted, moved or re-indented, more than its "lost" lines count of 0/,
  );
  // The fork's added seam lines (another `}`) lose no upstream line.
  assert.deepEqual(
    lostIn({ files: BRACE_SEAM }, up, { "s.ts": `${BRACE_UP}forkHook();\n}\n` }, true).reasons,
    [],
  );
});

// A seam line the file indents still matches its listed (trimmed) text when a pull request drops it.
test("a pull request may drop an indented seam line", () => {
  const main = { "a.ts": `${GUARD_BASE}    forkHook();\n` };
  assert.deepEqual(lostIn({ files: GUARD_SEAM }, main, { "a.ts": GUARD_BASE }, false).reasons, []);
});

test("lostLines counts the base lines not kept in order (longest common subsequence)", () => {
  assert.equal(lostLines(["a", "b", "c"], ["a", "b", "c"]), 0);
  assert.equal(lostLines(["a", "b", "c"], ["x", "a", "y", "b", "c", "z"]), 0);
  assert.equal(lostLines(["a", "b", "c"], ["b", "a", "c"]), 1);
  assert.equal(lostLines(["a", "b", "c", "d"], ["d", "c", "b", "a"]), 3);
  assert.equal(lostLines(["a", "a", "b"], ["a", "b"]), 1);
  assert.equal(lostLines(["a"], []), 1);
  assert.equal(lostLines([], ["a"]), 0);
});

// Upstream renames a listed file. The rebase follows the rename; the check before it
// refused the new name (the base lacks it) and the check after it the old one, so no list passed.
test("a listed path that neither the base nor the head has is a note, not a refusal", () => {
  const config = { files: { "old.ts": { why: "t", added: [] } }, pending: { "gone.ts": 1 } };
  const full = { files: {}, pending: {}, forkFiles: [], lost: {}, ...config };
  const none = () => undefined;
  assert.deepEqual(
    findSeamBreaches(full.files, { atBase: none, atHead: none, upstream: true }),
    [],
  );
  assert.deepEqual(
    findUnlistedChanges(full, {
      changed: [],
      atBase: none,
      atHead: none,
      upstream: true,
      isForkFile: () => false,
    }),
    { reasons: [], ceilings: [], rises: [], notes: [] },
  );
  assert.deepEqual(findListedGone(full, { atBase: none, atHead: none }), [
    "old.ts: listed in .github/fork-seams.json, but neither the base nor the head has it (renamed or removed upstream?); remove the entry once the fork's base no longer has it.",
    "gone.ts: listed in .github/fork-seams.json, but neither the base nor the head has it (renamed or removed upstream?); remove the entry once the fork's base no longer has it.",
  ]);
  // Only one side missing still fails closed.
  const has = () => UP;
  assert.equal(findSeamBreaches(full.files, { atBase: none, atHead: has }).length, 1);
  assert.equal(findSeamBreaches(full.files, { atBase: has, atHead: none }).length, 1);
  for (const [atBase, atHead] of [
    [none, has],
    [has, none],
  ]) {
    const result = findUnlistedChanges(full, { changed: [], atBase, atHead, upstream: true });
    assert.equal(result.reasons.length, 1);
    assert.deepEqual(findListedGone(full, { atBase, atHead }), []);
  }
});

test("upstream renames a pending file: the list with both names passes before and after the rebase", () => {
  withRepo(({ git, commit, fromUpstream }) => {
    const fork = "if (forkGate()) return;\n";
    const u1 = commit({ "old.ts": GUARD_BASE }, "upstream 1");
    git("checkout", "-q", "-b", "upstream", u1);
    git("mv", "old.ts", "new.ts");
    const u2 = commit({}, "upstream 2 renames old.ts");
    const both = seams({ files: {}, pending: { "old.ts": 1, "new.ts": 1 } });
    git("checkout", "-q", "-b", "series", u1);
    const before = commit({ ...both, "old.ts": `${GUARD_BASE}${fork}` }, "fork series");
    git("checkout", "-q", "-b", "rebased", u2);
    const after = commit({ ...both, "new.ts": `${GUARD_BASE}${fork}` }, "fork series, rebased");
    const first = fromUpstream(u1, before);
    assert.equal(first.status, 0, first.stderr);
    assert.match(first.stdout, /^Note: new\.ts: listed in .*, but neither the base nor the head/m);
    const second = fromUpstream("--after-rebase", u2, after);
    assert.equal(second.status, 0, second.stderr);
    assert.match(second.stdout, /^Note: old\.ts: listed in .*, but neither the base nor the head/m);
    // Runs again: the same answer.
    assert.equal(fromUpstream("--after-rebase", u2, after).stdout, second.stdout);
    // The rebased series' fork line in the renamed file is still counted.
    const grown = commit({ "new.ts": `${GUARD_BASE}${fork}forkMore();\n` }, "grow it");
    assert.equal(fromUpstream(u2, grown).status, 1);
  });
});

// Guards that a mutation used to survive.
test("a pull request that lists an upstream workflow in its own removed list cannot delete it", () => {
  withRepo(({ git, commit, run }) => {
    const ci = ".github/workflows/ci.yml";
    commit({ [ci]: "on: push\n", ...BASE }, "upstream");
    const main = commit(seams({ files: SEAM }), "fork main");
    const pr = commit(
      { [ci]: null, ".github/fork-removed-workflows.txt": `${ci}\n` },
      "list and delete ci.yml",
    );
    git("checkout", "-q", "-f", pr);
    for (const args of [
      [main, pr],
      ["--config-ref", main, main, pr],
    ]) {
      const result = run(...args);
      assert.equal(result.status, 1, args.join(" "));
      assert.match(
        result.stderr,
        /- \.github\/workflows\/ci\.yml: upstream file this change deletes/,
      );
    }
  });
});

test("from upstream, only a workflow named fork-* may go, not any upstream file with a fork name", () => {
  const files = ["src/forkJoin.ts", "src/fork-thing.ts", ".github/fork-up.txt", "fork/up.ts"];
  const base = Object.fromEntries(files.map((file) => [file, UP]));
  assert.deepEqual(
    unlisted({}, base, {}, true).reasons.map((reason) => reason.split(":")[0]),
    files,
  );
  for (const file of [".github/workflows/fork-x.yml", ".github/workflows/fork-y.yaml"]) {
    assert.deepEqual(unlisted({}, { [file]: "on: push\n" }, {}, true).reasons, [], file);
  }
  // A fork-* file in another folder, or a nested workflow folder, is not a workflow the fork deletes.
  for (const file of [".github/actions/fork-x.yml", ".github/workflows/sub/fork-x.yml"]) {
    assert.equal(unlisted({}, { [file]: "on: push\n" }, {}, true).reasons.length, 1, file);
  }
});

// Ceiling refusals were found by their message text, so a file named like that text got
// only the "lower the ceiling" advice.
test("a ceiling refusal is known by its kind, not its text", () => {
  withRepo(({ commit, fromUpstream }) => {
    const odd = 'x: "pending" with no fork lines left';
    const upstream = commit({ [odd]: UP }, "upstream");
    const series = commit({ ...seams({ files: {} }), [odd]: `${UP}forkLine();\n` }, "fork series");
    const result = fromUpstream(upstream, series);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /upstream file with 1 fork line but no entry/);
    assert.match(result.stderr, /a file of the fork's own/);
    assert.doesNotMatch(result.stderr, /Lower each "pending" ceiling/);
  });
});

// "From upstream" was guessed from a base without the list, so a fork of this fork
// (whose upstream carries a list) was judged by upstream's list as a pull request.
test("--from-upstream reads the head's list even when the upstream commit has one; without it a base with no list is refused", () => {
  withRepo(({ git, commit, run, fromUpstream }) => {
    // The fork this fork follows allows `inline: 1,` in a.ts; this fork's list does not.
    const wide = { files: { "a.ts": { why: "theirs", added: ["inline: 1,"] } } };
    const upstream = commit({ ...seams(wide), ...BASE }, "upstream fork");
    const series = commit(
      {
        ...seams({ files: SEAM }),
        "a.ts": BASE["a.ts"].replace("  a: 1,", "  inline: 1,\n  a: 1,"),
      },
      "this fork's series",
    );
    const refused = fromUpstream(upstream, series);
    assert.equal(refused.status, 1, refused.stdout);
    assert.match(refused.stderr, /a\.ts: adds `inline: 1,`/);
    assert.match(refused.stderr, /list from the head/);
    // Without the flag a base with no list is not taken for upstream: it is refused.
    git("checkout", "-q", "--orphan", "plain");
    git("rm", "-rfq", ".");
    const plain = commit(BASE, "upstream without a list");
    const onPlain = commit(seams({ files: SEAM }), "series");
    const noFlag = run(plain, onPlain);
    assert.equal(noFlag.status, 1);
    assert.match(noFlag.stderr, /^The base .* has no \.github\/fork-seams\.json/);
    assert.equal(fromUpstream(plain, onPlain).status, 0);
    // Bad flag use is a usage error, never a pass.
    for (const args of [
      ["--from-upstream", "--from-upstream", plain, onPlain],
      ["--from-upstream", "--config-ref", onPlain, plain, onPlain],
      ["--after-rebase", plain, onPlain],
      ["--from-upstream=1", plain, onPlain],
    ]) {
      assert.equal(run(...args).status, 2, args.join(" "));
    }
  });
});

// A list from before "lost" (main's today): from upstream, every lost line is counted against 0.
test('from upstream, a list with no "lost" counts refuses a seam file that lost an upstream line', () => {
  withRepo(({ commit, fromUpstream, run }) => {
    const upstream = commit({ "a.ts": GUARD_BASE }, "upstream");
    const series = commit(
      {
        ...seams({ files: GUARD_SEAM }),
        "a.ts": "import { x } from './x';\ndoSensitive();\nforkHook();\n",
      },
      "fork series drops the guard",
    );
    const result = fromUpstream(upstream, series);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /- a\.ts: 1 upstream line deleted, moved or re-indented/);
    const recorded = commit(
      seams({ files: GUARD_SEAM, lost: { "a.ts": { lines: 1, gone: 1 } } }),
      "record it",
    );
    assert.equal(fromUpstream(upstream, recorded).status, 0);
    // On a pull request to that main, moving an upstream line adds none, and is still refused.
    const pr = commit({ "a.ts": "doSensitive();\nimport { x } from './x';\nforkHook();\n" }, "PR");
    const moved = run(recorded, pr);
    assert.equal(moved.status, 1);
    assert.match(
      moved.stderr,
      /- a\.ts: 1 upstream line deleted, moved or re-indented by this change/,
    );
    assert.doesNotMatch(moved.stderr, /adds/);
  });
});

// A count is a number, not a set of lines: an upstream line the fork re-indented already counts
// as lost, so deleting it later left the ordered count the same, and both runs passed.
test("from upstream, deleting an upstream line already counted as re-indented is refused", () => {
  withRepo(({ git, commit, run, fromUpstream }) => {
    const guard = "if (!req.authorized) {\n  return deny();\n}\nserve();\n";
    const upstream = commit({ "p.ts": guard }, "upstream");
    const reindented = "  if (!req.authorized) {\n    return deny();\n  }\nserve();\nforkA();\n";
    const config = { files: {}, pending: { "p.ts": 1 }, lost: { "p.ts": { lines: 3, gone: 0 } } };
    const main = commit({ ...seams(config), "p.ts": reindented }, "fork re-indents the guard");
    const first = fromUpstream(upstream, main);
    assert.equal(first.status, 0, first.stderr);
    git("checkout", "-q", "-b", "pr", main);
    const pr = commit({ "p.ts": reindented.replace("    return deny();\n", "") }, "delete it");
    // The pull request's run cannot tell upstream's lines from the fork's in a pending file ...
    assert.equal(run(main, pr).status, 0);
    // ... and the run from upstream refuses it: the line's text is gone.
    const result = fromUpstream(upstream, pr);
    assert.equal(result.status, 1, result.stdout);
    assert.match(
      result.stderr,
      /- p\.ts: 1 upstream line deleted or changed \(its text is gone\), more than its "lost" gone count of 0/,
    );
  });
});

// The "lost" ceilings used to be dropped in main() with every test still passing.
test("from upstream, a lost count left too high is refused, naming the count; after a rebase it is a note", () => {
  withRepo(({ commit, fromUpstream }) => {
    const upstream = commit({ "p.ts": GUARD_BASE }, "upstream");
    const config = { files: {}, pending: { "p.ts": 1 }, lost: { "p.ts": { lines: 3, gone: 2 } } };
    const series = commit(
      { ...seams(config), "p.ts": "import { x } from './x';\nforkA();\n" },
      "fork series drops two lines",
    );
    const result = fromUpstream(upstream, series);
    assert.equal(result.status, 1, result.stdout);
    assert.match(
      result.stderr,
      /- p\.ts: 2 upstream lines deleted, moved or re-indented, below its "lost" lines count of 3; lower it to 2\./,
    );
    assert.match(result.stderr, /Lower each "pending" ceiling and "lost" count/);
    assert.doesNotMatch(result.stderr, /a file of the fork's own/);
    const after = fromUpstream("--after-rebase", upstream, series);
    assert.equal(after.status, 0, after.stderr);
    assert.match(
      after.stdout,
      /^Note: p\.ts: 2 upstream lines .* below its "lost" lines count of 3; lower it to 2\.$/m,
    );
  });
});

// Upstream's nightlies from …4695 on have this file; fork-follow stopped after the rebase onto it.
test("the real list takes no upstream file named like a fork file for the fork's own", () => {
  const isForkFile = forkFileTest(realConfig().forkFiles);
  assert.equal(
    isForkFile("scripts/scenarios/fork-title-regenerates-from-inherited-history.e2e.ts"),
    false,
  );
  // The exclusion is that one file: the fork's own scenario files keep their fork names.
  assert.equal(isForkFile("scripts/scenarios/fork-other.e2e.ts"), true);
});

// A PR's own list is printed in a pull_request_target log: a key holding a newline could start a
// line with `::` there, a workflow command.
test("a key from the list is printed escaped, so it cannot forge a workflow command line", () => {
  const forged = "x\n::notice title=forged::y";
  for (const config of [
    { files: SEAM, [forged]: {} },
    { files: { "a.ts": { ...SEAM["a.ts"], [forged]: 1 } } },
    { files: { [forged]: SEAM["a.ts"] } },
    { files: {}, pending: { [forged]: 1 } },
    { files: SEAM, lost: { [forged]: { lines: 1, gone: 0 } } },
    // C1 controls (NEL is a line break to some readers; CSI starts a terminal escape).
    { files: SEAM, ["x\u0085::notice title=forged::y\u009b31m"]: {} },
  ]) {
    const problem = ownListProblem(JSON.stringify(config));
    assert.ok(problem, JSON.stringify(config));
    assert.doesNotMatch(problem, /[\n\r\u0080-\u009f]/, problem);
    assert.match(problem, /x\\u(000a|0085)::notice|x\\n::notice/, problem);
  }
  assert.match(
    ownListProblem(JSON.stringify({ files: SEAM, ["x\u0085y\u009b"]: {} })),
    /x\\u0085y\\u009b/,
  );
  // Node's JSON error quotes a short bad text as it is.
  const badJson = ownListProblem("\n::notice title=forged::y");
  assert.match(badJson, /is malformed: .*JSON/);
  assert.doesNotMatch(badJson, /[\n\r]/, badJson);
});

test("a line the change adds is printed escaped in a refusal", () => {
  withRepo(({ commit, run }) => {
    const main = commit({ ...seams({ files: SEAM }), ...BASE }, "main");
    const pr = commit({ "a.ts": `${BASE["a.ts"]}x\r::warning::forged\u2028y\n` }, "pr");
    const result = run(main, pr);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /x\\u000d::warning::forged\\u2028y/);
    assert.doesNotMatch(result.stderr, /[\r\u2028]/);
    // Bidi and zero-width characters: a refusal shows what the line holds, not what it looks like.
    const hidden = commit(
      { "a.ts": `${BASE["a.ts"]}ok(/*\u202e } \u2066*/\ufeff\u061c\u200e\u200f);\u200b\n` },
      "hidden",
    );
    const shown = run(main, hidden);
    assert.equal(shown.status, 1);
    assert.match(
      shown.stderr,
      /ok\(\/\*\\u202e } \\u2066\*\/\\ufeff\\u061c\\u200e\\u200f\);\\u200b/,
    );
    assert.doesNotMatch(shown.stderr, /[\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/);
    // Other characters that print as nothing (or as a blank that is no space): soft hyphen,
    // Hangul fillers, the Mongolian vowel separator, variation selectors and tag characters.
    const INVISIBLE = [
      ["\u00ad", "\\u00ad"],
      ["\u115f", "\\u115f"],
      ["\u1160", "\\u1160"],
      ["\u180e", "\\u180e"],
      ["\u3164", "\\u3164"],
      ["\ufe00", "\\ufe00"],
      ["\ufe0f", "\\ufe0f"],
      ["\uffa0", "\\uffa0"],
      ["\u{e0000}", "\\u{e0000}"],
      ["\u{e0041}", "\\u{e0041}"],
      ["\u{e007f}", "\\u{e007f}"],
      ["\u{e0100}", "\\u{e0100}"],
      ["\u{e01ef}", "\\u{e01ef}"],
    ];
    const unseen = commit(
      { "a.ts": `${BASE["a.ts"]}ok(${INVISIBLE.map(([char]) => `${char}|`).join("")});\n` },
      "unseen",
    );
    const spelled = run(main, unseen);
    assert.equal(spelled.status, 1);
    assert.ok(
      spelled.stderr.includes(`ok(${INVISIBLE.map(([, text]) => `${text}|`).join("")});`),
      spelled.stderr,
    );
    assert.doesNotMatch(
      spelled.stderr,
      /[\u00ad\u115f\u1160\u180e\u3164\ufe00-\ufe0f\uffa0\u{e0000}-\u{e007f}\u{e0100}-\u{e01ef}]/u,
    );
    // The characters next to each range stay as they are, as does an astral one outside them.
    const kept =
      "\u00ac\u00ae\u115e\u1161\u180d\u180f\u3163\u3165\ufdff\ufe10\uff9f\uffa1\u{dffff}\u{e0080}\u{e00ff}\u{e01f0}\u{1f600}";
    const plain = run(main, commit({ "a.ts": `${BASE["a.ts"]}ok(${kept});\n` }, "kept"));
    assert.equal(plain.status, 1);
    assert.ok(plain.stderr.includes(`ok(${kept});`), plain.stderr);
  });
});

// A listed line with a line break was two allowances: "fork();\nevil();" let a change add
// `evil();` alone, which nobody listed as a line.
test("a listed or announced line with a line break is refused", () => {
  for (const [entry, line] of [
    [{ why: "t", added: ["fork();\nevil();"] }, "fork();\nevil();"],
    [{ why: "t", added: ["fork();\r"] }, "fork();\r"],
    [
      { why: "t", added: [], announced: { since: "2026-11-18", added: ["a();", "b();\nevil();"] } },
      "b();\nevil();",
    ],
  ]) {
    const text = JSON.stringify({ files: { "a.ts": entry } });
    assert.throws(
      () => parseConfig(text),
      (error) =>
        error.message ===
        `${CONFIG}: a.ts: a listed line holds a line break (one line per item): ${JSON.stringify(line)}`,
    );
    assert.match(ownListProblem(text), /a listed line holds a line break/);
  }
});

// JSON.parse keeps the last of two equal keys: a conflict resolved by keeping both entries would
// leave one that is shown but never applies.
test("a pull request's own list with a duplicate key is refused", () => {
  const entry = JSON.stringify(SEAM["a.ts"]);
  for (const [text, key] of [
    [`{"files": {"a.ts": ${entry}, "a.ts": ${entry}}}`, "a.ts"],
    [`{"files": {}, "pending": {"p.ts": 1, "q.ts": 2, "p.ts": 3}}`, "p.ts"],
    [`{"files": {"a.ts": {"why": "x", "added": [], "why": "y"}}}`, "why"],
    [`{"files": {}, "files": {}}`, "files"],
    // Equal once decoded.
    [`{"files": {}, "pending": {"p.ts": 1, "\\u0070.ts": 1}}`, "p.ts"],
  ]) {
    assert.equal(ownListProblem(text), `${OWN} is malformed: duplicate key ${JSON.stringify(key)}`);
  }
  // The same key in different objects, and keys inside strings, are not duplicates.
  for (const text of [
    `{"files": {"a.ts": {"why": "a \\" \\"why\\": {", "added": ["{\\"why\\": 1}"]}, "b.ts": {"why": "b", "added": []}}, "pending": {"a.x": 1}}`,
    `{"//": "files", "files": {}}`,
  ]) {
    assert.equal(ownListProblem(text), undefined, text);
  }
  assert.equal(ownListProblem(readReal(CONFIG)), undefined);
});
const OWN = "this change's own .github/fork-seams.json";

// Right after a rebase a count can rise from upstream's own change, not a fork pull request: the
// refusal then says what the operator does, not what a pull request author does.
test("after a rebase, a count that rises names the operator's next step", () => {
  const config = { pending: { "p.ts": 1 }, lost: { "p.ts": { lines: 1, gone: 1 } } };
  const base = { "p.ts": GUARD_BASE };
  const head = { "p.ts": "doSensitive();\nforkA();\n" };
  for (const afterRebase of [false, true]) {
    // After a rebase a rise is the operator's (its own kind), not a pull request author's.
    const { reasons, rises } = lostIn(config, base, head, true, afterRebase);
    assert.equal((afterRebase ? reasons : rises).length, 0);
    if (afterRebase) reasons.push(...rises);
    assert.equal(reasons.length, 2);
    for (const reason of reasons) {
      if (afterRebase) {
        assert.match(reason, /after the rebase .* rebase by hand .* #325/, reason);
        assert.doesNotMatch(reason, /a file of the fork's own/, reason);
      } else {
        assert.match(reason, /a file of the fork's own/, reason);
      }
    }
  }
  const over = findUnlistedChanges(
    { files: {}, pending: { "p.ts": 1 }, forkFiles: [] },
    {
      changed: ["p.ts"],
      atBase: () => UP,
      atHead: () => `${UP}forkA();\nforkB();\n`,
      upstream: true,
      isForkFile: () => false,
      afterRebase: true,
    },
  );
  assert.deepEqual(over.reasons, []);
  assert.match(over.rises[0], /more than its ceiling of 1 after the rebase .* rebase by hand/);
});

test("an announcement is refused from upstream once it is older than its days, or dated ahead", () => {
  const files = {
    "a.ts": SEAM["a.ts"],
    "n.ts": { why: "t", added: [], announced: { since: "2026-11-04", added: ["x", "y"] } },
  };
  assert.deepEqual(findExpiredAnnouncements(files, "2026-11-18"), []);
  assert.deepEqual(findExpiredAnnouncements(files, "2026-11-19"), [
    'n.ts: 2 lines "announced" on 2026-11-04, more than 14 days before 2026-11-19 (the head\'s commit date): announce them again (set "since" to the day that pull request merges) or remove the announcement, with a list-only pull request to main.',
  ]);
  // A date ahead of the head's would never expire (one day is left for time zones).
  const ahead = { "n.ts": { ...files["n.ts"], announced: { since: "2026-11-20", added: ["x"] } } };
  assert.deepEqual(findExpiredAnnouncements(ahead, "2026-11-19"), []);
  assert.deepEqual(findExpiredAnnouncements(ahead, "2026-11-18"), [
    'n.ts: 1 line "announced" on 2026-11-20, after 2026-11-18 (the head\'s commit date): date it the day it is announced.',
  ]);
});

// A pull request is judged by main's list, so a new seam line had no green path. Step 1 (a
// list-only change) announces it; step 2 adds it and moves it to "added".
test("a new seam line lands green in two steps: announce, then add", () => {
  withRepo(({ git, commit, run, fromUpstream }) => {
    const upstream = commit({ ...BASE, "n.ts": UP }, "upstream");
    const day = "2026-11-18T12:00:00Z";
    const main = commit(seams({ files: SEAM }), "fork main", day);
    const announce = (since = "2026-11-18") => ({
      files: {
        ...SEAM,
        "n.ts": { why: "new", added: [], announced: { since, added: ["forkNew();"] } },
      },
    });
    const added = { files: { ...SEAM, "n.ts": { why: "new", added: ["forkNew();"] } } };
    const line = { "n.ts": `${UP}forkNew();\n` };
    // Step 2 before step 1: refused, as before.
    git("checkout", "-q", "-b", "early", main);
    const early = commit({ ...line, ...seams(added) }, "step 2 first", day);
    assert.equal(run(main, early).status, 1);
    // Step 1: list only, green in the pull request's run and from upstream.
    git("checkout", "-q", "-f", "-B", "main", main);
    const step1 = commit(seams(announce()), "step 1", day);
    assert.equal(run(main, step1).status, 0);
    const listed = fromUpstream(upstream, step1);
    assert.equal(listed.status, 0, listed.stderr);
    // Step 2, judged by main's list with the announcement: green, and the line drops the flag.
    git("checkout", "-q", "-b", "step2", step1);
    const step2 = commit({ ...line, ...seams(added) }, "step 2", day);
    for (const args of [
      [step1, step2],
      ["--config-ref", step1, step1, step2],
    ]) {
      const result = run(...args);
      assert.equal(result.status, 0, result.stderr);
    }
    assert.equal(fromUpstream(upstream, step2).status, 0);
    // An announced line allows only its own copies, and only in its file.
    const twice = commit(
      { "n.ts": `${UP}forkNew();\nforkNew();\n`, "a.ts": `${BASE["a.ts"]}forkNew();\n` },
      "more",
      day,
    );
    const more = run(step1, twice);
    assert.equal(more.status, 1);
    assert.match(more.stderr, /- n\.ts: adds `forkNew\(\);`/);
    assert.match(more.stderr, /- a\.ts: adds `forkNew\(\);`/);
    // Never followed by step 2: from upstream the run refuses it once it is 15 days old, and a
    // list-only change that drops it (or step 2) is green again.
    git("checkout", "-q", "-f", "-B", "main", step1);
    const later = commit({ "fork/own.ts": UP }, "a later merge", "2026-12-03T12:00:00Z");
    const expired = fromUpstream(upstream, later);
    assert.equal(expired.status, 1, expired.stdout);
    assert.match(expired.stderr, /- n\.ts: 1 line "announced" on 2026-11-18, more than 14 days/);
    assert.match(
      expired.stderr,
      /Announce each line named above again \(set "since" to the day that pull request merges\) or remove its announcement, with a list-only pull request to main; only then step 2\. Step 2 alone is refused once main's commit date is past the announcement's days too/,
    );
    // One wording for the day in every message: "today" loses the days a review takes.
    assert.doesNotMatch(expired.stderr, /to today|refused too/);
    assert.doesNotMatch(expired.stderr, /add them \(step 2\)|Add each announced line/);
    assert.doesNotMatch(expired.stderr, /a file of the fork's own/);
    // The pull request's run reads no clock and refuses only a line an expired announcement
    // alone allows: a change that adds none is green.
    assert.equal(run(step1, later).status, 0);
    const dropped = commit(seams({ files: SEAM }), "drop it", "2026-12-03T12:00:00Z");
    assert.equal(run(later, dropped).status, 0);
    assert.equal(fromUpstream(upstream, dropped).status, 0);
    // Runs again on the same commits: the same answer (the head's commit date, not the clock).
    assert.equal(fromUpstream(upstream, later).stderr, expired.stderr);
    // "Step 2 alone is refused" holds only once main's own date is past the days. A head dated
    // later than main (a fresh rebase) expires first: step 2 alone, on a main still inside its
    // days, is green and fixes it.
    git("checkout", "-q", "-f", "-B", "rebased", step1);
    const rebased = commit({ "fork/own.ts": UP }, "rebased later", "2026-12-03T12:00:00Z");
    assert.equal(fromUpstream(upstream, rebased).status, 1);
    assert.equal(run(step1, step2).status, 0);
    assert.equal(fromUpstream(upstream, step2).status, 0);
  });
});

const ANNOUNCED = (since = "2026-11-18") => ({
  files: {
    ...SEAM,
    "n.ts": { why: "new", added: [], announced: { since, added: ["forkNew();"] } },
  },
});
const ADDED = { files: { ...SEAM, "n.ts": { why: "new", added: ["forkNew();"] } } };

// Upstream added a line main announces. fork-follow pushed the rebase, and main's stale-list test
// (fork-checks runs it on main's tree for every pull request) turned every pull request red, the
// fix's included. From upstream the run now stops first.
test("from upstream, an announced line upstream itself added stops the run before the push", () => {
  withRepo(({ git, commit, run, fromUpstream }) => {
    const day = "2026-11-18T12:00:00Z";
    const upstream = commit({ ...BASE, "n.ts": UP }, "upstream", day);
    const main = commit(seams(ANNOUNCED()), "step 1", day);
    assert.equal(fromUpstream(upstream, main).status, 0);
    // Upstream's next nightly adds the same line; the series is rebased onto it.
    git("checkout", "-q", "-b", "next", upstream);
    const next = commit({ "n.ts": `${UP}  forkNew();\n` }, "upstream adds the line", day);
    const rebased = commit(seams(ANNOUNCED()), "step 1, rebased", day);
    const first = fromUpstream("--after-rebase", next, rebased);
    for (const result of [first, fromUpstream(next, rebased)]) {
      assert.equal(result.status, 1, result.stdout);
      assert.match(
        result.stderr,
        /- n\.ts: the announced line `forkNew\(\);` is already in the head's file \(1× there\): upstream added it/,
      );
      assert.match(
        result.stderr,
        /Remove each announcement named above .* with a list-only pull request to main/,
      );
      assert.doesNotMatch(result.stderr, /a file of the fork's own/);
    }
    // Runs again: the same answer.
    assert.equal(fromUpstream("--after-rebase", next, rebased).stderr, first.stderr);
    // The way out: a list-only change to main that drops it is green (main's tree does not have
    // upstream's line yet), and so is the next run from upstream.
    git("checkout", "-q", "-f", "-B", "main", main);
    const dropped = commit(seams({ files: SEAM }), "drop it", day);
    assert.equal(run(main, dropped).status, 0);
    git("checkout", "-q", "-f", "-B", "rebased", next);
    const rebasedDropped = commit(seams({ files: SEAM }), "drop it, rebased", day);
    assert.equal(fromUpstream("--after-rebase", next, rebasedDropped).status, 0);
    assert.equal(fromUpstream(next, rebasedDropped).status, 0);
  });
});

// Round 2's surviving mutant (M22): the landed check read upstream's base, not the head. A head
// that has an announced line upstream lacks (step 2 kept its announcement) is refused too.
test("from upstream, an announced line the head has and upstream does not stops the run", () => {
  withRepo(({ commit, fromUpstream }) => {
    const day = "2026-11-18T12:00:00Z";
    const upstream = commit({ ...BASE, "n.ts": UP }, "upstream", day);
    const head = commit({ "n.ts": `${UP}forkNew();\n`, ...seams(ANNOUNCED()) }, "kept", day);
    for (const args of [
      [upstream, head],
      ["--after-rebase", upstream, head],
    ]) {
      const result = fromUpstream(...args);
      assert.equal(result.status, 1, result.stdout);
      assert.match(
        result.stderr,
        /- n\.ts: the announced line `forkNew\(\);` is already in the head's file \(1× there\)/,
      );
    }
  });
});

// The state lens's surviving mutant: only "added" lines are the fork's in main's file. Should an
// announced line be in it anyway (upstream's copy), a pull request may not delete it as "dropped".
test("on a pull request, an announced line in main's file is upstream's and may not go", () => {
  const config = ANNOUNCED();
  const main = { "n.ts": `${UP}forkNew();\n` };
  const { reasons } = lostIn(config, main, { "n.ts": UP }, false);
  assert.equal(reasons.length, 1, reasons.join("\n"));
  assert.match(reasons[0], /^n\.ts: 1 upstream line deleted, moved or re-indented by this change/);
});

// An expired announcement stayed spendable in a pull request's run. Dated by main's commit, never
// the pull request's (its author sets that date).
test("on a pull request, a line only an expired announcement allows is refused, by main's date", () => {
  withRepo(({ git, commit, run }) => {
    const step1 = commit(
      { ...BASE, "n.ts": UP, ...seams(ANNOUNCED()) },
      "main",
      "2026-11-18T12:00:00Z",
    );
    const step2 = { "n.ts": `${UP}forkNew();\n`, ...seams(ADDED) };
    const later = commit({ "fork/own.ts": UP }, "main moves on", "2026-12-03T12:00:00Z");
    git("checkout", "-q", "-b", "pr", later);
    // The pull request's own commit is dated inside the days: it does not count.
    const spend = commit(step2, "step 2", "2026-11-20T12:00:00Z");
    for (const args of [
      [later, spend],
      ["--config-ref", later, later, spend],
    ]) {
      const result = run(...args);
      assert.equal(result.status, 1, result.stdout);
      assert.match(
        result.stderr,
        /- n\.ts: adds `forkNew\(\);` \(its announcement of 2026-11-18 is more than 14 days before 2026-12-03, the list's commit date: announce it again with a list-only pull request\)/,
      );
      // A late step 2 is the announcement's to fix, not a reason to move the line elsewhere.
      assert.match(
        result.stderr,
        /Announce each line named above again .* then update this change/,
      );
      assert.doesNotMatch(result.stderr, /A new seam line needs its own entry/);
    }
    // With another refusal beside it, the author hint is printed too.
    const both = commit(
      { "a.ts": BASE["a.ts"].replace("  a: 1,", "  inline: 1,\n  a: 1,") },
      "step 2 and more",
      "2026-11-20T12:00:00Z",
    );
    const mixed = run(later, both);
    assert.equal(mixed.status, 1);
    assert.match(mixed.stderr, /A new seam line needs its own entry/);
    assert.match(mixed.stderr, /Announce each line named above again/);
    // The same expired line added twice: announcing again allows one copy, so only that copy is
    // the announcement's; the second is a line beyond the seam, with the author hint.
    git("checkout", "-q", "-f", "-B", "twice", later);
    const twice = commit(
      { "n.ts": `${UP}forkNew();\nforkNew();\n`, ...seams(ADDED) },
      "step 2, twice",
      "2026-11-20T12:00:00Z",
    );
    const doubled = run(later, twice);
    assert.equal(doubled.status, 1);
    assert.match(
      doubled.stderr,
      /- n\.ts: adds `forkNew\(\);` \(its announcement of 2026-11-18 is/,
    );
    assert.match(doubled.stderr, /- n\.ts: adds `forkNew\(\);`\n/);
    assert.doesNotMatch(doubled.stderr, /2×/);
    assert.match(doubled.stderr, /A new seam line needs its own entry/);
    assert.match(doubled.stderr, /Announce each line named above again/);
    // Inside the days by main's date, a late pull request date does not refuse it.
    git("checkout", "-q", "-f", "-B", "main2", step1);
    const inside = commit({ "fork/own.ts": UP }, "main moves on", "2026-12-02T12:00:00Z");
    const late = commit(step2, "step 2", "2027-01-30T12:00:00Z");
    assert.equal(run(inside, late).status, 0);
    // No deadlock: the change that drops the expired announcement adds no line, so it is green.
    git("checkout", "-q", "-f", "-B", "drop", later);
    const drop = commit(seams({ files: SEAM }), "drop it", "2026-12-03T12:00:00Z");
    assert.equal(run(later, drop).status, 0);
    // Lines "added" lists are not dated: an expired announcement elsewhere in the entry leaves them.
    git("checkout", "-q", "-f", "-B", "seam", later);
    const seam = commit({ "a.ts": BASE["a.ts"].replace("  a: 1,", "  ...fork,\n  a: 1,") }, "seam");
    assert.equal(run(later, seam).status, 0);
  });
});

// A future-dated announcement on main never expired in a pull request's run (only fork-follow
// refused it). Dated by the commit whose list judges the pull request (--config-ref, else base).
test("on a pull request, a line only a future-dated announcement allows is refused, dated by the list's commit", () => {
  withRepo(({ git, commit, run }) => {
    const day = "2026-11-18T12:00:00Z";
    const upstream = commit({ ...BASE, "n.ts": UP, ...seams({ files: SEAM }) }, "main", day);
    const step2 = { "n.ts": `${UP}forkNew();\n`, ...seams(ADDED) };
    const runs = (main, pr) => [run(main, pr), run("--config-ref", main, main, pr)];
    // Dated ahead (more than the day left for time zones): refused, naming the fix.
    git("checkout", "-q", "-b", "ahead", upstream);
    const ahead = commit(seams(ANNOUNCED("2099-01-01")), "step 1, dated ahead", day);
    const spend = commit(step2, "step 2", day);
    for (const result of runs(ahead, spend)) {
      assert.equal(result.status, 1, result.stdout);
      assert.match(
        result.stderr,
        /- n\.ts: adds `forkNew\(\);` \(its announcement of 2099-01-01 is dated after 2026-11-18, the list's commit date: announce it again with a list-only pull request\)/,
      );
      assert.doesNotMatch(result.stderr, /A new seam line needs its own entry/);
    }
    // One day ahead is time zones: allowed. Two: refused.
    for (const [since, status] of [
      ["2026-11-19", 0],
      ["2026-11-20", 1],
    ]) {
      git("checkout", "-q", "-f", "-B", "near", upstream);
      const near = commit(seams(ANNOUNCED(since)), "step 1", day);
      const used = commit(step2, "step 2", day);
      for (const result of runs(near, used)) assert.equal(result.status, status, since);
    }
    // The way out: a list-only change that re-dates it is green, and step 2 then is too.
    git("checkout", "-q", "-f", "-B", "redate", ahead);
    const redated = commit(seams(ANNOUNCED("2026-11-18")), "announce again", day);
    assert.equal(run(ahead, redated).status, 0);
    assert.equal(run(redated, commit(step2, "step 2", day)).status, 0);

    // A pull request's base (fork-checks: pull_request.base.sha) can be older than the list's
    // commit (main now): the date is the list's. Expired by main's date, not by the old base's:
    git("checkout", "-q", "-f", "-B", "main", upstream);
    const step1 = commit(seams(ANNOUNCED("2026-11-18")), "step 1", day);
    const later = commit({ "fork/own.ts": UP }, "main moves on", "2026-12-03T12:00:00Z");
    git("checkout", "-q", "-f", "-B", "old", step1);
    const fromOld = commit(step2, "step 2 from an old base", day);
    const late = run("--config-ref", later, step1, fromOld);
    assert.equal(late.status, 1, late.stdout);
    assert.match(late.stderr, /more than 14 days before 2026-12-03, the list's commit date/);
    // ...and a base older than the announcement does not make main's announcement "ahead".
    git("checkout", "-q", "-f", "-B", "early", upstream);
    const oldBase = commit({ "fork/old.ts": UP }, "old main", "2026-11-01T12:00:00Z");
    git("checkout", "-q", "-f", "-B", "main2", oldBase);
    const main2 = commit(seams(ANNOUNCED("2026-11-18")), "step 1", day);
    git("checkout", "-q", "-f", "-B", "pr2", oldBase);
    const pr2 = commit(step2, "step 2 from a base before step 1", day);
    const legit = run("--config-ref", main2, oldBase, pr2);
    assert.equal(legit.status, 0, legit.stderr);
  });
});

// For an annotated tag `git show` printed the tag's header and message before the date, and for a
// commit with no committer date no day at all. That text is no day, every comparison with it was
// false, and an expired or future-dated announcement allowed its lines again.
test("the date is read through an annotated tag, and a commit with no date stops the run", () => {
  withRepo(({ dir, git, commit, run, fromUpstream }) => {
    const upstream = commit({ ...BASE, "n.ts": UP }, "upstream", "2026-11-18T12:00:00Z");
    commit(seams(ANNOUNCED()), "step 1", "2026-11-18T12:00:00Z");
    const later = commit({ "fork/own.ts": UP }, "main moves on", "2026-12-03T12:00:00Z");
    git("tag", "-a", "-m", "a release\n\n2026-11-19", "listed", later);
    git("checkout", "-q", "-b", "pr", later);
    const step2 = { "n.ts": `${UP}forkNew();\n`, ...seams(ADDED) };
    const spend = commit(step2, "step 2", "2026-11-20T12:00:00Z");
    // A pull request's run with the list named by a tag: the tagged commit's date.
    const tagged = run("--config-ref", "listed", later, spend);
    assert.equal(tagged.status, 1, tagged.stdout);
    assert.match(tagged.stderr, /more than 14 days before 2026-12-03, the list's commit date/);
    // From upstream with the head named by a tag.
    const expired = fromUpstream(upstream, "listed");
    assert.equal(expired.status, 1, expired.stdout);
    assert.match(expired.stderr, /"announced" on 2026-11-18, more than 14 days before 2026-12-03/);
    // A commit whose committer line has no date: both runs stop, naming the ref.
    const undated = execFileSync(
      "git",
      ["-C", dir, "hash-object", "-t", "commit", "-w", "--literally", "--stdin"],
      {
        input: `tree ${git("rev-parse", `${later}^{tree}`).trim()}\nparent ${later}\nauthor t <t@t> 1795000000 +0000\ncommitter t <t@t>\n\nno date\n`,
        encoding: "utf8",
      },
    ).trim();
    for (const stopped of [
      run("--config-ref", undated, later, spend),
      fromUpstream(upstream, undated),
    ]) {
      assert.equal(stopped.status, 3, stopped.stdout + stopped.stderr);
      assert.ok(
        stopped.stderr.startsWith(`fork-seam-check crashed: ${undated}: no commit date (`),
        stopped.stderr,
      );
      assert.equal(stopped.stdout, "");
    }
    // Runs again: the same answer.
    assert.equal(run("--config-ref", "listed", later, spend).stderr, tagged.stderr);
  });
});

// The same guard for a caller of the functions: a date that is no day throws, it does not compare.
test("a date that is no day stops the check instead of allowing an announcement", () => {
  const files = { "n.ts": ANNOUNCED("2000-01-01").files["n.ts"] };
  const spent = (today) =>
    findSeamBreaches(files, { atBase: () => UP, atHead: () => `${UP}forkNew();\n`, today });
  assert.equal(spent("2026-11-18").length, 1);
  assert.equal(findExpiredAnnouncements(files, "2026-11-18").length, 1);
  for (const today of ["", "tag v1\nTagger: t <t@t>\n\nmsg\n2026-11-18", "%cs", "2026-02-30"]) {
    const refused = { message: `not a day (YYYY-MM-DD): ${JSON.stringify(today.slice(0, 40))}` };
    assert.throws(() => spent(today), refused);
    assert.throws(() => findExpiredAnnouncements(files, today), refused);
    assert.throws(() => findAnnouncementWarnings(files, {}, today), refused);
  }
});

// A step-1 pull request that waited for its reviews could merge (nearly) expired; fork-follow then
// stops. A warning in its run, never a failure.
test("a pull request's new announcement that expires within 3 days is a warning, not a failure", () => {
  const base = ANNOUNCED("2026-11-01").files;
  const own = (since) => ANNOUNCED(since).files;
  assert.deepEqual(findAnnouncementWarnings(own("2026-11-07"), {}, "2026-11-18"), []);
  assert.deepEqual(findAnnouncementWarnings(own("2026-11-06"), {}, "2026-11-18"), [
    'n.ts: "announced" on 2026-11-06, so fork-follow refuses it from 2026-11-21 (more than 14 days): set "since" to the day this pull request merges.',
  ]);
  assert.deepEqual(findAnnouncementWarnings(own("2026-11-20"), {}, "2026-11-18"), [
    'n.ts: "announced" on 2026-11-20, after today (2026-11-18), so fork-follow refuses it: set "since" to the day this pull request merges.',
  ]);
  assert.deepEqual(findAnnouncementWarnings(own("2026-11-19"), {}, "2026-11-18"), []);
  // Only an announcement this change adds or changes: main's own is fork-follow's to refuse.
  assert.deepEqual(findAnnouncementWarnings(own("2026-11-01"), base, "2026-11-18"), []);
  assert.equal(findAnnouncementWarnings(own("2026-11-02"), base, "2026-11-18").length, 1);
  // In the run (the clock's day: a warning only), the step-1 pull request stays green; a change
  // that keeps main's announcement as it is gets no warning.
  withRepo(({ commit, run }) => {
    const main = commit({ ...BASE, "n.ts": UP, ...seams({ files: SEAM }) }, "main");
    const step1 = commit(seams(ANNOUNCED("2020-01-01")), "step 1, long ago");
    const result = run(main, step1);
    assert.equal(result.status, 0, result.stderr);
    assert.match(
      result.stdout,
      /^Warning: n\.ts: "announced" on 2020-01-01, so fork-follow refuses it from 2020-01-16/m,
    );
    const other = commit(
      seams({ ...ANNOUNCED("2020-01-01"), forkFiles: ["tools/"] }),
      "a later list change",
    );
    const again = run(step1, other);
    assert.equal(again.status, 0, again.stderr);
    assert.doesNotMatch(again.stdout, /Warning:/);
  });
});

// After a rebase the author hint ("a file of the fork's own") followed the operator's next step
// and contradicted it.
test("after a rebase, a rising count prints the operator's step and not the author hint", () => {
  withRepo(({ commit, fromUpstream }) => {
    const upstream = commit({ "p.ts": UP }, "upstream");
    const series = commit(
      { "p.ts": `${UP}forkA();\nforkB();\n`, ...seams({ files: {}, pending: { "p.ts": 1 } }) },
      "fork series",
    );
    const after = fromUpstream("--after-rebase", upstream, series);
    assert.equal(after.status, 1);
    assert.match(after.stderr, /more than its ceiling of 1 after the rebase .* rebase by hand/);
    assert.doesNotMatch(after.stderr, /a file of the fork's own/);
    const next = fromUpstream(upstream, series);
    assert.equal(next.status, 1);
    assert.match(next.stderr, /a file of the fork's own/);
  });
});

// Upstream nightlies from …4736 on have this file, which "forkFiles" named; after the rebase the
// run from upstream refused it as hidden.
test("the real list takes upstream's MarkdownFileLink.logic.test.ts for an upstream file", () => {
  const file = "apps/web/src/components/chat/MarkdownFileLink.logic.test.ts";
  const isForkFile = forkFileTest(realConfig().forkFiles);
  assert.equal(isForkFile(file), false);
  assert.deepEqual(
    findHiddenUpstreamFiles([file], isForkFile, () => UP),
    [],
  );
  // The fork's other MarkdownFileLink files are upstream's already (in "pending").
  assert.equal(isForkFile("apps/web/src/components/chat/MarkdownFileLink.logic.ts"), false);
});
