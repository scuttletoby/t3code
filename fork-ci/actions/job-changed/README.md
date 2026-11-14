# job-changed

A composite action that answers one question for one job: **can the change from `base` to HEAD change what this
job does?** A workflow edit that only touches another job, a comment, a blank line, key order or quoting then does
not rebuild the job.

It is the general half of a "did this job's inputs change" gate. The project half (which source folders a job
builds, which docs a build reads) stays in the calling repository: pass folders as `paths`, or gate them with
[docs-scope](../docs-scope/README.md).

## Usage

Pin the action by full SHA (the tagged one, so a rebase of this fork cannot orphan it):

```yaml
jobs:
  plugins:
    steps:
      - uses: actions/checkout@<sha> # the default ref: on pull_request GitHub's merge commit
        with: { fetch-depth: 0 } # the base must be in the checkout
      - id: changed
        uses: scuttletoby/t3code/fork-ci/actions/job-changed@<full sha> # ci/vN
        with:
          base: ${{ github.event_name == 'pull_request' && 'HEAD^1' || steps.green.outputs.sha }}
          workflow: .github/workflows/ci.yml
          # job: defaults to github.job
          paths: | # optional: changed files that also count (Python regular expressions, re.search)
            ^plugins/
            ^tools/build\.py$
      - if: steps.changed.outputs.changed != 'false'
        run: ./gradlew build
```

Always test `!= 'false'`, never `== 'true'`: a missing or empty output (a step that failed, a runner without
Python) then means "run".

Run it as a step of the job it gates, as above. If it runs in a job of its own that others `need`, a failure of that
job skips them, and GitHub counts a skipped required job as passing: gate them with
`if: ${{ !cancelled() && (needs.scope.result != 'success' || needs.scope.outputs.changed != 'false') }}`.

### Inputs

| Input      | Default      | Meaning                                                                              |
| ---------- | ------------ | ------------------------------------------------------------------------------------ |
| `base`     | (empty)      | the commit to compare with; empty or not in the checkout means `changed=true`        |
| `head`     | `HEAD`       | the commit to judge                                                                  |
| `workflow` | (required)   | the workflow file, a path in the repository                                          |
| `job`      | `github.job` | the job id (in a reusable workflow, the called workflow's file and job)              |
| `paths`    | (empty)      | one regular expression per line; a changed file one finds counts (`#` lines skipped) |

### Outputs

| Output     | Value                                                                               |
| ---------- | ----------------------------------------------------------------------------------- |
| `changed`  | `false` only when neither the workflow part nor a path counts                       |
| `workflow` | the workflow part alone: `false` only when the workflow change cannot reach the job |
| `reason`   | one line: `changed: ...` or `unchanged: ...` and why                                |

`workflow` is for a caller that already knows its paths and only needs the workflow question, for example a test
scope that widens to every test when the job's own definition changed (not when another job's did).

### The base

- **pull_request** (checkout of GitHub's merge commit): `HEAD^1`, the base branch as GitHub merged it.
- **push**: the last commit whose push run of this workflow passed (see docs-scope's README, "The base on push",
  for the query), or nothing (always `changed`). Never `github.event.before`: if push A changed this job's block and
  its run was cancelled or red, push B diffs against A, the job reads `false`, and A's change is never tested.

## The rule

The workflow file counts for the job when one of these differs between the two commits:

- the job's own block;
- the block of every job it `needs`, directly or not (their outputs and results feed it);
- the workflow-level `on`, `env`, `defaults` or `permissions`;
- any other top-level key except `jobs`, `name`, `run-name` and `concurrency` (an unknown key could matter).

Both sides are compared parsed, in two views, and a part counts when either view differs:

- **text**: every scalar as written (YAML's BaseLoader), so `1` -> `true` or `0755` -> `493` counts;
- **types**: YAML's SafeLoader values compared strictly, so `3.10` -> `'3.10'` (the number 3.1 becomes a string) or
  `1` -> `'1'` counts, and `1`, `True` and `1.0` are all different.

Comments, blank lines, key order, flow vs block style and quoting that keeps the type (`'x'` -> `"x"` -> `x`) never
count. Aliases are resolved first, so an anchor edited in another job reaches every job that uses it.

`paths` never matches the workflow file itself: only its job compare decides about it.

**What only `paths` sees.** The action compares one workflow file. Anything else the job runs counts only when a
`paths` line finds it: local actions (`uses: ./...`), local reusable workflows it calls, and scripts or config files
its steps run. Inside a reusable workflow, edits in the calling workflow (its `with:`, `secrets:`, the calling job's
`if:` or `needs`) never reach the called file's compare: add the caller's file to `paths`, or run the check in the
calling workflow.

**Typed values.** The typed view is PyYAML's YAML 1.1 reading. Where it and GitHub's reader may differ (for example
an unquoted `0o17`, which PyYAML reads as text and GitHub may read as a number), quote the value.

## Fails closed

`changed=true` (and `workflow=true`), with the reason in the log and in `reason`, for: an empty `base`, a base or
head that is not a commit in the checkout (first push, force push, a base that is gone, a shallow checkout), a
workflow path that is not at HEAD (a wrong path must never read as "never changed"), a side that does not parse in
either view (a syntax error, an alias without its anchor, a date like `2026-13-45` or an unknown `!tag`, nesting too
deep), a file that is not a workflow (no `jobs` mapping), a new or deleted workflow, the job or one it needs missing on
either side (renamed, new, deleted, or a typo in `job`), a `paths` line that is not a regular expression, a failed
`git diff`, and any other error. The step never ends without outputs.

**No PyYAML on the runner:** the action installs nothing (an unpinned download at run time would be new code no
review saw). A changed workflow file then reads as `changed=true`, and the step prints a `::warning::` annotation
saying so; an unchanged workflow file needs no parsing and reads normally. Install `python3-yaml` on self-hosted
runners; GitHub's Ubuntu images have it.

## Why a pull request cannot loosen it

- The script runs from the action's own folder, at the SHA the caller pins: never a copy from the checkout.
- The workflow file is read from git objects (`git show <commit>:<path>`), never from the working tree.
- Inputs reach the script through environment variables only, never `${{ }}` inside `run:`.
- Printed text escapes control characters, so a job or file name can neither start a `::workflow-command::` line nor
  add an output line.

What it cannot stop: a `pull_request` run uses the workflow file from the pull request, so a pull request that edits
the `uses:` line or the job's `if:` chooses what judges it. Such an edit changes the job's own block, so every correct
version reads it as `changed`.

## State

The action writes nothing but its step outputs: no cache, no file, no label. A rerun of the same commits gives the
same answer (on push, a rerun may see a newer green base, which only shrinks the diff to commits a passing run
tested). Stopping half-way leaves nothing behind. An old or empty history (first push, shallow clone, force push)
has no base and reads as `changed`.

## Tests

`job-changed.test.py` runs the action's real `run:` text with bash in real git repositories, plus the rule on this
repository's own workflows. It runs in the fork's CI (`.github/fork-ci-tests.txt`):
`python3 fork-ci/actions/job-changed/job-changed.test.py`.
