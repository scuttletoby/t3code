"""Checks the fork-only switch of Fork tests: .github/fork-owned-paths.txt, .github/scripts/fork-owned-paths.cjs and
how .github/workflows/fork-tests.yml uses them.

  script    the real script, run as the `changes` job runs it, in throw-away repositories with GitHub's merge commit:
            only a pull request whose every changed path is fork-owned prints fork_only=true and upstream's gates,
            all false; a partly fork-owned change prints upstream's gates for its other paths alone (an unknown
            one still runs every job); a change to the list, the script, a workflow or a package file, a push, no
            merge commit, an empty change and a broken list all print fork_only=false and no gates.
  list      the real list: what it owns and what it must never own.
  workflow  fork-tests.yml: the step runs only on a pull request and not in a release run, every gate of `changes`
            takes its answer first, every app job's if: (evaluated as GitHub does) skips on a fork-only pull request
            and runs on any other run, a partly fork-owned pull request runs exactly the app jobs its other paths
            need (path X runs job Y), and no file an app job names is fork-owned.
  result    fork_ci_result's own script, run as Actions runs it: a fork-only run passes with the app jobs skipped;
            a skip the gates do not explain still fails.

    python3 .github/scripts/fork-owned-paths.test.py

Needs PyYAML, node, git, bash and jq. Each check prints ok/FAIL; any FAIL exits 1.
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

from fork_workflow_expr import gh_eval

root = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = root / ".github/scripts/fork-owned-paths.cjs"
LIST = root / ".github/fork-owned-paths.txt"
WORKFLOW = root / ".github/workflows/fork-tests.yml"
wf = yaml.safe_load(WORKFLOW.read_text())
jobs = wf["jobs"]
failures = []
# The ok lines printed, for the end line: fork-ci-tests.cjs checks its count against them (#664).
oks = []


def check(name, ok, detail=""):
    if ok:
        oks.append(1)
    print(("ok   " if ok else "FAIL ") + name + ("" if ok or not detail else f"\n     | {detail}"))
    if not ok:
        failures.append(name)


def node(code, *args):
    r = subprocess.run(["node", "-e", code, str(SCRIPT), *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr)
    return json.loads(r.stdout)


def owned(paths, list_text=None):
    """{path: fork-owned?} by the script's own rules, for the real list or `list_text`."""
    return node("const m = require(process.argv[1]); const fs = require('node:fs');"
                "const rules = m.parseList(process.argv[2] === '-' ? fs.readFileSync(process.argv[3], 'utf8') : process.argv[2]);"
                "console.log(JSON.stringify(Object.fromEntries(JSON.parse(process.argv[4]).map(p => [p, m.owned(p, rules)]))))",
                list_text if list_text is not None else "-", str(LIST), json.dumps(paths))


UPSTREAM_GATES = node("const { decide } = require(require('node:path').join(require('node:path').dirname(process.argv[1]), 'ci-changes.cjs'));"
                      "console.log(JSON.stringify(Object.keys(decide([], [], false).outputs)))")


# --- script --------------------------------------------------------------------------------------
def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                    *args], cwd=cwd, check=True, capture_output=True)


