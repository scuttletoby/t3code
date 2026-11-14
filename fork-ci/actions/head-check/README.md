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
- **One lens name, one read:** each lens name counts once, by its newest
  comment, whoever posted it. If two reviewers post under the same name, the
  newer one wins: the check can pass on a head only the second one read. Two
  independent reviews of one angle need two different names (`correctness`
  and `correctness-2`, say), each in `required-lenses`.
- **Bots:** a login in the config's `bot_logins`, a login ending in `[bot]`,
  or an author of type Bot. `gh` gives only the login, without `[bot]`
  (`coderabbitai`, not `coderabbitai[bot]`), so list every bot that comments
  in `bot_logins`. Bots usually have no lens author association either.
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

It fails (exit 1), never passes, when anything is unknown: GitHub cannot be
read after three tries, its answer is malformed, the checkout's merge commit
merges another head than the one GitHub reports (a newer push has its own
run), or the read commit is not in the checkout and cannot be fetched (GitHub
no longer has it, or the checkout kept no credentials: see Usage).

It refuses to start (exit 2, before it reads GitHub, every reason at once) on
a config error (also a base commit without the config), a missing pull
request number (not a `pull_request` event), a lens name in `required-lenses`
that names no lens, a git older than 2.38, a checkout that is not GitHub's
two-parent merge commit, a checkout that is shallow, or a checkout holding a
file that changes what git's objects or merges say (`info/grafts`, `shallow`,
alternates, `info/attributes`). A refusal is a red required check too. git
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
    # edited: a base change (a stacked pull request retargeted) runs it again
    types: [opened, synchronize, reopened, ready_for_review, edited]
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
      - uses: scuttletoby/t3code/fork-ci/actions/head-check@<release sha of ci/vN> # ci/vN
        with:
          config: .github/pr-queue.json
          # required-lenses: | # optional: lenses that must read the head
          #   correctness
          #   security
          # pass-drafts: true # optional: with ready_for_review in the types above
```

The check reads the comments when it runs. After a lens or an override is
posted for the head, run it again (re-run the job, or let a tool the project
uses for its ready command do that); a new push runs it anyway. The `edited`
trigger runs it again when the base branch changes: the answer depends on the
base (its config, and which merged-in commits are the base's). A title or body
edit also runs it; it only reads, so that costs one short job.

**Pin it by the release sha, always.** Use the full sha a `ci/vN` release
names (the release sha of ci/vN), never a branch, a tag alone or a local path
(`uses: ./fork-ci/actions/head-check`), not even inside the repository that
holds the action. With a local path a pull request runs its own copy of
`head_check.py` and `lens.py` and can weaken the rules without touching
`.github/`. (This repository runs no head check of its own; its reusable queue
workflow checks out the queue at its own workflow commit and never runs for a
pull request.)

**Drafts:** with `pass-drafts: true` a draft's check is green without a read.
When the draft is marked ready, that old green result still counts for a few
seconds, until the `ready_for_review` run starts. Arm auto-merge (or merge)
only after this check's `ready_for_review` run has finished on that head, or
keep `pass-drafts` false.

**Credentials:** a read commit the head dropped (a rebase or force-push) is
fetched from `origin` with the credentials actions/checkout stored. With
`persist-credentials: false` on a private repository that fetch fails, and so
does the check (it never passes on it).

No step before it may run the pull request's code: that code could change the
checkout the check reads.

**What it does not stop.** A pull request can change the workflow file itself
(a `pull_request` run uses the merge commit's workflows). So this check stops
an unread push, not an author who rewrites the check. On a repository owned
by a user account, anyone who can push a branch can rewrite this job, or add
another passing job with the required check's name. GitHub's "require
workflows to pass" ruleset rule would close that, but it is for organizations
on paid plans only, not for user-owned repositories. Until there is a better
way, a person reads every pull request that changes `.github/workflows/` or
this action's files. The real fix is future work: a `pull_request_target` job
that runs code pinned on the base branch, posts its result through a separate
GitHub App (its key in an environment only the base branch can use), and a
required check bound to that App as its only allowed source. Tracked in
[issue 34](https://github.com/scuttletoby/t3code/issues/34).

### Inputs

| Input             | Default                            | Meaning                                                                                             |
| ----------------- | ---------------------------------- | --------------------------------------------------------------------------------------------------- |
| `config`          | (required)                         | the queue's JSON config, a path in the repository, read from the base commit                        |
| `required-lenses` | (empty)                            | lens names, one per line, that must be among the reads; matched as lens keys (case, `r2`)           |
| `pass-drafts`     | `false`                            | `true` passes a draft without reading its review; add `ready_for_review` to the workflow's triggers |
| `path`            | `.`                                | where the merge commit is checked out                                                               |
| `pr`              | `github.event.pull_request.number` | the pull request                                                                                    |
| `repo`            | `github.repository`                | owner/name                                                                                          |
| `token`           | `github.token`                     | reads the pull request's comments and reviews (`pull-requests: read`)                               |

`required-lenses` takes one lens name per line; blanks around a name and
empty lines are skipped. Never separate names with commas: a lens name may
hold one (`data-integrity (DI-1, DI-2)` is one name). A required lens is
matched by its lens key: the whole name, case and blanks normalised, minus a
trailing round marker (`Correctness r2` is `correctness`, `correctness (DI-1)`
is not). When a required name matches no counted lens comment, the check
fails and its reason, in the log and the summary, lists the lens names it
found, so a typo shows. Which lenses a pull request needs is the
project's to decide; a workflow can compute the list in an earlier step (from
labels or changed paths, say) and pass it in.

### Outputs

| Output   | Value                                                  |
| -------- | ------------------------------------------------------ |
| `result` | `pass` or `fail`; empty when the step refused to start |

The log and the job summary list each lens with `ok` or `fail` and a short
reason (the sha it read, an override, edited, hidden, a short sha, required
but missing).

| Exit | Meaning                                                                          |
| ---- | -------------------------------------------------------------------------------- |
| 0    | Pass.                                                                            |
| 1    | Fail, also when GitHub or the checkout cannot say.                               |
| 2    | Refused to start (config, arguments, git or checkout); read nothing from GitHub. |

## Tests

`python3 fork-ci/actions/head-check/head_check.test.py`: real commits (a
local bare repository stands in for GitHub, the checkout is a merge commit as
GitHub makes it) and a fake `gh`; it fails the run if anything starts the real
`gh`.
