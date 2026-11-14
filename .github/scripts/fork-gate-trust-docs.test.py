"""Tests docs/fork/gate-trust.md: the owner's paste commands, run for real against a fake `gh`.

    python3 .github/scripts/fork-gate-trust-docs.test.py

The commands live only in that page. Each block marked `<!-- owner-step: NAME -->` is taken from it as
written, its CAPITAL placeholders are filled in, and bash runs it with a fake `gh` first in PATH that
answers like GitHub from a JSON state and logs every call. So the `jq` parts and the `gh` calls the owner
pastes are the ones tested. Each block is run twice (safe to run again), on old shapes (no rule, two
rules, a check bound to another App, a placeholder left in, an Environment with other rules) and with a
GitHub read that fails. The page's claims of what the gate posts (Environment widened, deleted, key
deleted first, classic protection) are checked against head_gate's own guard and drift, and each ordered
procedure (App made again, gate key, rollout) by the order of its steps. Step 4 runs with a fake curl. Then: every relative link of the fork pages this touches resolves (anchors too), the page names nothing
personal, and no other page carries a copy of the commands.

Needs bash and jq. Reaches no network and no GitHub. Each check prints ok/FAIL; any FAIL exits 1.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
root = pathlib.Path(__file__).resolve().parents[2]
PAGE = root / "docs/fork/gate-trust.md"
failures = []
BASE = tempfile.TemporaryDirectory(prefix="gate-trust-")  # every run's folder, removed at exit


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


# --- the fake gh -------------------------------------------------------------------------------------------------
# State (FAKE_GH_STATE, a JSON file): {"paths": {path: body}, "fail": [path, ...], "envs": {...}, "vars": {...},
# "secrets": {...}}. Every call is appended to FAKE_GH_LOG as one JSON line: argv, method, path, body, stdin.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
state_file, log_file = os.environ["FAKE_GH_STATE"], os.environ["FAKE_GH_LOG"]
state = json.load(open(state_file))
argv = sys.argv[1:]
entry = {"argv": argv}

def done(code, out=None, err=None):
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")
    json.dump(state, open(state_file, "w"))
    if out is not None:
        print(json.dumps(out))
    if err:
        print(err, file=sys.stderr)
    sys.exit(code)

def fields(args):
    out, i = {}, 0
    while i < len(args):
        if args[i] in ("-f", "-F"):
            k, v = args[i + 1].split("=", 1)
            if args[i] == "-F" and v in ("true", "false"):
                v = v == "true"
            out[k] = v
            i += 2
        else:
            i += 1
    return out

if argv[:1] == ["api"]:
    args, method, path, inp, jq = argv[1:], None, None, None, None
    i = 0
    rest = []
    while i < len(args):
        a = args[i]
        if a == "-X":
            method = args[i + 1]; i += 2
        elif a == "--input":
            inp = args[i + 1]; i += 2
        elif a == "--jq":
            jq = args[i + 1]; i += 2
        elif a in ("-f", "-F"):
            rest += args[i:i + 2]; i += 2
        elif a.startswith("-"):
            done(2, err="fake gh: flag not known here: " + a)
        else:
            path = a; i += 1
    f = fields(rest)
    method = method or ("POST" if f or inp else "GET")
    entry.update(method=method, path=path)
    if path in state.get("fail", []):
        done(1, err="HTTP 404: Not Found (" + path + ")")
    if "/deployment-branch-policies" in path:
        env = path.split("/environments/")[1].split("/")[0]
        pols = state["envs"].get(env, {}).get("policies")
        if method == "GET" and path.endswith("/deployment-branch-policies") and pols is not None:
            state["paths"][path] = {"total_count": len(pols), "branch_policies": pols}
        if method == "DELETE":
            pid = path.rsplit("/", 1)[1]
            if pols is None or pid not in [str(p["id"]) for p in pols]:
                done(1, err="HTTP 404: Not Found (" + path + ")")
            pols[:] = [p for p in pols if str(p["id"]) != pid]
            done(0)
    if method == "GET" and jq is not None and path in state["paths"]:
        import subprocess
        r = subprocess.run(["jq", "-r", jq], input=json.dumps(state["paths"][path]), capture_output=True, text=True)
        entry["jq"] = jq
        with open(log_file, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)
    if jq is not None:
        done(2, err="fake gh: --jq only on a GET here")
    if method == "GET":
        if path not in state["paths"]:
            done(1, err="HTTP 404: Not Found (" + path + ")")
        done(0, out=state["paths"][path])
    if inp is not None:
        text = sys.stdin.read() if inp == "-" else open(inp).read()
        try:
            body = json.loads(text)
        except ValueError:
            done(1, err="HTTP 400: Problems parsing JSON")
        entry["body"] = body
    if "/rulesets/" in path and method == "PUT":
        allowed = {"name", "target", "enforcement", "conditions", "bypass_actors", "rules"}
        if not isinstance(body, dict) or set(body) - allowed:
            done(1, err="HTTP 422: Invalid request (keys %s)" % sorted(set(body) - allowed))
        new = dict(state["paths"][path]); new.update(body)
        state["paths"][path] = new
        done(0, out=new)
    if path.endswith("/protection/required_status_checks") and method == "PATCH":
        if not isinstance(body, dict) or set(body) - {"strict", "checks"}:
            done(1, err="HTTP 422: Invalid request")
        old = state["paths"][path]
        new = {"url": old.get("url"), "strict": body["strict"], "contexts": [c["context"] for c in body["checks"]],
               "checks": body["checks"]}
        state["paths"][path] = new
        done(0, out=new)
    if path.endswith("/deployment-branch-policies") and method == "POST":
        env = path.split("/environments/")[1].split("/")[0]
        pols = state["envs"].setdefault(env, {}).setdefault("policies", [])
        if f.get("name") in [p["name"] for p in pols]:
            done(1, err="HTTP 303: a policy with this name pattern exists")
        state["next_id"] = state.get("next_id", 100) + 1
        pols.append({"id": state["next_id"], "name": f["name"], "type": f.get("type")})
        done(0, out=pols[-1])
    if "/environments/" in path and method == "PUT":
        env = path.split("/environments/")[1]
        e = state["envs"].setdefault(env, {})
        e["deployment_branch_policy"] = {k[len("deployment_branch_policy["):-1]: v for k, v in f.items()
                                         if k.startswith("deployment_branch_policy[")}
        done(0, out={"name": env})
    done(1, err="fake gh: no route for %s %s" % (method, path))

if argv[:2] in (["variable", "set"], ["secret", "set"]):
    name, args = argv[2], argv[3:]
    opts = dict(zip(args[0::2], args[1::2]))
    if "--body" in opts:
        value = opts["--body"]
    else:
        value = sys.stdin.read()
        entry["stdin_len"] = len(value)
    kind = "vars" if argv[0] == "variable" else "secrets"
    state[kind][opts["--env"] + "/" + name] = value
    done(0)

done(2, err="fake gh: command not known here: %s" % argv)
'''


def blocks():
    text = PAGE.read_text()
    return {m.group(1): m.group(2) for m in
            re.finditer(r"<!-- owner-step: (\S+) -->\s*\n```sh\n(.*?)```", text, re.S)}


SUBS = [  # placeholder -> value, as the owner fills them in
    ("OWNER/REPO", "acme/widgets"),
    ("POLICY_ID", "0"),
    ("DEFAULT_BRANCH", "main"),
    ("BRANCH", "main"),
    ("RULESET_ID", "42"),
    ("CHECK_NAME", "Head gate / review"),
    ("ANY_SOURCE_APP_ID", "15368"),
    ("APP_ID", "777777"),
]
NOT_PLACEHOLDERS = {"HEAD_GATE_APP_ID", "HEAD_GATE_APP_KEY", "GET", "PUT", "POST", "PATCH", "DELETE"}


def fill(block, subs):
    for k, v in subs:
        block = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(k) + r"(?![A-Za-z0-9_])", v, block)
    return block


def leftover(block):
    return sorted(set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", block)) - NOT_PLACEHOLDERS)


def run(block, state, subs=SUBS, tmp=None):
    """Run one filled-in block with the fake gh; returns (exit code, stderr, calls, state after, its folder), and
    keeps what it printed in the folder's stdout.txt."""
    tmp = pathlib.Path(tmp or tempfile.mkdtemp(dir=BASE.name))
    (tmp / "bin").mkdir(exist_ok=True)
    gh = tmp / "bin/gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    (tmp / "key.pem").write_text("-----FAKE KEY-----\nnot a real key\n")
    sf, lf = tmp / "state.json", tmp / "log.jsonl"
    sf.write_text(json.dumps(state))
    lf.write_text("")
    subs = [(k, str(tmp / "key.pem") if k == "PATH_TO_DOWNLOADED_KEY" else v) for k, v in subs]
    if not any(k == "PATH_TO_DOWNLOADED_KEY" for k, _ in subs):
        subs.append(("PATH_TO_DOWNLOADED_KEY", str(tmp / "key.pem")))
    script = fill(block, subs)
    env = {"PATH": f"{tmp / 'bin'}:/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp), "TMPDIR": str(tmp),
           "FAKE_GH_STATE": str(sf), "FAKE_GH_LOG": str(lf), "LC_ALL": "C.UTF-8"}
    p = subprocess.run(["bash", "--noprofile", "--norc", "-c", script], cwd=tmp, env=env,
                       capture_output=True, text=True, timeout=60)
    (tmp / "stdout.txt").write_text(p.stdout)
    calls = [json.loads(line) for line in lf.read_text().splitlines()]
    return p.returncode, p.stderr, calls, json.loads(sf.read_text()), tmp


