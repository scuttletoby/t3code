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
  head-gate    fork-head-gate.yml is only a reusable workflow; it checks out only its own toolkit (never
               the caller's tree or a pull request), puts no expression in a run script, every job only
               reads, every action is pinned by commit, only the gate job (after the guard job) names an
               Environment, the App key is named only by the guard's presence test and the one token
               step, and that token reaches only the start step right after it and the finish step
               (the last). No other fork workflow names the key.
  secret-scan  fork-secret-scan.yml runs on every pull request and every push (no branch, tag or
               path filter) on its own, with no concurrency group (workflow or job), so a newer push
               never cancels the scan of an older head; fork-tests.yml's `secrets` job calls it (the
               check a ruleset requires), and no other fork workflow runs the scan script. Its scan
               step runs the script read from the default branch's tip (CONFIG_BRANCH), never the
               checkout's copy. In the job that scans, no step runs the checkout's code (only the
               pinned checkout action and steps that run no workspace file or interpreter), nothing
               may fail quietly (continue-on-error) and no other job's output comes in (needs): the
               pull request's own tests run in a job of their own. The earlier-heads lookup keeps its
               exact form (a failure fails the job; both tips of each activity entry; nothing empties
               the list). The jobs' if: conditions are evaluated as GitHub does for each kind of run.
               The job that runs the scan's tests runs exactly that script against the installed
               gitleaks, and neither it, its steps nor fork-tests.yml's `secrets` call may fail
               quietly. fork-secret-sweep.yml runs the sweep (daily and by hand) by calling the
               scan workflow, read-only; only its alert job, after the sweep whatever its result,
               may write issues, and it hands the sweep's result to fork-run-alert.sh.
  removed      .github/fork-removed-workflows.txt (the upstream workflows the fork deletes, kept
               deleted by fork-follow.sh) is there, every line is an upstream workflow path, none of
               them is in the tree, and it names the files the owner decided to delete.
  result       fork-tests.yml's fork_ci_result, the one check a ruleset requires, needs every other job
               of that workflow except the ones in RESULT_OPTIONAL (none today), so no job's failure
               can merge.
  quiet        nothing in fork-tests.yml may fail quietly: no continue-on-error on a job or a step. A step whose
               run's code lines (not its comment lines) name a fork test, fork-kit, fork-ci or a test file is a
               test step, held to an allowlist, so its exit status is its tests': its run is only lines of the
               allowed test forms (the runner with path prefixes, or `--run <listed fork test>` for the runs the
               test's own `fork-ci: run` lines declare: as root, with its switches, one class; for upstream's
               tests only python3 with -I/-B/-u and a listed .py test, optionally -v <TestClass>, node --test or
               bash and listed tests), each optionally after XDG_RUNTIME_DIR and ending in 2>&1; no sudo, and a
               fork test run by nothing but the runner. Its keys, env and if: are from a fixed set (no
               working-directory, shell only bash, an if: only on upstream's tests, a runner step always with
               the records folder), its job has no defaults, env or container, a fixed if: and no step naming
               GITHUB_ENV, GITHUB_PATH or BASH_ENV, and the workflow has no defaults or env. Every test
               .github/fork-ci-tests.txt lists as run by fork-tests.yml is run whole by such a line, and each test
               with own runs by exactly one `--run` line. The runner calls read from those same accepted lines, job
               by job, are the three pinned ones (RUNNER_PARTS), and put each listed or present test in exactly one
               part, by the runner's own inPart. Anything else is refused with the allowed form.
  records      what each listed fork test ran is proved at run time (#664): each job that calls the runner
               uploads its records (one artifact per job, leg and attempt, none found fails it) and
               fork_ci_result downloads them all and runs the pinned check right after, with RECORD_JOBS (each such
               job and its legs) as the workflow has them. The check's own script, run here on records the real
               runner writes, fails a job or leg without records, a record of another run, attempt, job, leg or
               commit, a listed test (or own run) not recorded, recorded twice, failed, without a test count,
               with none run, or with skips other than exactly the tests its `fork-ci: may skip <test>` lines name
               (each skip by name; an own run: none; old records without names too), and a records file cut off.
               Those lines are pinned (MAY_SKIP), and so are the listed tests another runner runs (RUN_ELSEWHERE).
               The check's script imports only json, os and re (no aliases), uses of them only the names it needs,
               opens files only to read them or to add to the step summary, and reaches no builtins table, dunder
               attribute, exec, eval or getattr.
  policies     every workflow with a pull_request_target trigger is listed in
               .github/fork-actions-policies.json (#401): allowed (the fork workflows the repository's
               Actions policy allows; each there, with that trigger and no other) or upstream_blocked;
               fork-checks' check names are its jobs' names, and fork-tests.yml's fork_checks_ran is
               exactly the pinned job (#430): its if:, permissions, the shared checkout and the step
               running fork-checks-ran.py with the pinned env (PR_NUMBER too, #433), no
               continue-on-error, a checkout with its own timeout, and a job timeout that holds the worst
               case: the checkout's timeout, the wait cap (or the script's git calls, if longer) and one
               last poll, from the script's own limits, with a minute to spare for the job's set-up.
  windows      fork-windows-tests.yml (a nightly report, never in the merge path) runs on schedule and by
               hand only, reads only, cancels an older run, hides no red result, keeps the recorded steps of
               upstream's windows-tests.yml and its 45-minute limit on hosted Windows, and runs the Windows
               drift check itself; fork-tests.yml and its record never read or name windows-tests.yml.

  review-kit   the review kit (fork-kit/review) reads the fork's kit settings
               (.github/fork-review-kit.json, given with --settings) and through them the fork's queue
               config in enforce; without --settings its plan names that file.

    python3 .github/scripts/fork-contracts.test.py

Needs PyYAML, git and bash. Each check prints ok/FAIL; any FAIL exits 1.
"""

import ast
import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile

import yaml

from fork_workflow_expr import gh_eval

root = pathlib.Path(__file__).resolve().parents[2]
workflows_dir = root / ".github/workflows"
scripts_dir = root / ".github/scripts"
failures = []
# The ok lines printed, for the end line: fork-ci-tests.cjs checks its count against them (#664).
oks = []


def check(name, ok, detail=""):
    if ok:
        oks.append(1)
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


def result_job_problems(name, job, jobs):
    where = f"{name}: job {RESULT_JOBS[name]}"
    if job is None:
        return [f"{where} is missing"]
    problems = []
    needs = job.get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs
    if expression(job.get("if")) != "always()":
        problems.append(f"{where}: if is not exactly always() (skipped on an outside pull request = green)")
    if OUTSIDE_JOB not in needs:
        problems.append(f"{where}: does not need {OUTSIDE_JOB}")
    # The ruleset requires only this job: a job it does not need can fail, or be split off, and stay unseen.
    unneeded = sorted(set(jobs) - set(needs) - {RESULT_JOBS[name]})
    if unneeded:
        problems.append(f"{where}: does not need {', '.join(unneeded)} (a failure there would not block a merge)")
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
            problems += result_job_problems(name, jobs.get(RESULT_JOBS[name]), jobs)
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
    "fork_ci_result not needing a job (action_tests)":
        lambda wf: wf["jobs"]["fork_ci_result"]["needs"].remove("action_tests"),
    "a new guarded job fork_ci_result does not need":
        lambda wf: wf["jobs"].__setitem__("extra", {"if": "${{ " + PR_GUARD + " }}", "runs-on": "ubuntu-24.04"}),
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
        if not same or set(step) - {"timeout-minutes"} != {"name", "shell", "env", "run"} \
                or set(env) != {"REF", "HISTORY", "SPARSE"} or env.get("HISTORY") not in ("full", "tip"):
            problems.append(f"{name}/{job_id}: not the shared checkout step")
        # A job may bound its checkout (fork_checks_ran does, #443): a whole number of minutes, at least 1.
        if "timeout-minutes" in step and not (type(step["timeout-minutes"]) is int and step["timeout-minutes"] > 0):
            problems.append(f"{name}/{job_id}: the shared checkout's timeout-minutes is not a positive whole number")
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
for bad in ("3", True, 0, -1):
    check(f"checkout: a copy with timeout-minutes {bad!r} fails",
          checkout_mutant(lambda w, bad=bad: first_step(w, "fork-follow.yml", "advance").__setitem__(
              "timeout-minutes", bad)))
check("checkout: a copy with another key (continue-on-error) fails",
      checkout_mutant(lambda w: first_step(w, "fork-follow.yml", "advance").__setitem__("continue-on-error", True)))
check("checkout: a copy with timeout-minutes 3 passes",
      not checkout_mutant(lambda w: first_step(w, "fork-follow.yml", "advance").__setitem__("timeout-minutes", 3)))
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

# --- head-gate ---------------------------------------------------------------------------------
GATE_FILE = "fork-head-gate.yml"
GATE_KEY = "secrets.HEAD_GATE_APP_KEY"
GATE_READ = {"contents": "read", "pull-requests": "read", "actions": "read"}
GATE_TOOLKIT = {"repository": "${{ steps.toolkit.outputs.repo }}", "ref": "${{ steps.toolkit.outputs.sha }}"}


def head_gate_problems(wfs):
    wf = wfs.get(GATE_FILE)
    if not wf:
        return [f"{GATE_FILE} is missing"]
    problems = []
    if triggers(wf) != {"workflow_call"}:
        problems.append(f"triggers {sorted(map(str, triggers(wf)))}: only workflow_call")
    if wf.get("permissions") != {}:
        problems.append("top-level permissions are not {}")
    jobs = wf.get("jobs") or {}
    for job_id, job in jobs.items():
        if job.get("permissions") != GATE_READ:
            problems.append(f"{job_id}: permissions {job.get('permissions')} are not read-only")
        if "uses" in job:
            problems.append(f"{job_id}: calls another workflow")
        if job_id != "gate" and "environment" in job:
            problems.append(f"{job_id}: names an Environment")
    gate = jobs.get("gate") or {}
    if gate.get("environment") != "${{ inputs.environment }}" or gate.get("needs") != "guard" \
            or "needs.guard.result == 'success'" not in str(gate.get("if")):
        problems.append("gate: the Environment is not behind the guard job")
    tokens = []
    for job_id, step in steps(wf):
        label = f"{job_id}/{step.get('name')}"
        uses = str(step.get("uses", ""))
        if "${{" in str(step.get("run", "")):
            problems.append(f"{label}: an expression inside run")
        if uses and not re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses):
            problems.append(f"{label}: {uses} is not pinned by commit")
        if uses.startswith("actions/checkout@"):
            w = step.get("with") or {}
            if {k: w.get(k) for k in GATE_TOOLKIT} != GATE_TOOLKIT or w.get("path") != "toolkit" \
                    or set(w) != {"repository", "ref", "path", "sparse-checkout"}:
                problems.append(f"{label}: checks out something other than the gate's own toolkit")
        if "git " in str(step.get("run", "")) or "fetch" in str(step.get("run", "")).split():
            problems.append(f"{label}: runs git itself")
        if "steps.token" in str(step):
            tokens.append(label)
        if uses.startswith("actions/create-github-app-token@"):
            if "continue-on-error" in step:
                problems.append(f"{label}: no token must fail the job visibly")
            if job_id != "gate" or step.get("id") != "token" \
                    or {k for k in (step.get("with") or {}) if k.startswith("permission-")} != {"permission-checks"} \
                    or (step.get("with") or {}).get("permission-checks") != "write":
                problems.append(f"{label}: the App token is not the gate's checks-write token")
    gate_steps = gate.get("steps") or []
    makers = [s for s in gate_steps if str(s.get("uses", "")).startswith("actions/create-github-app-token@")]
    at = next((i for i, s in enumerate(gate_steps) if s in makers), -1)
    after = gate_steps[at + 1] if 0 <= at < len(gate_steps) - 1 else {}
    if len(makers) != 1 or after.get("id") != "start":
        problems.append("gate: not exactly one token step, right before the start step")
    if not gate_steps or tokens != [f"gate/{after.get('name')}", f"gate/{gate_steps[-1].get('name')}"] \
            or gate_steps[-1].get("id") != "finish":
        problems.append(f"the App token reaches {tokens}, not only the start step and the finish step (the last)")
    text = yaml.safe_dump(wf)
    if text.count("secrets.") != 2 or text.count(GATE_KEY) != 2 or (makers and GATE_KEY not in str(makers[0])):
        problems.append(f"the key is named {text.count(GATE_KEY)} times, other secrets {text.count('secrets.')}")
    for name, other in wfs.items():
        if name != GATE_FILE and ("HEAD_GATE_APP_KEY" in yaml.safe_dump(other)):
            problems.append(f"{name}: names the gate App key")
    return problems


check("head-gate: fork-head-gate.yml keeps the App key and its token to the token, start and finish steps",
      not head_gate_problems(real), "; ".join(head_gate_problems(real)))


def gate_mutant(mutate):
    wfs = copy.deepcopy(real)
    try:
        mutate(wfs[GATE_FILE], wfs)
    except (KeyError, IndexError, TypeError, StopIteration):  # the shape is missing: the check above failed
        return False
    return bool(head_gate_problems(wfs))


def gate_step(wf, job, name_part):
    return next(s for s in wf["jobs"][job]["steps"] if name_part in str(s.get("name")))


gate_mutants = {
    "a pull_request trigger": lambda wf, _: wf[True].__setitem__("pull_request", None),
    "a write permission": lambda wf, _: wf["jobs"]["gate"]["permissions"].__setitem__("checks", "write"),
    "top-level permissions": lambda wf, _: wf.__setitem__("permissions", {"contents": "read"}),
    "the guard job with the Environment": lambda wf, _: wf["jobs"]["guard"].__setitem__("environment", "head-gate"),
    "the gate job not needing the guard": lambda wf, _: wf["jobs"]["gate"].pop("needs"),
    "the gate job running when the guard failed": lambda wf, _: wf["jobs"]["gate"].__setitem__(
        "if", "always() && needs.guard.outputs.prs != '[]'"),
    "a checkout of the pull request": lambda wf, _: wf["jobs"]["gate"]["steps"].insert(3, {
        "name": "pr", "uses": "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
        "with": {"ref": "${{ github.event.pull_request.head.sha }}"}}),
    "the toolkit at the caller's ref": lambda wf, _: gate_step(wf, "gate", "Check out")["with"].__setitem__(
        "ref", "${{ github.sha }}"),
    "an expression in a run script": lambda wf, _: gate_step(wf, "gate", "Judge").__setitem__(
        "run", gate_step(wf, "gate", "Judge")["run"].replace('"$SHA"', '"${{ matrix.sha }}"')),
    "an action pinned by tag": lambda wf, _: gate_step(wf, "gate", "token").__setitem__(
        "uses", "actions/create-github-app-token@v3"),
    "the token with more permissions": lambda wf, _: gate_step(wf, "gate", "token")["with"].__setitem__(
        "permission-contents", "write"),
    "the token handed to judge": lambda wf, _: gate_step(wf, "gate", "Judge")["env"].__setitem__(
        "GH_TOKEN", "${{ steps.token.outputs.token }}"),
    "the token handed to target": lambda wf, _: gate_step(wf, "gate", "find what to judge")["env"].__setitem__(
        "GH_TOKEN", "${{ steps.token.outputs.token }}"),
    "the token made before target": lambda wf, _: wf["jobs"]["gate"]["steps"].insert(
        3, wf["jobs"]["gate"]["steps"].pop(5)),
    "a token step that hides its failure": lambda wf, _: gate_step(wf, "gate", "token").__setitem__(
        "continue-on-error", True),
    "a step after finish": lambda wf, _: wf["jobs"]["gate"]["steps"].append({"name": "x", "run": "true"}),
    "finish without the token": lambda wf, _: gate_step(wf, "gate", "Complete")["env"].pop("GH_TOKEN"),
    "the key in the judge step": lambda wf, _: gate_step(wf, "gate", "Judge")["env"].__setitem__(
        "KEY", "${{ secrets.HEAD_GATE_APP_KEY }}"),
    "another secret": lambda wf, _: gate_step(wf, "gate", "Judge")["env"].__setitem__("T", "${{ secrets.OTHER }}"),
    "a git fetch in a run step": lambda wf, _: gate_step(wf, "gate", "Judge").__setitem__(
        "run", "git fetch origin\n" + gate_step(wf, "gate", "Judge")["run"]),
    "another fork workflow naming the key": lambda _, wfs: next(
        s for n, w in wfs.items() if n != GATE_FILE for _, s in steps(w) if "run" in s).__setitem__(
        "env", {"K": "${{ secrets.HEAD_GATE_APP_KEY }}"}),
}
for label, mutate in gate_mutants.items():
    check(f"head-gate: {label} fails", gate_mutant(mutate))

# --- secret-scan -------------------------------------------------------------------------------
SCAN_FILE = "fork-secret-scan.yml"
SCAN_CALL = "./.github/workflows/" + SCAN_FILE
SWEEP_FILE = "fork-secret-sweep.yml"
# The default branch: from the event, or for a schedule (whose payload may have no repository, and which
# runs on the default branch) the ref's name.
CONFIG_BRANCH_EXPR = ("${{ github.event.repository.default_branch || "
                      "(github.event_name == 'schedule' && github.ref_name) || '' }}")
TESTS_RUN = "bash .github/scripts/fork-secret-scan.test.sh"


# A step that runs code from the workspace (the checkout): a relative path, a script file, an
# interpreter or a package manager, or the workspace itself.
INTERPRETER = (r"(?<![\w.$/-])(?:bash|sh|zsh|source|eval|exec|python3?|node|npm|npx|pnpm|bun|yarn|make|perl|ruby)\b"
               r"|GITHUB_WORKSPACE|github\.workspace")
CHECKOUT_CODE = re.compile(
    r"""(?:^|[\s;&|(<>'"=])(?:\.\.?/|\.github/|[\w.-]+/|[\w-]+\.(?:sh|py|js|mjs|cjs|ts|pl|rb)\b)|""" + INTERPRETER)
SCAN_STEP_CODE = re.compile(r"(?:^|[\s;&|(])\.\.?/|" + INTERPRETER)
CHECKOUT_ACTION = re.compile(r"actions/checkout@[0-9a-f]{40}")
SCAN_RUN_LINE = 'bash "${RUNNER_TEMP:?}/scan-from-default-branch.sh"'


# The sweep's record of the deleted tips a scan read (#691, #701): read by the scan job's first step
# (before the checkout, so nothing of the checkout has run yet) and kept by an artifact step after the
# scan step, exactly these two steps and nothing else from outside. No cache: any job of a branch can
# write a cache that branch's runs restore, and actions/cache unpacks it with tar -P anywhere on the
# runner (#701 review A); and the repository's caches outgrow their 10 GB, so the record was evicted
# within hours (review B). The read step runs only for the sweep, holds only the job's read-only token
# (no secret), takes the record only from a run of fork-secret-sweep.yml (that path, schedule or
# workflow_dispatch, this branch, this repository and not a fork, ended success or failure or a reset run
# in any state, #992, an ancestor commit, not this run but for a re-run's own newest earlier attempt, in a
# run of the sweep's workflow, unless the re-run's run started before a listed reset run, #992) and its
# one artifact of that name, never unpacks the archive (one plain file read in memory, every line
# checked), and falls back to a first run on any doubt; it reads nothing while a tag is named like the
# branch (#703); reset_record leaves an empty record instead (a reset run's re-run: its own newest
# earlier attempt's record, else, or on any failed or doubtful read, the empty one, #791; "Re-run failed
# jobs" is expected to leave that record in the run (not yet seen live), "Re-run all jobs" deletes it, and
# such a re-run warns and reads as a new run does, #950); a reset run (known by its run name) whose record
# is not read (cancelled, failed before its upload, a re-run under way, or on a commit this branch
# lacks) leaves no older one read either: the empty one (#950, #992). Its output `read`
# (found, none, reset, failed) tells the scan
# step whether a partial record may replace the last one (#703). Its behaviour: the reader e2e below.
# The keep step uploads only on a branch, once the scan step has written the record whole (its output
# `record`) or reset_record left the empty one, for 90 days (the repository's most). What the scan step
# trusts the record for is in SCANNED_LOOKUP_TEXT.
UPLOAD_SHA = "cf430e030ddbb5b0abf93d22962f4752f3646cd9"
READ_RECORD_NAME = "Read the sweep's record"
READ_RECORD_TEXT = r'''set -euo pipefail
dir="${RUNNER_TEMP:?}/sweep-record"
zip="${RUNNER_TEMP:?}/sweep-record.zip"
rm -rf -- "$dir" "$zip"
mkdir -m 700 -- "$dir"
# none STATE WHY: no record. STATE, the output `read`: "none" when no earlier sweep kept one,
# "failed" when one may exist but cannot be read or trusted; then a partial record of this
# run must not replace it (#703). A reset run on a branch (and its re-run) takes the empty
# record instead of "failed": it trusts nothing, so it is always safe, and a failed call
# cannot leave the record from before the reset in use (#791).
none() {
  rm -rf -- "$zip" "$dir/deleted-tips"
  if [[ "$1" == failed && "$RESET" == true && "$GITHUB_REF" == refs/heads/* ]]; then
    echo "::warning title=Secret scan::$2" >&2
    empty_record
  fi
  echo "read=$1" >> "${GITHUB_OUTPUT:?}"
  echo "::warning title=Secret scan::$2, so every listed tip is checked as on a first run" >&2
  exit 0
}
[[ "$GITHUB_REF" == refs/heads/* ]] || none failed "a run on a tag reads no record of the sweep"
# A re-run (attempt 2 on) first looks for its own earlier attempt's record (#703), and only in a
# run of the sweep's own workflow: in another workflow's run, any of its jobs could have kept an
# artifact of that name. "Re-run failed jobs" is expected to leave that record in the run (not
# yet seen live, #950); "Re-run all jobs" deletes it (#950), and the re-run then warns and reads
# as a new run does. Both checks are logged.
self=""
if [[ "${GITHUB_RUN_ATTEMPT:-1}" != 1 \
    && "${GITHUB_WORKFLOW_REF:-}" == "$GITHUB_REPOSITORY/.github/workflows/fork-secret-sweep.yml@"* ]]; then
  self="$GITHUB_RUN_ID $GITHUB_SHA"
fi
[[ "${GITHUB_RUN_ATTEMPT:-1}" == 1 ]] || echo "::notice title=Secret scan::attempt $GITHUB_RUN_ATTEMPT of run $GITHUB_RUN_ID, workflow ref ${GITHUB_WORKFLOW_REF:-(unset)}: $([[ -n "$self" ]] && echo "the sweep's, so its own earlier record is looked for first" || echo "not the sweep's, so its own earlier record is not looked for")"
# reset_record: an empty record (the header only), which the last step keeps even when this
# run fails before the scan step writes its own, so no later sweep reads the older one (#703).
# A re-run of a reset run (it keeps the input) first looks for its own earlier attempt's record,
# which was written after the reset, and no older run's; without one (none kept, or deleted by
# "Re-run all jobs", #950), or when it cannot be read, the empty record.
empty_record() {
  echo "fork secret sweep record 2" > "$dir/deleted-tips"
  echo "read=reset" >> "${GITHUB_OUTPUT:?}"
  echo "::warning title=Secret scan::the sweep's record is reset (input reset_record), so every listed tip is checked as on a first run, and an empty record is kept even if this run fails" >&2
  exit 0
}
[[ "$RESET" != true || -n "$self" ]] || empty_record
export BRANCH="${GITHUB_REF#refs/heads/}"
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || none failed "this branch's name has characters the tag check does not take"
if ! tag="$(gh api -X GET "repos/$GITHUB_REPOSITORY/git/matching-refs/tags/$BRANCH" \
    --jq '.[] | select(.ref == "refs/tags/" + env.BRANCH) | .ref')"; then
  none failed "whether a tag is named like this branch cannot be checked"
fi
[[ -z "$tag" ]] || none failed "a tag is named like this branch, and a sweep run on it would pass for one on the branch"
# The runs that ended in success or failure, and every reset run (known by its run name,
# fork-secret-sweep.yml's run-name, #950) whatever its outcome, unfinished too: a re-run of one
# that waits or runs (#992). So no status filter: the API then lists unfinished runs as well.
runs=""
if [[ "$RESET" != true ]] && ! runs="$(gh api -X GET "repos/$GITHUB_REPOSITORY/actions/workflows/fork-secret-sweep.yml/runs" \
    -f branch="$BRANCH" -f per_page=30 --jq '.workflow_runs[] |
      select(.path == ".github/workflows/fork-secret-sweep.yml" and (.event == "schedule" or .event == "workflow_dispatch")
        and .head_branch == env.BRANCH and .repository.full_name == env.GITHUB_REPOSITORY
        and .head_repository.full_name == env.GITHUB_REPOSITORY
        and (.display_title == "Fork secret sweep (reset_record)"
          or (.status == "completed" and (.conclusion == "success" or .conclusion == "failure")))) |
      "\(.id) \(.head_sha) \(if .display_title == "Fork secret sweep (reset_record)" then "reset" else "sweep" end)"')"; then
  none failed "the sweep's earlier runs cannot be listed"
fi
# A re-run of a sweep run that started before a reset run (a higher run id: ids only go up) does
# not read its own earlier attempt's record, which is from before that reset (#992): it reads as a
# new run does, so the reset run's record or the empty one. It sees only the reset runs listed.
if [[ -n "$self" ]]; then
  while read -r id sha kind; do
    [[ "$kind" == reset && "$id" =~ ^[1-9][0-9]{0,17}$ && "$id" -gt "$GITHUB_RUN_ID" ]] || continue
    echo "::warning title=Secret scan::this re-run's run started before sweep run $id, a reset run (input reset_record), so its own earlier attempt's record, from before that reset, is not read: it reads as a new run does" >&2
    self=""
    break
  done <<< "$runs"
fi
# unread WHY: a run whose record is not read. A reset run's (none kept: cancelled, failed before
# its upload, a re-run of it still under way; expired; or on a commit this branch lacks): an older
# record may be from before the reset, so none is read, and this run takes the empty record (kept
# even if it fails), as that run did (#950, #992). So does a reset run waiting behind this one:
# safe, only a second set of lookups. Any other run: the next one is tried.
unread() {
  [[ "$kind" == reset ]] || return 0
  echo "::warning title=Secret scan::sweep run $id, a reset run (input reset_record), $1, so no record from before it is read" >&2
  empty_record
}
from=""
while read -r id sha kind; do
  [[ "$id" =~ ^[0-9]+$ && "$sha" =~ ^[0-9a-f]{40}$ ]] || continue
  [[ "$id" != "$GITHUB_RUN_ID" || "$id $sha" == "$self" ]] || continue
  # "LISTED KEPT [ID SIZE]": how many artifacts the API listed for the name, and how many of
  # them are this run's unexpired record (at most one, else no record is trusted).
  if ! found="$(RUN="$id" gh api -X GET "repos/$GITHUB_REPOSITORY/actions/runs/$id/artifacts" -f name=fork-secret-sweep-record \
      --jq '"\(.artifacts | length) " +
        ([.artifacts[] | select(.name == "fork-secret-sweep-record" and .expired == false and (.workflow_run.id | tostring) == env.RUN)] |
        if length > 1 then error("more than one") else "\(length) \(.[0].id // "") \(.[0].size_in_bytes // "")" end)')"; then
    none failed "the record of sweep run $id cannot be listed"
  fi
  listed="" kept="" artifact="" size=""
  read -r listed kept artifact size <<< "$found"
  [[ "$listed" =~ ^[0-9]+$ && "$kept" =~ ^[01]$ ]] || none failed "the record of sweep run $id cannot be listed"
  if [[ "$id" == "$GITHUB_RUN_ID" ]]; then
    echo "::notice title=Secret scan::this run's own records: the API listed $listed artifact(s) of that name, $kept of them this run's unexpired record"
    [[ "$kept" == 1 ]] || echo "::warning title=Secret scan::this re-run finds no record of its earlier attempt: that attempt kept none, its record expired, or this is a \"Re-run all jobs\", which deletes an earlier attempt's artifacts (#950; \"Re-run failed jobs\" is expected to keep them, as GitHub's docs say, not yet seen live), so it reads as a new run does" >&2
  fi
  [[ "$kept" == 1 ]] || { unread "kept no record (or it expired)"; continue; }
  [[ "$artifact" =~ ^[0-9]+$ && "$size" =~ ^[0-9]+$ && "$size" -le 4000000 ]] || none failed "the record of sweep run $id is not the sweep's own"
  if ! has="$(gh api -X GET "repos/$GITHUB_REPOSITORY/compare/$sha...$GITHUB_SHA" -f per_page=1 --jq .status)"; then
    none failed "whether this branch has the commit of sweep run $id cannot be checked"
  fi
  [[ "$has" == ahead || "$has" == identical ]] || { unread "ran on a commit this branch does not have"; continue; }
  gh api "repos/$GITHUB_REPOSITORY/actions/artifacts/$artifact/zip" > "$zip" || none failed "the record of sweep run $id cannot be downloaded"
  [[ "$(stat -c %s -- "$zip")" -le 4000000 ]] || none failed "the record of sweep run $id is not the sweep's own"
  from="$id"
  break
done <<< "$self"$'\n'"$runs"
if [[ -z "$from" ]]; then
  [[ "$RESET" != true ]] || empty_record
  none none "no earlier sweep on $BRANCH kept a record (or it expired)"
fi
# The archive's one file, never unpacked: exactly one member, named deleted-tips, a plain
# file (no link, no folder, not encrypted), at most 4 MB; its lines exactly the sweep's own
# (header, then "<commit id> scanned", each once: no other byte, so no NUL or CR).
python3 -I - "$zip" "$dir/deleted-tips" <<'EOF' || none failed "the record of sweep run $from cannot be read"
import re, stat, sys, zipfile
src, dst = sys.argv[1:3]
try:
    with zipfile.ZipFile(src) as z:
        [m] = z.infolist()
        mode = m.external_attr >> 16
        if (m.filename != "deleted-tips" or m.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                or m.flag_bits & 1 or m.file_size > 4000000):
            raise ValueError("not one plain file named deleted-tips")
        with z.open(m) as f:
            data = f.read(4000001)
except Exception as e:
    sys.exit(f"the archive: {type(e).__name__}: {e}")
if len(data) > 4000000 or not re.fullmatch(rb"fork secret sweep record 2\n(?:[0-9a-f]{40} scanned\n)*", data):
    sys.exit("not the sweep's own lines")
tips = data.split(b"\n")[1:-1]
if len(set(tips)) != len(tips):
    sys.exit("a tip listed twice")
with open(dst, "xb") as f:
    f.write(data)
EOF
rm -f -- "$zip"
echo "read=found" >> "${GITHUB_OUTPUT:?}"
echo "::notice title=Secret scan::the sweep's record from run $from: $(($(wc -l < "$dir/deleted-tips") - 1)) deleted tip(s) a scan read"
'''
RECORD_STEPS = {
    "read": {"id": "read", "if": "${{ github.event_name == 'schedule' || github.event_name == 'workflow_dispatch' }}",
             "env": {"GH_TOKEN": "${{ github.token }}", "RESET": "${{ inputs.reset_record }}"},
             "run": READ_RECORD_TEXT},
    "keep": {"if": "${{ always() && (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') "
                   "&& startsWith(github.ref, 'refs/heads/') "
                   "&& (steps.scan.outputs.record == 'written' || steps.read.outputs.read == 'reset') }}",
             "uses": f"actions/upload-artifact@{UPLOAD_SHA}",
             "with": {"name": "fork-secret-sweep-record", "path": "${{ runner.temp }}/sweep-record/deleted-tips",
                      "if-no-files-found": "error", "retention-days": 90, "overwrite": True}},
}


def record_step(step):
    """Which of the record's two steps this step is, exactly (its name aside), else None."""
    bare = {k: v for k, v in step.items() if k != "name"}
    return next((kind for kind, want in RECORD_STEPS.items() if bare == want), None)


def record_problems(scan):
    problems = []
    for j, job in (scan.get("jobs") or {}).items():
        job_steps = job.get("steps") or []
        at = next((i for i, s in enumerate(job_steps) if "scan-from-default-branch" in str(s.get("run", ""))), None)
        if at is None:
            continue
        kinds = [(i, record_step(s)) for i, s in enumerate(job_steps)]
        reads = [i for i, kind in kinds if kind == "read"]
        keeps = [i for i, kind in kinds if kind == "keep"]
        if reads != [0]:
            problems.append(f"{SCAN_FILE}/{j}: the sweep's record is not read exactly once, by the first step, "
                            "in its exact form (#691, #701)")
        if len(keeps) != 1 or keeps[0] < at:
            problems.append(f"{SCAN_FILE}/{j}: the sweep's record is not kept exactly once, after the scan (#691)")
        if job_steps[at].get("id") != "scan":
            problems.append(f"{SCAN_FILE}/{j}: the scan step's id is not `scan` (the keep step reads its output)")
        if job.get("permissions", {"contents": "read"}) != {"contents": "read"} or scan.get("permissions") != {"contents": "read"}:
            problems.append(f"{SCAN_FILE}/{j}: the scan job's token is not exactly contents: read (the record's "
                            "read step needs no more: this repository's runs and artifacts are public)")
    return problems


def scan_job_problems(scan):
    """The job that scans runs nothing from the checkout, before or after the scan (s1 of PR #214: a
    pull request's test script, run on the same runner first, could swap gitleaks or write
    GITHUB_ENV / GITHUB_PATH, and main's scan would then pass a key)."""
    problems = []
    for j, job in (scan.get("jobs") or {}).items():
        job_steps = job.get("steps") or []
        if not any("scan-from-default-branch" in str(s.get("run", "")) for s in job_steps):
            continue
        if "needs" in job:
            problems.append(f"{SCAN_FILE}/{j}: needs another job (its outputs could reach the scan)")
        if job.get("continue-on-error"):
            problems.append(f"{SCAN_FILE}/{j}: continue-on-error (a failed scan would pass)")
        for i, step in enumerate(job_steps):
            label = f"{SCAN_FILE}/{j} step {i + 1} ({step.get('name', step.get('uses', 'run'))})"
            if step.get("continue-on-error"):
                problems.append(f"{label}: continue-on-error (a failed step would pass)")
            if "uses" in step:
                if not CHECKOUT_ACTION.fullmatch(str(step["uses"])) and record_step(step) != "keep":
                    problems.append(f"{label}: an action other than the pinned checkout and the sweep record's "
                                    "keep step (exactly as RECORD_STEPS) runs in the scan job")
                continue
            if record_step(step) == "read":
                continue  # before the checkout, and in its exact form (record_problems; the reader e2e)
            run = str(step.get("run", ""))
            if "scan-from-default-branch" in run:
                # The scan step: git, gh and the one line that runs the default branch's script.
                run = run.replace(SCAN_RUN_LINE, "")
                run = re.sub(r'git show "[^"\s]*:\.github/scripts/fork-secret-scan\.sh"', "git show", run)
                found = SCAN_STEP_CODE.search(run)
            else:
                found = CHECKOUT_CODE.search(run)
            if found:
                problems.append(f"{label}: may run the checkout's code ({found.group(0).strip()!r}); the pull "
                                "request's own tests belong in a job of their own")
    return problems


# The earlier-heads lookup of the scan step, exactly: a failed lookup fails the job, both tips of
# each activity entry are read, and nothing else touches OLD_HEADS (PR #214, c2).
LOOKUP = re.compile(
    r'OLD_HEADS=""\n(?:.*\n)*?\s*if \[\[ "\$EVENT_NAME" == pull_request \]\]; then\n'
    r'\s*if ! OLD_HEADS="\$\(GH_TOKEN="\$LOOKUP_TOKEN" gh api --paginate -X GET "repos/\$GITHUB_REPOSITORY/activity" \\\n'
    r"""\s*-f "ref=refs/heads/\$HEAD_REF" -f per_page=100 --jq '\.\[\] \| \.before, \.after'\)"; then\n"""
    r'\s*echo "::error::[^"\n]*" >&2\n\s*exit [1-9]\n\s*fi\n(?:.*\n)*?\s*unset LOOKUP_TOKEN\n')


# The sweep's lookup (#239), allowed in this exact form only, right after the earlier-heads lookup:
# every tip GitHub's whole activity record lists (no time window: an entry that ages out of a window
# would let the sweep pass with the key still there), of every ref, so the sweep also reads a tip a
# deletion or force push left behind. A failed lookup fails the job.
SWEEP_LOOKUP_TEXT = (
    'elif [[ "$EVENT_NAME" == schedule || "$EVENT_NAME" == workflow_dispatch ]]; then\n'
    '  if ! OLD_HEADS="$(GH_TOKEN="$LOOKUP_TOKEN" gh api --paginate -X GET "repos/$GITHUB_REPOSITORY/activity" \\\n'
    "      -f per_page=100 --jq '.[] | .before, .after')\"; then\n"
    '    echo "::error::Secret scan: cannot list GitHub\'s activity record, so a tip a deletion or force push left'
    ' behind could go unscanned. Re-run the job." >&2\n'
    "    exit 2\n"
    "  fi\n")
SWEEP_LOOKUP = re.compile(
    r'\n\s*fi\n\s*elif \[\[ "\$EVENT_NAME" == schedule \|\| "\$EVENT_NAME" == workflow_dispatch \]\]; then\n'
    r'\s*if ! OLD_HEADS="\$\(GH_TOKEN="\$LOOKUP_TOKEN" gh api --paginate -X GET "repos/\$GITHUB_REPOSITORY/activity" \\\n'
    r"""\s*-f per_page=100 --jq '\.\[\] \| \.before, \.after'\)"; then\n"""
    r'\s*echo "::error::[^"\n]*" >&2\n\s*exit [1-9]\n\s*fi\n\s*fi\n')


def with_sweep_lookup(run):
    """The scan step's run with the sweep's lookup (SWEEP_LOOKUP_TEXT) added, as the workflow
    change of #239 adds it; unchanged when it is there already."""
    if "workflow_dispatch ]]; then" in run:
        return run
    return run.replace("fi\nunset LOOKUP_TOKEN\n", SWEEP_LOOKUP_TEXT + "fi\nunset LOOKUP_TOKEN\n", 1)


# The sweep's lookup of the deleted tips a scan read (#482, #691), allowed in this exact form only
# (each line as written; indentation aside), between the sweep's lookup and the token's unset:
# GitHub's GraphQL API is asked about every tip the clone lacks, every run (a failed query fails the
# job; only a null answer counts as deleted, an answer naming another commit fails the query); the
# tips it shows are fetched in one batch (one by one only when that fails, and one that still fails
# is asked about again); a tip it has no commit for is fetched every run unless the record has it as
# read by a scan, and any failure but "not our ref" fails the job; only a successful secret-scan job
# of a run whose head commit is the tip counts as read; the last sweep's record (the read step) is read
# strictly, any doubt is a first run, and it spares a scanned deleted tip's fetch and lookup (when a
# lookup fails, or the step passes 20 minutes, the tips already proven, every recorded one among them,
# are kept before the job fails, unless the read step could not read the last record, #703); nothing else sets
# SCANNED_GONE, which the scan script trusts to turn a gone tip's failure into a warning, or
# DELETED_TIPS, which it trusts to skip a gone tip's fetch.
SCANNED_LOOKUP_TEXT = r'''SCANNED_GONE=""
DELETED_TIPS=""
if [[ "$EVENT_NAME" == schedule || "$EVENT_NAME" == workflow_dispatch ]]; then
mapfile -t tips < <(tr -s '[:space:]' '\n' <<< "$OLD_HEADS" | grep -E '^[0-9a-f]{40}$' | grep -vE '^0+$' | sort -u)
# Which of them this blob-less clone lacks: one git process for all, lazy fetching off
# (#637). A lazy fetch asks for the whole history again (no negotiation) and every lookup
# of a missing commit reads every pack, so one by one that outran the job's 30 minutes.
if ! lacked="$(printf '%s^{commit}\n' "${tips[@]}" | GIT_NO_LAZY_FETCH=1 git cat-file --batch-check='%(objectname)' 2> /dev/null)"; then
echo "::error::Secret scan: cannot check which listed tips this clone lacks. Re-run the job." >&2
exit 2
fi
mapfile -t missing < <(sed -n 's/^\([0-9a-f]\{40\}\)^{commit} missing$/\1/p' <<< "$lacked")
# The last sweep's record (#691, read by the job's first step): each deleted tip a scan
# read. Exactly its own lines or nothing: anything else (an older version's, a poisoned
# one) is a warning and removed, and every tip is checked as on a first run. The bytes
# are counted, not captured: a captured NUL would be dropped, and mapfile drops it too.
record="${RUNNER_TEMP:?}/sweep-record/deleted-tips"
declare -A recorded=()
if [[ -e "$record" || -L "$record" ]]; then
lines=()
valid=""
if [[ -f "$record" && ! -L "$record" ]] && [[ "$(stat -c %s -- "$record")" -le 4000000 ]] \
&& [[ "$(LC_ALL=C tr -d 'a-z0-9 \n' < "$record" | wc -c)" -eq 0 ]] && mapfile -t lines < "$record" \
&& [[ "${lines[0]:-}" == "fork secret sweep record 2" ]]; then
valid=1
fi
for line in "${lines[@]:1}"; do
if [[ -n "$valid" && "$line" =~ ^([0-9a-f]{40})\ scanned$ ]] \
&& [[ -z "${recorded[${BASH_REMATCH[1]}]:-}" ]]; then
recorded[${BASH_REMATCH[1]}]=1
else
valid=""
fi
done
if [[ -z "$valid" ]]; then
recorded=()
rm -rf -- "$record"
echo "::warning title=Secret scan::the last sweep's record of deleted tips cannot be read, so every listed tip is checked as on a first run" >&2
fi
fi
# Whether a partial record (below) may replace the last one (#703): only when this run
# read it here (or reset it to empty), or no earlier sweep kept one. Never when it could
# not be read: it may be good and bigger, and one failed call must not shrink it.
partial=""
case "${RECORD_READ:-}" in
none) partial=1 ;;
found | reset) [[ ! -f "$record" ]] || partial=1 ;;
esac
# The reviewed list of tips known gone, as the scan script reads it (the default branch's
# tip): a tip on it needs no lookup of its runs. Read loosely here, as the script checks
# the list itself: a misread tip is only looked up, or counted unscanned.
declare -A reviewed=()
if [[ ${#missing[@]} -gt 0 ]]; then
git fetch --quiet --no-tags --filter=blob:none origin "+refs/heads/$CONFIG_BRANCH:refs/scan-script/head"
while read -r tip; do reviewed[$tip]=1; done < <(git show "refs/scan-script/head:.github/fork-secret-sweep-gone-tips.txt" 2> /dev/null \
| sed -nE 's/^([0-9a-f]{40})[[:space:]].*/\1/p')
fi
# ask TIP...: GitHub's GraphQL API, 100 tips a query; sets absent to those it has no commit
# for at all. A failed query, or an answer naming another commit, fails the job.
declare -A absent=()
# shellcheck disable=SC2016 # $owner, $name and $id are GraphQL and jq variables, not the shell's
ask() {
local i tip query answer
absent=()
for ((i = 1; i <= $#; i += 100)); do
query='query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) {'
for tip in "${@:i:100}"; do query+=" c_$tip: object(oid: \"$tip\") { oid }"; done
if ! answer="$(GH_TOKEN="$LOOKUP_TOKEN" gh api graphql -f query="$query } }" \
-f owner="${GITHUB_REPOSITORY%%/*}" -f name="${GITHUB_REPOSITORY#*/}" \
--jq '.data.repository | to_entries[] | (.key | ltrimstr("c_")) as $id |
if .value == null then $id elif .value.oid == $id then empty else error("not the asked commit") end')"; then
echo "::error::Secret scan: cannot ask GitHub's API which listed tips it has no commit for, so a tip git no longer serves could not be told from one GitHub still shows. Re-run the job." >&2
exit 2
fi
while read -r tip; do
[[ ! "$tip" =~ ^[0-9a-f]{40}$ ]] || absent[$tip]=1
done <<< "$answer"
done
}
# GitHub is asked about every tip this clone lacks first (#691): those it still has are
# fetched all at once (one by one only when that fails), and none it deleted can fail
# that batch. A tip it has no commit for: git asked once every run (a tip git serves
# again is fetched, so scanned), unless the record has it as read by a scan.
ask "${missing[@]}"
shown=()
new=()
deleted=()
for tip in "${missing[@]}"; do
if [[ -z "${absent[$tip]:-}" ]]; then
shown+=("$tip")
elif [[ -n "${recorded[$tip]:-}" ]]; then
deleted+=("$tip")
else
new+=("$tip")
fi
done
unserved=()
if [[ ${#shown[@]} -gt 0 ]] \
&& ! timeout 600 git fetch --quiet --no-tags --filter=blob:none origin "${shown[@]}" 2> /dev/null; then
for tip in "${shown[@]}"; do
timeout 300 git fetch --quiet --no-tags --filter=blob:none origin "$tip" 2> /dev/null \
|| unserved+=("$tip")
done
fi
for tip in "${new[@]}"; do
if ! err="$(timeout 300 git fetch --quiet --no-tags --filter=blob:none origin "$tip" 2>&1)"; then
if ! grep -qiE 'not our ref|no such object' <<< "$err"; then
echo "::error::Secret scan: cannot fetch listed tip $tip, which GitHub's API has no commit for, and origin did not say it no longer has it (a network or server failure?). Re-run the job." >&2
exit 2
fi
deleted+=("$tip")
fi
done
# A tip GitHub showed that git could not fetch: asked again (GitHub may have deleted it
# meanwhile); one it still shows stays listed, and the scan script fails on it.
if [[ ${#unserved[@]} -gt 0 ]]; then
ask "${unserved[@]}"
for tip in "${unserved[@]}"; do
[[ -z "${absent[$tip]:-}" ]] || deleted+=("$tip")
done
fi
# keep: this run's record, for the next one (the job's last step keeps it): the deleted
# tips a scan read, as far as proven now (the last record's still deleted ones are marked
# before any lookup, so they are in even when a lookup fails), nothing older (a tip GitHub
# shows again, or the activity record no longer lists, is dropped). Written whole, then renamed, and only
# then marked written: a run stopped before that keeps nothing. Also when a lookup fails
# half-way, or the time limit is reached, before the job fails, if `partial` allows it:
# the next sweep run, or a "Re-run failed jobs" of this one, goes on from there (#701, #703,
# #950); "Re-run all jobs" deletes this attempt's record, so that re-run does not.
# again: what such a stop tells to do. A reset run is never to be re-run, only followed by
# a new run (fork-secret-sweep.yml's header says why), so its errors name no re-run (#950).
again='Start a new run of the sweep (or wait for the next daily one), or use "Re-run failed jobs": both go on from what this one kept. Not "Re-run all jobs": it deletes the record this attempt kept (#950).'
[[ "${RESET:-}" != true ]] || again='This is a reset run (input reset_record): start a new run of the sweep (or wait for the next daily one), never a re-run of this one. The new run goes on from what this one kept (fork-secret-sweep.yml says why).'
declare -A read_by_scan=()
keep() {
local tip
mkdir -p -- "${RUNNER_TEMP:?}/sweep-record"
rm -rf -- "$record.new"
{
echo "fork secret sweep record 2"
for tip in "${deleted[@]}"; do
[[ -z "${read_by_scan[$tip]:-}" ]] || echo "$tip scanned"
done
} > "$record.new"
mv -f -- "$record.new" "$record"
echo "record=written" >> "${GITHUB_OUTPUT:?}"
}
# Which deleted tips a scan read: on the record as read, no lookup (a scan read it for
# good); on the reviewed list, none either (no evidence needed); any other, the runs that
# had it as their head commit, one whose secret-scan job succeeded (fork-secret-scan.yml's
# "Secret scan", or fork-tests.yml's, called or, in older runs, its own).
for tip in "${deleted[@]}"; do
DELETED_TIPS+="$tip "
if [[ -n "${recorded[$tip]:-}" ]]; then
read_by_scan[$tip]=1
SCANNED_GONE+="$tip "
fi
done
for tip in "${deleted[@]}"; do
[[ -z "${read_by_scan[$tip]:-}" && -z "${reviewed[$tip]:-}" ]] || continue
# A time limit well inside the job's 30 minutes (#703): 20 minutes of this step (the
# steps before it take a few). Past it, the lookups stop as when one fails, and the
# record keeps what they proved, so the next sweep run goes on from there (the error
# says how: `again` above).
if ((SECONDS > 1200)); then
echo "::error::Secret scan: this step passed 20 minutes before the lookups of deleted tips ended, so they stop here, before the job's time limit. $again" >&2
[[ -z "$partial" ]] || keep
exit 2
fi
if ! runs="$(GH_TOKEN="$LOOKUP_TOKEN" TIP="$tip" gh api --paginate -X GET "repos/$GITHUB_REPOSITORY/actions/runs" \
-f head_sha="$tip" -f per_page=100 --jq '.workflow_runs[] |
if .head_sha != env.TIP then error("not the asked commit") else . end |
select(.path == ".github/workflows/fork-secret-scan.yml" or .path == ".github/workflows/fork-tests.yml") | .id')"; then
echo "::error::Secret scan: cannot list the runs of deleted tip $tip, so whether a scan read it is unknown. $again" >&2
[[ -z "$partial" ]] || keep
exit 2
fi
mapfile -t ids <<< "$runs"
for id in "${ids[@]}"; do
[[ "$id" =~ ^[0-9]+$ ]] || continue
if ! scanned="$(GH_TOKEN="$LOOKUP_TOKEN" TIP="$tip" gh api --paginate -X GET "repos/$GITHUB_REPOSITORY/actions/runs/$id/jobs" \
-f filter=all -f per_page=100 --jq '.jobs[] | select(.head_sha == env.TIP and .conclusion == "success" and
(.name == "Secret scan" or .name == "Secret scan / Secret scan")) | .id')"; then
echo "::error::Secret scan: cannot list the jobs of run $id of deleted tip $tip, so whether a scan read it is unknown. $again" >&2
[[ -z "$partial" ]] || keep
exit 2
fi
if [[ -n "$scanned" ]]; then
read_by_scan[$tip]=1
SCANNED_GONE+="$tip "
break
fi
done
done
keep
fi
unset LOOKUP_TOKEN
'''


# The lists' hand-over to the scan (#637), in this exact form only, at the step's end: each id once,
# the lists in files under RUNNER_TEMP (written afresh each run; "${RUNNER_TEMP:?}", #691), the files
# exported, the variables too only while they fit in one environment string (the kernel caps one at
# 128 KiB; past that every program the step starts fails, "Argument list too long"), and a default
# branch's script that reads only the variables refused when a list does not fit (#691).
HANDOFF_TEXT = r'''OLD_HEADS="$(tr -s '[:space:]' '\n' <<< "$OLD_HEADS" | sort -u)"
printf '%s\n' "$OLD_HEADS" > "${RUNNER_TEMP:?}/old-heads"
printf '%s\n' "$SCANNED_GONE" > "${RUNNER_TEMP:?}/scanned-gone"
printf '%s\n' "$DELETED_TIPS" > "${RUNNER_TEMP:?}/deleted-tips"
export UPSTREAM OLD_HEADS_FILE="${RUNNER_TEMP:?}/old-heads" SCANNED_GONE_FILE="${RUNNER_TEMP:?}/scanned-gone" \
DELETED_TIPS_FILE="${RUNNER_TEMP:?}/deleted-tips"
if [[ ${#OLD_HEADS} -le 100000 ]]; then export OLD_HEADS; fi
if [[ ${#SCANNED_GONE} -le 100000 ]]; then export SCANNED_GONE; fi
git fetch --quiet --no-tags --filter=blob:none origin "+refs/heads/$CONFIG_BRANCH:refs/scan-script/head"
git show "refs/scan-script/head:.github/scripts/fork-secret-scan.sh" > "${RUNNER_TEMP:?}/scan-from-default-branch.sh"
# A default branch's script from before the files reads only the variables: a list too
# long to export would reach it as an empty list, so a tip it names would go unscanned.
if [[ ${#OLD_HEADS} -gt 100000 || ${#SCANNED_GONE} -gt 100000 ]] \
&& ! grep -q OLD_HEADS_FILE "${RUNNER_TEMP:?}/scan-from-default-branch.sh"; then
echo "::error::Secret scan: the default branch's scan script reads the lists only from the environment, and they are too long for it, so it would scan none of the tips they name. Re-run the job once the default branch has the current script." >&2
exit 2
fi
bash "${RUNNER_TEMP:?}/scan-from-default-branch.sh"
'''


def stripped_lines(run):
    return "".join(line.strip() + "\n" for line in run.splitlines())


LOOKUP_LIMIT_MARGIN = 600


def lookup_problems(scan):
    problems = []
    for j, job in (scan.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            run = str(step.get("run", ""))
            if "scan-from-default-branch" not in run:
                continue
            if not LOOKUP.search(run):
                problems.append(f"{SCAN_FILE}/{j}: the earlier-heads lookup is not in its exact form (fails the job "
                                "on a failed lookup, reads .before and .after)")
            handoff = HANDOFF_TEXT in stripped_lines(run)
            if not handoff:
                problems.append(f"{SCAN_FILE}/{j}: the lists are not handed to the scan in files in their exact "
                                "form (#637)")
            scanned_lookup = SCANNED_LOOKUP_TEXT in stripped_lines(run)
            for name in ("SCANNED_GONE", "DELETED_TIPS"):
                word = rf"\b{name}\b"
                if re.search(word, run) and (
                        not scanned_lookup or not handoff
                        or len(re.findall(word, run)) != len(re.findall(word, SCANNED_LOOKUP_TEXT + HANDOFF_TEXT))):
                    problems.append(f"{SCAN_FILE}/{j}: {name} is not set by the sweep's lookup of deleted tips in "
                                    "its exact form (#482, #691), or is set or used outside it")
            want = (2 + len(re.findall(r"\bOLD_HEADS\b", HANDOFF_TEXT)) + (1 if SWEEP_LOOKUP.search(run) else 0)
                    + (len(re.findall(r"\bOLD_HEADS\b", SCANNED_LOOKUP_TEXT)) if scanned_lookup else 0))
            if len(re.findall(r"\bOLD_HEADS\b", run)) != want:
                problems.append(f"{SCAN_FILE}/{j}: OLD_HEADS is set or used outside the lookups")
            env = step.get("env") or {}
            if env.get("HEAD_REF") != "${{ github.event.pull_request.head.ref }}":
                problems.append(f"{SCAN_FILE}/{j}: HEAD_REF is not the pull request's branch")
            if env.get("LOOKUP_TOKEN") != "${{ github.token }}":
                problems.append(f"{SCAN_FILE}/{j}: LOOKUP_TOKEN is not the job's token")
            # The lookups' time limit (#703) must leave the steps before this one, and the lookup under
            # way when it is passed, inside the job's time-out (else the job is killed and keeps nothing).
            # The limit line missing or reworded fails too, never skips the check (#791).
            limit, timeout = re.search(r"\(\(SECONDS > (\d+)\)\)", run), job.get("timeout-minutes")
            if not limit:
                problems.append(f"{SCAN_FILE}/{j}: no lookups' time limit in the form ((SECONDS > N)), so it cannot be "
                                "checked against the job's timeout-minutes (#703, #791)")
            elif not (type(timeout) is int and int(limit.group(1)) + LOOKUP_LIMIT_MARGIN <= timeout * 60):
                problems.append(f"{SCAN_FILE}/{j}: the lookups' time limit ({limit.group(1)} s) is not at least "
                                f"{LOOKUP_LIMIT_MARGIN} s inside the job's timeout-minutes ({timeout!r}), #703")
            if env.get("RECORD_READ") != "${{ steps.read.outputs.read }}":
                problems.append(f"{SCAN_FILE}/{j}: RECORD_READ is not the read step's output `read` (whether a "
                                "partial record may replace the last one, #703)")
            if env.get("RESET") != "${{ inputs.reset_record }}":
                problems.append(f"{SCAN_FILE}/{j}: RESET is not the input reset_record (a reset run that stops "
                                "half-way must not tell to re-run it, #950)")
    return problems


def run_context(event_name, ref="refs/heads/main", test=None, deleted=False, pr_repo=None):
    event = {"repository": {"default_branch": "main"}, "deleted": deleted}
    if pr_repo:
        event["pull_request"] = {"head": {"repo": {"full_name": pr_repo}}}
    return {"github": {"event_name": event_name, "ref": ref, "repository": "owner/repo", "event": event},
            "inputs": {"test": test} if test is not None else {}}


# Which kinds of run each job of the scan workflow runs for: (context, scan job, tests job). The
# scan: every push but the default branch's own and a deletion (fork-tests.yml calls it with `test`
# for the default branch), every pull request of this repository, the call with `test`, the sweep.
# Its tests: only the call with `test`.
SCAN_RUNS = {
    "a push to a branch": (run_context("push", "refs/heads/feature"), True, False),
    "a push of a tag": (run_context("push", "refs/tags/v1"), True, False),
    "a push to the default branch (fork-tests.yml scans it)": (run_context("push"), False, False),
    "a branch deletion": (run_context("push", "refs/heads/feature", deleted=True), False, False),
    "fork-tests.yml's call on a push to the default branch": (run_context("push", test=True), True, True),
    "a pull request from this repository": (run_context("pull_request", pr_repo="owner/repo"), True, False),
    "fork-tests.yml's call on a pull request": (
        run_context("pull_request", pr_repo="owner/repo", test=True), True, True),
    "a pull request from another fork": (run_context("pull_request", pr_repo="other/repo"), False, False),
    "the daily schedule": (run_context("schedule"), True, False),
    "a manual sweep (workflow_dispatch)": (run_context("workflow_dispatch", "refs/heads/feature"), True, False),
}


def scan_if_problems(scan):
    problems = []
    jobs = scan.get("jobs") or {}
    scan_job = next((j for j, job in jobs.items()
                     if any("scan-from-default-branch" in str(s.get("run", "")) for s in job.get("steps") or [])), None)
    test_job = next((j for j, job in jobs.items()
                     if any("fork-secret-scan.test.sh" in str(s.get("run", "")) for s in job.get("steps") or [])), None)
    if test_job is None:
        problems.append(f"{SCAN_FILE}: no job runs the scan's tests")
    for j, index in ((scan_job, 1), (test_job, 2)):
        if j is None:
            continue
        for label, values in SCAN_RUNS.items():
            ctx, want = values[0], values[index]
            try:
                got = gh_eval(expression(jobs[j].get("if", "true")), ctx)
            except ValueError as e:
                problems.append(f"{SCAN_FILE}/{j}: if: cannot be evaluated ({e})")
                break
            if got != want:
                problems.append(f"{SCAN_FILE}/{j}: {'runs' if got else 'does not run'} on {label}")
    return problems


def tests_job_problems(scan):
    """The job that runs the scan's tests: exactly that script against the installed gitleaks, and
    nothing in it may fail quietly (c3 of PR #214 round 1: continue-on-error on the job would keep
    fork-ci-result green with the tests red)."""
    problems = []
    for j, job in (scan.get("jobs") or {}).items():
        job_steps = job.get("steps") or []
        runs = [s for s in job_steps if "fork-secret-scan.test.sh" in str(s.get("run", ""))]
        if not runs:
            continue
        if "continue-on-error" in job:
            problems.append(f"{SCAN_FILE}/{j}: continue-on-error (failed scan tests would pass)")
        for i, step in enumerate(job_steps):
            if "continue-on-error" in step:
                problems.append(f"{SCAN_FILE}/{j} step {i + 1}: continue-on-error (a failed step would pass)")
        for step in runs:
            if str(step.get("run", "")).strip() != TESTS_RUN or "if" in step:
                problems.append(f"{SCAN_FILE}/{j}: the tests step is not exactly `{TESTS_RUN}`")
            if (step.get("env") or {}).get("GITLEAKS_BIN") != "${{ runner.temp }}/gitleaks":
                problems.append(f"{SCAN_FILE}/{j}: the tests do not run against the installed gitleaks (GITLEAKS_BIN)")
    return problems


ALERT_RUN = re.compile(r'bash \.github/scripts/fork-run-alert\.sh --title "[^"]+" --result "\$RESULT"(?: --message "[^"]*")?')


# The sweep's one input to the scan: reset_record, only when a hand run asks for it (#701).
SWEEP_WITH = {"reset_record": "${{ inputs.reset_record == true }}"}
# Sweeps of one ref one at a time, the running one never cancelled (#791): a sweep that starts while
# a reset run runs waits for it and reads its record, so it cannot undo the reset unseen.
SWEEP_CONCURRENCY = {"group": "fork-secret-sweep-${{ github.ref }}", "cancel-in-progress": False}
# A reset run's run name (#950): the read step knows a reset run only by it, and when it does not read
# that run's record reads no older one (the empty one instead), so a reset is done over, not undone (#992).
RESET_RUN_TITLE = "Fork secret sweep (reset_record)"
SWEEP_RUN_NAME = "${{ inputs.reset_record && '" + RESET_RUN_TITLE + "' || 'Fork secret sweep' }}"
# What a sweep that stops half-way tells to do (#703, #950): a run that may be re-run, and a reset run,
# which never is (one cancelled leaves the whole run cancelled: fork-secret-sweep.yml's header).
RERUN_ADVICE = ('Start a new run of the sweep (or wait for the next daily one), or use "Re-run failed jobs": both go '
                'on from what this one kept. Not "Re-run all jobs": it deletes the record this attempt kept (#950).')
RESET_ADVICE = ("This is a reset run (input reset_record): start a new run of the sweep (or wait for the next daily "
                "one), never a re-run of this one. The new run goes on from what this one kept "
                "(fork-secret-sweep.yml says why).")


def same_group(group, *others):
    """True when GROUP is one of OTHERS, spaces aside (`${{ github.ref }}` and `${{github.ref}}` are one group)."""
    key = lambda g: re.sub(r"\s+", "", str(g))
    return any(o is not None and key(group) == key(o) for o in others)


def sweep_problems(wfs):
    """fork-secret-sweep.yml: the daily and manual sweep, a call of the scan workflow that only
    reads, and one alert job after it, whatever its result, that hands that result to
    fork-run-alert.sh (r1s-3 of PR #214: a red schedule run alerts no one)."""
    sweep = wfs.get(SWEEP_FILE)
    if sweep is None:
        return [f"{SWEEP_FILE} is missing (the daily sweep)"]
    problems = []
    events = triggers(sweep)
    for event in ("schedule", "workflow_dispatch"):
        if event not in events:
            problems.append(f"{SWEEP_FILE}: no {event} trigger (the sweep of every ref)")
    if events - {"schedule", "workflow_dispatch"}:
        problems.append(f"{SWEEP_FILE}: triggers other than schedule and workflow_dispatch "
                        f"({', '.join(sorted(events - {'schedule', 'workflow_dispatch'}))})")
    if sweep.get("permissions") != {"contents": "read"}:
        problems.append(f"{SWEEP_FILE}: workflow permissions are not exactly contents: read")
    if sweep.get("concurrency") != SWEEP_CONCURRENCY:
        problems.append(f"{SWEEP_FILE}: the workflow's concurrency is not one group per ref that never cancels the "
                        "running sweep (a sweep started during a reset run would undo the reset, #791)")
    if sweep.get("run-name") != SWEEP_RUN_NAME:
        problems.append(f"{SWEEP_FILE}: the run name does not mark a reset run as {RESET_RUN_TITLE!r} (the read step "
                        "knows a reset run by it and reads no record from before it, #950, #992)")
    jobs = sweep.get("jobs") or {}
    callers = [j for j, job in jobs.items() if job.get("uses") == SCAN_CALL]
    if len(callers) != 1:
        problems.append(f"{SWEEP_FILE}: not exactly one job calls {SCAN_CALL}")
        return problems
    caller = callers[0]
    if not set(jobs[caller]) <= {"name", "uses", "with"} or jobs[caller].get("with") != SWEEP_WITH:
        problems.append(f"{SWEEP_FILE}/{caller}: more than name, uses and the reset input (an if, other inputs "
                        "or permissions)")
    dispatch = (sweep.get(True) or sweep.get("on") or {}).get("workflow_dispatch") or {}
    if (dispatch.get("inputs") or {}).get("reset_record", {}).get("type") != "boolean" or (
            dispatch["inputs"]["reset_record"].get("default") is not False):
        problems.append(f"{SWEEP_FILE}: no workflow_dispatch input reset_record (boolean, default false): the "
                        "documented way to start the sweep's record afresh (#701)")
    alerts = [j for j, job in jobs.items() if any("fork-run-alert.sh" in str(s.get("run", "")) for s in job.get("steps") or [])]
    if len(alerts) != 1:
        problems.append(f"{SWEEP_FILE}: not exactly one job runs fork-run-alert.sh")
    for j, job in jobs.items():
        if j == caller:
            continue
        perms = job.get("permissions")
        if j not in alerts:
            problems.append(f"{SWEEP_FILE}/{j}: a job other than the sweep and its alert")
            continue
        needs = job.get("needs")
        if (needs if isinstance(needs, list) else [needs]) != [caller]:
            problems.append(f"{SWEEP_FILE}/{j}: does not need exactly the sweep job")
        if not expression(job.get("if", "")).startswith("always() && "):
            problems.append(f"{SWEEP_FILE}/{j}: does not run whatever the sweep's result (if: always() && ...)")
        if "continue-on-error" in job or any("continue-on-error" in s for s in job.get("steps") or []):
            problems.append(f"{SWEEP_FILE}/{j}: continue-on-error (a failed alert would pass)")
        if perms != {"contents": "read", "issues": "write"}:
            problems.append(f"{SWEEP_FILE}/{j}: permissions are not exactly contents: read, issues: write")
        conc = job.get("concurrency")
        if not (isinstance(conc, dict) and conc.get("group") and conc.get("cancel-in-progress") is False):
            problems.append(f"{SWEEP_FILE}/{j}: no concurrency group that queues (two sweeps at once would each open an issue)")
        elif same_group(conc["group"], SWEEP_CONCURRENCY["group"], (sweep.get("concurrency") or {}).get("group")):
            # A job group equal to its own run's workflow group: GitHub sees a deadlock and cancels the
            # job, so a failed sweep would alert no one (#791).
            problems.append(f"{SWEEP_FILE}/{j}: its concurrency group is the workflow's (GitHub cancels the alert "
                            "as a deadlock)")
        for step in job.get("steps") or []:
            run = " ".join(str(step.get("run", "")).split())
            if "fork-run-alert.sh" not in run:
                continue
            if not ALERT_RUN.fullmatch(run) or "if" in step:
                problems.append(f"{SWEEP_FILE}/{j}: the alert step is not one fork-run-alert.sh call with --result \"$RESULT\"")
            if (step.get("env") or {}).get("RESULT") != f"${{{{ needs.{caller}.result }}}}":
                problems.append(f"{SWEEP_FILE}/{j}: RESULT is not the sweep job's result")
    # The scan itself never writes issues: only the sweep's alert job does.
    problems += [f"{SCAN_FILE}/{j}: may write issues" for j, job in ((wfs.get(SCAN_FILE) or {}).get("jobs") or {}).items()
                 if "issues" in str(job.get("permissions", ""))]
    if "issues" in str((wfs.get(SCAN_FILE) or {}).get("permissions", "")):
        problems.append(f"{SCAN_FILE}: may write issues")
    return problems


def secret_scan_problems(wfs):
    problems = []
    scan = wfs.get(SCAN_FILE)
    if scan is None:
        return [f"{SCAN_FILE} is missing"]
    if "pull_request" not in triggers(scan):
        problems.append(f"{SCAN_FILE}: no pull_request trigger (a run of its own that nothing cancels)")
    on = scan.get(True, scan.get("on"))
    if "push" not in triggers(scan):
        problems.append(f"{SCAN_FILE}: no push trigger (a branch is public from its first push)")
    elif isinstance(on, dict) and set(on.get("push") or {}) & {
            "branches", "branches-ignore", "tags", "tags-ignore", "paths", "paths-ignore"}:
        problems.append(f"{SCAN_FILE}: the push trigger is filtered (every push to any branch is scanned)")
    runs = [(j, step) for j, step in steps(scan) if "fork-secret-scan.sh" in str(step.get("run", ""))]
    if not runs:
        problems.append(f"{SCAN_FILE}: no step runs the scan script")
    for j, step in runs:
        run = str(step.get("run", ""))
        fetched = re.search(r'git fetch [^\n]*"\+refs/heads/\$CONFIG_BRANCH:([^"\s]+)"', run)
        if re.search(r"(?<!:\.github/scripts/)fork-secret-scan\.sh", run):
            problems.append(f"{SCAN_FILE}/{j}: names the checkout's scan script (run the default branch's copy)")
        if not fetched or f'git show "{fetched.group(1)}:.github/scripts/fork-secret-scan.sh"' not in run:
            problems.append(f"{SCAN_FILE}/{j}: does not read the scan script from the default branch's tip")
        if (step.get("env") or {}).get("CONFIG_BRANCH") != CONFIG_BRANCH_EXPR:
            problems.append(f"{SCAN_FILE}/{j}: CONFIG_BRANCH is not the default branch")
    if "concurrency" in scan:
        problems.append(f"{SCAN_FILE}: a workflow concurrency group (a newer push could cancel a scan)")
    problems += [f"{SCAN_FILE}: job {j} has a concurrency group"
                 for j, job in (scan.get("jobs") or {}).items() if "concurrency" in job]
    problems += (scan_job_problems(scan) + record_problems(scan) + lookup_problems(scan) + scan_if_problems(scan)
                 + tests_job_problems(scan))
    caller = ((wfs.get("fork-tests.yml") or {}).get("jobs") or {}).get("secrets") or {}
    if caller.get("uses") != SCAN_CALL or (caller.get("with") or {}).get("test") is not True:
        problems.append(f"fork-tests.yml: job secrets does not call {SCAN_CALL} with test: true")
    if not set(caller) <= {"name", "if", "uses", "with"}:
        problems.append(f"fork-tests.yml: job secrets has more than name, if, uses and with "
                        f"({', '.join(sorted(set(caller) - {'name', 'if', 'uses', 'with'}))}): a failed scan could pass")
    problems += sweep_problems(wfs)
    problems += [f"{n}/{j}: runs the scan script outside {SCAN_FILE}" for n, wf in wfs.items() if n != SCAN_FILE
                 for j, step in steps(wf) if "fork-secret-scan.sh" in str(step.get("run", ""))]
    return problems


check("secret-scan: fork-secret-scan.yml runs on pull requests and pushes, uncancelled, with the default "
      "branch's script in a job that runs no checkout code; fork-tests.yml calls it with its tests, which "
      "cannot fail quietly; fork-secret-sweep.yml calls it daily and alerts by issue",
      not secret_scan_problems(real), "; ".join(secret_scan_problems(real)))

# What the sweep's header must tell whoever starts or re-runs a sweep by hand, so a reset holds (#992):
# a re-run of a sweep from before a reset run reads its record from before the reset (and on the
# default branch its alert closes an open issue), and the job's time limit also leaves a run cancelled.
# Comments only, so read from the file itself (words compared, line breaks and `#` aside).
SWEEP_HEADER_RULES = ("never re-run a sweep that started before a reset run",
                      "the scan job's 30-minute limit also leaves a run cancelled")


def sweep_header_problems(text):
    head = text.split("\nrun-name:", 1)[0]
    words = " ".join(" ".join(line.strip().lstrip("#") for line in head.splitlines()).split()).lower()
    return [f"{SWEEP_FILE}: its header does not say {rule!r} (#992)" for rule in SWEEP_HEADER_RULES if rule not in words]


sweep_text = (workflows_dir / SWEEP_FILE).read_text()
check("secret-scan: fork-secret-sweep.yml's header says never to re-run a sweep from before a reset run, and that "
      "the scan job's time limit leaves a run cancelled (#992)",
      not sweep_header_problems(sweep_text), "; ".join(sweep_header_problems(sweep_text)))
for rule in SWEEP_HEADER_RULES:
    # (each rule on one line of the header, so the mutant can drop it)
    check(f"secret-scan: a sweep header without {rule!r} fails (#992)",
          bool(re.search(re.escape(rule), sweep_text, re.I))
          and bool(sweep_header_problems(re.sub(re.escape(rule), "x", sweep_text, flags=re.I))))


def scan_mutant(mutate):
    wfs = copy.deepcopy(real)
    try:
        mutate(wfs)
        return bool(secret_scan_problems(wfs))
    except (KeyError, TypeError, AttributeError, StopIteration):  # nothing to break: the check above has already failed
        return False


GROUP = {"group": "scan-${{ github.ref }}", "cancel-in-progress": True}


def before_scan(wfs, step):
    job_steps = wfs[SCAN_FILE]["jobs"]["scan"]["steps"]
    job_steps.insert(next(i for i, s in enumerate(job_steps) if "scan-from-default-branch" in str(s.get("run", ""))), step)


def scan_step(wfs):
    return next(s for _, s in steps(wfs[SCAN_FILE]) if "scan-from-default-branch" in str(s.get("run", "")))


def read_step(wfs):
    return next((s for s in wfs[SCAN_FILE]["jobs"]["scan"]["steps"] if s.get("name") == READ_RECORD_NAME), None)


def keep_step(wfs):
    return next((s for s in wfs[SCAN_FILE]["jobs"]["scan"]["steps"] if "upload-artifact" in str(s.get("uses"))), None)


for label, mutate in {
    "a workflow concurrency group": lambda w: w[SCAN_FILE].__setitem__("concurrency", GROUP),
    "a job concurrency group": lambda w: w[SCAN_FILE]["jobs"]["scan"].__setitem__("concurrency", GROUP),
    "a concurrency group that does not cancel": lambda w: w[SCAN_FILE].__setitem__(
        "concurrency", {"group": "scan", "cancel-in-progress": False}),
    "no pull_request trigger": lambda w: w[SCAN_FILE][True].pop("pull_request"),
    "no push trigger": lambda w: w[SCAN_FILE][True].pop("push"),
    "a push trigger for main only": lambda w: w[SCAN_FILE][True].__setitem__("push", {"branches": ["main"]}),
    "a push trigger without tags": lambda w: w[SCAN_FILE][True].__setitem__("push", {"branches": ["**"]}),
    "the scan step running the checkout's script": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r'bash "[^"]*"\s*$', "bash .github/scripts/fork-secret-scan.sh\n", scan_step(w)["run"])),
    "the scan step copying the checkout's script": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace(
            "git show ", "cp ./.github/scripts/fork-secret-scan.sh \"$RUNNER_TEMP/x.sh\"\ngit show ", 1)),
    "the scan script read from the head": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace('"+refs/heads/$CONFIG_BRANCH:', '"+refs/heads/$HEAD_REF:')),
    "CONFIG_BRANCH from the pull request": lambda w: scan_step(w)["env"].__setitem__(
        "CONFIG_BRANCH", "${{ github.head_ref }}"),
    "fork-tests.yml scanning in a step of its own": lambda w: w["fork-tests.yml"]["jobs"].__setitem__(
        "secrets", {"runs-on": "ubuntu-24.04", "steps": [{"run": "bash .github/scripts/fork-secret-scan.sh"}]}),
    "fork-tests.yml calling it without its tests": lambda w: w["fork-tests.yml"]["jobs"]["secrets"].pop("with"),
    # s1: nothing from the checkout in the job that scans.
    "the tests back in the scan job, before the scan": lambda w: before_scan(w, w[SCAN_FILE]["jobs"]["tests"]["steps"][-1]),
    "the tests in the scan job, after the scan": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].append(
        w[SCAN_FILE]["jobs"]["tests"]["steps"][-1]),
    "a local action before the scan": lambda w: before_scan(w, {"uses": "./.github/actions/setup"}),
    "a package install before the scan": lambda w: before_scan(w, {"run": "pnpm install --frozen-lockfile"}),
    "a checkout file run by path before the scan": lambda w: before_scan(w, {"run": "scripts/prepare"}),
    "an unpinned action before the scan": lambda w: before_scan(w, {"uses": "actions/setup-node@v4"}),
    "the scan step running a checkout file too": lambda w: scan_step(w).__setitem__(
        "run", "./tools/x\n" + scan_step(w)["run"]),
    "a scan step that may fail quietly": lambda w: scan_step(w).__setitem__("continue-on-error", True),
    "a scan job that may fail quietly": lambda w: w[SCAN_FILE]["jobs"]["scan"].__setitem__("continue-on-error", True),
    "the scan job needing the tests job": lambda w: w[SCAN_FILE]["jobs"]["scan"].__setitem__("needs", "tests"),
    "no tests job": lambda w: w[SCAN_FILE]["jobs"].pop("tests"),
    # c2: the earlier-heads lookup.
    "a failed earlier-heads lookup ignored (|| true)": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace("--jq '.[] | .before, .after')\"; then",
                                           "--jq '.[] | .before, .after' || true)\"; then")),
    "the earlier heads emptied before the export": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace("unset LOOKUP_TOKEN\n", 'unset LOOKUP_TOKEN\nOLD_HEADS=""\n')),
    "the lookup reading the wrong field": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace(".[] | .before, .after", ".[] | .sha")),
    "the lookup's failure not failing the job": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r"exit 2\n", "true\n", scan_step(w)["run"], count=1)),
    "the lookup for another branch": lambda w: scan_step(w)["env"].__setitem__("HEAD_REF", "${{ github.base_ref }}"),
    # #239: the sweep's lookup, in its exact form only.
    "a sweep lookup whose failure is ignored (|| true)": lambda w: scan_step(w).__setitem__(
        "run", with_sweep_lookup(scan_step(w)["run"]).replace(
            "-f per_page=100 --jq '.[] | .before, .after')\"; then\n    echo \"::error::Secret scan: cannot list GitHub's",
            "-f per_page=100 --jq '.[] | .before, .after' || true)\"; then\n    echo \"::error::Secret scan: cannot list GitHub's")),
    "a sweep lookup with a time window (an entry ages out, the sweep passes)": lambda w: scan_step(w).__setitem__(
        "run", with_sweep_lookup(scan_step(w)["run"]).replace(
            "-f per_page=100 --jq '.[] | .before, .after')\"; then\n    echo \"::error::Secret scan: cannot list GitHub's",
            "-f time_period=week -f per_page=100 --jq '.[] | .before, .after')\"; then\n    echo \"::error::Secret scan: cannot list GitHub's")),
    "a sweep lookup whose failure does not fail the job": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r"exit 2\n(\s*fi\n\s*fi\n\s*SCANNED_GONE=)", r"true\n\1", with_sweep_lookup(scan_step(w)["run"]),
                      count=1)),
    "a sweep lookup reading the wrong field": lambda w: scan_step(w).__setitem__(
        "run", with_sweep_lookup(scan_step(w)["run"]).replace(
            "-f per_page=100 --jq '.[] | .before, .after')\"; then\n    echo \"::error::Secret scan: cannot list GitHub's",
            "-f per_page=100 --jq '.[] | .sha')\"; then\n    echo \"::error::Secret scan: cannot list GitHub's")),
    # #482: the sweep's lookup of deleted tips a scan read, in its exact form only.
    "an absent-tips lookup whose failure is ignored (|| true)": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace('error("not the asked commit") end\')"', 'error("not the asked commit") end\' || true)"')),
    "an absent-tips lookup that names every tip": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace("if .value == null then $id", "if true then $id")),
    "an absent-tips lookup that takes any answer": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace('else error("not the asked commit") end', "else $id end")),
    "an absent-tips lookup whose failure does not fail the job": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r"(Re-run the job\.\" >&2\n\s*)exit 2(\n\s*fi\n\s*while read -r tip; do)", r"\1true\2",
                      scan_step(w)["run"], count=1)),
    "shown tips not fetched one by one when the batch fails": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace(
            'timeout 300 git fetch --quiet --no-tags --filter=blob:none origin "$tip" 2> /dev/null \\\n', "")),
    "the lacked tips checked with lazy fetching on (#637)": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace("| GIT_NO_LAZY_FETCH=1 git cat-file --batch-check", "| git cat-file --batch-check")),
    "a failed check of the lacked tips that does not fail the job": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r"exit 2(\n\s*fi\n\s*mapfile -t missing)", r"true\1", scan_step(w)["run"], count=1)),
    "a runs lookup whose failure does not fail the job": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r"exit 2(\n\s*fi\n\s*mapfile -t ids)", r"true\1", scan_step(w)["run"], count=1)),
    "a runs lookup that takes a run of another commit": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace('if .head_sha != env.TIP then error("not the asked commit") else . end |', ".")),
    "a runs lookup that takes any workflow": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace('select(.path == ".github/workflows/fork-secret-scan.yml" or', "select(true or")),
    "a jobs lookup whose failure does not fail the job": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r"exit 2(\n\s*fi\n\s*if \[\[ -n \"\$scanned\" \]\])", r"true\1", scan_step(w)["run"], count=1)),
    "a jobs lookup that takes a failed scan": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace(' and .conclusion == "success" and', " and")),
    "a jobs lookup that takes any job": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace('(.name == "Secret scan" or', '(true or')),
    "SCANNED_GONE set outside its lookup": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace("unset LOOKUP_TOKEN\n", 'SCANNED_GONE="$OLD_HEADS"\nunset LOOKUP_TOKEN\n')),
    "SCANNED_GONE not exported to the scan": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace(' SCANNED_GONE_FILE="${RUNNER_TEMP:?}/scanned-gone"', "")),
    # #637: the lists handed over in files, the variables only while they fit.
    "the earlier heads not handed over in a file": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace(' OLD_HEADS_FILE="${RUNNER_TEMP:?}/old-heads"', "")),
    "the earlier heads exported whatever their size (Argument list too long)": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace("if [[ ${#OLD_HEADS} -le 100000 ]]; then export OLD_HEADS; fi", "export OLD_HEADS")),
    "SCANNED_GONE exported whatever its size": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace("if [[ ${#SCANNED_GONE} -le 100000 ]]; then export SCANNED_GONE; fi",
                                           "export SCANNED_GONE")),
    "the earlier heads' file appended to, not written afresh": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace('"$OLD_HEADS" > "${RUNNER_TEMP:?}/old-heads"',
                                           '"$OLD_HEADS" >> "${RUNNER_TEMP:?}/old-heads"')),
    # #691: the sweep's record, and the hand-over's two minors.
    "DELETED_TIPS not handed to the scan": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r' \\\n\s*DELETED_TIPS_FILE="\$\{RUNNER_TEMP:\?\}/deleted-tips"', "", scan_step(w)["run"])),
    "DELETED_TIPS set outside its lookup": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace("unset LOOKUP_TOKEN\n", 'DELETED_TIPS="$OLD_HEADS"\nunset LOOKUP_TOKEN\n')),
    "a list too long for an old default-branch script handed over anyway (read as empty)": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r"\n *# A default branch's script from before the files(?:.*\n)*? *fi\n", "\n", scan_step(w)["run"])),
    "the hand-over with RUNNER_TEMP unguarded": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace('> "${RUNNER_TEMP:?}/old-heads"', '> "$RUNNER_TEMP/old-heads"')),
    "the record trusted for a tip GitHub shows (recorded tips not asked about)": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace('ask "${missing[@]}"', 'ask $(for tip in "${missing[@]}"; do [[ -n "${recorded[$tip]:-}" ]] || echo "$tip"; done)')),
    "a record with any line taken": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r'(else\n\s*)valid=""\n', r"\1true\n", scan_step(w)["run"])),
    "a record that cannot be read trusted as empty and silent": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r"\n *echo \"::warning title=Secret scan::the last sweep's record[^\n]*", "", scan_step(w)["run"])),
    "the record written in place (a stopped run leaves half of one)": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r'\} > "\$record\.new"\n\s*mv -f -- "\$record\.new" "\$record"\n', '} > "$record"\n',
                      scan_step(w)["run"])),
    "a new deleted tip's fetch failure taken as deleted (a network error)": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace("if ! grep -qiE 'not our ref|no such object' <<< \"$err\"; then", "if false; then")),
    # #701: the record as an artifact of the sweep's own runs, read before the checkout, no cache.
    "the record read on every event (a pull request's or push's scan)": lambda w: read_step(w).pop("if"),
    "the record read with a secret in its step": lambda w: read_step(w)["env"].__setitem__(
        "PUSH", "${{ secrets.FORK_PUSH_TOKEN }}"),
    "the record read after the checkout": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].insert(
        2, w[SCAN_FILE]["jobs"]["scan"]["steps"].pop(0)),
    "the record read twice": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].insert(
        3, copy.deepcopy(read_step(w))),
    "the record read by an edited step (no ancestor check)": lambda w: read_step(w).__setitem__(
        "run", re.sub(r'\n *\[\[ "\$has" == ahead[^\n]*', "", read_step(w)["run"])),
    "the record read with the archive unpacked": lambda w: read_step(w).__setitem__(
        "run", read_step(w)["run"].replace('python3 -I - "$zip"', 'unzip -o "$zip" -d "$dir"; : python3 -I - "$zip"')),
    "the scan job with actions: write": lambda w: w[SCAN_FILE]["jobs"]["scan"].__setitem__(
        "permissions", {"contents": "read", "actions": "write"}),
    "the record kept by an action by tag, not pinned": lambda w: keep_step(w).__setitem__(
        "uses", "actions/upload-artifact@v7"),
    "the record kept for a day (the next sweep would not find it)": lambda w: keep_step(w)["with"].__setitem__(
        "retention-days", 1),
    "the record kept though not written whole": lambda w: keep_step(w).__setitem__("if", "${{ always() }}"),
    "the record kept on every event": lambda w: keep_step(w).__setitem__(
        "if", "${{ always() && steps.scan.outputs.record == 'written' }}"),
    "the record kept from the workspace": lambda w: keep_step(w)["with"].__setitem__("path", "sweep-record"),
    "the record kept before the scan": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].insert(
        2, w[SCAN_FILE]["jobs"]["scan"]["steps"].pop()),
    "the record not kept": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].pop(),
    "the record restored from a cache again": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].insert(
        1, {"uses": "actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
            "with": {"path": "${{ runner.temp }}/sweep-record", "key": "x", "restore-keys": "fork-secret-sweep-record-"}}),
    "the scan step without its id": lambda w: scan_step(w).pop("id"),
    # #703: the read step's output decides whether a partial record may replace the last one.
    "the read step without its id": lambda w: read_step(w).pop("id"),
    "the scan step without RECORD_READ": lambda w: scan_step(w)["env"].pop("RECORD_READ"),
    # #950: a reset run's half-way stop tells to start a new run, never a re-run.
    "the scan step without RESET": lambda w: scan_step(w)["env"].pop("RESET"),
    "the scan job's time-out cut to 25 minutes (the lookups' 20-minute limit no longer well inside it)": lambda w: w[
        SCAN_FILE]["jobs"]["scan"].__setitem__("timeout-minutes", 25),
    "the scan step with RECORD_READ fixed (a failed read taken as none)": lambda w: scan_step(w)["env"].__setitem__(
        "RECORD_READ", "none"),
    "the record kept from a run on a tag": lambda w: keep_step(w).__setitem__(
        "if", keep_step(w)["if"].replace("startsWith(github.ref, 'refs/heads/') && ", "")),
    "the record kept without reset_record's empty one": lambda w: keep_step(w).__setitem__(
        "if", keep_step(w)["if"].replace(" || steps.read.outputs.read == 'reset'", "")),
    "a sweep that always resets its record": lambda w: w[SWEEP_FILE]["jobs"]["sweep"].__setitem__(
        "with", {"reset_record": True}),
    "a sweep without its reset input": lambda w: (w[SWEEP_FILE][True]["workflow_dispatch"] or {}).pop("inputs", None),
    # c2: the jobs' if: as GitHub evaluates it.
    "every push skipped": lambda w: w[SCAN_FILE]["jobs"]["scan"].__setitem__(
        "if", w[SCAN_FILE]["jobs"]["scan"]["if"].replace("(github.event.deleted ||", "(true || github.event.deleted ||")),
    "tag pushes skipped": lambda w: w[SCAN_FILE]["jobs"]["scan"].__setitem__(
        "if", w[SCAN_FILE]["jobs"]["scan"]["if"].replace(
            "github.event.deleted ||", "github.event.deleted || startsWith(github.ref, 'refs/tags/') ||")),
    "the default branch's call skipped": lambda w: w[SCAN_FILE]["jobs"]["scan"].__setitem__(
        "if", w[SCAN_FILE]["jobs"]["scan"]["if"].replace("!inputs.test && ", "")),
    "the sweep skipped": lambda w: w[SCAN_FILE]["jobs"]["scan"].__setitem__(
        "if", w[SCAN_FILE]["jobs"]["scan"]["if"].replace(
            "(!(github.event_name == 'push'", "(github.event_name != 'schedule' && !(github.event_name == 'push'")),
    "the tests job on every pull request": lambda w: w[SCAN_FILE]["jobs"]["tests"].__setitem__(
        "if", w[SCAN_FILE]["jobs"]["tests"]["if"].replace("(inputs.test)", "(true)")),
    # c3 of round 1: the tests job, its steps and the call cannot fail quietly or test nothing.
    "a tests job that may fail quietly": lambda w: w[SCAN_FILE]["jobs"]["tests"].__setitem__("continue-on-error", True),
    "a tests step that may fail quietly": lambda w: w[SCAN_FILE]["jobs"]["tests"]["steps"][-1].__setitem__(
        "continue-on-error", True),
    "a gitleaks install that may fail quietly in the tests job": lambda w: w[SCAN_FILE]["jobs"]["tests"]["steps"][
        1].__setitem__("continue-on-error", "${{ true }}"),
    "a tests step that tests nothing (exit 0)": lambda w: w[SCAN_FILE]["jobs"]["tests"]["steps"][-1].__setitem__(
        "run", "exit 0"),
    "a tests step that ignores its failure": lambda w: w[SCAN_FILE]["jobs"]["tests"]["steps"][-1].__setitem__(
        "run", TESTS_RUN + " || true"),
    "the tests without the real gitleaks": lambda w: w[SCAN_FILE]["jobs"]["tests"]["steps"][-1].pop("env"),
    "fork-tests.yml's scan call that may fail quietly": lambda w: w["fork-tests.yml"]["jobs"]["secrets"].__setitem__(
        "continue-on-error", True),
    # The daily sweep (the gaps no push run covers) and its alert.
    "no fork-secret-sweep.yml": lambda w: w.pop(SWEEP_FILE),
    "a sweep without its schedule": lambda w: w[SWEEP_FILE][True].pop("schedule"),
    "a sweep without workflow_dispatch": lambda w: w[SWEEP_FILE][True].pop("workflow_dispatch"),
    "a sweep that does not call the scan": lambda w: w[SWEEP_FILE]["jobs"]["sweep"].__setitem__(
        "uses", "./.github/workflows/fork-tests.yml"),
    "a sweep job skipped (if: false)": lambda w: w[SWEEP_FILE]["jobs"]["sweep"].__setitem__("if", "${{ false }}"),
    "a sweep with issues: write for every job": lambda w: w[SWEEP_FILE].__setitem__(
        "permissions", {"contents": "read", "issues": "write"}),
    "no alert job": lambda w: w[SWEEP_FILE]["jobs"].pop("alert"),
    "an alert only when the sweep passed": lambda w: w[SWEEP_FILE]["jobs"]["alert"].__setitem__(
        "if", "${{ github.event_name == 'schedule' }}"),
    "an alert not after the sweep": lambda w: w[SWEEP_FILE]["jobs"]["alert"].pop("needs"),
    "an alert without its concurrency group": lambda w: w[SWEEP_FILE]["jobs"]["alert"].pop("concurrency"),
    # #791: the sweeps of one ref one at a time, the running one never cancelled.
    "a sweep without its concurrency group": lambda w: w[SWEEP_FILE].pop("concurrency"),
    # #950, #992: the read step knows a reset run by its run name.
    "a sweep without its run name": lambda w: w[SWEEP_FILE].pop("run-name"),
    "a sweep whose run name does not mark a reset run": lambda w: w[SWEEP_FILE].__setitem__(
        "run-name", "Fork secret sweep"),
    "a sweep whose concurrency cancels the running sweep": lambda w: w[SWEEP_FILE]["concurrency"].__setitem__(
        "cancel-in-progress", True),
    "a sweep whose concurrency group is one run's own (nothing waits)": lambda w: w[SWEEP_FILE]["concurrency"].__setitem__(
        "group", "fork-secret-sweep-${{ github.run_id }}"),
    "a sweep whose concurrency group is the alert's": lambda w: w[SWEEP_FILE]["concurrency"].__setitem__(
        "group", "fork-secret-sweep-alert"),
    "an alert whose concurrency group is the sweep's (a deadlock: GitHub cancels the alert)": lambda w: w[SWEEP_FILE][
        "jobs"]["alert"]["concurrency"].__setitem__("group", "fork-secret-sweep-${{ github.ref }}"),
    "an alert whose concurrency group is the sweep's, spaced otherwise": lambda w: w[SWEEP_FILE][
        "jobs"]["alert"]["concurrency"].__setitem__("group", "fork-secret-sweep-${{github.ref}}"),
    "an alert whose concurrency cancels a running one": lambda w: w[SWEEP_FILE]["jobs"]["alert"]["concurrency"].__setitem__(
        "cancel-in-progress", True),
    "an alert that may fail quietly": lambda w: w[SWEEP_FILE]["jobs"]["alert"].__setitem__("continue-on-error", True),
    "an alert without issues: write": lambda w: w[SWEEP_FILE]["jobs"]["alert"].__setitem__(
        "permissions", {"contents": "read"}),
    "an alert always told success": lambda w: w[SWEEP_FILE]["jobs"]["alert"]["steps"][-1]["env"].__setitem__(
        "RESULT", "success"),
    "an alert whose failure is ignored": lambda w: w[SWEEP_FILE]["jobs"]["alert"]["steps"][-1].__setitem__(
        "run", w[SWEEP_FILE]["jobs"]["alert"]["steps"][-1]["run"] + " || true"),
    "the scan workflow with issues: write": lambda w: w[SCAN_FILE]["jobs"]["scan"].__setitem__(
        "permissions", {"contents": "read", "issues": "write"}),
}.items():
    check(f"secret-scan: {SCAN_FILE} with {label} fails" if "fork-tests" not in label and "sweep" not in label
          and "alert" not in label else f"secret-scan: {label} fails", scan_mutant(mutate))

