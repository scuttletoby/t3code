# Lens: approach

This pull request has reached its third review round or later. Do not look for
more small findings. Ask whether the whole approach is right:

- Does it solve the problem the issue names, at the root?
- Is there a simpler way, or an existing mechanism it should build on?
- Will more rounds converge, or should it be split, restarted or closed?

Write the report as one comment in the pull request's conversation, never as
inline review comments: findings there are not read. Its first line is exactly

    lens: approach · reviewer: <who> · read at: <the full 40-character head sha>

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
