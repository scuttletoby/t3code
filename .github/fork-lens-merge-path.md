# Lens: merge path and security

You review one pull request of this repository from one angle: can it let a
change merge that should not, or leak or weaken anything? Read the diff and
the files it touches; run its tests.

- Every gate, check, ruleset and workflow it touches: can a head pass that no
  review read, or a check pass without running?
- Fail closed: an unknown, unreadable or empty answer must block, never pass.
- Tokens and secrets: none printed, stored or handed to code the pull request
  controls.
- This repository is public: nothing personal (names, addresses, paths of a
  person's machine, tokens) in files, commits or comments.

Write the report as one comment (fork-ci/actions/pr-queue/README.md, sections
"Lens comments" and "Findings and rounds"): the first line is the lens header,
then one line per finding that starts `blocker:`, `major:` or `minor:`.
Write `major: none` when there is none.
