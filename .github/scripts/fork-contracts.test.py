"""Rules every fork workflow and fork script keeps, each checked on the real files and on broken
copies that must fail (so a check that stops seeing its problem goes red too):

  pr-guard     every job of a workflow a pull request can start runs only for a branch in this
               repository, except `outside_pr`, which runs only for another fork's pull request,
               checks nothing out and fails, and fork-tests.yml's fork_ci_result, which needs it and
               fails there too (a job skipped by its if: counts as passed); a fork workflow takes no
               trigger where that cannot be told.
  checkout     no actions/checkout with persist-credentials: false (its cleanup fails on upstream's
               gitlinks); every plain-git checkout is the one shared step, run here end to end
               against a repository with a gitlink, no .gitmodules and a tag named like the branch.
  refusals     in fork-follow.yml no refusal is swallowed: a refusal step is only its script call
               (a whitelist), and no step before it in its job can change the shell (GITHUB_ENV,
               GITHUB_PATH, BASH_ENV, a function, an action). Other script steps keep a blacklist.
  gh-guard     every fork script that calls gh sources fork-gh-guard.sh at the top level, after
               nothing but `set`, before its first call (backslash continuations joined), and
               every fork shell test sets up the gh stub (fork-test-gh.sh) before anything else.
  removed      .github/fork-removed-workflows.txt (the upstream workflows the fork deletes, kept
               deleted by fork-follow.sh) is there, every line is an upstream workflow path, none of
               them is in the tree, and it names the files the owner decided to delete.

    python3 .github/scripts/fork-contracts.test.py

Needs PyYAML, git and bash. Each check prints ok/FAIL; any FAIL exits 1.
"""

import copy
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

root = pathlib.Path(__file__).resolve().parents[2]
workflows_dir = root / ".github/workflows"
scripts_dir = root / ".github/scripts"
failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok or not detail else f"\n     | {detail}"))
    if not ok:
        failures.append(name)


def load(path):
    return yaml.safe_load(path.read_text())


def fork_workflows():
    return {p.name: load(p) for p in sorted(workflows_dir.glob("fork-*.y*ml"))}


def triggers(wf):
    on = wf.get(True, wf.get("on"))  # YAML 1.1 reads the key `on` as true
    if isinstance(on, str):
        return {on}
    return set(on or [])


def steps(wf):
    for job_id, job in (wf.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_id, step


def expression(value):
    text = str(value).strip()
    m = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.S)
    return (m.group(1) if m else text).strip()


# --- pr-guard ----------------------------------------------------------------------------------
PR_GUARD = "(!github.event.pull_request || github.event.pull_request.head.repo.full_name == github.repository)"
PR_EVENTS = {"pull_request", "pull_request_target"}
# Events whose payload has no pull_request for the guard to read, although a pull request from
# another fork can cause them (issue_comment, workflow_run, pull_request_review ...), are refused.
ALLOWED_EVENTS = PR_EVENTS | {"push", "schedule", "workflow_dispatch", "workflow_call"}


def one_group(text):
    """True when text is one parenthesised group: '(' ... ')' with the first '(' closed last."""
    if not text.startswith("(") or not text.endswith(")"):
        return False
    depth, quoted = 0, False
    for i, c in enumerate(text):
        if c == "'":
            quoted = not quoted
        elif not quoted and c in "()":
            depth += 1 if c == "(" else -1
            if depth == 0 and i != len(text) - 1:
                return False
    return depth == 0 and not quoted


def guarded(condition):
    if condition is None:
        return False
    expr = expression(condition)
    return expr == PR_GUARD or (expr.startswith(PR_GUARD + " && ") and one_group(expr[len(PR_GUARD) + 4 :]))


# The job a pull request from another fork gets instead: it fails and runs nothing else. A job
# skipped by its if: counts as passed for a required check, so without it an untested outside pull
# request would show green.
OUTSIDE_JOB = "outside_pr"
OUTSIDE_IF = "github.event.pull_request && github.event.pull_request.head.repo.full_name != github.repository"
OUTSIDE_RUN = 'echo "::error::outside PR: not tested here; a maintainer must review"\nexit 1\n'
# fork-tests.yml's result job (the check a ruleset requires) runs on outside pull requests too: it
# needs outside_pr, checks nothing out and refuses first.
RESULT_JOBS = {"fork-tests.yml": "fork_ci_result"}
RESULT_REFUSAL = ('if [[ "$OUTSIDE_PR" == true ]]; then\n'
                  '  echo "::error::outside PR: not tested here; a maintainer must review"\n'
                  '  exit 1\n'
                  'fi\n')


def outside_job_problems(name, job):
    where = f"{name}: job {OUTSIDE_JOB}"
    if job is None:
        return [f"{where} is missing (an outside pull request would skip every job and show green)"]
    problems = []
    job_steps = job.get("steps") or []
    if expression(job.get("if")) != OUTSIDE_IF:
        problems.append(f"{where}: if is not exactly `{OUTSIDE_IF}`")
    if not set(job) <= {"name", "if", "runs-on", "timeout-minutes", "permissions", "steps"} \
            or job.get("permissions") != {}:
        problems.append(f"{where}: more than name, if, runs-on, timeout-minutes, permissions {{}} and steps")
    if len(job_steps) != 1 or not set(job_steps[0]) <= {"name", "run"} or job_steps[0].get("run") != OUTSIDE_RUN:
        problems.append(f"{where}: not exactly one step that prints the refusal and exits 1")
    return problems


