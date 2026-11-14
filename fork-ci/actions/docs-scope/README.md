# docs-scope

A composite action that answers two questions about the change a CI run tests:

- **Is it docs-only?** Then the build and test jobs can pass at once.
- **Which jobs does it reach?** A job runs when a changed file that is not docs matches its paths. A file that is
  not docs but that no job names (a root build file, a workflow), or a change to the config, runs every job, unless
  the config's `no_build` says it builds nothing.

It is the general half of a docs-only gate. The project half (which files are docs, which docs a build or test
reads, which folders each job builds) is a JSON config in the calling repository.

## Usage

Pin a release commit by its full SHA, never a commit of main ([why](../../README.md#pinning)):

```yaml
jobs:
  build:
    steps:
      - uses: actions/checkout@<sha> # the default ref: on pull_request GitHub's merge commit ($GITHUB_SHA)
        with: { fetch-depth: 0 } # the merge base must be there
      # Optional, push only: the last commit whose push run of this workflow passed (see "The base on push").
      - id: green
        if: github.event_name == 'push'
        env: { GH_TOKEN: "${{ github.token }}" } # needs `actions: read`
        run: |
          sha=$(gh api "repos/$GITHUB_REPOSITORY/actions/workflows/ci.yml/runs?branch=main&event=push&status=success&per_page=1" \
            --jq '.workflow_runs[0].head_sha // ""') || sha=""
          echo "sha=$sha" >> "$GITHUB_OUTPUT"
      - id: scope
        uses: scuttletoby/t3code/fork-ci/actions/docs-scope@<release sha> # ci/vN
        with:
          config: .github/docs-scope.json # default
          base: ${{ steps.green.outputs.sha }} # empty: a full run
      - if: steps.scope.outputs.docs_only != 'true'
        run: ./gradlew build
      - if: fromJSON(steps.scope.outputs.jobs).engine != 'false'
        run: ./gradlew :engine:test
```

Always test `!= 'true'` and `!= 'false'`, never `== 'false'` or `== 'true'`: a missing or empty output then means
"run". `jobs` is `{}` when the change or the config is unknown, so every job reads as "run".

Run it as a step of the jobs it gates, as above. If it runs in a job of its own that others `need`, a failure of that
job skips them, and GitHub counts a skipped required job as passing: gate them with
`if: ${{ !cancelled() && (needs.scope.result != 'success' || fromJSON(needs.scope.outputs.jobs).engine != 'false') }}`.

### Outputs

| Output      | Value                                                                          |
| ----------- | ------------------------------------------------------------------------------ |
| `docs_only` | `true` only when every changed file is docs and the event may be docs-only     |
| `jobs`      | JSON `{"<job>": "true" \| "false"}`; `{}` when unknown                         |
| `reason`    | one line: `docs-only:`, `full run:`, `scoped run:` or `not docs-only:` and why |

The step log also prints one line per job: `job engine: run (engine/A.java matches its paths)` or
`job engine: skip (...)`. Control characters, line separators and bytes that are not UTF-8 in a file name are
printed escaped (`\n`, `\udcff`), so a file name can neither start a `::workflow-command::` line nor add an output.

## What "the change" is

- **pull_request:** the union of two diffs without rename detection (a rename is its old and its new path):
  merge base .. PR head (every push of the PR together, never the last push alone) and base .. GitHub's merge commit.
  HEAD must be that merge commit: HEAD == `$GITHUB_SHA`, it has two parents, and HEAD^2 is the event's
  `pull_request.head.sha`. Only then is HEAD^1 trusted as the base. A checkout of the PR head is a full run: if its
  last commit merged main in, HEAD^1 would be the PR's own commit, the PR's code would drop out of both diffs and the
  config would be read from the PR.
- **Any other event** (push, merge_group, workflow_dispatch): `base` .. HEAD, only when the caller gives `base` and
  HEAD == `$GITHUB_SHA`, and the base must be on HEAD's first-parent line. Without a base the run is a full run. Such
  an event is docs-only only when `docs_only_events` names it; its `jobs` are read either way, so a docs-only push to
  main can still skip the build jobs.

### The base on push

Pass the last commit whose **push** run of the workflow on main **passed**, or nothing (a full run). Never
`github.event.before`: if push A (code) went red or was cancelled (for example by `concurrency`), push B (docs) diffs
B against A, every job reads `false`, and main turns green with A's code never tested; a rerun of B gives the same
answer. Against the last green commit, A's code is in the diff until a run that tests it passes.

Keep `event=push` in the query: `branch` matches a run's head branch, so without it a passing `pull_request` run from
a fork whose branch is also called `main` counts, and its head commit is a PR head that no run tested as main (the PR
run tested GitHub's merge commit). The action enforces the same rule: the base must be on HEAD's first-parent line
(main's own pushes), so a merged branch's commit, another branch or a force-pushed-away commit is a full run, and so
is a base that is not in the checkout. A rerun can see a newer green base than the first run did; that only shrinks
the diff to commits a passing push run already tested.

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
  "no_build": [".github/ISSUE_TEMPLATE/**", "tools/notes/**"],
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

- `docs` (required): globs of docs files. `**/` is any number of folders, `**` anything (a newline in a name too),
  `*` and `?` stay in one name. A name with a control character or bytes that are not UTF-8 is never docs.
- `never_docs`: files that match `docs` but that a build, test or tool reads; each says why.
- `never_docs_from` / `paths_from`: paths listed in a JSON file of the repository. `list` is a dotted key to a list
  of objects (none: the file itself), `where` keeps the objects whose keys equal these values, `field` names the key
  (or keys) holding a path or a list of paths, `label` names an object in the log. An entry matches a path exactly or
  as a folder. The file is read at the base and at the head; an entry at either makes a file not docs or runs a job,
  so a pull request cannot free a file by removing it from the list. Whether a file is named by no job (so every job
  runs) is judged by the base's entries only: a pull request that adds an entry for a root file, even with a new
  module, adds that job but cannot stop the full run. A list that could drop a rule quietly is an error: a kept
  object with none of the `field` keys, a `field` key no kept object has (a typo, or a `where` that keeps nothing),
  or an entry that is not a plain repository path (`./x`, `/x`, `x/../y`, `x//y`, a wildcard), which could never
  match.
- `docs_only_events`: events that may be docs-only (default `["pull_request"]`).
- `jobs`: per job, `paths` globs and `paths_from` lists. Docs files never run a job; a `never_docs` file does. A
  file that is not docs and that no job names runs every job, and so does a change to the config.
- `no_build`: globs of files that are not docs but that no job builds (an issue template, a tool's notes). Such a
  file no longer runs every job, exactly as a file a job names no longer does; it is never a job output, and the
  log says `no_build: <file> builds nothing (matches <glob>)`. A file a job names still runs that job. It is read
  from the base like every other rule, so a pull request cannot add its own files to it. Default `[]`: a config
  without the key behaves as before. A release older than the one that added `no_build` reads the key as unknown,
  which is a full run: bump the pin before adding the key.

Unknown keys and wrong types are an error (a typo such as `never_doc` must not drop a rule quietly), and an error
means a full run that names it. The `config` input must be a plain repository path (no `./`, `..` or leading `/`),
so the "config is never docs" rule cannot be dodged by spelling.

## Why a pull request cannot loosen it

- The script runs from the action's own folder, at the SHA the caller pins: never a copy from the checkout.
- The config is read from the base commit with `git show`, never from the working tree. A pull request that changes
  the config is a full run (the config file is never docs).

What it cannot stop: a `pull_request` run uses the workflow file from the pull request, so a pull request that edits
the `uses:` line chooses the code that judges it. That edit is a change to a non-docs file, so every correct version
runs in full for it; pin only release commits ([`fork-ci/README.md`](../../README.md#pinning)).

## Fails closed

A full run (and `jobs` = `{}`) for: an event without a base, a base off HEAD's first-parent line, HEAD that is not
`$GITHUB_SHA`, a pull_request HEAD that is not GitHub's merge commit (two parents, HEAD^2 = the event's PR head; no
event file), no merge base (shallow checkout), a base that is not a commit, a git error, an empty change, a config
path that is not plain, no config at the base, a config that does not parse or has unknown keys or wrong types, a
`*_from` file at neither side or not readable as configured (not UTF-8, nested too deep, a missing field, an entry
that is not a plain path). Any other error is caught too and gives a full run with its message, never a step without
outputs. A symlink or submodule is never docs, and the diff uses `--ignore-submodules=none`, so neither
`ignore = all` in `.gitmodules` nor the runner's `diff.ignoreSubmodules` can hide a gitlink bump.

## Python API

Tools outside a workflow (a local check, a deploy script) ask the same rules about one path. Run the module from
the release the project pins, checked by blob id ([`pinned`](../pinned/README.md):
`load(config, 'docs-scope', 'docs_scope')`). Callers may import exactly these names; anything else may change in
any release.

| Name                                                                         | What it does                                                                        |
| ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `docs_rule(config, read, config_path=DEFAULT_CONFIG)`                        | `is_docs(path) -> bool`, the action's rule; unreadable config or list: always False |
| `job_rule(config, job, read, config_path=...)`                               | `runs(path) -> bool` for one job, as `decide()` answers it; unreadable: always True |
| `rules(config, read, config_path=...)`                                       | a `Rules` object; raises `Unclear` when the config or a list cannot be read         |
| `Rules.not_docs(path, plain=True)`                                           | `None` for docs, else why not (`plain=False`: a symlink or submodule)               |
| `Rules.job_runs(job, path)`, `Rules.job_hit(job, path)`                      | why a change of path runs the job (or `None`); why the job's paths name it          |
| `Rules.runs_every_job(path)`, `Rules.builds_nothing(path)`                   | the "no job names it" rule; the `no_build` glob that matches, if no job names it    |
| `decide(event, config_path, head, base, run, sha, event_path)`               | the action's whole answer: `(docs_only, jobs, reason lines)`                        |
| `the_change(event, head, base, run, sha, event_path)`                        | `(rows, base commit, what was compared)`: the change as the action computes it      |
| `glob_re(pattern)`, `PLAIN_MODES`, `git(*args)`, `Unclear`, `DEFAULT_CONFIG` | the glob rule, file modes, the git runner, the error, the default path              |

- `config` is the config as a dict or its JSON text, checked as the action checks it (a typo is `Unclear`). The
  caller's dict is never changed.
- `read(file)` returns a `*_from` file's text, or `None` when it does not exist. Pass one reader (the working tree)
  or a list `[base, head]` as the action reads them: an entry at either counts, and "no job names it" is judged by
  the first alone. No fake ref is needed.
- `decide()` runs on the same `Rules`, so the answers cannot differ (`docs-scope.test.py`, `PythonApi`, checks them
  side by side on a sample of paths).
- Kept for older callers, to be dropped later: `read_entries(src, refs, run)`, `union(...)`, `entry_match(...)`.
  Use `rules()` instead.

## Tests

`docs-scope.test.py` builds real git repositories with GitHub-style merge commits. It runs in the fork's CI
(`.github/fork-ci-tests.txt`): `python3 fork-ci/actions/docs-scope/docs-scope.test.py`.
