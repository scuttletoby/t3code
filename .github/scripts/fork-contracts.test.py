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

from fork_workflow_expr import gh_eval

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
SCAN_RUN_LINE = 'bash "$RUNNER_TEMP/scan-from-default-branch.sh"'


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
                if not CHECKOUT_ACTION.fullmatch(str(step["uses"])):
                    problems.append(f"{label}: an action other than the pinned checkout runs in the scan job")
                continue
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
    r'\s*echo "::error::[^"\n]*" >&2\n\s*exit [1-9]\n\s*fi\n(?:.*\n)*?\s*export UPSTREAM OLD_HEADS\n')


# The sweep's lookup (#239), allowed in this exact form only, right after the earlier-heads lookup:
# every tip GitHub's activity record lists for the last week, of every ref, so the sweep also reads
# a tip a deletion or force push left behind. A failed lookup fails the job.
SWEEP_LOOKUP_TEXT = (
    'elif [[ "$EVENT_NAME" == schedule || "$EVENT_NAME" == workflow_dispatch ]]; then\n'
    '  if ! OLD_HEADS="$(GH_TOKEN="$LOOKUP_TOKEN" gh api --paginate -X GET "repos/$GITHUB_REPOSITORY/activity" \\\n'
    "      -f time_period=week -f per_page=100 --jq '.[] | .before, .after')\"; then\n"
    '    echo "::error::Secret scan: cannot list GitHub\'s activity record, so a tip a deletion or force push left'
    ' behind could go unscanned. Re-run the job." >&2\n'
    "    exit 2\n"
    "  fi\n")
SWEEP_LOOKUP = re.compile(
    r'\n\s*fi\n\s*elif \[\[ "\$EVENT_NAME" == schedule \|\| "\$EVENT_NAME" == workflow_dispatch \]\]; then\n'
    r'\s*if ! OLD_HEADS="\$\(GH_TOKEN="\$LOOKUP_TOKEN" gh api --paginate -X GET "repos/\$GITHUB_REPOSITORY/activity" \\\n'
    r"""\s*-f time_period=week -f per_page=100 --jq '\.\[\] \| \.before, \.after'\)"; then\n"""
    r'\s*echo "::error::[^"\n]*" >&2\n\s*exit [1-9]\n\s*fi\n\s*fi\n')


def with_sweep_lookup(run):
    """The scan step's run with the sweep's lookup (SWEEP_LOOKUP_TEXT) added, as the workflow
    change of #239 adds it; unchanged when it is there already."""
    if "time_period=week" in run:
        return run
    return run.replace("fi\nunset LOOKUP_TOKEN\n", SWEEP_LOOKUP_TEXT + "fi\nunset LOOKUP_TOKEN\n", 1)


