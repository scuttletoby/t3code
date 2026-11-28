# checkout-state

A composite action, a lint: it **fails when a job on a self-hosted runner could leave checkout state in the
runner's work folder for the next job**. Run it in the pull request check, so such a workflow never merges.

A self-hosted runner keeps one work folder per repository (`_work/<repo>/<repo>`), and every job of that repository on
that runner checks out into it. `actions/checkout` cleans the files (`git clean -ffdx`, `reset --hard`) but not every
setting an earlier checkout wrote into `.git`. A non-cone `sparse-checkout` leaves `core.sparseCheckout=true` in the
folder's local git config. That setting outlives the next job's `git sparse-checkout disable`, so every later job on
that runner builds from a partial tree.

## What it refuses

On any runner that is not one of GitHub's fresh machines (`ubuntu-*`, `windows-*`, `macos-*`; a self-hosted label, a
runner group or an expression all count as shared):

- an `actions/checkout` step with `sparse-checkout` (any value, `false` too: the action reads it as patterns),
  `filter` (a partial clone: the promisor remote and its filter stay) or `clean` other than true. A `path:` does not
  make it safe: `actions/checkout` only accepts a path inside the shared work folder;
- a `run:` step, or any `with:` input of another step or of a reusable workflow's job, that sets up a sparse or
  partial clone (`sparse-checkout`, `core.sparseCheckout`, `extensions.partialClone`, `.promisor`,
  `.partialCloneFilter`, `git ... --filter`): an action may run an input as a command;
- a local action (`uses: ./x`) that is not a composite action it can read. A composite one is read and checked the
  same way, nested ones too;
- an action from another repository (`owner/repo[/path]@ref`, `docker://`) or a reusable workflow without a reviewed
  entry in the config for its exact `uses:` (a new pin needs a new review). GitHub's own `actions/...` are trusted
  apart from their inputs;
- a `runs-on` that is not a label, a list of labels or a runner group, and a workflow file it cannot read;
- a config entry that nothing uses (remove it).

The workflow files are read as GitHub reads them (YAML 1.2): `clean: yes` is the string `yes`, not true.

## Usage

Pin a release commit by its full SHA, never a commit of main ([why](../../README.md#pinning)):

```yaml
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>
        with: { persist-credentials: false }
      - uses: scuttletoby/t3code/fork-ci/actions/checkout-state@<release sha> # ci/vN
        # with:
        #   config: .github/checkout-state.json # the default, read when it is there
```

| Input    | Default | Meaning                                                                                  |
| -------- | ------- | ---------------------------------------------------------------------------------------- |
| `config` | (empty) | the config, a path in the checkout; empty: `<root>/.github/checkout-state.json` if there |
| `root`   | `.`     | the repository to scan, relative to the workspace                                        |

No outputs. Exit 0 clean, 1 with one line per problem, 2 when it cannot judge (below). It runs no git and writes no
file. It needs Python 3 and PyYAML (`python3-yaml`; GitHub's Ubuntu images have it), as job-changed does.

The config is read from the checkout: an entry a pull request adds is part of what its review reads.

## Config

Optional. Project facts only: the folders besides `.github/workflows` that hold workflows for this repository's
shared runners, and the reviewed reusable workflows and actions from other repositories.

```json
{
  "workflows": ["ci/workflows/*.yml"],
  "reusable": [
    {
      "workflow": ".github/workflows/queue.yml",
      "job": "queue",
      "uses": "owner/repo/.github/workflows/queue.yml@<sha>",
      "why": "its checkouts go to path: caller and path: toolkit, nested repositories of their own",
      "runner-input": "runs-on"
    }
  ],
  "actions": [
    {
      "uses": "owner/repo/some-action@<sha>",
      "why": "one bash step; its git calls only read"
    }
  ]
}
```

- `reusable`: one entry per calling job (`workflow` and `job` say which), for the exact `uses:`. Such a job counts as
  shared whatever it hands the called workflow, unless `runner-input` names the input the reviewed workflow really
  runs its jobs on: then that input's value decides, as a job's `runs-on` would.
- `actions`: the exact `uses:` of an action from another repository and why it leaves nothing behind.

A project that used its own copy of this lint keeps only this file and one test that its repository scans clean
(the action exiting 0 in its check is that test), and deletes its copy of the script and its rule tests.

## Fails closed

Exit 2, nothing passes: no PyYAML, a root that is not a folder, no workflow file under the root (a wrong `root` or an
empty checkout), a config that cannot be read (a broken link, also at the default path), is not JSON, or has a key
this release does not know (a newer release's: move the pin first).

## What it does not do

It stops a merge; it does not heal a folder that a job broke anyway (a pull request's own CI before the lint, a
re-run of an old branch). On a self-hosted runner, run [git-clean](../git-clean/README.md) as each job's first step
(it empties the workspace), or heal the folder in a job-started hook on the runner host. Such a hook is installed on
the host, not pinned: moving a fork-ci pin does not change what the host runs.

## Tests

```sh
python3 fork-ci/actions/checkout-state/checkout_state.test.py
```