# #791: the lookups' time-limit line reworded or gone: its own check against the job's time-out fails
# (not only the exact-text contract), so it can never pass quietly by not finding the line.
for what, new in (("reworded (>=)", "if ((SECONDS >= 1200)); then"), ("gone", "if false; then")):
    tl_wfs = copy.deepcopy(real)
    tl_step = scan_step(tl_wfs)
    tl_run = tl_step["run"].replace("if ((SECONDS > 1200)); then", new)
    tl_step["run"] = tl_run
    tl_found = [p for p in lookup_problems(tl_wfs[SCAN_FILE]) if "no lookups' time limit" in p]
    check(f"secret-scan: the lookups' time-limit line {what}: its check against the job's timeout-minutes fails, "
          "not skips (#791)", tl_run != scan_step(real)["run"] and bool(tl_found),
          "the line not found in the real file" if tl_run == scan_step(real)["run"] else "no time-limit problem")

check("secret-scan: the sweep's activity lookup (#239) in its exact form passes",
      not scan_mutant(lambda w: scan_step(w).__setitem__("run", with_sweep_lookup(scan_step(w)["run"])))
      and SWEEP_LOOKUP.search(with_sweep_lookup(scan_step(real)["run"])) is not None)

# #637 and #691, end to end: the sweep's lookup of the tips it lacks, as the workflow has it (bash
# -e -o pipefail), in a blob-less clone of a local origin that still serves three tips no ref holds
# and lacks others (deleted), with a gh stub that answers as GitHub would. The sweep of 11-20 ran
# out of the job's 30 minutes here: `git cat-file -e` per tip, with lazy fetching on, fetched each
# lacked tip with the whole history again (no negotiation), and each lookup of a missing commit read
# every pack that left. Then (#691) its cost grew with the deleted tips: each was fetched again and
# its runs looked up again every day (231 of about 250 API calls), and one deleted tip in the batch
# sent every served one to the one-by-one fetch. Now: one cat-file for all tips, no lazy fetch,
# GitHub asked first, one batch of the tips it shows, git asked about a deleted tip until a scan is
# known to have read it, and the record (RUNNER_TEMP/sweep-record, the read step's) spares the rest.
# Also: run again (same clone, or a new runner with the record), stopped half-way (before or while
# writing the record), the first run (no record), a record that cannot be read, a tip that comes
# back, no tips at all, and no clone.
def sweep_lookup_code(run):
    start, end = run.find('SCANNED_GONE=""\n'), run.find("unset LOOKUP_TOKEN\n")
    return run[start:end] if 0 <= start < end else "exit 99\n"


