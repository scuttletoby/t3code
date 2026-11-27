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
# The ok lines printed, for the end line: fork-ci-tests.cjs checks its count against them (#664).
oks = []


def check(name, ok, detail=""):
    if ok:
        oks.append(1)
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

# Runners are named by OS version: a moving label such as ubuntu-latest can swap the OS under a
# release, and a newer glibc on the build host breaks the Linux build's glibc ceiling
# (scripts/linux-glibc.ts). Upstream's release.yml pins its Linux runners to Ubuntu 24.04 too.
moving = []
for job_id, job in jobs.items():
    labels = [job.get("runs-on")] + [(job.get("with") or {}).get(k) for k in ("runner", "verify_runner")]
    moving += [f"{job_id}: {l}" for l in labels if isinstance(l, str) and re.search(r"-latest\b", l)]
check("no job runs on a moving -latest runner label", not moving, ", ".join(moving))
linux = [(job.get("with") or {}).get("runner") for job in jobs.values()
         if (job.get("with") or {}).get("platform") in ("linux", "win")]
check("Linux-built desktop jobs run on ubuntu-24.04", linux and all(r == "ubuntu-24.04" for r in linux),
      ", ".join(map(str, linux)))

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
               or re.fullmatch(r"bash \.github/scripts/fork-release\.sh (on-main|remove-drafts|publish) .*", str(s.get("run", "")).strip())]
    check(f"{job_id}: the token reaches only the on-main, remove-drafts, create and publish steps",
          len(token_steps) == len(allowed) == 4, ", ".join(str(s.get("name")) for s in token_steps))

    def index(pred):
        return next((i for i, s in enumerate(job_steps) if pred(s)), -1)

    remove = index(lambda s: "fork-release.sh remove-drafts" in str(s.get("run", "")))
    create = index(lambda s: str(s.get("uses", "")).startswith("softprops/"))
    publish = index(lambda s: "fork-release.sh publish" in str(s.get("run", "")))
    check(f"{job_id}: refuse/remove drafts, then create the draft, then publish it",
          -1 < remove < create < publish)
    on_main = index(lambda s: str(s.get("run", "")).strip()
                    == 'bash .github/scripts/fork-release.sh on-main "$REF"')
    check(f"{job_id}: checks the commit is on main before anything is written",
          -1 < on_main < remove
          and (job_steps[on_main].get("env") or {}).get("WORKFLOW_REF") == "${{ github.workflow_ref }}"
          and (job_steps[on_main].get("env") or {}).get("REF") == "${{ needs.preflight.outputs.ref }}")
    attest = index(lambda s: str(s.get("uses", "")).startswith("actions/attest-build-provenance@"))
    subjects = str(((job_steps[attest].get("with") or {}) if attest >= 0 else {}).get("subject-path", "")).split()
    check(f"{job_id}: attests the server archives and SHA256SUMS before the draft is created",
          -1 < on_main < attest < create   # the certificate says refs/heads/main for any dispatched ref
          and subjects == ["release-assets/*.tar.gz", "release-assets/SHA256SUMS"])
    perms = jobs[job_id].get("permissions") or {}
    check(f"{job_id}: may sign (id-token) and store (attestations) the provenance",
          perms.get("id-token") == "write" and perms.get("attestations") == "write")
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

# SHA256SUMS lists every file a person downloads, desktop apps included (#822), and is written in
# assemble, which runs after every desktop build.
sums = [s for s in steps(jobs["assemble"]) if "> SHA256SUMS" in str(s.get("run", ""))]
sums_run = str(sums[0].get("run", "")) if len(sums) == 1 else ""
check("assemble writes one SHA256SUMS over the server archives and the desktop files",
      len(sums) == 1 and "sha256sum -- t3-*.tar.gz *.zip *.dmg *.exe *.AppImage > SHA256SUMS" in sums_run
      and {"desktop_linux_x64", "desktop_win_x64", "desktop_mac_arm64", "desktop_mac_x64"}
      <= set(jobs["assemble"].get("needs", [])))

