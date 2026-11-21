"""Checks .github/workflows/fork-pr-queue.yml and the action it runs (fork-ci/actions/pr-queue/action.yml): the
refusals are run as Actions runs them, the wiring is read from the files.

    python3 .github/scripts/fork-pr-queue-workflow.test.py

Needs PyYAML and bash. Each check prints ok/FAIL; any FAIL exits 1. Nothing here starts gh or the queue: a fake
python3 stands in for the queue's start.
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

root = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = root / ".github/workflows/fork-pr-queue.yml"
text = WORKFLOW.read_text()
wf = yaml.safe_load(text)
failures = []
# The ok lines printed, for the end line: fork-ci-tests.cjs checks its count against them (#664).
oks = []


def check(name, ok, detail=""):
    if ok:
        oks.append(1)
    print(("ok   " if ok else "FAIL ") + name + ("" if ok or not detail else f"\n     | {detail}"))
    if not ok:
        failures.append(name)


def bash(script, env, path=None):
    """The script as a bash `run:` step runs it (bash --noprofile --norc -eo pipefail)."""
    base = {"PATH": (path + os.pathsep if path else "") + os.environ["PATH"]}
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
    try:
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", f.name], env={**base, **env},
                              capture_output=True, text=True)
    finally:
        os.remove(f.name)


# --- triggers, permissions, concurrency -----------------------------------------------------------------------------
on = wf.get(True, wf.get("on"))  # YAML 1.1 reads the key `on` as true
check("it is only a reusable workflow (workflow_call): no event of its own starts it", set(on) == {"workflow_call"},
      str(set(on)))
jobs = wf["jobs"]
job = jobs.get("queue") or {}
check("one job, queue", set(jobs) == {"queue"}, ", ".join(jobs))
READS = {k: "read" for k in ("actions", "checks", "contents", "issues", "pull-requests", "statuses")}
check("no token by default; the job's GITHUB_TOKEN gets exactly the queue's read permissions (every read uses it)",
      wf.get("permissions") == {} and job.get("permissions") == READS, str(job.get("permissions")))
conc = job.get("concurrency") or {}
check("one queue run per repository at a time, never cancelled half-way",
      "github.repository" in str(conc.get("group")) and conc.get("cancel-in-progress") is False, str(conc))
check("it runs on the caller's runner (a self-hosted one: pr_queue.py refuses a GitHub-hosted one)",
      job.get("runs-on") == "${{ fromJSON(inputs.runs-on) }}")

steps = job.get("steps") or []
injected = [s.get("name") for s in steps if "${{" in str(s.get("run", ""))]
check("no expression inside a run script (inputs reach it as env only)", not injected, ", ".join(map(str, injected)))

# --- the first step: refusals and this workflow's own commit --------------------------------------------------------
first = steps[0] if steps else {}
check("the first step refuses and finds the toolkit, before anything is checked out",
      first.get("id") == "toolkit" and "uses" not in first and first.get("shell") == "bash")
SHA = "0123456789abcdef0123456789abcdef01234567"
GOOD_JOB = '{"status": "success", "workflow_repository": "owner/toolkit", "workflow_sha": "%s"}' % SHA


def refusal(event="push", config=".github/pr-queue.json", job_context=GOOD_JOB):
    with tempfile.NamedTemporaryFile("r", prefix="fork-pr-queue-out-") as out:
        r = bash(first.get("run", "exit 0"), {"EVENT": event, "CONFIG": config, "JOB_CONTEXT": job_context,
                                              "GITHUB_OUTPUT": out.name})
        return r, out.read()


for event in ("push", "schedule", "workflow_dispatch"):
    r, out = refusal(event)
    check(f"refusal: a {event} run goes on and names the toolkit's repository and commit",
          r.returncode == 0 and out == f"repo=owner/toolkit\nsha={SHA}\n", f"{r.returncode} {r.stdout} {out!r}")
for event in ("pull_request", "pull_request_target", "issue_comment", "workflow_run", "merge_group", ""):
    r, out = refusal(event)
    check(f"refusal: a {event or 'unnamed'} event fails and writes no output",
          r.returncode == 1 and out == "" and "refused on a" in r.stdout, f"{r.returncode} {r.stdout}")
for label, ctx in (("an empty job context", ""), ("no workflow_sha (an old runner)", '{"status": "success"}'),
                   ("a sha that is a branch name", GOOD_JOB.replace(SHA, "main")),
                   ("a repository that is no owner/name", GOOD_JOB.replace("owner/toolkit", "owner/toolkit/x")),
                   ("no JSON", "{")):
    r, out = refusal(job_context=ctx)
    check(f"refusal: {label} fails: it never guesses which queue to run",
          r.returncode == 1 and out == "" and "will not guess" in r.stdout, f"{r.returncode} {r.stdout} {r.stderr}")
for config in ("/etc/pr-queue.json", "../pr-queue.json", "a/../../pr-queue.json", "a/..", ""):
    r, out = refusal(config=config)
    check(f"refusal: config {config!r} outside the caller's checkout fails",
          r.returncode == 1 and out == "" and "is not a path inside" in r.stdout, f"{r.returncode} {r.stdout}")
r, out = refusal(config="ci/pr..queue.json")
check("refusal: a name with two dots in it is no parent folder", r.returncode == 0, r.stdout)

# --- the checkouts and the action ------------------------------------------------------------------------------------
by_name = {s.get("name"): s for s in steps}
clean = by_name.get("Remove an earlier run's caller and toolkit folders") or {}
caller = by_name.get("Check out the caller's config") or {}
toolkit = by_name.get("Check out the queue at this workflow's own commit") or {}
drop = by_name.get("Drop the checkouts' token") or {}
queue = by_name.get("Run the queue") or {}
check("steps: refuse, remove the old folders, the caller's config, the toolkit, drop their token, the queue, in that "
      "order", steps == [first, clean, caller, toolkit, drop, queue], ", ".join(str(s.get("name")) for s in steps))

# The runner is self-hosted and its workspace is reused: an earlier run's caller/ or toolkit/ (a symlink, a
# planted .git, files checkout would keep) never reaches this run's checkouts.
check("the removal is one bash step with no expression in its script",
      set(clean) == {"name", "shell", "run"} and clean.get("shell") == "bash" and "${{" not in str(clean.get("run")),
      str(clean))


def clean_run(workspace):
    return bash(clean.get("run", "exit 1"), {"GITHUB_WORKSPACE": workspace} if workspace is not None else {})


with tempfile.TemporaryDirectory(prefix="fork-pr-queue-ws-") as top:
    ws, outside = os.path.join(top, "ws"), os.path.join(top, "outside")
    for d in ("caller/.git/hooks", "toolkit/fork-ci/actions/pr-queue", "keep", "outside/kept"):
        os.makedirs(os.path.join(top, d if d.startswith("outside") else os.path.join("ws", d)))
    for f in ("caller/.git/config", "toolkit/fork-ci/actions/pr-queue/action.yml", "keep/file", "outside/kept/file"):
        pathlib.Path(top, f if f.startswith("outside") else os.path.join("ws", f)).write_text("x")
    os.chmod(os.path.join(ws, "toolkit/fork-ci/actions/pr-queue/action.yml"), 0o444)
    os.symlink(os.path.join(outside, "kept"), os.path.join(ws, "caller", "link"))
    r = clean_run(ws)
    left = sorted(os.listdir(ws))
    check("removal: an earlier run's caller/ and toolkit/ are gone, read-only files too; the rest of the workspace "
          "stays", r.returncode == 0 and left == ["keep"], f"{r.returncode} {left} {r.stderr}")
    check("removal: a symlink inside them is removed, not followed",
          pathlib.Path(outside, "kept", "file").read_text() == "x")
    for d in ("caller", "toolkit"):  # what a failed removal above left
        shutil.rmtree(os.path.join(ws, d), ignore_errors=True)
    os.symlink(os.path.join(outside, "kept"), os.path.join(ws, "caller"))
    os.symlink(outside, os.path.join(ws, "toolkit"))
    r = clean_run(ws)
    check("removal: caller and toolkit that are symlinks are removed; what they point to stays",
          r.returncode == 0 and sorted(os.listdir(ws)) == ["keep"]
          and pathlib.Path(outside, "kept", "file").read_text() == "x", f"{r.returncode} {os.listdir(ws)} {r.stderr}")
    r = clean_run(ws)
    check("removal: a run with neither folder (a first run, or a re-run) passes", r.returncode == 0, r.stderr)
    for label, value in (("no GITHUB_WORKSPACE", None), ("an empty GITHUB_WORKSPACE", "")):
        os.makedirs(os.path.join(ws, "caller"), exist_ok=True)
        r = clean_run(value)
        check(f"removal: {label} fails and removes nothing",
              r.returncode != 0 and os.path.isdir(os.path.join(ws, "caller")), f"{r.returncode} {r.stderr}")
        shutil.rmtree(os.path.join(ws, "caller"), ignore_errors=True)
pinned = all(re.fullmatch(r"actions/checkout@[0-9a-f]{40}", str(s.get("uses"))) for s in (caller, toolkit))
check("both checkouts are actions/checkout pinned by commit", pinned)
cw, tw = caller.get("with") or {}, toolkit.get("with") or {}
check("the caller's checkout holds only the config, in its own folder",
      cw == {"path": "caller", "sparse-checkout": "${{ inputs.config }}", "sparse-checkout-cone-mode": False}, str(cw))
check("the toolkit is checked out at this workflow's own repository and commit, not the caller's",
      tw.get("repository") == "${{ steps.toolkit.outputs.repo }}" and tw.get("ref") == "${{ steps.toolkit.outputs.sha }}"
      and set(tw) == {"repository", "ref", "path", "sparse-checkout"}, str(tw))
# #675: no checkout leaves the job's token where notify_command (the project's own code) can read it. Not with
# persist-credentials: false, whose cleanup fails on a repository with gitlinks and no .gitmodules (this one; the
# contract test .github/scripts/fork-contracts.test.py forbids it), but a step that drops it after both checkouts.
check("no checkout sets persist-credentials (its `false` fails on this repository's gitlinks)",
      not any("persist-credentials" in (s.get("with") or {}) for s in steps), f"{cw} {tw}")
check("the token drop is one bash step that cannot be skipped or fail quietly, with no expression in its script",
      set(drop) == {"name", "shell", "run"} and drop.get("shell") == "bash" and "${{" not in str(drop.get("run")),
      str(sorted(drop)))

HEADER = "AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46ZmFrZQ=="


def git(cwd, *a, env=None, ok=True):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True,
                       env={**GIT_ENV, **(env or {})})
    if ok and r.returncode != 0:
        raise RuntimeError(f"git {a}: {r.stderr}")
    return r


GIT_ENV = {"PATH": os.environ["PATH"], "HOME": tempfile.mkdtemp(prefix="fork-pr-queue-home-"),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def drop_e2e(run):
    """Runs the token drop as Actions runs it in a workspace whose caller/ and toolkit/ are laid out as
    actions/checkout leaves them; returns what went wrong."""
    problems = []
    top = pathlib.Path(tempfile.mkdtemp(prefix="fork-pr-queue-drop-"))
    ws, tmp = top / "ws", top / "runner-temp"
    tmp.mkdir()

    def checkout(name):
        dest = ws / name
        git(top, "init", "-q", str(dest))
        git(dest, "remote", "add", "origin", f"https://github.com/owner/{name}")
        return dest

    def step(**env):
        return bash(run, {**GIT_ENV, "GITHUB_WORKSPACE": str(ws), "RUNNER_TEMP": str(tmp), **env})

    def headers(dest, **env):
        return git(dest, "config", "--get-regexp", r"\.extraheader$", env=env, ok=False).returncode

    def lay_out():
        shutil.rmtree(ws, ignore_errors=True)
        caller_dir, toolkit_dir = checkout("caller"), checkout("toolkit")
        # checkout v6: the header in a file of its own under RUNNER_TEMP that the repository's config includes for
        # its git dir (one entry for the runner's path, one for a container's), a file per checkout.
        for i, dest in enumerate((caller_dir, toolkit_dir)):
            creds = tmp / f"git-credentials-{i}.config"
            creds.write_text(f'[http "https://github.com/"]\n\textraheader = {HEADER}\n')
            git(dest, "config", "--local", f"includeIf.gitdir:{dest}/.git.path", str(creds))
            git(dest, "config", "--local", f"includeIf.gitdir:/github/workspace/{dest.name}/.git.path",
                f"/github/runner_temp/{creds.name}")
        # an older checkout: the header in the repository's own config, twice
        git(toolkit_dir, "config", "--local", "http.https://github.com/.extraheader", HEADER)
        git(toolkit_dir, "config", "--local", "--add", "http.https://github.com/.extraheader", HEADER)
        return caller_dir, toolkit_dir

    caller_dir, toolkit_dir = lay_out()
    if headers(caller_dir) != 0 or headers(toolkit_dir) != 0:
        problems.append("fixture: the headers are not seen")
    r = step()
    for dest in (caller_dir, toolkit_dir):
        if r.returncode != 0 or headers(dest) != 1 or \
                git(dest, "config", "--local", "--get-regexp", "^includeif", ok=False).returncode != 1:
            problems.append(f"{dest.name}: the token is not removed with exit 0 (exit {r.returncode}: {r.stderr.strip()})")
        if git(dest, "config", "remote.origin.url").stdout.strip() != f"https://github.com/owner/{dest.name}":
            problems.append(f"{dest.name}: the step changed more than the token (origin's URL)")
    if list(tmp.glob("git-credentials-*.config")):
        problems.append("a credentials file under RUNNER_TEMP is left")
    # Runs again (a re-run, or the step after a half-way stop): nothing left to remove passes quietly.
    r = step()
    if r.returncode != 0 or r.stderr.strip():
        problems.append(f"re-run: nothing to remove does not pass quietly (exit {r.returncode}: {r.stderr.strip()})")
    # A header the step cannot remove (another config file): the closing check fails the step.
    lay_out()
    elsewhere = top / "global-with-header"
    elsewhere.write_text(f'[http "https://github.com/"]\n\textraheader = {HEADER}\n')
    r = step(GIT_CONFIG_GLOBAL=str(elsewhere))
    if r.returncode == 0 or "::error::" not in r.stderr:
        problems.append(f"a header left in another config: the step passes (exit {r.returncode})")
    # A config git cannot read: no proof the header is gone.
    lay_out()
    broken = top / "global-broken"
    broken.write_text("[http\n")
    r = step(GIT_CONFIG_GLOBAL=str(broken))
    if r.returncode == 0 or "::error::" not in r.stderr:
        problems.append(f"an unreadable config: the step passes (exit {r.returncode})")
    # A checkout that is not there (empty or old state): nothing proves it has no token.
    lay_out()
    shutil.rmtree(ws / "toolkit")
    r = step()
    if r.returncode == 0:
        problems.append("a missing toolkit checkout: the step passes")
    # A credentials file the step cannot remove: it fails.
    lay_out()
    (tmp / "git-credentials-stuck.config").mkdir()
    r = step()
    if r.returncode == 0:
        problems.append("a credentials path it cannot remove: the step passes")
    shutil.rmtree(tmp / "git-credentials-stuck.config")
    shutil.rmtree(top, ignore_errors=True)
    return problems


drop_run = str(drop.get("run", "exit 97"))
problems = drop_e2e(drop_run)
check("token drop e2e: removes checkout v6's includes and files and an older checkout's headers in caller/ and "
      "toolkit/, keeps the rest, passes quietly on a re-run, fails on a header it cannot remove, an unreadable "
      "config, a missing checkout or a file it cannot remove", not problems, "; ".join(problems))
# Broken forms, run: the e2e must see each (a check that stops seeing its problem goes red).
for label, broken_run in {
    "no `|| true` (nothing to remove ends the step)": drop_run.replace(" || true; }", "; }"),
    "no closing check": drop_run[:drop_run.find("rm -f --")] + 'rm -f -- "${RUNNER_TEMP:?}"/git-credentials-*.config\n',
    "a closing check that ignores read errors": drop_run.replace('"$rc" != 1', '"$rc" == 0'),
    "the include kept": drop_run.replace("includeif\\..*\\.path|", ""),
    "the old header kept": drop_run.replace("|http\\..*\\.extraheader", ""),
    "no dedup (a header set twice)": drop_run.replace("sort -u | while", "while"),
    "the credentials file kept": drop_run.replace('rm -f -- "${RUNNER_TEMP:?}"/git-credentials-*.config\n', ""),
    "only the caller's checkout": drop_run.replace(' "${GITHUB_WORKSPACE:?}/toolkit"', ""),
}.items():
    check(f"token drop e2e: a drop with {label} fails", broken_run != drop_run and bool(drop_e2e(broken_run)))
action_dir = str(tw.get("sparse-checkout"))
check("the queue step runs the action from that checkout",
      queue.get("uses") == f"./{tw.get('path')}/{action_dir}" and (root / action_dir / "action.yml").is_file(),
      f"{queue.get('uses')} vs ./{tw.get('path')}/{action_dir}")
action = yaml.safe_load((root / action_dir / "action.yml").read_text())
inputs = action.get("inputs") or {}
qw = queue.get("with") or {}
required = {k for k, v in inputs.items() if v.get("required")}
check("the queue step gives only inputs the action has, and every required one",
      set(qw) <= set(inputs) and required <= set(qw), f"given {sorted(qw)}, action {sorted(inputs)}")
check("the config is read from the caller's checkout", qw.get("config") == "caller/${{ inputs.config }}")
check("the token secret reaches only the queue step's token input",
      text.count("secrets.") == 1 and qw.get("token") == "${{ secrets.token }}")
check("the action's repository defaults to the caller's", inputs.get("repo", {}).get("default") == "${{ github.repository }}")

# --- the action's script: refusals, and the call it makes ------------------------------------------------------------
astep = (action.get("runs") or {}).get("steps", [{}])[0]
check("the action is one bash step with no expression in its script",
      action.get("runs", {}).get("using") == "composite" and astep.get("shell") == "bash"
      and "${{" not in str(astep.get("run")))
aenv = astep.get("env") or {}
check("the action gives the queue the token secret for writes and the run's GITHUB_TOKEN for reads, and no GH_TOKEN",
      aenv.get("PR_QUEUE_WRITE_TOKEN") == "${{ inputs.token }}" and aenv.get("PR_QUEUE_READ_TOKEN") == "${{ github.token }}"
      and not {"GH_TOKEN", "GITHUB_TOKEN", "RUN_TOKEN"} & set(aenv), str(sorted(aenv)))
fake = tempfile.mkdtemp(prefix="fork-pr-queue-bin-")
with open(os.path.join(fake, "python3"), "w") as f:
    f.write('#!/bin/sh\necho "started: $*"\n'
            'echo "READ=${PR_QUEUE_READ_TOKEN:+set} WRITE=${PR_QUEUE_WRITE_TOKEN:+set} GH_TOKEN=${GH_TOKEN-unset}"\n')
os.chmod(os.path.join(fake, "python3"), 0o755)


def action_run(token="t-queue", run_token="t-run", dry="false", lock=""):
    return bash(astep.get("run", "exit 0"), {"PR_QUEUE_WRITE_TOKEN": token, "PR_QUEUE_READ_TOKEN": run_token,
                                             "REPO": "owner/proj",
                                             "CONFIG": "caller/q.json", "STATE_FILE": "/srv/q/plan.json",
                                             "CACHE": "/srv/q/cache.git", "LOCK_FILE": lock, "DRY_RUN": dry,
                                             "GITHUB_ACTION_PATH": "/act"}, path=fake)


r = action_run()
check("action: a good call starts the queue with exactly its arguments and both tokens, and no GH_TOKEN",
      r.returncode == 0 and r.stdout == "started: -I /act/pr_queue.py --repo owner/proj --config caller/q.json "
      "--state-file /srv/q/plan.json --cache /srv/q/cache.git\nREAD=set WRITE=set GH_TOKEN=unset\n", r.stdout + r.stderr)
r = action_run(dry="true", lock="/srv/q/l")
check("action: dry-run true and a lock file are passed on",
      r.returncode == 0 and "--lock-file /srv/q/l --dry-run\n" in r.stdout, r.stdout)
for label, kw in (("the run's GITHUB_TOKEN as the token", {"token": "t-run"}), ("an empty token", {"token": ""}),
                  ("an empty GITHUB_TOKEN", {"run_token": ""}), ("a dry-run that is not true or false", {"dry": "yes"})):
    r = action_run(**kw)
    check(f"action: {label} is refused before the queue starts, and no token is printed",
          r.returncode == 2 and "started:" not in r.stdout and "t-run" not in r.stdout + r.stderr, r.stdout)

if failures:
    print(f"\n{len(failures)} failed")
    sys.exit(1)
print(f"\nall passed: {len(oks)} ok, 0 skipped")
