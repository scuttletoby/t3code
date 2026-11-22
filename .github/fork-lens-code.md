# Lens: code

You review one pull request of this repository: is it correct, tested and as
simple as it can be? Read the diff and the files it touches; run its tests.

- Correctness first: wrong results, crashes, missed cases.
- Ask: what happens when this runs again, stops half-way, or meets existing,
  old or empty state?
- Tests: do they cover the change, and do they fail on the old code?

Write the report as one comment in the pull request's conversation. The gate
reads each comment alone (an inline review comment counts too, as its own
comment): a finding in another comment, or in a review's inline comments, is
not part of this report. Its first line is exactly

    lens: code · reviewer: <who> · read at: <the full 40-character head sha>

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
