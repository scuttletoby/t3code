"""Checks .github/workflows/fork-head-gate.yml, its example caller (fork-ci/actions/head-check/gate-caller.example.yml)
and the review trigger the caller listens to (review-trigger.example.yml, issue #88): the step scripts are run as
Actions runs them, the wiring is read from the files.

    python3 .github/scripts/fork-head-gate-workflow.test.py

Needs PyYAML and bash. Each check prints ok/FAIL; any FAIL exits 1. Nothing here starts gh or the gate: a fake
python3 stands in for head_gate.py. What makes the key reachable only by the start and finish steps is checked here and,
against every fork workflow, in fork-contracts.test.py (head-gate).
"""

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

root = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = root / ".github/workflows/fork-head-gate.yml"
EXAMPLE = root / "fork-ci/actions/head-check/gate-caller.example.yml"
TRIGGER = root / "fork-ci/actions/head-check/review-trigger.example.yml"
GATE_PY = root / "fork-ci/actions/head-check/head_gate.py"
text = WORKFLOW.read_text()
wf = yaml.safe_load(text)
failures = []
GATE = "python3 -I -B toolkit/fork-ci/actions/head-check/head_gate.py"
READ = {"contents": "read", "pull-requests": "read", "actions": "read"}


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


# --- triggers, inputs, permissions ----------------------------------------------------------------------------------
on = wf.get(True, wf.get("on"))  # YAML 1.1 reads the key `on` as true
check("it is only a reusable workflow (workflow_call): no event of its own starts it", set(on) == {"workflow_call"},
      str(set(on)))
inputs = (on.get("workflow_call") or {}).get("inputs") or {}
check("inputs: pr, config, check-name, required-lenses, environment (no pass-drafts: a draft is never posted for)",
      set(inputs) == {"pr", "config", "check-name", "required-lenses", "environment"}, str(set(inputs)))
lenses_text = str(inputs.get("required-lenses", {}).get("description"))
check("required-lenses says when none may stand: only with the config's review block in enforce (#348)",
      "None = refused" in lenses_text and "enforce" in lenses_text, lenses_text)
example_lenses = next((x for x in EXAMPLE.read_text().splitlines() if "required-lenses:" in x), "")
check("example: the required-lenses comment says the same (none = refused unless the review block enforces)",
      "none = the gate refuses" in example_lenses and "enforce" in example_lenses, example_lenses)
check("config, check-name and required-lenses must be given (no default lens list); the Environment defaults to head-gate",
      all(inputs.get(k, {}).get("required") is True and "default" not in inputs.get(k, {})
          for k in ("config", "check-name", "required-lenses"))
      and inputs.get("environment", {}).get("default") == "head-gate")
check("it takes no secrets of its own: the key is the Environment's (secrets: inherit only lets the guard look)",
      "secrets" not in (on.get("workflow_call") or {}))
jobs = wf["jobs"]
guard, gate = jobs.get("guard") or {}, jobs.get("gate") or {}
check("two jobs, guard and gate", set(jobs) == {"guard", "gate"}, ", ".join(jobs))
check("no token by default; each job only reads (the write is the App token's)",
      wf.get("permissions") == {} and guard.get("permissions") == READ and gate.get("permissions") == READ,
      f"{guard.get('permissions')} {gate.get('permissions')}")

# --- the guard job --------------------------------------------------------------------------------------------------
check("the guard job has no Environment: it never sees the key", "environment" not in guard)
gate_if = str(gate.get("if"))
check("the gate job runs only after the guard job passed, when there is a head to judge and the guard answered",
      gate.get("needs") == "guard" and gate_if == "needs.guard.result == 'success' && needs.guard.outputs.prs != '[]'"
      " && needs.guard.outputs.guard != ''", f"{gate.get('needs')} {gate_if}")
check("only the gate job uses the Environment, the caller's input",
      gate.get("environment") == "${{ inputs.environment }}")
check("one gate job per head, from the guard job's list of {pr, sha}; one failing does not stop the others",
      (gate.get("strategy") or {}).get("matrix") == {"include": "${{ fromJSON(needs.guard.outputs.prs) }}"}
      and (gate.get("strategy") or {}).get("fail-fast") is False)
conc = gate.get("concurrency") or {}
check("one gate run per check name and HEAD at a time (a check run belongs to the commit), never cancelled half-way",
      conc.get("group") == "head-gate-${{ github.repository }}-${{ inputs.check-name }}-${{ matrix.sha }}"
      and conc.get("cancel-in-progress") is False, str(conc))