def writes(calls):
    return [c for c in calls if c.get("method") in ("PUT", "PATCH", "POST")]


def empty_state(paths=None, fail=()):
    return {"paths": paths or {}, "fail": list(fail), "envs": {}, "vars": {}, "secrets": {}}


RS = "repos/acme/widgets/rulesets/42"
CL = "repos/acme/widgets/branches/main/protection/required_status_checks"
GATE = "Head gate / review"


def ruleset(checks=None, rsc_rules=1, bypass=True):
    rules = [{"type": "deletion"},
             {"type": "pull_request", "parameters": {"required_approving_review_count": 0,
                                                     "dismiss_stale_reviews_on_push": False}}]
    for _ in range(rsc_rules):
        rules.append({"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": True, "do_not_enforce_on_create": False,
            "required_status_checks": checks if checks is not None else
            [{"context": "ci-result", "integration_id": 15368}, {"context": GATE}]}})
    r = {"id": 42, "name": "main: CI before merge", "target": "branch", "source_type": "Repository",
         "source": "acme/widgets", "enforcement": "active",
         "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}, "rules": rules,
         "node_id": "RRS_x", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
         "_links": {"self": {"href": "https://api.github.com/" + RS}}, "current_user_can_bypass": "always"}
    if bypass:
        r["bypass_actors"] = [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
    return r


def rsc_of(body):
    return [r["parameters"]["required_status_checks"] for r in body["rules"]
            if r["type"] == "required_status_checks"]


b = blocks()
check(set(b) == {"environment", "environment-rules", "environment-rule-delete", "bind-ruleset", "bind-classic",
                 "classic-check"}, f"the page marks the six tested blocks (found {sorted(b)})")
check(shutil.which("jq") is not None and shutil.which("bash") is not None, "jq and bash are on PATH")

# Every placeholder the tested blocks use is filled in by the test and listed in the page's placeholder table.
table = PAGE.read_text().split("| Placeholder", 1)[1].split("\n\n", 1)[0]
listed = set(re.findall(r"`([A-Z][A-Z0-9_/]+)`", table))
listed = {part for item in listed for part in item.split("/")}
for name, block in b.items():
    used = set(leftover(block))
    check(used <= listed, f"{name}: every placeholder is in the page's table (missing {sorted(used - listed)})")
    check(not leftover(fill(block, SUBS + [("PATH_TO_DOWNLOADED_KEY", "/k")])),
          f"{name}: the test fills in every placeholder (left {leftover(fill(block, SUBS + [('PATH_TO_DOWNLOADED_KEY', '/k')]))})")

every_block = re.findall(r"```sh\n(.*?)```", PAGE.read_text(), re.S)
unlisted = sorted({p for block in every_block for p in leftover(block)} - listed)
check(not unlisted, f"every placeholder of every command block on the page is in its table (missing {unlisted})")

# --- step 3: Environment, App id and key ---------------------------------------------------------------------------
code, err, calls, st, tmp = run(b["environment"], empty_state())
check(code == 0, f"environment: runs clean ({err.strip()[:200]})")
env = st["envs"].get("head-gate", {})
check(env.get("deployment_branch_policy") == {"protected_branches": False, "custom_branch_policies": True},
      f"environment: custom branch rules only, not 'protected branches' ({env.get('deployment_branch_policy')})")
check([{k: p[k] for k in ("name", "type")} for p in env.get("policies", [])] == [{"name": "main", "type": "branch"}],
      f"environment: exactly one deployment rule, the default branch as a branch ({env.get('policies')})")
check(st["vars"] == {"head-gate/HEAD_GATE_APP_ID": "777777"}, f"environment: the App id variable ({st['vars']})")
check(st["secrets"] == {"head-gate/HEAD_GATE_APP_KEY": (tmp / "key.pem").read_text()},
      "environment: the key secret is the file's text, in the Environment")
check(not any("not a real key" in a for c in calls for a in c["argv"]),
      "environment: the key text is never an argument (stdin only)")
code2, err2, calls2, st2, _ = run(b["environment"], st)
check(st2 == st, "environment: run twice, same state (still one deployment rule)")
check([c["argv"] for c in calls2] == [c["argv"] for c in calls],
      "environment: run twice, the same four calls; a refused second POST stops nothing after it")

# Step 3 on an Environment that already had other rules (finding 4): they stay, the read-back shows them, the delete
# by id removes them, and the read-back's "exactly one line, branch DEFAULT_BRANCH" is the guard's own rule.
sys.path.insert(0, str(root / "fork-ci/actions/head-check"))
import types  # noqa: E402

import head_gate  # noqa: E402

ENV_PATH = "repos/acme/widgets/environments/head-gate"


def guard_says(env_state):
    """head_gate.guard on the fake state's Environment (None: it does not exist): its GITHUB_OUTPUT guard= answer and
    its log."""
    routes = {"repos/acme/widgets": {"default_branch": "main"}}
    if env_state is not None:
        pols = env_state.get("policies", [])
        routes[ENV_PATH] = {"name": "head-gate", "deployment_branch_policy": env_state.get("deployment_branch_policy")}
        routes[ENV_PATH + "/deployment-branch-policies?per_page=100"] = {"total_count": len(pols),
                                                                         "branch_policies": pols}

    def gh(*a, stdin=None):
        if a[0] == "api" and len(a) == 2 and a[1] in routes:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(routes[a[1]]), stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="HTTP 404: Not Found")

    with tempfile.NamedTemporaryFile("r", dir=BASE.name) as o:
        log = []
        head_gate.guard("acme/widgets", "head-gate", "HEAD_GATE_APP_KEY", "false", False, gh, log.append,
                        {"GITHUB_OUTPUT": o.name})
        return dict(line.split("=", 1) for line in o.read().splitlines()).get("guard"), "\n".join(log)