def result_job_problems(name, job):
    where = f"{name}: job {RESULT_JOBS[name]}"
    if job is None:
        return [f"{where} is missing"]
    problems = []
    needs = job.get("needs") or []
    if expression(job.get("if")) != "always()":
        problems.append(f"{where}: if is not exactly always() (skipped on an outside pull request = green)")
    if OUTSIDE_JOB not in ([needs] if isinstance(needs, str) else needs):
        problems.append(f"{where}: does not need {OUTSIDE_JOB}")
    job_steps = job.get("steps") or []
    for step in job_steps:
        uses = str(step.get("uses", ""))
        if uses and not uses.startswith("actions/download-artifact@"):
            problems.append(f"{where}: uses {uses}")
        if re.search(r"\bgit\b|\.github/scripts|\bnode\b|\bbash\b", str(step.get("run", ""))):
            problems.append(f"{where}: a step runs git or a script from the checkout")
    first = job_steps[0] if job_steps else {}
    if RESULT_REFUSAL not in str(first.get("run", "")) or "if" in first or \
            expression((first.get("env") or {}).get("OUTSIDE_PR")) != OUTSIDE_IF:
        problems.append(f"{where}: its first step does not refuse an outside pull request (OUTSIDE_PR)")
    return problems


def pr_guard_problems(name, wf):
    events = triggers(wf)
    problems = [f"{name}: trigger {e} is not allowed (only {', '.join(sorted(ALLOWED_EVENTS))})"
                for e in sorted(events - ALLOWED_EVENTS)]
    if events & PR_EVENTS:
        jobs = wf.get("jobs") or {}
        problems += outside_job_problems(name, jobs.get(OUTSIDE_JOB))
        if name in RESULT_JOBS:
            problems += result_job_problems(name, jobs.get(RESULT_JOBS[name]))
        problems += [f"{name}: job {j} does not start with the pull request guard"
                     for j, job in jobs.items()
                     if j not in {OUTSIDE_JOB, RESULT_JOBS.get(name)} and not guarded(job.get("if"))]
    return problems


real = fork_workflows()
check("pr-guard: there are fork workflows to check, two of them started by pull requests",
      len([n for n, wf in real.items() if triggers(wf) & PR_EVENTS]) >= 2, ", ".join(real))
problems = [p for n, wf in real.items() for p in pr_guard_problems(n, wf)]
check("pr-guard: every job a pull request can start runs only for this repository's branches; an outside "
      "pull request gets only outside_pr (and fork_ci_result), which fail", not problems, "; ".join(problems))
check("pr-guard: the guard compares the head repository with this one (null when it is gone, so no run)",
      "github.event.pull_request.head.repo.full_name == github.repository" in PR_GUARD)


def mutant_flagged(name, mutate):
    wf = copy.deepcopy(real[name])
    try:
        mutate(wf)
    except (KeyError, ValueError, IndexError):  # nothing to break: the shape is missing, a FAIL above
        return False
    return bool(pr_guard_problems(name, wf))


def set_if(job, value):
    return lambda wf: wf["jobs"][job].__setitem__("if", value)


tests_jobs = real["fork-tests.yml"]["jobs"]
mutants = {
    "a job without the guard": lambda wf: wf["jobs"]["lint"].pop("if", None),
    "a new job without an if": lambda wf: wf["jobs"].__setitem__("extra", {"runs-on": "ubuntu-24.04"}),
    "always() || guard": set_if("fork_ci_result", "${{ always() || " + PR_GUARD + " }}"),
    "guard || always()": set_if("fork_ci_result", "${{ " + PR_GUARD + " || always() }}"),
    "guard && (a) || (b)": set_if("test", "${{ " + PR_GUARD + " && (always()) || (true) }}"),
    "fork_ci_result skipped on outside pull requests (guard && (always()))":
        set_if("fork_ci_result", "${{ " + PR_GUARD + " && (always()) }}"),
    "fork_ci_result not needing outside_pr": lambda wf: wf["jobs"]["fork_ci_result"]["needs"].remove("outside_pr"),
    "fork_ci_result without its outside refusal": lambda wf: wf["jobs"]["fork_ci_result"]["steps"][0].__setitem__(
        "run", wf["jobs"]["fork_ci_result"]["steps"][0]["run"].replace(RESULT_REFUSAL, "")),
    "fork_ci_result checking out": lambda wf: wf["jobs"]["fork_ci_result"]["steps"].insert(
        1, {"uses": "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"}),
    "no outside_pr job": lambda wf: wf["jobs"].pop("outside_pr"),
    "outside_pr exiting 0": lambda wf: wf["jobs"]["outside_pr"]["steps"][0].__setitem__(
        "run", OUTSIDE_RUN.replace("exit 1", "exit 0")),
    "outside_pr with continue-on-error": lambda wf: wf["jobs"]["outside_pr"]["steps"][0].__setitem__(
        "continue-on-error", True),
    "outside_pr checking out": lambda wf: wf["jobs"]["outside_pr"]["steps"].insert(
        0, {"uses": "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"}),
    "outside_pr with a token": lambda wf: wf["jobs"]["outside_pr"].__setitem__("permissions", {"contents": "read"}),
    "outside_pr with a secret in env": lambda wf: wf["jobs"]["outside_pr"].__setitem__("env", {"T": "${{ secrets.X }}"}),
    "outside_pr on own branches (== for !=)": set_if("outside_pr", "${{ " + OUTSIDE_IF.replace("!=", "==") + " }}"),
    "outside_pr with always()": set_if("outside_pr", "${{ always() }}"),
    "guard without its parentheses": set_if("lint", "${{ " + PR_GUARD[1:-1] + " && (true) }}"),
    "!= instead of ==": set_if("lint", "${{ " + PR_GUARD.replace("==", "!=") + " }}"),
    "the guard in a step only": lambda wf: (wf["jobs"]["changes"].pop("if", None),
                                            wf["jobs"]["changes"]["steps"][0].__setitem__("if", PR_GUARD)),
}
for label, mutate in mutants.items():
    check(f"pr-guard: fork-tests.yml with {label} fails", mutant_flagged("fork-tests.yml", mutate))
