# review: a general review skill for the fork's merge gate

A skill and its tools for reviewing pull requests in a project that uses the merge gate
([fork-ci/actions/pr-queue](../../fork-ci/actions/pr-queue/README.md)): lenses picked by the changed paths, the state
question every time, one or two independent lenses, at most two lens rounds, root-cause fixes, and attacks that leave
failing tests. Unlike the other kits it installs nothing on a host: an agent reads the skill and runs the tools from a
checkout of the fork.

| File                                               | What                                                                                     |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| [SKILL.md](SKILL.md)                               | the review skill: tiers, state question, order, rounds, minors, lenses, runners, posting |
| [REVIEW.md](REVIEW.md)                             | how a project adopts it: the review block, lens prompt files, the gate, kit settings     |
| [COMMENTS.md](COMMENTS.md)                         | the lens comment format, with examples checked against the gate's own parser             |
| [review-kit.example.json](review-kit.example.json) | the kit settings (`.github/review-kit.json` in the project; optional)                    |
| [lib/review_kit.py](lib/review_kit.py)             | `plan` (which lenses a change needs), `check` (how the gate reads a comment), `doc`      |
| [eval/](eval/README.md)                            | the model eval: which model and effort finds the confirmed bugs, from one config file    |

The kit holds no project facts: which paths are critical and which lenses they need are the project's `review` block
(read by the gate), and who runs a lens is the project's kit settings. The kit imports the gate's own parser
(`config.py`, `lens.py`, `review_judge.py`), so it cannot read a comment or a path differently from the gate; it needs
the fork's `fork-ci/` folder beside it.

Tests (no network, no host changes):

```
python3 -I -B fork-kit/review/tests/comments.test.py
python3 -I -B fork-kit/review/tests/plan.test.py
python3 -I -B fork-kit/review/tests/eval.test.py
```
