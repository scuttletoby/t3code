# Lens: state and tests

You review one pull request of this repository from one angle: does it hold
when it meets real state, and do its tests prove it? Read the diff and the
files it touches; run its tests.

- Ask every time: what happens when this runs again, stops half-way, or meets
  existing, old or empty state?
- Each fix line should turn a test red when it is removed. Spot-check some.
- New tests should fail on the old code. Check one.

Write the report as one comment (fork-ci/actions/pr-queue/README.md, sections
"Lens comments" and "Findings and rounds"): the first line is the lens header,
then one line per finding that starts `blocker:`, `major:` or `minor:`.
Write `major: none` when there is none.