def render(expr, ctx):
    return re.sub(r"\$\{\{\s*([^}]+?)\s*\}\}", lambda m: str(ctx.get(m.group(1), "")), expr)


def cancelled_by_queue(jobs):
    """GitHub's rule: per group (repository-wide) one running job and one pending; a newly queued job cancels the
    pending one of its group. `jobs` is a list of (job, group) in queue order; returns the cancelled jobs."""
    running, pending, cancelled = {}, {}, []
    for job, group in jobs:
        if group not in running:
            running[group] = job
            continue
        if group in pending:
            cancelled.append(pending[group])
        pending[group] = job
    return cancelled


# corr85r2 L1: two gates in one repository (two callers, or one with two check names) on one head. An issue comment
# queues both gates' runs while earlier ones still run: neither may cancel the other's pending (revoking) run.
H = "a1" * 20
def group(name):
    return render(str(conc.get("group", "")), {"github.repository": "o/p", "matrix.sha": H, "inputs.check-name": name})
lost = cancelled_by_queue([(("push", "correctness-gate"), group("correctness-gate")),
                           (("push", "security-gate"), group("security-gate")),
                           (("revoke", "correctness-gate"), group("correctness-gate")),
                           (("revoke", "security-gate"), group("security-gate"))])
check("two gates (two check names) on one head never cancel each other's pending run", lost == [], str(lost))
check("the guard job hands on the list and the guard's answer (ok or fail: fail = the gate posts only failures)",
      guard.get("outputs") == {"prs": "${{ steps.prs.outputs.prs }}", "guard": "${{ steps.guard.outputs.guard }}"})

gsteps, tsteps = guard.get("steps") or [], gate.get("steps") or []
injected = [s.get("name") for s in gsteps + tsteps if "${{" in str(s.get("run", ""))]
check("no expression inside a run script (inputs reach it as env only)", not injected, ", ".join(map(str, injected)))
check("every run step is bash", all(s.get("shell") == "bash" for s in gsteps + tsteps if "run" in s))
check("guard steps: find the toolkit, check it out, guard, list, fail when the guard said nothing",
      [s.get("id") for s in gsteps] == ["toolkit", None, "guard", "prs", None] and len(gsteps) == 5,
      ", ".join(str(s.get("name")) for s in gsteps))
check("gate steps: find the toolkit, check it out, guard the Environment, target, drift, token, start, judge, finish",
      [s.get("id") for s in tsteps] == ["toolkit", None, "envguard", "target", None, "token", "start", "judge", "finish"],
      ", ".join(str(s.get("name")) for s in tsteps))
# Round 2: a guard that refuses no longer stops the run (an older pass would stay): the gate posts a failure instead.
check("both guards go on when they refuse (their answer, guard=fail, makes the gate post only failures)",
      all(s.get("continue-on-error") is True for s in (gsteps + [{}] * 3)[2:3] + (tsteps + [{}] * 3)[2:3]))
check("no other step of the guard job or before finish in the gate job goes on after a failure (a failure is seen)",
      all("continue-on-error" not in s for i, s in enumerate(gsteps) if i != 2)
      and all("continue-on-error" not in s for i, s in enumerate(tsteps) if i not in (2, 4)))
last = (gsteps + [{}] * 5)[4]
r = bash(last.get("run", "exit 0"), {})
check("guard job: no answer from the guard (no Environment, or it crashed) fails the job visibly: no token, no post",
      last.get("if") == "steps.guard.outputs.guard == ''" and r.returncode == 1 and "::error::head gate" in r.stdout
      and "dispatched again" in r.stdout, f"{last.get('if')} {r.returncode} {r.stdout}")