def write(repo, files):
    for name, text in files.items():
        p = repo / name
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def run_script(change, event="pull_request", merge=True, base_files=None, list_text=None):
    """A repository whose base has the real list (or `list_text`) and `base_files`, a branch applying `change`
    ({path: text, or None to delete}), checked out as GitHub's merge commit (or the branch tip when not `merge`).
    Returns (the script's output lines, its stderr)."""
    with tempfile.TemporaryDirectory(prefix="fork-owned-paths-") as tmp:
        repo = pathlib.Path(tmp)
        git(repo, "init", "-q", "-b", "main")
        write(repo, {".github/fork-owned-paths.txt": LIST.read_text() if list_text is None else list_text,
                     "README.md": "x\n", **(base_files or {})})
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        git(repo, "checkout", "-q", "-b", "pr")
        write(repo, change)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "--allow-empty", "-m", "change")
        if merge:
            git(repo, "checkout", "-q", "main")
            git(repo, "commit", "-q", "--allow-empty", "-m", "main moved on")
            git(repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")
        env = {"PATH": os.environ["PATH"], "GITHUB_EVENT_NAME": event, "HOME": tmp}
        r = subprocess.run(["node", str(SCRIPT)], cwd=repo, env=env, capture_output=True, text=True)
        if r.returncode:
            return [f"exit {r.returncode}"], r.stderr
        return r.stdout.splitlines(), r.stderr


def fork_only(out):
    return out[0] == "fork_only=true"


def gates_of(out):
    return dict(line.split("=", 1) for line in out[1:])


def runs_everything(out):
    """No fork-only switch, and either no gates (the detector's own answer stands) or every app gate true."""
    g = gates_of(out)
    return out[0] == "fork_only=false" and (not g or (
        list(g) == UPSTREAM_GATES and g["test_packages"] == ""
        and all(g[k] == "true" for k in UPSTREAM_GATES if k not in ("test_packages", "mobile_native"))))


out, err = run_script({"fork-ci/actions/x/x.py": "1\n", "fork-kit/desk/README.md": "y\n",
                       ".github/scripts/fork-new.py": "2\n", ".github/fork-new.json": "{}\n"})
gates = dict(line.split("=", 1) for line in out[1:])
check("script: a fork-only pull request prints fork_only=true", fork_only(out), f"{out} {err}")
check("script: ... and every gate of upstream's detector, each false",
      list(gates) == UPSTREAM_GATES and all(gates[g] in ("false", "") for g in gates)
      and all(gates[g] == "false" for g in gates if g != "test_packages") and len(UPSTREAM_GATES) >= 9,
      f"{gates} vs {UPSTREAM_GATES}")
out, _ = run_script({"fork-ci/x.py": "1\n"}, base_files={"fork-ci/old name.py": "0\n"})
check("script: a fork-only change with a space in a path is fork-only", fork_only(out), str(out))

for label, change, base in [
    ("a change to the list itself", {"fork-ci/x.py": "1\n", ".github/fork-owned-paths.txt": "fork-ci/\napps/\n"}, None),
    ("a change to the script", {".github/scripts/fork-owned-paths.cjs": "x\n"}, None),
    ("a change to fork-tests.yml", {".github/workflows/fork-tests.yml": "x\n"}, None),
    ("a change to another fork workflow", {".github/workflows/fork-pr-queue.yml": "x\n"}, None),
    ("a change to a composite action", {".github/actions/setup-ci-deps/action.yml": "x\n"}, None),
    ("a change to upstream's detector", {".github/scripts/ci-changes.cjs": "x\n"}, None),
    ("a file an app job reads (fork-test-shards.cjs)", {".github/scripts/fork-test-shards.cjs": "x\n"}, None),
    ("the runner routing (fork-fleet.py)", {".github/scripts/fork-fleet.py": "x\n"}, None),
    ("the fleet facts (fork-fleet.json)", {".github/fork-fleet.json": "{}\n"}, None),
    ("a gate change with the review vectors the server test reads",
     {"fork-ci/actions/pr-queue/review_judge.py": "x\n", "fork-ci/actions/pr-queue/review.vectors.json": "{}\n"}, None),
    ("a change to the gate's lens corpus", {"fork-ci/actions/pr-queue/testdata/lens-corpus.jsonl": "{}\n"}, None),
    ("a package.json inside fork-kit", {"fork-kit/x/package.json": "{}\n"}, None),
    ("a lock file", {"pnpm-lock.yaml": "x\n"}, None),
    ("a path deeper than a `*` (fork-*/x)", {".github/scripts/fork-x/y.py": "1\n"}, None),
    ("an empty change", {}, None),
]:
    out, err = run_script(change, base_files=base)
    check(f"script: {label} runs every job (fork_only=false)", runs_everything(out), f"{out} {err}")
    if not any(owned(list(change)).values()):
        check(f"script: ... and, with no fork-owned path in it, prints no gates", out == ["fork_only=false"], f"{out}")

# A partly fork-owned change: upstream's detector judges the other paths alone (each row: the change, then the gates
# that must be true, then the ones that must be false). A path the detector does not know still runs every job.
SERVER = {"fork-ci/actions/x/x.py": "1\n", ".github/fork-seams.json": "{}\n", "fork-kit/heavy/README.md": "x\n",
          "apps/server/src/fork/a.ts": "1\n"}
for label, change, on, off, *base in [
    # A move lists the old and the new path (--no-renames): the app side still decides.
    ("a file moved out of fork-ci/ into packages/shared", {"fork-ci/a.ts": None, "packages/shared/src/a.ts": "a\n"},
     {"typecheck", "build", "test", "test_web", "test_server"}, {"rust"}, {"fork-ci/a.ts": "a\n"}),
    ("a file moved into fork-ci/ (an apps/web file deleted)", {"apps/web/src/a.ts": None, "fork-ci/a.ts": "a\n"},
     {"typecheck", "build", "test_web"}, {"rust"}, {"apps/web/src/a.ts": "a\n"}),
    ("fork-ci/, fork-kit/, .github/fork-* and apps/server/src/fork/", SERVER,
     {"typecheck", "build", "test", "test_server"}, {"test_web", "rust", "release_smoke", "desktop_launch", "mobile_native"}),
    ("FORK_CHANGES.md, docs/ and apps/server", {"FORK_CHANGES.md": "x\n", "docs/fork/a.md": "x\n",
                                                 ".github/fork-ci-tests.txt": "x\n", "apps/server/src/fork/a.ts": "1\n"},
     {"typecheck", "build", "test_server"}, {"test_web", "rust", "release_smoke"}),
    ("fork-ci/ and apps/web", {"fork-ci/x.py": "1\n", "apps/web/src/a.ts": "1\n"},
     {"typecheck", "build", "test_web"}, {"test_server", "rust", "release_smoke", "mobile_native"}),
    ("fork-kit/ and a Rust crate", {"fork-kit/heavy/README.md": "x\n", "native/resource-monitor/src/lib.rs": "x\n"},
     {"rust", "test", "release_smoke"}, {"test_web", "test_server", "mobile_native"}),
    ("fork-ci/ and a native mobile source", {"fork-ci/x.py": "1\n", "apps/mobile/ios/A.swift": "x\n"},
     {"mobile_native"}, {"rust", "test_server"}),
    ("fork-ci/ and only docs (upstream's inert paths)", {"fork-ci/x.py": "1\n", "docs/a.md": "x\n"},
     set(), {"typecheck", "build", "test", "test_web", "test_server", "rust", "release_smoke", "mobile_native"}),
]:
    out, err = run_script(change, base_files=base[0] if base else None)
    g = gates_of(out)
    check(f"script: partly fork-owned ({label}) prints upstream's gates for the other paths",
          out[0] == "fork_only=false" and list(g) == UPSTREAM_GATES
          and all(g[k] == "true" for k in on) and all(g[k] == "false" for k in off), f"{out} {err}")
    check(f"script: ... and says in the log which paths it left out", "Partly fork-owned" in err, err[-300:])
out, _ = run_script(SERVER)
check("script: partly fork-owned with apps/server tests only the packages the server change reaches",
      gates_of(out).get("test_packages") not in ("", None), str(out))
for label, extra in [("a path no package owns", {"newtopdir/a.txt": "x\n"}),
                     ("a package.json", {"apps/web/package.json": "{}\n"}),
                     ("fork-tests.yml", {".github/workflows/fork-tests.yml": "x\n"}),
                     ("the review vectors the server test reads", {"fork-ci/actions/pr-queue/review.vectors.json": "{}\n"}),
                     ("the runner routing (fork-fleet.py)", {".github/scripts/fork-fleet.py": "x\n"})]:
    out, err = run_script({"fork-ci/x.py": "1\n", "fork-kit/heavy/README.md": "x\n", **extra})
    check(f"script: partly fork-owned with {label} still runs every job", runs_everything(out), f"{out} {err}")

# Fail closed: a workspace the detector cannot read keeps the detector's answer (no gates), never all false.
r = subprocess.run(["node", "-e", "const m = require(process.argv[1]);"
                    "for (const pk of [[], null]) { try { m.narrowedOutputs(['apps/web/src/a.ts'], pk ?? undefined);"
                    " console.log('printed') } catch (e) { console.log('threw') } }"
                    "try { m.narrowedOutputs([], [{ name: 'a', dir: 'a', hasTest: true, deps: [] }]); console.log('printed') }"
                    " catch (e) { console.log('threw') }", str(SCRIPT)], capture_output=True, text=True)
check("script: narrowing refuses an empty workspace and an empty path list (the caller then prints no gates)",
      r.stdout.split()[0::2] == ["threw", "threw"] and r.stdout.split()[1] == "printed", r.stdout + r.stderr)

out, err = run_script({"fork-ci/x.py": "1\n"}, event="push")
check("script: a push to main with only fork-ci/ changes runs every job", out == ["fork_only=false"], f"{out} {err}")
out, err = run_script({"fork-ci/x.py": "1\n"}, merge=False)
check("script: a checkout that is no merge commit runs every job", out == ["fork_only=false"], f"{out} {err}")
out, err = run_script({"fork-ci/x.py": "1\n"}, list_text="fork-ci/\n../\n")
check("script: a list with a path outside the repository runs every job", out == ["fork_only=false"], f"{out} {err}")
out, err = run_script({"fork-ci/x.py": "1\n"}, list_text="*\n")
check("script: a list owning every top-level file is refused", out == ["fork_only=false"], f"{out} {err}")
with tempfile.TemporaryDirectory(prefix="fork-owned-paths-") as tmp:
    r = subprocess.run(["node", str(SCRIPT)], cwd=tmp, env={"PATH": os.environ["PATH"], "GITHUB_EVENT_NAME": "pull_request"},
                       capture_output=True, text=True)
    check("script: no git repository (or no list) runs every job and exits 0",
          r.returncode == 0 and r.stdout.split() == ["fork_only=false"], f"{r.returncode} {r.stdout} {r.stderr}")

# --- list ----------------------------------------------------------------------------------------
REAL = owned([
    "fork-ci/actions/pr-queue/pr_queue.py", "fork-kit/heavy/heavy", ".github/scripts/fork-fleet-walls.py",
    ".github/scripts/fork-fleet-walls/linux", ".github/scripts/fork_workflow_expr.py", ".github/fork-ci-tests.txt",
    ".gitleaks.toml", "FORK_CHANGES.md",
    # never
    ".github/fork-owned-paths.txt", ".github/scripts/fork-owned-paths.cjs", ".github/workflows/fork-tests.yml",
    ".github/workflows/fork-checks.yml", ".github/actions/ci-changes/action.yml", ".github/scripts/ci-changes.cjs",
    ".github/scripts/fork-fleet.py", ".github/scripts/fork-test-shards.cjs", ".github/scripts/fork-vitest-gc-check.cjs",
    ".github/scripts/fork-server-shards.cjs", ".github/fork-server-test-seconds.json",
    ".github/fork-fleet.json", "fork-ci/package.json", "fork-kit/x/tsconfig.json", "package.json", "apps/web/src/a.ts",
    "docs/fork/README.md", "scripts/release-smoke.ts", "fork-ci", "fork-cix/a",
    "fork-ci/actions/pr-queue/review.vectors.json", "fork-ci/actions/pr-queue/testdata/lens-corpus.jsonl",
])
want_owned = {p for p in REAL if p.startswith(("fork-ci/actions", "fork-kit/heavy")) and "/review.vectors" not in p
              and "/testdata/lens-corpus" not in p or p in {
    ".github/scripts/fork-fleet-walls.py", ".github/scripts/fork-fleet-walls/linux", ".github/scripts/fork_workflow_expr.py",
    ".github/fork-ci-tests.txt", ".gitleaks.toml", "FORK_CHANGES.md"}}
check("list: the fork's own folders and scripts are fork-owned; the rest, and every NEVER path, are not",
      {p for p, v in REAL.items() if v} == want_owned, json.dumps({p: v for p, v in REAL.items() if v != (p in want_owned)}))
check("list: NEVER wins over a line that owns everything under .github/",
      owned([".github/workflows/fork-tests.yml", ".github/fork-owned-paths.txt", ".github/x"], ".github/\n")
      == {".github/workflows/fork-tests.yml": False, ".github/fork-owned-paths.txt": False, ".github/x": True})
check("list: a `!` line wins over an owning line before or after it",
      owned(["a/b.py", "a/c.py"], "!a/b.py\na/\n") == {"a/b.py": False, "a/c.py": True}
      and owned(["a/b.py"], "a/\n!a/b.py\n") == {"a/b.py": False})
tracked = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True).stdout.split("\0")
missing = [l[1:] for l in LIST.read_text().splitlines() if l.startswith("!") and l[1:] not in tracked]
check("list: every `!` line names a file in the tree (a rename would leave the new name fork-owned)", not missing,
      ", ".join(missing))

