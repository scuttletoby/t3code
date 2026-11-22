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
  findSpareSeamLines,
  findListedGone,
  findLostLines,
  findListedNotFork,
  findLostNotUpstream,
  findOwnHiddenFiles,
  FETCH_ATTEMPT_MS,
  FETCH_TOTAL_MS,
  listEditedCounts,
  listEditedFiles,
  findStaleSeamLines,
  findUnlistedChanges,
  forkFileTest,
  GENERATED,
  lineCounts,
  lostLines,
  lowerList,
  ownListProblem,
  parseConfig,
  parseRemoved,
  promotedFiles,
  sameLines,
  UPSTREAM_FILE,
  UPSTREAM_REF,
  upstreamBase,
  upstreamNamedAt,
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
// BASE with exactly SEAM's lines: from upstream each listed copy must be one the fork adds.
const SEAMED = {
  "a.ts":
    "import { x } from './x';\nimport { fork } from './fork';\nconst s = {\n  ...fork,\n  a: 1,\n};\n",
};

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

test("the route tree is judged as generated output: with a route change and its proof test, or line by line", () => {
  const TREE = "apps/web/src/routeTree.gen.ts";
  const PROOF = "apps/web/src/forkRouteTree.test.ts";
  const ROUTE = "apps/web/src/routes/_chat.settings.fork.tsx";
  const tree = "const A = AImport.update({\n  getParentRoute: () => Root,\n} as any)\n";
  // A fork route's block repeats upstream's lines, which no seam entry can list.
  const forkTree = `${tree}const Fork = ForkImport.update({\n  getParentRoute: () => Root,\n} as any)\n`;
  const base = { [TREE]: tree, [PROOF]: "test\n" };
  const withRoute = { [TREE]: forkTree, [PROOF]: "test\n", [ROUTE]: "route\n" };
  for (const upstream of [true, false]) {
    assert.deepEqual(unlisted({}, base, withRoute, upstream).reasons, [], String(upstream));
    // Without a route change, a change to the tree is a hand edit, judged like any upstream file.
    const alone = unlisted({}, base, { ...base, [TREE]: forkTree }, upstream).reasons;
    assert.equal(alone.length, 1);
    assert.match(alone[0], /^apps\/web\/src\/routeTree\.gen\.ts: upstream file with 3 fork lines/);
    const { [PROOF]: _proof, ...noProof } = withRoute;
    assert.deepEqual(unlisted({}, { [TREE]: tree }, noProof, upstream).reasons, [
      `${TREE}: generated from apps/web/src/routes/, but this change has no ${PROOF}, the test that proves it is the generator's output.`,
    ]);
    // Deleting it is still refused.
    const { [TREE]: _tree, ...gone } = withRoute;
    assert.match(
      unlisted({}, base, gone, upstream).reasons[0],
      /^apps\/web\/src\/routeTree\.gen\.ts: upstream file this change deletes/,
    );
    // A folder that only starts like the routes folder is no route change.
    const sibling = { ...base, [TREE]: forkTree, "apps/web/src/routes-x/a.ts": "a\n" };
    const nearRoute = unlisted({}, base, sibling, upstream).reasons;
    assert.equal(nearRoute.length, 1);
    assert.match(
      nearRoute[0],
      /^apps\/web\/src\/routeTree\.gen\.ts: upstream file with 3 fork lines/,
    );
  }
  // On a pull request the proof must be main's: one that changes it (it.skip, an always-green test)
  // could pass its own hand edit, so the tree is then judged line by line. From upstream the head's
  // own script and tests judge it, and upstream has no proof to compare with.
  const weakened = { ...withRoute, [PROOF]: 'it.skip("…", () => {});\n' };
  assert.deepEqual(unlisted({}, { [TREE]: tree }, weakened, true).reasons, []);
  const refused = unlisted({}, base, weakened, false).reasons;
  assert.equal(refused.length, 1);
  assert.match(refused[0], /^apps\/web\/src\/routeTree\.gen\.ts: upstream file with 3 fork lines/);
  assert.match(refused[0], /forkRouteTree\.test\.ts is not main's/);
  // Main's, not the merge-base's: a branch from before main changed the proof is judged line by line
  // until it is updated from main.
  const judge = (main) =>
    findUnlistedChanges(
      { files: {}, pending: {}, forkFiles: [] },
      {
        changed: [TREE, ROUTE],
        atBase: (file) => base[file],
        atHead: (file) => withRoute[file],
        atMain: (file) => main[file],
        upstream: false,
        isForkFile: forkFileTest([]),
      },
    ).reasons;
  assert.deepEqual(judge(base), []);
  assert.match(judge({ ...base, [PROOF]: "stricter test\n" })[0], /is not main's/);
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

// The options apps/web/vite.config.ts passes to tanstackRouter(...), which dev and builds generate
// the route tree with. Throws when the call is gone, repeated or not a plain literal.
function viteRouterOptions(source) {
  const calls = [...source.matchAll(/\btanstackRouter\(/g)];
  const unreadable = (why) =>
    new Error(
      `apps/web/vite.config.ts: ${why}; teach this test to read the router plugin's options again.`,
    );
  if (calls.length !== 1) throw unreadable(`${calls.length} tanstackRouter( calls, not 1`);
  const start = calls[0].index + calls[0][0].length;
  let depth = 1;
  let end = start;
  while (depth > 0 && end < source.length) {
    const char = source[end++];
    if (char === "(") depth += 1;
    if (char === ")") depth -= 1;
  }
  const text = source.slice(start, end - 1).trim();
  if (text === "") return {};
  try {
    return new Function(`"use strict"; return (${text});`)();
  } catch (error) {
    throw unreadable(`its tanstackRouter(${text}) options are not a plain literal (${error})`);
  }
}

// GENERATED must name the tree and folder the router plugin really uses, or the seam check skips the
// wrong files. Checked here, not in the proof test: app code must not read this fork-owned script
// (fork-owned-reads.test.py), since a fork-only change to it would skip the app jobs. The proof test
// pins what the plugin resolves these options to (its defaults: src/routes/, src/routeTree.gen.ts).
test("the generated-file table names the route tree and routes folder vite.config.ts generates with", () => {
  const read = (file) => {
    try {
      return readReal(file);
    } catch {
      return undefined;
    }
  };
  const fileConfig = read("apps/web/tsr.config.json");
  const options = {
    ...(fileConfig === undefined ? {} : JSON.parse(fileConfig)),
    ...viteRouterOptions(readReal("apps/web/vite.config.ts")),
  };
  const inWeb = (file) => path.posix.join("apps/web", file);
  const source = `${inWeb(options.routesDirectory ?? "./src/routes")}/`;
  const tree = inWeb(options.generatedRouteTree ?? "./src/routeTree.gen.ts");
  assert.deepEqual(GENERATED.get(tree), { source, proof: "apps/web/src/forkRouteTree.test.ts" });
  for (const [file, { source: folder, proof }] of GENERATED) {
    assert.notEqual(read(file), undefined, `${file} is not in the tree`);
    assert.notEqual(read(proof), undefined, `${proof} is not in the tree`);
    assert.ok(
      folder.endsWith("/"),
      `${folder}: a folder ends with /, or a sibling's name matches it`,
    );
    const inFolder = execFileSync(
      "git",
      ["-C", REPO_ROOT, "ls-tree", "--name-only", "--full-tree", "HEAD", folder],
      { encoding: "utf8" },
    );
    assert.notEqual(inFolder.trim(), "", `${folder} has no files`);
  }
  // The reader itself: a moved or computed option fails it rather than passing on the defaults.
  assert.deepEqual(viteRouterOptions("x(tanstackRouter({ autoCodeSplitting: true }))"), {
    autoCodeSplitting: true,
  });
  assert.throws(() => viteRouterOptions("plugins: []"), /0 tanstackRouter\( calls/);
  assert.throws(() => viteRouterOptions("tanstackRouter(options)"), /not a plain literal/);
});

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
// Upstream as tests name it: only https:// is taken (a file:// path could name the checkout itself),
// so git's url.<base>.insteadOf maps these to local bare repositories, as a user's config could.
const UPSTREAM_URL = "https://upstream.test/u.git";
const GONE_URL = "https://gone.test/u.git";
const upstreamArgs = ["--from-upstream", "--upstream", "--no-upstream"];

function withRepo(body) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-seams-git-"));
  const upDir = `${dir}-up.git`;
  let published = false;
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
  // `rev` as upstream's default branch at UPSTREAM_URL. Once a test publishes one, its pull
  // request runs name it (--upstream), as fork-checks' run names main's UPSTREAM_FILE.
  const publish = (rev) => {
    if (!published) {
      execFileSync("git", ["init", "-q", "--bare", upDir]);
      execFileSync("git", ["-C", upDir, "config", "uploadpack.allowFilter", "true"]);
      published = true;
    }
    git("push", "-q", "-f", upDir, `${rev}:refs/heads/main`);
    execFileSync("git", ["-C", upDir, "symbolic-ref", "HEAD", "refs/heads/main"]);
  };
  const run = (...args) =>
    spawnSync(
      "node",
      [
        SCRIPT,
        ...(published && !args.some((arg) => upstreamArgs.includes(arg))
          ? ["--upstream", UPSTREAM_URL]
          : []),
        ...args,
      ],
      { cwd: dir, encoding: "utf8" },
    );
  // As fork-checks runs it once main names no upstream file: no flag at all.
  const runBare = (...args) => spawnSync("node", [SCRIPT, ...args], { cwd: dir, encoding: "utf8" });
  // As fork-follow runs it: from an upstream commit, the list read from the head.
  const fromUpstream = (...args) => run("--from-upstream", ...args);
  try {
    git("init", "-q");
    git("config", `url.file://${upDir}.insteadOf`, UPSTREAM_URL);
    git("config", `url.file://${dir}-no-such-upstream.git.insteadOf`, GONE_URL);
    body({ dir, git, commit, run, runBare, fromUpstream, publish, upDir });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(upDir, { recursive: true, force: true });
  }
}
const seams = (config) => ({ [CONFIG]: JSON.stringify(config) });

// Pins main()'s wiring: the proof is compared with the list's commit (main), not the merge-base.
test("the command line takes the route tree as generated only while the proof is main's", () => {
  withRepo(({ git, commit, run }) => {
    const TREE = "apps/web/src/routeTree.gen.ts";
    const PROOF = "apps/web/src/forkRouteTree.test.ts";
    const ROUTE = "apps/web/src/routes/_chat.settings.fork.tsx";
    const tree = "const A = AImport.update({\n  getParentRoute: () => Root,\n} as any)\n";
    const forkTree = `${tree}const Fork = ForkImport.update({\n  getParentRoute: () => Root,\n} as any)\n`;
    const main = commit(
      { ...seams({ files: SEAM }), ...BASE, [TREE]: tree, [PROOF]: "test\n" },
      "main",
    );
    const route = { [TREE]: forkTree, [ROUTE]: "route\n" };
    git("checkout", "-q", "-b", "pr", main);
    const pr = commit(route, "a route");
    assert.equal(run("--config-ref", main, main, pr).status, 0);
    git("checkout", "-q", "-B", "weak", main);
    const weak = commit({ ...route, [PROOF]: "it.skip();\n" }, "a route and a weaker proof");
    const refused = run("--config-ref", main, main, weak);
    assert.equal(refused.status, 1);
    assert.match(
      refused.stderr,
      /routeTree\.gen\.ts: upstream file with 3 fork lines.*is not main's/,
    );
    // Main tightened the proof after the branch: judged line by line until updated from main.
    git("checkout", "-q", "-B", "main2", main);
    const main2 = commit({ [PROOF]: "stricter test\n" }, "main: a stricter proof");
    assert.match(run("--config-ref", main2, main, pr).stderr, /is not main's/);
  });
});

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
    // A well-formed list that widens "pending" or "forkFiles" is counted from upstream too; main
    // names no upstream here, so it is refused as uncountable, never passed or blamed on the list.
    git("checkout", "-q", "-f", "-B", "pr", main);
    const wide = { files: SEAM, pending: { "z.ts": 9 }, forkFiles: ["tools/"] };
    const wider = run(main, commit(seams(wide), "a wider list"));
    assert.equal(wider.status, 1, wider.stdout);
    assert.match(wider.stderr, /^Could not count this change's list edits from upstream/m);
    assert.match(wider.stderr, /changes the "pending" ceiling of z\.ts, and changes "forkFiles"/);
    assert.doesNotMatch(wider.stderr, /Fix the list in this change/);
    // A seam line it lists more often (or less) is counted from upstream; main names no
    // upstream here, so that is refused rather than passed unchecked.
    const raised = commit(
      seams({ files: { "a.ts": { why: "mine", added: ["anything: 1,"] } } }),
      "a raised count",
    );
    const counted = run(main, raised);
    assert.equal(counted.status, 1, counted.stdout);
    assert.match(
      counted.stderr,
      /- a\.ts: this change edits their seam lines in its .*, but there is no \.github\/fork-upstream\.json/,
    );
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
        ...SEAMED,
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
    const series = commit({ ...seams({ files: SEAM }), ...SEAMED }, "fork series");
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

// A file upstream names like a fork file stopped every fork-follow after the rebase until a pull
// request excluded it. Right after a rebase that writes a lowered list, the list excludes it.
test("after a rebase, the lowered list excludes an upstream file a pull request's run takes for a fork file", () => {
  withRepo(({ dir, commit, fromUpstream }) => {
    const upstream = commit(
      { "src/exact.ts": UP, "src/forkJoin.ts": UP, "src/usage/Up.ts": UP, ...BASE },
      "upstream",
    );
    // Old state: a list with no "forkFiles" key, and one whose entries take upstream's files.
    const forkFiles = ["src/usage/", "src/exact.ts", "src/fork-own.ts"];
    const series = commit(
      { ...seams({ files: SEAM, forkFiles }), ...SEAMED, "src/fork-own.ts": UP },
      "fork series",
    );
    const out = path.join(dir, "..", `${path.basename(dir)}-lowered.json`);
    try {
      // Before the rebase, or after it with no list to write, it is still refused: nothing would
      // commit the exclusion, and main's pull request runs would keep the hole.
      for (const args of [[], ["--after-rebase"]]) {
        const refused = fromUpstream(...args, upstream, series);
        assert.equal(refused.status, 1, args.join(" "));
        assert.match(refused.stderr, /- src\/forkJoin\.ts: upstream has it/);
      }
      const result = fromUpstream("--after-rebase", "--lowered-list", out, upstream, series);
      assert.equal(result.status, 0, result.stderr);
      const why = "(upstream has it; a pull request's run took it for a fork file)";
      assert.deepEqual(result.stdout.match(/^Lowered: .*$/gm), [
        `Lowered: src/exact.ts: "forkFiles" entry removed ${why}`,
        `Lowered: src/forkJoin.ts: "forkFiles" "!src/forkJoin.ts" added ${why}`,
        `Lowered: src/usage/Up.ts: "forkFiles" "!src/usage/Up.ts" added ${why}`,
      ]);
      const lowered = parseConfig(fs.readFileSync(out, "utf8"));
      assert.deepEqual(lowered.forkFiles, [
        "src/usage/",
        "src/fork-own.ts",
        "!src/forkJoin.ts",
        "!src/usage/Up.ts",
      ]);
      // Main's pull request runs judge upstream's three as upstream's; the fork's own stay its own.
      const isForkFile = forkFileTest(lowered.forkFiles);
      for (const file of ["src/exact.ts", "src/forkJoin.ts", "src/usage/Up.ts"]) {
        assert.equal(isForkFile(file), false, file);
      }
      assert.equal(isForkFile("src/fork-own.ts"), true);
      assert.equal(isForkFile("src/usage/Own.ts"), true);
      // Runs again with the lowered list committed: nothing more to lower or refuse.
      fs.rmSync(out);
      const committed = commit({ [CONFIG]: JSON.stringify(lowered) }, "lowered");
      for (const args of [["--after-rebase", "--lowered-list", out], ["--after-rebase"], []]) {
        const again = fromUpstream(...args, upstream, committed);
        assert.equal(again.status, 0, `${args.join(" ")}: ${again.stderr}`);
        assert.doesNotMatch(again.stdout, /Lowered/);
      }
      assert.equal(fs.existsSync(out), false);
      // A fork change in such a file is refused, never excluded away, and nothing is written.
      const changed = commit(
        { [CONFIG]: JSON.stringify({ files: SEAM, forkFiles }), "src/forkJoin.ts": `${UP}x();\n` },
        "fork line",
      );
      const stopped = fromUpstream("--after-rebase", "--lowered-list", out, upstream, changed);
      assert.equal(stopped.status, 1);
      assert.match(stopped.stderr, /- src\/forkJoin\.ts: upstream file with 1 fork line/);
      assert.equal(fs.existsSync(out), false);
    } finally {
      fs.rmSync(out, { force: true });
    }
  });
});

test("lowerList excludes a hidden upstream file in a list with no forkFiles, or replaces its own entry", () => {
  const none = `${JSON.stringify({ files: {} }, null, 2)}\n`;
  const hidden = (...files) => ({ pending: new Map(), lost: new Map(), hidden: new Set(files) });
  assert.deepEqual(JSON.parse(lowerList(none, hidden("src/forkJoin.ts")).text), {
    files: {},
    forkFiles: ["!src/forkJoin.ts"],
  });
  // Named like the fork's and listed: the entry alone would not exclude it, so a `!` replaces it.
  const listed = `${JSON.stringify({ files: {}, forkFiles: ["fork/up.ts"] }, null, 2)}\n`;
  const result = lowerList(listed, hidden("fork/up.ts"));
  assert.deepEqual(JSON.parse(result.text).forkFiles, ["!fork/up.ts"]);
  assert.match(result.changes[0], /^fork\/up\.ts: "forkFiles" entry replaced by "!fork\/up\.ts"/);
  assert.equal(lowerList(none, hidden()), undefined);
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
      { ...seams({ files: SEAM, forkFiles: ["src/forkstuff/"] }), ...SEAMED, "fork/own.ts": UP },
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
    const onPlain = commit({ ...seams({ files: SEAM }), ...SEAMED }, "series");
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
    const hook = { "a.ts": { why: "test", added: ["forkHook();"] } };
    const upstream = commit({ "a.ts": GUARD_BASE }, "upstream");
    const series = commit(
      {
        ...seams({ files: hook }),
        "a.ts": "import { x } from './x';\ndoSensitive();\nforkHook();\n",
      },
      "fork series drops the guard",
    );
    const result = fromUpstream(upstream, series);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /- a\.ts: 1 upstream line deleted, moved or re-indented/);
    const recorded = commit(
      seams({ files: hook, lost: { "a.ts": { lines: 1, gone: 1 } } }),
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
    // The pull request's run cannot tell upstream's lines from the fork's in a pending file, so it
    // refuses a shrink that leaves the ceiling as it is, saying to keep upstream's lines ...
    const own = run(main, pr);
    assert.equal(own.status, 1, own.stdout);
    assert.match(own.stderr, /- p\.ts: "pending" with a ceiling of 1, .*keep upstream's lines/);
    // ... and the run from upstream refuses it whatever the list says: the line's text is gone.
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
    assert.ok(
      ownListProblem(text).startsWith(`${OWN} is malformed: duplicate key ${JSON.stringify(key)} `),
      text,
    );
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
        assert.match(reason, /after the rebase .* rebase by hand .* never raises one/, reason);
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
  withRepo(({ git, commit, run, fromUpstream, publish }) => {
    const upstream = commit({ ...BASE, "n.ts": UP }, "upstream");
    publish(upstream);
    const day = "2026-11-18T12:00:00Z";
    const main = commit({ ...seams({ files: SEAM }), ...SEAMED }, "fork main", day);
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
    const main = commit({ ...seams(ANNOUNCED()), ...SEAMED }, "step 1", day);
    assert.equal(fromUpstream(upstream, main).status, 0);
    // Upstream's next nightly adds the same line; the series is rebased onto it.
    git("checkout", "-q", "-b", "next", upstream);
    const next = commit({ "n.ts": `${UP}  forkNew();\n` }, "upstream adds the line", day);
    const rebased = commit({ ...seams(ANNOUNCED()), ...SEAMED }, "step 1, rebased", day);
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
    const rebasedDropped = commit(
      { ...seams({ files: SEAM }), ...SEAMED },
      "drop it, rebased",
      day,
    );
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
  withRepo(({ git, commit, run, publish }) => {
    publish(commit({ ...BASE, "n.ts": UP }, "upstream", "2026-11-18T12:00:00Z"));
    const step1 = commit(seams(ANNOUNCED()), "main", "2026-11-18T12:00:00Z");
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
    assert.doesNotMatch(doubled.stderr, /adds 2×/);
    // Its own list lists one copy where the fork adds two: counted from upstream, refused too.
    assert.match(
      doubled.stderr,
      /- n\.ts: listed in .*, whose "added" lists `forkNew\(\);`, but the fork adds it 2× over upstream/,
    );
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
  withRepo(({ git, commit, run, publish }) => {
    const day = "2026-11-18T12:00:00Z";
    publish(commit({ ...BASE, "n.ts": UP }, "upstream", day));
    const upstream = commit(seams({ files: SEAM }), "main", day);
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
    const reworded = { ...ANNOUNCED("2020-01-01").files, "a.ts": { ...SEAM["a.ts"], why: "new" } };
    const other = commit(seams({ files: reworded }), "a later list change");
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

// A file named after an Object property read `lost[file]` from Object's prototype: its counts were
// undefined, nothing compares above undefined, and every upstream line it lost passed.
test("from upstream, a file named like an Object property is held to a lost count of 0", () => {
  for (const file of ["constructor", "toString", "__proto__"]) {
    const config = { pending: { [file]: 1 } };
    const { reasons } = lostIn(
      config,
      { [file]: GUARD_BASE },
      { [file]: "import { x } from './x';\ndoSensitive();\nforkA();\n" },
      true,
    );
    assert.equal(reasons.length, 2, `${file}: ${JSON.stringify(reasons)}`);
    assert.match(reasons[0], /more than its "lost" lines count of 0/);
  }
});

// A pull request that moved fork lines out of a "pending" file left main's ceiling above its count,
// and the next run from upstream refused main for it.
const shrink = (pending, base, head, ownPending, ownFiles) =>
  findUnlistedChanges(
    { files: {}, pending, forkFiles: [] },
    {
      changed: Object.keys(head),
      atBase: (file) => base[file],
      atHead: (file) => head[file],
      upstream: false,
      isForkFile: () => false,
      ownPending,
      ownFiles,
    },
  );
test("from main, a change that shrinks a pending file lowers its ceiling in its own list", () => {
  const base = { "p.ts": `${UP}forkA();\nforkB();\nforkC();\n` };
  const head = { "p.ts": `${UP}forkA();\n` };
  const { ceilings } = shrink({ "p.ts": 3 }, base, head, { "p.ts": 3 });
  assert.deepEqual(ceilings.length, 1);
  assert.match(
    ceilings[0],
    /^p\.ts: "pending" with a ceiling of 3, and this change removes 2 more lines than it adds .*: set it to 1 in this change's \.github\/fork-seams\.json \(its list has 3; main holds its ceiling exactly\)\.$/,
  );
  assert.deepEqual(shrink({ "p.ts": 3 }, base, head, { "p.ts": 1 }), {
    reasons: [],
    ceilings: [],
    rises: [],
    notes: [],
  });
  // Still 2 is still too high; with no fork line left, the entry goes.
  assert.match(shrink({ "p.ts": 3 }, base, head, { "p.ts": 2 }).ceilings[0], /set it to 1 .*has 2/);
  const none = { "p.ts": UP };
  assert.match(
    shrink({ "p.ts": 3 }, base, none, { "p.ts": 1 }).ceilings[0],
    /removes 3 more lines .*: remove it from "pending" in this change's .* \(its list has 1\)\.$/,
  );
  assert.deepEqual(shrink({ "p.ts": 3 }, base, none, {}).ceilings, []);
  // Removing more lines than the ceiling (upstream's too) still only asks for the entry to go.
  const fewer = { "p.ts": "forkA();\n" };
  assert.deepEqual(
    shrink({ "p.ts": 1 }, { "p.ts": `${UP}forkA();\n` }, { "p.ts": "" }, {}).ceilings,
    [],
  );
  assert.deepEqual(shrink({ "p.ts": 3 }, base, fewer, {}).ceilings, []);
  // Exact, not only at most. A ceiling set below the lines left, or an entry dropped while
  // lines remain, passes here but the next run from upstream refuses main.
  assert.deepEqual(shrink({ "p.ts": 3 }, base, head, { "p.ts": 1, "q.ts": 1 }).ceilings, []);
  const twoLeft = { "p.ts": `${UP}forkA();\nforkB();\n` };
  assert.match(
    shrink({ "p.ts": 3 }, base, twoLeft, { "p.ts": 1 }).ceilings[0],
    /removes 1 more line than it adds .*: set it to 2 in this change's .* \(its list has 1; /,
  );
  assert.match(
    shrink({ "p.ts": 3 }, base, twoLeft, {}).ceilings[0],
    /: set it to 2 in this change's .* \(its list drops it; /,
  );
  // A change that swaps one line for another keeps the ceiling; one that grows is refused
  // elsewhere; with no own list read, nothing is held.
  const swap = { "p.ts": `${UP}forkA();\nforkB();\nforkD();\n` };
  assert.deepEqual(shrink({ "p.ts": 3 }, base, swap, { "p.ts": 3 }).ceilings, []);
  assert.match(
    shrink({ "p.ts": 3 }, base, swap, { "p.ts": 2 }).ceilings[0],
    /this change changes its lines: set it to 3 .*\(its list has 2; /,
  );
  assert.deepEqual(shrink({ "p.ts": 3 }, base, head, undefined).ceilings, []);
  // A list-only change (the file's text unchanged) is not held: it may correct main's ceiling.
  assert.deepEqual(shrink({ "p.ts": 3 }, base, base, { "p.ts": 2 }).ceilings, []);
});

// Moving a pending file to "files" with "added": [] (which allows no fork line) is the same as
// dropping the entry, so only a file with no fork line left may move that way.
test('from main, a pending file moves to "files" with "added": [] only once no fork line is left', () => {
  const base = { "p.ts": `${UP}forkA();\nforkB();\nforkC();\n` };
  const twoLeft = { "p.ts": `${UP}forkA();\nforkB();\n` };
  const moved = { "p.ts": { why: "w", added: [] } };
  assert.deepEqual(shrink({ "p.ts": 3 }, base, twoLeft, {}, moved).ceilings, [
    `p.ts: "pending" with a ceiling of 3, and this change removes 1 more line than it adds (keep upstream's lines; if they are fork lines moved out), and moves it to "files" in this change's .github/fork-seams.json while 2 fork lines are left: with fork lines left, only a list-only change (the file's text unchanged) may move it; change the file in one pull request (keeping it in "pending"), and move it in another.`,
  ]);
  // A list-only move holds main's ceiling, so it is refused too (it moves the file, and changes no line).
  assert.deepEqual(shrink({ "p.ts": 3 }, base, base, {}, moved).ceilings, [
    `p.ts: "pending" with a ceiling of 3, and this change moves it to "files" in this change's .github/fork-seams.json while 3 fork lines are left: keep it in "pending" with a ceiling of 3, or list exactly those 3 lines under "added" in its new entry (it lists 0; a move only reclassifies the lines left, and a new one is announced first).`,
  ]);
  // None left: the move is the fix (kept in "pending" it must go).
  assert.deepEqual(shrink({ "p.ts": 3 }, base, { "p.ts": UP }, {}, moved).ceilings, []);
  // With none left the entry lists none: an upstream line listed there would be a spare seam
  // allowance nobody reviewed as a fork line (both runs would pass it).
  const spare = { "p.ts": { why: "w", added: [UP.trim()] } };
  assert.deepEqual(shrink({ "p.ts": 3 }, base, { "p.ts": UP }, {}, spare).ceilings, [
    `p.ts: "pending" with a ceiling of 3, and this change removes 3 more lines than it adds (keep upstream's lines; if they are fork lines moved out), and moves it to "files" in this change's .github/fork-seams.json with no fork line left: list no line under "added" in its new entry (it lists 1; a line upstream has is never a seam line, and a new one is announced first).`,
  ]);
  // Listed in both (a malformed list is refused elsewhere): judged as "pending".
  assert.deepEqual(shrink({ "p.ts": 3 }, base, twoLeft, { "p.ts": 2 }, moved).ceilings, []);
});

// The pending lines a run from upstream counts, as fork-follow's check before the rebase does.
const fromUpstream = (list, upstream, head) => {
  const options = {
    atBase: (file) => upstream[file],
    atHead: (file) => head[file],
    upstream: true,
  };
  const unlisted = findUnlistedChanges(list, {
    ...options,
    changed: Object.keys(head),
    isForkFile: () => false,
  });
  const lost = findLostLines(list, options);
  return [
    ...findSeamBreaches(list.files, options),
    ...unlisted.reasons,
    ...unlisted.ceilings,
    ...lost.reasons,
    ...lost.ceilings,
  ];
};
// The pull request's run: main's list judges, the change's own list only refuses.
const fromMain = (list, own, main, head) => {
  const unlisted = findUnlistedChanges(list, {
    changed: Object.keys(head),
    atBase: (file) => main[file],
    atHead: (file) => head[file],
    upstream: false,
    isForkFile: () => false,
    ownPending: own.pending ?? {},
    ownFiles: own.files,
    ownLost: own.lost,
  });
  return [...unlisted.reasons, ...unlisted.ceilings];
};

// A pending file's fork lines change by the lines gained less the change in "gone",
// so a change that puts an upstream line back has one fork line fewer than its line count says.
test("from main, a change that puts an upstream line back counts it in the exact ceiling", () => {
  const upstream = { "p.ts": "a();\nb(x);\nc();\n" };
  const main = { "p.ts": "a();\nb(x, fork);\nforkG();\nforkH();\nc();\n" };
  const head = { "p.ts": "a();\nb(x);\nforkH();\nc();\n" };
  const mainList = { files: {}, pending: { "p.ts": 3 }, lost: { "p.ts": { lines: 1, gone: 1 } } };
  assert.deepEqual(fromUpstream(mainList, upstream, main), []);
  const own = (ceiling) => ({ files: {}, pending: { "p.ts": ceiling }, lost: {} });
  // Revert b(x, fork) and move forkG() out: 1 fork line is left.
  assert.deepEqual(fromUpstream(own(1), upstream, head), []);
  assert.deepEqual(fromMain(mainList, own(1), main, head), []);
  assert.deepEqual(fromMain(mainList, own(2), main, head), [
    `p.ts: "pending" with a ceiling of 3, and this change removes 1 more line than it adds (keep upstream's lines; if they are fork lines moved out), and sets its own "lost" to name 0 upstream lines gone where main's names 1 (an upstream line put back is a fork line fewer): set it to 1 in this change's .github/fork-seams.json (its list has 2; main holds its ceiling exactly).`,
  ]);
  assert.match(fromUpstream(own(2), upstream, head)[0], /fewer than its ceiling of 2/);
  // A pure revert (as many lines as before) is held too.
  const revert = { "p.ts": "a();\nb(x);\nforkG();\nforkH();\nc();\n" };
  assert.deepEqual(fromMain(mainList, own(2), main, revert), []);
  assert.match(fromMain(mainList, own(3), main, revert)[0], /set it to 2 .*its list has 3/);
});

// A pending file may become a declared seam while fork lines are left: its new entry lists every
// line left, and the move only reclassifies them (the change adds no fork line).
const promoted = (...added) => ({ "p.ts": { why: "w", added } });
const NONE = { reasons: [], ceilings: [], rises: [], notes: [] };
test('from main, a pending file moves to "files" with fork lines left when its entry lists each one', () => {
  const base = { "p.ts": `${UP}forkA();\nforkB();\nforkC();\n` };
  // List-only: main's ceiling of 3 is left, in any order.
  assert.deepEqual(
    shrink({ "p.ts": 3 }, base, base, {}, promoted("forkC();", "forkA();", "forkB();")),
    NONE,
  );
  // Copies count: a line the file has twice is listed twice.
  const twice = { "p.ts": `${UP}}\nforkA();\n}\n` };
  assert.deepEqual(shrink({ "p.ts": 3 }, twice, twice, {}, promoted("}", "}", "forkA();")), NONE);
  // An "announced" line (one a later change adds) is not one of the lines left: it is not counted.
  const announced = {
    "p.ts": {
      why: "w",
      added: ["forkA();", "forkB();", "forkC();"],
      announced: { since: "2026-11-19", added: ["forkD();"] },
    },
  };
  assert.deepEqual(shrink({ "p.ts": 3 }, base, base, {}, announced), NONE);
});

// With fork lines left, a move that also changes the file is refused: main's exact ceiling is then
// the count, and a "lost" entry left stale cannot hide an upstream line swapped for a fork line.
test('from main, a move to "files" with fork lines left is a list-only change', () => {
  const base = { "p.ts": `${UP}forkA();\nforkB();\nforkC();\n` };
  const refused =
    /and moves it to "files" in this change's \.github\/fork-seams\.json while (\d) fork lines are left: with fork lines left, only a list-only change \(the file's text unchanged\) may move it; /;
  // A line moved out (2 left), and a swap (one line out, one in), each listing what is left.
  const twoLeft = { "p.ts": `${UP}forkA();\nforkB();\n` };
  assert.match(
    shrink({ "p.ts": 3 }, base, twoLeft, {}, promoted("forkB();", "forkA();")).ceilings.join("\n"),
    refused,
  );
  const swap = { "p.ts": `${UP}forkA();\nforkB();\nforkD();\n` };
  assert.match(
    shrink(
      { "p.ts": 3 },
      base,
      swap,
      {},
      promoted("forkA();", "forkB();", "forkD();"),
    ).ceilings.join("\n"),
    refused,
  );
  // An upstream line swapped for a fork line, "lost" left as main's: the line count is level, so
  // the count looks the same, but main would hold one fork line more than its ceiling.
  const upstream = { "p.ts": "a();\nb();\nc();\n" };
  const main = { "p.ts": "a();\nb();\nc();\nforkA();\n" };
  const swapped = { "p.ts": "a();\nforkB();\nc();\nforkA();\n" };
  const mainList = { files: {}, pending: { "p.ts": 1 }, lost: {} };
  assert.deepEqual(fromUpstream(mainList, upstream, main), []);
  assert.deepEqual(fromMain(mainList, { files: promoted("forkA();"), lost: {} }, main, swapped), [
    `p.ts: "pending" with a ceiling of 1, and this change changes its lines, and moves it to "files" in this change's .github/fork-seams.json while 1 fork line is left: with fork lines left, only a list-only change (the file's text unchanged) may move it; change the file in one pull request (keeping it in "pending"), and move it in another.`,
  ]);
  // The same file left as it is moves.
  assert.deepEqual(fromMain(mainList, { files: promoted("forkA();"), lost: {} }, main, main), []);
});

test('from main, a move to "files" that leaves a fork line out of its entry is refused', () => {
  const base = { "p.ts": `${UP}forkA();\nforkB();\nforkC();\n` };
  assert.deepEqual(
    shrink({ "p.ts": 3 }, base, base, {}, promoted("forkA();", "forkB();")).ceilings,
    [
      `p.ts: "pending" with a ceiling of 3, and this change moves it to "files" in this change's .github/fork-seams.json while 3 fork lines are left: keep it in "pending" with a ceiling of 3, or list exactly those 3 lines under "added" in its new entry (it lists 2; a move only reclassifies the lines left, and a new one is announced first).`,
    ],
  );
  // A line the file does not have cannot stand in for one it has.
  assert.deepEqual(
    shrink({ "p.ts": 3 }, base, base, {}, promoted("forkA();", "forkB();", "forkZ();")).ceilings,
    [
      'p.ts: moved to "files" in this change\'s .github/fork-seams.json, whose "added" lists `forkZ();`, but the file has no copy: list only the fork lines the file has.',
    ],
  );
  assert.match(
    shrink({ "p.ts": 3 }, base, base, {}, promoted("forkA();", "forkA();", "forkB();")).ceilings[0],
    /lists `forkA\(\);` 2×, but the file has 1×/,
  );
  // More lines listed than are left: an extra one is announced first, not listed in the move.
  assert.match(
    shrink({ "p.ts": 3 }, base, base, {}, promoted("forkA();", "forkB();", "forkC();", UP.trim()))
      .ceilings[0],
    /while 3 fork lines are left: .*\(it lists 4; /,
  );
  // A fork line the file has, listed under "announced" instead of "added": it is not counted, and
  // an announced line is one a later change adds, so not one the file has.
  const announced = {
    "p.ts": {
      why: "w",
      added: ["forkA();", "forkB();"],
      announced: { since: "2026-11-19", added: ["forkC();"] },
    },
  };
  assert.deepEqual(shrink({ "p.ts": 3 }, base, base, {}, announced).ceilings, [
    `p.ts: "pending" with a ceiling of 3, and this change moves it to "files" in this change's .github/fork-seams.json while 3 fork lines are left: keep it in "pending" with a ceiling of 3, or list exactly those 3 lines under "added" in its new entry (it lists 2; a move only reclassifies the lines left, and a new one is announced first).`,
    'p.ts: moved to "files" in this change\'s .github/fork-seams.json, whose "announced" lists `forkC();`, but the file already has it (1× there): list a fork line the file has under "added", and announce only a line a later change adds (a line upstream has is never a seam line).',
  ]);
  // Every fork line listed, and an announced line the file already has (upstream's, here): after
  // the merge main's tree fails "no announced line is in its file yet" for every pull request.
  const landed = {
    "p.ts": {
      why: "w",
      added: ["forkA();", "forkB();", "forkC();"],
      announced: { since: "2026-11-19", added: [UP.trim()] },
    },
  };
  assert.deepEqual(shrink({ "p.ts": 3 }, base, base, {}, landed).ceilings, [
    `p.ts: moved to "files" in this change's .github/fork-seams.json, whose "announced" lists \`${UP.trim()}\`, but the file already has it (1× there): list a fork line the file has under "added", and announce only a line a later change adds (a line upstream has is never a seam line).`,
  ]);
});

test('from main, a move to "files" that also adds fork lines is refused', () => {
  const base = { "p.ts": `${UP}forkA();\nforkB();\n` };
  const grown = { "p.ts": `${UP}forkA();\nforkB();\nforkC();\n` };
  const result = shrink(
    { "p.ts": 2 },
    base,
    grown,
    {},
    promoted("forkA();", "forkB();", "forkC();"),
  );
  assert.deepEqual(result.ceilings, []);
  assert.deepEqual(result.reasons, [
    'p.ts: "pending" (fork lines to move out), and this change adds 1 more line than it removes.',
  ]);
  // An upstream line swapped for a fork line adds no line but one fork line (its "lost" says so).
  const upstream = { "p.ts": "a();\nb();\n" };
  const main = { "p.ts": "a();\nb();\nforkA();\n" };
  const swapped = { "p.ts": "a();\nforkB();\nforkA();\n" };
  const mainList = { files: {}, pending: { "p.ts": 1 }, lost: {} };
  assert.deepEqual(fromUpstream(mainList, upstream, main), []);
  const own = {
    files: promoted("forkA();", "forkB();"),
    lost: { "p.ts": { lines: 1, gone: 1 } },
  };
  const raised = `p.ts: "pending" with a ceiling of 1, and this change sets its own "lost" to name 1 upstream line gone where main's names 0 (an upstream line put back is a fork line fewer), so 2 fork lines are left, more than its ceiling: a pending file may not gain a fork line (an upstream line swapped for one is one more), whether it stays in "pending" or moves to "files"; make the change without it.`;
  assert.deepEqual(fromMain(mainList, own, main, swapped), [raised]);
  // Kept in "pending", its own list may not raise the ceiling either (the run from upstream on the
  // merged head would pass it: the list and the file agree).
  const stays = { files: {}, pending: { "p.ts": 2 }, lost: { "p.ts": { lines: 1, gone: 1 } } };
  assert.deepEqual(fromUpstream({ forkFiles: [], ...stays }, upstream, swapped), []);
  assert.deepEqual(fromMain(mainList, stays, main, swapped), [raised]);
});

// Whatever a pull request's run accepts for a pending file, fork-follow's check before the
// rebase accepts too, when the change's "lost" entry is the one that run holds (a wrong one is
// refused there whatever the ceiling); and the right ceiling always passes the pull request.
test("property: a pending ceiling the pull request accepts passes the run from upstream", () => {
  let seed = 510;
  const random = (n) => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return Math.floor((seed / 2147483648) * n);
  };
  const UPSTREAM_LINES = ["a();", "b();", "c();", "}"];
  const FORK_LINES = ["f();", "g();", "a(fork);", "}"];
  const pick = (lines) => lines[random(lines.length)];
  // Delete, replace with a fork or upstream line, insert one, or put an upstream line back.
  const edit = (lines, up) => {
    const out = [...lines];
    for (let step = random(3) + 1; step > 0; step--) {
      const at = random(out.length + 1);
      switch (random(4)) {
        case 0:
          if (out.length > 0) out.splice(Math.min(at, out.length - 1), 1);
          break;
        case 1:
          if (out.length > 0) out[Math.min(at, out.length - 1)] = pick(FORK_LINES);
          break;
        case 2:
          out.splice(at, 0, pick(random(2) === 0 ? FORK_LINES : UPSTREAM_LINES));
          break;
        default:
          out.splice(at, 0, pick(up));
      }
    }
    return out;
  };
  const text = (lines) => lines.map((line) => `${line}\n`).join("");
  const split = (file) => file.split("\n").filter(Boolean);
  // Copies `head` has over `base`, per line text, summed.
  const over = (base, head) => {
    const counts = lineCounts(base);
    let sum = 0;
    for (const [line, count] of lineCounts(head))
      sum += Math.max(0, count - (counts.get(line) ?? 0));
    return sum;
  };
  // Each copy `head` has over `base`, per line text: the fork lines a seam entry lists.
  const overLines = (base, head) => {
    const counts = lineCounts(base);
    return [...lineCounts(head)].flatMap(([line, count]) =>
      Array.from({ length: Math.max(0, count - (counts.get(line) ?? 0)) }, () => line),
    );
  };
  // The list a run from upstream holds exact for this head (the lines it counts).
  const exact = (upstream, head) => {
    const added = over(upstream["p.ts"], head["p.ts"]);
    const lines = lostLines(split(upstream["p.ts"]), split(head["p.ts"]));
    const gone = over(head["p.ts"], upstream["p.ts"]);
    const forkLines = overLines(upstream["p.ts"], head["p.ts"]);
    return { added, forkLines, lost: lines > 0 ? { "p.ts": { lines, gone } } : {} };
  };
  let checked = 0;
  let promotions = 0;
  for (let round = 0; round < 3000; round++) {
    const up = Array.from({ length: random(4) + 2 }, () => pick(UPSTREAM_LINES));
    const upstream = { "p.ts": text(up) };
    const main = { "p.ts": text(edit(up, up)) };
    const atMain = exact(upstream, main);
    if (atMain.added === 0) continue; // Not a pending file.
    const mainList = { files: {}, pending: { "p.ts": atMain.added }, lost: atMain.lost };
    assert.deepEqual(fromUpstream(mainList, upstream, main), []);
    const head = { "p.ts": text(edit(split(main["p.ts"]), up)) };
    if (head["p.ts"] === main["p.ts"]) continue;
    const atHead = exact(upstream, head);
    const lists = [
      { files: {}, lost: atHead.lost }, // dropped
      { files: { "p.ts": { why: "w", added: [] } }, lost: atHead.lost }, // moved
      // Moved as a seam that lists the fork lines left (refused: the change edits the file).
      { files: { "p.ts": { why: "w", added: atHead.forkLines } }, lost: atHead.lost },
      ...Array.from({ length: atMain.added + 3 }, (_, n) => ({
        files: {},
        pending: { "p.ts": n + 1 },
        lost: atHead.lost,
      })),
    ];
    for (const own of lists) {
      try {
        parseConfig(JSON.stringify(own));
      } catch {
        continue; // Refused as a malformed list.
      }
      if (fromMain(mainList, own, main, head).length > 0) continue;
      checked++;
      const config = { forkFiles: [], ...own, pending: own.pending ?? {} };
      assert.deepEqual(
        fromUpstream(config, upstream, head),
        [],
        JSON.stringify({ main, head, own }),
      );
    }
    // The right list passes the pull request (unless it grows or gains a fork line, which is
    // refused whatever its list says).
    const right =
      atHead.added > 0
        ? { files: {}, pending: { "p.ts": atHead.added }, lost: atHead.lost }
        : {
            files: Object.keys(atHead.lost).length > 0 ? { "p.ts": { why: "w", added: [] } } : {},
            lost: atHead.lost,
          };
    const grows = over(main["p.ts"], head["p.ts"]) - over(head["p.ts"], main["p.ts"]);
    const verdict = fromMain(mainList, right, main, head);
    if (grows <= 0 && atHead.added <= atMain.added)
      assert.deepEqual(verdict, [], JSON.stringify({ main, head, right }));
    else assert.notDeepEqual(verdict, [], JSON.stringify({ main, head, right }));
    // A list-only move to a seam that lists the lines left passes the pull request and the run
    // from upstream (which lines, only upstream can tell: the pull request's run holds their
    // number and that the file has them); with lines left, a move that edits the file never does.
    const promote = (at) => ({
      files: { "p.ts": { why: "w", added: at.forkLines } },
      lost: at.lost,
    });
    promotions++;
    assert.deepEqual(fromMain(mainList, promote(atMain), main, main), [], JSON.stringify({ main }));
    assert.deepEqual(
      fromUpstream({ forkFiles: [], pending: {}, ...promote(atMain) }, upstream, main),
      [],
      JSON.stringify({ main }),
    );
    if (atHead.added > 0)
      assert.notDeepEqual(
        fromMain(mainList, promote(atHead), main, head),
        [],
        JSON.stringify({ main, head }),
      );
  }
  assert.ok(checked > 500, `only ${checked} accepted lists checked`);
  assert.ok(promotions > 100, `only ${promotions} moves to a seam checked`);
});

test("the command line refuses a pull request that shrinks a pending file but keeps its ceiling", () => {
  withRepo(({ git, commit, run, publish }) => {
    // The lowered ceiling is counted from upstream, so upstream is published.
    publish(commit({ "p.ts": UP }, "upstream"));
    const config = { files: {}, pending: { "p.ts": 2 } };
    const main = commit({ ...seams(config), "p.ts": `${UP}forkA();\nforkB();\n` }, "main");
    git("checkout", "-q", "-b", "pr", main);
    const pr = commit({ "p.ts": `${UP}forkA();\n` }, "move forkB out");
    const kept = run(main, pr);
    assert.equal(kept.status, 1, kept.stdout);
    assert.match(
      kept.stderr,
      /- p\.ts: "pending" with a ceiling of 2, .* set it to 1 in this change's/,
    );
    assert.match(
      kept.stderr,
      /Lower each "pending" ceiling .* in this change's list where the line says so/,
    );
    const lowered = commit(seams({ files: {}, pending: { "p.ts": 1 } }), "lower it");
    const result = run(main, lowered);
    assert.equal(result.status, 0, result.stderr);
  });
});

// As fork-checks runs it (main's list through --config-ref), then as fork-follow's next run on main
// does (from the upstream commit main sits on): a pending file made a seam passes both.
test("the command line lets a pull request make a pending file a seam, and fork-follow then passes main", () => {
  withRepo(({ dir, git, commit, run, fromUpstream, publish }) => {
    const up = "export const x = 1;\nif (a) {\n}\n";
    const upstream = commit({ "p.ts": up }, "upstream");
    publish(upstream);
    const forked = `${up}forkA(); // Fork\nforkB(); // Fork\n`;
    const main = commit(
      { ...seams({ files: {}, pending: { "p.ts": 2 } }), "p.ts": forked },
      "main",
    );
    assert.equal(fromUpstream(upstream, main).status, 0);
    const branch = (name, files) => {
      git("checkout", "-q", "-b", name, main);
      return commit(files, name);
    };
    const asSeam = (...added) => seams({ files: { "p.ts": { why: "w", added } } });
    const pr = branch("promote", asSeam("forkA(); // Fork", "forkB(); // Fork"));
    const ok = run("--config-ref", main, main, pr);
    assert.equal(ok.status, 0, ok.stderr);
    // Merged, main's next fork-follow run passes and lowers nothing, before and after a rebase.
    assert.equal(fromUpstream(upstream, pr).status, 0);
    const lowered = path.join(dir, "lowered.json");
    const after = fromUpstream("--after-rebase", "--lowered-list", lowered, upstream, pr);
    assert.equal(after.status, 0, after.stderr);
    assert.equal(fs.existsSync(lowered), false);
    // A fork line left out of the entry is refused, here and from upstream.
    const short = branch("short", asSeam("forkA(); // Fork"));
    const refused = run("--config-ref", main, main, short);
    assert.equal(refused.status, 1);
    assert.match(
      refused.stderr,
      /- p\.ts: "pending" with a ceiling of 2, .* while 2 fork lines are left: .*\(it lists 1; /,
    );
    assert.match(fromUpstream(upstream, short).stderr, /p\.ts: adds `forkB\(\); \/\/ Fork`/);
    // A move that also adds a fork line is refused, even with the line listed.
    const grown = branch("grown", {
      ...asSeam("forkA(); // Fork", "forkB(); // Fork", "forkC(); // Fork"),
      "p.ts": `${forked}forkC(); // Fork\n`,
    });
    const grows = run("--config-ref", main, main, grown);
    assert.equal(grows.status, 1);
    assert.match(
      grows.stderr,
      /- p\.ts: "pending" \(fork lines to move out\), and this change adds 1 more line than it removes\./,
    );
    // Which lines are the fork lines only upstream can tell: an upstream line listed in place of
    // one is refused in the pull request's run (counted from upstream), and from upstream.
    const swapped = branch("swapped", asSeam("forkA(); // Fork", "}"));
    const counted = run("--config-ref", main, main, swapped);
    assert.equal(counted.status, 1, counted.stdout);
    assert.match(
      counted.stderr,
      /- p\.ts: moved to "files" .*, whose "added" lists `}`, but the fork adds no copy over upstream/,
    );
    const caught = fromUpstream(upstream, swapped);
    assert.equal(caught.status, 1);
    assert.match(caught.stderr, /p\.ts: adds `forkB\(\); \/\/ Fork`/);
  });
});

// Two branches from one merge-base could each add the one listed copy of a seam line: each run
// judged its own branch from the merge-base, and main ended with two copies.
test("from main, a listed copy the base branch already has since the merge-base is spent", () => {
  const main = { "a.ts": BASE["a.ts"].replace("  a: 1,", "  ...fork,\n  a: 1,") };
  const options = (tip) => ({
    atBase: (file) => BASE[file],
    atHead: (file) => main[file],
    upstream: false,
    atTip: (file) => tip[file],
  });
  assert.deepEqual(findSeamBreaches(SEAM, options(BASE)), []);
  assert.deepEqual(findSeamBreaches(SEAM, options(main)), [
    "a.ts: adds `...fork,` (the base branch has more copies than this branch's merge-base: update the branch from its base)",
  ]);
  // From upstream there is no base branch tip: each listed line is one copy over upstream's.
  assert.deepEqual(findSeamBreaches(SEAM, { ...options(main), upstream: true }), []);
  // Two copies are too many from the merge-base too, so updating would not help: no hint.
  const twice = { "a.ts": BASE["a.ts"].replace("  a: 1,", "  ...fork,\n  ...fork,\n  a: 1,") };
  assert.deepEqual(findSeamBreaches(SEAM, { ...options(main), atHead: (file) => twice[file] }), [
    "a.ts: adds 2× `...fork,`",
  ]);
  // An expired announcement allows no copy from the merge-base either: no hint.
  const fresh = {
    "a.ts": { why: "t", added: [], announced: { since: "2026-01-01", added: ["...fork,"] } },
  };
  const late = [];
  const reasons = findSeamBreaches(fresh, { ...options(main), today: "2026-11-19", late });
  assert.deepEqual([...reasons, ...late], ["a.ts: adds `...fork,`"]);
  // In date, the same copy is the base branch's newer one: the hint stays.
  assert.deepEqual(findSeamBreaches(fresh, { ...options(main), today: "2026-01-05" }), [
    "a.ts: adds `...fork,` (the base branch has more copies than this branch's merge-base: update the branch from its base)",
  ]);
});

test("the command line spends a seam copy another branch merged since the merge-base", () => {
  withRepo(({ git, commit, run }) => {
    const start = commit({ ...seams({ files: SEAM }), ...BASE }, "main");
    const withFork = { "a.ts": BASE["a.ts"].replace("  a: 1,", "  ...fork,\n  a: 1,") };
    git("checkout", "-q", "-b", "second", start);
    const second = commit(withFork, "second branch adds it too");
    git("checkout", "-q", "--detach", start);
    const first = commit(withFork, "first branch, merged");
    const result = run(first, second);
    assert.equal(result.status, 1, result.stdout);
    assert.match(result.stderr, /- a\.ts: adds `\.\.\.fork,` \(the base branch has more copies/);
    assert.equal(run(start, second).status, 0);
  });
});

// Right after a rebase, fork-follow commits the list with each ceiling and count it found too high
// lowered; it never raises one.
test("lowerList lowers and removes entries, keeps comments and order, and never raises", () => {
  const text = `${JSON.stringify(
    {
      "//": "comment",
      files: { "s.ts": { why: "w", added: ["x();"] } },
      pending: { "a.ts": 3, "b.ts": 2, "c.ts": 2, "e.ts": 4 },
      lost: {
        "c.ts": { lines: 2, gone: 1 },
        "e.ts": { lines: 3, gone: 1 },
        "s.ts": { lines: 1, gone: 0 },
      },
    },
    null,
    2,
  )}\n`;
  const lowered = {
    pending: new Map([
      ["a.ts", 1],
      ["b.ts", 0],
      ["c.ts", 0],
      ["e.ts", 5],
    ]),
    lost: new Map([
      ["c.ts", { lines: 1, gone: 1 }],
      ["e.ts", { lines: 4, gone: 0 }],
      ["s.ts", { lines: 0, gone: 0 }],
    ]),
  };
  const result = lowerList(text, lowered);
  const moved = JSON.parse(result.text).files["c.ts"];
  assert.match(moved.why, /^Was "pending": fork-follow moved it here/);
  assert.deepEqual(JSON.parse(result.text), {
    "//": "comment",
    // c.ts has no fork line left, but "lost" still names it (and a ceiling is 1 or more): it
    // moves to "files" with no seam line, which holds it tighter.
    files: { "s.ts": { why: "w", added: ["x();"] }, "c.ts": { why: moved.why, added: [] } },
    pending: { "a.ts": 1, "e.ts": 4 },
    // e.ts: only the count that fell is lowered.
    lost: { "c.ts": { lines: 1, gone: 1 }, "e.ts": { lines: 3, gone: 0 } },
  });
  assert.equal(result.text, `${JSON.stringify(JSON.parse(result.text), null, 2)}\n`);
  assert.deepEqual(result.changes, [
    'c.ts: "lost" 2/1 -> 1/1',
    'e.ts: "lost" 3/1 -> 3/0',
    's.ts: "lost" 1/0 removed (no upstream line lost)',
    'a.ts: "pending" 3 -> 1',
    'b.ts: "pending" 2 removed (no fork line left)',
    'c.ts: "pending" 2 moved to "files" with no seam line (no fork line left; "lost" still counts its upstream lines)',
  ]);
  assert.equal(lowerList(text, { pending: new Map([["e.ts", 9]]), lost: new Map() }), undefined);
  assert.equal(lowerList(text, { pending: new Map(), lost: new Map() }), undefined);
});

test("--lowered-list writes the lowered list after a rebase only, and only when it passes", () => {
  withRepo(({ dir, commit, fromUpstream }) => {
    const upstream = commit({ "p.ts": GUARD_BASE }, "upstream");
    const config = { files: {}, pending: { "p.ts": 3 }, lost: { "p.ts": { lines: 2, gone: 1 } } };
    const series = commit(
      { ...seams(config), "p.ts": "import { x } from './x';\ndoSensitive();\nforkA();\n" },
      "fork series",
    );
    const out = path.join(dir, "..", `${path.basename(dir)}-lowered.json`);
    try {
      const result = fromUpstream("--after-rebase", "--lowered-list", out, upstream, series);
      assert.equal(result.status, 0, result.stderr);
      assert.match(result.stdout, /^Lowered: p\.ts: "lost" 2\/1 -> 1\/1$/m);
      assert.match(result.stdout, /^Lowered: p\.ts: "pending" 3 -> 1$/m);
      assert.deepEqual(JSON.parse(fs.readFileSync(out, "utf8")), {
        files: {},
        pending: { "p.ts": 1 },
        lost: { "p.ts": { lines: 1, gone: 1 } },
      });
      // Committed, the next run has nothing to lower and writes nothing.
      fs.rmSync(out);
      const lowered = commit(
        seams({ files: {}, pending: { "p.ts": 1 }, lost: { "p.ts": { lines: 1, gone: 1 } } }),
        "lowered",
      );
      const again = fromUpstream("--after-rebase", "--lowered-list", out, upstream, lowered);
      assert.equal(again.status, 0, again.stderr);
      assert.equal(fs.existsSync(out), false);
      assert.doesNotMatch(again.stdout, /Lowered|Note/);
      // A refused run writes nothing.
      const rise = commit({ "p.ts": "doSensitive();\nforkA();\n" }, "loses another line");
      assert.equal(fromUpstream("--after-rebase", "--lowered-list", out, upstream, rise).status, 1);
      assert.equal(fs.existsSync(out), false);
      // Only after a rebase, from upstream, with a path.
      for (const args of [
        ["--lowered-list", out, upstream, series],
        ["--after-rebase", "--lowered-list", upstream, series],
        ["--after-rebase", upstream, series, "--lowered-list"],
      ]) {
        assert.equal(fromUpstream(...args).status, 2, args.join(" "));
      }
      assert.equal(fs.existsSync(out), false);
    } finally {
      fs.rmSync(out, { force: true });
    }
  });
});

// A listed "added" copy the fork no longer adds (a generic `};` or `settings = {` left from
// an older change) was room for a line nobody announced, and neither run noticed.
const spare = (files, base, head, options = {}) =>
  findSpareSeamLines(files, {
    atBase: (file) => base[file],
    atHead: (file) => head[file],
    upstream: true,
    ...options,
  });

test("from upstream, a listed seam copy the fork does not add is refused, naming the fix", () => {
  // Exact: each listed copy is one over upstream's.
  assert.deepEqual(spare(SEAM, BASE, SEAMED), { ceilings: [], notes: [] });
  // `...fork,` is listed but not added: refused, with the fix.
  const half = { "a.ts": SEAMED["a.ts"].replace("  ...fork,\n", "") };
  assert.deepEqual(spare(SEAM, BASE, half).ceilings, [
    'a.ts: "added" lists `...fork,`, but the fork adds no copy over upstream; remove it from "added" (a spare copy lets a later change add the line unannounced).',
  ]);
  // A line upstream also has (`};`) counts only the copies over upstream's.
  const generic = { "a.ts": { why: "t", added: ["};", "};"] } };
  const once = { "a.ts": `${BASE["a.ts"]}};\n` };
  assert.deepEqual(spare(generic, BASE, once).ceilings, [
    'a.ts: "added" lists `};` 2×, but the fork adds 1× over upstream; list it 1× (a spare copy lets a later change add the line unannounced).',
  ]);
  assert.deepEqual(spare(generic, BASE, BASE).ceilings.length, 1);
  // Upstream dropped its copy and the fork kept one: that copy is now the fork's (over upstream).
  const upstreamDropped = { "a.ts": BASE["a.ts"].replace("};\n", "}\n") };
  assert.deepEqual(
    spare({ "a.ts": { why: "t", added: ["};"] } }, upstreamDropped, BASE).ceilings,
    [],
  );
  // An announced line is not added yet: never spare. A missing file is refused elsewhere.
  const announced = {
    "a.ts": { ...SEAM["a.ts"], announced: { since: "2026-11-18", added: ["n();"] } },
  };
  assert.deepEqual(spare(announced, BASE, SEAMED).ceilings, []);
  assert.deepEqual(spare(SEAM, {}, SEAMED), { ceilings: [], notes: [] });
  assert.deepEqual(spare(SEAM, BASE, {}), { ceilings: [], notes: [] });
  // Right after a rebase (upstream took a fork line word for word) it is a note, and the copies
  // in use go to the lowered list.
  const lowered = { added: new Map() };
  const after = spare(SEAM, BASE, half, { afterRebase: true, lowered });
  assert.deepEqual(after.ceilings, []);
  assert.equal(after.notes.length, 1);
  assert.deepEqual(
    lowered.added.get("a.ts"),
    new Map([
      ["import { fork } from './fork';", 1],
      ["...fork,", 0],
    ]),
  );
});

test("from main, a change that drops a listed seam copy drops it from its own list, exactly", () => {
  const pr = (head, ownFiles) => spare(SEAM, SEAMED, head, { upstream: false, ownFiles }).ceilings;
  const half = { "a.ts": SEAMED["a.ts"].replace("  ...fork,\n", "") };
  const lowered = { "a.ts": { why: "test", added: ["import { fork } from './fork';"] } };
  // Kept in the own list (or no list edit at all): refused, naming the fix.
  assert.deepEqual(pr(half, SEAM), [
    `a.ts: this change drops \`...fork,\`, which main's list has under "added": remove it from "added" in this change's ${CONFIG} (its list has 1; a spare copy lets a later change add the line unannounced, too few fails the next run from upstream).`,
  ]);
  assert.deepEqual(pr(half, lowered), []);
  // Dropping the entry too is exact here: the dropped line has no copy left to list.
  assert.deepEqual(pr(half, {}), []);
  // Unchanged text (a list-only change may fix main), a change that adds lines, or an own list
  // that could not be read: not judged here.
  assert.deepEqual(pr(SEAMED, {}), []);
  assert.deepEqual(pr({ "a.ts": `${SEAMED["a.ts"]}more();\n` }, SEAM), []);
  assert.deepEqual(pr(half, undefined), []);
  // A generic line counts the copies dropped, whoever's they were: main lists `};` 2×, the
  // change drops one copy, so its list keeps exactly one (none fails the run from upstream).
  const generic = (count) => ({ "a.ts": { why: "t", added: Array(count).fill("};") } });
  const thrice = { "a.ts": `${BASE["a.ts"]}};\n};\n` };
  const twice = { "a.ts": `${BASE["a.ts"]}};\n` };
  const judged = (own) =>
    spare(generic(2), thrice, twice, { upstream: false, ownFiles: generic(own) }).ceilings;
  assert.deepEqual(judged(1), []);
  assert.match(
    judged(2)[0],
    /drops `};`, which main's list has 2× under "added": list it 1× under/,
  );
  assert.match(judged(0)[0], /list it 1× under "added" .*\(its list has none;/);
});

// Earlier surviving mutants: both `Math.max(0, …)` only showed in the wording ("-1×").
test("a fork that removes upstream copies of a listed line, or a change that drops more copies than listed, names the right fix", () => {
  const generic = (count) => ({ "a.ts": { why: "t", added: Array(count).fill("};") } });
  // From upstream: upstream has `};` twice, the fork keeps one: no copy over upstream.
  const twice = { "a.ts": `${BASE["a.ts"]}};\n` };
  assert.deepEqual(spare(generic(1), twice, BASE).ceilings, [
    'a.ts: "added" lists `};`, but the fork adds no copy over upstream; remove it from "added" (a spare copy lets a later change add the line unannounced).',
  ]);
  const lowered = { added: new Map() };
  spare(generic(1), twice, BASE, { afterRebase: true, lowered });
  assert.deepEqual(lowered.added.get("a.ts"), new Map([["};", 0]]));
  // From main: main lists `};` once, the change drops two copies (one upstream's).
  const thrice = { "a.ts": `${BASE["a.ts"]}};\n};\n` };
  const pr = (own) =>
    spare(generic(1), thrice, BASE, { upstream: false, ownFiles: generic(own) }).ceilings;
  assert.deepEqual(pr(0), []);
  assert.deepEqual(pr(1), [
    `a.ts: this change drops 2× \`};\`, which main's list has under "added": remove it from "added" in this change's ${CONFIG} (its list has 1; a spare copy lets a later change add the line unannounced, too few fails the next run from upstream).`,
  ]);
});

// Main's count comes from the list's commit, the drop from the branch; a branch
// whose merge-base lists another count is told to update, not only given a number.
test("from main, a drop judged by a list main changed since the merge-base says to update the branch", () => {
  const seam = (count) => ({ "a.ts": { why: "t", added: ["x();", ...Array(count).fill("};")] } });
  const base = { "a.ts": `${BASE["a.ts"]}x();\n};\n};\n` };
  const head = { "a.ts": `${BASE["a.ts"]}x();\n};\n` };
  const pr = (mainCount, baseCount, own = 0) =>
    spare(seam(mainCount), base, head, {
      upstream: false,
      ownFiles: seam(own),
      baseFiles: baseCount === undefined ? undefined : seam(baseCount),
    }).ceilings;
  // Main's list and the merge-base's agree: no hint.
  assert.equal(pr(2, 2).length, 1);
  assert.doesNotMatch(pr(2, 2)[0], /update the branch/);
  assert.doesNotMatch(pr(2, undefined)[0], /update the branch/);
  // Main dropped the same copy since (a duplicate fix): main asks for none, the branch's list
  // (from its merge-base) keeps one, so it is told to update the branch, not only given a number.
  assert.match(
    pr(1, 2, 1)[0],
    /drops `};`, which main's list has under "added": remove it from .* \(its list has 1; .*\) \(this branch's merge-base lists it 2×: update the branch from main, whose list changed since\)\.$/,
  );
  assert.deepEqual(pr(1, 2, 0), []);
});

test("lowerList drops spare seam copies, keeps the copies in use, and only lowers", () => {
  const text = `${JSON.stringify(
    {
      files: {
        "a.ts": { why: "w", added: ["x();", "};", "y();", "};"] },
        "b.ts": { why: "w", added: ["z();"] },
        // Indented items (as a YAML or nested line is listed): matched by their trimmed text.
        "c.ts": { why: "w", added: ["  ...x,", "\t...x,"] },
      },
    },
    null,
    2,
  )}\n`;
  const result = lowerList(text, {
    pending: new Map(),
    lost: new Map(),
    added: new Map([
      [
        "a.ts",
        new Map([
          ["x();", 1],
          ["};", 1],
          ["y();", 0],
        ]),
      ],
      ["b.ts", new Map([["z();", 1]])],
      ["c.ts", new Map([["...x,", 1]])],
      ["gone.ts", new Map([["q();", 0]])],
    ]),
  });
  assert.deepEqual(JSON.parse(result.text).files, {
    "a.ts": { why: "w", added: ["x();", "};"] },
    "b.ts": { why: "w", added: ["z();"] },
    "c.ts": { why: "w", added: ["  ...x,"] },
  });
  assert.deepEqual(result.changes, [
    'a.ts: "added" 4 -> 2 lines (spare: `y();`, `};`)',
    'c.ts: "added" 2 -> 1 lines (spare: `...x,`)',
  ]);
  // Nothing spare: nothing to write. A count above the list never adds a copy.
  const none = {
    pending: new Map(),
    lost: new Map(),
    added: new Map([["b.ts", new Map([["z();", 3]])]]),
  };
  assert.equal(lowerList(text, none), undefined);
});

test("the command line: a spare seam copy fails fork-follow's run, is lowered after a rebase, and a pull request drops its own", () => {
  withRepo(({ dir, git, commit, run, fromUpstream, publish }) => {
    const upstream = commit(BASE, "upstream");
    publish(upstream);
    const main = commit({ ...seams({ files: SEAM }), ...SEAMED }, "fork main");
    assert.equal(fromUpstream(upstream, main).status, 0);
    // A pull request moves `...fork,` out but keeps it listed: refused in its own run.
    git("checkout", "-q", "-b", "pr", main);
    const half = { "a.ts": SEAMED["a.ts"].replace("  ...fork,\n", "") };
    const kept = commit(half, "move it out");
    const refused = run(main, kept);
    assert.equal(refused.status, 1, refused.stdout);
    assert.match(refused.stderr, /- a\.ts: this change drops `\.\.\.fork,`, which main's list has/);
    assert.match(refused.stderr, /drop each spare "added" copy/);
    // Merged as it is, the next run from upstream refuses main.
    const nightly = fromUpstream(upstream, kept);
    assert.equal(nightly.status, 1, nightly.stdout);
    assert.match(nightly.stderr, /- a\.ts: "added" lists `\.\.\.fork,`, but the fork adds no copy/);
    assert.doesNotMatch(nightly.stderr, /a file of the fork's own/);
    // With its own list lowered it passes, and so does the run from upstream after the merge.
    const own = { files: { "a.ts": { why: "test", added: ["import { fork } from './fork';"] } } };
    const fixed = commit(seams(own), "drop it from the list");
    assert.equal(run(main, fixed).status, 0);
    assert.equal(fromUpstream(upstream, fixed).status, 0);
    // Right after a rebase fork-follow lowers it itself; the lowered list passes and lowers
    // nothing more (runs again: the same).
    const out = path.join(dir, "..", `${path.basename(dir)}-spare.json`);
    try {
      const after = fromUpstream("--after-rebase", "--lowered-list", out, upstream, kept);
      assert.equal(after.status, 0, after.stderr);
      assert.match(after.stdout, /^Note: a\.ts: "added" lists `\.\.\.fork,`/m);
      assert.match(
        after.stdout,
        /^Lowered: a\.ts: "added" 2 -> 1 lines \(spare: `\.\.\.fork,`\)$/m,
      );
      const loweredText = fs.readFileSync(out, "utf8");
      assert.deepEqual(parseConfig(loweredText).files, own.files);
      fs.rmSync(out);
      git("checkout", "-q", "-f", "-B", "rebased", kept);
      const committed = commit({ [CONFIG]: loweredText }, "fork-follow's lowering commit");
      for (const args of [["--after-rebase", "--lowered-list", out], []]) {
        const again = fromUpstream(...args, upstream, committed);
        assert.equal(again.status, 0, again.stderr);
        assert.doesNotMatch(again.stdout, /Lowered|Note/);
      }
      assert.equal(fs.existsSync(out), false);
    } finally {
      fs.rmSync(out, { force: true });
    }
  });
});

// A pull request that moves a "pending" file to "files", or changes how often a seam's "added"
// lists a line, has those entries held to the fork lines counted from upstream (--upstream or
// main's UPSTREAM_FILE, fetched read-only, commits only). Every way that count cannot run refuses.
const reEscape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
function withUpstream(body) {
  withRepo(({ dir, git, commit, run, runBare, publish, upDir }) => {
    const ref = () => {
      try {
        return git("rev-parse", "--verify", "--quiet", UPSTREAM_REF).trim();
      } catch {
        return undefined;
      }
    };
    const up = commit({ "p.ts": "a();\nb();\n" }, "upstream");
    git("checkout", "-q", "-b", "fork");
    const main = commit(
      {
        ...seams({ files: {}, pending: { "p.ts": 2 } }),
        "p.ts": "a();\nforkA();\nb();\nforkB();\n",
      },
      "main",
    );
    // Published only now: `run` names it from here on (with --upstream), unless a test names
    // another (or --no-upstream).
    publish(up);
    const move = (added, from = main) => {
      git("checkout", "-q", "-f", "-B", `pr-${added.join("-")}`, from);
      return commit(seams({ files: { "p.ts": { why: "w", added } } }), "move p.ts to files");
    };
    body({ dir, git, commit, run, runBare, up, upDir, main, move, ref, publish });
  });
}

test("--upstream: a promoted line upstream has is refused, and a fork line left out", () => {
  withUpstream(({ run, main, move, ref }) => {
    // `a();` is upstream's and `forkB();` is left out: the count (2) and "the file has it" pass.
    const wrong = move(["a();", "forkA();"]);
    const result = run("--config-ref", main, main, wrong);
    assert.equal(result.status, 1, result.stdout);
    assert.match(
      result.stderr,
      /- p\.ts: moved to "files" in this change's \.github\/fork-seams\.json, whose "added" lists `a\(\);`, but the fork adds no copy over upstream \([0-9a-f]{12}\): remove it from "added" \(a line upstream has is never a seam line, and a spare copy lets a later change add the line unannounced\)\./,
    );
    assert.match(
      result.stderr,
      /- p\.ts: moved to "files" .*, whose "added" lists `forkB\(\);` not at all, but the fork adds it over upstream \([0-9a-f]{12}\): list it/,
    );
    assert.equal(ref(), undefined);
  });
});

test("with no upstream named, a move or a seam count change is refused, not skipped", () => {
  withRepo(({ git, commit, run }) => {
    const up = commit({ "p.ts": "a();\nb();\n", ...BASE }, "upstream");
    git("checkout", "-q", "-b", "fork");
    const main = commit(
      {
        ...seams({ files: SEAM, pending: { "p.ts": 1 } }),
        ...SEAMED,
        "p.ts": "a();\nf();\nb();\n",
      },
      "main",
    );
    assert.ok(up);
    const moved = commit(
      seams({ files: { ...SEAM, "p.ts": { why: "w", added: ["f();"] } } }),
      "move p.ts",
    );
    git("checkout", "-q", "-f", "-B", "raise", main);
    const raised = commit(
      seams({
        files: { "a.ts": { ...SEAM["a.ts"], added: [...SEAM["a.ts"].added, "};"] } },
        pending: { "p.ts": 1 },
      }),
      "raise a count",
    );
    for (const [pr, what] of [
      [moved, /p\.ts: this change moves p\.ts from "pending" to "files" in its/],
      [raised, /a\.ts: this change edits their seam lines in its/],
    ]) {
      const result = run("--config-ref", main, main, pr);
      assert.equal(result.status, 1, result.stdout);
      assert.match(result.stderr, what);
      assert.match(
        result.stderr,
        /but there is no \.github\/fork-upstream\.json at [0-9a-f]{12} \(add it, naming upstream's https:\/\/ git URL; by hand, pass --upstream <url>\), so their fork lines cannot be counted/,
      );
      // --no-upstream (fork-tests' run on the merge commit; fork-checks counts): a note.
      const skipped = run("--no-upstream", "--config-ref", main, main, pr);
      assert.equal(skipped.status, 0, skipped.stderr);
      assert.match(skipped.stdout, /^Note: .*; --no-upstream: whether each entry lists exactly/m);
    }
  });
});

test("--upstream: an entry listing exactly the fork lines passes, and runs again the same as upstream moves on", () => {
  withUpstream(({ git, run, up, upDir, main, move, ref, commit }) => {
    const right = move(["forkB();", "forkA();"]);
    const wrong = move(["a();", "forkA();"]);
    const judge = (pr) => run("--config-ref", main, main, pr);
    const first = judge(right);
    assert.equal(first.status, 0, first.stderr);
    assert.match(
      first.stdout,
      new RegExp(
        `each entry lists exactly the fork lines counted from upstream \\(${up.slice(0, 12)}, the merge-base of ${main.slice(0, 12)} and ${reEscape(UPSTREAM_URL)}\\)`,
      ),
    );
    // The ref is gone after the run (a run by hand leaves nothing behind).
    assert.equal(ref(), undefined);
    // Runs again: the same verdicts.
    assert.equal(judge(right).status, 0);
    assert.equal(judge(wrong).status, 1);
    // Upstream moved on past the fork's base (and took a fork line word for word): still judged
    // from the commit main sits on, the merge-base, not upstream's newest head.
    git("checkout", "-q", "-B", "upnext", up);
    git("commit", "-q", "--allow-empty", "-m", "noop");
    const next = commit({ "p.ts": "a();\nforkA();\nb();\nc();\n" }, "upstream takes forkA");
    git("push", "-q", upDir, `${next}:refs/heads/main`);
    const again = judge(right);
    assert.equal(again.status, 0, again.stderr);
    assert.match(again.stdout, new RegExp(`\\(${up.slice(0, 12)}, `));
    assert.equal(ref(), undefined);
  });
});

test("--upstream: an unreachable, empty, unrelated or self-naming upstream is refused, never an old fetch", () => {
  withUpstream(({ dir, git, run, main, move, ref }) => {
    const wrong = move(["a();", "forkA();"]);
    const right = move(["forkA();", "forkB();"]);
    // An earlier run's fetch (or one stopped half-way) left the ref: it is not judged by.
    git("update-ref", UPSTREAM_REF, main);
    for (const pr of [wrong, right]) {
      const result = run("--config-ref", main, "--upstream", GONE_URL, main, pr);
      assert.equal(result.status, 1, result.stdout);
      assert.match(
        result.stderr,
        new RegExp(
          `- p\\.ts: this change moves p\\.ts from "pending" to "files" in its \\.github/fork-seams\\.json, but could not fetch the default branch of ${reEscape(GONE_URL)}: .*, so their fork lines cannot be counted\\.$`,
          "m",
        ),
      );
      // Said as an upstream or fetch problem with how to run it again, not as a wrong list.
      assert.match(
        result.stderr,
        /^Could not count this change's list edits from upstream \(an upstream, fetch or branch problem, not a wrong list;/,
      );
      assert.match(
        result.stderr,
        /Run the check again once the cause is fixed: "Re-run failed jobs" on the pull request's Fork checks run/,
      );
      assert.doesNotMatch(
        result.stderr,
        /must stay within the seam lines|Lower each "pending" ceiling/,
      );
      assert.equal(ref(), undefined);
    }
    const other = `${dir}-unrelated.git`;
    const named = (url, target) => git("config", `url.file://${target}.insteadOf`, url);
    try {
      execFileSync("git", ["init", "-q", "--bare", other]);
      named("https://other.test/u.git", other);
      // An empty upstream (no default branch yet): it cannot be fetched.
      const empty = run(
        "--config-ref",
        main,
        "--upstream",
        "https://other.test/u.git",
        main,
        right,
      );
      assert.equal(empty.status, 1, empty.stdout);
      assert.match(
        empty.stderr,
        /could not fetch the default branch of https:\/\/other\.test\/u\.git: /,
      );
      // An upstream with no commit in common with main: named, not taken for a base.
      git("checkout", "-q", "--orphan", "lonely");
      fs.writeFileSync(path.join(dir, "z.ts"), "z();\n");
      git("add", "-A");
      git("commit", "-q", "-m", "unrelated");
      const lonely = git("rev-parse", "HEAD").trim();
      git("push", "-q", other, `${lonely}:refs/heads/main`);
      execFileSync("git", ["-C", other, "symbolic-ref", "HEAD", "refs/heads/main"]);
      const unrelated = run(
        "--config-ref",
        main,
        "--upstream",
        "https://other.test/u.git",
        main,
        right,
      );
      assert.equal(unrelated.status, 1, unrelated.stdout);
      assert.match(
        unrelated.stderr,
        new RegExp(
          `, ${lonely.slice(0, 12)}\\) shares no commit with ${main}, so their fork lines`,
        ),
      );
      // A URL that names the fork itself: its branch holds main, so every fork line would be
      // "upstream's" and every entry would have to list nothing.
      git("push", "-q", "-f", other, `${main}:refs/heads/main`);
      const self = run("--config-ref", main, "--upstream", "https://other.test/u.git", main, right);
      assert.equal(self.status, 1, self.stdout);
      assert.match(
        self.stderr,
        new RegExp(
          `holds ${main.slice(0, 12)} itself, so the URL names this fork, not its upstream`,
        ),
      );
      assert.equal(ref(), undefined);
    } finally {
      fs.rmSync(other, { recursive: true, force: true });
    }
  });
});

test("--upstream: a change that moves no pending file and changes no seam count fetches nothing", () => {
  withUpstream(({ git, commit, run, main, ref }) => {
    git("checkout", "-q", "-B", "plain", main);
    const plain = commit({ "q.ts": "fork();\n" }, "a fork file");
    git("update-ref", UPSTREAM_REF, main);
    const result = run("--config-ref", main, "--upstream", GONE_URL, main, plain);
    assert.equal(result.status, 0, result.stderr);
    // Not fetched: the ref it would have deleted first is still there.
    assert.equal(ref(), main);
    git("update-ref", "-d", UPSTREAM_REF);
    // From upstream the base is upstream already, and --no-upstream names none: usage errors.
    assert.equal(run("--from-upstream", "--upstream", UPSTREAM_URL, main, plain).status, 2);
    assert.equal(run("--no-upstream", "--upstream", UPSTREAM_URL, main, plain).status, 2);
    assert.equal(run("--from-upstream", "--no-upstream", main, plain).status, 2);
    assert.equal(run("--upstream", main, plain).status, 2);
  });
});

test("--upstream takes only an https:// URL, as main's upstream file does", () => {
  withUpstream(({ upDir, run, main, move, ref }) => {
    const right = move(["forkA();", "forkB();"]);
    for (const url of [`file://${upDir}`, upDir, "-oProxyCommand=x", "ext::sh -c x"]) {
      const result = run("--config-ref", main, "--upstream", url, main, right);
      assert.equal(result.status, 1, `${url}: ${result.stdout}`);
      assert.match(result.stderr, /but --upstream: "url" must be one https:\/\/ git URL/);
      assert.equal(ref(), undefined);
    }
  });
});

// The filter was chosen from extensions.partialClone, which fork-checks' checkout never sets: CI
// fetched upstream whole (12 s, 35 MB). Now every fetch takes commits only.
test("the upstream fetch takes commits only (no trees), in a full clone and in fork-checks' partial one, and leaves no remote or ref", () => {
  withUpstream(({ dir, git, commit, upDir, main, move, up }) => {
    const right = move(["forkA();", "forkB();"]);
    const wrong = move(["a();", "forkA();"]);
    const clone = `${dir}-partial`;
    const full = `${dir}-full`;
    try {
      // A full clone (a run by hand), made before upstream's newest commit.
      execFileSync("git", ["clone", "-q", "--no-checkout", dir, full]);
      const fg = (...args) => execFileSync("git", ["-C", full, ...args], { encoding: "utf8" });
      fg("config", `url.file://${upDir}.insteadOf`, UPSTREAM_URL);
      // Upstream's newest commit, with a tree and a file no clone has.
      git("checkout", "-q", "-B", "upnext", up);
      const next = commit({ "u-only.ts": "upstream only\n" }, "upstream moves on");
      const tree = git("rev-parse", `${next}^{tree}`).trim();
      git("push", "-q", upDir, `${next}:refs/heads/main`);
      // fork-checks' checkout: git init, a blob:none fetch of main and the pull request's commits.
      execFileSync("git", ["init", "-q", clone]);
      const cg = (...args) => execFileSync("git", ["-C", clone, ...args], { encoding: "utf8" });
      cg("config", `url.file://${upDir}.insteadOf`, UPSTREAM_URL);
      execFileSync("git", ["-C", dir, "config", "uploadpack.allowFilter", "true"]);
      cg("remote", "add", "origin", `file://${dir}`);
      cg("fetch", "-q", "--no-tags", "--filter=blob:none", "origin", main, right, wrong);
      cg("checkout", "-q", "--detach", main);
      assert.throws(() => fg("cat-file", "-e", next), undefined, "the full clone predates it");
      for (const [where, run] of [
        [clone, cg],
        [full, fg],
      ]) {
        const judge = (pr) =>
          spawnSync("node", [SCRIPT, "--config-ref", main, "--upstream", UPSTREAM_URL, main, pr], {
            cwd: where,
            encoding: "utf8",
          });
        const ok = judge(right);
        assert.equal(ok.status, 0, ok.stderr);
        assert.match(ok.stdout, /each entry lists exactly the fork lines/);
        const bad = judge(wrong);
        assert.equal(bad.status, 1, bad.stdout);
        assert.match(bad.stderr, /lists `a\(\);`, but the fork adds no copy over upstream/);
        // Upstream's newest commit came (the merge-base was found from it) without its tree.
        const lazy = { env: { ...process.env, GIT_NO_LAZY_FETCH: "1" }, stdio: "pipe" };
        execFileSync("git", ["-C", where, "cat-file", "-e", next], lazy);
        assert.throws(() => execFileSync("git", ["-C", where, "cat-file", "-e", tree], lazy));
        // No remote registered and no ref left: the next run starts from the same config.
        assert.doesNotMatch(run("config", "--list"), /fork-seam-upstream/);
        assert.equal(run("for-each-ref", "refs/fork-seam-check/"), "");
      }
    } finally {
      fs.rmSync(clone, { recursive: true, force: true });
      fs.rmSync(full, { recursive: true, force: true });
    }
  });
});

// An upload-pack that fails or hangs on purpose: git runs remote.<name>.uploadpack for a local
// upstream, so the checker needs no test hook. `$1` counts the attempts.
const uploadPack = (dir, script) => {
  const file = `${dir}-upload-pack.sh`;
  fs.writeFileSync(file, `#!/bin/sh\necho x >> "${dir}-attempts"\n${script}\n`, { mode: 0o755 });
  return file;
};
const attempts = (dir) =>
  fs.existsSync(`${dir}-attempts`)
    ? fs.readFileSync(`${dir}-attempts`, "utf8").split("\n").length - 1
    : 0;

test("the upstream fetch is tried again once: a dropped first attempt still counts the lines", () => {
  withUpstream(({ dir, git, run, main, move, ref }) => {
    const right = move(["forkA();", "forkB();"]);
    const wrong = move(["a();", "forkA();"]);
    const script = uploadPack(
      dir,
      `[ "$(wc -l < "${dir}-attempts")" -gt 1 ] || { echo dropped >&2; exit 1; }\nexec git upload-pack "$@"`,
    );
    try {
      git("config", "remote.fork-seam-upstream.uploadpack", script);
      const ok = run("--config-ref", main, main, right);
      assert.equal(ok.status, 0, ok.stderr);
      assert.equal(attempts(dir), 2);
      fs.rmSync(`${dir}-attempts`);
      const bad = run("--config-ref", main, main, wrong);
      assert.equal(bad.status, 1, bad.stdout);
      assert.match(bad.stderr, /lists `a\(\);`, but the fork adds no copy over upstream/);
      assert.equal(attempts(dir), 2);
      // Two failures: refused, naming git's own words.
      fs.writeFileSync(script, `#!/bin/sh\necho x >> "${dir}-attempts"\necho down >&2\nexit 1\n`);
      fs.rmSync(`${dir}-attempts`);
      const down = run("--config-ref", main, main, right);
      assert.equal(down.status, 1, down.stdout);
      assert.match(
        down.stderr,
        /could not fetch the default branch of https:\/\/upstream\.test\/u\.git: /,
      );
      assert.equal(attempts(dir), 2);
      assert.equal(ref(), undefined);
    } finally {
      fs.rmSync(script, { force: true });
      fs.rmSync(`${dir}-attempts`, { force: true });
    }
  });
});

test("a hung upstream is refused within the total fetch time, which stays well below fork-checks' job limit", () => {
  withUpstream(({ dir, git, main, ref }) => {
    // Its stderr closed: the killed fetch's pipes must not wait for the orphaned sleep.
    const script = uploadPack(dir, "exec 2>/dev/null\nsleep 5");
    try {
      git("config", "remote.fork-seam-upstream.uploadpack", script);
      // A fake clock, no timing: the first attempt is capped at the 1.5 s total (not its own 10 s),
      // and the clock then says the total is spent, so no second attempt starts. Uncapped, or
      // tried again with nothing left (a 0 timeout is none), git waits out the 5 s sleep instead.
      const clock = [0, 0];
      const found = upstreamBase(UPSTREAM_URL, main, [main], {
        cwd: dir,
        attemptMs: 10_000,
        totalMs: 1_500,
        now: () => clock.shift() ?? 1_500,
      });
      assert.match(
        found.problem,
        /^could not fetch the default branch of .*: no answer within 2 s in all \(SIGTERM\)$/,
      );
      assert.equal(attempts(dir), 1);
      assert.equal(ref(), undefined);
    } finally {
      fs.rmSync(script, { force: true });
      fs.rmSync(`${dir}-attempts`, { force: true });
    }
  });
  // fork-checks' job runs checkout and these tests (about a minute) before the fetch.
  const workflow = fs.readFileSync(
    path.join(REPO_ROOT, ".github/workflows/fork-checks.yml"),
    "utf8",
  );
  const job = workflow.slice(workflow.indexOf("no_schema_changes:"));
  const minutes = Number(job.match(/timeout-minutes: (\d+)/)[1]);
  assert.ok(FETCH_TOTAL_MS <= (minutes * 60_000) / 2, `${FETCH_TOTAL_MS} ms of ${minutes} min`);
  assert.ok(FETCH_ATTEMPT_MS < FETCH_TOTAL_MS);
});

test("a shallow clone is refused as shallow, not as an upstream that shares no commit", () => {
  withUpstream(({ dir, main }) => {
    const shallow = `${dir}-shallow`;
    try {
      execFileSync("git", ["clone", "-q", "--depth", "1", `file://${dir}`, shallow]);
      const found = upstreamBase(UPSTREAM_URL, main, [main], { cwd: shallow });
      assert.match(
        found.problem,
        /^this clone is shallow, so the commit main shares with upstream is not in it \(fetch its whole history: git fetch --unshallow\)$/,
      );
    } finally {
      fs.rmSync(shallow, { recursive: true, force: true });
    }
  });
});

// A stacked pull request whose base branch predates main's last rebase: its merge-base has the
// list, but it lacks the upstream commit main sits on, so its lines cannot be counted from there.
test("a branch without the upstream commit main sits on is told to update, not counted", () => {
  withUpstream(({ git, commit, run, up, upDir, main, ref }) => {
    // Upstream moves on; main is rebased onto it (a new main with the same fork lines).
    git("checkout", "-q", "-f", "-B", "upnext", up);
    const next = commit({ "n.ts": "n();\n" }, "upstream moves on");
    git("push", "-q", upDir, `${next}:refs/heads/main`);
    const newMain = commit(
      {
        ...seams({ files: {}, pending: { "p.ts": 2 } }),
        "p.ts": "a();\nforkA();\nb();\nforkB();\n",
      },
      "main, rebased",
    );
    // The old base branch, and a pull request on it that moves p.ts.
    git("checkout", "-q", "-f", "-B", "stack", main);
    const pr = commit(
      seams({ files: { "p.ts": { why: "w", added: ["forkA();", "forkB();"] } } }),
      "move p.ts",
    );
    const result = run("--config-ref", newMain, main, pr);
    assert.equal(result.status, 1, result.stdout);
    assert.match(
      result.stderr,
      new RegExp(
        `- p\\.ts: this change moves p\\.ts from "pending" to "files" in its \\.github/fork-seams\\.json, but this branch does not have upstream's commit ${next.slice(0, 12)} that ${newMain.slice(0, 12)} sits on, so their fork lines cannot be counted: update the branch from its base\\.`,
      ),
    );
    assert.equal(ref(), undefined);
    // Updated onto the new main, it is counted and passes.
    git("checkout", "-q", "-f", "-B", "updated", newMain);
    const updated = commit(
      seams({ files: { "p.ts": { why: "w", added: ["forkA();", "forkB();"] } } }),
      "move p.ts",
    );
    const ok = run("--config-ref", newMain, newMain, updated);
    assert.equal(ok.status, 0, ok.stderr);
  });
});

// The pull request's run used to judge only a list change that lowered a count. One that raised a
// count with no copy added (spare room), or dropped a line still in use (fork-follow stops), passed
// its own run and stopped the next fork-follow instead. Now both are counted from upstream.
test("a seam count the pull request raises without the line, or lowers while the line is in use, is refused in its own run", () => {
  withRepo(({ git, commit, run, fromUpstream, publish }) => {
    const upstream = commit({ ...BASE, "u.ts": UP }, "upstream");
    // Main announces `forkMore();` (step 1) on upstream's commit day, so a step 2 may add it.
    const since = git("show", "-s", "--format=%cs", upstream).trim();
    const announced = { since, added: ["forkMore();"] };
    const main = commit(
      { ...seams({ files: { "a.ts": { ...SEAM["a.ts"], announced } } }), ...SEAMED },
      "fork main",
    );
    publish(upstream);
    assert.equal(fromUpstream(upstream, main).status, 0);
    const pr = (name, list, files = {}) => {
      git("checkout", "-q", "-f", "-B", name, main);
      return commit({ ...seams(list), ...files }, name);
    };
    const withAdded = (file, added, why = "test") => ({
      files: { ...SEAM, [file]: { why, added } },
    });
    const cases = [
      // A list-only change that lists upstream's `};` (the fork adds no copy): spare room.
      [
        "raise",
        withAdded("a.ts", [...SEAM["a.ts"].added, "};"]),
        {},
        /- a\.ts: listed in this change's \.github\/fork-seams\.json, whose "added" lists `};`, but the fork adds no copy over upstream \([0-9a-f]{12}\): remove it from "added"/,
      ],
      // A step-2 change that lists a line twice but adds one copy.
      [
        "twice",
        withAdded("a.ts", [...SEAM["a.ts"].added, "...fork,"]),
        {},
        /- a\.ts: listed in .*, whose "added" lists `\.\.\.fork,` 2×, but the fork adds 1× over upstream .*: list it 1×/,
      ],
      // A list-only change that drops a line the file still has.
      [
        "drop",
        withAdded("a.ts", ["import { fork } from './fork';"]),
        {},
        /- a\.ts: listed in .*, whose "added" lists `\.\.\.fork,` not at all, but the fork adds it over upstream .*: list it/,
      ],
      // A whole entry dropped while its lines are in use.
      [
        "entry",
        { files: {} },
        {},
        /- a\.ts: dropped from "files" in this change's \.github\/fork-seams\.json, but the fork adds 2 lines over upstream \([0-9a-f]{12}\): keep its entry, listing each/,
      ],
      // A new entry for an unlisted upstream file that lists one of upstream's lines.
      [
        "new",
        { files: { ...SEAM, "u.ts": { why: "new", added: ["export const x = 1;"] } } },
        {},
        /- u\.ts: listed in .*, whose "added" lists `export const x = 1;`, but the fork adds no copy over upstream/,
      ],
    ];
    for (const [name, list, files, why] of cases) {
      const head = pr(name, list, files);
      for (const args of [
        [main, head],
        ["--config-ref", main, main, head],
      ]) {
        const result = run(...args);
        assert.equal(result.status, 1, `${name}: ${result.stdout}`);
        assert.match(result.stderr, why, name);
      }
      // Merged as it was, the next run from upstream would refuse main.
      assert.equal(fromUpstream(upstream, head).status, 1, name);
    }
    // The right changes pass: step 2 lists the line it adds; a change that drops a line drops
    // it from its list; a list-only fix of main's spare copy passes (it changes no file).
    const passing = [
      pr("step2", withAdded("a.ts", [...SEAM["a.ts"].added, "forkMore();"]), {
        "a.ts": `${SEAMED["a.ts"]}forkMore();\n`,
      }),
      // It keeps main's in-date announcement (a change that drops one is refused).
      pr(
        "dropped",
        {
          files: {
            "a.ts": { why: "test", added: ["import { fork } from './fork';"], announced },
          },
        },
        { "a.ts": SEAMED["a.ts"].replace("  ...fork,\n", "") },
      ),
    ];
    git("checkout", "-q", "-f", "-B", "spare", main);
    const spareMain = commit(
      seams(withAdded("a.ts", [...SEAM["a.ts"].added, "};"])),
      "a spare copy",
    );
    passing.push(commit(seams({ files: SEAM }), "fix main's list"));
    for (const head of passing.slice(0, 2)) {
      const result = run(main, head);
      assert.equal(result.status, 0, result.stderr);
      assert.match(
        result.stdout,
        /^Note: a\.ts: this change edits their seam lines .*; each entry lists exactly the fork lines/m,
      );
      assert.equal(fromUpstream(upstream, head).status, 0);
    }
    const fix = run(spareMain, passing[2]);
    assert.equal(fix.status, 0, fix.stderr);
    assert.equal(fromUpstream(upstream, passing[2]).status, 0);
    // A change to "announced" only, or a new entry that lists nothing, changes no count: no fetch.
    const announcing = pr("announce", {
      files: {
        ...SEAM,
        "u.ts": { why: "n", added: [], announced: { since, added: ["forkNew();"] } },
      },
    });
    const quiet = run("--upstream", GONE_URL, main, announcing);
    assert.equal(quiet.status, 0, quiet.stderr);
  });
});

// A list-only change to a "pending" ceiling, a "lost" count or "forkFiles" was not counted in
// the pull request's run: one raised a ceiling and "lost" with no line changed, a second swapped
// upstream lines for fork lines within them, and neither run nor fork-follow refused either.
test("a pending ceiling, lost count, forkFiles pattern or entry the pull request's list changes is counted from upstream", () => {
  withRepo(({ git, commit, run, fromUpstream, publish }) => {
    const upstream = commit({ "p.ts": "a();\nb();\nc();\n", "q.ts": "q();\n" }, "upstream");
    git("checkout", "-q", "-b", "fork");
    const P = "a();\nforkA();\nb();\nforkB();\nc();\n";
    const main = commit({ ...seams({ files: {}, pending: { "p.ts": 2 } }), "p.ts": P }, "main");
    publish(upstream);
    assert.equal(fromUpstream(upstream, main).status, 0);
    const pr = (name, list, files = {}, from = main) => {
      git("checkout", "-q", "-f", "-B", name, from);
      return commit({ ...seams(list), ...files }, name);
    };
    const lost = (lines, gone) => ({ "p.ts": { lines, gone } });
    const swap = pr("swap-list", { files: {}, pending: { "p.ts": 4 }, lost: lost(2, 2) });
    const cases = [
      [
        "raise",
        { files: {}, pending: { "p.ts": 4 } },
        /- p\.ts: "pending" with a ceiling of 4 in this change's \.github\/fork-seams\.json, but the fork adds 2 lines over upstream \([0-9a-f]{12}\): set its ceiling to 2 \(a ceiling above the count is room for a fork line later/,
      ],
      [
        "swap",
        swap,
        /- p\.ts: set in "lost" in this change's .* \({"lines": 2, "gone": 2}\), but the fork loses 0 upstream lines over upstream \([0-9a-f]{12}\) .*: remove it from "lost"/,
      ],
      [
        "lost",
        { files: {}, pending: { "p.ts": 2 }, lost: lost(1, 0) },
        /- p\.ts: set in "lost" .*remove it from "lost"/,
      ],
      [
        "lower",
        { files: {}, pending: { "p.ts": 1 } },
        /ceiling of 1 .*adds 2 lines over upstream .*: set its ceiling to 2/,
      ],
      [
        "drop",
        { files: {} },
        /- p\.ts: dropped from "pending" in this change's .*, but the fork adds 2 lines over upstream \([0-9a-f]{12}\): keep its entry/,
      ],
      [
        "new",
        { files: {}, pending: { "p.ts": 2, "q.ts": 1 }, lost: { "q.ts": { lines: 1, gone: 1 } } },
        /- q\.ts: "pending" with a ceiling of 1 .*adds 0 lines over upstream .*: remove it from "pending"[^]*- q\.ts: set in "lost" .*remove it from "lost"/,
      ],
      [
        "forkFiles",
        { files: {}, pending: { "p.ts": 2 }, forkFiles: ["tools/", "q.*"] },
        /- q\.ts: upstream has it, but this change's \.github\/fork-seams\.json "forkFiles" takes it for a fork file .*: exclude it with "!q\.ts", or narrow the pattern\./,
      ],
      [
        "ghost",
        { files: { "ghost.ts": { why: "w", added: ["evil();"] } }, pending: { "p.ts": 2 } },
        /- ghost\.ts: listed in this change's \.github\/fork-seams\.json, but the head does not have it: list only a file this change keeps\./,
      ],
    ];
    for (const [name, list, why] of cases) {
      const head = typeof list === "string" ? list : pr(name, list);
      for (const args of [
        [main, head],
        ["--config-ref", main, main, head],
      ]) {
        const result = run(...args);
        assert.equal(result.status, 1, `${name}: ${result.stdout}`);
        assert.match(result.stderr, why, name);
      }
    }
    // Why the swap matters: merged, a change that swaps two upstream lines for fork lines fits
    // the raised ceiling and "lost", and neither its run nor fork-follow's refuses it.
    const swapped = pr(
      "swapped",
      { files: {}, pending: { "p.ts": 4 }, lost: lost(2, 2) },
      {
        "p.ts": "forkC();\nforkA();\nforkD();\nforkB();\nc();\n",
      },
      swap,
    );
    assert.equal(run(swap, swapped).status, 0);
    assert.equal(fromUpstream(upstream, swapped).status, 0);
    // The right changes pass, each counted: a ceiling lowered with its line, a list-only fix of
    // main's spare ceiling, and a fork-only "forkFiles" pattern.
    const lowered = pr(
      "lowered",
      { files: {}, pending: { "p.ts": 1 } },
      {
        "p.ts": "a();\nforkA();\nb();\nc();\n",
      },
    );
    const spareMain = pr("spare", { files: {}, pending: { "p.ts": 3 } });
    const fix = pr("fix", { files: {}, pending: { "p.ts": 2 } }, {}, spareMain);
    const tools = pr("tools", { files: {}, pending: { "p.ts": 2 }, forkFiles: ["tools/"] });
    for (const [base, head] of [
      [main, lowered],
      [spareMain, fix],
      [main, tools],
    ]) {
      const result = run(base, head);
      assert.equal(result.status, 0, result.stderr);
      assert.match(
        result.stdout,
        /; each entry lists exactly the fork lines counted from upstream/,
      );
      assert.equal(fromUpstream(upstream, head).status, 0);
    }
  });
});

test("listEditedCounts: a ceiling or lost count set, changed or dropped, and forkFiles changed", () => {
  const list = (pending, lost = {}, forkFiles = []) => ({ files: {}, pending, lost, forkFiles });
  const base = list({ "p.ts": 2 }, { "p.ts": { lines: 1, gone: 0 } }, ["tools/"]);
  const none = { pending: [], lost: [], forkFiles: false };
  assert.deepEqual(listEditedCounts(base, base), none);
  assert.deepEqual(
    listEditedCounts(base, list({ "p.ts": 2 }, base.lost, ["tools/", "tools/"])),
    none,
  );
  assert.deepEqual(listEditedCounts(base, list({ "p.ts": 3, "q.ts": 1 }, {}, ["x/"])), {
    pending: ["p.ts", "q.ts"],
    lost: ["p.ts"],
    forkFiles: true,
  });
  assert.deepEqual(
    listEditedCounts(base, list({}, { "p.ts": { lines: 1, gone: 1 } }, ["tools/"])),
    {
      ...none,
      pending: ["p.ts"],
      lost: ["p.ts"],
    },
  );
  assert.deepEqual(listEditedCounts(undefined, list({ "p.ts": 2 })), {
    pending: ["p.ts"],
    lost: [],
    forkFiles: true,
  });
});

test("findLostNotUpstream and findOwnHiddenFiles count from upstream", () => {
  const up = "a();\nb();\nc();\n";
  const at = (head) => ({ atUpstream: () => up, atHead: () => head, name: "abc" });
  // b(); deleted: one line lost, its text gone; c(); moved above a(): one more lost, not gone.
  assert.deepEqual(
    findLostNotUpstream(["p.ts"], { "p.ts": { lines: 1, gone: 1 } }, at("a();\nc();\n")),
    [],
  );
  assert.deepEqual(
    findLostNotUpstream(["p.ts"], { "p.ts": { lines: 2, gone: 1 } }, at("c();\na();\n")),
    [],
  );
  assert.deepEqual(findLostNotUpstream(["p.ts"], {}, at(up)), []);
  assert.deepEqual(findLostNotUpstream(["p.ts"], { "p.ts": { lines: 2, gone: 2 } }, at(up)), [
    'p.ts: set in "lost" in this change\'s .github/fork-seams.json ({"lines": 2, "gone": 2}), but the fork loses 0 upstream lines over upstream (abc) (deleted, moved or re-indented), 0 of them gone: remove it from "lost" (a count above the lines lost is room to swap one for a fork line later).',
  ]);
  assert.match(
    findLostNotUpstream(["p.ts"], {}, at("a();\nc();\n"))[0],
    /^p\.ts: dropped from "lost" .*\(no entry\), but the fork loses 1 upstream line over .*, 1 of them gone: set it to {"lines": 1, "gone": 1}/,
  );
  assert.match(
    findLostNotUpstream(
      ["p.ts"],
      { "p.ts": { lines: 1, gone: 1 } },
      { ...at(up), atUpstream: () => undefined },
    )[0],
    /upstream \(abc\) does not have it, so its lost lines cannot be counted/,
  );
  assert.match(
    findLostNotUpstream(["p.ts"], { "p.ts": { lines: 1, gone: 1 } }, at(undefined))[0],
    /but the head does not have it/,
  );
  assert.deepEqual(findLostNotUpstream(["p.ts"], {}, at(undefined)), []);
  const has = () => "x";
  const files = ["src/a.ts", "src/b.ts", "tools/t.ts"];
  assert.deepEqual(findOwnHiddenFiles(files, ["tools/"], [], has).length, 1);
  assert.deepEqual(findOwnHiddenFiles(files, ["tools/"], ["tools/"], has), []);
  assert.deepEqual(
    findOwnHiddenFiles(files, ["src/", "!src/b.ts"], undefined, has).map((r) => r.split(":")[0]),
    ["src/a.ts"],
  );
  assert.deepEqual(
    findOwnHiddenFiles(files, ["tools/"], [], () => undefined),
    [],
  );
});

test("listEditedFiles: only a line listed more or less often, and every entry when the base list is unreadable", () => {
  const entry = (...added) => ({ why: "w", added });
  const base = { "a.ts": entry("x();", "y();"), "b.ts": entry() };
  assert.deepEqual(listEditedFiles(base, { "a.ts": entry("  y();", "x();"), "b.ts": entry() }), []);
  assert.deepEqual(listEditedFiles(base, { "a.ts": entry("x();", "y();", "x();") }), ["a.ts"]);
  assert.deepEqual(listEditedFiles(base, { "a.ts": entry("x();"), "c.ts": entry() }), ["a.ts"]);
  assert.deepEqual(listEditedFiles(base, { "b.ts": entry() }), ["a.ts"]);
  assert.deepEqual(listEditedFiles(base, { ...base, "c.ts": entry("z();") }), ["c.ts"]);
  assert.deepEqual(listEditedFiles(undefined, { "b.ts": entry() }), ["b.ts"]);
});

test("findListedNotFork: exact copies both ways, a file upstream lacks, a dropped entry, and which files are promoted", () => {
  const up = "a();\nb();\n";
  const head = "a();\nforkA();\nforkA();\nb();\n";
  const at = (text, promoted = new Set(["p.ts"]), ownPending) => ({
    atUpstream: () => text,
    atHead: () => head,
    name: "abc",
    promoted,
    ownPending,
  });
  assert.deepEqual(findListedNotFork(["p.ts"], promoted("forkA();", "forkA();"), at(up)), []);
  assert.deepEqual(findListedNotFork(["p.ts"], promoted("forkA();"), at(up)), [
    'p.ts: moved to "files" in this change\'s .github/fork-seams.json, whose "added" lists `forkA();`, but the fork adds it 2× over upstream (abc): list it 2× (every fork line the file holds is listed).',
  ]);
  assert.match(
    findListedNotFork(["p.ts"], promoted("forkA();", "forkA();", "forkA();"), at(up))[0],
    /lists `forkA\(\);` 3×, but the fork adds 2× over upstream \(abc\): list it 2×/,
  );
  assert.match(
    findListedNotFork(["p.ts"], promoted("forkA();", "forkA();"), at(undefined))[0],
    /upstream \(abc\) does not have it, so its fork lines cannot be counted: keep it in "pending"/,
  );
  assert.match(
    findListedNotFork(["p.ts"], promoted("forkA();", "forkA();"), at(undefined, new Set()))[0],
    /^p\.ts: listed in .*upstream \(abc\) does not have it, .*: keep main's entry/,
  );
  // Dropped from "files": no fork line may be left, or exactly its "pending" ceiling.
  const none = new Set();
  assert.deepEqual(findListedNotFork(["p.ts"], {}, at(head, none)), []);
  assert.deepEqual(findListedNotFork(["p.ts"], {}, at(up, none)), [
    'p.ts: dropped from "files" in this change\'s .github/fork-seams.json, but the fork adds 2 lines over upstream (abc): keep its entry, listing each (every fork line the file holds is listed).',
  ]);
  assert.deepEqual(findListedNotFork(["p.ts"], {}, at(up, none, { "p.ts": 2 })), []);
  assert.deepEqual(findListedNotFork(["p.ts"], {}, at(up, none, { "p.ts": 1 })), [
    "p.ts: \"pending\" with a ceiling of 1 in this change's .github/fork-seams.json, but the fork adds 2 lines over upstream (abc): set its ceiling to 2 (a ceiling above the count is room for a fork line later; below it, fork-follow's next run refuses main).",
  ]);
  assert.match(
    findListedNotFork(["p.ts"], {}, at(head, none, { "p.ts": 3 }))[0],
    /ceiling of 3 .*, but the fork adds 0 lines over upstream \(abc\): remove it from "pending"/,
  );
  // An entry for a file the head does not have is refused; a dropped one is a deletion, refused
  // elsewhere (or right).
  const gone = { ...at(up), atHead: () => undefined };
  assert.deepEqual(findListedNotFork(["p.ts"], promoted("x();"), gone), [
    'p.ts: moved to "files" in this change\'s .github/fork-seams.json, but the head does not have it: list only a file this change keeps.',
  ]);
  assert.equal(findListedNotFork(["p.ts"], {}, { ...gone, ownPending: { "p.ts": 1 } }).length, 1);
  assert.deepEqual(findListedNotFork(["p.ts"], {}, { ...gone, promoted: none }), []);
  assert.deepEqual(
    promotedFiles(
      { pending: { "p.ts": 1, "q.ts": 1, "r.ts": 1 } },
      { "p.ts": {}, "q.ts": {} },
      { "q.ts": 1 },
    ),
    ["p.ts"],
  );
  assert.deepEqual(promotedFiles({ pending: { "p.ts": 1 } }), []);
});

test("the list commit's upstream file: a wrong move is refused without --upstream; the pull request's own copy is never read", () => {
  withUpstream(({ git, commit, run, runBare, main, move, ref }) => {
    const named = JSON.stringify({ "//": "test", url: UPSTREAM_URL });
    const plainRun = (...args) => run("--no-upstream", ...args);
    // The pull request adds the file itself (main has none): main's list judges, and names no
    // upstream, so the move is refused (with --no-upstream: a note).
    git("checkout", "-q", "-f", "-B", "own-file", main);
    const ownFile = commit(
      {
        ...seams({ files: { "p.ts": { why: "w", added: ["a();", "forkA();"] } } }),
        [UPSTREAM_FILE]: named,
      },
      "move p.ts and name upstream itself",
    );
    const own = runBare("--config-ref", main, main, ownFile);
    assert.equal(own.status, 1, own.stdout);
    assert.match(own.stderr, /there is no \.github\/fork-upstream\.json at [0-9a-f]{12}/);
    assert.equal(plainRun("--config-ref", main, main, ownFile).status, 0);
    assert.equal(ref(), undefined);
    // Main names upstream: the same wrong entry is refused with no flag, a right one passes, twice.
    git("checkout", "-q", "-f", "-B", "main-named", main);
    const mainNamed = commit({ [UPSTREAM_FILE]: named }, "name upstream");
    const wrong = move(["a();", "forkA();"], mainNamed);
    const right = move(["forkA();", "forkB();"], mainNamed);
    const byFile = runBare;
    for (let round = 0; round < 2; round++) {
      const refused = byFile("--config-ref", mainNamed, mainNamed, wrong);
      assert.equal(refused.status, 1, refused.stdout);
      assert.match(refused.stderr, /lists `a\(\);`, but the fork adds no copy over upstream/);
      const passed = byFile("--config-ref", mainNamed, mainNamed, right);
      assert.equal(passed.status, 0, passed.stderr);
      assert.match(passed.stdout, new RegExp(`and ${reEscape(UPSTREAM_URL)}\\)`));
    }
    // --upstream, given, wins over the file (an unreachable one: refused, not the file's verdict).
    const flagged = byFile("--config-ref", mainNamed, "--upstream", GONE_URL, mainNamed, right);
    assert.equal(flagged.status, 1, flagged.stdout);
    assert.match(
      flagged.stderr,
      /could not fetch the default branch of https:\/\/gone\.test\/u\.git: /,
    );
  });
});

test("a malformed upstream file on the list's commit refuses a move to files, and only a move", () => {
  withUpstream(({ upDir, git, commit, runBare, main, move, ref }) => {
    const bad = {
      "": /is not JSON/,
      "[]": /is not a JSON object/,
      '{"url":"https://example.invalid/u.git","repo":"x"}': /unknown key "repo" \(known: url\)/,
      '{"url":"--upload-pack=touch x"}': /"url" must be one https:\/\/ git URL/,
      '{"url":"ext::sh -c x"}': /"url" must be one https:\/\/ git URL/,
      [`{"url":"file://${upDir}"}`]: /"url" must be one https:\/\/ git URL/,
      [`{"url":"${UPSTREAM_URL} x"}`]: /"url" must be one https:\/\/ git URL/,
    };
    for (const [text, why] of Object.entries(bad)) {
      git("checkout", "-q", "-f", "-B", "main-bad", main);
      const mainBad = commit({ [UPSTREAM_FILE]: text }, "a broken upstream file");
      const result = runBare(
        "--config-ref",
        mainBad,
        mainBad,
        move(["forkA();", "forkB();"], mainBad),
      );
      assert.equal(result.status, 1, `${text}: ${result.stdout}`);
      assert.match(
        result.stderr,
        /p\.ts: this change moves p\.ts from "pending" to "files" in its \.github\/fork-seams\.json, but \.github\/fork-upstream\.json at [0-9a-f]{12}/,
      );
      assert.match(result.stderr, why);
      assert.match(result.stderr, /fix that with a pull request to main first/);
      assert.equal(ref(), undefined);
      // A change that moves no pending file (the fix itself, say) never reads it.
      git("checkout", "-q", "-f", "-B", "plain-bad", mainBad);
      const plain = commit({ "q.ts": "fork();\n" }, "a fork file");
      assert.equal(runBare("--config-ref", mainBad, mainBad, plain).status, 0);
    }
  });
});

test("this repository's upstream file is well formed and names an https URL", () => {
  const raw = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, UPSTREAM_FILE), "utf8"));
  assert.match(raw.url, /^https:\/\/[^\s]+\.git$/);
  assert.deepEqual(
    Object.keys(raw).filter((key) => !key.startsWith("//")),
    ["url"],
  );
  // Read from git as the check reads it (main's copy on a pull request).
  const named = upstreamNamedAt("HEAD");
  assert.equal(named.problem, undefined);
});

// fork-checks (pull_request_target, main's copy, full history) counts from upstream; fork-tests'
// run on the merge commit is shallow (two commits), so it says --no-upstream instead of being
// refused there. The other run must never say it.
test("fork-checks counts from upstream and fork-tests' shallow run passes --no-upstream", () => {
  const read = (file) => fs.readFileSync(path.join(REPO_ROOT, ".github/workflows", file), "utf8");
  const runs = (text) => text.split("\n").filter((line) => line.includes("fork-seam-check.cjs "));
  const checks = runs(read("fork-checks.yml"));
  assert.equal(checks.length, 1);
  assert.doesNotMatch(checks[0], /--no-upstream|--upstream/);
  const tests = runs(read("fork-tests.yml"));
  assert.equal(tests.length, 1);
  assert.match(tests[0], /fork-seam-check\.cjs --no-upstream HEAD\^1 HEAD$/);
});

// A pull request's run judged a "pending" file from the merge-base, where swapping an upstream
// line for a fork line keeps every count it sees (one line out, one in), so it passed while
// fork-follow's count from upstream refused main. Both forms now count the same lines.
test("a change that swaps an upstream line in a pending file whose counts are reached is refused by both forms", () => {
  withRepo(({ git, commit, run, runBare, fromUpstream, publish }) => {
    const up = commit({ "p.ts": "a();\nb();\nc();\n" }, "upstream");
    git("checkout", "-q", "-b", "fork");
    // One fork line where upstream's `b();` was: the ceiling and the "lost" counts are reached.
    const list = { files: {}, pending: { "p.ts": 1 }, lost: { "p.ts": { lines: 1, gone: 1 } } };
    const main = commit({ ...seams(list), "p.ts": "a();\nforkB();\nc();\n" }, "main");
    publish(up);
    assert.equal(fromUpstream(up, main).status, 0);
    const swap = commit({ "p.ts": "a();\nforkB();\nforkC();\n" }, "swap upstream's c();");
    const follow = fromUpstream(up, swap);
    assert.equal(follow.status, 1, follow.stdout);
    assert.match(
      follow.stderr,
      /p\.ts: 2 upstream lines deleted, moved or re-indented, more than its "lost" lines count of 1/,
    );
    assert.match(follow.stderr, /p\.ts: "pending" with 2 fork lines, more than its ceiling of 1/);
    const pr = run("--config-ref", main, main, swap);
    assert.equal(pr.status, 1, pr.stdout);
    assert.match(
      pr.stderr,
      /- p\.ts: "pending" with a ceiling of 1 in this change's \.github\/fork-seams\.json, but the fork adds 2 lines over upstream \([0-9a-f]{12}\): set its ceiling to 2/,
    );
    assert.match(
      pr.stderr,
      /- p\.ts: set in "lost" in this change's \.github\/fork-seams\.json \({"lines": 1, "gone": 1}\), but the fork loses 2 upstream lines over upstream \([0-9a-f]{12}\) .*: set it to {"lines": 2, "gone": 2}/,
    );
    // Runs again: the same answer.
    assert.equal(run("--config-ref", main, main, swap).stderr, pr.stderr);
    // A change to a fork line only keeps both counts: counted from upstream, it passes both forms.
    git("checkout", "-q", "-f", "-B", "reword", main);
    const reword = commit({ "p.ts": "a();\nforkB2();\nc();\n" }, "reword the fork line");
    const ok = run("--config-ref", main, main, reword);
    assert.equal(ok.status, 0, ok.stderr);
    assert.match(
      ok.stdout,
      /^Note: p\.ts: this change changes the text of the "pending" file p\.ts.*counted from upstream/m,
    );
    assert.equal(fromUpstream(up, reword).status, 0);
    // With no upstream to count from, the edit is refused, not skipped; fork-tests' run says
    // --no-upstream (fork-checks counts).
    const bare = runBare("--config-ref", main, main, reword);
    assert.equal(bare.status, 1, bare.stdout);
    assert.match(
      bare.stderr,
      /p\.ts: this change changes the text of the "pending" file p\.ts.*cannot be counted/,
    );
    assert.equal(run("--no-upstream", "--config-ref", main, main, reword).status, 0);
    // A change that leaves the pending file alone fetches nothing.
    git("checkout", "-q", "-f", "-B", "other", main);
    const other = commit({ "fork-own.ts": "x();\n" }, "a fork file");
    assert.equal(runBare("--config-ref", main, main, other).status, 0);
  });
});

// JSON.parse keeps the last of two equal keys, so the other entry is shown but never applies.
// A pull request's own list was refused for one; main's list (merged around that check) and the
// head's list fork-follow reads were not.
test("a list with a duplicate key is refused wherever it is read: main's, the merge-base's or fork-follow's head", () => {
  withRepo(({ git, commit, run, fromUpstream }) => {
    const up = commit({ ...BASE }, "upstream");
    git("checkout", "-q", "-b", "fork");
    const entry = (added) => JSON.stringify({ why: "w", added });
    const dup = `{"files": {"a.ts": ${entry(["import { fork } from './fork';"])}, "a.ts": ${entry(["...fork,"])}}}\n`;
    const main = commit({ [CONFIG]: dup, ...SEAMED }, "main with a duplicate key");
    // A malformed list stops the run (exit 3), as any other does: it never runs on half a list.
    const follow = fromUpstream(up, main);
    assert.equal(follow.status, 3, follow.stdout);
    assert.match(
      follow.stderr,
      /\.github\/fork-seams\.json at [0-9a-f]{12}: duplicate key "a\.ts"/,
    );
    const plain = commit({ "fork-own.ts": "x();\n" }, "a fork file");
    const pr = run("--config-ref", main, main, plain);
    assert.equal(pr.status, 3, pr.stdout);
    assert.match(pr.stderr, /\.github\/fork-seams\.json at [0-9a-f]{12}: duplicate key "a\.ts"/);
    assert.throws(() => parseConfig(dup), /duplicate key "a\.ts"/);
  });
});

// A squash merge from a branch made before an announcement dropped its lines, and nothing noticed. A change
// that is not list-only keeps each in-date announcement main's list has (and its merge-base had):
// still announced, or moved to "added".
test("a change that drops another change's in-date announcement is refused, unless list-only or expired", () => {
  withRepo(({ git, commit, run }) => {
    const day = "2026-11-18T12:00:00Z";
    commit({ ...BASE, "n.ts": UP }, "upstream", day);
    git("checkout", "-q", "-b", "fork");
    const main = commit({ ...seams(ANNOUNCED()), ...SEAMED }, "step 1", day);
    const withList = (files, list = { files: SEAM }) =>
      commit({ ...files, ...seams(list) }, "change", day);
    // A code change whose list came from an older copy: refused, naming the line.
    git("checkout", "-q", "-f", "-B", "drop", main);
    const drop = withList({ "fork-own.ts": "x();\n" });
    const refused = run("--config-ref", main, main, drop);
    assert.equal(refused.status, 1, refused.stdout);
    assert.match(
      refused.stderr,
      /- n\.ts: this change's \.github\/fork-seams\.json drops the announced line `forkNew\(\);` \(main's list announces it since 2026-11-18, in date\)/,
    );
    assert.equal(run("--config-ref", main, main, drop).stderr, refused.stderr);
    // The same drop in a list-only change says so: it passes.
    git("checkout", "-q", "-f", "-B", "withdraw", main);
    assert.equal(run("--config-ref", main, main, withList({})).status, 0);
    // Kept, or still announced with another list edit: passes.
    git("checkout", "-q", "-f", "-B", "keep", main);
    const keep = withList({ "fork-own.ts": "x();\n" }, { ...ANNOUNCED(), pending: {} });
    assert.equal(run("--config-ref", main, main, keep).status, 0);
    // Expired by main's commit date: may go with any change.
    git("checkout", "-q", "-f", "-B", "late", main);
    const lateMain = commit({ "late.txt": "x\n" }, "main, later", "2026-12-10T12:00:00Z");
    const late = withList({ "fork-own.ts": "x();\n" });
    assert.equal(run("--config-ref", lateMain, lateMain, late).status, 0);
    // A branch from before the announcement does not have it yet: not this change's to keep.
    git("checkout", "-q", "-f", "-B", "behind", main + "~1");
    const behind = withList({ "fork-own.ts": "x();\n" }, { files: SEAM, pending: {} });
    const result = run("--config-ref", main, main + "~1", behind);
    assert.doesNotMatch(result.stderr, /drops the announced line/);
  });
});

// A seam file with a "lost" entry may get upstream's lines back. Each line the pull
// request's own "lost" lowers allows one put-back line, and the run from upstream holds that entry
// to the head's lost lines exactly, so a fork line added in its place is refused there.
test('a pull request that lowers a seam file\'s "lost" entry may put those upstream lines back', () => {
  withRepo(({ git, commit, run, fromUpstream, publish }) => {
    const UPSTREAM_P = "a();\nb(x);\nc();\nif (y) {\n  d();\n}\n";
    const upstream = commit({ "p.ts": UPSTREAM_P }, "upstream");
    git("checkout", "-q", "-b", "fork");
    const entry = (...added) => ({ "p.ts": { why: "w", added } });
    const list = (added, lost) => ({ files: entry(...added), pending: {}, lost });
    // The fork changed b(x) (1 line gone) and re-indented d() (moved, its text kept).
    const main = commit(
      {
        ...seams(list(["b(x, fork);", "forkHook();"], { "p.ts": { lines: 2, gone: 1 } })),
        "p.ts": "a();\nb(x, fork);\nc();\nif (y) {\n    d();\n}\nforkHook();\n",
      },
      "main",
    );
    publish(upstream);
    assert.equal(fromUpstream(upstream, main).status, 0);
    const pr = (name, own, text) => {
      git("checkout", "-q", "-f", "-B", name, main);
      return commit({ ...seams(own), "p.ts": text }, name);
    };
    const passes = (head) => {
      for (const args of [
        [main, head],
        ["--no-upstream", main, head],
      ]) {
        const result = run(...args);
        assert.equal(result.status, 0, `${args.join(" ")}: ${result.stderr}`);
      }
      assert.equal(fromUpstream(upstream, head).status, 0);
    };
    // Both lines back, "lost" dropped; only b(x) back, "lost" lowered to the re-indented line.
    passes(pr("both", list(["forkHook();"], {}), `${UPSTREAM_P}forkHook();\n`));
    passes(
      pr(
        "gone",
        list(["forkHook();"], { "p.ts": { lines: 1, gone: 0 } }),
        "a();\nb(x);\nc();\nif (y) {\n    d();\n}\nforkHook();\n",
      ),
    );
    // Half-way: the lines are back but "lost" is main's. Refused as before, naming the way out.
    const halfway = run(
      main,
      pr(
        "halfway",
        list(["forkHook();"], { "p.ts": { lines: 2, gone: 1 } }),
        `${UPSTREAM_P}forkHook();\n`,
      ),
    );
    assert.equal(halfway.status, 1);
    assert.match(
      halfway.stderr,
      /- p\.ts: adds `b\(x\);` \(if it puts back an upstream line counted in "lost", lower that entry in this change's \.github\/fork-seams\.json\)/,
    );
    assert.match(
      halfway.stderr,
      /- p\.ts: 1 upstream line deleted, moved or re-indented by this change/,
    );
    // "lost" lowered, but a fork line in place of upstream's: main's tree cannot tell, upstream can.
    const swap = run(
      main,
      pr(
        "swap",
        list(["forkHook();"], {}),
        "a();\nforkZ();\nc();\nif (y) {\n  d();\n}\nforkHook();\n",
      ),
    );
    assert.equal(swap.status, 1, swap.stdout);
    assert.match(
      swap.stderr,
      /"added" lists `forkZ\(\);` not at all, but the fork adds it over upstream/,
    );
    assert.match(
      swap.stderr,
      /- p\.ts: dropped from "lost" .*\(no entry\), but the fork loses 1 upstream line over upstream \([0-9a-f]{12}\) .*1 of them gone/,
    );
    // A line put back in place lowers both counts: no room to move a line too (a moved seam line
    // looks like upstream's, so upstream's count would not see it).
    const moved = run(
      main,
      pr(
        "moved",
        list(["forkHook();"], { "p.ts": { lines: 1, gone: 0 } }),
        "forkHook();\na();\nb(x);\nc();\nif (y) {\n    d();\n}\n",
      ),
    );
    assert.equal(moved.status, 1, moved.stdout);
    assert.match(
      moved.stderr,
      /- p\.ts: 1 upstream line deleted, moved or re-indented by this change/,
    );
    // Raising one count while lowering the other allows nothing.
    const raised = run(
      main,
      pr(
        "raised",
        list(["forkHook();"], { "p.ts": { lines: 3, gone: 0 } }),
        "a();\nb(x);\nc();\nif (y) {\n    d();\n}\nforkHook();\n",
      ),
    );
    assert.equal(raised.status, 1);
    assert.match(raised.stderr, /- p\.ts: adds `b\(x\);`/);
  });
});

// A line put back where upstream has it is in order again, so it lowers "lost" `lines` with `gone`.
// One put back elsewhere lowers `gone` alone: a fork change to the order of upstream's lines, which
// the count from upstream (exact either way) cannot see, so the pull request's run refuses it.
test('a line put back out of place is refused, though "lost" is lowered to its exact count', () => {
  withRepo(({ git, commit, run, fromUpstream, publish }) => {
    const UPSTREAM_Q = "lock();\nwork();\nunlock();\n";
    const upstream = commit({ "q.ts": UPSTREAM_Q }, "upstream");
    git("checkout", "-q", "-b", "fork");
    const list = (lost) => ({ files: { "q.ts": { why: "w", added: [] } }, pending: {}, lost });
    // The fork dropped unlock(): 1 line not in order, its text gone.
    const main = commit(
      { ...seams(list({ "q.ts": { lines: 1, gone: 1 } })), "q.ts": "lock();\nwork();\n" },
      "main",
    );
    publish(upstream);
    assert.equal(fromUpstream(upstream, main).status, 0);
    const pr = (name, own, text) => {
      git("checkout", "-q", "-f", "-B", name, main);
      return commit({ ...seams(own), "q.ts": text }, name);
    };
    const runs = (head) => [run(main, head), run("--no-upstream", main, head)];
    // In place: "lost" dropped, all three runs pass.
    const inPlace = pr("in-place", list({}), UPSTREAM_Q);
    for (const result of runs(inPlace)) assert.equal(result.status, 0, result.stderr);
    assert.equal(fromUpstream(upstream, inPlace).status, 0);
    // Out of place: {"lines": 1, "gone": 0} is the head's exact count (fork-follow passes it), but
    // unlock() now runs before work().
    const outOfPlace = pr(
      "out-of-place",
      list({ "q.ts": { lines: 1, gone: 0 } }),
      "lock();\nunlock();\nwork();\n",
    );
    assert.equal(fromUpstream(upstream, outOfPlace).status, 0);
    for (const result of runs(outOfPlace)) {
      assert.equal(result.status, 1, result.stdout);
      assert.match(
        result.stderr,
        /- q\.ts: adds `unlock\(\);` \(this change's \.github\/fork-seams\.json lowers its "lost" gone by 1 but lines by only 0: put each upstream line back where upstream has it, in order, which lowers both\)/,
      );
    }
  });
});

// Only an own "lost" entry the pull request edits from its merge-base's list is counted from
// upstream, so only that one allows lines back: an entry left as the merge-base's, below main's
// newer one, is not checked by anyone and allows nothing.
test("an own \"lost\" entry left as the merge-base's allows no line back, though main's is higher", () => {
  withRepo(({ git, commit, run, fromUpstream, publish }) => {
    const upstream = commit({ "p.ts": "a();\nb();\nc();\n" }, "upstream");
    git("checkout", "-q", "-b", "fork");
    const list = (lost) => ({
      files: { "p.ts": { why: "w", added: ["b(fork);"] } },
      pending: {},
      lost,
    });
    const before = commit(
      { ...seams(list({ "p.ts": { lines: 1, gone: 1 } })), "p.ts": "a();\nb(fork);\nc();\n" },
      "fork changes b",
    );
    // Main drops c() after the branch's merge-base: its entry rises to {2, 2}.
    const main = commit(
      { ...seams(list({ "p.ts": { lines: 2, gone: 2 } })), "p.ts": "a();\nb(fork);\n" },
      "main",
    );
    publish(upstream);
    assert.equal(fromUpstream(upstream, main).status, 0);
    // The branch keeps its merge-base's list ({1, 1}, below main's) and adds only a fork line.
    git("checkout", "-q", "-f", "-B", "unedited", before);
    const head = commit({ "p.ts": "a();\nb(fork);\nc();\nforkZ();\n" }, "unedited");
    for (const args of [
      [main, head],
      ["--no-upstream", main, head],
    ]) {
      const result = run(...args);
      assert.equal(result.status, 1, `${args.join(" ")}: ${result.stdout}`);
      assert.match(
        result.stderr,
        /- p\.ts: adds `forkZ\(\);` \(if it puts back an upstream line counted in "lost", lower that entry in this change's \.github\/fork-seams\.json\) \(this branch's merge-base has another "lost" entry for it than main: update the branch from main, whose list changed since\)/,
      );
    }
  });
});

// Main lowered a "lost" entry since the branch's merge-base (another change put a line back): the
// branch's own entry, counted from its merge-base's, gets no room or no longer matches its head over
// upstream. Both refusals name the way out: update the branch from main.
test('a "lost" entry main changed since the merge-base names updating the branch', () => {
  withRepo(({ git, commit, run, publish }) => {
    const upstream = commit({ "p.ts": "a();\nb();\nc();\nd();\n" }, "upstream");
    git("checkout", "-q", "-b", "fork");
    const list = (lost) => ({
      files: { "p.ts": { why: "w", added: ["b(fork);"] } },
      pending: {},
      lost,
    });
    const before = commit(
      { ...seams(list({ "p.ts": { lines: 3, gone: 3 } })), "p.ts": "a();\nb(fork);\n" },
      "fork changes b, drops c and d",
    );
    const main = commit(
      { ...seams(list({ "p.ts": { lines: 2, gone: 2 } })), "p.ts": "a();\nb(fork);\nc();\n" },
      "main puts c back",
    );
    publish(upstream);
    const update =
      / \(this branch's merge-base has another "lost" entry for it than main: update the branch from main, whose list changed since\)/;
    const pr = (name, lost) => {
      git("checkout", "-q", "-f", "-B", name, before);
      return commit({ ...seams(list(lost)), "p.ts": "a();\nb(fork);\nd();\n" }, name);
    };
    // The branch puts d() back and lowers its merge-base's {3, 3} to main's {2, 2}: no room.
    const same = run(main, pr("as-main", { "p.ts": { lines: 2, gone: 2 } }));
    assert.equal(same.status, 1, same.stdout);
    assert.match(
      same.stderr,
      new RegExp(
        `- p\\.ts: adds \`d\\(\\);\` \\(if it puts back an upstream line counted in "lost", lower that entry in this change's \\.github/fork-seams\\.json\\)${update.source}`,
      ),
    );
    // It lowers main's {2, 2} by one: room for d(), but its head loses 2 over upstream (c() too).
    const lowered = run(main, pr("below-main", { "p.ts": { lines: 1, gone: 1 } }));
    assert.equal(lowered.status, 1, lowered.stdout);
    assert.doesNotMatch(lowered.stderr, /adds `d\(\);`/);
    assert.match(
      lowered.stderr,
      new RegExp(
        `- p\\.ts: set in "lost" .*set it to \\{"lines": 2, "gone": 2\\} \\(a count above the lines lost is room to swap one for a fork line later\\)${update.source}`,
      ),
    );
  });
});

// A line moved or re-indented back with "lost" left as main's is refused, naming the way out.
test('a line re-indented back with "lost" left as main\'s is refused, naming the entry to lower', () => {
  withRepo(({ git, commit, run, fromUpstream, publish }) => {
    const UPSTREAM_R = "if (y) {\n  d();\n}\n";
    const upstream = commit({ "r.ts": UPSTREAM_R }, "upstream");
    git("checkout", "-q", "-b", "fork");
    const list = (lost) => ({ files: { "r.ts": { why: "w", added: [] } }, pending: {}, lost });
    const main = commit(
      { ...seams(list({ "r.ts": { lines: 1, gone: 0 } })), "r.ts": "if (y) {\n    d();\n}\n" },
      "main",
    );
    publish(upstream);
    assert.equal(fromUpstream(upstream, main).status, 0);
    const pr = (name, own) => {
      git("checkout", "-q", "-f", "-B", name, main);
      return commit({ ...seams(own), "r.ts": UPSTREAM_R }, name);
    };
    const kept = run(main, pr("kept", list({ "r.ts": { lines: 1, gone: 0 } })));
    assert.equal(kept.status, 1, kept.stdout);
    assert.match(
      kept.stderr,
      /- r\.ts: 1 upstream line deleted, moved or re-indented by this change \(.*\) \(if it moves or re-indents back an upstream line counted in "lost", lower that entry in this change's \.github\/fork-seams\.json\)\./,
    );
    const dropped = run(main, pr("dropped", list({})));
    assert.equal(dropped.status, 0, dropped.stderr);
  });
});
