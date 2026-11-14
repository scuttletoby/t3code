const assert = require("node:assert/strict");
const { execFileSync, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { findUpstreamFileChanges, parseList } = require("./fork-upstream-files-check.cjs");
const { CHECK_FILES } = require("./fork-schema-check.cjs");

const SCRIPT = path.join(__dirname, "fork-upstream-files-check.cjs");
const REPO_ROOT = path.join(__dirname, "..", "..");
const WORKFLOW = path.join(REPO_ROOT, ".github", "workflows", "fork-checks.yml");

// The list is one config line in the workflow; the tests read it there, so they follow it.
const CONFIG_LINE = /^ +UPSTREAM_FILES: (.+)$/m;
const configured = (workflow) => parseList([workflow.match(CONFIG_LINE)?.[1] ?? ""]);
const LISTED = configured(fs.readFileSync(WORKFLOW, "utf8"));
const OTHER = "apps/server/src/feature/Store.ts";

// --- The judgement, on fake trees -------------------------------------------------------------

const entries = (files) => (file) => files[file];
const judge = (changed, base, head) =>
  findUpstreamFileChanges(["a.ts", "b.json"], changed, {
    atBase: entries(base),
    atHead: entries(head),
  });
const BASE = { "a.ts": "100644 blob 1", "b.json": "100644 blob 2" };

test("parseList splits the config line on any whitespace and drops repeats", () => {
  assert.deepEqual(parseList(["  a.ts\tb.json  a.ts\n"]), ["a.ts", "b.json"]);
  assert.deepEqual(parseList(["a.ts", "b.json"]), ["a.ts", "b.json"]);
  assert.deepEqual(parseList([""]), []);
});

test("an edit, a delete and a mode change of a listed file are refused, each named", () => {
  const reasons = judge(["a.ts", "b.json"], BASE, { "a.ts": "100755 blob 1" });
  assert.deepEqual(reasons, [
    "a.ts: this branch changes its file mode",
    "b.json: this branch deletes or renames it",
  ]);
  assert.deepEqual(judge(["a.ts"], BASE, { ...BASE, "a.ts": "100644 blob 9" }), [
    "a.ts: this branch changes it",
  ]);
});

test("a branch that carries main's copy of a listed file is not refused", () => {
  assert.deepEqual(judge(["a.ts"], BASE, BASE), []);
});

test("a listed file main does not have fails closed, whatever the branch does", () => {
  const reasons = judge([], { "a.ts": BASE["a.ts"] }, BASE);
  assert.equal(reasons.length, 1);
  assert.match(reasons[0], /^b\.json: listed, but main does not have it/);
});

// --- The workflow -----------------------------------------------------------------------------

test("the config line lists the upstream files, and each one exists in this repository", () => {
  assert.deepEqual(LISTED, [
    "apps/server/src/orchestration-v2/Orchestrator.ts",
    "apps/server/src/provider/agentContextBudget.json",
  ]);
  for (const file of LISTED) assert.ok(fs.existsSync(path.join(REPO_ROOT, file)), file);
});

// Why the workflow does not run the check as intended; empty when it does.
function workflowProblems(workflow) {
  const problems = [];
  const expect = (ok, why) => ok || problems.push(why);
  const step = workflow.match(
    /^ {6}- name: Refuse changes to upstream files[^\n]*\n((?: {8}.*\n|\n)+)/m,
  );
  expect(step, "no step runs the upstream files check");
  const body = step?.[1] ?? "";
  expect(
    /^ {8}run: node \.github\/scripts\/fork-upstream-files-check\.cjs "\$BASE_SHA" "\$HEAD_SHA" "\$UPSTREAM_FILES"$/m.test(
      body,
    ),
    "the step does not run the check on $BASE_SHA $HEAD_SHA with the list",
  );
  expect(CONFIG_LINE.test(body), "the list is not the step's UPSTREAM_FILES line");
  expect(
    /^ {8}if: \$\{\{ !cancelled\(\) \}\}$/m.test(body),
    "the step is skipped when the schema step refuses",
  );
  expect(
    /^ {10}BASE_SHA: \$\{\{ github\.event\.pull_request\.base\.sha \}\}$/m.test(body) &&
      /^ {10}HEAD_SHA: \$\{\{ github\.event\.pull_request\.head\.sha \}\}$/m.test(body),
    "the step does not judge the pull request's base and head",
  );
  expect(
    /run: node --test .*fork-upstream-files-check\.test\.cjs/.test(workflow),
    "the workflow does not run these tests",
  );
  // The step must be in the guarded job, not a new job or workflow.
  const jobs = workflow.split(/^ {2}(?=\w+:\n)/m);
  const job = jobs.find((part) => part.includes('fork-upstream-files-check.cjs "'));
  expect(job?.startsWith("no_schema_changes:"), "the step is not in the no_schema_changes job");
  return problems;
}

test("the workflow runs the check in the schema job, on the PR's base and head, with the list", () => {
  assert.deepEqual(workflowProblems(fs.readFileSync(WORKFLOW, "utf8")), []);
});

test("a workflow that drops, skips or mis-feeds the check fails the contract", () => {
  const workflow = fs.readFileSync(WORKFLOW, "utf8");
  const mutants = {
    "no list": workflow.replace(CONFIG_LINE, ""),
    "no if": workflow.replace(/^ {8}if: \$\{\{ !cancelled\(\) \}\}\n/m, ""),
    "base for head": workflow.replace(
      /"\$BASE_SHA" "\$HEAD_SHA" "\$UPSTREAM_FILES"/,
      '"$HEAD_SHA" "$HEAD_SHA" "$UPSTREAM_FILES"',
    ),
    "no tests": workflow.replace(" .github/scripts/fork-upstream-files-check.test.cjs", ""),
    "no step": workflow.replace(
      /^ {6}- name: Refuse changes to upstream files[^\n]*\n(?: {8}.*\n)+/m,
      "",
    ),
  };
  for (const [label, text] of Object.entries(mutants)) {
    assert.notEqual(text, workflow, label);
    assert.notDeepEqual(workflowProblems(text), [], label);
  }
});

test("the check's own files are guarded by the schema check (a PR cannot weaken it)", () => {
  for (const file of [SCRIPT, `${SCRIPT.replace(/\.cjs$/, "")}.test.cjs`, WORKFLOW]) {
    assert.ok(CHECK_FILES.includes(path.relative(REPO_ROOT, file)), file);
  }
});

// --- End to end: the real git in a scratch repository, the way CI calls the check -------------

function scratchRepo() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fork-upstream-files-check-"));
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
  const commit = (files, message = "change") => {
    for (const [file, text] of Object.entries(files)) {
      const target = path.join(dir, file);
      if (text === null) {
        fs.rmSync(target);
        continue;
      }
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(target, text);
    }
    git("add", "-A");
    git("commit", "-q", "--allow-empty", "-m", message);
    return git("rev-parse", "HEAD");
  };
  git("init", "-q", "-b", "main");
  const check = (base, head, list = LISTED.join(" ")) => {
    const result = spawnSync(process.execPath, [SCRIPT, base, head, list], {
      cwd: dir,
      env,
      encoding: "utf8",
    });
    return { code: result.status, output: result.stdout + result.stderr };
  };
  const done = () => fs.rmSync(dir, { recursive: true, force: true });
  return { dir, git, commit, check, done };
}

