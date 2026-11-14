"""A called workflow may not ask for more token permissions than its caller grants.

    python3 .github/scripts/fork-reusable-permissions.test.py

GitHub refuses such a run before any job starts ("startup_failure", no log), even when the
job that asks for more would be skipped, so a schedule-only workflow can stay broken for days
unnoticed. This walks every fork-*.yml workflow, follows each local `uses:
./.github/workflows/X.yml` call (nested calls too) and checks that every job of the called
workflow asks for at most what the calling job grants. Needs PyYAML. First it proves the
check itself on small made-up workflows. Any FAIL exits 1.
"""

import pathlib
import sys
import tempfile

import yaml

LEVEL = {"none": 0, "read": 1, "write": 2}
# Scopes read-all / write-all expand to (GitHub's list; id-token and models have no read/write split
# that matters here, but are listed so a write-all grant covers them).
SCOPES = [
    "actions", "attestations", "checks", "contents", "deployments", "discussions", "id-token",
    "issues", "models", "packages", "pages", "pull-requests", "repository-projects",
    "security-events", "statuses",
]
UNKNOWN = object()  # no permissions anywhere: the repository default applies, not known here


def normalise(perms):
    """dict scope -> level, or UNKNOWN when perms is None."""
    if perms is None:
        return UNKNOWN
    if perms == {} or perms == "":
        return {}
    if perms == "read-all":
        return {s: 1 for s in SCOPES}
    if perms == "write-all":
        return {s: 2 for s in SCOPES}
    if isinstance(perms, dict):
        return {str(k): LEVEL[str(v)] for k, v in perms.items()}
    raise ValueError(f"unreadable permissions: {perms!r}")


def load(root, rel):
    data = yaml.safe_load((root / rel).read_text())
    return data if isinstance(data, dict) else {}


def excess(asked, granted):
    """Scopes asked above the grant, as 'scope: asked > granted'."""
    if asked is UNKNOWN or granted is UNKNOWN:
        return []
    out = []
    for scope, level in sorted(asked.items()):
        have = granted.get(scope, 0)
        if level > have:
            name = {v: k for k, v in LEVEL.items()}
            out.append(f"{scope}: {name[level]} > {name[have]}")
    return out


def walk(root, rel, granted, chain, problems, seen):
    """Check workflow `rel`, called with token `granted`, and the workflows it calls."""
    key = (rel, repr(granted))
    if key in seen:
        return
    seen.add(key)
    wf = load(root, rel)
    top = normalise(wf.get("permissions"))
    for job_id, job in (wf.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        own = normalise(job.get("permissions"))
        effective = own if own is not UNKNOWN else (top if top is not UNKNOWN else granted)
        if chain:  # a called workflow: its jobs may not ask for more than the caller grants
            for item in excess(effective, granted):
                problems.append(f"{' -> '.join(chain + [rel])} job {job_id} asks {item} (caller grants less)")
        uses = job.get("uses")
        if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
            callee = uses[2:].split("@", 1)[0]
            if not (root / callee).is_file():
                problems.append(f"{rel} job {job_id} calls missing {callee}")
                continue
            walk(root, callee, effective, chain + [f"{rel}:{job_id}"], problems, seen)


def check_tree(root):
    problems = []
    for path in sorted((root / ".github/workflows").glob("fork-*.yml")):
        walk(root, str(path.relative_to(root)), UNKNOWN, [], problems, set())
    return problems


def selftest():
    """The check must catch the bug it exists for, nested and with skipped jobs, and pass the fix."""
    caller = """
permissions: {contents: read}
jobs:
  release:
    if: false
    permissions: {actions: read, contents: write}
    uses: ./.github/workflows/mid.yml
"""
    mid = """
permissions: {actions: read, contents: read}
jobs:
  tests:
    permissions: {actions: write, contents: read}
    uses: ./.github/workflows/leaf.yml
  publish:
    permissions: {contents: write}
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
"""
    leaf = """
permissions: {contents: read}
jobs:
  watch:
    if: false
    permissions: {actions: write}
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
"""
    cases = [
        ("caller grants actions: read, nested job asks write -> fail", caller, True),
        ("caller grants actions: write -> pass", caller.replace("actions: read, contents: write", "actions: write, contents: write"), False),
        ("caller grants nothing ({}) -> fail", caller.replace("{actions: read, contents: write}", "{}"), True),
        ("caller grants write-all -> pass", caller.replace("{actions: read, contents: write}", "write-all"), False),
    ]
    ok = True
    for name, text, want_fail in cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/fork-a.yml").write_text(text)
            (root / ".github/workflows/mid.yml").write_text(mid)
            (root / ".github/workflows/leaf.yml").write_text(leaf)
            got_fail = bool(check_tree(root))
        good = got_fail == want_fail
        print(("ok   " if good else "FAIL ") + "selftest: " + name)
        ok &= good
    return ok


def main():
    ok = selftest()
    root = pathlib.Path(__file__).resolve().parents[2]
    problems = check_tree(root)
    for p in problems:
        print("FAIL " + p)
    if not problems:
        print("ok   every called fork workflow asks for at most what its caller grants")
    sys.exit(0 if ok and not problems else 1)


if __name__ == "__main__":
    main()
