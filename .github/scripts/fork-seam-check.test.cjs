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
const unlisted = (config, base, head, upstream, isForkFile = forkFileTest(config.forkFiles)) =>
  findUnlistedChanges(
    { files: {}, pending: [], forkFiles: [], ...config },
    {
      changed: [...new Set([...Object.keys(base), ...Object.keys(head)])],
      atBase: (file) => base[file],
      atHead: (file) => head[file],
      upstream,
      isForkFile,
    },
  );

test("an upstream file with no entry refuses every fork line, from upstream and from main", () => {
  const head = { "u.ts": `${UP}export const forkLine = 2;\n` };
  for (const upstream of [true, false]) {
    const { reasons } = unlisted({}, { "u.ts": UP }, head, upstream);
    assert.equal(reasons.length, 1);
    assert.match(reasons[0], /^u\.ts: upstream file with 1 fork line but no entry/);
  }
  // Unchanged, only re-indented, or only losing lines: nothing added.
  for (const after of [UP, `  ${UP}\n\n`, ""]) {
    assert.deepEqual(unlisted({}, { "u.ts": UP }, { "u.ts": after }, false).reasons, []);
  }
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

test("a pending file: noted from upstream, and on a pull request it may change but not grow", () => {
  const config = { pending: ["p.ts"] };
  const main = { "p.ts": `${UP}forkA();\nforkB();\n` };
  assert.deepEqual(unlisted(config, { "p.ts": UP }, main, true), { reasons: [], notes: [] });
  // From upstream, a pending file with no fork lines left is a note to take it off the list.
  assert.match(
    unlisted(config, { "p.ts": UP }, { "p.ts": UP }, true).notes[0],
    /^p\.ts: "pending" with no/,
  );
  const swap = { "p.ts": main["p.ts"].replace("forkB();", "forkC();") };
  const less = { "p.ts": `${UP}forkA();\n` };
  for (const head of [swap, less, {}])
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
});

test("a malformed config throws instead of guarding nothing", () => {
  for (const bad of [
    {},
    { files: [] },
    { files: { "a.ts": { added: [] } } },
    { files: { "a.ts": { why: "x", added: "y" } } },
    { files: {}, pending: "a.ts" },
    { files: {}, pending: [""] },
    { files: {}, forkFiles: [1] },
    { files: SEAM, pending: ["a.ts"] },
    ...["/abs", "../up", "**", "*", "!", "a/../b"].map((pattern) => ({
      files: {},
      forkFiles: [pattern],
    })),
  ]) {
    assert.throws(() => parseConfig(JSON.stringify(bad)), JSON.stringify(bad));
  }
  assert.deepEqual(parseConfig(JSON.stringify({ files: SEAM })), {
    files: SEAM,
    pending: [],
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
        ...seams({ files: SEAM, pending: ["p.ts"] }),
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
