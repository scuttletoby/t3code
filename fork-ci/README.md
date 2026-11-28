# fork-ci: CI actions for other repositories

Composite actions under [`actions/`](actions/) and the reusable workflows
[`.github/workflows/fork-pr-queue.yml`](../.github/workflows/fork-pr-queue.yml) and
[`.github/workflows/fork-head-gate.yml`](../.github/workflows/fork-head-gate.yml) (the head
check as a gate App's check, [head-check](actions/head-check/README.md#the-gate)), for any
repository to use. Each action's README says what it does and what it needs.

## Pinning

Pin a **release commit** by its full SHA, with its tag as a comment:

```yaml
- uses: scuttletoby/t3code/fork-ci/actions/job-changed@<release sha> # ci/vN
```

Find it on the [`ci-release`](https://github.com/scuttletoby/t3code/tree/ci-release) branch, or with
`git ls-remote https://github.com/scuttletoby/t3code.git 'refs/tags/ci/v*'` (the `^{}` line is the
commit to pin).

Pin only a `ci/*` tag whose commit is on `ci-release`. Anyone with push rights can create a new
`ci/*` tag, and the rulesets below keep even a stray one from being deleted. Check before pinning
(replace `vN`; `ahead` or `identical` means the tag is on the branch, anything else means do not pin it):

```sh
curl -s https://api.github.com/repos/scuttletoby/t3code/compare/ci/vN...ci-release | jq -r .status
```

**Never pin a commit of `main`.** For a `uses:` line GitHub downloads the whole repository at the
pinned commit. Main also holds upstream T3 Code, and its tree has a dangling symlink that makes every
job fail at "Set up job" (`Could not find file '.../.repos/...'`). Main is also rebased onto
upstream, so its old commits disappear. `ci/v1` is such a main commit: it is kept for history, but
do not use it.

A release commit holds only what consumers use: `LICENSE`, `fork-ci/`, the host kits in `fork-kit/`
(installed only from a release commit, [fork-kit/README.md](../fork-kit/README.md)) and the files their installers
read from outside `fork-kit/` (the desk kit's fleet scripts and units, the heavy kit's capacity rule files in
`packages/shared/src/capacity/`), the scripts an action runs from outside `fork-ci/` (fleet-route runs
`.github/scripts/fork-fleet.py`) and the reusable workflows that start only on `workflow_call` (the list is `paths` in
[`.github/fork-ci-release.json`](../.github/fork-ci-release.json)). The paths are the same as on main.
Release commits contain no symlinks and no workflow that starts by itself. `fork-ci-release.test.py` writes this
commit's release files to a folder and runs each kit installer's own file check on it, with the `--source` the kit's
bootstrap passes (heavy also copies itself and desk stages the release, each with its self-test), so a kit that reads
a file outside the paths fails CI. Every `fork-kit/` folder needs a row in that test (`KITS`, or `NO_INSTALLER`).

## Using the actions

Every action is used the same way: a `uses:` line pinned to a release commit with its tag as a comment (see
"Pinning"), its inputs under `with:`, and its outputs read as `steps.<id>.outputs.<name>`. A project's own facts
(its repository, folders, runner labels, jobs) live in a config file in the project's repository, never in fork-ci.
Each action's README has a full example, every input and output, and what happens when something is unclear.

```yaml
- id: scope
  uses: scuttletoby/t3code/fork-ci/actions/docs-scope@<release sha> # ci/vN
  with:
    config: .github/docs-scope.json
- if: steps.scope.outputs.docs_only != 'true'
  run: ./build.sh
```

| Action                                             | What it answers or does                                                                   | Key inputs                               | Outputs                                           | Config file (in the project)                             |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------- | -------------------------------------------------------- |
| [docs-scope](actions/docs-scope/README.md)         | is this change docs-only, and which jobs does it reach                                    | `config`, `base`                         | `docs_only`, `jobs`, `reason`                     | `.github/docs-scope.json` (rules)                        |
| [job-changed](actions/job-changed/README.md)       | can the change alter what one job of a workflow does                                      | `workflow`, `base`, `job`, `paths`       | `changed`, `workflow`, `reason`                   | none                                                     |
| [test-select](actions/test-select/README.md)       | which test files can this pull request's change reach; `trace/` checks it                 | `config`                                 | `all`, `count`, `total`, `list`, `reason`         | `.github/test-select.json` (tests, always-all)           |
| [gradle-cache](actions/gradle-cache/README.md)     | stages the Maven files a lock lists, sha256-checked, from release assets                  | `lock`, `out`, `token`                   | `maven-repo-local`                                | the lock file                                            |
| [fleet-route](actions/fleet-route/README.md)       | which runner each job of a run gets: the project's self-hosted pools or hosted            | `config`, `vars`, `mode`, `gates`        | `runners`, `mode`, `attempt`, `fallback`, `watch` | the fleet config (e.g. `.github/fleet.json`)             |
| [fleet-watch](actions/fleet-watch/README.md)       | re-runs, once, a run that a dispatcher pool's runner cut off                              | `config`, `verb`, `run-id`, `attempt`    | `rerun`, `pending`                                | the fleet config (its `watch` section)                   |
| [handoff-put](actions/handoff-put/README.md)       | hands a file to a later job of the same run, keyed on the commit                          | `name`, `path`, `retention-days`         | `ok`, `artifact`, `sha`, `reason`                 | none                                                     |
| [handoff-get](actions/handoff-get/README.md)       | takes that file only when the commit inside it is this checkout's HEAD                    | `name`, `path`                           | `ok`, `sha`, `reason`                             | none                                                     |
| [git-clean](actions/git-clean/README.md)           | empties a reused self-hosted workspace, refuses a .git config it does not know            | `path`                                   | none                                              | none                                                     |
| [checkout-state](actions/checkout-state/README.md) | fails a workflow whose jobs could leave a sparse or partial clone on a self-hosted runner | `config`, `root`                         | none                                              | `.github/checkout-state.json` (optional: reviews)        |
| [head-check](actions/head-check/README.md)         | fails a pull request head that is not the change its review read                          | `config`, `required-lenses`, `token`     | `result`                                          | the pr-queue config                                      |
| [pr-queue](actions/pr-queue/README.md)             | a merge queue for an up-to-date-required repository (also a reusable workflow)            | `config`, `state-file`, `cache`, `token` | none                                              | the pr-queue config                                      |
| [doc-check](actions/doc-check/README.md)           | fails instruction files over budget, history in them, broken links, added dated reports   | `config`, `base`, `mode`                 | none                                              | `.github/doc-check.json` (optional: budgets, rules)      |
| [weakened-tests](actions/weakened-tests/README.md) | reports (never fails) the existing tests a pull request deletes, skips or weakens         | `config`, `token`                        | `status`, `count`, `missing_why`                  | `.github/weakened-tests.json` (optional: added patterns) |

Two habits every action expects:

- **A gate output is tested for "do not run", never for "run"** (`!= 'false'`, `!= 'true'` as its README says): a
  failed step leaves the output empty, and empty must mean the safe choice (run the job).
- **A job that others `need` must not skip them on failure:** GitHub counts a skipped required job as passing. Give
  the dependent job `if: ${{ !cancelled() }}` and a fallback for an empty output (fleet-route's README shows one).

To move to a new release, change the SHA and the comment together; read the new tag's release notes first
([RELEASE-NOTES.md](RELEASE-NOTES.md): what changed, and what to do before or after the move).

### Review rules

The queue and the head gate can also judge which lenses a change needs, its findings and its review rounds, from a
`review` block in the project's queue config. Turn it on in this order: release, pin, then config
([pr-queue README](actions/pr-queue/README.md#turning-it-on-release-pin-then-config)), in `shadow` first: a release
that does not know the key refuses the whole config.

### Outside a workflow

A project's tools that run outside a workflow (a deploy script, a local check) use the same code from the same
release through [`actions/pinned`](actions/pinned/README.md): a module and command (not an action) that fetches the
files a pin config names, checks each by git blob id, caches them and runs the checked bytes. Modules that are meant
for this say which names callers may import in their README's "Python API" section (docs-scope, gradle-cache).

[`actions/ci-demand`](actions/ci-demand/README.md) is such a command only: a read-only measure of a repository's CI
per day (CI hours, runs per workflow, queue wait per runner label, cancelled and failed share), for a before/after
record of a CI change.

### Law tests

[`laws/`](laws/README.md) is the contract for law tests (generated orders of runs, crashes, reruns and old state
played against the real code): a fixed seed in CI, the seed and the shortest failing steps printed, capped tries, no
sleeps, and each failure kept as a fixed test before its fix merges. Projects link to it and do not copy it.
[`laws/laws.py`](laws/laws.py) is its small runner (Python standard library only, test-side: no action imports it).

## How a release is made

[`.github/workflows/fork-ci-release.yml`](../.github/workflows/fork-ci-release.yml) runs once a day
and can also be started by hand. It runs
[`.github/scripts/fork-ci-release.py`](../.github/scripts/fork-ci-release.py) on the tip of main.
It makes a release only when that commit passed `fork-ci-result` and changed the release files.
The new commit's parent is the previous release. The script pushes branch `ci-release` and tag
`ci/vN` in one atomic push, and never forces either. A second run on the same main commit does
nothing.

The script refuses, and writes nothing, in these cases:

- a half-made release (a tag that is not on the branch, or a branch head that has no tag);
- a symlink, submodule or other non-file in the release paths;
- a released workflow that starts on anything other than `workflow_call`.

Each refusal says what is wrong, and for a half-made release it gives the command that repairs it. Next, a smoke job uses a released action
the way a consumer does (a remote `uses:` of `ci-release`). It checks that the downloaded files are
exactly the release commit's files.

The push needs `FORK_PUSH_TOKEN` (fine-grained, this repository only, Contents and Workflows
read and write). It must be a secret of the Environment `fork-push`, whose deployment branches are
`main` only, and not a repository secret. `fork-follow.yml` uses the same rule. Until then the guard
job refuses. Someone with push rights can release from a clean checkout of main with
`python3 .github/scripts/fork-ci-release.py release --require-check --remote https://github.com/scuttletoby/t3code.git --repo scuttletoby/t3code --source <main tip sha>`
(`plan` instead of `release` shows what would happen and writes nothing).

## Protection

Two repository rulesets keep pinned releases alive. They are recorded in
[`.github/fork-ci-release-rulesets.json`](../.github/fork-ci-release-rulesets.json):

- `refs/tags/ci/*`: tags cannot be deleted or moved; new ones can be created;
- `refs/heads/ci-release`: the branch cannot be deleted or force-pushed.

To create them again (repository admin):

```sh
jq -c '.[]' .github/fork-ci-release-rulesets.json | while read -r ruleset; do
  printf '%s' "$ruleset" | gh api -X POST repos/scuttletoby/t3code/rulesets --input -
done
```