def with_policy(pid):
    return [("POLICY_ID", pid) if k == "POLICY_ID" else (k, v) for k, v in SUBS]


widened = empty_state()
widened["envs"]["head-gate"] = {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
                                "policies": [{"id": 7, "name": "release/*", "type": "branch"},
                                             {"id": 8, "name": "v*", "type": "tag"}]}
code, err, calls, st, _ = run(b["environment"], widened)
check(code == 0, f"environment, existing rules: runs clean ({err.strip()[:200]})")
code_r, err_r, calls_r, st_r, tmp_r = run(b["environment-rules"], st)
lines = (tmp_r / "stdout.txt").read_text().splitlines()
check(code_r == 0 and not writes(calls_r) and st_r["envs"] == st["envs"],
      f"environment-rules: a read only ({code_r}, {err_r.strip()[:200]})")
check(sorted(line.split(" ", 1)[1] for line in lines) == ["branch main", "branch release/*", "tag v*"]
      and all(line.split(" ", 1)[0].isdigit() for line in lines),
      f"environment, existing rules: step 3 keeps them and the read-back lists each with its id ({lines})")
said, log = guard_says(st["envs"]["head-gate"])
check(said == "fail" and "posts a failure on every head" in log,
      f"environment, existing rules: the guard refuses and the gate posts failures, as the page says ({said})")
