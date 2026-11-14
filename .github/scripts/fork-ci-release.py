#!/usr/bin/env python3
"""Fork only: release the fork's CI code (fork-ci/) as commits other repositories can pin.

Why: a repository that runs `uses: OWNER/REPO/fork-ci/actions/<name>@<sha>` makes GitHub download the
WHOLE tree at <sha> before the job starts. A commit of this fork's main branch carries upstream's
whole tree, and upstream's tree can hold things the runner cannot unpack (ci/v1, a main commit,
fails every consumer job at "Set up job" on a dangling symlink under .repos/). Main is also rebased
onto upstream, so only a tag keeps one of its commits alive.

A release commit holds only the files in .github/fork-ci-release.json `paths` (fork-ci/, the
reusable workflows consumers call, scripts an action runs from outside fork-ci/, LICENSE), at the same paths as on the source branch, so a
consumer's `uses:` path never changes. It is built with git plumbing from one source-branch commit,
its parent is the previous release commit, and it is pushed as the branch `branch` (ci-release) and
the annotated tag <tagPrefix><N> (ci/vN) in ONE atomic push. Consumers pin `@<release sha> # ci/vN`.

  python3 -I .github/scripts/fork-ci-release.py plan    --remote URL --source SHA [--require-check] [--repo OWNER/NAME]
  python3 -I .github/scripts/fork-ci-release.py release --remote URL --source SHA [--require-check] [--repo OWNER/NAME]
  python3 -I .github/scripts/fork-ci-release.py verify-download --dir DIR --commit SHA [--git-dir DIR]

plan says what a release would do and writes nothing. release does it. Both refuse (exit 1, the
reason on stderr and in $GITHUB_STEP_SUMMARY) on:
  - a half state: a ci/vN tag (not in legacyTags) that is not on the branch (tag pushed, branch
    not), a branch head that no ci/vN tag names (branch pushed, tag not), tags but no branch;
  - a release tree that would hold a symlink, a submodule, anything but a plain file, a missing
    path, or a workflow that starts on anything but workflow_call;
  - a source whose required check (fork-ci-result) failed in any push run of it (--require-check;
    only runs a push or merge queue started count, never a workflow_dispatch);
  - a push the remote refused (nothing is half-written: the push is atomic).
They do nothing and exit 0 when the source branch has moved past --source (the run for the newer
commit releases it), when the required check has not finished yet, and when the release tree is the
same as the branch head's (a re-run, or a source that did not change fork-ci). An existing tag is
never moved or deleted: the push forces nothing, so the branch moves only fast-forward and a tag
that exists is refused.

verify-download checks that DIR (the folder the runner unpacked a remote `uses:` into) holds exactly
the files of COMMIT: the consumer smoke test of fork-ci-release.yml.

No dependencies but git and node (node runs fork-workflow-triggers.cjs). The work happens in a
temporary repository: the caller's checkout is never touched. Credentials for the push come from
git's own configuration (fork-ci-release.yml passes FORK_PUSH_TOKEN as GIT_CONFIG_* variables).
Output (action, tag, sha, source, reason) goes to $GITHUB_OUTPUT when it is set.
Tests: .github/scripts/fork-ci-release.test.py.
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = HERE.parent / "fork-ci-release.json"
TRIGGERS = HERE / "fork-workflow-triggers.cjs"
SHA = re.compile(r"^[0-9a-f]{40}$")
PLAIN_FILE = {"100644", "100755"}


class Refuse(Exception):
    """A state this tool will not act on; the message says why and what to do."""


class Skip(Exception):
    """Nothing to do now (exit 0); the message says why."""


def load_config(path=CONFIG):
    cfg = json.loads(pathlib.Path(path).read_text())
    for key in ("sourceBranch", "branch", "tagPrefix", "paths"):
        if not cfg.get(key):
            raise Refuse(f"{path}: `{key}` is missing")
    cfg.setdefault("legacyTags", [])
    cfg.setdefault("requiredCheck", "")
    for p in cfg["paths"]:
        if p.startswith("/") or p.endswith("/") or ".." in p.split("/") or not p:
            raise Refuse(f"{path}: path {p!r} is not a plain path from the repository root")
    return cfg


def git(repo, *args, input=None, env=None, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], input=input, capture_output=True,
                       env=env, encoding="utf-8", errors="surrogateescape")
    if check and r.returncode:
        raise Refuse(f"git {' '.join(args[:3])} failed: {r.stderr.strip()[-800:]}")
    return r


# --- remote state ------------------------------------------------------------------------------
def remote_refs(repo, remote, cfg):
    """{ref: sha} for the source branch, the release branch and the release tags; a tag's peeled
    commit is under ref + '^{}' (annotated tags)."""
    r = git(repo, "ls-remote", remote, f"refs/heads/{cfg['sourceBranch']}", f"refs/heads/{cfg['branch']}",
            f"refs/tags/{cfg['tagPrefix']}*")
    refs = {}
    for line in r.stdout.splitlines():
        sha, ref = line.split("\t", 1)
        refs[ref] = sha
    return refs


def release_tags(refs, cfg):
    """({name: (number, commit)}, [names that do not read as <prefix><N>])."""
    prefix = "refs/tags/" + cfg["tagPrefix"]
    tags, odd = {}, []
    for ref, sha in refs.items():
        if not ref.startswith(prefix) or ref.endswith("^{}"):
            continue
        name = ref[len("refs/tags/"):]
        m = re.fullmatch(r"[1-9][0-9]*", ref[len(prefix):])
        if not m:
            odd.append(name)
            continue
        tags[name] = (int(m.group(0)), refs.get(ref + "^{}", sha))
    return tags, odd


def check_state(refs, cfg, on_branch):
    """The previous release commit (None for the first release) and the next tag, or Refuse for a
    half state. on_branch(commit) says whether commit is the branch head or one of its ancestors."""
    tags, _ = release_tags(refs, cfg)
    branch = refs.get(f"refs/heads/{cfg['branch']}")
    numbers = [n for n, _ in tags.values()]
    next_tag = f"{cfg['tagPrefix']}{max(numbers, default=0) + 1}"
    ours = {name: (n, c) for name, (n, c) in tags.items() if name not in cfg["legacyTags"]}
    if branch is None:
        if ours:
            name, (_, commit) = max(ours.items(), key=lambda kv: kv[1][0])
            raise Refuse(
                f"half state: tag {name} exists but the branch {cfg['branch']} does not (a release whose branch push "
                f"failed, or a deleted branch). If {name} ({commit}) is a release commit, restore the branch: "
                f"git push <remote> {commit}:refs/heads/{cfg['branch']}")
        return None, next_tag
    for name, (_, commit) in sorted(ours.items(), key=lambda kv: kv[1][0]):
        if not on_branch(commit):
            raise Refuse(
                f"half state: tag {name} ({commit}) is not on the branch {cfg['branch']} (at {branch}): a release "
                f"whose branch push failed? If {name}'s parent is {branch}, move the branch to it: "
                f"git push <remote> {commit}:refs/heads/{cfg['branch']}")
    newest = max(ours.items(), key=lambda kv: kv[1][0], default=None)
    if newest is None or newest[1][1] != branch:
        named = f"the newest tag {newest[0]} names {newest[1][1]}" if newest else "no release tag exists"
        raise Refuse(
            f"half state: the branch {cfg['branch']} is at {branch}, but {named} (a release whose tag push failed?). "
            f"Tag the branch head as {next_tag} with git tag -a {next_tag} {branch} -m '{next_tag}' and push the tag, "
            f"after checking that {branch} is a release commit")
    return branch, next_tag


# --- the release tree --------------------------------------------------------------------------
def release_entries(repo, source, cfg):
    """[(mode, oid, path)] of the release tree; Refuse when it would hold anything but plain files."""
    r = git(repo, "--literal-pathspecs", "ls-tree", "-r", "-z", "--full-tree", source, "--", *cfg["paths"])
    entries = []
    for item in filter(None, r.stdout.split("\0")):
        meta, path = item.split("\t", 1)
        mode, kind, oid = meta.split()
        if mode == "120000":
            raise Refuse(f"{path} is a symlink: a release holds no symlinks (the runner cannot unpack a dangling one)")
        if kind != "blob" or mode not in PLAIN_FILE:
            raise Refuse(f"{path} is a {kind} (mode {mode}): a release holds only plain files")
        entries.append((mode, oid, path))
    for p in cfg["paths"]:
        if not any(path == p or path.startswith(p + "/") for _, _, path in entries):
            raise Refuse(f"{p} is not in {source}: every path in the config must be there")
    return entries


def ensure_objects(repo, remote, tree):
    """Fetch, in one fetch, the files of TREE that a partial (blob:none) fetch left out."""
    def missing():
        r = git(repo, "rev-list", "--objects", "--missing=print", "--no-object-names", tree)
        return [line[1:] for line in r.stdout.splitlines() if line.startswith("?")]
    gone = missing()
    if gone:
        git(repo, "-c", "fetch.negotiationAlgorithm=noop", "fetch", "-q", "--no-tags", "--no-write-fetch-head",
            "--filter=blob:none", "--stdin", remote, input="\n".join(gone) + "\n")
        gone = missing()
        if gone:
            raise Refuse(f"{len(gone)} file(s) of the release could not be fetched, e.g. {gone[0]}")


def workflow_triggers(text):
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
        f.write(text)
    try:
        r = subprocess.run(["node", str(TRIGGERS), f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    if r.returncode:
        return None, r.stderr.strip()
    return set(r.stdout.split()), ""


def check_workflows(repo, entries):
    """Every workflow in the release starts only on workflow_call: pushing the release branch or
    tag must start nothing in this repository."""
    for _, oid, path in entries:
        if not re.match(r"\.github/workflows/[^/]+$", path):
            continue
        events, why = workflow_triggers(git(repo, "cat-file", "blob", oid).stdout)
        if events is None:
            raise Refuse(f"{path}: cannot tell what starts it ({why}); a release workflow must be workflow_call only")
        if events != {"workflow_call"}:
            raise Refuse(f"{path} starts on {', '.join(sorted(events))}: a release workflow must be workflow_call only "
                         "(pushing the release branch would start it)")


def write_tree(repo, entries):
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(d, "index")}
        info = "".join(f"{mode} {oid}\t{path}\0" for mode, oid, path in entries)
        git(repo, "update-index", "-z", "--add", "--index-info", input=info, env=env)
        return git(repo, "write-tree", "--missing-ok", env=env).stdout.strip()


# --- the required check ------------------------------------------------------------------------
# Only check runs of workflow runs that a push to the source branch (or a merge queue) started count.
# A workflow_dispatch of fork-tests.yml with `ref=<other commit>` runs on the source tip's SHA (GitHub
# records the dispatching branch's head) but tests another commit, so its fork-ci-result says nothing
# about the tip; pull_request runs test a merge commit. Those are matched by check suite and ignored.
TRUSTED_EVENTS = ("push", "merge_group")


def check_verdict(runs, name, suites):
    """'success', 'pending' or 'failure' from the check runs named NAME of a commit. Only GitHub
    Actions check runs whose check suite is in SUITES (the suites of the commit's push and merge_group
    workflow runs, see trusted_suites) count. Any counted failure (or cancel) is a failure, even when a
    newer counted run passed: a re-run that went green does not hide a red push run (the next commit
    on the source branch releases instead). Otherwise any counted run still going is pending, and no
    counted run at all is pending too: a commit tested only by hand (workflow_dispatch) is not green,
    and its push run may not have started yet, so the next scheduled run looks again."""
    mine = [r for r in runs if r.get("name") == name and (r.get("app") or {}).get("slug") == "github-actions"
            and (r.get("check_suite") or {}).get("id") in suites]
    if not mine:
        others = sum(1 for r in runs if r.get("name") == name)
        return "pending", (f"no {name} check from a push run on it yet"
                           + (f" ({others} from other runs, e.g. workflow_dispatch, do not count)" if others else ""))
    bad = [r for r in mine if r.get("status") == "completed" and r.get("conclusion") != "success"]
    if bad:
        return "failure", f"{name} ended {bad[0].get('conclusion')} in a push run (check run {bad[0].get('id')})"
    going = [r for r in mine if r.get("status") != "completed"]
    if going:
        return "pending", f"{name} is {going[0].get('status')}"
    return "success", f"{name} passed"


def trusted_suites(workflow_runs):
    """The check suite ids of the workflow runs that TRUSTED_EVENTS started."""
    return {r.get("check_suite_id") for r in workflow_runs
            if r.get("event") in TRUSTED_EVENTS and r.get("check_suite_id") is not None}


def gh_get(path):
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    req = urllib.request.Request(f"{api}{path}", headers={"Accept": "application/vnd.github+json"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_check_runs(repo_name, sha, name):
    return gh_get(f"/repos/{repo_name}/commits/{sha}/check-runs?check_name={name}&per_page=100").get("check_runs", [])


def fetch_workflow_runs(repo_name, sha):
    return gh_get(f"/repos/{repo_name}/actions/runs?head_sha={sha}&per_page=100").get("workflow_runs", [])


def repo_name_of(remote):
    m = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", remote)
    return m.group(1) if m else None


# --- the release -------------------------------------------------------------------------------
def release(remote, source, cfg, write, required=None, before_push=None, log=print):
    """Plan (write=False) or make the release. Returns a dict (action, tag, sha, source, reason).
    required(source) -> (verdict, why) checks the source's CI; before_push() is a test hook."""
    if not SHA.match(source or ""):
        raise Refuse(f"--source must be a full commit SHA, not {source!r}")
    with tempfile.TemporaryDirectory(prefix="fork-ci-release-") as work:
        repo = pathlib.Path(work)
        git(repo, "init", "-q", "--bare")
        git(repo, "remote", "add", "origin", remote)
        refs = remote_refs(repo, "origin", cfg)
        tip = refs.get(f"refs/heads/{cfg['sourceBranch']}")
        if tip != source:
            raise Skip(f"{cfg['sourceBranch']} is at {tip}, not {source}: the run for the newer commit releases it")
        if required:
            verdict, why = required(source)
            if verdict == "pending":
                raise Skip(f"{source}: {why}; nothing released until it passes")
            if verdict != "success":
                raise Refuse(f"{source}: {why}; a release needs it green")
        branch = refs.get(f"refs/heads/{cfg['branch']}")
        if branch:  # its whole history: a release commit holds few files
            git(repo, "fetch", "-q", "--no-tags", "--filter=blob:none", "origin",
                f"+refs/heads/{cfg['branch']}:refs/release/branch")
        git(repo, "fetch", "-q", "--no-tags", "--depth=1", "--filter=blob:none", "origin", source)

        def on_branch(commit):
            return git(repo, "merge-base", "--is-ancestor", commit, branch, check=False).returncode == 0

        parent, tag = check_state(refs, cfg, on_branch)
        _, odd = release_tags(refs, cfg)
        for name in odd:
            log(f"::warning::tag {name} does not read as {cfg['tagPrefix']}<N>; ignored")
        entries = release_entries(repo, source, cfg)
        tree = write_tree(repo, entries)
        ensure_objects(repo, "origin", tree)
        check_workflows(repo, entries)
        if parent and git(repo, "rev-parse", f"{parent}^{{tree}}").stdout.strip() == tree:
            newest = max((n, name) for name, (n, c) in release_tags(refs, cfg)[0].items() if c == parent)[1]
            raise Skip(f"{newest} ({parent}) already holds these files: nothing to release")
        if not write:
            return {"action": "release", "tag": tag, "sha": "", "source": source, "parent": parent or "",
                    "reason": f"{tag} would hold {len(entries)} files from {source}"}
        ident = git(repo, "var", "GIT_COMMITTER_IDENT", check=False)
        if ident.returncode:
            raise Refuse("git has no identity (user.name, user.email) to write the release commit with")
        body = (f"{tag}: fork-ci from {cfg['sourceBranch']} {source}\n\n"
                f"Only {', '.join(cfg['paths'])}, at the same paths as on {cfg['sourceBranch']}.\n"
                f"Pin it as @<this commit's SHA> # {tag}.\n\n"
                f"Fork-CI-Source: {source}\n")
        commit = git(repo, "commit-tree", tree, *(["-p", parent] if parent else []), input=body).stdout.strip()
        tag_obj = git(repo, "mktag", input=(f"object {commit}\ntype commit\ntag {tag}\ntagger "
                                            f"{ident.stdout.strip()}\n\n{tag}: fork-ci from {source}\n")).stdout.strip()
        if before_push:
            before_push()
        # Right before the push: the source branch and the release refs are as planned.
        now = remote_refs(repo, "origin", cfg)
        if now.get(f"refs/heads/{cfg['sourceBranch']}") != source:
            raise Skip(f"{cfg['sourceBranch']} moved during the run (now {now.get('refs/heads/' + cfg['sourceBranch'])}): "
                       "nothing pushed; the run for the newer commit releases it")
        if now.get(f"refs/heads/{cfg['branch']}") != branch or f"refs/tags/{tag}" in now:
            raise Refuse(f"the branch {cfg['branch']} or the tag {tag} changed during the run (another release?): "
                         "nothing pushed; run again")
        # No force at all: the branch only fast-forwards (a branch someone else moved or created
        # meanwhile is not an ancestor, so the push is refused) and an existing tag is refused.
        r = git(repo, "push", "--atomic", "--porcelain", "origin",
                f"{commit}:refs/heads/{cfg['branch']}", f"{tag_obj}:refs/tags/{tag}", check=False)
        if r.returncode:
            raise Refuse(f"the push was refused, nothing was written (atomic): {(r.stdout + r.stderr).strip()[-800:]}")
        after = remote_refs(repo, "origin", cfg)
        if after.get(f"refs/heads/{cfg['branch']}") != commit or after.get(f"refs/tags/{tag}^{{}}") != commit:
            raise Refuse(f"pushed, but reading back shows the branch at {after.get('refs/heads/' + cfg['branch'])} and "
                         f"{tag} at {after.get('refs/tags/' + tag + '^{}')}, not {commit}")
        return {"action": "released", "tag": tag, "sha": commit, "source": source, "parent": parent or "",
                "reason": f"{tag} is {commit}: pin @{commit} # {tag}"}