# --- workflow ------------------------------------------------------------------------------------
changes = jobs["changes"]
steps = {s.get("id"): s for s in changes["steps"] if s.get("id")}
step = steps.get("fork_only", {})
order = [s.get("id") for s in changes["steps"]]
check("workflow: `changes` has the fork_only step, after the detector and before the macOS lint rule and the routing",
      "fork_only" in order and order.index("detect") < order.index("fork_only") < order.index("fork") < order.index("route"),
      str(order))
check("workflow: the step runs the script into the job's outputs",
      step.get("run", "").strip() == 'node .github/scripts/fork-owned-paths.cjs >> "$GITHUB_OUTPUT"', step.get("run"))
sparse = changes["steps"][0]["with"]["sparse-checkout"].split()
check("workflow: `changes` checks out the list and the script",
      {"/.github/fork-owned-paths.txt", "/.github/scripts/fork-owned-paths.cjs", "/.github/scripts/ci-changes.cjs"} <= set(sparse))
# The script reads the workspace (pnpm-workspace.yaml and each package's package.json) to narrow a partly fork-owned
# change, here and in this test, which runs in the `workflows` job: both checkouts must hold those files.
WORKSPACE = {"/pnpm-workspace.yaml", "/*/package.json", "/*/*/package.json"}
check("workflow: `changes` checks out the workspace the script narrows with", WORKSPACE <= set(sparse), str(sparse))
tests_sparse = jobs["workflows"]["steps"][0]["with"]["sparse-checkout"].split()
check("workflow: `workflows`, which runs this test, checks out that workspace too", WORKSPACE <= set(tests_sparse),
      str(sorted(WORKSPACE - set(tests_sparse))))


