# pr-queue

A merge queue for a repository whose branch ruleset requires pull requests to be
up to date with their base branch. After each push to the base branch it brings
exactly one waiting pull request up to date, and it turns auto-merge back on
only for a head that holds exactly the change the review read.

## Config

One JSON file in the project's repository, UTF-8. Unknown keys, a key written twice, a
missing required key, an empty `required_checks` or a value of the wrong type is
an error: the queue then exits non-zero and touches no pull request.

The queue reads the config with git, as the commit checked out at its path
(HEAD) holds it, never the file on disk: a checkout writes that file through
`.gitattributes` (an encoding, `ident`), which a pull request can change
without touching the config itself. So the path must be inside a git checkout,
and the config must be committed there as a regular file: an edit that is not
committed is not read, and a symlink, a submodule entry or a folder is refused.
The read fetches nothing: in a partial clone (a sparse checkout) the config's
blob must be checked out, so the sparse-checkout pattern has to match its name.
A name with `[ ] * ?`, a leading `!` or `#`, a trailing space or a backslash
does not match itself as a pattern, and the queue then refuses to start.

```json
{
  "required_checks": ["build", "test", "review-check"],
  "merge_method": "squash",
  "bot_logins": ["coderabbitai", "github-actions", "copilot"],
  "lens_author_associations": ["OWNER", "COLLABORATOR"],
  "override": { "prefix": "tool ready --head", "author_association": "OWNER" },
  "ready_command": "tool ready {pr}",
  "push_command": "tool push",
  "lens_format_doc": "docs/review.md, section Lens comments",
  "head_check": "review-check"
}
```

| Key                        | Required | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| -------------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `required_checks`          | yes      | Exactly the checks the branch ruleset requires. Not empty.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `merge_method`             | yes      | `merge`, `squash` or `rebase`: how auto-merge is turned back on.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `delete_branch`            | no       | Not used; accepted so that old configs stay valid. The queue never deletes a branch: when GitHub can merge at once, `gh pr merge --auto --delete-branch` merges and deletes the branch with the token, and GitHub then closes every open pull request stacked on it. Turn on the repository setting "Automatically delete head branches" instead: GitHub retargets those pull requests to the merged one's base before it deletes the branch. Each run reads that setting once (with the run's `GITHUB_TOKEN`) and prints a warning, never a failure, when it is off or cannot be read. |
| `bot_logins`               | yes      | Logins whose comments never count as a lens. May be empty. A login ending in `[bot]` or an author of type Bot is a bot too, but gh gives only the login, without `[bot]`: list every bot here.                                                                                                                                                                                                                                                                                                                                                                                          |
| `lens_author_associations` | yes      | Whose comments count as lens comments: GitHub's author association of the comment, from `OWNER`, `MEMBER`, `COLLABORATOR`. Everyone else's comments are ignored (Lens comments). `MEMBER` and `COLLABORATOR` do not mean write access: list only those whose holders may all vouch for a head.                                                                                                                                                                                                                                                                                          |
| `override`                 | yes      | The comment that vouches for a head when the lenses read older commits: `<prefix> <40-hex sha>: <why>`, posted with this author association (`OWNER`, `MEMBER` or `COLLABORATOR`, with the same caveat) and never edited.                                                                                                                                                                                                                                                                                                                                                               |
| `ready_command`            | yes      | The command people run to re-arm a pull request after a lens read; `{pr}` is the number. Only quoted in the queue's comments.                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `push_command`             | yes      | The command people run to push a fixed branch. Only quoted in comments.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `lens_format_doc`          | yes      | Where the project documents its lens comments. Only quoted in comments.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `head_check`               | yes      | The required check that fails a head no lens read; it must be one of `required_checks`. The queue acts only on pull requests that are behind: without this check, a push to an armed pull request that is already up to date merges unread.                                                                                                                                                                                                                                                                                                                                             |
| `wait_seconds`             | no       | How long to wait for GitHub to show the updated head, and how long to keep trying to turn auto-merge back on while GitHub says a required check of that head is still queued (default 60).                                                                                                                                                                                                                                                                                                                                                                                              |
| `poll_seconds`             | no       | How often to look meanwhile (default 5).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `list_limit`               | no       | How many open pull requests to read (default 50).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `update_when_base_was_red` | no       | `true` updates a behind pull request whose required checks failed only because the base commit they tested failed them too (section "The base was red"). Set it only when the job of every required check runs the `tested-base` action. Default `false`.                                                                                                                                                                                                                                                                                                                               |
| `head_check_app`           | no       | The id of the GitHub App that alone posts `head_check` (the head gate, `../head-check/README.md`, "The gate"). The queue then counts only that App's newest check run of that name and ignores a job or commit status of the same name from anyone else; it skips a pull request whose gate check it cannot read. Add it only after every queue that reads this config runs a release that knows it: an older release refuses an unknown key.                                                                                                                                           |
| `review`                   | no       | Which changed paths need which lens reads, and the round cap (section "Review rules"). Without it the queue and the gate behave as before.                                                                                                                                                                                                                                                                                                                                                                                                                                              |

