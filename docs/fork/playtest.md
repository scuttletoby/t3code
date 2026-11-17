# Playtest adapter contract

Fork only. A playtest session lets a person try an agent's unfinished work on a real test target
(a test server, a staging site) while the agent changes it, round after round, without a pull
request per round. T3 runs the session; the project's **adapter** does everything that touches the
target. This page is what an adapter must do. The settings are
[`server.playtest` and `projectOverride.playtest`](README.md#serverplaytest).

Status: the settings and this contract exist. The session store, the `playtest` tool, the renew
worker and the thread controls come in later pull requests and follow this page.

## The steps

An adapter is four kinds of step, set in the settings by the person:

| Step      | When T3 runs it                                                                   | Exit 0                                   | Exit 10                                         |
| --------- | --------------------------------------------------------------------------------- | ---------------------------------------- | ----------------------------------------------- |
| `claim`   | when a session starts                                                             | the target is this session's             | busy, or not allowed (the session never starts) |
| `renew`   | every `renewMinutes` while the session lives, and once after T3 restarts          | the claim's expiry moved forward         | the claim is gone or ended: T3 ends the session |
| `push`    | a round: each step in the list, in order; the first that does not exit 0 stops it | this step is done                        | refused (a guard said no); nothing changed      |
| `release` | when the session ends, for any reason                                             | the target is back as it was, claim free | (treated as a failure)                          |

Any other exit code, or a step that runs past its `timeoutSeconds`, is a failure. A failed `claim`
or `push` step stops there and is reported in the thread. A failed `renew` or `release` is tried
again later; the session's end is not complete until `release` exits 0.

## How a step runs

- The program is given by its **full path on the server's OS**: `/…` on Linux and macOS; `C:\…`,
  `C:/…` or `\\server\…` on Windows (there `/…` and `\…` are relative to the current drive). A
  relative program or a bare name is refused when the settings are saved: it would run from the
  session's worktree or the `PATH`, and the agent can change both.
- No shell. The arguments are the ones in the settings; T3 and the agent add none.
- Working folder: the session's worktree. Environment: the server's, plus the session's facts.
- **The working folder is the agent's.** A full path does not make a step safe when the program
  reads its scripts or config from the current folder: `git` (hooks, repository config), `make`,
  `npm`, `python -m …`, or a shell running a script from the repository all run files the agent
  wrote. Every step, and above all a guard, must be an installed script that does not run code from
  the worktree; read the worktree only as data (`T3_PLAYTEST_WORKTREE`). Put a guard step first in
  `push`.

| Variable               | Value                                                                       |
| ---------------------- | --------------------------------------------------------------------------- |
| `T3_PLAYTEST_SESSION`  | the session id, `pt_` and 10 characters; the same for every step            |
| `T3_PLAYTEST_TARGET`   | what the agent asked to playtest (letters, digits, `.`, `_`, `-`; up to 64) |
| `T3_PLAYTEST_NOTE`     | the agent's reason: one line, at most 200 characters, may be empty          |
| `T3_PLAYTEST_PROJECT`  | the project id                                                              |
| `T3_PLAYTEST_THREAD`   | the thread that owns the session                                            |
| `T3_PLAYTEST_WORKTREE` | the session's worktree                                                      |
| `T3_PLAYTEST_STEP`     | `claim`, `renew`, `release`, or the push step's name                        |
| `T3_PLAYTEST_ROUND`    | the round number: `0` before the first round, then `1`, `2`, …              |

The adapter decides whether a target is allowed (`claim` exits 10 when it is not). T3 checks only
the target's form.

## What every step must survive

T3 can stop, restart or lose its machine at any point, and so can the target. So:

- **The same call twice is safe.** `claim` with a session id that already holds the claim returns 0
  and changes nothing. `renew` only moves the expiry forward. A `push` step run again for the same
  round finishes or redoes that round.
- **`release` is safe with no claim**: it exits 0 when there is nothing to release, and finishes a
  release that stopped half-way.
- **The claim expires by itself on the target.** If nobody renews it (T3 is gone, the machine is
  off), the target ends the claim and puts itself back without T3. Set `renewMinutes` well under that
  expiry, so one or two missed renews do not end a session.
- **A step meets old state.** A claim from another session is "busy" (exit 10), never taken over. A
  push step checks that the claim is still this session's before it changes anything.

## What T3 promises

- One round at a time per session; a second `push` while a round runs is refused.
- A session keeps the steps it started with: editing the settings, or switching playtest off, during
  a session still renews and releases that session with the steps it claimed with.
- T3 ends a session when nothing happened for `idleMinutes` (no round, no message from the person),
  after `maxHours` in total, and when its thread is deleted or archived. Each end runs `release`.

## Limits

- At most 10 push steps, none named `claim`, `renew` or `release`; each step's argument list at most
  50 items, the program included; no empty argument; `timeoutSeconds` 1 to 3600 (default 120).
- `renewMinutes` 1 to 60 (default 10), `idleMinutes` 5 to 1440 (default 90), `maxHours` 1 to 48
  (default 6).
- The settings are person-only, like every fork setting: no agent tool can change them, and a
  repository's `t3.json` cannot set them. They do not stop an agent that can write the settings file
  itself with a shell; the target's own checks (the claim, the guard step) are the real guard.
