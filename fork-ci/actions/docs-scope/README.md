# docs-scope

A composite action that answers two questions about the change a CI run tests:

- **Is it docs-only?** Then the build and test jobs can pass at once.
- **Which jobs does it reach?** A job runs only when a changed file that is not docs matches its paths.

It is the general half of a docs-only gate. The project half (which files are docs, which docs a build or test
reads, which folders each job builds) is a JSON config in the calling repository.

## Usage

Pin the action by full SHA (the tagged one, so a rebase of this fork cannot orphan it):

```yaml
jobs:
  build:
    steps:
      - uses: actions/checkout@<sha> # pull_request: GitHub's merge commit, refs/pull/N/merge
        with: { fetch-depth: 0 } # the merge base must be there
      - id: scope
        uses: scuttletoby/t3code/fork-ci/actions/docs-scope@<full sha> # ci/vN
        with:
          config: .github/docs-scope.json # default
          base: ${{ github.event_name == 'push' && github.event.before || '' }} # optional, see below
      - if: steps.scope.outputs.docs_only != 'true'
        run: ./gradlew build
      - if: fromJSON(steps.scope.outputs.jobs).engine != 'false'
        run: ./gradlew :engine:test
```

Always test `!= 'true'` and `!= 'false'`, never `== 'false'` or `== 'true'`: a missing or empty output then means
"run". `jobs` is `{}` when the change or the config is unknown, so every job reads as "run".

### Outputs

| Output      | Value                                                                      |
| ----------- | -------------------------------------------------------------------------- |
| `docs_only` | `true` only when every changed file is docs and the event may be docs-only |
| `jobs`      | JSON `{"<job>": "true" \| "false"}`; `{}` when unknown                     |
| `reason`    | one line: why (every skip and every full run says why)                     |

The step log also prints one line per job: `job engine: run (engine/A.java matches its paths)` or
`job engine: skip (...)`.

## What "the change" is

- **pull_request:** the union of two diffs without rename detection (a rename is its old and its new path):
  merge base .. PR head (every push of the PR together, never the last push alone) and base .. GitHub's merge commit.
  HEAD must be that merge commit.
- **Any other event** (push, merge_group, workflow_dispatch): `base` .. HEAD, only when the caller gives `base`.
  Without it the run is a full run. Such an event is docs-only only when `docs_only_events` names it; its `jobs` are
  read either way, so a docs-only push to main can still skip the build jobs.

## Config

```json
{
  "docs": ["**/*.md"],
  "never_docs": [
    { "glob": "**/src/**", "why": "inside a source set (packed into jars)" },
    { "glob": "**/tests/**", "why": "inside a test folder" },
    { "glob": "tools/CLAIMS.md", "why": "read by the release tool" }
  ],
  "never_docs_from": [
    {
      "file": "tools/modules.json",
      "list": "modules",
      "field": "inputs",
      "label": "id",
      "why": "a module input"
    }
  ],
  "docs_only_events": ["pull_request"],
  "jobs": {
    "engine": {
      "paths": ["engine/**", "shared/items/**"],
      "paths_from": [
        {
          "file": "tools/modules.json",
          "list": "modules",
          "where": { "kind": "engine-module" },
          "field": ["paths", "inputs"],
          "label": "id"
        }
      ]
    }
  }
}
```

- `docs` (required): globs of docs files. `**/` is any number of folders, `**` anything, `*` and `?` stay in one name.
- `never_docs`: files that match `docs` but that a build, test or tool reads; each says why.
- `never_docs_from` / `paths_from`: paths listed in a JSON file of the repository. `list` is a dotted key to a list
  of objects (none: the file itself), `where` keeps the objects whose keys equal these values, `field` names the key
  (or keys) holding a path or a list of paths, `label` names an object in the log. An entry matches a path exactly or
  as a folder. The file is read at the base and at the head; an entry at either counts, so a pull request cannot
  free a file by removing it from the list.
- `docs_only_events`: events that may be docs-only (default `["pull_request"]`).
- `jobs`: per job, `paths` globs and `paths_from` lists. Docs files never run a job; a `never_docs` file does.

Unknown keys are an error (a typo such as `never_doc` must not drop a rule quietly), and an error means a full run.

## Why a pull request cannot loosen it

- The script runs from the action's own folder, at the SHA the caller pins: never a copy from the checkout.
- The config is read from the base commit with `git show`, never from the working tree. A pull request that changes
  the config is a full run (the config file is never docs).

What it cannot stop: a `pull_request` run uses the workflow file from the pull request, so a pull request that edits
the `uses:` line chooses the code that judges it. That edit is a change to a non-docs file, so every correct version
runs in full for it; pin only SHAs that this fork's CI tested and tagged.

## Fails closed

A full run (and `jobs` = `{}`) for: an event without a base, a pull_request HEAD that is not a two-parent merge
commit, no merge base (shallow checkout), a base that is not a commit, a git error, an empty change, no config at
the base, a config that does not parse or has unknown keys, a `*_from` file at neither side or not readable as
configured. A symlink or submodule is never docs. A crash fails the step, so the job is red.

## Tests

`docs-scope.test.py` builds real git repositories with GitHub-style merge commits. It runs in the fork's CI
(`.github/fork-ci-tests.txt`): `python3 fork-ci/actions/docs-scope/docs-scope.test.py`.