## Review rules

The optional `review` block of the config says which changed paths need which
lens reads and caps the review rounds. The head gate (`../head-check`) judges
the required lenses by changed path with it (below, "How the gate judges"); the
round cap, the approach lens and the `findings` / `state_line` lines are not
enforced yet. `review.recommended.json` (next to this file) is a complete
example with made-up paths; a test parses it. Real paths, lens names and prompt
files belong in the project's own config.

Every key is required and an unknown key is an error, as in the rest of the
config. A wrong block makes the whole config invalid, naming every problem at
once.

| Key          | Meaning                                                                                                                                                                                                                       |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `version`    | `1`. Any other version is refused by name (a newer one: update the pinned release first).                                                                                                                                     |
| `mode`       | `shadow`: the gate keeps judging by its `required-lenses` input and only reports what the path rules would have said. `enforce`: the path rules decide.                                                                       |
| `rules`      | An ordered list of `{"name", "weight", "paths", "lenses"}`. A changed file belongs to the first rule with a matching glob. `weight` is `critical`, `normal` or `light`; `lenses` are names from `lenses`.                     |
| `lenses`     | `{"<lens name>": {"prompt": "<file>"}}`: every lens a rule or `rounds` names, with its prompt file (repo-relative, no `..`, no leading `/`, no glob characters).                                                              |
| `min_lenses` | `{"critical", "normal", "light"}`: the fewest different lenses a rule of that weight must list, 0-10, never falling with weight.                                                                                              |
| `rounds`     | `{"approach_at", "max", "approach_lens"}`: from round `approach_at` the approach lens is required too; after round `max` a person must write an override comment or close the pull request. `max` is not below `approach_at`. |
| `findings`   | `true` or `false`: lens comments carry `blocker:`, `major:` and `minor:` lines, and a blocker or major at the head fails the gate.                                                                                            |
| `state_line` | `true` or `false`: a required lens comment needs a `state:` line to count.                                                                                                                                                    |

The config is refused when:

- a rule lists fewer different lenses than `min_lenses` of its weight. "Different"
  is as the gate reads lens names (section "Lens comments"): case, blanks and a
  trailing round marker do not count, so `data r1` and `data r2` are one lens;
- two names in `lenses` are one lens to the gate in that way;
- a lens name names no lens (a `<placeholder>`, a bare round marker such as `r2`,
  nothing), or the gate would read it as another name in a lens header (Markdown
  such as `data *2*`, or a `reviewer:` / `read at` field), or a rule or `rounds.approach_lens` names a lens with no entry in
  `lenses` (names match exactly);
- a `critical` or `normal` rule has `"lenses": []` (only `light` may);
- no rule has a catch-all path (`**`, or any glob that matches every path such as
  `**/*` or `*/**`), the catch-all rule is `light`, or a rule comes after the
  catch-all rule (it could never match): a new folder must never fall to no rule
  or to no lens;