def ctx(event, release=False, fork_only_out=None, detect=None, status="success", base="main"):
    return {"github": {"event_name": event, "base_ref": base if event == "pull_request" else "",
                       "event": {"repository": {"default_branch": "main"},
                                 **({"pull_request": {"head": {"repo": {"full_name": "o/r"}}}} if event == "pull_request" else {})},
                       "repository": "o/r"},
            "inputs": {"ref": "abc" if release else ""}, "status": status,
            "steps": {"fork_only": {"outputs": fork_only_out or {}}, "detect": {"outputs": detect or {}},
                      "release": {"outputs": {g: "true" for g in UPSTREAM_GATES} if release else {}},
                      "fork": {"outputs": {"mobile_native": fork_step(fork_only_out, detect) if not release else ""}}}}


def fork_step(fork_only_out, detect, changed=""):
    """The `fork` step (the macOS lint rule) run as Actions runs it: its env as `changes` evaluates it, `changed`
    as the detector's list of changed paths. Returns its mobile_native output."""
    step = steps["fork"]
    m = re.fullmatch(r"\$\{\{ steps\.(\w+)\.outputs\.mobile_native \|\| steps\.(\w+)\.outputs\.mobile_native \}\}",
                     step["env"]["DETECTED"])
    if not m:
        raise AssertionError(f"the fork step's DETECTED is not `a || b`: {step['env']['DETECTED']}")
    pick = {"fork_only": fork_only_out or {}, "detect": detect or {}}
    detected = pick[m.group(1)].get("mobile_native") or pick[m.group(2)].get("mobile_native") or ""
    with tempfile.TemporaryDirectory(prefix="fork-owned-paths-fork-") as tmp:
        pathlib.Path(tmp, "changed-paths").write_text(changed)
        out = pathlib.Path(tmp, "out")
        subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], check=True,
                       env={"PATH": os.environ["PATH"], "DETECTED": detected, "RUNNER_TEMP": tmp, "GITHUB_OUTPUT": str(out)})
        return dict(l.split("=", 1) for l in out.read_text().split())["mobile_native"]


