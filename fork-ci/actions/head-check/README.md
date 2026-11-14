# head-check

A composite action that answers one question for a pull request: **is its head
the change the review read?** It is the required `head_check` of the merge
queue ([pr-queue](../pr-queue/README.md)), with the queue's own rules.

The queue re-arms auto-merge only for a head that holds exactly the change the
lenses read, but it only touches pull requests that are behind their base. A
plain push to an armed pull request that is already up to date would merge
unread. This check, required by the branch ruleset, fails that head.

## The rules

All of them are the queue's, imported from `../pr-queue`, never copied:

- **The review** (`lens.py`, `Review.reviewed_head` and `Review.lens_reads`):
  the one full 40-hex sha every lens's newest comment read, or a newer
  override comment's sha. Only comments and reviews by the config's
  `lens_author_associations` count, and bots never do. A lens comment edited
  after posting or hidden (minimized), a short sha, and a comment that only
  looks like a lens report read nothing. The pr-queue README, section "Lens
  comments", has the full grammar.
- **The same change** (`pr_queue.holds`): the head is the commit read, or the
  base merged into it cleanly, byte for byte (a queue update, up to three in a
  row). A push after the review, a conflict resolution or an extra change in a
  merge is not the same change.
- **The config** is the queue's own JSON file (`config.py`). It is read from
  the base commit that the run's merge commit tests, never from the pull
  request's copy, so a pull request cannot change whose comments count for its
  own check.

It passes when:

- every lens read the head, or a newer override vouches for it, and every
  required lens (`required-lenses`) is among them;
- the pull request has no lens comments and no lens is required: there is
  nothing to compare (the queue then vouches only for the head it found);
- it is a draft and `pass-drafts` is `true`.

It fails, never passes, when anything is unknown: GitHub cannot be read after
three tries, its answer is malformed, the checkout is not GitHub's two-parent
merge commit of the head GitHub reports (a newer push has its own run), or the
read commit is neither in the checkout nor on GitHub.

It refuses to start (exit 2, every reason at once) on a config error, a
missing pull request number (not a `pull_request` event), a lens name in
`required-lenses` that names no lens, a git older than 2.38, a checkout that
is shallow, or a checkout holding a file that changes what git's objects or
merges say (`info/grafts`, `shallow`, alternates, `info/attributes`). git
runs with no hooks, no fsmonitor, no replace refs and no attributes: the pull
request's `.gitattributes` cannot turn a conflict into a clean merge.

It reads only and posts nothing. The one write is a fetch, into the job's own
checkout, of a read commit the head dropped (a rebase or force-push; GitHub
keeps it, and the queue fetches it the same way). So a re-run on the same head
gives the same answer and adds nothing, and a run stopped half-way leaves
nothing behind.

## Usage

Run it in a job of its own, right after the checkout of the merge commit, and
make that job a required check (the config's `head_check`):

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
permissions: {}
jobs:
  review-check:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
    steps:
      - uses: actions/checkout@<sha> # the default ref: on pull_request GitHub's merge commit
        with: { fetch-depth: 0 } # the base's history is needed
      - uses: OWNER/REPO/fork-ci/actions/head-check@<40-hex sha>
        with:
          config: .github/pr-queue.json
          # required-lenses: | # optional: lenses that must read the head
          #   correctness
          #   security
          # pass-drafts: true # optional: with ready_for_review in the types above
```

The check reads the comments when it runs. After a lens or an override is
posted for the head, run it again (re-run the job, or let a tool the project
uses for its ready command do that); a new push runs it anyway.

No step before it may run the pull request's code: that code could change the
checkout the check reads. A pull request can also change the workflow file
itself (a `pull_request` run uses the merge commit's workflows), so this check
stops an unread push, not an author who rewrites the check; reviewers read
workflow changes, or the project pins the check with a ruleset's required
workflow.

### Inputs

| Input             | Default                                 | Meaning                                                                                              |
| ----------------- | --------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `config`          | (required)                              | the queue's JSON config, a path in the repository, read from the base commit                         |
| `required-lenses` | (empty)                                 | lens names, one per line or comma, that must be among the reads; matched as lens keys (case, `r2`)   |
| `pass-drafts`     | `false`                                 | `true` passes a draft without reading its review; add `ready_for_review` to the workflow's triggers  |
| `path`            | `.`                                     | where the merge commit is checked out                                                                |
| `pr`              | `github.event.pull_request.number`      | the pull request                                                                                     |
| `repo`            | `github.repository`                     | owner/name                                                                                           |
| `token`           | `github.token`                          | reads the pull request's comments and reviews (`pull-requests: read`)                                |

A required lens is matched by its lens key: the whole name, case and blanks
normalised, minus a trailing round marker (`Correctness r2` is `correctness`,
`correctness (DI-1)` is not). Which lenses a pull request needs is the
project's to decide; a workflow can compute the list in an earlier step (from
labels or changed paths, say) and pass it in.

### Outputs

| Output   | Value                                                  |
| -------- | ------------------------------------------------------ |
| `result` | `pass` or `fail`; empty when the step refused to start |

The log and the job summary list each lens with `ok` or `fail` and a short
reason (the sha it read, an override, edited, hidden, a short sha, required
but missing).

| Exit | Meaning                                                           |
| ---- | ----------------------------------------------------------------- |
| 0    | Pass.                                                             |
| 1    | Fail, also when GitHub or the checkout cannot say.                |
| 2    | Refused to start (config, arguments, git or checkout); read nothing from GitHub. |

## Tests

`python3 fork-ci/actions/head-check/head_check.test.py`: real commits (a
local bare repository stands in for GitHub, the checkout is a merge commit as
GitHub makes it) and a fake `gh`; it fails the run if anything starts the real
`gh`.