- there is no `critical` rule (the built-in protected paths below take its lenses);
- in `enforce`, a rule comes before another rule, some path matches a glob of
  both, and the first rule is lighter (`**/*.md` light above `src/**` critical:
  `src/notes.md` would belong to the light rule and need no lens) or of the same
  weight without one of the other rule's lenses (`**/*.py` critical `[a]` above
  `src/**/*` critical `[a, b]`: `src/x.py` would need no `b`). Put heavier rules
  first, and of one weight the rule with more lenses. Only the match-every globs
  of the catch-all rule (`**`) are exempt: a lighter rule above it is the normal
  case, but an explicit glob it also names (`["src/**", "**"]`) is compared like
  any other. In `shadow` the same order is a warning in the gate's summary row,
  not a refusal, and the warning says that enforce refuses it;
- a glob is not in the dialect below, or a rule name or glob is written twice.

**Globs.** Paths are repo-relative and use `/`. `*` is any characters inside one
path segment, `**` is any number of whole segments (it must be a segment of its
own: `src/**/*.py`, not `src/**.py`), `?` is one character inside a segment.
There are no braces, character classes, `!` negation or backslashes, and no
empty, `.` or `..` segments. Matching is case-sensitive, and `*`, `**` and `?`
also match names that start with `.` (`**` matches `.github/x`). The gate and T3 match with this dialect, so a glob
means the same thing on both sides.

**Built-in protected paths.** The config file itself, every lens prompt file and
`.github/workflows/**` always count as critical, whatever the rules say, with the
lenses of the first critical rule. So a pull request that weakens the rules needs
the critical lenses, and it is judged by the old rules: the gate reads the config
from the base tip only. The config file is named as git names a changed file
(one leading `./` aside; no empty, `.` or `..` segment): any other character is
part of its name, glob characters too (`queue[main].json`), as the protected
paths are compared exactly, never matched as globs.

**How the gate judges.** The gate fetches the base tip and the head by sha and
reads the changed paths with `git diff --name-status -z -M <merge base> <head>`:
three dots, so what the base changed since the pull request branched off is not
the pull request's, and no API page cap (a pull request of 5000 files is judged
whole). Then (`review_judge.py`, `review_glob.py`):

- every changed path goes to the first rule with a matching glob. A rename or a
  copy counts under its old and its new path (moving a file out of a critical
  folder is a critical change); a delete, a mode change and a type change count
  under their path;
- a built-in protected path also counts for the first critical rule;
- the heaviest weight hit decides; the required lenses are the lenses of every
  rule of that weight that was hit, in rule order, each once;
- an empty diff (the head adds nothing to its merge base) counts as the catch-all
  rule, never as "no lens needed";
- with more than one merge base (criss-cross merges), the paths changed against
  each one count together: more paths, never fewer.

`review.vectors.json` holds the shared cases (globs, rule sets, changed paths with
their status, and the expected weight and lenses); the Python tests read it, and
T3 tests its own matcher on the same file.

Notes for T3's matcher and its GitHub reader (no change in the gate):

- add a glob vector with a non-ASCII name (an emoji, which is two UTF-16 units):
  `?` is one character of the path, so a TypeScript matcher must count code
  points as Python does, never UTF-16 units;
- GitHub's changed-files API stops at 3,000 files and gives text, not git's
  bytes (no `-z`, and a name that is not UTF-8 is changed). A reader on that API
  must map its names to git's, and when the list is cut, judge the change by the
  heaviest weight of the rules (unknown never judges lighter).

**Shadow and enforce.** In `shadow` the gate judges as before, by its
`required-lenses` input, and its check summary adds a row "the path rules would
have said: <weight>, lenses <...>" with the rules and paths behind it. In
`enforce` the path rules' lenses are required, and every lens in the
`required-lenses` input is still required as well (the input can add lenses,
never remove them). Switching `shadow` to `enforce` takes effect on each pull
request at its next gate run (a push, a base change, or the backfill dispatch);
a check posted before stays as it was until then.

