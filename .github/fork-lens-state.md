# Lens: state and tests

You review one pull request of this repository from one angle: does it hold
when it meets real state, and do its tests prove it? Read the diff and the
files it touches; run its tests.

- Ask every time: what happens when this runs again, stops half-way, or meets
  existing, old or empty state?
- Each fix line should turn a test red when it is removed. Spot-check some.
- New tests should fail on the old code. Check one.

Write the report as one comment in the pull request's conversation, never as
inline review comments: findings there are not read. Its first line is exactly

    lens: state · reviewer: <who> · read at: <the full 40-character head sha>

Then the findings section, always, also when it is empty:

    blocker: none
    major: none

or, for each finding, one line that starts `blocker:` or `major:` (`minor:`
for the rest). Quote code or example findings only inside a fenced code block.
A report without its `blocker:` and `major:` lines cannot be judged and fails
the head (fork-ci/actions/pr-queue/README.md, "Lens comments" and "Findings
and rounds").
