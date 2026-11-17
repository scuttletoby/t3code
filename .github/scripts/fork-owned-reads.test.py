"""Checks that nothing an app job reads names a fork-owned path (#254).

fork-owned-paths.test.py checks the names written in fork-tests.yml. A fork-owned file could also be read by app code
(an app test or a root script opening `.github/fork-*.json`), by a composite action or a local action of fork-tests.yml,
or by a `!` file of .github/fork-owned-paths.txt (read by app jobs) that starts to require or import a fork-owned
sibling. A fork-only pull request changing that file would skip the only jobs that read it.

So: every tracked file that is not fork-owned (NOT_SCANNED: the few folders and files no job reads, each with its
reason; a folder upstream adds later is scanned too) must not name a fork-owned path, import a fork-owned sibling, or be
a symlink into one, outside whole-line comments and Python docstrings (NOT_READS: the few names that are no read, each
with its reason). A path built from quoted parts (`path.join(".github", "fork-x")`, `Path("fork-kit") / "x"`,
`".github/" + "fork-x"`) is joined first; a part held in a variable is a known limit. What counts as
fork-owned comes from the list's own script, and the search from the list's top-level names, so a new owned folder is
searched too. A planted read must be found.

It needs the whole tree, so the Lint job of fork-tests.yml runs it (the `workflows` job checks out only the fork's
folders); a file it should scan that is missing from the checkout fails it.

    python3 .github/scripts/fork-owned-reads.test.py

Needs node and git (no PyYAML). Each check prints ok/FAIL; any FAIL exits 1.
"""

import ast
import json
import os
import pathlib
import re
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = root / ".github/scripts/fork-owned-paths.cjs"
LIST = root / ".github/fork-owned-paths.txt"
WORKFLOW = root / ".github/workflows/fork-tests.yml"
failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok or not detail else f"\n     | {detail}"))
    if not ok:
        failures.append(name)


def owned(paths):
    """{path: fork-owned?} by the script's own rules for the real list."""
    r = subprocess.run(["node", "-e", "const m = require(process.argv[1]); const fs = require('node:fs');"
                        "const rules = m.parseList(fs.readFileSync(process.argv[2], 'utf8'));"
                        "console.log(JSON.stringify(Object.fromEntries(JSON.parse(fs.readFileSync(0, 'utf8'))"
                        ".map(p => [p, m.owned(p, rules)]))))", str(SCRIPT), str(LIST)],
                       capture_output=True, text=True, input=json.dumps(paths))
    if r.returncode:
        raise RuntimeError(r.stderr)
    return json.loads(r.stdout)


LIST_LINES = [l.strip() for l in LIST.read_text().splitlines() if l.strip() and not l.startswith("#")]
ROOTS = sorted({l.lstrip("!").split("/")[0] for l in LIST_LINES})
PATH_RE = re.compile(r"(?<![\w.-])(" + "|".join(map(re.escape, ROOTS)) + r")(/[\w.*/-]*)?")
# A doc the list owns by its own line (FORK_CHANGES.md): naming it is a read, unlike a runbook a message points to
# (a `.md` matched by a wider line such as `.github/fork-*`). A name counts only when it equals a line exactly, which
# is what keeps those runbook pointers out (a `*.md` line matches only a name that writes the same `*`).
OWNED_DOCS = {l for l in LIST_LINES if l.endswith(".md") and not l.startswith("!")}
# A fork-owned folder named alone, as in `path.join(root, "fork-ci", name)`: counts as a path inside it.
FOLDERS = {l.rstrip("/") for l in LIST_LINES if l.endswith("/") and l.count("/") == 1}
# A path built from quoted parts: `".github", "fork-x"` (path.join), `Path(".github") / "fork-x"`, `".github/" + "x"`.
# The parts are joined before the search; a part held in a variable stays a known limit.
JOIN_RE = re.compile(r"""['"`]\s*(,|\)?\s*/|\+)\s*(?:Path\(\s*)?['"`]""")
MAYBE = re.compile("|".join(map(re.escape, ROOTS)) + "|fork[-_]")   # a quick test before the slower search
# A sibling by its bare name: a quoted "fork-x.cjs" / "./fork-x.cjs" (require, import, open, path.join) or a Python
# `import fork_x` / `from fork_x import`. Resolved against the file's folder and its parent; only a tracked file counts.
SIBLING_RE = re.compile(r"""['"`](?:\.{1,2}/)*(fork[-_][\w.-]+)['"`]|^[ \t]*(?:from|import)[ \t]+\.?(fork_\w+)""", re.M)
HASH_COMMENTS = {"", ".py", ".sh", ".bash", ".yml", ".yaml", ".toml", ".txt", ".cfg", ".ini"}
SLASH_COMMENTS = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".cjs", ".mjs", ".jsonc", ".json5", ".rs", ".swift",
                  ".kt", ".kts", ".java", ".go", ".c", ".h", ".m", ".mm", ".css", ".scss"}