sweep_work = pathlib.Path(tempfile.mkdtemp(prefix="fork-contracts-sweep-"))
sweep_origin = sweep_work / "origin"
sweep_origin.mkdir()
git(sweep_origin, "init", "-q", "-b", "main")
git(sweep_origin, "config", "uploadpack.allowFilter", "true")
git(sweep_origin, "config", "uploadpack.allowAnySHA1InWant", "true")
(sweep_origin / "a.txt").write_text("a\n")
git(sweep_origin, "add", "-A")
git(sweep_origin, "commit", "-q", "-m", "main")
sweep_main = git(sweep_origin, "rev-parse", "HEAD")
sweep_vanished = []
for n in range(3):  # a branch pushed, then deleted: no ref holds its tip, origin still serves it
    git(sweep_origin, "checkout", "-q", "-b", f"side{n}", sweep_main)
    (sweep_origin / "a.txt").write_text(f"side {n}\n")
    git(sweep_origin, "commit", "-q", "-am", f"side {n}")
    sweep_vanished.append(git(sweep_origin, "rev-parse", "HEAD"))
    git(sweep_origin, "checkout", "-q", "main")
    git(sweep_origin, "branch", "-q", "-D", f"side{n}")
sweep_gone = "ab" * 20  # listed by the activity record, garbage-collected by GitHub
sweep_gone2 = "cd" * 20  # another, on the reviewed list of a branch "reviewed"
git(sweep_origin, "checkout", "-q", "-b", "reviewed", sweep_main)
(sweep_origin / ".github").mkdir()
(sweep_origin / ".github" / "fork-secret-sweep-gone-tips.txt").write_text(f"{sweep_gone2} checked by hand (test)\n")
git(sweep_origin, "add", "-A")
git(sweep_origin, "commit", "-q", "-m", "the reviewed list")
git(sweep_origin, "checkout", "-q", "main")
sweep_stub = sweep_work / "bin"
sweep_stub.mkdir()
# GitHub as the stub has it: GraphQL has no commit for FAKE_GONE's tips, nor for FAKE_LATE's after
# the first question about it (deleted meanwhile); a run's secret-scan job succeeded for FAKE_SCANNED's.
(sweep_stub / "gh").write_text(r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  *graphql*)
    for a in "$@"; do [[ "$a" != query=* ]] || q="$a"; done
    for id in $(grep -oE 'c_[0-9a-f]{40}' <<< "$q" | cut -c3-); do
      if [[ "$id" == "${FAKE_LATE:-}" ]]; then
        if [[ -e "$GH_LOG.late" ]]; then echo "$id"; else : > "$GH_LOG.late"; fi
      elif [[ " $FAKE_GONE " == *" $id "* ]]; then
        echo "$id"
      fi
    done ;;
  *actions/runs*)
    # FAKE_BUDGET: the token's lookups left; past them every Actions call fails, as at a rate limit.
    if [[ -n "${FAKE_BUDGET:-}" ]]; then
      used=$(( $(cat "$GH_LOG.used" 2> /dev/null || echo 0) + 1 ))
      echo "$used" > "$GH_LOG.used"
      if (( used > FAKE_BUDGET )); then echo "gh stub: HTTP 403: API rate limit exceeded" >&2; exit 1; fi
    fi ;;&
  *actions/runs/*/jobs*)
    if [[ -n "${FAKE_JOBS_FAIL:-}" ]]; then echo "gh stub: HTTP 502" >&2; exit 1; fi
    [[ " $FAKE_SCANNED " != *" $TIP "* ]] || echo 9001 ;;
  *actions/runs*)
    if [[ -n "${FAKE_RUNS_FAIL:-}" ]]; then echo "gh stub: HTTP 502" >&2; exit 1; fi
    echo 101 ;;
  *) echo "gh stub: unexpected call: $*" >&2; exit 97 ;;
esac
""")
# A network failure on git's one fetch of FAKE_NET_FAIL (not git saying it lacks the commit).
(sweep_stub / "timeout").write_text(rf"""#!/usr/bin/env bash
if [[ -n "${{FAKE_NET_FAIL:-}}" && " $* " == *" $FAKE_NET_FAIL "* ]]; then
  echo "fatal: unable to access 'https://github.com/me/t3code/': Could not resolve host: github.com" >&2
  exit 128
fi
exec {shutil.which("timeout")} "$@"
""")
for stub in ("gh", "timeout"):
    (sweep_stub / stub).chmod(0o755)
lookup_code = sweep_lookup_code(scan_step(real)["run"])
RECORD_HEAD = "fork secret sweep record 2\n"


def sweep_clone(label):
    dest = sweep_work / label
    git(sweep_work, "clone", "-q", "--filter=blob:none", "--no-checkout", f"file://{sweep_origin}", str(dest))
    return dest


def sweep_temp(label, record=None):
    """A runner's RUNNER_TEMP, with the record as the read step leaves it (None: no record)."""
    temp = sweep_work / f"{label}.temp"
    temp.mkdir()
    if record is not None:
        (temp / "sweep-record").mkdir()
        (temp / "sweep-record" / "deleted-tips").write_text(record)
    return temp


def sweep_lookup(dest, old_heads, label, code=None, temp=None, gone=None, scanned=None, branch="main", **env):
    """Runs the lookup in dest, with RUNNER_TEMP temp (by default a new one: no record) and RECORD_READ
    (the read step's output) "found" when temp holds a record, else "none" (env may set another);
    returns its exit (.r), SCANNED_GONE (.gone) and DELETED_TIPS (.deleted) as word lists, the git
    trace, the gh calls, the record it leaves (None: none) and its step output."""
    trace, gh_log, output = (sweep_work / f"{label}.{x}" for x in ("trace", "gh", "out"))
    for f in (trace, gh_log, output):
        f.write_text("")
    pathlib.Path(f"{gh_log}.late").unlink(missing_ok=True)
    pathlib.Path(f"{gh_log}.used").unlink(missing_ok=True)
    temp = temp or sweep_temp(label)
    read = "found" if os.path.lexists(temp / "sweep-record" / "deleted-tips") else "none"
    script = (code or lookup_code) + 'printf "SCANNED_GONE=[%s]\\nDELETED_TIPS=[%s]\\n" "$SCANNED_GONE" "$DELETED_TIPS"\n'
    r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=dest,
                       env={**base_env, "PATH": f"{sweep_stub}:{base_env['PATH']}", "GITHUB_REPOSITORY": "me/t3code",
                            "EVENT_NAME": "schedule", "OLD_HEADS": old_heads, "LOOKUP_TOKEN": "x",
                            "RUNNER_TEMP": str(temp), "GITHUB_OUTPUT": str(output), "CONFIG_BRANCH": branch,
                            "GH_LOG": str(gh_log), "FAKE_GONE": " ".join([sweep_gone, sweep_gone2]) if gone is None else gone,
                            "FAKE_SCANNED": sweep_gone if scanned is None else scanned,
                            "GIT_TRACE": str(trace), "GIT_CEILING_DIRECTORIES": str(sweep_work),
                            "RECORD_READ": read, **env},
                       capture_output=True, text=True)

    def listed(name):
        found = re.search(rf"^{name}=\[(.*)\]$", r.stdout, re.M)
        return found.group(1).split() if found else None
    record = temp / "sweep-record" / "deleted-tips"
    return types.SimpleNamespace(
        r=r, gone=listed("SCANNED_GONE"), deleted=listed("DELETED_TIPS"), trace=trace.read_text(), calls=gh_log.read_text(),
        record=record.read_text() if record.is_file() and not record.is_symlink() else None, output=output.read_text(),
        temp=temp)


def sweep_detail(s):
    return (f"exit {s.r.returncode}: {s.r.stderr.strip()[-400:]} gone={s.gone} deleted={s.deleted} record={s.record!r} "
            f"output={s.output!r} fetches={commands(s.trace, 'fetch')} gh={s.calls.strip()[-600:]!r}")


def commands(trace, name):
    return len(re.findall(rf"trace: built-in: git {name}\b", trace))


def fetches_of(trace, sha):
    """How many git fetches named sha (the batch, one alone)."""
    return len(re.findall(rf"trace: built-in: git fetch [^\n]*\b{sha}\b", trace))


def lazy_fetches(trace):
    return len(re.findall(r"run_command: git -c fetch\.negotiationAlgorithm=noop fetch", trace))


def has_commit(dest, sha):
    """Whether the clone holds sha, from its own objects only (a lookup of a missing one would fetch it)."""
    return sha in git(dest, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").split()


# Whether this git honours GIT_NO_LAZY_FETCH (git 2.44, or 2.39.4 to 2.43.4 with the May 2024 fixes):
# the probe tries it rather than reading the version, as fork-secret-scan.test.sh does. On CI a git
# that ignores it fails; elsewhere the no-lazy-fetch half of the check is skipped.
probe = sweep_clone("probe")
lazy_honoured = subprocess.run(["git", "cat-file", "-e", f"{sweep_vanished[0]}^{{commit}}"], cwd=probe,
                               env={**base_env, "GIT_NO_LAZY_FETCH": "1"}, capture_output=True).returncode != 0
if not lazy_honoured and os.environ.get("CI") == "true":
    check("secret-scan e2e: git on CI honours GIT_NO_LAZY_FETCH (else the no-lazy-fetch checks prove nothing)", False)

all_heads = " ".join([sweep_main, *sweep_vanished, sweep_gone, "0" * 40, "not-a-sha"])
first_record = RECORD_HEAD + f"{sweep_gone} scanned\n"


def served_in_one_batch(s):
    """The served tips this clone lacked fetched together, once, and none of them alone."""
    return all(fetches_of(s.trace, v) == 1 for v in sweep_vanished) and any(
        all(v in line for v in sweep_vanished) for line in s.trace.splitlines() if "built-in: git fetch" in line)


fresh = sweep_clone("fresh")
s = sweep_lookup(fresh, all_heads, "fresh")
check("secret-scan e2e (#637, #691): the first run (no record), a fresh blob-less clone: GitHub asked once about every "
      "tip the clone lacks, the served ones fetched in one batch, the deleted one fetched once alone, its runs looked "
      "up; it is SCANNED_GONE and DELETED_TIPS, and the record says so; one cat-file for all tips"
      + ("" if lazy_honoured else " (lazy-fetch half skipped: this git ignores GIT_NO_LAZY_FETCH)"),
      s.r.returncode == 0 and s.gone == [sweep_gone] and s.deleted == [sweep_gone]
      and all(has_commit(fresh, v) for v in sweep_vanished) and served_in_one_batch(s)
      and fetches_of(s.trace, sweep_gone) == 1 and s.calls.count("graphql") == 1
      and s.calls.count(f"head_sha={sweep_gone}") == 1 and s.record == first_record and s.output == "record=written\n"
      and commands(s.trace, "cat-file") == 1 and (not lazy_honoured or lazy_fetches(s.trace) == 0), sweep_detail(s))
s = sweep_lookup(sweep_clone("rerun"), all_heads, "rerun", temp=sweep_temp("rerun", first_record))
check("secret-scan e2e (#691): run again on a new runner with the record: the deleted tip neither fetched nor looked "
      "up, the same answer, the served tips still fetched in one batch, the same record",
      s.r.returncode == 0 and s.gone == [sweep_gone] and s.deleted == [sweep_gone] and fetches_of(s.trace, sweep_gone) == 0
      and "actions/runs" not in s.calls and s.calls.count("graphql") == 1 and served_in_one_batch(s)
      and s.record == first_record and s.output == "record=written\n", sweep_detail(s))
first_temp = sweep_lookup(fresh, all_heads, "again-1").temp
s = sweep_lookup(fresh, all_heads, "again", temp=first_temp)
check("secret-scan e2e: run again in the same clone and RUNNER_TEMP: the same answer, nothing fetched but the "
      "default branch's tip, nothing looked up, no lazy fetch",
      s.r.returncode == 0 and s.gone == [sweep_gone] and s.deleted == [sweep_gone] and commands(s.trace, "fetch") == 1
      and lazy_fetches(s.trace) == 0 and s.calls.count("graphql") == 1 and "actions/runs" not in s.calls
      and s.record == first_record, sweep_detail(s))
half = sweep_clone("half")
git(half, "fetch", "-q", "--no-tags", "--filter=blob:none", "origin", sweep_vanished[1])
s = sweep_lookup(half, all_heads, "half")
check("secret-scan e2e: stopped half-way (one served tip fetched before): the same answer, all served tips here",
      s.r.returncode == 0 and s.gone == [sweep_gone] and all(has_commit(half, v) for v in sweep_vanished)
      and s.record == first_record and (not lazy_honoured or lazy_fetches(s.trace) == 0), sweep_detail(s))
stale = sweep_temp("stale", first_record)
(stale / "sweep-record" / "deleted-tips.new").write_text(RECORD_HEAD + f"{sweep_vanished[0]} scanned\n" + "ab" * 7)
s = sweep_lookup(sweep_clone("stale"), all_heads, "stale", temp=stale)
check("secret-scan e2e: stopped half-way while writing the record (its .new left beside it): the record read, the "
      ".new never, and written afresh",
      s.r.returncode == 0 and s.gone == [sweep_gone] and fetches_of(s.trace, sweep_gone) == 0
      and has_commit(s.temp.parent / "stale", sweep_vanished[0]) and s.record == first_record
      and not (stale / "sweep-record" / "deleted-tips.new").exists(), sweep_detail(s))
older = RECORD_HEAD + "ef" * 20 + " scanned\n"  # a tip the activity record no longer lists
cut = sweep_temp("cut", older)
s = sweep_lookup(sweep_clone("cut"), all_heads, "cut", temp=cut, FAKE_RUNS_FAIL="1")
check("secret-scan e2e (#701): stopped half-way (a runs lookup fails): the job fails, but first keeps what is proven "
      "so far (none here: the one deleted tip's lookup failed; the old tip the activity record no longer lists dropped), "
      "written whole and marked for keeping",
      s.r.returncode == 2 and "cannot list the runs of deleted tip" in s.r.stderr and s.record == RECORD_HEAD
      and s.output == "record=written\n" and not (cut / "sweep-record" / "deleted-tips.new").exists(), sweep_detail(s))
s = sweep_lookup(s.temp.parent / "cut", all_heads, "cut-again", temp=cut)
check("secret-scan e2e: and run again: passes, and the record holds this run's deleted tips only (the old one dropped)",
      s.r.returncode == 0 and s.gone == [sweep_gone] and s.record == first_record and s.output == "record=written\n",
      sweep_detail(s))
s = sweep_lookup(sweep_clone("net"), all_heads, "net", FAKE_NET_FAIL=sweep_gone)
check("secret-scan e2e (#691): git's fetch of a tip GitHub has no commit for fails for another reason (a network "
      "error): the job fails, the tip is not taken as deleted, no record",
      s.r.returncode == 2 and "cannot fetch listed tip" in s.r.stderr and s.record is None and s.output == "",
      sweep_detail(s))

# The lookup budget (#701, review B major): the token's Actions lookups run out half-way. The job
# fails, but first keeps what it proved (the deleted tips a scan read so far, and those on the last
# record), so each next sweep goes on from there and the sweep catches up over a few days instead of
# failing at the same tip every day. Three deleted tips a scan read, two lookups each, a budget of 3.
# Each case below is a function of the lookup's code, so that the guard it is about can be removed
# (the mutants after them) and the case seen to go red.
budget_tips = ["a1" * 20, "a2" * 20, "a3" * 20]
budget_heads = " ".join([sweep_main, *budget_tips])
budget_env = {"gone": " ".join(budget_tips), "scanned": " ".join(budget_tips)}
e2e_runs = iter(range(10 ** 6))


def budget_case(code):
    n = next(e2e_runs)
    clone, temp = sweep_clone(f"budget{n}"), sweep_temp(f"budget{n}")
    runs = [sweep_lookup(clone, budget_heads, f"budget{n}-{k}", code, temp, FAKE_BUDGET="3", **budget_env)
            for k in range(3)]
    return ([b.r.returncode for b in runs] == [2, 2, 0]
            and [b.record for b in runs] == [RECORD_HEAD + "".join(f"{x} scanned\n" for x in budget_tips[:k])
                                            for k in (1, 2, 3)]
            and all(b.output == "record=written\n" for b in runs)
            and "cannot list the jobs of run 101 of deleted tip " + budget_tips[1] in runs[0].r.stderr
            and runs[2].gone == budget_tips and f"head_sha={budget_tips[0]}" not in runs[1].calls), runs


def unreached_case(code):
    n = next(e2e_runs)
    s = sweep_lookup(sweep_clone(f"unreached{n}"), budget_heads, f"unreached{n}", code,
                     sweep_temp(f"unreached{n}", RECORD_HEAD + f"{budget_tips[2]} scanned\n"), FAKE_RUNS_FAIL="1",
                     **budget_env)
    return (s.r.returncode == 2 and s.record == RECORD_HEAD + f"{budget_tips[2]} scanned\n"
            and s.output == "record=written\n"), [s]


# The record's write (review B minor 3): whatever an earlier run (or anything else) left at the
# record's .new path, it is removed first and never written through; the record is written whole to
# .new and renamed, and marked written only after the rename.
def link_case(code):
    n = next(e2e_runs)
    temp = sweep_temp(f"link{n}", first_record)
    (temp / "outside").write_text("outside\n")
    (temp / "sweep-record" / "deleted-tips.new").symlink_to(temp / "outside")
    s = sweep_lookup(sweep_clone(f"link{n}"), all_heads, f"link{n}", code, temp)
    return (s.r.returncode == 0 and (temp / "outside").read_text() == "outside\n" and s.record == first_record
            and not (temp / "sweep-record" / "deleted-tips").is_symlink()
            and not os.path.lexists(temp / "sweep-record" / "deleted-tips.new")), [s]


def newdir_case(code):
    n = next(e2e_runs)
    temp = sweep_temp(f"newdir{n}", first_record)
    (temp / "sweep-record" / "deleted-tips.new" / "x").mkdir(parents=True)
    s = sweep_lookup(sweep_clone(f"newdir{n}"), all_heads, f"newdir{n}", code, temp)
    return (s.r.returncode == 0 and s.record == first_record
            and not os.path.lexists(temp / "sweep-record" / "deleted-tips.new")), [s]


kill_stub = sweep_work / "kill-bin"
kill_stub.mkdir()
(kill_stub / "mv").write_text("#!/usr/bin/env bash\nkill -9 \"$PPID\"\n")
(kill_stub / "mv").chmod(0o755)


def kill_case(code):
    n = next(e2e_runs)
    temp = sweep_temp(f"kill{n}", older)
    s = sweep_lookup(sweep_clone(f"kill{n}"), all_heads, f"kill{n}", code, temp,
                     PATH=f"{kill_stub}:{sweep_stub}:{base_env['PATH']}")
    return (s.r.returncode != 0 and s.record == older and s.output == ""
            and (temp / "sweep-record" / "deleted-tips.new").read_text() == first_record), [s]


def nul_case(code):
    n = next(e2e_runs)
    s = sweep_lookup(sweep_clone(f"nul{n}"), all_heads, f"nul{n}", code,
                     sweep_temp(f"nul{n}", RECORD_HEAD + f"{sweep_gone} scanned\0x\n"), scanned="")
    return (s.r.returncode == 0 and "record of deleted tips cannot be read" in s.r.stderr and s.gone == []
            and fetches_of(s.trace, sweep_gone) == 1 and s.record == RECORD_HEAD), [s]


# #703: a partial record replaces the last one only when the read step read it (or reset it to
# empty), or no earlier sweep kept one; never when the read failed (the last record may be good and
# bigger, and one failed call must not shrink it), nor when the scan step could not read it.
def failed_read_case(code):
    n = next(e2e_runs)
    s = sweep_lookup(sweep_clone(f"failedread{n}"), budget_heads, f"failedread{n}", code, sweep_temp(f"failedread{n}"),
                     FAKE_BUDGET="3", RECORD_READ="failed", **budget_env)
    return (s.r.returncode == 2 and "cannot list the jobs of run 101" in s.r.stderr and s.record is None
            and s.output == ""), [s]


# The same for the two other gates (#720 review B): a failed read, then the runs lookup fails, or the
# time limit is reached (the clock set ahead).
def failed_read_runs_case(code):
    n = next(e2e_runs)
    s = sweep_lookup(sweep_clone(f"failedruns{n}"), budget_heads, f"failedruns{n}", code, sweep_temp(f"failedruns{n}"),
                     FAKE_RUNS_FAIL="1", RECORD_READ="failed", **budget_env)
    return (s.r.returncode == 2 and "cannot list the runs of deleted tip" in s.r.stderr and s.record is None
            and s.output == ""), [s]


def failed_read_time_case(code):
    n = next(e2e_runs)
    s = sweep_lookup(sweep_clone(f"failedtime{n}"), budget_heads, f"failedtime{n}", "SECONDS=100000\n" + code,
                     sweep_temp(f"failedtime{n}"), RECORD_READ="failed", **budget_env)
    return (s.r.returncode == 2 and "passed 20 minutes" in s.r.stderr and "actions/runs" not in s.calls
            and s.record is None and s.output == ""), [s]


def rejected_case(code):
    n = next(e2e_runs)
    s = sweep_lookup(sweep_clone(f"rejected{n}"), budget_heads, f"rejected{n}", code,
                     sweep_temp(f"rejected{n}", "not a record\n"), FAKE_BUDGET="3", **budget_env)
    return (s.r.returncode == 2 and "record of deleted tips cannot be read" in s.r.stderr and s.record is None
            and s.output == ""), [s]


def reset_case(code):
    n = next(e2e_runs)
    s = sweep_lookup(sweep_clone(f"reset{n}"), budget_heads, f"reset{n}", code, sweep_temp(f"reset{n}", RECORD_HEAD),
                     FAKE_BUDGET="3", RECORD_READ="reset", **budget_env)
    return (s.r.returncode == 2 and s.record == RECORD_HEAD + f"{budget_tips[0]} scanned\n"
            and s.output == "record=written\n"), [s]


# #703: a recorded tip is kept by a partial record wherever it comes in the deleted list: one GitHub
# showed at first and deleted meanwhile (asked again) comes last, after a new deleted tip whose lookup fails.
def unserved_case(code):
    n = next(e2e_runs)
    late_tip = "ef" * 20
    s = sweep_lookup(sweep_clone(f"unserved{n}"), " ".join([*sweep_vanished, late_tip, sweep_gone]), f"unserved{n}", code,
                     sweep_temp(f"unserved{n}", RECORD_HEAD + f"{late_tip} scanned\n"), FAKE_LATE=late_tip,
                     gone=sweep_gone, FAKE_RUNS_FAIL="1")
    return (s.r.returncode == 2 and "cannot list the runs of deleted tip " + sweep_gone in s.r.stderr
            and s.record == RECORD_HEAD + f"{late_tip} scanned\n" and s.output == "record=written\n"), [s]


# #703: a time limit inside the lookups, well before the job's: past 20 minutes of the step, the
# lookups stop and the record keeps what they proved. The clock is set ahead (SECONDS) for the first
# case; the second stops after the first tip's lookups (the limit's test swapped for "a tip's runs
# were listed already").
def time_limit_case(code):
    n = next(e2e_runs)
    s = sweep_lookup(sweep_clone(f"late{n}"), budget_heads, f"late{n}", "SECONDS=100000\n" + code,
                     sweep_temp(f"late{n}", RECORD_HEAD + f"{budget_tips[2]} scanned\n"), **budget_env)
    return (s.r.returncode == 2 and "passed 20 minutes" in s.r.stderr and "actions/runs" not in s.calls
            and RERUN_ADVICE in s.r.stderr
            and s.record == RECORD_HEAD + f"{budget_tips[2]} scanned\n" and s.output == "record=written\n"), [s]


def time_limit_midway_case(code):
    n = next(e2e_runs)
    code = code.replace("((SECONDS > 1200))", '(($(grep -c head_sha= "$GH_LOG") >= 1))')
    s = sweep_lookup(sweep_clone(f"midway{n}"), budget_heads, f"midway{n}", code, sweep_temp(f"midway{n}"),
                     **budget_env)
    return (s.r.returncode == 2 and "passed 20 minutes" in s.r.stderr and s.calls.count("actions/runs") == 2
            and s.record == RECORD_HEAD + f"{budget_tips[0]} scanned\n" and s.output == "record=written\n"), [s]


# #950: what each half-way stop (the time limit, a failed runs lookup, a failed jobs lookup) tells to do,
# in its own error line: a run that is not a reset run may be re-run ("Re-run failed jobs") or followed by a
# new run; a reset run only by a new run, and no "Re-run" is named at all. Each keeps its partial record.
STOPS = {"time": ("SECONDS=100000\n", {}, "passed 20 minutes"),
         "runs": ("", {"FAKE_RUNS_FAIL": "1"}, "cannot list the runs of deleted tip"),
         "jobs": ("", {"FAKE_BUDGET": "3"}, "cannot list the jobs of run")}


def stop_advice_case(reset):
    def case(code):
        ok, runs = True, []
        for kind, (prefix, env, says) in STOPS.items():
            label = f"advice{'reset' if reset else ''}{kind}{next(e2e_runs)}"
            more = {"RESET": "true", "RECORD_READ": "reset"} if reset else {"RESET": "false"}
            s = sweep_lookup(sweep_clone(label), budget_heads, label, prefix + code, sweep_temp(label, RECORD_HEAD),
                             **more, **env, **budget_env)
            line = next((x for x in s.r.stderr.splitlines() if says in x), "")
            ok = (ok and s.r.returncode == 2 and (RESET_ADVICE if reset else RERUN_ADVICE) in line
                  and (not reset or "Re-run" not in s.r.stderr) and s.output == "record=written\n")
            runs.append(s)
        return ok, runs
    return case


E2E_CASES = {
    "budget": ("(#701): the lookup budget runs out half-way: each run fails, but keeps the tips proven so far (the "
               "last record's too), so the third run passes with all three recorded", budget_case),
    "unreached": ("(#701): a runs lookup fails at the first tip: the last record's tips, though not reached, are kept "
                  "(proven on an earlier day)", unreached_case),
    "link": ("(#701): a .new left as a link to a file outside: removed, the file it named untouched, the record "
             "written whole (a plain file)", link_case),
    "newdir": ("(#701): a .new left as a directory: removed, and the record written", newdir_case),
    "kill": ("(#701): stopped right before the rename: the last record whole as it was, nothing marked for keeping "
             "(a half-written record is never kept)", kill_case),
    "nul": ("(#701): a record with a NUL byte (<id> scanned\\0x; mapfile ends the line at a NUL and a captured one is "
            "dropped, so it would read as scanned): a "
            "warning, the tip fetched and looked up as on a first run, not trusted", nul_case),
    "failedread": ("(#703): the read step failed (RECORD_READ=failed) and a lookup fails: no partial record, nothing "
                   "marked for keeping, so the last record (perhaps bigger) stays the one in use", failed_read_case),
    "failedruns": ("(#703): the read step failed and a runs lookup fails: no partial record", failed_read_runs_case),
    "failedtime": ("(#703): the read step failed and the time limit is reached: no partial record",
                   failed_read_time_case),
    "rejected": ("(#703): the scan step could not read the record the read step left, and a lookup fails: no partial "
                 "record", rejected_case),
    "reset": ("(#703): reset_record (an empty record read) and a lookup fails: the tips proven so far kept, the old "
              "record replaced", reset_case),
    "unserved": ("(#703): a recorded tip GitHub showed at first and deleted meanwhile comes last in the list, after a "
                 "new deleted tip whose lookup fails: still kept by the partial record", unserved_case),
    "timelimit": ("(#703): the step is past 20 minutes at the first lookup: the lookups stop, the job fails, the "
                  "recorded tip kept", time_limit_case),
    "midway": ("(#703): the time limit reached after the first tip's lookups: it is kept, the rest not looked up",
               time_limit_midway_case),
    "rerunstop": ("(#950): a run that is not a reset run stops half-way (time limit, runs lookup, jobs lookup): each "
                  "error tells to start a new run or use \"Re-run failed jobs\", and the proven tips are kept",
                  stop_advice_case(False)),
    "resetstop": ("(#950): a reset run stops half-way (time limit, runs lookup, jobs lookup): each error tells to start "
                  "a new run, never a re-run (no \"Re-run\" named), and the proven tips are kept", stop_advice_case(True)),
}
for name, (what, case) in E2E_CASES.items():
    ok, runs = case(lookup_code)
    check(f"secret-scan e2e {what}", ok, " | ".join(sweep_detail(r) for r in runs))
# Each guard of the record's write and read, removed: the case about it goes red.
for what, old, new, cases in (
    ("the record's partial write before a failed runs lookup",
     'whether a scan read it is unknown. $again" >&2\n[[ -z "$partial" ]] || keep\nexit 2\n',
     'whether a scan read it is unknown. $again" >&2\nexit 2\n', ("budget", "unreached")),
    ("the partial record's gate (#703)", '[[ -z "$partial" ]] || keep\nexit 2\n', "keep\nexit 2\n",
     ("failedread", "rejected")),
    ("the partial record's gate at the jobs lookup alone (#703)",
     'whether a scan read it is unknown. $again" >&2\n[[ -z "$partial" ]] || keep\nexit 2\nfi\nif [[ -n "$scanned" ]]',
     'whether a scan read it is unknown. $again" >&2\nkeep\nexit 2\nfi\nif [[ -n "$scanned" ]]', ("failedread",)),
    ("the partial record's gate at the runs lookup alone (#703)",
     'cannot list the runs of deleted tip $tip, so whether a scan read it is unknown. $again" >&2\n'
     '[[ -z "$partial" ]] || keep\n',
     'cannot list the runs of deleted tip $tip, so whether a scan read it is unknown. $again" >&2\nkeep\n',
     ("failedruns",)),
    ("the partial record's gate at the time limit alone (#703)",
     'before the job\'s time limit. $again" >&2\n[[ -z "$partial" ]] || keep\n',
     'before the job\'s time limit. $again" >&2\nkeep\n', ("failedtime",)),
    ("a partial record when no earlier sweep kept one (#703)", "none) partial=1 ;;", "none) ;;", ("budget",)),
    ("a reset run's own advice (#950)", '[[ "${RESET:-}" != true ]] || again=', ": || again=", ("resetstop",)),
    ("a reset run's advice for every run (#950)", '[[ "${RESET:-}" != true ]] || again=', "again=", ("rerunstop",)),
    ("a partial record after a read or reset record (#703)", 'found | reset) [[ ! -f "$record" ]] || partial=1 ;;',
     "found | reset) ;;", ("reset", "unreached", "timelimit")),
    ("the recorded tips marked before any lookup (#703)",
     'SCANNED_GONE+="$tip "\nfi\ndone\nfor tip in "${deleted[@]}"; do\n'
     '[[ -z "${read_by_scan[$tip]:-}" && -z "${reviewed[$tip]:-}" ]] || continue\n',
     'SCANNED_GONE+="$tip "\ncontinue\nfi\n[[ -z "${reviewed[$tip]:-}" ]] || continue\n', ("unserved",)),
    ("the time limit (#703)", "if ((SECONDS > 1200)); then", "if false; then", ("timelimit", "midway")),
    ("the partial record at the time limit (#703)",
     'before the job\'s time limit. $again" >&2\n[[ -z "$partial" ]] || keep\n',
     'before the job\'s time limit. $again" >&2\n', ("timelimit", "midway")),
    ("the removal of a left .new", 'rm -rf -- "$record.new"\n', "", ("link", "newdir")),
    ("write-then-rename (the record written in place)", '} > "$record.new"\nmv -f -- "$record.new" "$record"\n',
     '} > "$record"\n', ("kill",)),
    ("marked written only after the rename",
     'mv -f -- "$record.new" "$record"\necho "record=written" >> "${GITHUB_OUTPUT:?}"\n',
     'echo "record=written" >> "${GITHUB_OUTPUT:?}"\nmv -f -- "$record.new" "$record"\n', ("kill",)),
    ("the NUL check (bytes counted, not captured)", """[[ "$(LC_ALL=C tr -d 'a-z0-9 \\n' < "$record" | wc -c)" -eq 0 ]]""",
     """[[ -z "$(LC_ALL=C tr -d 'a-z0-9 \\n' < "$record")" ]]""", ("nul",)),
):
    flat = stripped_lines(lookup_code)  # the same code without its indentation (no heredoc in it)
    mutated = flat.replace(old, new)
    check(f"secret-scan e2e (#701): without {what}, the case about it goes red",
          mutated != flat and E2E_CASES[cases[0]][1](flat)[0] and not any(E2E_CASES[c][1](mutated)[0] for c in cases),
          "unchanged" if mutated == flat else "still green")

# A record that cannot be read, however it came about: a warning, every tip checked as on a first
# run, and the record written afresh. Never "skip all".
corrupt = sweep_clone("corrupt")
git(corrupt, "fetch", "-q", "--no-tags", "--filter=blob:none", "origin", *sweep_vanished)
huge_record = RECORD_HEAD + "".join(f"{i} scanned\n" for i in (hashlib.sha1(str(n).encode()).hexdigest()
                                                                for n in range(4000000 // 48 + 1000)))
for n, (what, record) in enumerate({
    "garbage": "not a record\n",
    "an empty file": "",
    "no header": f"{sweep_gone} scanned\n",
    "another version's header (the cache's, with unscanned lines)": f"fork secret sweep record 1\n{sweep_gone} scanned\n",
    "a later version's header": f"fork secret sweep record 3\n{sweep_gone} scanned\n",
    "a NUL byte (bash would drop it: scan\\0ned read as scanned)": RECORD_HEAD + f"{sweep_gone} scan\0ned\n",
    "a NUL byte in the header": "fork secret sweep record 2\0\n" + f"{sweep_gone} scanned\n",
    "CRLF line ends": f"fork secret sweep record 2\r\n{sweep_gone} scanned\r\n",
    "a line cut short": RECORD_HEAD + sweep_gone[:20],
    "a duplicate line": RECORD_HEAD + f"{sweep_gone} scanned\n" * 2,
    "an id in capitals": RECORD_HEAD + f"{sweep_gone.upper()} scanned\n",
    "an unknown state": RECORD_HEAD + f"{sweep_gone} maybe\n",
    "the cache's unscanned state": RECORD_HEAD + f"{sweep_gone} unscanned\n",
    "a trailing space": RECORD_HEAD + f"{sweep_gone} scanned \n",
    "a short id": RECORD_HEAD + f"{sweep_gone[:39]} scanned\n",
    "over the size limit": huge_record + f"{sweep_gone} scanned\n",
    "a directory": None,
    "a symlink to a good record": None,
}.items()):
    temp = sweep_temp(f"corrupt{n}", record)
    if what == "a directory":
        (temp / "sweep-record" / "deleted-tips").mkdir(parents=True)
    if what == "a symlink to a good record":
        (temp / "good").write_text(first_record)
        (temp / "sweep-record").mkdir()
        (temp / "sweep-record" / "deleted-tips").symlink_to(temp / "good")
    s = sweep_lookup(corrupt, all_heads, f"corrupt{n}", temp=temp)
    check(f"secret-scan e2e (#691): a record with {what}: a warning, every tip checked as on a first run, the record "
          "written afresh",
          s.r.returncode == 0 and "record of deleted tips cannot be read" in s.r.stderr and s.gone == [sweep_gone]
          and fetches_of(s.trace, sweep_gone) == 1 and s.calls.count(f"head_sha={sweep_gone}") == 1
          and s.record == first_record and s.output == "record=written\n", sweep_detail(s))
s = sweep_lookup(corrupt, all_heads, "good", temp=sweep_temp("good", first_record))
check("secret-scan e2e: a good record: no warning", s.r.returncode == 0 and "cannot be read" not in s.r.stderr
      and fetches_of(s.trace, sweep_gone) == 0, sweep_detail(s))

# A tip that comes back: the record says deleted, but GitHub shows it again (or a ref holds it).
back = RECORD_HEAD + "".join(f"{t} scanned\n" for t in (sweep_vanished[0], sweep_main, sweep_gone))
back_clone = sweep_clone("back")
s = sweep_lookup(back_clone, all_heads, "back", temp=sweep_temp("back", back))
check("secret-scan e2e (#691): a recorded tip GitHub shows again: fetched with the batch (so scanned), not SCANNED_GONE "
      "nor DELETED_TIPS, and dropped from the record; one a ref holds again is dropped too",
      s.r.returncode == 0 and s.gone == [sweep_gone] and s.deleted == [sweep_gone] and has_commit(back_clone, sweep_vanished[0])
      and served_in_one_batch(s) and s.record == first_record, sweep_detail(s))
unscanned_clone = sweep_clone("unscanned")
s = sweep_lookup(unscanned_clone, all_heads, "unscanned-1", scanned="")
s = sweep_lookup(unscanned_clone, all_heads, "unscanned", temp=s.temp, scanned="")
check("secret-scan e2e (#701): a deleted tip no scan is known to have read: fetched again every run (so one git serves "
      "again is scanned), its runs looked up again every run (a scan of it may yet be found), and left off the record",
      s.r.returncode == 0 and s.gone == [] and s.deleted == [sweep_gone] and fetches_of(s.trace, sweep_gone) == 1
      and s.calls.count(f"head_sha={sweep_gone}") == 1 and s.record == RECORD_HEAD, sweep_detail(s))
s = sweep_lookup(unscanned_clone, all_heads, "found", temp=s.temp)
check("secret-scan e2e: and once a scan of it is found: SCANNED_GONE, and recorded as read",
      s.r.returncode == 0 and s.gone == [sweep_gone] and s.record == first_record, sweep_detail(s))
s = sweep_lookup(sweep_clone("reviewed"), all_heads + " " + sweep_gone2, "reviewed", branch="reviewed")
check("secret-scan e2e (#691): a deleted tip on the default branch's reviewed list: fetched once, its runs not looked "
      "up, DELETED_TIPS but not SCANNED_GONE, and not on the record",
      s.r.returncode == 0 and s.gone == [sweep_gone] and sorted(s.deleted) == sorted([sweep_gone, sweep_gone2])
      and fetches_of(s.trace, sweep_gone2) == 1 and f"head_sha={sweep_gone2}" not in s.calls
      and s.record == first_record, sweep_detail(s))
s = sweep_lookup(sweep_clone("reviewed-again"), all_heads + " " + sweep_gone2, "reviewed-again", branch="reviewed",
                 temp=s.temp)
check("secret-scan e2e (#701, review A minor 1): and run again with that record: the reviewed tip is fetched again "
      "(git may serve it again; then it is scanned), the recorded one is not",
      s.r.returncode == 0 and fetches_of(s.trace, sweep_gone2) == 1 and fetches_of(s.trace, sweep_gone) == 0
      and s.record == first_record, sweep_detail(s))
served_again = sweep_clone("served-again")
s = sweep_lookup(served_again, all_heads, "served-again", temp=sweep_temp("served-again", first_record),
                 gone=f"{sweep_gone} {sweep_vanished[0]}", scanned="")
check("secret-scan e2e (#701, review A minor 1): a tip GitHub's API has no commit for, off the record, that git serves "
      "again: fetched (so scanned), not deleted, not recorded",
      s.r.returncode == 0 and s.deleted == [sweep_gone] and has_commit(served_again, sweep_vanished[0])
      and fetches_of(s.trace, sweep_vanished[0]) == 1 and s.record == first_record, sweep_detail(s))
late, kept = "ef" * 20, "12" * 20  # origin has neither; GitHub shows both at first, then only kept
late_clone = sweep_clone("late")
s = sweep_lookup(late_clone, " ".join([*sweep_vanished, late, kept]), "late", FAKE_LATE=late, scanned=late)
check("secret-scan e2e (#691): GitHub shows two tips git cannot fetch: the batch fails, so one by one (the served "
      "ones still fetched); GitHub asked again: the one it deleted meanwhile is deleted (no third fetch), the one "
      "it still shows stays listed and out of the record (the scan script fails on it)",
      s.r.returncode == 0 and s.gone == [late] and s.deleted == [late] and fetches_of(s.trace, late) == 2
      and fetches_of(s.trace, kept) == 2 and s.calls.count("graphql") == 2
      and all(has_commit(late_clone, v) for v in sweep_vanished) and s.record == RECORD_HEAD + f"{late} scanned\n",
      sweep_detail(s))
served_null = sweep_clone("served-null")
s = sweep_lookup(served_null, all_heads, "served-null", gone=f"{sweep_gone} {sweep_vanished[0]}")
check("secret-scan e2e (#691): GitHub's API has no commit for a tip git still serves: git's one fetch gets it (so "
      "it is scanned), and it is not deleted",
      s.r.returncode == 0 and s.deleted == [sweep_gone] and has_commit(served_null, sweep_vanished[0])
      and s.record == first_record, sweep_detail(s))
served = sweep_clone("served")
s = sweep_lookup(served, " ".join(sweep_vanished), "served")
check("secret-scan e2e: no tip deleted: GitHub asked once, one batch fetch, nothing one by one or looked up, an "
      "empty record",
      s.r.returncode == 0 and s.gone == [] and s.deleted == [] and served_in_one_batch(s) and s.calls.count("graphql") == 1
      and "actions/runs" not in s.calls and all(has_commit(served, v) for v in sweep_vanished) and s.record == RECORD_HEAD,
      sweep_detail(s))
s = sweep_lookup(sweep_clone("empty"), "", "empty", temp=sweep_temp("empty", first_record))
check("secret-scan e2e: no tips listed: passes with nothing fetched or asked, and an empty record",
      s.r.returncode == 0 and s.gone == [] and commands(s.trace, "fetch") == 0 and s.calls == "" and s.record == RECORD_HEAD,
      sweep_detail(s))
not_a_clone = sweep_work / "not-a-clone"
not_a_clone.mkdir()
s = sweep_lookup(not_a_clone, all_heads, "no-clone", temp=sweep_temp("no-clone", first_record))
check("secret-scan e2e: no clone (cat-file fails): the job fails before asking GitHub anything; the record untouched",
      s.r.returncode == 2 and s.gone is None and s.calls == "" and "cannot check which listed tips" in s.r.stderr
      and s.record == first_record and s.output == "", sweep_detail(s))

# #701, end to end: the record's read step, as the workflow has it, with a gh stub that serves canned
# answers through the real jq (as gh's --jq does) and archives built here. It takes the record only
# from the newest run of fork-secret-sweep.yml (that path; schedule or workflow_dispatch; this branch;
# this repository, not a fork; completed, success or failure, or a reset run in any state, #992; not
# this run; whose commit this run's has) and its one live artifact of that name; never unpacks the
# archive (exactly one plain file named deleted-tips, read in memory), checks every line, and on any
# doubt warns and leaves no record (a first run); a reset run whose record it does not read leaves the
# empty one (#992); a re-run of a run that started before a listed reset run reads as a new run does
# (#992). The stub applies the API's status filter, as GitHub does. Run again, stopped half-way (a zip or
# record left in RUNNER_TEMP), a link where the record's folder goes, reset_record, a tag: the same.
read_work = pathlib.Path(tempfile.mkdtemp(prefix="fork-contracts-read-"))
read_stub = read_work / "bin"
read_stub.mkdir()
(read_stub / "gh").write_text(r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
if [[ -n "${FAKE_FAIL:-}" && "$*" == *"$FAKE_FAIL"* ]]; then echo "gh stub: HTTP 502" >&2; exit 1; fi
filter="" path="" status=""
args=("$@")
for ((i = 1; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    --jq) filter="${args[i + 1]}"; i=$((i + 1)) ;;
    -X) i=$((i + 1)) ;;
    -f) [[ "${args[i + 1]}" != status=* ]] || status="${args[i + 1]#status=}"; i=$((i + 1)) ;;
    *) [[ -n "$path" ]] || path="${args[i]}" ;;
  esac
done
case "$path" in
  repos/me/t3code/actions/workflows/fork-secret-sweep.yml/runs)
    file="$FAKE_DIR/runs.json"
    # The API's own filter (#992): -f status=S lists only the runs in that state.
    if [[ -n "$status" ]]; then
      jq --arg s "$status" '.workflow_runs |= map(select(.status == $s))' < "$file" | jq -r "${filter:-.}"
      exit
    fi ;;
  repos/me/t3code/actions/runs/*/artifacts) id="${path#*/actions/runs/}"; file="$FAKE_DIR/artifacts-${id%/artifacts}.json" ;;
  repos/me/t3code/compare/*) base="${path#*/compare/}"; file="$FAKE_DIR/compare-${base%%...*}.json" ;;
  repos/me/t3code/actions/artifacts/*/zip) id="${path#*/actions/artifacts/}"; cat "$FAKE_DIR/zip-${id%/zip}.zip"; exit ;;
  repos/me/t3code/git/matching-refs/tags/*) file="$FAKE_DIR/tags.json" ;;
  *) echo "gh stub: unexpected call: $*" >&2; exit 97 ;;
esac
[[ -e "$file" ]] || file="$FAKE_DIR/default-${path##*/}.json"
if [[ -n "$filter" ]]; then jq -r "$filter" < "$file"; else cat "$file"; fi
""")
(read_stub / "gh").chmod(0o755)
SWEEP_PATH = ".github/workflows/fork-secret-sweep.yml"
read_sha = {n: hashlib.sha1(f"commit {n}".encode()).hexdigest() for n in range(800, 810)}
good_record = RECORD_HEAD + f"{sweep_gone} scanned\n"
other_record = RECORD_HEAD + f"{sweep_gone2} scanned\n"  # what a run the step must not read kept


def a_run(n, **kw):
    return {"id": n, "head_sha": read_sha[n], "path": SWEEP_PATH, "event": "schedule", "head_branch": "main",
            "repository": {"full_name": "me/t3code"}, "head_repository": {"full_name": "me/t3code"},
            "status": "completed", "conclusion": "success", **kw}


def an_artifact(n, run, **kw):
    return {"id": n, "name": "fork-secret-sweep-record", "expired": False, "size_in_bytes": 200,
            "workflow_run": {"id": run}, **kw}


def a_zip(members):
    """An archive of (ZipInfo or name, bytes) members."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in members:
            z.writestr(info, data)
    return buf.getvalue()


