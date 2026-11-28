# Playtest adapter contract

Fork only. A playtest session lets a person try an agent's unfinished work on a real test target
(a test server, a staging site) while the agent changes it, round after round, without a pull
request per round. T3 runs the session; the project's **adapter** does everything that touches the
target. This page is what an adapter must do. The settings are
[`server.playtest` and `projectOverride.playtest`](README.md#serverplaytest).

Status: the settings, this contract, the session store, the worker (renew, the limits, ends for
gone threads) and the agent's `playtest` tool exist. The thread controls come in a later pull
request and follow this page.

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
again later; the session's end is not complete until `release` exits 0. One exception: a `release`
that cannot even start (its program is gone, or cannot be run) ends the session with the problem
`release could not run: …`, so a new session can start; the claim then expires on the target by
itself (below).

## How a step runs

- The program is given by its **full path on the server's OS**: `/…` on Linux and macOS; `C:\…`,
  `C:/…` or `\\server\…` on Windows (there `/…` and `\…` are relative to the current drive). A
  relative program or a bare name is refused when the settings are saved: it would run from the
  session's worktree or the `PATH`, and the agent can change both.
- No shell. The arguments are the ones in the settings; T3 and the agent add none.
- Working folder: `claim` and `push` steps run in the session's worktree; `renew` and `release` run
  in T3's own folder (`playtest` in its state folder), so a removed worktree never blocks an end.
- Environment: the server's, without what T3's terminals leave out (`PORT`, `T3CODE_*`, `VITE_*`
  and the like), plus the session's facts. A control character in a fact is sent as a space.
- Output: T3 reads the last 64 KB of a step's output, so a failure's own message is kept; the end
  of it is shown in the thread.
- **A step's own children die with it.** Its process group is killed when it exits, times out or is
  stopped. Anything that must outlive the step (a server it starts) needs its own session
  (`setsid`) or a service, and must not hold the step's output open.
- **The working folder of `claim` and `push` is the agent's.** A full path does not make a step safe
  when the program reads its scripts or config from the current folder: `git` (hooks, repository
  config), `make`, `npm`, `python -m …`, or a shell running a script from the repository all run
  files the agent wrote. Every step, and above all a guard, must be an installed script that does
  not run code from the worktree; read the worktree only as data (`T3_PLAYTEST_WORKTREE`). Put a
  guard step first in `push`.

| Variable               | Value                                                                       |
| ---------------------- | --------------------------------------------------------------------------- |
| `T3_PLAYTEST_SESSION`  | the session id, `pt_` and 10 characters; the same for every step            |
| `T3_PLAYTEST_TARGET`   | what the agent asked to playtest (letters, digits, `.`, `_`, `-`; up to 64) |
| `T3_PLAYTEST_NOTE`     | the agent's reason: at most 200 characters, no control characters, or empty |
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

## Sessions T3 keeps

T3 keeps its sessions in `playtest.json` in its state folder, so a session outlives a restart. A
session is `claiming`, `active`, `ending` (its `release` has not exited 0 yet) or `ended`, and keeps
why it ended and its last round. A thread has at most one session that is not ended.

- **One step at a time per session.** A push or start that meets a running step is refused as
  busy. `end` instead stops the running `claim` or round (the step's process is killed), then runs
  `release`. Two ends at once share one `release`.
- **A claim that fails** (any exit but 0 and 10, or a timeout) is followed by `release`, as the claim
  may be half taken. A claim that exits 10 ends the session with nothing to release.
- **After a restart** a round that was running reads `stopped`. The worker then finishes what the
  restart cut: a claim that was running ends the session as `claim-failed` and runs `release`; a
  release that was running runs again. If T3 crashed rather than stopped, the old step may still be
  running on the machine while that `release` runs: an adapter that cannot take two steps at once
  guards itself (a lock file).
- **A bad file** (torn, empty, not valid) is copied aside as `playtest.json.corrupt-<time>` and
  replaced by the sessions that still read; the lost ones are named in the server log, and their
  claims expire on the target by themselves. A file with no version or an older one is handled the
  same way: copied aside, its sessions kept, written again as the current version. A file from a
  newer T3 is not read or written: every playtest call is refused until that T3 runs again.

## The `playtest` tool

An agent runs its session with one T3 tool, `playtest`, in a tool group of its own, `playtest` (a
profile that keeps `waits` does not get it). It acts on the calling thread's own session only: the
thread comes from the agent's T3 credential, and the tool takes no thread and no setting.

| `action` | What it does                                                                                     |
| -------- | ------------------------------------------------------------------------------------------------ |
| `start`  | Claims `target` (and passes the optional `note`). Again for the target that is on: same session. |
| `push`   | Runs one round on the thread's worktree and answers when the round ended.                        |
| `status` | The thread's session, or its last one, with the last round and the step that runs now.           |
| `end`    | Ends the session and releases the target. With no session: nothing to end (not an error).        |

The answer names what happened and what to do next. Refusals are plain: playtest off ("only a
person can switch it on in the settings"), busy (a round runs: wait for it, or end the session),
no session (start one), a thread that is gone. `start` and `push` need a full-access thread not in
plan mode, by the thread's modes as T3 records them at each call (they run the person's steps
outside the thread's sandbox, without an approval); any other thread is refused with what to do.
`status` and `end` work in every thread, so a restricted thread can still release its target. An
outside MCP client (not in a T3 thread) is not offered the tool; a read-only client cannot use it.

## The thread chip

While a thread's session is on (`claiming`, `active` or `ending`), the web app shows a **Playtest**
chip above that thread's composer: the target, whether the session starts, is on or ends, its last
round and the step that runs now. Its **End** button ends the session exactly as the tool's `end`
does (the session's end reason is `person`), and a toast says what happened: ended (the target is
back, the worktree and its changes are kept), already ended, or still ending (the release has not
worked yet; T3 tries again, or press End again). End shows only for a connection that may change
threads (the operate scope); a read-only one sees the chip without it. The chip's tooltip has the
agent's note and how many rounds ran.

The chip goes when the session ends any other way (the agent's `end`, idle, time limit, claim gone,
thread gone). It is read again whenever the thread changes and every 10 seconds while it shows.
A read that fails keeps the chip as it was and reads again (up to six
failures in a row, then at the thread's next change); an older server without the chip's
routes, or a thread on another environment than the app's own server, shows no chip. An End whose
release could not even start says so: the session ended, but T3 did not give the target back (its
claim expires by itself). The routes are `GET /api/fork/playtest/state?threadId=`
(read scope) and `POST /api/fork/playtest/end` with `{ "threadId": … }` (operate scope). They send only
what the chip shows: no worktree, no step output and no problem text, just whether a release has
failed.

## The worker

A sweep on T3's scheduler (every few seconds; it reads the file only when something is due, and
not at all while the scheduler is held) does the rest:

- **Renew.** Once at once after T3 starts, then every `renewMinutes` after the last one. A claim
  that just worked counts as a renew. Renew runs beside a round, so a long build does not let the
  claim expire. A renew that fails (not 0, not 10) is kept as the session's problem and tried again
  after a minute (at most `renewMinutes`); a renew that exits 10 ends the session as `claim-gone`.
  A renew that has failed for 3 × `renewMinutes` in a row (since the last good claim or renew, or
  since its first failure after a restart) also ends it as `claim-gone`: the claim has expired on
  the target by then, and the notice says so. An end while renew runs stops it (its process is
  killed) before `release`. Renew, release and notice times run on the monotonic clock, so a wall
  clock set back does not hold them up.
- **Idle.** No round and no message from the person for `idleMinutes` ends the session as `idle`. A
  round that runs counts as activity; an agent's message does not.
- **Time limit.** `maxHours` after the start the session ends as `time-limit`, even while a round
  runs (the round is stopped).
- **Thread gone.** A thread that is archived or deleted ends its session as `thread-gone`. The
  sweep never waits for a thread read: each runs in the background (one per thread at a time,
  60 seconds at most), and the sweep after a "gone" answer ends the session. A read that is late or
  cannot answer counts as "not known", never as gone; it is logged and read again later, and the
  other sessions go on.
- **Release again.** A session left `ending` (its release failed, or a restart cut it) runs
  `release` again every `renewMinutes` until it exits 0.
- **One notice for the person.** When T3 ends a session itself (idle, time limit, claim gone, a
  claim a restart cut), it shows one notice in the thread, "Playtest ended": why, whether the target
  is back, that the worktree and its changes are kept, and to start a new session when the person
  is back. It is a system notice item (as T3's own "Branch not renamed"), not a message: it starts
  no agent turn and no agent reads it, so nothing claims the target again while nobody is there. It
  is written once its `release` worked, or once a `release` failed (it says so). It has a fixed id,
  so it shows once, also across restarts and retries. A write that fails or takes more than 60
  seconds is tried again a minute later. A thread that is gone does not get it (the server log says
  so); the session's status still says why it ended. Ends the agent or the person asked for, and
  `thread-gone`, show no notice.

## Limits

- At most 10 push steps, none named `claim`, `renew` or `release`; each step's argument list at most
  50 items, the program included; no empty argument; `timeoutSeconds` 1 to 3600 (default 120).
- `renewMinutes` 1 to 60 (default 10), `idleMinutes` 5 to 1440 (default 90), `maxHours` 1 to 48
  (default 6).
- The settings are person-only, like every fork setting: no agent tool can change them, and a
  repository's `t3.json` cannot set them. They do not stop an agent that can write the settings file
  itself with a shell; the target's own checks (the claim, the guard step) are the real guard.