Fix every config warning before switching to `enforce`: the gate reads the
config from the base tip, so a base config that does not parse refuses every
head, the one that fixes it too, and the queue stops (an owner then has to push
the fix by hand). The gate therefore refuses a pull request whose merged config
(GitHub's merge commit, or `git merge-tree` of base and head in the gate's bare
mode) does not parse, is deleted, is no regular file (a folder, a symlink or a
submodule entry), gains a twin whose name differs from it only in case, gains
a file, symlink or submodule entry whose name differs from one of its folders
only in case (`.GitHub` next to `.github/`; a folder of that name is read on,
and passes while it holds no twin of the config), or is in `enforce` with a
rule order `enforce` refuses (the order checks above), and caps what it reads
of a config a pull request changes (32 KiB, 128 characters per glob, 2,048 in
all). Names compare as macOS compares them: fully case folded (`ſ` is `s`), and
Unicode NFC and NFD forms as one name. NTFS compares with its own upcase table
and may pair a few exotic names this keeps apart (`ı`, `İ`). That one check reads the
merged config; everything else, the rules that judge the head too, the base's.
It judges the merge with the base of its run: two pull requests that each pass
alone can still merge into a bad config unless the ruleset requires branches to
be up to date, which the queue needs anyway.

**The empty input.** The gate refuses an empty `required-lenses` input (a head
with no lens comment would pass), except when the config at the base tip has a
`review` block in `enforce`: then the path rules name the lenses. A name that
names no lens (`<LENS_NAME>`, `r4`) is refused in every mode.

**Fail closed.** No merge base (the base was force-pushed, or the histories are
unrelated), a failed `git diff`, a diff line the judge cannot read, a base or
head that cannot be fetched, or a `review` block that does not parse: the gate
posts a failure that says why, in `shadow` as in `enforce`. Never a pass, never
"no lenses needed", and never today's rule instead. A config without the
`review` key gives exactly the gate of before this block.

**Adding the block.** A release that does not know the `review` key refuses the
whole config (an unknown key), so: release first, then pin that release in every
caller of the queue and the gate, then add the key. Roll back in the reverse order:
remove the key, then the pin.

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

Only comments, reviews and inline review comments (see below) that GitHub
records as written by one of `lens_author_associations` count: anyone can
comment on a public repository, so everyone else's comments are neither reads
nor reports. The run log names
each lens-looking comment it ignored this way and its author association, so a
wrong `lens_author_associations` shows there. A lens comment edited after
posting, or hidden (minimized), reads nothing until that lens posts a new
comment: an edit can move `read at` to a head nobody read, and hiding old review
rounds must not make a reviewed pull request look unreviewed (the queue would
then vouch for whatever head it found). A lens a later round drops therefore
needs an override, or its old comment deleted. A hidden override counts
nothing.

For reviews (a lens or an override posted as a PR review), `gh pr view` does
not ask GitHub whether the review was edited or hidden, yet prints
`includesCreatedEdit: false` for every one (checked with gh 2.102 and
`GH_DEBUG=api`). The queue never trusts that: it reads each review's
`includesCreatedEdit`, `isMinimized` and author type with one GraphQL query of
its own, by pull request, 100 reviews a page, after the view. A review that
query does not return reads nothing (and is no override). If the query fails,
nothing about the pull request is decided: the queue leaves it as it is and the
next run tries again; the head check fails.