cond = step.get("if", "")
check("workflow: the step runs on a pull request only, never on a push or a release run",
      gh_eval(cond, ctx("pull_request")) and not gh_eval(cond, ctx("push")) and not gh_eval(cond, ctx("workflow_dispatch", True))
      and not gh_eval(cond, ctx("workflow_call", True)) and not gh_eval(cond, ctx("pull_request", True)), cond)
check("workflow: ... and only into main: a stacked pull request (base: its parent branch) runs every job",
      not gh_eval(cond, ctx("pull_request", base="feature")), cond)

FORK_ONLY_OUT = {"fork_only": "true", **{g: ("" if g == "test_packages" else "false") for g in UPSTREAM_GATES}}
DETECT_ALL = {g: ("" if g == "test_packages" else "true") for g in UPSTREAM_GATES}


def outputs(c):
    def value(expr):
        m = re.fullmatch(r"\$\{\{(.*)\}\}", str(expr).strip(), re.S)
        if not m:
            return expr
        e = m.group(1).strip()
        # gh_eval answers truthiness; an output keeps the value of the operand `||` picks.
        for part in e.split("||"):
            part = part.strip()
            if part.startswith("'"):
                return part.strip("'")
            got = c
            for key in part.split("."):
                got = got.get(key) if isinstance(got, dict) else None
            if got not in (None, "", False):
                return got
        return ""
    return {k: value(v) for k, v in changes["outputs"].items()}