def zip_info(name, mode=0o100644):
    info = zipfile.ZipInfo(name)
    info.external_attr = mode << 16
    return info


def encrypted(data):
    """The archive with its members marked encrypted (Python's writer clears the flag as it writes)."""
    data = bytearray(data)
    for sig, at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = data.find(sig)
        while i >= 0:
            data[i + at] |= 1
            i = data.find(sig, i + 4)
    return bytes(data)


good_zip = a_zip([("deleted-tips", good_record)])
other_zip = a_zip([("deleted-tips", other_record)])
read_runs = iter(range(10 ** 6))


def read_record(code, runs=None, artifacts=None, zips=None, compare=None, temp=None, before=None, tags=None, **env):
    """Runs the read step in a new RUNNER_TEMP (or temp) as this run's first attempt; by default the
    newest sweep run (801) kept the good record, and no tag is named like a branch (tags: the refs
    the matching-refs call returns). Returns its exit, stderr, the record it leaves (None: none), what
    else is left in RUNNER_TEMP, the gh calls and its step output."""
    n = next(read_runs)
    fake = read_work / f"fake{n}"
    fake.mkdir()
    temp = temp or read_work / f"temp{n}"
    temp.mkdir(exist_ok=True)
    if before:
        before(temp)
    (fake / "runs.json").write_text(json.dumps({"workflow_runs": [a_run(801)] if runs is None else runs}))
    (fake / "default-artifacts.json").write_text(json.dumps({"artifacts": []}))
    for run, arts in ({801: [an_artifact(901, 801)]} if artifacts is None else artifacts).items():
        (fake / f"artifacts-{run}.json").write_text(json.dumps({"artifacts": arts}))
    for art, data in ({901: good_zip} if zips is None else zips).items():
        (fake / f"zip-{art}.zip").write_bytes(data)
    for sha in [*read_sha.values(), "f" * 40]:
        (fake / f"compare-{sha}.json").write_text(json.dumps({"status": (compare or {}).get(sha, "ahead")}))
    (fake / "tags.json").write_text(json.dumps([{"ref": ref} for ref in tags or []]))
    gh_log, output = fake / "gh.log", fake / "output"
    gh_log.write_text("")
    output.write_text("")
    r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", code], cwd=read_work,
                       env={**base_env, "PATH": f"{read_stub}:{base_env['PATH']}", "GITHUB_REPOSITORY": "me/t3code",
                            "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "900", "GITHUB_SHA": "f" * 40,
                            "GITHUB_RUN_ATTEMPT": "1", "RUNNER_TEMP": str(temp), "RESET": "false", "GH_TOKEN": "x",
                            "GITHUB_WORKFLOW_REF": f"me/t3code/{SWEEP_PATH}@refs/heads/main",
                            "FAKE_DIR": str(fake), "GH_LOG": str(gh_log), "GITHUB_OUTPUT": str(output), **env},
                       capture_output=True, text=True)
    record = temp / "sweep-record" / "deleted-tips"
    return types.SimpleNamespace(
        r=r, out=r.stdout + r.stderr, record=record.read_text() if record.is_file() and not record.is_symlink() else None, temp=temp,
        left=sorted(str(p.relative_to(temp)) for p in temp.rglob("*") if p != record and p != record.parent),
        calls=gh_log.read_text(), output=output.read_text())


def read_detail(s):
    return (f"exit {s.r.returncode}: {s.out.strip()[-500:]!r} record={s.record!r} left={s.left} "
            f"output={s.output!r} gh={s.calls.strip()[-500:]!r}")


def read_as(want, frm=801, state="failed"):
    """The case's check: the record from run frm (want: its text; output read=found), or none, a
    warning, and the output read=state ("failed": one may exist but cannot be read or trusted, so no
    partial record may replace it; "none": no earlier sweep kept one, #703). A found read warns of nothing
    but a re-run's missing own record (#950)."""
    def ok(s):
        if want is None:
            return (s.r.returncode == 0 and s.record is None and "::warning title=Secret scan::" in s.out
                    and "checked as on a first run" in s.out and "sweep-record.zip" not in s.left
                    and s.output == f"read={state}\n")
        return (s.r.returncode == 0 and s.record == want and f"the sweep's record from run {frm}:" in s.out
                and "::warning" not in s.out.replace(NO_OWN_RECORD, "") and s.left == [] and s.output == "read=found\n")
    return ok


def reset_empty(s):
    """A reset run's empty record (the header only), read=reset, no older run listed, nothing left."""
    return (s.r.returncode == 0 and s.record == RECORD_HEAD and s.output == "read=reset\n" and s.left == []
            and "actions/workflows" not in s.calls and "an empty record is kept even if this run fails" in s.out)


def reset_by(n, why):
    """A reset run (n) whose record is not read, for WHY (#950, #992): no older record read (801's is
    from before the reset), the empty one kept even if this run fails, as the reset run's own would be."""
    return lambda s: (
        s.r.returncode == 0 and s.record == RECORD_HEAD and s.output == "read=reset\n" and s.left == []
        and "runs/801/" not in s.calls and "artifacts/901/zip" not in s.calls
        and f"sweep run {n}, a reset run (input reset_record), {why}, so no record from before it is read" in s.out
        and "an empty record is kept even if this run fails" in s.out)


# What a re-run whose own run keeps no record warns (#950): the cause ("Re-run all jobs" deletes an
# earlier attempt's artifacts, seen live) and the re-run that keeps them.
NO_OWN_RECORD = ("::warning title=Secret scan::this re-run finds no record of its earlier attempt: that attempt kept none, "
                 "its record expired, or this is a \"Re-run all jobs\", which deletes an earlier attempt's artifacts "
                 "(#950; \"Re-run failed jobs\" is expected to keep them, as GitHub's docs say, not yet seen live), so "
                 "it reads as a new run does")


def rerun_log(matched=True, kept=None, listed=None):
    """A re-run's log (#950): whether its workflow ref was the sweep's, and, of its own run, how many
    artifacts of the record's name the API listed (listed; by default kept) and how many of them are
    its unexpired record (kept; None: its own run never asked about). Kept 0 also warns why (#950).
    A first attempt logs none of it."""
    def ok(s):
        ref = ("the sweep's, so its own earlier record is looked for first" if matched
               else "not the sweep's, so its own earlier record is not looked for")
        own = (f"::notice title=Secret scan::this run's own records: the API listed {kept if listed is None else listed} "
               f"artifact(s) of that name, {kept} of them this run's unexpired record\n")
        return (f"::notice title=Secret scan::attempt 2 of run 900, workflow ref " in s.out and ref in s.out
                and (own in s.out if kept is not None else "this run's own records" not in s.out)
                and ((NO_OWN_RECORD in s.out) == (kept == 0)))
    return ok


# What a re-run of a sweep run that started before a reset run warns (#992): it reads as a new run does.
PRE_RESET_RERUN = ("::warning title=Secret scan::this re-run's run started before sweep run {}, a reset run (input "
                   "reset_record), so its own earlier attempt's record, from before that reset, is not read: it reads "
                   "as a new run does")


def pre_reset_rerun(s, n):
    """The run S without the warning PRE_RESET_RERUN for reset run n, which it must carry (else None's
    record, so a found read's check fails)."""
    line = PRE_RESET_RERUN.format(n)
    if line not in s.out:
        return types.SimpleNamespace(**{**vars(s), "record": None})
    return types.SimpleNamespace(**{**vars(s), "out": s.out.replace(line, "")})


def first_attempt_log(s):
    return "workflow ref" not in s.out and "this run's own records" not in s.out and NO_OWN_RECORD not in s.out


def two_runs(bad):
    """The newest run (802) is one the step must not read, with a record of its own; 801 is good."""
    return {"runs": [a_run(802, **bad), a_run(801)],
            "artifacts": {802: [an_artifact(902, 802)], 801: [an_artifact(901, 801)]},
            "zips": {902: other_zip, 901: good_zip}}


def link_before(temp):
    (temp / "outside").mkdir()
    (temp / "outside" / "keep").write_text("keep\n")
    (temp / "sweep-record").symlink_to(temp / "outside")


def stale_before(temp):
    (temp / "sweep-record").mkdir()
    (temp / "sweep-record" / "deleted-tips").write_text(other_record)
    (temp / "sweep-record.zip").write_bytes(other_zip)