An inline review comment (written on a line of the diff, in the "Files
changed" tab) is a comment like any other: it can be a lens read, a comment
that looks like a report and reads nothing, or an override, by the same author,
edit and hidden rules. `gh pr view` lists none of them, so the same query reads
them, 100 a page for each review, with their own edited and hidden flags and
author type; every pull request is read this way, also one without a review in
`gh pr view`. A comment of a review that is not submitted yet (pending) does
not count: only its author can see it. Resolving the thread changes nothing: a
resolved comment still counts. Its ways out are the same as any comment's
(delete it, `not a lens: ...`, or an override).

The grammar is fixed: a project writes its lens reports this way, it does not
configure another way. It fails closed. A comment that looks like a lens report
but is not exactly one valid read counts as a lens that read nothing, so the
queue never takes a reviewed pull request for an unreviewed one. Looking like a
report means any of: `lens:`, `read at`, `safe to merge`, a `## ... lens`
heading near the top, or two `covered:`/`checks:` lines. A comment that is no
report says so on its first line, `not a lens: ...`. That clears it unless one of
its lines is a lens header, a lens field with a sha (`lens:`, `lens=`, `lens —`
or `lens –`, quoted or not), or a `read at` with a hex sha outside a `>` quote
(a fenced or indented block is no quote). To name a sha a lens read in such a
comment, quote that line with `>`.

A comment that looks like a report but is not one valid read is a lens of its
own, keyed by that comment. Posting the report again the right way adds a new
read and leaves the old comment blocking, and hiding it is not enough either.
The ways out: delete it, edit its first line to `not a lens: ...` with no lens
header line left in it and any `read at <sha>` quoted with `>`, or post an
override for the head.

These shapes were measured on this repository's 677 pull request comments by
people (November 2026): 25 are flagged, before and after the `not a lens:`
rule above. Of the 25, 7 are real lens reports with a broken header (one of
them NOT SAFE), caught by the `## ... lens` heading rule (6) or a hex
`read at` (1); 13 are fix-round comments caught by the same heading rule and 5
fix-round comments with a `lens:` or `read at` in their text, and
`not a lens:` is their way out. All 34 comments that say `safe to merge` are
valid lens reads (`Verdict: not safe to merge ...`), so the rules stay as they
are: each one that flags an ordinary comment also catches broken reports that
read the same way ([issue 76](https://github.com/scuttletoby/t3code/issues/76)).

## The queue

```
PR_QUEUE_READ_TOKEN=... PR_QUEUE_WRITE_TOKEN=... \
pr_queue.py --repo OWNER/NAME --config FILE --state-file ABS --cache ABS [--lock-file ABS] [--dry-run]
```

The action sets both tokens ("Tokens" below); a run without them is refused
(`--dry-run` needs only the read token).

Each run (a dry run too) first reads the repository setting "Automatically
delete head branches" (GraphQL `deleteBranchOnMerge`, with the read token: the
REST answer leaves the setting out for a token that cannot push). The queue
never deletes a branch itself (Config, `delete_branch`), so when the setting is
off it prints a `::warning::` that merged branches stay. A read that fails or
gives no clear answer is a warning too. Neither changes what the run does or
its exit code.

One run looks at the open, non-draft pull requests with auto-merge on, oldest
first, and acts on the first one it can:

- up to date with its base: it holds the queue while its required checks run;
- behind its base: auto-merge off, `gh pr update-branch`, and auto-merge back
  on for the new head only if that head is exactly the change the review read
  merged with the base. Anything else leaves it off with a comment that names
  the way out.

Right after the update GitHub can refuse to turn auto-merge back on only
because the new head's required checks have not started
(`Required status check "..." is queued`, or `in progress`, `expected`,
`pending`). That refusal alone is tried again for the same head, after
`poll_seconds`, then twice that, and so on, for at most `wait_seconds` in all.
Every try names the head that was checked, so a head pushed meanwhile is never
armed. Any other refusal, or a check still not started after that time, leaves
auto-merge off with the usual comment. A run stopped during the wait is
finished by the next run, the same way.

A pull request in a merge state the queue neither updates nor waits for is
passed over, and the run says so. `UNKNOWN` (GitHub is
still computing it, often for a few seconds after a push to the base) is listed
again every `poll_seconds`, for at most `wait_seconds`, before it is passed
over, so a younger pull request is not updated ahead of it.

An armed pull request that cannot merge does not hold the queue. It is told
why on the pull request, once per head, check and reason, and passed over:

- a conflict with its base (merge state `DIRTY`): GitHub cannot merge it and
  the queue cannot update it, and GitHub leaves auto-merge on without a word.
  The comment says to merge the base into it, resolve the conflict and push
  (`push_command`). The new head is then a change no lens read, so it needs a
  new read or an override like any push. A conflict is said before a red
  check of the same head: only a push ends either.
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

Tokens: the queue uses two and never gh's own login of the machine. Every
read (the pull request list with its checks, check runs and their
annotations, workflow runs, reviews, git fetch) uses the run's `GITHUB_TOKEN`
(`PR_QUEUE_READ_TOKEN`), read-only by the job's permissions ("Use it"). A
fine-grained personal access token cannot read check runs at all (GitHub
answers `Resource not accessible by personal access token`). The caller's
token (`PR_QUEUE_WRITE_TOKEN`) is used only for what needs it: the branch
update (one by `GITHUB_TOKEN` starts no workflow), auto-merge on (the merge
it makes must start the base branch's push workflows) and off, the queue's
comments, and the one read that asks whether the queue already posted a
notice (`viewerDidAuthor` means that token's own login). A read GitHub
refuses stops that step as any failed read does: nothing is decided on it,
and the run names the permissions the job needs. Git fetches into the
queue's own bare repository (`--cache`) and gets its login from
`gh auth git-credential` only, never from a helper the machine configured.

A token is in git's environment and `STATE` decides what is re-armed, so run
the queue on a runner (or runner group) that runs no `pull_request` job and no
other repository's job, as a user of its own. The `--state-file` and `--cache`
folders belong to that user alone. git runs with no hooks, no fsmonitor, no
replace refs and no attributes (`GIT_ATTR_SOURCE` is the empty tree, so no
`.gitattributes` of any commit, the cache's `HEAD` or `attr.tree` changes how
a merge reads a file), and a cache that is not that user's alone (mode
`0700`), has a config or a `HEAD` the queue did not write, has a branch at
`HEAD`, or holds a file that changes what its objects say or how a merge reads
them (`info/grafts`, `shallow`, `objects/info/alternates`,
`objects/info/http-alternates` or `info/attributes`) is rebuilt, never used.
git checks an object's hash on only some reads (never a tree or a blob
`merge-tree` reads), so the queue runs `git fsck` on the cache before its merge
check: an object stored under another object's name (a tree or a blob that
makes a conflict merge clean) is found there. In an update it runs in the
background right after the first fetch, while GitHub updates the branch, and
the queue waits for it before it fetches the new head; in a run that finishes
a stopped one it runs right after the fetch. Old formats that `git fetch`
takes (a commit with time zone `+99999`, a tree out of order, an odd
`.gitmodules`) are no problem (`FSCK_FORMAT_IDS`); an object that does not
hash to its name, a missing or unreadable one, or a corrupt pack is. A cache
`git fsck` finds broken, or one a fetch fails on for a reason that is not the
network or the login, is downloaded from scratch once and checked again; one
still broken after that is not merged in (STATE stays, and the PR comment says
the cache fails `git fsck`). If a later run's `git fsck` gives the same lines
as right after that download, the history GitHub serves is what fails: the
queue does not download it again, and a person must look. A fetch's automatic
`git gc` runs in the foreground, so no repack races the check. `git fsck`
reads every object on one core: about 30 seconds for 270,000 objects, at most
once per run (the queue makes at most one merge check per run). The cache is
made with SHA-1 objects and files refs whatever git's defaults
(`GIT_DEFAULT_HASH`, `GIT_DEFAULT_REF_FORMAT`; git 3.0 plans others); a git
that makes another format is refused, never written over.

That check ends before the merge check starts (in an update, minutes before),
and whoever can write the cache could change an object in between. So the
merge check never reads the cache. Right before it, the queue makes a
repository of its own (`CACHE.merge`, mode `0700`, new in every run) and
copies into it the objects the check reads: each one comes out of the cache
through one `git cat-file --batch`, the queue hashes it itself, and only an
object that hashes to its name is written. Every git call of the merge check
(`merge-base`, `rev-parse`, `merge-tree`) then runs in that copy, so what is
written to the cache after an object was copied cannot reach the merge.
Copied are every commit the head, the change read and the base reach, and for
each `merge-tree` the trees and blobs it can read: the root trees of the two
sides and of their merge bases, and below them everything that is not the
same entry in all of them (what no side changed is not read by a merge and is
left out; a submodule entry names a commit of another repository and is never
copied). On this fork (270,000 objects, 11,500 commits) that is about 2
seconds for the commits and a fraction of a second for a merge of 300 changed
files. Should git still say anything on stderr or stop there, the whole trees
of the merged commits are copied (about 10 seconds a tree here) and the merge
runs once more; a merge git calls clean while it reports an error is never
taken as clean, and two sides with more than 8 merge bases are not merged at
all (the head is then not the change read). An object that is missing or not
what its name says is handled as a failed `git fsck` is: the cache is
downloaded from scratch once and the merge check runs again in the same run;
if it fails again, nothing is merged in, STATE stays and the PR comment says
so; a download that fails the same way as the last one is not repeated. If
the copy cannot be made (no room on the disk) nothing is known either, and the
cache is kept. The copy is deleted when the check ends; what a killed run left
at `CACHE.merge` is deleted by the next check.

What the last download from scratch failed on is kept in `STATE.fsck-known`,
next to the plan and not in the cache, and it is read only as a regular file
of the queue's user that no one else can write. It can only stop a second
download of the same broken history, never let a merge through; a healthy
check deletes it. (Until this version it was `queue-fsck-known` inside the
cache. That file is no longer read.)

What is left for the runner rule above: a job of the same user can still
write the queue's ref or config in the cache between the fetch and the moment
the queue reads them, and can write into `CACHE.merge` during the seconds it
exists.

| Exit | Meaning                                                                                                     |
| ---- | ----------------------------------------------------------------------------------------------------------- |
| 0    | Done, or nothing to do (also: another run holds the lock).                                                  |
| 1    | A step failed; the next run tries again (a plan in `STATE` first).                                          |
| 2    | Refused to start; touched nothing. Every reason is printed at once.                                         |
| 3    | A pull request may have auto-merge off and no comment says so yet. `STATE` stays; the next run finishes it. |

It refuses to start on a GitHub-hosted runner (its disk, and with it the plan,
goes with the job), with a `--repo` that is not `owner/name`, a relative path,
a path inside the job's workspace (the next checkout empties it), a config
error, or a git older than 2.43 (`merge-tree --write-tree` that honours
`GIT_ATTR_SOURCE`).

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

Next to the plan the queue keeps `STATE.fsck-known` (what the last download
of the cache from scratch failed on, if it did), and next to the cache
`CACHE.merge` exists while a merge check runs. Both are the queue's own: give
each queue its own `--state-file` and `--cache`.

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

- a self-hosted runner whose disk outlives the job, with git 2.43+, `gh` and
  `python3`: the plan in `--state-file` and the cache must be there for the
  next run. That runner (or runner group) runs no `pull_request` job and no
  other repository's job, as a user of its own, and the state and cache
  folders are that user's alone: the token is in git's environment there, and
  the plan decides what is re-armed ("The queue");
- a required `head_check` that fails a head no lens read. The queue only
  touches pull requests that are behind; without that check, a push to an
  armed pull request that is already up to date merges unread. The
  [head-check](../head-check/README.md) action is that check, with the
  queue's own rules;
- a run after each push to the base branch and on a timer, so a pull request
  armed while the base is quiet is still picked up;
- a token that can update branches, comment and merge (a fine-grained personal
  access token or a GitHub App token with contents, pull requests and issues
  write, plus workflows write so a branch update can bring in the base's
  workflow file changes), used only for those writes. Not the run's `GITHUB_TOKEN`: a branch
  update pushed with it starts no workflow, so the updated pull request would
  never get its checks. The action refuses it;
- read permissions for the run's `GITHUB_TOKEN`, which does every read:
  `actions`, `checks`, `contents`, `issues`, `pull-requests` and `statuses`,
  all `read`. The reusable workflow's job asks for exactly these, and the
  caller's job must grant them: a reusable workflow gets no more than its
  caller gives, and GitHub refuses to start a called job that asks for more.

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
      actions: read
      checks: read
      contents: read
      issues: read
      pull-requests: read
      statuses: read
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
checkout of its config, in a job with the read permissions above:

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
checkout's path relative to the workspace, default `.`; an absolute path or a
`.git` that fork-ci git-clean refuses writes no note):

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

The port differs on purpose in five ways, and the replay checks each recorded
answer that changes for its reason. The source's repository is private, so its
comments carry no author association: the replay gives them `OWNER` and
unedited. A read shorter than 40 hex, and a lens comment edited after posting,
read nothing here (54 recorded answers). The source's `full_sha`, which matched
a short read to the pull request's commits, is gone. A hidden (minimized) lens
comment reads nothing: the source never read `isMinimized`, and no recorded
call has it, so no recorded answer changes for it. A comment whose first line
is `not a lens:` and that holds a hex `read at` outside a `>` quote is a lens
that read nothing (2 recorded answers; the source cleared it).

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
