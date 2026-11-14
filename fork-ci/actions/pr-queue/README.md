# pr-queue

A merge queue for a repository whose branch ruleset requires pull requests to be
up to date with their base branch. After each push to the base branch it brings
exactly one waiting pull request up to date, and it turns auto-merge back on
only for a head that holds exactly the change the review read.

## Config

One JSON file in the project's repository. Unknown keys, a key written twice, a
missing required key, an empty `required_checks` or a value of the wrong type is
an error: the queue then exits non-zero and touches no pull request.

```json
{
  "required_checks": ["build", "test", "review-check"],
  "merge_method": "squash",
  "delete_branch": true,
  "bot_logins": ["coderabbitai", "github-actions", "copilot"],
  "lens_author_associations": ["OWNER", "COLLABORATOR"],
  "override": { "prefix": "tool ready --head", "author_association": "OWNER" },
  "ready_command": "tool ready {pr}",
  "push_command": "tool push",
  "lens_format_doc": "docs/review.md, section Lens comments",
  "head_check": "review-check"
}
```

| Key                        | Required | Meaning                                                                                                                                                                                                                                                                                        |
| -------------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `required_checks`          | yes      | Exactly the checks the branch ruleset requires. Not empty.                                                                                                                                                                                                                                     |
| `merge_method`             | yes      | `merge`, `squash` or `rebase`: how auto-merge is turned back on.                                                                                                                                                                                                                               |
| `delete_branch`            | yes      | Whether auto-merge deletes the branch after merging.                                                                                                                                                                                                                                           |
| `bot_logins`               | yes      | Logins whose comments never count as a lens. Any login ending in `[bot]` is a bot too. May be empty.                                                                                                                                                                                           |
| `lens_author_associations` | yes      | Whose comments count as lens comments: GitHub's author association of the comment, from `OWNER`, `MEMBER`, `COLLABORATOR`. Everyone else's comments are ignored (Lens comments). `MEMBER` and `COLLABORATOR` do not mean write access: list only those whose holders may all vouch for a head. |
| `override`                 | yes      | The comment that vouches for a head when the lenses read older commits: `<prefix> <40-hex sha>: <why>`, posted with this author association (`OWNER`, `MEMBER` or `COLLABORATOR`, with the same caveat) and never edited.                                                                      |
| `ready_command`            | yes      | The command people run to re-arm a pull request after a lens read; `{pr}` is the number. Only quoted in the queue's comments.                                                                                                                                                                  |
| `push_command`             | yes      | The command people run to push a fixed branch. Only quoted in comments.                                                                                                                                                                                                                        |
| `lens_format_doc`          | yes      | Where the project documents its lens comments. Only quoted in comments.                                                                                                                                                                                                                        |
| `head_check`               | yes      | The required check that fails a head no lens read; it must be one of `required_checks`. The queue acts only on pull requests that are behind: without this check, a push to an armed pull request that is already up to date merges unread.                                                    |
| `wait_seconds`             | no       | How long to wait for GitHub to show the updated head (default 60).                                                                                                                                                                                                                             |
| `poll_seconds`             | no       | How often to look meanwhile (default 5).                                                                                                                                                                                                                                                       |
| `list_limit`               | no       | How many open pull requests to read (default 50).                                                                                                                                                                                                                                              |
| `update_when_base_was_red` | no       | `true` updates a behind pull request whose required checks failed only because the base commit they tested failed them too (section "The base was red"). Set it only when the job of every required check runs the `tested-base` action. Default `false`.                                      |

## Lens comments

A review is posted as lens comments: one comment per review angle, whose
first line is its header.

```
lens: correctness · reviewer: <who> · read at: <sha>
```

`Lens:`, `**Lens:**`, `## lens:` and `- lens:` work too; the separator may be
`·`, `,`, `—` or nothing, and `<sha>` is the full 40-hex commit sha. A shorter
sha reads nothing: a force-push can bring a new commit with the same prefix. The
sha need not still be a commit of the pull request (a rebase drops it); the
queue re-arms only a head whose change is exactly the change read there. Each
lens's newest comment counts. The reviewed head is the one commit every lens's
newest read names, or a newer override comment's sha (config `override`). A
pull request with no lens comments is not reviewed, and the queue then vouches
only for the head it found.

Only comments and reviews that GitHub records as written by one of
`lens_author_associations` count: anyone can comment on a public repository,
so everyone else's comments are neither reads nor reports. The run log names
each lens-looking comment it ignored this way and its author association, so a
wrong `lens_author_associations` shows there. A lens comment edited after
posting, or hidden (minimized), reads nothing until that lens posts a new
comment: an edit can move `read at` to a head nobody read, and hiding old review
rounds must not make a reviewed pull request look unreviewed (the queue would
then vouch for whatever head it found). A lens a later round drops therefore
needs an override, or its old comment deleted. A hidden override counts
nothing. GitHub shows no hidden state for reviews, so a review counts as shown.