for line in lines:
    pid, kind, name = line.split(" ", 2)
    if (kind, name) != ("branch", "main"):
        code_d, err_d, calls_d, st, _ = run(b["environment-rule-delete"], st, subs=with_policy(pid))
        check(code_d == 0 and [c.get("path") for c in calls_d] == [ENV_PATH + "/deployment-branch-policies/" + pid],
              f"environment-rule-delete: deletes rule {pid}, in the right repository ({err_d.strip()[:200]})")
pols = st["envs"]["head-gate"]["policies"]
check([(p["type"], p["name"]) for p in pols] == [("branch", "main")], f"environment: after the deletes, one rule ({pols})")
said, log = guard_says(st["envs"]["head-gate"])
check(said == "ok", f"environment: the read-back's one line 'branch main' is what the guard passes ({said}: {log[:200]})")
code_d, err_d, calls_d, st2, _ = run(b["environment-rule-delete"], st, subs=with_policy("424242"))
check(code_d != 0 and st2["envs"] == st["envs"], "environment-rule-delete: an unknown id deletes nothing")


def bullet(start):
    """The text of the page's list item that starts with `start`, up to the next item or heading."""
    found = re.search(r"^- " + re.escape(start) + r"(.*?)(?=^- |^#|\Z)", PAGE.read_text(), re.S | re.M)
    return " ".join(found.group(1).split()) if found else ""


