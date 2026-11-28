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

A post is a `gh` command that can write (not `view`, `list`, `status`, `checks`, `diff`, `download`, `search`, ...;
`gh api` only with `-f`/`-F`/`--input` or a `-X` other than GET), `git commit`, `push`, `tag`, `notes`, `merge`,
`commit-tree`, or curl, wget or http sending data to github. It is found also behind a wrapper and its options
(`env -u GH_TOKEN gh`, `sudo -u USER`, `timeout -k 5 30`, `nice -n 5`, `xargs -I X`, `stdbuf`, `flock`, ...), on a
line continued with a backslash, and inside `bash -c '...'`, `sh -c` and `eval`. For a command that holds a post to
a guarded repo, it reads:

- the command's words (quotes taken off, heredocs included, also with `%XX` URL-encoding decoded), leaving out only
  words that are no text: the folder of `cd DIR` and `git -C DIR`, the target of a `>` redirect, and the name of a
  file it reads (below). The whole command is checked, so a match in a part that posts nothing (a `grep` before the
  post) refuses too: the answer says to run that part on its own;
- the text, never the path, of the files the post reads, up to 5 MB each: the ones it names (`--body-file f`,
  `-F f`, `-F body=@f`, `< f`, `git commit -F f`, read in the folder of `git -C DIR`), files read inside `$(...)` or
  backquotes, and those of the commands piped into it (`cat f | gh pr comment 1 --body-file -`);
- for `git push`: exactly what that push sends, the commits of its refspecs (`HEAD`, a branch, `HEAD:refs/heads/x`,
  a sha, `--all`, `--tags`) that are not on the remote it pushes to yet (`git log <sources> --not
  --remotes=<remote>`): their message, author and committer, added lines and file paths, and the message and tagger
  of an annotated tag it pushes (named, `--tags`, `--follow-tags`). Other local branches and tags are not read. Git
  runs with no pager, external diff, textconv or fsmonitor, so the repo's own config runs nothing. More than
  `maxPushBytes` of changes (default 20 MB), or a push it cannot read in 8 s, is refused: push fewer commits at once.

Which repos are guarded: `leakGuard.repos` lists the public ones (`owner/name`, globs allowed, for example
`["example/public-fork"]`). Empty (the default), every post and push is checked. With a list, a post or push to a
repo not on it (a private notes repo, a remote that is no GitHub repo) is not checked; one whose repo is not known
(a gh post with no `-R` outside a checkout, `gh gist`) is.

On a match it refuses (exit 2) with the pattern file's **line number** only: the refusal never holds the pattern or
the matched text, so it can be pasted anywhere. It writes nothing (no log). Commands that post nothing (echo, grep,
read-only gh, a file written locally) always run, and the fast path passes a command that names no posting program
(gh, git, curl, wget, http, xh) without starting the hook.

Like the other guards it stops mistakes, not a hostile agent. Not read: text built at run time (a split string such
as `'Alex Ex''ample'` is read as one word, but `"Alex Ex$x"` is not), other encodings than `%XX` (base64, `\u`
escapes), a program it does not know (`python3 -c` that posts), a commit made by a script. The repo's permissions
and review are the wall; this catches the slip before it is public.

## What happens on a match

The agent takes the detail out and runs the command again. If it is already in a commit, amend or rebase that
commit before pushing (the push check reads every commit the push sends).

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
