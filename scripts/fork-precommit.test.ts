// @effect-diagnostics nodeBuiltinImport:off
// Fork: the pre-commit hook (.vite-hooks/pre-commit: `vp staged` with vite.config.ts's `staged` block), run by
// `git commit` in a scratch repository on this vite.config.ts. A `vp` shim hands `vp staged` to the real one and
// logs every other call (the hook's tasks), so each test sees what one commit runs, on which files.
import * as NodeChildProcess from "node:child_process";
import * as NodeFS from "node:fs";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";
import * as NodeURL from "node:url";

import { assert, describe, it } from "@effect/vitest";

const ROOT = NodePath.dirname(NodeURL.fileURLToPath(new URL("../package.json", import.meta.url)));
const REAL_VP = NodePath.join(ROOT, "node_modules/.bin/vp");
const HOOK = NodePath.join(ROOT, ".vite-hooks/pre-commit");

// Logs one JSON line per call. `fmt` stands in for the formatter: it turns `;;` into `;` in each file it is
// given (a change the hook must stage). FAKE_LINT_FAIL makes `lint` fail, as a lint error does.
const SHIM = `#!/bin/sh
if [ "$1" = staged ]; then exec "$REAL_VP" "$@"; fi
node -e 'require("fs").appendFileSync(process.env.VP_LOG, JSON.stringify(process.argv.slice(1)) + "\\n")' -- "$@"
if [ "$1" = fmt ]; then
  for f in "$@"; do case "$f" in -*|fmt) ;; *) [ -f "$f" ] && sed -i 's/;;/;/g' "$f" ;; esac; done
fi
if [ "$1" = lint ] && [ -n "$FAKE_LINT_FAIL" ]; then echo "fake lint error" >&2; exit 1; fi
exit 0
`;

interface GitOptions {
  readonly env?: Record<string, string>;
  readonly mayFail?: boolean;
}

function scratch() {
  assert.ok(NodeFS.existsSync(REAL_VP), `${REAL_VP} is missing: install the dependencies first`);
  const dir = NodeFS.realpathSync(
    NodeFS.mkdtempSync(NodePath.join(NodeOS.tmpdir(), "fork-precommit-")),
  );
  const repo = NodePath.join(dir, "repo");
  const bin = NodePath.join(dir, "bin");
  const hooks = NodePath.join(dir, "hooks");
  const log = NodePath.join(dir, "vp.log");
  // Nothing from an outer git (a hook's GIT_DIR or GIT_INDEX_FILE) reaches the scratch repository.
  const env: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env))
    if (v !== undefined && !k.startsWith("GIT_")) env[k] = v;
  Object.assign(env, {
    PATH: `${bin}${NodePath.delimiter}${env.PATH ?? ""}`,
    REAL_VP,
    VP_LOG: log,
    GIT_CEILING_DIRECTORIES: dir,
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_CONFIG_GLOBAL: NodePath.join(dir, "gitconfig"),
  });
  const git = (args: ReadonlyArray<string>, opts: GitOptions = {}) => {
    const r = NodeChildProcess.spawnSync("git", args, {
      cwd: repo,
      env: { ...env, ...opts.env },
      encoding: "utf8",
    });
    if (!opts.mayFail) assert.equal(r.status, 0, `git ${args.join(" ")}: ${r.stdout}${r.stderr}`);
    return r;
  };
  const write = (file: string, text: string) => {
    NodeFS.mkdirSync(NodePath.dirname(NodePath.join(repo, file)), { recursive: true });
    NodeFS.writeFileSync(NodePath.join(repo, file), text);
  };
  try {
    for (const d of [repo, bin, hooks]) NodeFS.mkdirSync(d);
    NodeFS.writeFileSync(NodePath.join(bin, "vp"), SHIM, { mode: 0o755 });
    // The hook's own text, run the way vp's hook wrapper runs it (sh -e).
    NodeFS.writeFileSync(
      NodePath.join(hooks, "pre-commit"),
      `#!/bin/sh\nexec sh -e ${JSON.stringify(HOOK)}\n`,
      { mode: 0o755 },
    );
    git(["init", "-q", "-b", "main"]);
    for (const [k, v] of [
      ["user.name", "test"],
      ["user.email", "test@example.invalid"],
      ["commit.gpgsign", "false"],
      ["core.hooksPath", hooks],
    ] as const)
      git(["config", k, v]);
    write(
      "vite.config.ts",
      `export { default } from ${JSON.stringify(NodePath.join(ROOT, "vite.config.ts"))};\n`,
    );
    write("keep.ts", "export const keep = 1;\n");
    write("old name.ts", "export const old = 1;\n");
    write("gone.ts", "export const gone = 1;\n");
    write("notes.bin", "\u0000\u0001");
    git(["add", "-A"]);
    git(["commit", "-q", "--no-verify", "-m", "base"]);
  } catch (error) {
    NodeFS.rmSync(dir, { recursive: true, force: true });
    throw error;
  }
  // Each call's words, with the scratch repository's absolute paths made repository-relative.
  const calls = (): Array<Array<string>> =>
    NodeFS.existsSync(log)
      ? NodeFS.readFileSync(log, "utf8")
          .split("\n")
          .filter(Boolean)
          .map((line) =>
            (JSON.parse(line) as Array<string>).map((w) =>
              w.startsWith(`${repo}/`) ? w.slice(repo.length + 1) : w,
            ),
          )
          .toSorted((a, b) => a.join(" ").localeCompare(b.join(" ")))
      : [];
  return {
    git,
    write,
    calls,
    commit: (opts?: GitOptions) => git(["commit", "-q", "-m", "change"], opts),
    read: (file: string) => NodeFS.readFileSync(NodePath.join(repo, file), "utf8"),
    [Symbol.dispose]() {
      NodeFS.rmSync(dir, { recursive: true, force: true });
    },
  };
}

