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
| `notify_command`           | no       | A command the queue runs once for each new notice it posts on an armed pull request it passes over (a conflict, a red, cancelled or missing check), so that someone learns of it without GitHub notifications: an argv list (no shell), the command a bare name on `PATH` or an absolute path. Section "Notify". Unset: no command, as before.                                                                                                                                                                                                                                          |
| `notify_timeout_seconds`   | no       | How long `notify_command` may run before it is stopped (default 30, 1-300). Only with `notify_command`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |

## Review rules

The optional `review` block of the config says which changed paths need which
lens reads and caps the review rounds. The head gate (`../head-check`) judges
the required lenses by changed path with it (below, "How the gate judges"), and
the findings, the approach lens and the round cap from the lens comments
(below, "Findings and rounds"). The `state_line` key is parsed but not enforced
yet. `review.recommended.json` (next to this file) is a complete example with
made-up paths; a test parses it. Real paths, lens names and prompt files belong
in the project's own config (below, "Turning it on: release, pin, then
config").

Every key is required except `override_authors`, and an unknown key is an
error, as in the rest of the config. A wrong block makes the whole config invalid, naming every problem at
once.

| Key                | Meaning                                                                                                                                                                                                                       |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `version`          | `1`. Any other version is refused by name (a newer one: update the pinned release first).                                                                                                                                     |
| `mode`             | `shadow`: the gate keeps judging by its `required-lenses` input and only reports what the path rules would have said. `enforce`: the path rules decide.                                                                       |
| `rules`            | An ordered list of `{"name", "weight", "paths", "lenses"}`. A changed file belongs to the first rule with a matching glob. `weight` is `critical`, `normal` or `light`; `lenses` are names from `lenses`.                     |
| `lenses`           | `{"<lens name>": {"prompt": "<file>"}}`: every lens a rule or `rounds` names, with its prompt file (repo-relative, no `..`, no leading `/`, no glob characters).                                                              |
| `min_lenses`       | `{"critical", "normal", "light"}`: the fewest different lenses a rule of that weight must list, 0-10, never falling with weight.                                                                                              |
| `rounds`           | `{"approach_at", "max", "approach_lens"}`: from round `approach_at` the approach lens is required too; after round `max` a person must write an override comment or close the pull request. `max` is not below `approach_at`. |
| `findings`         | `true` or `false`: lens comments carry a findings section (`blocker:` and `major:` lines, or headings), and a blocker or major at the head fails the gate.                                                                    |
| `override_authors` | Optional, default `[]`: the GitHub logins whose override comment is a person's decision (clears a finding, passes the round cap). `[]`: no override is a person's, so none does that (issue #78).                             |
| `state_line`       | `true` or `false`: a required lens comment needs a `state:` line to count. Not enforced yet.                                                                                                                                  |

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
- the heaviest weight hit is the change's weight; the required lenses are the
  lenses of every rule that was hit, whatever its weight, in rule order, each
  once. Each path gets its own rule's read: an app change plus one workflow
  file needs the app rule's lens and the critical lenses, never the critical
  lenses alone (owner decision,
  [issue 82](https://github.com/scuttletoby/t3code/issues/82): one _extra_
  review). A protected path counts for its own rule too, so one under a
  normal catch-all also needs that rule's lens; put the protected paths in a
  critical rule of their own to keep them at the critical lenses;
- an empty diff (the head adds nothing to its merge base) counts as the catch-all
  rule, never as "no lens needed";
- with more than one merge base (criss-cross merges), the paths changed against
  each one count together: more paths, never fewer.

`review.vectors.json` holds the shared cases (globs, rule sets, changed paths with
their status, and the expected weight and lenses; lens report bodies and the
findings read in them; lens comment scans; review rounds; what the head check
reads of a pull request's lens posts and overrides at a head (`lens_reads`,
`review_rounds`' lenses that must post again, the verdict), which T3's
`lens_start` reads the same way; bodies each rule must read in linear time,
with a time bound; the gate's Unicode version and the code points it assigns,
and text whose code points T3's newer Unicode assigns otherwise, which T3
refuses rather than reads another way when a rule reads them). The Python tests
and T3's pure copy of these rules (`apps/server/src/fork/reviewRules/`) both replay
every vector, and each side fails on a section or field it does not run. A rule
change updates both sides and the vectors in one pull request.

How a vector is made: write its input (name, body or posts, config) by hand,
run the gate's own function on it (`review_judge.judge`, `read_findings`,
`judge_rounds`, `lens.Review.lens_scan` the way `review_judge.test.py`'s
`scan()` posts it, with `override_prefix`, or its `replay_reads()`: a post is a
comment, or a `review` or an `inline` review comment, with its own `when` and,
for a review, `flags` false when its flags were not read), and paste the gate's
answer as the
expected value. Check that the answer is the one the rule means: a vector
copied from a broken gate holds both sides to the broken answer. The Python
test checks each expected value against the gate, so a stale one fails there.
The file is formatted with `vp fmt`.

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

**Findings and rounds.** The gate also judges the review itself, from the lens
comments it reads anyway (section "Lens comments"; `review_judge.judge_rounds`).
Owner decisions: issue #99 (findings), issue #100 (rounds).

- **Findings** (`findings: true`): a lens report has a findings section in its
  body: a `blocker:` and a `major:` line (`blocker: none`, `major: none` when it
  found nothing), or headings `### Blocking` / `### Major` with the findings,
  or `None.`, under them. What counts as a finding (`review_judge.read_findings`,
  table in `review_judge.test.py`): a label `blocker`, `blocking` or `major`
  (plural, `Major findings`, `major 2`, `major (state)` too; bold, list, emoji
  and heading marks do not matter) followed by text on the same line
  (`major: x`, `**Blocking:** x`, `major — x`, `[major] x`, a table row
  `| major | x |` or a table column headed `major`), or, for a bare label, the
  first line under it (a heading or nothing under it is a finding too). Every
  label of a line counts (`blocker: none, major: x`). Only an explicit nothing
  is no finding (`none`, `None found`, `none (only minors)`, `(none)`,
  `none — two minors below`, `nothing`, `no`, `0`, `n/a`, `-`). A recap of an
  earlier round with a label (`Major 1: fixed`) is read as an open finding:
  write it without one (`round 1, fixed: x`). The format of one line per
  finding, `finding: <blocker|major|minor> ...`, is read too (with
  `findings: none` when there is none); only its minors pass. Before reading,
  the text is folded: NFKC, zero-width and other format characters removed,
  Cyrillic and Greek look-alike letters read as Latin, `0` for `o` in a label,
  HTML entities decoded (`ma&#106;or`) and backslash escapes dropped
  (`major\:`). Lines inside a fenced code block are never labels, so quote
  code and example findings in one; quotes and indented blocks are read (fail
  closed). A title line or `verdict:` line that says
  NOT SAFE is a blocker. `minor:` and `nit:` never block.
  A lens whose newest report has a blocker or major fails the head. A report
  without a findings section cannot be judged and fails that lens: it needs a
  blocker label and a major label (`blocker: none` and `major: none` too), or
  `findings: none`, or `finding:` lines. One of the two labels is not enough:
  `blocker: none` with the major written another way (`F1 (major): x`) would
  read as clean. A report that writes `major: none` and then a major in a form
  not read here contradicts itself, and is read by its `major: none`. Findings
  go in the report body: findings in inline review comments (on a line of the
  diff) are not read, so a lens posted as a PR review whose body has no
  findings section fails. The ways out: fix it, and the lens
  posts a new report on the fixed head without a blocker or major; or a person
  posts an override comment for the head (config `override`) newer than the
  report. A person is a login listed in `override_authors` whose override
  comment GitHub also records with the override association
  (`override.author_association`); an override by a listed login with another
  association does not count, and the check output warns about it. An override by any
  other author of the override association covers a lens's read as before, but
  clears no finding and passes no cap. While agents still post as the owner's
  login (issue #78) an override from it proves no person, so keep the list
  empty until they no longer do: then no override clears a finding, and a
  finding is fixed or the pull request closed.
- **Rounds:** a round is a lens read of a fixed change, not of a new sha. The
  gate counts the different own changes the counted lens headers read: for each
  read sha, `git diff -U0 --binary` from its merge base with the base to it,
  the hunk headers (line numbers, function names) and `index` lines dropped
  (`head_check.own_change`), so a base that edits other lines of a file the
  pull request changes moves nothing. A head that only merges the base in (a
  queue update, `gh pr update-branch`) or the same change rebased onto a newer
  base is the same change, so the same round; a merge that also changes the pull
  request's own diff is a new one. A short sha counts as well, and a sha the
  gate cannot fetch, or one with several merge bases, counts as a round of its
  own (unknown counts more, never fewer). A round marker in a lens name
  (`code r3`, also `**r3**`) counts as that round when it is higher. The count
  comes from the lens comments the gate reads now: hidden and edited ones count
  too, so hiding or editing old rounds does not lower it. A deleted comment is
  gone from GitHub, and only lens authors can delete one, so the round marker of
  a later header is what keeps it counted; counting from commits instead would
  count every push, the opposite of a round. From round `rounds.approach_at`
  the lens `rounds.approach_lens` is required as well. Round `rounds.max` is the
  last: a round past it fails the head with "no more rounds; a person decides":
  the pull request is closed, or a person's override comment for the head,
  newer than every lens comment, lets it through on its own (it also waives the
  approach lens). With `override_authors` empty no override does: the reason
  says so. At round `rounds.max` a blocker or major says the same.
- In `shadow` the summary adds a row "findings and rounds (shadow): they would
  have said: ..." and nothing fails. Lens comments that cannot be judged fail
  the head in both modes. Without the `review` key nothing of this runs.

**Workflow, queue and lens files are critical** (owner decision, issue #82).
The built-in protected paths (above) make every change to `.github/workflows/`,
to the config itself and to a lens prompt need the critical lenses: the extra
review. A project whose gate or queue code lives in its own repository (the
fork's `fork-ci/`) adds a critical rule for those paths.

**The empty input.** The gate refuses an empty `required-lenses` input (a head
with no lens comment would pass), except when the config at the base tip has a
`review` block in `enforce`: then the path rules name the lenses. A name that
names no lens (`<LENS_NAME>`, `r4`) is refused in every mode.

**Fail closed.** No merge base (the base was force-pushed, or the histories are
unrelated), a failed `git diff`, a diff line the judge cannot read, a base or
head that cannot be fetched, a `review` block that does not parse, or lens
comments that cannot be read (the rounds cannot be judged): the gate
posts a failure that says why, in `shadow` as in `enforce`. Never a pass, never
"no lenses needed", and never today's rule instead. A config without the
`review` key gives exactly the gate of before this block.

### Turning it on: release, pin, then config

A project opts in through its own config, never through the fork. The order
matters: a release that does not know a key refuses the whole config (an
unknown key), and a refused config stops the queue and makes the gate refuse
every head, the pull request that would fix it too. So: release first, then pin
that release in every caller, then the config. Each step after the release is a
pull request of its own on the default branch.

1. **Release.** The fork makes the releases
   ([fork-ci README, "How a release is made"](../../README.md#how-a-release-is-made));
   a project never makes one. The path rules (`review`, `shadow` and `enforce`)
   are in `ci/v15` and later. Findings, rounds and `override_authors` are in
   `ci/v21` and later
   ([RELEASE-NOTES.md](../../RELEASE-NOTES.md), #740). Every rule hit gives
   its lenses from the first release after `ci/v22` (#82); up to `ci/v22` only
   the lenses of the heaviest weight hit were required. So a caller of a
   config in `enforce` pins the first release after `ci/v22` or a later one:
   `ci/v21` and `ci/v22` accept the config but judge mixed changes the old,
   looser way (an app change plus one critical file passes on the critical
   lenses alone, and no lens reads the app code). Take the newest
   `ci/vN` and its release sha as in
   [Pinning](../../README.md#pinning).
2. **Pin.** In one pull request, move every `uses:` of fork-ci in the
   project to that sha, with the tag as the comment: the queue's caller
   (`fork-pr-queue.yml` or the `pr-queue` action), the gate's caller
   ([`gate-caller.example.yml`](../head-check/gate-caller.example.yml)) and
   any job that runs the `head-check` action. Move the `sha` of the pin
   config of every tool outside a workflow that runs the queue's code too
   ([`actions/pinned`](../pinned/README.md), fork-ci README,
   ["Outside a workflow"](../../README.md#outside-a-workflow)): on an older
   release it refuses the new key the same way. Read the release notes between
   the old and the new tag first. After it merges, dispatch the gate's
   backfill (head-check README, "After a change to the caller, a new pin").
   The config does not change yet, so the gate judges as before. A project
   without the gate sets it up first (head-check README, "The gate", setup
   steps). In a public repository the caller needs a repository Actions
   policy that allows `pull_request_target` for exactly its file: keep the
   file name the policy names.
3. **Config in `shadow`.** Add the `review` block to the queue config the
   gate reads (its `config` input) with `"mode": "shadow"`. Start from
   `review.recommended.json` and put in the real paths, lens names and prompt
   files. Leave `override_authors` out until a person, not an agent, posts
   overrides from a login of their own (issue #78). The lens prompts ask for
   the lens header and the findings section (sections "Lens comments" and
   "Findings and rounds"; the fork's own prompts, `.github/fork-lens-*.md`,
   are an example), so a later switch to `enforce` does not fail every pull
   request. Keep the caller's `required-lenses` as it is: in `shadow` it
   still decides. This pull request is judged by the old rules (the gate
   reads the config from the base tip), and its merged config must parse, or
   the gate refuses it.
4. **Read the results.** From now on every gate run adds rows to the
   summary of the gate App's check run on the head ("Details" on the pull
   request, or
   `gh api "repos/OWNER/REPO/commits/HEAD_SHA/check-runs?check_name=CHECK_NAME&filter=latest" --jq '.check_runs[0].output.summary'`):
   - `path rules (shadow)`:
     `the path rules would have said: <weight>, lenses <...> (...); the required-lenses input decides (mode shadow)`,
     and `; config warning: ...` for each problem `enforce` would refuse (the
     run log has it as a warning too);
   - `findings and rounds (shadow)`: `they would have said: round N of M`,
     then `, approach lens <name> required from round <N>` once the round
     reaches `rounds.approach_at`, then
     `; findings are not judged (findings: false)` with `findings: false`,
     then the blockers and majors still open, a lens report with no findings
     section or the round cap, or `; no blocker or major open`.

   Compare them with the real verdict of each pull request that merges. A
   difference means `enforce` would have failed a head that passed (a lens
   the input does not name, an open finding, a report without a findings
   section) or the rules are wrong: fix the config or the lens prompts. The
   rows never fail a head in `shadow`; a change the rules cannot judge (no
   merge base, a failed diff, a config that does not parse, lens comments
   that cannot be read) fails it in both modes.

5. **Enforce.** When the rows have matched the real verdicts over the
   number of merged pull requests the project picked, and no row shows a
   config warning, one pull request sets `"mode": "enforce"`. It changes the
   config, so ask for the critical lenses plus the lenses of every other
   rule its paths hit (a config under a normal catch-all also needs that
   rule's lens): the gate does not require them yet
   (the base is still in `shadow`, where only the input decides). Dispatch
   the backfill right after it merges, and arm no pull request before the
   backfill has run: each open pull request is judged by the new rules only at
   its next run, so until then the queue can merge one on a pass it got under
   `shadow`. Nothing checks that gap. Now the path rules' lenses are required,
   and the `required-lenses` input is required as well (it can add lenses,
   never remove them).

   A later pull request may empty the input (the path rules name the
   lenses), but only once the base's config is in `enforce`: an empty input
   fails every head under `shadow` or without the block (above, "The empty
   input"). Emptying it also drops a floor: while the input names a lens,
   every change needs at least that lens; once it is empty, a change that only
   rules with `"lenses": []` match needs no lens at all. In
   `review.recommended.json` that is the `docs` rule (`docs/**`, `**/*.md`),
   which also matches agent instruction files such as `AGENTS.md` and
   `CLAUDE.md`: an edit to them would merge unread. A project that wants a
   lens on every change keeps the input, or gives no rule zero lenses. At
   least, put a rule for the agent instruction files above the `docs` rule:

   ```json
   {
     "name": "agent-instructions",
     "weight": "normal",
     "paths": ["**/AGENTS.md", "**/CLAUDE.md"],
     "lenses": ["code"]
   }
   ```

   The fork's own config,
   [`.github/fork-pr-queue.json`](../../../.github/fork-pr-queue.json), is at
   this step (owner decisions in issues
   [82](https://github.com/scuttletoby/t3code/issues/82),
   [99](https://github.com/scuttletoby/t3code/issues/99) and
   [100](https://github.com/scuttletoby/t3code/issues/100)). Its
   `override_authors` is written out empty: no person while agents post from
   the owner's login
   ([issue 78](https://github.com/scuttletoby/t3code/issues/78)), and
   `ci/v20` and older refuse the key, so a caller pinned to one of them fails
   closed instead of enforcing the path rules without findings and rounds. A
   caller of this config pins the first release after `ci/v22` or a later
   one (step 1): `ci/v21` and `ci/v22` accept it but judge mixed changes the
   old, looser way. The fork's gate caller
   ([issue 81](https://github.com/scuttletoby/t3code/issues/81)) starts at
   that floor. Its critical
   rule names the workflow and gate-setting files under `.github` one by one
   instead of `.github/**`: the seam list and the test list, which most pull
   requests add a line to, stay normal, or plain app changes would need the
   gate lenses and no `code` lens. It also holds `fork-ci/**`,
   `fork-kit/review/**` (the lens format checker and lens skill: what a lens
   report must say) and `fork-kit/gh-apps/**` (the gate App's key and
   tokens), and `docs/fork/gate-trust.md` (the owner's paste commands that
   set up that key and those tokens); the rest of `docs/fork` stays normal.
   Tests in `review_judge.test.py` fail when a new file under
   `.github` or a new folder under `fork-kit` is not sorted. The fork runs no
   gate on its own pull requests yet (no caller, App or ruleset,
   [issue 81](https://github.com/scuttletoby/t3code/issues/81)), so the block
   decides nothing there until one exists.

   Until issue 78 is done, two of these rules are not yet a person's
   decision on the fork. The round cap: rounds count from the lens comments
   that still exist, and every lens author is the owner's login that agents
   post from, so an agent can delete old round comments (or open a new pull
   request) and start again at round 1. A push after the review: an override
   for the new head lets it pass on the old reads without a listed person
   (open findings still block). Only clearing a finding and passing the cap
   need a listed person, and none is listed.

6. **Roll back** in the reverse order: lens names back into an emptied
   input, then `mode` back to `shadow` (or the block out), and only then a
   pin back. Never pin a release that does not know a key the config still
   has (`ci/v14` or older for `review`, `ci/v20` or older for
   `override_authors`). The gate does not check this; it is a review rule.
   The pin-back pull request is judged by the current gate, which knows the
   key, so it can merge; after it, the old release refuses the config, every
   head fails, and only the owner step below gets out.

   The fork's own rollback to `shadow` is one pull request with three
   changes: the `"mode"` line of `.github/fork-pr-queue.json`, and the two
   tests that pin `enforce`, `ForkConfigRules.test_it_is_in_enforce` in
   `head_check.test.py` and
   `ForkConfig.test_it_parses_in_enforce_with_the_owners_numbers_and_no_person_yet`
   in `review_judge.test.py`. While the base is in `enforce`, that pull
   request is judged by the enforce rules: it changes the config and
   `fork-ci/**`, so it needs clean `merge-path` and `state` reports within
   the round cap. If the gate itself is broken so that every head fails, only
   the owner step below gets out.

**Stopped half-way, or run again.** Every step leaves a state that works or
fails closed:

- Pins moved, no config change yet: the gate judges exactly as before (no
  `review` key is the gate of before).
- The block added while one caller still pins an older release: a gate on
  `ci/v15` or later refuses the block pull request itself (the config it would
  merge does not parse), so this happens only when the old caller is the
  queue or a `ci/v14` gate, or the block was merged by hand. That caller
  refuses the whole config. The queue stops: the gate still passes the pull
  request that moves the queue's pin, so merge that one by hand. A `ci/v14`
  gate fails every head, the pull request that moves its pin too: the owner
  step below. That is why step 2 moves every caller.
- `enforce` merged, backfill not yet dispatched: a pass posted under
  `shadow` stays on its head until the next event on it, and the queue can
  merge on it. Dispatch the backfill and arm nothing before it has run; a
  second dispatch judges the same heads again and changes nothing else.
- The input emptied while the base is not in `enforce`: every head fails,
  the pull request that puts a lens name back too (the base's caller judges
  it, with the empty input). The owner step below.

**The owner step.** When every head fails, the pull request that would fix it
too, no pull request gets out; nobody waits for one. An owner pushes the fix
straight to the default branch, past the branch ruleset (as for a base config
that does not parse, "Shadow and enforce" above): the revert of the commit that
broke it (a lens name back into the input, the pin moved forward, or the block
taken out), nothing else. Then dispatch the backfill.

## Lens comments

A review is posted as lens comments: one comment per review angle, whose
first line is its header.

```
lens: correctness · reviewer: <who> · read at: <sha>
```

`Lens:`, `**Lens:**`, `## lens:` and `- lens:` work too; the separator may be
`·`, `,`, `—` or nothing, and `<sha>` is the full 40-hex commit sha. Each line
is read in NFKC first, as the findings rules read it: the full-width colon `：`
reads as `:` and full-width letters and digits as plain ones, so
`lens： code · reviewer： x · read at： <sha>` is a header (issue 989: it was no
header, no marker and no look-alike, so it silently counted for nothing). A shorter
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
configure another way. A comment is a lens report only when it carries the lens
marker: a line (Markdown stripped, in a code block or quote too) that starts
with the lens field (`lens:`, `lens=`, `lens —` or `lens –`), names a read
(`read at` and a hex sha), or has a lens field with a hex sha after it anywhere.
On a lens report the rules fail closed: one that is not exactly one valid read
counts as a lens that read nothing, so the queue never takes a reviewed pull
request for an unreviewed one. A comment without the marker is no lens report,
however much it looks like one (a `## ... lens` heading near the top,
`safe to merge`, `lens:` in mid-line, or two `covered:`/`checks:` lines): it
never reads. If it holds no open finding it does not block either: the run log
names it, and the head check's summary lists it as `not a lens report` (a
`WARN` row) with the reason (issue 865: such a heading, posted from the owner's
account, held pull requests with no one seeing why). A lens whose report has no
marker has therefore posted nothing: a required lens still has no read, and the
head check fails on that. If it holds an open finding (a `blocker:` or `major:`
line that is not `none`, or NOT SAFE, as the findings rules below read them,
with its first two lines read as a heading and its title line, and
`not safe to merge` on any line outside a fenced code block read as NOT SAFE) it
is most likely a real report with a broken header, so it blocks like a broken
lens report, keyed by that comment, whatever an older or newer read of that lens
says: an older clean read never wins over it. Its reason says to post a NEW
comment whose first line is `lens: <name> · reviewer: <who> · read at: <sha>`
(editing the marker into the old comment does not count: an edited comment
reads nothing) and to delete the old one or edit it to start with `not a lens`
and a colon. It also stops holding once edited so it holds no open finding: the
same people could delete it, and the gate keeps no history. Lenses check their
comment with the review kit's `check` before posting (fork-kit/review). A
comment that is no report but holds a marker says so on its first line,
`not a lens: ...`. That clears it unless one of
its lines is a lens header, a lens field with a sha (`lens:`, `lens=`, `lens —`
or `lens –`, quoted or not), or a `read at` with a hex sha outside a `>` quote
(a fenced or indented block is no quote). To name a sha a lens read in such a
comment, quote that line with `>`.

A quote does not help a lens field: a line like `the security lens — fixed in
<sha>` blocks for good, quoted or not, and `not a lens:` cannot clear it. In a
fix-round or note comment, put each sha on a line of its own, with no `lens`
and `:`, `=`, `—` or `–` before it on that line:

```
not a lens: fix round 2 for the security and state lenses
- security, major: fixed, with a test that fails on the old head.
- state, minor: left as is, because ...
Fixed in:
<sha>
```

A comment holds one lens: below a valid header, a line that starts with
`lens:`, `lens —`, `lens –` or `lens=` and names a sha is a second header, and
the comment reads nothing. (`LENS = '...'`, with a blank before the `=`, is no
second header: a report's test code assigns sample headers that way.) Lines in
code blocks count too, so a report quotes sample headers with placeholders
(`<sha>`), never a real-looking sha (seven or more hex digits).

Fenced blocks are read as GitHub renders them: only a line of the same
character, at least as long and with nothing after it, closes a fence
(` ```js ` inside an open fence is code), and a backtick line with
another backtick after it is inline code, not a fence.

A lens report (a comment with the marker) that is not one valid read, or a
comment without it that holds an open finding, is a lens of its own, keyed by
that comment. Posting the report again the right way adds a new
read and leaves the old comment blocking, and hiding it is not enough either.
The ways out: delete it, edit its first line to `not a lens: ...` with no lens
header line left in it and any `read at <sha>` quoted with `>`, or post an
override for the head. One without the marker also stops holding once it is
edited so it holds no open finding (the same people could delete it).

These shapes were measured on this repository's 677 pull request comments by
people (November 2026, before the lens marker rule): 25 are flagged, before and
after the `not a lens:` rule above. Of the 25, 7 are real lens reports with a broken header (one of
them NOT SAFE), caught by the `## ... lens` heading rule (6) or a hex
`read at` (1); 13 are fix-round comments caught by the same heading rule and 5
fix-round comments with a `lens:` or `read at` in their text, and
`not a lens:` is their way out. All 34 comments that say `safe to merge` are
valid lens reads (`Verdict: not safe to merge ...`), so the rules stay as they
are: each one that flags an ordinary comment also catches broken reports that
read the same way ([issue 76](https://github.com/scuttletoby/t3code/issues/76)).
Under the lens marker rule 10 of the 25 carry no marker: 9 hold no open finding
and are only named, and 1 (a real report with an open major) still blocks.

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

A person's own auto-merge switch during the update stands, within what
GitHub allows: it has no compare-and-set for auto-merge, so the queue acts on
its last read. What is guaranteed, and what is not:

- Turned off before the queue turns it off: the queue reads the pull request
  right before its own turning off, and one that is off by then (or closed)
  is left as it is, not updated, without a comment.
- Turned off after that read, at the moment of the queue's own call: GitHub
  does not document what it answers for turning off an auto-merge that is
  already off. If it answers with an error, the queue cannot tell whether it
  or a person turned auto-merge off: it leaves it off, does not update, and
  says so on the pull request (the same when its own call answers with an
  error, or the run stops right there). If it answers with success, no read
  can tell that from the queue's own turning off: the pull request is updated
  and armed again. Not guaranteed: that window is one API call long.
- Turned on again after the queue turned it off: right before
  `gh pr update-branch` the queue reads the pull request again. Armed again
  (or closed), it is not updated, and the plan stays, so the next run decides
  from what GitHub shows then: armed or closed, nothing to do; off (GitHub
  showed the queue's own turning off late), armed again for the change read.
  Not guaranteed: a person who arms it in the moment between that read and
  the update is updated over.
- Turned on and off again after the queue turned it off, while the run waits
  for the new head or before the next run finishes a plan: the queue cannot
  see that and arms the change read over the last switch-off (see `STATE`,
  "A plan does not say who turned auto-merge off").

A pull request in a merge state the queue neither updates nor waits for is
passed over, and the run says so. `UNKNOWN` (GitHub is
still computing it, often for a few seconds after a push to the base) is listed
again every `poll_seconds`, for at most `wait_seconds`, before it is passed
over, so a younger pull request is not updated ahead of it.

An armed pull request that cannot merge does not hold the queue. It is told
why on the pull request, once per head, check and reason, and passed over (with
`notify_command`, that command runs for each such notice: section "Notify"):

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
the cache fails `git fsck`). One run downloads the cache from scratch at most
once, whether `git fsck`, a fetch or the merge check below asked for it: a
cache that fails again in that run is not downloaded a second time and not
merged in, STATE stays and the next run starts over. If a later run's `git fsck` gives the same lines
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
downloaded from scratch and the merge check runs again in the same run; if it
fails again, or if this run had already downloaded the cache (one download a
run, as above), nothing is merged in, STATE stays and the PR comment says so;
a download that fails the same way as the last one is not repeated. If the
copy cannot be made (no room on the disk) nothing is known either, and the
cache is kept. The same holds for a reader that hangs: a `git cat-file` that
owes one answer (the line that names an object, or the next 1 MiB of it) for
120 seconds is stopped, and nothing is merged in. The copy is deleted when the check ends; what a killed run left
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
exists. It can also make a git call wait without end (a FIFO where an object
file should be): only the merge check's object reader has the 120-second limit
above; `git fetch`, `git fsck` and the calls in the copy have no limit but the
job's `timeout-minutes`. Nothing is merged in while one waits, and STATE
stays.

The queue's own hash is Python's SHA-1, which does not detect a collision
attack as git's does (sha1dc). That is enough here: one of two colliding
objects would have to come from GitHub, which refuses them, and the cache's
`git fsck` hashes with git's own.

| Exit | Meaning                                                                                                     |
| ---- | ----------------------------------------------------------------------------------------------------------- |
| 0    | Done, or nothing to do (also: another run holds the lock).                                                  |
| 1    | A step failed; the next run tries again (a plan in `STATE` first).                                          |
| 2    | Refused to start; touched nothing. Every reason is printed at once.                                         |
| 3    | A pull request may have auto-merge off and no comment says so yet. `STATE` stays; the next run finishes it. |

It refuses to start on a GitHub-hosted runner (its disk, and with it the plan,
goes with the job), with a `--repo` that is not `owner/name`, a relative path,
a path inside the job's workspace (the next checkout empties it), a path that
is `/` or ends in `..`, a config error, or a git older than 2.43
(`merge-tree --write-tree` that honours `GIT_ATTR_SOURCE`). A `/` or `/.` at
the end of `--state-file`, `--cache` or `--lock-file` is dropped: the queue
names its other files (`CACHE.merge`, `CACHE.new`, `CACHE.old-*`, `STATE.lock`,
`STATE.fsck-known`) by adding to those paths, and they belong beside the cache
and the plan, not inside.

`--state-file` (`STATE`) holds the plan of the run that turned auto-merge off:
`{"repo", "pr", "old", "base", "read"}` (and `"told"` once the pull request was
told the run could not finish yet). It is written to disk (mode `0600`) before
the disarm with `"unsure": true`, written again without it once GitHub answered
the disarm with success, and removed when the pull request is re-armed or told
why not. A plan that is still unsure is never re-armed: the next run leaves the
pull request off with a comment. A plan also stays when the read right before
the update shows the pull request armed again or closed: the next run drops it
if GitHub still shows that, and re-arms the change read if it shows it off. The
next run finishes a plan first, against the review and the base as GitHub shows
them then (a lens posted since counts; an edited one reads nothing), and
touches no other pull request until it has. A plan
whose `repo` is another repository ends the run with exit 1 and stays as it
was: give each queue its own `--state-file`. A plan without `repo` (the source
queue's format) is this repository's. An unreadable plan, or one another user
could have written (not this user's, or writable by its group or others), is
kept as `STATE.bad`, the run fails, and every later run warns while that file
exists. A plan does not say who turned auto-merge off after the queue's own
disarm: a pull request a person armed and disarmed again since then (while
the run waited for the new head, after a stopped run, or after a run that saw
it armed again right before the update) is re-armed too, if its head holds the
change read.

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

## Notify

An armed pull request the queue passes over (section "The queue": a conflict, a
red check, a check that ended without passing or never reported) is told on the
pull request. That comment alone wakes nobody whose GitHub notifications are
off. With `notify_command` the queue also runs that command, once for each
notice it posts, with six arguments after the ones in the config:

```
<command> [config arguments...] <repo> <pr> <head> <kind> <reason> <link>
```

| Argument | Meaning                                                                                                                                                                                                                                                                                                                                                                                           |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `repo`   | `owner/name`.                                                                                                                                                                                                                                                                                                                                                                                     |
| `pr`     | The pull request number.                                                                                                                                                                                                                                                                                                                                                                          |
| `head`   | The full 40-hex head sha the notice is for.                                                                                                                                                                                                                                                                                                                                                       |
| `kind`   | `conflict` (merge state `DIRTY`), `red` (a required check failed), `stuck` (one ended without passing, or never reported) or `base-red` (it failed as on its base, and the queue updates it by itself once the base is green).                                                                                                                                                                    |
| `reason` | The notice's own words, such as `required check build is red`.                                                                                                                                                                                                                                                                                                                                    |
| `link`   | The page of the check's newest run, or the pull request's page when there is none (a conflict, a check that never reported) or it is not plainly a page of the repository on `https://github.com/` (a status can link any host): printable ASCII only, and no backslash, `.` or `..` path segment (also percent-encoded) or encoded `/` or `\` that a browser would follow to another repository. |

What the command does is the project's: wake the agent thread that armed the
pull request, send a message, write a file. The fork holds no such command;
a project names its own in its own config, for example
`"notify_command": ["/usr/local/bin/queue-notify", "--channel", "ci"]`.

- **Once per notice.** The command runs right after the queue posted the
  comment, and only then: the comment (the queue's own login, per head and
  reason) is the record that this head and reason were told. A later run, a
  run after a restart, or a run on another runner sees the comment and posts
  nothing, so the command does not run again. A new head that is still red or
  still conflicts is a new notice and runs it again; so does a new reason on the
  same head (a red after a cancelled check). A notice an older release posted
  counts too: adding the key wakes nobody for a pull request told before. A dry
  run posts nothing and runs nothing. The record is the comment itself: a
  queue comment someone deleted or edited, or a write token of another login
  (only the queue's own login's comments count), posts the notice again and so
  runs the command once more for every pull request passed over then. That is
  one burst, not a loop: the new comments are the record from then on.
- **A failed comment runs nothing.** The next run posts the comment and runs
  the command then.
- **Never in the queue's way.** A command that is missing, cannot start, exits
  non-zero or runs longer than `notify_timeout_seconds` is a `::warning::` in
  the run log, and the run goes on to the next pull request; the exit code of
  the run does not change, nor does a temp file the queue cannot make for its
  output. A command that runs too long is stopped with every process of its
  session (it starts a session of its own) and every process still linked to
  those by its parent, so a child in a process group or session of its own
  (coreutils `timeout`, `setsid`) goes too while its parent runs. That walk
  reads `/proc` (Linux); without it (another system, a sandbox) the command's
  own process group is killed, as `kill -9 -<pid>` would. A daemon whose
  parent already ended is beyond that; the runner's end-of-job cleanup stops
  it. After the command exits, a helper it left running in the background is
  not stopped: its output goes to a file, not a pipe, so it cannot hold the run
  either.
- **Five minutes per run in all.** The commands of one run take at most 300
  seconds together, so slow notices cannot reach the job's 20-minute limit and
  cut the run half-way: each gets `notify_timeout_seconds` or what is left.
  Once it is spent, the run posts no more notices for as long as
  `notify_command` is set (the log says so): the next run, with five fresh
  minutes, posts them and runs the command. So no notice is lost to the
  budget, and none runs twice.
- **Not tried again.** A command that failed, or a run stopped between the
  comment and the command, does not run it again later: the comment says the
  pull request was told, and the queue keeps no state of its own for this. The
  notice on the pull request still stands, and the run log names the failure.
- **No token in its environment, but not a sandbox.** The command gets the
  queue's environment without any variable whose name holds `TOKEN` (the
  queue's read and write tokens, `GH_TOKEN`, `ACTIONS_RUNTIME_TOKEN`, ...), no
  stdin, and the job's working folder. It still runs as the same user as the
  queue, so it can read the queue's own start-up environment (write token
  included) through `/proc`. The reusable workflow leaves no job token in the
  working folder or `RUNNER_TEMP`: right after its two checkouts, a step
  removes what `actions/checkout` saved (the `includeIf` entries and the
  `git-credentials-*.config` files they point at, and any
  `http.<url>.extraheader`) and fails unless none is left. It does not use
  `persist-credentials: false`, whose cleanup fails on a repository with
  gitlinks but no `.gitmodules`. A job of your own that runs the action should
  drop the token the same way. Trust the command like the
  queue itself: only a command from the project's own config, never one built
  from pull request text, and never one that hands what it can read to someone
  else. Its output (stdout and stderr, the first 4,000 characters) is shown in
  the run log, each line after `notify|` and with the runner's workflow
  commands stopped around it (`::stop-commands::` with a token the command
  cannot know), so it cannot write a workflow command in either form (`::` or
  `##[`). The commands are turned back on even when a line of the output cannot
  be printed (a stdout that is not UTF-8): the rest of it is left out, with a
  `::warning::`.
- **GitHub's text is an argument, never a shell line.** A check or base name
  can hold anything; it reaches the command as one argument.

Add the key only after every queue that reads the config runs a release that
knows it: an older release refuses an unknown key.

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

The port differs on purpose in six ways, and the replay checks each recorded
answer that changes for its reason. The source's repository is private, so its
comments carry no author association: the replay gives them `OWNER` and
unedited. A read shorter than 40 hex, and a lens comment edited after posting,
read nothing here (54 recorded answers). The source's `full_sha`, which matched
a short read to the pull request's commits, is gone. A hidden (minimized) lens
comment reads nothing: the source never read `isMinimized`, and no recorded
call has it, so no recorded answer changes for it. A comment whose first line
is `not a lens:` and that holds a hex `read at` outside a `>` quote is a lens
that read nothing (2 recorded answers; the source cleared it). A line is read in
NFKC (issue 989), so `read at：<sha>` with the full-width colon names its sha (2
recorded answers: the source read no sha; the one recorded sha is short, so it
still reads nothing).

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
