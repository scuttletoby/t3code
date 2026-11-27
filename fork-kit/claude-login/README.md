# claude-login: sign Claude Code in on a machine with no browser

A Claude Code login that is not a setup token ends after a fixed time and must then be signed in again. On a
machine nobody logs into with a browser, an agent runs the sign-in and the owner only opens a link and gives the
code. A timer opens an issue before a login ends, so nobody finds out from agents that stopped working.

Linux, Python 3 standard library, tmux and gh. Every account, the issue repo, its labels and the machine's name come
from the user's own config: nothing in the kit names a project, a person or a machine.

## Use

```sh
claude-login status                    # every account: masked email, plan, login=ok|signed_out|unreadable, time left
claude-login start main                # prints the sign-in link for the owner (a tmux session waits for the code)
claude-login finish main /path/code    # types the code from the file, checks the new login, deletes the file
claude-login set-aside main            # a credentials file that cannot be read: moved aside (kept), see below
claude-login check                     # the timer's job: one issue per account that needs a new sign-in
```

The agent gets the code with a secret prompt delivered as a file (never in chat or on a command line), and
`finish` reads it from that file. The proof of a new login is that its end (`refreshTokenExpiresAt`) moved on by
more than an hour: a refresh of the access token rewrites the file every few minutes, so "the file changed" proves
nothing.

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
Optional: `timeout` (seconds per claude, gh or tmux call, 60), `wait` (seconds `finish` waits for the new login,
30), `readPause` (seconds between reads of a credentials file, 1). Without `issueRepo`, `check` says what it would
open and fails (exit 1).

## What check opens

One issue per account and kind (it looks up open issues by title first; a failed lookup or create fails the run,
so the timer turns red instead of looking green):

- **signed out** (no credentials file, or no login in it): start, then finish.
- **unreadable** (a file that is not valid JSON, also when read again): set-aside first, then start and finish.
  `finish` refuses an unreadable file every time, since "no login" as the value before would make the next good
  read look like a new login. `set-aside` renames the file to `.credentials.json.unreadable-<UTC time>` (kept, never
  read or printed) only when it is still unreadable after three reads.
- **ends within `warnDays`**, and **not refreshing** (the access token ended over an hour ago: whatever refreshed it
  stopped).
- A file with no end date (an older Claude Code wrote no `refreshTokenExpiresAt`) opens nothing; check fails with
  that reason.

Not opened: an account whose sign-in is running right now (`claude-login-<account>` tmux session): an agent is on
it.

## State

- **A file half-written for a moment.** A refresh rewrites the credentials file; every read of it (status, check,
  start, finish) reads again, up to three times, while it is not valid.
- **`start` twice.** The second is refused while the first sign-in runs (another agent may be about to finish it);
  `start ACCOUNT --again` replaces it. One start at a time per account (a lock in `$XDG_RUNTIME_DIR`, which a tmux
  server started by it never holds).
- **A stale code.** `finish` prints only the error word the sign-in shows (the screen may still hold the code) and
  says to `start --again`. The code file is deleted once typed, kept when typing fails or no sign-in runs.
- **A login stopped half-way.** The tmux session stays until `start --again`, `finish`, or its 10 minutes end.

## Secrets

It reads only the expiry fields of a credentials file and whether a refresh token is there. It never prints or
logs a token, the file's text or the code; a broken file is named by its state, not its content. Claude runs with
`CLAUDE_CODE_OAUTH_TOKEN` unset, so a normal login is used and made, never a setup token. The tests use made-up
files and codes only, and check that none of them shows in any output.

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