const UPSTREAM_TREE = {
  [LISTED[0]]: "export const orchestrator = 1;\n",
  [LISTED[1]]: '{ "budget": 1 }\n',
  [OTHER]: "export const x = 1;\n",
};

function prAgainstBase(files) {
  const repo = scratchRepo();
  try {
    const base = repo.commit(UPSTREAM_TREE, "upstream");
    const head = repo.commit(files, "pr");
    return repo.check(base, head);
  } finally {
    repo.done();
  }
}

for (const file of LISTED) {
  test(`e2e: a pull request that edits ${path.basename(file)} is refused, naming it`, () => {
    const result = prAgainstBase({ [file]: `${UPSTREAM_TREE[file]}// fork\n` });
    assert.equal(result.code, 1, result.output);
    assert.ok(result.output.includes(`- ${file}: this branch changes it`), result.output);
    assert.match(result.output, /the owner merges past it/);
  });

  test(`e2e: deleting or renaming ${path.basename(file)} is refused`, () => {
    const deleted = prAgainstBase({ [file]: null });
    assert.equal(deleted.code, 1, deleted.output);
    assert.ok(
      deleted.output.includes(`- ${file}: this branch deletes or renames it`),
      deleted.output,
    );
    const repo = scratchRepo();
    try {
      const base = repo.commit(UPSTREAM_TREE, "upstream");
      repo.git("mv", file, `${file}.moved`);
      const renamed = repo.check(base, repo.commit({}, "rename"));
      assert.equal(renamed.code, 1, renamed.output);
      assert.ok(
        renamed.output.includes(`- ${file}: this branch deletes or renames it`),
        renamed.output,
      );
    } finally {
      repo.done();
    }
  });
}

