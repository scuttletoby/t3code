# Handoff instead of compaction

Fork only. When a long thread gets big, or sits idle while its prompt cache is about to run out, T3
starts a fresh session in the same thread and gives it a written handoff. The thread keeps its id,
its running child tasks, its watched pull requests and its waits: they all deliver into the new
session, because T3 sends them to the thread's active session, and the fresh start makes the new one
active. Upstream's compaction rewrites the old session's context instead.

It is off by default. The setting is [`server.handoff`](README.md#serverhandoff), and a project can
have its own ([`projectOverride.handoff`](README.md#projectoverridehandoff)). Only the person can
change it; agents cannot.

## When a thread hands off

The handoff service looks at a thread each time one of its runs completes. It reads the thread's
settings first: with a mode other than `handoff` it reads nothing more and sends nothing.

- **Size backstop.** At the end of a turn, a thread at `sizeTokens` (150,000) or more hands off.
- **Idle.** A thread at `idleTokens` (75,000; `0` never) or more hands off shortly before its prompt
  cache expires: 4 minutes before with the `agent` source (and with none), 2 minutes with `command`. The service
  sets one timer per thread; a newer run's turn end replaces it. When the timer fires and a newer
  run has finished since, that newer run decides instead. A late update of an older run (a child
  of an earlier run settling, say) does not lose the timer: the service takes over the latest
  completed run, as upstream's idle compaction does.
- Never after a failed or interrupted run, after a `/compact`, or after a handoff run.
- Under 20,000 tokens never.

Token counts are the provider's own last reading for the active session. A provider that reports
none never hands off.

## Threads with a role

The assistant thread and each project's orchestrator (`server.threadRoles`, the calm sidebar's
roles) can have their own mode: `roles` in the handoff settings, for example `handoff` while `mode`
stays `off`, so only the long-running role threads hand off. A thread's own entry in `threads`
still wins, and a project's value replaces the server's whole, its `roles` too. A role pointer to an
archived or deleted thread changes nothing: that thread is not handed off anyway.

Rolling back to a build without `roles` after setting it turns handoff off for every thread, not only
the role threads: an older build reads handoff settings with a field it does not know as off, and
its next settings save writes the default in their place. Clear `roles` ("Same as above") before
rolling back.

The new session runs on `modelSelection`, else the thread's current model, as for every thread. The
model policy (`server.modelPolicy`) checks agents' picks, not this one.

## Hand off now

A person can hand a thread off at once: **Hand off now** in the thread's menu (the sidebar's and the
chat header's). The item shows only for a thread on the app's own server with handoff on for it. It
is greyed out, naming what it waits on, while the thread is busy (a running turn, an approval or
question, a child's result not read yet, a fork or merge, background work, an active goal), and
hidden when a press would do nothing (just handed off or compacted, a newer turn running, under
20,000 tokens). A press decides as a turn end would, but skips the size backstop and the idle wait.
It settles first like any handoff and uses the same ids as the latest turn's own handoff, so a
second press, or a press while that turn's handoff is going, sends one handoff. A press on a busy
thread queues nothing: the toast says what it waits on, and the person may press again later. A
press that finds the thread busy again inside its settle ends unsent too; the turn end's own
decision goes on (its wait, then its idle handoff once the thread is quiet), as if no one had
pressed. The menu reads the item's state only when the settings this app holds may put handoff on
for the thread; off, the menu opens at once.

When a due handoff waits on a busy thread, a chip above the composer says "Handoff waiting on …"
and goes once it no longer waits. The handoff's first message still shows upstream's "New session"
divider; a "Handoff N" label there is still to come.

The app reads `GET /api/fork/handoff/state?threadId=` (read access) and presses with `POST
/api/fork/handoff/now` and `{ "threadId": ... }` (operate access). A thread that cannot be read
answers as off.

## One owner per thread

With handoff on for a thread (mode `handoff`, from the server, its project or the thread's own
entry) and a handoff that can run, upstream's idle compaction skips that thread, so a compaction
and a handoff never both act on it. The handoff settings alone decide what happens when it sits
idle: `idleTokens` `0` means no idle action at all. The check happens at the moment compaction
would send, so a setting changed while compaction waited counts. With any other mode, upstream's
idle compaction works as before. If the thread is missing, or the thread or the settings cannot be
read at that moment, compaction goes ahead. Nothing is remembered between two checks.

Three cases where neither acts for a while (each one idle spell; the next turn end decides again):

- **Handoff switched on while compaction waits.** The handoff service already decided "off" at the
  turn end, and compaction is now skipped at the send. Nothing acts on that idle spell; the handoff
  starts from the thread's next turn end.
- **The handoff's instance added back while compaction waits.** The same: the handoff service found
  it could not run at the turn end, and compaction now finds that it can (see below).
- **The handoff's instance turned off or removed after compaction skipped.** The mirror case. Both
  timers fire shortly before the cache expires, and the handoff also waits out the settle, so
  compaction's send can come first, find a handoff that can run, and skip. If the instance is then
  turned off or removed before the handoff's own read (after the idle sleep, or in the settle), the
  handoff stops too. The same goes for the mode switched from `handoff` to `off` after compaction
  skipped.

### A handoff that cannot run

A handoff runs on `modelSelection` from the setting, else the thread's current model. It can run
only when that provider instance is on for the thread's project (upstream's
`resolveProjectProviderInstanceEnabled`, the rule its idle compaction uses for its own instance) and
is registered (upstream's adapter lookup, which is where a send to a removed instance fails). When
it cannot run, handoff does not own the thread: the handoff service sends nothing, and upstream's
idle compaction works as it would with handoff off. Both read this through one function
(`threadHandoff` in `handoffOwner.ts`), so they never disagree. Each logs "handoff cannot run;
idle compaction covers this thread" with the instance and the reason (`instance-off` or
`instance-missing`); the thread itself shows nothing. The size backstop does nothing at such a turn
end; the provider's own compaction at its context limit still applies.

- **Removed.** A removed instance usually has no config left, so it already reads as off (only the
  built-in `codex` and `claudeAgent` are on without one). The registry lookup also covers an
  instance that has a config but is not running: a bad config, or secrets that cannot be read.
- **Turned off but still registered.** A turned-off instance stays in the registry, and upstream's
  orchestrator does not check "on" when a message starts at once. So before this rule, the handoff
  run started on the provider the person had turned off. Now the handoff counts it as cannot run.
- **Checked again at every step**: the turn end, after the idle sleep, after the settle, and before
  a retry of a failed handoff run. An instance turned off or removed meanwhile stops the handoff;
  compaction checks at its own send.
- **Added back or turned on again.** The next turn end (or the next check of a waiting compaction)
  sees it, and handoff owns the thread again. That one idle spell may have neither (above).
- **The retry of a failed handoff run** sends on the run's own model, which may differ from the
  one the setting names now. So it checks both: the setting's instance (as every step does) and the
  run's own (`handoffInstanceBlocked`, the same rule). If either is off or not registered, nothing
  is sent again, the text stays in the thread and the log says why. Nothing is stored for this: the
  run carries its model. Idle compaction already decided for that spell (upstream starts it only
  after a completed run), so the next turn end decides again.

To change what happens, fix `modelSelection` where it applies (the project's override, if it has
one, replaces the server's handoff settings whole), or set the mode to `off` or `compact`. A
thread's own entry wins over any mode, so if the thread has one, change that entry.

## Only at a quiet thread

A thread is quiet when none of these is true: a run is queued or running, a request (an approval or
a question) is pending, a child's result is on its way into the thread, a fork or merge is being
applied, the session itself still runs background agents or commands, or the session's goal is
active. Running T3 child tasks never count: their results arrive in the new session.

At the size backstop, a thread that is not quiet waits: the service reads it again each time the
thread changes, and hands off as soon as it is quiet (unless a newer run finished meanwhile). In the
idle window it does not wait: the next turn end starts over.

The service decides from what it read, but something else can land before the handoff does. So it
checks twice more around the send:

- **Settle.** Once the thread is quiet and the handoff is due, the service waits 10 s and reads
  the thread again. The relay holds a "finished" alert for 5 s in case the run goes on, and longer
  while it shows more work: a running T3 child, an awaited PR watch, a monitor's wake, or a child's
  request. A handoff run that starts while the relay holds would drop the person's alert for their
  own turn. So during the settle the service reads the thread as upstream's relay shows it
  (`projectThreadAwarenessV2` on the thread's awareness shell, with a live child's request on it):
  at the start, every second, and on every request or run event. Only when every check showed
  "finished" is the handoff sent quietly; otherwise, or when a read fails, it is sent as one that
  alerts (see "No second relay alert"). Children never hold the handoff back. The 10 s is a fixed
  sleep, so a change of the wall clock does not cut it short. If the thread stops being quiet
  meanwhile, the settle starts over once it is quiet again; a late update that takes over a newer
  run settles afresh too.
  A reply from the person inside those 10 s wins: it runs in the old session, on its full context,
  and that turn's end decides again.
- **Right before.** The thread must still be quiet, and its latest run must still be the one the
  handoff was made from. Otherwise nothing is sent.
- **Right after.** If something landed in between, the orchestrator has queued the handoff behind
  it. The service cancels the queued handoff (upstream's own "remove a queued message"), which
  refuses a run that already started.

A message from the person or a child result that lands first always wins, and nothing of theirs is
lost. The send and the cancel are one step that a late event or a shutdown cannot cut in half. One
narrow case remains: if that message's whole run ends between the send and the cancel, the handoff
runs after it. Nothing is lost then either, but the handoff's text does not include that
last turn; the new session can read it in the thread.

## The message

The new session's first message has these parts:

1. **T3's state**, read when it is sent: the handoff's number, the thread, the run it was made from,
   the model and effort of the new session, the open child tasks (and which wait for an approval or
   an answer), and the watched pull requests with their checks.
2. **The old session's own handoff**, from the setting's `source`:
   - **`none`** (the default, and a handoff setting saved before the sources existed): no part 2
     but a line that no source is set. Nothing else runs.
   - **`command`:** T3 runs the program in `command` in the project folder, with the runner wait
     checks use (`makeSettingsProgramRunner` in `waits/WaitJudges.ts`): no shell, a 60-second
     timeout (the process is killed; what it printed so far is not used), a 32 KB cap on stdout
     and on stderr each (warnings count too), its stdin closed, and a short list of the server's
     environment plus the thread and project ids (`T3CODE_THREAD_ID`, `T3CODE_PROJECT_ID`). Its
     output, trimmed, is
     the handoff, fenced as the command's notes (see the `agent` source's fence below). It must only
     read and be safe to run twice: a second trigger for the same run may run it again. It runs
     once the settle is over, right before the send; a handoff waiting for an agent slot keeps its
     result, a failure too, and does not run it again. A stale state file is caught by part 1,
     which T3 reads fresh. A program that is missing or cannot be run reads as "its program could
     not start" (exit code 126 or 127, as `timeout` reports it on Linux).
   - **The environment** is only `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_ALL`,
     `LC_CTYPE`, `TZ`, `TMPDIR`, the `XDG_*` folders (those the server has) and the two ids, not
     the server's tokens and keys (`HANDOFF_COMMAND_ENV_KEYS` in `handoffCommand.ts`). T3 starts
     the command through `/usr/bin/env -i` with that list, as the runner it shares with wait checks
     always adds to the server's whole environment (upstream's `processRunner.ts`). A program that
     needs more sets it itself. On Windows there is no such program: the command gets the server's
     whole environment there. A program path with `=` in it fails, as `env` would read it as a
     setting.
   - Point `command` at a program outside the repository: one inside it (`./scripts/x`, or a
     script an interpreter is given) can be edited by an agent. A save that sets a new command
     checks the command's words against the disk (`fork/handoff/handoffCommandSaveCheck.ts`). It
     guards against mistakes, not against the person (the setting is theirs alone). It refuses:
     - a program that is not a full path on the server's OS, not a file, or inside a git checkout
       (a folder above it, or above the file a link points to, holds `.git`);
     - for a known interpreter (shells, `python`, `node`, `ruby`, `perl`, `php`, `lua`, `tclsh`,
       `pwsh`, `Rscript --vanilla`): its script (the first argument that is not an option,
       whatever its ending) by a relative path, missing, or inside a checkout, a folder Python
       would run (`__main__.py`) too; and a module found by name (`python -m x`, `node -r x`,
       `--import x`), as that is looked up from the project folder;
     - for any program: an argument ending in a script ending (`.py`, `.sh`, …) by a relative
       path, or by a full path that is missing; a full-path argument that is a file inside a
       checkout;
     - `env` running a program by name (found on the `PATH`), or with `-S` or `-C` however they
       are written (`-S'cmd'`, grouped as `-iS'cmd'`, `--split-string`, `--split=cmd`, `--chdir`);
       otherwise the program after `env`'s options (`-u NAME` and `-a NAME` skip their value) and
       settings is checked as above;
     - programs that run what a file in their working folder says: `make`, `just`, `rake`, `npm`,
       `npx`, `pnpm`, `yarn`, `cargo`, `go`, `gradle`, `mvn`, `ant`, `task`, `tox`, `nox`,
       `poetry`, `pipenv`, `uv`, `composer`; and `bun`, `bunx` and `deno`, whatever script they
       are given, as they read their config from it first (`bunfig.toml`'s `preload` runs project
       code, `deno.json`'s import map changes what a script imports); `R`, whatever it is given,
       and `Rscript` without `--vanilla` among its options (before its script or `-e`), as R
       sources `.Rprofile` and reads `.Renviron` from its working folder before the script.

     It does not catch: code given inline (`sh -c '…'`, `python -c '…'`, which may itself run
     project files); wrappers other than `env` (`nice`, `timeout`, `xargs`, …), whose program is
     not followed; interpreters and runners not named above, or under another name; an option
     that takes a value the check does not know, which can hide the script; a program that reads
     files or config from the project folder by itself (a script that imports a project file,
     `git`, which reads `.git/config`, which can name programs to run); a folder argument to a
     program that is not an interpreter; and anything changed on disk after the save. A command
     already stored at the same place is not checked again, so a file moved since, or a hand
     edit, blocks no other save; it then fails at the handoff as "its program could not start".

   - **`agent`:** once the settle is over, T3 sends the old session one normal turn, the write
     turn: the setting's `instructions` (empty: a built-in text), then "start your reply with this
     line, alone: `[T3 handoff notes]`" and the 32 KB cap. It runs on the old session as it is (no
     model change, no fresh context), so its cache is still warm, with the same check before it as
     the handoff (a thread that moved on drops it) and an agent slot when the server has a gate.
     Its ids are fixed per source run (`server:handoff-write:<thread>:<run>`), so a second trigger
     does not send a second one. The reply, after the marker line, is the handoff, fenced as the
     old session's notes (the last reply that starts with the marker, when the session wrote more
     than one). **The fence:** part 2 goes, as written, between a begin and an end line that carry
     a token of 128 random bits drawn for this message (`----- end handoff from the old session
     <token> -----`), after a line saying the notes end only at the end line with that token. The
     notes were written before the token was drawn, so no line of theirs (a copy of the end line,
     one with a guessed token, look-alike letters of any script, markup, odd line breaks) ends
     them early or fakes another part of the message; notes that happen to hold the token get a
     new one. A handoff sent again (a second trigger, a slot wait) draws a new token; a failed
     handoff's retry resends the same text.
     - **Part 3 leaves out** the write turn's prompt and its marked reply. A message the person
       steered into a write turn, and the answer to it, stay in later handoffs' part 3.
     - **No handoff at all** (the old session carries on with everything it had, and the next turn
       end decides again): a person stopped the write turn, or another message joined it (steered
       in, so the old session answered it there). The handoff's check also drops it when anything
       ran after the write turn.
     - **A write turn that does not count**, which fails as below: it failed; it did not end within
       4 minutes of its request (the idle window it starts in), so T3 stops it (`run.interrupt`
       with T3's own reason, which tells it from a person's Stop) and waits up to a minute more
       for it to end; or its reply has no marker on the first line, is over 32 KB in UTF-8, is
       empty, or is only the marker.
     - **Bounded.** At the size backstop, when the last write turn or handoff of the thread is a
       write turn T3 had to stop, the next handoff skips the write turn and sends parts 1, 3 and
       4 at once, so a slow provider cannot keep a thread growing behind write turn after write
       turn. At the idle trigger a write turn runs again.
     - **Not lost.** The write turn's handoff is sent by whichever loop finds it as the thread's
       latest run: a late update of the source run (a child settling, say), or a restart while
       it runs, starts a new loop, and that loop carries the write turn on to its handoff, with the
       same ids. A write turn that did not stop even after T3's stop sends its handoff when it ends.
       A restart that cuts the write turn short ends it `cancelled`, and upstream continues it in a
       new run (`restartContinuationOfRunId`, with upstream's own "continue" prompt; a
       continuation that fails before it starts is continued again, up to 3 in a row). T3 takes
       that chain of runs as the one write turn: the handoff waits for its last run, judges the
       chain's replies in order without upstream's prompts, and sends with the same ids, once; no
       second write turn starts (`handoffWriteChain` in `handoffFacts.ts`). Each run of the chain
       gets the 4-minute timeout from its own request. A run of the chain that ended cut short
       with no continuation yet is judged only once upstream owes none: while upstream's
       `effect:restart-continuation:<run>` row is pending (written with the cancel at a restart, or
       due after upstream's retry delay for a continuation that failed before its prompt), or
       upstream's `continuationStartRetryDelayMs` says a retry is coming and its row is not written
       yet, the handoff waits for it: for a written row, until upstream's effect worker settles it,
       however late a busy restart makes that (the worker runs a few rows at a time); for a retry
       whose row is not written yet (upstream writes it right after the failure), up to a minute
       past the time it is due. A continuation that still does not come leaves the chain judged as
       it is (the log says so). A row that cannot be read counts as owed, never as "no row": the
       handoff reads it again every 5 seconds, with no time limit, and logs a warning each time, so
       it is never sent without its notes; an outbox that stays unreadable keeps that one handoff
       waiting (other threads go on). The row's id is upstream's own, which upstream does not export:
       `handoffContinuationRow.test.ts` runs upstream's restart writer and checks the handoff finds
       its row.
     - **Quiet.** A write turn that completes is marked quiet as the handoff run is (same rule,
       same `:loud` suffix when the settle saw the relay not show the person's finish), so its end
       does not alert. A write turn started where the fallback follows its failure (the size
       backstop, or a person asking) has `:fallback` in its message id, before `:loud`; it is quiet
       when it fails too, as the fallback, with the reason, follows at once. At the idle trigger
       nothing follows a failed write turn, so it has no `:fallback` and its failure alerts as
       upstream's failed run, besides the server log's "handoff write turn did not count". Found
       after a restart, a `:fallback` write turn gets the fallback. One the person steered into
       alerts. Upstream's alerts for a failed run (the relay's in
       `packages/shared/src/agentAwareness.ts`, the browser's in
       `ThreadNotificationCoordinator.tsx`) honour the mark once their announced seam lines are in
       (`.github/fork-seams.json`); until then any failed write turn still alerts there. Those lines
       also quiet a failed run a person asked to stop, as a completed one already is. A failed
       handoff run is not marked: its retry, or its alert, follows.

   **If the source fails** (for `command`: an exit code other than 0, a timeout, empty output,
   output over the cap, a program that cannot start, a project that cannot be read; for `agent`: a
   write turn that does not count): at the idle trigger the handoff is skipped, and the next turn end
   decides again. At the size backstop T3 hands off with parts 1, 3 and 4 only, and the message
   says that the source failed and why. The server log says so in both cases ("handoff command
   failed", "handoff write turn did not count").

   The command is code that T3 runs, so only the person's settings set it: `handoff` is a
   person-only setting (no T3 tool's input may name it), and a repository's `t3.json` has no
   `handoff`.

3. **The recent conversation**, chosen by upstream's handoff budget with a smaller cap (8,000).
4. **The rules**: this is context, not a request; reply with one line and act on the next message or
   event; older history is in the thread (`t3_thread_read`).

The new session uses `modelSelection` from the setting, else the thread's current model, always
written out in full. The message says when no effort is set.

## After the handoff run

- **Read stays read.** If the person had seen the thread before the handoff, the handoff's own
  completion does not mark it unread.
- **One retry.** A handoff run that fails is sent once more with the same text. If that also fails,
  the text stays in the thread and the server log says so; the next turn end decides again. A
  handoff the person stopped stays stopped: an interrupted handoff run is never sent again.
- **No second alert.** The person's turn alerts once, in the browser and through the relay (phone
  and other devices). A completed handoff run then does not alert again: the thread shell shows it
  as upstream shows a stopped run (`latestRunStopRequested`), so the browser gives no notice and the
  phone's card for the thread is cleared, not shown as finished. It is quiet
  only when the settle saw the relay show the person's finish at every check, and the run was
  requested at least 5 s after the person's run finished, so their alert went out first. Otherwise
  the service sends it as one that alerts: its message id ends in `:loud`, and the run alerts when
  it finishes, as the turn's finish. The mark is the run's own message (its `handoff:` or `handoff-write:` id, sent by
  the server as an agent, with or without `:loud`) and the two runs' times, all stored, so a restart
  keeps it. A failed handoff run, its retry (the last alert said "failed"), a run the person
  steered a message into, and the next real turn alert as before. The rule is
  `fork/handoff/handoffShell.ts`, read in both of the shell's reads (JavaScript and SQL, one line
  each in `ProjectionStore.ts`), and every shell reader, the relay included, gets it from there.

## Limits

- Timers, waits and the settle live in memory. After a server restart, the next turn end, or a late update of
  an older run, decides again, with a fresh settle. A crash between the send and the cancel leaves a queued handoff
  queued; it runs after the newer run.
- A press of Hand off now lives in memory too: a restart before its settle ends forgets it, and
  the person presses again.
- The same run never hands off twice: the check before the send drops a second trigger, and every
  trigger for one run uses the same ids, so one that got past would replay the first. A cancelled
  handoff spends that run's ids; the next run's turn end decides. After a restart, a write turn
  that is still running, or ended less than its 4-minute timeout ago, is carried on to its handoff
  (the same ids, so at most once); one that ended longer ago is left, and nothing more is sent for
  that run.
- A browser that is offline from before the person's turn ends until after the quiet handoff run
  completes (asleep, network drop) gets no notice for that turn when it reconnects: it sees only the
  quiet run. The relay's alert for the turn (phone and other devices) still goes out.

## For developers

- Fork code: `apps/server/src/fork/handoff/` (`HandoffService.ts`, `handoffDispatch.ts`,
  `handoffFacts.ts`, `handoffMessage.ts`, `handoffCommand.ts`, `handoffAgent.ts`, `handoffDecision.ts`,
  `handoffQuiet.ts`,
  `handoffShell.ts`, `handoffRelayView.ts`, `handoffRoutes.ts`). Settings and the routes' shapes:
  `packages/contracts/src/handoff.ts`. The app's parts: `apps/web/src/components/fork/handoff/`
  (the menu item and the chip), each joined to upstream's files by seam lines.
- `Orchestrator.ts` stays upstream's (the fork check refuses edits to it). Every handoff send goes
  through `dispatchHandoff` (`handoffDispatch.ts`), and a test checks that. The service starts from
  `ForkLayers.layerRuntimeServices` (`apps/server/src/fork/forkLayers.ts`), which `server.ts`
  already wires.
- One owner per thread: `handoffOwner.ts` wraps upstream's orchestrator for the runtime's workers
  and skips only an idle compaction send to a thread with handoff on. `IdleCompactionService.ts`
  stays upstream's; `server.ts` gets one fork line,
  `Layer.provide(ForkLayers.handoffThreadsSkipIdleCompaction)`, first in the pipe of
  `layerRuntimeCoreDependenciesBase`, and a test checks it is there. `threadHandoff` (same file) is
  the one place that picks a thread's settings and decides whether its handoff can run, for both the
  wrapper and the service; its instance check, `handoffInstanceBlocked`, also checks a retry's own
  instance. One table of cases (`runnableCases` in `handoff.testkit.ts`) tests both.
- The message reuses upstream's handoff budget (`handoffBudget`) and transcript reading
  (`ContextHandoffService.ts`); nothing is copied.