The grammar is fixed: a project writes its lens reports this way, it does not
configure another way. It fails closed. A comment that looks like a lens report
but is not exactly one valid read counts as a lens that read nothing, so the
queue never takes a reviewed pull request for an unreviewed one. Looking like a
report means any of: `lens:`, `read at`, `safe to merge`, a `## ... lens`
heading near the top, or two `covered:`/`checks:` lines. A comment that is no
report says so on its first line, `not a lens: ...`. That clears it unless one of
its lines is a lens header or a `lens:` field with a sha.

A comment that looks like a report but is not one valid read is a lens of its
own, keyed by that comment. Posting the report again the right way adds a new
read and leaves the old comment blocking, and hiding it is not enough either.
The ways out: delete it, edit its first line to `not a lens: ...` with no lens
header line left in it, or post an override for the head.

## The queue

```
pr_queue.py --repo OWNER/NAME --config FILE --state-file ABS --cache ABS [--lock-file ABS] [--dry-run]
```

One run looks at the open, non-draft pull requests with auto-merge on, oldest
first, and acts on the first one it can:

- up to date with its base: it holds the queue while its required checks run;
- behind its base: auto-merge off, `gh pr update-branch`, and auto-merge back
  on for the new head only if that head is exactly the change the review read
  merged with the base. Anything else leaves it off with a comment that names
  the way out.

A pull request in a merge state the queue neither updates nor waits for (a
conflict, `DIRTY`) is passed over, and the run says so. `UNKNOWN` (GitHub is
still computing it, often for a few seconds after a push to the base) is listed
again every `poll_seconds`, for at most `wait_seconds`, before it is passed
over, so a younger pull request is not updated ahead of it.

An armed pull request that cannot merge does not hold the queue. It is told
why on the pull request, once per head, check and reason, and passed over:

- a required check whose newest run failed (`FAILURE`, `ERROR`, `TIMED_OUT`,
  or `CANCELLED` after a red with nothing green since), behind or not. The
  newest run of the name counts, whichever workflow ran it, as the ruleset
  reads it. Its author fixes it; nothing in the queue re-runs it.
- up to date, and a required check's newest run ended without passing and
  without a red (`CANCELLED`, `STARTUP_FAILURE`, `STALE`, ...), and no
  workflow runs on the head any more (a newer run may still be queued): nothing
  re-runs it by itself. The comment says to re-run the CI run or push.
- up to date, and a required check never reported although no workflow runs
  on the head any more, also when no check at all reported. The comment says
  to push or merge the base: a re-run repeats the jobs of the workflow files
  the head was built with, so it cannot add a check those files do not have
  (one added or renamed since).

A head with no check and no workflow run at all still holds the queue: nothing
tells "not started yet" from "never starts" (every workflow filtered out by
its paths) without a clock, and the queue keeps no state for one.

Only the newest run of a check counts, and a check not in `required_checks` is
ignored. Only the queue's own comment (posted with its token) counts as told:
a copy of it by anyone else does not silence it. A pull request whose head
moved between the list and the comment is told by the next run, on its new
head. When GitHub cannot say which workflows still run on the head, the pull
request is not called stuck: it holds the queue, and the run says why.

`gh` must be logged in for the repository (`GH_TOKEN`). Git fetches into the
queue's own bare repository (`--cache`) and gets its login from
`gh auth git-credential` only, never from a helper the machine configured.

The token is in git's environment and `STATE` decides what is re-armed, so run
the queue on a runner (or runner group) that runs no `pull_request` job and no
other repository's job, as a user of its own. The `--state-file` and `--cache`
folders belong to that user alone. git runs with no hooks, no fsmonitor and no
replace refs, and a cache that is not that user's alone (mode `0700`), has a
config the queue did not write, or holds a file that changes what its objects
say or how a merge reads them (`info/grafts`, `shallow`,
`objects/info/alternates`, `objects/info/http-alternates` or
`info/attributes`) is rebuilt, never used. A job of the same user
can still change the cache between that check and git's next read: only the
runner rule above stops that.

| Exit | Meaning                                                                                                     |
| ---- | ----------------------------------------------------------------------------------------------------------- |
| 0    | Done, or nothing to do (also: another run holds the lock).                                                  |
| 1    | A step failed; the next run tries again (a plan in `STATE` first).                                          |
| 2    | Refused to start; touched nothing. Every reason is printed at once.                                         |
| 3    | A pull request may have auto-merge off and no comment says so yet. `STATE` stays; the next run finishes it. |