huge_lines = RECORD_HEAD + "".join(f"{hashlib.sha1(str(n).encode()).hexdigest()} scanned\n" for n in range(4000000 // 48 + 10))
READ_CASES = {
    "the newest sweep run's record": ({}, read_as(good_record)),
    "a plain file with its type in the mode (as upload-artifact writes it)": (
        {"zips": {901: a_zip([(zip_info("deleted-tips"), good_record)])}}, read_as(good_record)),
    "a device file in the archive": ({"zips": {901: a_zip([(zip_info("deleted-tips", 0o20644), good_record)])}}, read_as(None)),
    "a header-only record (no deleted tip a scan read)": (
        {"zips": {901: a_zip([("deleted-tips", RECORD_HEAD)])}}, read_as(RECORD_HEAD)),
    "reset_record: an empty record (the header only), kept even if the run fails before the scan writes its own "
    "(#703), nothing asked": (
        {"RESET": "true", "before": stale_before},
        lambda s: s.r.returncode == 0 and s.record == RECORD_HEAD and s.output == "read=reset\n" and s.calls == ""
        and "an empty record is kept even if this run fails" in s.out and s.left == []),
    "a run on a tag: no record, nothing asked": (
        {"GITHUB_REF": "refs/tags/v1"}, lambda s: read_as(None)(s) and s.calls == ""),
    "reset_record on a tag: no record, nothing asked": (
        {"GITHUB_REF": "refs/tags/v1", "RESET": "true"}, lambda s: read_as(None)(s) and s.calls == ""),
    "a tag named like the branch (a sweep run on it has that head_branch too): no record, no run listed (#703)": (
        {"tags": ["refs/tags/main"]},
        lambda s: read_as(None)(s) and "actions/workflows" not in s.calls and "matching-refs/tags/main" in s.calls),
    "a tag whose name only starts with the branch's (main-old): read as usual": (
        {"tags": ["refs/tags/main-old"]}, read_as(good_record)),
    "whether a tag is named like the branch cannot be checked: no record": ({"FAKE_FAIL": "matching-refs"}, read_as(None)),
    "a branch whose name the tag check does not take (fix+1): no record, nothing asked": (
        {"GITHUB_REF": "refs/heads/fix+1", "runs": [a_run(801, head_branch="fix+1")]},
        lambda s: read_as(None)(s) and s.calls == ""),
    "a branch with a slash (feature/x)": (
        {"GITHUB_REF": "refs/heads/feature/x", "runs": [a_run(801, head_branch="feature/x")]},
        lambda s: read_as(good_record)(s) and "matching-refs/tags/feature/x" in s.calls),
    "a re-run (attempt 2): its own first attempt's record first, though older runs are newer in the list (#703)": (
        {**two_runs({}), "GITHUB_RUN_ATTEMPT": "2", "artifacts": {900: [an_artifact(903, 900)], 802: [an_artifact(902, 802)],
                                                                  801: [an_artifact(901, 801)]},
         "zips": {903: a_zip([("deleted-tips", RECORD_HEAD + f"{sweep_gone2} scanned\n{sweep_gone} scanned\n")]),
                  902: other_zip, 901: good_zip}},
        lambda s: read_as(RECORD_HEAD + f"{sweep_gone2} scanned\n{sweep_gone} scanned\n", 900)(s)
        and rerun_log(kept=1)(s)),
    # #950: "Re-run all jobs" deletes an earlier attempt's artifacts, so these cases (its own run lists
    # none) are what such a re-run meets live: it reads as a new run does, and warns why.
    "a re-run whose first attempt's record is not listed (#950): the newest earlier run's, logged": (
        {"GITHUB_RUN_ATTEMPT": "2", "runs": [a_run(802), a_run(801)]},
        lambda s: read_as(good_record)(s) and "runs/900/artifacts" in s.calls and rerun_log(kept=0)(s)),
    "a re-run whose own record is not listed and whose newest earlier run kept an empty one (#950)": (
        {"GITHUB_RUN_ATTEMPT": "2", "zips": {901: a_zip([("deleted-tips", RECORD_HEAD)])}},
        lambda s: read_as(RECORD_HEAD)(s) and rerun_log(kept=0)(s)),
    "a re-run whose own record is not listed and whose newest earlier run's expired: the one before (#950)": (
        {**two_runs({}), "GITHUB_RUN_ATTEMPT": "2",
         "artifacts": {802: [an_artifact(902, 802, expired=True)], 801: [an_artifact(901, 801)]}},
        lambda s: read_as(good_record)(s) and rerun_log(kept=0)(s)),
    "a re-run whose own run lists an artifact of that name that is not its unexpired record: both counts "
    "logged (listed 1, kept 0), and the newest earlier run's read (#967)": (
        {"GITHUB_RUN_ATTEMPT": "2", "artifacts": {900: [an_artifact(903, 900, expired=True)], 801: [an_artifact(901, 801)]},
         "zips": {903: other_zip, 901: good_zip}},
        lambda s: read_as(good_record)(s) and "artifacts/903/zip" not in s.calls and rerun_log(kept=0, listed=1)(s)),
    "a re-run whose own record is not listed and no earlier sweep kept one (or it expired): a first run "
    "(#950)": (
        {"GITHUB_RUN_ATTEMPT": "2", "runs": []},
        lambda s: read_as(None, state="none")(s) and rerun_log(kept=0)(s)),
    "a re-run of another workflow's run (fork-tests.yml, which may also call the scan): its own run never "
    "asked about, though it has an artifact of that name (#703)": (
        {"GITHUB_RUN_ATTEMPT": "2", "GITHUB_WORKFLOW_REF": "me/t3code/.github/workflows/fork-tests.yml@refs/heads/main",
         "artifacts": {900: [an_artifact(903, 900)], 801: [an_artifact(901, 801)]},
         "zips": {903: other_zip, 901: good_zip}},
        lambda s: read_as(good_record)(s) and "runs/900/" not in s.calls and rerun_log(matched=False)(s)),
    "a re-run of a reset_record run: its own first attempt's record (written after the reset), no older run "
    "listed (#703)": (
        {"RESET": "true", "GITHUB_RUN_ATTEMPT": "2", "artifacts": {900: [an_artifact(903, 900)], 801: [an_artifact(901, 801)]},
         "zips": {903: a_zip([("deleted-tips", other_record)]), 901: good_zip}},
        lambda s: read_as(other_record, 900)(s) and "actions/workflows" not in s.calls and rerun_log(kept=1)(s)),
    "a re-run of a reset_record run whose first attempt's record is not listed (#950): the empty record, no "
    "older run listed (#703)": (
        {"RESET": "true", "GITHUB_RUN_ATTEMPT": "2"},
        lambda s: s.r.returncode == 0 and s.record == RECORD_HEAD and s.output == "read=reset\n"
        and "runs/900/artifacts" in s.calls and "actions/workflows" not in s.calls and s.left == []
        and rerun_log(kept=0)(s)),
    # #791: any failed or doubtful read in a reset run's re-run gives the empty record, never read=failed:
    # if its first attempt kept nothing, read=failed would leave the record from before the reset in use.
    "a re-run of a reset_record run whose own record cannot be listed: the empty record, no older run "
    "listed (#791)": (
        {"RESET": "true", "GITHUB_RUN_ATTEMPT": "2", "FAKE_FAIL": "/artifacts -f",
         "artifacts": {900: [an_artifact(903, 900)]}, "zips": {903: good_zip}},
        lambda s: reset_empty(s) and "runs/900/artifacts" in s.calls and "cannot be listed" in s.out),
    "a re-run of a reset_record run whose tag check fails: the empty record (#791)": (
        {"RESET": "true", "GITHUB_RUN_ATTEMPT": "2", "FAKE_FAIL": "matching-refs",
         "artifacts": {900: [an_artifact(903, 900)]}, "zips": {903: good_zip}},
        lambda s: reset_empty(s) and "runs/900/" not in s.calls and "cannot be checked" in s.out),
    "a re-run of a reset_record run while a tag is named like the branch: the empty record (#791)": (
        {"RESET": "true", "GITHUB_RUN_ATTEMPT": "2", "tags": ["refs/tags/main"],
         "artifacts": {900: [an_artifact(903, 900)]}, "zips": {903: good_zip}},
        lambda s: reset_empty(s) and "runs/900/" not in s.calls),
    "a re-run of a reset_record run whose own record cannot be downloaded: the empty record (#791)": (
        {"RESET": "true", "GITHUB_RUN_ATTEMPT": "2", "artifacts": {900: [an_artifact(903, 900)]}, "zips": {}},
        lambda s: reset_empty(s) and "artifacts/903/zip" in s.calls),
    "a re-run of a reset_record run whose own record cannot be read: the empty record, not a partial bad one "
    "(#791)": (
        {"RESET": "true", "GITHUB_RUN_ATTEMPT": "2", "artifacts": {900: [an_artifact(903, 900)]},
         "zips": {903: a_zip([("deleted-tips", good_record + "junk\n")])}},
        lambda s: reset_empty(s) and "artifacts/903/zip" in s.calls),
    "a re-run of a reset_record run whose commit check fails: the empty record (#791)": (
        {"RESET": "true", "GITHUB_RUN_ATTEMPT": "2", "FAKE_FAIL": "compare/",
         "artifacts": {900: [an_artifact(903, 900)]}, "zips": {903: good_zip}}, reset_empty),
    "a first attempt: its own run never asked about, no re-run log (#950)": (
        {}, lambda s: read_as(good_record)(s) and "runs/900/" not in s.calls and first_attempt_log(s)),
    "no sweep run kept one yet (or it expired): a first run": ({"runs": []}, read_as(None, state="none")),
    "the newest run kept none: the one before": (
        {"runs": [a_run(802), a_run(801)]}, lambda s: read_as(good_record)(s) and "runs/802/artifacts" in s.calls),
    "this run itself listed: skipped": (
        {**two_runs({}), "runs": [a_run(800, id=900), a_run(801)], "artifacts": {900: [an_artifact(902, 900)],
                                                                                  801: [an_artifact(901, 801)]}},
        read_as(good_record)),
    "another workflow's run (fork-tests.yml)": (two_runs({"path": ".github/workflows/fork-tests.yml"}), read_as(good_record)),
    "the scan workflow's own run (a push's)": (two_runs({"path": ".github/workflows/fork-secret-scan.yml"}), read_as(good_record)),
    "a pull request's run": (two_runs({"event": "pull_request"}), read_as(good_record)),
    "a push's run": (two_runs({"event": "push"}), read_as(good_record)),
    "another branch's run": (two_runs({"head_branch": "feature"}), read_as(good_record)),
    "a fork's run (head repository)": (two_runs({"head_repository": {"full_name": "other/t3code"}}), read_as(good_record)),
    "another repository's run": (two_runs({"repository": {"full_name": "other/t3code"}}), read_as(good_record)),
    "a cancelled run": (two_runs({"conclusion": "cancelled"}), read_as(good_record)),
    "a cancelled run of the sweep's usual name (a new sweep cancelled while it waited): skipped": (
        two_runs({"conclusion": "cancelled", "display_title": "Fork secret sweep"}), read_as(good_record)),
    # #950: a reset run cancelled before it ended (a re-run of it cancelled, or the run by hand) shows as
    # cancelled, and its record is not read; an older record may be from before the reset, so none is:
    # the empty record, kept even if this run fails, as the reset run's own would have been.
    "a cancelled reset run (by its run name) newer than every record: no older record read, the empty one "
    "kept (#950)": (
        {"runs": [a_run(802, conclusion="cancelled", display_title=RESET_RUN_TITLE), a_run(801)]},
        reset_by(802, "kept no record (or it expired)")),
    "a cancelled reset run older than the newest record (a sweep after it read it): that record (#950)": (
        {"runs": [a_run(802), a_run(801, conclusion="cancelled", display_title=RESET_RUN_TITLE)],
         "artifacts": {802: [an_artifact(902, 802)], 801: [an_artifact(901, 801)]}, "zips": {902: good_zip, 901: other_zip}},
        read_as(good_record, 802)),
    "a reset run's name on a run that ended (failure): read as usual (#950)": (
        {"runs": [a_run(801, conclusion="failure", display_title=RESET_RUN_TITLE)]}, read_as(good_record)),
    # #992: one rule for a reset run, whatever its outcome or state: its own record is read when it has
    # one (written after the reset, so safe), and when it is not read, no older record is either.
    "a cancelled reset run that kept a record (a re-run of it cancelled, its earlier attempt's record still "
    "listed): that record, no second set of lookups (#992)": (
        {"runs": [a_run(802, conclusion="cancelled", display_title=RESET_RUN_TITLE), a_run(801)],
         "artifacts": {802: [an_artifact(902, 802)], 801: [an_artifact(901, 801)]}, "zips": {902: good_zip, 901: other_zip}},
        read_as(good_record, 802)),
    "a reset run that failed and kept no record (its upload failed), newer than every record: no older record "
    "read, the empty one kept (#992)": (
        {"runs": [a_run(802, conclusion="failure", display_title=RESET_RUN_TITLE), a_run(801)]},
        reset_by(802, "kept no record (or it expired)")),
    "a reset run that passed but whose record expired, newer than every record: the empty one (#992)": (
        {"runs": [a_run(802, display_title=RESET_RUN_TITLE), a_run(801)],
         "artifacts": {802: [an_artifact(902, 802, expired=True)], 801: [an_artifact(901, 801)]}},
        reset_by(802, "kept no record (or it expired)")),
    "a reset run on a commit this branch lacks (diverged): the empty one, no older record (#992)": (
        {"runs": [a_run(802, display_title=RESET_RUN_TITLE), a_run(801)],
         "artifacts": {802: [an_artifact(902, 802)], 801: [an_artifact(901, 801)]}, "zips": {902: other_zip, 901: good_zip},
         "compare": {read_sha[802]: "diverged"}},
        reset_by(802, "ran on a commit this branch does not have")),
    "a reset run's re-run still waiting (queued), its earlier attempt's record deleted, newer than every "
    "record: the empty one (#992)": (
        {"runs": [a_run(802, status="queued", conclusion=None, display_title=RESET_RUN_TITLE), a_run(801)]},
        lambda s: reset_by(802, "kept no record (or it expired)")(s) and "status=completed" not in s.calls),
    "a reset run's re-run still running whose earlier attempt's record is listed: that record (#992)": (
        {"runs": [a_run(802, status="in_progress", conclusion=None, display_title=RESET_RUN_TITLE), a_run(801)],
         "artifacts": {802: [an_artifact(902, 802)], 801: [an_artifact(901, 801)]}, "zips": {902: good_zip, 901: other_zip}},
        read_as(good_record, 802)),
    # #992: a re-run of a sweep run that started before a reset run (run ids only go up, so the reset
    # run's id is higher) reads as a new run does, never its own earlier attempt's record, which is from
    # before the reset. A reset run older than it, or a newer run that is not a reset run, changes nothing.
    "a re-run of a sweep that started before a reset run (950) that kept a record: the reset run's record, "
    "never its own from before the reset (#992)": (
        {"GITHUB_RUN_ATTEMPT": "2", "runs": [a_run(802, id=950, display_title=RESET_RUN_TITLE), a_run(801)],
         "artifacts": {900: [an_artifact(903, 900)], 950: [an_artifact(902, 950)], 801: [an_artifact(901, 801)]},
         "zips": {903: other_zip, 902: good_zip, 901: other_zip}},
        lambda s: read_as(good_record, 950)(pre_reset_rerun(s, 950)) and "runs/900/" not in s.calls and rerun_log()(s)),
    "a re-run of a sweep that started before a reset run (950) that kept none: the empty record, never its "
    "own from before the reset (#992)": (
        {"GITHUB_RUN_ATTEMPT": "2", "runs": [a_run(802, id=950, display_title=RESET_RUN_TITLE), a_run(801)],
         "artifacts": {900: [an_artifact(903, 900)], 801: [an_artifact(901, 801)]},
         "zips": {903: other_zip, 901: other_zip}},
        lambda s: reset_by(950, "kept no record (or it expired)")(s) and "runs/900/" not in s.calls
        and PRE_RESET_RERUN.format(950) in s.out and "artifacts/903/zip" not in s.calls),
    "a re-run of a sweep that started after a reset run (802): its own first attempt's record (#992)": (
        {"GITHUB_RUN_ATTEMPT": "2", "runs": [a_run(802, display_title=RESET_RUN_TITLE), a_run(801)],
         "artifacts": {900: [an_artifact(903, 900)], 802: [an_artifact(902, 802)], 801: [an_artifact(901, 801)]},
         "zips": {903: good_zip, 902: other_zip, 901: other_zip}},
        lambda s: read_as(good_record, 900)(s) and rerun_log(kept=1)(s)),
    "a re-run of a sweep before a newer run (950) that is not a reset run: its own first attempt's record (#992)": (
        {"GITHUB_RUN_ATTEMPT": "2", "runs": [a_run(802, id=950), a_run(801)],
         "artifacts": {900: [an_artifact(903, 900)], 950: [an_artifact(902, 950)], 801: [an_artifact(901, 801)]},
         "zips": {903: good_zip, 902: other_zip, 901: other_zip}},
        lambda s: read_as(good_record, 900)(s) and rerun_log(kept=1)(s)),
    "a run still in progress": (two_runs({"status": "in_progress", "conclusion": None}), read_as(good_record)),
    "a run still waiting (queued)": (two_runs({"status": "queued", "conclusion": None}), read_as(good_record)),
    "a failed run (its record is whole: the scan step wrote it)": (
        {**two_runs({"conclusion": "failure"}), "zips": {902: good_zip, 901: other_zip}}, read_as(good_record, 802)),
    "a run whose commit this branch lacks (diverged)": (
        {**two_runs({}), "compare": {read_sha[802]: "diverged"}}, read_as(good_record)),
    "a run whose commit is ahead of this one (behind)": (
        {**two_runs({}), "compare": {read_sha[802]: "behind"}}, read_as(good_record)),
    "an expired artifact": (
        {**two_runs({}), "artifacts": {802: [an_artifact(902, 802, expired=True)], 801: [an_artifact(901, 801)]}},
        read_as(good_record)),
    "an artifact of another name": (
        {**two_runs({}), "artifacts": {802: [an_artifact(902, 802, name="x")], 801: [an_artifact(901, 801)]}},
        read_as(good_record)),
    "an artifact another run kept": (
        {**two_runs({}), "artifacts": {802: [an_artifact(902, 999)], 801: [an_artifact(901, 801)]}},
        read_as(good_record)),
    "two artifacts of that name: no record": (
        {**two_runs({}), "artifacts": {802: [an_artifact(902, 802), an_artifact(903, 802)]}}, read_as(None)),
    "an artifact listed over 4 MB": ({"artifacts": {801: [an_artifact(901, 801, size_in_bytes=4000001)]}}, read_as(None)),
    "an archive bigger than 4 MB (a good one behind 4 MB of junk, which zipfile still reads)": (
        {"zips": {901: b"\0" * 4000001 + good_zip}}, read_as(None)),
    "the runs cannot be listed": ({"FAKE_FAIL": "actions/workflows"}, read_as(None)),
    "the artifacts cannot be listed": ({"FAKE_FAIL": "/artifacts -f"}, read_as(None)),
    "the commits cannot be compared": ({"FAKE_FAIL": "compare/"}, read_as(None)),
    "the archive cannot be downloaded": ({"zips": {}}, read_as(None)),
    "a stopped run's zip and record left in RUNNER_TEMP, and no record now": (
        {"runs": [], "before": stale_before}, lambda s: read_as(None, state="none")(s) and s.left == []),
    "a stopped run's zip and record left, then a good one": ({"before": stale_before}, read_as(good_record)),
    "a link where the record's folder goes: removed, its target untouched": (
        {"before": link_before}, lambda s: s.r.returncode == 0 and s.record == good_record
        and "the sweep's record from run 801:" in s.out and s.left == ["outside", "outside/keep"]
        and (s.temp / "outside" / "keep").read_text() == "keep\n" and not (s.temp / "sweep-record").is_symlink()),
    "an archive that is no zip": ({"zips": {901: b"not a zip"}}, read_as(None)),
    "an empty archive": ({"zips": {901: a_zip([])}}, read_as(None)),
    "two files in the archive": ({"zips": {901: a_zip([("deleted-tips", good_record), ("x", "x")])}}, read_as(None)),
    "the record as a link in the archive": (
        {"zips": {901: a_zip([(zip_info("deleted-tips", 0o120777), "/etc/passwd")])}}, read_as(None)),
    "the record as a folder in the archive": ({"zips": {901: a_zip([(zip_info("deleted-tips/", 0o40755), "")])}}, read_as(None)),
    "a path out of the folder (../deleted-tips)": ({"zips": {901: a_zip([("../deleted-tips", good_record)])}}, read_as(None)),
    "a path in a subfolder (x/deleted-tips)": ({"zips": {901: a_zip([("x/deleted-tips", good_record)])}}, read_as(None)),
    "another file name": ({"zips": {901: a_zip([("record", good_record)])}}, read_as(None)),
    "an encrypted member": ({"zips": {901: encrypted(good_zip)}}, read_as(None)),
    "a record over 4 MB": ({"zips": {901: a_zip([("deleted-tips", huge_lines)])}}, read_as(None)),
    "a NUL byte": ({"zips": {901: a_zip([("deleted-tips", RECORD_HEAD + f"{sweep_gone} scanned\0x\n")])}}, read_as(None)),
    "CRLF line ends": ({"zips": {901: a_zip([("deleted-tips", good_record.replace("\n", "\r\n"))])}}, read_as(None)),
    "a duplicate line": ({"zips": {901: a_zip([("deleted-tips", good_record + f"{sweep_gone} scanned\n")])}}, read_as(None)),
    "the cache's header (record 1)": (
        {"zips": {901: a_zip([("deleted-tips", good_record.replace("record 2", "record 1"))])}}, read_as(None)),
    "the cache's unscanned state": (
        {"zips": {901: a_zip([("deleted-tips", RECORD_HEAD + f"{sweep_gone} unscanned\n")])}}, read_as(None)),
    "a line cut short (no newline at the end)": ({"zips": {901: a_zip([("deleted-tips", good_record[:-1])])}}, read_as(None)),
    "an id in capitals": ({"zips": {901: a_zip([("deleted-tips", good_record.upper().replace("FORK SECRET SWEEP RECORD", "fork secret sweep record").replace(" SCANNED", " scanned"))])}}, read_as(None)),
    "no header": ({"zips": {901: a_zip([("deleted-tips", f"{sweep_gone} scanned\n")])}}, read_as(None)),
}
read_code = (read_step(real) or {}).get("run", "exit 99\n")  # (no read step: every case red)


def read_results(code, names=None, first_red=False):
    """Each case's (ok, run); with first_red, stops at the first red one (a mutant needs only one)."""
    out = {}
    for name, (setup, ok) in READ_CASES.items():
        if names is not None and name not in names:
            continue
        setup = dict(setup)
        s = read_record(code, setup.pop("runs", None), setup.pop("artifacts", None), setup.pop("zips", None),
                        setup.pop("compare", None), before=setup.pop("before", None), tags=setup.pop("tags", None),
                        **setup)
        out[name] = (bool(ok(s)), s)
        if first_red and not out[name][0]:
            break
    return out


for name, (ok, s) in read_results(read_code).items():
    check(f"secret-scan e2e (#701): the record's read step, {name}", ok, read_detail(s))
# The read step, then the scan step's lookup on that RUNNER_TEMP: the deleted tip the last sweep's scan
# read is neither fetched nor looked up.
s = read_record(read_code)
s = sweep_lookup(sweep_clone("read-then-scan"), all_heads, "read-then-scan", temp=s.temp)
check("secret-scan e2e (#701): the record the read step left is the one the scan step reads: the tip it lists "
      "neither fetched nor looked up",
      s.r.returncode == 0 and s.gone == [sweep_gone] and fetches_of(s.trace, sweep_gone) == 0
      and "actions/runs" not in s.calls and s.record == first_record, sweep_detail(s))
# #703, the read step's output into the scan step: reset_record's empty record, then a lookup that
# fails half-way keeps the tips proven so far (the old record is not used again); a read that failed,
# then the same: nothing marked for keeping.
for n, (what, setup, want_record, want_output) in enumerate((
    ("reset_record", {"RESET": "true"}, RECORD_HEAD + f"{budget_tips[0]} scanned\n", "record=written\n"),
    ("a failed read (the runs cannot be listed)", {"FAKE_FAIL": "actions/workflows"}, None, ""),
    ("a reset run's re-run whose own record cannot be listed (#791)",
     {"RESET": "true", "GITHUB_RUN_ATTEMPT": "2", "FAKE_FAIL": "/artifacts -f", "artifacts": {900: [an_artifact(903, 900)]}},
     RECORD_HEAD + f"{budget_tips[0]} scanned\n", "record=written\n"),
)):
    r = read_record(read_code, **setup)
    read = re.fullmatch(r"read=(\w+)\n", r.output)
    s = sweep_lookup(sweep_clone(f"read-scan{n}"), budget_heads, f"read-scan{n}", temp=r.temp,
                     RECORD_READ=read.group(1) if read else "", FAKE_BUDGET="3", **budget_env)
    check(f"secret-scan e2e (#703): {what}, then the scan's lookups fail half-way: "
          + ("the proven tips kept" if want_record else "no partial record"),
          read is not None and s.r.returncode == 2 and s.record == want_record and s.output == want_output,
          f"read: {read_detail(r)} | scan: {sweep_detail(s)}")
# #950: a re-run (its own record not listed, as live), then the scan's lookups fail half-way: it read the
# newest earlier sweep's record as a first attempt does, and keeps the same partial record.
halfway = []
for n, attempt in enumerate(("1", "2")):
    r = read_record(read_code, GITHUB_RUN_ATTEMPT=attempt)
    read = re.fullmatch(r"read=(\w+)\n", r.output)
    halfway.append((r, read, sweep_lookup(sweep_clone(f"rerun-half{n}"), budget_heads, f"rerun-half{n}", temp=r.temp,
                                          RECORD_READ=read.group(1) if read else "", FAKE_BUDGET="3", **budget_env)))
(r1, read1, s1), (r2, read2, s2) = halfway
check("secret-scan e2e (#950): a re-run whose own record is not listed, then the scan's lookups fail half-way: "
      "the newest earlier sweep's record read, and the same proven tips kept as a first attempt keeps",
      read1 is not None and read2 is not None and read2.group(1) == read1.group(1) == "found"
      and "this run's own records: the API listed 0 artifact(s) of that name, 0 of them" in r2.out and s2.r.returncode == 2 and s2.output == "record=written\n"
      and s2.record is not None and s2.record == s1.record,
      f"first: {read_detail(r1)} | {sweep_detail(s1)} || re-run: {read_detail(r2)} | {sweep_detail(s2)}")
# Each of the read step's guards, removed: a case above goes red. (Two are only belts and braces, so no
# mutant: a run not completed has no conclusion yet, and zipfile will not read an encrypted member.)
for what, old, new in (
    ("the workflow path check", '.path == ".github/workflows/fork-secret-sweep.yml" and ', ""),
    ("the event check", '(.event == "schedule" or .event == "workflow_dispatch")', "true"),
    ("the branch check", ".head_branch == env.BRANCH and ", ""),
    ("the repository check", "env.BRANCH and .repository.full_name == env.GITHUB_REPOSITORY\n", "env.BRANCH and true\n"),
    ("the head repository check", "and .head_repository.full_name == env.GITHUB_REPOSITORY\n", "and true\n"),
    ("the conclusion check", '(.conclusion == "success" or .conclusion == "failure")', "true"),
    ("every reset run listed, whatever its outcome (#950, #992)",
     '(.display_title == "Fork secret sweep (reset_record)"\n          or ', "("),
    ("the unfinished runs listed (#992)", '-f branch="$BRANCH" -f per_page=30', '-f branch="$BRANCH" -f status=completed -f per_page=30'),
    ("the reset run's name check (#950)", 'then "reset" else "sweep" end', 'then "sweep" else "sweep" end'),
    ("no older record after a reset run kept none (#950, #992)", '[[ "$kind" == reset ]] || return 0', "return 0"),
    ("no older record after a reset run on a commit this branch lacks (#992)",
     '|| { unread "ran on a commit this branch does not have"; continue; }', "|| continue"),
    ("this run's own skip", '[[ "$id" != "$GITHUB_RUN_ID" || "$id $sha" == "$self" ]] || continue', ":"),
    ("a re-run from before a reset run reads as a new run (#992)", '    self=""\n    break\n', "    break\n"),
    ("the reset run's kind in the re-run check (#992)", '[[ "$kind" == reset && "$id" =~ ^[1-9]', '[[ "$id" =~ ^[1-9]'),
    ("the higher run id in the re-run check (#992)", ' && "$id" -gt "$GITHUB_RUN_ID" ]] ||', " ]] ||"),
    ("a re-run's own first attempt first (#703)", 'done <<< "$self"$\'\\n\'"$runs"', 'done <<< "$runs"'),
    ("the first attempt's check (#703)", '[[ "${GITHUB_RUN_ATTEMPT:-1}" != 1 \\', "[[ true \\"),
    ("the sweep's own workflow check for a re-run (#703)",
     '\n    && "${GITHUB_WORKFLOW_REF:-}" == "$GITHUB_REPOSITORY/.github/workflows/fork-secret-sweep.yml@"* ]]', " ]]"),
    ("a reset run's re-run from its own record (#703)", '[[ "$RESET" != true || -n "$self" ]] || empty_record',
     '[[ "$RESET" != true ]] || empty_record'),
    ("the older runs left out for a reset run's re-run (#703)", 'if [[ "$RESET" != true ]] && ! runs=', "if ! runs="),
    ("the empty record for a reset run's re-run without its own (#703)", '  [[ "$RESET" != true ]] || empty_record\n', ""),
    ("the tag named like the branch (#703)", '[[ -z "$tag" ]] ||', ": ||"),
    ("the failed tag check (#703)", 'none failed "whether a tag is named like this branch cannot be checked"', ":"),
    ("the branch name check (#703)", '[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] ||', ": ||"),
    ("the output read=none (#703)", 'none none "no earlier sweep', 'none failed "no earlier sweep'),
    ("the output read=failed (#703)", 'none failed "the sweep\'s earlier runs cannot be listed"',
     'none none "the sweep\'s earlier runs cannot be listed"'),
    ("the output read=found (#703)", 'echo "read=found" >> "${GITHUB_OUTPUT:?}"\n', ""),
    ("the artifact's name check", 'select(.name == "fork-secret-sweep-record" and ', "select("),
    ("the expired check", ".expired == false and ", ""),
    ("the artifact's run check", " and (.workflow_run.id | tostring) == env.RUN", ""),
    ("the one-artifact check", 'if length > 1 then error("more than one") else "\\(length)',
     'if false then empty else "\\([length, 1] | min)'),
    ("the ancestor check", '[[ "$has" == ahead || "$has" == identical ]] ||', ": ||"),
    ("the listed size check", ' && "$size" -le 4000000', ""),
    ("the archive's size check", '[[ "$(stat -c %s -- "$zip")" -le 4000000 ]] ||', ": ||"),
    ("reset_record", '[[ "$RESET" != true || -n "$self" ]] || empty_record\n', ""),
    ("reset_record's empty record (#703)", 'echo "fork secret sweep record 2" > "$dir/deleted-tips"\n', ""),
    ("the tag check", '[[ "$GITHUB_REF" == refs/heads/* ]] ||', ": ||"),
    ("the one-member check", "[m] = z.infolist()", "m = z.infolist()[0]"),
    ("the member's name check", 'm.filename != "deleted-tips" or ', ""),
    ("the plain-file check", "m.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG)", "False"),
    ("the whole-text match", "re.fullmatch(", "re.match("),
    ("the duplicate check", "if len(set(tips)) != len(tips):", "if False:"),
    ("the fresh folder", 'rm -rf -- "$dir" "$zip"\n', ""),
    ("the empty record on a failed read in a reset run (#791)",
     '  if [[ "$1" == failed && "$RESET" == true && "$GITHUB_REF" == refs/heads/* ]]; then\n'
     '    echo "::warning title=Secret scan::$2" >&2\n    empty_record\n  fi\n', ""),
    ("the branch check of the empty record on a failed read (#791)",
     ' && "$GITHUB_REF" == refs/heads/* ]]; then', " ]]; then"),
):
    mutated = read_code.replace(old, new)
    results = read_results(mutated, first_red=True) if mutated != read_code else {}
    check(f"secret-scan e2e (#701): the record's read step without {what}: a case above goes red",
          mutated != read_code and not all(ok for ok, _ in results.values()),
          "unchanged" if mutated == read_code else "every case still green")

# #637, end to end: the lists' hand-over, as the workflow has it (from the token's unset to the end),
# in a clone whose origin's default branch holds a stub scan script that reports what it was given.
# The sweep of 11-20's dispatch got past the lookup and then failed here: OLD_HEADS (every activity
# entry's before and after, 131 KB) went out as one environment string, over the kernel's 128 KiB,
# and the next program (git) could not start ("Argument list too long", exit 126). Also: a list
# too long even with each id once (only the file carries it), small and empty lists, the files
# of an earlier or stopped run left in RUNNER_TEMP (written afresh, nothing of theirs read), and
# (#691) a default branch whose script predates the files: a list too long to export fails the job.
def handoff_code(run):
    start = run.find("unset LOOKUP_TOKEN\n")
    return run[start:] if start >= 0 else "exit 99\n"


handoff_origin = sweep_work / "handoff-origin"
(handoff_origin / ".github" / "scripts").mkdir(parents=True)
git(handoff_origin, "init", "-q", "-b", "main")
handoff_script = handoff_origin / ".github" / "scripts" / "fork-secret-scan.sh"
handoff_script.write_text(r"""#!/usr/bin/env bash
set -euo pipefail
printf 'env_old=%s\nenv_gone=%s\n' "${OLD_HEADS+set}" "${SCANNED_GONE+set}"
printf 'env_old_words=%s\n' "$(wc -w <<< "${OLD_HEADS:-}")"
printf 'file_old=%s\nfile_gone=%s\nupstream=%s\n' "${OLD_HEADS_FILE:-}" "${SCANNED_GONE_FILE:-}" "${UPSTREAM-unset}"
printf 'file_deleted=%s\n' "${DELETED_TIPS_FILE:-}"
""")
git(handoff_origin, "add", "-A")
git(handoff_origin, "commit", "-q", "-m", "stub scan script")
git(handoff_origin, "checkout", "-q", "-b", "old")
handoff_script.write_text("#!/usr/bin/env bash\nprintf 'old_script=ran\\nenv_old_words=%s\\n' \"$(wc -w <<< \"${OLD_HEADS:-}\")\"\n")
git(handoff_origin, "commit", "-q", "-am", "a scan script from before the files")
git(handoff_origin, "checkout", "-q", "main")
handoff_clone = sweep_work / "handoff-clone"
git(sweep_work, "clone", "-q", f"file://{handoff_origin}", str(handoff_clone))
handoff_temp = sweep_work / "handoff-temp"
handoff_temp.mkdir()


def handoff(old_heads, scanned_gone, label, code=None, deleted="", branch="main"):
    """Runs the hand-over with these lists (shell variables, as the lookups leave them); returns
    (exit, stub report, the words of the old-heads file, the words of the scanned-gone file, the
    words of the deleted-tips file)."""
    for ext, text in (("old", old_heads), ("gone", scanned_gone), ("deleted", deleted)):
        (sweep_work / f"{label}.{ext}").write_text(text)
    script = ('set -euo pipefail\nUPSTREAM=up/stream\nLOOKUP_TOKEN=x\n'
              f'OLD_HEADS="$(cat {sweep_work / (label + ".old")})"\n'
              f'SCANNED_GONE="$(cat {sweep_work / (label + ".gone")})"\n'
              f'DELETED_TIPS="$(cat {sweep_work / (label + ".deleted")})"\n'
              + (code or handoff_code(scan_step(real)["run"])))
    r = subprocess.run(["bash", "--noprofile", "--norc", "-c", script], cwd=handoff_clone,
                       env={**base_env, "RUNNER_TEMP": str(handoff_temp), "CONFIG_BRANCH": branch,
                            "GIT_CEILING_DIRECTORIES": str(sweep_work)}, capture_output=True, text=True)
    report = dict(re.findall(r"^(\w+)=(.*)$", r.stdout, re.M))

    def words(key):
        path = report.get(key) or ""
        return pathlib.Path(path).read_text().split() if path and pathlib.Path(path).is_file() else None
    return r, report, words("file_old"), words("file_gone"), words("file_deleted")


def ids(n, salt):
    return [hashlib.sha1(f"{salt}{i}".encode()).hexdigest() for i in range(n)]


day_ids = ids(1700, "tip")  # the 11-20 shape: each id twice (a push's after is the next one's before)
day_gone = ids(130, "gone")
day_list = "\n".join(x for i in day_ids for x in (i, i)) + "\n"
r, rep_, old_words, gone_words, deleted_words = handoff(day_list, " ".join(day_gone) + " ", "day",
                                                        deleted=" ".join(day_gone) + " ")
check("secret-scan e2e (#637): a list over 128 KiB: the scan script starts, the lists reach it in files (each id once), "
      "the variables are still exported for an older script while they fit",
      len(day_list) > 131072 and r.returncode == 0 and old_words == sorted(day_ids) and gone_words == day_gone
      and deleted_words == day_gone and rep_.get("env_old") == "set" and rep_.get("env_old_words") == str(len(day_ids))
      and rep_.get("env_gone") == "set" and rep_.get("upstream") == "up/stream",
      f"exit {r.returncode}: {r.stderr.strip()[-300:]} report={rep_} old={len(old_words or [])}")
huge_ids = ids(3000, "huge")
r, rep_, old_words, _, _ = handoff("\n".join(huge_ids), "", "huge")
check("secret-scan e2e: a list over 128 KiB with each id once: only the file carries it, and the scan starts",
      r.returncode == 0 and old_words == sorted(huge_ids) and rep_.get("env_old") == "",
      f"exit {r.returncode}: {r.stderr.strip()[-300:]} report={rep_}")
for what, lists in {"the earlier heads": ("\n".join(huge_ids), ""), "SCANNED_GONE": ("", " ".join(huge_ids))}.items():
    r, rep_, *_ = handoff(*lists, "huge-old", branch="old")
    check(f"secret-scan e2e (#691): {what} too long to export, and a default branch whose script reads only the "
          "variables (it would read an empty list): the job fails, that script not run",
          r.returncode == 2 and "too long for it" in r.stderr and "old_script" not in rep_,
          f"exit {r.returncode}: {r.stderr.strip()[-300:]} report={rep_}")
r, rep_, *_ = handoff(" ".join(day_ids), "", "small-old", branch="old")
check("secret-scan e2e: lists that fit, and that older default-branch script: it runs, with the list in its variable",
      r.returncode == 0 and rep_.get("old_script") == "ran" and rep_.get("env_old_words") == str(len(day_ids)),
      f"exit {r.returncode}: {r.stderr.strip()[-300:]} report={rep_}")
r, rep_, old_words, gone_words, deleted_words = handoff("", "", "nothing")
check("secret-scan e2e: no tips listed, none gone: empty files, empty variables, the scan starts",
      r.returncode == 0 and old_words == [] and gone_words == [] and deleted_words == [] and rep_.get("env_old") == "set"
      and rep_.get("env_old_words") == "0", f"exit {r.returncode}: {r.stderr.strip()[-300:]} report={rep_}")
(handoff_temp / "old-heads").write_text("\n".join(huge_ids[:5]) + "\n")  # a run stopped half-way left these
(handoff_temp / "scanned-gone").write_text(day_gone[0] + "\n")
(handoff_temp / "deleted-tips").write_text(day_gone[1] + "\n")
small = [sweep_main, *sweep_vanished]
r, rep_, old_words, gone_words, deleted_words = handoff(" ".join(small + small), "", "again")
check("secret-scan e2e: run again over an earlier or stopped run's files: only this run's lists are read",
      r.returncode == 0 and old_words == sorted(small) and gone_words == [] and deleted_words == [],
      f"exit {r.returncode}: {r.stderr.strip()[-300:]} old={old_words} gone={gone_words} deleted={deleted_words}")

check("secret-scan: the expression evaluator reads GitHub's rules (&& and || give an operand, == ignores case)",
      gh_eval("'a' && 'B' == 'b'", {}) and not gh_eval("'' || null", {}) and gh_eval("!x.y", {})
      and gh_eval("format('refs/heads/{0}', a.b) == 'REFS/heads/main'", {"a": {"b": "main"}})
      and not gh_eval("(true || false) && !true", {}))

# The scan job's token drop (#317 item 4): actions/checkout keeps the job's token in the repository's
# git config (persist-credentials stays on: the checkout rule above), and every origin fetch of the
# scan script would send it. The step right after the checkout takes it out (checkout v6: an
# includeIf.<gitdir>.path pointing at a file with http.<url>.extraheader; older ones: the header
# itself; the includes, the headers and the file under RUNNER_TEMP go), then fails unless no
# extraheader is left in any config git reads (includes followed).
# It must pass when there is nothing to remove (a re-run, an old checkout), so the listing's "no
# match" (exit 1) may not end the step; any other failure to read the config fails it.
TOKEN_DROP_NAME = "Drop the checkout's token from the git config"
TOKEN_DROP_RUN = """\
set -euo pipefail
{ git config --local --name-only --get-regexp '^(includeif\\..*\\.path|http\\..*\\.extraheader)$' || true; } |
  sort -u | while IFS= read -r key; do git config --local --unset-all "$key"; done
rm -f -- "${RUNNER_TEMP:?}"/git-credentials-*.config
rc=0
git config --get-regexp '\\.extraheader$' > /dev/null || rc=$?
if [[ "$rc" != 1 ]]; then
  echo "::error::Secret scan: the checkout's token is still in the git config (or the config cannot be read), so the scan's fetches would send it" >&2
  exit 2
fi
"""


def token_drop_problems(scan):
    problems = []
    for j, job in (scan.get("jobs") or {}).items():
        job_steps = job.get("steps") or []
        if not any("scan-from-default-branch" in str(s.get("run", "")) for s in job_steps):
            continue
        first = 1 if job_steps and record_step(job_steps[0]) == "read" else 0
        if len(job_steps) < first + 2 or not CHECKOUT_ACTION.fullmatch(str(job_steps[first].get("uses", ""))):
            problems.append(f"{SCAN_FILE}/{j}: the checkout is not the first step (but the record's read step)")
            continue
        step = job_steps[first + 1]
        if step.get("name") != TOKEN_DROP_NAME or str(step.get("run", "")).strip() != TOKEN_DROP_RUN.strip():
            problems.append(f"{SCAN_FILE}/{j}: the step right after the checkout is not `{TOKEN_DROP_NAME}` "
                            "in its exact form")
        if set(step) - {"name", "run"}:
            problems.append(f"{SCAN_FILE}/{j}: `{TOKEN_DROP_NAME}` has more than name and run "
                            f"({', '.join(sorted(set(step) - {'name', 'run'}))}): it could be skipped or fail quietly")
        if sum(TOKEN_DROP_NAME == s.get("name") for s in job_steps) != 1:
            problems.append(f"{SCAN_FILE}/{j}: not exactly one `{TOKEN_DROP_NAME}` step")
    if not any(any("scan-from-default-branch" in str(s.get("run", "")) for s in job.get("steps") or [])
               for job in (scan.get("jobs") or {}).values()):
        problems.append(f"{SCAN_FILE}: no job runs the scan")
    return problems


def token_drop_step_run(scan):
    for job in (scan.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if step.get("name") == TOKEN_DROP_NAME:
                return str(step.get("run", ""))
    return "exit 97"


HEADER = "AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46ZmFrZQ=="


def token_drop_e2e(run):
    """Runs the step's text as Actions runs it (bash -e -o pipefail) in checkouts laid out as
    actions/checkout leaves them; returns what went wrong."""
    problems = []
    case_dir = pathlib.Path(tempfile.mkdtemp(prefix="token-drop-", dir=work))

    def repo(label):
        dest = case_dir / label
        git(work, "init", "-q", str(dest))
        git(dest, "remote", "add", "origin", "https://github.com/me/t3code")
        return dest

    def step(dest, **env):
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run], cwd=dest,
                              env={**base_env, "RUNNER_TEMP": str(case_dir), **env}, capture_output=True, text=True)

    def headers(dest, **env):
        return subprocess.run(["git", "config", "--get-regexp", r"\.extraheader$"], cwd=dest,
                              env={**base_env, **env}, capture_output=True, text=True)

    # checkout v6: the header lives in a file of its own, which the repository's config includes for
    # its git dir (one entry for the runner's path, one for a container's).
    v6 = repo("v6")
    creds = case_dir / "git-credentials-1234.config"
    creds.write_text(f'[http "https://github.com/"]\n\textraheader = {HEADER}\n')
    git(v6, "config", "--local", f"includeIf.gitdir:{v6}/.git.path", str(creds))
    git(v6, "config", "--local", "includeIf.gitdir:/github/workspace/.git.path", "/github/runner_temp/" + creds.name)
    if headers(v6).returncode != 0:
        problems.append("fixture: the v6 layout's header is not seen through the include")
    r = step(v6)
    if r.returncode != 0 or headers(v6).returncode != 1 \
            or git(v6, "config", "--local", "--get-regexp", "^includeif", ok=False).returncode != 1:
        problems.append(f"v6 layout: the header (include) is not removed with exit 0 (exit {r.returncode}: {r.stderr.strip()})")
    if creds.exists():
        problems.append("v6 layout: the credentials file under RUNNER_TEMP is left")
    if git(v6, "config", "remote.origin.url") != "https://github.com/me/t3code":
        problems.append("v6 layout: the step changed more than the token (origin's URL)")
    # Run again (a re-run of the job's step, or the same step after a half-way stop): nothing left.
    r = step(v6)
    if r.returncode != 0 or r.stderr.strip():
        problems.append(f"re-run: nothing to remove does not pass quietly (exit {r.returncode}: {r.stderr.strip()})")
    # An older checkout: the header in the repository's own config, twice.
    old = repo("old")
    git(old, "config", "--local", "http.https://github.com/.extraheader", HEADER)
    git(old, "config", "--local", "--add", "http.https://github.com/.extraheader", HEADER)
    r = step(old)
    if r.returncode != 0 or headers(old).returncode != 1:
        problems.append(f"old layout: the header is not removed with exit 0 (exit {r.returncode}: {r.stderr.strip()})")
    # A checkout that kept no token at all.
    none = repo("none")
    r = step(none)
    if r.returncode != 0 or r.stderr.strip():
        problems.append(f"no header: does not pass quietly (exit {r.returncode}: {r.stderr.strip()})")
    # A header the step cannot remove (another config file): the closing check fails the step.
    elsewhere = case_dir / "global-with-header"
    elsewhere.write_text(f'[http "https://github.com/"]\n\textraheader = {HEADER}\n')
    r = step(repo("elsewhere"), GIT_CONFIG_GLOBAL=str(elsewhere))
    if r.returncode == 0 or "::error::" not in r.stderr:
        problems.append(f"a header left in another config: the step passes (exit {r.returncode})")
    # A config git cannot read: no proof the header is gone, so the step fails.
    broken = case_dir / "global-broken"
    broken.write_text("[http\n")
    r = step(repo("broken"), GIT_CONFIG_GLOBAL=str(broken))
    if r.returncode == 0 or "::error::" not in r.stderr:
        problems.append(f"an unreadable config: the step passes (exit {r.returncode})")
    return problems


check("secret-scan: the scan job drops the checkout's token right after the checkout, in its exact form",
      not token_drop_problems(real[SCAN_FILE]), "; ".join(token_drop_problems(real[SCAN_FILE])))
e2e_problems = token_drop_e2e(token_drop_step_run(real[SCAN_FILE]))
check("secret-scan e2e: the token drop removes checkout v6's include and an older checkout's header, passes "
      "quietly with nothing to remove (re-run, no header), fails on a header it cannot remove or an unreadable config",
      not e2e_problems, "; ".join(e2e_problems))


def token_drop_mutant(mutate):
    wfs = copy.deepcopy(real)
    try:
        mutate(wfs)
    except (KeyError, TypeError, IndexError, StopIteration):
        return False
    return bool(token_drop_problems(wfs[SCAN_FILE]))


def drop_step(wfs):
    return next(s for s in wfs[SCAN_FILE]["jobs"]["scan"]["steps"] if s.get("name") == TOKEN_DROP_NAME)


for label, mutate in {
    "no token drop": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].remove(drop_step(w)),
    "the token drop after the scan": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].append(
        w[SCAN_FILE]["jobs"]["scan"]["steps"].pop(1)),
    "a step between the checkout and the token drop": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].insert(
        1, {"name": "x", "run": "git fetch origin main"}),
    "a token drop that can be skipped (if:)": lambda w: drop_step(w).__setitem__("if", "${{ false }}"),
    "a token drop that may fail quietly": lambda w: drop_step(w).__setitem__("continue-on-error", True),
    "a token drop without its closing check": lambda w: drop_step(w).__setitem__(
        "run", drop_step(w)["run"].split("rc=0")[0]),
    "a token drop that keeps the include": lambda w: drop_step(w).__setitem__(
        "run", drop_step(w)["run"].replace("includeif\\..*\\.path|", "")),
}.items():
    check(f"secret-scan: {SCAN_FILE} with {label} fails", token_drop_mutant(mutate))