check("pr-guard: fork-checks.yml without the guard fails",
      mutant_flagged("fork-checks.yml", lambda wf: wf["jobs"]["no_schema_changes"].pop("if", None)))
check("pr-guard: fork-checks.yml without outside_pr fails",
      mutant_flagged("fork-checks.yml", lambda wf: wf["jobs"].pop("outside_pr")))
check("pr-guard: outside_pr's if is the guard's exact negation",
      OUTSIDE_IF == PR_GUARD[2:-1].replace(" || ", " && ").replace("==", "!="))

# fork_ci_result's own script, run as Actions runs it, on the results of each kind of run.
result_job = real["fork-tests.yml"]["jobs"].get("fork_ci_result", {})
result_step = next((s for s in result_job.get("steps") or [] if s.get("name") == "Require every job to pass"), {})
GATES = ["typecheck", "build", "desktop_launch", "test", "test_packages", "test_web", "test_server", "rust",
         "release_smoke", "mobile_native"]


def result_run(outside_pr, outside_result, others):
    needs = result_job.get("needs") or []
    results = {j: {"result": others, "outputs": {}} for j in needs}
    if "changes" in results and others == "success":
        results["changes"]["outputs"] = {g: "true" for g in GATES}
    results[OUTSIDE_JOB] = {"result": outside_result, "outputs": {}}
    with tempfile.NamedTemporaryFile(prefix="fork-contracts-summary-") as summary:
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", str(result_step.get("run", "exit 0"))],
            env={"PATH": os.environ["PATH"], "RESULTS": json.dumps(results), "RELEASE_RUN": "false",
                 "OUTSIDE_PR": outside_pr, "GITHUB_STEP_SUMMARY": summary.name},
            capture_output=True, text=True)


for label, (outside_pr, outside_result, others, ok) in {
    "an outside pull request (outside_pr failed, the rest skipped) fails": ("true", "failure", "skipped", False),
    "an outside pull request fails even when every other job succeeded": ("true", "skipped", "success", False),
    "outside_pr failed on a run that is no outside pull request fails": ("false", "failure", "success", False),
    "an own-branch pull request with every job passed passes": ("false", "skipped", "success", True),
    "a push (OUTSIDE_PR empty) with every job passed passes": ("", "skipped", "success", True),
}.items():
    r = result_run(outside_pr, outside_result, others)
    check(f"pr-guard: fork_ci_result's script: {label}",
          (r.returncode == 0) == ok and (outside_pr != "true" or "outside PR: not tested here" in r.stdout),
          f"exit {r.returncode}: {r.stdout.strip()[-300:]} {r.stderr.strip()[-300:]}")
for event in ["issue_comment", "workflow_run", "pull_request_review", "issues"]:
    check(f"pr-guard: a fork workflow triggered by {event} fails",
          mutant_flagged("fork-release.yml", lambda wf, e=event: wf[True].__setitem__(e, None)))
check("pr-guard: fork-release.yml given a pull_request trigger fails (its jobs have no guard)",
      mutant_flagged("fork-release.yml", lambda wf: wf[True].__setitem__("pull_request", None)))
check("pr-guard: the guard alone, and the guard && (one group), pass",
      guarded("${{ " + PR_GUARD + " }}") and guarded(PR_GUARD + " && (a == 'x)' || b)")
      and not guarded(PR_GUARD + " && (a) && (b)"))

# --- checkout ----------------------------------------------------------------------------------
# fork-checks.yml has its own step: it fetches the default branch, base and head by SHA.
OWN_CHECKOUT = {"fork-checks.yml"}


def checkout_steps(wfs):
    for name, wf in wfs.items():
        for job_id, step in steps(wf):
            if name not in OWN_CHECKOUT and "git init" in str(step.get("run", "")):
                yield name, job_id, step


def checkout_problems(wfs):
    problems = []
    for name, wf in wfs.items():
        for job_id, step in steps(wf):
            if str(step.get("uses", "")).startswith("actions/checkout@") and \
                    str((step.get("with") or {}).get("persist-credentials", "true")).lower() == "false":
                problems.append(f"{name}/{job_id}: actions/checkout with persist-credentials: false")
    shared = list(checkout_steps(wfs))
    if not shared:
        return problems + ["no plain-git checkout step found"]
    first = shared[0][2]
    for name, job_id, step in shared:
        env = step.get("env") or {}
        same = {k: step.get(k) for k in ("name", "shell", "run")} == {k: first.get(k) for k in ("name", "shell", "run")}
        if not same or set(step) != {"name", "shell", "env", "run"} or set(env) != {"REF", "HISTORY", "SPARSE"} \
                or env.get("HISTORY") not in ("full", "tip"):
            problems.append(f"{name}/{job_id}: not the shared checkout step")
    return problems


check("checkout: no actions/checkout drops credentials, every plain-git checkout is the shared step",
      not checkout_problems(real), "; ".join(checkout_problems(real)))
shared = list(checkout_steps(real))
release_jobs = {j for n, j, _ in shared if n == "fork-release.yml"}
follow_jobs = {j for n, j, _ in shared if n == "fork-follow.yml"}
check("checkout: fork-release.yml's preflight, build_bundle, assemble and publish use it",
      release_jobs == {"preflight", "build_bundle", "assemble", "publish"}, ", ".join(sorted(release_jobs)))
check("checkout: fork-follow.yml's guard, plan, advance and report use it",
      follow_jobs == {"guard", "plan", "advance", "report"}, ", ".join(sorted(follow_jobs)))


def checkout_mutant(mutate):
    wfs = copy.deepcopy(real)
    try:
        mutate(wfs)
    except StopIteration:  # no shared step to break: the check above has already failed
        return False
    return bool(checkout_problems(wfs))


