# Adopting the review kit

How a project that uses the fork's merge gate (`fork-ci/actions/pr-queue`) adopts the review skill. The gate's own
guide is its README, sections [Review rules](../../fork-ci/actions/pr-queue/README.md#review-rules) and
[Lens comments](../../fork-ci/actions/pr-queue/README.md#lens-comments); this page is the order of steps and what the
kit adds.

## 1. The review block

Add a `review` block to the queue config (`.github/pr-queue.json` by default), in `"mode": "shadow"` first: the gate
then only reports what it would ask for. Start from the gate's
[review.recommended.json](../../fork-ci/actions/pr-queue/review.recommended.json):

- `rules`: the project's paths, heaviest first; the first rule that matches a path takes it. A rule's `weight` is
  the tier of its paths (`critical`, `normal` or `light`; SKILL.md, "Tier"). Critical paths (stored data,
  migrations, deploy, security, the merge path itself) name **two** lenses; the last rule is the catch-all `**`,
  normal, one lens. A project with its own tier names (a tier per module or plugin) writes one rule per module
  path with the weight its tier maps to; the tier list itself stays in the project's own files.
- `lenses`: each lens's name and its prompt file.
- `min_lenses`: at least 2 for critical, 1 for normal, 0 for light.
- `rounds`: the gate's own cap. Keep `max` at or above the kit's `lens_rounds` (the plan warns otherwise).
- `findings: true`: the gate holds a head while a lens's newest read has an open blocker or major.
- `state_line: true`: the gate parses it but does not enforce it yet; the kit's `check` does, with the gate's own
  rule for what counts as a `state:` line (`review_judge.has_state_line`).

Also set the queue config's `lens_format_doc` (a text the gate only quotes in its comments) to where agents read
the format: the kit's [COMMENTS.md](COMMENTS.md) at the fork tag the project pins, or the project's adapter skill
if it links there.

Check it: `python3 -I fork-kit/review/lib/review_kit.py plan --root . --changes <file>` with a
`git diff --name-status -z -M` output, or `--base origin/main` on a branch. A config the gate refuses gives a plain
message and exit 2. Run the kit from a fork checkout at the release the project's gate pins (README.md, "Which fork
version").

## 2. Lens prompt files

One file per lens named in the block (`.github/lenses/<lens>.md`). Each one says:

- what this lens looks for (SKILL.md, "Lenses"), and for a data or state lens: attacks with failing tests;
- the state question word for word (the kit settings' `state_question`), answered on a `state:` line;
- the comment format of [COMMENTS.md](COMMENTS.md): the header line first, `covered:`, `checks:`, `state:`,
  `blocker:`, `major:`, `minor:`; a round-2 read adds `r2` to the name and writes a recap as `round 1, fixed: ...`;
- to check the comment with `review_kit.py check --head <sha>` before posting, and never to edit a posted comment.

Keep the project's own hints (its domain's typical bugs and attacks) in these files, not in the kit.

## 3. The gate

The gate job, its pin and the order to change them (a gate release first, then the pin, then the config) are in the
gate's README, section "Use it". Run it in shadow for a while: read the gate's summary row on real pull requests and
compare it with what the plan says.

## 4. Kit settings

Optional: `.github/review-kit.json`. No file means the defaults. Every key is optional except `version`:

| Key                 | Default                                                                                        |
| ------------------- | ---------------------------------------------------------------------------------------------- |
| `version`           | `1` (a newer version is refused with a plain message: update the kit)                          |
| `queue_config`      | `.github/pr-queue.json`                                                                        |
| `runner`            | `{"model": "claude-opus-5-5", "effort": "high"}`: every lens                                   |
| `lens_runners`      | `{}`: `{"<lens>": {"model", "effort"}}` for a lens that needs another runner                   |
| `audit_runner`      | `{"model": "claude-fable-5-2", "effort": "high"}`: the one audit of a pull request still stuck |
| `forbidden_efforts` | `["low", "max"]`: a runner with one of these is refused                                        |
| `lens_rounds`       | `2`: lens rounds before the orchestrator checks fix diffs itself and stuck means an audit      |
| `minors`            | `same-round` (fixed in the PR or dropped; see `MINORS` in lib/review_kit.py) or `issue`        |
| `state_question`    | _What happens when this runs again, stops half-way, or meets existing, old or empty state?_    |

[review-kit.example.json](review-kit.example.json) shows one. An empty file, an unknown key or a wrong value is
refused with a plain message naming every problem, so a typo never turns into a silent default. A project that keeps
the file under another name gives it with `--settings <file>` to every `plan` and `check`; without it, a plan that
finds no queue config names any `.github/*review-kit*.json` it sees as the likely missing `--settings`.

**This file or T3's review rules setting:** T3 has a per-machine setting for the same runners (`server.reviewRules`
and `projectOverride.reviewRules`, docs/fork/README.md). Nothing in T3 reads it yet, so today this file decides. Once
T3 starts lenses itself, each decides what it starts: T3's setting the lenses T3 starts on that machine (the
machine's half: which provider and model it has), this file every lens an agent starts by hand from the skill and
what `plan` prints. Keep the two the same; the gate does not read either, so a difference changes cost and recall,
never what passes.

## 5. Enforce

When the shadow rows match what the lenses did, set `"mode": "enforce"`. The gate refuses an enforce block whose
rule order would let a lighter rule take a heavier rule's paths.

## What stays in the project

| Project fact                                                     | Where                                                          |
| ---------------------------------------------------------------- | -------------------------------------------------------------- |
| which paths are critical, normal or light; the lenses each needs | the `review` block of the queue config (read by the gate)      |
| its lens prompts, with its domain's typical bugs and attacks     | the lens prompt files the block names                          |
| who runs each lens, rounds, minors, the state question           | `.github/review-kit.json`                                      |
| the override prefix, ready and push commands, lens authors       | the queue config (`override`, `ready_command`, `push_command`) |
| outside reviewers, labels for a filed issue, its own checks      | the project's adapter skill or docs                            |

The general rules (tiers by path, the state question and the failure table, one or two independent lenses, twin lens
names, rounds and the nit-only quick check, minors, attacks with tests, the comment format) are the kit's, so a
project does not keep its own copy.

## The adapter skill

A project's own review skill is a thin adapter. It links the kit's [SKILL.md](SKILL.md) and
[COMMENTS.md](COMMENTS.md) at the fork's `ci/vN` tag the project's gate pins (README.md, "Which fork version"), and
moves that link when the pin moves. It never copies the kit's rules, and it never changes one: a rule that should
differ is changed in the kit. It adds only project facts:

- its tier names and examples, and how they map onto critical, normal and light;
- its commands: build and test, posting a comment, a check of a posted comment (ready and push are the queue
  config's, above);
- its domain lenses: the lens prompt files that hold their attack lists, and their test harnesses and end-to-end
  scenarios;
- its labels (risk labels for filed issues), its outside reviewers, and any effort raised for critical code.