# The page's claims about what is posted are the code's (the lens's two must-fix findings were wrong claims here).
widened_text = bullet("**Environment widened**")
check("posts a failure on every head" in widened_text
      and "nothing" not in widened_text.replace("may post nothing", ""),
      f"rotation: 'Environment widened' says a failure is posted on every head, as the guard does ({widened_text[:120]})")
said, log = guard_says(None)
deleted_text = bullet("**Environment deleted")
check(said is None and "nothing is posted" in deleted_text and "stays passed" in deleted_text,
      f"rotation: a missing Environment gives no guard output, and the page says nothing is posted ({said})")
key_text = bullet("**Gate key:**")
readme = (root / "fork-ci/actions/head-check/README.md").read_text()
check("**not** fail closed" in key_text and "stays passed" in key_text and "No token can be made" in readme
      and "Stops before start" in key_text and "Stops before start" in readme,
      "rotation: a key deleted first is not fail closed (the README's no-post case), and an old pass stays")


def in_order(text, marks):
    """The first place of each mark in text: (all found and strictly in this order, the places)."""
    at = [text.find(m) for m in marks]
    return all(a >= 0 for a in at) and at == sorted(at) and len(set(at)) == len(at), dict(zip(marks, at))


# Ordered procedures: the step order is checked, not only the words (a lens put a wrong order back, all words kept).
again_text = bullet("**App made again**")
ok, at = in_order(again_text, ["install", "(step 6)", "HEAD_GATE_APP_ID", "backfill", "(step 7)", "delete the old App"])
check(ok and "HEAD_GATE_APP_KEY" in again_text,
      f"rotation: 'App made again' order: install, bind the new id (step 6), switch id and key, backfill, step 7, then "
      f"delete the old App ({at})")


# The same order as a model: the gate posts as `gate`, the ruleset requires `ruleset`'s check, the queue counts only
# `queue`'s runs. A head the old App passed then gets an event that should fail it; it must never merge at any point.
def stale_pass_merges(s):
    newest = {"old": "pass"}
    newest[s["gate"]] = "fail"
    return newest.get(s["ruleset"]) == "pass" and newest.get(s["queue"]) == "pass"


points = sorted((p, k) for k, p in {"switch": again_text.find("HEAD_GATE_APP_ID"), "bind": again_text.find("(step 6)"),
                                     "queue": again_text.find("(step 7)")}.items() if p >= 0)
s, open_after = {"gate": "old", "ruleset": "old", "queue": "old"}, []
for _, k in points:
    s = {**s, {"switch": "gate", "bind": "ruleset", "queue": "queue"}[k]: "new"}
    if stale_pass_merges(s):
        open_after.append(k)
check(len(points) == 3 and not open_after,
      f"rotation: 'App made again' is closed at every point: no old pass merges after a failing event "
      f"(open after {open_after}, order {[k for _, k in points]})")
check("not** fail closed" in again_text and "blocked at every point" in again_text,
      "rotation: 'App made again' names the window and says the other order is not fail closed")
step6_intro = PAGE.read_text().split("**6. Bind the check to the gate App**", 1)[1].split("<!-- owner-step:", 1)[0]
check(re.search(r"App\s+made again", step6_intro) and "**first**" in step6_intro,
      "step 6: its 'only after the backfill' rule names the App-made-again exception (bind first)")

ok, at = in_order(key_text, ["add a new key", "Environment secret", "backfill", "delete the old key"])
check(ok and "delete the new downloaded `.pem`" in key_text,
      f"rotation: gate key order: add, set the secret (and delete the .pem), backfill, then delete the old key ({at})")
rollout = PAGE.read_text().split("## Rollout order", 1)[1].split("\n## ", 1)[0]
ok, at = in_order(rollout, ["Backfill", "owner step 6", "owner step 7", "Roll back"])
check(ok and "head_check_app" in rollout.split("Roll back", 1)[1],
      f"rollout: backfill, bind (step 6), queue key (step 7); a rollback after step 7 also takes the key out ({at})")

# The other places that state what the guard does: failures are posted, the guard does not "refuse to run".
proves = bullet("**Only the default branch could make the App's token.**")
step3_intro = PAGE.read_text().split("**3. Environment, App id and key.**", 1)[1].split("<!-- owner-step:", 1)[0]
for where, said_text in (("What the gate proves", proves), ("step 3 intro", " ".join(step3_intro.split()))):
    check("posts only failures" in said_text and not re.search(r"guard( job)? refuses (to run|anything)", said_text),
          f"{where}: says the gate posts only failures, not that the guard job refuses to run ({said_text[:120]})")