# --- the toolkit: this workflow's own commit, never the caller's tree -----------------------------------------------
SHA = "0123456789abcdef0123456789abcdef01234567"
GOOD_JOB = '{"status": "success", "workflow_repository": "owner/toolkit", "workflow_sha": "%s"}' % SHA
for job_name, steps in (("guard", gsteps), ("gate", tsteps)):
    first = steps[0] if steps else {}
    check(f"{job_name}: the first step finds this workflow's own commit, before anything is checked out",
          first.get("id") == "toolkit" and "uses" not in first and first.get("env") == {"JOB_CONTEXT": "${{ toJSON(job) }}"})

    def toolkit(job_context, first=first):
        with tempfile.NamedTemporaryFile("r", prefix="fork-head-gate-out-") as out:
            r = bash(first.get("run", "exit 0"), {"JOB_CONTEXT": job_context, "GITHUB_OUTPUT": out.name})
            return r, out.read()

    r, out = toolkit(GOOD_JOB)
    check(f"{job_name}: a good job context names the toolkit's repository and commit",
          r.returncode == 0 and out == f"repo=owner/toolkit\nsha={SHA}\n", f"{r.returncode} {r.stdout} {out!r}")
    for label, ctx in (("an empty job context", ""), ("no workflow_sha (an old runner)", '{"status": "success"}'),
                       ("a sha that is a branch name", GOOD_JOB.replace(SHA, "main")),
                       ("a repository that is no owner/name", GOOD_JOB.replace("owner/toolkit", "owner/toolkit/x")),
                       ("no JSON", "{")):
        r, out = toolkit(ctx)
        check(f"{job_name}: {label} fails: it never guesses which gate to run",
              r.returncode == 1 and out == "" and "will not guess" in r.stdout, f"{r.returncode} {r.stdout}")
    co = steps[1] if len(steps) > 1 else {}
    cw = co.get("with") or {}
    check(f"{job_name}: the gate is checked out at this workflow's own repository and commit, pinned, and nothing else",
          re.fullmatch(r"actions/checkout@[0-9a-f]{40}", str(co.get("uses"))) is not None
          and cw.get("repository") == "${{ steps.toolkit.outputs.repo }}"
          and cw.get("ref") == "${{ steps.toolkit.outputs.sha }}" and cw.get("path") == "toolkit"
          and set(cw) == {"repository", "ref", "path", "sparse-checkout"}
          and str(cw.get("sparse-checkout")).split() == ["fork-ci/actions/head-check", "fork-ci/actions/pr-queue"], str(cw))
check("both jobs find the toolkit with the same script", gsteps[:1] and tsteps[:1]
      and gsteps[0].get("run") == tsteps[0].get("run"))

# --- the steps' scripts, run with a fake python3 --------------------------------------------------------------------
fake = tempfile.mkdtemp(prefix="fork-head-gate-bin-")
with open(os.path.join(fake, "python3"), "w") as f:
    f.write('#!/bin/sh\nfor a in "$@"; do printf "[%s]" "$a"; done\necho\n'
            'echo "GH_TOKEN=${GH_TOKEN:-unset} SECRET=${REPO_SECRET_PRESENT-unset}"\n')
os.chmod(os.path.join(fake, "python3"), 0o755)


def run(step, **env):
    return bash(step.get("run", "exit 0"), {"RUNNER_TEMP": "/rt", **{k: str(v) for k, v in env.items()}}, path=fake)


def args(*a):
    return "".join(f"[{x}]" for x in a)


genv, prs = (gsteps[2] if len(gsteps) > 2 else {}), (gsteps[3] if len(gsteps) > 3 else {})
check("guard: the job's token reads, and the key's presence is passed as true or false, never the key",
      (genv.get("env") or {}) == {"GH_TOKEN": "${{ github.token }}", "REPO": "${{ github.repository }}",
                                  "ENVIRONMENT": "${{ inputs.environment }}",
                                  "REPO_SECRET_PRESENT": "${{ secrets.HEAD_GATE_APP_KEY != '' }}"}, str(genv.get("env")))
r = run(genv, REPO="o/p", ENVIRONMENT="head-gate", REPO_SECRET_PRESENT="false", GH_TOKEN="t")
check("guard: it runs the whole guard",
      r.returncode == 0 and r.stdout.startswith(args("-I", "-B", "toolkit/fork-ci/actions/head-check/head_gate.py",
                                                     "guard", "--repo", "o/p", "--environment", "head-gate") + "\n")
      and "SECRET=false" in r.stdout, r.stdout + r.stderr)
r = run(prs, REPO="o/p", EVENT="workflow_dispatch", PR="")
check("prs: the event and the number go on as they are (an empty number is the backfill)",
      r.returncode == 0 and r.stdout.startswith(args("-I", "-B", "toolkit/fork-ci/actions/head-check/head_gate.py",
                                                     "prs", "--repo", "o/p", "--event", "workflow_dispatch", "--pr", "",
                                                     "--event-sha", "", "--run-event", "", "--run-sha", "", "--run-repo", "",
                                                     "--run-branch=") + "\n"), r.stdout)
