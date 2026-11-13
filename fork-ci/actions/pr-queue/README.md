# pr-queue

A merge queue for a repository whose branch ruleset requires pull requests to be
up to date with their base branch. After each push to the base branch it brings
exactly one waiting pull request up to date, and it turns auto-merge back on
only for a head that holds exactly the change the review read.

This folder is built in four stacked pull requests:

1. the review rules and the config (`lens.py`, `config.py`);
2. the queue itself (`pr_queue.py`, section "The queue"): this part;
3. armed pull requests that cannot merge (a red required check, or one that
   ended without passing or never reported) are told once and passed over;
4. the "red only because the base was red" rule, the reusable workflow
   `.github/workflows/fork-pr-queue.yml`, `action.yml` and the usage sections
   of this file.

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
conflict, `DIRTY`) is passed over, and the run says so.

`gh` must be logged in for the repository (`GH_TOKEN`). Git fetches into the
queue's own bare repository (`--cache`) and gets its login from
`gh auth git-credential` only, never from a helper the machine configured.

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
told the run could not finish yet). It is written to disk before the disarm
and removed when the pull request is re-armed or told why not. The next run
finishes a plan first, against the review as it is then (a lens posted or
edited since counts), and touches no other pull request until it has. A plan
whose `repo` is another repository ends the run with exit 1 and stays as it
was: give each queue its own `--state-file`. A plan without `repo` (the source
queue's format) is this repository's. An unreadable plan is kept as
`STATE.bad`, the run fails, and every later run warns while that file exists.

`--lock-file` (default: `STATE.lock`) keeps two runs from overlapping.
`--dry-run` takes no lock and writes nothing: no `STATE`, no cache, no comment.

## Where the rules come from

`lens.py` is a port of a production queue's review rules, behaviour for
behaviour. `testdata/lens-corpus.jsonl` holds every call that project's lens
tests made to its copy, with that copy's answer; `lens.test.py` replays all of
them against `lens.py`. Logins and the project name in the corpus were
replaced, and the replaced corpus was checked against the source copy before it
was committed. A rule change here must keep the corpus green or say in its pull
request why a recorded answer changes.

The port differs on purpose in three ways, and the replay checks each recorded
answer that changes for its reason. The source's repository is private, so its
comments carry no author association: the replay gives them `OWNER` and
unedited. A read shorter than 40 hex, and a lens comment edited after posting,
read nothing here (54 recorded answers). The source's `full_sha`, which matched
a short read to the pull request's commits, is gone.

`pr_queue.py` is a port of the same project's queue, and `pr_queue.test.py`
holds that project's queue tests with the port's names. What the port changed:
everything project-specific comes from the config; `STATE` names its
repository; exit 3 (was 2) means "may be off and silent", so that 2 can mean
"refused"; the step that adopted an older queue's state file is gone; and the
queue's own advice in a comment keeps its backticks and `<sha>` placeholder
(`lens.no_lens_text(keep_markdown=True)`), which the source dropped.