simple = [v for k, v in changes["outputs"].items() if k in UPSTREAM_GATES + ["fork_only"]]
check("workflow: every gate output is a plain `a || b || c` of step outputs (as read here)",
      all(re.fullmatch(r"\$\{\{ (steps\.\w+\.outputs\.\w+ \|\| )*(steps\.\w+\.outputs\.\w+|'false') \}\}", v) for v in simple),
      "; ".join(simple))
fo = outputs(ctx("pull_request", fork_only_out=FORK_ONLY_OUT, detect=DETECT_ALL))
check("workflow: on a fork-only pull request every gate of `changes` is false and fork_only is true",
      fo.get("fork_only") == "true" and all(fo.get(g) == "false" for g in UPSTREAM_GATES if g != "test_packages"),
      json.dumps(fo))
mixed = outputs(ctx("pull_request", fork_only_out={"fork_only": "false"}, detect=DETECT_ALL))
check("workflow: on any other pull request the detector's gates stand",
      mixed.get("fork_only") == "false" and all(mixed.get(g) == "true" for g in UPSTREAM_GATES if g != "test_packages"),
      json.dumps(mixed))
release = outputs(ctx("workflow_call", release=True, detect={}))
check("workflow: a release run keeps every gate true", all(release.get(g) == "true" for g in UPSTREAM_GATES if g != "test_packages"),
      json.dumps(release))
route_gates = steps.get("route", {}).get("env", {}).get("GATES", "")
check("workflow: the runner routing gets the fork_only step's gates whenever it printed them",
      route_gates == "${{ steps.fork_only.outputs.typecheck && toJSON(steps.fork_only.outputs) || toJSON(steps.detect.outputs) }}",
      route_gates)
check("workflow: ... which it does exactly when it prints typecheck (with every other gate)",
      "typecheck" in UPSTREAM_GATES and runs_everything(["fork_only=false"]), route_gates)

# A partly fork-owned pull request: the script's gates for the other paths win over the detector's (all true here, as
# the fork-owned paths made it run everything).
NARROW_SERVER = {"fork_only": "false", "typecheck": "true", "build": "true", "desktop_launch": "false", "test": "true",
                 "test_packages": "@t3tools/evals @t3tools/scripts", "test_web": "false", "test_server": "true",
                 "rust": "false", "release_smoke": "false", "mobile_native": "false"}