r = run(prs, REPO="o/p", EVENT="pull_request_target", PR="7", EVENT_SHA="ab" * 20)
check("prs: the head the event carries goes on too (used only when the pull request read fails every try)",
      r.returncode == 0 and r.stdout.startswith(args("-I", "-B", "toolkit/fork-ci/actions/head-check/head_gate.py",
                                                     "prs", "--repo", "o/p", "--event", "pull_request_target", "--pr",
                                                     "7", "--event-sha", "ab" * 20, "--run-event", "", "--run-sha", "",
                                                     "--run-repo", "", "--run-branch=") + "\n"), r.stdout)
r = run(prs, REPO="o/p", EVENT="workflow_run", PR="", RUN_EVENT="pull_request_review", RUN_SHA="cd" * 20,
        RUN_REPO="f/p", RUN_BRANCH="-x y")
check("prs: a review trigger's run goes on as its head (event, sha, repository, branch); a branch is one --run-branch=",
      r.returncode == 0 and r.stdout.startswith(args("-I", "-B", "toolkit/fork-ci/actions/head-check/head_gate.py",
                                                     "prs", "--repo", "o/p", "--event", "workflow_run", "--pr", "",
                                                     "--event-sha", "", "--run-event", "pull_request_review",
                                                     "--run-sha", "cd" * 20, "--run-repo", "f/p",
                                                     "--run-branch=-x y") + "\n"), r.stdout)
check("prs: the event comes from the run and the number from the caller; a workflow_run's head is GitHub's field of "
      "the finished run, never its outputs",
      (prs.get("env") or {}) == {"GH_TOKEN": "${{ github.token }}", "REPO": "${{ github.repository }}",
                                 "EVENT": "${{ github.event_name }}", "PR": "${{ inputs.pr }}",
                                 "EVENT_SHA": "${{ github.event.pull_request.head.sha }}",
                                 "RUN_EVENT": "${{ github.event.workflow_run.event }}",
                                 "RUN_SHA": "${{ github.event.workflow_run.head_sha }}",
                                 "RUN_REPO": "${{ github.event.workflow_run.head_repository.full_name }}",
                                 "RUN_BRANCH": "${{ github.event.workflow_run.head_branch }}"}, str(prs.get("env")))

eguard, target, drift, token, start, judge, finish = (tsteps + [{}] * 9)[2:9]
check("gate: the Environment is checked again right before the token, without the secret's presence",
      "--environment-only" in str(eguard.get("run")) and "REPO_SECRET_PRESENT" not in (eguard.get("env") or {})
      and "secrets." not in str(eguard))
RUN_ENV = {"GH_TOKEN": "${{ github.token }}", "REPO": "${{ github.repository }}", "SHA": "${{ matrix.sha }}",
           "REQUIRED_LENSES": "${{ inputs.required-lenses }}", "CALLER_REF": "${{ github.workflow_ref }}",
           "CALLER_SHA": "${{ github.workflow_sha }}", "GATE_REPO": "${{ steps.toolkit.outputs.repo }}",
           "GATE_SHA": "${{ steps.toolkit.outputs.sha }}", "RUN_ATTEMPT": "${{ github.run_attempt }}",
           "GUARD": "${{ needs.guard.outputs.guard }}", "ENVIRONMENT_GUARD": "${{ steps.envguard.outputs.guard }}"}
check("target: the head is the matrix's, the caller and gate commits and the attempt the run's; the job's token reads",
      (target.get("env") or {}) == RUN_ENV and "if" not in target, str(target.get("env")))
check("judge: the same, plus the config; the job's token reads",
      (judge.get("env") or {}) == {**RUN_ENV, "CONFIG": "${{ inputs.config }}"}, str(judge.get("env")))
lenses = "correctness\ndata-integrity (DI-1, DI-2)"
SHA_H = "ab" * 20
renv = dict(REPO="o/p", SHA=SHA_H, REQUIRED_LENSES=lenses, CALLER_REF="o/p/.github/workflows/g.yml@refs/heads/main",
            CALLER_SHA="cd" * 20, GATE_REPO="t/k", GATE_SHA="ef" * 20, RUN_ATTEMPT="1", GUARD="ok",
            ENVIRONMENT_GUARD="fail")