# "Environment widened" by "protected branches": the read-back shows only the good line while the guard fails, so the
# fix must be step 3 again (its PUT); and the page must say the gate may post nothing in that case.
pb = {"deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False},
      "policies": [{"id": 101, "name": "main", "type": "branch"}]}
said, _ = guard_says(pb)
code_r, _, _, _, tmp_r = run(b["environment-rules"], {**empty_state(), "envs": {"head-gate": pb}})
shown = (tmp_r / "stdout.txt").read_text().splitlines()
check(said == "fail" and shown == ["101 branch main"],
      f"protected branches: the guard fails ({said}) while the read-back shows only the good line ({shown})")
check(all(w in widened_text for w in ("protected branches", "step 3's first line", "may post nothing")),
      "rotation: 'Environment widened' sends 'protected branches' to step 3 again and says the gate may post nothing")

# Step 4: a failed download never runs an older script left by an earlier try; a good one runs the new script.
m = re.search(r"\*\*4\. Host install\*\*.*?```sh\n(.*?)```", PAGE.read_text(), re.S)
block4 = m.group(1) if m else ""


def run_step4(download_ok, partial=False):
    tmp = pathlib.Path(tempfile.mkdtemp(dir=BASE.name))
    (tmp / "bin").mkdir()
    curl = ('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
            f"echo \"touch '{tmp}/NEW_SCRIPT_RAN'\" > \"$out\"\n") if download_ok else \
        ('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
         f"echo \"touch '{tmp}/OLD_SCRIPT_RAN'\" > \"$out\"\nexit 18\n") if partial else \
        "#!/bin/sh\necho 'curl: (22) The requested URL returned error: 404' >&2\nexit 22\n"
    (tmp / "bin/curl").write_text(curl)
    (tmp / "bin/curl").chmod(0o755)
    (tmp / "gh-apps-bootstrap.sh").write_text(f"touch '{tmp}/OLD_SCRIPT_RAN'\n")  # left by an earlier try
    script = fill(block4.replace("/root/", str(tmp) + "/"), [("FORK_OWNER", "o"), ("FORK", "f"),
                                                             ("RELEASE_SHA", "a" * 40), ("KIT_PATH", "kit"),
                                                             ("AGENT_USER", "agent")])
    subprocess.run(["bash", "--noprofile", "--norc", "-c", script], cwd=tmp, capture_output=True, text=True,
                   env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(tmp)}, timeout=30)
    return (tmp / "OLD_SCRIPT_RAN").exists(), (tmp / "NEW_SCRIPT_RAN").exists()


check(block4 and run_step4(False) == (False, False),
      "step 4: a failed download runs nothing, not an older script left at the same path")
check(block4 and run_step4(True) == (False, True), "step 4: a good download runs the new script, not the old one")
check(block4 and run_step4(False, partial=True) == (False, False),
      "step 4: a download that stops half-way (curl exit 18, partial file kept) runs nothing")

# Repairs after a deleted or widened Environment (lens t3lens87q findings 1-2).
deleted_text = " ".join(re.search(r"^- \*\*Environment deleted(.*?)(?=^- |^#|\Z)", PAGE.read_text(), re.S | re.M).group(1).split())
check("new key" in deleted_text and "Gate key" in deleted_text,
      "Environment deleted: its key secret goes with it and the .pem was deleted, so the repair makes a new key first")
check("step 3's first line" in widened_text and "Run step 3 again" not in widened_text,
      "Environment widened: the repair names step 3's first line only (the key line needs the deleted .pem)")
again_text = " ".join(re.search(r"^- \*\*App made again(.*?)(?=^- |^#|\Z)", PAGE.read_text(), re.S | re.M).group(1).split())
check("do not follow it" in again_text, "App made again: the expected drift warning between step 6 and the switch is named")

# --- step 6: bind the check in a ruleset ---------------------------------------------------------------------------
blk = b["bind-ruleset"]
old = ruleset()
code, err, calls, st, _ = run(blk, empty_state({RS: old}))
put = [c for c in calls if c.get("method") == "PUT"]
check(code == 0 and len(put) == 1, f"ruleset: one PUT ({code}, {err.strip()[:300]})")
body = put[0]["body"] if put else {}
check(set(body) == {"name", "target", "enforcement", "conditions", "bypass_actors", "rules"},
      f"ruleset: the PUT holds only the writable fields ({sorted(body)})")
