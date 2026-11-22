# Lens comments: the format the gate reads

A lens posts its report as one pull request comment. The merge gate (`fork-ci/actions/pr-queue`) reads that comment
with fixed rules. A comment that carries the lens marker (below) is a lens report, and the gate fails closed on it: one
that is not exactly one valid read blocks the head. A comment without the marker that looks like a report is no lens
report and never reads. It blocks only when it holds an open finding (a `blocker:` or `major:` line that is not
`none`, NOT SAFE on its first two lines or a `verdict:` line, or `not safe to merge` on any line outside a fenced code
block): then it is most likely a real report with a broken header, so it holds the head whatever an older or newer
read of that lens says, until it is deleted, starts with `not a lens:`, or is edited so it holds no open finding (the
same people could delete it). One with no open finding never blocks, and the gate's summary names it
`not a lens report` with the reason.
This page shows the format with examples. The rules themselves are the gate's code and its README (sections
[Lens comments](../../fork-ci/actions/pr-queue/README.md#lens-comments) and
[Review rules](../../fork-ci/actions/pr-queue/README.md#review-rules)); the gate's test vectors
([review.vectors.json](../../fork-ci/actions/pr-queue/review.vectors.json)) pin how it judges paths and lens counts.

Every example below is checked: `tests/comments.test.py` feeds each one through the gate's own parser (`lens.py`,
`review_judge.py`) and fails when the gate reads it differently from what the line above it says. To check a comment
of your own before posting it: `python3 -I fork-kit/review/lib/review_kit.py check [--settings <file>] --head <sha>
comment.md` (`--settings` when the project's kit settings are not at `.github/review-kit.json`).

The examples use the override prefix `tool ready --head`. A project's prefix is `override.prefix` in its queue config.

## The lens marker

A comment carries the lens marker when one of its lines, read in NFKC (a full-width colon `：` is a colon) with
Markdown stripped (emphasis, links, tags, quote, list and heading marks), in a code block or quote too:

- starts with the lens field: `lens:` (also `lens=`, `lens —`, `lens –`), or
- names a read: `read at` and a hex sha (`read at: 0123abc`, `read-at 0123abc`), or
- has a lens field followed by a hex sha anywhere in the line (`the security lens — fixed in 0123abc`).

A real report has it on its first line, the header. The marker decides only whether a comment is a lens report; the
rest of this page decides whether that report is one valid read. A heading such as `## Lens A`, `safe to merge`, or
`covered:` and `checks:` lines are no marker: such a comment is `not a lens`, `no lens marker`, and a lens whose report
reads that way has posted nothing the gate counts (a required lens then still has no read). If it holds an open finding
it is `reads nothing`, `no lens marker` and blocks. Either way the fix is a NEW comment whose first line is
`lens: <name> · reviewer: <who> · read at: <sha>`: editing the marker into the old comment does not count (an edited
comment reads nothing).

## The shape

```
lens: <name> · reviewer: <who> · read at: <the full 40-character head sha>
covered: <what the lens read>
checks: <what it ran or tried>
state: <the answer to the state question>
blocker: none
major: none
minor: <a small thing>; <another>
```

- **Header: the first non-blank line**, nothing before it. `<name>` is the lens name from the config (`data-1`,
  `merge-path`). `read at:` takes the full 40-character sha of the head the lens read; a short sha reads nothing.
  The fields may be split by `·`, `,` or `—`; `Lens:`, `**Lens:**`, `## lens:` and `- lens:` work too.
- **Findings:** a `blocker:` line and a `major:` line, each `none` or the finding. Both labels must be there for the
  gate to see a findings section. Other forms the gate reads: `finding: major <text>` lines, table rows
  `| major | <text> |`, `[major] <text>`, a `findings: none` line. `minor:` and `nit:` never block.
- **Verdict line (optional):** `verdict: safe to merge` or `verdict: not safe to merge`. A `verdict:` line, or the
  first line under the header, that says NOT SAFE is a blocker whatever the labels say.
- **State line:** `state:` and the lens's answer to the state question ("What happens when this runs again, stops
  half-way, or meets existing, old or empty state?"). What counts as one is the gate's rule
  (`review_judge.has_state_line`); the gate parses the config's `state_line` setting but does not enforce it yet; the
  kit's `check` does, and so does every lens prompt the kit writes.
- **Round markers:** a re-read after a fix may add `r2`, `r3` to the lens name (`lens: data-1 r2`), or `re-check`,
  which names no round number. The gate counts rounds from the different changes lenses read and from the numbered
  markers, whichever is higher; a marker is not part of the lens's name (`data-1 r2` and `data-1 re-check` are lens
  `data-1`).
- **A recap of an earlier round** must not look like an open finding: `round 1, fixed: <what>`, never
  `Major 1: fixed` (that reads as an open major).
- **One lens per comment**, never edited after posting (an edited or hidden comment reads nothing: post a new one).
- Lines inside a fenced code block are not findings.

## Who and where

The gate's rules for which comments count, in short (gate README,
[Lens comments](../../fork-ci/actions/pr-queue/README.md#lens-comments) and
[Review rules](../../fork-ci/actions/pr-queue/README.md#review-rules)):

- **The name is the whole name**, a bracket part too: `correctness (a.py)` and `correctness (b.py)` are two lenses.
- **Where:** plain comments, reviews and inline review comments all count; a comment of a review not yet submitted
  (pending) never does.
- **Who:** only comments whose author association is in the queue config's `lens_author_associations`.
- **Which config:** every rule that judges the head is the base tip's queue config. The gate reads the pull
  request's merged config only to refuse one that does not parse (or is deleted, or gains a case twin).

## Verdicts

Each example says, on the line before it, what the gate reads, as words:

| Words                 | Meaning                                                                                                             |
| --------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `lens <key>`          | a lens header naming this lens (`lens_key`: lowercased, a trailing round marker dropped)                            |
| `round <n>`           | the header carries round marker `r<n>`                                                                              |
| `read`                | the header names a full sha: this is a read of that head                                                            |
| `short sha`, `no sha` | the header's sha is short or missing: no read                                                                       |
| `clean`               | a findings section with no blocker and no major                                                                     |
| `blocker`, `major`    | an open blocker or major: the gate holds the head until a new read on a fixed head, or a person                     |
| `no findings section` | no blocker and major labels: with `findings` on, the gate does not take the read                                    |
| `state`, `no state`   | a `state:` line with an answer, or none                                                                             |
| `reads nothing`       | not one valid read that the gate counts as a blocking problem: it carries the lens marker, or holds an open finding |
| `no lens marker`      | looks like a lens report but carries no lens marker: never a read; a block only with `reads nothing` (open finding) |
| `not a lens`          | not a lens report at all (a plain comment, or one that opts out with `not a lens:`): ignored                        |
| `override`            | an override comment for that sha (counts only from the configured author association, never edited and not hidden)  |

## Good reports

A clean report: findings section, no blocker or major, a state line. A minor never blocks.

The gate reads: `lens code, read, clean, state`

```text
lens: code · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
covered: the diff of src/queue.ts and its tests
checks: ran the unit tests; tried an empty queue and a queue file cut half-way
state: runs again: the same result; stopped half-way: the temp file is renamed only when complete; old file: read
blocker: none
major: none
minor: the error text names the wrong flag
```

The same with Markdown emphasis and `·`-free separators. The gate reads the labels through the emphasis.

The gate reads: `lens merge-path, read, clean, state`

```text
**Lens:** merge-path, reviewer: lens agent, read at: 0123456789abcdef0123456789abcdef01234567
**State:** a second run finds the queue entry and does nothing
**Blocker:** none
**Major:** none
```

A report with an explicit safe verdict.

The gate reads: `lens state, read, clean, state`

```text
lens: state · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
verdict: safe to merge
state: an old config without the new key is read with the default
findings: none
```

A quick check of a nit-only fix (SKILL.md, "Order", step 7). The `nit-only:` line is for the orchestrator: on `yes` it
still posts the override for the fix head, because the other lenses still name the old head. The gate reads the report
as any other, and counts it as a round (a read of a new head).

The gate reads: `lens quick-check, read, clean, state`

```text
lens: quick-check · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
covered: the fix diff since round 1 (tests and message text only)
nit-only: yes
state: no write step changed
blocker: none
major: none
```

The state answer only in `checks:` (an older shape): the gate takes the read, but the kit's `check` refuses it (no state
line). Write the answer on a `state:` line.

The gate reads: `lens correctness, read, clean, no state`

```text
lens: correctness · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
covered: src/queue.ts
checks: a second run finds the entry and does nothing; an empty queue file is read as empty
finding: minor src/queue.ts:12 the error text names the wrong flag
```

## Reports that block

A major finding. The head is held until this lens posts a new read on a fixed head (or a person decides).

The gate reads: `lens code, read, major, state`

```text
lens: code · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
covered: src/queue.ts
state: stopped half-way: the queue file is written in place, so a crash leaves it cut
blocker: none
major: the queue file is written in place, not through a temp file and a rename (src/queue.ts:88)
```

Findings as `finding:` lines.

The gate reads: `lens data-1, read, blocker, state`

```text
lens: data-1 · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
state: runs again: the migration adds the column a second time and fails
finding: blocker the migration is not safe to run twice (migrations/0042.sql:3)
finding: minor the comment names the old table
```

Findings as table rows.

The gate reads: `lens data-2, read, major, state`

```text
lens: data-2 · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
state: empty state: a new user has no row, and the read returns null
| kind | finding |
| ---- | ------- |
| blocker | none |
| major | a null row is read as zero coins (src/storage/wallet.ts:41) |
```

A NOT SAFE verdict is a blocker even when the labels say none.

The gate reads: `lens code, read, blocker, state`

```text
lens: code · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
verdict: not safe to merge
state: not checked
blocker: none
major: none
```

## Rounds

A re-read after a fix, round 2. The recap of round 1 is written so that it does not read as an open finding.

The gate reads: `lens data-1, round 2, read, clean, state`

```text
lens: data-1 r2 · reviewer: lens agent · read at: 89abcdef0123456789abcdef0123456789abcdef
covered: the fix diff since round 1
state: runs again: the migration checks for the column first, and a second run does nothing
round 1, fixed: the migration was not safe to run twice
blocker: none
major: none
```

The wrong recap: `Major 1: fixed` reads as an open major.

The gate reads: `lens data-1, round 2, read, major, state`

```text
lens: data-1 r2 · reviewer: lens agent · read at: 89abcdef0123456789abcdef0123456789abcdef
state: runs again: a second run does nothing
Major 1: fixed
blocker: none
major: none
```

## Comments that read nothing or are no report

A comment with the lens marker that is not one valid read blocks the head (`reads nothing`). One without the marker
is no report, however much it looks like one (`no lens marker`), and it blocks only when it holds an open finding.

No findings section: only a major label, no blocker label. The gate sees no section and does not take the read.

The gate reads: `lens code, read, no findings section, no state`

```text
lens: code · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
looks fine to me
major: none
```

A short sha: no read.

The gate reads: `lens code, short sha`

```text
lens: code · reviewer: lens agent · read at: 0123456
blocker: none
major: none
```

The header is not the first line. The comment carries the lens marker, so it is a blocking problem.

The gate reads: `reads nothing`

```text
Here is my review.
lens: code · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
blocker: none
major: none
```

Two lenses in one comment: post one comment per lens.

The gate reads: `reads nothing`

```text
lens: data-1 · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
blocker: none
major: none
lens: data-2 · reviewer: lens agent · read at: 0123456789abcdef0123456789abcdef01234567
blocker: none
major: none
```

A lens heading that names a read: `read at` and a sha are the marker, so this is a broken report, and it blocks.

The gate reads: `reads nothing`

```text
## Lens code
read at: 0123456789abcdef0123456789abcdef01234567
blocker: none
major: none
```

A report with no header, only report fields: no marker, so no lens report. It does not block, and it counts for
nothing: the lens has not reported.

The gate reads: `not a lens, no lens marker`

```text
covered: src/queue.ts
checks: ran the tests
blocker: none
major: none
```

A lens-like heading with no marker: no lens report.

The gate reads: `not a lens, no lens marker`

```text
## Lens A
blocker: none
major: none
```

A lens-like heading with no marker that holds an open finding: most likely a real report with a broken header, so it
blocks, whatever another read of that lens says. Delete it or start it with `not a lens:`, and post a new comment with
the header.

The gate reads: `reads nothing, no lens marker`

```text
## Lens A
blocker: a second run loses the saved rows
major: none
```

Without the marker the line under the heading is read as a title line, and `not safe to merge` anywhere outside a
fenced code block is NOT SAFE, so this blocks too.

The gate reads: `reads nothing, no lens marker`

```text
## Lens A, round 2

**NOT SAFE to merge**

covered: the retry path
checks: ran the tests
```

A note between rounds (for example from the orchestrator, after it checked a fix diff) opts out with `not a lens:`
on its first line. It must not hold a `read at` sha outside a `>` quote, so a sha goes on its own line.

The gate reads: `not a lens`

```text
not a lens: fix round 1 checked
head 0123456789abcdef0123456789abcdef01234567 fixes the round 1 major and its minors; one nit dropped, reason under Left
```

A plain comment.

The gate reads: `not a lens`

```text
Thanks, I will look at this tomorrow.
```

An override comment: `<prefix> <full sha>:` and what was read. It covers lens reads; it clears a blocker or major only
when its author is listed in the review block's `override_authors`.

The gate reads: `override`

```text
tool ready --head 0123456789abcdef0123456789abcdef01234567: fix diff checked, the round 1 minors are fixed in this head
```