DETECT_ALL_MOBILE_OFF = {**DETECT_ALL, "mobile_native": "false"}
narrow = outputs(ctx("pull_request", fork_only_out=NARROW_SERVER, detect=DETECT_ALL_MOBILE_OFF))
check("workflow: on a partly fork-owned pull request the script's gates stand over the detector's",
      all(narrow.get(g) == NARROW_SERVER[g] for g in UPSTREAM_GATES), json.dumps(narrow))
check("workflow: the macOS lint rule still adds fork-tests.yml on top of the script's gates",
      fork_step(NARROW_SERVER, DETECT_ALL_MOBILE_OFF, ".github/workflows/fork-tests.yml\nfork-ci/x.py\n") == "true"
      and fork_step(NARROW_SERVER, DETECT_ALL_MOBILE_OFF, "fork-ci/x.py\n") == "false"
      and fork_step({"fork_only": "false"}, {**DETECT_ALL, "mobile_native": "true"}, "apps/mobile/a.swift\n") == "true"
      and fork_step({}, {}, "") == "", "fork step")

# Jobs that run on a fork-only pull request: they test the fork's own files (Lint too: `vp check` formats and lints
# every file, the fork's included, and knip reads .github/**/*.cjs; fork_checks_ran checks that fork-checks.yml ran).
# Every other job is an app job.
RUNS = {"outside_pr", "changes", "fleet_watch", "fork_checks_ran", "workflows", "action_tests", "kit_tests",
        "kit_root_tests", "secrets",
        "lint",
        "fork_ci_result"}
app_jobs = sorted(set(jobs) - RUNS)
check("workflow: the app jobs are the ones this switch is for",
      {"build", "test", "test_web", "test_server", "rust", "release_smoke", "mobile_native_static_analysis"} <= set(app_jobs),
      ", ".join(app_jobs))


def job_if(name, needs_outputs, event="pull_request", status="success"):
    c = ctx(event, status=status)
    c["needs"] = {"changes": {"outputs": needs_outputs}}
    return gh_eval(jobs[name].get("if", "${{ true }}").strip().removeprefix("${{").removesuffix("}}"), c)


ran_on_fork_only = [j for j in app_jobs if job_if(j, fo)]
check("workflow: on a fork-only pull request every app job is skipped by its if:", not ran_on_fork_only,
      ", ".join(ran_on_fork_only))
skipped_mixed = [j for j in app_jobs if not job_if(j, mixed)]
check("workflow: on a pull request the script prints no gates for, every app job runs", not skipped_mixed,
      ", ".join(skipped_mixed))
ran_narrow = sorted(j for j in app_jobs if job_if(j, {**narrow, "mobile_native": "false"}))
check("workflow: a partly fork-owned server change runs Build, Test and Test Server, and skips Test Web, Rust, "
      "Release Smoke and the macOS lint", ran_narrow == ["build", "test", "test_server"], ", ".join(ran_narrow))
web_only = {**NARROW_SERVER, "test_web": "true", "test_server": "false", "test_packages": "@t3tools/scripts"}
ran_web = sorted(j for j in app_jobs if job_if(j, web_only))
check("workflow: a change the server does not depend on skips every Test Server shard at the job level",
      "test_server" not in ran_web and "test_web" in ran_web, ", ".join(ran_web))
ran_blank = [j for j in app_jobs if not job_if(j, {})]
check("workflow: with no gate outputs at all (a broken `changes`), every app job's if: still runs it",
      not ran_blank, ", ".join(ran_blank))
ts_steps = jobs["test_server"]["steps"]
check("workflow: Test Server's steps no longer re-run the detector (its job gate decides)",
      not any("ci-changes" in str(st.get("uses", "")) for st in ts_steps)
      and not any("steps.changes" in str(st.get("if", "")) for st in ts_steps),
      ", ".join(str(st.get("if")) for st in ts_steps if st.get("if")))
