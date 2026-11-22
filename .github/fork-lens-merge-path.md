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

Write the report as one comment in the pull request's conversation. The gate
reads each comment alone (an inline review comment counts too, as its own
comment): a finding in another comment, or in a review's inline comments, is
not part of this report. Its first line is exactly

    lens: merge-path · reviewer: <who> · read at: <the full 40-character head sha>

Then the findings section, always, also when it is empty:

    blocker: none
    major: none

or, for each finding, one line that starts `blocker:` or `major:` (`minor:`
for the rest). Quote code or example findings only inside a fenced code block.
Write a finding of an earlier round without a label (`round 1, fixed: x`): a
`blocker:` or `major:` line is read as open, also `Major 1: fixed`.
A report without its `blocker:` and `major:` lines cannot be judged and fails
the head (fork-ci/actions/pr-queue/README.md, "Lens comments" and "Findings
and rounds").