def first_step(wfs, name, job):
    return next(s for s in wfs[name]["jobs"][job]["steps"] if "git init" in str(s.get("run", "")))


check("checkout: actions/checkout with persist-credentials: false fails",
      checkout_mutant(lambda w: w["fork-release.yml"]["jobs"]["publish"]["steps"].insert(0, {
          "uses": "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
          "with": {"persist-credentials": False}})))
check("checkout: a copy that drifts from the shared step fails",
      checkout_mutant(lambda w: first_step(w, "fork-follow.yml", "advance").__setitem__(
          "run", first_step(w, "fork-follow.yml", "advance")["run"].replace(" --no-recurse-submodules", ""))))
check("checkout: a copy with a token in its env fails",
      checkout_mutant(lambda w: first_step(w, "fork-release.yml", "assemble")["env"].__setitem__(
          "GH_TOKEN", "${{ github.token }}")))

# End to end: the shared step as Actions runs it (bash -e -o pipefail), against a local origin
# whose tree has gitlinks under .repos/ and no .gitmodules, like upstream's.
work = pathlib.Path(tempfile.mkdtemp(prefix="fork-contracts-"))
stub = work / "bin"
stub.mkdir()
(stub / "gh").write_text("#!/bin/sh\necho 'fork-contracts.test.py: no gh here' >&2\nexit 97\n")
(stub / "gh").chmod(0o755)
base_env = {k: v for k, v in os.environ.items()
            if k not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GITHUB_ACTIONS"}}
base_env.update(PATH=f"{stub}:{os.environ['PATH']}", GH_HOST="gh-stub.invalid", GIT_CONFIG_NOSYSTEM="1",
                GIT_CONFIG_GLOBAL=str(work / "gitconfig"), GIT_TERMINAL_PROMPT="0",
                GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid")


def git(cwd, *args, ok=True):
    r = subprocess.run(["git", *args], cwd=cwd, env=base_env, capture_output=True, text=True)
    if ok and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip() if ok else r


origin = work / "gh" / "me" / "t3code"
origin.mkdir(parents=True)
git(origin, "init", "-q", "-b", "main")
git(origin, "config", "uploadpack.allowFilter", "true")
git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
for rel, text in {"package.json": "{}\n", ".github/scripts/fork-x.sh": "echo x\n",
                  ".repos/alchemy-effect/README.md": "vendored\n"}.items():
    (origin / rel).parent.mkdir(parents=True, exist_ok=True)
    (origin / rel).write_text(text)
git(origin, "add", "-A")
git(origin, "commit", "-q", "-m", "first")
first_sha = git(origin, "rev-parse", "HEAD")
git(origin, "update-index", "--add", "--cacheinfo", f"160000,{first_sha},.repos/alchemy-effect/submodules/distilled")
git(origin, "commit", "-q", "-m", "gitlink, no .gitmodules")
main_sha = git(origin, "rev-parse", "HEAD")
# A tag named like the branch, on the older commit: a bare `git fetch origin main` takes the tag.
git(origin, "tag", "main", first_sha)
script = shared[0][2]["run"] if shared else "exit 1"


def run_step(env, label):
    dest = work / f"runner-{label}"
    dest.mkdir()
    r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=dest,
                       env={**base_env, "GITHUB_SERVER_URL": f"file://{work}/gh", "GITHUB_REPOSITORY": "me/t3code", **env},
                       capture_output=True, text=True)
    return dest, r


# Each job's own REF/HISTORY/SPARSE, with the REF expression standing for a branch or a SHA.
branch_refs = {"${{ inputs.ref }}", "${{ env.SERIES_BRANCH }}"}
for name, job_id, step in shared:
    env = dict(step["env"])
    label = f"{name}/{job_id}"
    env["REF"] = "main" if env["REF"] in branch_refs else main_sha
    dest, r = run_step(env, label.replace("/", "-"))
    head = git(dest, "rev-parse", "HEAD") if r.returncode == 0 else ""
    sparse = env["SPARSE"].split()
    present = {p: (dest / p).exists() for p in ["package.json", ".github/scripts/fork-x.sh", ".repos/alchemy-effect/README.md"]}
    if not sparse:
        want = {p: True for p in present}
    elif sparse == ["/*", "!/.repos/"]:
        want = {"package.json": True, ".github/scripts/fork-x.sh": True, ".repos/alchemy-effect/README.md": False}
    else:
        want = {"package.json": False, ".github/scripts/fork-x.sh": True, ".repos/alchemy-effect/README.md": False}
    config = (dest / ".git/config").read_text() if r.returncode == 0 else ""
    shallow = (dest / ".git/shallow").exists()
    check(f"checkout e2e {label}: checks out the commit ({env['HISTORY']}, sparse {env['SPARSE'] or 'none'}) "
          "despite the gitlink, files as asked, no credential in .git",
          r.returncode == 0 and head == main_sha and present == want
          and shallow == (env["HISTORY"] == "tip")
          and not re.search(r"extraheader|token|password|@", config, re.I)
          and git(dest, "symbolic-ref", "-q", "HEAD", ok=False).returncode != 0,
          f"exit {r.returncode}: {r.stderr.strip()} head={head} files={present}")

for history in ["tip", "full"]:
    dest, r = run_step({"REF": "main", "HISTORY": history, "SPARSE": ""}, f"tag-{history}")
    head = git(dest, "rev-parse", "HEAD") if r.returncode == 0 else ""
    check(f"checkout e2e: a branch name takes the branch, never a tag of the same name ({history})",
          r.returncode == 0 and head == main_sha, f"exit {r.returncode}: {r.stderr.strip()} head={head}")
_, r = run_step({"REF": "refs/tags/main", "HISTORY": "tip", "SPARSE": ""}, "tag-ref")
check("checkout e2e: a tag's full ref fails the step (a branch name or a 40-hex SHA only)", r.returncode != 0)
full, r = run_step({"REF": main_sha, "HISTORY": "full", "SPARSE": ""}, "control")
check("checkout e2e: full history reaches the first commit (blobs on demand)",
      r.returncode == 0 and git(full, "rev-list", "--count", "HEAD") == "2")
# The fixture reproduces the failure: this is the command actions/checkout's cleanup runs.
foreach = git(full, "submodule", "foreach", "--recursive", "true", ok=False)
check("checkout e2e: the fixture breaks `git submodule foreach` as upstream's tree does",
      foreach.returncode != 0 and "No url found for submodule path" in foreach.stderr, foreach.stderr.strip())
for label, env in {
    "an unknown SHA": {"REF": "1" * 40, "HISTORY": "full", "SPARSE": ""},
    "an unknown branch": {"REF": "gone", "HISTORY": "tip", "SPARSE": ""},
    "an empty REF": {"REF": "", "HISTORY": "full", "SPARSE": ""},
    "an unknown HISTORY": {"REF": main_sha, "HISTORY": "all", "SPARSE": ""},
}.items():
    _, r = run_step(env, re.sub(r"\W", "-", label))
    check(f"checkout e2e: {label} fails the step", r.returncode != 0)
# A re-run of the step in a workspace that already holds a checkout fails (never mixes two).
leftover, r = run_step({"REF": main_sha, "HISTORY": "tip", "SPARSE": ""}, "leftover")
again = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=leftover,
                       env={**base_env, "GITHUB_SERVER_URL": f"file://{work}/gh", "GITHUB_REPOSITORY": "me/t3code",
                            "REF": main_sha, "HISTORY": "tip", "SPARSE": ""}, capture_output=True, text=True)