test("e2e: a mode change of a listed file is refused", () => {
  const repo = scratchRepo();
  try {
    const base = repo.commit(UPSTREAM_TREE, "upstream");
    repo.git("update-index", "--chmod=+x", LISTED[0]);
    repo.git("commit", "-q", "-m", "chmod");
    const result = repo.check(base, repo.git("rev-parse", "HEAD"));
    assert.equal(result.code, 1, result.output);
    assert.ok(
      result.output.includes(`- ${LISTED[0]}: this branch changes its file mode`),
      result.output,
    );
  } finally {
    repo.done();
  }
});

test("e2e: an unrelated pull request passes and says what it checked; a rerun says the same", () => {
  const repo = scratchRepo();
  try {
    const base = repo.commit(UPSTREAM_TREE, "upstream");
    const head = repo.commit({ [OTHER]: "export const x = 2;\n", "README.md": "fork\n" }, "pr");
    const first = repo.check(base, head);
    assert.equal(first.code, 0, first.output);
    assert.match(
      first.output,
      /No upstream file changes \(2 listed files, 2 changed files checked\)/,
    );
    assert.deepEqual(repo.check(base, head), first);
  } finally {
    repo.done();
  }
});

test("e2e: an empty pull request (head is the base) passes", () => {
  const repo = scratchRepo();
  try {
    const base = repo.commit(UPSTREAM_TREE, "upstream");
    const result = repo.check(base, base);
    assert.equal(result.code, 0, result.output);
    assert.match(result.output, /0 changed files checked/);
  } finally {
    repo.done();
  }
});

test("e2e: a branch that edits a listed file and then reverts the edit passes", () => {
  const repo = scratchRepo();
  try {
    const base = repo.commit(UPSTREAM_TREE, "upstream");
    repo.commit({ [LISTED[0]]: "// tried\n" }, "try");
    const head = repo.commit({ [LISTED[0]]: UPSTREAM_TREE[LISTED[0]] }, "revert");
    passes(repo.check(base, head));
  } finally {
    repo.done();
  }
});

test("e2e: the list is read from the arguments, so a new path is guarded once it is listed", () => {
  const repo = scratchRepo();
  try {
    const base = repo.commit(UPSTREAM_TREE, "upstream");
    const head = repo.commit({ [OTHER]: "export const x = 2;\n" }, "pr");
    passes(repo.check(base, head));
    const listed = repo.check(base, head, `${LISTED.join(" ")} ${OTHER}`);
    assert.equal(listed.code, 1, listed.output);
    assert.ok(listed.output.includes(`- ${OTHER}: this branch changes it`), listed.output);
    const none = repo.check(base, head, "  ");
    assert.equal(none.code, 2, none.output);
    assert.match(none.output, /Usage:/);
  } finally {
    repo.done();
  }
});

