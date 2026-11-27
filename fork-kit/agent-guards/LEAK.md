# Leak guard: nothing personal in the public fork

The fork is public. Names, addresses, machine names and private paths must never reach its PRs, issues, comments,
releases or commits. `leak-guard` (a hook) refuses a post or a push that holds one; `leak-scan` (a command) reports
old text that does. Both match the text against a **private pattern file** that never enters a repo.

## The pattern file

Where: `/etc/fork-kit/leak-patterns` by default, or the machine file's `agent-guards.leakGuard.patternsFile`. Keep
it outside every git checkout (`agent-guards self-check` names a file inside one as a problem), owned by root and
readable by the agents' user, so an agent can read it but not weaken it (self-check warns when the agents' user can
write it):

```sh
sudo install -m 0644 -o root -g root /dev/null /etc/fork-kit/leak-patterns
sudoedit /etc/fork-kit/leak-patterns
```

One pattern per line:

```text
# comments and blank lines are skipped
Alex Example          plain text, in any case, not inside a longer word ("Alex" is not found in "Alexander")
alex@example.org
/home/alex
re:box-\d+\.lan       a Python regular expression, in any case
```

A file that is there but cannot be used whole (unreadable, bigger than 1 MB, a line that is no valid pattern)
makes the guard refuse every post and push, naming the line, until it is fixed. No file: nothing is checked, and
self-check says so.

## What the hook checks

For a command that posts or pushes (any `gh` command; `git commit`, `push`, `tag`, `notes`, `merge`, `commit-tree`;
curl, wget or http naming github), it reads:

- the whole command text, heredocs included (a folder it only runs in, `cd DIR` or `git -C DIR`, is left out);
- every file the command names (`--body-file f`, `-F f`, `-F body=@f`, `< f`, `git commit -F f`), up to 5 MB each;
- for `git push`: every commit on no remote yet (`git log --branches --tags --not --remotes`), its message, author
  and committer, and its added lines and file paths. Git runs with no pager, external diff, textconv or fsmonitor,
  so the repo's own config runs nothing. More than `maxPushBytes` of changes (default 20 MB), or a push it cannot
  read in 8 s, is refused: push fewer commits at once.

On a match it refuses (exit 2) with the pattern file's **line number** only: the refusal never holds the pattern or
the matched text, so it can be pasted anywhere. It writes nothing (no log). Commands that post nothing (echo, grep,
a file written locally) always run.

Like the other guards it stops mistakes, not a hostile agent: text built at run time, a program it does not know
(`python3 -c` that posts), or `bash -c '...'` around a post are not read. The repo's permissions and review are the
wall; this catches the slip before it is public.

## What happens on a match

The agent takes the detail out and runs the command again. If it is already in a commit, amend or rebase that
commit before pushing (the push check reads every commit not yet on a remote).

## Old text: leak-scan

```sh
leak-scan --repo OWNER/FORK [--commits 300]     # exit 0 clean, 1 matches, 2 could not read
```

It reads, through the caller's gh login, every issue and PR (title and body), issue and PR comments, review comments,
releases, and the newest commits of the default branch (message, author, committer), and prints one line per match:
`MATCH <url> (<what>): pattern line <n>`. It changes nothing. Not read: PR review bodies, wikis, edit history.

Run it on a schedule as the agents' user, for example a crontab line:

```cron
17 6 * * * /usr/local/bin/leak-scan --repo OWNER/FORK >> ~/.local/state/leak-scan.log 2>&1
```

**Removing old text.** An edited issue, PR or comment keeps its old version in GitHub's edit history, visible to
anyone. To remove a detail, delete the comment (or ask GitHub support for an issue or PR body) and post it again
without it. A pushed commit stays reachable until the branch is rewritten and GitHub's caches expire; for a secret
(a token) rotate it instead.
