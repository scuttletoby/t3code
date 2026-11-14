"""Checks .github/workflows/fork-head-gate.yml and its example caller (fork-ci/actions/head-check/gate-caller.example.yml):
the step scripts are run as Actions runs them, the wiring is read from the files.

    python3 .github/scripts/fork-head-gate-workflow.test.py

Needs PyYAML and bash. Each check prints ok/FAIL; any FAIL exits 1. Nothing here starts gh or the gate: a fake
python3 stands in for head_gate.py. What makes the key reachable only by the start and finish steps is checked here and,
against every fork workflow, in fork-contracts.test.py (head-gate).
"""

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
check("one gate run per HEAD at a time (a check run belongs to the commit), never cancelled half-way",
      conc.get("group") == "head-gate-${{ github.repository }}-${{ matrix.sha }}"
      and conc.get("cancel-in-progress") is False, str(conc))
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
                                                     "--event-sha", "") + "\n"), r.stdout)
r = run(prs, REPO="o/p", EVENT="pull_request_target", PR="7", EVENT_SHA="ab" * 20)
check("prs: the head the event carries goes on too (used only when the pull request read fails every try)",
      r.returncode == 0 and r.stdout.startswith(args("-I", "-B", "toolkit/fork-ci/actions/head-check/head_gate.py",
                                                     "prs", "--repo", "o/p", "--event", "pull_request_target", "--pr",
                                                     "7", "--event-sha", "ab" * 20) + "\n"), r.stdout)
check("prs: the event comes from the run and the number from the caller",
      (prs.get("env") or {}) == {"GH_TOKEN": "${{ github.token }}", "REPO": "${{ github.repository }}",
                                 "EVENT": "${{ github.event_name }}", "PR": "${{ inputs.pr }}",
                                 "EVENT_SHA": "${{ github.event.pull_request.head.sha }}"}, str(prs.get("env")))

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
check("example: it runs on pull_request_target, issue_comment and workflow_dispatch only (never pull_request)",
      set(eon) == {"pull_request_target", "issue_comment", "workflow_dispatch"}, str(set(eon)))
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
check("example: an issue comment that is not on a pull request starts nothing",
      ejob.get("if") == "github.event_name != 'issue_comment' || github.event.issue.pull_request")
ew = ejob.get("with") or {}
required = {k for k, v in inputs.items() if v.get("required")}
check("example: it gives only inputs the gate has, and every required one",
      set(ew) <= set(inputs) and required <= set(ew), f"{sorted(ew)} vs {sorted(inputs)}")
check("example: the pull request is the event's number, as text, from all three events",
      ew.get("pr") == "${{ format('{0}', github.event.pull_request.number || github.event.issue.number || inputs.pr) }}")
needed = {}
for j in jobs.values():
    for scope, level in (j.get("permissions") or {}).items():
        needed[scope] = max(needed.get(scope, "none"), level, key=["none", "read", "write"].index)
check("example: it grants every permission the gate's jobs ask for (else GitHub refuses the run before it starts)",
      all(["none", "read", "write"].index((ejob.get("permissions") or {}).get(s, "none"))
          >= ["none", "read", "write"].index(lv) for s, lv in needed.items()), str(needed))

if failures:
    print(f"\n{len(failures)} failed")
    sys.exit(1)
print("\nall passed")
