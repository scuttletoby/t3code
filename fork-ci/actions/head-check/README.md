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
  looks like a lens report read nothing. A review's edited and hidden flags
  come from the queue's own GraphQL query, never from `gh pr view` (which
  prints `includesCreatedEdit: false` for every review without asking GitHub).
  The same query reads the inline review comments (on a line of the diff),
  which `gh pr view` does not list and which count like any other comment. It
  runs for every pull request; if it fails after the retries, the check
  fails. The pr-queue README, section "Lens comments", has the full grammar.
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
this action's files. The gate below closes that: the same rules, run from the
default branch and posted by an App that only the default branch can use.

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

## The gate

The same check as a gate no pull request can rewrite: the reusable workflow
[`.github/workflows/fork-head-gate.yml`](../../../.github/workflows/fork-head-gate.yml)
(script `head_gate.py`). Use it instead of the action when the check must be
trusted, not only helpful ([issue 34](https://github.com/scuttletoby/t3code/issues/34)).

How it works:

- A caller on the default branch ([`gate-caller.example.yml`](gate-caller.example.yml))
  runs on `pull_request_target`, `issue_comment` and `workflow_dispatch`.
  For a pull request into the default branch GitHub takes that file from the
  default branch, so a pull request that edits it changes nothing for its own
  run. The caller passes `secrets: inherit` (see "What it does not stop").
- The gate's code comes from the workflow's own pinned release commit, never
  from the caller's tree. Nothing of the pull request is checked out: the
  head and the base tip are read from the API and fetched by sha into a new
  bare repository, and `head_check.decide` (the same rule code as the action,
  in bare mode) judges them. The config is read from the base tip.
- **A check run belongs to a commit, not to a pull request.** The newest check
  run of the name on a head counts for every pull request with that head. So
  the gate judges a head, never one pull request alone:
  - Only open, ready pull requests into the default branch are judged. A
    draft, or a pull request into any other branch, gets nothing posted, ever
    (there is no "pass drafts" in the gate). A draft is judged when it is
    marked ready.
  - Every open, ready pull request into the default branch with the head is
    judged in the same run, and the head passes only when all of them pass:
    the strictest verdict wins.
  - Runs on one head wait for each other (concurrency per head), and each
    reads every such pull request again when it starts. So the newest check
    run on a head always comes from the newest complete read.
- The guard job (no Environment) refuses unless the App key can only be the
  Environment's: no repository or organisation secret of its name, the
  Environment exists, and its deployment branches are exactly one rule, the
  default branch. The gate job checks the Environment again right before the
  token is made.
- Before any token, the gate job refuses, and posts nothing, when:
  - the run is a re-run (attempt 2 or later). A re-run runs the caller and
    gate release of its first attempt, and a re-run of the gate job alone
    skips the guard job. Dispatch the gate with the pull request number
    instead.
  - the caller is not the default branch's current one: the caller file
    changed since the event, or it no longer pins this gate release. A run
    can wait a long time for an earlier run on the same head.
  - the required lenses name no lens. With none, a head with no lens comment
    would pass, so the input is required.
- Only three steps touch the key. One makes a token of the gate App (checks
  write, this repository). **start** posts an in-progress check run on the
  head *before* anything is judged; an in-progress check never passes. Then
  **finish** (it runs always, also after a failed or cancelled step) completes
  that check run with the verdict, or with a failure when there is none. The
  ruleset requires the check from that App alone, so a job of a pull
  request's own workflow with the same name does not count.
- A dispatch without a pull request number judges the head of every open,
  ready pull request into the default branch (the backfill, each head once,
  at most 256; more is refused).

Setup (the repository owner's steps; nothing here creates them):

1. Create a GitHub App with one permission, Checks: read and write, and no
   webhook. Make a private key.
2. Install it on the repository only.
3. Create an Environment (default name `head-gate`) whose deployment branches
   are one rule: the default branch. Add the variable `HEAD_GATE_APP_ID` and
   the secret `HEAD_GATE_APP_KEY`. Keep no repository or organisation secret of
   that name: the guard refuses one.
4. Copy the example caller to `.github/workflows/` on the default branch, fill
   in the check name, config path and required lenses (at least one), and pin
   the release.
5. Dispatch it once without a number (the backfill), so open pull requests get
   the check under its name.
6. Bind the required check to the App: in a ruleset, the required status
   check's `integration_id` is the App's id; in classic branch protection, its
   `app_id`. The gate warns (never fails) when the base branch does not bind it.
7. Set the queue config's `head_check_app` to the App's id
   ([pr-queue](../pr-queue/README.md)), so the queue counts only that App's
   check runs. Release the code first, then bump the pins, then add the key:
   an older queue refuses a key it does not know.

After a change to the caller or a new pin, dispatch the backfill again: a run
that started before the change is refused, and nothing else judges the open
pull requests again.

**Run again after a review.** Reviews and inline diff comments
(`pull_request_review`, `pull_request_review_comment`) do not start the gate,
though the rules count them (a lens may post as a review or on a line). GitHub
runs those two events from the pull request's own merge commit, not from the
default branch: the pull request's copy of the caller runs, the Environment
(default branch only) refuses it, and it could never post. After a lens posts
as a review or on a line of the diff, or deletes one, post an issue comment on
the pull request or dispatch the gate with its number. A new push, ready or
draft, a base change and any issue comment start it anyway. A trusted way to
run on reviews is open:
[issue 88](https://github.com/scuttletoby/t3code/issues/88).

**Runs again, stops half-way, old state.**

- *Runs again.* Each run judges what GitHub says now. A new run on a head
  first posts a new in-progress check run, then completes it. The newest check
  run of that name from the App is the one that counts.
- *Stops half-way.* A run that stops after start (an API error, a crash, a
  timeout, a cancel) leaves its in-progress check run, or finish completes it
  as a failure. Either way an older pass on the head no longer counts, and
  the gate job is red. If start itself cannot post (GitHub refuses or is
  down), nothing on the head changes and the job is red. That is the one case
  where an older verdict still stands: until the next event, or a dispatch.
- *The head moves.* If the head moves while a run works, that run finds no
  pull request with its old head and posts nothing. If it was already
  judging, it posts a failure on the old head ("a newer head has its own
  run"), and the new head gets its own run.
- *A key that is rotated or deleted* makes no token: the job fails and nothing
  is posted, so the required check stays missing or as it was.
- *Old state.*
  - A check run of the same name posted by GitHub Actions (an older,
    non-gate check) does not count once the ruleset binds the check to the
    App.
  - A head shared by two pull requests is judged for both (above).
  - A repository without the Environment is refused by the guard before any
    token is made.

**What it does not stop.**

- Anyone who can change the default branch (or the Environment, the App or
  the ruleset) can change the gate.
- A lens comment is still only as good as the people allowed to post it (the
  config's `lens_author_associations`).
- The gate judges the comments that exist when it runs. Between a change (a
  deleted comment, a sibling pull request marked ready) and the start of the
  run it triggers, the check shows the older answer. A run waiting for an
  earlier one on the same head extends that window.
- The caller must pass `secrets: inherit`. Without it, the guard sees no
  repository or organisation secret of the key's name, so it cannot refuse
  one. Nothing in the gate can check what the caller passes; the Environment
  rule (default branch only) still holds the key.

## Tests

`python3 fork-ci/actions/head-check/head_check.test.py`: real commits (a
local bare repository stands in for GitHub, the checkout is a merge commit as
GitHub makes it) and a fake `gh`; it fails the run if anything starts the real
`gh`.
`python3 fork-ci/actions/head-check/head_gate.test.py`: the gate's steps with
a fake `gh`, and its judge on the same real commits in bare mode. The workflow
itself: `.github/scripts/fork-head-gate-workflow.test.py` and the head-gate
section of `.github/scripts/fork-contracts.test.py`.