run_args_ = ("--repo", "o/p", "--sha", SHA_H, "--required-lenses", lenses, "--caller-ref", renv["CALLER_REF"],
             "--caller-sha", "cd" * 20, "--gate-repo", "t/k", "--gate-sha", "ef" * 20, "--run-attempt", "1",
             "--guard", "ok", "--environment-guard", "fail")
r = run(target, **renv)
check("target: every input is one argument",
      r.returncode == 0 and r.stdout.startswith(args("-I", "-B", "toolkit/fork-ci/actions/head-check/head_gate.py",
                                                     "target", *run_args_) + "\n"), r.stdout + r.stderr)
r = run(judge, CONFIG=".github/q.json", **renv)
check("judge: every input is one argument; the verdict goes to the runner temp for finish",
      r.returncode == 0 and r.stdout.startswith(args(
          "-I", "-B", "toolkit/fork-ci/actions/head-check/head_gate.py", "judge", "--repo", "o/p", "--sha", SHA_H,
          "--config", ".github/q.json", *run_args_[4:], "--work", "/rt", "--verdict", "/rt/head-gate-verdict.json")
          + "\n"), r.stdout + r.stderr)
check("token and start run when target says to post (a judged head, or a failure: re-run, old caller, guard)",
      all(s.get("if") == "steps.target.outputs.post == 'true'" for s in (token, start)))
check("drift runs only for a judged head (target names its base)", drift.get("if") == "steps.target.outputs.base != ''")
check("judge runs only after start posted its in-progress check run",
      judge.get("if") == "steps.start.outputs.id != ''")
check("finish runs whenever start posted, whatever happened after (always()): a stop leaves a failure, never an old pass",
      finish.get("if") == "always() && steps.start.outputs.id != ''")
check("drift only warns: it cannot fail the job, and has no secret",
      drift.get("continue-on-error") is True and "secrets." not in str(drift)
      and (drift.get("env") or {}).get("BRANCH") == "${{ steps.target.outputs.base }}"
      and (drift.get("env") or {}).get("APP_ID") == "${{ vars.HEAD_GATE_APP_ID }}")
tw = token.get("with") or {}
check("token: the App token is made pinned by commit, for this repository only, with checks write only",
      re.fullmatch(r"actions/create-github-app-token@[0-9a-f]{40}", str(token.get("uses"))) is not None
      and tw == {"app-id": "${{ vars.HEAD_GATE_APP_ID }}", "private-key": "${{ secrets.HEAD_GATE_APP_KEY }}",
                 "owner": "${{ github.repository_owner }}", "repositories": "${{ github.event.repository.name }}",
                 "permission-checks": "write"}, str(tw))
check("start and finish: the App token reaches only these two steps; judge reads with the job's token",
      (start.get("env") or {}).get("GH_TOKEN") == "${{ steps.token.outputs.token }}"
      and (finish.get("env") or {}).get("GH_TOKEN") == "${{ steps.token.outputs.token }}"
      and text.count("steps.token.outputs.token") == 2)
check("the key is named twice only: the guard's presence test and the token step",
      text.count("secrets.") == 2 and text.count("secrets.HEAD_GATE_APP_KEY") == 2)
r = run(start, GH_TOKEN="app-token", REPO="o/p", SHA=SHA_H, CHECK_NAME="review-check")
check("start: it posts the in-progress check run on the matrix's head, with the App token",
      r.returncode == 0 and r.stdout.startswith(args("-I", "-B", "toolkit/fork-ci/actions/head-check/head_gate.py",
                                                     "start", "--repo", "o/p", "--sha", SHA_H, "--check-name",
                                                     "review-check") + "\n")
      and "GH_TOKEN=app-token" in r.stdout, r.stdout)
check("finish: the check run is start's, the head the matrix's",
      (finish.get("env") or {}) == {"GH_TOKEN": "${{ steps.token.outputs.token }}", "REPO": "${{ github.repository }}",
                                    "SHA": "${{ matrix.sha }}", "CHECK_RUN": "${{ steps.start.outputs.id }}"})
r = run(finish, GH_TOKEN="app-token", REPO="o/p", SHA=SHA_H, CHECK_RUN="42")
check("finish: it completes that check run with the verdict judge wrote, with the App token",
      r.returncode == 0 and r.stdout.startswith(args("-I", "-B", "toolkit/fork-ci/actions/head-check/head_gate.py",
                                                     "finish", "--repo", "o/p", "--sha", SHA_H, "--id", "42",
                                                     "--verdict", "/rt/head-gate-verdict.json") + "\n")
      and "GH_TOKEN=app-token" in r.stdout, r.stdout)