main_push = {**DETECT_ALL, "fork_only": "false"}
skipped_push = [j for j in app_jobs if not job_if(j, main_push, event="push")]
check("workflow: on a push to main every app job runs", not skipped_push, ", ".join(skipped_push))
kept = [j for j in sorted(RUNS - {"outside_pr", "fleet_watch", "fork_ci_result"}) if not job_if(j, fo)]
check("workflow: on a fork-only pull request the fork's own jobs and Lint still run", not kept, ", ".join(kept))

# Files an app job names (or the routing every app job uses) must never be fork-owned: a fork-only pull request
# changing one would skip the only jobs that use it.
named = set()
for name in app_jobs + ["changes"]:
    text = yaml.safe_dump(jobs[name], width=10_000)
    named |= {m.rstrip("/") + ("/x" if m.endswith("/") else "") for m in re.findall(r"(?<![\w.-])/?(\.github/[\w./-]+|fork-(?:ci|kit)/[\w./-]*)", text)}
# A runbook an error message points to (.md) is not read by the job.
named = {n.lstrip("/") for n in named if not n.endswith(".md")}
bad = sorted(p for p, v in owned(sorted(named)).items() if v)
check("workflow: no file an app job or `changes` names is fork-owned", not bad and len(named) > 5,
      f"owned: {', '.join(bad)}; named: {', '.join(sorted(named))}")

# --- result --------------------------------------------------------------------------------------
result_job = jobs["fork_ci_result"]
result_step = next(s for s in result_job["steps"] if s.get("name") == "Require every job to pass")


def result_run(change_outputs, skipped, overrides=None):
    results = {j: {"result": "skipped" if j in skipped else "success", "outputs": {}} for j in result_job["needs"]}
    results["changes"]["outputs"] = {**change_outputs, "fleet_watch": "false"}
    results["outside_pr"]["result"] = "skipped"
    results["fleet_watch"]["result"] = "skipped"
    for j, r in (overrides or {}).items():
        results[j]["result"] = r
    with tempfile.NamedTemporaryFile(prefix="fork-owned-paths-summary-") as summary:
        r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", result_step["run"]],
                           env={"PATH": os.environ["PATH"], "RESULTS": json.dumps(results), "RELEASE_RUN": "false",
                                "OUTSIDE_PR": "false", "GITHUB_STEP_SUMMARY": summary.name}, capture_output=True, text=True)
        return r.returncode, pathlib.Path(summary.name).read_text() + r.stderr


code, text = result_run(fo, set(app_jobs))
check("result: a fork-only run with every app job skipped passes, and its summary says why",
      code == 0 and "Fork-only pull request" in text and "no reason: fails" not in text, text[-600:])
code, text = result_run({**fo, "test_server": "true"}, set(app_jobs))
check("result: a skipped Test Server whose gate is not false still fails", code != 0, text[-300:])
code, text = result_run(fo, set(app_jobs), {"lint": "skipped"})
check("result: a skipped Lint on a fork-only run still fails", code != 0, text[-300:])
code, text = result_run(fo, set(app_jobs), {"kit_tests": "failure"})
check("result: a failed fork job on a fork-only run fails", code != 0, text[-300:])
code, text = result_run(mixed, set())
check("result: a run with every job passed passes", code == 0 and "Fork-only" not in text, text[-300:])
code, text = result_run(mixed, set(app_jobs))
check("result: the app jobs skipped on a run that is not fork-only fails", code != 0, text[-300:])
narrow_skipped = set(app_jobs) - {"build", "test", "test_server"}
code, text = result_run(NARROW_SERVER, narrow_skipped)
check("result: a partly fork-owned run with the app jobs its gates skip skipped passes, each with its reason",
      code == 0 and "no reason: fails" not in text and "Fork-only" not in text, text[-600:])
code, text = result_run(NARROW_SERVER, narrow_skipped | {"test_server"})
check("result: ... but Test Server skipped while its gate is true fails", code != 0, text[-300:])
code, text = result_run({**NARROW_SERVER, "test_server": "false"}, narrow_skipped | {"test_server"})
check("result: Test Server skipped by its job gate passes, with its reason",
      code == 0 and "affects nothing t3" in text, text[-600:])

print(f"\n{len(failures)} failed" if failures else f"\nall passed: {len(oks)} ok, 0 skipped")
sys.exit(1 if failures else 0)
