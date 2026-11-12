"""Checks the shape of .github/workflows/fork-release.yml that its guards rely on.

    python3 .github/scripts/fork-release-workflow.test.py [path/to/fork-release.yml]

Needs PyYAML. Each check prints ok/FAIL; any FAIL exits 1.
"""

import pathlib
import re
import sys

import yaml

root = pathlib.Path(__file__).resolve().parents[2]
path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else root / ".github/workflows/fork-release.yml"
text = path.read_text()
wf = yaml.safe_load(text)
desktop = yaml.safe_load((root / ".github/workflows/release-desktop.yml").read_text())
jobs = wf["jobs"]
failures = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok or not detail else f"\n     | {detail}"))
    if not ok:
        failures.append(name)


def steps(job):
    return job.get("steps", [])


def all_steps():
    for job_id, job in jobs.items():
        for step in steps(job):
            yield job_id, step


# Actions run by commit, so a moved tag cannot change what runs (the release action holds a write
# token). Local actions and workflows are part of this repository.
unpinned = []
for line in text.splitlines():
    m = re.match(r"\s*(?:-\s*)?uses:\s*(\S+)(.*)$", line)
    if not m or m.group(1).startswith("./"):
        continue
    if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", m.group(1)) or not re.match(r"\s*#\s*v\d", m.group(2)):
        unpinned.append(m.group(1))
check("external actions are pinned by commit SHA with a version comment", not unpinned, ", ".join(unpinned))

# Named secrets only: the signing secrets release-desktop.yml declares, nothing inherited.
declared = set(desktop[True]["workflow_call"]["secrets"])
inherit = [j for j, job in jobs.items() if job.get("secrets") == "inherit"]
check("no job passes secrets: inherit", not inherit, ", ".join(inherit))
wrong = []
for job_id, job in jobs.items():
    if job.get("uses") == "./.github/workflows/release-desktop.yml":
        passed = job.get("secrets")
        if not isinstance(passed, dict) or set(passed) != declared or any(
            v != "${{ secrets.%s }}" % k for k, v in passed.items()
        ):
            wrong.append(job_id)
check("release-desktop.yml gets exactly the secrets it declares", not wrong, ", ".join(wrong))
check("FORK_PUSH_TOKEN appears nowhere", "FORK_PUSH_TOKEN" not in text)

# actions/checkout fails on upstream's gitlinks without .gitmodules once it cleans up credentials;
# every job checks out with the shared plain-git step instead (fork-contracts.test.py runs it).
bad = [j for j, s in all_steps() if str(s.get("uses", "")).startswith("actions/checkout@")]
check("no job uses actions/checkout", not bad, ", ".join(bad))
plain = {j for j, s in all_steps() if s.get("name") == "Checkout" and "git init" in str(s.get("run", ""))}
check("every job that checks out uses the plain-git step, with no token",
      plain == {"preflight", "build_bundle", "assemble", "publish"}
      and all("github.token" not in yaml.safe_dump(s) and "secrets." not in yaml.safe_dump(s)
              for _, s in all_steps() if s.get("name") == "Checkout"), ", ".join(sorted(plain)))

release_steps = [(j, s) for j, s in all_steps() if str(s.get("uses", "")).startswith("softprops/action-gh-release@")]
check("one release-action step", len(release_steps) == 1)
check("the release action fails on unmatched files",
      all((s.get("with") or {}).get("fail_on_unmatched_files") is True for _, s in release_steps))