# --- the example caller ---------------------------------------------------------------------------------------------
ex = yaml.safe_load(EXAMPLE.read_text())
eon = ex.get(True, ex.get("on")) or {}
check("example: it runs on pull_request_target, issue_comment, workflow_run and workflow_dispatch only (never "
      "pull_request, never a review event: those run the pull request's own copy)",
      set(eon) == {"pull_request_target", "issue_comment", "workflow_run", "workflow_dispatch"}, str(set(eon)))
check("example: every change of the head, the draft state and the title or base starts it",
      set((eon.get("pull_request_target") or {}).get("types") or []) >= {"opened", "synchronize", "reopened",
                                                                          "ready_for_review", "converted_to_draft", "edited"})
check("example: a lens comment written, edited or deleted starts it",
      set((eon.get("issue_comment") or {}).get("types") or []) == {"created", "edited", "deleted"})
check("example: a dispatch takes one pull request or none (the backfill)",
      set(((eon.get("workflow_dispatch") or {}).get("inputs") or {})) == {"pr"})
ejobs = ex.get("jobs") or {}
ejob = next(iter(ejobs.values()), {})
check("example: no token by default; the one job calls the gate with what its jobs read",
      ex.get("permissions") == {} and len(ejobs) == 1 and ejob.get("permissions") == READ)
uses = str(ejob.get("uses"))
check("example: it calls this workflow of the fork, pinned by a release (never a branch)",
      uses.startswith("scuttletoby/t3code/.github/workflows/fork-head-gate.yml@")
      and not re.search(r"@(main|master|ci-release|ci/v\d+)$", uses), uses)
check("example: secrets: inherit (so the guard can see a repository secret of the key's name), and says why",
      ejob.get("secrets") == "inherit" and "secrets: inherit" in EXAMPLE.read_text()
      and "Needed:" in EXAMPLE.read_text())
check("example: no pass-drafts anywhere (a draft's check run would count for a ready pull request at its head)",
      "pass-drafts" not in EXAMPLE.read_text() and "pass-drafts" not in text)
# The tuple as Python reads it (any quoting), from head_gate.py's top-level assignment; [] if it is not a literal.
REVIEW_EVENTS = next((n.value for n in ast.parse(GATE_PY.read_text()).body if isinstance(n, ast.Assign)
                      and [getattr(t, "id", None) for t in n.targets] == ["REVIEW_EVENTS"]), None)
try:
    REVIEW_EVENTS = list(ast.literal_eval(REVIEW_EVENTS)) if REVIEW_EVENTS is not None else []
except (ValueError, TypeError, SyntaxError):
    REVIEW_EVENTS = []
check("example: an issue comment that is not on a pull request starts nothing, nor a review trigger run that did "
      "not succeed (a newer one replaced it) or that another event started (#379: skipped before a guard job "
      "starts; the events are head_gate's REVIEW_EVENTS)",
      REVIEW_EVENTS == ["pull_request_review", "pull_request_review_comment"]
      and ejob.get("if") == "(github.event_name != 'issue_comment' || github.event.issue.pull_request) && "
      "(github.event_name != 'workflow_run' || (github.event.workflow_run.conclusion == 'success' && "
      "contains(fromJSON('" + json.dumps(REVIEW_EVENTS, separators=(",", ":")) + "'), "
      "github.event.workflow_run.event)))", ejob.get("if"))
ew = ejob.get("with") or {}
required = {k for k, v in inputs.items() if v.get("required")}
check("example: it gives only inputs the gate has, and every required one",
      set(ew) <= set(inputs) and required <= set(ew), f"{sorted(ew)} vs {sorted(inputs)}")
check("example: the pull request is the event's number, as text (none on a workflow_run: the gate looks it up)",
      ew.get("pr") == "${{ format('{0}', github.event.pull_request.number || github.event.issue.number || inputs.pr) }}")
needed = {}
for j in jobs.values():
    for scope, level in (j.get("permissions") or {}).items():
        needed[scope] = max(needed.get(scope, "none"), level, key=["none", "read", "write"].index)
check("example: it grants every permission the gate's jobs ask for (else GitHub refuses the run before it starts)",
      all(["none", "read", "write"].index((ejob.get("permissions") or {}).get(s, "none"))
          >= ["none", "read", "write"].index(lv) for s, lv in needed.items()), str(needed))

