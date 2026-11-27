// @effect-diagnostics nodeBuiltinImport:off - tests the plain node prepare script against real git checkouts.
/** Fork: the prepare before a delegated agent starts, on real git repositories. */
import * as NodeChildProcess from "node:child_process";
import * as NodeFS from "node:fs";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";

import { T3ProjectFileScript } from "@t3tools/contracts";
import { afterAll, assert, beforeAll, describe, it } from "@effect/vitest";
import { Schema } from "effect";

import {
  type PrepareOptions,
  parsePrepareConfig,
  RESULT_JSON_FILE,
  RESULT_TEXT_FILE,
  runPrepare,
  shellQuote,
} from "./delegatePrepare.ts";
import { parsePrepareArgs } from "./prepareCli.ts";

const IDENTITY = {
  GIT_AUTHOR_NAME: "Test Writer",
  GIT_AUTHOR_EMAIL: "writer@example.invalid",
  GIT_COMMITTER_NAME: "Test Writer",
  GIT_COMMITTER_EMAIL: "writer@example.invalid",
};
const saved: Record<string, string | undefined> = {};
let root = "";

beforeAll(() => {
  root = NodeFS.mkdtempSync(NodePath.join(NodeOS.tmpdir(), "fork-prepare-"));
  for (const [key, value] of Object.entries(IDENTITY)) {
    saved[key] = process.env[key];
    process.env[key] = value;
  }
});
afterAll(() => {
  for (const [key, value] of Object.entries(saved)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  NodeFS.rmSync(root, { recursive: true, force: true });
});

const git = (cwd: string, ...args: Array<string>) => {
  const run = NodeChildProcess.spawnSync("git", args, { cwd, encoding: "utf8" });
  if (run.status !== 0) throw new Error(`git ${args.join(" ")}: ${run.stderr}`);
  return run.stdout.trim();
};
const write = (dir: string, file: string, text: string) => {
  NodeFS.mkdirSync(NodePath.dirname(NodePath.join(dir, file)), { recursive: true });
  NodeFS.writeFileSync(NodePath.join(dir, file), text);
};
const commit = (dir: string, files: Record<string, string>, message: string) => {
  for (const [file, text] of Object.entries(files)) write(dir, file, text);
  git(dir, "add", "-A");
  git(dir, "commit", "--quiet", "-m", message);
};

/** A test script the configs run: exits 1 when a named file holds RED; writes what it was given. */
const CHECK_SCRIPT = `const fs = require("node:fs");
const files = process.argv.slice(2);
fs.writeFileSync("baseline-args.txt", files.join("\\n"));
const red = files.filter((file) => fs.readFileSync(file, "utf8").includes("RED"));
if (red.length > 0) { console.log("FAIL " + red.join(" ")); process.exit(1); }
console.log("ok " + files.length);
`;
const INSTALL_OK = `node -e "require('node:fs').writeFileSync('installed.txt', 'yes')"`;

let counter = 0;
/** An origin with main (its config, if any) and a work clone on a feature branch. */
const setup = (config: object | string | null) => {
  const name = `case${++counter}`;
  const origin = NodePath.join(root, `${name}-origin.git`);
  const seed = NodePath.join(root, `${name}-seed`);
  const work = NodePath.join(root, `${name}-work`);
  git(root, "init", "--quiet", "--bare", "--initial-branch=main", origin);
  git(root, "clone", "--quiet", origin, seed);
  git(seed, "symbolic-ref", "HEAD", "refs/heads/main");
  const files: Record<string, string> = {
    "check.cjs": CHECK_SCRIPT,
    "src/a.txt": "a\n",
    "src/shared.txt": "base\n",
    ".gitignore": "installed.txt\nbaseline-args.txt\nseen.json\n*.pid\n",
  };
  if (config !== null) {
    files[".t3code/prepare.json"] = typeof config === "string" ? config : JSON.stringify(config);
  }
  commit(seed, files, "main");
  git(seed, "push", "--quiet", "origin", "main");
  git(root, "clone", "--quiet", origin, work);
  git(work, "checkout", "--quiet", "-b", "feature");
  commit(work, { "src/feature.txt": "feature\n" }, "feature work");
  // main moves on after the branch started.
  commit(seed, { "src/later.txt": "later\n" }, "later on main");
  git(seed, "push", "--quiet", "origin", "main");
  return { origin, seed, work, gitDir: git(work, "rev-parse", "--absolute-git-dir") };
};
const prepare = (options: PrepareOptions) => runPrepare({ log: () => {}, ...options });
const readResult = (gitDir: string) =>
  JSON.parse(NodeFS.readFileSync(NodePath.join(gitDir, RESULT_JSON_FILE), "utf8"));
const CONFIG = { install: [INSTALL_OK], baseline: "node check.cjs {files}" };
const LOCK = "t3-prepare.lock";
const decodeScript = Schema.decodeUnknownSync(T3ProjectFileScript);
const clonePath = (name: string) => NodePath.join(root, "clones", `${name}${counter}`);
const readPid = (file: string) => Number.parseInt(NodeFS.readFileSync(file, "utf8"), 10);
/** Gone or a zombie: nothing of it runs any more (Linux). */
const isGone = (pid: number) => {
  try {
    return NodeFS.readFileSync(`/proc/${pid}/stat`, "utf8").split(") ")[1]?.startsWith("Z") ?? true;
  } catch {
    return true;
  }
};
const killQuietly = (pid: number) => {
  try {
    process.kill(pid, "SIGKILL");
  } catch {
    // Already gone.
  }
};

describe("runPrepare", () => {
  it("does nothing but report for a project with no prepare config", async () => {
    const { work, gitDir } = setup(null);
    const head = git(work, "rev-parse", "HEAD");
    const outcome = await prepare({ dir: work });
    assert.strictEqual(outcome.exitCode, 0);
    assert.strictEqual(outcome.result.status, "no-config");
    assert.strictEqual(outcome.result.merge, null);
    assert.strictEqual(outcome.result.baseline, null);
    assert.strictEqual(git(work, "rev-parse", "HEAD"), head, "main is not merged without a config");
    assert.isFalse(NodeFS.existsSync(NodePath.join(work, "installed.txt")));
    assert.include(outcome.text, "no project config");
    assert.strictEqual(readResult(gitDir).status, "no-config");
  });

  it("merges the base, installs and runs a green baseline on the touched files", async () => {
    const { work, gitDir } = setup(CONFIG);
    const outcome = await prepare({ dir: work, paths: ["src/a.txt", "missing.txt", "../outside"] });
    assert.strictEqual(outcome.exitCode, 0);
    assert.strictEqual(outcome.result.status, "prepared");
    assert.deepStrictEqual(outcome.result.merge, { status: "merged", base: "origin/main" });
    assert.isTrue(
      NodeFS.existsSync(NodePath.join(work, "src/later.txt")),
      "main's new commit is in",
    );
    assert.strictEqual(NodeFS.readFileSync(NodePath.join(work, "installed.txt"), "utf8"), "yes");
    const baseline = outcome.result.baseline;
    assert.strictEqual(baseline?.status, "green");
    // The branch's own change and the asked-for file that exists; not main's, not missing ones.
    assert.deepStrictEqual(
      NodeFS.readFileSync(NodePath.join(work, "baseline-args.txt"), "utf8").split("\n"),
      ["src/a.txt", "src/feature.txt"],
    );
    assert.strictEqual(outcome.result.branch, "feature");
    assert.include(outcome.text, "Prepared before you started");
    assert.include(outcome.text, "Baseline tests: green on 2 touched file(s)");
    assert.strictEqual(
      NodeFS.readFileSync(NodePath.join(gitDir, RESULT_TEXT_FILE), "utf8"),
      outcome.text,
    );
    assert.strictEqual(
      git(work, "status", "--porcelain"),
      "",
      "the result is not in the work tree",
    );
  });

  it("fails when an install command fails, and runs nothing after it", async () => {
    const { work, gitDir } = setup({
      install: [`node -e "process.exit(3)"`, INSTALL_OK],
      baseline: "node check.cjs {files}",
    });
    const outcome = await prepare({ dir: work });
    assert.strictEqual(outcome.exitCode, 1);
    assert.strictEqual(outcome.result.status, "failed");
    assert.strictEqual(outcome.result.failure?.step, "install");
    assert.include(outcome.result.failure?.message ?? "", "exited 3");
    assert.strictEqual(outcome.result.install.length, 1);
    assert.strictEqual(outcome.result.baseline, null);
    assert.isFalse(NodeFS.existsSync(NodePath.join(work, "installed.txt")));
    assert.isFalse(NodeFS.existsSync(NodePath.join(work, "baseline-args.txt")));
    assert.include(outcome.text, "Prepare FAILED at install");
    assert.strictEqual(readResult(gitDir).status, "failed");
  });

  it("stops a command at the time limit and counts it as failed", async () => {
    const { work } = setup({
      install: [`node -e "setTimeout(() => {}, 60000)"`],
      timeoutMinutes: 0.01,
    });
    const outcome = await prepare({ dir: work });
    assert.strictEqual(outcome.exitCode, 1);
    assert.isTrue(outcome.result.install[0]?.timedOut);
    assert.include(outcome.result.failure?.message ?? "", "stopped after 0.01 min");
  });

  it("reports a red baseline as found before the work, not as a failure", async () => {
    const { work } = setup(CONFIG);
    commit(work, { "src/feature.txt": "RED\n" }, "a test that was already red");
    const outcome = await prepare({ dir: work });
    assert.strictEqual(outcome.exitCode, 0);
    assert.strictEqual(outcome.result.status, "prepared");
    const baseline = outcome.result.baseline;
    assert.strictEqual(baseline?.status, "red");
    assert.include(baseline?.status === "red" ? baseline.tail : "", "FAIL src/feature.txt");
    assert.include(outcome.text, "Baseline tests: RED before any change (exit 1)");
  });

  it("hands the baseline touched file names with spaces and quotes intact", async () => {
    const { work } = setup(CONFIG);
    commit(
      work,
      {
        "src/it's a file.txt": "x\n",
        "src/café.txt": "x\n",
        "..notes.md": "x\n",
        "--reporter=evil.txt": "x\n",
        "a$&b.txt": "x\n",
        "q$'r$`s$$.txt": "x\n",
      },
      "odd names",
    );
    const outcome = await prepare({ dir: work });
    assert.strictEqual(outcome.result.baseline?.status, "green");
    // Non-ASCII and `..name` files are kept; a name like an option is never read as one.
    assert.deepStrictEqual(
      NodeFS.readFileSync(NodePath.join(work, "baseline-args.txt"), "utf8").split("\n"),
      [
        "./--reporter=evil.txt",
        "..notes.md",
        "a$&b.txt",
        "q$'r$`s$$.txt",
        "src/café.txt",
        "src/feature.txt",
        "src/it's a file.txt",
      ],
    );
    assert.strictEqual(shellQuote("a/b-c.ts"), "a/b-c.ts");
  });

  it("skips the baseline when there are no touched files", async () => {
    const { work } = setup(CONFIG);
    git(work, "reset", "--quiet", "--hard", "origin/main");
    const outcome = await prepare({ dir: work });
    assert.deepStrictEqual(outcome.result.baseline, {
      status: "skipped",
      reason: "no touched files",
    });
  });

  it("reports merge conflicts, undoes the merge and still installs", async () => {
    const { work, seed } = setup(CONFIG);
    commit(seed, { "src/shared.txt": "main side\n" }, "main edits shared");
    git(seed, "push", "--quiet", "origin", "main");
    commit(work, { "src/shared.txt": "branch side\n" }, "branch edits shared");
    const head = git(work, "rev-parse", "HEAD");
    const outcome = await prepare({ dir: work });
    assert.strictEqual(outcome.exitCode, 0);
    assert.deepStrictEqual(outcome.result.merge, {
      status: "conflicts",
      base: "origin/main",
      conflicts: ["src/shared.txt"],
    });
    assert.strictEqual(git(work, "rev-parse", "HEAD"), head);
    assert.strictEqual(git(work, "status", "--porcelain", "--untracked-files=no"), "");
    assert.isFalse(
      NodeFS.existsSync(NodePath.join(git(work, "rev-parse", "--absolute-git-dir"), "MERGE_HEAD")),
    );
    assert.isTrue(NodeFS.existsSync(NodePath.join(work, "installed.txt")));
    assert.include(outcome.text, "CONFLICTS in src/shared.txt. The merge was undone");
  });

  it("does not merge over local changes", async () => {
    const { work } = setup(CONFIG);
    write(work, "src/a.txt", "edited, not committed\n");
    const head = git(work, "rev-parse", "HEAD");
    const outcome = await prepare({ dir: work });
    assert.deepStrictEqual(outcome.result.merge, {
      status: "skipped",
      base: "origin/main",
      reason: "local changes here",
    });
    assert.strictEqual(git(work, "rev-parse", "HEAD"), head);
    assert.strictEqual(
      NodeFS.readFileSync(NodePath.join(work, "src/a.txt"), "utf8"),
      "edited, not committed\n",
    );
  });

  it("gives the same checkout when it runs again", async () => {
    const { work } = setup(CONFIG);
    const first = await prepare({ dir: work });
    const second = await prepare({ dir: work });
    assert.strictEqual(first.result.merge?.status, "merged");
    assert.strictEqual(second.result.merge?.status, "up-to-date");
    assert.strictEqual(second.result.head, first.result.head);
    assert.strictEqual(second.result.baseline?.status, "green");
  });

  it("marks the result running before any command, so an old result never looks current", async () => {
    const { work, gitDir } = setup({
      install: [
        `node -e "require('node:fs').copyFileSync('.git/${RESULT_JSON_FILE}', 'seen.json')"`,
      ],
    });
    // An older run's result, as a run stopped half-way would leave it.
    write(
      gitDir,
      RESULT_JSON_FILE,
      JSON.stringify({ status: "prepared", finishedAt: "2026-01-01" }),
    );
    await prepare({ dir: work });
    assert.strictEqual(readResult(gitDir).status, "prepared");
    const seen = JSON.parse(NodeFS.readFileSync(NodePath.join(work, "seen.json"), "utf8"));
    assert.strictEqual(seen.status, "running");
    assert.strictEqual(seen.finishedAt, null);
  });

  it("never undoes a merge someone left in progress", async () => {
    const { work, seed, gitDir } = setup(CONFIG);
    commit(seed, { "src/shared.txt": "main side\n" }, "main edits shared");
    git(seed, "push", "--quiet", "origin", "main");
    commit(work, { "src/shared.txt": "branch side\n" }, "branch edits shared");
    git(work, "fetch", "--quiet", "origin");
    NodeChildProcess.spawnSync("git", ["merge", "origin/main"], { cwd: work });
    assert.isTrue(NodeFS.existsSync(NodePath.join(gitDir, "MERGE_HEAD")));
    const outcome = await prepare({ dir: work });
    assert.strictEqual(outcome.exitCode, 1);
    assert.strictEqual(outcome.result.failure?.step, "checkout");
    assert.isTrue(
      NodeFS.existsSync(NodePath.join(gitDir, "MERGE_HEAD")),
      "the merge is still there",
    );
    assert.isFalse(NodeFS.existsSync(NodePath.join(work, "installed.txt")));
  });

  it("refuses a bad config, a typo included", async () => {
    const { work } = setup({ instal: [INSTALL_OK] });
    const outcome = await prepare({ dir: work });
    assert.strictEqual(outcome.exitCode, 1);
    assert.strictEqual(outcome.result.failure?.step, "config");
    assert.include(outcome.result.failure?.message ?? "", "unknown key(s) instal");
    const broken = await prepare({ dir: setup("{ not json").work });
    assert.strictEqual(broken.result.failure?.step, "config");
  });

  it("refuses a folder that is not a checkout", async () => {
    const dir = NodeFS.mkdtempSync(NodePath.join(root, "plain-"));
    const outcome = await prepare({ dir });
    assert.strictEqual(outcome.exitCode, 1);
    assert.strictEqual(outcome.result.failure?.step, "checkout");
  });

  it("clones, then reuses the clone, and switches branches", async () => {
    const { origin, work } = setup(CONFIG);
    git(work, "push", "--quiet", "origin", "feature");
    const dir = NodePath.join(root, "clones", `clone${counter}`);
    const fresh = await prepare({ dir, clone: origin, branch: "new-work" });
    assert.strictEqual(fresh.exitCode, 0);
    assert.strictEqual(fresh.result.branch, "new-work");
    assert.strictEqual(git(dir, "rev-parse", "HEAD"), git(dir, "rev-parse", "origin/main"));
    assert.deepStrictEqual(fresh.result.baseline, {
      status: "skipped",
      reason: "no touched files",
    });

    const again = await prepare({ dir, clone: origin, branch: "feature" });
    assert.strictEqual(again.exitCode, 0);
    assert.include(again.result.notes, "reused the existing clone");
    assert.strictEqual(again.result.branch, "feature");
    assert.strictEqual(
      git(dir, "rev-parse", "--abbrev-ref", "feature@{upstream}"),
      "origin/feature",
    );
    assert.strictEqual(again.result.merge?.status, "merged");

    const other = setup(null);
    const refused = await prepare({ dir, clone: other.origin });
    assert.strictEqual(refused.exitCode, 1);
    assert.strictEqual(refused.result.failure?.step, "clone");
    assert.strictEqual(
      git(dir, "symbolic-ref", "--short", "HEAD"),
      "feature",
      "nothing changed there",
    );
  });

  it("reads the config of the branch it prepares, after switching to it", async () => {
    const { origin, work } = setup(null);
    commit(work, { ".t3code/prepare.json": JSON.stringify(CONFIG) }, "config on the branch only");
    git(work, "push", "--quiet", "origin", "feature");
    const dir = clonePath("branch-config");
    const onBranch = await prepare({ dir, clone: origin, branch: "feature" });
    assert.strictEqual(onBranch.result.status, "prepared");
    assert.isTrue(NodeFS.existsSync(NodePath.join(dir, "installed.txt")));
    // Back to a branch with no config: the last branch's commands do not run there.
    NodeFS.rmSync(NodePath.join(dir, "installed.txt"));
    const onMain = await prepare({ dir, clone: origin, branch: "main" });
    assert.strictEqual(onMain.result.status, "no-config");
    assert.isFalse(NodeFS.existsSync(NodePath.join(dir, "installed.txt")));
  });

  it("never reuses a clone stopped half-way as a good one, and leaves no half clone itself", async () => {
    const { origin } = setup(CONFIG);
    const dir = clonePath("half");
    NodeFS.mkdirSync(dir, { recursive: true });
    git(dir, "init", "--quiet");
    git(dir, "remote", "add", "origin", origin);
    const outcome = await prepare({ dir, clone: origin, branch: "feature" });
    assert.strictEqual(outcome.exitCode, 1);
    assert.strictEqual(outcome.result.failure?.step, "clone");
    assert.include(outcome.result.failure?.message ?? "", "stopped half-way");

    const whole = clonePath("whole");
    assert.strictEqual((await prepare({ dir: whole, clone: origin })).exitCode, 0);
    const leftovers = NodeFS.readdirSync(NodePath.dirname(whole)).filter((name) =>
      name.startsWith(`${NodePath.basename(whole)}.`),
    );
    assert.deepStrictEqual(leftovers, []);
  });

  it("fast-forwards a branch that is behind its remote, and refuses one that has diverged", async () => {
    const { origin, work } = setup(CONFIG);
    git(work, "fetch", "--quiet", "origin");
    git(work, "merge", "--quiet", "--no-edit", "origin/main");
    git(work, "push", "--quiet", "origin", "feature");
    const dir = clonePath("behind");
    assert.strictEqual((await prepare({ dir, clone: origin, branch: "feature" })).exitCode, 0);

    commit(work, { "src/more.txt": "more\n" }, "another writer pushed");
    git(work, "push", "--quiet", "origin", "feature");
    const again = await prepare({ dir, clone: origin, branch: "feature" });
    assert.strictEqual(again.exitCode, 0);
    assert.strictEqual(again.result.head, git(work, "rev-parse", "HEAD"));
    assert.include(again.result.notes, "fast-forwarded feature by 1 commit(s) to origin/feature");
    assert.include(
      again.result.baseline?.status === "green" ? again.result.baseline.files : [],
      "src/more.txt",
    );

    commit(dir, { "src/mine.txt": "mine\n" }, "local, not pushed");
    commit(work, { "src/theirs.txt": "theirs\n" }, "pushed meanwhile");
    git(work, "push", "--quiet", "origin", "feature");
    const mine = git(dir, "rev-parse", "HEAD");
    const diverged = await prepare({ dir, clone: origin, branch: "feature" });
    assert.strictEqual(diverged.exitCode, 1);
    assert.strictEqual(diverged.result.failure?.step, "branch");
    assert.include(diverged.result.failure?.message ?? "", "have diverged (1 local, 1 remote");
    assert.strictEqual(git(dir, "rev-parse", "HEAD"), mine, "the branch is left as it was");
  });

  it("does not compare a branch with the base it tracks; the merge brings the base in", async () => {
    const { origin, seed } = setup(CONFIG);
    const dir = clonePath("tracks-base");
    assert.strictEqual((await prepare({ dir, clone: origin })).exitCode, 0);
    // As \`git checkout -b\` from a remote branch, or a new worktree branch, sets it up.
    git(dir, "checkout", "--quiet", "-b", "tracks-base", "origin/main");
    assert.strictEqual(
      git(dir, "rev-parse", "--abbrev-ref", "tracks-base@{upstream}"),
      "origin/main",
    );
    commit(dir, { "src/own.txt": "own\n" }, "own work");
    git(dir, "push", "--quiet", "origin", "tracks-base");
    commit(seed, { "src/newer.txt": "newer\n" }, "main moves on");
    git(seed, "push", "--quiet", "origin", "main");

    const again = await prepare({ dir, clone: origin, branch: "tracks-base" });
    assert.strictEqual(again.exitCode, 0, again.text);
    assert.strictEqual(again.result.status, "prepared");
    assert.strictEqual(again.result.merge?.status, "merged");
    assert.isTrue(NodeFS.existsSync(NodePath.join(dir, "src/newer.txt")));
  });

  it("lets a second first clone into the same folder reuse the one that got there first", async () => {
    const { origin } = setup(CONFIG);
    const dir = clonePath("race");
    // The other prepare's clone lands at the folder while this one's clone is still beside it.
    const templates = NodePath.join(root, `templates${counter}`);
    write(
      templates,
      "hooks/post-checkout",
      `#!/bin/sh\n[ -e "$T3_TEST_RACE" ] || cp -a "$PWD" "$T3_TEST_RACE"\n`,
    );
    NodeFS.chmodSync(NodePath.join(templates, "hooks/post-checkout"), 0o755);
    const before = { template: process.env.GIT_TEMPLATE_DIR, race: process.env.T3_TEST_RACE };
    process.env.GIT_TEMPLATE_DIR = templates;
    process.env.T3_TEST_RACE = dir;
    try {
      const outcome = await prepare({ dir, clone: origin });
      assert.strictEqual(outcome.exitCode, 0, outcome.text);
      assert.include(outcome.result.notes, "reused the existing clone");
    } finally {
      for (const [key, value] of [
        ["GIT_TEMPLATE_DIR", before.template],
        ["T3_TEST_RACE", before.race],
      ] as const) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
    }
    const leftovers = NodeFS.readdirSync(NodePath.dirname(dir)).filter((name) =>
      name.startsWith(`${NodePath.basename(dir)}.`),
    );
    assert.deepStrictEqual(leftovers, []);
  });

  it("refuses to switch away from a detached HEAD with commits on no branch", async () => {
    const { origin, work } = setup(CONFIG);
    git(work, "push", "--quiet", "origin", "feature");
    const dir = clonePath("detached");
    assert.strictEqual((await prepare({ dir, clone: origin })).exitCode, 0);
    git(dir, "checkout", "--quiet", "--detach", "origin/main");
    commit(dir, { "src/stray.txt": "stray\n" }, "a commit on no branch");
    const stray = git(dir, "rev-parse", "HEAD");
    const outcome = await prepare({ dir, clone: origin, branch: "feature" });
    assert.strictEqual(outcome.exitCode, 1);
    assert.strictEqual(outcome.result.failure?.step, "branch");
    assert.include(outcome.result.failure?.message ?? "", "commits on no branch");
    assert.strictEqual(git(dir, "rev-parse", "HEAD"), stray);
  });

  it("tracks the remote branch when the base has no remote in its name", async () => {
    const { origin, work } = setup(null);
    git(work, "push", "--quiet", "origin", "feature");
    const dir = clonePath("plain-base");
    await prepare({ dir, clone: origin, branch: "feature", base: "main" });
    assert.strictEqual(
      git(dir, "rev-parse", "--abbrev-ref", "feature@{upstream}"),
      "origin/feature",
    );
  });

  it("runs one prepare at a time on a checkout, and takes over a stale lock", async () => {
    const { work, gitDir } = setup(CONFIG);
    write(gitDir, LOCK, `${process.pid}\n`);
    write(gitDir, RESULT_JSON_FILE, JSON.stringify({ status: "running", pid: process.pid }));
    const refused = await prepare({ dir: work });
    assert.strictEqual(refused.exitCode, 1);
    assert.include(refused.result.failure?.message ?? "", "another prepare");
    assert.strictEqual(readResult(gitDir).status, "running", "the other run's result is kept");
    assert.isFalse(NodeFS.existsSync(NodePath.join(work, "installed.txt")));

    const gone = NodeChildProcess.spawnSync(process.execPath, ["-e", ""]).pid;
    write(gitDir, LOCK, `${gone}\n`);
    const taken = await prepare({ dir: work });
    assert.strictEqual(taken.result.status, "prepared");
    assert.include(
      taken.result.notes,
      `an earlier prepare (pid ${gone}) was stopped before it finished`,
    );
    assert.isFalse(NodeFS.existsSync(NodePath.join(gitDir, LOCK)), "the lock is released");
  });

  it("reports a baseline command that cannot run as a failure, not as red tests", async () => {
    const { work } = setup({ baseline: "t3-no-such-test-runner {files}" });
    const outcome = await prepare({ dir: work });
    assert.strictEqual(outcome.exitCode, 1);
    assert.strictEqual(outcome.result.failure?.step, "baseline");
    assert.include(outcome.result.failure?.message ?? "", "could not run");
    assert.notInclude(outcome.text, "RED");
  });

  it("reports a baseline killed by a signal as a failure with no result, not as red tests", async () => {
    const { work } = setup({ baseline: "kill -9 $$" });
    const outcome = await prepare({ dir: work });
    assert.strictEqual(outcome.exitCode, 1);
    assert.strictEqual(outcome.result.failure?.step, "baseline");
    assert.include(outcome.result.failure?.message ?? "", "killed by SIGKILL");
    assert.notInclude(outcome.text, "RED");
  });

  it("does not hang on a process that left the command's group but holds its output", async () => {
    const { work } = setup({
      install: [`setsid sh -c 'echo $$ > stray.pid; exec sleep 300' &`],
    });
    try {
      const outcome = await prepare({ dir: work });
      assert.strictEqual(outcome.result.status, "prepared");
    } finally {
      const stray = NodePath.join(work, "stray.pid");
      if (NodeFS.existsSync(stray)) killQuietly(readPid(stray));
    }
  });

  it("does not switch branches over local changes", async () => {
    const { work } = setup(CONFIG);
    write(work, "src/a.txt", "edited\n");
    const outcome = await prepare({ dir: work, branch: "other" });
    assert.strictEqual(outcome.result.failure?.step, "branch");
    assert.strictEqual(git(work, "symbolic-ref", "--short", "HEAD"), "feature");
  });
});

describe("docs/fork/delegate-prepare.md", () => {
  it("shows a setup script that holds the agent until the prepare is done", () => {
    const docs = NodeFS.readFileSync(
      NodePath.join(import.meta.dirname, "../../../../../docs/fork/delegate-prepare.md"),
      "utf8",
    );
    const block = [...docs.matchAll(/```json\n([\s\S]*?)```/g)]
      .map((match) => match[1] ?? "")
      .find((text) => text.includes("runOnWorktreeCreate"));
    assert.isDefined(block, "the docs show the setup script entry");
    const script = decodeScript(JSON.parse(block ?? "{}"));
    assert.strictEqual(script.runOnWorktreeCreate, true);
    assert.strictEqual(script.async, false, "upstream's default (true) starts the agent at once");
  });
});

describe("parsePrepareConfig", () => {
  it("fills defaults and refuses bad values", () => {
    const parsed = parsePrepareConfig("{}");
    assert.deepStrictEqual(parsed, {
      ok: true,
      config: { base: "origin/main", install: [], baseline: undefined, timeoutMinutes: 30 },
    });
    for (const bad of [
      "[]",
      `{"base": ""}`,
      `{"install": "pnpm install"}`,
      `{"install": [""]}`,
      `{"baseline": 3}`,
      `{"timeoutMinutes": 0}`,
      `{"timeoutMinutes": 999}`,
    ]) {
      assert.isFalse(parsePrepareConfig(bad).ok, bad);
    }
  });
});

describe("prepareCli", () => {
  it("parses its arguments", () => {
    const args = parsePrepareArgs(
      ["--dir", "sub", "--paths", "a.ts", "b.ts", "--branch", "x", "--json"],
      "/base",
    );
    assert.deepStrictEqual(args, {
      json: true,
      options: { dir: "/base/sub", paths: ["a.ts", "b.ts"], branch: "x" },
    });
    assert.throws(() => parsePrepareArgs(["--branch"], "/base"), /needs a value/);
    assert.throws(() => parsePrepareArgs(["--nope"], "/base"), /unknown argument/);
  });

  it("runs under plain node and exits by the outcome", () => {
    const { work } = setup({ install: [`node -e "process.exit(1)"`] });
    const cli = NodePath.join(import.meta.dirname, "prepareCli.ts");
    const failed = NodeChildProcess.spawnSync(process.execPath, [cli, "--json"], {
      cwd: work,
      encoding: "utf8",
    });
    assert.strictEqual(failed.status, 1, failed.stderr);
    assert.strictEqual(JSON.parse(failed.stdout).failure.step, "install");
    const usage = NodeChildProcess.spawnSync(process.execPath, [cli, "--nope"], {
      encoding: "utf8",
    });
    assert.strictEqual(usage.status, 2);
  });

  it("stops its command and writes a final result when it is stopped itself", async () => {
    const { work, gitDir } = setup({
      install: [
        `node -e "require('node:fs').writeFileSync('install.pid', String(process.pid)); console.log(['install', 'started'].join(' ')); setTimeout(() => {}, 300000)"`,
      ],
    });
    const cli = NodePath.join(import.meta.dirname, "prepareCli.ts");
    const child = NodeChildProcess.spawn(process.execPath, [cli], {
      cwd: work,
      stdio: ["ignore", "ignore", "pipe"],
    });
    // The CLI sends command output to stderr: the install's own line marks it running (built
    // at run time, so the echoed command line does not match it).
    const started = new Promise<void>((resolve) => {
      let seen = "";
      child.stderr.on("data", (chunk: Buffer) => {
        seen += chunk.toString("utf8");
        if (seen.includes("install started")) resolve();
      });
    });
    const exited = new Promise<number | null>((resolve) =>
      child.on("exit", (code) => resolve(code)),
    );
    const pidFile = NodePath.join(work, "install.pid");
    let installPid = 0;
    try {
      await started;
      installPid = readPid(pidFile);
      child.kill("SIGTERM");
      assert.strictEqual(await exited, 143);
      const result = readResult(gitDir);
      assert.strictEqual(result.status, "failed");
      assert.strictEqual(result.failure.step, "install");
      assert.include(result.failure.message, "stopped by SIGTERM");
      // Its group was sent SIGKILL before the CLI wrote the result and exited.
      assert.isTrue(isGone(installPid), "the install was stopped with the prepare");
      assert.isFalse(NodeFS.existsSync(NodePath.join(gitDir, LOCK)), "the lock is released");
    } finally {
      child.kill("SIGKILL");
      if (installPid > 0) killQuietly(installPid);
    }
  });
});