const FMT = ["fmt", "--no-error-on-unmatched-pattern"];
const LINT = ["lint", "--no-error-on-unmatched-pattern"];
const fmtAndLint = (file: string) => [
  [...FMT, file],
  [...LINT, file],
];

describe("pre-commit hook", () => {
  it("one staged file: formats and lints that file only, and runs nothing else (no knip)", () => {
    using s = scratch();
    s.write("keep.ts", "export const keep = 2;\n");
    s.write("unstaged.ts", "export const other = 1;\n");
    s.git(["add", "keep.ts"]);
    s.commit();
    assert.deepEqual(s.calls(), fmtAndLint("keep.ts"));
  });

  it("no staged files: an empty commit runs no task and passes", () => {
    using s = scratch();
    s.git(["commit", "-q", "--allow-empty", "-m", "empty"]);
    assert.deepEqual(s.calls(), []);
  });

  it("only a deletion staged: no task runs, and the commit passes", () => {
    using s = scratch();
    s.git(["rm", "-q", "gone.ts"]);
    s.commit();
    assert.deepEqual(s.calls(), []);
    assert.equal(s.git(["ls-files", "gone.ts"]).stdout, "");
  });

  it("a rename to a name with a space: the tasks get the new path, whole, as one argument", () => {
    using s = scratch();
    s.git(["mv", "old name.ts", "new name.ts"]);
    s.commit();
    assert.deepEqual(s.calls(), fmtAndLint("new name.ts"));
  });

  it("a file type neither tool handles goes with the no-error-on-unmatched flag", () => {
    using s = scratch();
    s.write("notes.bin", "\u0000\u0002");
    s.git(["add", "notes.bin"]);
    s.commit();
    assert.deepEqual(s.calls(), fmtAndLint("notes.bin"));
  });

  it("partial staging: the formatter's fix is committed, the unstaged hunk stays out and in the file", () => {
    using s = scratch();
    const lines = Array.from({ length: 12 }, (_, i) => `export const v${i} = ${i};`);
    s.write("part.ts", `${lines.join("\n")}\n`);
    s.git(["add", "part.ts"]);
    s.git(["commit", "-q", "--no-verify", "-m", "part"]);
    // Staged: the first line, unformatted. Unstaged: the last line.
    const staged = ["export const v0 = 100;;", ...lines.slice(1)];
    s.write("part.ts", `${staged.join("\n")}\n`);
    s.git(["add", "part.ts"]);
    s.write("part.ts", `${[...staged.slice(0, -1), "export const v11 = 999;"].join("\n")}\n`);
    s.commit();
    const committed = s.git(["show", "HEAD:part.ts"]).stdout;
    assert.match(committed, /^export const v0 = 100;$/m, "the formatter's fix is committed");
    assert.match(committed, /^export const v11 = 11;$/m, "the unstaged hunk is not committed");
    assert.match(s.read("part.ts"), /^export const v11 = 999;$/m, "the hunk is still in the file");
    assert.match(s.git(["diff", "--name-only"]).stdout, /^part\.ts$/m, "and still unstaged");
  });

  it("a lint error refuses the commit and keeps the change staged; run again, it commits", () => {
    using s = scratch();
    s.write("keep.ts", "export const keep = 3;\n");
    s.git(["add", "keep.ts"]);
    const r = s.commit({ env: { FAKE_LINT_FAIL: "1" }, mayFail: true });
    assert.notEqual(r.status, 0, "the commit was refused");
    assert.equal(s.git(["rev-list", "--count", "HEAD"]).stdout.trim(), "1");
    assert.equal(s.git(["diff", "--cached", "--name-only"]).stdout.trim(), "keep.ts");
    s.commit();
    assert.equal(s.git(["rev-list", "--count", "HEAD"]).stdout.trim(), "2");
  });
});