# The write token: only the publish job has it, and only its three release-writing steps use it.
write_jobs = [j for j, job in jobs.items() if (job.get("permissions") or {}).get("contents") == "write"]
check("only the publish job has contents: write", write_jobs == ["publish"], ", ".join(write_jobs))
package_code = re.compile(r"(^|[\s;&|(])(vp|node|npm|npx|pnpm|bun)\s")
for job_id in write_jobs:
    job_steps = steps(jobs[job_id])
    runs_packages = [s.get("name") for s in job_steps
                     if str(s.get("uses", "")).startswith("./") or package_code.search(str(s.get("run", "")))]
    check(f"{job_id}: runs no package code", not runs_packages, ", ".join(map(str, runs_packages)))
    token_steps = [s for s in job_steps if "github.token" in yaml.safe_dump(s) or "secrets." in yaml.safe_dump(s)
                   or str(s.get("uses", "")).startswith("softprops/")]
    allowed = [s for s in token_steps if str(s.get("uses", "")).startswith("softprops/")
               or re.fullmatch(r"bash \.github/scripts/fork-release\.sh (remove-drafts|publish) .*", str(s.get("run", "")).strip())]
    check(f"{job_id}: the token reaches only the remove-drafts, create and publish steps",
          len(token_steps) == len(allowed) == 3, ", ".join(str(s.get("name")) for s in token_steps))

    def index(pred):
        return next((i for i, s in enumerate(job_steps) if pred(s)), -1)

    remove = index(lambda s: "fork-release.sh remove-drafts" in str(s.get("run", "")))
    create = index(lambda s: str(s.get("uses", "")).startswith("softprops/"))
    publish = index(lambda s: "fork-release.sh publish" in str(s.get("run", "")))
    check(f"{job_id}: refuse/remove drafts, then create the draft, then publish it",
          -1 < remove < create < publish)
    if create >= 0 and publish >= 0:
        check(f"{job_id}: publishes the draft it created, by id",
              job_steps[create].get("id") == "draft"
              and "steps.draft.outputs.id" in yaml.safe_dump(job_steps[publish]))

# Shell steps fail on any error in a pipeline.
lax = []
for job_id, s in all_steps():
    run = s.get("run")
    if run is None:
        continue
    lines = [l for l in run.strip().splitlines() if l.strip() and not l.strip().startswith("#")]
    if s.get("shell") != "bash" or (len(lines) > 1 and lines[0].strip() != "set -euo pipefail"):
        lax.append(f"{job_id}/{s.get('name')}")
check("run steps use bash and multi-line ones start with set -euo pipefail", not lax, "; ".join(lax))

runs = "\n".join(str(s.get("run", "")) for _, s in all_steps())
check("preflight resolves the version through the guard script",
      "fork-release.sh resolve" in "\n".join(str(s.get("run", "")) for s in steps(jobs["preflight"])))
check("preflight runs the guard tests", "fork-release.test.sh" in runs)
check("the CLI archives are checked for the release repository", "fork-release.sh check-archives release-assets" in runs)
check("preflight runs this shape test", "fork-release-workflow.test.py" in
      "\n".join(str(s.get("run", "")) for s in steps(jobs["preflight"])))

# The server bundle check skips dist/client, which names this repository on every Actions build.
bundle_steps = [s for s in steps(jobs["build_bundle"]) if "apps/server/dist" in str(s.get("run", ""))]
check("the server bundle is checked through check-bundle (without dist/client)",
      [str(s.get("run", "")).strip() for s in bundle_steps]
      == ["bash .github/scripts/fork-release.sh check-bundle apps/server/dist"])

# The build names this fork through the setting scripts/lib/release-repository.ts reads.
build = next((s for s in steps(jobs["build_bundle"]) if s.get("name") == "Build JS bundle"), {})
check("the build sets T3CODE_DESKTOP_UPDATE_REPOSITORY to this repository",
      (build.get("env") or {}).get("T3CODE_DESKTOP_UPDATE_REPOSITORY") == "${{ github.repository }}")
check("the former name T3CODE_RELEASE_REPOSITORY appears nowhere", "T3CODE_RELEASE_REPOSITORY" not in text)

# The write job's guard script comes from the workflow's own commit, not the commit being built.
guard = [s for s in steps(jobs["publish"]) if "git init" in str(s.get("run", ""))]
check("publish checks out only its guard scripts, at github.workflow_sha",
      len(guard) == 1 and guard[0].get("env") == {"REF": "${{ github.workflow_sha }}", "HISTORY": "tip",
                                                  "SPARSE": "/.github/scripts/"})

if failures:
    print(f"{len(failures)} failed")
    sys.exit(1)
print("all passed")