# The same broken forms, run: the e2e must see each (a check that stops seeing its problem goes red).
for label, text in {
    "no `|| true` (nothing to remove ends the step)": TOKEN_DROP_RUN.replace(" || true; }", "; }"),
    "no closing check": TOKEN_DROP_RUN.split("rc=0")[0],
    "a closing check that ignores read errors": TOKEN_DROP_RUN.replace('"$rc" != 1', '"$rc" == 0'),
    "the include kept": TOKEN_DROP_RUN.replace("includeif\\..*\\.path|", ""),
    "the old header kept": TOKEN_DROP_RUN.replace("|http\\..*\\.extraheader", ""),
    "no dedup (a header set twice)": TOKEN_DROP_RUN.replace("  sort -u | while", "  while"),
    "the credentials file kept": TOKEN_DROP_RUN.replace('rm -f -- "${RUNNER_TEMP:?}"/git-credentials-*.config\n', ""),
}.items():
    check(f"secret-scan e2e: a token drop with {label} fails", bool(token_drop_e2e(text)))

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

# --- result ------------------------------------------------------------------------------------
# Jobs fork_ci_result may leave out of its needs, each with its reason. None today: every job's
# failure must block a merge.
RESULT_OPTIONAL = {}


def result_problems(wf):
    jobs = wf.get("jobs") or {}
    if "fork_ci_result" not in jobs:
        return ["fork-tests.yml has no fork_ci_result job"]
    needs = jobs["fork_ci_result"].get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs
    problems = [f"fork_ci_result does not need {job}" for job in jobs
                if job != "fork_ci_result" and job not in RESULT_OPTIONAL and job not in needs]
    problems += [f"RESULT_OPTIONAL names {job}, which is not a job" for job in RESULT_OPTIONAL if job not in jobs]
    return problems


fork_tests = load(workflows_dir / "fork-tests.yml")
check("result: fork_ci_result needs every other job of fork-tests.yml",
      not result_problems(fork_tests), "; ".join(result_problems(fork_tests)))


def result_mutant(mutate):
    wf = copy.deepcopy(fork_tests)
    mutate(wf["jobs"])
    return result_problems(wf)


for label, mutate in {
    "a job dropped from its needs": lambda jobs: jobs["fork_ci_result"]["needs"].remove("changes"),
    "a new job left out of its needs": lambda jobs: jobs.update(extra={"runs-on": "ubuntu-24.04"}),
    "needs only one job, as a string": lambda jobs: jobs["fork_ci_result"].update(needs="changes"),
    "no fork_ci_result job": lambda jobs: jobs.pop("fork_ci_result"),
}.items():
    check(f"result: {label} fails", bool(result_mutant(mutate)))

# --- quiet ---------------------------------------------------------------------------------------
# fork_ci_result trusts each job's result, so nothing in fork-tests.yml may fail quietly: no continue-on-error on a
# job or a step (any value: an expression can turn it on for one matrix shard). A step that runs tests the fork
# relies on is held to an allowlist, not a blocklist: a blocklist of shell tricks keeps missing one (`sh -c '... ||
# true'`, `set -n`, a function named node, `python3 -m py_compile <test>`). The allowlist:
#   - its run is only lines (backslash continuations joined, whole comment lines skipped) of one TEST_FORMS shape;
#     the fork's own tests run only through the runner (a part, or `--run <test>` for the runs the test's own
#     `fork-ci: run` lines declare: as root, with its switches, one class), which records what each ran (#664);
#   - the step has only STEP_KEYS: shell only `bash`, env only STEP_ENV (a runner step always has the records folder),
#     an if: only STEP_IFS and only on a step that runs upstream's tests alone (the `test` job runs those in its first
#     shard); no working-directory;
#   - its job has only JOB_KEYS (no defaults, env, container or services), its if: is one of JOB_IFS, and no run step
#     of the job names GITHUB_ENV, GITHUB_PATH or BASH_ENV (each changes what a later step's command runs);
#   - the workflow has no defaults or env.
# Every test .github/fork-ci-tests.txt lists as run by fork-tests.yml is run whole by such a line, and every test with
# own runs (OWN_RUNS, read by the runner's ownRuns) by exactly one `--run` line. The runner's parts are read from the
# same accepted lines (YAML parsed, quotes removed as bash removes them), never from the file's text, so a call kept in
# a YAML comment or a step name counts for nothing. Anything else is refused with the allowed form in the message.
# This is the fast first check; the records (below) prove at run time what each listed fork test ran.
# Not checked here: what a step that is not a test step does to the next one's interpreter or files (beyond
# GITHUB_ENV, GITHUB_PATH and BASH_ENV); a test that needs a switch it does not declare skips, which its record shows.

# The tests .github/fork-ci-tests.txt lists, read by fork-ci-tests.cjs's own readList (which spells each `run by`
# path one way: `./a/b` is `a/b`), and those it says this file runs.
LISTED = json.loads(subprocess.run(
    ["node", "-e", "const fs = require('fs'); const { readList } = require(process.argv[1]); "
     "console.log(JSON.stringify(readList(fs.readFileSync(process.argv[2], 'utf8'))))",
     str(scripts_dir / "fork-ci-tests.cjs"), str(root / ".github/fork-ci-tests.txt")],
    check=True, capture_output=True, text=True).stdout)
RUN_BY_HERE = [e["file"] for e in LISTED if e["runBy"] == ".github/workflows/fork-tests.yml"]
LISTED_FILES = {e["file"] for e in LISTED}
# The fork's own tests among them (upstream's keep their if:, as in ci.yml).
FORK_OWN_TEST = re.compile(r"(?:fork-kit/|fork-ci/|\.github/scripts/fork-)\S*\.test\.\w+")
# A step whose run's code lines (comment lines skipped, as bash skips them) name any of these is a test step and must
# have the allowed shape. It is wide on purpose (a folder name alone, any test extension the fork uses): a step that
# runs a test by another spelling still names one. A name kept only in a comment line runs nothing, so it does not make
# a step a test step; a listed test named only there is still caught, as no test step runs it.
TEST_MENTION = re.compile(r"fork-ci-tests|fork-kit|fork-ci/|\.test\.(?:py|sh|cjs|mjs|js)\b")
RUNNER = ".github/scripts/fork-ci-tests.cjs"

# The VAR=value words a test line may start with (each at most once), exactly as written: the one value the heavy
# test's step sets for it (a new step would lose it). A test's own switches are not here: the test declares them in its
# `fork-ci: run` line and the runner sets them (#664), so no line can drop or misspell one.
ALLOWED_VARS = {'XDG_RUNTIME_DIR="/run/user/$(id -u)"'}
# The own runs of each listed test a part or fork-tests.yml runs, and the `fork-ci:` lines the runner cannot read, both
# by the runner's own ownRuns.
_OWN = json.loads(subprocess.run(
    ["node", "-e", "const fs = require('fs'); const path = require('path'); const r = require(process.argv[1]); "
     "const read = (f) => { try { return fs.readFileSync(path.join(process.argv[2], f), 'utf8'); } "
     "catch { return null; } }; "
     "console.log(JSON.stringify(r.readList(read('.github/fork-ci-tests.txt'))"
     ".filter((e) => !e.runBy || e.runBy === '.github/workflows/fork-tests.yml')"
     ".map((e) => [e.file, r.ownRuns(e, read(e.file))])))",
     str(scripts_dir / "fork-ci-tests.cjs"), str(root)],
    check=True, capture_output=True, text=True).stdout)
OWN_RUNS = {file: own["runs"] for file, own in _OWN if own["runs"]}
# The tests a run in a part may (and must) skip, by name, from each test's `fork-ci: may skip` lines (#664).
DECLARED_SKIPS = {file: own["skips"] for file, own in _OWN if own["skips"]}
DECLARATION_PROBLEMS = [p for _, own in _OWN for p in own["problems"]]
PY_FLAGS = {"-I", "-B", "-u"}
STEP_KEYS = {"name", "id", "run", "env", "if", "shell", "timeout-minutes"}
RECORDS_VAR, RECORDS_DIR = "FORK_CI_TEST_RECORDS", "${{ runner.temp }}/fork-test-records"
STEP_ENV = {"FORK_CI_TEST_SHARD": "${{ matrix.shard }}/${{ strategy.job-total }}", RECORDS_VAR: RECORDS_DIR}
STEP_IFS = {"matrix.shard == 1"}
JOB_KEYS = {"name", "if", "needs", "runs-on", "timeout-minutes", "strategy", "permissions", "steps"}
JOB_IFS = {"${{ " + PR_GUARD + " }}",
           "${{ " + PR_GUARD + " && (!cancelled() && needs.changes.outputs.test != 'false') }}"}
TEST_FORMS = (
    "a fork test step's run is only lines of these forms, each optionally ending in 2>&1, with no trailing # comment: "
    f"`[VAR=value ...] node {RUNNER} [<path prefix> | '!<path prefix>' ...]` (a part of the fork's tests), "
    f"`[VAR=value ...] node {RUNNER} --run <listed fork test>` (the runs its `fork-ci: run` lines declare: as root, "
    "with its switches, one class), and for upstream's tests only `python3 [-I] [-B] [-u] <listed .py test> "
    "[-v <TestClass>]` (no arguments after a test the list says fork-tests.yml runs: it runs whole), "
    "`node --test <listed .cjs/.mjs/.js test> ...` or `bash <listed .sh test>`; a test path written as listed (or "
    "with ./), VAR=value one of " + ", ".join(sorted(ALLOWED_VARS)) +
    f"; a step that calls the runner has env {RECORDS_VAR}: {RECORDS_DIR} (the records fork-ci-result checks)")

TOKEN = re.compile(r"""(?:[^\s'"]+|'[^']*'|"(?:[^"\\]|\\.)*")+""")
PLAIN = re.compile(r"""'([\w./!+-]+)'|"([\w./!+-]+)"|([\w./!+-]+)""")


def words(line):
    """A shell line's words as written (quotes kept), or None if a quote is not closed."""
    rest, out = line.strip(), []
    while rest:
        m = TOKEN.match(rest)
        if not m:
            return None
        out.append(m.group(0))
        rest = rest[m.end():].lstrip()
    return out


def plain(word):
    """A word's text if it is a plain path or name, quoted whole or not; None for anything a shell would expand,
    split or treat as an operator."""
    m = PLAIN.fullmatch(word)
    return next((g for g in m.groups() if g is not None), None) if m else None


def test_path(word):
    text = plain(word)
    return text[2:] if text and text.startswith("./") else text


def test_line(line):
    """The tests one shell line runs, as {"tests": [...], "partial": [...], "parts": [...], "own": test} (tests: run
    whole; partial: run with arguments that pick some of them; parts: the runner's arguments as bash hands them to it,
    or None when the line does not call the runner for a part; own: the test of a `--run` call, else None), or a string
    saying why the line is not one of the allowed forms."""
    w = words(line)
    if not w:
        return "a quote that is not closed"
    comment = next((i for i, word in enumerate(w) if word.startswith("#")), None)
    if comment is not None:
        return f"a trailing comment ({' '.join(w[comment:])}) is not allowed on a test line (a comment goes on a line " \
               "of its own)"
    if w[-1] == "2>&1":
        w = w[:-1]
    env = []
    while w and re.match(r"[A-Za-z_]\w*=", w[0]):
        if w[0] not in ALLOWED_VARS or w[0] in env:
            return f"{w[0]} is not an allowed VAR=value (or comes twice); a test's own switch goes in its " \
                   "`fork-ci: run with <NAME>_TESTS=value` line"
        env.append(w.pop(0))
    if not w:
        return "no command"
    cmd, args = w[0], w[1:]
    if cmd == "sudo":
        return "no sudo on a test line: a test that runs as root says so in its `fork-ci: run as root` line, and " \
               f"`{RUNNER} --run` runs it under sudo"
    parts = own = None
    if cmd == "python3":
        while args and args[0] in PY_FLAGS and args[0] not in args[1:]:
            args = args[1:]
        path = test_path(args[0]) if args else None
        if path not in LISTED_FILES or not path.endswith(".py"):
            return "python3 takes only -I, -B and -u, then a listed .py test"
        rest = args[1:]
        if rest and not (len(rest) == 2 and rest[0] == "-v" and re.fullmatch(r"[A-Z]\w*", rest[1] or "")):
            return f"after {path} only -v <TestClass>"
        found = {"tests": [path], "partial": []}
        if rest:
            found = {"tests": [], "partial": [path]}
    elif cmd == "node" and args and test_path(args[0]) == RUNNER and args[1:2] == ["--run"]:
        own = test_path(args[2]) if len(args) == 3 else None
        if own not in LISTED_FILES or not FORK_OWN_TEST.fullmatch(own):
            return f"{RUNNER} --run takes one listed fork test"
        found = {"tests": [own], "partial": []}
    elif cmd == "node" and args and test_path(args[0]) == RUNNER:
        for a in args[1:]:
            text = plain(a)
            if text is None or not re.fullmatch(r"!?[\w.][\w./-]*", text):
                return f"{RUNNER} takes only path prefixes (`fork-kit/`, '!fork-kit/') or --run <listed fork test>"
        found = {"tests": [RUNNER], "partial": []}
        parts = [plain(a) for a in args[1:]]
    elif cmd == "node" and args[:1] == ["--test"] and len(args) > 1:
        paths = [test_path(a) for a in args[1:]]
        if not all(p in LISTED_FILES and re.search(r"\.[cm]?js$", p) for p in paths):
            return "node --test takes only listed .cjs, .mjs or .js tests"
        found = {"tests": paths, "partial": []}
    elif cmd == "bash" and len(args) == 1 and (test_path(args[0]) or "").endswith(".sh") \
            and test_path(args[0]) in LISTED_FILES:
        found = {"tests": [test_path(args[0])], "partial": []}
    else:
        return f"`{cmd}` with these arguments is not a test command"
    if own is None and parts is None:
        for t in found["tests"] + found["partial"]:
            if FORK_OWN_TEST.fullmatch(t):
                return f"{t} is a fork test: only {RUNNER} runs it (in its part, or `--run {t}` for the runs its " \
                       "`fork-ci: run` lines declare), so that fork-ci-result sees what it ran"
    return {**found, "parts": parts, "own": own}


def step_runs(step):
    return joined_lines(str(step.get("run", "")))


def test_mention(step):
    """What in a step's run code lines (not its comment lines) makes it a test step, or None."""
    m = TEST_MENTION.search("\n".join(line for _, line in step_runs(step)))
    return m.group(0) if m else None


def is_test_step(step):
    return test_mention(step) is not None


def test_step_problems(step):
    """Why a test step is not of the allowed shape (empty: it is), the tests it runs whole, its runner part calls and the
    tests of its `--run` calls."""
    problems, ran, calls, owns = [], [], [], []
    for key in sorted(set(step) - STEP_KEYS):
        problems.append(f"{key}: is not allowed on a test step (only {', '.join(sorted(STEP_KEYS))})")
    if "shell" in step and step["shell"] != "bash":
        problems.append(f"shell: {step['shell']} (only bash, which keeps -e)")
    env = step.get("env") or {}
    for k, v in env.items():
        if STEP_ENV.get(k) != v:
            problems.append(f"env {k}: is not allowed on a test step (only {STEP_ENV})")
    lines = [(n, line) for n, line in step_runs(step) if line.strip()]
    if not lines:
        problems.append(f"names a test but runs none; {TEST_FORMS}")
    for n, line in lines:
        found = test_line(line)
        if isinstance(found, str):
            problems.append(f"line {n}: {found}; {TEST_FORMS}")
        else:
            ran += found["tests"]
            if found["parts"] is not None:
                calls.append(found["parts"])
            if found["own"] is not None:
                owns.append(found["own"])
    if (calls or owns) and env.get(RECORDS_VAR) != RECORDS_DIR:
        problems.append(f"calls the runner without env {RECORDS_VAR}: {RECORDS_DIR} (no records: fork-ci-result "
                        "would see none of its tests run)")
    if "if" in step:
        own = [t for t in ran if t == RUNNER or FORK_OWN_TEST.fullmatch(t)]
        if own or not ran or str(step["if"]) not in STEP_IFS:
            problems.append(f"if: {step['if']} (a step of the fork's own tests has none; upstream's may have only "
                            f"{', '.join(sorted(STEP_IFS))})")
    return problems, ran, calls, owns


# The runner calls each job makes, as read from its accepted test lines: one call per job (two in one job run one after
# the other). The kits sharded, the composite actions with the fleet walls (about two thirds of the non-kit script
# tests' time), the rest: each of the three jobs stays well under its time limit.
RUNNER_PARTS = {
    "workflows": [["!fork-kit/", "!fork-ci/", "!.github/scripts/fork-fleet-walls"]],
    "action_tests": [["fork-ci/", ".github/scripts/fork-fleet-walls"]],
    "kit_tests": [["fork-kit/"]],
}


def part_problems(calls_by_job):
    """Why the runner calls are not the pinned ones, or leave a listed or present test in no part or in two, by the
    runner's own inPart and presentTests."""
    problems = []
    if calls_by_job != RUNNER_PARTS:
        problems.append(f"fork-tests.yml: the runner calls by job are {calls_by_job}, not {RUNNER_PARTS}")
    calls = [parts for job_calls in calls_by_job.values() for parts in job_calls]
    wrong = json.loads(subprocess.run(
        ["node", "-e", "const fs = require('fs'); const r = require(process.argv[1]); "
         "const calls = JSON.parse(process.argv[3]); "
         "const listed = r.readList(fs.readFileSync(process.argv[2], 'utf8')).map((e) => e.file); "
         "const files = [...new Set([...listed, ...r.presentTests()])]; "
         "console.log(JSON.stringify(files.map((f) => [f, calls.filter((parts) => r.inPart(f, parts))])"
         ".filter(([, parts]) => parts.length !== 1)))",
         str(scripts_dir / "fork-ci-tests.cjs"), str(root / ".github/fork-ci-tests.txt"), json.dumps(calls)],
        check=True, capture_output=True, text=True).stdout)
    for file, parts in wrong:
        where = "; ".join(" ".join(p) or "all" for p in parts)
        problems.append(f"{file}: in {len(parts)} runner parts ({where}), not exactly one")
    return problems


def own_problems(owns, own_runs=None):
    """Why the `--run` calls (one test each) are not exactly one for each test with own runs."""
    own_runs = OWN_RUNS if own_runs is None else own_runs
    problems = []
    for t in sorted(set(own_runs) | set(owns)):
        if t not in own_runs:
            problems.append(f"{t}: run by `{RUNNER} --run` in fork-tests.yml, but it declares no own run (a part runs "
                            "it), so the runner refuses it")
        elif owns.count(t) != 1:
            problems.append(f"{t}: its own runs (`fork-ci: run` lines) are made by {owns.count(t)} `{RUNNER} --run` "
                            "lines in fork-tests.yml, not exactly one")
    return problems


def quiet_problems(wf, tests=RUN_BY_HERE, own_runs=None):
    problems, ran, calls_by_job, owns, record_jobs = [], set(), {}, [], set()
    for key in ("defaults", "env"):
        if key in wf:
            problems.append(f"fork-tests.yml: workflow-level {key}: is not allowed (it reaches every test step)")
    for j, job in (wf.get("jobs") or {}).items():
        if "continue-on-error" in job:
            problems.append(f"fork-tests.yml/{j}: continue-on-error on the job (its failure would pass)")
        job_steps = job.get("steps") or []
        for i, step in enumerate(job_steps):
            if "continue-on-error" in step:
                where = f"fork-tests.yml/{j} step {i + 1} ({step.get('name', 'unnamed')})"
                problems.append(f"{where}: continue-on-error (its failure would pass)")
        if not any(is_test_step(st) for st in job_steps):
            continue
        for key in sorted(set(job) - JOB_KEYS):
            problems.append(f"fork-tests.yml/{j}: {key}: is not allowed on a job with test steps (only "
                            f"{', '.join(sorted(JOB_KEYS))})")
        if job.get("if") not in JOB_IFS:
            problems.append(f"fork-tests.yml/{j}: if: {job.get('if')} (a job with test steps has one of "
                            f"{' or '.join(sorted(JOB_IFS))})")
        for i, step in enumerate(job_steps):
            where = f"fork-tests.yml/{j} step {i + 1} ({step.get('name', 'unnamed')})"
            if re.search(r"GITHUB_ENV|GITHUB_PATH|BASH_ENV", str(step.get("run", ""))):
                problems.append(f"{where}: names GITHUB_ENV, GITHUB_PATH or BASH_ENV in a job with test steps (it "
                                "changes what a later step's command runs)")
            mention = test_mention(step)
            if mention:
                found, step_ran, calls, step_owns = test_step_problems(step)
                problems += [f"{where}, a test step (its run names `{mention}`): {why}" for why in found]
                if not found:
                    ran.update(step_ran)
                    calls_by_job.setdefault(j, []).extend(calls)
                    owns += step_owns
                    if calls or step_owns:
                        record_jobs.add(j)
    calls_by_job = {j: calls for j, calls in calls_by_job.items() if calls}
    problems += part_problems(calls_by_job)
    problems += own_problems(owns, own_runs)
    problems += record_problems(wf, record_jobs)
    for t in tests:
        if t not in ran:
            problems.append(f"{t}: .github/fork-ci-tests.txt says fork-tests.yml runs it, but no test step runs it "
                            f"whole; {TEST_FORMS}")
    return problems


# --- records (#664) ------------------------------------------------------------------------------
# The quiet checks above read the workflow; the records prove at run time what each listed fork test ran. The runner
# (fork-ci-tests.cjs, RECORDS there) writes one file per call into FORK_CI_TEST_RECORDS: its keyed run (run id,
# attempt, job), the commit, the shard, every key the run owes (`all`), the keys this call owed and, per test run, its
# exit status and the tests its output says ran, skipped and failed. Each job that calls the runner (RECORD_JOBS) ends
# its runner steps with RECORD_UPLOAD: one artifact per job, leg and attempt (artifact names are unique in a run and
# kept across attempts, so no attempt, leg or job can write over another's), none uploaded fails the job. fork_ci_result
# downloads them all (RECORD_DOWNLOAD) and its RECORD_CHECK step, right after it, fails the run unless each job and leg
# uploaded records, the newest attempt of each is of this run, job, leg and commit, and each key of `all` is recorded
# exactly once with a passing run that ran a test and skipped exactly the tests its test declares (by name). The
# check's own script is run below on records the real runner writes. The records live 7 days: the run reads them
# within it, and a re-run attempt after that fails for want of them (red, never green).
RECORD_UPLOAD = {
    "name": "Upload the fork test records",
    "uses": "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "with": {"name": "fork-test-records-${{ github.job }}-${{ strategy.job-index }}-${{ github.run_attempt }}",
             "path": RECORDS_DIR + "/", "if-no-files-found": "error", "retention-days": 7},
}
RECORD_DOWNLOAD = {
    "name": "Download the fork test records",
    "uses": "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
    "with": {"pattern": "fork-test-records-*", "path": RECORDS_DIR},
}
RECORD_CHECK = "Require a record of every listed fork test"
RECORD_CHECK_ENV = {"RECORDS": RECORDS_DIR, "RUN_ID": "${{ github.run_id }}",
                    "RUN_ATTEMPT": "${{ github.run_attempt }}", "SHA": "${{ needs.changes.outputs.sha || github.sha }}"}
CHECK_START, CHECK_END = "python3 -I - <<'CHECK'\n", "\nCHECK\n"


def legs(job):
    """A job's number of legs: 1, or its shard matrix's size; None for another matrix."""
    matrix = (job.get("strategy") or {}).get("matrix")
    if matrix is None:
        return 1
    if not isinstance(matrix, dict) or set(matrix) != {"shard"} or not isinstance(matrix["shard"], list):
        return None
    return len(matrix["shard"])


def check_script(run):
    """The record check's Python, from its step's run, or None if the run is not exactly that heredoc."""
    run = str(run)
    if not run.startswith(CHECK_START) or not run.endswith(CHECK_END):
        return None
    body = run[len(CHECK_START):-len(CHECK_END)]
    return None if "\nCHECK\n" in body + "\n" or body.startswith("CHECK\n") else body + "\n"


# The files the runner needs beside the tests (its list, itself, the shim it runs each Python test through and the
# reporter each node test reports to), so a sparse checkout of a job that calls it must hold each (#664: a root job
# without the shim failed every Python test).
RUNNER_FILES = [".github/fork-ci-tests.txt", RUNNER, ".github/scripts/fork-ci-unittest.py",
                ".github/scripts/fork-ci-node-counts.cjs"]


def checks_out(step, path):
    """Whether a step's checkout (a non-cone sparse one: `/path`, `/dir/`, `/*`, `!...`) holds path; a step that is
    no checkout of this repository, or checks out the whole tree, holds it."""
    w = step.get("with") or {}
    if "actions/checkout" not in str(step.get("uses", "")) or w.get("repository") or w.get("path"):
        return True
    lines = [line.strip() for line in str(w.get("sparse-checkout") or "").splitlines() if line.strip()]
    if not lines:
        return True

    def covers(pattern):
        pattern = pattern.lstrip("/")
        return pattern in ("*", "**") or pattern == path or (pattern.endswith("/") and path.startswith(pattern))
    held = False
    for line in lines:
        if line.startswith("!"):
            held = held and not covers(line[1:])
        elif covers(line):
            held = True
    return held


def record_problems(wf, record_jobs):
    """Why the jobs that call the runner do not each upload their records as pinned, or fork_ci_result does not check
    them all as pinned."""
    jobs = wf.get("jobs") or {}
    problems, want = [], {}
    for j in sorted(record_jobs):
        job_steps = jobs[j].get("steps") or []
        n = legs(jobs[j])
        if n is None:
            problems.append(f"fork-tests.yml/{j}: a job that calls the runner has no matrix but `shard` (its legs)")
            continue
        want[j] = n
        uploads = [i for i, st in enumerate(job_steps) if st == RECORD_UPLOAD]
        near = [i for i, st in enumerate(job_steps) if "fork-test-records" in json.dumps(st.get("with") or {})]
        runner_steps = [i for i, st in enumerate(job_steps) if RUNNER in str(st.get("run", ""))]
        if len(uploads) != 1 or near != uploads:
            problems.append(f"fork-tests.yml/{j}: calls the runner, so it has exactly one records upload step, as "
                            f"pinned: {RECORD_UPLOAD}")
        elif uploads[0] < max(runner_steps):
            problems.append(f"fork-tests.yml/{j}: its records upload step comes before its last runner step")
        missing = [f for f in RUNNER_FILES for st in job_steps if not checks_out(st, f)]
        if missing:
            problems.append(f"fork-tests.yml/{j}: calls the runner, but its checkout leaves out {sorted(set(missing))} "
                            "(the runner's own files: its list, itself, fork-ci-unittest.py and fork-ci-node-counts.cjs)")
    result = jobs.get("fork_ci_result") or {}
    steps = result.get("steps") or []
    downloads = [i for i, st in enumerate(steps) if st == RECORD_DOWNLOAD]
    checks = [i for i, st in enumerate(steps) if st.get("name") == RECORD_CHECK]
    if len(downloads) != 1 or len(checks) != 1 or checks[0] != downloads[0] + 1:
        problems.append(f"fork-tests.yml/fork_ci_result: no pinned records download ({RECORD_DOWNLOAD}) right "
                        f"before one {RECORD_CHECK!r} step")
        return problems
    st = steps[checks[0]]
    env = dict(st.get("env") or {})
    try:
        record_jobs_env = json.loads(env.pop("RECORD_JOBS", "null"))
    except ValueError:
        record_jobs_env = None
    if set(st) != {"name", "env", "run"} or env != RECORD_CHECK_ENV:
        problems.append(f"fork-tests.yml/fork_ci_result: the {RECORD_CHECK!r} step is not only its name, its run and "
                        f"env {RECORD_CHECK_ENV} with RECORD_JOBS")
    if record_jobs_env != want:
        problems.append(f"fork-tests.yml/fork_ci_result: RECORD_JOBS is {record_jobs_env}, not {want} (each job that "
                        "calls the runner, with its legs)")
    if check_script(st.get("run")) is None:
        problems.append(f"fork-tests.yml/fork_ci_result: the {RECORD_CHECK!r} step's run is not only the python3 "
                        "heredoc (CHECK)")
    return problems


check("quiet: no job or step of fork-tests.yml may fail quietly, every test step has the allowed shape, and each "
      "test listed as run by fork-tests.yml is run whole by one",
      not quiet_problems(fork_tests), "; ".join(quiet_problems(fork_tests)))
check("quiet: the check sees the fork's test steps (script tests, actions, kit shards, kit as root, lint) and upstream's",
      {j for j, job in fork_tests["jobs"].items() for st in job.get("steps") or []
       if is_test_step(st)} >= {"workflows", "action_tests", "kit_tests", "kit_root_tests", "lint", "test"})
check("quiet: the list's run-by-fork-tests.yml entries are read (the kit-as-root ones, the owned-reads one, "
      "upstream's release checks)",
      {"fork-kit/gh-apps/tests/root.test.py", "fork-kit/heavy/tests/heavy.test.py",
       ".github/scripts/fork-owned-reads.test.py", ".github/scripts/test-delta.test.cjs"} <= set(RUN_BY_HERE))

def quiet_mutant(mutate, whole=None, own_runs=None):
    wf = copy.deepcopy(fork_tests)
    (whole or (lambda w: mutate(w["jobs"])))(wf)
    return quiet_problems(wf, own_runs=own_runs)


HEAVY = "fork-kit/heavy/tests/heavy.test.py"
ROOT_TEST = "fork-kit/gh-apps/tests/root.test.py"
KIT = f"node {RUNNER} fork-kit/"


def heavy_step(jobs):
    return test_step(jobs, "kit_root_tests", HEAVY)


def with_heavy(jobs, **step):
    """The heavy kit step changed: its run line (the last) rewritten by `run` (a function of it), other keys set."""
    st = heavy_step(jobs)
    lines = st["run"].rstrip("\n").split("\n")
    lines[-1] = step.pop("run", lambda line: line)(lines[-1])
    st["run"] = "\n".join(lines) + "\n"
    st.update(step)


def test_step(jobs, job, needle):
    return next(st for st in jobs[job]["steps"] if needle in str(st.get("run", "")))


def kit_run(run):
    return lambda jobs: test_step(jobs, "kit_tests", RUNNER).__setitem__("run", run)


def heavy_run(run):
    return lambda jobs: with_heavy(jobs, run=run)


def job_set(job, key, value):
    return lambda jobs: jobs[job].__setitem__(key, value)


UPSTREAM_PY = "stage-preview-bundle.test.py"
FLEET_TEST = ".github/scripts/fork-fleet.test.py"


def upstream_py_run(run):
    """Upstream's .py test step's run rewritten by `run` (a function of it)."""
    return lambda jobs: (lambda st: st.__setitem__("run", run(st["run"])))(test_step(jobs, "test", UPSTREAM_PY))


def without_step(job, needle):
    return lambda jobs: jobs[job]["steps"].remove(test_step(jobs, job, needle))


def upload_step(jobs, job):
    return next(st for st in jobs[job]["steps"] if "upload-artifact" in str(st.get("uses", "")))


def upload_set(job, key, value):
    return lambda jobs: upload_step(jobs, job)["with"].__setitem__(key, value)


def without_step_uses(job, needle):
    return lambda jobs: jobs[job]["steps"].remove(upload_step(jobs, job))


def sparse_edit(jobs, job, edit):
    st = next(st for st in jobs[job]["steps"] if "actions/checkout" in str(st.get("uses", "")))
    st["with"]["sparse-checkout"] = "\n".join(edit(st["with"]["sparse-checkout"].splitlines())) + "\n"


def move_upload_before(jobs, job, needle):
    steps = jobs[job]["steps"]
    upload = steps.pop(steps.index(upload_step(jobs, job)))
    steps.insert(steps.index(test_step(jobs, job, needle)), upload)


def result_step(jobs, name):
    return next(st for st in jobs["fork_ci_result"]["steps"] if st.get("name") == name)