It refuses to start on a GitHub-hosted runner (its disk, and with it the plan,
goes with the job), with a `--repo` that is not `owner/name`, a relative path,
a path inside the job's workspace (the next checkout empties it), a config
error, or a git older than 2.38 (`merge-tree --write-tree`).

`--state-file` (`STATE`) holds the plan of the run that turned auto-merge off:
`{"repo", "pr", "old", "base", "read"}` (and `"told"` once the pull request was
told the run could not finish yet). It is written to disk (mode `0600`) before
the disarm and removed when the pull request is re-armed or told why not. The
next run finishes a plan first, against the review and the base as GitHub shows
them then (a lens posted since counts; an edited one reads nothing), and
touches no other pull request until it has. A plan
whose `repo` is another repository ends the run with exit 1 and stays as it
was: give each queue its own `--state-file`. A plan without `repo` (the source
queue's format) is this repository's. An unreadable plan, or one another user
could have written (not this user's, or writable by its group or others), is
kept as `STATE.bad`, the run fails, and every later run warns while that file
exists. A plan does not say who turned auto-merge off: a pull request a person
disarmed after the stop is re-armed too, if its head holds the change read.

The plan is on disk, and that is why the runner rule above exists. It could
live in a comment of the queue's own login instead (trusted by
`viewerDidAuthor`, as the told notices are): the next run reads the review and
the base from GitHub anyway, and a failed write before the disarm would only
mean no disarm. That stays open for a queue that moves several pull requests at
once, and for GitHub-hosted runners.

`--lock-file` (default: `STATE.lock`) keeps two runs from overlapping.
`--dry-run` takes no lock and writes nothing: no `STATE`, no cache, no comment.

## The base was red

A `pull_request` run tests GitHub's merge of the head into the base as the
base was when the run started. When that base commit failed the same check,
the red says nothing about the pull request, and nothing runs the check again
on a newer base. With `update_when_base_was_red`, a behind pull request with
red required checks is therefore updated when, for every red required check:

- the job of the check's newest run names the base commit it tested (the
  `tested-base` action's note);
- that base commit has a real failure of the same check (`FAILURE` or
  `ERROR`: a cancel, a time-out, a skip or no result says nothing about the
  base's code); and
- the base branch's tip has the check green now.

When the base tip is not green yet, the pull request is told once, per head
and check, that it failed as on its base and that the queue updates it by
itself once the base is green. The notice names the first check still red on
the base; when the base fixes that one and another is still red there, a new
notice names that one. A red of the pull request's own beside a base red is
named as its own ("fix the cause"). It does not hold the queue meanwhile. When
anything is unknown (no note among the job's first 100 annotations, a failed read,
more than 100 checks on a commit), the red counts as the pull request's own, as without
the rule. There is no state and no loop: after the update the new runs test a
green base, so a red then is the pull request's own. A re-run of the old merge
names the old red base again and is updated, which it needs.

The base branch's pushes must run the required checks under the same names,
or every base commit has "no result" and no red is ever the base's. The rule
reads the tested base from the job, never from a clock: a job that starts late
or a re-run of an old merge still tests the base its merge commit holds.

The note is an annotation on the job, titled `pr_queue tested base`, whose
message is the merge commit's first parent. `tested-base/` writes it only on a
`pull_request` run whose checkout is exactly the run's merge commit (`HEAD` is
`GITHUB_SHA`, with two parents, the second the event's pull request head).
Otherwise it writes a warning and no note. It never fails the job, and a
depth-1 checkout is enough. The pull request's own code runs in that job and
could write such a note too. A forged note can only get a red pull request
updated, or told that its base is the reason; it never re-arms one, because
the re-arm still needs exactly the change the lenses read.

## Use it

The queue needs:

- a self-hosted runner whose disk outlives the job, with git 2.38+, `gh` and
  `python3`: the plan in `--state-file` and the cache must be there for the
  next run. That runner (or runner group) runs no `pull_request` job and no
  other repository's job, as a user of its own, and the state and cache
  folders are that user's alone: the token is in git's environment there, and
  the plan decides what is re-armed ("The queue");
- a required `head_check` that fails a head no lens read. The queue only
  touches pull requests that are behind; without that check, a push to an
  armed pull request that is already up to date merges unread;
- a run after each push to the base branch and on a timer, so a pull request
  armed while the base is quiet is still picked up;
- a token that can update branches, comment and merge (a fine-grained personal
  access token or a GitHub App token with contents, pull requests and issues
  write). Not the run's `GITHUB_TOKEN`: a branch update pushed with it starts
  no workflow, so the updated pull request would never get its checks. The
  action refuses it.

### The reusable workflow

```yaml
name: PR queue
on:
  push:
    branches: [main]
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch:
permissions: {}
jobs:
  queue:
    permissions:
      contents: read
    uses: OWNER/REPO/.github/workflows/fork-pr-queue.yml@<40-hex sha>
    with:
      runs-on: '["self-hosted", "queue"]'
      config: .github/pr-queue.json
      state-file: /srv/pr-queue/plan.json
      cache: /srv/pr-queue/cache.git
    secrets:
      token: ${{ secrets.PR_QUEUE_TOKEN }}
```

It runs only on `push`, `schedule` and `workflow_dispatch`, so the code that
merges pull requests never runs for a pull request. It checks out only the
config file from the caller, and the queue from the workflow's own repository
and commit (`job.workflow_repository`, `job.workflow_sha`, runner 2.334.0 or
newer): the queue that runs is the one the caller pinned. `<40-hex sha>` here and
below is a release commit (`@<sha> # ci/vN`), never a commit of main: see
[`fork-ci/README.md`](../../README.md#pinning). One run per
repository at a time, never cancelled half-way. Inputs: `runs-on`, `config`,
`state-file`, `cache`, `lock-file` and `dry-run`, as in "The queue".

### The actions

A project that writes its own job can use the action directly, after a
checkout of its config:

```yaml
- uses: OWNER/REPO/fork-ci/actions/pr-queue@<40-hex sha>
  with:
    config: .github/pr-queue.json
    state-file: /srv/pr-queue/plan.json
    cache: /srv/pr-queue/cache.git
    token: ${{ secrets.PR_QUEUE_TOKEN }}
```

With `update_when_base_was_red`, the job of every required check runs
`tested-base` right after its checkout of the merge commit (`path` is the
checkout's path, default `.`):

```yaml
- uses: actions/checkout@<sha>
- uses: OWNER/REPO/fork-ci/actions/pr-queue/tested-base@<40-hex sha>
```

## Where the rules come from

`lens.py` is a port of a production queue's review rules, behaviour for
behaviour. `testdata/lens-corpus.jsonl` holds every call that project's lens
tests made to its copy, with that copy's answer; `lens.test.py` replays all of
them against `lens.py`. Logins and the project name in the corpus were
replaced, and the replaced corpus was checked against the source copy before it
was committed. A rule change here must keep the corpus green or say in its pull
request why a recorded answer changes.

The port differs on purpose in four ways, and the replay checks each recorded
answer that changes for its reason. The source's repository is private, so its
comments carry no author association: the replay gives them `OWNER` and
unedited. A read shorter than 40 hex, and a lens comment edited after posting,
read nothing here (54 recorded answers). The source's `full_sha`, which matched
a short read to the pull request's commits, is gone. A hidden (minimized) lens
comment reads nothing: the source never read `isMinimized`, and no recorded
call has it, so no recorded answer changes for it.

`pr_queue.py` is a port of the same project's queue, and `pr_queue.test.py`
holds that project's queue tests with the port's names. What the port changed:
everything project-specific comes from the config; `STATE` names its
repository; exit 3 (was 2) means "may be off and silent", so that 2 can mean
"refused"; the step that adopted an older queue's state file is gone; and the
queue's own advice in a comment keeps its backticks and `<sha>` placeholder
(`lens.no_lens_text(keep_markdown=True)`), which the source dropped. For a
required check that never reported, the comment says to push or merge the
base; the source said to re-run, which cannot add a check the head's workflow
files do not have. With two red checks the comment names the first by name
(the source named whichever the check list gave first). A red check is the
newest run of its name whichever workflow ran it, and a red that a green re-run
and then a cancelled run followed is not red (the source took the newest run
per workflow, and counted any red before a cancel). An `UNKNOWN` merge state is
listed again (the source passed it over at once), and a cancelled check waits
while a workflow still runs on the head (the source called it stuck at once).

The "base was red" rule is a port of a change proposed to the source queue,
with its tests. What the port changed: the rule is off unless the config turns
it on; the note is written by a general action (`tested-base/`) that also
checks the checkout is the run's merge commit, where the source had a step in
the project's own CI; a job's annotations are read 100 to a page (the source
read GitHub's default page of 30) and a commit with more than 100 checks is
unknown (the source read the first 100 as all); and red checks are looked at
in name order, so a run names the same reason each time.