def lookup_problems(scan):
    problems = []
    for j, job in (scan.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            run = str(step.get("run", ""))
            if "scan-from-default-branch" not in run:
                continue
            if not LOOKUP.search(run):
                problems.append(f"{SCAN_FILE}/{j}: the earlier-heads lookup is not in its exact form (fails the job "
                                "on a failed lookup, reads .before and .after, exports OLD_HEADS)")
            if len(re.findall(r"\bOLD_HEADS\b", run)) != (4 if SWEEP_LOOKUP.search(run) else 3):
                problems.append(f"{SCAN_FILE}/{j}: OLD_HEADS is set or used outside the lookups")
            env = step.get("env") or {}
            if env.get("HEAD_REF") != "${{ github.event.pull_request.head.ref }}":
                problems.append(f"{SCAN_FILE}/{j}: HEAD_REF is not the pull request's branch")
            if env.get("LOOKUP_TOKEN") != "${{ github.token }}":
                problems.append(f"{SCAN_FILE}/{j}: LOOKUP_TOKEN is not the job's token")
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
    jobs = sweep.get("jobs") or {}
    callers = [j for j, job in jobs.items() if job.get("uses") == SCAN_CALL]
    if len(callers) != 1:
        problems.append(f"{SWEEP_FILE}: not exactly one job calls {SCAN_CALL}")
        return problems
    caller = callers[0]
    if not set(jobs[caller]) <= {"name", "uses"}:
        problems.append(f"{SWEEP_FILE}/{caller}: more than name and uses (an if, inputs or permissions)")
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
    problems += scan_job_problems(scan) + lookup_problems(scan) + scan_if_problems(scan) + tests_job_problems(scan)
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


def scan_mutant(mutate):
    wfs = copy.deepcopy(real)
    try:
        mutate(wfs)
    except (KeyError, TypeError):  # nothing to break: the check above has already failed
        return False
    return bool(secret_scan_problems(wfs))


GROUP = {"group": "scan-${{ github.ref }}", "cancel-in-progress": True}


def scan_step(wfs):
    return next(s for _, s in steps(wfs[SCAN_FILE]) if "scan-from-default-branch" in str(s.get("run", "")))


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
    "the tests back in the scan job, before the scan": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].insert(
        -1, w[SCAN_FILE]["jobs"]["tests"]["steps"][-1]),
    "the tests in the scan job, after the scan": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].append(
        w[SCAN_FILE]["jobs"]["tests"]["steps"][-1]),
    "a local action before the scan": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].insert(
        -1, {"uses": "./.github/actions/setup"}),
    "a package install before the scan": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].insert(
        -1, {"run": "pnpm install --frozen-lockfile"}),
    "a checkout file run by path before the scan": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].insert(
        -1, {"run": "scripts/prepare"}),
    "an unpinned action before the scan": lambda w: w[SCAN_FILE]["jobs"]["scan"]["steps"].insert(
        -1, {"uses": "actions/setup-node@v4"}),
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
        "run", scan_step(w)["run"].replace("export UPSTREAM OLD_HEADS", 'OLD_HEADS=""\nexport UPSTREAM OLD_HEADS')),
    "the lookup reading the wrong field": lambda w: scan_step(w).__setitem__(
        "run", scan_step(w)["run"].replace(".[] | .before, .after", ".[] | .sha")),
    "the lookup's failure not failing the job": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r"exit 2\n", "true\n", scan_step(w)["run"], count=1)),
    "the lookup for another branch": lambda w: scan_step(w)["env"].__setitem__("HEAD_REF", "${{ github.base_ref }}"),
    # #239: the sweep's lookup, in its exact form only.
    "a sweep lookup whose failure is ignored (|| true)": lambda w: scan_step(w).__setitem__(
        "run", with_sweep_lookup(scan_step(w)["run"]).replace(
            "-f time_period=week -f per_page=100 --jq '.[] | .before, .after')\"; then",
            "-f time_period=week -f per_page=100 --jq '.[] | .before, .after' || true)\"; then")),
    "a sweep lookup whose failure does not fail the job": lambda w: scan_step(w).__setitem__(
        "run", re.sub(r"exit 2\n(\s*fi\n\s*fi\nunset)", r"true\n\1", with_sweep_lookup(scan_step(w)["run"]), count=1)),
    "a sweep lookup reading the wrong field": lambda w: scan_step(w).__setitem__(
        "run", with_sweep_lookup(scan_step(w)["run"]).replace(
            "time_period=week -f per_page=100 --jq '.[] | .before, .after'",
            "time_period=week -f per_page=100 --jq '.[] | .sha'")),
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

check("secret-scan: the sweep's activity lookup (#239) in its exact form passes",
      not scan_mutant(lambda w: scan_step(w).__setitem__("run", with_sweep_lookup(scan_step(w)["run"])))
      and SWEEP_LOOKUP.search(with_sweep_lookup(scan_step(real)["run"])) is not None)

check("secret-scan: the expression evaluator reads GitHub's rules (&& and || give an operand, == ignores case)",
      gh_eval("'a' && 'B' == 'b'", {}) and not gh_eval("'' || null", {}) and gh_eval("!x.y", {})
      and gh_eval("format('refs/heads/{0}', a.b) == 'REFS/heads/main'", {"a": {"b": "main"}})
      and not gh_eval("(true || false) && !true", {}))

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

shutil.rmtree(work, ignore_errors=True)
if failures:
    print(f"{len(failures)} failed")
    sys.exit(1)
print("all passed")