# --- the consumer smoke test -------------------------------------------------------------------
def verify_download(directory, commit, git_dir="."):
    """Problems (a list) when DIRECTORY does not hold exactly the plain files of COMMIT."""
    r = git(git_dir, "ls-tree", "-r", "-z", "--full-tree", commit)
    want = {}
    for item in filter(None, r.stdout.split("\0")):
        meta, path = item.split("\t", 1)
        mode, kind, oid = meta.split()
        want[path] = (mode, oid)
    root = pathlib.Path(directory)
    if not root.is_dir():
        return [f"{directory} does not exist: the runner did not unpack the action there"]
    problems, have = [], {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            problems.append(f"{rel} is a symlink")
        elif p.is_file():
            oid = git(git_dir, "hash-object", "--no-filters", "--", str(p)).stdout.strip()
            have[rel] = oid
    for path, (mode, oid) in want.items():
        if mode not in PLAIN_FILE:
            problems.append(f"{path} is mode {mode} in {commit}")
        elif path not in have:
            problems.append(f"{path} is missing")
        elif have[path] != oid:
            problems.append(f"{path} differs from {commit}")
    problems += [f"{path} is not in {commit}" for path in sorted(set(have) - set(want))]
    return problems


# --- command line ------------------------------------------------------------------------------
def emit(result):
    out = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{k}={str(v).replace(chr(10), ' ')}" for k, v in result.items()]
    if out:
        with open(out, "a") as f:
            f.write("\n".join(lines) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"### fork-ci release: {result['action']}\n\n{result['reason']}\n")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "release"):
        s = sub.add_parser(name)
        s.add_argument("--remote", required=True, help="the repository's git URL")
        s.add_argument("--source", required=True, help="the source-branch commit to release (full SHA)")
        s.add_argument("--require-check", action="store_true", help="needs the config's requiredCheck green on it")
        s.add_argument("--repo", help="OWNER/NAME for the check API (default: from --remote)")
        s.add_argument("--config", default=str(CONFIG))
    v = sub.add_parser("verify-download")
    v.add_argument("--dir", required=True)
    v.add_argument("--commit", required=True)
    v.add_argument("--git-dir", default=".")
    a = p.parse_args(argv)
    if a.cmd == "verify-download":
        problems = verify_download(a.dir, a.commit, a.git_dir)
        for line in problems[:50]:
            print(f"::error::{line}", file=sys.stderr)
        if not problems:
            print(f"{a.dir} holds exactly the files of {a.commit}")
        return 1 if problems else 0
    try:
        cfg = load_config(a.config)
        required = None
        if a.require_check:
            if not cfg["requiredCheck"]:
                raise Refuse("--require-check, but the config names no requiredCheck")
            name = a.repo or repo_name_of(a.remote)
            if not name:
                raise Refuse(f"cannot tell OWNER/NAME from {a.remote}; pass --repo")
            required = lambda sha: check_verdict(  # noqa: E731
                fetch_check_runs(name, sha, cfg["requiredCheck"]), cfg["requiredCheck"],
                trusted_suites(fetch_workflow_runs(name, sha)))
        result = release(a.remote, a.source, cfg, write=a.cmd == "release", required=required)
    except Skip as e:
        result = {"action": "none", "tag": "", "sha": "", "source": a.source, "parent": "", "reason": str(e)}
    except Refuse as e:
        print(f"::error::fork-ci release: {e}", file=sys.stderr)
        emit({"action": "refused", "tag": "", "sha": "", "source": a.source, "parent": "", "reason": str(e)})
        return 1
    print(f"{result['action']}: {result['reason']}")
    emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