HEAVY_LINE = next(line for _, line in step_runs(heavy_step(fork_tests["jobs"])) if HEAVY in line)
ROOT_LINE = next(line for _, line in step_runs(test_step(fork_tests["jobs"], "kit_root_tests", ROOT_TEST)))
QUIET_REFUSED = {
    "a kit shard's test step skipped in one shard": lambda jobs: test_step(jobs, "kit_tests", RUNNER)
        .__setitem__("if", "matrix.shard != 3"),
    "the kit tests' job with continue-on-error": job_set("kit_tests", "continue-on-error", True),
    "one kit shard with continue-on-error": job_set("kit_tests", "continue-on-error", "${{ matrix.shard == 3 }}"),
    "the kit tests as root with continue-on-error": job_set("kit_root_tests", "continue-on-error", True),
    "a root kit test step with continue-on-error": lambda jobs: test_step(jobs, "kit_root_tests", ROOT_TEST)
        .__setitem__("continue-on-error", True),
    "a root kit test step under an if:": lambda jobs: test_step(jobs, "kit_root_tests", ROOT_TEST)
        .__setitem__("if", "github.event_name == 'push'"),
    "the script tests' step under an if:": lambda jobs: test_step(jobs, "workflows", RUNNER)
        .__setitem__("if", "always()"),
    "an upstream test step under another if:": lambda jobs: test_step(jobs, "test", "stage-preview-bundle.test.py")
        .__setitem__("if", "always()"),
    "a step with continue-on-error: false (any value)": lambda jobs: jobs["lint"]["steps"][0].__setitem__(
        "continue-on-error", False),
    "a non-test step of the build job with continue-on-error": lambda jobs: jobs["build"]["steps"][0].__setitem__(
        "continue-on-error", True),
    # The first review's dodge: the test run from its folder under an if:, its path only in a comment.
    "the heavy test run after a cd, under an if:, its path in a comment": lambda jobs: with_heavy(
        jobs, run=lambda line: f"# {HEAVY}\ncd fork-kit/heavy && HEAVY_REAL_TESTS=1 python3 -I -B tests/heavy.test.py",
        **{"if": "github.event_name == 'push'"}),
    "the heavy test run from a working-directory, under an if:": lambda jobs: with_heavy(
        jobs, run=lambda line: f"HEAVY_REAL_TESTS=1 python3 -I -B tests/heavy.test.py  # {HEAVY}",
        **{"if": "github.event_name == 'push'", "working-directory": "fork-kit/heavy"}),
    "the heavy test run after a cd, under an if:, its path only in a YAML comment (gone once parsed)":
        lambda jobs: with_heavy(jobs, run=lambda line: "cd fork-kit/heavy\nHEAVY_REAL_TESTS=1 python3 -I -B "
                                "tests/heavy.test.py", **{"if": "github.event_name == 'push'"}),
    "the heavy test run after a cd (no if:)": heavy_run(
        lambda line: "cd fork-kit/heavy\nHEAVY_REAL_TESTS=1 python3 -I -B tests/heavy.test.py"),
    "a cd /tmp, then the heavy test by its full path": heavy_run(lambda line: "cd /tmp\n" + line),
    "a pushd, then the heavy test by its full path": heavy_run(lambda line: "pushd /tmp\n" + line),
    "the heavy test's path only echoed": heavy_run(lambda line: f"echo {HEAVY}"),
    "the heavy test with a working-directory and its full path": lambda jobs: with_heavy(
        jobs, **{"working-directory": "."}),
    "a listed root kit test no step runs": lambda jobs: test_step(jobs, "kit_root_tests", ROOT_TEST).__setitem__(
        "run", f"echo {ROOT_TEST}"),
    "an upstream test the list says this file runs, run by no step": lambda jobs: test_step(
        jobs, "test", "stage-preview-bundle.test.py").__setitem__("run", "true"),
    # A status hidden after the test, or the test not reached.
    "a kit shard's tests || true": kit_run(f"{KIT} || true"),
    "a kit shard's tests || exit 0 on a continued line": kit_run(f"node {RUNNER} \\\n  fork-kit/ \\\n  || exit 0\n"),
    "a kit shard's tests under shell: bash {0}, then true": lambda jobs: test_step(jobs, "kit_tests", RUNNER).update(
        shell="bash {0}", run=f"{KIT}\ntrue\n"),
    "a kit shard's tests under shell: sh": lambda jobs: test_step(jobs, "kit_tests", RUNNER).update(shell="sh"),
    "a kit shard's tests; true": kit_run(f"{KIT}; true"),
    "a kit shard's tests;true (no space)": kit_run(f"{KIT};true"),
    "a kit shard's tests && true": kit_run(f"{KIT} && true"),
    "a kit shard's tests as an if: condition": kit_run(f"if {KIT}; then :; fi"),
    "the heavy test's quoted path; true": heavy_run(lambda line: line.replace(HEAVY, f'"{HEAVY}"') + "; true"),
    "a kit shard's tests negated with !": kit_run(f"! {KIT}"),
    "a kit shard's tests piped into tee": kit_run(f"{KIT} | tee log"),
    "a kit shard's tests in the background, then wait": kit_run(f"{KIT} &\nwait\n"),
    "a heavy step with set +e before its test": heavy_run(lambda line: "set +e\n" + line),
    "a heavy step with set +o errexit before its test": heavy_run(lambda line: "set +o errexit\n" + line),
    "a heavy step with a trap that exits 0": heavy_run(lambda line: "trap 'exit 0' EXIT\n" + line),
    "the heavy test || true": heavy_run(lambda line: line + " || true"),
    "the root kit test under shell: bash {0}": lambda jobs: test_step(jobs, "kit_root_tests", ROOT_TEST)
        .__setitem__("shell", "bash {0}"),
    # The second round's dodges (both reviews of the blocklist): the test inside a quoted string,
    "sh -c '<kit tests> || true'": kit_run(f"sh -c '{KIT} || true'"),
    "bash -c '<kit tests> || true'": kit_run(f"bash -c '{KIT} || true'"),
    "eval \"<kit tests> || true\"": kit_run(f'eval "{KIT} || true"'),
    # the shell or folder set for the job or the whole workflow,
    "job-level defaults.run.shell: bash {0}, then true": lambda jobs: (
        jobs["kit_tests"].__setitem__("defaults", {"run": {"shell": "bash {0}"}}),
        kit_run(f"{KIT}\ntrue")(jobs)),
    "job-level defaults.run.working-directory": job_set("kit_root_tests", "defaults",
                                                        {"run": {"working-directory": "fork-kit/heavy"}}),
    "job-level env: NODE_OPTIONS": job_set("kit_tests", "env", {"NODE_OPTIONS": "--require=/tmp/x.js"}),
    "a container for the job": job_set("kit_tests", "container", "ubuntu:24.04"),
    "a job-level if: that skips the job": job_set("kit_root_tests", "if", "${{ false }}"),
    # lines around the test that stop it running or hide its status,
    "exit 0 before the test": heavy_run(lambda line: "exit 0\n" + line),
    "set -n before the test (reads the rest, runs none of it)": heavy_run(lambda line: "set -n\n" + line),
    "the test inside if false; then ... fi": heavy_run(lambda line: f"if false; then\n{line}\nfi"),
    "the test inside while false; do ... done": heavy_run(lambda line: f"while false; do\n{line}\ndone"),
    "the test inside a case that never matches": heavy_run(lambda line: f"case x in\ny) {line} ;;\nesac"),
    "the test in a heredoc to :": heavy_run(lambda line: f": <<'EOF'\n{line}\nEOF"),
    "set -x +e before the test": heavy_run(lambda line: "set -x +e\n" + line),
    "shopt -uo errexit before the test, true after": heavy_run(lambda line: f"shopt -uo errexit\n{line}\ntrue"),
    "a function named python3 before the test": heavy_run(lambda line: "python3() { :; }\n" + line),
    "an alias node=true before the kit tests": kit_run(f"shopt -s expand_aliases\nalias node=true\n{KIT}"),
    # arguments that run nothing,
    "python3 -c pass <upstream .py test>": upstream_py_run(lambda run: run.replace("python3 -B", "python3 -c pass")),
    "python3 -c 'import sys; sys.exit(0)' <upstream .py test>": upstream_py_run(
        lambda run: run.replace("python3 -B", "python3 -c 'import sys; sys.exit(0)'")),
    "python3 -m py_compile <upstream .py test>": upstream_py_run(
        lambda run: run.replace("python3 -B", "python3 -m py_compile")),
    "<upstream .py test> --help": upstream_py_run(lambda run: run.rstrip("\n") + " --help"),
    "<heavy test's own run> --help": heavy_run(lambda line: line + " --help"),
    "<heavy test's own run> with one test class picked": heavy_run(lambda line: line + " -v SomeClass"),
    "--run with two tests": heavy_run(lambda line: line + f" {ROOT_TEST}"),
    "--run of an upstream test": heavy_run(lambda line: line.replace(HEAVY, ".github/scripts/test-delta.test.cjs")),
    "node --check <runner> fork-kit/": kit_run(f"node --check {RUNNER} fork-kit/"),
    "node -e 0 <runner> fork-kit/": kit_run(f"node -e 0 {RUNNER} fork-kit/"),
    "the runner with --help": kit_run(f"{KIT} --help"),
    "node --test <upstream test> --test-only": lambda jobs: test_step(jobs, "test", "test-delta.test.cjs")
        .__setitem__("run", "node --test .github/scripts/test-delta.test.cjs --test-only"),
    # what the command runs changed through its environment,
    "NODE_OPTIONS=--require=... before the kit tests": kit_run(f"NODE_OPTIONS=--require=/tmp/x.js {KIT}"),
    "PATH=/tmp/f:$PATH before the heavy test": heavy_run(lambda line: "PATH=/tmp/f:$PATH " + line),
    "step env: BASH_ENV": lambda jobs: heavy_step(jobs).__setitem__("env", {"BASH_ENV": "/tmp/x"}),
    "step env: NODE_OPTIONS": lambda jobs: test_step(jobs, "kit_tests", RUNNER)["env"].__setitem__(
        "NODE_OPTIONS", "--require=/tmp/x.js"),
    "step env: another FORK_CI_TEST_SHARD": lambda jobs: test_step(jobs, "kit_tests", RUNNER)["env"].__setitem__(
        "FORK_CI_TEST_SHARD", "1/1"),
    "an earlier step that writes NODE_OPTIONS to GITHUB_ENV": lambda jobs: jobs["kit_tests"]["steps"].insert(
        1, {"run": 'echo "NODE_OPTIONS=--require=/tmp/x.js" >> "$GITHUB_ENV"'}),
    "an earlier step that puts a folder on GITHUB_PATH": lambda jobs: jobs["kit_root_tests"]["steps"].insert(
        1, {"run": 'echo /tmp/f >> "$GITHUB_PATH"'}),
    # sudo anywhere: a test that runs as root says so itself (`fork-ci: run as root`), and the runner runs it so,
    "sudo env before the kit runner": kit_run(f"sudo env {KIT}"),
    "sudo env before a listed .sh test run by bash": kit_run(f"{KIT}\nsudo env bash .github/scripts/fork-follow.test.sh"),
    "sudo env before node --test <upstream tests>": lambda jobs: (lambda st: st.__setitem__(
        "run", "sudo env " + st["run"]))(test_step(jobs, "test", "test-delta.test.cjs")),
    "sudo before the root test's own run": lambda jobs: test_step(jobs, "kit_root_tests", ROOT_TEST).__setitem__(
        "run", f"sudo -E {ROOT_LINE}"),
    # a fork test run by hand, so nothing records what it ran (the pre-#664 lines),
    "the fleet launcher run directly as root": lambda jobs: test_step(jobs, "workflows", FLEET_TEST).__setitem__(
        "run", f"sudo env FORK_FLEET_ROOT_TESTS=1 python3 -B {FLEET_TEST} -v LauncherAsRoot"),
    "the root test run directly as root": lambda jobs: test_step(jobs, "kit_root_tests", ROOT_TEST).__setitem__(
        "run", f'sudo env GH_APPS_ROOT_TESTS=1 GITHUB_ACTIONS="$GITHUB_ACTIONS" python3 -I -B {ROOT_TEST}'),
    "the heavy test run directly": heavy_run(
        lambda line: f'XDG_RUNTIME_DIR="/run/user/$(id -u)" python3 -I -B {HEAVY}'),
    "the owned-reads test run directly": lambda jobs: test_step(jobs, "lint", "fork-owned-reads.test.py").__setitem__(
        "run", "python3 -I -B .github/scripts/fork-owned-reads.test.py"),
    "a listed fork .sh test run by bash": kit_run(f"{KIT}\nbash .github/scripts/fork-follow.test.sh"),
    "a fork .cjs test run by node --test": kit_run(f"{KIT}\nnode --test .github/scripts/fork-ci-tests.test.cjs"),
    # a switch set on the line, not declared by the test (the test's own line is what the runner reads),
    "HEAVY_REAL_TESTS=1 before the heavy test's own run": heavy_run(lambda line: "HEAVY_REAL_TESTS=1 " + line),
    # an own run made twice, or not at all,
    "the heavy test's own run made twice": heavy_run(lambda line: f"{line}\n{line}"),
    "the fleet launcher's own run dropped": without_step("workflows", FLEET_TEST),
    "the owned-reads test's own run dropped": without_step("lint", "fork-owned-reads.test.py"),
    # the records not written or not uploaded,
    "the kit shards' runner step without the records folder": lambda jobs: test_step(jobs, "kit_tests", RUNNER)[
        "env"].pop(RECORDS_VAR),
    "the root test's own run without the records folder": lambda jobs: test_step(
        jobs, "kit_root_tests", ROOT_TEST).pop("env"),
    "the records folder elsewhere": lambda jobs: test_step(jobs, "kit_tests", RUNNER)["env"].__setitem__(
        RECORDS_VAR, "/tmp/r"),
    "the kit shards' records upload dropped": without_step_uses("kit_tests", "upload-artifact"),
    "the lint job's records upload dropped": without_step_uses("lint", "upload-artifact"),
    "a records upload that may find no files": upload_set("kit_tests", "if-no-files-found", "warn"),
    "a records upload named without the attempt": upload_set(
        "kit_tests", "name", "fork-test-records-${{ github.job }}-${{ strategy.job-index }}"),
    "a records upload named without the leg": upload_set(
        "kit_tests", "name", "fork-test-records-${{ github.job }}-${{ github.run_attempt }}"),
    "a records upload of another folder": upload_set("workflows", "path", "/tmp/r/"),
    "a records upload under an if:": lambda jobs: upload_step(jobs, "kit_root_tests").__setitem__("if", "success()"),
    "a records upload before the heavy test's own run": lambda jobs: move_upload_before(jobs, "kit_root_tests", HEAVY),
    "a second records upload in a job": lambda jobs: jobs["lint"]["steps"].append(copy.deepcopy(
        upload_step(jobs, "lint"))),
    "the kit-as-root checkout without the unittest shim": lambda jobs: sparse_edit(
        jobs, "kit_root_tests", lambda lines: [x for x in lines if "fork-ci-unittest" not in x]),
    "the kit-as-root checkout without the node reporter": lambda jobs: sparse_edit(
        jobs, "kit_root_tests", lambda lines: [x for x in lines if "fork-ci-node-counts" not in x]),
    "the kit-as-root checkout without the runner": lambda jobs: sparse_edit(
        jobs, "kit_root_tests", lambda lines: [x for x in lines if "fork-ci-tests.cjs" not in x]),
    "a kit shard checkout that drops the scripts folder": lambda jobs: sparse_edit(
        jobs, "kit_tests", lambda lines: lines + ["!/.github/scripts/"]),
    "the lint checkout that drops the shim": lambda jobs: sparse_edit(
        jobs, "lint", lambda lines: lines + ["!/.github/scripts/fork-ci-unittest.py"]),
    "a kit shard matrix with another key": lambda jobs: jobs["kit_tests"]["strategy"]["matrix"].__setitem__(
        "os", ["a", "b"]),
    "a fourth kit shard RECORD_JOBS does not know": lambda jobs: jobs["kit_tests"]["strategy"]["matrix"]["shard"]
        .append(4),
    # and fork-ci-result not checking them.
    "fork_ci_result without the records download": lambda jobs: jobs["fork_ci_result"]["steps"].remove(
        result_step(jobs, "Download the fork test records")),
    "fork_ci_result without the records check": lambda jobs: jobs["fork_ci_result"]["steps"].remove(
        result_step(jobs, RECORD_CHECK)),
    "the records check under an if:": lambda jobs: result_step(jobs, RECORD_CHECK).__setitem__("if", "failure()"),
    "the records check with continue-on-error": lambda jobs: result_step(jobs, RECORD_CHECK).__setitem__(
        "continue-on-error", True),
    "the records check before the download": lambda jobs: (lambda steps: steps.insert(
        1, steps.pop(steps.index(result_step(jobs, RECORD_CHECK)))))(jobs["fork_ci_result"]["steps"]),
    "the records download of one artifact": lambda jobs: result_step(jobs, "Download the fork test records")[
        "with"].__setitem__("pattern", "fork-test-records-workflows-*"),
    "RECORD_JOBS without lint": lambda jobs: result_step(jobs, RECORD_CHECK)["env"].__setitem__(
        "RECORD_JOBS", '{"workflows": 1, "action_tests": 1, "kit_tests": 3, "kit_root_tests": 1}'),
    "RECORD_JOBS with two kit shards": lambda jobs: result_step(jobs, RECORD_CHECK)["env"].__setitem__(
        "RECORD_JOBS", '{"workflows": 1, "action_tests": 1, "kit_tests": 2, "kit_root_tests": 1, "lint": 1}'),
    "the records check's SHA from the event alone (a release run tests another)": lambda jobs: result_step(
        jobs, RECORD_CHECK)["env"].__setitem__("SHA", "${{ github.sha }}"),
    "the records check's SHA from the release ref alone (empty on a pull request)": lambda jobs: result_step(
        jobs, RECORD_CHECK)["env"].__setitem__("SHA", "${{ needs.changes.outputs.sha }}"),
    "the records check's attempt fixed at 1": lambda jobs: result_step(jobs, RECORD_CHECK)["env"].__setitem__(
        "RUN_ATTEMPT", "1"),
    "the records check || true": lambda jobs: result_step(jobs, RECORD_CHECK).__setitem__(
        "run", result_step(jobs, RECORD_CHECK)["run"] + "true\n"),
    "the records check after set +e": lambda jobs: result_step(jobs, RECORD_CHECK).__setitem__(
        "run", "set +e\n" + result_step(jobs, RECORD_CHECK)["run"]),
    "a test path with a glob": heavy_run(lambda line: line.replace(HEAVY, "fork-kit/heavy/tests/*.test.py")),
    "a quote left open": heavy_run(lambda line: line + " '"),
    "a runner part that starts with -": kit_run(f"node {RUNNER} --x"),
    "a test line written to a log, not shown": heavy_run(lambda line: line + " > log 2>&1"),
    # The runner's parts read from the accepted lines: the kit cut to one folder (the shards still each get a file).
    "the kit runner with ./, cut to fork-kit/pc-agents/": kit_run(f"node ./{RUNNER} fork-kit/pc-agents/"),
    "the kit runner quoted, cut to fork-kit/pc-agents/": kit_run(f"node '{RUNNER}' 'fork-kit/pc-agents/'"),
    "a second runner call in the kit job": kit_run(f"{KIT}\n{KIT}"),
    "the script tests' runner without its !fork-ci/ part (the action tests run twice)": lambda jobs: test_step(
        jobs, "workflows", RUNNER).__setitem__("run", f"node {RUNNER} '!fork-kit/' '!.github/scripts/fork-fleet-walls'"),
}
QUIET_WHOLE_REFUSED = {
    "workflow-level defaults.run.shell: bash {0}": lambda wf: wf.__setitem__("defaults", {"run": {"shell": "bash {0}"}}),
    "workflow-level defaults.run.working-directory": lambda wf: wf.__setitem__(
        "defaults", {"run": {"working-directory": "fork-kit/heavy"}}),
    "workflow-level env: PATH": lambda wf: wf.__setitem__("env", {"PATH": "/tmp/f:/usr/bin"}),
}
for label, mutate in QUIET_REFUSED.items():
    try:  # a mutant whose step is gone (a workflow from before it) is a named FAIL, not a traceback
        found = quiet_mutant(mutate)
    except (KeyError, IndexError, StopIteration, TypeError, AttributeError) as error:
        found = None
        check(f"quiet: {label} fails", False, f"the mutant could not be made: {type(error).__name__}: {error}")
    if found is not None:
        check(f"quiet: {label} fails", bool(found), "")
for label, mutate in QUIET_WHOLE_REFUSED.items():
    check(f"quiet: {label} fails", bool(quiet_mutant(None, whole=mutate)))
# A refused line says what is allowed (the form), so the fix is plain from the message alone.
check("quiet: a refused test line names the allowed forms",
      any(TEST_FORMS in p for p in quiet_mutant(QUIET_REFUSED["sh -c '<kit tests> || true'"])))
# The same cut kit call with the whole call kept where a raw-text reader would see it but YAML and bash do not: in a
# YAML comment after the run value, or in the step's name. Made on the workflow's text and parsed, as Actions reads it.
fork_tests_text = (workflows_dir / "fork-tests.yml").read_text()
KIT_TEXT = f"        run: {KIT}\n"
QUIET_TEXT_REFUSED = {
    "the kit runner with ./ cut to fork-kit/pc-agents/, the whole call in a YAML comment": (
        KIT_TEXT, f"        run: node ./{RUNNER} fork-kit/pc-agents/ # {KIT}\n"),
    "the kit runner with ./ cut to fork-kit/pc-agents/, the whole call in the step's name": (
        f"      - name: Fork kit tests\n", f"      - name: Fork kit tests {KIT}\n",
        KIT_TEXT, f"        run: node ./{RUNNER} fork-kit/pc-agents/\n"),
    "the kit runner cut to fork-kit/pc-agents/, the whole call on a comment line of the run": (
        KIT_TEXT, f"        run: |\n          # {KIT}\n          node {RUNNER} fork-kit/pc-agents/\n"),
}
check("quiet: the kit step's text is as the text mutants expect", fork_tests_text.count(KIT_TEXT) == 1
      and fork_tests_text.count("      - name: Fork kit tests\n") == 1)
for label, pairs in QUIET_TEXT_REFUSED.items():
    text = fork_tests_text
    for old, new in zip(pairs[::2], pairs[1::2]):
        text = text.replace(old, new)
    found = quiet_problems(yaml.safe_load(text))
    check(f"quiet: {label} fails", text != fork_tests_text and any("runner parts" in p for p in found),
          "; ".join(found))
check("quiet: the real runner calls are the pinned ones, read from the accepted lines",
      not part_problems(RUNNER_PARTS) and any(st.get("run", "").strip() == KIT
                                              for st in fork_tests["jobs"]["kit_tests"]["steps"]))
# A refusal names the right reason.
REASONS = {
    "a trailing # comment on the kit runner line": (
        kit_run(f"{KIT}  # shard"), "a trailing comment (# shard) is not allowed"),
    "sudo env before the kit runner": (QUIET_REFUSED["sudo env before the kit runner"], "no sudo on a test line"),
    "the heavy test run directly": (QUIET_REFUSED["the heavy test run directly"],
                                    f"{HEAVY} is a fork test: only {RUNNER} runs it"),
    "a switch on the heavy test's line": (QUIET_REFUSED["HEAVY_REAL_TESTS=1 before the heavy test's own run"],
                                          "a test's own switch goes in its `fork-ci: run with"),
    "a runner step without the records folder": (
        QUIET_REFUSED["the kit shards' runner step without the records folder"],
        f"calls the runner without env {RECORDS_VAR}", False),
    "a set-up step named a test step by its code": (
        lambda jobs: jobs["kit_root_tests"]["steps"].insert(1, {"run": "cp fork-kit/heavy/x /tmp/x"}),
        "a test step (its run names `fork-kit`)"),
    "a test step that runs nothing": (kit_run(f"echo {KIT}"), "`echo` with these arguments is not a test command"),
}
# Each names the allowed forms too, but for the records folder (its own message says what is missing).
for label, (mutate, reason, *forms) in REASONS.items():
    try:
        found = quiet_mutant(mutate)
    except (KeyError, IndexError, StopIteration, TypeError, AttributeError) as error:
        found = [f"the mutant could not be made: {type(error).__name__}: {error}"]
    check(f"quiet: {label} is refused, saying why",
          any(reason in p and (TEST_FORMS in p or forms == [False]) for p in found),
          "; ".join(found))
check("quiet: a step that runs nothing but names a test is refused with the allowed forms",
      any("names a test but runs none; " + TEST_FORMS in p for p in test_step_problems({"run": "# x\n\n"})[0]))
check("quiet: the heavy test as its step runs it today is seen as run whole, and its step as a test step",
      test_line(HEAVY_LINE) == {"tests": [HEAVY], "partial": [], "parts": None, "own": HEAVY}
      and is_test_step(heavy_step(fork_tests["jobs"])))
QUIET_ACCEPTED = {
    "shell: bash and a runner line split by \\ with a quoted part": lambda jobs: test_step(
        jobs, "kit_tests", RUNNER).update(shell="bash", run=f"node {RUNNER} \\\n  'fork-kit/'\n"),
    "the heavy test's path quoted": heavy_run(lambda line: line.replace(HEAVY, f'"{HEAVY}"')),
    "the heavy test's path with ./": heavy_run(lambda line: line.replace(HEAVY, f"./{HEAVY}")),
    "the runner with ./ and 2>&1": kit_run(f"node ./{RUNNER} fork-kit/ 2>&1"),
    "the heavy test's own run with 2>&1": heavy_run(lambda line: line + " 2>&1"),
    "upstream's .py test with -I -B -u and 2>&1": upstream_py_run(
        lambda run: run.rstrip("\n").replace("python3 -B", "python3 -I -B -u") + " 2>&1\n"),
    "a whole comment line before the test": heavy_run(lambda line: "# the heavy kit\n" + line),
    "two own runs in one step (the heavy one's step gone)": lambda jobs: (
        jobs["kit_root_tests"]["steps"].remove(heavy_step(jobs)),
        test_step(jobs, "kit_root_tests", ROOT_TEST).__setitem__("run", f"{ROOT_LINE}\n{HEAVY_LINE}\n")),
    "a step timeout and id": lambda jobs: heavy_step(jobs).update({"timeout-minutes": 5, "id": "heavy"}),
    # A kit path in a set-up step's comment line only: the step runs no test, so it is not a test step.
    "a set-up step with a kit path in a comment line": lambda jobs: next(
        st for st in jobs["kit_root_tests"]["steps"] if "loginctl" in str(st.get("run", ""))).__setitem__(
        "run", "# slice as in fork-kit/heavy/README.md\n" + next(
            st for st in jobs["kit_root_tests"]["steps"] if "loginctl" in str(st.get("run", "")))["run"]),
    "the kit runner quoted and with ./": kit_run(f"node './{RUNNER}' \"fork-kit/\""),
}
for label, mutate in QUIET_ACCEPTED.items():
    check(f"quiet: {label} passes", not quiet_mutant(mutate), "; ".join(quiet_mutant(mutate)))
check("quiet: upstream's test steps under if: matrix.shard == 1 pass",
      not quiet_mutant(lambda jobs: None) and any(
          "if" in st and ".test." in str(st.get("run", "")) for st in fork_tests["jobs"]["test"]["steps"]))

# An own run's switch declared by the test itself: without the line, `--run` of it is refused; with a line no step
# makes, the run is missing.
check("quiet: the heavy test's `fork-ci: run` line gone (its switch with it) fails",
      any(f"{HEAVY}: run by `{RUNNER} --run`" in p
          for p in quiet_mutant(lambda jobs: None, own_runs={t: r for t, r in OWN_RUNS.items() if t != HEAVY})))
check("quiet: a test that declares an own run no step makes fails",
      any("made by 0" in p for p in quiet_mutant(lambda jobs: None, own_runs={
          **OWN_RUNS, ".github/scripts/fork-follow.test.sh": [{"root": False, "vars": [], "only": None}]})))
check("records: every `fork-ci:` line of a listed test is one the runner reads", not DECLARATION_PROBLEMS,
      "; ".join(DECLARATION_PROBLEMS))
check("records: the own runs are read from the tests' own lines (root and gh-apps switch, heavy switch, the fleet "
      "launcher as root in one class, owned-reads plain)",
      OWN_RUNS.get(ROOT_TEST) == [{"root": True, "vars": ["GH_APPS_ROOT_TESTS=1"], "only": None}]
      and OWN_RUNS.get(HEAVY) == [{"root": False, "vars": ["HEAVY_REAL_TESTS=1"], "only": None}]
      and OWN_RUNS.get(FLEET_TEST) == [{"root": True, "vars": ["FORK_FLEET_ROOT_TESTS=1"], "only": "LauncherAsRoot"}]
      and OWN_RUNS.get(".github/scripts/fork-owned-reads.test.py") == [{"root": False, "vars": [], "only": None}],
      json.dumps(OWN_RUNS))

# The tests a run in a part may skip (#664), each by name and pinned here, so a new skip is a change to this table too;
# the runner and the record check hold each run to exactly these (a declared skip that ran is red as well).
MAY_SKIP = {
    # Its root tests run as root in their own run (`--run only LauncherAsRoot`); the part run is not root.
    FLEET_TEST: ["LauncherAsRoot." + t for t in (
        "test_check_net_proves_a_runners_network", "test_check_nets_probe_names_drives_and_sockets",
        "test_nothing_a_job_leaves_reaches_the_next_job_of_its_user", "test_one_slots_user_cannot_read_or_change_the_others",
        "test_refresh_net_takes_only_roots_lookup_stamp", "test_root_deletes_nothing_in_dev_shm",
        "test_the_real_job_unit_writes_its_copy_and_nothing_else", "test_the_walls_hold_in_a_real_systemd_unit",
        "test_the_walls_proof_refuses_a_host_with_a_pools_users")],
    # Needs a checkout that is not a merge commit; CI's pull request checkout is one.
    "fork-ci/actions/head-check/head_check.test.py": [
        "SameChangeBare.test_a_checkout_that_is_not_a_merge_commit_is_refused"],
    # Need the ci/vN tags; CI's checkout fetches none (only its Releases class runs there).
    "fork-ci/actions/head-check/review_guide.test.py": ["ReleaseTags." + t for t in (
        "test_override_authors_is_first_in_the_release_after_the_one_the_notes_name",
        "test_the_path_rules_are_first_in_the_release_the_guide_names")],
    # Runs an older release's installer, found by its ci/vN tag; CI's checkout fetches none.
    "fork-kit/heavy/tests/install.test.py": [
        "Install.test_older_installers_install_and_uninstall_keep_the_hosts_t3_capacity"],
}
check("records: the tests a part run may skip are exactly the pinned ones, each by name (MAY_SKIP)",
      {f: sorted(n) if isinstance(n, list) else n for f, n in DECLARED_SKIPS.items()} == MAY_SKIP,
      json.dumps(DECLARED_SKIPS))
# The listed tests another workflow or script runs (their `run by`): any other `run by` but fork-tests.yml could take a
# fork test out of the records, whatever its name, so the runner refuses it and still expects the test's record
# (fork-ci-tests.cjs, RUN_ELSEWHERE). A change here is a change of its own, which review sees.
RUN_ELSEWHERE = {".github/scripts/fork-secret-scan.test.sh": ".github/workflows/fork-secret-scan.yml",
                 ".github/scripts/relay-state-output.test.cjs": "scripts/release-smoke.ts"}
_ELSEWHERE = json.loads(subprocess.run(
    ["node", "-e", "const r = require(process.argv[1]); console.log(JSON.stringify(r.RUN_ELSEWHERE ?? null))",
     str(scripts_dir / "fork-ci-tests.cjs")], check=True, capture_output=True, text=True).stdout)
_SHIM = json.loads(subprocess.run(
    ["node", "-e", "const r = require(process.argv[1]); console.log(JSON.stringify(r.UNITTEST_SHIM ?? null))",
     str(scripts_dir / "fork-ci-tests.cjs")], check=True, capture_output=True, text=True).stdout)
_NODE_COUNTS = json.loads(subprocess.run(
    ["node", "-e", "const r = require(process.argv[1]); console.log(JSON.stringify(r.NODE_COUNTS ?? null))",
     str(scripts_dir / "fork-ci-tests.cjs")], check=True, capture_output=True, text=True).stdout)
check("records: the shim the runner runs Python tests through is the one each record job's checkout must hold",
      _SHIM == RUNNER_FILES[2] and (root / RUNNER_FILES[2]).is_file(), json.dumps(_SHIM))
check("records: the reporter the runner gives node tests is the one each record job's checkout must hold",
      _NODE_COUNTS == RUNNER_FILES[3] and (root / RUNNER_FILES[3]).is_file(), json.dumps(_NODE_COUNTS))
check("records: checks_out reads sparse checkouts (a file, a folder, all, a negated path, none, another repository)",
      [checks_out({"uses": "actions/checkout@x", "with": w}, ".github/scripts/fork-ci-unittest.py") for w in (
          {"sparse-checkout": "/.github/scripts/fork-ci-unittest.py\n"}, {"sparse-checkout": "/.github/\n"},
          {"sparse-checkout": "/*\n!/.repos/\n"}, {"sparse-checkout": "/*\n!/.github/scripts/\n"},
          {"sparse-checkout": "/fork-kit/\n"}, {}, {"repository": "o/r", "sparse-checkout": "/x\n"})]
      == [True, True, True, False, False, True, True])
check("records: the listed tests another workflow or script runs are exactly the pinned ones (RUN_ELSEWHERE)",
      _ELSEWHERE == RUN_ELSEWHERE, json.dumps(_ELSEWHERE))
check("records: each listed test the list says another workflow or script runs is in RUN_ELSEWHERE, with that one",
      all(RUN_ELSEWHERE.get(e["file"]) == e["runBy"] for e in LISTED
          if e["runBy"] and e["runBy"] != ".github/workflows/fork-tests.yml"),
      json.dumps([e for e in LISTED if e["runBy"]]))

# The record check's own script (from the workflow, as fork_ci_result runs it) on records the real runner writes
# (its record and writeRecords), so a change to either side's format fails here. The good set: every job and leg of
# RECORD_JOBS uploaded, each key of `all` recorded once. Each change below is a state the check must refuse: a job,
# leg or attempt missing or stale, a record from another run, attempt, job, leg or commit, a test missing, run
# twice, failed, with no count, none run, a skip not declared or a declared one that ran, a skip without a name, a
# record cut off half-way, an older runner's record.
CHECK_STEP = result_step(fork_tests["jobs"], RECORD_CHECK)
CHECK_SCRIPT = check_script(CHECK_STEP["run"])
REAL_RECORD_JOBS = json.loads(CHECK_STEP["env"]["RECORD_JOBS"])
FIX_RUN, FIX_SHA = "4242", "c0ffee" + "0" * 34
# A Python test's result as the runner reads it, the list saying "unittest" for each (#702): the lines
# fork-ci-unittest.py wrote (`unittest`: a plan, then its counts, each test by name, then its end line: the tests the
# file defines that no run loaded); a node test's, the line fork-ci-node-counts.cjs wrote.
def py(tests, skipped=(), unloaded=()):
    return {"status": 0, "seconds": "1.0", "stdout": "", "stderr": f"Ran {tests} tests in 0.1s\n\nOK\n",
            "unittest": [{"plan": tests}, {"tests": tests, "skipped": list(skipped), "failed": 0},
                         {"unloaded": list(unloaded)}]}


def js(tests, skipped=0, **more):
    line = {"tests": tests, "skipped": skipped, "failed": 0, "started": tests, "file_test": False, "stopped": 0}
    return {"status": 0, "seconds": "1.0", "stdout": "", "stderr": "", "unittest": [{**line, **more}]}


PY_OK = py(4)
PY_SKIP1 = py(4, ["A.test_net"])
PY_SKIP_OTHER = py(4, ["A.test_disk"])
PY_ALL_SKIPPED = py(2, ["A.test_net", "A.test_disk"])
PY_NONE = py(0)
JS_OK = js(3)
JS_SKIP1 = js(3, 1)
SH_OK = {"status": 0, "seconds": "1.0", "stdout": "ok one\nok two\nall passed: 2 ok, 0 skipped\n", "stderr": ""}
SH_SILENT = {"status": 0, "seconds": "1.0", "stdout": "all passed\n", "stderr": ""}
ROOT_RUN = {"root": True, "vars": ["X_TESTS=1"], "only": None}
CLASS_RUN = {"root": True, "vars": ["Y_TESTS=1"], "only": "AsRoot"}
PLAIN_RUN = {"root": False, "vars": [], "only": None}


def good_records():
    """{artifact: [call]}: every job and leg of the real RECORD_JOBS, attempt 1, each call's results as
    [key, test, own run or None, its result, the tests it may and must skip]."""
    assert REAL_RECORD_JOBS == {"workflows": 1, "action_tests": 1, "kit_tests": 3, "kit_root_tests": 1, "lint": 1}
    calls = {
        ("workflows", 0): [("part", ["!fork-kit/"], [["fork-a.test.py", "fork-a.test.py", None, PY_SKIP1, ["A.test_net"]],
                                                     ["fork-s.test.sh", "fork-s.test.sh", None, SH_OK, []]]),
                           ("run", "fork-f.test.py", [["fork-f.test.py --run only AsRoot", "fork-f.test.py",
                                                       CLASS_RUN, PY_OK, []]])],
        ("action_tests", 0): [("part", ["fork-ci/"], [["fork-ci/b.test.cjs", "fork-ci/b.test.cjs", None, JS_OK, []]])],
        ("kit_tests", 0): [("part", ["fork-kit/"], [["fork-kit/k1.test.py", "fork-kit/k1.test.py", None, PY_OK, []]])],
        ("kit_tests", 1): [("part", ["fork-kit/"], [["fork-kit/k2.test.py", "fork-kit/k2.test.py", None, PY_OK, []]])],
        ("kit_tests", 2): [("part", ["fork-kit/"], [])],
        ("kit_root_tests", 0): [("run", "fork-kit/r.test.py", [["fork-kit/r.test.py --run", "fork-kit/r.test.py",
                                                                ROOT_RUN, PY_OK, []]]),
                                ("run", "fork-kit/h.test.py", [["fork-kit/h.test.py --run", "fork-kit/h.test.py",
                                                                PLAIN_RUN, PY_OK, []]])],
        ("lint", 0): [("run", "fork-o.test.py", [["fork-o.test.py --run", "fork-o.test.py", PLAIN_RUN, PY_OK, []]])],
    }
    every = sorted(row[0] for leg in calls.values() for _, _, rows in leg for row in rows)
    out = {}
    for (job, index), leg in calls.items():
        records = []
        for n, (kind, what, rows) in enumerate(leg):
            body = {"job": job, "attempt": 1, "run_id": FIX_RUN, "sha": FIX_SHA, "results": rows,
                    "owed": sorted(row[0] for row in rows)}
            if kind == "part":
                body.update(name=f"part-{index + 1}of{REAL_RECORD_JOBS[job]}-{'_'.join(what)}",
                            call={"parts": what, "shard": f"{index + 1}/{REAL_RECORD_JOBS[job]}"}, all=every)
            else:
                body.update(name=f"run-{what}", call={"run": what}, all=None)
            records.append(body)
        out[f"fork-test-records-{job}-{index}-1"] = records
    return out


WRITE_RECORDS = (
    "const fs = require('fs'); const path = require('path'); const r = require(process.argv[1]); "
    "const [folder, artifacts] = [process.argv[2], JSON.parse(fs.readFileSync(0, 'utf8'))]; "
    "for (const [name, calls] of Object.entries(artifacts)) { fs.mkdirSync(path.join(folder, name), "
    "{ recursive: true }); for (const c of calls) r.writeRecords({ dir: path.join(folder, name), run_id: c.run_id, "
    "run_attempt: c.attempt, job: c.job }, c.name, { sha: c.sha, call: c.call, all: c.all, owed: c.owed, "
    "results: c.results.map(([key, file, run, result, allowed]) => r.record(key, file, run, result, allowed, "
    "file.endsWith('.py') ? 'unittest' : null)) }); }")


