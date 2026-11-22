---
name: review
description: Review a pull request with lenses picked by the changed paths (the repo's `review` rules), the state question every time, one or two independent lenses, at most two lens rounds, root-cause fixes and attacks that leave failing tests. Use before a non-trivial pull request merges, when asked to review code, or when picking review lenses for a change.
---

# Review

Goal: find bugs early and fix them at the root, so they cannot come back in a slightly different case. CI and lenses
check side by side; the merge gate lets a head merge only when both pass. A lens is one separate read-only agent.

This is the one general review skill. A project keeps only its facts, in two files, never in this text: the `review`
block of its queue config (which paths are critical, normal or light, and which lenses each needs; read by the gate)
and its kit settings `.github/review-kit.json` (who runs a lens, how many lens rounds, what happens to minors, the
state question). A project's own skill, if it has one, is a thin adapter: it points here and adds only its domain
lenses and commands. Adopting the kit: [REVIEW.md](REVIEW.md). The comment format the gate reads:
[COMMENTS.md](COMMENTS.md).

## 1. Tier: from the changed paths

The tier of a change is the gate's `weight`: each rule of the review block gives its paths a weight, and the heaviest
weight any changed path hits decides. The tiers, with their defaults (the gate's `min_lenses` in
[review.recommended.json](../../fork-ci/actions/pr-queue/review.recommended.json) and the kit's `runner`):

| Tier     | Typical paths                                                                       | Lenses (default)                                      |
| -------- | ----------------------------------------------------------------------------------- | ----------------------------------------------------- |
| critical | stored data, migrations, deploy, security, the merge path, anything that loses data | at least **2 independent** lenses, each Opus 5.5 high |
| normal   | other code                                                                          | at least **1** lens, Opus 5.5 high                    |
| light    | docs and other paths the rules mark light (never a protected path, below)           | **0**: CI only                                        |

**Always critical** (the gate's built-in protected paths, whatever rule they fall under): the queue config file, every
lens prompt file named in the review block's `lenses`, and `.github/workflows/**`. A comment-only change takes its
path's tier.

A project with its own tier names maps them onto these three in its rules (for example tiers 0-1 light, 2 normal, 3
critical). A project with **no CI** (no queue config) has no gate: every change is normal with one lens, by hand.

```
python3 -I fork-kit/review/lib/review_kit.py plan --root <checkout> [--settings <file>] --base origin/main
```

Run the kit from a fork checkout at the release the project's gate pins (README.md, "Which fork version"). A project
whose kit settings are not at `.github/review-kit.json` adds `--settings <its file>` to every `plan` and `check`.

The plan judges the change with the gate's own rules and prints the tier, the lenses (each with its prompt file and
its runner), why, the state question, the round limit and the minors rule. Do what it says; never pick fewer lenses
by hand. Unsure, or a path the rules do not cover: take the heavier tier. Two independent reads miss different bugs;
a second identical read of normal code adds almost nothing.

No queue config, or one without a `review` block: the plan says so and treats every change as normal with one lens
`review`. That is a plain fallback, never a guess at which paths are critical.

## 2. The state question, every time

Every lens prompt contains, word for word, the project's state question (default: _What happens when this runs
again, stops half-way, or meets existing, old or empty state?_), and every lens answers it on a `state:` line. In the
eval that shaped this kit, no model at any effort found the rerun and empty-history bugs without it.

The author asks it first. A change that saves, migrates, deploys or changes state carries a **failure table** in its
description: one row per write step, with what a crash right after it, a second run, and an old, empty or
half-written start state do, each with its test; and every skip or fallback the code has, with how it reports
itself (no quiet fallbacks). Every lens checks that table: a write step without a row, or a row without a test that
fails without the fix, is a finding (major on a critical or normal change).

## 3. Order

Each step is done by another agent than the author.

1. **Author** (not a lens): why this approach and the rejected ones, the root cause and where else the pattern occurs
   (with the search used), every new check shown red on the old code and green on the fix, the failure table (§2).
   One behaviour or a batch of small related fixes per pull request, about 400 changed code lines at most; bigger is
   a stack. A project may open critical and normal changes as drafts until the lenses are done.
2. **CI** starts with the push. Lenses do not wait for it. Red: fix the cause.
3. **Outside reviewers** (bots, other tools) are extra opinions, never required; the project lists its own.
4. **Lenses** get the diff, the files it calls into and the problem _as reported_, never the author's summary, an
   outside reviewer's list or another lens's report (they would anchor on it). **Batch per area:** a stack or a group
   of small pull requests in one area may get one lens pass of at most about 1500 changed lines (never mixing
   areas); the lens posts one comment per pull request, each read at that pull request's head.
5. **Orchestrator** merges the lens lists (and the outside reviewers'; an outside finding no lens confirmed gets one
   verifier).
6. **Verify:** another agent reproduces every blocker and major (a test or an exact trace). False positive: a
   one-line reason. Confirmed: a root-cause fix with a red-to-green test. All fixes of a round go up in **one push**.
7. **Rounds:** at most `lens_rounds` (default **2**): round 1 lenses, one fix round, round 2 lenses on the fix (only
   those whose area the fix touches, and every lens that reported a blocker or major: the gate holds the head until
   that lens reads a fixed head). Stop early when a round finds no blocker and no major.
   - **How the gate counts rounds:** the round is the number of different changes of the pull request the lenses read
     (a head that only merges the base in is the same change) or the highest `r2`, `r3` marker in a lens name,
     whichever is higher. From round `rounds.approach_at` of the review block (3 in review.recommended.json; `plan`
     prints the project's) the gate requires one more lens, `rounds.approach_lens`, on the same head as the others;
     past `rounds.max` it fails the head (in `enforce`; `shadow` only reports both).
   - **Which head passes:** the gate passes a head only when every lens's newest read names that head, or an override
     comment newer than a lens's read covers it (`<override prefix> <full sha>: <what was read, why no lens>`,
     COMMENTS.md; it counts only from the configured author association, the queue config's
     `override.author_association`, and only while it is never edited and not hidden). So after a
     fix the lenses of the last round still name the old head, and the fix head passes only when every lens reads it
     again (a new round) or the orchestrator posts the override for it (no new round).
   - **Nit-only fix:** a fix diff that touches only tests, docs, comments, message text or names (judged by the diff,
     not by the findings' severity) needs no new lens round: the orchestrator checks the fix diff and the lenses' proof
     tests itself and posts the override for the fix head. Before that it may ask for one quick check: one lens on the
     fix head whose comment has a line `nit-only: yes` or `nit-only: no`. The quick check only adds an independent
     read; **it does not replace the override**: it is one lens on a new head while the other lenses still name the
     old one. It is a lens read of a new head, so **the gate counts it as a round**. On `nit-only: yes` the
     orchestrator still posts the override for the fix head; on `no` the fix goes to the next round. When the quick
     check's round reaches `rounds.approach_at`, run the approach lens too: one more independent read-only agent with
     the prompt file the review block names for that lens, on the same head, posting its own report under that lens
     name. The override alone, with no quick check, adds no round.
   - An override covers lens reads; it clears a blocker or major, and waives a required approach lens, only when its
     author is in the review block's `override_authors` (empty by default). An orchestrator's override never replaces
     the approach lens.
   - A note between rounds that is no lens report starts with `not a lens:` (COMMENTS.md).
   - **No round past the limit.** A fix after the last lens round (`lens_rounds` used up) that is not nit-only, for
     example a minor of the last round fixed in code with `minors: same-round`, gets no new lens round: the
     orchestrator checks that fix diff itself and posts the override for the fix head. A blocker or major left over
     counts as stuck. A pull request still stuck gets one audit by the `audit_runner` ("good enough to merge, or what
     next"), posted as a `not a lens:` comment; then a person decides.
8. **Minors** (`minors`, default `same-round`): do what the plan's `minors:` line says; the rule is written once, in
   `MINORS` in [lib/review_kit.py](lib/review_kit.py). With the default, a minor or nit is fixed in the pull request
   or dropped with a reason under "Left", and becomes an issue (with the project's labels) only as a real bug outside
   the pull request's scope. Blockers and majors are not minors: they are fixed before merge or the pull request is
   stuck. A project that sets `issue` files the leftovers as **one** follow-up issue linked on the pull request.
9. **Ready:** the project's ready command (the queue config's `ready_command`) arms the head every lens read. Push
   nothing after it; a new head needs the lenses again.

## 4. Lenses

Each lens is a prompt file named in the review block. Every prompt holds the state question and the comment format.
Common lenses, picked by the rules; a project adds its own domain lenses (its typical bugs and attacks) in its
prompt files:

| Lens                | Looks for                                                                                                                    |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| correctness         | null, empty and bounds, error paths, the unhappy branch of every `if`, units; is this the root cause, where else?            |
| design / root cause | forms its own view from the problem as reported; the simpler or more general fix                                             |
| tests               | every requirement and fix has a test that fails without it; assertions that cannot fail; skipped checks                      |
| data and state      | **attacks**: double submit, two processes, disconnect mid-action, kill after each write, retry; each with a **failing test** |
| security            | input from outside, permissions, forged or out-of-order messages, secrets, injection                                         |
| concurrency         | races, ordering, shutdown                                                                                                    |
| performance         | hot paths and queries, with numbers at the expected load                                                                     |
| domain expert       | how such systems are built and broken elsewhere; says what is general knowledge and what it checked in this code             |
| visual              | only when people see the change: real screenshots                                                                            |

**Attacks leave failing tests.** A data or state lens that finds a way to lose, duplicate or lock data writes the
test that shows it (red on the head it read) and names it in the finding; the fix makes it green and keeps it. An
opinion without a test or exact trace is not a finding.

**Twin lenses** (the two independent lenses of a critical change, or a lens the person who decides asks to run twice)
have different names in the review block (`data-1`, `data-2`): the gate counts one name as one lens.

**Covered:** a lens lists every file and function it read in full; anything not listed counts as not reviewed.
Every changed file must be in some lens's `covered:` in its final state; a fix round lists the files it changes.

### Lens prompt

A lens with no prompt file of its own (the fallback lens `review`) uses this text, filled in:

> You are the `<lens>` lens, one read-only reviewer. Review the diff `<base>...<head>` and read the changed files in
> full and what they call into. Find bugs at their root: the unhappy branch of every `if`, error paths, empty and
> bounds, and where else the same pattern occurs. Answer the state question on a `state:` line: _<state question>_
> Every finding needs file:line and inputs or state leading to the wrong result. Post one comment in the format of
> §6, the header first. Check it with `review_kit.py check [--settings <file>] --head <sha>` before posting (a
> blocker or major exits 1 by design: post it as found, never softened); never edit a posted comment (post a new
> one).

## 5. Who runs what

Runners are kit settings, not rules: `runner` (default `claude-opus-5-5`, effort `high`) for every lens,
`lens_runners` for a lens that needs another (a project may give its critical twin lenses `xhigh`), `audit_runner`
(default `claude-fable-5-2`, effort `high`) for the stuck-PR audit and design options, and `forbidden_efforts`
(default `low` and `max`: the highest effort cost about 3x for no reliable gain). The audit model is never a lens, and
a cheaper model is never the only reviewer of important code (it missed a blocker in both its eval runs). The plan
prints the runner of each lens. A project changes them from eval numbers ([eval/](eval/README.md)): the cheapest
setting whose worst-case recall equals the best.

## 6. Posting

One plain pull request comment per lens (not an inline comment on a diff line, not inside a code block or quote),
in this shape:

```text
lens: <lens name> · reviewer: <who> · read at: <the full 40-character head sha>
covered: <every file and function read in full>
checks: <what was traced or tested, including the hardest case>
state: <the answer to the state question>
blocker: none
major: none
minor: <file:line and what goes wrong, or none>
```

A blocker or major replaces `none` with the finding: file:line, the inputs or state leading to the wrong result, and
the test that shows it. `finding: <blocker|major|minor> <text>` lines and `findings: none` are read too (COMMENTS.md),
but every report keeps the `state:` line. The sha is `git rev-parse HEAD` of the head read; a short sha reads nothing.
Templates in briefs write `<sha>`, never a real one. Check before posting:

```
python3 -I fork-kit/review/lib/review_kit.py check --root <checkout> [--settings <file>] --head <sha> comment.md
```

Exit 0 only for a read of that head with a findings section, no blocker or major, and a state line; otherwise exit 1,
and it prints how the gate reads it. A report with a real blocker or major exits 1 **by design**: post it when the
words read `read, blocker, state` or `read, major, state`. Never soften or drop a finding to get exit 0. Words
`not a lens, no lens marker` mean the gate would not see a report at all, and `reads nothing, no lens marker` mean it
blocks the head without reading it (COMMENTS.md, "The lens marker"): fix the header, never post it that way. A project may
also have a check of the posted comment (who wrote it, edits): run it right after posting. A comment is never edited
or hidden (an edited or hidden lens comment reads nothing): post a new one. A comment the gate reads as
`reads nothing` keeps blocking the head even after a new post: delete it, or edit its first line to start with
`not a lens:`, with no line below it holding `lens:` and no `read at` sha outside a `>` quote (COMMENTS.md, the
`not a lens:` example).