# Files the Lint checkout leaves out on purpose (fork-tests.yml): binary static libraries, never read as text.
NOT_CHECKED_OUT = re.compile(r"^apps/mobile/modules/t3-terminal/Vendor/.*\.a$")
# Folders never read by a job of this repository, each with its reason. Everything else that is tracked is scanned, so
# a folder upstream adds later is scanned too.
NOT_SCANNED = (
    ".repos/",   # reference copies of other repositories, read by people and agents, never by a job
    ".github/workflows/",   # never fork-owned (a change to one runs every job); fork-owned-paths.test.py checks the
    # names fork-tests.yml's app jobs use, and the fork's own workflows name fork-owned files on purpose
    ".github/fork-owned-paths.txt",   # the list itself: its patterns name fork-owned paths, it reads none
)
# Names that are not reads, each with its reason. A stale entry fails too, so this list cannot grow quietly.
NOT_READS = {
    ("vite.config.ts", ".gitleaks.toml"): "vp fmt's ignore list: Lint, which runs on every pull request, skips the file",
    (".github/secret_scanning.yml", ".gitleaks.toml"): "GitHub's secret scanning settings, read by GitHub, not a job",
}


def code_only(path, text):
    """`text` without its whole-line comments and, in Python, its docstrings (a trailing comment stays: a match there
    fails closed, as does a Python file that does not parse)."""
    ext = os.path.splitext(path)[1].lower()
    lines = text.split("\n")
    if ext == ".py":
        try:
            for n in ast.walk(ast.parse(text)):
                if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                    lines[n.lineno - 1:n.end_lineno] = [""] * (n.end_lineno - n.lineno + 1)
        except (SyntaxError, ValueError):
            pass
    marks = ("#",) if ext in HASH_COMMENTS else ("//", "/*", "*") if ext in SLASH_COMMENTS else ()
    return "\n".join(l for l in lines if not (marks and l.lstrip().startswith(marks)))


def reads(files, tracked_files):
    """{(file, fork-owned path it names)} for `files` ({path: text}), by the list's own rules."""
    found = set()
    for path, text in files.items():
        if path.endswith(".md") or not MAYBE.search(text):
            continue
        text, here = code_only(path, text), os.path.dirname(path)
        joined = JOIN_RE.sub(lambda j: "" if j.group(1) == "+" else "/", text)
        for t, m in [(t, m) for t in {text, joined} for m in PATH_RE.finditer(t)]:   # both: a join can glue on more
            name = (m.group(1) + (m.group(2) or "")).rstrip(".")
            if not m.group(2) and m.group(1) in FOLDERS and not re.match(r"[\w.-]", t[m.end():m.end() + 1]):
                name += "/"
            if not name.endswith(".md") or name in OWNED_DOCS:   # a runbook a message points to is not read
                found.add((path, name.rstrip("/") + ("/x" if name.endswith("/") else "")))
        for m in SIBLING_RE.finditer(text):
            name = m.group(1) or m.group(2) + ".py"
            names = (name,) if "." in name else (name, name + ".py")   # importlib.import_module("fork_x")
            found |= {(path, p) for d in (here, os.path.dirname(here)) for n in names
                      if (p := os.path.normpath(os.path.join(d, n))) in tracked_files}
    names = owned(sorted({name for _, name in found})) if found else {}
    return {(path, name) for path, name in found if names[name]}


tracked = set(subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True,
                             check=True).stdout.split("\0")) - {""}
