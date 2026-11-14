"""Tests .github/scripts/fork-ci-release.py (the fork-ci release) against throwaway local repositories
(file:// remotes, so the partial fetch the real run uses is exercised), the real tree, the real
config, the ruleset file and .github/workflows/fork-ci-release.yml.

    python3 .github/scripts/fork-ci-release.test.py

Needs git, node and PyYAML. Reaches no network and no GitHub. Each check prints ok/FAIL; any FAIL
exits 1. Each row of the failure table (first release, re-run, half states, main moving, a push
refused half-way) is one block below.
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

sys.dont_write_bytecode = True  # importing fork-ci-release.py must not leave __pycache__ in the tree
root = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fork_ci_release", root / ".github/scripts/fork-ci-release.py")
rel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rel)
failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok or not detail else f"\n     | {detail}"))
    if not ok:
        failures.append(name)


work = pathlib.Path(tempfile.mkdtemp(prefix="fork-ci-release-test-"))
gitconfig = work / "gitconfig"
gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n"
                     "[protocol \"file\"]\n\tallow = always\n")
for k in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "GIT_DIR", "GIT_INDEX_FILE"):
    os.environ.pop(k, None)
os.environ.update(GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1", GH_CONFIG_DIR=str(work / "gh"))

CFG = {"sourceBranch": "main", "requiredCheck": "fork-ci-result", "branch": "ci-release", "tagPrefix": "ci/v",
       "legacyTags": ["ci/v1"], "paths": ["LICENSE", "fork-ci", ".github/workflows/fork-pr-queue.yml"]}
CALL_ONLY = "on:\n  workflow_call:\n    inputs:\n      x:\n        type: string\njobs: {}\n"


def sh(cwd, *args, input=None, check_=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, input=input)
    if check_ and r.returncode:
        raise RuntimeError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


class World:
    """A bare 'GitHub' remote and a clone that pushes main to it."""

    def __init__(self, name):
        self.dir = work / name
        self.remote = self.dir / "remote.git"
        self.src = self.dir / "src"
        self.url = f"file://{self.remote}"
        sh(work, "init", "-q", "--bare", str(self.remote))
        for k, v in (("uploadpack.allowFilter", "true"), ("uploadpack.allowAnySHA1InWant", "true")):
            sh(self.remote, "config", k, v)
        sh(work, "init", "-q", str(self.src))
        self.write({"LICENSE": "MIT\n", "README.md": "upstream\n",
                    "fork-ci/actions/a/action.yml": "name: a\nruns: {using: composite, steps: []}\n",
                    "fork-ci/actions/a/a.py": "print('a')\n",
                    ".github/workflows/fork-pr-queue.yml": CALL_ONLY,
                    ".github/workflows/fork-tests.yml": "on:\n  push:\njobs: {}\n"})
        os.chmod(self.src / "fork-ci/actions/a/a.py", 0o755)
        (self.src / ".repos/x/.vscode").mkdir(parents=True)
        os.symlink("does/not/exist", self.src / ".repos/x/.vscode/tsgo")  # upstream's dangling symlink
        self.first = self.commit("upstream + fork")
        sh(self.src, "tag", "-a", "ci/v1", "-m", "ci/v1", self.first)
        sh(self.src, "push", "-q", self.url, "main", "ci/v1")

    def write(self, files):
        for path, text in files.items():
            p = self.src / path
            if text is None:
                p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def commit(self, msg, push=False):
        sh(self.src, "add", "-A")
        sh(self.src, "commit", "-q", "--allow-empty", "-m", msg)
        sha = sh(self.src, "rev-parse", "HEAD")
        if push:
            sh(self.src, "push", "-q", "-f", self.url, "main")
        return sha

    def refs(self):
        out = sh(self.remote, "for-each-ref", "--format=%(refname) %(objectname)")
        return dict(line.split() for line in out.splitlines())

    def run(self, source=None, write=True, cfg=CFG, **kw):
        """('released'|'none'|'refused', result or message)."""
        try:
            return "ok", rel.release(self.url, source or sh(self.remote, "rev-parse", "main"), cfg, write,
                                     log=lambda *_: None, **kw)
        except rel.Skip as e:
            return "none", str(e)
        except rel.Refuse as e:
            return "refused", str(e)


def tree_of(w, commit):
    out = sh(w.remote, "ls-tree", "-r", "--full-tree", commit)
    return {line.split("\t")[1]: line.split()[0] for line in out.splitlines()}


# --- first release ever (no branch; only the legacy tag ci/v1) -----------------------------------
w = World("main")
before = w.refs()
kind, plan = w.run(write=False)
check("first release: plan says release ci/v2 and writes nothing",
      kind == "ok" and plan["action"] == "release" and plan["tag"] == "ci/v2" and w.refs() == before, str(plan))
kind, r = w.run()
refs = w.refs()
v2 = r["sha"] if kind == "ok" else ""
check("first release: released ci/v2", kind == "ok" and r["action"] == "released" and r["tag"] == "ci/v2", str(r))
check("first release: branch ci-release and tag ci/v2 name the same new commit",
      refs.get("refs/heads/ci-release") == v2 and sh(w.remote, "rev-parse", "ci/v2^{commit}") == v2)
check("first release: ci/v2 is an annotated tag",
      sh(w.remote, "cat-file", "-t", refs.get("refs/tags/ci/v2", "x"), check_=False) == "tag")
check("first release: ci/v1 (legacy) did not move", refs.get("refs/tags/ci/v1") == before["refs/tags/ci/v1"])
files = tree_of(w, v2)
check("first release: only the configured paths, at the same paths as on main",
      set(files) == {"LICENSE", "fork-ci/actions/a/action.yml", "fork-ci/actions/a/a.py",
                     ".github/workflows/fork-pr-queue.yml"}, str(sorted(files)))
check("first release: no symlink, only plain files", set(files.values()) <= {"100644", "100755"}, str(files))
check("first release: an executable keeps its mode", files.get("fork-ci/actions/a/a.py") == "100755")
check("first release: the release commit has no parent and names its source",
      sh(w.remote, "rev-list", "--parents", "-n1", v2) == v2 and
      f"Fork-CI-Source: {w.first}" in sh(w.remote, "log", "-1", "--format=%B", v2))
check("first release: the files are the source's files",
      all(sh(w.remote, "rev-parse", f"{v2}:{p}") == sh(w.remote, "rev-parse", f"{w.first}:{p}") for p in files))

# --- re-run: same main commit, or a main commit that does not change the release files -------------
kind, msg = w.run()
check("rerun: same main commit releases nothing and moves nothing", kind == "none" and w.refs() == refs, msg)
w.write({"README.md": "upstream moved on\n"})
w.commit("unrelated upstream change", push=True)
refs = w.refs()
kind, msg = w.run()
check("rerun: a main commit that leaves the release files alone releases nothing",
      kind == "none" and "already holds" in msg and w.refs() == refs, msg)

# --- a second release: parent is the previous release, ci/v2 never moves --------------------------
w.write({"fork-ci/actions/a/a.py": "print('a2')\n", "fork-ci/actions/b/action.yml": "name: b\n"})
w.commit("fork-ci change", push=True)
v2_tag = w.refs()["refs/tags/ci/v2"]
kind, r = w.run()
v3 = r["sha"] if kind == "ok" else ""
check("next release: ci/v3 whose parent is ci/v2's commit",
      kind == "ok" and r["tag"] == "ci/v3" and sh(w.remote, "rev-parse", f"{v3}^") == v2, str(r))
check("next release: ci/v2 still names the same tag object and commit",
      w.refs()["refs/tags/ci/v2"] == v2_tag and sh(w.remote, "rev-parse", "ci/v2^{commit}") == v2)

# --- what a release tree may hold --------------------------------------------------------------
def refused_tree(label, files, expect, mode=None, link=None):
    t = World(label)
    t.write(files)
    if link:
        os.symlink(link[1], t.src / link[0])
    sh(t.src, "add", "-A")
    if mode:  # after add -A, which would drop an index entry with nothing on disk
        sh(t.src, "update-index", "--add", "--cacheinfo", mode)
    sh(t.src, "commit", "-q", "-m", label)
    sh(t.src, "push", "-q", t.url, "main")
    refs = t.refs()
    kind, msg = t.run()
    check(f"tree: {label} is refused, nothing written",
          kind == "refused" and expect in msg and t.refs() == refs, f"{kind}: {msg}")


refused_tree("a symlink in fork-ci", {}, "symlink", link=("fork-ci/actions/a/link", "../../../README.md"))
refused_tree("a dangling symlink in fork-ci", {}, "symlink", link=("fork-ci/actions/a/tsgo", "nowhere"))
refused_tree("a submodule in fork-ci", {}, "commit", mode=f"160000,{'1' * 40},fork-ci/actions/sub")
refused_tree("a release workflow that starts on push",
             {".github/workflows/fork-pr-queue.yml": "on:\n  push:\n  workflow_call:\njobs: {}\n"}, "workflow_call only")
refused_tree("a release workflow whose triggers cannot be read",
             {".github/workflows/fork-pr-queue.yml": "on: &x\n  workflow_call:\njobs: {}\n"}, "cannot tell")
refused_tree("a configured path missing on main", {"LICENSE": None}, "LICENSE is not in")

# --- half states: tag pushed but branch push failed, and the other way round ---------------------
h = World("half")
h.run()
v2 = sh(h.remote, "rev-parse", "ci-release")
h.write({"fork-ci/actions/a/a.py": "print('half')\n"})
h.commit("change", push=True)
orphan = sh(h.remote, "commit-tree", f"{v2}^{{tree}}", "-p", v2, input="ci/v3 whose branch push failed\n")
sh(h.remote, "update-ref", "refs/tags/ci/v3", orphan)
refs = h.refs()
kind, msg = h.run()
check("half state: tag ci/v3 pushed, branch not: refused, nothing written, the message gives the repair",
      kind == "refused" and "ci/v3" in msg and f"git push <remote> {orphan}:refs/heads/ci-release" in msg
      and h.refs() == refs, msg)
sh(h.remote, "update-ref", "refs/heads/ci-release", orphan, v2)  # the repair the message gives
kind, r = h.run()
check("half state: after the repair the next release is ci/v4 on top of ci/v3",
      kind == "ok" and r["tag"] == "ci/v4" and sh(h.remote, "rev-parse", f"{r['sha']}^") == orphan, str(r))

b = World("branch-only")
b.run()
v2 = sh(b.remote, "rev-parse", "ci-release")
untagged = sh(b.remote, "commit-tree", f"{v2}^{{tree}}", "-p", v2, input="tag push failed\n")
sh(b.remote, "update-ref", "refs/heads/ci-release", untagged, v2)
b.write({"fork-ci/actions/a/a.py": "print('b')\n"})
b.commit("change", push=True)
refs = b.refs()
kind, msg = b.run()
check("half state: branch pushed, tag not: refused, nothing written, says to tag the head",
      kind == "refused" and untagged in msg and "git tag -a ci/v3" in msg and b.refs() == refs, msg)

n = World("no-branch")
n.run()
sh(n.remote, "update-ref", "-d", "refs/heads/ci-release")
n.write({"fork-ci/actions/a/a.py": "print('n')\n"})
n.commit("change", push=True)
refs = n.refs()
kind, msg = n.run()
check("half state: release tags but no branch: refused, nothing written",
      kind == "refused" and "branch ci-release does not" in msg and n.refs() == refs, msg)

# --- main moved: before the run, and during it ---------------------------------------------------
m = World("moved")
old = sh(m.remote, "rev-parse", "main")
m.write({"fork-ci/actions/a/a.py": "print('m')\n"})
m.commit("newer", push=True)
kind, msg = m.run(source=old)
check("main moved before the run: releases nothing (the newer commit's run does)",
      kind == "none" and "not " + old in msg and "refs/heads/ci-release" not in m.refs(), msg)


def move_main():
    m.write({"fork-ci/actions/a/a.py": "print('m2')\n"})
    m.commit("moved during the run", push=True)


kind, msg = m.run(before_push=move_main)
check("main moved during the run: nothing pushed, exit 0 with the reason",
      kind == "none" and "moved during the run" in msg and "refs/heads/ci-release" not in m.refs(), msg)
kind, r = m.run()
check("main moved: the next run releases the new tip", kind == "ok" and r["source"] == sh(m.remote, "rev-parse", "main"))

c = World("race")
mine = sh(c.remote, "rev-parse", "main")
kind, msg = c.run(before_push=lambda: sh(c.remote, "update-ref", "refs/tags/ci/v2", mine))
check("a tag made by someone else during the run is never moved: refused, nothing pushed",
      kind == "refused" and sh(c.remote, "rev-parse", "ci/v2") == mine and "refs/heads/ci-release" not in c.refs(), msg)

# --- the push stops half-way: the remote refuses one of the two refs -------------------------------
p = World("atomic")
hook = p.remote / "hooks/update"
hook.write_text('#!/bin/sh\ncase "$1" in refs/tags/*) echo "no tags today" >&2; exit 1;; esac\nexit 0\n')
hook.chmod(0o755)
kind, msg = p.run()
check("push refused for the tag only: refused, and the branch was not written either (atomic)",
      kind == "refused" and "refs/heads/ci-release" not in p.refs() and "refs/tags/ci/v2" not in p.refs(), msg)
hook.unlink()
kind, r = p.run()
check("push refused: a re-run once the remote accepts releases ci/v2", kind == "ok" and r["tag"] == "ci/v2", str(r))

# --- the required check (fork-ci-result) ---------------------------------------------------------
q = World("checks")
for verdict, want in (("pending", "none"), ("failure", "refused"), ("success", "ok")):
    kind, msg = q.run(required=lambda sha, v=verdict: (v, f"fork-ci-result {v}"))
    check(f"required check {verdict}: {want}", kind == want, str(msg))
PUSH, DISPATCH = 501, 502  # check suite ids of a push run and a workflow_dispatch run of the same commit
wf_runs = [{"id": 1, "event": "push", "check_suite_id": PUSH},
           {"id": 2, "event": "workflow_dispatch", "check_suite_id": DISPATCH},
           {"id": 3, "event": "pull_request", "check_suite_id": 503},
           {"id": 4, "event": "merge_group", "check_suite_id": 504}]
check("trusted suites: only push and merge_group workflow runs", rel.trusted_suites(wf_runs) == {PUSH, 504},
      str(rel.trusted_suites(wf_runs)))
run = lambda i, status, conclusion, slug="github-actions", suite=PUSH, t="2026-11-14T00:00:0": {  # noqa: E731
    "id": i, "name": "fork-ci-result", "status": status, "conclusion": conclusion, "started_at": f"{t}{i}Z",
    "app": {"slug": slug}, "check_suite": {"id": suite}}
for label, runs, want in (
        ("none at all is pending", [], "pending"),
        ("a push run that passed is green", [run(1, "completed", "success")], "success"),
        ("a newer passing workflow_dispatch run does not hide an older failed push run",
         [run(1, "completed", "failure"), run(2, "completed", "success", suite=DISPATCH)], "failure"),
        ("only workflow_dispatch runs (even green) are not green: pending",
         [run(1, "completed", "success", suite=DISPATCH)], "pending"),
        ("a pull_request run does not count", [run(1, "completed", "success", suite=503)], "pending"),
        ("any failed push run refuses, even under a newer pass (a re-run)",
         [run(1, "completed", "failure"), run(2, "completed", "success")], "failure"),
        ("an old pass, a new failure", [run(1, "completed", "success"), run(2, "completed", "failure")], "failure"),
        ("a push run in progress is pending", [run(1, "completed", "success"), run(2, "in_progress", None)], "pending"),
        ("a cancelled run is not a pass", [run(1, "completed", "cancelled")], "failure"),
        ("a check of the same name from another app does not count", [run(1, "completed", "success", "evil")], "pending")):
    got, why = rel.check_verdict(runs, "fork-ci-result", rel.trusted_suites(wf_runs))
    check(f"check verdict: {label}", got == want, f"{got}: {why}")

# The same through main(): the API answers come from the fake gh_get, so the wiring (check runs
# matched to the commit's push workflow runs) is what decides.
g = World("gate")
gate_sha = sh(g.remote, "rev-parse", "main")
for label, checks_, want in (
        ("main(): a green dispatch run over a red push run refuses",
         [run(1, "completed", "failure"), run(2, "completed", "success", suite=DISPATCH)], 1),
        ("main(): only a green dispatch run does nothing (pending)", [run(2, "completed", "success", suite=DISPATCH)], 0),
        ("main(): a green push run plans the release", [run(1, "completed", "success")], 0)):
    asked = []

    def fake_gh_get(path, checks_=checks_):
        asked.append(path)
        if "/check-runs?" in path:
            return {"check_runs": checks_}
        if "/actions/runs?" in path:
            return {"workflow_runs": wf_runs}
        raise AssertionError(path)
    real, rel.gh_get = rel.gh_get, fake_gh_get
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = rel.main(["plan", "--require-check", "--remote", g.url, "--repo", "o/r", "--source", gate_sha])
    finally:
        rel.gh_get = real
    check(label, code == want and any(f"head_sha={gate_sha}" in x for x in asked)
          and ("release:" in out.getvalue()) == (label.endswith("plans the release")), f"{code} {out.getvalue()} {asked}")

# --- verify-download (the consumer smoke test's check) -------------------------------------------
v = World("verify")
v.run()
commit = sh(v.remote, "rev-parse", "ci-release")


def unpacked(name):
    d = work / "unpacked" / name
    d.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(v.remote), "archive", commit], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(d)], input=archive, check=True)
    return d


check("verify-download: an unpacked release commit passes", rel.verify_download(unpacked("good"), commit, v.remote) == [])
d = unpacked("extra")
(d / "fork-ci/extra.txt").write_text("x")
check("verify-download: an extra file fails", any("extra.txt" in p for p in rel.verify_download(d, commit, v.remote)))
d = unpacked("changed")
(d / "LICENSE").write_text("other\n")
check("verify-download: a changed file fails", any("LICENSE differs" in p for p in rel.verify_download(d, commit, v.remote)))
d = unpacked("missing")
(d / "LICENSE").unlink()
check("verify-download: a missing file fails", any("LICENSE is missing" in p for p in rel.verify_download(d, commit, v.remote)))
d = unpacked("link")
os.symlink("nowhere", d / "fork-ci/tsgo")
check("verify-download: a symlink fails", any("symlink" in p for p in rel.verify_download(d, commit, v.remote)))
check("verify-download: no folder fails", rel.verify_download(work / "none", commit, v.remote) != [])
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    code = rel.main(["verify-download", "--dir", str(work / "none"), "--commit", commit, "--git-dir", str(v.remote)])
check("verify-download: the command exits 1 on a problem", code == 1)

# --- the command line: exit codes and outputs ------------------------------------------------------
cl = World("cli")
cfg_file = cl.dir / "cfg.json"
cfg_file.write_text(json.dumps(CFG))
out = cl.dir / "out"
os.environ["GITHUB_OUTPUT"] = str(out)
src = sh(cl.remote, "rev-parse", "main")
with contextlib.redirect_stdout(io.StringIO()):
    c1 = rel.main(["release", "--remote", cl.url, "--source", src, "--config", str(cfg_file)])
    c2 = rel.main(["release", "--remote", cl.url, "--source", src, "--config", str(cfg_file)])
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    c3 = rel.main(["release", "--remote", cl.url, "--source", "main", "--config", str(cfg_file)])
lines = out.read_text().splitlines()
os.environ.pop("GITHUB_OUTPUT")
check("cli: release exits 0 and writes action=released, tag and sha",
      c1 == 0 and "action=released" in lines and "tag=ci/v2" in lines and f"sha={sh(cl.remote, 'rev-parse', 'ci/v2^{commit}')}" in lines)
check("cli: the re-run exits 0 with action=none", c2 == 0 and lines.count("action=none") == 1)
check("cli: a source that is not a full SHA is refused (exit 1)", c3 == 1 and "action=refused" in lines)

# --- the real config, the real tree, the ruleset file, the workflow ------------------------------
cfg = rel.load_config()
check("config: the real config reads", cfg["branch"] and cfg["tagPrefix"] and cfg["paths"])
head = sh(root, "rev-parse", "HEAD")
try:
    entries = rel.release_entries(root, head, cfg)
    rel.check_workflows(root, entries)
    real_ok, why = True, ""
except rel.Refuse as e:
    real_ok, why = False, str(e)
check("real tree: this commit's release would hold plain files only and workflow_call-only workflows",
      real_ok and entries, why)
check("real tree: fork-ci-release.yml itself is not released (it starts on schedule)",
      real_ok and not any(p.endswith("fork-ci-release.yml") for _, _, p in entries))
for bad in ("/abs", "a/../b", "dir/", ""):
    t = work / f"cfg-{abs(hash(bad))}.json"
    t.write_text(json.dumps({**CFG, "paths": [bad]}))
    try:
        rel.load_config(t)
        ok = False
    except rel.Refuse:
        ok = True
    check(f"config: path {bad!r} is refused", ok)

rulesets = json.loads((root / ".github/fork-ci-release-rulesets.json").read_text())
by_target = {r["target"]: r for r in rulesets}
tag_rs, branch_rs = by_target.get("tag", {}), by_target.get("branch", {})
tag_patterns = tag_rs.get("conditions", {}).get("ref_name", {}).get("include", [])
check("ruleset: tags matching the release tags (and ci/v1) cannot be deleted or updated; creating them is allowed",
      any(cfg["tagPrefix"].startswith(p[len("refs/tags/"):-1]) for p in tag_patterns if p.endswith("*"))
      and {r["type"] for r in tag_rs.get("rules", [])} == {"deletion", "update"}, json.dumps(tag_rs))
check("ruleset: the release branch cannot be deleted or force-pushed; fast-forward pushes are allowed",
      branch_rs.get("conditions", {}).get("ref_name", {}).get("include") == [f"refs/heads/{cfg['branch']}"]
      and {r["type"] for r in branch_rs.get("rules", [])} == {"deletion", "non_fast_forward"}, json.dumps(branch_rs))
check("ruleset: both active, nobody bypasses",
      all(r.get("enforcement") == "active" and r.get("bypass_actors") == [] for r in rulesets) and len(rulesets) == 2)

wf = yaml.safe_load((root / ".github/workflows/fork-ci-release.yml").read_text())
on = wf.get(True, wf.get("on"))
jobs = wf["jobs"]
check("workflow: starts only on schedule and workflow_dispatch", set(on) == {"schedule", "workflow_dispatch"}, str(on))
check("workflow: one release at a time, never cancelled half-way",
      wf.get("concurrency", {}).get("cancel-in-progress") is False)
check("workflow: no write permission anywhere (the push uses FORK_PUSH_TOKEN)",
      wf.get("permissions") == {} and all("write" not in json.dumps(j.get("permissions", {})) for j in jobs.values()))
uses_token = [jid for jid, j in jobs.items() if "secrets.FORK_PUSH_TOKEN" in json.dumps(j)]
check("workflow: only guard (presence) and release (value) see FORK_PUSH_TOKEN",
      set(uses_token) == {"guard", "release"} and jobs["release"].get("environment") == "fork-push"
      and "secrets.FORK_PUSH_TOKEN != ''" in json.dumps(jobs["guard"]), str(uses_token))
check("workflow: release runs only after this attempt's guard passed",
      jobs["release"].get("if") == "needs.guard.outputs.attempt == github.run_attempt")
rel_run = next((s.get("run", "") for s in jobs["release"]["steps"] if s.get("id") == "release"), "")
check("workflow: release is the script's release with the required check, from this run's commit",
      "fork-ci-release.py release --require-check" in rel_run and '--source "$GITHUB_SHA"' in rel_run)
check("workflow: the plan job writes nothing (plan, no token)",
      "fork-ci-release.py plan --require-check" in json.dumps(jobs["plan"]) and "FORK_PUSH" not in json.dumps(jobs["plan"]))
smoke_uses = [s.get("uses", "") for s in jobs["smoke"]["steps"] if s.get("uses")]
check("workflow: the smoke test runs a released action from a remote ref of the release branch (not ./)",
      smoke_uses == [f"scuttletoby/t3code/fork-ci/actions/job-changed@{cfg['branch']}"], str(smoke_uses))
check("workflow: the smoke test checks the unpacked files and the released SHA",
      "verify-download" in json.dumps(jobs["smoke"]) and "needs.release.outputs.sha" in json.dumps(jobs["smoke"])
      and f"_actions/scuttletoby/t3code/{cfg['branch']}" in json.dumps(jobs["smoke"]))

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
