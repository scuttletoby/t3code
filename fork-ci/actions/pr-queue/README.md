# pr-queue

A merge queue for a repository whose branch ruleset requires pull requests to be
up to date with their base branch. After each push to the base branch it brings
exactly one waiting pull request up to date, and it turns auto-merge back on
only for a head that holds exactly the change the review read.

This folder is built in four stacked pull requests:

1. the review rules and the config (`lens.py`, `config.py`): this part;
2. the queue itself (`pr_queue.py`);
3. armed pull requests that cannot merge (a red required check, or one that
   ended without passing or never reported) are told once and passed over;
4. the "red only because the base was red" rule, the reusable workflow
   `.github/workflows/fork-pr-queue.yml`, `action.yml` and the usage sections
   of this file.

## Config

One JSON file in the project's repository. Unknown keys, a missing required
key, an empty `required_checks` or a value of the wrong type is an error: the
queue then exits non-zero and touches no pull request.

```json
{
  "required_checks": ["build", "test", "review-check"],
  "merge_method": "squash",
  "delete_branch": true,
  "bot_logins": ["coderabbitai", "github-actions", "copilot"],
  "override": { "prefix": "tool ready --head", "author_association": "OWNER" },
  "ready_command": "tool ready {pr}",
  "push_command": "tool push",
  "lens_format_doc": "docs/review.md, section Lens comments",
  "head_check": "review-check"
}
```

| Key               | Required | Meaning                                                                                                                                                                            |
| ----------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `required_checks` | yes      | Exactly the checks the branch ruleset requires. Not empty.                                                                                                                         |
| `merge_method`    | yes      | `merge`, `squash` or `rebase`: how auto-merge is turned back on.                                                                                                                   |
| `delete_branch`   | yes      | Whether auto-merge deletes the branch after merging.                                                                                                                               |
| `bot_logins`      | yes      | Logins whose comments never count as a lens. Any login ending in `[bot]` is a bot too. May be empty.                                                                               |
| `override`        | yes      | The comment that vouches for a head when the lenses read older commits: `<prefix> <40-hex sha>: <why>`, posted with this author association (`OWNER`, `MEMBER` or `COLLABORATOR`). |
| `ready_command`   | yes      | The command people run to re-arm a pull request after a lens read; `{pr}` is the number. Only quoted in the queue's comments.                                                      |
| `push_command`    | yes      | The command people run to push a fixed branch. Only quoted in comments.                                                                                                            |
| `lens_format_doc` | yes      | Where the project documents its lens comments. Only quoted in comments.                                                                                                            |
| `head_check`      | no       | The required check that checks lens reads on an up-to-date head, if the project has one; it must be one of `required_checks`. Only changes the advice in comments.                 |
| `wait_seconds`    | no       | How long to wait for GitHub to show the updated head (default 60).                                                                                                                 |
| `poll_seconds`    | no       | How often to look meanwhile (default 5).                                                                                                                                           |
| `list_limit`      | no       | How many open pull requests to read (default 50).                                                                                                                                  |

## Lens comments

A review is posted as lens comments: one comment per review angle, whose
first line is its header.

```
lens: correctness · reviewer: <who> · read at: <sha>
```

`Lens:`, `**Lens:**`, `## lens:` and `- lens:` work too; the separator may be
`·`, `,`, `—` or nothing, and `<sha>` is 7 to 40 hex digits of a commit of the
pull request. Each lens's newest comment counts. The reviewed head is the one
commit every lens's newest read names, or a newer override comment's sha
(config `override`). A pull request with no lens comments is not reviewed, and
the queue then vouches only for the head it found.

The grammar is fixed: a project writes its lens reports this way, it does not
configure another way. It fails closed. A comment that looks like a lens report
but is not exactly one valid read counts as a lens that read nothing, so the
queue never takes a reviewed pull request for an unreviewed one. Looking like a
report means any of: `lens:`, `read at`, `safe to merge`, a `## ... lens`
heading near the top, or two `covered:`/`checks:` lines. A comment that is no
report says so on its first line, `not a lens: ...`. That clears it unless one of
its lines is a lens header or a `lens:` field with a sha.

## Where the rules come from

`lens.py` is a port of a production queue's review rules, behaviour for
behaviour. `testdata/lens-corpus.jsonl` holds every call that project's lens
tests made to its copy, with that copy's answer; `lens.test.py` replays all of
them against `lens.py`. Logins and the project name in the corpus were
replaced, and the replaced corpus was checked against the source copy before it
was committed. A rule change here must keep the corpus green or say in its pull
request why a recorded answer changes.