excluded = {l[1:] for l in LIST_LINES if l.startswith("!")}
to_scan = sorted(f for f in tracked if not f.startswith(NOT_SCANNED))
fork_own = owned(to_scan)   # the fork's own files (.gitleaks.toml at the root) are read by fork jobs only
scanned, missing = {}, []
for f in to_scan:
    if fork_own[f]:
        continue
    p = root / f
    if not os.path.lexists(p):
        if not NOT_CHECKED_OUT.match(f):
            missing.append(f)
        continue
    if p.is_symlink():   # a link into a fork-owned path is a read of it: scan the path it points to
        scanned[f] = os.path.normpath(os.path.join(os.path.dirname(f), os.readlink(p))) + "\n"
        continue
    try:
        scanned[f] = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
        pass   # binary, a submodule or a dangling symlink
check("reads: every file to scan is checked out (a sparse checkout would hide reads)", not missing,
      f"{len(missing)} missing, e.g. {missing[:5]}")
top = lambda files: {f.split("/")[0] for f in files if "/" in f}
unscanned = sorted(top(tracked) - top(scanned) - {n.rstrip("/") for n in NOT_SCANNED}
                   - top(f for f in to_scan if fork_own[f]))
check("reads: every top-level folder a job could read is scanned (infra/, native/, a folder upstream adds later)",
      not unscanned and ".github/scripts/test-delta.cjs" in scanned, f"not scanned: {unscanned}")
found = reads(scanned, tracked)
bad = sorted(found - set(NOT_READS))
stale = sorted(set(NOT_READS) - found)
check("reads: no app code, root build config, composite action or `!` file names a fork-owned path",
      not bad and not stale and len(scanned) > 1000 and excluded <= set(scanned),
      f"reads: {bad}; stale NOT_READS: {stale}; scanned {len(scanned)} files")

PLANTED = {
    "apps/web/src/a.test.ts": 'const t = readFileSync(".github/fork-ci-tests.txt", "utf8");\n',
    "packages/x/b.ts": 'import { load } from "../../fork-ci/actions/pr-queue/config.py";\n',
    "scripts/c.ts": 'const kit = path.join(root, "fork-kit/heavy/heavy");\n',
    ".github/scripts/fork-fleet.py": '"""Reads .github/fork-ci-tests.txt? No: a docstring."""\nimport json\n'
                                     "from fork_workflow_expr import gh_eval\n",
    ".github/scripts/fork-test-shards.cjs": 'const x = require("./fork-seam-check.cjs");\n',
    "infra/e.ts": 'const t = path.join(".github", "fork-ci-tests.txt");\n',
    "native/f.py": 'kit = Path("fork-kit") / "heavy"\n',
    "evals/g.ts": 'const a = join(root, "fork-ci", name);\nconst b = ".github/" + "fork-ci-tests.txt";\n',
    ".github/scripts/h.py": 'm = importlib.import_module("fork_workflow_expr")\n',
    "packages/shared/src/i.test.ts": 'const changes = repoFile("FORK_CHANGES.md");\n',
}
got = reads({**PLANTED, "apps/web/src/d.ts": "// reads .github/fork-ci-tests.txt and fork-ci/x\n"
             "  * fork-kit/heavy, see .github/fork-fleet.md\nconst p = '.github/pull_request_template.md';\n"
             "const q = '.github/scripts/ci-changes.cjs'; const r = './fork-not-a-file.cjs';\n"
             "const s = 'fork-ci-release'; const u = join('a', 'b');\n"},
            tracked)
check("reads: a planted read is found in app code, a root script and a `!` file; comments and other paths are not",
      got == {("apps/web/src/a.test.ts", ".github/fork-ci-tests.txt"),
              ("packages/x/b.ts", "fork-ci/actions/pr-queue/config.py"), ("scripts/c.ts", "fork-kit/heavy/heavy"),
              (".github/scripts/fork-fleet.py", ".github/scripts/fork_workflow_expr.py"),
              (".github/scripts/fork-test-shards.cjs", ".github/scripts/fork-seam-check.cjs"),
              ("infra/e.ts", ".github/fork-ci-tests.txt"), ("native/f.py", "fork-kit/heavy"),
              ("native/f.py", "fork-kit/x"), ("evals/g.ts", "fork-ci/x"), ("evals/g.ts", ".github/fork-ci-tests.txt"),
              (".github/scripts/h.py", ".github/scripts/fork_workflow_expr.py"),
              ("packages/shared/src/i.test.ts", "FORK_CHANGES.md")}, str(sorted(got)))

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