# --- the review trigger and the caller's workflow_run (issue #88) ---------------------------------------------------
trigger_text = TRIGGER.read_text()
tr = yaml.safe_load(trigger_text)
ton = tr.get(True, tr.get("on")) or {}
wr = eon.get("workflow_run") or {}
check("example: workflow_run of the review trigger, by its name, when its run completed",
      wr == {"workflows": [tr.get("name")], "types": ["completed"]} and tr.get("name") == "Head gate review trigger",
      str(wr))
check("trigger: a review submitted, edited or dismissed and an inline review comment created, edited or deleted, "
      "nothing else",
      ton == {"pull_request_review": {"types": ["submitted", "edited", "dismissed"]},
              "pull_request_review_comment": {"types": ["created", "edited", "deleted"]}}, str(ton))
tjobs = tr.get("jobs") or {}
tsteps_ = [s for j in tjobs.values() for s in (j.get("steps") or [])]
check("trigger: no token, no secrets, no Environment, no other workflow, no action: one job that only finishes",
      tr.get("permissions") == {} and len(tjobs) == 1
      and all(j.get("permissions") == {} and not ({"environment", "uses", "secrets", "needs", "services",
                                                   "container"} & set(j)) for j in tjobs.values())
      and len(tsteps_) == 1 and set(tsteps_[0]) == {"name", "shell", "run"}
      and "${{" not in str(tsteps_[0].get("run")) and "secrets" not in trigger_text.replace("no secrets", "")
      and "checkout" not in trigger_text, str(tjobs))
r = bash(tsteps_[0].get("run", "exit 1") if tsteps_ else "exit 1", {})
check("trigger: its one step finishes (the caller starts only on a run that succeeded)", r.returncode == 0, r.stderr)
tconc = tr.get("concurrency") or {}
check("trigger: a burst on one pull request leaves one run (per pull request, the older ones cancelled)",
      tconc == {"group": "head-gate-review-trigger-${{ github.event.pull_request.number }}", "cancel-in-progress": True},
      str(tconc))

econc = ejob.get("concurrency") or {}
check("example: the caller is never cancelled half-way (a cancel would post a failure on the head)",
      econc.get("cancel-in-progress") is False, str(econc))


def caller_group(event, run_id, sha="", workflow_ref="o/p/.github/workflows/gate.yml@refs/heads/main"):
    """The caller's concurrency group as GitHub renders it (`a && b || c` of non-empty strings)."""
    m = re.fullmatch(r"\$\{\{ github\.event_name == 'workflow_run' && format\('([^']*)', github\.workflow_ref, "
                     r"github\.event\.workflow_run\.head_sha\) \|\| format\('([^']*)', github\.run_id\) \}\}",
                     str(econc.get("group")))
    if not m:
        return None
    if event == "workflow_run":
        return m.group(1).replace("{0}", workflow_ref).replace("{1}", sha)
    return m.group(2).replace("{0}", str(run_id))


check("example: the caller's group is read here as GitHub reads it", caller_group("push", 1) is not None,
      str(econc.get("group")))
if caller_group("push", 1) is not None:
    burst = [(("review", i), caller_group("workflow_run", i, H)) for i in range(20)]
    lost = cancelled_by_queue(burst)
    check("example: a burst of 20 review trigger runs on one head: the first judges, the newest waits and judges, the "
          "18 in between are dropped while they wait",
          sorted(lost) == [("review", i) for i in range(1, 19)], str(lost))
    mixed = [(("review", 0), caller_group("workflow_run", 100, H)), (("comment", 1), caller_group("issue_comment", 101)),
             (("review", 2), caller_group("workflow_run", 102, H)), (("push", 3), caller_group("pull_request_target", 103)),
             (("review", 4), caller_group("workflow_run", 104, H)), (("other-head", 5), caller_group("workflow_run", 105, "b2" * 20)),
             (("other-gate", 6), caller_group("workflow_run", 106, H, "o/p/.github/workflows/gate2.yml@refs/heads/main"))]
    lost = cancelled_by_queue(mixed)
    check("example: in the caller's group, review runs never drop a comment's, a push's, another head's or another "
          "caller's run (the gate job's own group is per head: a newer job there reads the newest state)",
          lost == [("review", 2)], str(lost))

if failures:
    print(f"\n{len(failures)} failed")
    sys.exit(1)
print("\nall passed")
