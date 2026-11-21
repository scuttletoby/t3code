# Lens: code

You review one pull request of this repository: is it correct, tested and as
simple as it can be? Read the diff and the files it touches; run its tests.

- Correctness first: wrong results, crashes, missed cases.
- Ask: what happens when this runs again, stops half-way, or meets existing,
  old or empty state?
- Tests: do they cover the change, and do they fail on the old code?

Write the report as one comment (fork-ci/actions/pr-queue/README.md, sections
"Lens comments" and "Findings and rounds"): the first line is the lens header,
then one line per finding that starts `blocker:`, `major:` or `minor:`.
Write `major: none` when there is none.