check(body.get("rules", [])[:2] == old["rules"][:2], "ruleset: the other rules are unchanged")
check(body and rsc_of(body) == [[{"context": "ci-result", "integration_id": 15368},
                                 {"context": GATE, "integration_id": 777777}]],
      f"ruleset: the gate entry is bound to the App, the other check unchanged ({body and rsc_of(body)})")
check(body and [r for r in body["rules"] if r["type"] == "required_status_checks"][0]["parameters"][
    "strict_required_status_checks_policy"] is True, "ruleset: the rule's other parameters stay")
check(body.get("bypass_actors") == old["bypass_actors"] and body.get("conditions") == old["conditions"],
      "ruleset: bypass list and conditions are kept as they were")

code2, err2, calls2, st2, _ = run(blk, st)
put2 = [c for c in calls2 if c.get("method") == "PUT"]
check(code2 == 0 and len(put2) == 1 and put2[0]["body"] == body and st2 == st,
      "ruleset: run twice, the same PUT and the same ruleset")

code, err, calls, st, _ = run(blk, empty_state({RS: ruleset(checks=[{"context": "ci-result"}])}))
put = [c for c in calls if c.get("method") == "PUT"]
check(code == 0 and put and rsc_of(put[0]["body"]) == [[{"context": "ci-result"},
                                                        {"context": GATE, "integration_id": 777777}]],
      "ruleset: a ruleset without the check gets it, bound; an unbound other check stays unbound")

code, err, calls, st, _ = run(blk, empty_state({RS: ruleset(checks=[
    {"context": GATE, "integration_id": 15368}, {"context": "lint"}, {"context": GATE}])}))
put = [c for c in calls if c.get("method") == "PUT"]
check(code == 0 and put and rsc_of(put[0]["body"]) == [[{"context": "lint"},
                                                        {"context": GATE, "integration_id": 777777}]],
      "ruleset: the gate's name bound to another App, twice: one entry, bound to the gate App")

code, err, calls, _, _ = run(blk, empty_state({RS: ruleset(rsc_rules=0)}))
check(code != 0 and not writes(calls) and "exactly one required status checks rule" in err,
      f"ruleset: no required-checks rule: refused, nothing written ({code}, {err.strip()[:200]})")
code, err, calls, _, _ = run(blk, empty_state({RS: ruleset(rsc_rules=2)}))
check(code != 0 and not writes(calls) and "exactly one required status checks rule" in err,
      "ruleset: two required-checks rules: refused, nothing written")
code, err, calls, _, _ = run(blk, empty_state({RS: ruleset(bypass=False)}))
check(code != 0 and not writes(calls) and "bypass_actors" in err,
      f"ruleset: bypass list not shown (not an admin): refused, nothing written ({err.strip()[:200]})")
code, err, calls, _, _ = run(blk, empty_state({RS: ruleset()}, fail=[RS]))
check(code != 0 and not writes(calls), "ruleset: the read fails: nothing written")
code, err, calls, _, _ = run(blk, empty_state({RS: ruleset()}), subs=[s for s in SUBS if s[0] != "APP_ID"])
check(code != 0 and not writes(calls), "ruleset: APP_ID left as a placeholder: nothing written")

# --- step 6: bind the check in classic branch protection -------------------------------------------------------------
blk = b["bind-classic"]
cold = {"url": "https://api.github.com/" + CL, "strict": True, "contexts": ["ci", "lint", GATE, "third"],
        "contexts_url": "https://api.github.com/" + CL + "/contexts",
        "checks": [{"context": "ci", "app_id": 15368}, {"context": "lint", "app_id": None},
                   {"context": GATE, "app_id": None}, {"context": "third", "app_id": 9999}]}
code, err, calls, st, _ = run(blk, empty_state({CL: cold}))
patch = [c for c in calls if c.get("method") == "PATCH"]
check(code == 0 and len(patch) == 1, f"classic: one PATCH ({code}, {err.strip()[:300]})")
body = patch[0]["body"] if patch else {}
check(body == {"strict": True, "checks": [{"context": "ci", "app_id": 15368}, {"context": "lint", "app_id": 15368},
                                          {"context": "third", "app_id": 9999},
                                          {"context": GATE, "app_id": 777777}]},
      f"classic: gate bound, any-source pinned to the given id, bound checks and strict kept ({body})")