# The shipped macOS apps verify as a person unpacks them; assemble waits for it (#822).
verify_mac = jobs.get("verify_mac_signature", {})
verify_runs = "\n".join(str(s.get("run", "")) for s in steps(verify_mac))
check("verify_mac_signature unpacks each macOS zip and dmg and runs codesign --verify --deep --strict",
      "verify_mac_signature" in jobs["assemble"].get("needs", [])
      and {"desktop_mac_arm64", "desktop_mac_x64"} <= set(verify_mac.get("needs", []))
      and "macos" in str(verify_mac.get("runs-on", ""))
      and "ditto -x -k" in verify_runs and "hdiutil attach" in verify_runs
      and "codesign --verify --deep --strict" in verify_runs)
check("verify_mac_signature holds no token and detaches each dmg on any exit",
      verify_mac.get("permissions") == {}
      and "trap 'hdiutil detach \"$mount\" -force" in verify_runs)

# The macOS files that ship are the ones verified: assemble compares SHA256SUMS with the hashes
# verify_mac_signature printed (#822).
compare = [s for s in steps(jobs["assemble"])
           if "needs.verify_mac_signature.outputs.sums" in str(s.get("env", {}).get("VERIFIED", ""))]
assemble_names = [s.get("name") for s in steps(jobs["assemble"])]
check("assemble compares the macOS lines of SHA256SUMS with the verified hashes",
      "shasum -a 256 --" in verify_runs and "steps.verify.outputs.sums" in str(verify_mac.get("outputs", {}).get("sums"))
      and len(compare) == 1 and "SHA256SUMS" in str(compare[0].get("run", ""))
      and sums and assemble_names.index(compare[0].get("name")) > assemble_names.index(sums[0].get("name")))

# Every file the publish job uploads for people has a SHA256SUMS line: the check's patterns are the
# publish list less the files the apps read (*.blockmap, *.yml) and SHA256SUMS itself.
draft = [s for s in steps(jobs["publish"]) if "action-gh-release" in str(s.get("uses", ""))]
published = {line.strip().removeprefix("release-assets/")
             for line in str(draft[0].get("with", {}).get("files", "") if draft else "").splitlines() if line.strip()}
shipped = published - {"*.blockmap", "*.yml", "SHA256SUMS"}
covers = [s for s in steps(jobs["assemble"]) if "sha256sum --check" in str(s.get("run", ""))]
covers_run = str(covers[0].get("run", "")) if len(covers) == 1 else ""
loop = re.search(r"for file in ([^;]+); do", covers_run)
check("assemble checks SHA256SUMS has a line for every file the publish job uploads",
      bool(shipped) and loop is not None and set(loop.group(1).split()) == shipped
      and assemble_names.index(covers[0].get("name")) < assemble_names.index("Upload release files"),
      f"publish uploads {sorted(shipped)}, the check reads {loop.group(1) if loop else None}")

# The server bundle check skips dist/client, which names this repository on every Actions build.
bundle_steps = [s for s in steps(jobs["build_bundle"]) if "check-bundle" in str(s.get("run", ""))]
check("the server bundle is checked through check-bundle (without dist/client)",
      [str(s.get("run", "")).strip() for s in bundle_steps]
      == ["bash .github/scripts/fork-release.sh check-bundle apps/server/dist"])

# What upstream's release.yml does to the bundle between building and uploading it, the fork does
# too: release-desktop.yml (shared) expects it, as with the staged web asset manifest (#42806).
def between(names, first, last):
    return names[names.index(first) + 1 : names.index(last)] if first in names and last in names else None


upstream_bundle = (yaml.safe_load((root / ".github/workflows/release.yml").read_text())["jobs"]
                   .get("build_bundle", {}))
up_names = [s.get("name") for s in steps(upstream_bundle)]
fork_names = [s.get("name") for s in steps(jobs["build_bundle"])]
up_between = between(up_names, "Build JS bundle", "Upload JS bundle")
fork_between = between(fork_names, "Build JS bundle", "Upload JS bundle") or []
missing = [n for n in (up_between or []) if n not in fork_between]
check("build_bundle runs every step upstream's release.yml runs between building and uploading the bundle",
      up_between is not None and not missing, f"missing: {missing}" if up_between is not None
      else "release.yml's build_bundle has no 'Build JS bundle' then 'Upload JS bundle'")

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
print(f"all passed: {len(oks)} ok, 0 skipped")
