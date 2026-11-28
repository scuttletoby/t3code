# claude-login: sign Claude Code in on a machine with no browser

A Claude Code login that is not a setup token ends after a fixed time and must then be signed in again. On a
machine nobody logs into with a browser, an agent runs the sign-in and the owner only opens a link and gives the
code. A timer opens an issue before a login ends, so nobody finds out from agents that stopped working.

Linux, Python 3 standard library, tmux and gh. Every account, the issue repo, its labels and the machine's name come
from the user's own config: nothing in the kit names a project, a person or a machine.

## Use

```sh
claude-login status                    # every account: masked email, plan, login=ok|ended|signed_out|unreadable, …
claude-login start main                # prints the sign-in link for the owner (a tmux session waits for the code)
claude-login finish main /path/code    # types the code from the file, checks the new login, deletes the file
claude-login set-aside main            # a credentials file that cannot be read: moved aside (kept), see below
claude-login check                     # the timer's job: one issue per account that needs a new sign-in
```

The agent gets the code with a secret prompt delivered as a file (never in chat or on a command line), and
`finish` reads it from that file and hands it to tmux on stdin (`tmux load-buffer -`, then `paste-buffer`), never as
an argument. The proof of a new login is the sign-in's own `claude auth login` ending with exit 0 and a login in the
file, or the login's end (`refreshTokenExpiresAt`) moving on by more than an hour. A refresh of the access token
rewrites the file every few minutes, so "the file changed" proves nothing.

## Config

`$XDG_CONFIG_HOME/claude-login/config.json` (`~/.config/claude-login/config.json`), the user's own file:

```json
{
  "version": 1,
  "claude": "~/.local/bin/claude",
  "accounts": [
    { "name": "main", "configDir": "" },
    { "name": "second", "configDir": "~/.claude-second" }
  ],
  "issueRepo": "OWNER/REPO",
  "labels": ["type:ops"],
  "machine": "the build box",
  "warnDays": 2
}
```

`configDir` `""` is Claude Code's default folder (`~/.claude`); another folder is used as `CLAUDE_CONFIG_DIR`.
Optional: `timeout` (seconds per claude, gh or tmux call, above 0, 60), `wait` (seconds `start` waits for the link
and `finish` for the new login, 30), `readPause` (seconds between reads of a credentials file, 1). Numbers must be
finite (no `NaN`, `Infinity` or `1e400`). Without `issueRepo`, `check` says what it would open and fails (exit 1).

## What check opens

One issue per account, machine and kind. Each title names all three (`Renew the Claude login of main on the build
box (signed out)`), and check compares the titles of the open issues itself: GitHub's title search matches words,
so `main` also finds `main-2`. Machines that share one `issueRepo` and use the same account names never hide each
other's issues. A failed lookup or create fails the run, so the timer turns red instead of looking green.

- **signed out** (no credentials file, or no login in it): start, then finish.
- **ended** (its `refreshTokenExpiresAt` has passed): start, then finish.
- **unreadable** (not valid JSON, also when read again; or no regular file: a link, also a dangling one, a FIFO or a
  file over 64 KiB): set-aside first, then start and finish.
  `finish` refuses an unreadable file every time, since "no login" as the value before would make the next good
  read look like a new login. `set-aside` renames the file to `.credentials.json.unreadable-<UTC time>` (kept, never
  read or printed; `-1`, `-2` … at the end when that name is taken) only when it is still unreadable after three
  reads.
- **ends within `warnDays`**, and **not refreshing** (the access token ended over an hour ago: whatever refreshed it
  stopped).
- A file with no end date (an older Claude Code wrote no `refreshTokenExpiresAt`) opens nothing; check fails with
  that reason.

Not opened: any issue of an account whose sign-in is running right now (`claude-login-<account>` tmux session, under
30 minutes old): an agent is on it.

## State

- **A file half-written for a moment.** A refresh rewrites the credentials file; every read of it (status, check,
  start, finish) reads again, up to three times, while it is not valid.
- **`start` twice.** The second is refused while the first sign-in runs (another agent may be about to finish it);
  `start ACCOUNT --again` replaces it. One start, finish or set-aside at a time per account: a lock in
  `$XDG_RUNTIME_DIR/claude-login/` when that folder is the user's and nobody else can write to it, else in
  `~/.local/state/claude-login/` (`$XDG_STATE_HOME`), never in a shared `/tmp`. The lock folder must be the user's
  own with mode 0700, and the lock is opened without following a link; a tmux server started under it never holds
  it.
- **A stale code.** `finish` prints only the error word the sign-in shows (the screen may still hold the code) and
  says to `start --again`. The code file is kept when nothing was typed (no sign-in runs, or tmux would not take the
  code). Once the code may be in the sign-in, the session is ended on every way out (no screen keeps the code) and
  the code file deleted; a failed paste or Enter says to `start --again`, so a retry never types the code twice.
- **A login stopped half-way.** A sign-in session older than 30 minutes is stale: the next `start`, `finish` or
  `check` ends it, so an unfinished start never keeps check quiet for longer than that. (The shell's `sleep` after
  `claude auth login` only starts once claude exits, and claude waits for a code for as long as it is left.)

## Secrets

It reads only the expiry fields of a credentials file and whether a refresh token is there. It never prints or
logs a token, the file's text or the code, and never puts the code on a command line (where `ps` shows it to every
local user); a broken file is named by its state, not its content. Claude runs with `CLAUDE_CODE_OAUTH_TOKEN`
unset, so a normal login is used and made, never a setup token. The tests use made-up files and codes only, and
check that none of them shows in any output or in the command lines given to tmux and gh.

## Moving from a copy kept in a project

A `claude-login` helper copied into a project before this kit keeps running after the kit is installed: a user
unit `~/.config/systemd/user/claude-login-check.timer` (or `.service`) overrides the kit's units in
`/etc/systemd/user/`, and `~/.local/bin/claude-login` usually comes before `/usr/local/bin` on PATH. Its issue
titles (`Renew the <account>'s Claude login`) are not the kit's, so check does not see them as open. For each user:

```sh
systemctl --user disable --now claude-login-check.timer   # the old helper's timer, if it ran
rm -f ~/.config/systemd/user/claude-login-check.timer ~/.config/systemd/user/claude-login-check.service
rm -f ~/.local/bin/claude-login                           # only the old helper's copy
systemctl --user daemon-reload && systemctl --user enable --now claude-login-check.timer
command -v claude-login                                   # /usr/local/bin/claude-login
```

Then close the old helper's open issues; check opens its own where one is still needed.

## Install

As root, from a release commit of the fork (the shared [bootstrap](../common/bootstrap.sh) checks that it is on the
protected `ci-release` branch first):

```sh
sh fork-kit-bootstrap.sh --kit claude-login --repo OWNER/FORK --sha RELEASE_SHA [--timer | --no-timer]
```

The installer ([sbin/claude-login-install](sbin/claude-login-install), on the shared
[kit_install.py](../common/kit_install.py)) copies the release to `/usr/local/libexec/claude-login/versions/<sha>/`,
self-tests it and links `/usr/local/bin/claude-login`. `--timer` writes the user units
`/etc/systemd/user/claude-login-check.{service,timer}` (hourly); each user with a config then runs
`systemctl --user daemon-reload && systemctl --user enable --now claude-login-check.timer`. `claude-login-install
self-check` reports drift; `uninstall` removes everything it wrote.

## Tests

`tests/claude-login.test.py` (stand-in claude, gh and tmux on a fake HOME; the real tmux on a socket of its own when
it is there) and `tests/install.test.py` (the installer on a fake host). No root, no network, no real login.