code2, err2, calls2, st2, _ = run(blk, st)
patch2 = [c for c in calls2 if c.get("method") == "PATCH"]
check(code2 == 0 and len(patch2) == 1 and patch2[0]["body"] == body and st2 == st,
      "classic: run twice, the same PATCH and the same protection")

anysrc = [s if s[0] != "ANY_SOURCE_APP_ID" else ("ANY_SOURCE_APP_ID", "-1") for s in SUBS]
code, err, calls, _, _ = run(blk, empty_state({CL: cold}), subs=anysrc)
patch = [c for c in calls if c.get("method") == "PATCH"]
check(code == 0 and patch and patch[0]["body"]["checks"][1] == {"context": "lint", "app_id": -1},
      "classic: ANY_SOURCE_APP_ID -1 keeps 'any source'")
code, err, calls, _, _ = run(blk, empty_state({CL: cold}, fail=[CL]))
check(code != 0 and not writes(calls), "classic: no protection to read: nothing written")
code, err, calls, _, _ = run(blk, empty_state({CL: {"url": "x", "strict": False, "contexts": ["ci"]}}))
check(code != 0 and not writes(calls), "classic: an answer without a checks list: nothing written")

# The by-hand classic check reads, never writes, and shows the gate's entry with its app_id.
code, err, calls, st, tmp = run(b["classic-check"], empty_state({CL: cold}))
shown = json.loads((tmp / "stdout.txt").read_text() or "null")
check(code == 0 and not writes(calls) and shown == cold["checks"],
      f"classic-check: a read only, showing each check with its app_id ({code}, {err.strip()[:200]})")
warned = []
head_gate.drift("acme/widgets", "main", GATE, "777777",
                lambda *a, stdin=None: types.SimpleNamespace(returncode=0, stdout="[]", stderr=""),
                warned.append, {})
check(any("::warning::" in w and "classic branch protection cannot be read" in w for w in warned)
      and "cannot be read" in PAGE.read_text().split("<!-- owner-step: classic-check -->")[0][-400:],
      f"classic: the gate warns that it cannot read classic protection, as the page says ({warned})")

# --- links, public safety, one copy ----------------------------------------------------------------------------------
def slugs(md):
    out = set(re.findall(r'<a id="([^"]+)"', md))
    for h in re.findall(r"^#{1,6} (.+)$", md, re.M):
        s = re.sub(r"[^\w\- ]", "", h.strip().lower().replace("`", "")).replace(" ", "-")
        out.add(s)
    return out


PAGES = [PAGE, root / "fork-ci/actions/head-check/README.md", root / ".github/fork-fleet.md",
         root / "docs/fork/host-tools.md"]
for page in PAGES:
    md = page.read_text()
    md_nocode = re.sub(r"```.*?```", "", md, flags=re.S)
    bad = []
    for target in re.findall(r"\]\(([^)\s]+)\)", md_nocode):
        if re.match(r"[a-z]+:", target):
            continue
        path, _, anchor = target.partition("#")
        dest = (page.parent / path).resolve() if path else page
        if not dest.exists():
            bad.append(target)
        elif anchor and dest.suffix == ".md" and anchor not in slugs(dest.read_text()):
            bad.append(target)
    check(not bad, f"{page.relative_to(root)}: every relative link resolves ({bad})")

for page in PAGES[2:]:
    check("gate-trust.md" in page.read_text(), f"{page.relative_to(root)} links to the gate-trust page")

text = PAGE.read_text()
personal = {
    "home folder": r"/home/|/Users/|~/",
    "IPv4 address": r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
    "VM id": r"\b(?:VM|vm|qm \w+) ?\d{2,}\b",
    "token or key": r"gh[pousr]_[A-Za-z0-9]{10,}|github_pat_|BEGIN [A-Z ]*PRIVATE KEY",
    "email address": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "ruleset id": r"rulesets/\d+",
}
for what, pat in personal.items():
    hits = re.findall(pat, text)
    check(not hits, f"the page names no {what} ({hits[:3]})")

copies = []
for f in [*root.glob("docs/**/*.md"), *root.glob("fork-ci/**/*.md"), *root.glob(".github/*.md"),
          *root.glob("fork-ci/**/*.yml")]:
    if f == PAGE:
        continue
    t = f.read_text(errors="replace")
    if re.search(r"integration_id: ?\$app|deployment-branch-policies -f name|gh secret set HEAD_GATE_APP_KEY", t):
        copies.append(str(f.relative_to(root)))
check(not copies, f"the owner's commands live only in the gate-trust page ({copies})")

print(f"\n{len(failures)} failed" if failures else "\nall ok")
sys.exit(1 if failures else 0)
