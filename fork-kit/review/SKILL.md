---
name: review
description: Review a pull request with lenses picked by the changed paths (the repo's `review` rules), the state question every time, one or two independent lenses, at most two lens rounds, root-cause fixes and attacks that leave failing tests. Use before a non-trivial pull request merges, when asked to review code, or when picking review lenses for a change.
---

# Review

Goal: find bugs early and fix them at the root, so they cannot come back in a slightly different case. CI and lenses
check side by side; the merge gate lets a head merge only when both pass. A lens is one separate read-only agent.

The project's facts are in two files, never in this text: the `review` block of its queue config (which paths are
critical, normal or light, and which lenses each needs; read by the gate) and its kit settings
`.github/review-kit.json` (who runs a lens, how many lens rounds, what happens to minors). Adopting the kit:
[REVIEW.md](REVIEW.md). The comment format the gate reads: [COMMENTS.md](COMMENTS.md).

## 1. Plan: which lenses, from the changed paths

```
python3 -I fork-kit/review/lib/review_kit.py plan --root <checkout> --base origin/main
```

It judges the change with the gate's own rules and prints the weight, the lenses (each with its prompt file and its
runner), why, the state question, the round limit and the minors rule. Do what it says; never pick fewer lenses by
hand. Unsure, or a path the rules do not cover: take the heavier weight.

- **critical:** every lens it names, **two independent lenses** at least (separate agents, no shared context, none
  sees another's report). Two independent reads miss different bugs; a second identical read of normal code adds
  almost nothing.
- **normal:** one lens.
- **light:** no lens; CI only.

No queue config, or one without a `review` block: the plan says so and treats every change as normal with one lens
`review`. That is a plain fallback, never a guess at which paths are critical.

## 2. The state question, every time

Every lens prompt contains, word for word, the project's state question (default: _What happens when this runs
again, stops half-way, or meets existing, old or empty state?_), and every lens answers it on a `state:` line. In the
eval that shaped this kit, no model at any effort found the rerun and empty-history bugs without it. The author asks
it first: a change that saves, migrates, deploys or retries lists each write step with what a crash right after it,
a second run and an old, empty or half-written start state do, each with a test.

## 3. Order

1. **Author** (not a lens): why this approach, the root cause and where else the pattern occurs (with the search
   used), every new check shown red on the old code and green on the fix.
2. **CI** starts with the push. Lenses do not wait for it.
3. **Lenses** get the diff, the files it calls into and the problem _as reported_, never the author's summary or
   another reviewer's list (they would anchor on it).
4. **Orchestrator** merges the lens lists.
5. **Verify:** another agent reproduces every blocker and major (a test or an exact trace). False positive: a
   one-line reason. Confirmed: a root-cause fix with a red-to-green test. All fixes of a round go up in one push.
6. **Rounds:** at most `lens_rounds` (default **2**): round 1 lenses, one fix round, round 2 lenses on the fix (only
   those whose area the fix touches, and every lens that reported a blocker or major: the gate holds the head until
   that lens reads a fixed head). Stop early when a round finds no blocker and no major.
   - The rest the orchestrator checks itself from the fix diff and the lenses' proof tests, then posts the override
     comment (`<override prefix> <full sha>: <what was read, why no lens>`, COMMENTS.md). An override covers lens
     reads; it clears a blocker or major only when its author is in the review block's `override_authors`.
   - A note between rounds that is no lens report starts with `not a lens:` (COMMENTS.md).
   - **No round past the limit.** A pull request still stuck (a blocker or major left) gets one audit by the
     `audit_runner` ("good enough to merge, or what next"), posted as a `not a lens:` comment; then a person decides.
7. **Minors** (`minors`, default `issue`): minors not fixed when the pull request stops go into **one** follow-up
   issue, linked on the pull request. With `same-round`, minors are fixed in the round that found them and any left
   at the last round are listed in the pull request description with a reason each.

## 4. Lenses

Each lens is a prompt file named in the review block. Every prompt holds the state question and the comment format.
Common lenses, picked by the rules:

| Lens                | Looks for                                                                                                                    |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| correctness         | null, empty and bounds, error paths, the unhappy branch of every `if`, units; is this the root cause?                        |
| design / root cause | forms its own view from the problem as reported; the simpler or more general fix                                             |
| tests               | every requirement and fix has a test that fails without it; assertions that cannot fail; skipped checks                      |
| data and state      | **attacks**: double submit, two processes, disconnect mid-action, kill after each write, retry; each with a **failing test** |
| security            | input from outside, permissions, secrets, injection                                                                          |
| concurrency         | races, ordering, shutdown                                                                                                    |

**Attacks leave failing tests.** A data or state lens that finds a way to lose, duplicate or lock data writes the
test that shows it (red on the head it read) and names it in the finding; the fix makes it green and keeps it. An
opinion without a test or exact trace is not a finding.

### Lens prompt

A lens with no prompt file of its own (the fallback lens `review`) uses this text, filled in:

> You are the `<lens>` lens, one read-only reviewer. Review the diff `<base>...<head>` and read the changed files in
> full and what they call into. Find bugs at their root: the unhappy branch of every `if`, error paths, empty and
> bounds, and where else the same pattern occurs. Answer the state question on a `state:` line: _<state question>_
> Every finding needs file:line and inputs or state leading to the wrong result. Post one comment in the format of
> COMMENTS.md, the header first: `lens: <lens> · reviewer: <who> · read at: <full head sha>`, then `covered:`,
> `checks:`, `state:`, `blocker:`, `major:`, `minor:`. Check it with `review_kit.py check --head <sha>` before
> posting; never edit a posted comment (post a new one).

## 5. Who runs what

Runners are kit settings, not rules: `runner` (default `claude-opus-5-5`, effort `high`) for every lens,
`lens_runners` for a lens that needs another, `audit_runner` (default `claude-fable-5-2`, effort `high`) for the
stuck-PR audit, and `forbidden_efforts` (default `low` and `max`: the highest effort cost about 3x for no reliable
gain). The plan prints the runner of each lens. A project changes them from eval numbers ([eval/](eval/README.md)):
the cheapest setting whose worst-case recall equals the best.

## 6. Posting

One comment per lens, header on the first line, full 40-character sha, `state:`, `blocker:` and `major:` lines.
Check before posting:

```
python3 -I fork-kit/review/lib/review_kit.py check --root <checkout> --head <sha> comment.md
```

Exit 0 only for a read of that head with a findings section, no blocker or major, and a state line; otherwise it
prints how the gate reads it. A comment the gate reads as `reads nothing` blocks the head: delete it, or edit its
first line to start with `not a lens:` (COMMENTS.md).