test("e2e: a listed file main no longer has fails every pull request, naming the list", () => {
  const repo = scratchRepo();
  try {
    const base = repo.commit(UPSTREAM_TREE, "upstream");
    const head = repo.commit({ [OTHER]: "export const x = 2;\n" }, "pr");
    const result = repo.check(base, head, `${LISTED.join(" ")} apps/server/src/Gone.ts`);
    assert.equal(result.code, 1, result.output);
    assert.match(result.output, /Gone\.ts: listed, but main does not have it/);
    assert.match(result.output, /fork-checks\.yml/);
  } finally {
    repo.done();
  }
});

// Upstream changes a listed file; fork-follow rebases the fork's series onto it and moves main.
function upstreamSync() {
  const repo = scratchRepo();
  repo.commit({ ...UPSTREAM_TREE, [LISTED[0]]: "export const orchestrator = 0;\n" }, "upstream v0");
  const upstreamOld = repo.commit(UPSTREAM_TREE, "upstream v1");
  const series = repo.commit({ "fork/feature.ts": "export {};\n" }, "fork: feature");
  repo.git("branch", "old-main", series);
  repo.git("checkout", "-q", "-b", "upstream", upstreamOld);
  const upstreamNew = repo.commit(
    { [LISTED[0]]: "export const orchestrator = 2;\n", [LISTED[1]]: '{ "budget": 2 }\n' },
    "upstream v2",
  );
  repo.git("checkout", "-q", "-b", "new-main", upstreamNew);
  repo.git("cherry-pick", series);
  const newMain = repo.git("rev-parse", "HEAD");
  return { repo, upstreamOld, upstreamNew, series, newMain };
}
const PASSED = /No upstream file changes \(2 listed files, \d+ changed files checked\)/;
function passes(result) {
  assert.equal(result.code, 0, result.output);
  assert.match(result.output, PASSED);
}

test("e2e: an upstream sync stays green: fork-follow's series and a PR on the new main", () => {
  const { repo, upstreamNew, newMain } = upstreamSync();
  try {
    // fork-follow judges the series against the upstream release it sits on.
    passes(repo.check(upstreamNew, newMain));
    // A new pull request on the moved main.
    repo.git("checkout", "-q", "-b", "pr", newMain);
    const pr = repo.commit({ [OTHER]: "export const x = 2;\n" }, "pr");
    passes(repo.check(newMain, pr));
  } finally {
    repo.done();
  }
});

test("e2e: a PR whose base moved (main rebased onto a newer upstream) is not blamed for upstream", () => {
  const { repo, upstreamNew, series, newMain } = upstreamSync();
  try {
    // A pull request opened from the old main, before the sync.
    repo.git("checkout", "-q", "-b", "pr", series);
    const old = repo.commit({ [OTHER]: "export const x = 2;\n" }, "pr");
    passes(repo.check(newMain, old));
    // The same branch takes upstream's change another way (a cherry-pick, not a rebase onto main):
    // its merge-base with main stays old, but it carries main's copies, so it changes nothing.
    repo.git("cherry-pick", upstreamNew);
    passes(repo.check(newMain, repo.git("rev-parse", "HEAD")));
    // A real edit is still refused against the moved base.
    const edit = repo.commit({ [LISTED[1]]: '{ "budget": 3 }\n' }, "edit budget");
    const refused = repo.check(newMain, edit);
    assert.equal(refused.code, 1, refused.output);
    assert.ok(refused.output.includes(`- ${LISTED[1]}: this branch changes it`), refused.output);
  } finally {
    repo.done();
  }
});

test("e2e: a branch behind a main that moved forward normally is judged on its own edits", () => {
  const repo = scratchRepo();
  try {
    const base = repo.commit(UPSTREAM_TREE, "fork main");
    repo.git("checkout", "-q", "-b", "pr");
    const unrelated = repo.commit({ "README.md": "notes\n" }, "docs");
    repo.git("checkout", "-q", "main");
    const moved = repo.commit({ [OTHER]: "export const x = 9;\n" }, "fork: more");
    passes(repo.check(moved, unrelated));
    repo.git("checkout", "-q", "pr");
    const edit = repo.commit({ [LISTED[0]]: "// fork\n" }, "edit");
    assert.equal(repo.check(moved, edit).code, 1);
    assert.equal(repo.check(base, edit).code, 1);
  } finally {
    repo.done();
  }
});
