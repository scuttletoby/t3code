const assert = require("node:assert/strict");
const { execFileSync, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  CONFIG,
  compileForkPattern,
  findHiddenUpstreamFiles,
  findSeamBreaches,
  findUnlistedChanges,
  forkFileTest,
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
  // Unchanged, re-indented, blank lines or CRLF line ends: no change to judge.
  for (const after of [UP, `  ${UP}\n\n`, UP.replace("\n", "\r\n")]) {
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
      assert.match(reasons[0], /^u\.ts: upstream file whose lines this change deletes or moves/);
    }
  }
  assert.equal(sameLines("a b\n\nc\n", "  ab\r\nc"), true);
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
  assert.deepEqual(unlisted(config, { "p.ts": UP }, main, true), { reasons: [], notes: [] });
  // From upstream, a pending file with no fork lines left, or fewer than its ceiling, is refused:
  // a ceiling left too high is room to swap upstream lines for fork lines later, and a note in
  // the nightly log is never seen.
  assert.match(
    unlisted(config, { "p.ts": UP }, { "p.ts": UP }, true).reasons[0],
    /^p\.ts: "pending" with no fork lines left/,
  );
  assert.deepEqual(unlisted(config, { "p.ts": UP }, { "p.ts": `${UP}forkA();\n` }, true), {
    reasons: ['p.ts: "pending" with 1 fork lines, fewer than its ceiling of 2; lower it to 1.'],
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

// Round-2 state lens on #277: PR 1 moves a fork line out and the ceiling stays; PR 2 swaps an
// upstream guard for a fork line (net zero on the PR). From upstream that must be refused.
test("from upstream, a ceiling left too high after lines moved out is refused, so a later swap cannot use it", () => {
  const upstreamText = `${UP}if (!ok) return;\n`;
  const config = { pending: { "p.ts": 2 } };
  const afterMoveOut = `${upstreamText}forkA();\n`;
  assert.deepEqual(unlisted(config, { "p.ts": upstreamText }, { "p.ts": afterMoveOut }, true).reasons, [
    'p.ts: "pending" with 1 fork lines, fewer than its ceiling of 2; lower it to 1.',
  ]);
  // (So the nightly stops right after PR 1, before a PR 2 could use the spare line.)
  // Exactly at the ceiling passes; one over is refused.
  const one = { pending: { "p.ts": 1 } };
  assert.deepEqual(unlisted(one, { "p.ts": upstreamText }, { "p.ts": afterMoveOut }, true), {
    reasons: [],
    notes: [],
  });
});

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
    ...["/abs", "../up", "**", "*", "!", "a/../b"].map((pattern) => ({
      files: {},
      forkFiles: [pattern],
    })),
  ]) {
    assert.throws(() => parseConfig(JSON.stringify(bad)), JSON.stringify(bad));
  }
  assert.deepEqual(parseConfig(JSON.stringify({ files: SEAM })), {
    files: SEAM,
    pending: {},
    forkFiles: [],
  });
});

// The real files: read from the working tree, or from HEAD in a sparse checkout that leaves them out.
const readReal = (file) =>
  fs.existsSync(path.join(REPO_ROOT, file))
    ? fs.readFileSync(path.join(REPO_ROOT, file), "utf8")
    : execFileSync("git", ["-C", REPO_ROOT, "show", `HEAD:${file}`], { encoding: "utf8" });
const REAL_CONFIG = parseConfig(readReal(CONFIG));
const REAL = REAL_CONFIG.files;

test("every listed seam line is in its file, so the list is not stale", () => {
  for (const [file, seam] of Object.entries(REAL)) {
    const lines = new Set(
      readReal(file)
        .split("\n")
        .map((line) => line.trim()),
    );
    for (const line of seam.added) assert.ok(lines.has(line.trim()), `${file}: ${line}`);
  }
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
    const reasons = judge(REAL, { [SETTINGS]: source }, { [SETTINGS]: head });
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
  const commit = (files, message) => {
    for (const [file, text] of Object.entries(files)) {
      const at = path.join(dir, file);
      if (text === null) fs.rmSync(at);
      else {
        fs.mkdirSync(path.dirname(at), { recursive: true });
        fs.writeFileSync(at, text);
      }
    }
    git("add", "-A");
    git("commit", "-q", "--allow-empty", "-m", message);
    return git("rev-parse", "HEAD").trim();
  };
  const run = (...args) => spawnSync("node", [SCRIPT, ...args], { cwd: dir, encoding: "utf8" });
  try {
    git("init", "-q");
    body({ dir, git, commit, run });
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

// Every upstream file on the command line, from upstream (no list there: the series' own list judges it) and
// from main with a list that predates "pending" and "forkFiles" (the first run after this change).
test("every upstream file counts on the command line, from upstream and from main", () => {
  withRepo(({ git, commit, run }) => {
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
    const refused = run(upstream, unlistedLine);
    assert.equal(refused.status, 1, refused.stdout);
    assert.match(refused.stderr, /- u\.ts: upstream file with 1 fork line but no entry/);
    const first = run(upstream, series);
    assert.equal(first.status, 0, first.stderr);
    assert.match(first.stdout, /every upstream file; list from the head/);
    // Runs again: the same answer.
    const again = run(upstream, series);
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
  withRepo(({ commit, run }) => {
    const upstream = commit({ "src/forkJoin.ts": UP, ...BASE }, "upstream");
    const series = commit(seams({ files: SEAM }), "fork series");
    const refused = run(upstream, series);
    assert.equal(refused.status, 1, refused.stdout);
    assert.match(
      refused.stderr,
      /- src\/forkJoin\.ts: upstream has it, but .* "!src\/forkJoin\.ts"/,
    );
    const fixed = commit(seams({ files: SEAM, forkFiles: ["!src/forkJoin.ts"] }), "exclude it");
    assert.equal(run(upstream, fixed).status, 0);
  });
});

// Both cases passed in both modes before.
test("a rename or delete cannot hide an upstream file, from upstream or on a pull request", () => {
  withRepo(({ dir, git, commit, run }) => {
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
        const result = run(base, head);
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
    assert.equal(run(upstream, dropped).status, 0);
  });
});

// A submodule pointer read as no file, so moving it passed.
test("a moved submodule pointer in an upstream path is a changed line", () => {
  withRepo(({ dir, git, commit, run }) => {
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
      const result = run(base, moved);
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