check("checkout e2e: a workspace that already holds a checkout fails (never mixes two)",
      r.returncode == 0 and again.returncode != 0)

# --- refusals ----------------------------------------------------------------------------------
follow = real["fork-follow.yml"]
SCRIPT_LINE = re.compile(r'bash \.github/scripts/[\w.-]+\.sh(?: (?:"\$[A-Z_]+"|--?[a-z-]+|[a-z-]+))*')
SWALLOW = re.compile(r"\|\||(^|;|&)\s*(true|:)\s*$|set \+[a-zA-Z]*e|set \+o|\btrap\b|\bexit 0\b|&\s*$|^\s*!",
                     re.M)
SHELL_ENV = {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"}


def refusal_steps(wf):
    """Script steps outside the Report job (whose last sweep may only warn)."""
    for job_id, step in steps(wf):
        if job_id != "report" and "bash .github/scripts/" in str(step.get("run", "")):
            yield job_id, step


def is_refusal(step):
    """The guard, the Environment re-check and the sweep before the move."""
    return "fork-follow-guard.sh" in str(step.get("run", "")) or \
        (step.get("env") or {}).get("REQUIRE_NEW_OFF") == "true"


# Whitelist for a refusal step: these keys, and its run is the optional report folder, then exactly
# one script call. No if, continue-on-error, shell, working-directory: the runner's default
# `bash -e {0}` runs it.
REFUSAL_KEYS = {"id", "name", "env", "run"}
REFUSAL_PRE = {'mkdir -p "$RUNNER_TEMP/reports"'}
# What may run before a refusal in its job: the shared checkout (the checkout rule holds it to one
# text), another refusal step, or one line that writes a step output.
OUTPUT_LINE = re.compile(r'echo "\w+=(?:\$[A-Z_]+|\$\(date -u \+[%A-Za-z:-]+\))" >> "\$GITHUB_OUTPUT"')
SHELL_CHANGE = re.compile(r"GITHUB_ENV|GITHUB_PATH|BASH_ENV|BASH_FUNC_|^\s*(?:function\s+\S+|[^\s=()$]+\s*\(\s*\))",
                          re.M)


def shell_env_keys(env):
    return {k for k in env or {} if k in SHELL_ENV or str(k).startswith("BASH_FUNC_")}


def earlier_step_problem(step, shared_run):
    run = str(step.get("run", ""))
    if "uses" in step:
        return f"an action ({step['uses']}) can write GITHUB_ENV"
    if SHELL_CHANGE.search(run):
        return "writes GITHUB_ENV/GITHUB_PATH/BASH_ENV or defines a shell function"
    if run == shared_run or is_refusal(step):
        return None
    if not set(step) <= {"id", "name", "run"} or not OUTPUT_LINE.fullmatch(run.strip()):
        return "is not the shared checkout, a refusal or one step-output line"
    return None


def refusal_problems(wf):
    problems = []
    if "defaults" in wf:
        problems.append("workflow defaults:")
    for scope, env in [("workflow", wf.get("env"))] + [
            (j, job.get("env")) for j, job in wf["jobs"].items()] + [
            (f"{j}/{s.get('name')}", s.get("env")) for j, s in steps(wf)]:
        problems += [f"{scope}: env {k}" for k in shell_env_keys(env)]
    for job_id, job in wf["jobs"].items():
        if job_id != "report" and ("continue-on-error" in job or "defaults" in job):
            problems.append(f"{job_id}: continue-on-error or defaults on the job")
    shared_run = shared[0][2]["run"] if shared else None
    for job_id, step in refusal_steps(wf):
        where = f"{job_id}/{step.get('name')}"
        lines = [l.strip() for l in str(step["run"]).strip().splitlines() if l.strip()]
        if not is_refusal(step):
            # Other script steps: a blacklist, they may hold more than the call.
            calls = [l for l in lines if "bash .github/scripts/" in l]
            if "continue-on-error" in step:
                problems.append(f"{where}: continue-on-error")
            if step.get("shell", "bash") != "bash":
                problems.append(f"{where}: shell {step.get('shell')}")
            if not calls or any(not SCRIPT_LINE.fullmatch(c) for c in calls) or lines[-1] != calls[-1]:
                problems.append(f"{where}: the script call is not a plain last line")
            if SWALLOW.search(str(step["run"])):
                problems.append(f"{where}: a swallowing construct")
            continue
        # A refusal step: only what the whitelist names.
        if not set(step) <= REFUSAL_KEYS:
            problems.append(f"{where}: keys {', '.join(sorted(set(step) - REFUSAL_KEYS))} on a refusal step")
        body = lines[1:] if lines[:1] and lines[0] in REFUSAL_PRE else lines
        if len(body) != 1 or not SCRIPT_LINE.fullmatch(body[0]):
            problems.append(f"{where}: the run is more than the report folder and one script call")
        job_steps = wf["jobs"][job_id]["steps"]
        for earlier in job_steps[:next(i for i, s in enumerate(job_steps) if s is step)]:
            why = earlier_step_problem(earlier, shared_run)
            if why:
                problems.append(f"{where}: an earlier step ({earlier.get('name') or earlier.get('id')}) {why}")
    return problems


check("refusals: no refusal in fork-follow.yml can be swallowed", not refusal_problems(follow),
      "; ".join(refusal_problems(follow)))
refusal_ids = [(j, s.get("name")) for j, s in refusal_steps(follow) if is_refusal(s)]
check("refusals: the guard, the Environment re-check and the sweep before the move are found",
      len(refusal_ids) == 3 and {j for j, _ in refusal_ids} == {"guard", "advance"}, str(refusal_ids))


def refusal_mutant(index, mutate):
    wf = copy.deepcopy(follow)
    job_id, name = refusal_ids[index]
    step = next(s for s in wf["jobs"][job_id]["steps"] if s.get("name") == name)
    mutate(wf, job_id, step)
    return bool(refusal_problems(wf))


def tail(text):
    return lambda wf, j, s: s.__setitem__("run", s["run"].rstrip("\n") + text + "\n")


swallowers = {
    "|| true": tail(" || true"),
    "|| :": tail(" || :"),
    "|| exit 0": tail(" || exit 0"),
    "; true": tail("; true"),
    "a trailing `true` line": tail("\ntrue"),
    "& (background)": tail(" &"),
    "set +e first": lambda wf, j, s: s.__setitem__("run", "set +e\n" + s["run"]),
    "set +o errexit first": lambda wf, j, s: s.__setitem__("run", "set +o errexit\n" + s["run"]),
    "a trap": lambda wf, j, s: s.__setitem__("run", "trap 'exit 0' EXIT\n" + s["run"]),
    "if ...; then": lambda wf, j, s: s.__setitem__("run", "if " + s["run"].strip() + "; then :; fi\n"),
    "! negation": lambda wf, j, s: s.__setitem__("run", s["run"].replace("bash .github", "! bash .github")),
    "step continue-on-error": lambda wf, j, s: s.__setitem__("continue-on-error", True),
    "job continue-on-error": lambda wf, j, s: wf["jobs"][j].__setitem__("continue-on-error", True),
    "a step if:": lambda wf, j, s: s.__setitem__("if", "false"),
    "shell: sh": lambda wf, j, s: s.__setitem__("shell", "sh -c 'bash {0} || true'"),
    "BASH_ENV on the step": lambda wf, j, s: s.setdefault("env", {}).__setitem__("BASH_ENV", "x"),
    "BASH_ENV on the job": lambda wf, j, s: wf["jobs"][j].setdefault("env", {}).__setitem__("BASH_ENV", "x"),
    "BASH_ENV on the workflow": lambda wf, j, s: wf["env"].__setitem__("BASH_ENV", "x"),
    "defaults on the job": lambda wf, j, s: wf["jobs"][j].__setitem__("defaults", {"run": {"shell": "sh"}}),
    "defaults on the workflow": lambda wf, j, s: wf.__setitem__("defaults", {"run": {"shell": "sh"}}),
    "a bare exit before the call": lambda wf, j, s: s.__setitem__("run", "exit\n" + s["run"]),
    "a bash() function before the call": lambda wf, j, s: s.__setitem__("run", "bash() { echo skip; }\n" + s["run"]),
    "the call inside if false; then ... fi": lambda wf, j, s: s.__setitem__(
        "run", "if false; then\n" + s["run"] + "fi\n"),
    "a second call after the script": tail("\nbash .github/scripts/fork-workflows.sh"),
    "working-directory on the step": lambda wf, j, s: s.__setitem__("working-directory", "/tmp"),
    "BASH_FUNC_bash%% in the step env": lambda wf, j, s: s.setdefault("env", {}).__setitem__(
        "BASH_FUNC_bash%%", "() { :; }"),
    "an earlier step writing BASH_ENV to $GITHUB_ENV": lambda wf, j, s: wf["jobs"][j]["steps"].insert(
        0, {"name": "x", "run": 'echo "BASH_ENV=/tmp/skip" >> "$GITHUB_ENV"'}),
    "an earlier step writing $GITHUB_PATH": lambda wf, j, s: wf["jobs"][j]["steps"].insert(
        0, {"name": "x", "run": 'echo /tmp/fake-bin >> "$GITHUB_PATH"'}),
    "an earlier step defining a function": lambda wf, j, s: wf["jobs"][j]["steps"].insert(
        0, {"name": "x", "run": "function bash { :; }\nexport -f bash"}),
    "an earlier action": lambda wf, j, s: wf["jobs"][j]["steps"].insert(
        0, {"uses": "some/action@0000000000000000000000000000000000000000"}),
    "an earlier step that runs anything else": lambda wf, j, s: wf["jobs"][j]["steps"].insert(
        0, {"name": "x", "run": 'f=GITHUB_; echo "BASH_EN""V=/tmp/x" >> "${!f}EN""V"'}),
}
for index, (job_id, name) in enumerate(refusal_ids):
    missed = [label for label, mutate in swallowers.items() if not refusal_mutant(index, mutate)]
    check(f"refusals: every way to swallow {job_id}/{name} fails the check", not missed, ", ".join(missed))

# --- gh-guard ----------------------------------------------------------------------------------
GUARD_LINE = '. "$(dirname "${BASH_SOURCE[0]}")/fork-gh-guard.sh"'
GH_SELF = {"fork-gh-guard.sh", "fork-test-gh.sh"}
# A gh command: the word gh followed by an argument. "\`gh ...\`" inside a message is text.
GH_CALL = re.compile(r"(?<![\w./-])gh(?=[ \t]+[\w\"'$-])")
GH_STRING = re.compile(r"""["'`]gh["'`\s]""")


def code_lines(text):
    return [(n, l) for n, l in enumerate(text.splitlines(), 1) if not l.lstrip().startswith("#")]


def joined_lines(text):
    """Shell lines with backslash continuations joined, each numbered by its first line."""
    out, start, parts = [], None, []
    for n, line in enumerate(text.splitlines(), 1):
        if not parts and line.lstrip().startswith("#"):
            continue
        start = n if not parts else start
        if line.endswith("\\") and not line.endswith("\\\\"):
            parts.append(line[:-1] + " ")
            continue
        out.append((start, "".join(parts) + line))
        parts = []
    if parts:
        out.append((start, "".join(parts)))
    return out


def gh_calls(name, text):
    if not name.endswith(".sh"):
        return [n for n, l in code_lines(text) if GH_STRING.search(l)]
    return [n for n, l in joined_lines(text) for m in GH_CALL.finditer(l)
            if l[max(0, m.start() - 2):m.start()] != "\\`"]


# Before the guard line only these may stand, so it is sourced at the top level, unconditionally.
GUARD_PRELUDE = re.compile(r"#!.*|set -[a-z]+(?: pipefail)?|")


def gh_guard_problems(files):
    problems = []
    for name, text in sorted(files.items()):
        if name in GH_SELF or ".test." in name:
            continue
        calls = gh_calls(name, text)
        if not calls:
            continue
        if not name.endswith(".sh"):
            problems.append(f"{name}: calls gh but is not a shell script that can source the guard")
            continue
        guard = [n for n, l in enumerate(text.splitlines(), 1) if l == GUARD_LINE]
        if not guard or guard[0] > calls[0]:
            problems.append(f"{name}: calls gh (line {calls[0]}) without sourcing fork-gh-guard.sh first")
            continue
        before = [l for _, l in code_lines("\n".join(text.splitlines()[:guard[0] - 1]))
                  if not GUARD_PRELUDE.fullmatch(l.strip())]
        if before:
            problems.append(f"{name}: {before[0].strip()!r} before the guard line (only #!, set and comments)")
    return problems


def script_files():
    return {p.name: p.read_text() for p in sorted(scripts_dir.glob("fork-*")) if p.is_file()}


files = script_files()
guarded_scripts = sorted(n for n, t in files.items() if n not in GH_SELF and ".test." not in n and gh_calls(n, t))
check("gh-guard: the scan finds the scripts that call gh",
      {"fork-workflows.sh", "fork-follow-guard.sh", "fork-follow-report.sh", "fork-release.sh"} <= set(guarded_scripts),
      ", ".join(guarded_scripts))
check("gh-guard: messages that only name a gh command are not calls",
      not gh_calls("fork-follow.sh", files["fork-follow.sh"]) and not gh_calls("fork-follow-push.sh", files["fork-follow-push.sh"]))
check("gh-guard: every fork script that calls gh sources fork-gh-guard.sh first",
      not gh_guard_problems(files), "; ".join(gh_guard_problems(files)))
for label, (name, text) in {
    "a new script calling gh without the guard": ("fork-new.sh", "#!/usr/bin/env bash\nset -euo pipefail\nx=\"$(gh api user)\"\n"),
    "the guard sourced after the first call": ("fork-new.sh", "gh api user\n" + GUARD_LINE + "\n"),
    "the guard sourced inside a function": ("fork-new.sh", "f() {\n  " + GUARD_LINE + "\n}\n| gh api x\n"),
    "a node script spawning gh": ("fork-new.cjs", 'require("node:child_process").execFileSync("gh", ["api", "x"]);\n'),
    "a python script running gh": ("fork-new.py", "subprocess.run(['gh', 'api', 'x'])\n"),
    "gh after a pipe": ("fork-new.sh", "echo x | gh api x --input -\n"),
    "gh in a command substitution": ("fork-new.sh", 'echo "$(gh api x)"\n'),
    "gh split by a line continuation": ("fork-new.sh", "x=\"$(gh \\\n  api user)\"\n"),
    "gh after a continued line": ("fork-new.sh", "echo a \\\n  | gh api x --input -\n" + GUARD_LINE + "\n"),
    "the guard inside if false; then ... fi": ("fork-new.sh", "set -euo pipefail\nif false; then\n" + GUARD_LINE
                                               + "\nfi\ngh api user\n"),
    "the guard after an exit 0 at the top": ("fork-new.sh", "exit 0\n" + GUARD_LINE + "\ngh api user\n"),
}.items():
    check(f"gh-guard: {label} fails", bool(gh_guard_problems({name: text})))
check("gh-guard: a script that only mentions gh in a message passes",
      not gh_guard_problems({"fork-new.sh": 'echo "start it (\\`gh workflow run x.yml\\`)"\n'}))

# Shell tests: nothing runs before the stub is up but these set-up lines.
SETUP_LINES = [re.compile(p) for p in [
    r"#!/usr/bin/env bash", r"set -[a-z]+( pipefail)?", r'here="\$\(cd "\$\(dirname "\$\{BASH_SOURCE\[0\]\}"\)" && pwd\)"',
    r'root="\$\(mktemp -d\)"', r"""trap 'rm -rf "\$root"' EXIT""", r'\. "\$here/fork-test-gh\.sh"', r'mkdir -p( "\$root/[\w/-]+")+',
]]
STUB_CALL = re.compile(r'fork_test_gh "\$root/[\w/-]+" <<-? ?\'?GH\'?')
SPAWN = re.compile(r"spawnSync|execFileSync|execSync|\bspawn\(|\bexec\(|subprocess|os\.system|Popen")


def test_problems(files):
    problems = []
    gh_scripts = [n for n, t in files.items() if n not in GH_SELF and ".test." not in n and gh_calls(n, t)]
    for name, text in sorted(files.items()):
        # This test runs only git and the checkout step, with its own refusing gh first on PATH.
        if ".test." not in name or name == pathlib.Path(__file__).name:
            continue
        if name.endswith(".sh"):
            before = []
            for line in text.splitlines():
                if STUB_CALL.fullmatch(line):
                    break
                if line.strip() and not line.startswith("#") and not any(p.fullmatch(line) for p in SETUP_LINES):
                    before.append(line)
            else:
                problems.append(f"{name}: never calls fork_test_gh at the top level")
                continue
            if before:
                problems.append(f"{name}: runs {before[0]!r} before the gh stub is set up")
        else:
            code = [l for _, l in code_lines(text)]
            if any(GH_STRING.search(l) for l in code if SPAWN.search(l)) or any(
                    SPAWN.search(l) and any(s in l for s in gh_scripts) for l in code):
                problems.append(f"{name}: runs gh or a script that calls gh; only a .sh test with the stub may")
    return problems


check("gh-guard: every fork shell test sets up the gh stub before anything else; no other test runs gh",
      not test_problems(files), "; ".join(test_problems(files)))
check("gh-guard: the shell tests found include the three that use the stub",
      {"fork-follow.test.sh", "fork-release.test.sh", "fork-secret-scan.test.sh"}
      <= {n for n in files if n.endswith(".test.sh")})
stub_head = "#!/usr/bin/env bash\nset -euo pipefail\nhere=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"\n"
for label, text in {
    "no stub at all": stub_head + "bash \"$here/fork-workflows.sh\"\n",
    "a git command before the stub": stub_head + "git init -q\nroot=\"$(mktemp -d)\"\n. \"$here/fork-test-gh.sh\"\nfork_test_gh \"$root/bin\" <<'GH'\nexit 1\nGH\n",
    "a script run before the stub": stub_head + "root=\"$(mktemp -d)\"\nbash \"$here/fork-release.sh\" resolve\nfork_test_gh \"$root/bin\" <<'GH'\nGH\n",
    "the stub only inside a function": stub_head + "setup() {\nfork_test_gh \"$root/bin\" <<'GH'\nGH\n}\n",
}.items():
    check(f"gh-guard: a shell test with {label} fails", bool(test_problems({**files, "fork-new.test.sh": text})))
check("gh-guard: a node test spawning a gh script fails",
      bool(test_problems({**files, "fork-new.test.cjs": 'spawnSync("bash", [".github/scripts/fork-release.sh", "resolve"]);\n'})))
check("gh-guard: a node test spawning gh fails",
      bool(test_problems({**files, "fork-new.test.cjs": 'execFileSync("gh", ["api", "x"]);\n'})))

# --- removed -----------------------------------------------------------------------------------
removed_path = root / ".github/fork-removed-workflows.txt"
REMOVED_LINE = re.compile(r"\.github/workflows/[^/,\s]+\.ya?ml")
# Owner decision 2026-11-12: outside events start these before the sweep can disable them.
OWNER_REMOVED = {".github/workflows/" + n for n in [
    "mobile-eas-preview-fork.yml", "desktop-macos-preview.yml", "thread-transfer-report.yml",
    "mobile-fingerprint-label.yml"]}


def removed_entries(text):
    return [l.rstrip("\r") for l in text.splitlines() if not re.fullmatch(r"\s*(#.*)?", l.rstrip("\r"))]


def removed_problems(text, tree):
    problems = []
    for line in removed_entries(text):
        if not REMOVED_LINE.fullmatch(line) or line.startswith(".github/workflows/fork-"):
            problems.append(f"{line!r} is not an upstream workflow path")
        elif line in tree:
            problems.append(f"{line} is listed as deleted but is in the tree")
    return problems


tree = {str(p.relative_to(root)) for p in workflows_dir.iterdir()}
removed_text = removed_path.read_text() if removed_path.is_file() else None
check("removed: .github/fork-removed-workflows.txt is there", removed_text is not None)
removed_text = removed_text or ""
check("removed: every line is an upstream workflow path that is not in the tree",
      not removed_problems(removed_text, tree), "; ".join(removed_problems(removed_text, tree)))
check("removed: the list names the four outside-started workflows the owner decided to delete",
      OWNER_REMOVED <= set(removed_entries(removed_text)),
      ", ".join(sorted(OWNER_REMOVED - set(removed_entries(removed_text)))))
for label, (text, extra) in {
    "a listed file still in the tree": (removed_text, {".github/workflows/thread-transfer-report.yml"}),
    "a bare name": (removed_text + "ci.yml\n", set()),
    "a fork-*.yml": (removed_text + ".github/workflows/fork-follow.yml\n", set()),
    "a path outside .github/workflows": (removed_text + ".github/workflows/../ci.yml\n", set()),
}.items():
    check(f"removed: {label} fails", bool(removed_problems(text, tree | extra)))

shutil.rmtree(work, ignore_errors=True)
if failures:
    print(f"{len(failures)} failed")
    sys.exit(1)
print("all passed")