def run_check(artifacts, attempt=1, tweak=None, sha=FIX_SHA):
    """The check's exit status and output on these records (tweak: changes the written files)."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = pathlib.Path(tmp) / "records"
        folder.mkdir()
        subprocess.run(["node", "-e", WRITE_RECORDS, str(scripts_dir / "fork-ci-tests.cjs"), str(folder)],
                       input=json.dumps(artifacts), check=True, capture_output=True, text=True)
        if tweak:
            tweak(folder)
        env = {"PATH": os.environ["PATH"], "RECORDS": str(folder), "RUN_ID": FIX_RUN, "RUN_ATTEMPT": str(attempt),
               "SHA": sha, "RECORD_JOBS": CHECK_STEP["env"]["RECORD_JOBS"],
               "GITHUB_STEP_SUMMARY": str(pathlib.Path(tmp) / "summary")}
        done = subprocess.run(["python3", "-I", "-"], input=CHECK_SCRIPT, env=env, capture_output=True, text=True,
                              cwd=tmp)
        return done.returncode, done.stdout + done.stderr


def changed(change):
    records = good_records()
    change(records)
    return records


def leg(records, job, index=0, attempt=1):
    return records[f"fork-test-records-{job}-{index}-{attempt}"]


def row(records, key):
    return next(r for calls in records.values() for c in calls for r in c["results"] if r[0] == key)


def drop_key(records, key):
    for calls in records.values():
        for c in calls:
            c["results"] = [r for r in c["results"] if r[0] != key]
            c["owed"] = [k for k in c["owed"] if k != key]


def set_row(key, result=None, allowed=None):
    def change(records):
        r = row(records, key)
        if result is not None:
            r[3] = result
        if allowed is not None:
            r[4] = allowed
    return change


def again(records, job, index=0, attempt=2, change=None):
    """The leg re-run in another attempt: its artifact again, under that attempt."""
    calls = copy.deepcopy(leg(records, job, index))
    for c in calls:
        c["attempt"] = attempt
        if change:
            change(c)
    records[f"fork-test-records-{job}-{index}-{attempt}"] = calls


def edit_file(artifact, change):
    """A written records file changed on disk (as no runner writes it)."""
    def tweak(folder):
        file = sorted((folder / artifact).iterdir())[0]
        file.write_text(change(file.read_text()))
    return tweak


def edit_record(artifact, change):
    """A written records file's JSON changed on disk (an older runner's, or one no runner writes)."""
    def tweak(folder):
        file = sorted((folder / artifact).iterdir())[0]
        body = json.loads(file.read_text())
        change(body)
        file.write_text(json.dumps(body))
    return tweak


def old_format(body):
    """A record as the runner before named skips wrote it: `allowed` a number, no `skipped_tests`."""
    for r in body["results"]:
        r["allowed"] = 1
        r.pop("skipped_tests", None)


check("records: the record check's script is read from its step", CHECK_SCRIPT is not None
      and "raise SystemExit" in CHECK_SCRIPT)

# The names of each module the record check's script may use, and where it may write: only the step summary.
SCRIPT_NAMES = {"json": {"load", "loads"}, "re": {"fullmatch"}, "os": {"environ", "listdir", "path", "devnull"},
                "os.path": {"isdir", "join"}}
SUMMARY = "os.environ.get('GITHUB_STEP_SUMMARY', os.devnull)"


def script_reach(script):
    """What the record check's script could reach beyond reading the records and the env and adding to the step
    summary (empty: nothing): an import but of json, os and re, or under another name; a name of those modules but
    SCRIPT_NAMES; an open() but to read or to add to the summary; a dunder; exec, eval, compile, getattr and the like."""
    out = []
    tree = ast.parse(script)
    calls = {id(n.func): n for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name if a.name not in SCRIPT_NAMES else f"import {a.name} as {a.asname}"
                    for a in node.names if a.name not in SCRIPT_NAMES or a.asname]
        elif isinstance(node, ast.ImportFrom):
            out.append(f"from {node.module}")
        elif isinstance(node, ast.Attribute) and ast.unparse(node.value) in SCRIPT_NAMES:
            if node.attr not in SCRIPT_NAMES[ast.unparse(node.value)]:
                out.append(ast.unparse(node))
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            out.append(f".{node.attr}")
        elif isinstance(node, ast.Name) and node.id == "open":
            call = calls.get(id(node))
            mode = [ast.unparse(m) for m in (call.args[1:2] if call else [])] + [
                ast.unparse(k.value) for k in (call.keywords if call else []) if k.arg == "mode"]
            if call is None or not (mode == [] or mode == ["'a'"] and ast.unparse(call.args[0]) == SUMMARY):
                out.append(f"open({', '.join(mode)})" if call else "open")
        elif isinstance(node, ast.Name) and (node.id.startswith("__") or node.id in {
                "exec", "eval", "compile", "getattr", "setattr", "delattr", "globals", "locals", "vars",
                "breakpoint", "input", "memoryview"}):
            out.append(node.id)
    return out


check("records: the record check's script reads only the records and the env, and writes only to the step summary",
      CHECK_SCRIPT is not None and not script_reach(CHECK_SCRIPT), str(CHECK_SCRIPT and script_reach(CHECK_SCRIPT)))
check("records: a record check that would start a process, write a file or reach past its modules is caught",
      sorted(script_reach("import subprocess\nimport os\nos.system('x')\nfrom shutil import which\neval('1')\n"
                          "import os as o\no.system('x')\nopen('/etc/x', 'w')\nopen(p, mode='w')\nf = open\n"
                          "__builtins__.__dict__['exec']\njson.decoder.sys.modules\nre.sub\n"))
      == sorted(["eval", "from shutil", "os.system", "subprocess", "import os as o", "open('w')", "open('w')",
                 "open", "__builtins__", ".__dict__", "json.decoder", "re.sub"]))
status, out = run_check(good_records())
check("records: a run with every job's and leg's records, each listed test recorded once and run, passes",
      status == 0 and "9 runs of listed tests" in out, out)
status, out = run_check(changed(lambda r: again(r, "kit_tests", 1)), attempt=2)
check("records: a leg re-run in attempt 2 (its attempt-1 records kept) passes on the newest", status == 0, out)
status, out = run_check(good_records(), attempt=2)
check("records: attempt 2 that re-ran no record job (attempt 1's records stand) passes", status == 0, out)
RECORDS_REFUSED = {
    # A job or leg that uploaded nothing.
    "no records at all (nothing downloaded)": (lambda r: r.clear(), {}, "uploaded no fork test records"),
    "a kit leg without records": (lambda r: r.pop("fork-test-records-kit_tests-1-1"), {},
                                  "kit_tests (leg 2 of 3): uploaded no fork test records"),
    "the lint job without records": (lambda r: r.pop("fork-test-records-lint-0-1"), {}, "lint (leg 1 of 1)"),
    "an empty records artifact": (lambda r: r.__setitem__("fork-test-records-lint-0-1", []), {}, "empty"),
    # No commit to compare with (a pull request's release-ref output is empty): red, never "all records match".
    "an empty commit to compare with": (lambda r: None, {"sha": ""}, "is '', not a full SHA"),
    "a short commit to compare with": (lambda r: None, {"sha": FIX_SHA[:12]}, "not a full SHA"),
    # A record that is not this run's, attempt's, job's, leg's or commit's.
    "a record of another run": (lambda r: leg(r, "lint")[0].update(run_id="4241"), {},
                                "written by another run, attempt or job"),
    "attempt 1's record in attempt 2's artifact": (lambda r: (again(r, "lint", change=lambda c: c.update(attempt=1))),
                                                   {"attempt": 2}, "written by another run, attempt or job"),
    "another job's record in an artifact": (lambda r: leg(r, "lint")[0].update(job="workflows"), {},
                                            "written by another run, attempt or job"),
    "a leg's record under another leg's name": (lambda r: (r.__setitem__(
        "fork-test-records-kit_tests-0-1", copy.deepcopy(leg(r, "kit_tests", 1))),
        leg(r, "kit_tests", 1)[0]["results"].clear(), leg(r, "kit_tests", 1)[0]["owed"].clear()), {},
        "shard 2/3, not leg 1/3"),
    "a record of another commit": (lambda r: leg(r, "action_tests")[0].update(sha="f" * 40), {}, "tests ffff"),
    "an artifact of an attempt after this one": (lambda r: again(r, "lint", attempt=3), {"attempt": 2},
                                                 "no records artifact of a job, leg and attempt of this run"),
    "an artifact of a job that runs no fork test": (lambda r: r.__setitem__(
        "fork-test-records-build-0-1", copy.deepcopy(leg(r, "lint"))), {}, "fork-test-records-build-0-1"),
    "an artifact of a fourth kit leg": (lambda r: r.__setitem__(
        "fork-test-records-kit_tests-3-1", copy.deepcopy(leg(r, "kit_tests", 2))), {}, "kit_tests-3-1"),
    # A re-run whose newest attempt failed: the passing older one never stands in.
    "a leg re-run in attempt 2 whose test failed (attempt 1 passed)": (lambda r: again(
        r, "kit_tests", 1, change=lambda c: c["results"][0].__setitem__(3, {**PY_OK, "status": 1})),
        {"attempt": 2}, "fork-kit/k2.test.py failed (exit 1)"),
    # A listed test missing, twice, or extra.
    "a listed test no job recorded": (lambda r: drop_key(r, "fork-kit/k2.test.py"), {},
                                      "fork-kit/k2.test.py: listed, but no job recorded it"),
    "an own run no job recorded": (lambda r: drop_key(r, "fork-f.test.py --run only AsRoot"), {},
                                   "fork-f.test.py --run only AsRoot: listed, but no job recorded it"),
    "a test recorded twice": (lambda r: leg(r, "kit_tests", 2)[0].update(
        results=[copy.deepcopy(row(r, "fork-kit/k1.test.py"))], owed=["fork-kit/k1.test.py"]), {},
        "recorded twice"),
    "a test recorded that is not to run": (lambda r: leg(r, "kit_tests", 2)[0].update(
        results=[["fork-kit/x.test.py", "fork-kit/x.test.py", None, PY_OK, []]], owed=["fork-kit/x.test.py"]), {},
        "fork-kit/x.test.py: recorded, but not among the tests to run"),
    "a call that owed a test it has no result of": (lambda r: leg(r, "kit_tests", 2)[0].update(
        owed=["fork-kit/k1.test.py"]), {}, "results do not match what it owed"),
    "two parts that disagree on the tests to run": (lambda r: leg(r, "action_tests")[0].update(
        all=leg(r, "action_tests")[0]["all"] + ["fork-kit/y.test.py"]), {}, "its tests to run differ"),
    "no part record (nothing says what is to run)": (lambda r: [
        c.update(call={"run": "x"}, all=None) for calls in r.values() for c in calls], {},
        "no part records: the tests to run are unknown"),
    # A test that did not really run.
    "a test that failed": (set_row("fork-kit/k1.test.py", {**PY_OK, "status": 1}), {},
                           "fork-kit/k1.test.py failed (exit 1)"),
    "a test that ran zero tests": (set_row("fork-kit/k1.test.py", PY_NONE), {},
                                   "fork-kit/k1.test.py ran no test (0 found, 0 skipped)"),
    "a test that skipped all its tests (both declared)": (
        set_row("fork-kit/k1.test.py", PY_ALL_SKIPPED, ["A.test_net", "A.test_disk"]), {},
        "ran no test (2 found, 2 skipped)"),
    # Skips are tight both ways, by name: one not declared, one declared that ran, another in a declared one's place.
    "a test that skipped one it does not declare": (set_row("fork-kit/k1.test.py", PY_SKIP1), {},
                                                    "fork-kit/k1.test.py skipped ['A.test_net'], but may and must "
                                                    "skip []"),
    "a test whose declared skip ran (a stale declaration)": (set_row("fork-a.test.py", PY_OK), {},
                                                             "fork-a.test.py skipped [], but may and must skip "
                                                             "['A.test_net']"),
    "a test that skipped another test than the one it declares": (set_row("fork-a.test.py", PY_SKIP_OTHER), {},
                                                                  "skipped ['A.test_disk'], but may and must skip "
                                                                  "['A.test_net']"),
    "an own run that skipped one (it may skip none, whatever its record says)": (
        set_row("fork-kit/r.test.py --run", PY_SKIP1, ["A.test_net"]), {},
        "fork-kit/r.test.py --run skipped ['A.test_net'], but may and must skip []"),
    "a node test that skipped one (node names no skip)": (set_row("fork-ci/b.test.cjs", JS_SKIP1), {},
                                                          "fork-ci/b.test.cjs skipped 1 tests, not each once by name"),
    "an ok-lines test that skipped one it does not declare": (set_row("fork-s.test.sh", {
        **SH_OK, "stdout": "ok one\nskip net no network here\nall passed: 1 ok, 1 skipped\n"}), {},
        "fork-s.test.sh skipped ['net'], but may and must skip []"),
    "an ok-lines test that stopped before its end line": (set_row("fork-s.test.sh", {
        **SH_OK, "stdout": "ok one\n"}), {}, "fork-s.test.sh: no test count"),
    "an ok-lines test whose count differs from its lines": (set_row("fork-s.test.sh", {
        **SH_OK, "stdout": "ok one\nok two\nok a child's\nall passed: 2 ok, 0 skipped\n"}), {},
        "fork-s.test.sh: no test count"),
    "a Python test whose counts line was cut off (no count)": (set_row("fork-kit/k1.test.py", {
        **PY_OK, "unittest": [{"plan": 4}, None]}), {}, "fork-kit/k1.test.py: no test count"),
    # A test that stopped hard half-way (exit 0), or ran nothing but the file: no count, whatever its stdout says.
    "a Python test that exited hard half-way (a plan, no counts), its stdout a child's ok lines": (set_row(
        "fork-kit/k1.test.py", {**PY_OK, "stdout": "ok child\nall passed: 1 ok, 0 skipped\n",
                                "unittest": [{"plan": 4}]}), {}, "fork-kit/k1.test.py: no test count"),
    # A hard stop before the first plan (as the file loads) leaves no shim line: not read as ok lines (#702).
    "a Python test that stopped hard before its first plan (no shim line), its stdout a child's ok and end lines": (
        set_row("fork-kit/k1.test.py", {**PY_OK, "stdout": "ok child\nall passed: 1 ok, 0 skipped\n",
                                        "unittest": []}), {}, "fork-kit/k1.test.py: no test count"),
    "a Python test whose runs ended but the file did not (no end line)": (set_row(
        "fork-kit/k1.test.py", {**PY_OK, "unittest": PY_OK["unittest"][:-1]}), {}, "fork-kit/k1.test.py: no test count"),
    # A test the file defines that no run loaded is a skip by its name, held to the declarations like any (#702).
    "a Python test that never loaded a test its source defines (not declared)": (set_row(
        "fork-kit/k1.test.py", py(4, unloaded=["Tool.test_b"])), {},
        "fork-kit/k1.test.py skipped ['Tool.test_b'], but may and must skip []"),
    "a node test whose only test was the file itself (empty, a top-level return, an early exit)": (set_row(
        "fork-ci/b.test.cjs", js(0, file_test=True)), {}, "fork-ci/b.test.cjs: no test count"),
    "a node test that exited half-way (tests started that never ended)": (set_row(
        "fork-ci/b.test.cjs", js(2, started=4, stopped=2)), {}, "fork-ci/b.test.cjs: no test count"),
    "a test whose output shows no test count": (set_row("fork-s.test.sh", SH_SILENT), {},
                                                "fork-s.test.sh: no test count"),
    "a test with a failure its exit status hid": (set_row("fork-s.test.sh", {
        **SH_OK, "stdout": "ok one\nFAIL two\n"}), {}, "fork-s.test.sh: 1 failed"),
}
for label, (change, how, reason) in RECORDS_REFUSED.items():
    status, out = run_check(changed(change), **how)
    check(f"records: {label} fails, saying why", status == 1 and reason in out, out)
# Stopped half-way: a records file cut off, or a records folder that is not there.
for label, tweak, reason in [
    ("a records file cut off half-way", edit_file("fork-test-records-lint-0-1", lambda text: text[:len(text) // 2]),
     "unreadable"),
    ("a records file of another version", edit_file("fork-test-records-lint-0-1",
                                                    lambda text: text.replace('"v": 1', '"v": 2')), "not a version 1"),
    ("a records folder that is not there", lambda folder: shutil.rmtree(folder), "uploaded no fork test records"),
    # Old state: a record an older runner wrote (a re-run of an old commit's job) proves no skip by name.
    ("a record of the old format (a skip count allowed, no names)", edit_record("fork-test-records-workflows-0-1",
                                                                                 old_format),
     "fork-a.test.py skipped 1 tests, not each once by name"),
    ("a record whose skipped names were dropped", edit_record(
        "fork-test-records-workflows-0-1", lambda b: b["results"][0].update(skipped_tests=None)),
     "not each once by name"),
    # A record without names at all, even with no skip, and one that names one skip twice, are not the runner's.
    ("a record with no skipped names and no skip", edit_record(
        "fork-test-records-kit_tests-0-1", lambda b: b["results"][0].pop("skipped_tests")), "not each once by name"),
    ("a record that names its two skips as one test twice (declared once)", edit_record(
        "fork-test-records-workflows-0-1", lambda b: b["results"][0].update(
            skipped=2, skipped_tests=["A.test_net", "A.test_net"])), "not each once by name"),
]:
    status, out = run_check(good_records(), tweak=tweak)
    check(f"records: {label} fails, saying why", status == 1 and reason in out, out)

# --- policies ----------------------------------------------------------------------------------
# GitHub blocks pull_request_target (from 2026-12-02) unless a repository Actions policy allows it
# for the workflow file (#401). .github/fork-actions-policies.json is the one place that policy is
# written down: every workflow in the tree with that trigger is either allowed (a fork workflow the
# policy names) or upstream_blocked (upstream's, kept disabled and unnamed, so it stays blocked).
# A rename or a new pull_request_target workflow fails here, at pull request time. fork-checks-ran.py
# (fork-tests.yml's fork_checks_ran) proves the effect on each pull request.
policies_path = root / ".github/fork-actions-policies.json"
POLICY_SCRIPT = "python3 -I .github/scripts/fork-checks-ran.py"
# fork_checks_ran as it must be (#430): any other env value could turn it into a silent pass (EVENT_NAME
# from another expression, OUTSIDE_PR hard-coded, the base or head from elsewhere).
POLICY_ENV = {
    "EVENT_NAME": "${{ github.event_name }}",
    "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
    "PR_NUMBER": "${{ github.event.pull_request.number }}",
    "OUTSIDE_PR": "${{ github.event.pull_request && github.event.pull_request.head.repo.full_name != github.repository }}",
    "DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
    "GITHUB_TOKEN": "${{ github.token }}",
}
POLICY_CHECKOUT_ENV = {"REF": "${{ github.sha }}", "HISTORY": "tip", "SPARSE": "/.github/scripts/"}
POLICY_JOB_KEYS = {"name", "if", "runs-on", "timeout-minutes", "permissions", "steps"}
_fcr_spec = importlib.util.spec_from_file_location("fork_checks_ran", root / ".github/scripts/fork-checks-ran.py")
_fcr = importlib.util.module_from_spec(_fcr_spec)
sys.dont_write_bytecode = True   # no __pycache__ in the tree
_fcr_spec.loader.exec_module(_fcr)
# The script's own limits (fork-checks-ran.test.py checks it keeps to them): its cap counts its git calls.
POLICY_LIMITS = {k: getattr(_fcr, k) for k in ("GIT_TIMEOUT", "GIT_CALLS", "API_TIMEOUT", "API_CALLS_PER_POLL")}
# Room the worst case leaves in the job timeout for the job's own set-up and the script's start (#449): a worst case
# of exactly the timeout would be cancelled before its message.
POLICY_MARGIN = 60


def policy_worst_case(checkout, cap, limits):
    """Seconds fork_checks_ran can take: the checkout (its step's own timeout, #443), then the cap (the git calls run
    inside it, or past it if they take longer), then one last poll after the deadline."""
    return (checkout + max(cap, limits["GIT_TIMEOUT"] * limits["GIT_CALLS"])
            + limits["API_TIMEOUT"] * limits["API_CALLS_PER_POLL"])


def policy_job_problems(job, fc, limits=POLICY_LIMITS):
    """fork-tests.yml's fork_checks_ran: exactly the pinned shape (the shared checkout step itself is held by the
    checkout section)."""
    problems = []
    where = "fork-tests.yml: job fork_checks_ran"
    if set(job) != POLICY_JOB_KEYS:
        problems.append(f"{where} has keys {sorted(set(job))}, not {sorted(POLICY_JOB_KEYS)} (continue-on-error, needs, env ...)")
    if expression(job.get("if", "")) != PR_GUARD:
        problems.append(f"{where}: if: is not the pull request guard alone")
    if job.get("permissions") != {"actions": "read", "contents": "read"}:
        problems.append(f"{where}: permissions are not exactly actions: read, contents: read")
    steps_ = job.get("steps") or []
    checkout_minutes = None
    if len(steps_) != 2:
        problems.append(f"{where}: not exactly two steps (checkout, script)")
    else:
        checkout, script = steps_
        # Its own timeout, so a slow checkout fails as that step, not as the job cancelled (#443).
        checkout_minutes = checkout.get("timeout-minutes")
        if type(checkout_minutes) is not int or checkout_minutes <= 0:
            problems.append(f"{where}: the checkout step has no timeout-minutes of its own (a positive whole number)")
            checkout_minutes = None
        if "git init" not in str(checkout.get("run", "")) or checkout.get("env") != POLICY_CHECKOUT_ENV:
            problems.append(f"{where}: the first step is not the shared checkout of github.sha with only the script")
        if set(script) != {"name", "env", "run"} or str(script.get("run", "")).strip() != POLICY_SCRIPT \
                or script.get("env") != POLICY_ENV:
            problems.append(f"{where}: the second step is not `{POLICY_SCRIPT}` with the pinned env and nothing else")
    timeout, cap = job.get("timeout-minutes"), fc.get("wait_seconds")
    if not isinstance(timeout, int) or not isinstance(cap, int) or checkout_minutes is None \
            or policy_worst_case(checkout_minutes * 60, cap, limits) + POLICY_MARGIN > timeout * 60:
        problems.append(f"{where}: its worst case (checkout {checkout_minutes!r} min, fork_checks.wait_seconds "
                        f"{cap!r} or the git calls {limits}, one last poll) plus {POLICY_MARGIN} s for the job's "
                        f"set-up does not fit the timeout ({timeout!r} min), so a timeout would cancel the job before "
                        "its message")
    return problems


def policy_problems(policies, wfs, tests_wf, limits=POLICY_LIMITS):
    """wfs: tree path -> parsed workflow (every workflow file, upstream's too)."""
    prt = policies.get("pull_request_target") or {}
    allowed, blocked = prt.get("allowed") or [], prt.get("upstream_blocked") or []
    problems = []
    for path in sorted(p for p, wf in wfs.items() if "pull_request_target" in triggers(wf)):
        if path not in allowed and path not in blocked:
            problems.append(f"{path} runs on pull_request_target but is in neither list of {policies_path.name}")
    for path in allowed:
        if path not in wfs:
            problems.append(f"allowed {path} is not in the tree (renamed? the Actions policy names the old path)")
        elif triggers(wfs[path]) != {"pull_request_target"}:
            # Another trigger runs the pull request's own copy of the file: a same-named job of no worth (#430).
            problems.append(f"allowed {path} has triggers {sorted(triggers(wfs[path]))}, not pull_request_target alone")
        if not path.startswith(".github/workflows/fork-"):
            problems.append(f"allowed {path} is not a fork workflow")
    for path in blocked:
        if path.startswith(".github/workflows/fork-") or path in allowed:
            problems.append(f"upstream_blocked {path} is a fork workflow or also allowed")
    fc = policies.get("fork_checks") or {}
    if fc.get("workflow") not in allowed:
        problems.append(f"fork_checks.workflow {fc.get('workflow')!r} is not an allowed workflow")
    else:
        names = {str(j.get("name")) for j in (wfs.get(fc["workflow"], {}).get("jobs") or {}).values()}
        problems += [f"{fc['workflow']} has no job named {fc.get(k)!r} (fork_checks.{k})"
                     for k in ("check", "outside_check") if fc.get(k) not in names]
    problems += policy_job_problems((tests_wf.get("jobs") or {}).get("fork_checks_ran") or {}, fc, limits)
    return problems


def policy_problems_or_crash(policies, wfs, tests_wf, limits=POLICY_LIMITS):
    """policy_problems; a crash (a contract bug, or a mutation that cannot apply to the file) is one named problem,
    so the later checks, the failure count and the clean-up still run (#449)."""
    try:
        return policy_problems(policies, wfs, tests_wf, limits)
    except Exception as e:
        return [f"the policy check crashed: {e!r}"]


all_wfs = {str(p.relative_to(root)): load(p) for p in sorted(workflows_dir.glob("*.y*ml"))}
policies = json.loads(policies_path.read_text()) if policies_path.is_file() else {}
check("policies: .github/fork-actions-policies.json is there", bool(policies))
problems = policy_problems_or_crash(policies, all_wfs, fork_tests)
check("policies: every pull_request_target workflow is allowed or upstream_blocked, and fork-checks' check "
      "names are its jobs'", not problems, "; ".join(problems))


def policy_mutant(mutate, limits=POLICY_LIMITS):
    """(caught, detail): caught when the mutated files give a problem. A crash, in the mutation or in the check, is
    not a catch but a named FAIL with its error (#449)."""
    p, w, t = copy.deepcopy(policies), copy.deepcopy(all_wfs), copy.deepcopy(fork_tests)
    try:
        mutate(p, w, t)
        return bool(policy_problems(p, w, t, limits)), ""
    except Exception as e:
        return False, f"crashed: {e!r}"


FC = ".github/workflows/fork-checks.yml"
for label, mutate in {
    "fork-checks.yml renamed": lambda p, w, t: w.__setitem__(".github/workflows/fork-checks2.yml", w.pop(FC)),
    "a new fork workflow on pull_request_target": lambda p, w, t: w.__setitem__(
        ".github/workflows/fork-new.yml", {True: {"pull_request_target": None}, "jobs": {}}),
    "a new upstream workflow on pull_request_target": lambda p, w, t: w.__setitem__(
        ".github/workflows/new.yml", {True: ["pull_request_target"], "jobs": {}}),
    "an allowed workflow without the trigger": lambda p, w, t: w[FC].__setitem__(True, {"pull_request": None}),
    "an upstream workflow allowed": lambda p, w, t: p["pull_request_target"]["allowed"].append(
        ".github/workflows/pr-size.yml"),
    "a fork workflow in upstream_blocked": lambda p, w, t: p["pull_request_target"]["upstream_blocked"].append(FC),
    "fork-checks' job renamed": lambda p, w, t: w[FC]["jobs"]["no_schema_changes"].__setitem__("name", "Schema"),
    "fork_checks naming another workflow": lambda p, w, t: p["fork_checks"].__setitem__(
        "workflow", ".github/workflows/fork-tests.yml"),
    "no fork_checks_ran job": lambda p, w, t: t["jobs"].pop("fork_checks_ran"),
    "fork_checks_ran without actions: read": lambda p, w, t: t["jobs"]["fork_checks_ran"]["permissions"].pop("actions"),
    "fork_checks_ran not running the script": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][-1].__setitem__(
        "run", POLICY_SCRIPT + " || true"),
    "fork-checks.yml also on push": lambda p, w, t: w[FC][True].__setitem__("push", None),
    "fork-checks.yml also on pull_request": lambda p, w, t: w[FC][True].__setitem__("pull_request", None),
    "fork_checks_ran job may fail quietly": lambda p, w, t: t["jobs"]["fork_checks_ran"].__setitem__(
        "continue-on-error", True),
    "fork_checks_ran step may fail quietly": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][-1].__setitem__(
        "continue-on-error", True),
    "fork_checks_ran with another if": lambda p, w, t: t["jobs"]["fork_checks_ran"].__setitem__(
        "if", "${{ " + PR_GUARD + " && github.actor != 'x' }}"),
    "fork_checks_ran with OUTSIDE_PR hard-coded": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][-1]["env"]
        .__setitem__("OUTSIDE_PR", "false"),
    "fork_checks_ran with EVENT_NAME from elsewhere": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][-1]["env"]
        .__setitem__("EVENT_NAME", "push"),
    "fork_checks_ran with the base from the head": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][-1]["env"]
        .__setitem__("BASE_SHA", "${{ github.event.pull_request.head.sha }}"),
    "fork_checks_ran with an extra env": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][-1]["env"]
        .__setitem__("BASH_ENV", "x"),
    "fork_checks_ran with a job env": lambda p, w, t: t["jobs"]["fork_checks_ran"].__setitem__("env", {"X": "1"}),
    "fork_checks_ran with a step between": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"].insert(
        1, {"run": "echo 'EVENT_NAME=push' >> $GITHUB_ENV"}),
    "fork_checks_ran checking out with actions/checkout": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"]
        .__setitem__(0, {"uses": "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"}),
    "fork_checks_ran with write permissions": lambda p, w, t: t["jobs"]["fork_checks_ran"]["permissions"].__setitem__(
        "actions", "write"),
    "a wait cap equal to the job timeout": lambda p, w, t: p["fork_checks"].__setitem__(
        "wait_seconds", t["jobs"]["fork_checks_ran"]["timeout-minutes"] * 60),
    "a job timeout cut below the cap": lambda p, w, t: t["jobs"]["fork_checks_ran"].__setitem__("timeout-minutes", 10),
    "a cap that leaves no room for the last poll": lambda p, w, t: p["fork_checks"].__setitem__(
        "wait_seconds", (t["jobs"]["fork_checks_ran"]["timeout-minutes"]
                         - (t["jobs"]["fork_checks_ran"]["steps"][0].get("timeout-minutes") or 0)) * 60 - 1),
    # The checkout's own timeout (#443): there, a whole number, and counted in the worst case.
    "fork_checks_ran's checkout without its own timeout": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][0]
        .pop("timeout-minutes", None),
    "fork_checks_ran's checkout timeout as text": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][0]
        .__setitem__("timeout-minutes", "3"),
    "fork_checks_ran's checkout timeout true": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][0]
        .__setitem__("timeout-minutes", True),
    "fork_checks_ran's checkout timeout zero": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][0]
        .__setitem__("timeout-minutes", 0),
    "a checkout timeout that breaks the budget": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][0]
        .__setitem__("timeout-minutes", (t["jobs"]["fork_checks_ran"]["steps"][0].get("timeout-minutes") or 0) + 2),
    # Worst case exactly the job timeout leaves nothing for the job's set-up: cancelled before the message (#449).
    "a checkout timeout that fills the job timeout exactly": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][0]
        .__setitem__("timeout-minutes", (t["jobs"]["fork_checks_ran"]["timeout-minutes"] * 60
                                         - policy_worst_case(0, p["fork_checks"]["wait_seconds"], POLICY_LIMITS)) // 60),
    "fork_checks_ran without PR_NUMBER": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][-1]["env"]
        .pop("PR_NUMBER"),
    "fork_checks_ran with PR_NUMBER from elsewhere": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][-1]["env"]
        .__setitem__("PR_NUMBER", "${{ github.run_number }}"),
    # The checkout's env (#433 fix round 1, state finding 5): only github.sha, depth 1, only the scripts.
    "fork_checks_ran checking out the pull request's head": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][0]
        ["env"].__setitem__("REF", "${{ github.event.pull_request.head.sha }}"),
    "fork_checks_ran checking out the whole tree": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][0]["env"]
        .__setitem__("SPARSE", ""),
    "fork_checks_ran checking out the full history": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][0]["env"]
        .__setitem__("HISTORY", "full"),
    "fork_checks_ran's checkout with an extra env": lambda p, w, t: t["jobs"]["fork_checks_ran"]["steps"][0]["env"]
        .__setitem__("BASH_ENV", "x"),
}.items():
    check(f"policies: {label} fails", *policy_mutant(mutate))
# A crash in a mutation or in the check is a contract bug: a named FAIL, so the later checks, the count and the
# clean-up still run (#449).
crash = policy_mutant(lambda p, w, t: 1 / 0)
check("policies: a mutant that crashes is a named FAIL, not a traceback",
      crash[0] is False and "ZeroDivisionError" in crash[1], repr(crash))
crash = policy_problems_or_crash(policies, all_wfs, None)   # no fork-tests.yml to read: the check itself crashes
check("policies: a crash in the check on the real files is a named problem, not a traceback",
      len(crash) == 1 and crash[0].startswith("the policy check crashed: AttributeError"), repr(crash))
# The script's limits, as if it changed them: the budget must still fit (#433 fix round 1, nit of both lenses).
for label, change in {
    "git calls that outlast the job": {"GIT_TIMEOUT": 300},
    "more git calls than the budget holds": {"GIT_CALLS": 20},
    "a slower last poll than the budget holds": {"API_TIMEOUT": 200},
}.items():
    check(f"policies: {label} fails", *policy_mutant(lambda p, w, t: None, {**POLICY_LIMITS, **change}))

# --- windows -------------------------------------------------------------------------------------
# fork-windows-tests.yml: a nightly report of upstream's windows-tests.yml on GitHub-hosted Windows. Its own `drift`
# job follows upstream's file (fork-tests-drift.cjs --windows); this contract reads only fork files, so an upstream
# change or removal of windows-tests.yml never turns Fork tests (the merge path and the release) red.
WINDOWS_FILE = "fork-windows-tests.yml"
WINDOWS_UPSTREAM = ".github/workflows/windows-tests.yml"
WINDOWS_RECORD = ".github/fork-windows-tests-upstream.json"
WINDOWS_LIMIT = 45  # upstream's timeout-minutes
WINDOWS_DRIFT = "node .github/scripts/fork-tests-drift.cjs --windows"


def windows_problems(wf, recorded, tests_wf, tests_text, tests_record):
    problems = []
    if triggers(wf) != {"schedule", "workflow_dispatch"}:
        problems.append(f"{WINDOWS_FILE}: triggers are not exactly schedule and workflow_dispatch")
    if wf.get("permissions") != {"contents": "read"}:
        problems.append(f"{WINDOWS_FILE}: permissions are not exactly contents: read")
    if (wf.get("concurrency") or {}).get("cancel-in-progress") is not True:
        problems.append(f"{WINDOWS_FILE}: a newer run does not cancel an older one (cancel-in-progress)")
    jobs = wf.get("jobs") or {}
    if set(jobs) != {"drift", "test"}:
        problems.append(f"{WINDOWS_FILE}: jobs are not exactly drift and test: {sorted(jobs)}")
    for job_id, job in jobs.items():
        limit = job.get("timeout-minutes")
        if not (type(limit) is int and 0 < limit <= WINDOWS_LIMIT):
            problems.append(f"{WINDOWS_FILE}/{job_id}: timeout-minutes is not 1 to {WINDOWS_LIMIT}")
        hosted = ("windows-",) if job_id == "test" else ("windows-", "ubuntu-")
        if not str(job.get("runs-on", "")).startswith(hosted):
            problems.append(f"{WINDOWS_FILE}/{job_id}: runs-on is not a GitHub-hosted {' or '.join(hosted)} image")
        if job.get("continue-on-error") or any(s.get("continue-on-error") for s in job.get("steps") or []):
            problems.append(f"{WINDOWS_FILE}/{job_id}: a red result is hidden (continue-on-error)")
    drift_runs = [str(s.get("run", "")).strip() for s in (jobs.get("drift") or {}).get("steps") or []]
    if WINDOWS_DRIFT not in drift_runs:
        problems.append(f"{WINDOWS_FILE}/drift: no step runs `{WINDOWS_DRIFT}`")
    up_steps = [s.get("name") for s in ((recorded.get(WINDOWS_UPSTREAM) or {}).get("test") or {}).get("steps") or []]
    if not up_steps:
        problems.append(f"{WINDOWS_RECORD}: no recorded test job of {WINDOWS_UPSTREAM} (record it with --windows)")
    mine = [s.get("name") or s.get("uses") for s in (jobs.get("test") or {}).get("steps") or []]
    if mine != up_steps:
        problems.append(f"{WINDOWS_FILE}/test: steps differ from the recorded upstream steps: {mine} != {up_steps}")
    # Fork tests (merge path, release) must not depend on upstream's Windows lane.
    if re.search(r"(?<!fork-)windows-tests\.yml", tests_text):
        problems.append("fork-tests.yml: names upstream's windows-tests.yml")
    if any("fork-tests-drift.cjs" in str(s.get("run", "")) and "--windows" in str(s.get("run", ""))
           for _, s in steps(tests_wf)):
        problems.append("fork-tests.yml: runs the Windows drift check (fork-tests-drift.cjs --windows)")
    if WINDOWS_UPSTREAM in tests_record:
        problems.append(f".github/fork-tests-upstream.json: tracks {WINDOWS_UPSTREAM} (Fork tests would follow it)")
    return problems


windows_inputs = (
    real.get(WINDOWS_FILE, {}),
    json.loads((root / WINDOWS_RECORD).read_text()) if (root / WINDOWS_RECORD).exists() else {},
    real.get("fork-tests.yml", {}),
    (workflows_dir / "fork-tests.yml").read_text(),
    json.loads((root / ".github/fork-tests-upstream.json").read_text()),
)
problems = windows_problems(*windows_inputs)
check("windows: fork-windows-tests.yml runs nightly and by hand only, cancels an older run, keeps the recorded "
      "upstream steps and 45-minute limit on hosted Windows, runs its own drift check; Fork tests never read "
      "windows-tests.yml", not problems, "; ".join(problems))
for label, mutate in {
    "a pull_request trigger": lambda w, r, t, x, tr: w[True].__setitem__("pull_request", None),
    "a push trigger": lambda w, r, t, x, tr: w[True].__setitem__("push", None),
    "no cancel-in-progress": lambda w, r, t, x, tr: w["concurrency"].__setitem__("cancel-in-progress", False),
    "a write permission": lambda w, r, t, x, tr: w["permissions"].__setitem__("actions", "write"),
    "a longer limit": lambda w, r, t, x, tr: w["jobs"]["test"].__setitem__("timeout-minutes", 90),
    "upstream's Blacksmith runner": lambda w, r, t, x, tr: w["jobs"]["test"].__setitem__(
        "runs-on", "blacksmith-8vcpu-windows-2025"),
    "a step dropped": lambda w, r, t, x, tr: w["jobs"]["test"]["steps"].pop(1),
    "a step renamed": lambda w, r, t, x, tr: w["jobs"]["test"]["steps"][0].__setitem__("name", "Clone"),
    "a recorded upstream step the copy lacks": lambda w, r, t, x, tr: r[WINDOWS_UPSTREAM]["test"]["steps"].append(
        {"name": "New"}),
    "no record": lambda w, r, t, x, tr: r.clear(),
    "continue-on-error": lambda w, r, t, x, tr: w["jobs"]["test"].__setitem__("continue-on-error", True),
    "no drift job": lambda w, r, t, x, tr: w["jobs"].pop("drift"),
    "a drift job without --windows": lambda w, r, t, x, tr: w["jobs"]["drift"]["steps"][-1].__setitem__(
        "run", "node .github/scripts/fork-tests-drift.cjs"),
    "fork-tests.yml running the Windows drift check": lambda w, r, t, x, tr: t["jobs"]["workflows"]["steps"].append(
        {"run": WINDOWS_DRIFT}),
    "the merge-path record tracking windows-tests.yml": lambda w, r, t, x, tr: tr.__setitem__(WINDOWS_UPSTREAM, {}),
}.items():
    args = copy.deepcopy(windows_inputs)
    try:
        mutate(*args)
        caught = bool(windows_problems(*args))
    except Exception as e:
        caught = False
        label += f" (crashed: {e!r})"
    check(f"windows: {label} fails", caught)
caught = bool(windows_problems(*windows_inputs[:3], windows_inputs[3] + "\n# windows-tests.yml\n", windows_inputs[4]))
check("windows: fork-tests.yml naming windows-tests.yml fails", caught)

# --- review-kit (#957) ---------------------------------------------------------------------------
# The fork keeps its kit settings under its own name, so the general kit's tests do not name them (#957 item 5).


def review_plan(*args):
    changes = work / "review-kit-changes.bin"
    changes.write_bytes(b"M\0fork-kit/review/SKILL.md\0")
    r = subprocess.run([sys.executable, "-I", "-B", str(root / "fork-kit/review/lib/review_kit.py"), "plan", "--root",
                        str(root), *args, "--changes", str(changes)], capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


code, out, err = review_plan("--settings", ".github/fork-review-kit.json")
check("review-kit: the fork's settings point the kit at the fork's queue config, in enforce, with no note",
      (code, err) == (0, "") and "note:" not in out and "weight: critical" in out, out + err)
code, out, err = review_plan()
check("review-kit: without --settings the plan names the fork's settings file",
      code == 0 and "pass --settings .github/fork-review-kit.json" in out, out + err)

shutil.rmtree(work, ignore_errors=True)
if failures:
    print(f"{len(failures)} failed")
    sys.exit(1)
print(f"all passed: {len(oks)} ok, 0 skipped")
