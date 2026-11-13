"""Checks .github/workflows/fork-pr-queue.yml and the action it runs (fork-ci/actions/pr-queue/action.yml): the
refusals are run as Actions runs them, the wiring is read from the files.

    python3 .github/scripts/fork-pr-queue-workflow.test.py

Needs PyYAML and bash. Each check prints ok/FAIL; any FAIL exits 1. Nothing here starts gh or the queue: a fake
python3 stands in for the queue's start.
"""

import os
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

root = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = root / ".github/workflows/fork-pr-queue.yml"
text = WORKFLOW.read_text()
wf = yaml.safe_load(text)
failures = []


def check(name, ok, detail=""):
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
check("no token by default; the job reads contents only (the queue acts with the token secret)",
      wf.get("permissions") == {} and job.get("permissions") == {"contents": "read"})
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
caller = by_name.get("Check out the caller's config") or {}
toolkit = by_name.get("Check out the queue at this workflow's own commit") or {}
queue = by_name.get("Run the queue") or {}
check("steps: refuse, the caller's config, the toolkit, the queue, in that order",
      steps == [first, caller, toolkit, queue], ", ".join(str(s.get("name")) for s in steps))
pinned = all(re.fullmatch(r"actions/checkout@[0-9a-f]{40}", str(s.get("uses"))) for s in (caller, toolkit))
check("both checkouts are actions/checkout pinned by commit", pinned)
cw, tw = caller.get("with") or {}, toolkit.get("with") or {}
check("the caller's checkout holds only the config, in its own folder",
      cw == {"path": "caller", "sparse-checkout": "${{ inputs.config }}", "sparse-checkout-cone-mode": False}, str(cw))
check("the toolkit is checked out at this workflow's own repository and commit, not the caller's",
      tw.get("repository") == "${{ steps.toolkit.outputs.repo }}" and tw.get("ref") == "${{ steps.toolkit.outputs.sha }}"
      and set(tw) == {"repository", "ref", "path", "sparse-checkout"}, str(tw))
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
check("the action passes the token to gh as GH_TOKEN, and the run's own token only to compare",
      (astep.get("env") or {}).get("GH_TOKEN") == "${{ inputs.token }}"
      and (astep.get("env") or {}).get("RUN_TOKEN") == "${{ github.token }}")
fake = tempfile.mkdtemp(prefix="fork-pr-queue-bin-")
with open(os.path.join(fake, "python3"), "w") as f:
    f.write('#!/bin/sh\necho "started: $*"\necho "RUN_TOKEN=${RUN_TOKEN-unset} GH_TOKEN=${GH_TOKEN:+set}"\n')
os.chmod(os.path.join(fake, "python3"), 0o755)


def action_run(token="t-queue", run_token="t-run", dry="false", lock=""):
    return bash(astep.get("run", "exit 0"), {"GH_TOKEN": token, "RUN_TOKEN": run_token, "REPO": "owner/proj",
                                             "CONFIG": "caller/q.json", "STATE_FILE": "/srv/q/plan.json",
                                             "CACHE": "/srv/q/cache.git", "LOCK_FILE": lock, "DRY_RUN": dry,
                                             "GITHUB_ACTION_PATH": "/act"}, path=fake)


r = action_run()
check("action: a good call starts the queue with exactly its arguments, GH_TOKEN set and the run's token unset",
      r.returncode == 0 and r.stdout == "started: -I /act/pr_queue.py --repo owner/proj --config caller/q.json "
      "--state-file /srv/q/plan.json --cache /srv/q/cache.git\nRUN_TOKEN=unset GH_TOKEN=set\n", r.stdout + r.stderr)
r = action_run(dry="true", lock="/srv/q/l")
check("action: dry-run true and a lock file are passed on",
      r.returncode == 0 and "--lock-file /srv/q/l --dry-run\n" in r.stdout, r.stdout)
for label, kw in (("the run's GITHUB_TOKEN as the token", {"token": "t-run"}), ("an empty token", {"token": ""}),
                  ("a dry-run that is not true or false", {"dry": "yes"})):
    r = action_run(**kw)
    check(f"action: {label} is refused before the queue starts, and no token is printed",
          r.returncode == 2 and "started:" not in r.stdout and "t-run" not in r.stdout + r.stderr, r.stdout)

if failures:
    print(f"\n{len(failures)} failed")
    sys.exit(1)
print("\nall passed")
